"""Export jobs and delivery: scratch cleanup on cancel and watchdog kills, FFmpeg
supervision, the master limiter on decoded files, and error messages."""
from array import array
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file

POSIX = os.name != 'nt'


def ffmpeg(*arguments, timeout=120):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def probe(path, entries):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', entries, '-of', 'json', str(path)],
                                              timeout=30))


def true_peak(path):
    """Decoded true peak in dBTP (ITU-R BS.1770, as measured by FFmpeg's ebur128)."""
    report = subprocess.run(['ffmpeg', '-nostdin', '-nostats', '-v', 'info', '-i', str(path), '-vn',
                             '-af', 'ebur128=peak=true:framelog=quiet', '-f', 'null', '-'],
                            capture_output=True, text=True, timeout=120).stderr
    return float(re.findall(r'Peak:\s+(-?[\d.]+) dBFS', report[report.rfind('True peak:'):])[0])


FAKE_FFMPEG = r'''#!/bin/sh
# Stand-in FFmpeg for supervision tests: hangs at a chosen export stage.
stage="$SPLICE_TEST_HANG"
for argument in "$@"; do
  case "$stage:$argument" in
    picture:*picture.filtergraph|audio:*/audio-*.filtergraph|mux:*segments.txt)
      : > "$SPLICE_TEST_MARK"; exec sleep 300;;
  esac
done
case "$stage" in
  holdpipes) sleep 30 & echo $! > "$SPLICE_TEST_MARK";;
  frozen)
    for argument in "$@"; do
      if [ "$argument" = "-progress" ]; then
        while :; do printf 'frame=1\nout_time_us=0\ntotal_size=48\nprogress=continue\n'; sleep .3; done
      fi
    done;;
esac
exec "$SPLICE_TEST_REAL_FFMPEG" "$@"
'''


@unittest.skipUnless(POSIX, 'uses a shell stand-in for FFmpeg')
class FakeFFmpeg(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory/'bin'
        self.bin.mkdir()
        script = self.bin/'ffmpeg'
        script.write_text(FAKE_FFMPEG)
        script.chmod(0o755)
        self.mark = self.directory/'hung'
        self.environment = {'PATH': f'{self.bin}{os.pathsep}{os.environ.get("PATH", "")}',
                            'SPLICE_TEST_REAL_FFMPEG': shutil.which('ffmpeg'), 'SPLICE_TEST_MARK': str(self.mark)}

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(MEDIA_ROOT))


class JobCleanup(FakeFFmpeg):
    """Killing an export (cancel or watchdog) must leave no scratch folder behind."""

    def project(self):
        source = self.directory/'cam.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=64x36:r=25:d=4', '-f', 'lavfi', '-i', 'sine=d=4',
               '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(source))
        return dict(width=64, height=36, fps=25, media=[dict(id='c', src=self.src(source))],
                    clips=[dict(type='video', mediaId='c', track=1, start=0, duration=3, **{'in': .5}),
                           dict(type='audio', mediaId='c', track=1, start=0, duration=3, **{'in': .5})])

    def run_until_hung(self, stage, *, stall='60', grace=None, cancel=True):
        from export_jobs import ExportJobs
        import export_jobs
        scratch = self.directory/f'work-{stage}'
        scratch.mkdir()
        project = self.project()
        environment = {**self.environment, 'SPLICE_TEMP_DIR': str(scratch), 'SPLICE_TEST_HANG': stage,
                       'SPLICE_EXPORT_STALL_SECONDS': stall}
        patches = [mock.patch.dict(os.environ, environment)]
        if grace is not None:
            patches.append(mock.patch.object(export_jobs, 'JOB_STALL_GRACE', grace))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.mark.unlink(missing_ok=True)
        jobs = ExportJobs()
        self.addCleanup(jobs.shutdown)
        identifier = jobs.start(project, {'codec': 'h264'})
        deadline = time.monotonic()+60
        while not self.mark.exists() and time.monotonic() < deadline:
            self.assertEqual(jobs.status(identifier)['state'], 'rendering', jobs.status(identifier)['error'])
            time.sleep(.1)
        self.assertTrue(self.mark.exists(), f'the export never reached its {stage} stage')
        self.assertTrue(any(scratch.iterdir()), 'the job made no scratch folder')
        if cancel:
            jobs.status(identifier, cancel=True)
        while jobs.status(identifier)['state'] == 'rendering' and time.monotonic() < deadline:
            time.sleep(.1)
        return jobs.status(identifier), scratch

    def test_cancel_at_each_stage_removes_all_scratch(self):
        for stage in ('picture', 'audio', 'mux'):
            with self.subTest(stage=stage):
                status, scratch = self.run_until_hung(stage)
                self.assertEqual(status['state'], 'cancelled')
                self.assertEqual(sorted(path.name for path in scratch.iterdir()), [])

    def test_watchdog_kill_removes_all_scratch(self):
        # The job watchdog fires (2 s) long before the renderer's own (60 s).
        status, scratch = self.run_until_hung('picture', stall='60', grace=-58, cancel=False)
        self.assertEqual(status['state'], 'failed')
        self.assertIn('no progress', status['error'])
        self.assertEqual(sorted(path.name for path in scratch.iterdir()), [])


