"""The ``/api/resolve/*`` routes. server.py registers them in its route table with ``@post``, so
they pass the same request checks (session cookie, page token, same-origin fetch) as every other
/api/ route before any of this runs."""
import json

from . import ResolveError, list_library, open_drp, open_library_project, originals
from .media import link_local
from .source import MAX_DRP_BYTES, load_drp

MAX_JSON = 64 * 1024
#: A save request carries every chosen timeline as Splice's project JSON.
MAX_SAVE_JSON = 48 * 1024 * 1024

__all__ = ['MAX_DRP_BYTES', 'MAX_JSON', 'MAX_SAVE_JSON', 'open_route', 'library_list_route', 'library_open_route',
           'seed_route', 'save_route']


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


def save_route(handler, body):
    """POST /api/resolve/library/save: check (``confirm`` false) or save Splice timelines as a new
    project in a Resolve disk library (see resolve.save)."""
    def work():
        import media_links
        from . import drp_seed, save
        from .source import scratch_folder
        return save.run(_json_body(body), originals=lambda kind, sha: originals.find(kind, sha),
                        scratch=scratch_folder(), resolve_media=media_links.resolve_media,
                        drp_seed=drp_seed.make_seed)
    _answer(handler, work)
