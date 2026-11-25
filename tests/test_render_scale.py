"""Feature-scale export: limits, bounded work, graph files, watchdog and scratch space.

The feature-length checks are slow. Run them with SPLICE_SLOW_TESTS=1.
"""
from array import array
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file

SLOW = os.environ.get('SPLICE_SLOW_TESTS') == '1'


def ffmpeg(*arguments, timeout=300):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def probe(path, entries):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', entries, '-of', 'json', str(path)],
                                              timeout=30))


def pcm(path, start, duration):
    return array('f', subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(start), '-i', str(path),
                                               '-t', str(duration), '-vn', '-ac', '2', '-f', 'f32le', 'pipe:1'], timeout=120))


def peak(samples):
    return max((abs(sample) for sample in samples), default=0)


class ScaleCase(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(MEDIA_ROOT))

    def source(self, name, duration, frequency, size='32x18', rate=24):
        path = self.directory / name
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s={size}:r={rate}:d={duration}', '-f', 'lavfi',
               '-i', f'sine=frequency={frequency}:sample_rate=48000:duration={duration}',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(rate*2), '-c:a', 'aac', '-b:a', '64k', '-shortest', str(path))
        return path


class FrameSizes(ScaleCase):
    def test_dci_4k_vertical_uhd_and_8k_sequences_render(self):
        source = self.source('card.mp4', 1, 440, size='320x180', rate=25)
        for width, height, codec in ((4096, 2160, 'h264'), (2160, 3840, 'h264'), (8192, 4320, 'h264')):
            with self.subTest(size=f'{width}x{height}', codec=codec):
                project = dict(width=width, height=height, fps=25, media=[dict(id='card', src=self.src(source))],
                               clips=[dict(type='video', mediaId='card', track=1, start=0, duration=.08)])
                output = self.directory / ('frame.mov' if codec == 'prores' else 'frame.mp4')
                render_to_file(project, output, options={'codec': codec})
                stream = next(s for s in probe(output, 'stream=codec_type,width,height,nb_frames')['streams']
                              if s['codec_type'] == 'video')
                self.assertEqual((stream['width'], stream['height'], stream['nb_frames']), (width, height, '2'))
                center = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output), '-vf',
                    f'crop=64:64:{width//2-32}:{height//2-32}', '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo',
                    'pipe:1'], timeout=60)
                self.assertGreater(max(center), 60, 'the source picture is missing from the frame')

    def test_frame_size_errors_name_the_limit(self):
        base = dict(fps=25, media=[], clips=[dict(type='title', start=0, duration=1, title='x')])
        for width, height, message in ((8194, 4320, '16 to 8192'), (4096, 4322, '16 to 4320'), (1921, 1080, 'even')):
            with self.subTest(width=width, height=height):
                with self.assertRaisesRegex(ValueError, message):
                    render_to_file({**base, 'width': width, 'height': height}, self.directory / 'x.mp4')


