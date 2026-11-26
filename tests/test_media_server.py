"""Server safety and media handling: request proof, bounded decoders,
link-in-place media, source metadata, and the managed media cache."""
from array import array
import gzip
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *map(str, arguments)], check=True, timeout=60)


def tone(path, frequencies, seconds=6, extension_args=()):
    """One audio stream per frequency (0 means silence) beside a small picture."""
    inputs = ['-f', 'lavfi', '-i', f'color=gray:s=64x36:r=25:d={seconds}']
    maps = ['-map', '0:v']
    for index, frequency in enumerate(frequencies, 1):
        source = f'sine=frequency={frequency}:sample_rate=48000:duration={seconds}' if frequency else \
                 f'anullsrc=r=48000:cl=mono:d={seconds}'
        inputs += ['-f', 'lavfi', '-i', source]
        maps += ['-map', f'{index}:a']
    ffmpeg(*inputs, *maps, '-c:v', 'mpeg4', '-c:a', 'pcm_s16le', *extension_args, path)


def dominant_frequency(wav_bytes):
    """Zero-crossing estimate over the left channel of 48 kHz s16 stereo."""
    start = wav_bytes.index(b'data') + 8
    samples = array('h', wav_bytes[start:start + (len(wav_bytes) - start) // 4 * 4])
    if sys.byteorder == 'big':
        samples.byteswap()
    left = samples[0::2][4800:4800 + 48000]
    crossings = sum(1 for a, b in zip(left, left[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(left) / 48000)


def _limit_memory():
    # The unfixed audition decoder allocates without bound; keep a failing
    # run from taking a shared machine down with it.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024 ** 3, 4 * 1024 ** 3))


class ServerProcess:
    """``python3 server.py`` with its own data directory, driven like the app."""

    def __init__(self, data):
        self.data = Path(data)
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, '-u', 'server.py', '--port', '0'], cwd=ROOT,
                                        env={**os.environ, 'SPLICE_DATA_DIR': str(self.data)},
                                        stdout=subprocess.PIPE, stderr=self.log,
                                        preexec_fn=_limit_memory if os.name == 'posix' else None)
        line = self.process.stdout.readline().decode().strip()
        match = re.fullmatch(r'http://127\.0\.0\.1:(\d+)/\?launch=([A-Za-z0-9_-]{20,})', line)
        if not match:
            self.stop()
            raise RuntimeError('Server did not start: ' + line)
        self.port = int(match.group(1))
        navigate = {'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
        status, headers, _ = self.request('GET', '/?launch=' + match.group(2), auth=False, headers=navigate)
        assert status == 303, status
        self.cookie = headers['set-cookie'].split(';', 1)[0]
        status, headers, page = self.request('GET', '/', token=False, headers=navigate)
        assert status == 200, status
        self.token = re.search(rb'name="splice-token" content="([^"]+)"', page).group(1).decode()

    def request(self, method, path, body=None, headers=None, auth=True, token=True, timeout=60):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        try:
            sent = {'Host': f'127.0.0.1:{self.port}'}
            if auth:
                sent['Cookie'] = self.cookie
                if token:
                    sent['X-Splice-Token'] = self.token
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
                sent['Content-Type'] = 'application/json'
            sent.update(headers or {})
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def json(self, method, path, body=None, **options):
        status, _, data = self.request(method, path, body, **options)
        return status, json.loads(data or b'{}')

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()

    def logs(self):
        self.log.seek(0)
        return self.log.read().decode(errors='replace')

    def close(self):
        self.stop()
        self.log.close()


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-media-server-')
        cls.data = Path(cls.temp.name) / 'data'
        cls.outside = Path(cls.temp.name) / 'card'
        cls.outside.mkdir()
        cls.server = ServerProcess(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.temp.cleanup()

    def media_bytes(self):
        root = self.data / 'media'
        return sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) if root.exists() else 0

    def link(self, path):
        status, data = self.server.json('POST', '/api/media/link', {'paths': [str(path)]})
        self.assertEqual(status, 200, data)
        entry = data['files'][0]
        self.assertNotIn('error', entry)
        return entry


class InProcessServer:
    """create_server() in this process, for launch-link checks."""

    def __init__(self, launch_token=None, quiet=True):
        from server import create_server
        self.server = create_server(0, launch_token=launch_token)
        self.port = self.server.server_port
        if quiet:
            self.server.RequestHandlerClass = type('QuietHandler', (self.server.RequestHandlerClass,),
                                                   {'log_message': lambda *args: None})

    def __enter__(self):
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)

    def get(self, path, headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request('GET', path, headers={'Host': f'127.0.0.1:{self.port}', **headers})
            response = connection.getresponse()
            return response.status, response.getheader('Set-Cookie'), response.read()
        finally:
            connection.close()


class RequestProof(ServerTestCase):
    def test_cross_site_requests_without_the_launch_secret_are_refused(self):
        src = '/api/media/info?src=' + quote('/assets/score.wav')
        # A cross-site <audio>/<img> GET: no Origin, no cookie, no token.
        self.assertEqual(self.server.request('GET', src, auth=False, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.server.request('GET', src, auth=False)[0], 403)
        # Another localhost port is same-site and may carry the cookie.
        for site in ('cross-site', 'same-site'):
            self.assertEqual(self.server.request('GET', src, headers={'Sec-Fetch-Site': site})[0], 403, site)
        self.assertEqual(self.server.request('GET', src, token=False)[0], 403)
        self.assertEqual(self.server.request('POST', '/api/media/link', {'paths': ['/etc/hostname']}, token=False)[0], 403)
        # A page elsewhere that opens the app in a new tab gets no cookie.
        status, headers, _ = self.server.request('GET', '/', auth=False, headers={
            'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})
        self.assertEqual(status, 403)
        self.assertNotIn('set-cookie', headers)
        # The app's own fetches.
        status, info = self.server.json('GET', src, headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(info['duration'], 32, delta=1)

    def test_media_elements_read_with_the_cookie_alone(self):
        clip = self.outside / 'element.mp4'
        shutil.copy(ROOT / 'assets' / 'ridge.mp4', clip)
        entry = self.link(clip)
        element = {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Dest': 'video', 'Range': 'bytes=0-99'}
        status, _, data = self.server.request('GET', entry['src'], token=False, headers=element)
        self.assertEqual((status, data), (206, clip.read_bytes()[:100]))
        self.assertEqual(self.server.request('GET', entry['src'], auth=False, headers=element)[0], 403)

    def test_legacy_render_route_cannot_bypass_the_export_lock(self):
        project = dict(name='Legacy', width=160, height=90, fps=25, clips=[],
                       media=[{'id': 'ridge', 'name': 'ridge', 'src': '/assets/ridge.mp4'}])
        status, _, _ = self.server.request('POST', '/api/render', {'project': project})
        self.assertEqual(status, 404)

    def test_launch_link_works_once(self):
        with InProcessServer('launch-secret') as server:
            navigate = {'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate'}
            self.assertEqual(server.get('/', navigate)[:2], (403, None))
            self.assertEqual(server.get('/?launch=wrong', navigate)[:2], (403, None))
            status, cookie, _ = server.get('/?launch=launch-secret', navigate)
            self.assertEqual(status, 303)
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
            self.assertEqual(server.get('/?launch=launch-secret', navigate)[:2], (403, None))
            # The same browser opening the printed link again just continues.
            session = {**navigate, 'Cookie': cookie.split(';')[0]}
            self.assertEqual(server.get('/?launch=launch-secret', session)[:2], (303, None))

    def test_server_py_issues_no_session_without_the_launch_link(self):
        # A local process imitating a browser navigation used to get a full
        # session (and with it /api/files and linking) from python3 server.py.
        with InProcessServer() as server:
            for headers in ({'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'},
                            {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'navigate'}, {}):
                status, cookie, page = server.get('/', headers)
                self.assertEqual((status, cookie), (403, None), headers)
                self.assertIn(b'launch', page)
                self.assertNotIn(b'splice-token', page)

    def test_launch_works_without_sec_fetch_headers(self):
        # Safari < 16.4 and Firefox < 90 send no Sec-Fetch headers.
        with InProcessServer('old-browser') as server:
            status, cookie, _ = server.get('/?launch=old-browser', {})
            self.assertEqual(status, 303)
            status, _, page = server.get('/', {'Cookie': cookie.split(';')[0]})
            self.assertEqual(status, 200)
            self.assertIn(b'splice-token', page)

    def test_cross_site_launch_continues_as_a_same_origin_navigation(self):
        # The desktop launcher opens a local file that forwards to the link.
        with InProcessServer('from-file') as server:
            status, cookie, page = server.get('/?launch=from-file', {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})
            self.assertEqual(status, 200)
            self.assertIn('SameSite=Strict', cookie)
            self.assertIn(b'location.replace("/")', page)
            self.assertNotIn(b'splice-token', page)

    def test_desktop_launcher_keeps_the_link_off_the_command_line(self):
        from splice_app import write_launcher
        with tempfile.TemporaryDirectory() as temp:
            launcher = write_launcher(Path(temp), 'http://127.0.0.1:1/?launch=abc&x=<y>')
            try:
                self.assertTrue(launcher.as_uri().startswith('file://'))
                self.assertNotIn('launch=', launcher.as_uri())
                self.assertIn('url=http://127.0.0.1:1/?launch=abc&amp;x=&lt;y&gt;', launcher.read_text())
                if os.name == 'posix':
                    self.assertEqual(launcher.stat().st_mode & 0o077, 0)
            finally:
                launcher.unlink()

    def test_the_desktop_launcher_file_is_removed_when_the_link_is_used(self):
        used = []
        with InProcessServer('once') as server:
            server.server.on_launch = lambda: used.append(1)
            server.get('/?launch=once', {'Sec-Fetch-Site': 'cross-site'})
            server.get('/?launch=once', {'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(used, [1])

    def test_launch_token_is_not_logged(self):
        import contextlib
        import io
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), InProcessServer('do-not-log-me', quiet=False) as server:
            server.get('/?launch=do-not-log-me', {'Sec-Fetch-Site': 'none'})
            server.get('/?launch=do-not-log-me&x=1', {'Sec-Fetch-Site': 'none'})
        self.assertIn('launch=', captured.getvalue())
        self.assertNotIn('do-not-log-me', captured.getvalue())

    def test_cookies_from_other_local_apps_do_not_lock_splice_out(self):
        # Cookies on 127.0.0.1 are shared by every port. One other app's
        # loosely formatted cookie used to hide Splice's session.
        src = '/api/media/info?src=' + quote('/assets/score.wav')
        name, value = self.server.cookie.split('=', 1)
        for jar in (f'prefs={{"theme":"dark"}}; {name}={value}', f'a=b c; ü=ö; {name}={value}; z="q', 
                    f'{name}=forged; {name}={value}', f'{name}={value}; {name}=forged', f'x; {name}="{value}"'):
            self.assertEqual(self.server.request('GET', src, headers={'Cookie': jar})[0], 200, jar)
        for jar in (f'{name}=forged', f'other_{name}={value}', f'{name}x={value}', f'prefs={{"a":"b"}}'):
            self.assertEqual(self.server.request('GET', src, headers={'Cookie': jar})[0], 403, jar)

    def test_origin_must_be_this_server(self):
        src = '/api/media/info?src=' + quote('/assets/score.wav')
        for origin in ('http://evil.example', 'null', f'http://localhost:{self.server.port}', 'http://127.0.0.1:1'):
            self.assertEqual(self.server.request('GET', src, headers={'Origin': origin})[0], 403, origin)
        self.assertEqual(self.server.request('GET', src, headers={'Origin': f'http://127.0.0.1:{self.server.port}'})[0], 200)

    def test_app_page_needs_a_same_origin_request_even_with_the_cookie(self):
        for site in ('cross-site', 'same-site'):
            status, headers, page = self.server.request('GET', '/', token=False,
                                                        headers={'Sec-Fetch-Site': site, 'Sec-Fetch-Mode': 'navigate'})
            self.assertEqual(status, 403, site)
            self.assertNotIn(self.server.token.encode(), page)
        status, headers, page = self.server.request('GET', '/', token=False, headers={'Sec-Fetch-Site': 'none'})
        self.assertEqual(status, 200)
        self.assertEqual(headers['x-frame-options'], 'DENY')
        self.assertEqual(headers['x-content-type-options'], 'nosniff')

    def test_user_files_never_render_as_pages(self):
        managed = self.data / 'media' / ('d' * 32)
        managed.mkdir(parents=True, exist_ok=True)
        for name in ('page.html', 'image.svg', 'notes.txt', 'clip.mp4'):
            (managed / name).write_bytes(b'<html><script>alert(1)</script>')
        for name, expected in (('page.html', 'application/octet-stream'), ('image.svg', 'application/octet-stream'),
                               ('notes.txt', 'application/octet-stream'), ('clip.mp4', 'video/mp4')):
            status, headers, _ = self.server.request('GET', f'/media/{"d" * 32}/{name}', token=False)
            self.assertEqual((status, headers['content-type']), (200, expected), name)
            self.assertEqual(headers['x-content-type-options'], 'nosniff')

    def test_only_the_exact_element_routes_skip_the_token(self):
        fake = 'a' * 32
        for path in (f'/api/files/api/exports/{fake}/file?path=%2F', f'/api/media/cache/api/exports/{fake}/preview',
                     f'/api/exports/{fake}/file/x'):
            self.assertEqual(self.server.request('GET', path, token=False)[0], 403, path)
        self.assertEqual(self.server.request('GET', f'/api/exports/{fake}/file', token=False)[0], 404)


class Relaunch(unittest.TestCase):
    """Getting back in without a restart, and launch callbacks that fail."""

    navigate = {'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}

    def test_a_failing_launch_callback_still_issues_the_session(self):
        import contextlib
        import io
        calls = []

        def locked_file():
            calls.append(1)
            raise PermissionError('The process cannot access the file because it is being used by another process')
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), InProcessServer('held-open') as server:
            server.server.on_launch = locked_file
            status, cookie, _ = server.get('/?launch=held-open', {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})
            self.assertEqual(status, 200)
            self.assertIn('SameSite=Strict', cookie)
            status, _, page = server.get('/', {**self.navigate, 'Sec-Fetch-Site': 'same-origin',
                                               'Cookie': cookie.split(';')[0]})
            self.assertEqual(status, 200)
            self.assertIn(b'splice-token', page)
            self.assertEqual(server.get('/?launch=held-open', self.navigate)[:2], (403, None))
        self.assertEqual(calls, [1])
        self.assertEqual(captured.getvalue().count('launch callback failed'), 1)
        self.assertIn('PermissionError', captured.getvalue())

    def test_stale_launch_files_are_removed_at_start(self):
        from splice_app import remove_stale_launchers, write_launcher
        with tempfile.TemporaryDirectory() as temp:
            profile, outside = Path(temp) / 'browser', Path(temp) / 'outside'
            profile.mkdir()
            outside.mkdir()
            old = time.time() - 3600
            stale = write_launcher(profile, 'http://127.0.0.1:1/?launch=old')
            fresh = write_launcher(profile, 'http://127.0.0.1:1/?launch=new')
            keep = {name: profile / name for name in ('launch-notes.txt', 'launch-ABCDEFGH.html', 'mylaunch-abcdefgh.html',
                                                      'launch-abcdefghi.html')}
            target = outside / 'launch-precious.html'
            target.write_text('not ours')
            for path in (*keep.values(), target):
                path.write_text('x')
            os.utime(stale, (old, old))
            for path in (*keep.values(), target):
                os.utime(path, (old, old))
            link = profile / 'launch-symlink1.html'
            os.symlink(target, link)
            nested = profile / 'launch-folder1.html'
            nested.mkdir()
            self.assertEqual(remove_stale_launchers(profile), [stale.name])
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists(), 'a launch file younger than a minute may belong to a starting window')
            self.assertTrue(all(path.exists() for path in keep.values()))
            self.assertTrue(link.is_symlink() and target.read_text() == 'x' and nested.is_dir())
            linked_profile = Path(temp) / 'linked-profile'
            os.symlink(outside, linked_profile)
            (outside / 'launch-abcdefgh.html').write_text('x')
            os.utime(outside / 'launch-abcdefgh.html', (old, old))
            self.assertEqual(remove_stale_launchers(linked_profile), [], 'never through a symlinked folder')
            self.assertTrue((outside / 'launch-abcdefgh.html').exists())

    def start(self, stdin):
        import io
        data = tempfile.TemporaryDirectory(prefix='splice-relaunch-')
        self.addCleanup(data.cleanup)
        process = subprocess.Popen([sys.executable, '-u', 'server.py', '--port', '0'], cwd=ROOT, stdin=stdin,
                                   env={**os.environ, 'SPLICE_DATA_DIR': data.name}, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(stop)
        return process

    def line(self, process, timeout=10):
        import select
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        return process.stdout.readline().decode().strip() if ready else None

    def launch(self, link, cookie=None):
        match = re.fullmatch(r'http://127\.0\.0\.1:(\d+)/\?launch=([A-Za-z0-9_-]{20,})', link or '')
        self.assertIsNotNone(match, link)
        connection = http.client.HTTPConnection('127.0.0.1', int(match.group(1)), timeout=10)
        try:
            headers = {'Host': f'127.0.0.1:{match.group(1)}', **self.navigate}
            if cookie:
                headers['Cookie'] = cookie
            connection.request('GET', '/?launch=' + match.group(2), headers=headers)
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader('Set-Cookie')
        finally:
            connection.close()

    @unittest.skipUnless(os.name == 'posix', 'a pseudo-terminal and SIGUSR1 are POSIX')
    def test_the_operator_gets_a_fresh_link_from_the_console_or_usr1(self):
        import pty
        import signal
        controller, terminal = pty.openpty()
        self.addCleanup(os.close, controller)
        process = self.start(terminal)
        os.close(terminal)
        first = self.line(process)
        status, cookie = self.launch(first)
        self.assertEqual(status, 303)
        # The browser restarted: its session cookie is gone and the printed link is spent.
        self.assertEqual(self.launch(first)[0], 403)
        os.write(controller, b'\n')  # Enter on the server's terminal.
        second = self.line(process)
        self.assertNotEqual(second, first)
        self.assertEqual(self.launch(second)[0], 303)
        self.assertEqual(self.launch(second)[0], 403, 'the new link works once too')
        process.send_signal(signal.SIGUSR1)
        third = self.line(process)
        self.assertEqual(self.launch(first)[0], 403)
        status, again = self.launch(third)
        self.assertEqual(status, 303)
        self.assertEqual(again.split(';')[0], cookie.split(';')[0], 'windows already open keep their session')
        self.assertEqual(process.poll(), None)

    def test_input_that_is_not_a_terminal_never_mints_a_link(self):
        # Another program feeding the server's stdin (a pipe) must not be able to ask for a link.
        process = self.start(subprocess.PIPE)
        first = self.line(process)
        process.stdin.write(b'\n' * 50)
        process.stdin.flush()
        self.assertIsNone(self.line(process, timeout=1.5), 'a pipe on stdin printed a new link')
        self.assertEqual(self.launch(first)[0], 303)
        process.stdin.close()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'job control on a pseudo-terminal; process state from /proc')
    def test_a_background_job_on_the_terminal_keeps_serving(self):
        """``python3 server.py &`` from a shell: the server is a background job of the terminal on
        its stdin. Reading that terminal would get it stopped (SIGTTIN); it must keep answering,
        read Enter only once ``fg`` makes it the foreground job, and survive Ctrl-Z then ``bg``."""
        import pty
        import select
        import signal
        controller, terminal = pty.openpty()
        self.addCleanup(os.close, controller)
        commands_read, commands = os.pipe()
        self.addCleanup(os.close, commands)
        data = tempfile.TemporaryDirectory(prefix='splice-job-')
        self.addCleanup(data.cleanup)
        # A minimal job-control shell: a new session on the pseudo-terminal, the server in its own
        # process group (a background job), and "fg"/"bg" commands that hand the terminal over.
        shell = f'''
import os, signal, sys, fcntl, termios
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    os.setpgid(0, 0)
    os.execv(sys.executable, [sys.executable, '-u', 'server.py', '--port', '0'])
os.setpgid(child, child)
print(child, file=sys.stderr, flush=True)
commands = os.fdopen({commands_read}, 'r')
for command in commands:
    command = command.strip()
    if command == 'fg':
        os.tcsetpgrp(0, child)
    elif command == 'stop':
        os.killpg(child, signal.SIGTSTP)
        os.waitpid(child, os.WUNTRACED)
        os.tcsetpgrp(0, os.getpgrp())
    elif command == 'bg':
        os.killpg(child, signal.SIGCONT)
    print('done', file=sys.stderr, flush=True)
'''
        wrapper = subprocess.Popen([sys.executable, '-c', shell], cwd=ROOT, stdin=terminal, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, pass_fds=(commands_read,),
                                   env={**os.environ, 'SPLICE_DATA_DIR': data.name})
        os.close(terminal)
        os.close(commands_read)
        server_pid = int(wrapper.stderr.readline())

        def cleanup():
            # A server stopped by the kernel would ignore SIGTERM until continued: kill exactly it.
            for pid in (server_pid, wrapper.pid):
                for sig in (signal.SIGCONT, signal.SIGKILL):
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            wrapper.wait(10)
            try:
                os.waitpid(server_pid, os.WNOHANG)
            except ChildProcessError:
                pass
            wrapper.stdout.close()
            wrapper.stderr.close()
        self.addCleanup(cleanup)

        def command(name):
            os.write(commands, name.encode() + b'\n')
            for _ in range(20):  # the server's own messages share this stream
                if wrapper.stderr.readline().strip() == b'done':
                    return
            self.fail(f'the job-control shell did not finish {name}')

        def state():
            fields = Path(f'/proc/{server_pid}/stat').read_text().rsplit(')', 1)[1].split()
            return fields[0], int(fields[11]) + int(fields[12])  # state, CPU ticks (user + system)

        def line(timeout):
            ready, _, _ = select.select([wrapper.stdout], [], [], timeout)
            return wrapper.stdout.readline().decode().strip() if ready else None

        first = line(10)
        self.assertIsNotNone(first)
        time.sleep(1.5)
        os.write(controller, b'\n')  # Enter typed while the server runs in the background
        self.assertIsNone(line(2), 'a background job does not read the terminal')
        self.assertNotEqual(state()[0], 'T', 'the kernel stopped the background server')
        self.assertEqual(self.launch(first)[0], 303, 'the background server answers')
        command('fg')
        second = line(5)  # the Enter queued while in the background is read once in front
        self.assertIsNotNone(second, 'fg brought the console back')
        self.assertNotEqual(second, first)
        os.write(controller, b'\n')
        third = line(5)
        self.assertEqual(self.launch(third)[0], 303, 'Enter in the foreground mints a link')
        self.assertEqual(self.launch(second)[0], 403)
        command('stop')  # Ctrl-Z
        self.assertEqual(state()[0], 'T')
        command('bg')
        time.sleep(1.5)
        ticks = state()[1]
        os.write(controller, b'\n')
        self.assertIsNone(line(2), 'back in the background, the terminal is not read')
        current, later = state()
        self.assertNotEqual(current, 'T', 'the server was stopped after bg')
        self.assertLess(later - ticks, 50, 'the background reader must not spin')
        os.kill(server_pid, signal.SIGUSR1)
        fourth = line(5)
        self.assertEqual(self.launch(fourth)[0], 303, 'USR1 still works in the background')
        self.assertIsNone(wrapper.poll())

    def test_no_request_can_produce_a_fresh_link(self):
        import inspect
        import server as server_module
        process = self.start(subprocess.DEVNULL)
        first = self.line(process)
        self.assertEqual(self.launch(first)[0], 303)
        port = int(first.split(':')[2].split('/')[0])
        probes = [('GET', '/?launch='), ('GET', '/?launch=new'), ('GET', '/?relaunch=1'), ('GET', '/api/launch'),
                  ('POST', '/api/launch'), ('POST', '/api/relaunch'), ('POST', '/'), ('GET', '/index.html?launch=1')]
        for method, path in probes:
            for headers in ({}, {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'http://127.0.0.1:1'}, self.navigate):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                try:
                    connection.request(method, path, body=b'{}' if method == 'POST' else None,
                                       headers={'Host': f'127.0.0.1:{port}', **headers})
                    response = connection.getresponse()
                    response.read()
                    self.assertIsNone(response.getheader('Set-Cookie'), (method, path, headers))
                finally:
                    connection.close()
        self.assertIsNone(self.line(process, timeout=1), 'no request printed a new link')
        self.assertEqual(self.launch(first)[0], 403)
        # Only the console reader and the signal handler call it; no route or request handler does.
        self.assertNotIn('new_launch_url', inspect.getsource(server_module.Handler))
        self.assertNotIn('print_new_launch', inspect.getsource(server_module.Handler))
        for function in [*server_module.GET_ROUTES.values(), *(f for f, _, _ in server_module.POST_ROUTES.values())]:
            self.assertNotIn('launch', inspect.getsource(function), function.__name__)
        with InProcessServer() as server:
            page = server.get('/', self.navigate)[2]
            self.assertIn(b'press Enter in the terminal', page)
            self.assertIn(b'USR1', page)


class LinkInPlace(ServerTestCase):
    def test_linked_file_plays_saves_reopens_and_exports_without_a_copy(self):
        clip = self.outside / 'A001 take 1.mov'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=25:d=2', '-f', 'lavfi', '-i', 'sine=440:sample_rate=48000:d=2',
               '-c:v', 'prores_ks', '-profile:v', '0', '-c:a', 'pcm_s16le', clip)
        before = self.media_bytes()
        entry = self.link(clip)
        self.assertEqual(entry['sourcePath'], str(clip.resolve()))
        self.assertRegex(entry['src'], r'^/media/linked/[a-f0-9]{32}/A001%20take%201\.mov$')
        self.assertEqual(self.media_bytes(), before, 'linking must not copy the original')
        status, _, data = self.server.request('GET', entry['src'], token=False)
        self.assertEqual((status, data), (200, clip.read_bytes()))
        status, info = self.server.json('GET', '/api/media/info?src=' + quote(entry['src']))
        self.assertEqual(status, 200, info)
        self.assertEqual(info['sourcePath'], str(clip.resolve()))
        project = dict(name='Linked', width=160, height=90, fps=25,
                       media=[dict(id='a', name=clip.name, src=entry['src'], **info)],
                       clips=[dict(id='c', mediaId='a', type='video', track=1, start=0, duration=1.5, **{'in': 0})])
        status, _, saved = self.server.request('POST', '/api/premiere/save', {'project': project})
        self.assertEqual(status, 200, saved[:200])
        self.assertIn(str(clip.resolve()).encode(), gzip.decompress(saved))
        boundary = uuid.uuid4().hex
        form = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="p.prproj"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n').encode() + saved + f'\r\n--{boundary}--\r\n'.encode()
        status, reopened = self.server.json('POST', '/api/premiere/open', form,
                                            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        self.assertEqual(status, 200, reopened)
        self.assertEqual(reopened['project']['media'][0]['path'], str(clip.resolve()))
        status, job = self.server.json('POST', '/api/exports', {'project': project, 'options': {'codec': 'h264'}})
        self.assertEqual(status, 202, job)
        deadline = time.monotonic() + 120
        while True:
            status, state = self.server.json('GET', '/api/exports/' + job['id'])
            if state['state'] != 'rendering' or time.monotonic() > deadline:
                break
            time.sleep(.25)
        self.assertEqual(state['state'], 'complete', state)
        status, _, movie = self.server.request('GET', f'/api/exports/{job["id"]}/file', token=False)
        self.assertEqual(status, 200)
        self.assertGreater(len(movie), 1000)
        self.assertEqual(self.media_bytes(), before, 'exporting a linked source must not copy it either')

    def test_moved_linked_file_goes_offline_and_relinks(self):
        clip = self.outside / 'moving.wav'
        ffmpeg('-f', 'lavfi', '-i', 'sine=440:sample_rate=48000:d=1', clip)
        entry = self.link(clip)
        self.assertEqual(self.server.request('HEAD', entry['src'], token=False)[0], 200)
        moved = self.outside / 'archive'
        moved.mkdir()
        clip.rename(moved / clip.name)
        self.assertEqual(self.server.request('HEAD', entry['src'], token=False)[0], 404)
        status, info = self.server.json('GET', '/api/media/info?src=' + quote(entry['src']))
        self.assertEqual(status, 422)
        self.assertIn('offline', info['error'])
        relinked = self.link(moved / clip.name)
        self.assertEqual(relinked['fingerprint'], entry['fingerprint'])
        self.assertEqual(self.server.request('HEAD', relinked['src'], token=False)[0], 200)
        # A changed file is offline too until the user confirms it.
        with (moved / clip.name).open('ab') as sink:
            sink.write(b'\0' * 4096)
        status, info = self.server.json('GET', '/api/media/info?src=' + quote(relinked['src']))
        self.assertEqual(status, 422)
        self.assertIn('changed', info['error'])

    def test_unregistered_paths_are_refused(self):
        secret = self.outside / 'private.wav'
        ffmpeg('-f', 'lavfi', '-i', 'sine=440:sample_rate=48000:d=1', secret)
        entry = self.link(self.outside / 'private.wav')
        identifier = entry['src'].split('/')[3]
        for src in (str(secret), 'file://' + str(secret), '/media/linked/' + 'a' * 32 + '/private.wav',
                    f'/media/linked/{identifier}/other.wav', '/media/../../' + str(secret).lstrip('/'),
                    '/media/linked/../' + identifier):
            status, info = self.server.json('GET', '/api/media/info?src=' + quote(src))
            self.assertEqual(status, 422, src)
        for path in ('/media/linked/' + 'a' * 32 + '/private.wav', f'/media/linked/{identifier}/other.wav',
                     '/media/%2e%2e/%2e%2e/' + quote(str(secret).lstrip('/'))):
            self.assertEqual(self.server.request('GET', path)[0], 404, path)

    def test_folder_link_lists_media_with_relative_paths(self):
        card = self.outside / 'CARD_B' / 'CLIPS'
        card.mkdir(parents=True)
        ffmpeg('-f', 'lavfi', '-i', 'sine=440:sample_rate=48000:d=1', card / 'B001.wav')
        (card / 'notes.txt').write_text('slate notes')
        (card / 'B002.braw').write_bytes(b'not decodable')
        status, data = self.server.json('POST', '/api/media/link', {'folder': str(self.outside / 'CARD_B')})
        self.assertEqual(status, 200)
        self.assertEqual([f['relativePath'] for f in data['files'] if 'error' not in f], ['CARD_B/CLIPS/B001.wav'])
        status, listing = self.server.json('GET', '/api/files?path=' + quote(str(card)))
        self.assertEqual(status, 200)
        kinds = {e['name']: e['kind'] for e in listing['entries']}
        self.assertEqual(kinds, {'B001.wav': 'media', 'B002.braw': 'raw', 'notes.txt': 'other'})

    def test_upload_accepts_what_ffmpeg_decodes_and_explains_camera_raw(self):
        transport = self.outside / 'clip.mts'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=25:d=1', '-c:v', 'mpeg2video', '-f', 'mpegts', transport)
        status, data = self.server.json('POST', '/api/media/upload?name=clip.mts', transport.read_bytes())
        self.assertEqual(status, 200, data)
        status, info = self.server.json('GET', '/api/media/info?src=' + quote(data['src']))
        self.assertEqual((status, info['type'], info['proxyRecommended']), (200, 'video', True))
        self.assertEqual(self.server.request('HEAD', data['src'], token=False)[0], 200)
        status, data = self.server.json('POST', '/api/media/upload?name=A001.braw', b'\0' * 1024)
        self.assertEqual(status, 400)
        self.assertIn('camera RAW', data['error'])
        self.assertIn('Blackmagic', data['error'])
        status, data = self.server.json('POST', '/api/media/upload?name=notes.mov', b'plain text, not media')
        self.assertEqual(status, 400)
        self.assertIn('cannot decode', data['error'])

    def test_cache_report_counts_copies_and_proxies_but_not_links(self):
        status, data = self.server.json('POST', '/api/media/upload?name=ridge.mp4', (ROOT / 'assets/ridge.mp4').read_bytes())
        self.assertEqual(status, 200)
        status, report = self.server.json('GET', '/api/media/cache')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(report['importBytes'], (ROOT / 'assets/ridge.mp4').stat().st_size)
        status, result = self.server.json('POST', '/api/media/cache/prune', {'referenced': [data['src']]})
        self.assertEqual((status, result['failed']), (200, []))
        self.assertNotIn(data['src'].split('/')[2], result['deleted'])


class AuditionBounds(ServerTestCase):
    def test_past_end_audition_is_bounded_and_fast(self):
        started = time.monotonic()
        status, _, audio = self.server.request(
            'GET', '/api/media/audition?' + 'src=%2Fassets%2Fscore.wav&index=2000&rate=1&reverse=0', timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertLessEqual(len(audio), 1024 * 1024)
        self.assertLess(elapsed, 3)
        self.assertFalse(any(audio[audio.index(b'data') + 8:]))
        status, _, ordinary = self.server.request('GET', '/api/media/audition?src=%2Fassets%2Fscore.wav&index=1&rate=1')
        self.assertEqual(status, 200)
        self.assertAlmostEqual(len(ordinary), 4 * 48000 * 4, delta=1024)

    def test_stream_selected_audition_returns_that_stream(self):
        source = self.outside / 'two-streams.mov'
        tone(source, (440, 1000))
        entry = self.link(source)
        heard = []
        for stream in (0, 1):
            status, _, audio = self.server.request(
                'GET', f'/api/media/audition?src={quote(entry["src"])}&index=0&rate=1&stream={stream}')
            self.assertEqual(status, 200, audio[:200])
            heard.append(dominant_frequency(audio))
        self.assertAlmostEqual(heard[0], 440, delta=25)
        self.assertAlmostEqual(heard[1], 1000, delta=40)
        status, _, _ = self.server.request('GET', f'/api/media/audition?src={quote(entry["src"])}&index=0&stream=2')
        self.assertEqual(status, 422)

    def test_waveform_reads_the_selected_stream_at_duration_scaled_resolution(self):
        source = self.outside / 'long.mkv'
        tone(source, (0, 700), seconds=660, extension_args=('-c:a', 'flac', '-r', '1'))
        entry = self.link(source)
        status, silent = self.server.json('GET', '/api/media/waveform?src=' + quote(entry['src']), timeout=200)
        self.assertEqual(status, 200, silent)
        status, loud = self.server.json('GET', f'/api/media/waveform?src={quote(entry["src"])}&stream=1', timeout=200)
        self.assertEqual(status, 200, loud)
        self.assertAlmostEqual(len(loud['peaks']), 660 * 20, delta=20)
        self.assertLess(max(silent['peaks']), .01)
        # FFmpeg's sine source peaks at 1/8 of full scale.
        self.assertGreater(min(loud['peaks'][10:-10]), .1)


class SourceMetadata(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='splice-describe-')
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def describe(self, path):
        from media_proxy import describe_path
        return describe_path(path)

    def test_drop_frame_prores_reports_rate_timecode_and_reel(self):
        clip = self.dir / 'A001C003.mov'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=192x108:r=30000/1001:d=1', '-c:v', 'prores_ks', '-profile:v', '0',
               '-timecode', '01:00:00;00', '-metadata:s:v:0', 'reel_name=A001C003', clip)
        info = self.describe(clip)
        self.assertEqual(info['sourcePath'], str(clip))
        self.assertEqual(info['rate'], '30000/1001')
        self.assertEqual(info['timecode'], '01:00:00;00')
        self.assertTrue(info['dropFrame'])
        self.assertEqual(info['reel'], 'A001C003')
        self.assertFalse(info['vfr'])
        self.assertEqual(info['pixelFormat'], 'yuv422p10le')

    def test_variable_frame_rate_phone_clip_is_detected(self):
        # A phone that drops to 20 fps in low light for the first second.
        phone = self.dir / 'phone.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=30:d=2', '-vf', "setpts='if(lt(N,20),N/20,1+(N-20)/30)/TB'",
               '-fps_mode', 'passthrough', '-c:v', 'libx264', '-preset', 'ultrafast', '-video_track_timescale', '600', phone)
        info = self.describe(phone)
        self.assertTrue(info['vfr'])
        self.assertTrue(info['proxyRecommended'])
        steady = self.dir / 'steady.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=30:d=2', '-c:v', 'libx264', '-preset', 'ultrafast', steady)
        self.assertFalse(self.describe(steady)['vfr'])
        self.assertEqual(self.describe(steady)['rate'], '30/1')
        self.assertFalse(self.describe(steady)['proxyRecommended'])

    def test_multiple_audio_streams_are_listed(self):
        clip = self.dir / 'multi.mov'
        ffmpeg('-f', 'lavfi', '-i', 'color=gray:s=64x36:r=25:d=1', '-f', 'lavfi', '-i', 'sine=440:d=1',
               '-f', 'lavfi', '-i', 'sine=1000:d=1', '-map', '0:v', '-map', '1:a', '-map', '2:a',
               '-c:v', 'mpeg4', '-c:a:0', 'pcm_s16le', '-c:a:1', 'aac', '-ac:a:1', '2', clip)
        info = self.describe(clip)
        self.assertEqual(info['audioStreams'], 2)
        self.assertEqual([(s['index'], s['channels'], s['codec']) for s in info['audioStreamDetails']],
                         [(0, 1, 'pcm_s16le'), (1, 2, 'aac')])
        self.assertEqual(info['audioStreamDetails'][1]['layout'], 'stereo')

    def test_hdr_tags_and_rotation(self):
        flat = self.dir / 'flat.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=192x108:r=24:d=1', '-c:v', 'libx264', '-preset', 'ultrafast',
               '-pix_fmt', 'yuv420p10le', '-x264-params', 'colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc',
               '-color_range', 'tv', flat)
        clip = self.dir / 'hdr.mp4'
        ffmpeg('-display_rotation', '90', '-i', flat, '-c', 'copy', clip)
        info = self.describe(clip)
        self.assertEqual((info['colorPrimaries'], info['colorTransfer'], info['colorSpace'], info['colorRange']),
                         ('bt2020', 'smpte2084', 'bt2020nc', 'tv'))
        self.assertEqual(info['pixelFormat'], 'yuv420p10le')
        self.assertTrue(info['hdr'])
        # -display_rotation is counterclockwise; describe() reports clockwise.
        self.assertEqual(info['rotation'], 270)
        self.assertEqual(self.describe(flat)['rotation'], 0)


class Platform(unittest.TestCase):
    def test_data_directory_resolves_a_symlinked_home(self):
        if os.name != 'posix' or sys.platform == 'darwin':
            self.skipTest('Linux data directory layout')
        from splice_platform import data_directory
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / 'real-home'
            real.mkdir()
            (Path(temp) / 'home').symlink_to(real)
            saved = {k: os.environ.pop(k, None) for k in ('SPLICE_DATA_DIR', 'XDG_DATA_HOME', 'HOME')}
            os.environ['HOME'] = str(Path(temp) / 'home')
            try:
                self.assertEqual(data_directory(), real.resolve() / '.local' / 'share' / 'splice')
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_bounded_runner_kills_a_child_that_writes_too_much(self):
        from media_exec import OutputLimitError, run_bounded
        started = time.monotonic()
        with self.assertRaises(OutputLimitError):
            run_bounded([sys.executable, '-c', 'import sys\nwhile True: sys.stdout.buffer.write(b"x" * 65536)'],
                        timeout=20, max_output=1024 * 1024)
        self.assertLess(time.monotonic() - started, 10)
        result = run_bounded([sys.executable, '-c', 'print("ok")'], timeout=20, max_output=1024)
        self.assertEqual(result.stdout.strip(), b'ok')


class CachePrune(unittest.TestCase):
    def test_prune_keeps_referenced_and_recent_entries(self):
        import media_links
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old, kept, fresh = (root / (c * 32) for c in 'abc')
            for directory in (old, kept, fresh):
                directory.mkdir()
                (directory / 'edit.mp4').write_bytes(b'x' * 1000)
            stale = time.time() - 3600
            for directory in (old, kept):
                os.utime(directory, (stale, stale))
            saved = media_links.MEDIA_ROOT
            media_links.MEDIA_ROOT = root
            try:
                result = media_links.prune(['/media/' + 'b' * 32 + '/edit.mp4'])
            finally:
                media_links.MEDIA_ROOT = saved
            self.assertEqual(result['deleted'], ['a' * 32])
            self.assertEqual(result['freedBytes'], 1000)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['b' * 32, 'c' * 32])


class IsolatedRegistry(unittest.TestCase):
    """media_links pointed at a temporary data folder."""

    def setUp(self):
        import media_links
        self.ml = media_links
        temp = tempfile.TemporaryDirectory(prefix='splice-registry-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / 'data'
        (self.data / 'media').mkdir(parents=True)
        saved = dict(MEDIA_ROOT=media_links.MEDIA_ROOT, REGISTRY=media_links.REGISTRY)
        media_links.MEDIA_ROOT = self.data / 'media'
        media_links.REGISTRY = self.data / 'linked-media.json'
        media_links._cache.update(stamp=None, links={}, recovered=None, good_at=None)
        media_links._deferred.update(stamp=None, until=0.0)
        media_links._forgotten.clear()

        def restore():
            for key, value in saved.items():
                setattr(media_links, key, value)
            media_links._cache.update(stamp=None, links={}, recovered=None, good_at=None)
            media_links._forgotten.clear()
        self.addCleanup(restore)

    def files(self, count, folder='card'):
        directory = self.root / folder
        directory.mkdir(exist_ok=True)
        paths = []
        for index in range(count):
            path = directory / f'clip{index:03}.wav'
            path.write_bytes(b'RIFF' + index.to_bytes(4, 'little') * 8)
            paths.append(path)
        return paths


class LinkRegistry(IsolatedRegistry):
    def test_two_processes_linking_at_once_keep_every_entry(self):
        paths = self.files(120)
        script = ('import sys; sys.path.insert(0, sys.argv[1]); import media_links\n'
                  'for path in sys.argv[2:]: media_links.link(path)\n')
        env = {**os.environ, 'SPLICE_DATA_DIR': str(self.data)}
        workers = [subprocess.Popen([sys.executable, '-c', script, str(ROOT), *map(str, chunk)], env=env)
                   for chunk in (paths[:60], paths[60:])]
        for worker in workers:
            self.assertEqual(worker.wait(120), 0)
        self.ml._cache.update(stamp=None)
        self.assertEqual(sorted(e['path'] for e in self.ml._links().values()), sorted(map(str, paths)))
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.startswith('.linked-media-')], [])

    def test_an_outside_rewrite_with_the_same_size_and_mtime_is_noticed(self):
        first, second = self.files(2)
        entry = self.ml.link(str(first))
        identifier = entry['src'].split('/')[3]
        stat = self.ml.REGISTRY.stat()
        text = self.ml.REGISTRY.read_text()
        swapped = text.replace(str(first), str(second)).replace('clip000', 'clip001')
        self.assertEqual(len(swapped), len(text))
        with open(self.ml.REGISTRY, 'r+', encoding='utf-8') as sink:  # In place: same inode.
            sink.write(swapped)
        # Restoring the mtime changes the ctime, but only by the filesystem's timestamp
        # granularity (a ZFS tick is milliseconds; tmpfs is nanoseconds): repeat until it shows.
        deadline = time.monotonic() + 5
        while True:
            os.utime(self.ml.REGISTRY, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if self.ml.REGISTRY.stat().st_ctime_ns != stat.st_ctime_ns or time.monotonic() > deadline:
                break
            time.sleep(.01)
        after = self.ml.REGISTRY.stat()
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns), (stat.st_ino, stat.st_size, stat.st_mtime_ns))
        self.assertEqual(self.ml._links()[identifier]['path'], str(second))

    def test_link_ids_are_random_and_stable_for_a_path(self):
        import hashlib
        path, = self.files(1)
        entry = self.ml.link(str(path))
        identifier = entry['src'].split('/')[3]
        self.assertRegex(identifier, r'^[a-f0-9]{32}$')
        self.assertNotEqual(identifier, hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32])
        self.ml._cache.update(stamp=None, links={})  # As after a restart.
        self.assertEqual(self.ml.link(str(path))['src'], entry['src'])
        other = self.root / 'other'
        other.mkdir()
        (other / path.name).write_bytes(path.read_bytes())
        self.assertNotEqual(self.ml.link(str(other / path.name))['src'].split('/')[3], identifier)

    def test_a_damaged_registry_is_set_aside_and_replaced(self):
        first, second = self.files(2)
        self.ml.link(str(first))
        self.ml.REGISTRY.write_text('{"version":1,"links":{"x"')
        entry = self.ml.link(str(second))
        aside = [p for p in self.data.iterdir() if p.name.startswith('linked-media.damaged-')]
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_text(), '{"version":1,"links":{"x"')
        self.assertEqual([e['path'] for e in self.ml._links().values()], [entry['sourcePath']])
        notice = self.ml.cache_report()['notice']
        self.assertIn(str(aside[0]), notice)
        self.assertIn('relink', notice)
        aside[0].unlink()
        self.assertIsNone(self.ml.recovery_notice())

    def test_full_registry_refuses_new_links_until_links_are_forgotten(self):
        paths = self.files(3)
        saved = self.ml.MAX_LINKS
        self.ml.MAX_LINKS = 2
        try:
            kept = self.ml.link(str(paths[0]))
            self.ml.link(str(paths[1]))
            with self.assertRaisesRegex(ValueError, 'full'):
                self.ml.link(str(paths[2]))
            self.ml.link(str(paths[0]))  # Relinking a known path still works.
            self.assertEqual(self.ml.forget([kept['src'].split('/')[3], 'not-an-id']), [kept['src'].split('/')[3]])
            self.ml.link(str(paths[2]))
        finally:
            self.ml.MAX_LINKS = saved
        self.assertEqual(paths[0].read_bytes()[:4], b'RIFF', 'forgetting never touches the file')

    def test_forget_unreferenced_keeps_used_and_recent_links(self):
        used, unused, fresh = (self.ml.link(str(p)) for p in self.files(3))
        stale = time.time() - 3600
        def age(links):
            for key, entry in links.items():
                if entry['path'] != fresh['sourcePath']:
                    entry['linkedAt'] = stale
        self.ml._update(age)
        self.assertEqual(self.ml.forget_unreferenced([used['src'], '/media/' + 'b' * 32 + '/x.mp4']),
                         [unused['src'].split('/')[3]])
        self.assertEqual(sorted(e['path'] for e in self.ml._links().values()),
                         sorted([used['sourcePath'], fresh['sourcePath']]))

    def test_a_folder_batch_writes_the_registry_once(self):
        paths = self.files(25)
        writes = []
        original = self.ml._write
        self.ml._write = lambda *args: (writes.append(1), original(*args))[1]
        try:
            results = self.ml.link_many([str(p) for p in paths] + ['relative.wav', str(self.root / 'missing.wav')])
        finally:
            self.ml._write = original
        self.assertEqual(len(writes), 1)
        self.assertEqual(sum('src' in r for r in results), 25)
        self.assertIn('absolute path', results[-2]['error'])
        self.assertIn('not a readable file', results[-1]['error'])

    def test_relative_paths_are_refused(self):
        path, = self.files(1)
        for value in ('card/clip000.wav', 'clip000.wav', './' + path.name, ''):
            with self.assertRaises(ValueError, msg=value):
                self.ml.link(value)

    def test_an_mtime_only_change_takes_the_link_offline(self):
        path, = self.files(1)
        entry = self.ml.link(str(path))
        self.assertEqual(self.ml.resolve_media(entry['src']), path.resolve())
        os.utime(path, ns=(time.time_ns(), path.stat().st_mtime_ns + 1_000_000_000))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.ml.resolve_media(entry['src'])

    def test_a_file_swapped_in_after_resolving_is_not_served(self):
        path, = self.files(1)
        impostor = self.root / 'impostor.wav'
        impostor.write_bytes(b'Z' * path.stat().st_size)
        # Another file: same size, but not the recorded modification time. Written a moment after
        # the original, it could share its mtime on a filesystem with coarse timestamps (ZFS).
        os.utime(impostor, ns=(time.time_ns(), path.stat().st_mtime_ns + 5_000_000_000))
        entry = self.ml.link(str(path))
        with self.ml.open_media(entry['src']) as source:
            self.assertEqual(source.read(4), b'RIFF')
        original = self.ml.resolve_media
        self.ml.resolve_media = lambda src: impostor  # The path now names another file.
        try:
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.ml.open_media(entry['src'])
        finally:
            self.ml.resolve_media = original

    def test_a_writer_killed_mid_save_leaves_no_registry_copy_behind(self):
        import signal
        first, second = self.files(2)
        script = ('import sys, time; sys.path.insert(0, sys.argv[1]); import media_links\n'
                  'media_links.os.replace = lambda *a: time.sleep(60)\n'
                  'media_links.link(sys.argv[2])\n')
        env = {**os.environ, 'SPLICE_DATA_DIR': str(self.data)}
        writer = subprocess.Popen([sys.executable, '-c', script, str(ROOT), str(first)], env=env)
        temporaries = lambda: sorted(p.name for p in self.data.iterdir() if p.name.startswith('.linked-media-'))
        deadline = time.monotonic() + 30
        while not temporaries() and time.monotonic() < deadline:
            time.sleep(.05)
        writer.send_signal(signal.SIGKILL if hasattr(signal, 'SIGKILL') else signal.SIGTERM)
        writer.wait(10)
        left = temporaries()
        self.assertEqual(len(left), 1, 'the killed writer left its copy')
        outside = self.root / 'outside.json'
        outside.write_text('keep')
        # Named exactly like a writer's copy, but a symlink and a folder: never removed.
        os.symlink(outside, self.data / '.linked-media-symlink.json')
        os.symlink(outside, self.data / '.linked-media-symlink1.json')
        (self.data / '.linked-media-folder01.json').mkdir()
        (self.data / '.linked-media-notes.txt').write_text('not ours')
        self.ml.link(str(second))
        self.assertEqual(temporaries(), ['.linked-media-folder01.json', '.linked-media-notes.txt',
                                         '.linked-media-symlink.json', '.linked-media-symlink1.json'])
        self.assertEqual(outside.read_text(), 'keep')

    def hold_the_registry_lock(self):
        """Another process that took the registry lock and stalled."""
        script = ('import fcntl, sys, time\n'
                  'handle = open(sys.argv[1], "a+b"); fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n'
                  'print("locked", flush=True); time.sleep(60)\n')
        holder = subprocess.Popen([sys.executable, '-c', script, str(self.data / 'linked-media.lock')],
                                  stdout=subprocess.PIPE)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), b'locked')
        return holder

    @unittest.skipUnless(os.name == 'posix', 'the stalled holder uses flock')
    def test_serving_registered_media_does_not_wait_on_a_stalled_writer(self):
        import threading
        first, second = self.files(2)
        entry = self.ml.link(str(first))
        self.ml._cache.update(stamp=None, links={})  # As in a process that has not read it yet.
        holder = self.hold_the_registry_lock()
        writer = threading.Thread(target=lambda: results.append(self.ml.link(str(second))), daemon=True)
        results = []
        writer.start()  # Queued behind the stalled holder.
        time.sleep(.2)
        started = time.monotonic()
        self.assertEqual(self.ml.resolve_media(entry['src']), first.resolve())
        with self.ml.open_media(entry['src']) as source:
            self.assertEqual(source.read(4), b'RIFF')
        self.assertEqual(self.ml.cache_report()['linkedFiles'], 1)
        self.ml.browse()
        self.assertLess(time.monotonic() - started, 1.5, 'readers wait on neither the file lock nor the queued writer')
        # A damaged registry needs the lock to be set aside; until then the last good list is served.
        good = self.ml.REGISTRY.read_text()
        temporary = self.ml.REGISTRY.with_name('damaged.tmp')
        temporary.write_text('{"links": ')
        os.replace(temporary, self.ml.REGISTRY)
        started = time.monotonic()
        self.assertEqual(self.ml.resolve_media(entry['src']), first.resolve())
        self.assertLess(time.monotonic() - started, self.ml.READ_LOCK_SECONDS + 1)
        started = time.monotonic()
        for _ in range(20):
            self.ml.resolve_media(entry['src'])
        self.assertLess(time.monotonic() - started, .5, 'the repair is deferred, not retried on every read')
        temporary.write_text(good)
        os.replace(temporary, self.ml.REGISTRY)
        holder.kill()
        holder.wait()
        writer.join(30)
        self.assertEqual(len(results), 1)
        self.assertEqual(sorted(e['path'] for e in self.ml._links().values()), sorted([str(first), str(second)]))

    def damage_the_registry(self):
        temporary = self.ml.REGISTRY.with_name('damaged.tmp')
        temporary.write_text('{"links": ')
        os.replace(temporary, self.ml.REGISTRY)

    def busy_lock(self):
        """The registry lock as another process stalled on it would leave it: never free."""
        def busy(timeout=30):
            raise TimeoutError('Another Splice window is still updating the linked media list')
        return mock.patch.object(self.ml, '_registry_lock', busy)

    def test_a_link_forgotten_here_stops_resolving_here_whatever_the_registry_state(self):
        first, second = self.files(2)
        kept, dropped = self.ml.link(str(first)), self.ml.link(str(second))
        good = self.ml.REGISTRY.read_text()
        self.damage_the_registry()
        with self.busy_lock():
            self.assertEqual(self.ml.resolve_media(dropped['src']), second.resolve(), 'the last good list is served')
            identifier = dropped['src'].split('/')[3]
            with self.assertRaises(TimeoutError):
                self.ml.forget([identifier])  # the registry cannot be written now
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.ml.resolve_media(dropped['src'])
            self.assertEqual(self.ml.resolve_media(kept['src']), first.resolve())
        # The registry readable again, and still listing it (the write failed): this process
        # was asked to forget it, so it does not come back here; a later forget succeeds.
        temporary = self.ml.REGISTRY.with_name('good.tmp')
        temporary.write_text(good)
        os.replace(temporary, self.ml.REGISTRY)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.ml.resolve_media(dropped['src'])
        self.assertEqual(self.ml.forget([identifier]), [identifier])
        self.assertEqual(self.ml._forgotten, set(), 'trimmed once the registry no longer lists it')
        self.assertEqual(sorted(self.ml._links()), [kept['src'].split('/')[3]])

    def test_the_last_good_list_stands_in_for_a_damaged_registry_only_briefly(self):
        first, = self.files(1)
        entry = self.ml.link(str(first))
        self.ml._links()
        self.damage_the_registry()
        with self.busy_lock(), mock.patch.object(self.ml, 'LAST_GOOD_SECONDS', 0.5):
            self.assertEqual(self.ml.resolve_media(entry['src']), first.resolve())
            self.assertEqual(self.ml.resolve_media(entry['src']), first.resolve(), 'within the deferred window too')
            time.sleep(0.7)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.ml.resolve_media(entry['src'])  # fail closed: a link forgotten elsewhere may be in it
            self.ml._deferred.update(until=0.0)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.ml.resolve_media(entry['src'])
        self.ml._deferred.update(until=0.0)  # the next repair attempt is due
        self.assertEqual(self.ml._links(), {}, 'repaired once the lock is free: set aside, a new empty list')
        self.assertIsNotNone(self.ml.recovery_notice())

    def test_a_replace_refused_while_another_process_reads_is_retried(self):
        first, second = self.files(2)
        real, calls = os.replace, []

        def refused_twice(source, target):
            calls.append(target)
            if len(calls) <= 2:
                raise PermissionError(13, 'Access is denied')
            real(source, target)
        with mock.patch.object(self.ml.os, 'replace', refused_twice):
            entry = self.ml.link(str(first))
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.ml.resolve_media(entry['src']), first.resolve())
        before = self.ml.REGISTRY.read_bytes()
        calls.clear()

        def always_refused(source, target):
            calls.append(target)
            raise PermissionError(13, 'Access is denied')
        with mock.patch.object(self.ml.os, 'replace', always_refused), \
                self.assertRaisesRegex(self.ml.RegistryBusy, 'kept the linked media list open.*Access is denied'):
            self.ml.link(str(second))
        self.assertEqual(len(calls), self.ml.REPLACE_ATTEMPTS, 'bounded')
        self.assertIsInstance(self.ml.RegistryBusy('x'), OSError)
        self.assertEqual(self.ml.REGISTRY.read_bytes(), before, 'the registry is unchanged')
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.startswith('.linked-media-')], [],
                         'no copy left behind')
        self.ml._cache.update(stamp=None)
        self.assertEqual(sorted(e['path'] for e in self.ml._links().values()), [str(first.resolve())])

    def test_the_lock_free_reader_opens_the_registry_like_open(self):
        first, = self.files(1)
        self.ml.link(str(first))
        with self.ml._open_for_reading(self.ml.REGISTRY) as source:
            self.assertEqual(source.read(), self.ml.REGISTRY.read_bytes())
        with self.assertRaises(FileNotFoundError):
            self.ml._open_for_reading(self.data / 'missing.json')

    def test_premiere_resolves_linked_media_without_a_patch(self):
        path, = self.files(1)
        entry = self.ml.link(str(path))
        env = {**os.environ, 'SPLICE_DATA_DIR': str(self.data)}
        # premiere alone: nothing imported media_links to patch it.
        script = ('import sys; sys.path.insert(0, sys.argv[1]); import premiere\n'
                  'assert "media_links" not in sys.modules\n'
                  'print(premiere._media_path(sys.argv[2]))\n')
        result = subprocess.run([sys.executable, '-c', script, str(ROOT), entry['src']], env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(path.resolve())), result.stderr)
        script = ('import sys; sys.path.insert(0, sys.argv[1]); import render_movie, media_links, premiere\n'
                  'assert render_movie._media_path is media_links.resolve_media, "install() rebinds render_movie"\n'
                  'assert premiere._media_path is not media_links.resolve_media, "premiere is not patched"\n'
                  'print(render_movie._render_source({"src": sys.argv[2]}))\n')
        result = subprocess.run([sys.executable, '-c', script, str(ROOT), entry['src']], env=env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(path.resolve())), result.stderr)
        import premiere
        self.assertEqual(premiere.managed_media_path('/assets/score.wav'), ROOT / 'assets' / 'score.wav')
        with self.assertRaises(ValueError):
            premiere.managed_media_path(entry['src'])

    def test_folder_walk_and_listing_are_bounded(self):
        deep = self.root / 'card'
        for level in range(self.ml.MAX_FOLDER_DEPTH + 3):
            deep = deep / f'd{level}'
        deep.mkdir(parents=True)
        (deep / 'too-deep.wav').write_bytes(b'x')
        (self.root / 'card' / 'top.wav').write_bytes(b'x')
        hidden = self.root / 'card' / '.cache'
        hidden.mkdir()
        (hidden / 'hidden.wav').write_bytes(b'x')
        (self.root / 'card' / '.dot.wav').write_bytes(b'x')
        found = [relative for _, relative in self.ml.folder_files(str(self.root / 'card'))]
        self.assertEqual(found, ['card/top.wav'])
        many = self.root / 'many'
        many.mkdir()
        for index in range(6):
            (many / f'{index}.wav').write_bytes(b'x')
        saved = self.ml.MAX_FOLDER_FILES, self.ml.MAX_LISTING
        self.ml.MAX_FOLDER_FILES, self.ml.MAX_LISTING = 5, 4
        try:
            with self.assertRaisesRegex(ValueError, 'more than 5'):
                self.ml.folder_files(str(many))
            listing = self.ml.browse(str(many))
        finally:
            self.ml.MAX_FOLDER_FILES, self.ml.MAX_LISTING = saved
        self.assertEqual((len(listing['entries']), listing['truncated']), (4, True))
        for value in ('many', 'relative/path', '\0'):
            with self.assertRaises(ValueError):
                self.ml.browse(value)
            with self.assertRaises(ValueError):
                self.ml.folder_files(value)

    def test_a_hung_volume_scan_delays_only_the_first_browse(self):
        import threading
        release = threading.Event()
        saved = self.ml._scan_places, dict(self.ml._places_state)
        self.ml._places_state.update(at=0, value=None, worker=None)
        self.ml._scan_places = lambda: release.wait(30) and [dict(name='Late', path='/')]
        try:
            for limit in (self.ml.PLACES_TIMEOUT + 1.5, .5):
                started = time.monotonic()
                places = self.ml.browse()['places']
                self.assertLess(time.monotonic() - started, limit)
                self.assertEqual([p['name'] for p in places], ['Home'])
        finally:
            release.set()
            self.ml._places_state['worker'].join(5)
            self.ml._scan_places = saved[0]
            self.ml._places_state.update(saved[1])


