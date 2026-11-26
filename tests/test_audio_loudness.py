"""Loudness delivery: normalization targets, the true-peak limiter and the audio report,
measured on rendered files (audio finishing items 1 and 2).

The limiter's result is checked with an independent 4x true-peak measurement (SoX resampler,
not the limiter's own libswresample detector) and with FFmpeg's ebur128."""
import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from audio_fixtures import AudioCase, independent_true_peak, loudness, pcm, speech_like  # noqa: E402

DIALOGUE = ("anoisesrc=c=pink:a=0.5:r=48000:seed=3,bandpass=f=900:w=1400,"
            "aeval='val(0)*pow(max(0,sin(2*PI*3.7*t)),2)|val(0)*pow(max(0,sin(2*PI*3.7*t+0.3)),2)':c=stereo,volume=12dB")
MUSIC = ("aevalsrc='0.3*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*0.4*t))+0.15*sin(2*PI*660*t)+0.05*sin(2*PI*3000*t)*(0.5+0.5*sin(2*PI*3*t))|"
         "0.3*sin(2*PI*165*t)+0.15*sin(2*PI*880*t)*(0.6+0.4*sin(2*PI*0.25*t))':s=48000")


class Normalization(AudioCase):
    def program(self, graph=DIALOGUE, name='dialogue.wav', seconds=12):
        source = self.source(name, graph, seconds)
        return [dict(id='p', src=self.src(source))], [dict(type='audio', mediaId='p', track=-1, start=0, duration=seconds-1, **{'in': .5})]

    def test_default_export_leaves_loudness_and_samples_unchanged(self):
        media, clips = self.program()
        report = {}
        off = self.render(clips, media, name='off', limiter=False)
        default = self.render(clips, media, name='default', report=report)
        self.assertEqual(pcm(off), pcm(default), 'a program under the ceiling must pass bit for bit')
        self.assertIsNone(report['audio'].get('normalization'))
        self.assertEqual(report['audio']['gainDb'], 0.0)
        self.assertFalse(report['audio']['limiter']['acted'])

    def test_every_target_is_reached_linearly_with_the_range_unchanged(self):
        # A program whose peak-to-loudness ratio fits every target under -1 dBTP.
        media, clips = self.program(MUSIC+',volume=-18dB', 'quiet-music.wav')
        source_i, source_lra, _ = loudness(self.render(clips, media, name='source', limiter=False))
        for target in ('streaming', 'podcast', 'ebu-r128', 'atsc-a85', 'cinema-dialogue', -20.5):
            with self.subTest(target=target):
                report = {}
                output = self.render(clips, media, name=f'n{target}', report=report, normalize=target)
                integrated, lra, _ = loudness(output)
                wanted = {'streaming': -14, 'podcast': -16, 'ebu-r128': -23, 'atsc-a85': -24, 'cinema-dialogue': -27}.get(target, target)
                self.assertAlmostEqual(integrated, wanted, delta=.2)
                self.assertAlmostEqual(lra, source_lra, delta=.15, msg='linear normalization changed the loudness range')
                self.assertLessEqual(independent_true_peak(output), -1.0+.02)
                audio = report['audio']
                self.assertTrue(audio['normalization']['reached'])
                self.assertAlmostEqual(audio['gainDb'], wanted-source_i, delta=.15)
                self.assertFalse(audio['limiter']['acted'])

    def test_linear_mode_stops_at_the_ceiling_and_says_so(self):
        media, clips = self.program()
        report = {}
        output = self.render(clips, media, name='short', report=report, normalize=-12)
        normalization = report['audio']['normalization']
        self.assertFalse(normalization['reached'])
        self.assertIn('short of the target', normalization['note'])
        self.assertLessEqual(independent_true_peak(output), -1.0+.02)
        self.assertLess(loudness(output)[0], -12.5)

    def test_limiting_mode_reaches_the_target_under_the_ceiling(self):
        media, clips = self.program()
        report = {}
        output = self.render(clips, media, name='limited', report=report, normalize=-12, normalizeMode='limit')
        self.assertAlmostEqual(loudness(output)[0], -12, delta=.2)
        self.assertLessEqual(independent_true_peak(output), -1.0+.02)
        self.assertTrue(report['audio']['limiter']['acted'])
        self.assertTrue(report['audio']['normalization']['reached'])

    def test_every_container_reports_input_gain_output_peak_and_range(self):
        media, clips = self.program()
        for codec in ('wav', 'prores', 'h264'):
            with self.subTest(codec=codec):
                report = {}
                output = self.render(clips, media, codec=codec, name=f'r-{codec}', report=report, normalize=-23)
                audio = report['audio']
                for part in ('input', 'output'):
                    self.assertEqual(set(audio[part]), {'integratedLufs', 'loudnessRangeLu', 'truePeakDbtp'})
                integrated, lra, _ = loudness(output)
                self.assertAlmostEqual(audio['output']['integratedLufs'], integrated, delta=.06)
                self.assertAlmostEqual(audio['output']['loudnessRangeLu'], lra, delta=.11)
                self.assertAlmostEqual(audio['output']['truePeakDbtp'], independent_true_peak(output), delta=.1)
                self.assertAlmostEqual(integrated, -23, delta=.25 if codec == 'h264' else .2)
                json.dumps(audio, allow_nan=False)

    def test_silence_is_reported_without_infinities_and_not_normalized(self):
        media, clips = self.program('anullsrc=r=48000:cl=stereo', 'silence.wav')
        report = {}
        self.render(clips, media, name='silent', report=report, normalize=-23)
        audio = report['audio']
        self.assertIsNone(audio['output']['integratedLufs'])
        self.assertFalse(audio['normalization']['reached'])
        json.dumps(audio, allow_nan=False)

    def test_options_are_validated_with_units(self):
        media, clips = self.program()
        for options, message in (({'normalize': -60}, r'LUFS'), ({'normalize': 'loud'}, 'loudness target'),
                                 ({'normalize': -23, 'normalizeMode': 'auto'}, 'normalization mode'),
                                 ({'normalize': -23, 'normalizeMode': 'limit', 'limiter': False}, 'needs the limiter'),
                                 ({'audioLayout': '7.1'}, 'audio layout')):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                self.render(clips, media, name='bad', **options)


