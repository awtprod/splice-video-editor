"""Local export jobs. Each render owns a process group and a locked work directory.

POSIX: the worker starts its own session; FFmpeg and anything FFmpeg starts
stay in that process group, and the whole group is killed on cancel, on a
watchdog kill, when the worker ends (a process left holding FFmpeg's pipes
dies with it) and at shutdown. Windows has no process groups that can be
killed this way: the worker's process tree is stopped with taskkill /T while
it is alive, and the renderer stops each FFmpeg tree the same way.

Renders have no wall-clock deadline: a feature-length master can take hours.
A job fails only when it stops making progress (see JOB_STALL_GRACE) or the
user cancels it.
"""
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid
import warnings


# Seconds beyond the renderer's own FFmpeg stall limit before the job itself is
# declared stuck (covers time spent outside FFmpeg, such as probing media).
JOB_STALL_GRACE = 120

FORMATS = {'h264':('mp4','video/mp4',True), 'prores':('mov','video/quicktime',False),
           'wav':('wav','audio/wav',True)}


def _worker(project, options, output, progress, beat, cancelled, connection):
    # A cancelled render must stop its FFmpeg children as well as Python.
    if os.name != 'nt':
        os.setsid()
    connection.send(('ready', os.name != 'nt'))
    try:
        from render_movie import render_to_file

        def advance(fraction):
            progress.value = min(1, max(0, float(fraction)))

        def alive():
            with beat.get_lock():
                beat.value += 1

        report = {}
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter('once')
            # Scratch files go inside the job's own folder, which the parent
            # removes even when it has to kill this process.
            render_to_file(project, output, options=options, progress=advance, cancelled=cancelled, heartbeat=alive,
                           work_directory=Path(output).parent, report=report)
        connection.send(('done', {'warnings': list(dict.fromkeys(str(note.message) for note in notices)), 'report': report}))
    except Exception as error:
        connection.send(('error', str(error)))
    finally:
        connection.close()