class Limits(ScaleCase):
    def test_every_size_limit_names_itself(self):
        title = dict(type='title', start=0, duration=1, title='')
        cases = [
            (dict(clips=[title]*20_001), r'at most 20,000'),
            (dict(clips=[dict(title, start=12*3600)]), r'at most 12 hours'),
            (dict(media=[dict(id=str(n), src='/media/none.mp4') for n in range(5_001)],
                  clips=[dict(type='audio', mediaId=str(n), track=-1, start=0, duration=1) for n in range(5_001)]),
             r'uses 5,001 media files; export supports at most 5,000'),
            (dict(captions=[dict(start=0, duration=.5, text='x')]*50_001), r'at most 50,000'),
            (dict(clips=[dict(title, keyframes={'opacity': [dict(time=n/1000, value=50) for n in range(101)]}, title='k')]),
             r'more than 100 opacity keyframes'),
        ]
        for patch, message in cases:
            with self.subTest(message=message):
                project = dict(width=64, height=36, fps=25, media=[], clips=[title], **{})
                project.update(patch)
                with self.assertRaisesRegex(ValueError, message):
                    render_to_file(project, self.directory / 'x.mp4')

    def test_one_frame_range_from_a_large_long_timeline_reads_only_what_it_needs(self):
        source = self.source('cam.mp4', 30, 440)
        # Everything outside the range points at offline files: touching it would fail.
        media = [dict(id='cam', src=self.src(source))]
        media += [dict(id=f'offline{n}', src='/media/offline.mp4') for n in range(700)]
        clips = []
        for n in range(1_100):
            start = n*10.0
            identity = 'cam' if 540 <= n <= 541 else f'offline{n % 700}'
            clips.append(dict(type='video', mediaId=identity, track=1, start=start, duration=10, **{'in': 2}))
            clips.append(dict(type='audio', mediaId=identity, track=-1, start=start, duration=10, **{'in': 2}))
        project = dict(width=64, height=36, fps=24, media=media, clips=clips)
        self.assertGreater(len(clips), 2_000)
        output = self.directory / 'frame.mp4'
        began = time.monotonic()
        render_to_file(project, output, options={'range': {'start': 5_405, 'end': 5_405+1/24}})
        self.assertLess(time.monotonic()-began, 60)
        streams = probe(output, 'stream=codec_type,nb_frames')['streams']
        self.assertEqual(next(s['nb_frames'] for s in streams if s['codec_type'] == 'video'), '1')
        with self.assertRaisesRegex(ValueError, 'Media file is unavailable'):
            render_to_file(project, output, options={'range': {'start': 100, 'end': 101}})


class GraphFiles(ScaleCase):
    def test_thousand_clip_mix_renders_and_keeps_every_command_short(self):
        tone = self.directory / 'tone.wav'
        ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000:duration=10', str(tone))
        clips = [dict(type='audio', mediaId='tone', track=-1, start=n*.1, duration=.05, **{'in': (n % 90)/10})
                 for n in range(1_010)]
        project = dict(width=64, height=36, fps=25, media=[dict(id='tone', src=self.src(tone))], clips=clips)
        longest = [0]
        real = render_movie._run_ffmpeg

        def measured(command, *arguments, **keywords):
            longest[0] = max(longest[0], sum(len(part)+1 for part in command))
            return real(command, *arguments, **keywords)

        output = self.directory / 'mix.wav'
        with mock.patch.object(render_movie, '_run_ffmpeg', measured):
            render_to_file(project, output, options={'codec': 'wav'})
        self.assertLess(longest[0], 16_000, 'a command line grows with the timeline')
        self.assertAlmostEqual(float(probe(output, 'format=duration')['format']['duration']), 100.96, places=2)
        for clip in (0, 543, 1_009):
            with self.subTest(clip=clip):
                self.assertGreater(peak(pcm(output, clip*.1+.01, .03)), .05)
                self.assertLess(peak(pcm(output, clip*.1+.06, .03)), .001)


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    @unittest.skipIf(os.name == 'nt', 'uses a POSIX FIFO to simulate a stalled source')
    def test_stalled_ffmpeg_is_stopped_after_the_stall_limit(self):
        fifo = Path(self.temp.name)/'stalled'
        os.mkfifo(fifo)
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 's16le', '-i', str(fifo), '-f', 'null', '-']
        began = time.monotonic()
        holder = threading.Thread(target=lambda: open(fifo, 'wb').close(), daemon=True)
        with mock.patch.dict(os.environ, {'SPLICE_EXPORT_STALL_SECONDS': '2'}):
            with self.assertRaisesRegex(TimeoutError, 'no progress for 2 seconds'):
                render_movie._run_ffmpeg(command)
        self.assertLess(time.monotonic()-began, 10)
        holder.start()  # Release the FIFO so nothing waits on it.

    def test_slow_but_progressing_render_is_not_stopped(self):
        target = Path(self.temp.name)/'slow.wav'
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-re', '-f', 'lavfi', '-i', 'sine=d=3', '-y', str(target)]
        with mock.patch.dict(os.environ, {'SPLICE_EXPORT_STALL_SECONDS': '2'}):
            beats = []
            render_movie._run_ffmpeg(command, heartbeat=lambda: beats.append(1))
        self.assertGreater(target.stat().st_size, 100_000)
        self.assertGreater(len(beats), 2)

    def test_cancel_stops_ffmpeg_promptly(self):
        cancelled = threading.Event()
        threading.Timer(.5, cancelled.set).start()
        began = time.monotonic()
        with self.assertRaises(InterruptedError):
            render_movie._run_ffmpeg(['ffmpeg', '-nostdin', '-v', 'error', '-re', '-f', 'lavfi', '-i', 'sine=d=30',
                                      '-f', 'null', '-'], cancelled)
        self.assertLess(time.monotonic()-began, 5)

    def test_invalid_stall_setting_is_named(self):
        with mock.patch.dict(os.environ, {'SPLICE_EXPORT_STALL_SECONDS': 'soon'}):
            with self.assertRaisesRegex(ValueError, 'SPLICE_EXPORT_STALL_SECONDS'):
                render_movie._run_ffmpeg(['ffmpeg', '-version'])


