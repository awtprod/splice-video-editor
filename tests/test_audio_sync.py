"""Dual-system sound sync (audio finishing item 6): waveform offsets on generated takes at
23.976/24/25/29.97 fps from -10 s to +10 s, confidence and refusal, drift detection, timecode
sync from a BWF time reference, and the /api/audio/sync request checks."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from audio_fixtures import camera_take, ffmpeg, speech_like  # noqa: E402
import audio_sync  # noqa: E402

FRAME_RATES = (24000/1001, 24, 25, 30000/1001)


class WaveformSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=__import__('os').environ.get('SPLICE_TEMP_DIR'))
        cls.directory = Path(cls.temp.name)
        cls.recorder = speech_like(cls.directory/'recorder.wav', 45, seed=7)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_offsets_from_minus_ten_to_plus_ten_seconds_at_every_rate(self):
        worst = 0.0
        for index, offset in enumerate((-9.6, -3.217, -.5, 0.0, .0417, 2.75, 7.123, 9.95)):
            fps = FRAME_RATES[index % len(FRAME_RATES)]
            with self.subTest(offset=offset, fps=fps):
                camera = camera_take(self.recorder, self.directory/f'camera{index}.mp4', offset, 20, fps=round(fps))
                result = audio_sync.waveform_offset(camera, self.recorder, picture_seconds=20, search_seconds=12, fps=fps)
                self.assertTrue(result['ok'], result)
                error = result['offsetSamples']-round(offset*48000)
                worst = max(worst, abs(error))
                self.assertLessEqual(abs(error), 4, result)
                self.assertAlmostEqual(result['offsetFrames'], round(offset*fps, 2), delta=.01)
                self.assertGreater(result['confidence'], .8)
                self.assertNotIn('drift', result)
        print(f'\nAUDIO sync worst error {worst} samples at 48 kHz', flush=True)

    def test_a_different_take_is_refused(self):
        other = speech_like(self.directory/'other.wav', 25, seed=99)
        result = audio_sync.waveform_offset(other, self.recorder, search_seconds=12)
        self.assertFalse(result['ok'])
        self.assertLess(result['confidence'], audio_sync.MIN_CONFIDENCE)
        self.assertIn('Not synced', result['note'])

    def test_level_and_scratch_quality_do_not_matter(self):
        quiet = camera_take(self.recorder, self.directory/'quiet.mp4', 4.2, 20, gain_db=-30, hiss=.002)
        result = audio_sync.waveform_offset(quiet, self.recorder, search_seconds=12)
        self.assertTrue(result['ok'], result)
        self.assertLessEqual(abs(result['offsetSamples']-round(4.2*48000)), 6)

    def test_drift_between_sample_clocks_is_detected_not_hidden(self):
        # The recorder's clock runs 400 ppm fast: 24 ms over the minute.
        fast = self.directory/'fast.wav'
        ffmpeg('-i', str(self.recorder), '-af', 'asetrate=48019.2,aresample=48000', '-c:a', 'pcm_s16le', str(fast))
        camera = camera_take(self.recorder, self.directory/'long.mp4', 2.0, 42)
        result = audio_sync.waveform_offset(camera, fast, picture_seconds=42, search_seconds=6, fps=25)
        self.assertTrue(result.get('drift'), result)
        self.assertFalse(result['ok'])
        self.assertAlmostEqual(result['driftPpm'], -400, delta=60)
        self.assertIn('drift', result['note'])

    def test_long_take_drift_both_directions_beyond_ten_ms_and_no_drift_baseline(self):
        recorder = speech_like(self.directory/'long65.wav', 65, seed=19)
        camera = camera_take(recorder, self.directory/'cam60.mp4', 2.0, 60, fps=24, gain_db=-24, hiss=.002)
        # No-drift baseline on a 60 s noisy take must accept cleanly.
        baseline = audio_sync.waveform_offset(camera, recorder, picture_seconds=60, search_seconds=6, fps=24)
        self.assertTrue(baseline['ok'], baseline)
        self.assertNotIn('drift', baseline)
        self.assertAlmostEqual(baseline['driftPpm'], 0.0, delta=15)
        self.assertLessEqual(abs(baseline['offsetSamples']-round(2.0*48000)), 8)
        # Both directions of drift well beyond +-10 ms (24 ms and 60 ms over 60 s).
        for ppm in (-400, 400, -1000, 1000):
            with self.subTest(ppm=ppm):
                rate = 48000/(1+ppm*1e-6)
                drifted = self.directory/f'drifted_{ppm}.wav'
                ffmpeg('-i', str(recorder), '-af', f'asetrate={rate:.4f},aresample=48000', '-c:a', 'pcm_s16le', str(drifted))
                result = audio_sync.waveform_offset(camera, drifted, picture_seconds=60, search_seconds=6, fps=24)
                self.assertTrue(result.get('drift'), result)
                self.assertFalse(result['ok'], result)
                self.assertAlmostEqual(result['driftPpm'], ppm, delta=max(40, abs(ppm)*.08), msg=result)
                self.assertAlmostEqual(result['driftSeconds'], ppm*1e-6*60, delta=.004, msg=result)
                expected_start = 2.0*(1+ppm*1e-6)
                self.assertAlmostEqual(result['offsetSeconds'], expected_start, delta=.004, msg=result)
                self.assertIn('drift', result['note'])

    def test_separated_bursts_keep_sample_alignment(self):
        sound = self.directory/'separated-bursts.wav'
        bursts = 'between(t,0.3,0.8)+between(t,7.1,7.6)+between(t,50,50.7)+between(t,56,56.4)'
        ffmpeg('-f', 'lavfi', '-i', 'anoisesrc=r=8000:d=60:seed=7',
               '-af', f"aeval='val(0)*({bursts})'", str(sound))
        delayed = self.directory/'separated-bursts-delayed.wav'
        ffmpeg('-i', str(sound), '-af', 'adelay=16271S:all=1', str(delayed))
        inverted = self.directory/'separated-bursts-inverted.wav'
        ffmpeg('-i', str(sound), '-af', 'volume=-1', str(inverted))
        for recording, expected_samples in ((sound, 0), (delayed, 16271*6), (inverted, 0)):
            with self.subTest(recording=recording.name):
                result = audio_sync.waveform_offset(sound, recording, search_seconds=6)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['offsetSamples'], expected_samples, result)

    def test_drift_with_hiss_around_quiet_leaders_and_tails(self):
        sound = self.directory/'padded.wav'
        ffmpeg('-i', str(self.recorder), '-af', 'adelay=20000:all=1,apad=pad_dur=20', str(sound))
        camera = camera_take(sound, self.directory/'padded-camera.m4a', 0, 85, video=False, gain_db=-30, hiss=.002)
        for ppm in (0, 400, -400):
            with self.subTest(ppm=ppm):
                recorder = self.directory/f'padded-{ppm}.wav'
                ffmpeg('-i', str(sound), '-af', f'asetrate={48000/(1+ppm*1e-6):.6f},aresample=48000', str(recorder))
                result = audio_sync.waveform_offset(camera, recorder, search_seconds=6, fps=24)
                self.assertEqual(result['ok'], ppm == 0, result)
                self.assertEqual(bool(result.get('drift')), ppm != 0, result)
                self.assertAlmostEqual(result['driftPpm'], ppm, delta=50)
                self.assertAlmostEqual(result['offsetSeconds'], 0, delta=.003)

    def test_first_24_seconds_of_dialogue_can_establish_drift_before_a_silent_tail(self):
        sound = self.directory/'dialogue-tail.wav'
        ffmpeg('-i', str(self.recorder), '-af', 'atrim=duration=24,apad=whole_dur=55', str(sound))
        camera = camera_take(sound, self.directory/'dialogue-tail.m4a', 0, 55, video=False)
        fast = self.directory/'dialogue-tail-fast.wav'
        ffmpeg('-i', str(sound), '-af', 'asetrate=48019.2,aresample=48000', str(fast))
        for recorder, ppm in ((sound, 0), (fast, -400)):
            with self.subTest(ppm=ppm):
                result = audio_sync.waveform_offset(camera, recorder, search_seconds=6)
                self.assertEqual(result['ok'], ppm == 0, result)
                self.assertAlmostEqual(result['driftPpm'], ppm, delta=50, msg=result)

    def test_long_take_with_one_short_shared_passage_needs_confirmation(self):
        sound = self.directory/'short-passage.wav'
        ffmpeg('-i', str(self.recorder), '-af', 'atrim=duration=5,adelay=22000:all=1,apad=whole_dur=50', str(sound))
        result = audio_sync.waveform_offset(sound, sound, search_seconds=6)
        self.assertGreater(result['confidence'], .9, result)
        self.assertFalse(result['ok'], result)
        self.assertNotIn('driftPpm', result)
        self.assertIn('verify clock drift', result['note'])

    def test_drifting_recorder_at_the_minimum_overlap(self):
        fast = self.directory/'forty-fast.wav'
        ffmpeg('-i', str(self.recorder), '-af', 'atrim=duration=40,asetrate=48019.2,aresample=48000', str(fast))
        result = audio_sync.waveform_offset(self.recorder, fast, picture_seconds=40, search_seconds=6)
        self.assertFalse(result['ok'], result)
        self.assertTrue(result.get('drift'), result)
        self.assertAlmostEqual(result['driftPpm'], -400, delta=60)

    def test_short_and_long_unrelated_recordings_are_refused_without_drift(self):
        short = speech_like(self.directory/'short.wav', 1.2, seed=31)
        too_short = audio_sync.waveform_offset(short, self.recorder, search_seconds=6)
        self.assertFalse(too_short['ok'])
        self.assertIn('under 2 seconds', too_short['note'])
        other_a = speech_like(self.directory/'unrelated_a.wav', 50, seed=101)
        other_b = speech_like(self.directory/'unrelated_b.wav', 50, seed=202)
        unrelated = audio_sync.waveform_offset(other_a, other_b, picture_seconds=50, search_seconds=6)
        self.assertFalse(unrelated['ok'])
        self.assertNotIn('drift', unrelated)
        self.assertIn('Not synced', unrelated['note'])

    def test_differing_sample_rates_sync_cleanly_and_catch_drift(self):
        recorder = speech_like(self.directory/'sr_rec.wav', 56, seed=23, rate=48000)
        cam_441 = self.directory/'sr_cam_441.wav'
        ffmpeg('-ss', '1.25', '-t', '50', '-i', str(recorder), '-ar', '44100', '-c:a', 'pcm_s16le', str(cam_441))
        ok_res = audio_sync.waveform_offset(cam_441, recorder, picture_seconds=50, search_seconds=6, fps=24)
        self.assertTrue(ok_res['ok'], ok_res)
        self.assertNotIn('drift', ok_res)
        self.assertAlmostEqual(ok_res['offsetSeconds'], 1.25, delta=.0005)
        self.assertAlmostEqual(ok_res['driftPpm'], 0.0, delta=15)
        drift_441 = self.directory/'sr_drift_441.wav'
        ffmpeg('-i', str(recorder), '-af', 'asetrate=47980.8,aresample=44100', '-c:a', 'pcm_s16le', str(drift_441))
        bad_res = audio_sync.waveform_offset(cam_441, drift_441, picture_seconds=50, search_seconds=6, fps=24)
        self.assertTrue(bad_res.get('drift'), bad_res)
        self.assertFalse(bad_res['ok'], bad_res)
        self.assertAlmostEqual(bad_res['driftPpm'], 400, delta=45)


class TimecodeSync(unittest.TestCase):
    def test_bwf_time_reference_against_the_picture_timecode(self):
        from media_proxy import describe_path
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # Recorder rolled at 10:00:00:00; the camera at 10:00:03:12 (25 fps).
            recorder = directory/'bwf.wav'
            ffmpeg('-f', 'lavfi', '-i', 'sine=f=440:d=10:r=48000', '-write_bext', '1', '-metadata', f'time_reference={10*3600*48000}',
                   '-c:a', 'pcm_s24le', str(recorder))
            camera = directory/'tc.mov'
            ffmpeg('-f', 'lavfi', '-i', 'color=gray:s=32x18:r=25:d=2', '-timecode', '10:00:03:12', '-c:v', 'libx264', str(camera))
            sound, picture = describe_path(recorder), describe_path(camera)
            self.assertEqual((sound['timeReference'], sound['audioSampleRate']), (10*3600*48000, 48000))
            result = audio_sync.timecode_offset(picture, sound, fps=25)
            self.assertTrue(result['ok'])
            self.assertAlmostEqual(result['offsetSeconds'], 3+12/25, places=6)
            self.assertEqual(result['offsetFrames'], 87)

    def test_drop_frame_labels(self):
        self.assertAlmostEqual(audio_sync.timecode_seconds('00:01:00;02', 30000/1001), 1800*1001/30000, places=9)
        self.assertAlmostEqual(audio_sync.timecode_seconds('00:10:00;00', 30000/1001), 17982*1001/30000, places=9)
        for text, rate in [('garbage', 25), ('22:75:00:00', 25), ('00:00:00:25', 25),
                           ('00:01:00;00', 30000/1001), ('00:00:00:00', float('nan'))]:
            self.assertIsNone(audio_sync.timecode_seconds(text, rate))

    def test_requests_are_validated(self):
        for payload, message in (({}, '1 to 40'), ({'pairs': [{'picture': 'a'}]}, 'names a picture and a sound'),
                                 ({'pairs': [{'picture': 'a', 'sound': 'b'}], 'method': 'magic'}, 'sync method'),
                                 ({'pairs': [{'picture': 'a', 'sound': 'b'}], 'searchSeconds': 0}, 'search range'),
                                 ({'pairs': [{'picture': 'a', 'sound': 'b', 'pictureStream': -1}]}, 'audio stream')):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                audio_sync.sync_pairs(payload)


if __name__ == '__main__':
    unittest.main()