class Supervision(FakeFFmpeg):
    def test_pipes_held_by_another_process_do_not_hang_cleanup(self):
        with mock.patch.dict(os.environ, {**self.environment, 'SPLICE_TEST_HANG': 'holdpipes'}):
            began = time.monotonic()
            render_movie._run_ffmpeg(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=d=0.2', '-f', 'null', '-'])
            elapsed = time.monotonic()-began
        holder = int(self.mark.read_text())
        try:
            os.kill(holder, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.assertLess(elapsed, 15)

    def test_repeated_identical_progress_is_not_progress(self):
        cancelled = threading.Event()
        timer = threading.Timer(20, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.dict(os.environ, {**self.environment, 'SPLICE_TEST_HANG': 'frozen', 'SPLICE_EXPORT_STALL_SECONDS': '2'}):
            with self.assertRaisesRegex(TimeoutError, 'no progress for 2 seconds'):
                render_movie._run_ffmpeg(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=d=1', '-f', 'null', '-'],
                                         cancelled)


class Case(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(MEDIA_ROOT))


class LossyLimiter(Case):
    """Finding 6: the decoded MP4 must stay under the ceiling, not just the mix."""

    def program(self, peak):
        source = self.directory/f'program-{peak}.wav'
        ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.45*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*0.5*t))+0.35*sin(2*PI*1375*t)|"
               "0.45*sin(2*PI*330*t)+0.35*sin(2*PI*997*t)*(0.6+0.4*sin(2*PI*0.3*t))':s=48000:d=8",
               '-af', f'volume={peak/0.8:.6f}', '-c:a', 'pcm_f32le', str(source))
        return source

    def export(self, source, codec, volume=100, **options):
        suffix = {'h264': '.mp4', 'prores': '.mov', 'wav': '.wav'}[codec]
        output = self.directory/f'mix-{codec}-{volume}{suffix}'
        render_to_file(dict(width=64, height=36, fps=24, media=[dict(id='p', src=self.src(source))],
                            clips=[dict(type='audio', mediaId='p', track=1, start=0, duration=7, volume=volume, **{'in': .5})]),
                       output, options={'codec': codec, **options})
        return output

    def test_mp4_decoded_true_peak_stays_under_the_ceiling(self):
        full = self.program(1.0)
        for volume in (100, 200):
            with self.subTest(volume=volume):
                self.assertLessEqual(true_peak(self.export(full, 'h264', volume)), -1.0)
        self.assertLessEqual(true_peak(self.export(full, 'h264', limiterCeiling=-3)), -3.0)

    def test_pcm_deliveries_hold_the_sample_peak_ceiling(self):
        full = self.program(1.0)
        for codec in ('prores', 'wav'):
            with self.subTest(codec=codec):
                output = self.export(full, codec, 200)
                samples = array('f', subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output), '-vn',
                                                              '-f', 'f32le', 'pipe:1'], timeout=60))
                self.assertLess(max(abs(sample) for sample in samples), 10**(-1/20)+1e-4)

    def test_lossy_mix_limits_true_peak_and_pcm_mix_limits_sample_peak(self):
        # A 12 kHz tone sampled 45 degrees off its crests: samples peak 3 dB
        # below its true (between-sample) peak.
        source = self.directory/'between.wav'
        ffmpeg('-f', 'lavfi', '-i', "aevalsrc='sin(2*PI*12000*t+PI/4)|sin(2*PI*12000*t+PI/4)':s=48000:d=3",
               '-c:a', 'pcm_f32le', str(source))
        events = [dict(path=source, streams={0: 2}, source=0.0, length=2.0, speed=1.0, reverse=False, start=0.0,
                       processing='anull', envelope=[], stateful=False)]
        peaks = {}
        for true_peak_mode in (True, False):
            chunks = render_movie._render_audio(events, 0, 96000, (-1.0, true_peak_mode), str(self.directory), None, None)
            peaks[true_peak_mode] = true_peak(chunks[0])
        self.assertLessEqual(peaks[True], -1.0)
        self.assertGreater(peaks[False], -.5, 'a sample-peak limiter lets the between-sample peak through')

    def test_lossy_deliveries_use_the_true_peak_limiter(self):
        source = self.program(.5)
        seen = {}
        real = render_movie._render_audio

        def spy(events, first, stop, limiter, *arguments, **keywords):
            seen[codec] = limiter
            return real(events, first, stop, limiter, *arguments, **keywords)
        with mock.patch.object(render_movie, '_render_audio', spy):
            for codec in ('h264', 'prores', 'wav'):
                self.export(source, codec)
        self.assertEqual(seen, {'h264': (-1.0, True), 'prores': (-1.0, False), 'wav': (-1.0, False)})

    def test_limiter_off_leaves_aac_untouched(self):
        # No ceiling, no measuring: the program is encoded once at unity gain.
        full = self.program(.5)
        runs = []
        real = render_movie._run_ffmpeg

        def spy(command, *arguments, **keywords):
            runs.append(command)
            return real(command, *arguments, **keywords)
        with mock.patch.object(render_movie, '_run_ffmpeg', spy):
            self.export(full, 'h264', limiter=False)
        self.assertFalse(any('ebur128' in ' '.join(map(str, command)) for command in runs))
        self.assertFalse(any('volume=' in ' '.join(map(str, command)) and 'aac' in command for command in runs))


