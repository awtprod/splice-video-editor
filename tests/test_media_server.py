"""Server safety and media handling: request proof, bounded decoders,
link-in-place media, source metadata, and the managed media cache."""
from array import array
import gzip
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
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
        self.process = subprocess.Popen([sys.executable, '-u', 'server.py', '--port', '0'], cwd=ROOT,
                                        env={**os.environ, 'SPLICE_DATA_DIR': str(self.data)},
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        preexec_fn=_limit_memory if os.name == 'posix' else None)
        line = self.process.stdout.readline().decode().strip()
        match = re.fullmatch(r'http://127\.0\.0\.1:(\d+)', line)
        if not match:
            self.stop()
            raise RuntimeError('Server did not start: ' + line)
        self.port = int(match.group(1))
        status, headers, page = self.request('GET', '/', auth=False, headers={
            'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        assert status == 200, status
        self.cookie = headers['set-cookie'].split(';', 1)[0]
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
        cls.server.stop()
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

    def test_desktop_launch_link_works_once(self):
        import threading
        from server import create_server
        server = create_server(0, launch_token='launch-secret')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(path):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                connection.request('GET', path, headers={'Host': f'127.0.0.1:{server.server_port}',
                                                         'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate'})
                response = connection.getresponse()
                response.read()
                connection.close()
                return response.status, response.getheader('Set-Cookie')
            self.assertEqual(get('/'), (403, None))
            self.assertEqual(get('/?launch=wrong'), (403, None))
            status, cookie = get('/?launch=launch-secret')
            self.assertEqual(status, 303)
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
            self.assertEqual(get('/?launch=launch-secret'), (403, None))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(10)


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
        self.assertIn(info['rotation'], (90, 270))
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


if __name__ == '__main__':
    unittest.main()
