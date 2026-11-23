"""Launch Splice in a dedicated desktop window."""
import argparse
import os
import shutil
import subprocess
import sys
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
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return subprocess.run([
                browser, f"--app=http://127.0.0.1:{server.server_port}/",
                f"--user-data-dir={profile}", "--no-first-run", "--disable-sync",
                "--window-size=1480,960", *flags,
            ], check=False).returncode
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    sys.exit(main())
