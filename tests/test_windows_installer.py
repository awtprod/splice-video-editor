"""The Windows installer's platform pieces, exercised on any OS with mocks: the
FFmpeg feature gate and its lists against the code, per-user settings locations,
the no-console process helper, the taskkill stop paths, the launcher's server mode
and exclusive port, the desktop launcher (tests/windows-launcher.cjs), and the
installer build (staging, download checks, Electron fuses, NSIS, source archive)."""
import ast
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'desktop' / 'windows'))
sys.path.insert(0, str(ROOT / 'tests'))

import delivery  # noqa: E402
import export_jobs  # noqa: E402
import media_exec  # noqa: E402
import raw_decode  # noqa: E402
import render_movie  # noqa: E402
import render_queue  # noqa: E402
from resolve import http as resolve_http  # noqa: E402
import runtime_check  # noqa: E402
import splice_platform  # noqa: E402
from windows_port_probe import second_bind_refused  # noqa: E402

POSIX = os.name == 'posix'


def _free_port():
    while True:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        if port != 47834:
            return port


def _listing(kind, names):
    if kind == 'filters':
        return 'Filters:\n  T.. = Timeline support\n' + ''.join(f' TS {name:<16} V->V       x\n' for name in names)
    if kind == 'muxers':
        return 'Formats:\n D.. = Demuxing\n .E. = Muxing\n ---\n' + ''.join(f'  E  {name:<15} x\n' for name in names)
    if kind == 'demuxers':
        return ('Formats:\n D.. = Demuxing supported\n ..d = Is a device\n ---\n' +
                ''.join(f' D {"d" if name == "lavfi" else " "} {name:<15} x\n' for name in names))
    if kind == 'bsfs':
        return 'Bitstream filters:\n' + ''.join(f'{name}\n' for name in names)
    return f'{kind.title()}:\n V..... = Video\n ------\n' + ''.join(f' V....D {name:<20} x\n' for name in names)


def fake_ffmpeg(directory, *, drop=(), exit_code=0, sleep=0):
    """An ffmpeg/ffprobe pair answering the six listing flags like FFmpeg 8."""
    directory = Path(directory)
    everything = dict(
        filters=runtime_check.REQUIRED_FILTERS + runtime_check.OPTIONAL_FILTERS,
        encoders=runtime_check.REQUIRED_ENCODERS + runtime_check.OPTIONAL_ENCODERS,
        decoders=runtime_check.REQUIRED_DECODERS + runtime_check.OPTIONAL_DECODERS,
        bsfs=runtime_check.REQUIRED_BITSTREAM_FILTERS,
        muxers=runtime_check.REQUIRED_MUXERS,
        demuxers=runtime_check.REQUIRED_DEMUXERS)
    for kind, names in everything.items():
        (directory / f'{kind}.txt').write_text(_listing(kind, [n for n in names if n not in drop]))
    script = directory / 'ffmpeg'
    script.write_text('#!/bin/sh\n' + (f'exec sleep {sleep}\n' if sleep else '') +
                      (f'echo "boom: broken build" >&2\nexit {exit_code}\n' if exit_code else '') +
                      f'cat "{directory}/${{2#-}}.txt"\n')
    script.chmod(0o755)
    probe = directory / 'ffprobe'
    probe.write_text('#!/bin/sh\nexit 0\n')
    probe.chmod(0o755)
    return str(script), str(probe)


