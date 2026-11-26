"""The ``/api/resolve/*`` routes. server.py registers them in its route table with ``@post``, so
they pass the same request checks (session cookie, page token, same-origin fetch) as every other
/api/ route before any of this runs."""
import json
import os
import tempfile
import threading
from pathlib import Path

from . import ResolveError, list_library, open_drp, open_library_project, originals
from .media import link_local
from .source import MAX_DRP_BYTES, load_drp

MAX_JSON = 64 * 1024
#: A save request carries every chosen timeline as Splice's project JSON.
MAX_SAVE_JSON = 48 * 1024 * 1024

__all__ = ['MAX_DRP_BYTES', 'MAX_JSON', 'MAX_SAVE_JSON', 'open_route', 'library_list_route', 'library_open_route',
           'seed_route', 'save_route', 'clean_staging_at_start']
#: Libraries Splice saved into (their ``Resolve Projects`` folders), kept so the server can remove
#: what a killed save left there when it starts again.
MAX_REMEMBERED = 32


def _json_body(body):
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ResolveError('Expected a JSON request') from None
    if not isinstance(payload, dict):
        raise ResolveError('Expected a JSON object')
    return payload


def _index(value):
    if value is None:
        return 0
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ResolveError('Invalid timeline')
    return value


def _timeline_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ResolveError('Invalid timeline')
    return value


def _path(payload):
    value = payload.get('path')
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or '\x00' in value:
        raise ResolveError('Enter the path of a folder on this computer.')
    return value.strip()


def _answer(handler, work):
    """Run ``work`` and reply; a failure is reported in the body, never a dropped connection."""
    try:
        handler._json(200, work())
    except (ResolveError, ValueError) as error:
        handler._json(400, {'error': str(error)})
    except (BrokenPipeError, ConnectionResetError):
        return
    except Exception as error:
        handler._json(500, {'error': f'The Resolve request failed ({type(error).__name__}: {error}).'})


def open_route(handler, length, query):
    """POST /api/resolve/open?timeline=N|timelineId=ID with the .drp bytes as the body."""
    def work():
        data = handler.rfile.read(length)
        if len(data) != length:
            raise ResolveError('Incomplete request')
        index = _index(query.get('timeline', [None])[0])
        timeline_id = _timeline_id(query.get('timelineId', [None])[0])
        return {'project': open_drp(data, index, link_local, timeline_id)}
    _answer(handler, work)


def library_list_route(handler, body):
    def work():
        from .place import PlaceError, library_users
        path = _path(_json_body(body))
        projects, truncated = list_library(path)
        try:
            users = sorted(library_users(path)[2])
        except PlaceError:
            users = []
        return {'projects': projects, 'truncated': truncated, 'users': users}
    _answer(handler, work)


def _keep_library(copy, digest):
    originals.keep('library', digest, copy)


def library_open_route(handler, body):
    def work():
        payload = _json_body(body)
        return {'project': open_library_project(_path(payload), _index(payload.get('timeline')), link_local,
                                                timeline_id=_timeline_id(payload.get('timelineId')),
                                                keep=_keep_library)}
    _answer(handler, work)


def seed_route(handler, length, query):
    """POST /api/resolve/seed with a .drp as the body: kept as a save's seed; returns its SHA-256."""
    def work():
        data = handler.rfile.read(length)
        if len(data) != length:
            raise ResolveError('Incomplete request')
        load_drp(data)  # only a readable Resolve project is kept
        return {'sha256': originals.keep_bytes('drp', data)}
    _answer(handler, work)


def _remembered_file():
    from splice_platform import data_directory
    return data_directory() / 'resolve-save-libraries.json'


def _remembered():
    try:
        value = json.loads(_remembered_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [v for v in value if isinstance(v, str)][:MAX_REMEMBERED] if isinstance(value, list) else []


def remember_library(root):
    """Note the ``Resolve Projects`` folder of a library a save is about to stage in."""
    root = str(root)
    known = _remembered()
    if root in known:
        return
    known = ([root] + known)[:MAX_REMEMBERED]
    path = _remembered_file()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.libraries-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(known, handle)
        os.replace(name, path)
    except OSError:
        pass


def clean_staging_at_start():
    """Remove what interrupted saves left in the libraries Splice saved into (in the background,
    so a library on a volume that does not answer cannot hold up the server's start)."""
    from .place import clean_stale

    def work():
        for root in _remembered():
            try:
                clean_stale(Path(root))
            except Exception:  # a library that went away or cannot be read: nothing to clean
                continue
    thread = threading.Thread(target=work, daemon=True, name='splice-resolve-staging-cleanup')
    thread.start()
    return thread


def save_route(handler, body):
    """POST /api/resolve/library/save: check (``confirm`` false) or save Splice timelines as a new
    project in a Resolve disk library (see resolve.save)."""
    def work():
        import media_links
        from . import drp_seed, save
        from .source import scratch_folder
        return save.run(_json_body(body), originals=lambda kind, sha: originals.find(kind, sha),
                        scratch=scratch_folder(), resolve_media=media_links.resolve_media,
                        drp_seed=drp_seed.make_seed, remember=remember_library)
    _answer(handler, work)
