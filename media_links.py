"""Media referenced in place, plus the app's own managed media cache.

Linked originals are never copied. The server keeps a registry of the
absolute paths the user explicitly picked; a linked source is addressed as
``/media/linked/<id>/<name>`` and resolves only through that registry, and
only while the file still has the size and modification time recorded when
it was linked. Anything else under ``/media/`` must be a managed file inside
the media directory.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import premiere
from premiere import MEDIA_ROOT

LINK_PREFIX = '/media/linked/'
REGISTRY = MEDIA_ROOT.parent / 'linked-media.json'
MAX_LINKS = 200_000
MAX_LINK_BATCH = 500
MAX_FOLDER_FILES = 2000
MAX_FOLDER_DEPTH = 8
MAX_LISTING = 5000
FINGERPRINT_BYTES = 64 * 1024
CACHE_MIN_AGE = 600
_ID = re.compile(r'[a-f0-9]{32}')

MEDIA_EXTENSIONS = frozenset({
    '.mp4', '.mov', '.mkv', '.webm', '.avi', '.mxf', '.m4v', '.mts', '.m2ts', '.ts', '.mpg', '.mpeg',
    '.wmv', '.flv', '.3gp', '.dv', '.wav', '.mp3', '.m4a', '.aac', '.ogg', '.oga', '.opus', '.flac',
    '.aif', '.aiff', '.caf', '.wma', '.ac3', '.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.exr',
    '.dpx', '.webp'})
# Vendor RAW formats FFmpeg cannot decode. Name the tool that can.
CAMERA_RAW = {
    '.braw': 'Blackmagic RAW (transcode it in DaVinci Resolve or Blackmagic RAW Player)',
    '.r3d': 'REDCODE RAW (transcode it in REDCINE-X PRO)',
    '.ari': 'ARRIRAW (transcode it in ARRI Reference Tool)',
    '.arx': 'ARRIRAW (transcode it in ARRI Reference Tool)',
    '.crm': 'Canon Cinema RAW Light (transcode it in Canon Cinema RAW Development)',
    '.nev': 'Nikon N-RAW (transcode it in the camera vendor\'s RAW tool)',
}

_lock = threading.RLock()
_cache = {'stamp': None, 'links': {}, 'recovered': None}
_managed_path = None
REGISTRY_VERSION = 1


class RegistryFull(ValueError):
    pass


def camera_raw_error(name):
    kind = CAMERA_RAW.get(Path(str(name)).suffix.lower())
    if not kind:
        return None
    return (f'{Path(str(name)).name} is {kind}. Splice edits through FFmpeg, which cannot decode this camera RAW '
            'format: export a ProRes or DNxHR transcode or proxy from the camera vendor\'s tool and import that.')


def fingerprint(path, size):
    """A cheap content identity: size plus the first and last 64 KiB."""
    digest = hashlib.sha256(str(size).encode())
    with open(path, 'rb') as source:
        digest.update(source.read(FINGERPRINT_BYTES))
        if size > FINGERPRINT_BYTES:
            source.seek(max(FINGERPRINT_BYTES, size - FINGERPRINT_BYTES))
            digest.update(source.read(FINGERPRINT_BYTES))
    return digest.hexdigest()[:32]


class _FileLock:
    """An exclusive lock on ``linked-media.lock`` shared by every Splice
    process on this machine (desktop app, ``python3 server.py``, export
    workers). Held only around a registry read-modify-write."""

    def __init__(self, path, timeout=30):
        self.path, self.timeout, self.handle = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError('Another Splice window is still updating the linked media list') from None
                    time.sleep(.02)
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        return self

    def __exit__(self, *exc):
        handle, self.handle = self.handle, None
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _registry_lock():
    return _FileLock(REGISTRY.with_name('linked-media.lock'))


def _stamp():
    """Identity of the registry file. Every save replaces the file, so the
    inode changes too; size and mtime alone miss a same-size rewrite within
    the filesystem's timestamp resolution."""
    try:
        stat = REGISTRY.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _valid_entry(key, value):
    return (_ID.fullmatch(key) is not None and isinstance(value, dict) and isinstance(value.get('path'), str)
            and isinstance(value.get('name'), str) and isinstance(value.get('size'), int)
            and isinstance(value.get('mtimeNs'), int))


def _set_aside():
    """Keep a damaged registry for the user under a clear name."""
    aside = REGISTRY.with_name(time.strftime('linked-media.damaged-%Y%m%d-%H%M%S.json'))
    counter = 1
    while aside.exists():
        aside = REGISTRY.with_name(time.strftime('linked-media.damaged-%Y%m%d-%H%M%S') + f'-{counter}.json')
        counter += 1
    os.replace(REGISTRY, aside)
    return aside


