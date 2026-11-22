"""Loopback server for the Linux editing workspace."""
import argparse
import os
import re
import shutil
from export_jobs import ExportJobs
import json
import hashlib
import mimetypes
import uuid
import tempfile
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs, quote

from premiere import open_project, save_project

ROOT = Path(__file__).resolve().parent
from premiere import MEDIA_ROOT
MAX_NATIVE_PROJECT = 50 * 1024 * 1024
# JSON also carries the base64 original graph plus the current editing model.
MAX_PROJECT = 112 * 1024 * 1024
MAX_MEDIA = 256 * 1024 ** 3
STATIC_EXTENSIONS = {'.html', '.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.webm', '.wav', '.mp3', '.m4a', '.mkv', '.aac', '.ogg', '.flac', '.avi', '.mxf', '.aiff', '.m4v', '.bmp', '.woff', '.woff2', '.ttf'}

class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, data, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
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

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._local_request():
            self.send_error(403)
            return
        path = unquote(urlsplit(self.path).path)
        if path == '/api/media/info':
            from media_proxy import describe
            try:
                self._json(200, describe(parse_qs(urlsplit(self.path).query).get('src',[''])[0]))
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                self._json(422, {'error':str(error)})
            return
        if path == '/api/media/waveform':
            from media_peaks import waveform
            try:
                self._json(200, {'peaks':waveform(parse_qs(urlsplit(self.path).query).get('src',[''])[0])})
            except (ValueError, OSError, TimeoutError, RuntimeError, subprocess.SubprocessError) as error:
                self._json(422, {'error':str(error)})
            return
        job = re.fullmatch(r'/api/exports/([a-f0-9]{32})(/file|/preview)?', path)
        if job:
            try:
                if job.group(2):
                    with self.server.jobs.open_file(job.group(1)) as source:
                        spec = self.server.jobs.status(job.group(1))
                        filename = Path(parse_qs(urlsplit(self.path).query).get('name', ['Splice export.' + spec['extension']])[0]).name
                        disposition = "attachment; filename*=UTF-8''" + quote(filename, safe='') if job.group(2) == '/file' else None
                        self._serve_file(source, spec['mime'], disposition)
                else:
                    self._json(200, self.server.jobs.status(job.group(1)))
            except KeyError:
                self._json(404, {'error': 'Export expired'})
            except ValueError as error:
                self._json(409, {'error': str(error)})
            return
        if path == '/':
            path = '/index.html'
        media_request = path.startswith('/media/')
        base = MEDIA_ROOT if media_request else ROOT
        relative = path[len('/media/'):] if media_request else path.lstrip('/')
        target = (base / relative).resolve()
        if not target.is_relative_to(base) or not target.is_file() or target.suffix.lower() not in STATIC_EXTENSIONS or '.git' in target.parts or 'vendor' in target.parts:
            self.send_error(404)
            return
        with target.open('rb') as source:
            self._serve_file(source, mimetypes.guess_type(target.name)[0] or 'application/octet-stream')

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
        route = urlsplit(self.path).path
        cancel = re.fullmatch(r'/api/exports/([a-f0-9]{32})/cancel', route)
        if cancel:
            try:
                self._json(200, self.server.jobs.status(cancel.group(1), cancel=True))
            except KeyError:
                self._json(404, {'error': 'Unknown export'})
            return
        if route not in ('/api/exports', '/api/media/proxy', '/api/premiere/sequence', '/api/premiere/open', '/api/premiere/save', '/api/media/upload', '/api/render'):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            limit = (MAX_MEDIA if route == '/api/media/upload' else
                     MAX_NATIVE_PROJECT + 1024 * 1024 if route == '/api/premiere/open' else MAX_PROJECT)
            if length <= 0 or length > limit:
                raise ValueError('Upload size is invalid')
            if route == '/api/media/upload':
                name = Path(parse_qs(urlsplit(self.path).query).get('name', ['media'])[0]).name
                suffix = Path(name).suffix.lower()
                if suffix not in {'.mp4', '.mov', '.mkv', '.webm', '.wav', '.mp3', '.m4a', '.jpg', '.jpeg', '.png', '.aac', '.ogg', '.flac', '.avi', '.mxf', '.aiff', '.m4v', '.bmp'}:
                    raise ValueError('Unsupported media extension')
                MEDIA_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
                if shutil.disk_usage(MEDIA_ROOT).free < length + 512 * 1024 ** 2:
                    raise ValueError('There is insufficient local space to import this file')
                digest = hashlib.sha256()
                with tempfile.NamedTemporaryFile(dir=MEDIA_ROOT, delete=False) as sink:
                    temporary = Path(sink.name)
                    try:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError('Incomplete upload')
                            digest.update(chunk)
                            sink.write(chunk)
                            remaining -= len(chunk)
                        sink.flush()
                        directory = MEDIA_ROOT / digest.hexdigest()[:32]
                        directory.mkdir(mode=0o700, exist_ok=True)
                        destination = directory / name
                        try:
                            os.link(temporary, destination)
                        except FileExistsError:
                            pass  # Identical bytes and filename already imported.
                    finally:
                        temporary.unlink(missing_ok=True)
                self._json(200, {'src':'/media/' + directory.name + '/' + quote(name,safe=''),
                                 'path':str(destination)})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete request')
            if route == '/api/media/proxy':
                from media_proxy import proxy
                payload=json.loads(body)
                self._json(200, {'src':proxy(payload['src'])})
                return
            if route == '/api/exports':
                payload = json.loads(body)
                self._json(202, {'id': self.server.jobs.start(payload['project'], payload.get('options', {}))})
                return
            if route == '/api/premiere/sequence':
                payload = json.loads(body)
                original = save_project(payload['project'])
                self._json(200, {'project': open_project(original, payload['index'])})
                return
            if route == '/api/render':
                from render_movie import render_to_file
                payload = json.loads(body)
                with tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / 'export.mp4'
                    render_to_file(payload['project'], output)
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Disposition', 'attachment; filename=export.mp4')
                    self.send_header('Content-Length', str(output.stat().st_size))
                    self.end_headers()
                    with output.open('rb') as source:
                        while chunk := source.read(1024 * 1024):
                            self.wfile.write(chunk)
                return
            if route == '/api/premiere/save':
                payload = json.loads(body)
                data = save_project(payload['project'])
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', 'attachment; filename="project.prproj"')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            content_type = self.headers.get('Content-Type', '')
            if not content_type.startswith('multipart/form-data;') or 'boundary=' not in content_type:
                raise ValueError('Expected a file upload')
            from email.parser import BytesParser
            from email.policy import default
            message = BytesParser(policy=default).parsebytes(b'MIME-Version: 1.0\r\nContent-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + body)
            part = next((p for p in message.iter_parts() if p.get_param('name', header='content-disposition') == 'file'), None)
            if part is None:
                raise ValueError('Missing file')
            data = part.get_payload(decode=True)
            self._json(200, {'project': open_project(data)})
        except (ValueError, KeyError, TypeError) as error:
            self._json(400, {'error': str(error)})
        except Exception as error:
            self._json(500, {'error': str(error)})


class StudioServer(ThreadingHTTPServer):
    def __init__(self, *args):
        self.jobs = ExportJobs()
        super().__init__(*args)

    def server_close(self):
        self.jobs.shutdown()
        super().server_close()


def create_server(port=0):
    return StudioServer(('127.0.0.1', port), Handler)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.port)
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    with server:
        server.serve_forever()