class WorkSpace(ScaleCase):
    def project(self):
        source = self.source('cam.mp4', 2, 440)
        return dict(width=64, height=36, fps=24, media=[dict(id='cam', src=self.src(source))],
                    clips=[dict(type='video', mediaId='cam', track=1, start=0, duration=1)])

    def test_intermediates_go_to_splice_temp_dir_and_are_removed(self):
        project = self.project()
        scratch = self.directory/'scratch'
        seen = set()
        real = render_movie._run_ffmpeg

        def watch(command, *arguments, **keywords):
            seen.update(Path(part).parent.parent for part in command if part.endswith(('.filtergraph', '.txt')))
            return real(command, *arguments, **keywords)

        with mock.patch.dict(os.environ, {'SPLICE_TEMP_DIR': str(scratch)}), mock.patch.object(render_movie, '_run_ffmpeg', watch):
            render_to_file(project, self.directory/'out.mp4')
        self.assertIn(scratch.resolve(), {path.resolve() for path in seen} | {path.parent.resolve() for path in seen})
        self.assertEqual(list(scratch.iterdir()), [])

    def test_default_work_root_is_under_the_data_directory(self):
        from splice_platform import data_directory
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SPLICE_TEMP_DIR', None)
            self.assertTrue(render_movie.work_root().is_relative_to(data_directory().resolve()))

    def test_free_space_is_checked_before_rendering(self):
        project = self.project()
        usage = mock.Mock(return_value=mock.Mock(free=1024))
        with mock.patch.object(render_movie.shutil, 'disk_usage', usage):
            with self.assertRaisesRegex(ValueError, 'Not enough free disk space.*SPLICE_TEMP_DIR'):
                render_to_file(project, self.directory/'out.mp4')

    def test_stale_directories_are_reaped_and_live_ones_kept(self):
        root = self.directory/'work'
        root.mkdir()
        live = render_movie.WorkDirectory('splice-export-', root)
        self.addCleanup(live.cleanup)
        crashed = render_movie.WorkDirectory('splice-segments-', root)
        crashed._lock.close(); crashed._lock = None  # As if its process died.
        legacy = root/'splice-export-legacy'
        legacy.mkdir()
        old = time.time()-3600
        os.utime(legacy, (old, old))
        fresh = root/'splice-audio-starting'
        fresh.mkdir()
        unrelated = root/'keep-me'
        unrelated.mkdir()
        self.assertEqual(render_movie.reap_stale_work(root), 2)
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         sorted([Path(live.name).name, 'splice-audio-starting', 'keep-me']))

    def test_export_jobs_reap_on_start_and_run_without_a_deadline(self):
        from export_jobs import ExportJobs
        project = self.project()
        scratch = self.directory/'jobs'
        scratch.mkdir()
        stale = render_movie.WorkDirectory('splice-export-', scratch)
        stale._lock.close(); stale._lock = None
        with mock.patch.dict(os.environ, {'SPLICE_TEMP_DIR': str(scratch)}):
            jobs = ExportJobs()
            self.addCleanup(jobs.shutdown)
            self.assertFalse(Path(stale.name).exists())
            identifier = jobs.start(project, {'codec': 'h264'})
            deadline = time.monotonic()+120
            while jobs.status(identifier)['state'] == 'rendering' and time.monotonic() < deadline:
                time.sleep(.2)
            status = jobs.status(identifier)
            self.assertEqual(status['state'], 'complete', status['error'])
            with jobs.open_file(identifier) as handle:
                self.assertEqual(handle.read(12)[4:8], b'ftyp')
                self.assertTrue(Path(handle.name).resolve().is_relative_to(scratch.resolve()))