@unittest.skipUnless(POSIX, 'fake FFmpeg scripts are POSIX shell')
class FFmpegFeatureGate(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='splice-ffgate-')
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_complete_build_passes(self):
        ffmpeg, ffprobe = fake_ffmpeg(self.directory)
        self.assertEqual(runtime_check.startup_problem(ffmpeg, ffprobe), '')
        self.assertEqual(runtime_check.startup_problem(ffmpeg, ffprobe, strict=True), '')

    def test_missing_required_filter_names_it(self):
        ffmpeg, ffprobe = fake_ffmpeg(self.directory, drop=('zscale', 'drawtext', 'prores_ks'))
        problem = runtime_check.startup_problem(ffmpeg, ffprobe)
        self.assertIn('Splice cannot start', problem)
        self.assertIn('filters: drawtext, zscale', problem)
        self.assertIn('encoders: prores_ks', problem)
        self.assertIn('Reinstall Splice', problem)

    def test_missing_optional_feature_only_fails_strict(self):
        ffmpeg, ffprobe = fake_ffmpeg(self.directory, drop=('vidstabtransform', 'vidstabdetect', 'prores_raw'))
        self.assertEqual(runtime_check.startup_problem(ffmpeg, ffprobe), '')
        problem = runtime_check.startup_problem(ffmpeg, ffprobe, strict=True)
        self.assertIn('vidstabdetect, vidstabtransform', problem)
        self.assertIn('decoders: prores_raw', problem)
        self.assertEqual(runtime_check.report(ffmpeg, ffprobe)['optional'],
                         {'filters': ['vidstabdetect', 'vidstabtransform'], 'decoders': ['prores_raw']})

    def test_missing_formats_and_bitstream_filters_are_named(self):
        ffmpeg, ffprobe = fake_ffmpeg(self.directory, drop=('h264_metadata', 'webm', 'lavfi', 'join'))
        problem = runtime_check.startup_problem(ffmpeg, ffprobe)
        for part in ('filters: join', 'bitstream filters: h264_metadata', 'muxers: webm', 'demuxers: lavfi'):
            self.assertIn(part, problem)

    def test_unusable_ffmpeg_is_a_message_not_an_exception(self):
        missing = str(Path(self.directory) / 'nowhere' / 'ffmpeg')
        self.assertIn('FFmpeg could not start', runtime_check.startup_problem(missing, missing))
        broken, probe = fake_ffmpeg(self.directory, exit_code=3)
        self.assertIn('boom: broken build', runtime_check.startup_problem(broken, probe))
        slow, probe = fake_ffmpeg(self.directory, sleep=5)
        self.assertIn('did not answer', runtime_check.startup_problem(slow, probe, timeout=.3))
        empty = Path(self.directory) / 'empty'
        empty.write_text('#!/bin/sh\nexit 0\n')
        empty.chmod(0o755)
        self.assertIn('printed no filters', runtime_check.startup_problem(str(empty), probe))
        with mock.patch('splice_platform.shutil.which', return_value=None):
            self.assertIn('ffmpeg was not found', runtime_check.startup_problem())

    def test_server_refuses_to_start_with_a_clear_message(self):
        ffmpeg, _ = fake_ffmpeg(self.directory, drop=('zscale',))
        env = dict(os.environ, PATH=self.directory + os.pathsep + '/usr/bin:/bin', SPLICE_DATA_DIR=self.directory)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'server.py'), '--port', str(_free_port()),
                                 '--exit-with-stdin', '--require-ffmpeg-features'],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('is missing filters: zscale', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_a_slow_ffmpeg_is_reported_within_the_total_limit(self):
        # The first listings take a while and the third hangs: the check gives up when the total
        # runs out, not a full per-listing timeout later.
        ffmpeg, ffprobe = fake_ffmpeg(self.directory)
        script = Path(ffmpeg)
        script.write_text(script.read_text().replace(
            '#!/bin/sh\n', '#!/bin/sh\ncase "$2" in -decoders) exec sleep 30;; *) sleep 0.3;; esac\n', 1))
        started = time.monotonic()
        problem = runtime_check.startup_problem(ffmpeg, ffprobe, timeout=6, total=1.5)
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertIn('FFmpeg did not answer "-decoders"', problem)
        self.assertIn('Reinstall Splice', problem)
        # Listings that each answer in time but together take too long stop at the total as well.
        script.write_text(script.read_text().replace('-decoders) exec sleep 30;;', '-nothing) ;;'))
        problem = runtime_check.startup_problem(ffmpeg, ffprobe, timeout=6, total=.5)
        self.assertRegex(problem, r'FFmpeg did not (answer|list its features within 0\.5 seconds)')

    def test_server_shows_the_slow_ffmpeg_message(self):
        ffmpeg, _ = fake_ffmpeg(self.directory, sleep=30)
        env = dict(os.environ, PATH=self.directory + os.pathsep + '/usr/bin:/bin', SPLICE_DATA_DIR=self.directory)
        # The server as the launcher starts it, with the limits shortened for the test.
        code = ('import sys, runtime_check; runtime_check.PROBE_TIMEOUT = 1; runtime_check.TOTAL_TIMEOUT = 2; '
                'sys.argv = ["server.py", "--port", "0", "--exit-with-stdin", "--require-ffmpeg-features"]; '
                'import runpy; runpy.run_path("server.py", run_name="__main__")')
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, env=env, capture_output=True, text=True,
                                timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('FFmpeg did not answer "-filters" within 1 seconds', result.stderr)

    def test_the_check_ends_before_the_launcher_gives_up(self):
        # main.cjs shows "did not start within N seconds" after N seconds; the FFmpeg check (plus
        # starting Python, about 2 s on Windows) must finish well before, so its message is the one shown.
        main = (ROOT / 'desktop' / 'windows' / 'main.cjs').read_text(encoding='utf-8')
        limit = re.search(r"did not start within (\d+) seconds\.'\)\), (\d+)\)", main)
        self.assertIsNotNone(limit)
        self.assertEqual(int(limit.group(1)) * 1000, int(limit.group(2)))
        self.assertLessEqual(runtime_check.TOTAL_TIMEOUT + 10, int(limit.group(1)))
        self.assertLessEqual(runtime_check.PROBE_TIMEOUT, runtime_check.TOTAL_TIMEOUT)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is not installed')
    def test_every_listed_feature_is_a_real_ffmpeg_name(self):
        # Catches a typo in the lists: the system FFmpeg used by the test suite has them all.
        result = runtime_check.report()
        self.assertEqual(result['missing'], {}, result)


def _shipped_python():
    import build
    return sorted(path for path in build.source_files() if path.suffix == '.py' and path.parts[0] != 'vendor')


def _text_pieces(tree):
    """(line, text) for every string in ``tree`` except docstrings. In an f-string a field
    becomes NUL, or the text of its ``or 'fallback'`` when it has one (``{chain or 'anull'}``)."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(value.value)
                elif (isinstance(value.value, ast.BoolOp) and isinstance(value.value.op, ast.Or) and
                      isinstance(value.value.values[-1], ast.Constant) and isinstance(value.value.values[-1].value, str)):
                    parts.append(value.value.values[-1].value)
                else:
                    parts.append('\0')
            yield node.lineno, ''.join(parts)


# A name in filtergraph position: at the start, or right after , ; ] or an f-string field, and
# followed by = , ; [ @ or the end. Prose ("wheels, curves, LUTs") has a space after its commas.
_GRAPH_NAME = re.compile(r'(?:^|(?<=[,;\]\0]))([a-z][a-z0-9_]*)(?=[=,;\[@]|$)')
_CODEC_OPTIONS = {'-c:v', '-c:a', '-c:s', '-vcodec', '-acodec', '-scodec', '-codec:v', '-codec:a', '-codec:s'}
# Output files named by extension pick their muxer that way.
_EXTENSION_MUXERS = {'webm': 'webm', 'm4a': 'ipod', 'mkv': 'matroska'}


def ffmpeg_usage(root, files, universe):
    """{'filters'|'encoders'|'bitstream filters'|'formats': {name: ['file:line', ...]}} for the FFmpeg
    names (from ``universe``, FFmpeg's own lists) that ``files`` use."""
    used = {kind: {} for kind in ('filters', 'encoders', 'bitstream filters', 'formats')}

    def note(kind, name, where):
        used[kind].setdefault(name, []).append(where)

    for relative in files:
        tree = ast.parse((root / relative).read_text(encoding='utf-8'))
        for line, text in _text_pieces(tree):
            where = f'{relative}:{line}'
            if re.search(r'[=,;\[]', text):
                for name in _GRAPH_NAME.findall(text):
                    if name in universe['filters']:
                        note('filters', name, where)
                    elif name in universe['bitstream filters']:
                        note('bitstream filters', name, where)
            extension = re.fullmatch(r'.*[\w}\0-]\.([a-z0-9]+)', text)
            if extension and extension.group(1) in _EXTENSION_MUXERS:
                note('formats', _EXTENSION_MUXERS[extension.group(1)], where)
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                for option, value in zip(node.elts, node.elts[1:]):
                    if not (isinstance(option, ast.Constant) and isinstance(value, ast.Constant) and
                            isinstance(value.value, str)):
                        continue
                    if option.value in _CODEC_OPTIONS and value.value in universe['encoders']:
                        note('encoders', value.value, f'{relative}:{value.lineno}')
                    elif option.value == '-f' and value.value in universe['formats']:
                        note('formats', value.value, f'{relative}:{value.lineno}')
            elif isinstance(node, ast.keyword) and isinstance(node.value, ast.Constant):
                if node.arg in ('encoder', 'subtitles') and node.value.value in universe['encoders']:
                    note('encoders', node.value.value, f'{relative}:{node.value.lineno}')
                elif node.arg == 'muxer' and node.value.value in universe['formats']:
                    note('formats', node.value.value, f'{relative}:{node.value.lineno}')
    return used


@unittest.skipUnless(shutil.which('ffmpeg'), "needs FFmpeg's own lists of names")
class FFmpegFeatureListsMatchTheCode(unittest.TestCase):
    """runtime_check's lists against what the shipped sources actually use, both ways, so a
    filter, encoder, bitstream filter or format added to the code (or dropped from it)
    without updating the startup check fails here."""

    # Found by the scan, but not something the startup check should require.
    NOT_REQUIRED = {
        'filters': {
            'colormatrix': 'an x265-params option in delivery.py, not the colormatrix filter',
            'hwupload': 'only for hardware encoders, which delivery.py test-encodes and hides when they fail',
        },
        'encoders': {},
        'bitstream filters': {},
        'formats': {},
    }
    # Listed for work not on this branch yet; the test fails once the code uses them, so the
    # entry is dropped then.
    ANTICIPATED = {
        'vidstabdetect': 'the effects engine (PR #18) stabilizer; optional, it checks vidstab_available()',
        'vidstabtransform': 'the effects engine (PR #18) stabilizer; optional, it checks vidstab_available()',
        'matroska': "the effects engine's (PR #18) FFV1 .mkv intermediates",
    }

    @classmethod
    def setUpClass(cls):
        ffmpeg = shutil.which('ffmpeg')
        names = {flag: runtime_check._listing(ffmpeg, flag, 30) for _, flag in runtime_check._KINDS}
        universe = {'filters': names['-filters'], 'encoders': names['-encoders'],
                    'bitstream filters': names['-bsfs'], 'formats': names['-muxers'] | names['-demuxers']}
        cls.used = ffmpeg_usage(ROOT, _shipped_python(), universe)
        cls.listed = {
            'filters': set(runtime_check.REQUIRED_FILTERS + runtime_check.OPTIONAL_FILTERS),
            'encoders': set(runtime_check.REQUIRED_ENCODERS + runtime_check.OPTIONAL_ENCODERS),
            'bitstream filters': set(runtime_check.REQUIRED_BITSTREAM_FILTERS),
            'formats': set(runtime_check.REQUIRED_MUXERS + runtime_check.REQUIRED_DEMUXERS),
        }

    def test_every_name_the_code_uses_is_checked(self):
        for kind, used in self.used.items():
            with self.subTest(kind):
                unlisted = {name: places[:3] for name, places in used.items()
                            if name not in self.listed[kind] and name not in self.NOT_REQUIRED[kind]}
                self.assertEqual(unlisted, {}, f'{kind} used by the code but missing from runtime_check.py')

    def test_every_checked_name_is_used(self):
        for kind, listed in self.listed.items():
            with self.subTest(kind):
                unused = sorted(listed - set(self.used[kind]) - set(self.ANTICIPATED))
                self.assertEqual(unused, [], f'{kind} runtime_check.py requires but the code never uses')

    def test_exceptions_are_still_needed(self):
        for kind, names in self.NOT_REQUIRED.items():
            for name in names:
                with self.subTest(name):
                    self.assertIn(name, self.used[kind], 'no longer used: drop it from NOT_REQUIRED')
        for name in self.ANTICIPATED:
            with self.subTest(name):
                self.assertFalse(any(name in used for used in self.used.values()),
                                 f'{name} is used now: drop it from ANTICIPATED')
                self.assertTrue(any(name in listed for listed in self.listed.values()))

    def test_the_scan_sees_known_uses(self):
        # Guards the scanner itself against matching nothing.
        self.assertIn('media_proxy.py:134', ' '.join(self.used['filters']['join']))
        self.assertIn('ebur128', self.used['filters'])
        self.assertIn('anull', self.used['filters'])  # only as {chain or 'anull'}
        self.assertIn('reverse', self.used['filters'])
        self.assertNotIn('curves', self.used['filters'])  # prose: "Wheels, curves, input LUTs"
        self.assertEqual(set(self.used['bitstream filters']), {'h264_metadata', 'prores_metadata'})
        self.assertLessEqual({'concat', 'lavfi', 'webm', 'ipod', 'mov', 'mxf'}, set(self.used['formats']))
        self.assertLessEqual({'libx264', 'libvpx-vp9', 'prores_ks', 'libsvtav1', 'aac'}, set(self.used['encoders']))


class LauncherServerMode(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='splice-launcher-')
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.env = dict(os.environ, SPLICE_DATA_DIR=self.directory)

    def test_prints_the_launch_link_and_stops_when_the_pipe_closes(self):
        port = _free_port()
        process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'server.py'), '--port', str(port), '--exit-with-stdin'],
                                   cwd=ROOT, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            first = process.stdout.readline().strip()
            self.assertRegex(first, rf'^http://127\.0\.0\.1:{port}/\?launch=[A-Za-z0-9_-]{{20,}}$')
            process.stdin.close()
            self.assertEqual(process.wait(timeout=20), 0)
            errors = process.stderr.read()
            self.assertNotIn('Press Enter', errors)  # no console relaunch offer in launcher mode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_port_in_use_is_a_message(self):
        # Also when the program on the port set SO_REUSEADDR, which on Windows would let a
        # second SO_REUSEADDR socket (Splice's, before SO_EXCLUSIVEADDRUSE) bind beside it.
        for reuse in (False, True):
            with self.subTest(occupant_sets_so_reuseaddr=reuse), socket.socket() as taken:
                if reuse:
                    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                taken.bind(('127.0.0.1', 0))
                taken.listen()
                port = taken.getsockname()[1]
                result = subprocess.run([sys.executable, '-B', str(ROOT / 'server.py'), '--port', str(port)],
                                        cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60,
                                        stdin=subprocess.DEVNULL)
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertIn(f'could not listen on 127.0.0.1:{port}', result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_nothing_can_bind_beside_a_running_server(self):
        port = _free_port()
        process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'server.py'), '--port', str(port), '--exit-with-stdin'],
                                   cwd=ROOT, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        try:
            self.assertTrue(process.stdout.readline().startswith(f'http://127.0.0.1:{port}/?launch='))
            self.assertTrue(second_bind_refused(port), f'a second SO_REUSEADDR socket bound 127.0.0.1:{port}')
            probe = subprocess.run([sys.executable, str(ROOT / 'tests' / 'windows_port_probe.py'), str(port)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(probe.returncode, 0, probe.stdout)
        finally:
            process.stdin.close()
            process.wait(timeout=20)
            process.stdout.close()


class ExclusivePort(unittest.TestCase):
    """StudioServer.server_bind on each platform, with a stand-in socket."""

    def bind(self, os_name):
        import server
        fake = _windows_os('C:\\Windows')
        fake.name = os_name
        instance = object.__new__(server.StudioServer)
        instance.socket = mock.Mock()
        instance.socket.getsockname.return_value = ('127.0.0.1', 47999)
        instance.server_address = ('127.0.0.1', 47999)
        with mock.patch.object(server, 'os', fake), \
             mock.patch.object(socket, 'SO_EXCLUSIVEADDRUSE', -5, create=True), \
             mock.patch.object(socket, 'getfqdn', return_value='localhost'):
            instance.server_bind()
        instance.socket.bind.assert_called_once_with(('127.0.0.1', 47999))
        return [call.args for call in instance.socket.setsockopt.call_args_list]

    def test_windows_binds_exclusively(self):
        options = self.bind('nt')
        self.assertEqual(options, [(socket.SOL_SOCKET, -5, 1)])  # SO_EXCLUSIVEADDRUSE, never SO_REUSEADDR

    def test_posix_keeps_so_reuseaddr(self):
        options = self.bind('posix')
        self.assertIn((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), options)
        self.assertNotIn(-5, [option[1] for option in options])


class WindowsSettingsLocations(unittest.TestCase):
    """Every per-user file lands under %LOCALAPPDATA%\\Splice, never beside the program."""

    def setUp(self):
        self.local = Path(tempfile.mkdtemp(prefix='splice-localappdata-')).resolve()
        self.addCleanup(shutil.rmtree, self.local, True)
        self.install = self.local / 'Programs' / 'Splice' / 'Application'
        environ = {k: v for k, v in os.environ.items() if k not in ('SPLICE_DATA_DIR', 'SPLICE_TEMP_DIR', 'SPLICE_EXPORT_DIR')}
        environ['LOCALAPPDATA'] = str(self.local)
        patches = [mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(sys, 'platform', 'win32')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_data_directory(self):
        self.assertEqual(splice_platform.data_directory(), self.local / 'Splice')
        self.assertEqual(splice_platform.data_directory(browser=True), self.local / 'Splice')
        del os.environ['LOCALAPPDATA']
        with mock.patch.object(Path, 'home', return_value=self.local / 'home'):
            self.assertEqual(splice_platform.data_directory(), (self.local / 'home' / 'AppData' / 'Local' / 'Splice'))
        os.environ['SPLICE_DATA_DIR'] = str(self.local / 'elsewhere')
        self.assertEqual(splice_platform.data_directory(), self.local / 'elsewhere')

    def test_settings_and_caches_are_per_user(self):
        data = self.local / 'Splice'
        locations = {
            'RAW decoder settings': raw_decode.config_path(),
            'delivery presets': delivery.user_presets_path(),
            'delivery capability cache': delivery._cache_file(),
            'Resolve libraries': resolve_http._remembered_file(),
            'render queue exports': render_queue.default_folder(),
            'render scratch': render_movie.work_root(),
        }
        for name, path in locations.items():
            with self.subTest(name):
                self.assertTrue(Path(path).is_relative_to(data), f'{name}: {path}')
                self.assertFalse(Path(path).is_relative_to(self.install), f'{name}: {path}')
        self.assertEqual(raw_decode.config_path(), data / 'raw-decoders.json')

    def test_media_root_follows_the_data_directory(self):
        # MEDIA_ROOT is fixed at import, from the same data_directory() checked above.
        result = subprocess.run([sys.executable, '-B', '-c', 'import premiere; print(premiere.MEDIA_ROOT)'], cwd=ROOT,
                                env=dict(os.environ, SPLICE_DATA_DIR=str(self.local / 'data')), capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.local / 'data' / 'media')


class NoConsoleWindows(unittest.TestCase):
    """Every child process goes through splice_platform.run/popen/check_output, which add
    CREATE_NO_WINDOW on Windows, whatever console the server itself has."""
    CREATE_NO_WINDOW = 0x08000000

    def test_helpers_add_the_flag_and_keep_others(self):
        for name, target in (('run', 'run'), ('popen', 'Popen'), ('check_output', 'check_output')):
            with self.subTest(name), mock.patch.object(splice_platform, 'HIDE_CONSOLE', self.CREATE_NO_WINDOW), \
                 mock.patch.object(splice_platform.subprocess, target) as call:
                getattr(splice_platform, name)(['ffmpeg', '-version'], timeout=3)
                getattr(splice_platform, name)(['ffmpeg'], creationflags=0x200)
                self.assertEqual(call.call_args_list[0].args, (['ffmpeg', '-version'],))
                self.assertEqual(call.call_args_list[0].kwargs, {'timeout': 3, 'creationflags': self.CREATE_NO_WINDOW})
                self.assertEqual(call.call_args_list[1].kwargs['creationflags'], self.CREATE_NO_WINDOW | 0x200)

    def test_posix_gets_no_flags(self):
        self.assertEqual(splice_platform.HIDE_CONSOLE, getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if POSIX:
            self.assertEqual(splice_platform.HIDE_CONSOLE, 0)  # the only creationflags value POSIX accepts

    def test_no_shipped_module_starts_a_process_any_other_way(self):
        direct = {'run', 'Popen', 'call', 'check_call', 'check_output', 'getoutput', 'getstatusoutput'}
        found = []
        for relative in _shipped_python():
            if relative == Path('splice_platform.py'):
                continue
            tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (
                        (node.value.id == 'subprocess' and node.attr in direct) or
                        (node.value.id == 'os' and (node.attr in ('system', 'popen') or node.attr.startswith('spawn')))):
                    found.append(f'{relative}:{node.lineno} {node.value.id}.{node.attr}')
                elif isinstance(node, ast.ImportFrom) and node.module == 'subprocess' and \
                        any(alias.name in direct for alias in node.names):
                    found.append(f'{relative}:{node.lineno} from subprocess import ...')
        self.assertEqual(found, [], 'start child processes with splice_platform.run/popen/check_output')
        # The scan is not vacuous: these modules start FFmpeg and ffprobe through the helper.
        for module in ('render_movie.py', 'raw_decode.py', 'delivery.py', 'audio_loudness.py', 'resolve/media.py',
                       'interchange/media.py', 'media_exec.py', 'runtime_check.py'):
            self.assertRegex((ROOT / module).read_text(encoding='utf-8'), r'splice_platform\.(run|popen|check_output)\(')


class BundledFFmpegOnly(unittest.TestCase):
    """The desktop app's server inherits the user's PATH (after the bundled FFmpeg folder) and
    SPLICE_FFMPEG_DIR: every ffmpeg/ffprobe it runs or reports is the bundled one, by path."""
    EXE = '.exe' if sys.platform == 'win32' else ''

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='splice-bundled-'))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.bundled = self.directory / 'bundled'
        self.user = self.directory / 'user'
        self.bundled.mkdir()
        self.user.mkdir()

    def environment(self, **values):
        env = {key: value for key, value in os.environ.items() if key != 'SPLICE_FFMPEG_DIR'}
        env.update(values)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_bare_names_run_the_bundled_tools(self):
        for name in ('run', 'popen', 'check_output'):
            target = {'popen': 'Popen'}.get(name, name)
            with self.subTest(name), self.environment(SPLICE_FFMPEG_DIR=str(self.bundled)), \
                 mock.patch.object(splice_platform.subprocess, target) as call:
                getattr(splice_platform, name)(['ffmpeg', '-version'])
                getattr(splice_platform, name)(('ffprobe', '-v', 'error'))
                getattr(splice_platform, name)(['taskkill', '/T'])
                getattr(splice_platform, name)([str(self.user / 'ffmpeg'), '-version'])  # an explicit path is kept
                commands = [entry.args[0] for entry in call.call_args_list]
                self.assertEqual(commands, [[str(self.bundled / ('ffmpeg' + self.EXE)), '-version'],
                                            [str(self.bundled / ('ffprobe' + self.EXE)), '-v', 'error'],
                                            ['taskkill', '/T'], [str(self.user / 'ffmpeg'), '-version']])
        with self.environment(), mock.patch.object(splice_platform.subprocess, 'run') as call:
            splice_platform.run(['ffmpeg', '-version'])  # outside the desktop app: PATH decides, as before
            self.assertEqual(call.call_args.args[0], ['ffmpeg', '-version'])

    def test_lookups_never_fall_back_to_the_users_ffmpeg(self):
        for name in ('ffmpeg', 'ffprobe'):
            (self.user / (name + self.EXE)).write_text('')
            (self.user / (name + self.EXE)).chmod(0o755)
        path = str(self.user) + os.pathsep + os.environ.get('PATH', '')
        with self.environment(SPLICE_FFMPEG_DIR=str(self.bundled), PATH=path):
            # The bundled files are missing: nothing is found, rather than the user's copy.
            self.assertIsNone(splice_platform.ffmpeg_tool('ffmpeg'))
            for name in ('ffmpeg', 'ffprobe'):
                (self.bundled / (name + self.EXE)).write_text('')
            self.assertEqual(splice_platform.ffmpeg_tool('ffmpeg'), str(self.bundled / ('ffmpeg' + self.EXE)))
            self.assertEqual(splice_platform.ffmpeg_tool('ffprobe'), str(self.bundled / ('ffprobe' + self.EXE)))
            self.assertEqual(delivery._ffmpeg_identity()[0], str(self.bundled / ('ffmpeg' + self.EXE)))
        with self.environment(PATH=path):
            self.assertEqual(Path(splice_platform.ffmpeg_tool('ffmpeg')).parent, self.user)

    @unittest.skipUnless(POSIX, 'fake FFmpeg scripts are POSIX shell')
    def test_startup_check_examines_the_bundled_ffmpeg(self):
        # The user's FFmpeg comes first on PATH and lacks a required filter; the bundled one is complete.
        bundled, _ = fake_ffmpeg(self.bundled)
        fake_ffmpeg(self.user, drop=('zscale',))
        path = str(self.user) + os.pathsep + '/usr/bin:/bin'
        with self.environment(SPLICE_FFMPEG_DIR=str(self.bundled), PATH=path):
            result = runtime_check.report()
        self.assertEqual(result['ffmpeg'], bundled)
        self.assertEqual(result['missing'], {})
        with self.environment(PATH=path):
            self.assertIn('zscale', runtime_check.startup_problem())



class DecoderOnUserPath(unittest.TestCase):
    """The installed app keeps the user's PATH, so a RAW decoder there is found, also when the
    PATH folder is written in Windows' short 8.3 form (C:\\Users\\RUNNER~1\\...)."""

    @unittest.skipUnless(POSIX, 'a symlink stands in for an 8.3 short name')
    def test_decoder_in_a_short_named_folder_is_found(self):
        directory = Path(tempfile.mkdtemp(prefix='splice-userpath-'))
        self.addCleanup(shutil.rmtree, directory, True)
        long = directory / 'runneradmin tools'
        long.mkdir()
        decoder = long / 'braw-decode'
        decoder.write_text('#!/bin/sh\nexit 9\n')
        decoder.chmod(0o755)
        short = directory / 'RUNNER~1'
        short.symlink_to(long, target_is_directory=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith('SPLICE_')}
        env.update(PATH=str(short) + os.pathsep + '/usr/bin:/bin', SPLICE_DATA_DIR=str(directory / 'data'))
        with mock.patch.dict(os.environ, env, clear=True):
            braw = next(entry for entry in raw_decode.decoder_status()['decoders'] if entry['id'] == 'braw')
        self.assertTrue(braw['available'], braw)
        self.assertEqual(braw['path'], str(decoder.resolve()))


def _windows_os(system_root):
    """The os module as seen by code that branches on os.name == 'nt'."""
    fake = types.SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if not name.startswith('__')})
    fake.name = 'nt'
    fake.environ = dict(os.environ, SystemRoot=system_root)
    return fake


