"""The render queue: order, hold, cancel, retry, batch, output confinement, persistence across a
restart, and no process or scratch left when the server dies mid-render."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from export_jobs import ExportJobs
from premiere import MEDIA_ROOT
import render_movie
import render_queue
from render_queue import ConflictError, RenderQueue

ROOT = Path(__file__).resolve().parent.parent
POSIX = os.name != 'nt'


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=120)


class QueueCase(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.media = Path(tempfile.mkdtemp(prefix='queue-', dir=MEDIA_ROOT))
        self.addCleanup(shutil.rmtree, self.media, True)
        self.state = Path(tempfile.mkdtemp(prefix='queue-state-'))
        self.addCleanup(shutil.rmtree, self.state, True)
        self.folder = Path(tempfile.mkdtemp(prefix='queue-out-'))
        self.addCleanup(shutil.rmtree, self.folder, True)
        reveal = mock.patch.dict(os.environ, {'SPLICE_NO_REVEAL': '1'})
        reveal.start()
        self.addCleanup(reveal.stop)
        clip = self.media / 'clip.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=256x144:r=24:d=2', '-f', 'lavfi', '-i', 'sine=d=2', '-shortest', '-c:v', 'libx264',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(clip))
        self.project = dict(name='Queue test', width=256, height=144, fps=24, media=[dict(id='m', src='/media/' + str(clip.relative_to(MEDIA_ROOT)))],
                            clips=[dict(type='video', mediaId='m', track=1, start=0, duration=2, **{'in': 0}),
                                   dict(type='audio', mediaId='m', track=1, start=0, duration=2, **{'in': 0})],
                            captions=[dict(start=.2, duration=.5, text='Hi')])

    def queue(self, jobs=None):
        jobs = jobs or ExportJobs()
        queue = RenderQueue(jobs, root=self.state)
        self.addCleanup(lambda: (queue.shutdown(), jobs.shutdown()))
        return queue

    def wait(self, queue, identifiers, states=('complete', 'failed', 'cancelled'), timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = {job['id']: job for job in queue.list()['jobs']}
            if all(jobs[i]['state'] in states for i in identifiers):
                return jobs
            time.sleep(.1)
        self.fail(f'jobs did not settle: {[(jobs[i]["name"], jobs[i]["state"]) for i in identifiers]}')


class Running(QueueCase):
    def test_jobs_run_in_order_one_at_a_time_and_land_in_the_folder(self):
        queue = self.queue()
        queue.set_paused(True)
        ids = queue.add(self.project, [dict(options=dict(codec='h264'), name='A web'), dict(options=dict(codec='prores', profile='proxy'), name='B proxy'),
                                       dict(options=dict(codec='wav'), name='C mix')], folder=str(self.folder))
        queue.action(ids[2], 'top')
        self.assertEqual([job['name'] for job in queue.list()['jobs']], ['C mix', 'A web', 'B proxy'])
        queue.action(ids[1], 'up')
        self.assertEqual([job['name'] for job in queue.list()['jobs']], ['C mix', 'B proxy', 'A web'])
        queue.action(ids[1], 'down')
        queue.set_paused(False)
        started = {}
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            jobs = queue.list()['jobs']
            rendering = [job['name'] for job in jobs if job['state'] == 'rendering']
            self.assertLessEqual(len(rendering), 1, 'one render at a time')
            for job in jobs:
                if job['state'] != 'queued' and job['name'] not in started:
                    started[job['name']] = len(started)
            if all(job['state'] == 'complete' for job in jobs):
                break
            time.sleep(.05)
        self.assertEqual(sorted(started, key=started.get), ['C mix', 'A web', 'B proxy'])
        jobs = {job['name']: job for job in queue.list()['jobs']}
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ['A web.mp4', 'B proxy.mov', 'C mix.wav'])
        self.assertEqual(jobs['A web']['outputs'], [str(self.folder / 'A web.mp4')])
        self.assertIn('audio', jobs['A web']['report'])
        self.assertEqual(jobs['B proxy']['report']['delivery']['profile'], 'ProRes 422 Proxy')
        self.assertEqual(queue.reveal(ids[0]), str(self.folder / 'A web.mp4'))

    def test_hold_cancel_retry_remove(self):
        queue = self.queue()
        queue.set_paused(True)
        ids = queue.add(self.project, [dict(options=dict(codec='h264'), name='one'), dict(options=dict(codec='h264'), name='two')],
                        folder=str(self.folder))
        queue.action(ids[0], 'hold')
        queue.action(ids[1], 'cancel')
        queue.set_paused(False)
        time.sleep(1.5)
        jobs = {job['id']: job for job in queue.list()['jobs']}
        self.assertEqual((jobs[ids[0]]['state'], jobs[ids[1]]['state']), ('held', 'cancelled'))
        queue.action(ids[1], 'retry')
        self.wait(queue, [ids[1]])
        queue.action(ids[0], 'release')
        self.wait(queue, [ids[0]])
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ['one.mp4', 'two.mp4'])
        queue.action(ids[0], 'remove')
        self.assertEqual([job['id'] for job in queue.list()['jobs']], [ids[1]])
        with self.assertRaisesRegex(ValueError, 'Only waiting jobs'):
            queue.action(ids[1], 'up')

    def test_cancel_a_running_job_leaves_no_scratch_and_the_next_job_runs(self):
        scratch = Path(tempfile.mkdtemp(prefix='queue-scratch-', dir=self.state))
        with mock.patch.dict(os.environ, {'SPLICE_TEMP_DIR': str(scratch)}):
            queue = self.queue()
            long = dict(self.project, clips=[dict(c, duration=60) for c in self.project['clips']])
            long['clips'] = [dict(type='video', mediaId='m', track=1, start=n * 2, duration=2, **{'in': 0}) for n in range(30)]
            ids = queue.add(long, [dict(options=dict(codec='prores', profile='hq'), name='long')], folder=str(self.folder))
            ids += queue.add(self.project, [dict(options=dict(codec='h264'), name='after')], folder=str(self.folder))
            deadline = time.monotonic() + 60
            while queue.list()['jobs'][0]['state'] != 'rendering' or not any(scratch.iterdir()):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.05)
            time.sleep(.5)
            queue.action(ids[0], 'cancel')
            jobs = self.wait(queue, ids)
            self.assertEqual((jobs[ids[0]]['state'], jobs[ids[1]]['state']), ('cancelled', 'complete'))
            self.assertEqual(list(scratch.iterdir()), [], 'no scratch after cancel')
            self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ['after.mp4'])

    def test_batch_of_presets_with_sidecars(self):
        queue = self.queue()
        ids = queue.add(self.project, [dict(options=dict(codec='h264', captionFiles=['vtt']), name='film - web', preset='Web'),
                                       dict(options=dict(codec='dnxhr', profile='lb', container='mxf'), name='film - avid', preset='Avid'),
                                       dict(options=dict(codec='png', profile='8'), name='film - frames (8-bit)', preset='Frames')],
                        folder=str(self.folder))
        jobs = self.wait(queue, ids)
        self.assertTrue(all(job['state'] == 'complete' for job in jobs.values()), [job['error'] for job in jobs.values()])
        names = sorted(path.name for path in self.folder.iterdir())
        self.assertEqual(names, ['film - avid.mxf', 'film - avid.srt', 'film - frames (8-bit)', 'film - web.mp4', 'film - web.vtt'])
        frames = sorted((self.folder / 'film - frames (8-bit)').iterdir())
        pictures = [path for path in frames if path.suffix == '.png']
        self.assertEqual(len(pictures), 48)
        # The files inside are named in plain characters; the captions travel as a sidecar.
        self.assertEqual(pictures[0].name, 'film - frames -8-bit_0000000.png')
        self.assertTrue((self.folder / 'film - frames (8-bit)' / 'film - frames -8-bit.srt').exists())
        self.assertTrue((self.folder / 'film - frames (8-bit)' / 'film - frames -8-bit.wav').exists())


class Turnover(QueueCase):
    def test_reference_movie_rendered_into_the_turnover_package(self):
        import zipfile
        queue = self.queue()
        project = dict(self.project, startTimecode=24 * 3600 * 10, marks={})
        ids = queue.add_turnover(project, formats=['fcpxml', 'edl'], movie='prores', folder=str(self.folder), span=dict(start=0.5, end=1.5))
        jobs = self.wait(queue, ids)
        self.assertEqual(jobs[ids[0]]['state'], 'complete', jobs[ids[0]]['error'])
        package = self.folder / 'Queue-test-turnover.zip'
        self.assertEqual(jobs[ids[0]]['outputs'], [str(package)])
        with zipfile.ZipFile(package) as archive:
            names = sorted(archive.namelist())
            request = json.loads(archive.read('Queue-test/REFERENCE-MOVIE-REQUEST.json'))
            movie = self.folder / 'movie.mov'
            movie.write_bytes(archive.read('Queue-test/Queue-test-reference.mov'))
            edl = archive.read(next(name for name in names if name.endswith('.edl'))).decode()
        self.assertIn('Queue-test/Queue-test.fcpxml', names)
        self.assertEqual(request['status'], 'rendered by the Splice render queue')
        self.assertEqual(request['file'], 'Queue-test-reference.mov')
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-count_frames', '-of', 'json', str(movie)]))
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        timecode = next(s for s in info['streams'] if s['codec_type'] == 'data')['tags']['timecode']
        # The movie matches the exported timeline: its range, frame count and start timecode.
        self.assertEqual((video['profile'], int(video['nb_read_frames'])), ('Proxy', request['range']['frames']))
        self.assertEqual(request['range']['frames'], 24)
        self.assertEqual(timecode, request['startTimecode'])
        self.assertIn(request['startTimecode'], edl, 'the EDL record starts where the movie starts')
        self.assertFalse((self.folder / 'Queue-test-reference.mov').exists(), 'the movie lives in the package')


class Confinement(QueueCase):
    def test_names_and_folders_are_confined(self):
        queue = self.queue()
        for name in ('../escape', 'a/b', 'nul', 'x' * 121, 'C:\\temp', 'bad\x00'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    queue.add(self.project, [dict(options=dict(codec='h264'), name=name)], folder=str(self.folder))
        for folder in ('relative/path', str(self.folder / 'missing'), '\x00'):
            with self.subTest(folder=folder):
                with self.assertRaises(ValueError):
                    queue.add(self.project, [dict(options=dict(codec='h264'), name='ok')], folder=folder)
        with self.assertRaisesRegex(ValueError, 'Unknown export format'):
            queue.add(self.project, [dict(options=dict(codec='h266'), name='ok')], folder=str(self.folder))
        with self.assertRaisesRegex(ValueError, 'DNxHR needs'):
            queue.add(dict(self.project, width=160, height=90), [dict(options=dict(codec='dnxhr'), name='ok')], folder=str(self.folder))
        with self.assertRaisesRegex(ValueError, 'Two exports in this batch'):
            queue.add(self.project, [dict(options=dict(codec='h264'), name='same'), dict(options=dict(codec='h264'), name='same')],
                      folder=str(self.folder))
        self.assertEqual(queue.list()['jobs'], [])

    def test_a_link_in_the_folder_is_never_written_through(self):
        queue = self.queue()
        outside = Path(tempfile.mkdtemp(prefix='queue-outside-'))
        self.addCleanup(shutil.rmtree, outside, True)
        (self.folder / 'linked.mp4').symlink_to(outside / 'victim.mp4')
        with self.assertRaisesRegex(ValueError, 'is a link'):
            queue.add(self.project, [dict(options=dict(codec='h264'), name='linked')], folder=str(self.folder), overwrite=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_an_existing_file_is_replaced_only_when_confirmed(self):
        queue = self.queue()
        existing = self.folder / 'film.mp4'
        existing.write_bytes(b'keep me')
        with self.assertRaisesRegex(ConflictError, 'film.mp4'):
            queue.add(self.project, [dict(options=dict(codec='h264'), name='film')], folder=str(self.folder))
        self.assertEqual(existing.read_bytes(), b'keep me')
        ids = queue.add(self.project, [dict(options=dict(codec='h264'), name='film')], folder=str(self.folder), overwrite=True)
        self.wait(queue, ids)
        self.assertNotEqual(existing.read_bytes(), b'keep me')

    def test_a_file_that_appears_while_rendering_is_not_replaced(self):
        queue = self.queue()
        queue.set_paused(True)
        ids = queue.add(self.project, [dict(options=dict(codec='h264'), name='late')], folder=str(self.folder))
        (self.folder / 'late.mp4').write_bytes(b'appeared')
        queue.set_paused(False)
        jobs = self.wait(queue, ids)
        self.assertEqual((self.folder / 'late.mp4').read_bytes(), b'appeared')
        self.assertTrue((self.folder / 'late (2).mp4').exists())
        self.assertTrue(any('saved as late (2)' in warning for warning in jobs[ids[0]]['warnings']))


class Restart(QueueCase):
    def test_waiting_jobs_survive_and_a_rendering_job_becomes_interrupted(self):
        queue = self.queue()
        queue.set_paused(True)
        ids = queue.add(self.project, [dict(options=dict(codec='h264'), name='first'), dict(options=dict(codec='h264'), name='second')],
                        folder=str(self.folder))
        queue.shutdown()
        # As if the server stopped while the first job rendered.
        state = json.loads((self.state / 'queue.json').read_text())
        state['jobs'][0]['state'] = 'rendering'
        (self.state / 'queue.json').write_text(json.dumps(state))
        again = self.queue()
        jobs = {job['id']: job for job in again.list()['jobs']}
        self.assertEqual((jobs[ids[0]]['state'], jobs[ids[1]]['state']), ('interrupted', 'queued'))
        self.assertTrue(again.list()['paused'], 'the pause survives too')
        again.action(ids[0], 'retry')
        again.set_paused(False)
        self.wait(again, ids)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ['first.mp4', 'second.mp4'])

    def test_a_corrupt_state_file_is_set_aside(self):
        (self.state / 'queue.json').write_text('{not json')
        queue = self.queue()
        self.assertEqual(queue.list()['jobs'], [])
        self.assertTrue((self.state / 'queue.unreadable.json').exists())

    @unittest.skipUnless(POSIX, 'kills a process group')
    def test_killing_the_server_mid_render_leaves_no_process_or_scratch(self):
        scratch = Path(tempfile.mkdtemp(prefix='kill-scratch-', dir=self.state))
        marker = self.state / 'pids'
        script = textwrap.dedent(f'''
            import json, os, sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from export_jobs import ExportJobs
            jobs = ExportJobs()
            project = json.loads(sys.stdin.read())
            identifier = jobs.start(project, {{'codec': 'prores', 'profile': 'hq'}})
            while jobs.status(identifier)['state'] == 'rendering':
                job = jobs.jobs[identifier]
                if job['group']:
                    open({str(marker)!r}, 'w').write(str(job['group']))
                time.sleep(.1)
        ''')
        long = dict(self.project, clips=[dict(type='video', mediaId='m', track=1, start=n * 2, duration=2, **{'in': 0}) for n in range(60)])
        environment = {**os.environ, 'SPLICE_TEMP_DIR': str(scratch)}
        server = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE, env=environment, start_new_session=True)
        server.stdin.write(json.dumps(long).encode()); server.stdin.close()
        deadline = time.monotonic() + 60
        while not (marker.exists() and marker.read_text() and any(scratch.iterdir())):
            self.assertLess(time.monotonic(), deadline, 'the render never started')
            time.sleep(.1)
        group = int(marker.read_text())
        time.sleep(1)
        os.kill(server.pid, signal.SIGKILL)  # the server dies without any cleanup
        server.wait(timeout=10)

        def members():
            found = []
            for entry in os.listdir('/proc') if os.path.isdir('/proc') else []:
                if entry.isdigit():
                    try:
                        if os.getpgid(int(entry)) == group:
                            found.append(int(entry))
                    except (ProcessLookupError, PermissionError):
                        pass
            return found
        deadline = time.monotonic() + 15
        while members() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertEqual(members(), [], 'the worker and its FFmpeg died with the server')
        with mock.patch.dict(os.environ, {'SPLICE_TEMP_DIR': str(scratch)}):
            self.assertGreaterEqual(render_movie.reap_stale_work(scratch, unlocked_grace=0), 1)
        self.assertEqual(list(scratch.iterdir()), [], 'the next start removes its scratch')


class Presets(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp(prefix='presets-')
        self.addCleanup(shutil.rmtree, directory, True)
        patch = mock.patch('delivery.user_presets_path', return_value=Path(directory) / 'delivery-presets.json')
        patch.start()
        self.addCleanup(patch.stop)

    def test_builtin_presets_are_valid_and_state_their_purpose(self):
        import delivery
        for preset in delivery.BUILTIN_PRESETS:
            with self.subTest(preset=preset['name']):
                spec = delivery.normalize(delivery.resolve_preset_options(preset['options'], 3840, 2160))
                self.assertTrue(preset['purpose'])
                self.assertNotIn('range', preset['options'])
                self.assertNotIn('fps', preset['options'])
                self.assertIsNotNone(spec['container'])

    def test_save_rename_delete_export_import(self):
        import delivery
        saved = delivery.save_user_presets([dict(name='Client review', purpose='x', options=dict(codec='h264', crf=24, profile='high',
                                                                                                     rateControl='crf'))])
        self.assertEqual([p['name'] for p in delivery.load_user_presets()[0]], ['Client review'])
        renamed = delivery.save_user_presets([dict(name='Client review v2', purpose=saved[0]['purpose'], options=saved[0]['options'])])
        self.assertEqual(renamed[0]['name'], 'Client review v2')
        exported = json.dumps({'splicePresets': 1, 'presets': renamed})
        imported = delivery.import_presets(exported)
        self.assertEqual(imported[0]['options'], renamed[0]['options'])
        self.assertEqual(delivery.save_user_presets([]), [])
        self.assertEqual(delivery.load_user_presets(), ([], []))

    def test_untrusted_preset_files_are_refused(self):
        import delivery
        for text in ('not json', '[]', json.dumps({'presets': [{'name': 'x', 'options': {'codec': 'h264', 'rm': '-rf'}}]}),
                     json.dumps({'presets': [{'name': 'x', 'options': {'codec': 'h264', 'title': 'x' * 5000}}]}),
                     json.dumps({'presets': [{'name': '', 'options': {'codec': 'h264'}}]}),
                     json.dumps({'presets': [{'name': 'x', 'options': {'codec': 'h264', 'range': {'start': 0, 'end': 1}}}]}),
                     json.dumps({'presets': [{'name': 'x', 'options': {'codec': 'h264'}, 'script': 'alert(1)'}]}),
                     'x' * (delivery.PRESET_FILE_LIMIT + 1)):
            with self.subTest(text=text[:60]):
                with self.assertRaises(ValueError):
                    delivery.import_presets(text)
        delivery.user_presets_path().write_text(json.dumps({'presets': [{'name': 'good', 'options': {'codec': 'prores'}},
                                                                        {'name': 'bad', 'options': {'codec': 'nope'}}]}))
        presets, problems = delivery.load_user_presets()
        self.assertEqual([p['name'] for p in presets], ['good'])
        self.assertEqual(len(problems), 1)
        with self.assertRaisesRegex(ValueError, 'built-in'):
            delivery.save_user_presets([dict(name=delivery.BUILTIN_PRESETS[0]['name'], options=dict(codec='h264'))])


if __name__ == '__main__':
    unittest.main()