def _read_locked():
    """Read the registry; the caller holds the file lock. A damaged file is
    set aside and a fresh registry started, recording where it went."""
    stamp = _stamp()
    if stamp is None:
        return stamp, {}, None
    try:
        data = json.loads(REGISTRY.read_text(encoding='utf-8'))
        raw = data.get('links') if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            raise ValueError
    except (UnicodeDecodeError, ValueError):
        aside = _set_aside()
        recovered = dict(file=aside.name, at=int(time.time()))
        _write({}, recovered)
        return _stamp(), {}, recovered
    links = {key: value for key, value in raw.items() if _valid_entry(key, value)}
    recovered = data.get('recovered') if isinstance(data.get('recovered'), dict) else None
    return stamp, links, recovered


def _write(links, recovered):
    REGISTRY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=REGISTRY.parent, prefix='.linked-media-', suffix='.json')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as sink:
            payload = {'version': REGISTRY_VERSION, 'links': links}
            if recovered:
                payload['recovered'] = recovered
            json.dump(payload, sink, separators=(',', ':'))
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, REGISTRY)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _links():
    """The registry, reloaded when another process (an export worker, the
    other Splice server) or a previous launch changed the file."""
    with _lock:
        stamp = _stamp()
        if stamp is None:
            _cache.update(stamp=None, links={}, recovered=None)
        elif stamp != _cache['stamp']:
            with _registry_lock():
                stamp, links, recovered = _read_locked()
            _cache.update(stamp=stamp, links=links, recovered=recovered)
        return _cache['links']


def _update(change):
    """Read-modify-write under the cross-process lock, always from the file
    itself, then replace it atomically."""
    with _lock, _registry_lock():
        _, links, recovered = _read_locked()
        links = dict(links)
        result = change(links)
        _write(links, recovered)
        _cache.update(stamp=_stamp(), links=links, recovered=recovered)
        return result


def recovery_notice():
    """Set when a damaged registry was set aside, while that file remains."""
    _links()
    recovered = _cache['recovered']
    if not recovered or not isinstance(recovered.get('file'), str):
        return None
    aside = REGISTRY.with_name(Path(recovered['file']).name)
    if not aside.exists():
        return None
    return (f'The linked media list was damaged, so Splice set it aside as {aside} and started a new one. '
            'Linked sources it listed show as offline: relink them from File → Link media in place.')