class TruePeakLimiter(AudioCase):
    def single(self, name, graph, seconds=6, **options):
        source = self.source(name, graph, seconds)
        clips = [dict(type='audio', mediaId='s', track=-1, start=0, duration=seconds-1, **{'in': .5})]
        report = {}
        output = self.render(clips, [dict(id='s', src=self.src(source))], name=name.split('.')[0]+'-out', report=report, **options)
        return output, report['audio'], source

    def test_a_single_full_scale_click_is_held_under_the_ceiling(self):
        # Dialogue at -23 LUFS with one full-scale sample: a sample-peak limiter left +1.1 dBTP.
        graph = DIALOGUE + ",volume=-12dB,aeval='if(between(t,3,3+1/48000),1,val(ch))':c=stereo"
        output, audio, _ = self.single('click.wav', graph)
        self.assertLessEqual(independent_true_peak(output), -1.0)
        self.assertLessEqual(loudness(output)[2], -1.0)
        self.assertLessEqual(audio['output']['truePeakDbtp'], -1.0)
        self.assertTrue(audio['limiter']['acted'])
        self.assertEqual(audio['limiter']['regions'], 1)
        # Away from the click the program is untouched, sample for sample.
        unlimited = pcm(self.render([dict(type='audio', mediaId='s', track=-1, start=0, duration=5, **{'in': .5})],
                                    [dict(id='s', src=self.src(self.directory/'click.wav'))], name='click-off', limiter=False))
        limited = pcm(output)
        far = [index for index in range(0, len(limited), 7) if abs(index/2/48000-2.5) > 1.2]
        self.assertTrue(all(limited[index] == unlimited[index] for index in far))

    def test_inter_sample_peaks_of_a_45_degree_tone(self):
        # A 0 dBFS tone at a quarter of the sample rate (12 kHz here, 11.025 kHz at 44.1 kHz)
        # sampled 45 degrees off its crests: every sample at -3 dBFS, true peak 0 dBTP.
        output, audio, source = self.single('isp.wav', "aevalsrc='sin(2*PI*12000*t+PI/4)|sin(2*PI*12000*t+PI/4)':s=48000")
        samples = pcm(source)
        self.assertLess(max(abs(value) for value in samples[:96000]), .7072)
        self.assertGreater(independent_true_peak(source), -.3)
        self.assertLessEqual(independent_true_peak(output), -1.0)
        self.assertLessEqual(audio['output']['truePeakDbtp'], -1.0)

    def test_every_pcm_container_holds_the_true_peak_ceiling(self):
        for codec in ('wav', 'prores'):
            for ceiling in (-1.0, -2.0, -6.0):
                with self.subTest(codec=codec, ceiling=ceiling):
                    source = self.source('loud.wav', MUSIC+',volume=8dB', 6)
                    report = {}
                    output = self.render([dict(type='audio', mediaId='s', track=-1, start=0, duration=5)], [dict(id='s', src=self.src(source))],
                                         codec=codec, name=f'loud-{codec}{ceiling}', report=report, limiterCeiling=ceiling)
                    self.assertLessEqual(independent_true_peak(output), ceiling+.02)
                    self.assertAlmostEqual(report['audio']['output']['truePeakDbtp'], independent_true_peak(output), delta=.05)

    def test_speech_program_peaks_only_where_the_limiter_acts(self):
        source = self.directory/'speech.wav'
        speech_like(source, 8, seed=3)
        clips = [dict(type='audio', mediaId='s', track=-1, start=0, duration=7, volume=200)]
        report = {}
        output = self.render(clips, [dict(id='s', src=self.src(source))], name='speech', report=report,
                             project={'trackMix': {'-1': {'gain': 200}}})
        self.assertLessEqual(independent_true_peak(output), -1.0)
        self.assertGreater(report['audio']['limiter']['regions'], 1)


class AnalyzeProgram(AudioCase):
    def test_analysis_measures_the_mix_and_its_range(self):
        source = self.source('dialogue.wav', DIALOGUE, 12)
        media = [dict(id='p', src=self.src(source))]
        clips = [dict(type='audio', mediaId='p', track=-1, start=0, duration=11, volume=70, **{'in': .5})]
        report = {}
        output = self.render(clips, media, codec='loudness', name='analysis', report=report)
        result = json.loads(Path(output).read_text())
        self.assertEqual(result, report['audio'])
        mix = self.render(clips, media, name='mix', limiter=False)
        integrated, lra, _ = loudness(mix)
        program = result['program']
        self.assertAlmostEqual(program['integratedLufs'], integrated, delta=.06)
        self.assertAlmostEqual(program['loudnessRangeLu'], lra, delta=.11)
        self.assertAlmostEqual(program['truePeakDbtp'], independent_true_peak(mix), delta=.1)
        self.assertGreaterEqual(program['maxShortTermLufs'], integrated-1)
        self.assertGreaterEqual(program['maxMomentaryLufs'], program['maxShortTermLufs'])
        ranged = json.loads(Path(self.render(clips, media, codec='loudness', name='ranged', range={'start': 2, 'end': 6})).read_text())
        self.assertEqual((ranged['startSeconds'], ranged['seconds']), (2.0, 4.0))


if __name__ == '__main__':
    unittest.main()
