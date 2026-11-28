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
