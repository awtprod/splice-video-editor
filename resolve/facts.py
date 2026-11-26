"""Measure a media file on this computer the way Resolve's media-pool rows need it.

The facts have the shape ai-agent-resolve's facts script writes on the edit PC: the path as the
Resolve computer sees it (after the user's path mapping), the size, the modification time on the
wall clock (this computer's time zone), the creation time in UTC where the file system keeps one
(``statx``'s birth time on Linux, ``st_birthtime`` elsewhere) and ``ffprobe -show_format
-show_streams``. Nothing is written beside the file.
"""
import ctypes
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from media_exec import run_bounded

from .mediapool import Facts, MediaPoolError

PROBE_SECONDS = 25
MAX_PROBE_OUTPUT = 4 * 1024 * 1024
MAX_MAPPINGS = 16
_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def check_mappings(mappings):
    """``[(from prefix, to prefix)]`` from the request: at most 16 pairs of non-empty text."""
    if mappings in (None, ''):
        return []
    if not isinstance(mappings, list) or len(mappings) > MAX_MAPPINGS:
        raise ValueError('Path mapping must be a list of at most 16 pairs.')
    out = []
    for pair in mappings:
        if not isinstance(pair, dict):
            raise ValueError('Each path mapping needs a “from” and a “to” folder.')
        source, target = pair.get('from'), pair.get('to')
        if not isinstance(source, str) or not isinstance(target, str) or not source.strip() or not target.strip() \
                or len(source) > 1024 or len(target) > 1024 or '\x00' in source + target:
            raise ValueError('Each path mapping needs a “from” and a “to” folder.')
        out.append((source.strip(), target.strip()))
    return out


def map_path(local, mappings):
    """The path Resolve sees for ``local``: the first mapping whose folder holds it (compared
    folder by folder), with the rest joined in the target's style; ``local`` itself otherwise."""
    local = str(local)
    for source, target in mappings:
        base = source.rstrip('/\\')
        if local == base or local.startswith(base + '/') or local.startswith(base + '\\'):
            rest = [p for p in local[len(base):].replace('\\', '/').split('/') if p]
            windows = '\\' in target or (len(target) >= 2 and target[1] == ':') or target.startswith('\\\\')
            root = target.rstrip('/\\')
            if windows:
                return str(PureWindowsPath(root + '\\', *rest)) if len(root) == 2 else '\\'.join([root, *rest])
            return str(PurePosixPath(root or '/', *rest))
    return local


def _birth_ns(path):
    """The file's creation time in ns since 1970 (UTC), or None where the system keeps none."""
    info = os.stat(path)
    if getattr(info, 'st_birthtime_ns', None):
        return info.st_birthtime_ns
    if getattr(info, 'st_birthtime', None):
        return int(info.st_birthtime * 1_000_000_000)
    if not sys.platform.startswith('linux'):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        statx = libc.statx
    except (OSError, AttributeError):
        return None
    buffer = ctypes.create_string_buffer(256)
    statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_char_p]
    statx.restype = ctypes.c_int
    if statx(-100, os.fsencode(path), 0, 0x800, buffer) != 0:
        return None
    mask = int.from_bytes(buffer.raw[0:4], sys.byteorder)
    if not mask & 0x800:
        return None
    seconds = int.from_bytes(buffer.raw[80:88], sys.byteorder, signed=True)
    nanos = int.from_bytes(buffer.raw[88:92], sys.byteorder)
    return seconds * 1_000_000_000 + nanos if seconds > 0 else None


def _ctime_text(seconds):
    value = time.localtime(seconds)
    return (f'{_DAYS[value.tm_wday]} {_MONTHS[value.tm_mon - 1]} {value.tm_mday:2d} '
            f'{value.tm_hour:02d}:{value.tm_min:02d}:{value.tm_sec:02d} {value.tm_year:04d}')


def _utc_text(ns):
    whole = datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc)
    return whole.strftime('%Y-%m-%dT%H:%M:%S') + f'.{ns % 1_000_000_000:09d}'[:8] + 'Z'


def probe(local):
    result = run_bounded(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_format', '-show_streams',
                          '-of', 'json', 'file:' + str(local)], timeout=PROBE_SECONDS, max_output=MAX_PROBE_OUTPUT)
    if result.returncode:
        raise MediaPoolError('cannot_derive', f'FFprobe cannot read {Path(local).name}')
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise MediaPoolError('cannot_derive', f'FFprobe cannot read {Path(local).name}')
    return data


def measure(local, resolve_path):
    """Checked :class:`Facts` of the file at ``local`` (on this computer), as Resolve will see it
    at ``resolve_path``; MediaPoolError when it cannot be measured."""
    local = Path(local)
    before = os.stat(local)
    if not stat.S_ISREG(before.st_mode):
        raise MediaPoolError('bad_facts', f'{local.name} is not a regular file')
    data = probe(local)
    after = os.stat(local)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise MediaPoolError('bad_facts', f'{local.name} changed while it was measured')
    birth = _birth_ns(local)
    raw = {'path': resolve_path, 'size': after.st_size, 'mtime_local': _ctime_text(after.st_mtime),
           'mtime_utc': _utc_text(after.st_mtime_ns), 'ffprobe': data}
    if birth is not None:
        raw['ctime_utc'] = _utc_text(birth)
    return Facts(raw)