class TaskkillBranches(unittest.TestCase):
    ROOT_DIR = '/fake/Windows'

    def test_render_stop_tree_uses_taskkill_on_the_tree(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        with mock.patch.object(render_movie, 'os', _windows_os(self.ROOT_DIR)), \
             mock.patch.object(render_movie.subprocess, 'run') as run:
            render_movie._stop_tree(process)
        command = run.call_args.args[0]
        self.assertEqual(command, [str(Path(self.ROOT_DIR) / 'System32' / 'taskkill.exe'), '/PID', '4321', '/T', '/F'])
        self.assertEqual(run.call_args.kwargs['creationflags'], splice_platform.HIDE_CONSOLE)
        process.kill.assert_called_once_with()

    def test_render_stop_tree_survives_taskkill_failure(self):
        process = mock.Mock(pid=99)
        process.poll.return_value = None
        with mock.patch.object(render_movie, 'os', _windows_os(self.ROOT_DIR)), \
             mock.patch.object(render_movie.subprocess, 'run', side_effect=OSError('no taskkill')):
            render_movie._stop_tree(process, force=False)
        process.terminate.assert_called_once_with()

    def test_posix_stop_tree_never_calls_taskkill(self):
        process = mock.Mock(pid=5)
        process.poll.return_value = None
        fake = _windows_os(self.ROOT_DIR)
        fake.name = 'posix'
        with mock.patch.object(render_movie, 'os', fake), mock.patch.object(render_movie.subprocess, 'run') as run:
            render_movie._stop_tree(process)
        run.assert_not_called()
        process.kill.assert_called_once_with()

    def test_orphan_guard_kills_the_worker_tree_when_the_server_dies(self):
        # A real process that has exited stands in for the dead server: its sentinel is
        # ready, and it is a valid waitable on both POSIX (an fd) and Windows (a handle).
        gone = multiprocessing.get_context('spawn').Process(target=int)
        gone.start()
        gone.join(30)
        self.addCleanup(gone.close)
        read = gone.sentinel
        called = threading.Event()
        calls = []

        def run(command, **options):
            calls.append((command, options))
            called.set()

        fake = _windows_os(self.ROOT_DIR)
        fake.killpg = killpg = mock.Mock()  # the POSIX branch would end this test run's process group
        with mock.patch.object(export_jobs, 'os', fake), \
             mock.patch.object(export_jobs.multiprocessing, 'parent_process', return_value=types.SimpleNamespace(sentinel=read)), \
             mock.patch.object(export_jobs.subprocess, 'run', side_effect=run):
            export_jobs._orphan_guard()
            self.assertTrue(called.wait(10))
        command, options = calls[0]
        self.assertEqual(command, [str(Path(self.ROOT_DIR) / 'System32' / 'taskkill.exe'), '/PID', str(os.getpid()), '/T', '/F'])
        self.assertEqual(options['creationflags'], splice_platform.HIDE_CONSOLE)
        killpg.assert_not_called()

    def test_export_cancel_path_keeps_the_windows_tree_kill(self):
        # The job loop's Windows branch (taskkill /T on the worker, then kill) and the POSIX
        # process-group branch from the cancel fixes both stay in place.
        source = (ROOT / 'export_jobs.py').read_text(encoding='utf-8')
        windows = source[source.index("if os.name == 'nt':\n                            # Let the renderer"):]
        windows = windows[:windows.index('elif grouped:')]
        self.assertIn("'/PID', str(process.pid), '/T', '/F'", windows)
        self.assertIn('splice_platform.run([str(taskkill)', windows)  # no console window
        self.assertIn('process.kill()', windows)
        self.assertIn('os.killpg(process.pid, signal.SIGKILL)', source)

    def test_bounded_runs_hide_the_console(self):
        with mock.patch.object(media_exec.subprocess, 'Popen', side_effect=OSError('stop')) as popen:
            with self.assertRaises(OSError):
                media_exec.run_bounded(['ffprobe'], timeout=1, max_output=10)
        self.assertEqual(popen.call_args.kwargs['creationflags'], splice_platform.HIDE_CONSOLE)


def _zip(path, members):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


class InstallerStaging(unittest.TestCase):
    """build.py --stage-only with small stand-in runtime archives."""

    def setUp(self):
        import build
        self.build = build
        self.directory = Path(tempfile.mkdtemp(prefix='splice-stage-'))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.cache = self.directory / 'downloads'
        self.cache.mkdir()

    def runtimes(self, *, python_lacks=(), ffmpeg_members=None):
        library = io.BytesIO()
        with zipfile.ZipFile(library, 'w') as archive:
            for name in sorted(sys.stdlib_module_names - set(python_lacks)):
                archive.writestr(f'{name}.pyc', b'')
        archives = dict(
            electron=_zip(self.cache / 'electron.zip', {'electron.exe': b'MZ', 'resources/default_app.asar': b'x',
                                                        'LICENSE': b'MIT', 'LICENSES.chromium.html': b'<html>'}),
            python=_zip(self.cache / 'python.zip', {'python.exe': b'MZ', 'python314._pth': 'python314.zip\n.\n',
                                                    'python314.zip': library.getvalue(), '_sqlite3.pyd': b'',
                                                    'LICENSE.txt': b'PSF'}),
            ffmpeg=_zip(self.cache / 'ffmpeg.zip', ffmpeg_members or {
                'ffmpeg-9.1/bin/ffmpeg.exe': b'MZ', 'ffmpeg-9.1/bin/ffprobe.exe': b'MZ', 'ffmpeg-9.1/bin/ffplay.exe': b'MZ',
                'ffmpeg-9.1/bin/avfilter-12.dll': b'MZ', 'ffmpeg-9.1/LICENSE': b'GPLv3', 'ffmpeg-9.1/README.txt': b'config',
                'ffmpeg-9.1/include/libavutil/avutil.h': b''}),
        )
        specs = {name: dict(self.build.RUNTIMES[name], url='https://example.invalid/' + path.name,
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for name, path in archives.items()}
        return mock.patch.dict(self.build.RUNTIMES, specs)

    def sources(self):
        return mock.patch.object(self.build, 'source_files', return_value=[
            Path('runtime_check.py'), Path('splice_platform.py'), Path('media_exec.py'), Path('assets/fonts/dmsans-LICENSE.txt')])

    def test_stage_layout_and_manifest(self):
        with self.runtimes(), self.sources(), mock.patch.object(self.build.urllib.request, 'urlopen') as fetch:
            manifest = self.build.stage(self.directory / 'out', self.cache)
        fetch.assert_not_called()  # cached, verified archives are reused
        payload = self.directory / 'out' / 'stage' / 'payload'
        resources = payload / 'resources'
        self.assertTrue((payload / 'Splice.exe').is_file())
        self.assertFalse((resources / 'default_app.asar').exists())
        self.assertTrue((payload / 'Third-party.txt').is_file())
        self.assertEqual((resources / 'python' / 'python314._pth').read_text().splitlines()[-1], '../splice')
        self.assertEqual(sorted(p.name for p in (resources / 'ffmpeg').iterdir()),
                         ['BUILD-CONFIGURATION.txt', 'LICENSE.txt', 'avfilter-12.dll', 'ffmpeg.exe', 'ffprobe.exe'])
        self.assertTrue((resources / 'splice' / 'assets' / 'fonts' / 'dmsans-LICENSE.txt').is_file())
        self.assertTrue((resources / 'splice' / 'runtime_check.py').is_file())
        self.assertEqual(sorted(p.name for p in (resources / 'app').iterdir()), ['main.cjs', 'package.json'])
        self.assertIn('subprocess', manifest['python_modules']['required'])
        self.assertEqual(json.loads((self.directory / 'out' / 'manifest.json').read_text()), manifest)
        self.assertEqual(manifest['ffmpeg_files'], ['BUILD-CONFIGURATION.txt', 'LICENSE.txt', 'avfilter-12.dll',
                                                    'ffmpeg.exe', 'ffprobe.exe'])

    def test_missing_stdlib_module_stops_the_build(self):
        with self.runtimes(python_lacks=('subprocess',)), self.sources():
            with self.assertRaisesRegex(ValueError, 'lacks modules Splice imports: subprocess'):
                self.build.stage(self.directory / 'out', self.cache)

    def test_archive_without_ffprobe_stops_the_build(self):
        with self.runtimes(ffmpeg_members={'f/bin/ffmpeg.exe': b'MZ', 'f/LICENSE': b'', 'f/README.txt': b''}), self.sources():
            with self.assertRaisesRegex(ValueError, 'no ffprobe.exe'):
                self.build.stage(self.directory / 'out', self.cache)

    def test_unsafe_archive_paths_are_refused(self):
        with self.runtimes(ffmpeg_members={'../escape.exe': b'MZ'}), self.sources():
            with self.assertRaisesRegex(ValueError, 'unsafe path'):
                self.build.stage(self.directory / 'out', self.cache)

    def test_bad_download_is_discarded(self):
        spec = dict(self.build.RUNTIMES['python'], url='https://example.invalid/python.zip', sha256='0' * 64)
        with mock.patch.dict(self.build.RUNTIMES, {'python': spec}), \
             mock.patch.object(self.build.urllib.request, 'urlopen', return_value=io.BytesIO(b'tampered')):
            with self.assertRaisesRegex(ValueError, 'checksum did not match'):
                self.build.fetch('python', self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_cached_archive_is_hashed_again(self):
        # A cached download that no longer matches its pin is replaced, never trusted.
        good = _zip(self.directory / 'good.zip', {'python.exe': b'MZ'}).read_bytes()
        spec = dict(self.build.RUNTIMES['python'], url='https://example.invalid/python.zip',
                    sha256=hashlib.sha256(good).hexdigest())
        cached = self.cache / 'python.zip'
        cached.write_bytes(b'tampered after an earlier build')
        with mock.patch.dict(self.build.RUNTIMES, {'python': spec}), \
             mock.patch.object(self.build.urllib.request, 'urlopen', return_value=io.BytesIO(good)) as download:
            self.assertEqual(self.build.fetch('python', self.cache), cached)
        download.assert_called_once()
        self.assertEqual(cached.read_bytes(), good)
        with mock.patch.dict(self.build.RUNTIMES, {'python': spec}), \
             mock.patch.object(self.build.urllib.request, 'urlopen') as download:
            self.build.fetch('python', self.cache)
        download.assert_not_called()  # now it matches, so it is reused

    def test_nsis_is_pinned_and_unpacked_from_the_verified_zip(self):
        spec = self.build.BUILD_TOOLS['nsis']
        self.assertTrue(spec['url'].startswith('https://'))
        self.assertRegex(spec['sha256'], r'^[0-9a-f]{64}$')
        archive = _zip(self.cache / 'nsis.zip', {'nsis-3.11/makensis.exe': b'MZ', 'nsis-3.11/Stubs/zlib-x86-unicode': b''})
        fake = dict(spec, url='https://example.invalid/nsis.zip', sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        with mock.patch.dict(self.build.BUILD_TOOLS, {'nsis': fake}):
            compiler = self.build.nsis_compiler(self.directory / 'out', self.cache)
        self.assertEqual(compiler, self.directory / 'out' / 'nsis' / 'nsis-3.11' / 'makensis.exe')
        with mock.patch.dict(self.build.BUILD_TOOLS, {'nsis': dict(fake, sha256='0' * 64)}), \
             mock.patch.object(self.build.urllib.request, 'urlopen', return_value=io.BytesIO(b'other')):
            with self.assertRaisesRegex(ValueError, 'checksum did not match'):
                self.build.nsis_compiler(self.directory / 'out', self.cache)
        workflow = (ROOT / '.github' / 'workflows' / 'windows-installer.yml').read_text(encoding='utf-8')
        self.assertNotIn('choco', workflow)  # the installer stub comes from the pinned zip only

    def test_source_archive_collects_the_ffmpeg_source_and_names_the_gap(self):
        readme = ('FFmpeg 64-bit shared Windows build from www.gyan.dev\n\nSource Code: '
                  'https://github.com/FFmpeg/FFmpeg/commit/74e4393595\n\nrelease-full external libraries\' versions: \n\n'
                  'x264                0.165.3223\nzimg                release-3.0.6-253-g67e0603\n')
        build = _zip(self.cache / 'ffmpeg.zip', {'ffmpeg-9.1/README.txt': readme, 'ffmpeg-9.1/LICENSE': b'GPLv3'})
        tarball = self.cache / 'ffmpeg-9.1.tar.xz'
        tarball.write_bytes(b'source')
        runtime = dict(self.build.RUNTIMES['ffmpeg'], url='https://example.invalid/ffmpeg.zip',
                       sha256=hashlib.sha256(build.read_bytes()).hexdigest())
        source = dict(self.build.SOURCES['ffmpeg-source'], url='https://example.invalid/ffmpeg-9.1.tar.xz',
                      sha256=hashlib.sha256(b'source').hexdigest())
        with mock.patch.dict(self.build.RUNTIMES, {'ffmpeg': runtime}), \
             mock.patch.dict(self.build.SOURCES, {'ffmpeg-source': source}):
            report = self.build.source_archive(self.directory / 'out', self.cache)
            folder = self.directory / 'out' / 'source'
            self.assertEqual((folder / 'ffmpeg-9.1.tar.xz').read_bytes(), b'source')
            self.assertEqual(report['libraries'], [
                dict(name='x264', version='0.165.3223', git_revision=None),
                dict(name='zimg', version='release-3.0.6-253-g67e0603', git_revision='67e0603')])
            text = (folder / 'SOURCES.txt').read_text()
            self.assertIn('KNOWN GAP', text)
            self.assertIn('74e4393595cf2b5857f0da925c4e0dd74ea306e5', text)
            self.assertEqual(json.loads((folder / 'sources.json').read_text()), report)
            other = _zip(self.cache / 'ffmpeg.zip', {'ffmpeg-9.1/README.txt': readme.replace('74e4393595', '0123456789')}
                         ).read_bytes()
            with mock.patch.dict(self.build.RUNTIMES, {'ffmpeg': dict(runtime, sha256=hashlib.sha256(other).hexdigest())}):
                with self.assertRaisesRegex(ValueError, 'different source commit'):
                    self.build.source_archive(self.directory / 'out', self.cache)
        self.assertEqual(self.build.SOURCES['ffmpeg-source']['commit'][:10], '74e4393595')
        self.assertIn('--source-archive', (ROOT / 'desktop' / 'windows' / 'Third-party.txt').read_text(encoding='utf-8'))

    def test_real_sources_need_only_bundled_modules(self):
        # The editor as shipped: everything it imports is standard library or guarded optional.
        shipped = [ROOT / path for path in self.build.source_files() if path.suffix == '.py']
        required, optional = self.build.stdlib_requirements(ROOT, shipped)
        self.assertLessEqual(set(optional), {'importlib_metadata', 'pyzstd', 'zstandard'})
        self.assertIn('sqlite3', required)
        self.assertIn('compression', required)  # zstd for Resolve libraries (Python 3.14)

    def test_pins_are_recorded_in_the_notices(self):
        notices = (ROOT / 'desktop' / 'windows' / 'Third-party.txt').read_text(encoding='utf-8')
        for name, spec in self.build.RUNTIMES.items():
            with self.subTest(name):
                self.assertTrue(spec['url'].startswith('https://'))
                self.assertRegex(spec['sha256'], r'^[0-9a-f]{64}$')
                self.assertIn(spec['sha256'], notices)
                self.assertIn(spec['version'].split()[0], notices)
        package = json.loads((ROOT / 'desktop' / 'windows' / 'package.json').read_text(encoding='utf-8'))
        self.assertIn('!ifndef VERSION', (ROOT / 'desktop' / 'windows' / 'installer.nsi').read_text(encoding='utf-8'))
        self.assertRegex(package['version'], r'^\d+\.\d+\.\d+')

    @unittest.skipUnless(shutil.which('node'), 'Node is not installed')
    def test_launcher_scripts_parse(self):
        for script in ('desktop/windows/main.cjs', 'desktop/windows/fuses.mjs', 'tests/windows-installer.cjs',
                       'tests/windows-launcher.cjs'):
            with self.subTest(script):
                subprocess.run(['node', '--check', str(ROOT / script)], check=True, timeout=30)

    @unittest.skipUnless(shutil.which('node') and (ROOT / 'desktop' / 'windows' / 'node_modules' / '@electron' / 'fuses').is_dir(),
                         'needs Node and "npm ci" in desktop/windows')
    def test_fuses_are_turned_off(self):
        from fuses_fixture import fake_electron
        executable = fake_electron(self.directory / 'Splice.exe')
        states = self.build.harden_electron(executable)
        self.assertEqual(states, dict(RunAsNode='disabled', EnableNodeOptionsEnvironmentVariable='disabled',
                                      EnableNodeCliInspectArguments='disabled'))
        wire = fake_electron.wire(executable)
        self.assertEqual(wire, '000011111')  # only fuses 0, 2 and 3 changed (cookie encryption etc. untouched)
        node = shutil.which('node')
        script = str(ROOT / 'desktop' / 'windows' / 'fuses.mjs')
        check = subprocess.run([node, script, 'check', str(executable)], capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stderr)
        # The Playwright copy gets --inspect back; the shipped name is refused.
        refused = subprocess.run([node, script, 'test-copy', str(executable)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(refused.returncode, 0)
        copy = self.directory / 'Splice-under-test.exe'
        shutil.copy2(executable, copy)
        subprocess.run([node, script, 'test-copy', str(copy)], check=True, capture_output=True, timeout=60)
        self.assertEqual(fake_electron.wire(copy), '000111111')
        check = subprocess.run([node, script, 'check', str(copy)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(check.returncode, 0)
        self.assertIn('EnableNodeCliInspectArguments', check.stderr)

    def test_build_turns_off_the_fuses_before_compiling(self):
        source = (ROOT / 'desktop' / 'windows' / 'build.py').read_text(encoding='utf-8')
        body = source[source.index('def build('):]
        self.assertLess(body.index('harden_electron('), body.index('verify_on_windows('))
        package = json.loads((ROOT / 'desktop' / 'windows' / 'package.json').read_text(encoding='utf-8'))
        self.assertEqual(list(package['devDependencies']), ['@electron/fuses'])
        self.assertRegex(package['devDependencies']['@electron/fuses'], r'^\d+\.\d+\.\d+$')  # pinned, no range
        self.assertNotIn('dependencies', package)
        lock = json.loads((ROOT / 'desktop' / 'windows' / 'package-lock.json').read_text(encoding='utf-8'))
        self.assertEqual(set(lock['packages']), {'', 'node_modules/@electron/fuses'})  # nothing else is pulled in

    @unittest.skipUnless(shutil.which('node'), 'Node is not installed')
    def test_desktop_launcher(self):
        # main.cjs with a stand-in Electron and editor server: the launch link is loaded exactly as
        # printed, a wrong one is refused with a message at once, permissions and navigation are locked.
        result = subprocess.run([shutil.which('node'), str(ROOT / 'tests' / 'windows-launcher.cjs')], cwd=ROOT,
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('launcher scenarios passed', result.stdout)

    def test_launcher_matches_the_server(self):
        main = (ROOT / 'desktop' / 'windows' / 'main.cjs').read_text(encoding='utf-8')
        for flag in re.findall(r"'(--[a-z-]+)'", main):
            if flag in ('--port',):
                continue
            with self.subTest(flag):
                self.assertIn(f"'{flag}'", (ROOT / 'server.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
