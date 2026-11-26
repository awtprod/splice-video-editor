"""Build portable Windows and macOS desktop archives from the checkout."""
import argparse
from pathlib import Path
import plistlib
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent


def source_files():
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    return sorted({
        Path(name) for name in tracked if name and (
            name.startswith(("assets/", "vendor/", "interchange/")) or name == "README.md" or
            (name.startswith("resolve/") and name.endswith(".py")) or
            ("/" not in name and Path(name).suffix in (".py", ".js", ".css", ".html")
             and name != Path(__file__).name)
        )
    })


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    sources = source_files()
    with ZipFile(output / "Splice-Windows.zip", "w", ZIP_DEFLATED) as archive:
        for path in sources:
            archive.write(ROOT / path, "Splice/" + path.as_posix())
        archive.writestr("Splice/Splice.cmd", (ROOT / "desktop/Splice.cmd").read_text().replace("\n", "\r\n"))
    with ZipFile(output / "Splice-macOS.zip", "w", ZIP_DEFLATED) as archive:
        bundle = "Splice.app/Contents"
        for path in sources:
            archive.write(ROOT / path, bundle + "/Resources/" + path.as_posix())
        executable = ZipInfo(bundle + "/MacOS/Splice")
        executable.create_system = 3
        executable.external_attr = 0o100755 << 16
        archive.writestr(executable, (ROOT / "desktop/Splice").read_bytes())
        archive.writestr(bundle + "/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "local.splice.editor",
            "CFBundleName": "Splice",
            "CFBundleExecutable": "Splice",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "NSHighResolutionCapable": True,
        }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)
