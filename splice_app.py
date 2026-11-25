"""Launch Splice in a dedicated desktop window."""
import argparse
import html
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from server import create_server
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
    with server:
        launcher = write_launcher(profile, server.launch_url())
        server.on_launch = lambda: launcher.unlink(missing_ok=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return subprocess.run([
                browser, f"--app={launcher.as_uri()}",
                f"--user-data-dir={profile}", "--no-first-run", "--disable-sync",
                "--window-size=1480,960", *flags,
            ], check=False).returncode
        finally:
            launcher.unlink(missing_ok=True)
            server.shutdown()
            thread.join()


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
