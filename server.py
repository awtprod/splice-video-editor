"""Loopback server for the Linux editing workspace."""
import argparse
import json
import mimetypes
import uuid
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs, quote

from premiere import open_project, save_project

ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = Path.home() / '.local' / 'share' / 'splice' / 'media'
MAX_PROJECT = 50 * 1024 * 1024
MAX_MEDIA = 1024 * 1024 * 1024
STATIC_EXTENSIONS = {'.html', '.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.webm', '.wav', '.mp3', '.woff', '.woff2', '.ttf'}

class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, data, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._reply(status, json.dumps(payload).encode(), 'application/json')

    def _local_request(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        expected = f'127.0.0.1:{self.server.server_port}'
        return host == expected and (origin is None or origin == f'http://{expected}')

    def do_GET(self):
        if not self._local_request():
            self.send_error(403)
            return
        path = unquote(urlsplit(self.path).path)
        if path == '/':
            path = '/index.html'
        media_request = path.startswith('/media/')
        base = MEDIA_ROOT if media_request else ROOT
        relative = path[len('/media/'):] if media_request else path.lstrip('/')
        target = (base / relative).resolve()
        if not target.is_relative_to(base) or not target.is_file() or target.suffix.lower() not in STATIC_EXTENSIONS or '.git' in target.parts or 'vendor' in target.parts:
            self.send_error(404)
            return
        size = target.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get('Range', '')
        if range_header.startswith('bytes='):
            try:
                lower, upper = range_header[6:].split('-', 1)
                if lower:
                    start = int(lower)
                    end = min(int(upper), end) if upper else end
                else:
                    count = int(upper)
                    if count <= 0:
                        raise ValueError()
                    start = max(0, size - count)
                if start < 0 or start > end:
                    raise ValueError()
            except ValueError:
                self.send_error(416)
                return
        self.send_response(206 if range_header else 200)
        self.send_header('Content-Type', mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges', 'bytes')
        if range_header:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        with target.open('rb') as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        if not self._local_request():
            self.send_error(403)
            return
        self.connection.settimeout(15)
        route = urlsplit(self.path).path
        if route not in ('/api/premiere/open', '/api/premiere/save', '/api/media/upload', '/api/render'):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            limit = MAX_MEDIA if route == '/api/media/upload' else MAX_PROJECT
            if length <= 0 or length > limit:
                raise ValueError('Upload size is invalid')
            if route == '/api/media/upload':
                name = Path(parse_qs(urlsplit(self.path).query).get('name', ['media'])[0]).name
                suffix = Path(name).suffix.lower()
                if suffix not in {'.mp4', '.mov', '.mkv', '.webm', '.wav', '.mp3', '.m4a', '.jpg', '.jpeg', '.png'}:
                    raise ValueError('Unsupported media extension')
                directory = MEDIA_ROOT / uuid.uuid4().hex
                directory.mkdir(parents=True, exist_ok=False)
                destination = directory / name
                try:
                    with destination.open('wb') as sink:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError('Incomplete upload')
                            sink.write(chunk)
                            remaining -= len(chunk)
                except Exception:
                    destination.unlink(missing_ok=True)
                    directory.rmdir()
                    raise
                self._json(200, {'src': '/media/' + directory.name + '/' + quote(name, safe=''), 'path': str(destination)})
                return
            body = self.rfile.read(length)
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


def create_server(port=0):
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.port)
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()
