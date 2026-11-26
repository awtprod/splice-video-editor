"""Track automation, channel mapping, 5.1 layouts and stems, measured sample by sample on
rendered files (audio finishing items 4 and 5)."""
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
from audio_fixtures import AudioCase, ffmpeg, pcm, tone_level  # noqa: E402

RATE = 48000


def stereo_pan(pan, left, right):
    """Web Audio StereoPannerNode on a stereo input (engine.js / render_movie._stereo_pan)."""
    if pan <= 0:
        x = (pan+1)*math.pi/2
        return left+math.cos(x)*right, math.sin(x)*right
    x = pan*math.pi/2
    return math.cos(x)*left, right+math.sin(x)*left


def curve(points, t, base):
    if not points: return base
    if t <= points[0][0]: return points[0][1]
    for (a, va, ease), (b, vb, _) in zip(points, points[1:]):
        if t <= b:
            u = (t-a)/(b-a)
            if ease: u = u*u*(3-2*u)
            return va+(vb-va)*u
    return points[-1][1]


class Automation(AudioCase):
    def dc(self):
        # A constant level: every output sample is that level times the gain at that sample.
        return self.source('dc.wav', "aevalsrc='0.4|0.4':s=48000", 6)

    def test_track_volume_ramps_are_sample_accurate_linear_and_eased(self):
        points = [(.5, 100, False), (1.5, 20, True), (3, 160, False), (3.5, 160, False), (4.2, 0, True), (5, 100, False)]
        project = {'trackMix': {'-1': {'keyframes': {'volume': [dict(time=t, value=v, **({'ease': True} if e else {})) for t, v, e in points]}}}}
        media = [dict(id='d', src=self.src(self.dc()))]
        clips = [dict(type='audio', mediaId='d', track=-1, start=0, duration=5.52)]
        for chunk in (None, .9):
            with self.subTest(chunk=chunk):
                left = pcm(self.render(clips, media, name=f'auto{chunk}', limiter=False, chunk=chunk, project=project))[0::2]
                self.assertEqual(len(left), round(5.52*RATE))
                worst = max(abs(left[n]-.4*curve(points, n/RATE, 100)/100) for n in range(len(left)))
                self.assertLess(worst, 2e-6)

    def test_clip_keyframes_and_track_automation_multiply(self):
        clip_keys = [dict(time=0, value=100), dict(time=2, value=50, ease=True), dict(time=4, value=100)]
        track_keys = [(1, 100, False), (3, 50, False)]
        project = {'trackMix': {'-1': {'keyframes': {'volume': [dict(time=t, value=v) for t, v, _ in track_keys]}}}}
        media = [dict(id='d', src=self.src(self.dc()))]
        clips = [dict(type='audio', mediaId='d', track=-1, start=1, duration=4, keyframes={'volume': clip_keys})]
        left = pcm(self.render(clips, media, name='both', limiter=False, project=project))[0::2]
        own = [(p['time'], p['value'], p.get('ease', False)) for p in clip_keys]
        for n in range(RATE, 5*RATE, 97):
            t = n/RATE
            self.assertAlmostEqual(left[n], .4*curve(own, t-1, 100)/100*curve(track_keys, t, 100)/100, delta=2e-6)

    def test_track_pan_automation_follows_the_stereo_panner(self):
        tone = self.source('lr.wav', "aevalsrc='0.3|0.2':s=48000", 5)
        keys = [(0, -100, False), (2, 100, True), (4, 0, False)]
        project = {'trackMix': {'-1': {'keyframes': {'pan': [dict(time=t, value=v, **({'ease': True} if e else {})) for t, v, e in keys]}}}}
        samples = pcm(self.render([dict(type='audio', mediaId='t', track=-1, start=0, duration=4.5)], [dict(id='t', src=self.src(tone))],
                                  name='pan', limiter=False, project=project))
        for n in range(0, round(4.5*RATE), 113):
            left, right = stereo_pan(max(-1, min(1, curve(keys, n/RATE, 0)/100)), .3, .2)
            self.assertAlmostEqual(samples[2*n], left, delta=2e-6)
            self.assertAlmostEqual(samples[2*n+1], right, delta=2e-6)

    def test_automation_is_validated(self):
        media = [dict(id='d', src=self.src(self.dc()))]
        clips = [dict(type='audio', mediaId='d', track=-1, start=0, duration=1)]
        for keys, message in (({'volume': [dict(time=0, value=300)]}, 'track volume keyframe value'),
                              ({'gain': []}, 'volume and pan'), ({'pan': [dict(time=1, value=0), dict(time=1, value=5)]}, 'Duplicate'),
                              ({'pan': [dict(time=1, value=0, ease='yes')]}, 'ease')):
            with self.subTest(keys=keys), self.assertRaisesRegex(ValueError, message):
                self.render(clips, media, name='bad', project={'trackMix': {'-1': {'keyframes': keys}}})