class Delivery(Case):
    def test_title_track_follows_its_video_track_visibility(self):
        red = self.directory/'red.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'color=red:s=64x64:r=25:d=1', '-c:v', 'libx264', '-qp', '0', str(red))
        project = dict(width=64, height=64, fps=25, media=[dict(id='red', src=self.src(red))],
                       clips=[dict(type='video', mediaId='red', track=1, start=0, duration=1),
                              dict(type='title', track=4, start=0, duration=1, title='MMMMMMMM',
                                   titleStyle={'color': '#00ff00', 'fontSize': 40, 'y': 50})],
                       trackState={'video:4': {'hidden': True}})
        output = self.directory/'out.mp4'
        render_to_file(project, output)
        raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-ss', '0.5', '-i', str(output), '-frames:v', '1',
                                       '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], timeout=30)
        self.assertLess(sum(raw[1::3])/(64*64), 3, 'a title on a hidden video track was drawn')

    def test_full_range_source_is_delivered_tagged_and_coded_as_video_range(self):
        source = self.directory/'full.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'color=white:s=64x64:r=25:d=1', '-vf', 'format=yuvj420p', '-c:v', 'libx264', '-qp', '0',
               '-color_range', 'pc', str(source))
        output = self.directory/'out.mp4'
        render_to_file(dict(width=64, height=64, fps=25, media=[dict(id='a', src=self.src(source))],
                            clips=[dict(type='video', mediaId='a', track=1, start=0, duration=.4)]), output)
        stream = probe(output, 'stream=color_range')['streams'][0]
        frame = probe(output, 'frame=color_range')['frames'][0]
        self.assertEqual((stream.get('color_range'), frame.get('color_range')), ('tv', 'tv'))
        luma = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output), '-frames:v', '1',
                                        '-vf', 'extractplanes=y', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'], timeout=30)
        self.assertLessEqual(max(luma), 236, 'white must be coded at video level 235')

    def test_over_long_timeline_says_by_how_much(self):
        project = dict(width=64, height=64, fps=25, media=[],
                       clips=[dict(type='title', track=4, start=12*3600-1, duration=1.5, title='x')])
        with self.assertRaisesRegex(ValueError, r'0\.500 seconds longer than the export limit of 12 hours'):
            render_to_file(project, self.directory/'out.mp4')

    @unittest.skipUnless(POSIX and os.geteuid() != 0, 'needs POSIX permissions and a non-root user')
    def test_unwritable_scratch_folder_names_the_setting(self):
        locked = self.directory/'locked'
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        for target in (locked, locked/'inner'):
            with self.subTest(target=target.name):
                with mock.patch.dict(os.environ, {'SPLICE_TEMP_DIR': str(target)}):
                    with self.assertRaisesRegex(ValueError, 'SPLICE_TEMP_DIR'):
                        render_movie.work_root()


if __name__ == '__main__':
    unittest.main()
