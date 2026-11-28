# Contributing: running from source and testing

## Running from source

You need:

- **Python 3.10 or newer.** The launchers check for 3.10. Development, the test suites and the Windows build use Python 3.14; older versions are not covered by CI. Reading Resolve projects uses `compression.zstd` on Python 3.14, an installed `zstandard` or `pyzstd` package if present, and otherwise Splice's own zstd decoder.
- **FFmpeg and ffprobe** on `PATH`, with the features in [Requirements](../README.md#requirements). Run `python3 runtime_check.py` to check them.
- **Chrome, Chromium or Edge** for the desktop window and the browser tests.

No Python packages are needed; Splice uses the standard library only.

Start Splice in one of two ways:

- `./splice` on Linux (or `python3 splice_app.py` on any OS) opens Splice in a dedicated browser window on port 47834 and stops the server when the window closes. It needs a graphical session.
- `python3 server.py` starts the server on port 8765 (`--port` to change it) and prints a **one-time launch link** (`http://127.0.0.1:8765/?launch=…`). Open that link in Chrome, Chromium or Edge. The plain address is refused, and the link works once. Tabs that used it keep working. If you lose the session, press **Enter** in the server's terminal, or run `kill -USR1 <pid>` on Linux or macOS, for a new one-time link; the old link stops working.

`python3 server.py --require-ffmpeg-features` refuses to start, with a message, when FFmpeg lacks something Splice uses. The desktop app always starts the server this way.

### Environment variables

| Variable | Effect |
|---|---|
| `SPLICE_DATA_DIR` | Splice's data folder. Default: `~/.local/share/splice` (Linux), `~/Library/Application Support/Splice` (macOS), `%LOCALAPPDATA%\Splice` (Windows). |
| `SPLICE_TEMP_DIR` | Render scratch folder. Default: `render-work` in the data folder. |
| `SPLICE_EXPORT_DIR` | Default render-queue output folder. Default: `Exports` in the data folder. |
| `SPLICE_EXPORT_STALL_SECONDS` | Stop an export when FFmpeg makes no progress for this long (1–86400; default 300). |
| `SPLICE_BROWSER` | Browser for `./splice` / `splice_app.py`. |
| `SPLICE_BRAW_SDK`, `SPLICE_REDLINE`, `SPLICE_ARRI_CMD`, `SPLICE_LIBRAW`, `SPLICE_CRM_CMD`, `SPLICE_NRAW_CMD`, `SPLICE_XOCN_CMD`, `SPLICE_RAW_DECODERS` | Camera RAW decoders; see [Camera RAW](camera-raw.md#setting-up-vendor-decoders). |
| `SPLICE_CHROME` | Chrome or Chromium for the browser tests, if it is not in a standard location. |
| `SPLICE_SLOW_TESTS=1` | Also run the feature-length export tests. |

## Running the tests

There are two suites: Python unit and render tests (`tests/test_*.py`) and browser flows (`tests/*.cjs`) that drive the real app in headless Chromium.

### Python, in batches

The whole Python suite is large (several hundred tests, roughly a quarter of an hour of work, much of it real FFmpeg renders). Run it **in batches** rather than in one go, so a single slow or stuck module does not hide the others and no one command runs for too long. One module per run is the simplest:

```sh
for module in tests/test_*.py; do
  python3 -m unittest discover -s tests -p "$(basename "$module")" || echo "FAILED: $module"
done
```

or group by area, for example `-p 'test_render*.py'`, `-p 'test_delivery*.py'`, `-p 'test_audio*.py'`, `-p 'test_interchange*.py'`, `-p 'test_resolve*.py'`, `-p 'test_camera_raw*.py'`, then the remaining modules. The feature-length export tests are skipped unless `SPLICE_SLOW_TESTS=1`.

`python3 -m unittest discover -s tests` runs everything in one process if you prefer.

### Browser suites

Install Node.js 20 or newer and Chrome or Chromium, then:

```sh
npm ci
npm test
```

`npm test` runs the whole Python suite first and then every browser flow in order. To run browser flows on their own, run them one at a time, for example `node tests/editing-core.cjs`. The full list is the `test` script in [`package.json`](../package.json). Set `SPLICE_CHROME` if Chrome or Chromium is not at a standard path. Several flows accept `SPLICE_ONLY=<scenario>[,<scenario>]` to run chosen scenarios, and `SPLICE_EVIDENCE_DIR` or `SPLICE_PROOF_DIR` to save screenshots and measurements.

### Temporary files and disk space

The tests render real media and create many temporary folders. Point `TMPDIR` at a dedicated, empty folder on a disk with plenty of free space, so you can see what a run leaves behind and remove it afterwards:

```sh
export TMPDIR="$(mktemp -d /var/tmp/splice-tests-XXXX)"
python3 -m unittest discover -s tests -p 'test_lut.py'
ls -A "$TMPDIR"    # should be empty after a clean run
```

A short `TMPDIR` path is safest for the browser flows. After a clean run `TMPDIR` should hold no `splice-*` folders or `*.prproj` files; if it does, that is a bug worth reporting. Python's multiprocessing can print a `resource_tracker` warning about leaked semaphores when a render worker is killed on purpose; that is not a file leak.

### Windows

`tests/test_windows_installer.py` runs on any OS (with Windows behaviour mocked). The installed-app check (`tests/windows-installer.cjs`) runs in the *Windows installer* GitHub workflow.

## Building

- Portable Windows and macOS archives: `python3 package_desktop.py artifacts`.
- Windows installer, on Windows with Python 3.11 or later: `python desktop/windows/build.py artifacts/installer` (`--stage-only` stages without NSIS, on any OS; `--source-archive` collects the bundled FFmpeg's published source). See [Windows](windows.md).

## Project conventions

- Python standard library only; no runtime dependencies. The only Node dependency is `playwright-core`, for tests.
- Every child process goes through `splice_platform.run` / `popen` / `check_output` (a test fails on a direct `subprocess` call in shipped code), so no console window opens on Windows.
- `runtime_check.py` lists exactly the FFmpeg features the code uses; a test keeps that list in step with the code in both directions.