class MediaCache(IsolatedRegistry):
    def entry(self, name, kind=None, content=b'x' * 1000, identifier=None, age=3600):
        import hashlib
        directory = self.ml.MEDIA_ROOT / (identifier or hashlib.sha256(content).hexdigest()[:32])
        directory.mkdir()
        (directory / name).write_bytes(content)
        if kind:
            self.ml.mark(directory, kind)
        stale = time.time() - age
        os.utime(directory / name, (stale, stale))
        os.utime(directory, (stale, stale))
        return directory.name

    def test_bulk_prune_deletes_only_unused_old_derived_files(self):
        imported = self.entry('interview.mp4', 'import', b'interview')
        # From before markers existed: named like a proxy, stored under its content hash.
        legacy_import = self.entry('edit.mp4', None, b'a phone clip the user named edit.mp4')
        legacy_proxy = self.entry('edit.mp4', None, b'proxy', identifier='c' * 32)
        proxy = self.entry('seek.mp4', 'derived', b'seek', identifier='d' * 32)
        used_proxy = self.entry('edit.mp4', 'derived', b'used', identifier='e' * 32)
        fresh_proxy = self.entry('edit.mp4', 'derived', b'fresh', identifier='f' * 32, age=0)
        kinds = {e['id']: e['kind'] for e in self.ml.cache_report()['entries']}
        self.assertEqual(kinds, {imported: 'import', legacy_import: 'import', legacy_proxy: 'proxy', proxy: 'proxy',
                                 used_proxy: 'proxy', fresh_proxy: 'proxy'})
        result = self.ml.prune([f'/media/{used_proxy}/edit.mp4'])
        self.assertEqual(sorted(result['deleted']), sorted([legacy_proxy, proxy]))
        left = sorted(p.name for p in self.ml.MEDIA_ROOT.iterdir())
        self.assertEqual(left, sorted([imported, legacy_import, used_proxy, fresh_proxy]))

    def test_an_unmarked_folder_of_identical_generated_names_is_an_import(self):
        import hashlib
        content = b'the same take imported twice'
        directory = self.ml.MEDIA_ROOT / hashlib.sha256(content).hexdigest()[:32]
        directory.mkdir()
        for name in ('a-audio.wav', 'b-audio.wav'):
            (directory / name).write_bytes(content)
        stale = time.time() - 3600
        os.utime(directory, (stale, stale))
        legacy_proxies = self.ml.MEDIA_ROOT / ('c' * 32)
        legacy_proxies.mkdir()
        for name, data in (('edit.mp4', b'edit'), ('seek.mp4', b'seek')):
            (legacy_proxies / name).write_bytes(data)
        os.utime(legacy_proxies, (stale, stale))
        # The original stored second, after a generated-looking file with other bytes: still an import.
        second = self.ml.MEDIA_ROOT / hashlib.sha256(b'an original').hexdigest()[:32]
        second.mkdir()
        (second / 'a-audio.wav').write_bytes(b'something else')
        (second / 'b-audio.wav').write_bytes(b'an original')
        os.utime(second, (stale, stale))
        kinds = {e['id']: e['kind'] for e in self.ml.cache_report()['entries']}
        self.assertEqual(kinds, {directory.name: 'import', second.name: 'import', legacy_proxies.name: 'proxy'})
        self.assertEqual(self.ml.prune([])['deleted'], [legacy_proxies.name])
        self.assertTrue(directory.exists() and second.exists())

    def test_legacy_classification_hashes_a_bounded_amount_per_report(self):
        identifiers = [self.entry('edit.mp4', None, bytes([i]) * 1000, identifier=f'{i:x}' * 32) for i in range(1, 6)]
        hashed = []
        original = self.ml._content_id
        self.ml._content_id = lambda path: (hashed.append(path), original(path))[1]
        saved = self.ml.HASH_BYTES_PER_REPORT
        self.ml.HASH_BYTES_PER_REPORT = 2500
        self.ml._kinds.clear()
        try:
            report = self.ml.cache_report()
            self.assertEqual(len(hashed), 2)
            self.assertEqual(report['unclassified'], 3)
            provisional = [e['id'] for e in report['entries'] if e['provisional']]
            self.assertEqual(len(provisional), 3)
            self.assertTrue(all(e['kind'] == 'import' for e in report['entries'] if e['provisional']))
            # The prune's own report classifies the next two; the last stays unclassified and is kept.
            deleted = self.ml.prune([])['deleted']
            self.assertEqual(len(hashed), 4)
            self.assertEqual(len(deleted), 4)
            left, = [p.name for p in self.ml.MEDIA_ROOT.iterdir()]
            self.assertIn(left, provisional)
            report = self.ml.cache_report()
            self.assertEqual((len(hashed), report['unclassified'], [e['kind'] for e in report['entries']]),
                             (5, 0, ['proxy']))
            big = self.entry('edit.mp4', None, b'x' * 3000, identifier='9' * 32)
            self.assertEqual({e['id']: e['kind'] for e in self.ml.cache_report()['entries']}[big], 'import',
                             'a file too large to hash is kept as an import')
        finally:
            self.ml._content_id = original
            self.ml.HASH_BYTES_PER_REPORT = saved
            self.ml._kinds.clear()
        self.assertTrue(set(identifiers))

    def test_imports_are_deleted_only_when_selected(self):
        imported = self.entry('interview.mp4', 'import', b'interview')
        other = self.entry('broll.mp4', 'import', b'broll')
        report = {e['id']: e for e in self.ml.cache_report()['entries']}
        self.assertEqual(report[imported]['files'], ['interview.mp4'])
        self.assertEqual(report[imported]['src'], f'/media/{imported}/interview.mp4')
        self.assertGreater(report[imported]['lastUsed'], 0)
        result = self.ml.delete_entries([imported])
        self.assertEqual((result['deleted'], result['freedBytes']), ([imported], len(b'interview')))
        self.assertEqual([p.name for p in self.ml.MEDIA_ROOT.iterdir()], [other])
        for bad in ([], ['../x'], 'all', [None]):
            with self.assertRaises(ValueError):
                self.ml.delete_entries(bad)