@unittest.skipUnless(SLOW, 'feature-scale export; set SPLICE_SLOW_TESTS=1')
class FeatureScale(ScaleCase):
    """A 3,000-item timeline longer than 2 h 30 min, built from generated sources."""

    def report(self, stage, began, output, **extra):
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        row = dict(stage=stage, wall_seconds=round(time.monotonic()-began, 1), peak_ffmpeg_rss_mb=round(rss/1024),
                   output_mb=round(Path(output).stat().st_size/1e6, 1), **extra)
        print('\nSCALE ' + json.dumps(row), flush=True)

    def test_feature_length_timeline(self):
        cameras = [self.source(f'camera{n}.mp4', 600, 300+100*n) for n in range(3)]
        music = self.directory/'music.wav'
        ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=48000:duration=600', str(music))
        media = [dict(id=f'camera{n}', src=self.src(path)) for n, path in enumerate(cameras)]
        media.append(dict(id='music', src=self.src(music)))
        clips = []
        for n in range(2_000):
            clip = dict(type='video', mediaId=f'camera{n % 3}', track=1, start=n*4.75, duration=4.75,
                        **{'in': 5+(n*37 % 580)})
            if n % 10 == 5:
                clip.update(transition='dissolve', transitionDuration=.5)
            if n % 50 == 7:
                clip['reverse'] = True
            clips.append(clip)
        for n in range(1_000):
            clips.append(dict(type='audio', mediaId='music', track=-1, start=n*9.5, duration=9.5, fadeIn=.25, fadeOut=.25,
                              volume=50, **{'in': n*13 % 590}))
        total = 2_000*4.75
        self.assertEqual(len(clips), 3_000)
        self.assertGreater(total, 2.5*3600)
        project = dict(width=32, height=18, fps=24, media=media, clips=clips)
        facts = dict(clips=len(clips), timeline_seconds=total)

        began = time.monotonic()
        short = self.directory/'range.mp4'
        render_to_file(project, short, options={'range': {'start': 4_000, 'end': 4_030}})
        self.report('range_mp4_30s', began, short, **facts)
        self.assertEqual(next(s['nb_frames'] for s in probe(short, 'stream=codec_type,nb_frames')['streams']
                              if s['codec_type'] == 'video'), str(30*24))

        began = time.monotonic()
        mix = self.directory/'mix.wav'
        render_to_file(project, mix, options={'codec': 'wav'})
        self.report('full_wav_mix', began, mix, **facts)
        self.assertAlmostEqual(float(probe(mix, 'format=duration')['format']['duration']), total, places=2)
        self.assertGreater(peak(pcm(mix, total-3, 1)), .05)
        mix.unlink()

        began = time.monotonic()
        movie = self.directory/'full.mp4'
        render_to_file(project, movie)
        self.report('full_mp4_32x18', began, movie, **facts)
        streams = probe(movie, 'stream=codec_type,nb_frames,duration')['streams']
        self.assertEqual(next(s['nb_frames'] for s in streams if s['codec_type'] == 'video'), str(round(total*24)))
        self.assertAlmostEqual(float(next(s['duration'] for s in streams if s['codec_type'] == 'audio')), total, delta=.05)


if __name__ == '__main__':
    unittest.main()
