"""Loopback server for the Splice editing workspace.

Every /api/ route and every request for user media needs proof that it comes
from the Splice page this server handed out:

* ``Sec-Fetch-Site``, when the browser sends it, must be ``same-origin`` or
  ``none``. Pages on other sites, including other localhost ports, are
  refused even for <img>/<audio> GETs that carry no Origin header.
* A per-launch session cookie (HttpOnly, SameSite=Strict). The only way to
  get it is the one-time launch link ``/?launch=<token>``: the desktop
  launcher opens it, and ``python3 server.py`` prints it. Media elements and
  downloads carry the cookie.
* An ``X-Splice-Token`` header, carrying a per-launch token embedded in the
  app page, on every /api/ request except the two routes that <video> and
  download links read directly.

Routes live in GET_ROUTES and POST_ROUTES; add a route by decorating a
function with @get(path) or @post(path).
"""
import argparse
import hmac
import os
import re
import secrets
import shutil
import signal
import sys
from export_jobs import ExportJobs
import json
import hashlib
import mimetypes
import tempfile
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs, quote

from premiere import open_project, save_project

ROOT = Path(__file__).resolve().parent
from premiere import MEDIA_ROOT
import media_links
from media_proxy import audition_audio, decodable, describe, nested_preview, proxy
from media_peaks import waveform

MAX_NATIVE_PROJECT = 50 * 1024 * 1024
# JSON also carries the base64 original graph plus the current editing model.
MAX_PROJECT = 112 * 1024 * 1024
MAX_MEDIA = 256 * 1024 ** 3
MAX_SMALL_BODY = 64 * 1024 * 1024
STATIC_EXTENSIONS = {'.html', '.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.webm', '.wav', '.mp3', '.m4a', '.mkv', '.aac', '.ogg', '.flac', '.avi', '.mxf', '.aiff', '.m4v', '.bmp', '.woff', '.woff2', '.ttf'}
TOKEN_HEADER = 'X-Splice-Token'
# Read by <video> elements and download links, which cannot send a header.
ELEMENT_ROUTES = re.compile(r'/api/exports/[a-f0-9]{32}/(?:file|preview)')
FORBIDDEN_PAGE = (b'<!doctype html><meta charset="utf-8"><title>Splice</title>'
                  b'<h1>Open Splice from its launch link</h1>'
                  b'<p>For your security this address only works in the browser that opened Splice\'s one-time '
                  b'launch link. Use the desktop launcher, or open the full link that <code>python3 server.py</code> '
                  b'printed in its terminal (it ends in <code>?launch=</code> and a code). If you already used that '
                  b'link in another browser, stop the server and start it again for a new one.</p>')
# A launch from a page on another origin (the desktop launcher's local
# redirect file) arrives cross-site, and a SameSite=Strict cookie is not sent
# on the rest of that redirect chain. This page continues as a same-origin
# navigation instead.
CONTINUE_PAGE = (b'<!doctype html><meta charset="utf-8"><title>Splice</title>'
                 b'<meta http-equiv="refresh" content="0;url=/"><script>location.replace("/")</script>'
                 b'<p><a href="/">Open Splice</a></p>')
LAUNCH_PARAMETER = re.compile(r'(launch=)[^&\s"]*')

GET_ROUTES = {}
POST_ROUTES = {}


def get(path):
    def register(function):
        GET_ROUTES[path] = function
        return function
    return register


def post(path, limit=MAX_PROJECT, stream=False):
    """``stream`` handlers read the request body themselves."""
    def register(function):
        POST_ROUTES[path] = (function, limit, stream)
        return function
    return register


def _query_int(query, name, default):
    value = query.get(name, [None])[0]
    if value is None or value == '':
        return default
    if not re.fullmatch(r'-?\d{1,9}', value):
        raise ValueError(f'Invalid {name}')
    return int(value)


def _json_body(body):
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object')
    return payload


@get('/api/media/info')
def media_info(handler, query):
    handler._json(200, describe(query.get('src', [''])[0]))


@get('/api/media/waveform')
def media_waveform(handler, query):
    handler._json(200, {'peaks': waveform(query.get('src', [''])[0], _query_int(query, 'stream', 0))})


@get('/api/media/audition')
def media_audition(handler, query):
    try:
        rate = float(query.get('rate', ['1'])[0])
    except ValueError:
        raise ValueError('Invalid audio preview speed') from None
    audio = audition_audio(query.get('src', [''])[0], _query_int(query, 'index', -1), rate,
                           query.get('reverse', ['0'])[0] == '1', _query_int(query, 'stream', 0))
    handler._reply(200, audio, 'audio/wav')


@get('/api/files')
def browse_files(handler, query):
    handler._json(200, media_links.browse(query.get('path', [''])[0]))


@get('/api/media/cache')
def media_cache(handler, query):
    handler._json(200, media_links.cache_report())


