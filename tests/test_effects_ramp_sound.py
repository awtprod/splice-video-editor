"""Speed-ramp sound: pitch-preserving playback (the default) lasts the whole clip and puts every
click where the curve puts it, on every Inspector preset and an eased ramp; varispeed stays exact;
a freeze is silent in both modes.

The source is a click track (a 2 ms 3 kHz burst every 100 ms of source) over a quiet 220 Hz bed, so
the clicks give timing and the bed shows where sound stops.
"""
import array
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import premiere  # noqa: E402
import render_movie  # noqa: E402
import effects_engine as E  # noqa: E402
from test_effects import js, pcm  # noqa: E402

RATE = 48000
FPS = 25
AUDIO_FRAME = 1024          # one AAC frame: how close to the clip's end the sound must reach
CLICK_LEVEL = 0.3           # the bed is 0.05, a click 0.8
TOLERANCE = 0.005           # each click lands within 5 ms of the curve
IN = 0.53            # the freeze preset then holds between two clicks, not on one


def click_times(source_low, source_high):
    """Source seconds of the clicks between the two source times."""
    first = math.ceil(source_low*10-1e-9)
    return [k/10 for k in range(first, int(source_high*10)+1) if source_low <= k/10 < source_high]


def landing(points, source):
    """Clip-local time t where the ramp has played ``source`` seconds (the first such t)."""
    low, high = 0.0, 7200.0
    while E.ramp_source(points, high) < source:
        high *= 2
    for _ in range(80):
        middle = (low+high)/2
        if E.ramp_source(points, middle) < source:
            low = middle
        else:
            high = middle
    return high


def onset(samples, at, reach):
    """First sample in at±reach (seconds) louder than a click's threshold, in seconds, or None."""
    low, high = max(0, int((at-reach)*RATE)), min(len(samples), int((at+reach)*RATE))
    return next((i/RATE for i in range(low, high) if abs(samples[i]) > CLICK_LEVEL), None)


def onsets(samples, gap=0.008):
    found, i = [], 0
    while i < len(samples):
        if abs(samples[i]) > CLICK_LEVEL:
            found.append(i/RATE)
            i += int(gap*RATE)
        else:
            i += 1
    return found


class StretchAlong(unittest.TestCase):
    """The grain stretcher alone, at constant speeds: exact length, clicks on time, a steady tone."""
    def stretch(self, source, speed, count):
        return E._stretch_along(source, 1, len(source), count, RATE, lambda n: n*speed, lambda n: speed, lambda n: True)

    def test_clicks_land_on_the_curve_and_a_tone_stays_steady(self):
        clicks = array.array('f', bytes(4*2*RATE))
        times = [0.05+0.1*k+0.0013*(k % 5) for k in range(19)]
        for at in times:
            start = int(round(at*RATE))
            for j in range(96):
                clicks[start+j] = 0.8*math.sin(2*math.pi*3000*j/RATE)
        tone = array.array('f', [0.5*math.sin(2*math.pi*220*i/RATE) for i in range(2*RATE)])
        for speed in (0.25, 0.5, 0.8, 1.5, 2.5, 4.0):
            count = int(1.9/speed*RATE)
            out = self.stretch(clicks, speed, count)
            self.assertEqual(len(out), count)
            expected = [at/speed for at in times if at/speed < count/RATE-0.01]
            for at in expected:
                found = onset(out, at, 0.012)
                self.assertIsNotNone(found, f'{speed}x: no click at {at:.3f}s')
                self.assertLess(abs(found-at), TOLERANCE, f'{speed}x: click at {found:.4f}s, the curve puts it at {at:.4f}s')
            for found in onsets(out):
                self.assertLess(min(abs(found-at/speed) for at in times), TOLERANCE, f'{speed}x: a stray click at {found:.4f}s')
            steady = self.stretch(tone, speed, count)
            window = RATE//100
            for start in range(window, count-2*window, window):
                rms = math.sqrt(sum(v*v for v in steady[start:start+window])/window)
                self.assertAlmostEqual(20*math.log10(rms/(0.5/math.sqrt(2))), 0, delta=1.5,
                                       msg=f'{speed}x: the tone dips at {start/RATE:.3f}s')


def envelope(samples, start, width=48):
    """Peak level of each ``width``-sample block from ``start`` (silence past the end)."""
    return [max(map(abs, samples[i:i+width]), default=0.0) for i in range(start, len(samples), width)]


