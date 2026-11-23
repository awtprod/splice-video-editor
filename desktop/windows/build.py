"""Assemble the Windows x64 installer using pinned upstream distributions."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from package_desktop import ROOT, source_files

RUNTIMES = {
    "electron": (
        "https://github.com/electron/electron/releases/download/v45.4.1/electron-v45.4.1-win32-x64.zip",
        "2ba6223afa9121f42831fefe92c434334907540165294fc56697449d12e7cfe9",
    ),
    "python": (
        "https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip",
        "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38",
    ),
    "ffmpeg": (
        "https://github.com/GyanD/codexffmpeg/releases/download/9.1/ffmpeg-9.1-essentials_build.zip",
        "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd",
    ),
}


def extract_runtime(stage, name, destination):
    url, expected = RUNTIMES[name]
    archive = stage / (name + ".zip")
    print(f"Downloading {name}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    with archive.open("rb") as downloaded:
        if hashlib.file_digest(downloaded, "sha256").hexdigest() != expected:
            raise ValueError(f"{name} download checksum did not match")
    with ZipFile(archive) as package:
        package.extractall(destination)
    archive.unlink()


def build(output, nsis):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="windows-build-", dir=output) as temporary:
        stage = Path(temporary)
        payload = stage / "payload"
        resources = payload / "resources"
        extract_runtime(stage, "electron", payload)
        (payload / "electron.exe").rename(payload / "Splice.exe")
        (resources / "default_app.asar").unlink(missing_ok=True)
        python = resources / "python"
        extract_runtime(stage, "python", python)
        config, = python.glob("python*._pth")
        with config.open("a", encoding="utf-8") as settings:
            settings.write("\n../splice\n")
        codec = resources / "ffmpeg"
        codec.mkdir()
        extract_runtime(stage, "ffmpeg", stage / "ffmpeg")
        distribution, = (stage / "ffmpeg").iterdir()
        for filename in ("ffmpeg.exe", "ffprobe.exe"):
            (distribution / "bin" / filename).rename(codec / filename)
        shutil.copy2(distribution / "LICENSE", codec)
        shutil.copy2(HERE / "Third-party.txt", codec)
        editor = resources / "splice"
        for path in source_files():
            destination = editor / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, destination)
        entry = resources / "app"
        entry.mkdir()
        for name in ("package.json", "main.cjs"):
            shutil.copy2(HERE / name, entry)
        shutil.copy2(HERE / "installer.nsi", stage)
        subprocess.run([nsis, "installer.nsi"], cwd=stage, check=True, timeout=600)
        shutil.move(stage / "Splice-Setup-x64.exe", output / "Splice-Setup-x64.exe")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--nsis", default="makensis", help="Path to the NSIS compiler.")
    args = parser.parse_args()
    build(args.output.resolve(), args.nsis)
