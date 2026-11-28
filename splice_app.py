"""Launch Splice in a dedicated desktop window."""
import argparse
import html
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time

from server import create_server
import splice_platform
from splice_platform import browser_executable, data_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=47834)
    args = parser.parse_args()
    browser = browser_executable()
    if not browser:
        sys.exit("Install Google Chrome, Chromium, or Microsoft Edge to launch Splice.")
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        sys.exit("Install FFmpeg and add its bin directory to PATH. Missing: " + ", ".join(missing))
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.exit("Splice needs a graphical Linux session. Run python3 server.py for browser access.")
    profile = data_directory(browser=True) / "browser"
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        server = create_server(args.port)
    except OSError as error:
        sys.exit(f"Could not open Splice on port {args.port}: {error}. Check for an existing window.")
    flags = ["--class=Splice"] if sys.platform.startswith("linux") else []
    remove_stale_launchers(profile)
    with server:
        launcher = write_launcher(profile, server.launch_url())
        server.on_launch = lambda: launcher.unlink(missing_ok=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return splice_platform.run([
                browser, f"--app={launcher.as_uri()}",
                f"--user-data-dir={profile}", "--no-first-run", "--disable-sync",
                "--window-size=1480,960", *flags,
            ], check=False).returncode
        finally:
            launcher.unlink(missing_ok=True)
            server.shutdown()
            thread.join()


# tempfile.mkstemp(prefix="launch-", suffix=".html") names: eight characters from its alphabet.
LAUNCHER_NAME = re.compile(r"launch-[a-z0-9_]{8}\.html")
STALE_LAUNCHER_SECONDS = 60


def remove_stale_launchers(directory, min_age=STALE_LAUNCHER_SECONDS):
    """Delete launch pages an earlier run left behind (it was killed before the link was used).
    Their links are dead with that server, but each still names a launch secret. Only regular
    files directly in ``directory`` named exactly like write_launcher's are removed, never a
    symlink or anything through one, and none younger than ``min_age`` seconds (another Splice
    starting on a different port may be using its own). Returns the names removed."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return []
    removed, failed, now = [], 0, time.time()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not LAUNCHER_NAME.fullmatch(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False) or now - entry.stat(follow_symlinks=False).st_mtime < min_age:
                        continue
                    os.unlink(entry.path)  # Removes a name in this folder; a swapped-in symlink's target is untouched.
                except FileNotFoundError:
                    continue  # Another Splice starting at the same time removed it first.
                except OSError as error:
                    if not failed:  # One line, however many files: the cause is the same.
                        print(f"Splice could not remove an old launch file {entry.name}: {error}", file=sys.stderr)
                    failed += 1
                    continue
                removed.append(entry.name)
    except OSError as error:  # An unreadable profile folder: start anyway; the browser will say more.
        print(f"Splice could not look for old launch files in {directory}: {error}", file=sys.stderr)
    return removed


def write_launcher(directory, url):
    """A private local page that forwards to the one-time launch link, so
    the link never appears on the browser's command line (visible to every
    process on the machine). The server deletes it once the link is used."""
    handle, name = tempfile.mkstemp(dir=directory, prefix="launch-", suffix=".html")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as page:
            page.write('<!doctype html><meta charset="utf-8"><title>Splice</title>'
                       f'<meta http-equiv="refresh" content="0;url={html.escape(url)}">')
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)

if __name__ == "__main__":
    sys.exit(main())