class CacheRoutes(ServerTestCase):
    def test_uploads_are_marked_as_imports_and_survive_the_bulk_prune(self):
        status, data = self.server.json('POST', '/api/media/upload?name=edit.mp4', (ROOT / 'assets/ridge.mp4').read_bytes())
        self.assertEqual(status, 200, data)
        identifier = data['src'].split('/')[2]
        directory = self.data / 'media' / identifier
        self.assertTrue((directory / '.splice-import').exists())
        stale = time.time() - 3600
        os.utime(directory, (stale, stale))
        status, result = self.server.json('POST', '/api/media/cache/prune', {'referenced': []})
        self.assertEqual(status, 200)
        self.assertNotIn(identifier, result['deleted'])
        self.assertEqual(self.server.request('HEAD', data['src'], token=False)[0], 200)
        status, _ = self.server.json('POST', '/api/media/cache/delete', {'ids': [identifier]})
        self.assertEqual(status, 400)
        status, result = self.server.json('POST', '/api/media/cache/delete', {'ids': [identifier], 'confirm': True})
        self.assertEqual((status, result['deleted']), (200, [identifier]))
        self.assertEqual(self.server.request('HEAD', data['src'], token=False)[0], 404)

    def test_links_can_be_forgotten(self):
        clip = self.outside / 'forget-me.wav'
        ffmpeg('-f', 'lavfi', '-i', 'sine=440:sample_rate=48000:d=1', clip)
        entry = self.link(clip)
        identifier = entry['src'].split('/')[3]
        status, report = self.server.json('GET', '/api/media/cache')
        self.assertIn(identifier, [link['id'] for link in report['links']])
        status, result = self.server.json('POST', '/api/media/links/forget', {'ids': [identifier]})
        self.assertEqual((status, result['forgotten']), (200, 1))
        self.assertEqual(self.server.request('HEAD', entry['src'], token=False)[0], 404)
        self.assertTrue(clip.exists())