def _absolute_file(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
        raise ValueError('Choose a file on this computer')
    if not os.path.isabs(value):
        raise ValueError('Linked media needs an absolute path')
    path = Path(value).resolve(strict=False)
    if not path.is_file():
        raise ValueError(f'{Path(value).name} is not a readable file')
    return path


def _prepare(value, validate):
    path = _absolute_file(value)
    message = camera_raw_error(path.name)
    if message:
        raise ValueError(message)
    if validate:
        validate(path, path.name)
    stat = path.stat()
    return dict(path=str(path), name=path.name, size=stat.st_size, mtimeNs=stat.st_mtime_ns,
                fingerprint=fingerprint(path, stat.st_size), linkedAt=int(time.time()))


def _source(identifier, entry):
    return dict(src=LINK_PREFIX + identifier + '/' + quote(entry['name'], safe=''), sourcePath=entry['path'],
                size=entry['size'], fingerprint=entry['fingerprint'])


def link_many(values, validate=None):
    """Register user-picked files with one registry write. Returns, per
    value, a source description or ``{'error': message}``.

    ``validate(path, name)`` may reject a file before it is registered.
    Link ids are random and kept in the registry, so a path alone does not
    reveal its URL; linking a path again reuses its id."""
    prepared = []
    for value in values:
        try:
            prepared.append(_prepare(value, validate))
        except (ValueError, OSError) as error:
            prepared.append(error)

    def change(links):
        by_path = {entry['path']: key for key, entry in links.items()}
        results = []
        for item in prepared:
            if isinstance(item, Exception):
                results.append(dict(error=str(item)))
                continue
            identifier = by_path.get(item['path'])
            if identifier is None:
                if len(links) >= MAX_LINKS:
                    results.append(dict(error=f'The linked media list is full ({MAX_LINKS:,} files). Open File → '
                                              'Media cache and forget links no project uses.'))
                    continue
                identifier = secrets.token_hex(16)
                by_path[item['path']] = identifier
            links[identifier] = item
            results.append(_source(identifier, item))
        return results

    if not any(isinstance(item, dict) for item in prepared):
        return [dict(error=str(item)) for item in prepared]
    return _update(change)


def link(value, validate=None):
    """Register one user-picked file and return its source description."""
    result = link_many([value], validate)[0]
    if 'error' in result:
        raise (RegistryFull if 'is full' in result['error'] else ValueError)(result['error'])
    return result


def forget(identifiers):
    """Remove links. The files themselves are never touched."""
    wanted = {i for i in identifiers if isinstance(i, str) and _ID.fullmatch(i)}

    def change(links):
        removed = [key for key in wanted if key in links]
        for key in removed:
            del links[key]
        return sorted(removed)
    return _update(change) if wanted else []


def forget_unreferenced(sources, min_age=CACHE_MIN_AGE):
    """Forget links no listed source uses, except ones made in the last
    ``min_age`` seconds (an import may still be in progress)."""
    keep = referenced_links(sources)
    now = time.time()

    def change(links):
        removed = [key for key, entry in links.items()
                   if key not in keep and now - entry.get('linkedAt', 0) >= min_age]
        for key in removed:
            del links[key]
        return removed
    return _update(change)


def folder_files(value):
    """Media files below a picked folder, with browser-style relative paths."""
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError('Choose a folder on this computer')
    root = Path(value).resolve(strict=False)
    if not root.is_dir():
        raise ValueError('The chosen folder is unavailable')
    found = []
    for directory, folders, files in os.walk(root):
        depth = len(Path(directory).relative_to(root).parts)
        folders[:] = sorted(f for f in folders if not f.startswith('.')) if depth < MAX_FOLDER_DEPTH else []
        for name in sorted(files):
            if name.startswith('.') or Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            if len(found) >= MAX_FOLDER_FILES:
                raise ValueError(f'The folder holds more than {MAX_FOLDER_FILES} media files. Choose a smaller folder.')
            path = Path(directory) / name
            found.append((str(path), (Path(root.name) / path.relative_to(root)).as_posix()))
    return found


def _entry(src):
    path = unquote(urlparse(str(src)).path)
    if not path.startswith(LINK_PREFIX):
        return None, None
    identifier, _, name = path[len(LINK_PREFIX):].partition('/')
    entry = _links().get(identifier) if _ID.fullmatch(identifier) else None
    if entry is None or name != entry['name']:
        raise ValueError('Media file is unavailable')
    return identifier, entry


def _check(entry, stat):
    if not stat_is_file(stat) or stat.st_size != entry['size'] or stat.st_mtime_ns != entry['mtimeNs']:
        raise ValueError(f'{entry["name"]} changed after it was linked. Relink it to confirm the new file.')


def stat_is_file(stat):
    import stat as modes
    return modes.S_ISREG(stat.st_mode)


def resolve_media(src):
    """Resolve a media URL to a local path: a registered linked original
    (``/media/linked/<id>/<name>``) or, through premiere's resolver, a
    managed copy, proxy or bundled asset. Raises ValueError otherwise."""
    identifier, entry = _entry(src)
    if entry is None:
        return _managed_path(src)
    path = Path(entry['path'])
    try:
        stat = path.stat()
    except OSError:
        raise ValueError(f'{entry["name"]} is offline: it moved or its drive is disconnected. Relink it.') from None
    _check(entry, stat)
    return path


def open_media(src):
    """Open a media URL for reading. A linked original is checked again on
    the open handle, so a file swapped in after resolving is not served."""
    path = resolve_media(src)
    source = open(path, 'rb')
    try:
        _, entry = _entry(src)
        if entry is not None:
            _check(entry, os.fstat(source.fileno()))
    except BaseException:
        source.close()
        raise
    return source


def is_linked(src):
    return str(src).startswith(LINK_PREFIX)


def install():
    """Route every _media_path lookup through resolve_media.

    premiere.py owns _media_path and render_movie.py binds it by name at
    import. media_proxy imports this module, and render_movie imports
    media_proxy immediately after binding, so this runs in the server and
    in every spawned export worker before any media is resolved."""
    global _managed_path
    current = premiere._media_path
    if current is resolve_media:
        return
    _managed_path = current
    premiere._media_path = resolve_media
    module = sys.modules.get('render_movie')
    if module is not None and getattr(module, '_media_path', None) is current:
        module._media_path = resolve_media


PLACES_TIMEOUT = 2
PLACES_TTL = 30
_places_state = {'at': 0, 'value': None, 'worker': None}


def _windows_drives():
    """Drive letters from the OS bitmask: no drive is touched, so a
    disconnected network drive cannot stall the listing."""
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (ImportError, AttributeError, OSError):
        return []
    return [chr(65 + bit) + ':\\' for bit in range(26) if mask >> bit & 1]


def _scan_places():
    home = Path.home().resolve()
    places = [('Home', home)]
    for name in ('Movies', 'Videos', 'Desktop', 'Music', 'Pictures', 'Downloads'):
        if (home / name).is_dir():
            places.append((name, home / name))
    if sys.platform == 'win32':
        places.extend((drive[:2], Path(drive)) for drive in _windows_drives())
    else:
        places.append(('Computer', Path('/')))
        user = home.name
        for parent in (Path('/Volumes'), Path('/media') / user, Path('/run/media') / user, Path('/mnt')):
            try:
                volumes = sorted(p for p in parent.iterdir() if p.is_dir())[:64]
            except OSError:
                continue
            places.extend((volume.name, volume) for volume in volumes)
    return [dict(name=name, path=str(path)) for name, path in places]


def _places():
    """Places for the picker, refreshed at most every PLACES_TTL seconds in
    a worker. A slow or hung volume costs PLACES_TIMEOUT once, never each
    browse: the last known list (or Home alone) is returned meanwhile."""
    state = _places_state
    started = None
    with _lock:
        fresh = state['value'] is not None and time.monotonic() - state['at'] < PLACES_TTL
        if not fresh and (state['worker'] is None or not state['worker'].is_alive()):
            def scan():
                try:
                    value = _scan_places()
                except OSError:
                    value = None
                with _lock:
                    state.update(value=value or state['value'], at=time.monotonic())
            started = state['worker'] = threading.Thread(target=scan, daemon=True, name='splice-places')
            started.start()
    if started is not None:
        started.join(PLACES_TIMEOUT)  # Only the request that started a scan waits for it.
    with _lock:
        return state['value'] or [dict(name='Home', path=str(Path.home()))]


def browse(value=None):
    """List one directory for the in-app picker. Names and sizes only."""
    if not value:
        return dict(path=None, parent=None, entries=[], places=_places(), truncated=False)
    if not isinstance(value, str) or not os.path.isabs(value) or len(value) > 4096 or '\0' in value:
        raise ValueError('Choose a folder on this computer')
    directory = Path(value).resolve(strict=False)
    if not directory.is_dir():
        raise ValueError('The folder is unavailable')
    entries, truncated = [], False
    with os.scandir(directory) as listing:
        for item in listing:
            if item.name.startswith('.'):
                continue
            if len(entries) >= MAX_LISTING:
                truncated = True
                break
            try:
                folder = item.is_dir()
                stat = None if folder else item.stat()
            except OSError:
                continue
            if not folder and not item.is_file():
                continue
            suffix = Path(item.name).suffix.lower()
            entries.append(dict(name=item.name, path=item.path, folder=folder,
                                size=None if folder else stat.st_size,
                                kind='folder' if folder else 'raw' if suffix in CAMERA_RAW
                                else 'media' if suffix in MEDIA_EXTENSIONS else 'other'))
    entries.sort(key=lambda e: (not e['folder'], e['name'].casefold()))
    parent = directory.parent
    return dict(path=str(directory), parent=None if parent == directory else str(parent), entries=entries,
                places=_places(), truncated=truncated)


def _tree_size(path):
    total, used = 0, 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                stat = (Path(directory) / name).stat()
            except OSError:
                continue
            total += stat.st_size
            if not name.startswith('.'):
                used = max(used, stat.st_atime, stat.st_mtime)
    return total, used


PROXY_NAMES = frozenset({'edit.mp4', 'seek.mp4', 'grade.mp4', 'edit.webm', 'edit.m4a', 'nested.mp4'})
# Markers the server writes beside what it stores. Imports are originals a
# user handed to Splice, possibly the only copy; derived files (proxies,
# previews, Premiere audio) are rebuilt on demand.
IMPORT_MARKER = '.splice-import'
DERIVED_MARKER = '.splice-derived'
_kinds = {}


def mark(directory, kind):
    (Path(directory) / (IMPORT_MARKER if kind == 'import' else DERIVED_MARKER)).touch()


def _content_id(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:32]


def _kind(directory, names):
    """'import' or 'proxy'. Unmarked folders from earlier versions count as
    imports unless they hold a single generated file whose content does not
    name the folder (imports are stored under their content hash)."""
    markers = {p.name for p in directory.iterdir() if p.name in (IMPORT_MARKER, DERIVED_MARKER)}
    if IMPORT_MARKER in markers:
        return 'import'
    if DERIVED_MARKER in markers:
        return 'proxy'
    if not names or not all(n in PROXY_NAMES or n.endswith('-audio.wav') for n in names):
        return 'import'
    if len(names) > 1:
        return 'proxy'
    target = directory / names[0]
    stat = target.stat()
    key = (str(target), stat.st_size, stat.st_mtime_ns)
    if key not in _kinds:
        _kinds[key] = 'import' if _content_id(target) == directory.name else 'proxy'
    return _kinds[key]


def cache_report():
    """Managed imported copies and derived files under the media directory,
    and the linked originals (which are not copied)."""
    entries = []
    if MEDIA_ROOT.is_dir():
        for directory in MEDIA_ROOT.iterdir():
            if not directory.is_dir() or not _ID.fullmatch(directory.name):
                continue
            try:
                names = sorted(p.name for p in directory.iterdir() if p.is_file() and not p.name.startswith('.'))
                kind = _kind(directory, names)
                size, used = _tree_size(directory)
                modified = directory.stat().st_mtime
            except OSError:
                continue
            entries.append(dict(id=directory.name, kind=kind, files=names[:8], bytes=size,
                                src=f'/media/{directory.name}/{quote(names[0], safe="")}' if names else None,
                                modified=int(modified), lastUsed=int(max(used, modified))))
    entries.sort(key=lambda e: -e['bytes'])
    links = _links()
    newest = sorted(links.items(), key=lambda item: -item[1].get('linkedAt', 0))[:MAX_LINKS_SHOWN]
    free = shutil.disk_usage(MEDIA_ROOT if MEDIA_ROOT.is_dir() else MEDIA_ROOT.parent if MEDIA_ROOT.parent.is_dir() else Path.home())
    return dict(directory=str(MEDIA_ROOT), entries=entries,
                importBytes=sum(e['bytes'] for e in entries if e['kind'] == 'import'),
                proxyBytes=sum(e['bytes'] for e in entries if e['kind'] == 'proxy'),
                linkedFiles=len(links), linkedBytes=sum(e['size'] for e in links.values()),
                links=[dict(id=key, **_source(key, entry), name=entry['name'], linkedAt=entry.get('linkedAt'))
                       for key, entry in newest],
                maxLinks=MAX_LINKS, freeBytes=free.free, notice=recovery_notice())


MAX_LINKS_SHOWN = 500


def referenced_ids(sources):
    return _referenced(sources, re.compile(r'/media/([a-f0-9]{32})/'))


def referenced_links(sources):
    return _referenced(sources, re.compile(r'/media/linked/([a-f0-9]{32})/'))


def _referenced(sources, pattern):
    if not isinstance(sources, list) or len(sources) > 1_000_000:
        raise ValueError('Invalid list of referenced media')
    ids = set()
    for value in sources:
        if not isinstance(value, str):
            raise ValueError('Invalid list of referenced media')
        match = pattern.match(value)
        if match:
            ids.add(match.group(1))
    return ids


def _remove(entry):
    directory = MEDIA_ROOT / entry['id']
    shutil.rmtree(directory)


def prune(sources, min_age=CACHE_MIN_AGE):
    """Delete derived files (proxies, previews) no listed source uses.

    Imported copies are never deleted here: another Splice window, the
    desktop app or a project file saved to disk may use one this browser
    does not know about, and an import cannot be rebuilt. Entries touched in
    the last ``min_age`` seconds may belong to work in progress and stay."""
    keep = referenced_ids(sources)
    deleted, freed, failed = [], 0, []
    now = time.time()
    for entry in cache_report()['entries']:
        if entry['kind'] != 'proxy' or entry['id'] in keep or now - entry['modified'] < min_age:
            continue
        try:
            _remove(entry)
        except OSError as error:
            failed.append(dict(id=entry['id'], error=str(error)))
            continue
        deleted.append(entry['id'])
        freed += entry['bytes']
    return dict(deleted=deleted, freedBytes=freed, failed=failed)


def delete_entries(identifiers):
    """Delete managed entries the user selected one by one, imports
    included. The caller has confirmed that imports cannot be rebuilt."""
    if not isinstance(identifiers, list) or not 0 < len(identifiers) <= 10_000:
        raise ValueError('Choose the items to delete')
    wanted = set()
    for value in identifiers:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError('Invalid media cache item')
        wanted.add(value)
    deleted, freed, failed = [], 0, []
    for entry in cache_report()['entries']:
        if entry['id'] not in wanted:
            continue
        try:
            _remove(entry)
        except OSError as error:
            failed.append(dict(id=entry['id'], error=str(error)))
            continue
        deleted.append(entry['id'])
        freed += entry['bytes']
    return dict(deleted=deleted, freedBytes=freed, failed=failed)


install()
