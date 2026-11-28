"""The Windows installer's platform pieces, exercised on any OS with mocks: the
FFmpeg feature gate, per-user settings locations, the taskkill stop paths, the
launcher's server mode and the installer staging (build.py --stage-only)."""
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
import types
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'desktop' / 'windows'))

import delivery  # noqa: E402
import export_jobs  # noqa: E402
import media_exec  # noqa: E402
import raw_decode  # noqa: E402
import render_movie  # noqa: E402
import render_queue  # noqa: E402
from resolve import http as resolve_http  # noqa: E402
import runtime_check  # noqa: E402
import splice_platform  # noqa: E402

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
        return 'Formats:\n D.. = Demuxing\n ---\n' + ''.join(f'  E  {name:<15} x\n' for name in names)
    return f'{kind.title()}:\n V..... = Video\n ------\n' + ''.join(f' V....D {name:<20} x\n' for name in names)


def fake_ffmpeg(directory, *, drop=(), exit_code=0, sleep=0):
    """An ffmpeg/ffprobe pair answering the four listing flags like FFmpeg 8."""
    directory = Path(directory)
    everything = dict(
        filters=runtime_check.REQUIRED_FILTERS + runtime_check.OPTIONAL_FILTERS,
        encoders=runtime_check.REQUIRED_ENCODERS + runtime_check.OPTIONAL_ENCODERS,
        decoders=runtime_check.REQUIRED_DECODERS + runtime_check.OPTIONAL_DECODERS,
        muxers=runtime_check.REQUIRED_MUXERS)
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
        ffmpeg, ffprobe = fake_ffmpeg(self.directory, drop=('rubberband', 'vidstabdetect', 'prores_raw'))
        self.assertEqual(runtime_check.startup_problem(ffmpeg, ffprobe), '')
        problem = runtime_check.startup_problem(ffmpeg, ffprobe, strict=True)
        self.assertIn('vidstabdetect, rubberband', problem)
        self.assertIn('decoders: prores_raw', problem)
        self.assertEqual(runtime_check.report(ffmpeg, ffprobe)['optional'],
                         {'filters': ['vidstabdetect', 'rubberband'], 'decoders': ['prores_raw']})

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
        with mock.patch('runtime_check.shutil.which', return_value=None):
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

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is not installed')
    def test_every_listed_feature_is_a_real_ffmpeg_name(self):
        # Catches a typo in the lists: the system FFmpeg used by the test suite has them all.
        result = runtime_check.report()
        self.assertEqual(result['missing'], {}, result)


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
        with socket.socket() as taken:
            taken.bind(('127.0.0.1', 0))
            taken.listen()
            port = taken.getsockname()[1]
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'server.py'), '--port', str(port)],
                                    cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(f'could not listen on 127.0.0.1:{port}', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


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
        source = (ROOT / 'premiere.py').read_text(encoding='utf-8')
        self.assertIn("MEDIA_ROOT = data_directory() / 'media'", source)

    def test_hidden_console_flag(self):
        # CREATE_NO_WINDOW on Windows; 0 (the only value POSIX accepts) elsewhere.
        self.assertEqual(splice_platform.HIDE_CONSOLE, getattr(subprocess, 'CREATE_NO_WINDOW', 0))


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
        self.assertIn('creationflags=HIDE_CONSOLE', windows)
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
        for script in ('desktop/windows/main.cjs', 'tests/windows-installer.cjs'):
            with self.subTest(script):
                subprocess.run(['node', '--check', str(ROOT / script)], check=True, timeout=30)

    def test_launcher_matches_the_server(self):
        main = (ROOT / 'desktop' / 'windows' / 'main.cjs').read_text(encoding='utf-8')
        for flag in re.findall(r"'(--[a-z-]+)'", main):
            if flag in ('--port',):
                continue
            with self.subTest(flag):
                self.assertIn(f"'{flag}'", (ROOT / 'server.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