def rms(samples):
    return math.sqrt(sum(v*v for v in samples)/max(1, len(samples)))


def frequency(samples):
    """Mean frequency from the rising zero crossings (interpolated) of a steady tone."""
    crossings = [i+a/(a-b) for i, (a, b) in enumerate(zip(samples, samples[1:])) if a < 0 <= b]
    return (len(crossings)-1)*RATE/(crossings[-1]-crossings[0])


def tone_snr(samples, f):
    """Fundamental against everything else (dB): the median over 200 ms blocks of a least-squares
    sine fit at ``f``."""
    block = int(round(round(0.2*f)*RATE/f))
    values = []
    for start in range(0, len(samples)-block, block):
        x = samples[start:start+block]
        c = sum(v*math.cos(2*math.pi*f*i/RATE) for i, v in enumerate(x))*2/block
        s = sum(v*math.sin(2*math.pi*f*i/RATE) for i, v in enumerate(x))*2/block
        fundamental = (c*c+s*s)/2
        values.append(10*math.log10(fundamental/max(sum(v*v for v in x)/block-fundamental, 1e-14)))
    return sorted(values)[len(values)//2]


class StretchQuality(unittest.TestCase):
    """The pitch-preserving stretch keeps a transient single and its decay as loud as the curve
    puts it at slow motion, keeps pitch down to 45 Hz, stays clean at fast speeds and plays
    nothing of a click before it."""
    def stretch(self, source, speed, count, offset=0.0):
        return E._stretch_along(source, 1, len(source), count, RATE, lambda n: offset+n*speed, lambda n: speed, lambda n: True)

    def fresh_onsets(self, samples, at, peak, span=0.040):
        """Times (s) within ``span`` after ``at`` where the 1 ms envelope rises to half of ``peak``
        again after falling below a quarter of it (a second attack after a gap)."""
        found, gap = [], False
        for k, level in enumerate(envelope(samples[:int((at+span)*RATE)], int(at*RATE))):
            if level < peak/4:
                gap = True
            elif gap and level >= peak/2:
                found.append(at+k/1000)
                gap = False
        return found

    def test_slow_motion_plays_each_transient_once_and_keeps_its_decay(self):
        import random
        noise = random.Random(5)
        # Decaying noise hits (1 ms attack, 40 ms decay) over a quiet noise bed, and clicks.
        hits = array.array('f', [noise.uniform(-.09, .09) for _ in range(5*RATE)])
        clicks = array.array('f', bytes(4*5*RATE))
        onsets = [0.2+0.4*k for k in range(11)]
        for at in onsets:
            start = int(at*RATE)
            for i in range(int(.25*RATE)):
                hits[start+i] += noise.uniform(-1, 1)*min(1, i/RATE/.001)*math.exp(-i/RATE/.04)*.8
            for i in range(96):
                clicks[start+i] = 0.8*math.sin(2*math.pi*3000*i/RATE)
        for speed in (0.1, 0.25):
            count = int(min(4.4/speed, 9.0)*RATE)
            played = [at/speed for at in onsets if at/speed+0.15 < count/RATE]
            out = self.stretch(clicks, speed, count)
            for at in played:
                self.assertEqual(self.fresh_onsets(out, at+0.002, 0.8), [], f'{speed}x: a click plays again')
            out = self.stretch(hits, speed, count)
            for at in played:
                start = next(i for i in range(int(at*RATE), count) if abs(out[i]) > 0.3)
                self.assertLess(abs(start/RATE-at), 0.008, f'{speed}x: the hit lands at {start/RATE:.4f}s, the curve puts it at {at:.4f}s')
                self.assertEqual(self.fresh_onsets(out, start/RATE+0.002, 0.8), [], f'{speed}x: a hit at {at:.3f}s plays again')
                # 4 ms RMS of the output against the source around the curve's position, for 120 ms.
                for k in range(1, 30):
                    a = start+k*192
                    level = rms(out[a:a+192])
                    source = rms(hits[int(a*speed)-96:int((a+192)*speed)+96])
                    self.assertGreater(20*math.log10(level/source), -6,
                                       f'{speed}x: {k*4} ms after the hit at {at:.3f}s the sound is {20*math.log10(level/source):.1f} dB below the source')

    def test_pitch_holds_from_45_to_440_hz(self):
        for f in (45, 60, 110, 220, 440):
            tone = array.array('f', [0.5*math.sin(2*math.pi*f*i/RATE) for i in range(int(13*RATE))])
            for speed in (0.25, 0.5, 2, 4, 8):
                out = self.stretch(tone, speed, int(1.5*RATE), offset=0.2*RATE)
                found = frequency(out[int(.2*RATE):int(1.3*RATE)])
                self.assertLess(abs(found-f), 2, f'{f} Hz at {speed}x plays at {found:.1f} Hz')
                if speed >= 4 and f >= 110:
                    snr = tone_snr(out[int(.15*RATE):int(1.35*RATE)], f)
                    self.assertGreater(snr, 25, f'{f} Hz at {speed}x: the fundamental is only {snr:.1f} dB over the artefacts')

    def test_nothing_of_a_click_plays_before_it(self):
        clicks = array.array('f', bytes(4*7*RATE))
        onsets = [0.2+0.4*k for k in range(16)]
        for at in onsets:
            for i in range(192):
                clicks[int(at*RATE)+i] = 0.8*math.sin(2*math.pi*3000*i/RATE)
        for speed in (3.0, 4.0, 8.0):
            count = int(6.2/speed*RATE)
            out = self.stretch(clicks, speed, count)
            for at in onsets:
                land = int(at/speed*RATE)
                if land+RATE//100 > count:
                    break
                before = max(map(abs, out[land-RATE//200:land-RATE//4000]))
                self.assertLess(20*math.log10(max(before, 1e-9)/.8), -30, f'{speed}x: sound {land/RATE:.4f}s before the click')
                self.assertGreater(max(map(abs, out[land:land+RATE//1000])), 0.6, f'{speed}x: the click plays at full level')


def amplitude(samples, f):
    """Least-squares amplitude of a sine at ``f`` Hz."""
    c = sum(v*math.cos(2*math.pi*f*i/RATE) for i, v in enumerate(samples))*2/len(samples)
    s = sum(v*math.sin(2*math.pi*f*i/RATE) for i, v in enumerate(samples))*2/len(samples)
    return math.hypot(c, s)


class Varispeed(unittest.TestCase):
    """Varispeed resamples along the curve band-limited to the new rate: content pushed above the
    output's Nyquist frequency is removed, not folded back as an audible alias."""
    def play(self, source, place, speed, count):
        blocks = []
        E._varispeed(E._SourceReader(1, len(source), RATE, data=source), count, RATE, place, speed, blocks.append)
        out = array.array('f')
        for block in blocks:
            out.extend(block)
        self.assertEqual(len(out), count)
        return out

    def test_a_speed_up_does_not_alias(self):
        # 9 kHz at 4x is 36 kHz (above 24 kHz): linear interpolation folded it to a full-level 12 kHz.
        source = array.array('f', [0.25*math.sin(2*math.pi*9000*i/RATE)+0.25*math.sin(2*math.pi*1000*i/RATE) for i in range(4*RATE)])
        out = self.play(source, lambda n: 4*n, lambda n: 4.0, int(0.8*RATE))
        window = out[RATE//10:RATE//10+RATE//2]
        self.assertLess(20*math.log10(amplitude(window, 12000)/0.25), -60, 'the 9 kHz tone aliases to 12 kHz')
        self.assertAlmostEqual(20*math.log10(amplitude(window, 4000)/0.25), 0, delta=0.5, msg='1 kHz plays as 4 kHz')
        # A ramp from 1x to 6x: everything above the moving cutoff goes, in-band stays.
        ramp = lambda n: n+2.5*n*n/RATE      # speed 1+5n/RATE over one second
        out = self.play(source, ramp, lambda n: 1+5*n/RATE, RATE)
        late = out[int(.7*RATE):int(.9*RATE)]   # 4.5x-5.5x: 9 kHz is 40-50 kHz, folding to 0-8 kHz
        spectrum = rms(late)
        tone = 0.25/math.sqrt(2)
        self.assertLess(abs(20*math.log10(spectrum/tone)), 1.0, 'only the 1 kHz tone (now 4.5-5.5 kHz) is left')


PEAK_SCRIPT = r"""
import resource, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import effects_engine as E
mode, seconds, media, work = sys.argv[2], float(sys.argv[3]), sys.argv[4], Path(sys.argv[5])
points = [{'time': 0, 'value': 400}, {'time': 3600, 'value': 400}]
event = {'ramp': {'ramp': E.Ramp({}, points), 'before': 0.0, 'origin': 0.0, 'mode': mode}, 'streams': {0: 2},
         'rates': {0: 48000}, 'path': media, 'processing': 'anull', 'envelope': [], 'post': '', 'start': 0.0}
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
done = E.prerender_ramp(event, 0.0, seconds, work/f'{mode}-{seconds:g}.wav', work, None, None)
print((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss-base)/1024, Path(done['path']).stat().st_size)
"""


class RampSoundMemory(unittest.TestCase):
    """A ramp's sound is rendered a window at a time: peak memory does not grow with the clip
    (a minute at 8x held 1.2 GB once)."""
    def test_peak_memory_does_not_grow_with_the_clip(self):
        with tempfile.TemporaryDirectory(prefix='splice-ramp-memory-') as temp:
            media = Path(temp)/'long.wav'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=f=220:r=48000:d=100',
                            '-ac', '2', '-c:a', 'pcm_s16le', str(media)], check=True, timeout=120)
            for mode, short, long in (('pitch', 5, 24), ('varispeed', 3, 12)):
                peaks = []
                for seconds in (short, long):
                    result = subprocess.run([sys.executable, '-c', PEAK_SCRIPT, str(ROOT), mode, str(seconds), str(media), temp],
                                            capture_output=True, text=True, timeout=300, check=True)
                    growth, size = result.stdout.split()
                    peaks.append(float(growth))
                    # 32-bit float stereo of the whole span, as the mix reads it.
                    self.assertGreater(int(size), seconds*48000*8)
                # Holding the 96 s of source the long run reads would add 50 MB and more.
                self.assertLess(peaks[1], peaks[0]+8, f'{mode}: peak memory grew from {peaks[0]:.0f} to {peaks[1]:.0f} MB')
                self.assertLess(peaks[1], 64, f'{mode}: {peaks[1]:.0f} MB at peak')
            self.assertEqual(sorted(path.name for path in Path(temp).iterdir() if path.suffix == '.f32'), [], 'scratch files are removed')


class RampSoundRenders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-ramp-sound-')
        cls.media = Path(cls.temp.name)/'media'
        cls.media.mkdir()
        cls.patches = [mock.patch.object(premiere, 'MEDIA_ROOT', cls.media.resolve()),
                       mock.patch.object(render_movie, 'MEDIA_ROOT', cls.media.resolve())]
        for patch in cls.patches:
            patch.start()
        sound = "0.05*sin(2*PI*220*t)+if(lt(mod(t+0.00001,0.1),0.002),0.8*sin(2*PI*3000*t),0)"
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=gray:s=64x36:r={FPS}:d=12',
                        '-f', 'lavfi', '-i', f"aevalsrc='{sound}':s={RATE}:d=12", '-c:v', 'libx264', '-qp', '0', '-g', '1',
                        '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-shortest', str(cls.media/'clicks.mov')],
                       check=True, timeout=120)
        # A constant level: a freeze that holds a sample instead of going silent shows as that level.
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=gray:s=64x36:r={FPS}:d=6',
                        '-f', 'lavfi', '-i', f"aevalsrc='0.5':s={RATE}:d=6", '-c:v', 'libx264', '-qp', '0', '-g', '1',
                        '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-shortest', str(cls.media/'level.mov')],
                       check=True, timeout=120)
        cls.item = {'id': 'clicks', 'name': 'clicks.mov', 'src': '/media/clicks.mov', 'type': 'video', 'duration': 12}
        cls.level = {'id': 'level', 'name': 'level.mov', 'src': '/media/level.mov', 'type': 'video', 'duration': 6}

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        cls.temp.cleanup()

    def render(self, keys, duration, mode, name, media='clicks'):
        clip = {'id': 'c', 'type': 'video', 'mediaId': media, 'track': 1, 'start': 0, 'duration': duration, 'in': IN,
                'transition': 'none', 'keyframes': {'speed': keys}, 'effects': {'speedAudio': mode}}
        out = Path(self.temp.name)/f'{name}-{mode}.mov'
        render_movie.render_to_file({'name': 'r', 'width': 64, 'height': 36, 'fps': FPS, 'media': [self.item, self.level], 'clips': [clip]},
                                    out, options={'codec': 'prores'})
        return pcm(out)

    def check(self, keys, duration, label):
        points = E.ordered_points(keys)
        played = E.ramp_source(points, duration)
        # Clicks the curve plays (the first and last 10 ms are left out: a grain there is half outside).
        every = [landing(points, at-IN) for at in click_times(IN, IN+played)]
        expected = [at for at in every if 0.01 < at < duration-0.01 and E.ramp_speed(points, at) >= E.RAMP_HELD_SPEED]
        held = [(a['time'], b['time']) for a, b in zip(points, points[1:]) if a['value'] == 0 and a.get('interp') == 'hold']
        for mode, tolerance in (('pitch', TOLERANCE), ('varispeed', 0.0005)):
            samples = self.render(keys, duration, mode, label)
            count = round(duration*RATE)
            self.assertLessEqual(abs(len(samples)-count), AUDIO_FRAME, f'{label} {mode}: {len(samples)} samples for {count}')
            # The bed sounds to the clip's end (unless the ramp ends in a freeze).
            last = next(i for i in range(len(samples)-1, -1, -1) if abs(samples[i]) > 0.01)
            self.assertGreaterEqual(last, count-AUDIO_FRAME, f'{label} {mode}: sound stops at {last/RATE:.3f}s of {duration:.3f}s')
            errors = []
            for at in expected:
                found = onset(samples, at, 0.012)
                self.assertIsNotNone(found, f'{label} {mode}: no click at {at:.3f}s')
                errors.append(found-at)
                self.assertLess(abs(found-at), tolerance, f'{label} {mode}: click at {found:.4f}s, the curve puts it at {at:.4f}s')
            for found in onsets(samples):
                self.assertLess(min(abs(found-at) for at in every), tolerance, f'{label} {mode}: a stray click at {found:.4f}s')
            for a, b in held:
                middle = samples[int((a+0.03)*RATE):int((b-0.03)*RATE)]
                self.assertLess(max(map(abs, middle)), 1e-3, f'{label} {mode}: a freeze is silent')

    def test_every_preset_and_an_eased_ramp(self):
        for name in ('smoothSlowMo', 'fastToSlow', 'speedUp', 'freeze'):
            with self.subTest(preset=name):
                preset, = js(('presetRamp', name, 3))
                self.check(preset['keys'], math.floor(preset['duration']*FPS)/FPS, name)
        eased = [{'time': 0, 'value': 100, 'interp': 'easeInOut'}, {'time': 1, 'value': 250, 'interp': 'easeInOut'},
                 {'time': 2, 'value': 40}, {'time': 3, 'value': 100}]
        self.check(eased, 3.0, 'eased')

    def test_a_freeze_is_silent_not_a_held_sample(self):
        # Varispeed reads the source at a standstill during a freeze; a constant source shows a held
        # sample as its level. The freeze starts 21 ms into an audio block (the gate is read per block).
        keys = [{'time': 0, 'value': 100, 'interp': 'hold'}, {'time': 0.501, 'value': 0, 'interp': 'hold'},
                {'time': 1.2, 'value': 100}, {'time': 2.0, 'value': 100}]
        for mode in ('varispeed', 'pitch'):
            samples = self.render(keys, 2.0, mode, 'freeze-level', media='level')
            self.assertGreater(min(map(abs, samples[int(0.1*RATE):int(0.45*RATE)])), 0.45, f'{mode}: the level plays')
            self.assertLess(max(map(abs, samples[int(0.56*RATE):int(1.15*RATE)])), 1e-3, f'{mode}: a freeze is silent')
            self.assertGreater(min(map(abs, samples[int(1.26*RATE):int(1.95*RATE)])), 0.45, f'{mode}: the level plays again')

    def test_a_clip_split_at_a_cut_sounds_as_it_did_whole(self):
        # Rendered apart, each piece restarted its stretch (or atempo) at the cut: a step of 0.23
        # (ramp) or 0.08 (constant speed) on a 0.25 tone. Joined, the split clip is the whole clip.
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=gray:s=64x36:r={FPS}:d=8',
                        '-f', 'lavfi', '-i', f'sine=f=220:r={RATE}:d=8', '-c:v', 'libx264', '-qp', '0', '-g', '1', '-pix_fmt', 'yuv420p',
                        '-c:a', 'pcm_s16le', '-shortest', str(self.media/'tone.mov')], check=True, timeout=120)
        tone = {'id': 'tone', 'name': 'tone.mov', 'src': '/media/tone.mov', 'type': 'video', 'duration': 8}
        keys = [{'time': 0, 'value': 100, 'interp': 'easeInOut'}, {'time': 1, 'value': 40, 'interp': 'easeInOut'},
                {'time': 2, 'value': 160, 'interp': 'easeInOut'}, {'time': 3, 'value': 100}]
        cut, duration, source_in = 1.48, 3.0, 0.5
        first, second = js(('windowPoints', keys, 0, cut), ('windowPoints', keys, cut, duration))
        second_in = source_in+E.ramp_source(E.ordered_points(keys), cut)

        def render(clips, name):
            out = Path(self.temp.name)/f'{name}.mov'
            render_movie.render_to_file({'name': 'r', 'width': 64, 'height': 36, 'fps': FPS, 'media': [tone], 'clips': clips},
                                        out, options={'codec': 'prores'})
            return pcm(out)
        base = {'type': 'video', 'mediaId': 'tone', 'track': 1, 'transition': 'none'}
        at = round(cut*RATE)
        for mode in ('pitch', 'varispeed'):
            whole = render([{**base, 'id': 'a', 'start': 0, 'duration': duration, 'in': source_in, 'keyframes': {'speed': keys},
                             'effects': {'speedAudio': mode}}], f'whole-{mode}')
            split = render([{**base, 'id': 'a', 'start': 0, 'duration': cut, 'in': source_in, 'keyframes': {'speed': first},
                             'effects': {'speedAudio': mode}},
                            {**base, 'id': 'b', 'start': cut, 'duration': duration-cut, 'in': second_in, 'keyframes': {'speed': second},
                             'effects': {'speedAudio': mode}}], f'split-{mode}')
            self.assertLess(max(abs(a-b) for a, b in zip(whole[at-4800:at+4800], split[at-4800:at+4800])), 1e-3, f'{mode}: the split clip differs')
            self.assertLess(max(abs(split[j]-split[j-1]) for j in range(at-3, at+4)), 0.01, f'{mode}: a click at the cut')
        constant = {**base, 'speed': 0.5}
        whole = render([{**constant, 'id': 'a', 'start': 0, 'duration': duration, 'in': source_in}], 'whole-constant')
        split = render([{**constant, 'id': 'a', 'start': 0, 'duration': cut, 'in': source_in},
                        {**constant, 'id': 'b', 'start': cut, 'duration': duration-cut, 'in': source_in+cut*0.5}], 'split-constant')
        self.assertLess(max(abs(a-b) for a, b in zip(whole[at-4800:at+4800], split[at-4800:at+4800])), 1e-3, 'constant speed: the split clip differs')
        # A piece with its own level is its own sound.
        louder = render([{**constant, 'id': 'a', 'start': 0, 'duration': cut, 'in': source_in},
                         {**constant, 'id': 'b', 'start': cut, 'duration': duration-cut, 'in': source_in+cut*0.5, 'volume': 50}], 'split-level')
        self.assertAlmostEqual(20*math.log10(rms(louder[at+4800:at+9600])/rms(louder[at-9600:at-4800])), -6.02, delta=0.3)

    def test_varispeed_whose_first_sample_precedes_the_source(self):
        # A 8.1 ms crossfade pre-roll from source 0 starts the read 389 samples before the file: the
        # first sample in range is not on a 1 ms gate block (an UnboundLocalError once).
        keys = [{'time': 0, 'value': 100}, {'time': 1, 'value': 50}]
        a = {'id': 'A', 'type': 'video', 'mediaId': 'level', 'track': 1, 'start': 0, 'duration': 1, 'in': 1, 'transition': 'none'}
        b = {'id': 'B', 'type': 'video', 'mediaId': 'level', 'track': 1, 'start': 1, 'duration': 1, 'in': 0, 'transition': 'dissolve',
             'transitionDuration': 0.04, 'transitionBefore': 0.0081, 'keyframes': {'speed': keys}, 'effects': {'speedAudio': 'varispeed'}}
        out = Path(self.temp.name)/'preroll.mov'
        render_movie.render_to_file({'name': 'r', 'width': 64, 'height': 36, 'fps': FPS, 'media': [self.level], 'clips': [a, b]},
                                    out, options={'codec': 'prores'})
        samples = pcm(out)
        self.assertGreater(min(map(abs, samples[int(1.1*RATE):int(1.9*RATE)])), 0.45, 'the ramped clip sounds')


if __name__ == '__main__':
    unittest.main()