@post('/api/media/cache/prune')
def prune_cache(handler, body):
    """Derived files (proxies, previews) only. Imported copies are deleted
    one by one through /api/media/cache/delete."""
    payload = _json_body(body)
    handler._json(200, media_links.prune(payload.get('referenced')))


@post('/api/media/cache/delete')
def delete_cache_entries(handler, body):
    payload = _json_body(body)
    if payload.get('confirm') is not True:
        raise ValueError('Confirm that imported copies cannot be rebuilt')
    handler._json(200, media_links.delete_entries(payload.get('ids')))


@post('/api/media/links/forget')
def forget_links(handler, body):
    """Forget linked originals (the files are not touched): the listed ids,
    or every link the listed project sources do not use."""
    payload = _json_body(body)
    if isinstance(payload.get('ids'), list):
        removed = media_links.forget(payload['ids'])
    else:
        removed = media_links.forget_unreferenced(payload.get('referenced'))
    handler._json(200, {'forgotten': len(removed)})


@post('/api/media/link', limit=MAX_SMALL_BODY)
def link_media(handler, body):
    """Reference user-picked originals in place. No bytes are copied. The
    registry is written once for the whole batch."""
    payload = _json_body(body)
    if isinstance(payload.get('folder'), str):
        chosen = media_links.folder_files(payload['folder'])
    else:
        paths = payload.get('paths')
        if not isinstance(paths, list) or not 0 < len(paths) <= media_links.MAX_LINK_BATCH:
            raise ValueError(f'Choose between 1 and {media_links.MAX_LINK_BATCH} files')
        chosen = [(path, None) for path in paths]
    results = media_links.link_many([path for path, _ in chosen], validate=decodable)
    linked = [{**result, 'name': Path(result['sourcePath']).name if 'sourcePath' in result else Path(str(path)).name,
               'relativePath': relative} for result, (path, relative) in zip(results, chosen)]
    handler._json(200, {'files': linked, 'notice': media_links.recovery_notice()})


@post('/api/media/upload', limit=MAX_MEDIA, stream=True)
def upload_media(handler, length, query):
    name = Path(query.get('name', ['media'])[0]).name
    if not name or name in ('.', '..') or len(name) > 255:
        raise ValueError('Invalid file name')
    message = media_links.camera_raw_error(name)
    if message:
        raise ValueError(message)
    MEDIA_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(MEDIA_ROOT).free < length + 512 * 1024 ** 2:
        raise ValueError('There is insufficient local space to import this file')
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(dir=MEDIA_ROOT, prefix='.import-') as staging:
        temporary = Path(staging) / ('upload' + Path(name).suffix.lower())
        with temporary.open('wb') as sink:
            remaining = length
            while remaining:
                chunk = handler.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('Incomplete upload')
                digest.update(chunk)
                sink.write(chunk)
                remaining -= len(chunk)
        decodable(temporary, name)
        directory = MEDIA_ROOT / digest.hexdigest()[:32]
        directory.mkdir(mode=0o700, exist_ok=True)
        media_links.mark(directory, 'import')
        destination = directory / name
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass  # Identical bytes and filename already imported.
    handler._json(200, {'src': '/media/' + directory.name + '/' + quote(name, safe=''), 'path': str(destination)})


@post('/api/media/nested-preview')
def media_nested_preview(handler, body):
    handler._json(200, {'src': nested_preview(_json_body(body)['project'])})


@post('/api/media/proxy')
def media_proxy_route(handler, body):
    payload = _json_body(body)
    handler._json(200, {'src': proxy(payload['src'], seek_optimized=payload.get('seekOptimized') is True,
                                     audio_stream=payload.get('audioStream'),
                                     grading_data=payload.get('gradingData') is True)})


@post('/api/exports')
def start_export(handler, body):
    payload = _json_body(body)
    handler._json(202, {'id': handler.server.jobs.start(payload['project'], payload.get('options', {}))})


@post('/api/premiere/sequence')
def premiere_sequence(handler, body):
    payload = _json_body(body)
    original = save_project(payload['project'])
    handler._json(200, {'project': open_project(original, payload['index'])})


@post('/api/premiere/save')
def premiere_save(handler, body):
    data = save_project(_json_body(body)['project'])
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/octet-stream')
    handler.send_header('Content-Disposition', 'attachment; filename="project.prproj"')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(data)


