"""The ``/api/resolve/*`` routes, kept here so server.py needs one dispatch line."""
import json

from . import ResolveError, list_library, open_drp, open_library_project
from .media import link_local
from .source import MAX_DRP_BYTES

MAX_JSON = 64 * 1024


def _body(handler, limit):
    length = int(handler.headers.get('Content-Length', '0'))
    if length <= 0 or length > limit:
        raise ResolveError('Upload size is invalid')
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ResolveError('Incomplete request')
    return body


def _json_body(handler):
    try:
        payload = json.loads(_body(handler, MAX_JSON))
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


def _path(payload):
    value = payload.get('path')
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or '\x00' in value:
        raise ResolveError('Enter the path of a folder on this computer.')
    return value.strip()


def handle_post(handler, route, query):
    """Answer one POST under /api/resolve/ (the caller has already checked the request is local)."""
    try:
        if route == '/api/resolve/open':
            data = _body(handler, MAX_DRP_BYTES)
            index = _index(query.get('timeline', [None])[0])
            handler._json(200, {'project': open_drp(data, index, link_local)})
        elif route == '/api/resolve/library/list':
            projects, truncated = list_library(_path(_json_body(handler)))
            handler._json(200, {'projects': projects, 'truncated': truncated})
        elif route == '/api/resolve/library/open':
            payload = _json_body(handler)
            project = open_library_project(_path(payload), _index(payload.get('timeline')), link_local)
            handler._json(200, {'project': project})
        else:
            handler.send_error(404)
    except (ResolveError, ValueError) as error:
        handler._json(400, {'error': str(error)})
    except (BrokenPipeError, ConnectionResetError):
        return
    except Exception as error:  # reported like server.py's other routes, never a dropped connection
        handler._json(500, {'error': f'The Resolve project could not be opened ({type(error).__name__}: {error}).'})
