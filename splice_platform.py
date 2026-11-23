"""Installation paths for the desktop editor."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


HIDE_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def data_directory(*, browser=False):
    override = os.environ.get("SPLICE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Splice"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Splice"
    base = os.environ.get("XDG_DATA_HOME") if browser else None
    return Path(base or home / ".local" / "share") / "splice"


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