@post('/api/premiere/open', limit=MAX_NATIVE_PROJECT + 1024 * 1024)
def premiere_open(handler, body):
    content_type = handler.headers.get('Content-Type', '')
    if not content_type.startswith('multipart/form-data;') or 'boundary=' not in content_type:
        raise ValueError('Expected a file upload')
    from email.parser import BytesParser
    from email.policy import default
    message = BytesParser(policy=default).parsebytes(b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    part = next((p for p in message.iter_parts() if p.get_param('name', header='content-disposition') == 'file'), None)
    if part is None:
        raise ValueError('Missing file')
    handler._json(200, {'project': open_project(part.get_payload(decode=True))})


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, data, content_type, headers=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, payload):
        self._reply(status, json.dumps(payload).encode(), 'application/json')

    def _local_request(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        expected = f'127.0.0.1:{self.server.server_port}'
        return host == expected and (origin is None or origin == f'http://{expected}')

    def _same_site_fetch(self):
        return self.headers.get('Sec-Fetch-Site') in (None, 'same-origin', 'none')

    def _session(self):
        """Any cookie named like ours holding this launch's session. Parsed
        by hand: cookies on 127.0.0.1 are shared by every local port, and
        http.cookies stops at the first value another app formats loosely
        (JSON, spaces, non-ASCII), which would lock Splice out."""
        expected = self.server.session.encode()
        found = False
        for header in self.headers.get_all('Cookie') or ():
            for pair in header.split(';'):
                name, separator, value = pair.partition('=')
                if separator and name.strip() == self.server.cookie_name:
                    value = value.strip().strip('"').encode('utf-8', 'surrogateescape')
                    found = hmac.compare_digest(value, expected) or found
        return found

    def log_message(self, format, *args):
        # Launch links are secrets until used: never write them to the log.
        super().log_message('%s', LAUNCH_PARAMETER.sub(r'\1…', format % args))

    def _authorized(self, path):
        if not (self._local_request() and self._same_site_fetch() and self._session()):
            return False
        if path.startswith('/api/') and not ELEMENT_ROUTES.fullmatch(path):
            token = self.headers.get(TOKEN_HEADER, '')
            return hmac.compare_digest(token.encode(), self.server.page_token.encode())
        return True

    def _refuse(self, path):
        if path.startswith('/api/'):
            self._json(403, {'error': 'Splice refused a request that did not come from its own window.'})
        else:
            self.send_error(403)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._local_request():
            self.send_error(403)
            return
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        if path in ('/', '/index.html'):
            self._serve_app(query)
            return
        protected = path.startswith('/api/') or path.startswith('/media/')
        if protected and not self._authorized(path):
            self._refuse(path)
            return
        route = GET_ROUTES.get(path)
        if route:
            try:
                route(self, query)
            except (BrokenPipeError, ConnectionResetError):
                return
            except (ValueError, OSError, TimeoutError, RuntimeError, subprocess.SubprocessError) as error:
                self._json(422, {'error': str(error)})
            return
        job = re.fullmatch(r'/api/exports/([a-f0-9]{32})(/file|/preview)?', path)
        if job:
            try:
                if job.group(2):
                    with self.server.jobs.open_file(job.group(1)) as source:
                        spec = self.server.jobs.status(job.group(1))
                        filename = Path(query.get('name', ['Splice export.' + spec['extension']])[0]).name
                        disposition = "attachment; filename*=UTF-8''" + quote(filename, safe='') if job.group(2) == '/file' else None
                        self._serve_file(source, spec['mime'], disposition)
                else:
                    self._json(200, self.server.jobs.status(job.group(1)))
            except KeyError:
                self._json(404, {'error': 'Export expired'})
            except ValueError as error:
                self._json(409, {'error': str(error)})
            return
        if path.startswith('/media/'):
            # Managed copies and proxies, or originals the user linked.
            try:
                source = media_links.open_media(parts.path)
            except (ValueError, OSError):
                self.send_error(404)
                return
        else:
            target = (ROOT / path.lstrip('/')).resolve()
            if (not target.is_relative_to(ROOT) or not target.is_file() or target.suffix.lower() not in STATIC_EXTENSIONS
                    or '.git' in target.parts or 'vendor' in target.parts):
                self.send_error(404)
                return
            source = target.open('rb')
        with source:
            mime = mimetypes.guess_type(Path(source.name).name)[0] or 'application/octet-stream'
            if path.startswith('/media/') and (not mime.startswith(('video/', 'audio/', 'image/')) or mime == 'image/svg+xml'):
                mime = 'application/octet-stream'  # User files never render as pages on this origin.
            self._serve_file(source, mime)

    def _serve_app(self, query):
        """The only responses that hand out the session cookie (the one-time
        launch link) and the page token (the app page, to that cookie)."""
        server = self.server
        launch = query.get('launch', [''])[0]
        page_headers = [('X-Frame-Options', 'DENY'), ('Referrer-Policy', 'no-referrer'),
                        ('X-Content-Type-Options', 'nosniff')]
        if launch:
            if server.claim_launch(launch):
                # The token is the proof, so a browser that sends no
                # Sec-Fetch headers (Safari < 16.4, Firefox < 90) can launch.
                cookie = [('Set-Cookie', server.cookie())]
                if self._same_site_fetch():
                    self._reply(303, b'', 'text/plain', cookie + [('Location', '/')] + page_headers)
                else:
                    self._reply(200, CONTINUE_PAGE, 'text/html; charset=utf-8', cookie + page_headers)
            elif self._session() and self._same_site_fetch():
                # The printed link opened again in the browser that used it.
                self._reply(303, b'', 'text/plain', [('Location', '/')] + page_headers)
            else:
                self._reply(403, FORBIDDEN_PAGE, 'text/html; charset=utf-8', page_headers)
            return
        if not (self._session() and self._same_site_fetch()):
            self._reply(403, FORBIDDEN_PAGE, 'text/html; charset=utf-8', page_headers)
            return
        page = (ROOT / 'index.html').read_bytes().replace(
            b'<head>', b'<head>\n    <meta name="splice-token" content="' + server.page_token.encode() + b'" />', 1)
        self._reply(200, page, 'text/html; charset=utf-8', page_headers)

    def _serve_file(self, source, mime, disposition=None):
        size = os.fstat(source.fileno()).st_size
        start, end = 0, size - 1
        header = self.headers.get('Range', '')
        if header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
            if match:
                lower, upper = match.groups()
                if lower:
                    start = int(lower)
                    end = min(int(upper), end) if upper else end
                elif upper:
                    start = max(0, size - int(upper))
            if not match or not any(match.groups()) or start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        self.send_response(206 if header else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if disposition:
            self.send_header('Content-Disposition', disposition)
        if header:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        if self.command == 'HEAD':
            return
        source.seek(start)
        remaining = end - start + 1
        try:
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return  # Browsers cancel range reads during seeks.

    def do_POST(self):
        if not self._local_request():
            self.send_error(403)
            return
        self.connection.settimeout(15)
        parts = urlsplit(self.path)
        route = parts.path
        if not self._authorized(route):
            self._refuse(route)
            return
        cancel = re.fullmatch(r'/api/exports/([a-f0-9]{32})/cancel', route)
        if cancel:
            try:
                self._json(200, self.server.jobs.status(cancel.group(1), cancel=True))
            except KeyError:
                self._json(404, {'error': 'Unknown export'})
            return
        if route not in POST_ROUTES:
            self.send_error(404)
            return
        function, limit, stream = POST_ROUTES[route]
        try:
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ValueError('Upload size is invalid') from None
            if length <= 0 or length > limit:
                raise ValueError('Upload size is invalid')
            if stream:
                function(self, length, parse_qs(parts.query))
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete request')
            function(self, body)
        except (ValueError, KeyError, TypeError) as error:
            self._json(400, {'error': str(error)})
        except Exception as error:
            self._json(500, {'error': str(error)})


class StudioServer(ThreadingHTTPServer):
    def __init__(self, *args, launch_token):
        self.jobs = ExportJobs()
        # Per-launch secrets. The cookie proves a request comes from a page
        # this server served; the page token proves a fetch was made by it.
        self.session = secrets.token_urlsafe(32)
        self.page_token = secrets.token_urlsafe(32)
        self.launch_token = launch_token
        self._launch_claimed = False
        self._launch_lock = threading.Lock()
        self.on_launch = None
        super().__init__(*args)
        self.cookie_name = f'splice_session_{self.server_port}'

    def cookie(self):
        return f'{self.cookie_name}={self.session}; Path=/; HttpOnly; SameSite=Strict'

    def launch_url(self):
        return f'http://127.0.0.1:{self.server_port}/?launch={self.launch_token}'

    def claim_launch(self, token):
        """The launch link works once, so a copy seen later (shell history,
        a process list, browser history) grants nothing."""
        with self._launch_lock:
            if self._launch_claimed or not hmac.compare_digest(token.encode('utf-8', 'replace'),
                                                               self.launch_token.encode()):
                return False
            self._launch_claimed = True
        if self.on_launch:
            self.on_launch()
        return True

    def server_close(self):
        self.jobs.shutdown()
        super().server_close()


def create_server(port=0, launch_token=None):
    """The session cookie is issued only by the one-time launch link,
    ``server.launch_url()``. ``launch_token`` defaults to a fresh secret."""
    return StudioServer(('127.0.0.1', port), Handler, launch_token=launch_token or secrets.token_urlsafe(32))

def _exit_on_terminate(signum, frame):
    raise KeyboardInterrupt

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.port)
    with server:
        signal.signal(signal.SIGTERM,_exit_on_terminate)
        # The first stdout line is the one-time launch link (tests read it).
        print(server.launch_url(), flush=True)
        print('Open the link above in your browser to start Splice. It works once; restart the server for a new '
              f'one. http://127.0.0.1:{server.server_port}/ alone is refused.', file=sys.stderr, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