class RunnerBounds(unittest.TestCase):
    def test_output_cap_is_exact(self):
        from media_exec import OutputLimitError, run_bounded
        write = 'import sys; sys.stdout.buffer.write(b"x" * {})'
        self.assertEqual(len(run_bounded([sys.executable, '-c', write.format(4096)], timeout=20, max_output=4096).stdout), 4096)
        with self.assertRaises(OutputLimitError):
            run_bounded([sys.executable, '-c', write.format(4097)], timeout=20, max_output=4096)

    def test_timeout_kills_the_child(self):
        from media_exec import run_bounded
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=1, max_output=1024)
        self.assertLess(time.monotonic() - started, 6)

    def test_stderr_keeps_only_a_tail(self):
        from media_exec import MAX_STDERR, run_bounded
        result = run_bounded([sys.executable, '-c', 'import sys; sys.stderr.buffer.write(b"e" * 4_000_000 + b"END")'],
                             timeout=20, max_output=1024)
        self.assertLessEqual(len(result.stderr), MAX_STDERR)
        self.assertTrue(result.stderr.endswith(b'END'))


class DecoderBounds(unittest.TestCase):
    """Audio preview and waveform checks that must hold before FFmpeg runs."""

    def ffmpeg_calls(self, module):
        calls = []
        original = module.run_bounded

        def watched(cmd, **options):
            if cmd[0] == 'ffmpeg':
                calls.append((cmd, options))
            return original(cmd, **options)
        module.run_bounded = watched
        self.addCleanup(setattr, module, 'run_bounded', original)
        return calls

    def test_audition_just_past_the_end_is_silence_without_ffmpeg(self):
        import media_proxy
        calls = self.ffmpeg_calls(media_proxy)
        duration = media_proxy.describe('/assets/score.wav')['duration']
        index = math.ceil(duration / media_proxy.AUDITION_SECONDS)
        audio = media_proxy.audition_audio('/assets/score.wav', index, 1)
        self.assertEqual(calls, [])
        self.assertFalse(any(audio[44:]))
        self.assertEqual(len(audio), 44 + media_proxy.AUDITION_SECONDS * 48000 * 4)

    def test_audition_decoder_is_capped_and_padded_to_the_window(self):
        import media_proxy
        calls = self.ffmpeg_calls(media_proxy)
        audio = media_proxy.audition_audio('/assets/score.wav', 2, 1.5)
        (cmd, options), = calls
        self.assertLessEqual(options['max_output'], 1024 * 1024)
        self.assertLessEqual(len(audio), options['max_output'])
        filters = cmd[cmd.index('-af') + 1].split(',')
        self.assertTrue(all(f.startswith('apad=whole_dur=') for f in filters if f.startswith('apad')), filters)
        self.assertEqual(filters[-1], f'apad=whole_dur={media_proxy.AUDITION_SECONDS + .02:.9f}')

    def test_out_of_range_streams_are_refused_before_decoding(self):
        import media_peaks
        import media_proxy
        calls = self.ffmpeg_calls(media_proxy)
        for stream in (1, 5, -1, '0', 1.0):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                media_proxy.audition_audio('/assets/score.wav', 0, 1, stream=stream)
        self.assertEqual(calls, [])
        probes = []
        original = media_peaks.run_bounded
        media_peaks.run_bounded = lambda *a, **k: probes.append(a) or original(*a, **k)
        self.addCleanup(setattr, media_peaks, 'run_bounded', original)
        for stream in (16, 99, -1, '1', None):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                media_peaks.waveform('/assets/score.wav', stream)
        self.assertEqual(probes, [])


