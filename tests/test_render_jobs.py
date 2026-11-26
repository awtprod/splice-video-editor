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
import warnings

from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file

POSIX = os.name != 'nt'


def ffmpeg(*arguments, timeout=120):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def probe(path, entries):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', entries, '-of', 'json', str(path)],
                                              timeout=30))


def measure(path):
    """Decoded integrated loudness (LUFS, to 0.001) and true peak (dBTP, to 0.1), ITU-R BS.1770 as measured by
    FFmpeg's ebur128."""
    log = Path(path).with_name(Path(path).name+'.r128.txt')
    report = subprocess.run(['ffmpeg', '-nostdin', '-nostats', '-v', 'info', '-i', str(path), '-vn', '-af',
                             'ebur128=peak=true:framelog=quiet:metadata=1,ametadata=mode=print:file='
                             + render_movie._filter_path(log), '-f', 'null', '-'],
                            capture_output=True, text=True, timeout=120).stderr
    integrated = float(re.findall(r'lavfi\.r128\.I=(-?[\d.]+)', log.read_text())[-1])
    log.unlink()
    summary = report[report.rfind('Integrated loudness:'):]
    return integrated, float(re.findall(r'Peak:\s+(-?[\d.]+) dBFS', summary[summary.rfind('True peak:'):])[0])


def true_peak(path):
    return measure(path)[1]


def alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as stat:
            return stat.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


