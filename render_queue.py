"""The render queue: several exports run one after another without babysitting.

Jobs (a project snapshot, export options, an output folder and name) are kept in Splice's data
folder, so the queue survives a page reload and a server restart: jobs not started stay queued; a
job that was rendering when the server stopped is marked *interrupted* and can be retried (its
worker killed itself with the server, see export_jobs._orphan_guard, and its scratch folder is
removed by reap_stale_work at the next start). Renders run one at a time through ExportJobs, the
same path as a direct export, so a direct export and the queue never render at once.

Output files are written only inside the chosen folder, under a sanitised name; an existing file
is never replaced unless the user confirmed it (``overwrite``). Finished files are moved there from
the job's scratch folder.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import delivery
from render_movie import _export_settings

MAX_JOBS = 500
MAX_FINISHED = 200
MAX_BATCH = 20
STATE_VERSION = 1
ACTIVE = ('queued', 'held')
FINISHED = ('complete', 'failed', 'cancelled', 'interrupted')
NAME = re.compile(r'[^\\/:*?"<>|\x00-\x1f]{1,120}')


class ConflictError(ValueError):
    """The output already exists and replacing it was not confirmed."""


def default_folder():
    override = os.environ.get('SPLICE_EXPORT_DIR')
    if override:
        return Path(override).expanduser().absolute()
    from splice_platform import data_directory
    return data_directory() / 'Exports'


def _clean_name(name):
    if not isinstance(name, str):
        raise ValueError('Invalid output name')
    name = name.strip().rstrip('.')
    if not NAME.fullmatch(name) or name in ('.', '..') or name.upper().split('.')[0] in (
            'CON', 'PRN', 'AUX', 'NUL', *(f'COM{n}' for n in range(1, 10)), *(f'LPT{n}' for n in range(1, 10))):
        raise ValueError('Invalid output name: use up to 120 characters without / \\ : * ? " < > |')
    return name


def _folder(value):
    create = value in (None, '')
    if create:
        value = str(default_folder())
    if not isinstance(value, str) or len(value) > 4096 or '\x00' in value:
        raise ValueError('Invalid output folder')
    folder = Path(value).expanduser()
    if not folder.is_absolute() or '..' in folder.parts:
        raise ValueError('Choose the output folder as a full path')
    if any(part.is_symlink() for part in (folder, *folder.parents)):
        raise ValueError('The output folder is a link: choose its actual path')
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise ValueError(f'The output folder {folder} does not exist')
    if not os.access(folder, os.W_OK | os.X_OK):
        raise ValueError(f'The output folder {folder} is not writable')
    return folder


@contextmanager
def _destination(value):
    """Pin the chosen Linux directory across delivery, even if a parent gets renamed."""
    folder = _folder(value)
    if not sys.platform.startswith('linux'):
        yield folder
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(folder.anchor, flags)
    try:
        for part in folder.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield Path('/proc/self/fd') / str(fd)
    finally:
        os.close(fd)


def _publish_file(staged, target, overwrite):
    if overwrite:
        os.replace(staged, target)
        return
    try:
        os.link(staged, target)
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS, errno.EACCES):
            raise
        # FAT/exFAT has no hard links. Exclusive creation preserves the same no-clobber rule.
        with target.open('xb') as destination:
            try:
                with staged.open('rb') as source:
                    shutil.copyfileobj(source, destination)
            except BaseException:
                destination.close()
                target.unlink()
                raise


def _targets(folder, name, spec, captions=()):
    """The paths a job writes: the output (file or image folder) and its possible sidecars."""
    if spec['kind'] == 'images':
        return [folder / name]
    main = folder / f'{name}.{spec["extension"]}'
    return [main] + [folder / f'{name}.{kind}' for kind in captions]


class RenderQueue:
    def __init__(self, exports, root=None):
        from splice_platform import data_directory
        self.exports = exports
        self.root = Path(root) if root else data_directory() / 'render-queue'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closing = threading.Event()
        self.jobs = []
        self.paused = False
        self.current = None
        self._load()
        self.thread = threading.Thread(target=self._run, daemon=True, name='splice-render-queue')
        self.thread.start()

    # ---- persistence ------------------------------------------------------------------------
    def _state_file(self):
        return self.root / 'queue.json'

    def _load(self):
        try:
            data = json.loads(self._state_file().read_text(encoding='utf-8'))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            broken = self._state_file().with_suffix('.unreadable.json')
            os.replace(self._state_file(), broken)
            data = {}
        jobs = data.get('jobs') if isinstance(data, dict) else None
        self.paused = bool(data.get('paused')) if isinstance(data, dict) else False
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', job['id']):
                continue
            if job.get('state') == 'rendering':
                job.update(state='interrupted', error='Splice stopped while this job was rendering. Retry it to render again.',
                           progress=0, eta=None)
            if job.get('state') not in ACTIVE + FINISHED:
                continue
            if not self._project_file(job['id']).is_file() and job['state'] in ACTIVE + ('interrupted', 'failed', 'cancelled'):
                job.update(state='failed', error='The saved timeline for this job is missing')
            self.jobs.append(job)
        known = {job['id'] for job in self.jobs}
        for path in self.root.glob('*.project.json'):
            if path.name.split('.')[0] not in known:
                path.unlink(missing_ok=True)
        self._save()

    def _save(self):
        temporary = self._state_file().with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': STATE_VERSION, 'paused': self.paused, 'jobs': self.jobs}), encoding='utf-8')
        os.replace(temporary, self._state_file())

    def _project_file(self, identifier):
        return self.root / f'{identifier}.project.json'

    # ---- adding -----------------------------------------------------------------------------
    def add(self, project, entries, *, folder=None, overwrite=False):
        """Queue one job per entry ({options, name, preset}) for one timeline. Returns the job ids."""
        if not isinstance(project, dict):
            raise ValueError('Invalid project')
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BATCH:
            raise ValueError(f'Queue 1 to {MAX_BATCH} exports at a time')
        if not isinstance(overwrite, bool):
            raise ValueError('Invalid overwrite setting')
        folder = _folder(folder)
        width, height, fps = project.get('width'), project.get('height'), project.get('fps')
        prepared, names, conflicts = [], set(), []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {'options', 'name', 'preset'}:
                raise ValueError('Invalid queue entry')
            options = entry.get('options')
            if not isinstance(options, dict):
                raise ValueError('Invalid export options')
            if any(key.startswith('_') for key in options):
                raise ValueError('Invalid private export option')
            spec, *_ = _export_settings(options)
            if spec['kind'] != 'audio' and isinstance(width, int) and isinstance(height, int) and isinstance(fps, (int, float)):
                problems = delivery.frame_problems(spec, width, height, fps)
                if problems:
                    raise ValueError(' '.join(problems))
            reason = delivery.unavailable_reason(options)
            if reason:
                raise ValueError(f'{spec["profile_label"] or spec["label"]} is not available on this machine: {reason}')
            name = _clean_name(entry.get('name') or project.get('name') or 'Untitled')
            preset = entry.get('preset') or ''
            if not isinstance(preset, str) or len(preset) > 120:
                raise ValueError('Invalid preset name')
            key = (name.casefold(), spec['extension'] or 'folder')
            if key in names:
                raise ValueError(f'Two exports in this batch would both be named {name}')
            names.add(key)
            captions = set(spec['caption_files'])
            if not delivery.CONTAINERS[spec['container']]['subtitles']:
                captions.add('srt')
            targets = _targets(folder, name, spec, sorted(captions) if project.get('captions') and options.get('captions', True) else ())
            for target in targets:
                if target.is_symlink():
                    raise ValueError(f'{target.name} is a link: Splice does not write through links')
                if target.exists() and not overwrite:
                    conflicts.append(target.name)
            prepared.append(dict(options=options, spec=spec, name=name, preset=preset, targets=targets))
        if conflicts:
            raise ConflictError('Already in the output folder: ' + ', '.join(conflicts) + '. Replace them?')
        text = json.dumps(project)
        ids = []
        with self.lock:
            if len([job for job in self.jobs if job['state'] not in FINISHED]) + len(prepared) > MAX_JOBS:
                raise ValueError(f'The queue holds at most {MAX_JOBS} jobs')
            claimed = {t for job in self.jobs if job['state'] in ACTIVE + ('rendering',) for t in job['targets']}
            for item in prepared:
                for target in item['targets']:
                    if str(target) in claimed:
                        raise ValueError(f'Another queued export already writes {target.name} in that folder')
                    claimed.add(str(target))
            for item in prepared:
                identifier = uuid.uuid4().hex
                self._project_file(identifier).write_text(text, encoding='utf-8')
                spec = item['spec']
                self.jobs.append(dict(
                    id=identifier, state='queued', name=item['name'], preset=item['preset'], options=item['options'],
                    folder=str(folder), overwrite=overwrite, targets=[str(t) for t in item['targets']],
                    sequence=str(project.get('name') or 'Untitled')[:120],
                    summary=_summary(spec, item['options']), created=time.time(), started=None, finished=None, progress=0,
                    eta=None, error='', warnings=[], report={}, outputs=[], attempts=0))
                ids.append(identifier)
            self._trim()
            self._save()
        self.wake.set()
        return ids

    def add_turnover(self, project, *, formats=None, movie='h264', name=None, folder=None, overwrite=False, span=None,
                     audio_edl=False):
        """Queue a turnover package whose reference movie the queue renders.

        The interchange exporter writes the timeline files, the report and REFERENCE-MOVIE-REQUEST.json
        (range, start timecode, rate); the movie is rendered from that request (timecode and clip-name
        burn-ins, the timeline's range and start timecode) and the finished package
        ``<name>-turnover.zip`` holds all of them."""
        import base64
        import io
        import zipfile
        from fractions import Fraction
        from interchange.api import export_timeline
        from interchange.timecode import timecode_frames
        if not isinstance(overwrite, bool):
            raise ValueError('Invalid overwrite setting')
        if movie not in ('h264', 'prores'):
            raise ValueError('The reference movie is H.264 or ProRes Proxy')
        if not isinstance(project, dict):
            raise ValueError('Invalid project')
        options = {'formats': formats or ['fcpxml'], 'package': True, 'referenceMovie': True, 'audioEdl': bool(audio_edl)}
        if span is not None:
            if not isinstance(span, dict):
                raise ValueError('Invalid range')
            options['range'] = span
        result = export_timeline(project, options)
        package = base64.b64decode(result['package']['base64'])
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            names = archive.namelist()
            request_name = next(entry for entry in names if entry.endswith('/REFERENCE-MOVIE-REQUEST.json'))
            request = json.loads(archive.read(request_name))
        rate = Fraction(request['rate'])
        first, frames = int(request['range']['startFrame']), int(request['range']['frames'])
        start = timecode_frames(request['startTimecode'], rate, bool(request['dropFrame']))
        per_second = round(rate)
        day = per_second * 86400 - (per_second // 15) * 1296 if request['dropFrame'] else per_second * 86400
        # The renderer labels frame f of the sequence startTimecode + f: the range's first frame
        # gets the timeline's start label.
        rendered = dict(project, startTimecode=(start - first) % day, dropFrame=bool(request['dropFrame']))
        burn = dict(fields=['timecode', 'clipName'], position='bottom-center', clipNamePosition='top-left', size=4)
        movie_options = dict(codec='h264', crf=23, burnIns=burn) if movie == 'h264' else dict(codec='prores', profile='proxy', burnIns=burn)
        movie_options['range'] = dict(start=float(Fraction(first) / rate), end=float(Fraction(first + frames) / rate))
        base = request_name.split('/')[0]
        name = _clean_name(name or base)
        folder = _folder(folder)
        target = folder / f'{name}-turnover.zip'
        if target.exists() and not overwrite:
            raise ConflictError(f'Already in the output folder: {target.name}. Replace it?')
        spec = delivery.normalize(movie_options)
        identifier = uuid.uuid4().hex
        with self.lock:
            if sum(job['state'] not in FINISHED for job in self.jobs) >= MAX_JOBS:
                raise ValueError(f'The queue holds at most {MAX_JOBS} jobs')
            if any(str(target) in job['targets'] for job in self.jobs if job['state'] in ACTIVE + ('rendering',)):
                raise ValueError(f'Another queued export already writes {target.name} in that folder')
            self._project_file(identifier).write_text(json.dumps(rendered), encoding='utf-8')
            self.jobs.append(dict(
                id=identifier, state='queued', name=name, preset='Turnover reference movie', options=movie_options,
                folder=str(folder), overwrite=overwrite, targets=[str(target)], sequence=str(project.get('name') or 'Untitled')[:120],
                summary=_summary(spec, movie_options) + ' · turnover package', created=time.time(), started=None, finished=None,
                progress=0, eta=None, error='', warnings=[], report={}, outputs=[], attempts=0,
                turnover=dict(package=result['package']['base64'], request=request_name, base=base)))
            self._trim()
            self._save()
        self.wake.set()
        return [identifier]

    def _package(self, job, identifier, spec, folder):
        """Write the turnover zip: the exporter's files, the rendered movie and the request marked done."""
        import base64
        import io
        import zipfile
        output, _ = self.exports.outputs(identifier)
        name = _clean_name(job['name'])
        target = folder / f'{name}-turnover.zip'
        notes = []
        if target.is_symlink():
            raise ValueError(f'{target} is a link: Splice does not write through links')
        if target.exists() and not job['overwrite']:
            for number in range(2, 1000):
                candidate = folder / f'{job["name"]}-turnover ({number}).zip'
                if not os.path.lexists(candidate):
                    notes.append(f'{target.name} appeared while this job rendered, so the package was saved as {candidate.name}.')
                    target = candidate
                    break
            else:
                raise ValueError(f'{target.name} already exists in {folder}')
        turnover = job['turnover']
        movie_name = f'{turnover["base"]}-reference.{spec["extension"]}'
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(turnover['package']))) as source, \
                    zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for entry in source.namelist():
                    data = source.read(entry)
                    if entry == turnover['request']:
                        request = json.loads(data)
                        request.update(status='rendered by the Splice render queue', file=movie_name,
                                       burnIns=['sequence timecode', 'clip name'], codec=spec['profile_label'] or spec['label'])
                        data = (json.dumps(request, indent=2) + '\n').encode()
                    archive.writestr(entry, data)
                # Video is already compressed: store it.
                archive.write(output, f'{turnover["base"]}/{movie_name}', compress_type=zipfile.ZIP_STORED)
            _publish_file(temporary, target, job['overwrite'])
        finally:
            temporary.unlink(missing_ok=True)
        return [str(target)], notes

    def _trim(self):
        finished = [job for job in self.jobs if job['state'] in FINISHED]
        for job in finished[:max(0, len(finished) - MAX_FINISHED)]:
            self.jobs.remove(job)
            self._project_file(job['id']).unlink(missing_ok=True)

    # ---- listing and actions ----------------------------------------------------------------
    def list(self):
        with self.lock:
            return {'paused': self.paused, 'folder': str(default_folder()),
                    'jobs': [{key: value for key, value in job.items() if key not in ('options', 'targets', 'turnover')} |
                             {'options': job['options'], 'turnover': bool(job.get('turnover'))} for job in self.jobs]}

    def set_paused(self, paused):
        if not isinstance(paused, bool):
            raise ValueError('Invalid pause setting')
        with self.lock:
            self.paused = paused
            self._save()
        self.wake.set()

    def action(self, identifier, action):
        with self.lock:
            job = self._find(identifier)
            if action == 'cancel':
                if job['state'] == 'rendering':
                    job['cancelRequested'] = True
                    running = self.current
                elif job['state'] in ACTIVE:
                    job.update(state='cancelled', finished=time.time())
                    running = None
                else:
                    raise ValueError('This job is not waiting or rendering')
            elif action == 'retry':
                if job['state'] not in ('failed', 'cancelled', 'interrupted', 'complete'):
                    raise ValueError('Only a finished job can be rendered again')
                if not self._project_file(identifier).is_file():
                    raise ValueError('The saved timeline for this job is missing')
                job.update(state='queued', error='', warnings=[], report={}, progress=0, eta=None, started=None, finished=None,
                           outputs=[], cancelRequested=False)
                # Retried jobs go to the end of the waiting jobs.
                self.jobs.remove(job)
                self.jobs.append(job)
            elif action == 'hold':
                if job['state'] != 'queued':
                    raise ValueError('Only a waiting job can be held')
                job['state'] = 'held'
            elif action == 'release':
                if job['state'] != 'held':
                    raise ValueError('This job is not held')
                job['state'] = 'queued'
            elif action in ('up', 'down', 'top'):
                if job['state'] not in ACTIVE:
                    raise ValueError('Only waiting jobs can be moved')
                waiting = [index for index, item in enumerate(self.jobs) if item['state'] in ACTIVE]
                position = waiting.index(self.jobs.index(job))
                target = {'up': position - 1, 'down': position + 1, 'top': 0}[action]
                if 0 <= target < len(waiting) and target != position:
                    self.jobs.remove(job)
                    self.jobs.insert(waiting[target] if target < position else waiting[target], job)
            elif action == 'remove':
                if job['state'] == 'rendering':
                    raise ValueError('Cancel the render before removing it')
                self.jobs.remove(job)
                self._project_file(identifier).unlink(missing_ok=True)
            else:
                raise ValueError('Unknown queue action')
            self._save()
        if action == 'cancel' and running:
            try:
                self.exports.status(running, cancel=True)
            except KeyError:
                pass
        self.wake.set()
        return self.list()

    def reveal(self, identifier):
        """Show the job's output in the system file manager; returns its path."""
        with self.lock:
            job = self._find(identifier)
            if job['state'] != 'complete' or not job['outputs']:
                raise ValueError('This job has no output yet')
            path = Path(job['outputs'][0])
        if not path.exists():
            raise ValueError(f'{path} is no longer there')
        if os.environ.get('SPLICE_NO_REVEAL') != '1':
            if sys.platform == 'darwin':
                command = ['open', '-R', str(path)]
            elif os.name == 'nt':
                command = ['explorer', '/select,', str(path)]
            else:
                command = ['xdg-open', str(path if path.is_dir() else path.parent)]
            if shutil.which(command[0]):
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=os.name != 'nt')
        return str(path)

    def _find(self, identifier):
        for job in self.jobs:
            if job['id'] == identifier:
                return job
        raise KeyError(identifier)

    # ---- running ----------------------------------------------------------------------------
    def _next(self):
        if self.paused:
            return None
        return next((job for job in self.jobs if job['state'] == 'queued'), None)

    def _run(self):
        while not self.closing.is_set():
            self.wake.wait(1)
            self.wake.clear()
            if self.closing.is_set():
                break
            with self.lock:
                job = self._next()
            if job is None or self.exports.running():
                continue
            try:
                self._render(job)
            except Exception as error:  # noqa: BLE001 - a job's failure must not stop the queue
                with self.lock:
                    job.update(state='failed', error=str(error) or type(error).__name__, finished=time.time(), eta=None)
                    self._save()

    def _render(self, job):
        project = json.loads(self._project_file(job['id']).read_text(encoding='utf-8'))
        spec = delivery.normalize(job['options'])
        with self.lock:
            if job['state'] != 'queued':
                return
            job.update(state='rendering', started=time.time(), progress=0, eta=None, attempts=job.get('attempts', 0) + 1,
                       cancelRequested=False)
            self._save()
        try:
            identifier = self.exports.start(project, job['options'], folder_name=_frame_name(job['name']) if spec['kind'] == 'images' else 'frames')
        except ValueError as error:
            with self.lock:
                job.update(state='failed', error=str(error), finished=time.time())
                self._save()
            return
        with self.lock:
            self.current = identifier
        saved = 0
        try:
            while True:
                if self.closing.is_set():
                    return
                status = self.exports.status(identifier)
                now = time.time()
                with self.lock:
                    job['progress'] = round(status['progress'], 4)
                    elapsed = now - job['started']
                    job['eta'] = round(elapsed * (1 - status['progress']) / status['progress']) if status['progress'] > .02 else None
                    if job.get('cancelRequested') and status['state'] == 'rendering':
                        cancel = True
                    else:
                        cancel = False
                    if now - saved > 2:
                        self._save()
                        saved = now
                if cancel:
                    status = self.exports.status(identifier, cancel=True)
                if status['state'] != 'rendering':
                    break
                time.sleep(.25)
            with self.lock:
                job.update(warnings=status['warnings'], report=status['report'], finished=time.time(), eta=None)
            if status['state'] == 'complete':
                publish = self._package if job.get('turnover') else self._deliver
                with _destination(job['folder']) as folder:
                    paths, notes = publish(job, identifier, spec, folder)
                    outputs = [str(Path(job['folder']) / Path(path).name) for path in paths]
                with self.lock:
                    job.update(state='complete', progress=1, outputs=outputs, warnings=job['warnings'] + notes)
            else:
                with self.lock:
                    if status['state'] == 'cancelled' and not job.get('cancelRequested'):
                        # Stopped by the server shutting down, not by the user.
                        job.update(state='interrupted', error='Splice stopped while this job was rendering. Retry it to render again.')
                    else:
                        job.update(state=status['state'], error=status['error'] if status['state'] == 'failed' else '')
        finally:
            with self.lock:
                self.current = None
                self._save()
            self.exports.release(identifier)
            self.wake.set()

    def _deliver(self, job, identifier, spec, folder):
        """Move the finished output and sidecars into the chosen folder; never replace unconfirmed."""
        output, sidecars = self.exports.outputs(identifier)
        name, notes = _clean_name(job['name']), []
        captions = [sidecar.suffix[1:] for sidecar in sidecars]
        targets = _targets(folder, name, spec, captions)
        if any(target.is_symlink() for target in targets):
            raise ValueError('An output is a link: Splice does not write through links')
        if not job['overwrite'] and any(target.exists() for target in targets):
            for number in range(2, 1000):
                candidate = f'{name} ({number})'
                candidates = _targets(folder, candidate, spec, captions)
                if not any(os.path.lexists(target) for target in candidates):
                    notes.append(f'An output appeared while rendering, so this export was saved as {candidate} instead.')
                    targets = candidates
                    break
            else:
                raise ValueError(f'No available output name for {name}')
        delivered = []
        for source, target in zip([output, *sidecars], targets):
            if source.is_dir():
                with tempfile.TemporaryDirectory(dir=folder) as stage:
                    staged = Path(stage) / 'images'
                    shutil.move(str(source), str(staged))
                    previous = Path(stage) / 'previous'
                    if os.path.lexists(target):
                        if not job['overwrite']:
                            raise FileExistsError(target)
                        os.rename(target, previous)
                    try:
                        os.rename(staged, target)
                    except BaseException:
                        if previous.exists():
                            os.rename(previous, target)
                        raise
            else:
                with tempfile.TemporaryDirectory(dir=folder) as stage:
                    staged = Path(stage) / 'file'
                    shutil.move(str(source), str(staged))
                    _publish_file(staged, target, job['overwrite'])
            delivered.append(str(target))
        return delivered, notes

    def shutdown(self):
        self.closing.set()
        self.wake.set()
        self.thread.join(timeout=10)


def _frame_name(name):
    """The image files' base name: the job name in letters, digits, space, _ . - (what ExportJobs accepts)."""
    clean = re.sub(r'[^A-Za-z0-9 _.\-]+', '-', name).strip(' .-')[:80]
    return clean if clean and clean[0].isalnum() else 'frames'


def _summary(spec, options):
    parts = [spec['profile_label'] or spec['label'], delivery.CONTAINERS[spec['container']]['label'],
             delivery.AUDIO_CODECS[spec['audio']]['label']]
    if spec['output_size']:
        parts.append(f'{spec["output_size"]["width"]} × {spec["output_size"]["height"]}')
    if options.get('range'):
        rng = options['range']
        parts.append(f'{rng.get("start", 0):.2f}–{rng.get("end", 0):.2f} s')
    return ' · '.join(parts)