class ProbeDetails(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='splice-probe-')
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def test_rates_snap_to_the_standard_a_camera_wrote(self):
        from fractions import Fraction
        from media_proxy import _snap
        for probed, expected in (('2997/100', '30000/1001'), ('23976/1000', '24000/1001'), ('5994/100', '60000/1001'),
                                 ('25', '25'), ('2500/100', '25'), ('25/2', '25/2'), ('599/20', '599/20')):
            self.assertEqual(_snap(Fraction(probed)), Fraction(expected), probed)

    def test_variable_rate_in_matroska_is_found_by_the_packet_scan(self):
        # Matroska reports equal nominal and average rates for this clip.
        for name in ('vfr.mkv', 'vfr.webm'):
            clip = self.dir / name
            codec = ['-c:v', 'libx264', '-preset', 'ultrafast'] if name.endswith('mkv') else \
                    ['-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '8']
            ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=96x54:r=30:d=3', '-vf', "setpts='if(lt(N,30),N/15,2+(N-30)/30)/TB'",
                   '-fps_mode', 'passthrough', *codec, clip)
            from media_proxy import describe_path
            info = describe_path(clip)
            self.assertTrue(info['vfr'], name)
            self.assertTrue(info['proxyRecommended'], name)
        steady = self.dir / 'steady.mkv'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=96x54:r=30:d=2', '-c:v', 'libx264', '-preset', 'ultrafast', steady)
        from media_proxy import describe_path
        self.assertEqual((describe_path(steady)['vfr'], describe_path(steady)['rate']), (False, '30/1'))

    def test_too_few_packets_fall_back_to_the_rate_mismatch(self):
        from fractions import Fraction
        import media_proxy
        original = media_proxy._ffprobe
        packets = [{'pts_time': str(t)} for t in (0, .05, .1, .2)]
        media_proxy._ffprobe = lambda *a, **k: {'packets': packets}
        try:
            self.assertTrue(media_proxy._variable_rate('x', 0, Fraction(30), Fraction(20)))
            self.assertFalse(media_proxy._variable_rate('x', 0, Fraction(30), Fraction(30)))
            steady = [{'pts_time': str(i / 30)} for i in range(40)]
            media_proxy._ffprobe = lambda *a, **k: {'packets': steady}
            self.assertFalse(media_proxy._variable_rate('x', 0, Fraction(30), Fraction(20)))
        finally:
            media_proxy._ffprobe = original
        short = self.dir / 'short.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=96x54:r=30:d=0.17', '-c:v', 'libx264', '-preset', 'ultrafast', short)
        self.assertFalse(media_proxy.describe_path(short)['vfr'])

    def test_text_files_are_not_media(self):
        from media_proxy import decodable
        notes = self.dir / 'notes.txt'
        notes.write_text('Slate notes for the whole day of shooting.\n' * 40)
        for name in ('notes.txt', 'notes.mov'):
            with self.assertRaisesRegex(ValueError, 'cannot decode'):
                decodable(notes, name)

    def test_playlists_renamed_as_movies_are_not_media(self):
        from media_proxy import decodable
        clip = self.dir / 'take.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=32x18:r=25:d=0.2', '-c:v', 'mpeg4', clip)
        decodable(clip, clip.name)
        playlist = self.dir / 'playlist.mov'
        playlist.write_text(f"ffconcat version 1.0\nfile '{clip.name}'\n")
        with self.assertRaisesRegex(ValueError, 'cannot decode'):
            decodable(playlist, playlist.name)

    def test_hls_playlists_are_not_media(self):
        from media_proxy import TEXT_FORMATS, decodable
        segment = self.dir / 'segment0.ts'
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=32x18:r=25:d=0.4', '-c:v', 'mpeg4', '-f', 'mpegts', segment)
        decodable(segment, segment.name)
        # A playlist picked under its own name (Link media in place takes any file the user picks).
        # FFmpeg 8 no longer detects one renamed .mov, older versions do; both must be refused.
        playlist = self.dir / 'playlist.m3u8'
        playlist.write_text('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXTINF:0.4,\n'
                            f'{segment.name}\n#EXT-X-ENDLIST\n')
        renamed = self.dir / 'playlist.mov'
        renamed.write_bytes(playlist.read_bytes())
        with self.assertRaisesRegex(ValueError, 'cannot decode'):
            decodable(renamed, renamed.name)
        # Without the refusal FFmpeg reads it as a video through the playlist.
        probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries',
                                'stream=codec_type:format=format_name', '-of', 'json', str(playlist)],
                               capture_output=True, text=True, timeout=60)
        data = json.loads(probe.stdout or '{}')
        self.assertIn('hls', data.get('format', {}).get('format_name', '').split(','))
        self.assertIn('video', [s.get('codec_type') for s in data.get('streams', [])])
        with self.assertRaisesRegex(ValueError, 'cannot decode'):
            decodable(playlist, playlist.name)
        self.assertIn('hls', TEXT_FORMATS)

    def test_the_refusal_list_passes_ordinary_media(self):
        from media_proxy import _ffprobe, _text_art, decodable
        made = {'clip.mp4': ('-c:v', 'mpeg4'), 'clip.mov': ('-c:v', 'mpeg4'), 'clip.mkv': ('-c:v', 'mpeg4'),
                'clip.avi': ('-c:v', 'mpeg4'), 'clip.ts': ('-c:v', 'mpeg4'), 'clip.mxf': ('-c:v', 'mpeg2video'),
                'clip.m4v': ('-c:v', 'mpeg4', '-f', 'mp4'), 'still.png': ('-frames:v', '1')}
        for name, arguments in made.items():
            with self.subTest(name=name):
                target = self.dir / name
                ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=32x18:r=25:d=0.2', *arguments, target)
                decodable(target, name)
                data = _ffprobe(target, '-show_entries', 'stream=codec_type,codec_name:format=format_name')
                self.assertFalse(_text_art(data['format'], data['streams']))
        for name, arguments in {'tone.wav': ('-c:a', 'pcm_s16le'), 'tone.flac': ('-c:a', 'flac'),
                                'tone.m4a': ('-c:a', 'aac'), 'tone.ogg': ('-c:a', 'flac', '-f', 'ogg')}.items():
            with self.subTest(name=name):
                target = self.dir / name
                ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=0.2', *arguments, target)
                decodable(target, name)

    def test_a_probe_timeout_is_reported_as_a_timeout(self):
        import media_proxy
        original = media_proxy.run_bounded

        def slow(cmd, **options):
            raise subprocess.TimeoutExpired(cmd, options['timeout'])
        media_proxy.run_bounded = slow
        try:
            with self.assertRaisesRegex(ValueError, 'timed out') as caught:
                media_proxy.decodable(ROOT / 'assets' / 'score.wav', 'score.wav')
            self.assertIsInstance(caught.exception, media_proxy.ProbeTimeout)
        finally:
            media_proxy.run_bounded = original


if __name__ == '__main__':
    unittest.main()