class Channels(AudioCase):
    def dual(self):
        # A two-channel file with 500 Hz on the left and 900 Hz on the right (a boom on one, a lav on the other).
        return self.source('dual.wav', "aevalsrc='0.2*sin(2*PI*500*t)|0.3*sin(2*PI*900*t)':s=48000", 4)

    def levels(self, samples):
        return {(frequency, channel): round(tone_level(samples, frequency, 1, 2, channel=channel), 3) for frequency in (500, 900) for channel in (0, 1)}

    def test_channel_maps_route_the_source_channels(self):
        media = [dict(id='d', src=self.src(self.dual()))]
        expected = {'stereo': {(500, 0): .2, (500, 1): 0, (900, 0): 0, (900, 1): .3},
                    'left': {(500, 0): .2, (500, 1): .2, (900, 0): 0, (900, 1): 0},
                    'right': {(500, 0): 0, (500, 1): 0, (900, 0): .3, (900, 1): .3},
                    'swap': {(500, 0): 0, (500, 1): .2, (900, 0): .3, (900, 1): 0},
                    'mono': {(500, 0): .1, (500, 1): .1, (900, 0): .15, (900, 1): .15}}
        for mapping, wanted in expected.items():
            with self.subTest(mapping=mapping):
                samples = pcm(self.render([dict(type='audio', mediaId='d', track=-1, start=0, duration=3, channelMap=mapping)], media,
                                          name=mapping, limiter=False))
                for key, value in wanted.items():
                    self.assertAlmostEqual(self.levels(samples)[key], value, delta=.002, msg=key)
        with self.assertRaisesRegex(ValueError, 'channel mapping'):
            self.render([dict(type='audio', mediaId='d', track=-1, start=0, duration=1, channelMap='rear')], media, name='bad')

    def test_a_mono_track_sums_and_pans_with_the_mono_law(self):
        media = [dict(id='d', src=self.src(self.dual()))]
        samples = pcm(self.render([dict(type='audio', mediaId='d', track=-1, start=0, duration=3, pan=50)], media, name='mono', limiter=False,
                                  project={'trackMix': {'-1': {'channels': 'mono'}}}))
        angle = (.5+1)*math.pi/4
        self.assertAlmostEqual(tone_level(samples, 900, 1, 2, channel=0), .15*math.cos(angle), delta=.002)
        self.assertAlmostEqual(tone_level(samples, 900, 1, 2, channel=1), .15*math.sin(angle), delta=.002)

    def test_selected_microphone_on_mono_track(self):
        media = [dict(id='d', src=self.src(self.dual()))]
        for mapping, selected, rejected in [('left', 500, 900), ('right', 900, 500)]:
            clips = [dict(type='audio', mediaId='d', track=-1, start=0, duration=3, channelMap=mapping)]
            samples = pcm(self.render(clips, media, name=mapping, project={'trackMix': {'-1': {'channels': 'mono'}}}))
            for channel in (0, 1):
                self.assertGreater(tone_level(samples, selected, 1, 2, channel=channel), .1)
                self.assertLess(tone_level(samples, rejected, 1, 2, channel=channel), .001)

    def surround_project(self):
        tones = {route: self.source(f'{route}.wav', f"aevalsrc='0.2*sin(2*PI*{f}*t)|0.2*sin(2*PI*{f}*t)':s=48000", 4)
                 for route, f in (('front', 400), ('center', 700), ('lfe', 60), ('surround', 1300))}
        media = [dict(id=route, src=self.src(path)) for route, path in tones.items()]
        clips = [dict(type='audio', mediaId=route, track=-1-index, start=0, duration=3) for index, route in enumerate(tones)]
        mix = {str(-1-index): {'route': route} for index, route in enumerate(tones)}
        return clips, media, {'trackMix': mix}

    def test_51_layout_puts_each_track_in_smpte_order(self):
        clips, media, project = self.surround_project()
        # Expected level per channel (L R C LFE Ls Rs) for each tone.
        where = {400: (0, 1), 700: (2,), 60: (3,), 1300: (4, 5)}
        for codec in ('wav', 'prores', 'h264'):
            with self.subTest(codec=codec):
                report = {}
                output = self.render(clips, media, codec=codec, name=f'surround-{codec}', limiter=False, audioLayout='5.1', report=report,
                                     project=project)
                layout = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                                                             'stream=channels,channel_layout', '-of', 'json', str(output)]))['streams'][0]
                self.assertEqual((layout['channels'], layout['channel_layout']), (6, '5.1'))
                samples = pcm(output, channels=6)
                tolerance = .02 if codec == 'h264' else .002
                for frequency, channels in where.items():
                    for channel in range(6):
                        if frequency == 60 and codec == 'h264' and channel != 3: continue
                        level = tone_level(samples, frequency, 1, 2, channels=6, channel=channel)
                        # Centre and LFE take the mono sum 0.7071*(L+R) of the (stereo, 0.2 per side) track.
                        expected = (.2 if frequency in (400, 1300) else .2*2*.70710678) if channel in channels else 0
                        self.assertAlmostEqual(level, expected, delta=tolerance, msg=f'{frequency} Hz in channel {channel}')
                self.assertEqual(report['audio']['channels'], 6)

    def test_51_downmix_follows_itu_coefficients(self):
        clips, media, project = self.surround_project()
        samples = pcm(self.render(clips, media, name='downmix', limiter=False, audioLayout='5.1-downmix', project=project))
        # Lo = L + 0.7071 C + 0.7071 Ls (Ro likewise), LFE left out; C carries 0.2828 here.
        for frequency, left, right in ((400, .2, .2), (700, .2, .2), (1300, .2*.70710678, .2*.70710678), (60, 0, 0)):
            self.assertAlmostEqual(tone_level(samples, frequency, 1, 2, channel=0), left, delta=.002)
            self.assertAlmostEqual(tone_level(samples, frequency, 1, 2, channel=1), right, delta=.002)