def _kill_group(group):
    """Kill every process left in a job's process group (POSIX)."""
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class ExportJobs:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.RLock()
        self.context = multiprocessing.get_context('spawn')
        from render_movie import reap_stale_work
        try:
            reap_stale_work()
        except OSError as error:
            warnings.warn(f'Could not clean up old export files: {error}', stacklevel=2)

    def start(self, project, options):
        if not isinstance(project, dict) or not isinstance(options, dict):
            raise ValueError('Invalid export settings')
        codec = options.get('codec','h264')
        if codec not in FORMATS:
            raise ValueError('Unknown export format')
        extension, mime, previewable = FORMATS[codec]
        with self.lock:
            self.reap()
            if any(job['state'] == 'rendering' for job in self.jobs.values()):
                raise ValueError('An export is already running')
            identifier = uuid.uuid4().hex
            from render_movie import WorkDirectory, _stall_seconds
            stall = _stall_seconds() + JOB_STALL_GRACE
            temp = WorkDirectory('splice-export-')
            out = Path(temp.name) / ('output.'+extension)
            progress = self.context.Value('d', 0)
            beat = self.context.Value('q', 0)
            cancelled = self.context.Event()
            reader, writer = self.context.Pipe(duplex=False)
            process = self.context.Process(target=_worker, args=(
                project, options, out, progress, beat, cancelled, writer))
            try:
                process.start()
            except BaseException:
                reader.close()
                temp.cleanup()
                raise
            finally:
                writer.close()
            job = dict(state='rendering', directory=temp, output=out, process=process, extension=extension, mime=mime, previewable=previewable,
                       progress=progress, beat=beat, stall=stall, cancelled=cancelled, error='', warnings=[], report={}, finished=0,
                       group=None, settled=threading.Event())
            self.jobs[identifier] = job
            threading.Thread(target=self._watch, args=(job, reader), daemon=True).start()
            return identifier

    def _watch(self, job, reader):
        error, done, grouped = '', False, False
        process = job['process']
        stall = job['stall']
        last_change, last_seen = time.monotonic(), None
        try:
            while True:
                if reader.poll(.15):
                    try:
                        kind, message = reader.recv()
                    except EOFError:
                        break
                    if kind == 'ready':
                        grouped = message
                        if grouped:
                            job['group'] = process.pid
                    elif kind == 'done':
                        done = True
                        job['warnings'] = message.get('warnings') or []
                        job['report'] = message.get('report') or {}
                    elif kind == 'error':
                        error = message
                seen = (job['beat'].value, job['progress'].value)
                if seen != last_seen:
                    last_seen, last_change = seen, time.monotonic()
                stuck = not done and time.monotonic() - last_change > stall
                if job['cancelled'].is_set() or stuck:
                    if not job['cancelled'].is_set():
                        error = f'Export made no progress for {stall:g} seconds and was stopped'
                    try:
                        if os.name == 'nt':
                            # Let the renderer terminate FFmpeg first. If it is
                            # blocked in a probe, stop this exact process tree.
                            if job['cancelled'].is_set():
                                process.join(.6)
                            if process.is_alive():
                                taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
                                try:
                                    subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                                                   capture_output=True, timeout=4, check=True)
                                except (OSError, subprocess.SubprocessError):
                                    process.kill()
                        elif grouped:
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    break
                if not process.is_alive() and not reader.poll():
                    break
        except BrokenPipeError:
            # Windows can report a closed worker pipe while polling. The
            # exit code and completion message still determine the outcome.
            if not done and not error and not job['cancelled'].is_set():
                error = 'Export stopped before completion'
        finally:
            reader.close()
            process.join()
            if grouped:
                # The worker has exited (or was killed); its session still holds
                # anything FFmpeg left running, such as a process holding its pipes.
                _kill_group(process.pid)
            with self.lock:
                job['state'] = ('cancelled' if job['cancelled'].is_set() else
                                'complete' if done and process.exitcode == 0 else 'failed')
                job['error'] = error or ('' if done else 'Export stopped before completion')
                job['finished'] = time.monotonic()
                if job['state'] != 'complete':
                    job['directory'].cleanup()
                process.close()
                job['settled'].set()

    def reap(self):
        for identifier, job in list(self.jobs.items()):
            if job['finished'] and time.monotonic() - job['finished'] > 3600:
                try:
                    job['directory'].cleanup()
                except PermissionError:
                    continue  # Windows still has this export open for streaming.
                del self.jobs[identifier]

    def status(self, identifier, cancel=False):
        with self.lock:
            self.reap()
            job = self.jobs[identifier]
            if cancel and job['state'] == 'rendering':
                job['cancelled'].set()
        if cancel:
            job['settled'].wait(timeout=6)
        with self.lock:
            return {'state': job['state'], 'progress': 1 if job['state'] == 'complete' else job['progress'].value,
                    'error': job['error'], 'warnings':job['warnings'], 'report': job['report'], 'extension':job['extension'],
                    'mime':job['mime'], 'previewable':job['previewable']}

    def open_file(self, identifier):
        with self.lock:
            job = self.jobs[identifier]
            if job['state'] != 'complete':
                raise ValueError('Export is not ready')
            return job['output'].open('rb')

    def shutdown(self):
        with self.lock:
            jobs = list(self.jobs.values())
            for job in jobs:
                if job['state'] == 'rendering':
                    job['cancelled'].set()
        end = time.monotonic() + 5
        while time.monotonic() < end and any(j['state'] == 'rendering' for j in jobs):
            time.sleep(.05)
        for job in jobs:
            if job['group'] is not None and not job['settled'].is_set():
                _kill_group(job['group'])
            job['settled'].wait(timeout=5)
        for job in jobs:
            if job['state'] != 'rendering':
                job['directory'].cleanup()
