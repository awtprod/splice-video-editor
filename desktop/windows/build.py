"""Assemble the Windows x64 installer using pinned upstream distributions.

``python desktop/windows/build.py OUTPUT`` downloads the pinned runtimes, stages
the application and compiles ``OUTPUT/Splice-Setup-x64.exe`` with NSIS.
``--stage-only`` stops after staging (it runs on any OS) and ``--cache`` keeps the
verified downloads for the next build. Every build writes ``manifest.json``
with the bundled versions, checksums and the standard-library modules Splice
imports, checked against the bundled Python.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from package_desktop import ROOT, source_files  # noqa: E402

RUNTIMES = {
    "electron": dict(
        version="45.5.0", license="MIT (Electron); Chromium and dependencies: see LICENSES.chromium.html",
        url="https://github.com/electron/electron/releases/download/v45.5.0/electron-v45.5.0-win32-x64.zip",
        sha256="20155c8b8aa5b1f575f3757bdf21490e0bac2f19a65a26ce9ba99bcf9f2c69bd",
    ),
    "python": dict(
        version="3.14.10", license="PSF License Version 2 (LICENSE.txt)",
        url="https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip",
        sha256="90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159",
    ),
    "ffmpeg": dict(
        version="9.1 (gyan.dev full_build-shared)", license="GNU GPL version 3 (LICENSE)",
        url="https://github.com/GyanD/codexffmpeg/releases/download/9.1/ffmpeg-9.1-full_build-shared.zip",
        sha256="d010a19b78d5eae62dca8d4c7f195acd56dae8ed6fb85f72821edd9437da2898",
    ),
}
FFMPEG_FILES = ("ffmpeg.exe", "ffprobe.exe")

# Standard-library modules that exist only on POSIX; Splice imports them only
# when os.name != 'nt'.
POSIX_ONLY = frozenset({"fcntl", "grp", "posix", "pty", "pwd", "readline", "resource", "syslog", "termios", "tty"})
# Built into python3XX.dll on Windows rather than shipped as a .pyd or in the zip.
WINDOWS_BUILTINS = frozenset({
    "_abc", "_ast", "_bisect", "_blake2", "_codecs", "_collections", "_contextvars", "_csv", "_datetime",
    "_functools", "_heapq", "_imp", "_io", "_json", "_locale", "_md5", "_opcode", "_operator", "_pickle",
    "_random", "_sha1", "_sha2", "_sha3", "_signal", "_sre", "_stat", "_statistics", "_string", "_struct",
    "_symtable", "_thread", "_tokenize", "_tracemalloc", "_typing", "_warnings", "_weakref", "_winapi",
    "array", "atexit", "binascii", "builtins", "cmath", "errno", "faulthandler", "gc", "itertools",
    "marshal", "math", "mmap", "msvcrt", "nt", "sys", "time", "winreg", "zlib",
})


def fetch(name, cache):
    """The verified archive for ``name``, downloaded unless ``cache`` holds it."""
    spec = RUNTIMES[name]
    archive = cache / spec["url"].rsplit("/", 1)[1]
    if archive.is_file() and _sha256(archive) == spec["sha256"]:
        return archive
    partial = archive.with_name(archive.name + ".part")
    print(f"Downloading {name} {spec['version']}", flush=True)
    with urllib.request.urlopen(spec["url"], timeout=120) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output)
    if _sha256(partial) != spec["sha256"]:
        partial.unlink()
        raise ValueError(f"{name} download checksum did not match")
    partial.replace(archive)
    return archive


def _sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _extract(archive, destination):
    with ZipFile(archive) as package:
        for member in package.namelist():
            target = (destination / member).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError(f"{archive.name} has an unsafe path: {member}")
        package.extractall(destination)


def imported_modules(paths):
    """Top-level module names imported by the given Python files (absolute imports only)."""
    names = set()
    for path in paths:
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names.add(node.module.split(".")[0])
    return names


def _guarded_imports(paths):
    """Modules imported only inside ``try: ... except ImportError`` (optional accelerators) or
    in the fallback branch of a ``sys.version_info`` check."""
    guarded = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
            if isinstance(node, ast.If) and "version_info" in ast.unparse(node.test):
                guarded |= imported_modules_in(node.orelse)  # a fallback for older Pythons
                continue
            if not isinstance(node, ast.Try):
                continue
            catches = {getattr(handler.type, "id", None) for handler in node.handlers} | {
                getattr(element, "id", None) for handler in node.handlers
                if isinstance(handler.type, ast.Tuple) for element in handler.type.elts}
            if catches & {"ImportError", "ModuleNotFoundError", "Exception"}:
                guarded |= imported_modules_in(node.body)
                for handler in node.handlers:  # a fallback import in the handler is optional too
                    guarded |= imported_modules_in(handler.body)
    return guarded


def imported_modules_in(statements):
    names = set()
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names.add(node.module.split(".")[0])
    return names


def stdlib_requirements(editor, files=None):
    """(required stdlib modules, optional third-party modules) for the editor sources in ``editor``
    (all of its Python files unless ``files`` names them)."""
    files = sorted(files if files is not None else editor.rglob("*.py"))
    local = {path.stem for path in editor.glob("*.py")} | {path.name for path in editor.iterdir() if path.is_dir()}
    vendor = editor / "vendor"
    if vendor.is_dir():
        local |= {path.name if path.is_dir() else path.stem for path in vendor.iterdir()}
    names = imported_modules(files) - local
    third_party = sorted(name for name in names if name not in sys.stdlib_module_names)
    unguarded = sorted(set(third_party) - _guarded_imports(files))
    if unguarded:
        raise ValueError("Splice imports modules outside the standard library: " + ", ".join(unguarded))
    return sorted(name for name in names if name in sys.stdlib_module_names), third_party


def embedded_modules(python):
    """Module names the embeddable Python distribution provides (zip, .pyd, Windows built-ins)."""
    archive, = python.glob("python3*.zip")
    with ZipFile(archive) as library:
        names = {member.split("/")[0].removesuffix(".pyc") for member in library.namelist()}
    names |= {path.stem for path in python.glob("*.pyd")}
    return names | WINDOWS_BUILTINS


def stage(output, cache):
    """Build the installer payload in ``output/stage``; return the manifest it wrote."""
    stage_root = output / "stage"
    if stage_root.exists():
        shutil.rmtree(stage_root)
    payload = stage_root / "payload"
    resources = payload / "resources"
    payload.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="windows-runtimes-", dir=output) as temporary:
        scratch = Path(temporary)
        _extract(fetch("electron", cache), payload)
        (payload / "electron.exe").rename(payload / "Splice.exe")
        (resources / "default_app.asar").unlink(missing_ok=True)

        python = resources / "python"
        _extract(fetch("python", cache), python)
        config, = python.glob("python3*._pth")
        with config.open("a", encoding="utf-8") as settings:
            settings.write("\n../splice\n")

        codec = resources / "ffmpeg"
        codec.mkdir()
        _extract(fetch("ffmpeg", cache), scratch / "ffmpeg")
        distribution, = (scratch / "ffmpeg").iterdir()
        binaries = distribution / "bin"
        for path in sorted(binaries.iterdir()):
            if path.name in FFMPEG_FILES or path.suffix.lower() == ".dll":
                shutil.copy2(path, codec / path.name)
        missing = [name for name in FFMPEG_FILES if not (codec / name).is_file()]
        if missing:
            raise ValueError("The FFmpeg archive has no " + ", ".join(missing))
        shutil.copy2(distribution / "LICENSE", codec / "LICENSE.txt")
        shutil.copy2(distribution / "README.txt", codec / "BUILD-CONFIGURATION.txt")
    shutil.copy2(HERE / "Third-party.txt", payload / "Third-party.txt")

    editor = resources / "splice"
    for path in source_files():
        destination = editor / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    entry = resources / "app"
    entry.mkdir()
    for name in ("package.json", "main.cjs"):
        shutil.copy2(HERE / name, entry)

    required, optional = stdlib_requirements(editor)
    provided = embedded_modules(python)
    absent = [name for name in required if name not in provided and name not in POSIX_ONLY]
    if absent:
        raise ValueError("The bundled Python lacks modules Splice imports: " + ", ".join(absent))
    version = json.loads((HERE / "package.json").read_text(encoding="utf-8"))["version"]
    manifest = dict(
        version=version,
        runtimes={name: {key: spec[key] for key in ("version", "license", "url", "sha256")}
                  for name, spec in RUNTIMES.items()},
        ffmpeg_files=sorted(path.name for path in codec.iterdir()),
        python_modules=dict(required=[name for name in required if name not in POSIX_ONLY],
                            posix_only=[name for name in required if name in POSIX_ONLY],
                            optional_third_party=optional),
        files=sum(1 for path in payload.rglob("*") if path.is_file()),
        bytes=sum(path.stat().st_size for path in payload.rglob("*") if path.is_file()),
    )
    text = json.dumps(manifest, indent=2) + "\n"
    for destination in (stage_root, payload, output):  # payload: the installed app carries it too
        (destination / "manifest.json").write_text(text, encoding="utf-8")
    return manifest


def verify_on_windows(output):
    """Run the bundled Python against the bundled FFmpeg with nothing else on PATH."""
    resources = output / "stage" / "payload" / "resources"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    env = {key: value for key, value in os.environ.items() if key.lower() != "path"}
    env["Path"] = str(resources / "ffmpeg")
    python = str(resources / "python" / "python.exe")
    modules = ",".join(manifest["python_modules"]["required"])
    subprocess.run([python, "-I", "-c", f"import importlib\nfor m in '{modules}'.split(','): importlib.import_module(m)"],
                   check=True, timeout=120, env=env, cwd=resources / "splice")
    subprocess.run([python, "-B", "runtime_check.py", "--strict"], check=True, timeout=120, env=env,
                   cwd=resources / "splice")


def build(output, nsis, cache, stage_only=False):
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    manifest = stage(output, cache)
    print(f"Staged {manifest['files']} files ({manifest['bytes'] / 1e6:.0f} MB) in {output / 'stage'}", flush=True)
    if stage_only:
        return
    if sys.platform != "win32":
        raise SystemExit("Compiling the installer needs Windows (the bundled runtime is checked by running it); "
                         "use --stage-only elsewhere.")
    verify_on_windows(output)
    shutil.copy2(HERE / "installer.nsi", output / "stage")
    subprocess.run([nsis, f"/DVERSION={manifest['version']}", "installer.nsi"], cwd=output / "stage", check=True,
                   timeout=1200)
    shutil.move(output / "stage" / "Splice-Setup-x64.exe", output / "Splice-Setup-x64.exe")
    shutil.rmtree(output / "stage")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--nsis", default="makensis", help="Path to the NSIS compiler.")
    parser.add_argument("--cache", type=Path, help="Folder that keeps verified downloads (default: OUTPUT/downloads).")
    parser.add_argument("--stage-only", action="store_true", help="Stage the payload and manifest without NSIS.")
    args = parser.parse_args()
    output = args.output.resolve()
    build(output, args.nsis, (args.cache or output / "downloads").resolve(), args.stage_only)
