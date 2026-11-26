"""The Resolve projects Splice opened, kept privately so a later save can use them as its seed.

A library project's verified snapshot (the copy the reader read) and a ``.drp`` the browser sends
are kept in Splice's own data folder under their SHA-256, never in the library. The newest
:data:`KEEP` are kept (and at most :data:`MAX_BYTES` in all); a kept file is found again by its
kind and hash, so a save after a reload or a recovery uses exactly the bytes that were opened.
"""
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

KEEP = 8
MAX_BYTES = 2 * 1024 * 1024 * 1024
_NAME = re.compile(r'(library|drp)-([0-9a-f]{64})\.(db|drp)\Z')
_SUFFIX = {'library': 'db', 'drp': 'drp'}


def folder():
    from splice_platform import data_directory
    path = data_directory() / 'resolve-originals'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _path(kind, sha256, root):
    if kind not in _SUFFIX or not re.fullmatch(r'[0-9a-f]{64}', str(sha256)):
        raise ValueError('Not a kept original.')
    return Path(root) / f'{kind}-{sha256}.{_SUFFIX[kind]}'


def find(kind, sha256, root=None):
    """The kept file of ``kind`` with ``sha256``, or None (its hash checked again)."""
    path = _path(kind, sha256, root or folder())
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        path.unlink(missing_ok=True)
        return None
    os.utime(path)
    return path


def keep(kind, sha256, source, root=None):
    """Keep a copy of ``source`` (already hashed to ``sha256``); returns its path."""
    root = Path(root or folder())
    target = _path(kind, sha256, root)
    if not target.is_file():
        fd, name = tempfile.mkstemp(prefix='.keep-', dir=root)
        os.close(fd)
        try:
            shutil.copyfile(source, name)
            os.replace(name, target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
    else:
        os.utime(target)
    prune(root)
    return target


def keep_bytes(kind, data, root=None):
    sha256 = hashlib.sha256(data).hexdigest()
    root = Path(root or folder())
    target = _path(kind, sha256, root)
    if not target.is_file():
        fd, name = tempfile.mkstemp(prefix='.keep-', dir=root)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
            os.replace(name, target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
    prune(root)
    return sha256


def prune(root):
    entries = []
    for entry in os.scandir(root):
        if _NAME.match(entry.name) and entry.is_file(follow_symlinks=False):
            info = entry.stat(follow_symlinks=False)
            entries.append((info.st_mtime, info.st_size, entry.path))
    entries.sort(reverse=True)
    total = 0
    for index, (_mtime, size, path) in enumerate(entries):
        total += size
        if index >= KEEP or total > MAX_BYTES:
            Path(path).unlink(missing_ok=True)
