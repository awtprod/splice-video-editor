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
