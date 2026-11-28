"""Installation paths for the desktop editor."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


# Windows: start console programs (FFmpeg, taskkill) without a console window.
# 0 elsewhere, the only creationflags value POSIX accepts.
HIDE_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# Every child process Splice starts (FFmpeg, ffprobe, taskkill, ...) goes through
# these, so none opens a console window on Windows however the server itself was
# started. tests/test_windows_installer.py fails if a shipped module calls
# subprocess directly.
def _no_console(options):
    options["creationflags"] = options.get("creationflags", 0) | HIDE_CONSOLE
    return options


# The desktop app sets SPLICE_FFMPEG_DIR to its bundled FFmpeg folder. Then every "ffmpeg"
# and "ffprobe" Splice runs or looks up is that folder's, by absolute path. A bare name is
# not enough: Windows looks in the Python folder, the working folder and System32 before PATH,
# and PATH also holds the user's folders (searched for camera RAW decoders).
FFMPEG_TOOLS = ("ffmpeg", "ffprobe")


def _bundled_tool(name):
    folder = os.environ.get("SPLICE_FFMPEG_DIR")
    if not folder:
        return None
    return str(Path(folder) / (name + (".exe" if sys.platform == "win32" else "")))


def ffmpeg_tool(name):
    """Path of ``ffmpeg`` or ``ffprobe``: the bundled one in the desktop app, else the one on
    PATH. None when it is missing."""
    bundled = _bundled_tool(name)
    if bundled is not None:
        return bundled if os.path.isfile(bundled) else None
    return shutil.which(name)


def _command(command):
    if isinstance(command, (list, tuple)) and command and command[0] in FFMPEG_TOOLS:
        bundled = _bundled_tool(command[0])
        if bundled is not None:
            # Even a missing bundled file is run by its path, so it fails rather than falling back.
            return [bundled, *command[1:]]
    return command


def run(command, **options):
    """subprocess.run without a console window."""
    return subprocess.run(_command(command), **_no_console(options))


def popen(command, **options):
    """subprocess.Popen without a console window."""
    return subprocess.Popen(_command(command), **_no_console(options))


def check_output(command, **options):
    """subprocess.check_output without a console window."""
    return subprocess.check_output(_command(command), **_no_console(options))


def data_directory(*, browser=False):
    """The per-user data folder, with symlinks resolved so the media checks
    that compare resolved paths agree with it (a symlinked home included)."""
    override = os.environ.get("SPLICE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == "darwin":
        path = home / "Library" / "Application Support" / "Splice"
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Splice"
    else:
        base = os.environ.get("XDG_DATA_HOME") if browser else None
        path = Path(base or home / ".local" / "share") / "splice"
    return path.resolve()


def browser_executable():
    override = os.environ.get("SPLICE_BROWSER")
    if override:
        return shutil.which(override)
    names = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "msedge")
    for name in names:
        candidate = shutil.which(name)
        if candidate:
            return candidate
    if sys.platform == "darwin":
        roots = (Path("/Applications"), Path.home() / "Applications")
        for name in ("Google Chrome", "Chromium", "Microsoft Edge"):
            for root in roots:
                path = root / (name + ".app") / "Contents" / "MacOS" / name
                if path.is_file():
                    return str(path)
    elif sys.platform == "win32":
        roots = [Path(os.environ[key]) for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                 if os.environ.get(key)]
        for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe",
                         "Chromium/Application/chrome.exe"):
            for root in roots:
                path = root / relative
                if path.is_file():
                    return str(path)
    return None


def font_roots():
    if sys.platform == "darwin":
        return (Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts")
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts", local / "Microsoft/Windows/Fonts")
    return (Path("/usr/share/fonts"),)


def installed_font(family):
    roots = font_roots()
    if sys.platform == "win32":
        import winreg
        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
        for hive, root in zip((winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER), roots):
            try:
                with winreg.OpenKey(hive, key) as fonts:
                    for index in range(winreg.QueryInfoKey(fonts)[1]):
                        name, value, kind = winreg.EnumValue(fonts, index)
                        if name.split(" (", 1)[0].casefold() == family.casefold() and kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                            candidate = (root / os.path.expandvars(value)).resolve()
                            if candidate.is_file() and any(candidate.is_relative_to(folder.resolve()) for folder in roots):
                                return name, candidate
            except FileNotFoundError:
                continue
    if sys.platform in ("darwin", "win32"):
        if Path(family).name != family or family in (".", ".."):
            return None
        for root in roots:
            for directory in (root, root / "Supplemental"):
                for name in (family, family.replace(" ", "")):
                    for extension in (".ttf", ".ttc", ".otf"):
                        candidate = (directory / (name + extension)).resolve()
                        if candidate.is_file() and candidate.is_relative_to(root.resolve()):
                            return family, candidate
    return None
