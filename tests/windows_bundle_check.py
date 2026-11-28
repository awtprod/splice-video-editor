"""Check an installed (or staged) Windows bundle with its own Python and FFmpeg.

Run with the bundled interpreter, PATH holding only the bundled FFmpeg:

    <app>\\resources\\python\\python.exe tests\\windows_bundle_check.py <app>\\resources <report.json>

It fails unless every standard-library module in the build manifest imports,
FFmpeg passes the strict feature gate (optional filters included), every
software delivery format passes a real test encode (x265 10-bit, ProRes,
DNxHR, AV1, EXR, TIFF, PNG, DPX, ...), FFmpeg has the prores_raw decoder, and
the server started as the launcher starts it, with no FFmpeg reachable, exits
with its message instead of a traceback.
"""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _gate_without_ffmpeg(editor, data):
    empty = Path(data) / 'empty-path'
    empty.mkdir()
    env = {key: value for key, value in os.environ.items() if key.lower() != 'path'}
    env['PATH' if os.name != 'nt' else 'Path'] = env['SPLICE_FFMPEG_DIR'] = str(empty)
    result = subprocess.run([sys.executable, '-B', str(editor / 'server.py'), '--port', '0', '--exit-with-stdin',
                             '--require-ffmpeg-features'], cwd=editor, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=120)
    return dict(exit=result.returncode, stdout=result.stdout[-500:], stderr=result.stderr[-1500:])


def main(resources, report_path):
    resources = Path(resources)
    editor = resources / 'splice'
    sys.path.insert(0, str(editor))  # the bundle's ._pth already has it; this also covers a staged copy
    failures, report = [], {}

    manifest_path = resources.parent / 'manifest.json'
    if manifest_path.is_file():
        modules = json.loads(manifest_path.read_text(encoding='utf-8'))['python_modules']['required']
    else:
        modules = ['sqlite3', 'ctypes', 'multiprocessing', 'compression', 'msvcrt', 'winreg', 'zlib', 'hashlib']
    broken = {}
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as error:  # report every missing module, not just the first
            broken[name] = f'{type(error).__name__}: {error}'
    report['python'] = dict(version=sys.version, executable=sys.executable, modules=len(modules), broken=broken)
    if broken:
        failures.append('Python modules failed to import: ' + ', '.join(sorted(broken)))

    import runtime_check
    problem = runtime_check.startup_problem(strict=True)
    report['ffmpeg_features'] = problem or 'all required and optional features present'
    if problem:
        failures.append(problem)

    with tempfile.TemporaryDirectory(prefix='splice-bundle-check-') as data:
        os.environ['SPLICE_DATA_DIR'] = data
        gate = _gate_without_ffmpeg(editor, data)
        report['startup_gate'] = gate
        if gate['exit'] != 3 or 'ffmpeg was not found' not in gate['stderr'] or 'Traceback' in gate['stderr']:
            failures.append('The server did not refuse to start cleanly without FFmpeg: ' + json.dumps(gate))
        import delivery
        import raw_decode
        capabilities = delivery.capabilities(refresh=True)
        software = {key: reason for key, reason in capabilities['variants'].items() if not key.split('|')[4]}
        failed = {key: reason for key, reason in software.items() if reason}
        report['delivery'] = dict(ffmpeg=capabilities['ffmpeg'], software_variants=len(software), failed=failed,
                                  audio=capabilities['audio'])
        if not software or failed:
            failures.append('Delivery test encodes failed: ' + json.dumps(failed or 'no software variants'))
        if any(capabilities['audio'].values()):
            failures.append('Audio encoders failed: ' + json.dumps(capabilities['audio']))
        has_raw, version = raw_decode.ffmpeg_has_prores_raw()
        report['prores_raw'] = dict(decoder=has_raw, ffmpeg=version)
        if not has_raw:
            failures.append('FFmpeg has no prores_raw decoder')

    report['failures'] = failures
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