class Stems(AudioCase):
    def project(self):
        voice = self.source('voice.wav', "aevalsrc='0.2*sin(2*PI*300*t)*(0.6+0.4*sin(2*PI*2*t))|0.2*sin(2*PI*300*t)':s=48000", 9)
        music = self.source('music.wav', "aevalsrc='0.1*sin(2*PI*220*t)+0.05*sin(2*PI*1100*t)|0.1*sin(2*PI*330*t)':s=48000", 9)
        sfx = self.source('sfx.wav', "anoisesrc=c=pink:a=0.1:r=48000:seed=2", 9)
        media = [dict(id='v', src=self.src(voice)), dict(id='m', src=self.src(music)), dict(id='s', src=self.src(sfx))]
        clips = [dict(type='audio', mediaId='v', track=-1, start=.5, duration=6), dict(type='audio', mediaId='v', track=-2, start=3, duration=4, **{'in': 1}),
                 dict(type='audio', mediaId='m', track=-3, start=0, duration=8, fadeIn=1, fadeOut=2),
                 dict(type='audio', mediaId='s', track=-4, start=2.25, duration=1.5, speed=1.3)]
        mix = {'-1': {'stem': 'Dialogue', 'fx': {'eq': {'bands': [{'type': 'highpass', 'freq': 90, 'q': .7}]}, 'comp': {'threshold': -30, 'ratio': 3}}},
               '-2': {'stem': 'Dialogue', 'keyframes': {'volume': [dict(time=3, value=60), dict(time=5, value=120)]}},
               '-3': {'stem': 'Music'}, '-4': {'stem': 'Effects'}}
        return clips, media, {'trackMix': mix, 'masterFx': {'eq': {'bands': [{'type': 'peak', 'freq': 2000, 'q': 1, 'gain': -3}]}}}

    def read(self, archive, name, channels=2):
        target = self.directory/('x-'+name)
        target.write_bytes(archive.read(name))
        return pcm(target, channels)

    def test_stems_sum_to_the_mix_full_length_and_aligned(self):
        clips, media, project = self.project()
        report = {}
        output = self.render(clips, media, codec='stems', name='stems', report=report, project=project, normalize=-20, isoTracks=True)
        with zipfile.ZipFile(output) as archive:
            names = sorted(archive.namelist())
            self.assertEqual(names, ['iso-A1.wav', 'iso-A2.wav', 'iso-A3.wav', 'iso-A4.wav', 'mix.wav', 'report.json',
                                     'stem-01-Dialogue.wav', 'stem-02-Music.wav', 'stem-03-Effects.wav'])
            mix = self.read(archive, 'mix.wav')
            stems = [self.read(archive, name) for name in names if name.startswith('stem-')]
            isos = [self.read(archive, name) for name in names if name.startswith('iso-')]
            self.assertEqual(json.loads(archive.read('report.json')), report['audio'])
        self.assertEqual({len(mix), *map(len, stems), *map(len, isos)}, {2*8*RATE})
        residual = max(abs(sum(values)-mixed) for *values, mixed in zip(*stems, mix))
        peak = max(abs(value) for value in mix)
        self.assertLess(20*math.log10(residual/peak), -90)
        self.assertTrue(report['audio']['stemsSumToMix'])
        self.assertAlmostEqual(report['audio']['output']['integratedLufs'], -20, delta=.2)
        self.assertEqual([stem['tracks'] for stem in report['audio']['stems']], [['A1', 'A2'], ['A3'], ['A4']])

    def test_camera_and_sound_tracks_have_distinct_isos(self):
        picture = self.directory/'camera.mov'
        ffmpeg('-f', 'lavfi', '-i', 'color=s=160x90:d=3', '-f', 'lavfi', '-i', 'sine=f=500:d=3',
               '-c:v', 'libx264', '-c:a', 'pcm_s16le', str(picture))
        sound = self.source('sound.wav', 'sine=f=900', 3)
        media = [dict(id='v', src=self.src(picture)), dict(id='a', src=self.src(sound))]
        clips = [dict(type='video', track=1, mediaId='v', start=0, duration=3, embeddedAudio=True),
                 dict(type='audio', track=-1, mediaId='a', start=0, duration=3)]
        output = self.render(clips, media, codec='stems', name='camera-isos', isoTracks=True)
        with zipfile.ZipFile(output) as archive:
            report = json.loads(archive.read('report.json'))
            self.assertEqual({entry['track']: entry['file'] for entry in report['isos']},
                             {'A1': 'iso-A1.wav', 'V1': 'iso-V1.wav'})
            for label, selected, rejected in [('V1', 500, 900), ('A1', 900, 500)]:
                samples = self.read(archive, f'iso-{label}.wav')
                self.assertGreater(tone_level(samples, selected, 1, 2), .05)
                self.assertLess(tone_level(samples, rejected, 1, 2), .001)

    def test_stems_and_isos_retain_overrange_peaks(self):
        media = [dict(id='hot', src=self.src(self.source('hot.wav', "aevalsrc='0.8*sin(2*PI*500*t)|0.8*sin(2*PI*500*t)':s=48000", 2)))]
        clips = [dict(type='audio', track=-1, mediaId='hot', start=0, duration=2, volume=200)]
        output = self.render(clips, media, codec='stems', isoTracks=True, limiter=True)
        with zipfile.ZipFile(output) as archive:
            for name in ['stem-01-Other.wav', 'iso-A1.wav']:
                self.assertAlmostEqual(max(self.read(archive, name)), 1.6, delta=.002)
            self.assertLess(max(self.read(archive, 'mix.wav')), 1)

    def test_nested_project_with_per_track_isos(self):
        source = self.source('recorder.wav', 'sine=f=500', 2)
        child = dict(width=32, height=18, fps=25, media=[dict(id='a', src=self.src(source))],
                     clips=[dict(type='audio', track=-1, mediaId='a', start=0, duration=2)])
        media = [dict(id='nested', nestedProject=child)]
        clips = [dict(type='video', track=1, mediaId='nested', start=0, duration=2)]
        output = self.render(clips, media, codec='stems', isoTracks=True)
        with zipfile.ZipFile(output) as archive:
            self.assertGreater(max(self.read(archive, 'iso-V1.wav')), .04)
        with self.assertRaisesRegex(ValueError, 'Track isos are part of the stems export'):
            self.render(clips, media, codec='wav', isoTracks=True)

    def test_a_master_compressor_is_named_when_stems_cannot_sum(self):
        clips, media, project = self.project()
        project['masterFx']['comp'] = {'threshold': -30, 'ratio': 4}
        report = {}
        self.render(clips, media, codec='stems', name='stems-comp', report=report, project=project)
        self.assertFalse(report['audio']['stemsSumToMix'])
        self.assertIn('master compressor', report['audio']['stemsNote'])


if __name__ == '__main__':
    unittest.main()
