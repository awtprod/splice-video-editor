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
import shutil
import string
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
_cache = {'stamp': None, 'links': {}}
_managed_path = None


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


def _stamp():
    try:
        stat = REGISTRY.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _links():
    """The registry, reloaded when another process (an export worker) or a
    previous launch changed the file."""
    with _lock:
        stamp = _stamp()
        if stamp != _cache['stamp']:
            links = {}
            if stamp is not None:
                try:
                    data = json.loads(REGISTRY.read_text(encoding='utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError(f'The linked media registry is damaged: {REGISTRY}') from None
                raw = data.get('links') if isinstance(data, dict) else None
                if not isinstance(raw, dict):
                    raise ValueError('The linked media registry is damaged')
                links = {key: value for key, value in raw.items() if _ID.fullmatch(key) and isinstance(value, dict)
                         and isinstance(value.get('path'), str) and isinstance(value.get('name'), str)
                         and isinstance(value.get('size'), int) and isinstance(value.get('mtimeNs'), int)}
            _cache.update(stamp=stamp, links=links)
        return _cache['links']


def _save(links):
    REGISTRY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=REGISTRY.parent, prefix='.linked-media-', suffix='.json')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as sink:
            json.dump({'version': 1, 'links': links}, sink, separators=(',', ':'))
        os.replace(temporary, REGISTRY)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    _cache.update(stamp=_stamp(), links=links)


def _absolute_file(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
        raise ValueError('Choose a file on this computer')
    if not os.path.isabs(value):
        raise ValueError('Linked media needs an absolute path')
    path = Path(value).resolve(strict=False)
    if not path.is_file():
        raise ValueError(f'{Path(value).name} is not a readable file')
    return path


def link(value, validate=None):
    """Register one user-picked file and return its source description.
    ``validate(path, name)`` may reject it before it is registered."""
    path = _absolute_file(value)
    message = camera_raw_error(path.name)
    if message:
        raise ValueError(message)
    if validate:
        validate(path, path.name)
    stat = path.stat()
    entry = dict(path=str(path), name=path.name, size=stat.st_size, mtimeNs=stat.st_mtime_ns,
                 fingerprint=fingerprint(path, stat.st_size), linkedAt=int(time.time()))
    identifier = hashlib.sha256(str(path).encode()).hexdigest()[:32]
    with _lock:
        links = dict(_links())
        if identifier not in links and len(links) >= MAX_LINKS:
            raise ValueError('The linked media registry is full')
        links[identifier] = entry
        _save(links)
    return dict(src=LINK_PREFIX + identifier + '/' + quote(path.name, safe=''), sourcePath=str(path),
                size=stat.st_size, fingerprint=entry['fingerprint'])


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


def resolve_media(src):
    """premiere._media_path, extended with registered linked originals."""
    identifier, entry = _entry(src)
    if entry is None:
        return _managed_path(src)
    path = Path(entry['path'])
    try:
        stat = path.stat()
    except OSError:
        raise ValueError(f'{entry["name"]} is offline: it moved or its drive is disconnected. Relink it.') from None
    if not path.is_file() or stat.st_size != entry['size'] or stat.st_mtime_ns != entry['mtimeNs']:
        raise ValueError(f'{entry["name"]} changed after it was linked. Relink it to confirm the new file.')
    return path


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


def _places():
    home = Path.home().resolve()
    places = [('Home', home)]
    for name in ('Movies', 'Videos', 'Desktop', 'Music', 'Pictures', 'Downloads'):
        if (home / name).is_dir():
            places.append((name, home / name))
    if sys.platform == 'win32':
        for letter in string.ascii_uppercase:
            drive = Path(letter + ':\\')
            if os.path.isdir(drive):
                places.append((letter + ':', drive))
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
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except OSError:
                continue
    return total


PROXY_NAMES = frozenset({'edit.mp4', 'seek.mp4', 'grade.mp4', 'edit.webm', 'edit.m4a', 'nested.mp4'})


def cache_report():
    """Managed copies and generated proxies under the media directory."""
    entries = []
    if MEDIA_ROOT.is_dir():
        for directory in MEDIA_ROOT.iterdir():
            if not directory.is_dir() or not _ID.fullmatch(directory.name):
                continue
            files = sorted(p.name for p in directory.iterdir() if p.is_file())
            kind = 'proxy' if files and all(n in PROXY_NAMES or n.endswith('-audio.wav') for n in files) else 'import'
            entries.append(dict(id=directory.name, kind=kind, files=files[:8], bytes=_tree_size(directory),
                                modified=int(directory.stat().st_mtime)))
    entries.sort(key=lambda e: -e['bytes'])
    links = _links()
    free = shutil.disk_usage(MEDIA_ROOT if MEDIA_ROOT.is_dir() else MEDIA_ROOT.parent if MEDIA_ROOT.parent.is_dir() else Path.home())
    return dict(directory=str(MEDIA_ROOT), entries=entries,
                importBytes=sum(e['bytes'] for e in entries if e['kind'] == 'import'),
                proxyBytes=sum(e['bytes'] for e in entries if e['kind'] == 'proxy'),
                linkedFiles=len(links), linkedBytes=sum(e['size'] for e in links.values()), freeBytes=free.free)


def referenced_ids(sources):
    if not isinstance(sources, list) or len(sources) > 1_000_000:
        raise ValueError('Invalid list of referenced media')
    ids = set()
    for value in sources:
        if not isinstance(value, str):
            raise ValueError('Invalid list of referenced media')
        match = re.match(r'/media/([a-f0-9]{32})/', value)
        if match:
            ids.add(match.group(1))
    return ids


def prune(sources, min_age=CACHE_MIN_AGE):
    """Delete managed copies and proxies no listed source uses. Entries
    touched in the last ``min_age`` seconds may belong to an import or proxy
    still in progress and are kept."""
    keep = referenced_ids(sources)
    deleted, freed, failed = [], 0, []
    now = time.time()
    for entry in cache_report()['entries']:
        directory = MEDIA_ROOT / entry['id']
        if entry['id'] in keep or now - directory.stat().st_mtime < min_age:
            continue
        try:
            shutil.rmtree(directory)
        except OSError as error:
            failed.append(dict(id=entry['id'], error=str(error)))
            continue
        deleted.append(entry['id'])
        freed += entry['bytes']
    return dict(deleted=deleted, freedBytes=freed, failed=failed)


install()