FAKE_FFMPEG = r'''#!/bin/sh
# Stand-in FFmpeg for supervision tests: hangs at a chosen export stage.
stage="$SPLICE_TEST_HANG"
for argument in "$@"; do
  case "$stage:$argument" in
    picture:*picture.filtergraph|audio:*/audio-*.filtergraph|mux:*segments.txt)
      : > "$SPLICE_TEST_MARK"; exec sleep 300;;
    hangchild:*picture.filtergraph)
      # A process FFmpeg started keeps its pipes open; FFmpeg itself hangs.
      sleep 300 & echo $! > "$SPLICE_TEST_MARK"; exec sleep 300;;
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

    def assertGone(self, pid):
        deadline = time.monotonic()+5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(.1)
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
            self.fail('a process FFmpeg started outlived the export job')

    def test_processes_ffmpeg_started_die_with_the_job(self):
        # Round 2, finding 3: after the renderer's own stall limit killed FFmpeg,
        # a process FFmpeg had started (holding its pipes) outlived the job.
        for cancel, stall in ((False, '2'), (True, '60')):
            with self.subTest(cancel=cancel):
                status, scratch = self.run_until_hung('hangchild', stall=stall, cancel=cancel)
                self.assertEqual(status['state'], 'cancelled' if cancel else 'failed')
                self.assertEqual(sorted(path.name for path in scratch.iterdir()), [])
                self.assertGone(int(self.mark.read_text()))
                scratch.rmdir()

    def test_shutdown_kills_a_stuck_job_and_what_it_started(self):
        from export_jobs import ExportJobs
        scratch = self.directory/'work-shutdown'
        scratch.mkdir()
        with mock.patch.dict(os.environ, {**self.environment, 'SPLICE_TEMP_DIR': str(scratch), 'SPLICE_TEST_HANG': 'hangchild',
                                          'SPLICE_EXPORT_STALL_SECONDS': '600'}):
            self.mark.unlink(missing_ok=True)
            jobs = ExportJobs()
            identifier = jobs.start(self.project(), {'codec': 'h264'})
            deadline = time.monotonic()+60
            while not (self.mark.exists() and self.mark.read_text().strip()) and time.monotonic() < deadline:
                time.sleep(.1)
            holder = int(self.mark.read_text())
            jobs.shutdown()
        self.assertNotEqual(jobs.status(identifier)['state'], 'rendering')
        self.assertGone(holder)

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

    def export(self, source, codec, volume=100, report=None, name='mix', duration=7, **options):
        suffix = {'h264': '.mp4', 'prores': '.mov', 'wav': '.wav'}[codec]
        output = self.directory/f'{name}-{codec}-{volume}{suffix}'
        render_to_file(dict(width=64, height=36, fps=24, media=[dict(id='p', src=self.src(source))],
                            clips=[dict(type='audio', mediaId='p', track=1, start=0, duration=duration, volume=volume, **{'in': .5})]),
                       output, options={'codec': codec, **options}, report=report)
        return output

    def test_aac_peak_safe_keeps_the_decoded_true_peak_under_the_ceiling(self):
        # Opt-in (aacPeakSafe): the whole program is lowered until the decoded
        # AAC fits, and the applied gain is reported.
        full = self.program(1.0)
        for volume, ceiling in ((100, -1.0), (200, -1.0), (100, -3.0)):
            with self.subTest(volume=volume, ceiling=ceiling):
                report = {}
                with warnings.catch_warnings(record=True) as notices:
                    warnings.simplefilter('always')
                    output = self.export(full, 'h264', volume, report=report, aacPeakSafe=True, limiterCeiling=ceiling)
                self.assertLessEqual(true_peak(output), ceiling)
                gain = report['audio']['gainDb']
                self.assertEqual(report['audio']['aacPeakSafe'], True)
                self.assertAlmostEqual(report['audio']['decodedTruePeakDb'], true_peak(output), delta=.15)
                self.assertEqual(gain % render_movie.AAC_PEAK_STEP_DB, 0)
                if gain:
                    self.assertTrue(any(f'{-gain:.1f} dB' in str(notice.message) for notice in notices), [str(n.message) for n in notices])

    def test_pcm_deliveries_hold_the_sample_peak_ceiling(self):
        full = self.program(1.0)
        for codec in ('prores', 'wav'):
            with self.subTest(codec=codec):
                output = self.export(full, codec, 200)
                samples = array('f', subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output), '-vn',
                                                              '-f', 'f32le', 'pipe:1'], timeout=60))
                self.assertLess(max(abs(sample) for sample in samples), 10**(-1/20)+1e-4)

    def test_every_delivery_limits_the_true_peak(self):
        # Audio finishing, item 2: a full-scale 12 kHz tone sampled 45 degrees
        # off its crests: samples at -3 dBFS, true peak 0 dBTP. A sample-peak
        # limiter at -1 dBFS never acted on it, so PCM deliveries left at 0 dBTP;
        # every container now limits the 4x oversampled peak.
        source = self.directory/'between.wav'
        ffmpeg('-f', 'lavfi', '-i', "aevalsrc='sin(2*PI*12000*t+PI/4)|sin(2*PI*12000*t+PI/4)':s=48000:d=8",
               '-c:a', 'pcm_f32le', str(source))
        self.assertGreater(true_peak(source), -.5)
        for codec in ('prores', 'wav'):
            with self.subTest(codec=codec):
                report = {}
                output = self.export(source, codec, report=report)
                self.assertLessEqual(true_peak(output), -1.0)
                self.assertLessEqual(report['audio']['output']['truePeakDbtp'], -1.0)
                self.assertTrue(report['audio']['limiter']['acted'])

    def test_every_delivery_uses_the_one_true_peak_stage(self):
        source = self.program(.5)
        seen = {}
        real = render_movie.audio_loudness.finish

        def spy(*arguments, **keywords):
            seen[codec] = keywords['ceiling']
            return real(*arguments, **keywords)
        with mock.patch.object(render_movie.audio_loudness, 'finish', spy):
            for codec in ('h264', 'prores', 'wav'):
                self.export(source, codec)
        self.assertEqual(seen, {'h264': -1.0, 'prores': -1.0, 'wav': -1.0})

    def test_limiter_off_leaves_aac_untouched(self):
        # No ceiling: the program is encoded once at unity gain (and measured for the report).
        full = self.program(.5)
        runs = []
        real = render_movie._run_ffmpeg

        def spy(command, *arguments, **keywords):
            runs.append(command)
            return real(command, *arguments, **keywords)
        with mock.patch.object(render_movie, '_run_ffmpeg', spy):
            self.export(full, 'h264', limiter=False)
        self.assertEqual(sum('aac' in command for command in runs), 1)
        self.assertFalse(any('volume=' in ' '.join(map(str, command)) and 'aac' in command for command in runs))


class ProgramLoudness(Case):
    """Round 2, finding 2: by default an MP4 export never changes program
    loudness. The true-peak limiter acts before the encoder; the decoded AAC
    true peak is measured once and reported, with a warning when AAC overshoot
    puts it over the ceiling."""
    program = LossyLimiter.program
    export = LossyLimiter.export

    def make(self, name, graph, *, loudness=None, peak=None, click=False):
        source = self.directory/f'{name}.wav'
        raw = self.directory/f'{name}-raw.wav'
        ffmpeg('-f', 'lavfi', '-i', graph, '-t', '12', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_f32le', str(raw))
        integrated, true = measure(raw)
        gain = loudness-integrated if loudness is not None else peak-true
        chain = f'volume={gain:.4f}dB'
        if click:
            chain += ",aeval='if(between(t,6,6+1/48000),1,val(ch))':c=stereo"   # one full-scale sample
        ffmpeg('-i', str(raw), '-af', chain, '-c:a', 'pcm_f32le', str(source))
        return source

    def programs(self):
        tones = ("aevalsrc='0.45*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*0.5*t))+0.35*sin(2*PI*1375*t)|"
                 "0.45*sin(2*PI*330*t)+0.35*sin(2*PI*997*t)*(0.6+0.4*sin(2*PI*0.3*t))':s=48000:d=12")
        speech = ("anoisesrc=c=pink:a=0.5:r=48000:seed=7,bandpass=f=900:w=1400,"
                  "aeval='val(0)*pow(max(0,sin(2*PI*3.7*t)),2)*gt(sin(2*PI*0.31*t),-0.4)|"
                  "val(0)*pow(max(0,sin(2*PI*3.7*t+0.3)),2)*gt(sin(2*PI*0.31*t),-0.4)':c=stereo")
        dense = ("aevalsrc='0.2*sin(2*PI*55*t)+0.15*sin(2*PI*110*t)*(0.5+0.5*sin(2*PI*2*t))+0.12*sin(2*PI*440*t)"
                 "+0.1*sin(2*PI*659*t)+0.08*sin(2*PI*2637*t)*(0.5+0.5*sin(2*PI*4*t))+0.25*(random(0)-0.5)|"
                 "0.2*sin(2*PI*55*t+1)+0.15*sin(2*PI*165*t)+0.12*sin(2*PI*554*t)+0.1*sin(2*PI*880*t)"
                 "+0.08*sin(2*PI*3520*t)+0.25*(random(1)-0.5)':s=48000:d=12")
        dialogue = ("anoisesrc=c=pink:a=0.5:r=48000:seed=3,bandpass=f=900:w=1400,"
                    "aeval='val(0)*pow(max(0,sin(2*PI*3.7*t)),2)|val(0)*pow(max(0,sin(2*PI*3.7*t+0.3)),2)':c=stereo")
        return {'music mastered to -1.2 dBTP': self.make('music', tones, peak=-1.2),
                'speech bursts at -3 dBTP': self.make('speech', speech, peak=-3),
                'dense music at -14 LUFS': self.make('dense', dense, loudness=-14),
                'dialogue at -23 LUFS with one full-scale click': self.make('dialogue', dialogue, loudness=-23, click=True)}

    def test_default_mp4_has_the_loudness_of_the_wav_and_reports_its_peak(self):
        overs = 0
        for label, source in self.programs().items():
            with self.subTest(program=label):
                wav_loudness, _ = measure(self.export(source, 'wav', duration=11))
                report = {}
                with warnings.catch_warnings(record=True) as notices:
                    warnings.simplefilter('always')
                    movie = self.export(source, 'h264', duration=11, report=report)
                loudness, peak = measure(movie)
                # Both are limited by the same true-peak stage now (the WAV keeps the source's
                # loudness to 0.01 LU); what is left is the AAC encoder's own loss on the
                # noise-heavy dense program, 0.30 LU at 192 kbit/s.
                self.assertLessEqual(abs(loudness-wav_loudness), .32, f'MP4 {loudness} LUFS, WAV {wav_loudness} LUFS')
                audio = report['audio']
                self.assertEqual((audio['gainDb'], audio['aacPeakSafe'], audio['ceilingDb']), (0, False, -1.0))
                self.assertAlmostEqual(audio['decodedTruePeakDb'], peak, delta=.15)
                self.assertAlmostEqual(audio['loudnessLufs'], loudness, delta=.15)
                messages = [str(notice.message) for notice in notices]
                if audio['decodedTruePeakDb'] > -1.0:
                    overs += 1
                    self.assertEqual(len(messages), 1, messages)
                    self.assertIn(f"{audio['decodedTruePeakDb']:.1f}".replace('-', '\u2212'), messages[0])
                    self.assertIn('aacPeakSafe', messages[0])
                else:
                    self.assertEqual(messages, [])
        self.assertGreater(overs, 0, 'no program put AAC overshoot over the ceiling: the warning went untested')

    def test_the_warning_threshold_is_the_ceiling(self):
        # A program 3 dB under the ceiling: the limiter is idle and the decoded
        # peak does not depend on the ceiling. Set the ceiling a quarter dB under
        # the decoded peak: the export must warn, and aacPeakSafe must fit it.
        source = self.make('quiet', "aevalsrc='0.45*sin(2*PI*220*t)+0.35*sin(2*PI*1375*t)|0.45*sin(2*PI*330*t)"
                                    "+0.35*sin(2*PI*997*t)':s=48000:d=12", peak=-6)
        report = {}
        self.export(source, 'h264', report=report)
        decoded = report['audio']['decodedTruePeakDb']
        ceiling = round(decoded-.25, 2)
        self.assertLess(-6, ceiling)
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter('always')
            report = {}
            movie = self.export(source, 'h264', report=report, limiterCeiling=ceiling)
        self.assertAlmostEqual(report['audio']['decodedTruePeakDb'], decoded, delta=.05)
        self.assertGreater(true_peak(movie), ceiling)
        self.assertEqual(len(notices), 1)
        report = {}
        movie = self.export(source, 'h264', report=report, limiterCeiling=ceiling, aacPeakSafe=True, name='safe')
        self.assertLessEqual(true_peak(movie), ceiling)
        self.assertLess(report['audio']['gainDb'], 0)

    def test_silent_program_reports_null_peak_and_loudness(self):
        # The export job status is sent as strict JSON: no infinities.
        silence = self.directory/'silence.wav'
        ffmpeg('-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '8', '-c:a', 'pcm_f32le', str(silence))
        for options in ({}, {'aacPeakSafe': True}):
            with self.subTest(**options):
                report = {}
                self.export(silence, 'h264', report=report, **options)
                # FFmpeg's ebur128 reports silence as -inf dBTP and -70 LUFS (its gate floor).
                self.assertEqual((report['audio']['decodedTruePeakDb'], report['audio']['gainDb']), (None, 0))
                json.dumps(report, allow_nan=False)

    def test_aac_peak_safe_is_deterministic_and_range_exports_use_the_same_rule(self):
        source = self.program(1.0)
        runs = []
        for attempt in range(2):
            report = {}
            movie = self.export(source, 'h264', 160, report=report, aacPeakSafe=True, name=f'run{attempt}')
            runs.append((report['audio'], subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(movie),
                                                                    '-vn', '-f', 'f32le', 'pipe:1'], timeout=60)))
        self.assertEqual(runs[0], runs[1])
        report = {}
        ranged = self.export(source, 'h264', 160, report=report, aacPeakSafe=True, name='range', range={'start': 2, 'end': 5})
        self.assertLessEqual(true_peak(ranged), -1.0)
        self.assertEqual(report['audio']['gainDb'] % render_movie.AAC_PEAK_STEP_DB, 0)


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


class Python310(Case):
    """Splice supports Python 3.10; Fraction accepts format specs only from 3.12."""

    def test_exports_format_no_fraction_with_a_format_spec(self):
        from fractions import Fraction

        def format_as_on_python_310(value, spec):
            if spec:
                raise TypeError(f'unsupported format string passed to Fraction.__format__: {spec!r}')
            return str(value)
        source = self.directory/'cam.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=64x36:r=30000/1001:d=6', '-f', 'lavfi', '-i', 'sine=f=300:d=6',
               '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(source))
        fps = 24000/1001
        clips = [dict(type='video', mediaId='c', track=1, start=0, duration=2, **{'in': .5}),
                 dict(type='video', mediaId='c', track=1, start=2, duration=1.5, **{'in': 1}, reverse=True, transition='dissolve',
                      transitionDuration=.5, transitionBefore=.25, keyframes={'opacity': [dict(time=0, value=100), dict(time=1, value=40)]}),
                 dict(type='video', mediaId='c', track=2, start=1, duration=2, **{'in': 2}, speed=1.5, transition='wipe',
                      transitionDuration=.3, keyframes={'rotation': [dict(time=0, value=0), dict(time=2, value=30)]}),
                 dict(type='audio', mediaId='c', track=1, start=0, duration=3.5, **{'in': .5}, speed=1.25),
                 dict(type='title', track=4, start=.5, duration=2, title='Ten')]
        project = dict(width=64, height=36, fps=fps, media=[dict(id='c', src=self.src(source))], clips=clips,
                       captions=[dict(start=.2, duration=1, text='Hello')])
        with mock.patch.object(Fraction, '__format__', format_as_on_python_310), \
                mock.patch.multiple(render_movie, VIDEO_WINDOW_SECONDS=1, AUDIO_CHUNK_SECONDS=1.3):
            for codec, suffix in (('h264', '.mp4'), ('prores', '.mov'), ('wav', '.wav')):
                for span in (None, {'start': 13/fps, 'end': 61/fps}):
                    with self.subTest(codec=codec, range=bool(span)):
                        output = self.directory/f'out{suffix}'
                        render_to_file(project, output, options={'codec': codec, **({'range': span} if span else {})})
                        self.assertGreater(output.stat().st_size, 0)


if __name__ == '__main__':
    unittest.main()
