"""Assemble the Windows x64 installer using pinned upstream distributions.

``python desktop/windows/build.py OUTPUT`` downloads the pinned runtimes, stages
the application, turns off the Electron fuses that would let Splice.exe run as
plain Node.js (``fuses.mjs``; run ``npm ci`` in this folder first) and compiles
``OUTPUT/Splice-Setup-x64.exe`` with the pinned NSIS (``--nsis PATH`` uses
another). ``--stage-only`` stops after staging (it runs on any OS) and ``--cache``
keeps the verified downloads for the next build. Every build writes
``manifest.json`` with the bundled versions, checksums and the standard-library
modules Splice imports, checked against the bundled Python.
``--source-archive`` instead collects the bundled FFmpeg's corresponding source
(as far as it is published) in ``OUTPUT/source``.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
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
# Used to build the installer, not installed. NSIS's own stub is part of the setup EXE,
# so it is pinned like the runtimes rather than taken from whatever is installed.
BUILD_TOOLS = {
    "nsis": dict(
        version="3.11", license="zlib/libpng (NSIS, including the installer stub)",
        url="https://downloads.sourceforge.net/project/nsis/NSIS%203/3.11/nsis-3.11.zip",
        sha256="c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1",
    ),
}
# The source of the bundled FFmpeg, for --source-archive. The 9.1 release tarball is the
# n9.1 tag, which is the commit gyan.dev's README names (FFmpeg signs it: ffmpeg-9.1.tar.xz.asc).
SOURCES = {
    "ffmpeg-source": dict(
        version="9.1", commit="74e4393595cf2b5857f0da925c4e0dd74ea306e5",
        url="https://ffmpeg.org/releases/ffmpeg-9.1.tar.xz",
        signature="https://ffmpeg.org/releases/ffmpeg-9.1.tar.xz.asc",
        sha256="1a4d394ae63d190a526cd052eb4415c156b5c5e2df37efab41a7de0b8fde2433",
    ),
}

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
    """The verified archive for ``name``, downloaded unless ``cache`` holds it. A cached
    copy is re-hashed every time and downloaded again if it does not match."""
    spec = {**RUNTIMES, **BUILD_TOOLS, **SOURCES}[name]
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
    _write_manifest(output, manifest)
    return manifest


def _write_manifest(output, manifest):
    text = json.dumps(manifest, indent=2) + "\n"
    stage_root = output / "stage"
    for destination in (stage_root, stage_root / "payload", output):  # payload: the installed app carries it too
        (destination / "manifest.json").write_text(text, encoding="utf-8")


def harden_electron(executable):
    """Turn off the RunAsNode, NODE_OPTIONS and --inspect fuses of ``executable``; return their states."""
    node = shutil.which("node")
    if not node:
        raise SystemExit("Setting the Electron fuses needs Node.js on PATH.")
    if not (HERE / "node_modules" / "@electron" / "fuses").is_dir():
        raise SystemExit('Run "npm ci" in desktop/windows first: it installs the pinned @electron/fuses.')
    result = subprocess.run([node, str(HERE / "fuses.mjs"), "harden", str(executable)], cwd=HERE,
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise SystemExit("Setting the Electron fuses failed: " + (result.stderr or result.stdout).strip())
    return json.loads(result.stdout.strip().splitlines()[-1])


def nsis_compiler(output, cache):
    """makensis.exe from the pinned, verified NSIS zip, unpacked in ``output``."""
    folder = output / "nsis"
    if folder.exists():
        shutil.rmtree(folder)
    _extract(fetch("nsis", cache), folder)
    compiler, = folder.glob("nsis-*/makensis.exe")
    return compiler


def source_archive(output, cache):
    """Collect what is published of the bundled FFmpeg's corresponding source in ``output/source``:
    the signed FFmpeg release tarball for the commit gyan.dev built, and SOURCES.txt/sources.json
    listing every external library with the version gyan.dev reports. gyan.dev publishes neither the
    libraries' sources nor its build scripts, so those are listed, not collected."""
    folder = output / "source"
    folder.mkdir(parents=True, exist_ok=True)
    spec = SOURCES["ffmpeg-source"]
    tarball = fetch("ffmpeg-source", cache)
    shutil.copy2(tarball, folder / tarball.name)
    with ZipFile(fetch("ffmpeg", cache)) as package:
        readme, = (name for name in package.namelist() if name.count("/") == 1 and name.endswith("/README.txt"))
        text = package.read(readme).decode("utf-8", "replace")
    link = re.search(r"^Source Code:\s*(\S+)", text, re.M)
    if not link or not spec["commit"].startswith(link.group(1).rsplit("/", 1)[-1]):
        raise ValueError("The FFmpeg build names a different source commit than " + spec["commit"])
    _, _, table = text.partition("external libraries' versions:")
    libraries = []
    for line in table.strip().splitlines():
        name, _, version = line.strip().partition(" ")
        if name and version.strip():
            version = version.strip()
            revision = re.search(r"-g([0-9a-f]{7,})$", version)
            libraries.append(dict(name=name, version=version, git_revision=revision.group(1) if revision else None))
    if not libraries:
        raise ValueError("The FFmpeg build's README lists no library versions")
    report = dict(
        ffmpeg=dict(version=spec["version"], commit=spec["commit"], tarball=tarball.name, url=spec["url"],
                    signature=spec["signature"], sha256=spec["sha256"], build=RUNTIMES["ffmpeg"]["url"],
                    build_source_link=link.group(1)),
        libraries=libraries,
        known_gaps=["gyan.dev publishes no source archives for the external libraries and no build scripts; "
                    "only the versions below. Their sources are not collected here.",
                    "Library versions ending in -g<hash> name a git revision of that project; the others are "
                    "release versions."],
    )
    (folder / "sources.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [f"Corresponding source for FFmpeg {spec['version']} in the Splice Windows installer",
             "",
             f"FFmpeg: {tarball.name} (SHA-256 {spec['sha256']})",
             f"  from {spec['url']}, signed by FFmpeg: {spec['signature']}",
             f"  the n{spec['version']} release, commit {spec['commit']}; gyan.dev's build names {link.group(1)}",
             "",
             "KNOWN GAP: gyan.dev publishes no source for the libraries below and no build scripts, only",
             "these versions. They are not collected here.",
             ""]
    lines += [f"  {item['name']:<20} {item['version']}" for item in libraries]
    (folder / "SOURCES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


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
    manifest["electron_fuses"] = harden_electron(output / "stage" / "payload" / "Splice.exe")
    _write_manifest(output, manifest)
    print(f"Staged {manifest['files']} files ({manifest['bytes'] / 1e6:.0f} MB) in {output / 'stage'}", flush=True)
    if stage_only:
        return
    if sys.platform != "win32":
        raise SystemExit("Compiling the installer needs Windows (the bundled runtime is checked by running it); "
                         "use --stage-only elsewhere.")
    if nsis == "pinned":
        nsis = nsis_compiler(output, cache)
        manifest["nsis"] = {key: BUILD_TOOLS["nsis"][key] for key in ("version", "url", "sha256")}
        _write_manifest(output, manifest)
    verify_on_windows(output)
    shutil.copy2(HERE / "installer.nsi", output / "stage")
    subprocess.run([nsis, f"/DVERSION={manifest['version']}", "installer.nsi"], cwd=output / "stage", check=True,
                   timeout=1200)
    shutil.move(output / "stage" / "Splice-Setup-x64.exe", output / "Splice-Setup-x64.exe")
    shutil.rmtree(output / "stage")
    if (output / "nsis").exists():
        shutil.rmtree(output / "nsis")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--nsis", default="pinned",
                        help='Path to the NSIS compiler; "pinned" (the default) downloads and verifies NSIS 3.11.')
    parser.add_argument("--cache", type=Path, help="Folder that keeps verified downloads (default: OUTPUT/downloads).")
    parser.add_argument("--stage-only", action="store_true", help="Stage the payload and manifest without NSIS.")
    parser.add_argument("--source-archive", action="store_true",
                        help="Only collect the bundled FFmpeg's corresponding source in OUTPUT/source.")
    args = parser.parse_args()
    output = args.output.resolve()
    cache = (args.cache or output / "downloads").resolve()
    if args.source_archive:
        output.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        print(json.dumps(source_archive(output, cache)["ffmpeg"], indent=2))
    else:
        build(output, args.nsis, cache, args.stage_only)
