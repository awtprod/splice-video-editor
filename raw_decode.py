"""Camera RAW for Splice: metadata, decoder discovery, preview proxies and export decodes.

Python standard library plus FFmpeg. No vendor code or binaries live in the repository.

Built in (works out of the box):
  - CinemaDNG frames and numbered sequences (``A001_000001.dng`` ...) with a sidecar WAV:
    :mod:`raw_bayer` reads the TIFF/DNG tags and develops uncompressed 8-16-bit Bayer data;
    lossless-JPEG DNG is unpacked by FFmpeg's tiff decoder and developed the same way.
  - Apple ProRes RAW (``aprn``/``aprh`` in QuickTime) through FFmpeg 8's ``prores_raw`` decoder.

Metadata without a decoder (sound, timecode, reel, frame rate, size and duration from the
container), picture through a decoder the user installs:
  - Blackmagic RAW ``.braw``, Canon Cinema RAW Light ``.crm``, Nikon N-RAW ``.nev`` (QuickTime/ISO
    BMFF), RED ``.r3d`` (RED1/RED2 header and RDVO frame index, ``.RMD`` sidecar), ARRIRAW
    ``.ari``/``.arx`` frames, Sony X-OCN and ARRIRAW MXF, still RAW (``.cr3`` ``.nef`` ...).
  - Decoders are found from File > RAW Decoders (saved in Splice's user data folder), the
    environment (``SPLICE_BRAW_SDK``, ``SPLICE_REDLINE``, ``SPLICE_ARRI_CMD``, ``SPLICE_LIBRAW``,
    ``SPLICE_CRM_CMD``, ``SPLICE_NRAW_CMD``, ``SPLICE_XOCN_CMD`` or ``SPLICE_RAW_DECODERS`` as a
    JSON object), PATH and the vendors' standard install folders. ``REDline``, ``art-cmd`` and
    ``dcraw_emu`` are driven with their own options; any other program must speak the Splice RAW
    decoder contract::

        DECODER --input FILE --pix-fmt rgb48le --width W --height H --start-frame N --frames N
                --decode-using camera|clip --iso ISO --exposure EV --kelvin K --tint T
                --colorspace NAME --gamma NAME --highlight-recovery 0|1
                --quality full|half|quarter|eighth --output -

    and write exactly ``--frames`` frames of W x H 16-bit little-endian RGB to stdout.

Nothing runs through a shell: decoders get an argv list, and saved paths must be absolute,
existing, executable files without shell metacharacters.
"""
from __future__ import annotations

from array import array
import ctypes
import ctypes.util
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

import raw_bayer
from raw_bayer import RawError
import splice_platform

# Extensions that are always Camera RAW formats (QuickTime .mov and .mxf are probed by codec tag).
RAW_EXTENSIONS = frozenset({
    '.dng', '.braw', '.r3d', '.ari', '.arx', '.crm', '.nev',
    '.cr3', '.cr2', '.nef', '.arw', '.raf', '.orf', '.rw2',
})
STILL_RAW_EXTENSIONS = frozenset({'.cr3', '.cr2', '.nef', '.arw', '.raf', '.orf', '.rw2'})

PRORES_RAW_TAGS = frozenset({'aprn', 'aprh'})
BRAW_CODEC_TAGS = frozenset({'brhq', 'brq0', 'brq1', 'brq2', 'brq3', 'brq4', 'brq5', 'br51', 'braw'})
XOCN_MARKERS = ('x-ocn', 'xocn', 'sony raw', 'f65raw', 'f55raw')
ARRI_MXF_MARKERS = ('arriraw', 'arri raw', 'arri_raw')

VALID_DECODE_USING = frozenset({'camera', 'clip'})
VALID_COLOR_SPACES = frozenset({
    'rec709', 'linear', 'rec2020', 'bmd_wide_gamut_gen5',
    'red_wide_gamut', 'arri_wide_gamut_3', 'arri_wide_gamut_4',
})
VALID_GAMMAS = frozenset({
    'rec709', 'linear', 'cinemadng_log', 'bmd_film_gen5',
    'bmd_extended_video', 'log3g10', 'arri_logc3', 'arri_logc4',
})
VALID_DEBAYER = frozenset({'full', 'half', 'quarter', 'eighth'})
DEBAYER_SCALES = {'full': 1, 'half': 2, 'quarter': 4, 'eighth': 8}

RAW_BACKEND_SPECS = (
    {
        'id': 'prores_raw',
        'label': 'Apple ProRes RAW',
        'tier': 1,
        'extensions': ['.mov'],
        'configurable': False,
        'envVar': None,
        'cliNames': (),
        'libNames': (),
        'standardPaths': (),
        'installHint': 'Built in: FFmpeg 8 or later decodes it (prores_raw).',
    },
    {
        'id': 'cinemadng',
        'label': 'CinemaDNG',
        'tier': 1,
        'extensions': ['.dng'],
        'configurable': False,
        'envVar': None,
        'cliNames': (),
        'libNames': (),
        'standardPaths': (),
        'installHint': 'Built in: uncompressed 8-16-bit Bayer frames and sequences with a sidecar WAV; lossless-JPEG DNG through FFmpeg.',
    },
    {
        'id': 'libraw',
        'label': 'LibRaw / dcraw_emu (Compressed DNG & Still RAW)',
        'tier': 1,
        'extensions': ['.dng', '.cr3', '.cr2', '.nef', '.arw', '.raf', '.orf', '.rw2'],
        'configurable': True,
        'envVar': 'SPLICE_LIBRAW',
        'cliNames': ('dcraw_emu',),
        'libNames': ('raw', 'raw_r'),
        'standardPaths': (
            '/usr/bin/dcraw_emu', '/usr/local/bin/dcraw_emu', '/opt/homebrew/bin/dcraw_emu',
            '/usr/lib/x86_64-linux-gnu/libraw.so', '/usr/lib/libraw.so', '/opt/homebrew/lib/libraw.dylib',
        ),
        'installHint': 'Install LibRaw\'s dcraw_emu (apt install libraw-bin, brew install libraw) and set its path here.',
    },
    {
        'id': 'canon_crm',
        'label': 'Canon Cinema RAW Light',
        'tier': 1,
        'extensions': ['.crm'],
        'configurable': True,
        'envVar': 'SPLICE_CRM_CMD',
        'cliNames': ('crm-decode', 'canon-raw-cli'),
        'libNames': (),
        'standardPaths': (),
        'installHint': 'Canon publishes Cinema RAW Development as a desktop app, not a command-line decoder. Point '
                       'Splice at a command-line bridge that speaks the Splice RAW decoder contract, or attach a proxy.',
    },
    {
        'id': 'braw',
        'label': 'Blackmagic RAW (BRAW)',
        'tier': 2,
        'extensions': ['.braw'],
        'configurable': True,
        'envVar': 'SPLICE_BRAW_SDK',
        'cliNames': ('braw-decode',),
        'libNames': ('BlackmagicRawAPI',),
        'standardPaths': (
            '/usr/lib/blackmagic/BlackmagicRAWSDK/Linux/Libraries/libBlackmagicRawAPI.so',
            '/usr/local/lib/libBlackmagicRawAPI.so',
            '/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries/BlackmagicRawAPI.framework/BlackmagicRawAPI',
            '/Library/Application Support/Blackmagic Design/Blackmagic RAW/BlackmagicRawAPI.framework/BlackmagicRawAPI',
            r'C:\Program Files\Blackmagic Design\Blackmagic RAW\Blackmagic RAW SDK\Win\Libraries\BlackmagicRawAPI.dll',
        ),
        'installHint': ('Download the free Blackmagic RAW SDK (blackmagicdesign.com) and a command-line bridge built on it '
                        '(braw-decode, or any program speaking the Splice RAW decoder contract); set the bridge\'s path here.'),
    },
    {
        'id': 'r3d',
        'label': 'RED RAW (R3D)',
        'tier': 2,
        'extensions': ['.r3d'],
        'configurable': True,
        'envVar': 'SPLICE_REDLINE',
        'cliNames': ('REDline', 'redline'),
        'libNames': ('R3DSDK',),
        'standardPaths': (
            '/usr/local/bin/REDline', '/opt/red/bin/REDline',
            '/Applications/REDCINE-X PRO/REDCINE-X PRO.app/Contents/MacOS/REDline',
            r'C:\Program Files\REDCINE-X PRO 64-bit\REDline.exe',
        ),
        'installHint': 'Install the free REDCINE-X PRO (red.com), which includes the REDline command-line tool, and set REDline\'s path here.',
    },
    {
        'id': 'arriraw',
        'label': 'ARRIRAW / HDE',
        'tier': 2,
        'extensions': ['.ari', '.arx', '.mxf'],
        'configurable': True,
        'envVar': 'SPLICE_ARRI_CMD',
        'cliNames': ('art-cmd', 'arri-raw-decode'),
        'libNames': ('ARRIRAW_SDK',),
        'standardPaths': (
            '/usr/local/bin/art-cmd', '/opt/arri/bin/art-cmd',
            '/Applications/ARRI Reference Tool.app/Contents/MacOS/art-cmd',
            r'C:\Program Files\ARRI\ARRI Reference Tool\art-cmd.exe',
        ),
        'installHint': 'Install the free ARRI Reference Tool (arri.com), which includes art-cmd, and set art-cmd\'s path here.',
    },
    {
        'id': 'nraw',
        'label': 'Nikon N-RAW',
        'tier': 2,
        'extensions': ['.nev'],
        'configurable': True,
        'envVar': 'SPLICE_NRAW_CMD',
        'cliNames': ('nraw-decode', 'ticoraw-decode'),
        'libNames': (),
        'standardPaths': (),
        'installHint': 'Nikon publishes no command-line N-RAW decoder: set a bridge that speaks the Splice RAW decoder contract, or attach a proxy.',
    },
    {
        'id': 'xocn',
        'label': 'Sony X-OCN',
        'tier': 2,
        'extensions': ['.mxf'],
        'configurable': True,
        'envVar': 'SPLICE_XOCN_CMD',
        'cliNames': ('xocn-decode', 'sony-raw-cli'),
        'libNames': (),
        'standardPaths': (),
        'installHint': 'Sony RAW Viewer is a desktop app: set a command-line bridge that speaks the Splice RAW decoder contract, or attach a proxy.',
    },
)

RAW_BACKEND_BY_ID = {spec['id']: spec for spec in RAW_BACKEND_SPECS}

FORMAT_LABELS = {
    'prores_raw': 'Apple ProRes RAW',
    'cinemadng': 'CinemaDNG',
    'still_raw': 'Camera RAW Still',
    'canon_crm': 'Canon Cinema RAW Light',
    'braw': 'Blackmagic RAW',
    'r3d': 'REDCODE RAW',
    'arriraw': 'ARRIRAW',
    'nraw': 'Nikon N-RAW',
    'xocn': 'Sony X-OCN',
}

FORMAT_DEFAULTS = {
    'cinemadng': {'colorSpace': 'rec709', 'gamma': 'rec709'},
    'prores_raw': {'colorSpace': 'rec709', 'gamma': 'rec709'},
    'still_raw': {'colorSpace': 'rec709', 'gamma': 'rec709'},
    'canon_crm': {'colorSpace': 'rec709', 'gamma': 'rec709'},
    'braw': {'colorSpace': 'bmd_wide_gamut_gen5', 'gamma': 'bmd_film_gen5'},
    'r3d': {'colorSpace': 'red_wide_gamut', 'gamma': 'log3g10'},
    'arriraw': {'colorSpace': 'arri_wide_gamut_4', 'gamma': 'arri_logc4'},
    'nraw': {'colorSpace': 'rec709', 'gamma': 'rec709'},
    'xocn': {'colorSpace': 'rec709', 'gamma': 'rec709'},
}

# Decoders run from an argv list, never a shell; these are refused anyway so a pasted command line
# fails loudly. Parentheses are allowed: Windows installs under "C:\Program Files (x86)\...".
_SHELL_META = re.compile(r'[;&|$`<>\{\}\[\]\*\?~\r\n\x00]')
# A RAW decoder is a vendor tool or a bridge program, never a general interpreter or shell (which
# would run anything) and never a kernel pseudo-file such as /proc/self/exe.
_INTERPRETER = re.compile(
    r'^(?:python[\d.]*w?|pypy[\d.]*|perl[\d.]*|ruby[\d.]*|node(?:js)?|deno|bun|php[\d.]*|lua[\d.]*|luajit|'
    r'tclsh[\d.]*|wish[\d.]*|java|javaw|osascript|ba?sh|dash|zsh|ksh|mksh|csh|tcsh|fish|sh|busybox|env|'
    r'cmd|powershell|pwsh|wscript|cscript|mshta|rundll32|gawk|mawk|awk|sed)(?:\.exe)?$', re.IGNORECASE)
_PSEUDO_ROOTS = ('/proc/', '/sys/', '/dev/')
_LIB_SUFFIX = re.compile(r'\.(?:so(?:\.\d+)*|dylib|dll)$', re.IGNORECASE)
_TIMECODE_RE = re.compile(r'(\d{2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})')
# DNG compressions FFmpeg's tiff decoder unpacks to a linearized Bayer plane (7 = lossless JPEG).
FFMPEG_DNG_COMPRESSIONS = frozenset({7})
_config_lock = threading.Lock()
_probe_lock = threading.Lock()
_container_cache: dict = {}


# ---------------------------------------------------------------------------
# User Decoder Config & Security Validation
# ---------------------------------------------------------------------------

def config_path() -> Path:
    return splice_platform.data_directory() / 'raw-decoders.json'


def validate_decoder_path(decoder_id: str, raw_path) -> str | None:
    """Strictly validate a user-configured decoder CLI or shared library path.

    Rejects unknown decoder IDs, relative paths, ``..`` traversal, shell metacharacters,
    directories, non-existent files, and non-executable CLI paths. Returns ``None`` to clear.
    """
    if decoder_id not in RAW_BACKEND_BY_ID or not RAW_BACKEND_BY_ID[decoder_id]['configurable']:
        raise ValueError(f'Unknown or built-in RAW decoder: {decoder_id!r}')
    if raw_path is None or raw_path == '':
        return None
    if not isinstance(raw_path, str):
        raise ValueError('Decoder path must be a string')
    value = raw_path.strip()
    if not value:
        return None
    if len(value) > 4096:
        raise ValueError('Decoder path is too long')
    if _SHELL_META.search(value):
        raise ValueError('Decoder path contains disallowed characters')
    if not os.path.isabs(value):
        raise ValueError('Decoder path must be an absolute path')
    # Reject explicit traversal segments before resolving symlinks
    if '..' in Path(value).parts:
        raise ValueError('Decoder path cannot contain parent directory traversal (..)')
    resolved = Path(value).resolve(strict=False)
    if _not_a_decoder(value, resolved):
        raise ValueError(f'{Path(value).name} is a system program or interpreter, not a RAW decoder: {value}')
    if not resolved.is_file():
        raise ValueError(f'Decoder path does not point to a regular file: {value}')
    is_lib = bool(_LIB_SUFFIX.search(resolved.name))
    if is_lib:
        if not os.access(resolved, os.R_OK):
            raise ValueError(f'Decoder library is not readable: {value}')
    else:
        if os.name != 'nt' and not os.access(resolved, os.X_OK):
            raise ValueError(f'Decoder executable does not have execute permission: {value}')
    return str(resolved)


def _not_a_decoder(value: str, resolved: Path) -> bool:
    """True for kernel pseudo-files and for general interpreters or shells (by the resolved name)."""
    for candidate in (str(value), str(resolved)):
        if any(candidate.startswith(root) for root in _PSEUDO_ROOTS):
            return True
    return bool(_INTERPRETER.match(resolved.name) or _INTERPRETER.match(Path(value).name))


def load_decoder_config() -> dict[str, str]:
    """Load saved custom decoder paths from user data directory."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get('decoders') if isinstance(data.get('decoders'), dict) else data
    out = {}
    for key, val in raw.items():
        if key in RAW_BACKEND_BY_ID and isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


def save_decoder_config(updates: dict) -> dict[str, str]:
    """Validate and atomically persist custom decoder paths."""
    if not isinstance(updates, dict):
        raise ValueError('Expected a JSON object of decoder paths')
    with _config_lock:
        current = load_decoder_config()
        for key, value in updates.items():
            cleaned = validate_decoder_path(str(key), value)
            if cleaned is None:
                current.pop(str(key), None)
            else:
                current[str(key)] = cleaned
        target = config_path()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.raw-decoders-', suffix='.json')
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as sink:
                json.dump({'version': 1, 'decoders': current}, sink, indent=2, sort_keys=True)
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return dict(current)


# ---------------------------------------------------------------------------
# Auto-Detection of Built-in & Tier 2 Backends
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def ffmpeg_has_prores_raw() -> tuple[bool, str | None]:
    """Check whether the installed FFmpeg includes the native ``prores_raw`` decoder."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return False, None
    try:
        dec = subprocess.run([ffmpeg, '-hide_banner', '-decoders'], capture_output=True, text=True, timeout=8,
                             stdin=subprocess.DEVNULL)
        has_dec = 'prores_raw' in dec.stdout
        ver_out = subprocess.run([ffmpeg, '-version'], capture_output=True, text=True, timeout=8,
                                 stdin=subprocess.DEVNULL).stdout.splitlines()
        match = re.match(r'ffmpeg version (\S+)', ver_out[0]) if ver_out else None
        version = f'FFmpeg {match.group(1)}' if match else 'FFmpeg'
        return has_dec, version
    except (OSError, subprocess.SubprocessError):
        return False, None


def _env_decoders() -> dict[str, str]:
    out = {}
    blob = os.environ.get('SPLICE_RAW_DECODERS')
    if blob:
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k in RAW_BACKEND_BY_ID and isinstance(v, str) and v.strip():
                        out[k] = v.strip()
        except json.JSONDecodeError:
            pass
    for spec in RAW_BACKEND_SPECS:
        env_var = spec.get('envVar')
        if env_var and os.environ.get(env_var):
            out[spec['id']] = os.environ[env_var].strip()
    return out


def _is_usable_candidate(candidate: str) -> tuple[bool, str | None]:
    if not candidate or _SHELL_META.search(candidate):
        return False, None
    if os.path.isabs(candidate):
        if '..' in Path(candidate).parts:
            return False, None
        p = Path(candidate).resolve(strict=False)
        if _not_a_decoder(candidate, p) or not p.is_file():
            return False, None
        if _LIB_SUFFIX.search(p.name) or '.framework/' in str(p):
            return os.access(p, os.R_OK), 'sdk'
        if os.name == 'nt' or os.access(p, os.X_OK):
            return True, 'cli'
        return False, None
    found = shutil.which(candidate)
    if found and not _not_a_decoder(found, Path(found).resolve(strict=False)):
        return True, 'cli'
    return False, None


@lru_cache(maxsize=32)
def _find_library(name: str) -> str | None:
    """``ctypes.util.find_library`` runs ldconfig / gcc each call: look each SDK name up once per process."""
    return ctypes.util.find_library(name)


def _resolve_backend(backend_id: str, cfg: dict | None = None, env_map: dict | None = None) -> dict:
    """Where this machine's decoder for ``backend_id`` is: built in, FFmpeg, or an executable
    found from the saved path, the environment, PATH or the vendor's standard install folders.

    Splice drives decoders as command-line programs. A vendor SDK *library* found on its own is
    reported (``sdkPath``) but is not "available": it needs a command-line bridge such as
    ``braw-decode`` or any program speaking the Splice RAW decoder contract."""
    spec = RAW_BACKEND_BY_ID[backend_id]
    cfg = load_decoder_config() if cfg is None else cfg
    env_map = _env_decoders() if env_map is None else env_map
    configured = cfg.get(backend_id)
    base = {k: spec[k] for k in ('id', 'label', 'tier', 'extensions', 'configurable', 'envVar', 'installHint')}
    if backend_id == 'cinemadng':
        return {**base, 'available': True, 'activeBackend': 'builtin', 'path': None,
                'version': 'Built-in (uncompressed 8-16-bit Bayer; lossless-JPEG DNG via FFmpeg)',
                'configuredPath': None, 'sdkPath': None, 'guidance': None}
    if backend_id == 'prores_raw':
        has_dec, version = ffmpeg_has_prores_raw()
        return {**base, 'available': has_dec, 'activeBackend': 'ffmpeg' if has_dec else None,
                'path': shutil.which('ffmpeg') if has_dec else None, 'version': version if has_dec else None,
                'configuredPath': None, 'sdkPath': None,
                'guidance': None if has_dec else 'Install FFmpeg 8 or later (its prores_raw decoder).'}
    candidates = [c for c in (configured, env_map.get(backend_id)) if c]
    candidates += [found for found in (shutil.which(cli) for cli in spec['cliNames']) if found]
    candidates += list(spec['standardPaths'])
    cli_path = sdk_path = None
    for candidate in candidates:
        ok, kind = _is_usable_candidate(candidate)
        if not ok:
            continue
        resolved = str(Path(candidate).resolve()) if os.path.isabs(candidate) else shutil.which(candidate)
        if kind == 'cli' and cli_path is None:
            cli_path = resolved
        elif kind == 'sdk' and sdk_path is None:
            sdk_path = resolved
    if sdk_path is None and spec['libNames']:
        sdk_path = next((found for found in map(_find_library, spec['libNames']) if found), None)
    guidance = None
    if not cli_path:
        guidance = (f'{Path(sdk_path).name} is installed, but Splice decodes through a command-line bridge. '
                    f'{spec["installHint"]}') if sdk_path else spec['installHint']
    return {**base, 'available': bool(cli_path), 'activeBackend': 'cli' if cli_path else None, 'path': cli_path,
            'version': Path(cli_path).name if cli_path else None, 'configuredPath': configured,
            'sdkPath': sdk_path, 'guidance': guidance}


def decoder_status() -> dict:
    """Return live status of all Tier 1 and Tier 2 Camera RAW backends."""
    cfg = load_decoder_config()
    env_map = _env_decoders()
    decoders = [_resolve_backend(spec['id'], cfg, env_map) for spec in RAW_BACKEND_SPECS]
    return {
        'decoders': decoders,
        'configPath': str(config_path()),
    }


def decoder_cache_salt(fmt: str) -> str:
    """Short salt reflecting the active decoder for ``fmt`` so installing one invalidates slate proxies."""
    backend_id = 'libraw' if fmt == 'still_raw' else fmt
    if backend_id not in RAW_BACKEND_BY_ID:
        return 'none'
    info = _resolve_backend(backend_id)
    if not info['available']:
        return 'missing'
    return f"{info['activeBackend']}:{info['path'] or 'builtin'}"


# ---------------------------------------------------------------------------
# RAW Settings Normalization & Validation
# ---------------------------------------------------------------------------

def normalize_raw_settings(settings: dict | None, fmt: str | None = None, defaults: dict | None = None) -> dict:
    """Validate and normalize Camera RAW decode parameters."""
    fmt = fmt or 'cinemadng'
    fmt_def = FORMAT_DEFAULTS.get(fmt, FORMAT_DEFAULTS['cinemadng'])
    base = {
        'decodeUsing': 'camera',
        'iso': 800,
        'exposure': 0.0,
        'kelvin': 5600,
        'tint': 0.0,
        'colorSpace': fmt_def['colorSpace'],
        'gamma': fmt_def['gamma'],
        'highlightRecovery': False,
        'debayerQuality': 'half',
    }
    if isinstance(defaults, dict):
        for k in base:
            if k in defaults and defaults[k] is not None:
                base[k] = defaults[k]
    if not settings:
        return base
    if not isinstance(settings, dict):
        raise ValueError('RAW settings must be an object')
    out = dict(base)
    if 'decodeUsing' in settings and settings['decodeUsing'] is not None:
        val = str(settings['decodeUsing'])
        if val not in VALID_DECODE_USING:
            raise ValueError(f'Invalid RAW decodeUsing: {val!r}')
        out['decodeUsing'] = val
    if 'iso' in settings and settings['iso'] is not None:
        val = settings['iso']
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 50 <= val <= 102400:
            raise ValueError('RAW ISO must be between 50 and 102400')
        out['iso'] = int(round(val))
    if 'exposure' in settings and settings['exposure'] is not None:
        val = settings['exposure']
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not -4.0 <= val <= 4.0:
            raise ValueError('RAW exposure must be between -4.0 and +4.0 EV')
        out['exposure'] = round(float(val), 4)
    if 'kelvin' in settings and settings['kelvin'] is not None:
        val = settings['kelvin']
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 2000 <= val <= 15000:
            raise ValueError('RAW white balance Kelvin must be between 2000 and 15000')
        out['kelvin'] = int(round(val))
    if 'tint' in settings and settings['tint'] is not None:
        val = settings['tint']
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not -100 <= val <= 100:
            raise ValueError('RAW tint must be between -100 and +100')
        out['tint'] = round(float(val), 2)
    if 'colorSpace' in settings and settings['colorSpace'] is not None:
        val = str(settings['colorSpace'])
        if val not in VALID_COLOR_SPACES:
            raise ValueError(f'Unsupported RAW color space: {val!r}')
        out['colorSpace'] = val
    if 'gamma' in settings and settings['gamma'] is not None:
        val = str(settings['gamma'])
        if val not in VALID_GAMMAS:
            raise ValueError(f'Unsupported RAW gamma: {val!r}')
        out['gamma'] = val
    if 'highlightRecovery' in settings and settings['highlightRecovery'] is not None:
        if not isinstance(settings['highlightRecovery'], bool):
            raise ValueError('RAW highlightRecovery must be a boolean')
        out['highlightRecovery'] = settings['highlightRecovery']
    dq = settings.get('debayerQuality') if settings.get('debayerQuality') is not None else settings.get('debayer')
    if dq is not None:
        val = str(dq)
        if val not in VALID_DEBAYER:
            raise ValueError(f'Unsupported RAW debayer quality: {val!r}')
        out['debayerQuality'] = val
    return out


def raw_settings_key(settings: dict) -> str:
    """Canonical hash key for caching RAW proxies by decode settings."""
    payload = json.dumps(settings, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Native Container Metadata Parsers (QuickTime/ISO BMFF, R3D, ARRIRAW, MXF)
# ---------------------------------------------------------------------------

def _read_qt_boxes(buf: bytes, start: int, end: int, depth: int = 0):
    """Yield ``(fourcc_str, payload_start, payload_end)`` within ``buf[start:end]``."""
    if depth > 8:
        return
    pos = start
    limit = min(end, len(buf))
    count = 0
    while pos + 8 <= limit and count < 2048:
        size, kind = struct.unpack_from('>I4s', buf, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > limit:
                break
            size = struct.unpack_from('>Q', buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = limit - pos
        if size < hdr or pos + size > limit:
            break
        yield kind.decode('latin-1'), pos + hdr, pos + size
        pos += size
        count += 1


def _parse_qt_container(path: Path) -> dict:
    """Safely parse QuickTime / ISO BMFF container atoms (for ``.braw``, ``.crm``, ``.nev``, ``.mov``).

    Reads only bounded header/moov bytes so multi-gigabyte media files probe instantaneously.
    """
    size = path.stat().st_size
    if size < 16:
        raise RawError(f'{path.name} is truncated (not a valid QuickTime/ISO BMFF container)')
    # Scan top-level atom headers to locate ftyp and moov (moov can sit at head or tail)
    ftyp_brand = None
    moov_bytes = None
    valid_top = False
    max_moov = 8 * 1024 * 1024
    with open(path, 'rb') as fh:
        pos = 0
        steps = 0
        while pos + 8 <= size and steps < 512:
            fh.seek(pos)
            header = fh.read(16)
            if len(header) < 8:
                break
            box_size, box_type = struct.unpack_from('>I4s', header, 0)
            hdr_len = 8
            if box_size == 1:
                if len(header) < 16:
                    break
                box_size = struct.unpack_from('>Q', header, 8)[0]
                hdr_len = 16
            elif box_size == 0:
                box_size = size - pos
            if box_size < hdr_len or pos + box_size > size:
                break
            tag = box_type.decode('latin-1')
            if tag in ('ftyp', 'moov', 'mdat', 'wide', 'free', 'skip', 'meta', 'uuid'):
                valid_top = True
            if tag == 'ftyp' and box_size <= 4096:
                fh.seek(pos + hdr_len)
                payload = fh.read(box_size - hdr_len)
                if len(payload) >= 4:
                    ftyp_brand = payload[:4].decode('latin-1', 'replace')
            elif tag == 'moov' and box_size <= max_moov:
                fh.seek(pos + hdr_len)
                moov_bytes = fh.read(box_size - hdr_len)
            pos += box_size
            steps += 1

    if not valid_top or (ftyp_brand is None and moov_bytes is None):
        raise RawError(f'{path.name} is not a valid QuickTime/ISO BMFF camera RAW container')

    info = {
        'brand': ftyp_brand,
        'width': 0,
        'height': 0,
        'fps': None,
        'duration': 0.0,
        'videoCodecTag': None,
        'audioStreams': [],
        'timecode': None,
        'dropFrame': False,
        'reel': None,
        'camera': None,
    }

    if moov_bytes:
        mvhd_scale = 0
        mvhd_dur = 0
        for kind, s, e in _read_qt_boxes(moov_bytes, 0, len(moov_bytes), 0):
            if kind == 'mvhd' and e - s >= 20:
                ver = moov_bytes[s]
                if ver == 0 and e - s >= 20:
                    mvhd_scale, mvhd_dur = struct.unpack_from('>II', moov_bytes, s + 12)
                elif ver == 1 and e - s >= 32:
                    mvhd_scale, mvhd_dur = struct.unpack_from('>IQ', moov_bytes, s + 20)
                if mvhd_scale > 0 and mvhd_dur > 0:
                    info['duration'] = mvhd_dur / mvhd_scale
            elif kind == 'trak':
                _parse_qt_track(moov_bytes, s, e, info)
            elif kind in ('udta', 'meta'):
                _parse_qt_meta(moov_bytes, s, e, info)

    return info


def _parse_qt_track(buf: bytes, start: int, end: int, info: dict):
    handler = None
    md_scale = 0
    md_dur = 0
    tk_w, tk_h = 0, 0
    stsd_entries = []
    stts_entries = []
    for kind, s, e in _read_qt_boxes(buf, start, end, 1):
        if kind == 'tkhd' and e - s >= 84:
            ver = buf[s]
            off = s + (88 if ver == 1 else 76)
            if off + 8 <= e:
                w_fp, h_fp = struct.unpack_from('>II', buf, off)
                tk_w, tk_h = w_fp >> 16, h_fp >> 16
        elif kind == 'mdia':
            for mk, ms, me in _read_qt_boxes(buf, s, e, 2):
                if mk == 'mdhd' and me - ms >= 20:
                    ver = buf[ms]
                    if ver == 0 and me - ms >= 20:
                        md_scale, md_dur = struct.unpack_from('>II', buf, ms + 12)
                    elif ver == 1 and me - ms >= 32:
                        md_scale, md_dur = struct.unpack_from('>IQ', buf, ms + 20)
                elif mk == 'hdlr' and me - ms >= 12:
                    handler = buf[ms + 8:ms + 12].decode('latin-1', 'replace')
                elif mk == 'minf':
                    for ink, ins, ine in _read_qt_boxes(buf, ms, me, 3):
                        if ink == 'stbl':
                            for sk, ss, se in _read_qt_boxes(buf, ins, ine, 4):
                                if sk == 'stsd' and se - ss >= 16:
                                    cnt = struct.unpack_from('>I', buf, ss + 4)[0]
                                    ep = ss + 8
                                    for _ in range(min(cnt, 8)):
                                        if ep + 8 > se:
                                            break
                                        esize, efcc = struct.unpack_from('>I4s', buf, ep)
                                        if esize < 8 or ep + esize > se:
                                            break
                                        stsd_entries.append((efcc.decode('latin-1', 'replace'), ep + 8, ep + esize))
                                        ep += esize
                                elif sk == 'stts' and se - ss >= 16:
                                    cnt = struct.unpack_from('>I', buf, ss + 4)[0]
                                    ep = ss + 8
                                    for _ in range(min(cnt, 16)):
                                        if ep + 8 > se:
                                            break
                                        sc, sd = struct.unpack_from('>II', buf, ep)
                                        stts_entries.append((sc, sd))
                                        ep += 8
    if handler == 'vide':
        if stsd_entries:
            fcc, es, ee = stsd_entries[0]
            info['videoCodecTag'] = fcc
            if ee - es >= 28:
                sw, sh = struct.unpack_from('>HH', buf, es + 24)
                if sw > 0 and sh > 0:
                    tk_w, tk_h = sw, sh
        if tk_w > 0 and tk_h > 0:
            info['width'], info['height'] = tk_w, tk_h
        if md_scale > 0 and stts_entries and stts_entries[0][1] > 0:
            info['fps'] = Fraction(md_scale, stts_entries[0][1])
        if md_scale > 0 and md_dur > 0 and not info['duration']:
            info['duration'] = md_dur / md_scale
    elif handler == 'soun':
        ch, sr, codec = 2, 48000, 'pcm'
        if stsd_entries:
            fcc, es, ee = stsd_entries[0]
            codec = fcc.strip() or 'pcm'
            if ee - es >= 28:
                ch = struct.unpack_from('>H', buf, es + 16)[0] or 2
                sr_fp = struct.unpack_from('>I', buf, es + 24)[0]
                if sr_fp >> 16 > 0:
                    sr = sr_fp >> 16
        info['audioStreams'].append({'channels': ch, 'sampleRate': sr, 'codec': codec})
    elif handler == 'tmcd':
        # Extract reel name from tmcd sample entry name atom if present
        for fcc, es, ee in stsd_entries:
            if fcc == 'tmcd':
                for nk, ns, ne in _read_qt_boxes(buf, es + 26, ee, 5):
                    if nk == 'name' and ne - ns >= 4:
                        nlen = struct.unpack_from('>H', buf, ns)[0]
                        if 0 < nlen <= ne - ns - 4:
                            text = buf[ns + 4:ns + 4 + nlen].decode('utf-8', 'replace').strip()
                            if text and not info['reel']:
                                info['reel'] = text


def _parse_qt_meta(buf: bytes, start: int, end: int, info: dict, _depth: int = 0):
    """Best-effort extraction of ASCII/UTF-8 reel, camera, and timecode strings from udta/meta."""
    if _depth > 3 or end <= start:
        return
    chunk = buf[start:min(end, start + 65536)]
    text = chunk.decode('latin-1', 'replace')
    if not info['timecode']:
        m = _TIMECODE_RE.search(text)
        if m:
            drop = m.group(4) in ';,'
            info['timecode'] = f'{m.group(1)}:{m.group(2)}:{m.group(3)}{";" if drop else ":"}{m.group(5)}'
            info['dropFrame'] = drop
    # ISO BMFF `meta` boxes begin with a 4-byte version/flags fullbox header; QuickTime `meta` boxes do not.
    scan_start = start
    if end - start >= 12 and buf[start:start + 4] == b'\x00\x00\x00\x00':
        child_size = struct.unpack_from('>I', buf, start + 4)[0]
        if 8 <= child_size <= end - (start + 4):
            scan_start = start + 4
    # QuickTime `meta` box with `keys` + `ilst` (used by FFmpeg -movflags use_metadata_tags)
    keys: list[str] = []
    ilst_range = None
    for k, s, e in _read_qt_boxes(buf, scan_start, end, 2):
        if k in ('meta', 'udta'):
            _parse_qt_meta(buf, s, e, info, _depth + 1)
        elif k == 'keys' and e - s >= 8:
            count = struct.unpack_from('>I', buf, s + 4)[0]
            pos = s + 8
            for _ in range(min(count, 256)):
                if pos + 8 > e:
                    break
                ksize = struct.unpack_from('>I', buf, pos)[0]
                if ksize < 8 or pos + ksize > e:
                    break
                keys.append(buf[pos + 8:pos + ksize].decode('utf-8', 'replace'))
                pos += ksize
        elif k == 'ilst':
            ilst_range = (s, e)
    if keys and ilst_range:
        is_, ie_ = ilst_range
        pos = is_
        while pos + 8 <= ie_:
            esize, idx = struct.unpack_from('>II', buf, pos)
            if esize < 8 or pos + esize > ie_:
                break
            if 1 <= idx <= len(keys):
                key_name = keys[idx - 1].lower()
                for dk, ds, de in _read_qt_boxes(buf, pos + 8, pos + esize, 2):
                    if dk == 'data' and de - ds > 8:
                        val = buf[ds + 8:de].strip(b'\x00').decode('utf-8', 'replace').strip()
                        if val and len(val) <= 128:
                            if key_name in ('reel_name', 'com.apple.quicktime.reelname') and not info['reel']:
                                info['reel'] = val
                            elif key_name in ('com.apple.quicktime.model', 'model', 'camera') and not info['camera']:
                                info['camera'] = val
                            elif key_name == 'timecode' and not info['timecode']:
                                m = _TIMECODE_RE.fullmatch(val)
                                if m:
                                    drop = m.group(4) in ';,'
                                    info['timecode'] = f'{m.group(1)}:{m.group(2)}:{m.group(3)}{";" if drop else ":"}{m.group(5)}'
                                    info['dropFrame'] = drop
            pos += esize
    # Custom Splice/QT udta key=value atoms (e.g. 'reel', 'cam ', 'tmcd')
    for k, s, e in _read_qt_boxes(buf, scan_start, end, 2):
        val = buf[s:e].strip(b'\x00').decode('utf-8', 'replace').strip()
        if not val or len(val) > 128:
            continue
        if k == 'reel' and not info['reel']:
            info['reel'] = val
        elif k in ('cam ', 'modl', '\xa9mod') and not info['camera']:
            info['camera'] = val
        elif k == 'tmcd' and not info['timecode']:
            m = _TIMECODE_RE.fullmatch(val)
            if m:
                drop = m.group(4) in ';,'
                info['timecode'] = f'{m.group(1)}:{m.group(2)}:{m.group(3)}{";" if drop else ":"}{m.group(5)}'
                info['dropFrame'] = drop


def _ffprobe_optional(path: Path) -> dict | None:
    """Run ffprobe safely, returning parsed JSON or None if FFmpeg cannot inspect the container."""
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    try:
        res = subprocess.run(
            [ffprobe, '-v', 'error', '-protocol_whitelist', 'file', '-show_streams', '-show_format',
             '-of', 'json', str(path)],
            capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
        )
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            return data if isinstance(data, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def _read_braw_sidecar(path: Path) -> dict:
    """Read optional Blackmagic RAW ``.sidecar`` JSON next to ``path``."""
    sidecar = path.with_suffix('.sidecar')
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    if isinstance(data.get('iso'), (int, float)):
        out['iso'] = int(data['iso'])
    if isinstance(data.get('exposure'), (int, float)):
        out['exposure'] = float(data['exposure'])
    kelvin = data.get('white_balance_kelvin', data.get('color_temp', data.get('colorTemp')))
    if isinstance(kelvin, (int, float)):
        out['kelvin'] = int(kelvin)
    tint = data.get('white_balance_tint', data.get('tint'))
    if isinstance(tint, (int, float)):
        out['tint'] = float(tint)
    hr = data.get('highlight_recovery', data.get('highlightRecovery'))
    if isinstance(hr, bool):
        out['highlightRecovery'] = hr
    return out


_RED_REEL = re.compile(r'^([A-Z][0-9]{3})_C[0-9]{3}', re.IGNORECASE)


def _read_red_sidecar(path: Path) -> dict:
    """Optional RED ``.RMD`` XML sidecar: look settings, TC and reel by tag name."""
    out = {}
    for rmd in (path.with_suffix('.RMD'), path.with_suffix('.rmd')):
        try:
            if not rmd.is_file() or rmd.stat().st_size > 1024 * 1024:
                continue
            root = ET.fromstring(rmd.read_text(encoding='utf-8', errors='replace'))
        except (OSError, ET.ParseError):
            continue
        for elem in root.iter():
            tag = str(elem.tag).rsplit('}', 1)[-1].lower()
            val = (elem.text or '').strip()
            if not val or len(val) > 64:
                continue
            try:
                if tag == 'iso' and val.isdigit():
                    out['iso'] = int(val)
                elif tag in ('kelvin', 'color_temp'):
                    out['kelvin'] = int(float(val))
                elif tag == 'tint':
                    out['tint'] = float(val)
                elif tag in ('exposure', 'exposureadjust'):
                    out['exposure'] = float(val)
                elif tag in ('timecode', 'abs_tc', 'starttimecode') and _TIMECODE_RE.fullmatch(val):
                    out['timecode'] = val
                elif tag in ('reel', 'reel_id', 'reelname'):
                    out['reel'] = val
            except ValueError:
                continue
        break
    return out


def _parse_r3d_container(path: Path) -> dict:
    """RED ``.r3d``: the ``RED1``/``RED2`` header atom and the ``REOB`` -> ``RDVO`` frame index.

    Field offsets follow FFmpeg's ``r3d`` demuxer (libavformat/r3d.c). Values the header does
    not carry (start timecode, reel) come from an ``.RMD`` sidecar or are left unknown."""
    size = path.stat().st_size
    if size < 72:
        raise RawError(f'{path.name} is truncated (not a valid RED R3D file)')
    with open(path, 'rb') as fh:
        head = fh.read(min(size, 4096))
        if head[4:8] not in (b'RED1', b'RED2'):
            raise RawError(f'{path.name} is not a RED R3D file (missing RED1/RED2 header)')
        timescale, = struct.unpack_from('>I', head, 12)
        width, height = struct.unpack_from('>II', head, 52)
        fps_num, fps_den = struct.unpack_from('>HH', head, 62)
        channels = head[66]
        stored = head[67:67 + 257].split(b'\x00', 1)[0].decode('utf-8', 'replace').strip()
        frames = None
        if size >= 56 + 72:
            fh.seek(size - 56)
            tail = fh.read(56)
            if len(tail) == 56 and tail[4:8] in (b'REOB', b'REOF', b'REOS'):
                rdvo, = struct.unpack_from('>I', tail, 8)
                if 72 <= rdvo <= size - 8:
                    fh.seek(rdvo)
                    atom = fh.read(8)
                    if len(atom) == 8 and atom[4:8] == b'RDVO':
                        atom_size, = struct.unpack_from('>I', atom, 0)
                        if 8 <= atom_size <= size - rdvo:
                            frames = (atom_size - 8) // 4 or None
    sidecar = _read_red_sidecar(path)
    fps = Fraction(fps_num, fps_den) if fps_num and fps_den and 1 <= fps_num / fps_den <= 300 else None
    tc = sidecar.pop('timecode', None)
    reel = sidecar.pop('reel', None)
    reel_source = 'sidecar' if reel else None
    if not reel:
        match = _RED_REEL.match(path.stem)
        if match:
            reel, reel_source = match.group(1).upper(), 'file name'
    return {
        'width': width if 16 <= width <= 16384 else None,
        'height': height if 16 <= height <= 16384 else None,
        'fps': fps,
        'frameCount': frames,
        'timescale': timescale or None,
        'audioChannels': channels if 0 < channels <= 32 else 0,
        'clipName': stored or None,
        'timecode': tc,
        'reel': reel,
        'reelSource': reel_source,
        'defaults': sidecar,
    }


def _parse_ari_container(path: Path) -> dict:
    """ARRIRAW ``.ari``/``.arx`` frame header: ``ARRI`` magic, little-endian width/height at 20
    and camera model at 668 (the fields LibRaw documents). Frame rate, timecode and look
    metadata live in vendor-defined blocks Splice does not guess at."""
    size = path.stat().st_size
    if size < 732:
        raise RawError(f'{path.name} is truncated (not a valid ARRIRAW frame)')
    with open(path, 'rb') as fh:
        head = fh.read(4096)
    if head[:4] != b'ARRI':
        raise RawError(f'{path.name} is not an ARRIRAW frame (missing ARRI magic)')
    width, height = struct.unpack_from('<II', head, 20)
    model = head[668:732].split(b'\x00', 1)[0].decode('latin-1', 'replace').strip()
    seq = raw_bayer.detect_dng_sequence(path)
    return {
        'width': width if 16 <= width <= 16384 else None,
        'height': height if 16 <= height <= 16384 else None,
        'camera': ''.join(ch for ch in model if ch.isprintable()) or None,
        'frameCount': seq['frameCount'],
        'isSequence': seq['isSequence'],
        'audioPath': seq['audioPath'],
        'prefix': seq['prefix'],
    }


# ---------------------------------------------------------------------------
# Format Detection & Unified Probe
# ---------------------------------------------------------------------------

def is_raw_extension(name: str) -> bool:
    return Path(str(name)).suffix.lower() in RAW_EXTENSIONS


def detect_raw_format(path) -> str | None:
    """Return the RAW format ID for ``path``, or ``None`` for standard non-RAW media."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == '.dng':
        return 'cinemadng'
    if suffix == '.braw':
        return 'braw'
    if suffix == '.r3d':
        return 'r3d'
    if suffix in ('.ari', '.arx'):
        return 'arriraw'
    if suffix == '.crm':
        return 'canon_crm'
    if suffix == '.nev':
        return 'nraw'
    if suffix in STILL_RAW_EXTENSIONS:
        return 'still_raw'
    if suffix not in ('.mov', '.mxf'):
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _probe_lock:
        if key in _container_cache:
            return _container_cache[key]
    found = None
    if suffix == '.mov':
        # ProRes RAW / Blackmagic RAW sample entries, read from the bounded moov box.
        try:
            tag = _parse_qt_container(path).get('videoCodecTag')
            found = 'prores_raw' if tag in PRORES_RAW_TAGS else 'braw' if tag in BRAW_CODEC_TAGS else None
        except (RawError, OSError, struct.error):
            found = None
    else:
        # Only an MXF whose picture FFmpeg cannot decode, and whose tags name a RAW essence, is
        # camera RAW. Every MXF FFmpeg can decode (XDCAM, DNxHD, AVC-Intra, ...) stays ordinary media.
        data = _ffprobe_optional(path)
        video = _ffprobe_streams(data, 'video')
        # A picture stream must exist: a sound-only MXF whose tags name a RAW camera is just sound.
        if data and video and str(video[0].get('codec_name') or 'none') in ('none', 'unknown'):
            tags = ' '.join(f'{k} {v}' for k, v in _ffprobe_tags(data).items()).lower()
            descriptor = ' '.join(str(v) for s in video for k, v in s.items() if k in ('codec_tag_string', 'codec_long_name')).lower()
            blob = tags + ' ' + descriptor
            found = 'xocn' if any(m in blob for m in XOCN_MARKERS) else \
                'arriraw' if any(m in blob for m in ARRI_MXF_MARKERS) else None
    with _probe_lock:
        if len(_container_cache) >= 256:
            _container_cache.pop(next(iter(_container_cache)))
        _container_cache[key] = found
    return found


def is_raw_candidate(path, name: str | None = None) -> bool:
    """True if ``name`` or ``path`` should be handled by the Camera RAW engine."""
    target = Path(path)
    if is_raw_extension(name or target.name):
        return True
    return detect_raw_format(target) is not None


def _rational_str(frac: Fraction | None) -> str | None:
    if not frac:
        return None
    return f'{frac.numerator}/{frac.denominator}'


def _missing_decoder_message(fmt: str, name: str) -> str:
    label = FORMAT_LABELS.get(fmt, 'Camera RAW')
    spec = RAW_BACKEND_BY_ID.get('libraw' if fmt == 'still_raw' else fmt, {})
    hint = spec.get('installHint', 'Install the vendor SDK or CLI.')
    return (f'{name} is {label} (camera RAW). Its metadata and sound were imported, but no decoder for its picture '
            f'is installed. {hint.rstrip(".")}. Open File > RAW Decoders… to set its path, or attach a ProRes/DNxHR '
            f'proxy in the Inspector (export keeps pointing at the original).')


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _audio_details(streams) -> tuple[list[dict], int | None]:
    details = []
    for i, stream in enumerate(streams[:16]):
        channels = max(1, min(64, _int(stream.get('channels'), 2)))
        details.append({'index': i, 'streamIndex': _int(stream.get('index'), i), 'channels': channels,
                        'layout': stream.get('channel_layout') or stream.get('layout') or
                        ('stereo' if channels == 2 else 'mono' if channels == 1 else None),
                        'codec': stream.get('codec_name') or stream.get('codec') or 'pcm'})
    rate = _int(streams[0].get('sample_rate') or streams[0].get('sampleRate'), 0) if streams else 0
    return details, rate or None


def _ffprobe_streams(data: dict | None, kind: str) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get('streams'), list):
        return []
    return [s for s in data['streams'] if isinstance(s, dict) and s.get('codec_type') == kind]


def _ffprobe_tags(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    tags = {}
    sources = [data.get('format') or {}] + [s for s in data.get('streams') or [] if isinstance(s, dict)]
    for source in sources:
        for key, value in (source.get('tags') or {}).items() if isinstance(source, dict) else ():
            if isinstance(value, str) and value.strip():
                tags.setdefault(str(key).lower(), value.strip())
    return tags


def _timecode(value) -> tuple[str | None, bool]:
    match = _TIMECODE_RE.fullmatch(str(value or '').strip())
    if not match:
        return None, False
    drop = match.group(4) in ';,'
    return f'{match.group(1)}:{match.group(2)}:{match.group(3)}{";" if drop else ":"}{match.group(5)}', drop


def _fraction(value) -> Fraction | None:
    try:
        frac = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return frac if 0 < frac <= 300 else None


def _seconds(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and 0 < seconds < 10 * 24 * 3600 else None


# Look metadata a camera records; a placeholder stands in (and is flagged) when the file has none.
LOOK_METADATA = ('iso', 'kelvin', 'tint')


def _record(path: Path, fmt: str, *, width=None, height=None, fps=None, duration=None, frame_count=None,
            timecode=None, drop_frame=False, reel=None, camera=None, defaults=None, audio=(), audio_rate=None,
            audio_path=None, still=False, label=None, decoder=None, metadata=None, video_codec=None,
            decoder_kind=None, camera_placeholder=None) -> dict:
    """One Splice ``describe()`` record for a camera RAW source.

    Nothing is invented: a value the file does not carry falls back to an editable placeholder
    and is named in ``rawEstimated`` so the Inspector and interchange reports can say so. That
    covers the picture (size, rate, duration, timecode) and the look the camera recorded: ISO,
    white balance (``kelvin``, ``tint``) and the ``camera`` model, when neither the file nor its
    sidecar holds them (``defaults`` carries only what was read)."""
    estimated = []
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        width, height = 1920, 1080
        estimated += ['width', 'height']
    if not fps:
        fps = Fraction(24, 1)
        estimated.append('fps')
    if not (isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0):
        if frame_count:
            duration = float(frame_count / fps)
        else:
            duration = 5.0
            if not still:
                estimated.append('duration')
    if timecode is None:
        estimated.append('timecode')
    read = {k for k, v in (defaults or {}).items() if v is not None}
    estimated += [key for key in LOOK_METADATA if key not in read]
    if not camera:
        estimated.append('camera')
        camera = camera_placeholder
    if decoder is None:
        backend = _resolve_backend('libraw' if fmt == 'still_raw' else fmt)
        available = backend['available']
        decoder = (available, f"{backend['version']} ({backend['activeBackend']})" if available else None,
                   None if available else _missing_decoder_message(fmt, path.name))
        decoder_kind = _decoder_kind(fmt, backend)
    available, decoder_name, decoder_message = decoder
    audio = list(audio)
    meta = {'camera': camera or None, 'isSequence': False}
    meta.update(metadata or {})
    if frame_count:
        meta['frameCount'] = int(frame_count)
    return {
        'type': 'video', 'raw': True, 'rawFormat': fmt, 'rawFormatLabel': label or FORMAT_LABELS[fmt],
        'rawDecoderAvailable': bool(available), 'rawDecoderName': decoder_name,
        'rawDecoderMessage': decoder_message,
        'rawDefaults': normalize_raw_settings(defaults or {}, fmt=fmt),
        'rawAudioPath': audio_path, 'rawMetadata': meta, 'rawEstimated': estimated,
        'rawCapabilities': decoder_capabilities(decoder_kind) if available else None,
        'duration': float(duration), 'width': int(width), 'height': int(height),
        'hdr': False, 'hdrPreviewVersion': None, 'fps': float(fps), 'rate': _rational_str(fps), 'vfr': False,
        'timecode': timecode, 'dropFrame': bool(timecode and drop_frame), 'reel': reel or None,
        'timeReference': None, 'audioSampleRate': audio_rate, 'videoCodec': video_codec or fmt,
        'audioCodec': audio[0]['codec'] if audio else None,
        'audioChannels': 2 if len(audio) > 1 else (audio[0]['channels'] if audio else 0),
        'audioStreams': len(audio), 'audioStreamDetails': audio,
        'audioStreamChannels': [stream['channels'] for stream in audio],
        'still': bool(still), 'stillPreviewVersion': None,
        # Every decoder below delivers display-referred Rec.709 RGB (or what the clip's
        # colorSpace/gamma choice asks for) into a BT.709-tagged picture.
        'pixelFormat': 'rgb48le', 'colorPrimaries': 'bt709', 'colorTransfer': 'bt709', 'colorSpace': 'bt709',
        'colorRange': 'tv', 'rotation': 0, 'proxyRecommended': True,
    }


def _probe_cinemadng(path: Path) -> dict:
    seq = raw_bayer.detect_dng_sequence(path)
    meta = raw_bayer.parse_dng(seq['firstFrame'])
    kind = 'builtin'
    if meta['builtinDecodable']:
        decoder = (True, f"Built-in CinemaDNG ({meta['bitsPerSample']}-bit {meta['cfaName']})", None)
    elif meta['compression'] in FFMPEG_DNG_COMPRESSIONS and shutil.which('ffmpeg'):
        decoder = (True, f"FFmpeg DNG + built-in color ({meta['bitsPerSample']}-bit, compression {meta['compression']})", None)
    else:
        libraw = _resolve_backend('libraw')
        decoder = (libraw['available'], f"LibRaw ({libraw['version']})" if libraw['available'] else None,
                   None if libraw['available'] else _missing_decoder_message('libraw', path.name))
        kind = _decoder_kind('libraw', libraw)
    look = {'iso': meta.get('iso')}
    if meta.get('whiteBalanceKnown'):
        look.update(kelvin=meta['kelvin'], tint=meta['tint'])
    audio, rate = _audio_details(_ffprobe_streams(_ffprobe_optional(Path(seq['audioPath'])), 'audio')) \
        if seq['audioPath'] else ([], None)
    return _record(
        path, 'cinemadng', width=meta['width'], height=meta['height'], fps=meta['fps'],
        frame_count=seq['frameCount'] if seq['isSequence'] else None, timecode=meta['timecode'],
        drop_frame=meta['dropFrame'], reel=meta['reel'] or seq['prefix'][:16] or None,
        camera=meta['camera'] if meta.get('cameraKnown') else None, camera_placeholder=meta['camera'],
        defaults=look, decoder_kind=kind,
        audio=audio, audio_rate=rate, audio_path=seq['audioPath'], still=not seq['isSequence'],
        label=f"CinemaDNG {meta['bitsPerSample']}-bit", decoder=decoder, video_codec='cinemadng',
        metadata={'bitDepth': meta['bitsPerSample'], 'bayerPattern': meta['cfaName'],
                  'isSequence': seq['isSequence'], 'compressed': meta['compressed'],
                  'missingFrames': seq['missingFrames'],
                  'reelSource': 'ReelName tag' if meta['reel'] else 'file name'})


def _probe_quicktime(path: Path, fmt: str) -> dict:
    qt = _parse_qt_container(path)
    ff = _ffprobe_optional(path)
    tags = _ffprobe_tags(ff)
    audio, rate = _audio_details(_ffprobe_streams(ff, 'audio') or qt['audioStreams'])
    duration = _seconds(qt['duration']) or _seconds(((ff or {}).get('format') or {}).get('duration'))
    timecode, drop = (qt['timecode'], qt['dropFrame']) if qt['timecode'] else _timecode(tags.get('timecode'))
    return _record(
        path, fmt, width=qt['width'] or None, height=qt['height'] or None, fps=qt['fps'],
        duration=duration, timecode=timecode, drop_frame=drop,
        reel=qt['reel'] or tags.get('reel_name') or tags.get('com.apple.quicktime.reelname'),
        camera=qt['camera'] or tags.get('com.apple.quicktime.model') or tags.get('model'),
        defaults=_read_braw_sidecar(path) if fmt == 'braw' else {}, audio=audio, audio_rate=rate,
        video_codec=qt['videoCodecTag'] or fmt,
        metadata={'codecTag': qt['videoCodecTag'], 'brand': qt['brand'],
                  'hasSidecar': fmt == 'braw' and path.with_suffix('.sidecar').is_file()})


def _probe_r3d(path: Path) -> dict:
    red = _parse_r3d_container(path)
    audio, rate = _audio_details(_ffprobe_streams(_ffprobe_optional(path), 'audio'))
    timecode, drop = _timecode(red['timecode'])
    return _record(
        path, 'r3d', width=red['width'], height=red['height'], fps=red['fps'], frame_count=red['frameCount'],
        timecode=timecode, drop_frame=drop, reel=red['reel'], camera='RED', defaults=red['defaults'],
        audio=audio, audio_rate=rate, label='RED RAW (R3D)', video_codec='r3d',
        metadata={'clipName': red['clipName'], 'reelSource': red['reelSource'],
                  'headerAudioChannels': red['audioChannels']})


def _probe_ari(path: Path) -> dict:
    ari = _parse_ari_container(path)
    audio, rate = _audio_details(_ffprobe_streams(_ffprobe_optional(Path(ari['audioPath'])), 'audio')) \
        if ari['audioPath'] else ([], None)
    return _record(
        path, 'arriraw', width=ari['width'], height=ari['height'],
        frame_count=ari['frameCount'] if ari['isSequence'] else None, reel=ari['prefix'][:16] or None,
        camera=ari['camera'], camera_placeholder='ARRI', audio=audio, audio_rate=rate, audio_path=ari['audioPath'],
        still=not ari['isSequence'], label='ARRIRAW', video_codec='arriraw',
        metadata={'isSequence': ari['isSequence']})


def _probe_ffprobe(path: Path, fmt: str) -> dict:
    ff = _ffprobe_optional(path)
    if not ff:
        raise RawError(f'{path.name} is {FORMAT_LABELS[fmt]} (camera RAW) and FFmpeg could not read its container')
    video = (_ffprobe_streams(ff, 'video') or [{}])[0]
    tags = _ffprobe_tags(ff)
    audio, rate = _audio_details(_ffprobe_streams(ff, 'audio'))
    timecode, drop = _timecode(tags.get('timecode'))
    fps = _fraction(video.get('avg_frame_rate')) or _fraction(video.get('r_frame_rate'))
    duration = _seconds((ff.get('format') or {}).get('duration')) or _seconds(video.get('duration'))
    return _record(
        path, fmt, width=_int(video.get('width')) or None, height=_int(video.get('height')) or None, fps=fps,
        duration=duration, timecode=timecode, drop_frame=drop,
        reel=tags.get('reel_name') or tags.get('com.apple.quicktime.reelname'),
        camera=tags.get('com.apple.quicktime.model') or tags.get('model') or tags.get('company_name'),
        audio=audio, audio_rate=rate, video_codec=video.get('codec_name') or fmt,
        metadata={'codecTag': video.get('codec_tag_string')})


def _probe_still(path: Path) -> dict:
    backend = _resolve_backend('libraw')
    if not backend['available']:
        raise RawError(_missing_decoder_message('still_raw', path.name).split(' Its metadata')[0] +
                       ' Install LibRaw (dcraw_emu) and set its path in File > RAW Decoders….')
    return _record(path, 'still_raw', still=True, label=f'Camera RAW ({path.suffix.upper().lstrip(".")})',
                   camera=None, camera_placeholder=path.suffix.upper().lstrip('.') + ' camera', video_codec='still_raw',
                   decoder=(True, f"LibRaw ({backend['version']})", None), decoder_kind=_decoder_kind('libraw', backend))


_PROBERS = {
    'cinemadng': _probe_cinemadng, 'braw': lambda p: _probe_quicktime(p, 'braw'),
    'canon_crm': lambda p: _probe_quicktime(p, 'canon_crm'), 'nraw': lambda p: _probe_quicktime(p, 'nraw'),
    'r3d': _probe_r3d, 'prores_raw': lambda p: _probe_ffprobe(p, 'prores_raw'),
    'xocn': lambda p: _probe_ffprobe(p, 'xocn'), 'still_raw': _probe_still,
}
_probe_cache: dict = {}


def _probe_stamp(path: Path):
    stat = path.stat()
    try:
        parent = path.parent.stat().st_mtime_ns
    except OSError:
        parent = 0
    try:
        config = config_path().stat().st_mtime_ns
    except OSError:
        config = 0
    return str(path), stat.st_size, stat.st_mtime_ns, parent, config


def probe_raw(path) -> dict:
    """Probe a camera RAW file or sequence into a Splice ``describe()`` record.

    Raises :class:`RawError` (a ``ValueError``) when the file is malformed or unreadable."""
    path = Path(path)
    fmt = detect_raw_format(path)
    if not fmt:
        raise RawError(f'{path.name} is not a recognized camera RAW file')
    try:
        stamp = _probe_stamp(path)
    except OSError as error:
        raise RawError(f'{path.name} cannot be read: {error.strerror or error}') from None
    with _probe_lock:
        cached = _probe_cache.get(stamp)
    if cached is not None:
        return json.loads(cached)
    if fmt == 'arriraw':
        prober = _probe_ari if path.suffix.lower() in ('.ari', '.arx') else (lambda p: _probe_ffprobe(p, 'arriraw'))
    else:
        prober = _PROBERS[fmt]
    try:
        record = prober(path)
    except RawError as error:
        message = str(error)
        raise RawError(message if FORMAT_LABELS[fmt] in message else
                       f'{path.name} is named like {FORMAT_LABELS[fmt]} (camera RAW) but cannot be read: {message}') from None
    except (OSError, ValueError, *raw_bayer.MALFORMED_ERRORS) as error:
        raise RawError(f'{path.name} is {FORMAT_LABELS[fmt]} (camera RAW) but its header is damaged '
                       f'({type(error).__name__})') from None
    with _probe_lock:
        if len(_probe_cache) >= 128:
            _probe_cache.pop(next(iter(_probe_cache)))
        _probe_cache[stamp] = json.dumps(record)
    return record


def enrich_prores_raw(path, described: dict) -> dict:
    """Mark an ffprobe ``describe()`` record whose video codec is ``prores_raw`` as camera RAW."""
    backend = _resolve_backend('prores_raw')
    out = dict(described)
    out.update({
        'raw': True, 'rawFormat': 'prores_raw', 'rawFormatLabel': 'Apple ProRes RAW',
        'rawDecoderAvailable': backend['available'],
        'rawDecoderName': f"FFmpeg prores_raw ({backend['version']})" if backend['available'] else None,
        'rawDecoderMessage': None if backend['available'] else _missing_decoder_message('prores_raw', Path(path).name),
        'rawDefaults': normalize_raw_settings({}, fmt='prores_raw'), 'rawAudioPath': None,
        'rawMetadata': {'camera': out.get('camera') or None, 'isSequence': False},
        # FFmpeg's prores_raw decoder exposes no ISO or white balance: the look starts from placeholders.
        'rawEstimated': list(LOOK_METADATA) + ([] if out.get('camera') else ['camera']),
        'rawCapabilities': decoder_capabilities('prores_raw') if backend['available'] else None,
        'pixelFormat': 'rgb48le', 'colorPrimaries': 'bt709', 'colorTransfer': 'bt709', 'colorSpace': 'bt709',
        'proxyRecommended': True,
    })
    return out


# ---------------------------------------------------------------------------
# Streaming Decode -> Proxy MP4 & Full-Res 16-bit Export Intermediate
# ---------------------------------------------------------------------------
#
# Every decoder hands Splice 16-bit RGB (``rgb48le``) frames. One encoder turns those frames
# into either a playback proxy (H.264, at most 1280x720) or a full-resolution export
# intermediate (lossless FFV1 in QuickTime, 16-bit 4:4:4, BT.709-tagged) that render_movie.py grades and
# delivers like any other source.

PROXY_BOX = (1280, 720)
SEEK_PROXY_BOX = (960, 540)
INTERMEDIATE_PICTURE = ('scale=out_color_matrix=bt709:out_range=tv,'
                        'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,'
                        'format=yuv444p16le')


class _Job:
    """Cancellation and liveness hooks for a long decode (export or proxy)."""

    def __init__(self, cancelled=None, heartbeat=None):
        self.cancelled = cancelled
        self.heartbeat = heartbeat

    def tick(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise InterruptedError('Export cancelled')
        if self.heartbeat is not None:
            self.heartbeat()


def _timescale(fps: Fraction) -> str:
    """A QuickTime track timescale in which every frame lasts a whole number of ticks, so the
    renderer's frame-accurate seeks land on frames (Matroska's 1 ms timebase does not)."""
    return str(fps.numerator * (1000 if fps.denominator == 1 else 1))


def _output_args(is_intermediate: bool, seek_optimized: bool, has_audio: bool, audio_map: str,
                 fps: Fraction = Fraction(24)) -> list[str]:
    if is_intermediate:
        args = ['-map', '0:v:0', '-vf', INTERMEDIATE_PICTURE, '-c:v', 'ffv1', '-level', '3', '-g', '1',
                '-video_track_timescale', _timescale(fps),
                '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
        return args + (['-map', audio_map, '-c:a', 'pcm_s24le', '-ar', '48000'] if has_audio else ['-an'])
    box_w, box_h = SEEK_PROXY_BOX if seek_optimized else PROXY_BOX
    args = ['-map', '0:v:0', '-vf',
            f"scale=w='min({box_w},iw)':h='min({box_h},ih)':force_original_aspect_ratio=decrease:"
            "force_divisible_by=2:out_color_matrix=bt709:out_range=tv,setsar=1,"
            "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,format=yuv420p",
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16' if seek_optimized else '14',
            '-g', '1' if seek_optimized else '12', '-pix_fmt', 'yuv420p',
            '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    audio = ['-map', audio_map, '-c:a', 'aac', '-b:a', '160k', '-ac', '2'] if has_audio else ['-an']
    return args + audio + ['-movflags', '+faststart']


def _audio_input(audio_path, audio_stream, start_seconds: float) -> tuple[list[str], str | None]:
    if audio_path is None or not Path(audio_path).is_file():
        return [], None
    seek = ['-ss', f'{start_seconds:.6f}'] if start_seconds > 0 else []
    stream = audio_stream if isinstance(audio_stream, int) and 0 <= audio_stream < 16 else 0
    return seek + ['-i', str(audio_path)], f'1:a:{stream}?'


def _ffmpeg_pipe_encode(frames_iter, width: int, height: int, fps: Fraction, out_path: Path, *,
                        is_intermediate: bool, seek_optimized: bool = False, audio_path=None,
                        audio_stream: int | None = None, audio_start: float = 0.0, frame_total: int,
                        job: _Job | None = None):
    """Stream ``frame_total`` ``rgb48le`` frames from ``frames_iter`` into FFmpeg's stdin."""
    job = job or _Job()
    audio_in, audio_map = _audio_input(audio_path, audio_stream, audio_start)
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-threads', '2',
           '-protocol_whitelist', 'file,pipe', '-f', 'rawvideo', '-pix_fmt', 'rgb48le',
           '-s', f'{width}x{height}', '-framerate', _rational_str(fps), '-i', 'pipe:0', *audio_in,
           *_output_args(is_intermediate, seek_optimized, audio_map is not None, audio_map or '', fps),
           '-t', f'{float(frame_total / fps):.6f}', str(out_path)]
    errors = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
    try:
        written = 0
        for frame in frames_iter:
            job.tick()
            proc.stdin.write(frame)
            written += 1
        proc.stdin.close()
        if written != frame_total:
            raise RawError(f'The RAW decoder delivered {written} of {frame_total} frames')
        proc.wait(timeout=600)
    except BrokenPipeError:
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass
        errors.seek(0)
        message = errors.read()[-600:].decode('utf-8', 'replace').strip()
        errors.close()
    if proc.returncode:
        raise RawError('Encoding the decoded RAW frames failed: ' + (message or f'FFmpeg exit {proc.returncode}'))


def _ffmpeg_bayer_plane(path: Path, meta: dict):
    """Unpack a compressed (lossless-JPEG) DNG frame with FFmpeg's tiff decoder. FFmpeg subtracts
    the black level and scales to 16 bits but applies no colour, so the plane goes through the
    same white balance / matrix / transfer as uncompressed frames."""
    pix = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-select_streams', 'v:0',
                          '-show_entries', 'stream=pix_fmt,width,height', '-of', 'json', 'file:' + str(path)],
                         capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    try:
        stream = json.loads(pix.stdout)['streams'][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise RawError(f'FFmpeg cannot read the compressed DNG frame {path.name}') from None
    fmt = str(stream.get('pix_fmt') or '')
    match = re.fullmatch(r'bayer_(rggb|bggr|grbg|gbrg)16le', fmt)
    if not match or (stream.get('width'), stream.get('height')) != (meta['width'], meta['height']):
        raise RawError(f'FFmpeg decodes {path.name} as {fmt or "an unknown format"}, not a {meta["width"]}x'
                       f'{meta["height"]} Bayer plane. Install LibRaw (File > RAW Decoders…) for this DNG.')
    out = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file', '-i', 'file:' + str(path),
                          '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', fmt, 'pipe:1'],
                         capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
    need = meta['width'] * meta['height'] * 2
    if out.returncode or len(out.stdout) != need:
        raise RawError(f'FFmpeg could not unpack {path.name}: ' + out.stderr.decode('utf-8', 'replace')[-300:])
    plane = array('H')
    plane.frombytes(out.stdout)
    if sys.byteorder != 'little':
        plane.byteswap()
    linear = dict(meta, cfaPattern=raw_bayer.CFA_BY_PIX_FMT[match.group(1)], blackLevel=(0.0,) * 4,
                  whiteLevel=65535.0)
    return plane, linear


def decode_dng(path: Path, settings: dict, scale: int, via_ffmpeg: bool = False):
    """Develop one CinemaDNG frame to ``(w, h, rgb48le)``: built in, or FFmpeg-unpacked when compressed."""
    if not via_ffmpeg:
        return raw_bayer.decode_dng_frame(path, settings=settings, scale=scale)
    meta = raw_bayer.parse_dng(path)
    plane, linear = _ffmpeg_bayer_plane(path, meta)
    try:
        return raw_bayer.decode_plane(plane, linear, settings, scale=scale, name=path.name)
    except raw_bayer.MALFORMED_ERRORS as error:
        raise raw_bayer.malformed(path.name, error) from None


def _frame_window(total: int, start: int, count: int | None) -> tuple[int, int]:
    start = max(0, min(int(start or 0), max(0, total - 1)))
    count = total - start if count is None else max(1, min(int(count), total - start))
    return start, count


def _decode_cinemadng(path: Path, out_path: Path, settings: dict, info: dict, *, is_intermediate: bool,
                      seek_optimized=False, audio_stream=None, start_frame=0, frame_count=None, job=None):
    seq = raw_bayer.detect_dng_sequence(path)
    first = raw_bayer.parse_dng(seq['frames'][0])
    via_ffmpeg = False
    if not first['builtinDecodable']:
        if first['compression'] in FFMPEG_DNG_COMPRESSIONS and shutil.which('ffmpeg'):
            via_ffmpeg = True
        else:
            libraw = _resolve_backend('libraw')
            if not libraw['available']:
                raise RawError(_missing_decoder_message('libraw', path.name))
            return _decode_via_cli(path, out_path, settings, backend=libraw, info=info, is_intermediate=is_intermediate,
                                   seek_optimized=seek_optimized, audio_stream=audio_stream, start_frame=start_frame,
                                   frame_count=frame_count, job=job,
                                   frame_paths=seq['frames'] if seq['isSequence'] else None)
    scale = 1 if is_intermediate else DEBAYER_SCALES.get(settings.get('debayerQuality', 'half'), 2)
    fps = Fraction(info.get('rate') or '24/1')
    if seq['isSequence']:
        start, count = _frame_window(len(seq['frames']), start_frame, frame_count)
        frames = seq['frames'][start:start + count]
    else:
        # A single DNG is a still: hold it as long as the caller asks (the longest clip using it).
        start, count = 0, max(1, int(frame_count) if frame_count else round(float(info.get('duration') or 5.0) * fps))
        frames = [seq['frames'][0]] * count
    out_w, out_h, first_rgb = decode_dng(frames[0], settings, scale, via_ffmpeg)

    def developed():
        yield first_rgb
        previous, rgb = frames[0], first_rgb
        for frame in frames[1:]:
            if frame != previous:
                width, height, rgb = decode_dng(frame, settings, scale, via_ffmpeg)
                if (width, height) != (out_w, out_h):
                    raise RawError(f'CinemaDNG frame {frame.name} is {width}x{height}, not {out_w}x{out_h}')
                previous = frame
            yield rgb

    _ffmpeg_pipe_encode(developed(), out_w, out_h, fps, out_path, is_intermediate=is_intermediate,
                        seek_optimized=seek_optimized, audio_path=seq['audioPath'] if seq['isSequence'] else None,
                        audio_stream=audio_stream, audio_start=float(start / fps), frame_total=count, job=job)


def _gains(settings: dict) -> tuple[float, float, float]:
    """Exposure x ISO x white balance gains relative to a D55 (5600 K) neutral, for decoders that
    hand Splice white-balanced or camera-native RGB without a colour matrix."""
    clip = settings.get('decodeUsing') == 'clip'
    exposure = float(settings.get('exposure', 0.0)) if clip else 0.0
    iso = float(settings.get('iso', 800)) if clip else 800.0
    kelvin = float(settings.get('kelvin', 5600)) if clip else 5600.0
    tint = float(settings.get('tint', 0.0)) if clip else 0.0
    gain = (2.0 ** exposure) * (max(50.0, iso) / 800.0)
    to709 = raw_bayer.XYZ_D65_TO_GAMUT['rec709']
    ref = raw_bayer._mat_vec(to709, raw_bayer.kelvin_tint_to_xyz(5600.0, 0.0))
    cur = raw_bayer._mat_vec(to709, raw_bayer.kelvin_tint_to_xyz(kelvin, tint))
    red = (ref[0] / max(1e-4, cur[0])) / (ref[1] / max(1e-4, cur[1]))
    blue = (ref[2] / max(1e-4, cur[2])) / (ref[1] / max(1e-4, cur[1]))
    return red * gain, gain, blue * gain


_TRANSFER_EXPR = {
    'linear': 'val',
    'rec709': "if(lt(val,1179.63),4.5*val,65535*(1.099*pow(val/65535,0.45)-0.099))",
    'cinemadng_log': "65535*log(val/65535*15+1)/log(2)/4",
}


def prores_raw_filters(settings: dict) -> str:
    """FFmpeg filter chain applied to ``prores_raw`` output: debayer to 16-bit RGB, exposure/ISO/white
    balance gains in linear light, then the chosen transfer. ProRes RAW's camera colour matrix is
    not exposed by FFmpeg, so primaries stay camera-native (reported in the Inspector)."""
    red, green, blue = _gains(settings)
    expr = _TRANSFER_EXPR.get(str(settings.get('gamma') or 'rec709'), _TRANSFER_EXPR['rec709'])
    chain = ['format=rgb48le', f'colorchannelmixer=rr={red:.6f}:gg={green:.6f}:bb={blue:.6f}']
    if expr != 'val':
        chain.append(f"lutrgb=r='{expr}':g='{expr}':b='{expr}'")
    return ','.join(chain)


def _decode_prores_raw(path: Path, out_path: Path, settings: dict, info: dict, *, is_intermediate: bool,
                       seek_optimized=False, audio_stream=None, start_frame=0, frame_count=None, job=None,
                       input_args: list[str] | None = None):
    """Apple ProRes RAW through FFmpeg 8's native ``prores_raw`` decoder, streamed as rgb48le.
    ``input_args`` replaces the decoder input (tests feed a Bayer rawvideo stand-in)."""
    job = job or _Job()
    fps = Fraction(info.get('rate') or '24/1')
    total = max(1, round(float(info.get('duration') or 0) * fps))
    start, count = _frame_window(total, start_frame, frame_count)
    width, height = int(info['width']), int(info['height'])
    seek = ['-ss', f'{float(start / fps):.6f}'] if start else []
    source = input_args or ['-c:v', 'prores_raw', '-i', 'file:' + str(path)]
    decoder = _spawn(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '2',
                      '-protocol_whitelist', 'file', *seek, *source, '-map', '0:v:0', '-frames:v', str(count),
                      '-vf', prores_raw_filters(settings), '-s', f'{width}x{height}', '-f', 'rawvideo',
                      '-pix_fmt', 'rgb48le', 'pipe:1'])
    has_audio = bool(info.get('audioStreams')) and input_args is None
    try:
        _ffmpeg_pipe_encode(_read_frames(decoder, width, height, count, 'FFmpeg prores_raw'), width, height, fps,
                            out_path, is_intermediate=is_intermediate, seek_optimized=seek_optimized,
                            audio_path=path if has_audio else None, audio_stream=audio_stream,
                            audio_start=float(start / fps), frame_total=count, job=job)
        _finish(decoder, 'FFmpeg prores_raw')
    finally:
        _reap(decoder)


def _spawn(argv: list[str]):
    """Start a frame-producing decoder. Its stderr goes to a file, so a chatty decoder can never
    fill a pipe and stall while Splice is reading frames from its stdout."""
    errors = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
    except OSError as error:
        errors.close()
        raise RawError(f'Could not start {Path(argv[0]).name}: {error.strerror or error}') from None
    proc.splice_errors = errors
    return proc


def _read_frames(proc, width: int, height: int, count: int, label: str):
    size = width * height * 6
    for index in range(count):
        frame = proc.stdout.read(size)
        if len(frame) != size:
            _finish(proc, label, allow_running=True)
            raise RawError(f'{label} delivered {index} of {count} frames'
                           + (f' (frame {index + 1} was {len(frame)} of {size} bytes)' if frame else ''))
        yield frame


def _finish(proc, label: str, allow_running: bool = False):
    try:
        proc.wait(timeout=5 if allow_running else 120)
    except subprocess.TimeoutExpired:
        if allow_running:
            return
        raise RawError(f'{label} did not exit after delivering its frames') from None
    if proc.returncode:
        proc.splice_errors.seek(0)
        detail = proc.splice_errors.read()[-400:].decode('utf-8', 'replace').strip()
        raise RawError(f'{label} failed (exit {proc.returncode}): {detail}')


def _reap(proc):
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdout:
        proc.stdout.close()
    proc.splice_errors.close()


def _vendor_kind(exe: str) -> str:
    name = Path(exe).name.lower()
    stem = name[:-4] if name.endswith('.exe') else name
    return {'redline': 'redline', 'art-cmd': 'art-cmd', 'dcraw_emu': 'dcraw'}.get(stem, 'splice')


# Option values from each tool's published documentation:
#   REDline: REDCINE-X PRO Operation Guide, "REDline arguments" (--colorSpace, --gammaCurve,
#            --start/--frameCount, --res, --format 1 = TIFF, --iso/--kelvin/--tint/--exposure).
#   art-cmd: ARRI Reference Tool command line user manual (process --start/--duration,
#            --target-colorspace, --output-width/--output-height, --parameters JSON overrides).
#   dcraw_emu: LibRaw samples/dcraw_emu.cpp usage (-o [0-8], -g pow ts, -H, -b, -h, -w, -W, -6 -T -Z).
_REDLINE_COLOR_SPACES = {'red_wide_gamut': '25', 'rec709': '1', 'linear': '1', 'rec2020': '24'}
_REDLINE_GAMMAS = {'log3g10': '34', 'rec709': '1', 'linear': '-1'}
_REDLINE_RES = {'full': '1', 'half': '2', 'quarter': '4', 'eighth': '8'}
_ARTCMD_TARGETS = {
    ('arri_wide_gamut_4', 'arri_logc4'): 'AWG4/D65/LogC4',
    ('arri_wide_gamut_3', 'arri_logc3'): 'AWG3/D65/LogC3',
    ('rec709', 'rec709'): 'Rec.709/D65/BT.1886',
    ('linear', 'rec709'): 'Rec.709/D65/BT.1886',
    ('rec2020', 'rec709'): 'Rec.2020/D65/BT.1886',
}
_DCRAW_COLOR_SPACES = {'rec709': '1', 'linear': '1', 'rec2020': '8'}
_DCRAW_GAMMAS = {'rec709': ('2.222', '4.5'), 'linear': ('1', '1')}
RAW_SETTING_KEYS = ('iso', 'exposure', 'kelvin', 'tint', 'colorSpace', 'gamma', 'highlightRecovery', 'debayer')
_PRORES_RAW_GAMMAS = ('rec709', 'linear', 'cinemadng_log')
_TOOL_NAMES = {'redline': 'REDline', 'art-cmd': 'art-cmd', 'dcraw': 'dcraw_emu', 'prores_raw': 'FFmpeg prores_raw'}


def decoder_capabilities(kind: str | None) -> dict | None:
    """Which RAW settings the decode route ``kind`` applies: ``settings`` (keys of
    ``RAW_SETTING_KEYS``), and where only some values work, ``colorSpaces`` / ``gammas`` and
    ``pairs`` (colour space + gamma combinations). ``None``: every setting (Splice's own CinemaDNG
    developer, or a bridge speaking the Splice RAW decoder contract) or no decoder yet."""
    if kind in (None, 'builtin', 'splice'):
        return None
    if kind == 'redline':
        return {'tool': 'REDline', 'settings': ['iso', 'exposure', 'kelvin', 'tint', 'colorSpace', 'gamma', 'debayer'],
                'colorSpaces': sorted(_REDLINE_COLOR_SPACES), 'gammas': sorted(_REDLINE_GAMMAS), 'pairs': None}
    if kind == 'art-cmd':
        return {'tool': 'art-cmd', 'settings': ['iso', 'kelvin', 'tint', 'colorSpace', 'gamma', 'debayer'],
                'colorSpaces': sorted({cs for cs, _ in _ARTCMD_TARGETS}),
                'gammas': sorted({gamma for _, gamma in _ARTCMD_TARGETS}),
                'pairs': [list(pair) for pair in _ARTCMD_TARGETS]}
    if kind == 'dcraw':
        return {'tool': 'dcraw_emu', 'settings': ['iso', 'exposure', 'colorSpace', 'gamma', 'highlightRecovery', 'debayer'],
                'colorSpaces': sorted(_DCRAW_COLOR_SPACES), 'gammas': sorted(_DCRAW_GAMMAS), 'pairs': None}
    if kind == 'prores_raw':
        return {'tool': 'FFmpeg prores_raw', 'settings': ['iso', 'exposure', 'kelvin', 'tint', 'gamma', 'debayer'],
                'colorSpaces': None, 'gammas': list(_PRORES_RAW_GAMMAS), 'pairs': None}
    return None


def unapplied_settings(settings: dict, capabilities: dict | None) -> list[str]:
    """The keys of ``settings`` (a clip's ``rawSettings``) its decoder cannot apply."""
    if not capabilities or not isinstance(settings, dict):
        return []
    allowed = set(capabilities.get('settings') or ())
    out = []
    for key in RAW_SETTING_KEYS:
        value = settings.get('debayerQuality' if key == 'debayer' and 'debayer' not in settings else key)
        if value is None:
            continue
        if key not in allowed:
            out.append(key)
        elif key == 'colorSpace' and capabilities.get('colorSpaces') and value not in capabilities['colorSpaces']:
            out.append(key)
        elif key == 'gamma' and capabilities.get('gammas') and value not in capabilities['gammas']:
            out.append(key)
    pairs = capabilities.get('pairs')
    if pairs and 'colorSpace' not in out and 'gamma' not in out and \
            [settings.get('colorSpace'), settings.get('gamma')] not in pairs:
        out += ['colorSpace', 'gamma']
    return out


def _decoder_kind(fmt: str, backend: dict | None) -> str | None:
    if fmt == 'prores_raw':
        return 'prores_raw'
    if not backend or not backend.get('available'):
        return None
    if backend.get('activeBackend') == 'builtin':
        return 'builtin'
    return _vendor_kind(backend['path']) if backend.get('path') else None


def _require_free_space(directory: Path, needed: int, what: str):
    """Fail before writing ``what`` (about ``needed`` bytes) to ``directory`` if it cannot fit."""
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return
    margin = 512 * 1024 ** 2
    if free < needed + margin:
        gib = 1024 ** 3
        raise RawError(f'Not enough free disk space for {what}: it needs about {(needed + margin) / gib:.1f} GB in '
                       f'{directory}, and {free / gib:.1f} GB is free. Free some space, or shorten the export range.')


def intermediate_bytes(width: int, height: int, frames: int) -> int:
    """A cautious size for a lossless FFV1 16-bit 4:4:4 intermediate (camera pictures compress to
    roughly 50-70% of their 6 bytes per pixel)."""
    return int(max(2, width) * max(2, height) * 6 * max(1, frames) * 0.7)


def _kill_tree(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name != 'nt':
            os.killpg(proc.pid, 9)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_tool(argv: list[str], job: _Job, label: str, timeout: float):
    """Run a decoder or encoder to completion while polling ``job``: a cancelled export kills the
    tool and everything it started within about 0.1 s, and leaves no process behind."""
    errors = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                start_new_session=os.name != 'nt')
    except OSError as error:
        errors.close()
        raise RawError(f'Could not start {label}: {error.strerror or error}') from None
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            job.tick()
            if time.monotonic() > deadline:
                raise RawError(f'{label} did not finish within {int(timeout)} s')
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if os.name != 'nt':  # Anything the tool left running in its session goes too.
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        errors.seek(0)
        detail = errors.read()[-400:].decode('utf-8', 'replace').strip()
        errors.close()
    if proc.returncode:
        raise RawError(f'{label} failed (exit {proc.returncode}): {detail}')


def _decode_via_cli(path: Path, out_path: Path, settings: dict, *, backend: dict, info: dict, is_intermediate: bool,
                    seek_optimized=False, audio_stream=None, start_frame=0, frame_count=None, job=None,
                    frame_paths: list[Path] | None = None):
    """Run a detected decoder executable with an argv list (never a shell).

    Executables named ``REDline``, ``art-cmd`` or ``dcraw_emu`` get their own documented options
    and write 16-bit TIFFs to a private staging folder. Any other executable speaks the Splice RAW
    decoder contract: ``--input FILE --start-frame N --frames N --width W --height H`` plus the
    decode settings, writing exactly N rgb48le frames to stdout."""
    job = job or _Job()
    exe = backend.get('path')
    if not exe or backend.get('activeBackend') != 'cli':
        raise RawError(f"No decoder executable is configured for {info.get('rawFormatLabel', 'this RAW format')}. "
                       'Open File > RAW Decoders… to set one.')
    clip = settings.get('decodeUsing') == 'clip'
    quality = 'full' if is_intermediate else str(settings.get('debayerQuality', 'half'))
    scale = DEBAYER_SCALES.get(quality, 1)
    src_w, src_h = int(info.get('width') or 0), int(info.get('height') or 0)
    if src_w < 2 or src_h < 2:
        raise RawError(f'{path.name}: the picture size is unknown, so the decoder output cannot be read')
    out_w = max(2, (src_w // scale) & ~1) if scale > 1 else src_w
    out_h = max(2, (src_h // scale) & ~1) if scale > 1 else src_h
    fps = Fraction(info.get('rate') or '24/1')
    total = max(1, round(float(info.get('duration') or 0) * fps))
    if frame_paths:
        total = len(frame_paths)
    if info.get('still') and not frame_paths:
        # A still is held for as long as the caller asks (the longest clip using it).
        start, count = 0, max(1, int(frame_count) if frame_count else total)
    else:
        start, count = _frame_window(total, start_frame, frame_count)
    source = str(path.resolve())
    kind = _vendor_kind(exe)
    if kind != 'splice':
        return _decode_vendor_tiffs(kind, exe, source, path, out_path, settings, clip, quality, fps, info,
                                    start=start, count=count, total=total, out_size=(out_w, out_h),
                                    is_intermediate=is_intermediate, seek_optimized=seek_optimized, job=job,
                                    frame_paths=frame_paths, audio_stream=audio_stream)
    argv = [exe, '--input', source, '--pix-fmt', 'rgb48le', '--width', str(out_w), '--height', str(out_h),
            '--start-frame', str(start), '--frames', str(count), '--decode-using', 'clip' if clip else 'camera',
            '--iso', str(int(settings.get('iso', 800))), '--exposure', f"{float(settings.get('exposure', 0.0)):.4f}",
            '--kelvin', str(int(settings.get('kelvin', 5600))), '--tint', f"{float(settings.get('tint', 0.0)):.2f}",
            '--colorspace', str(settings.get('colorSpace', 'rec709')), '--gamma', str(settings.get('gamma', 'rec709')),
            '--highlight-recovery', '1' if settings.get('highlightRecovery') else '0', '--quality', quality,
            '--output', '-']
    decoder = _spawn(argv)
    audio = Path(info['rawAudioPath']) if info.get('rawAudioPath') else (path if info.get('audioStreams') else None)
    label = f'RAW decoder {Path(exe).name}'
    try:
        _ffmpeg_pipe_encode(_read_frames(decoder, out_w, out_h, count, label), out_w, out_h, fps, out_path,
                            is_intermediate=is_intermediate, seek_optimized=seek_optimized, audio_path=audio,
                            audio_stream=audio_stream, audio_start=float(start / fps), frame_total=count, job=job)
        _finish(decoder, label)
    finally:
        _reap(decoder)


def _artcmd_input(path: Path, info: dict) -> str:
    """art-cmd reads a legacy ``.ari``/``.arx`` sequence from a printf pattern; a clip file as is."""
    meta = info.get('rawMetadata') or {}
    pattern = raw_bayer.sequence_pattern(path.name) if path.suffix.lower() in ('.ari', '.arx') else None
    if pattern and meta.get('isSequence'):
        prefix, digits, _ = pattern
        return str(path.resolve().parent / f'{prefix}%0{len(digits)}d{path.suffix}')
    return str(path.resolve())


def vendor_argv(kind: str, exe: str, source: str, staging: Path, settings: dict, *, quality: str, start: int,
                count: int, out_size: tuple[int, int], path: Path | None = None, info: dict | None = None) -> list[str]:
    """The documented command line that makes ``kind`` (``redline``, ``art-cmd`` or ``dcraw``) write
    frames ``start .. start+count-1`` of ``source`` as 16-bit TIFFs into ``staging``, with every RAW
    setting that tool accepts. Settings it cannot take are left out (``decoder_capabilities``)."""
    clip = settings.get('decodeUsing') == 'clip'
    color_space = str(settings.get('colorSpace') or 'rec709')
    gamma = str(settings.get('gamma') or 'rec709')
    if kind == 'redline':
        argv = [exe, '--i', source, '--outDir', str(staging), '--o', 'frame', '--format', '1',
                '--res', _REDLINE_RES.get(quality, '2'), '--start', str(start), '--frameCount', str(count)]
        if color_space in _REDLINE_COLOR_SPACES:
            argv += ['--colorSpace', _REDLINE_COLOR_SPACES[color_space]]
        if gamma in _REDLINE_GAMMAS:
            argv += ['--gammaCurve', _REDLINE_GAMMAS[gamma]]
        if clip:
            argv += ['--iso', str(int(settings.get('iso', 800))), '--kelvin', str(int(settings.get('kelvin', 5600))),
                     '--tint', f"{float(settings.get('tint', 0.0)):.2f}",
                     '--exposure', f"{float(settings.get('exposure', 0.0)):.3f}"]
        return argv
    if kind == 'art-cmd':
        argv = [exe, 'process', '--input', _artcmd_input(path or Path(source), info or {}),
                '--output', str(staging / 'frame.%07d.tif'), '--start', str(start), '--duration', str(count)]
        target = _ARTCMD_TARGETS.get((color_space, gamma))
        if target:
            argv += ['--target-colorspace', target]
        if quality != 'full':
            argv += ['--output-width', str(out_size[0]), '--output-height', str(out_size[1])]
        if clip:
            overrides = {'exposureIndexOverride': int(settings.get('iso', 800)),
                         'colorTemperatureOverride': int(settings.get('kelvin', 5600)),
                         'tintOverride': float(settings.get('tint', 0.0))}
            parameters = staging / 'splice-parameters.json'
            parameters.write_text(json.dumps(overrides), encoding='utf-8')
            argv += ['--parameters', str(parameters)]
        return argv
    # dcraw_emu develops one still. -W keeps it from auto-brightening, so exposure and ISO are exact
    # brightness gains (-b); white balance is the camera's (-w): it takes no Kelvin or tint.
    gain = 1.0
    if clip:
        gain = (2.0 ** float(settings.get('exposure', 0.0))) * (max(50.0, float(settings.get('iso', 800))) / 800.0)
    argv = [exe, '-6', '-T', '-W', '-w', '-b', f'{gain:.6f}', '-o', _DCRAW_COLOR_SPACES.get(color_space, '1'),
            '-g', *_DCRAW_GAMMAS.get(gamma, _DCRAW_GAMMAS['rec709']), '-H', '2' if settings.get('highlightRecovery') else '0']
    if quality != 'full':
        argv.append('-h')
    return argv + ['-Z', str(staging / 'frame.tiff'), source]


def _decode_vendor_tiffs(kind, exe, source, path, out_path, settings, clip, quality, fps, info, *, start, count,
                         total, out_size, is_intermediate, seek_optimized, job, frame_paths=None, audio_stream=None):
    """REDline / ARRI Reference Tool / dcraw_emu: decode the window's frames to 16-bit TIFFs in a
    private staging folder, then encode them. A tool that writes the whole clip instead of the
    window is trimmed to it, so the cut always gets the frames it reads. dcraw_emu develops one
    still per run: once for a still, once per frame of a (compressed) DNG sequence's window."""
    timeout = max(600, int(float(info.get('duration') or 0) * 60))
    out_w, out_h = out_size
    dcraw_sources = [Path(f) for f in frame_paths[start:start + count]] if kind == 'dcraw' and frame_paths else None
    # A still (one ARRIRAW frame, a camera RAW photo) is developed once and held for ``count`` frames.
    hold = kind == 'dcraw' and not dcraw_sources or (bool(info.get('still')) and not frame_paths)
    tool_start, tool_count = (0, 1) if hold else (start, count)
    frames_written = len(dcraw_sources) if dcraw_sources else tool_count
    _require_free_space(out_path.parent, out_w * out_h * 6 * frames_written + 1024 ** 2 * frames_written,
                        f"{_TOOL_NAMES[kind]}'s 16-bit TIFF frames")
    with tempfile.TemporaryDirectory(prefix='.splice-raw-', dir=out_path.parent) as staging:
        staging = Path(staging)
        if dcraw_sources:
            for index, frame in enumerate(dcraw_sources):
                job.tick()
                argv = vendor_argv(kind, exe, str(frame.resolve()), staging, settings, quality=quality, start=0,
                                   count=1, out_size=out_size)
                argv[argv.index('-Z') + 1] = str(staging / f'frame.{index:07d}.tiff')
                _run_tool(argv, job, Path(exe).name, timeout)
        else:
            argv = vendor_argv(kind, exe, source, staging, settings, quality=quality, start=tool_start,
                               count=tool_count, out_size=out_size, path=path, info=info)
            job.tick()
            _run_tool(argv, job, Path(exe).name, timeout)
        job.tick()
        files = _staged_frames(staging)
        if dcraw_sources and len(files) != len(dcraw_sources):
            raise RawError(f'{Path(exe).name} developed {len(files)} of {len(dcraw_sources)} frames')
        if hold:
            files = files[:1]
        elif not dcraw_sources:
            if len(files) == count:
                pass
            elif len(files) >= total and start + count <= len(files):
                files = files[start:start + count]
            else:
                raise RawError(f'{Path(exe).name} wrote {len(files)} frames; the export needs source frames '
                               f'{start}-{start + count - 1} of {total}')
        audio = Path(info['rawAudioPath']) if info.get('rawAudioPath') else (path if info.get('audioStreams') else None)
        _encode_decoded_stills(staging, files, out_path, fps, is_intermediate=is_intermediate,
                               seek_optimized=seek_optimized, frames=count, job=job, audio_path=audio,
                               audio_stream=audio_stream, audio_start=float(start / fps))


def _staged_frames(staging: Path) -> list[Path]:
    return sorted(p for p in staging.rglob('*') if p.is_file() and p.suffix.lower() in ('.tif', '.tiff', '.dpx', '.png'))


def _encode_decoded_stills(staging: Path, files: list[Path], out_path: Path, fps: Fraction, *, is_intermediate: bool,
                           seek_optimized=False, frames: int = 1, job: _Job | None = None, audio_path=None,
                           audio_stream=None, audio_start: float = 0.0):
    job = job or _Job()
    audio_in, audio_map = _audio_input(audio_path, audio_stream, audio_start)
    if not files:
        raise RawError('The RAW decoder produced no frames')
    if len(files) > 1:
        # A numbered frame list in its own folder: FFmpeg's image2 sequence reader plays it in order.
        listing = staging / 'splice-frames'
        listing.mkdir()
        for index, file in enumerate(files):
            os.link(file, listing / f'{index:07d}{file.suffix.lower()}')
        source = ['-f', 'image2', '-framerate', _rational_str(fps), '-start_number', '0',
                  '-i', str(listing / f'%07d{files[0].suffix.lower()}')]
    else:
        source = ['-loop', '1', '-framerate', _rational_str(fps), '-i', str(files[0])]
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-protocol_whitelist', 'file', *source,
           *audio_in, *_output_args(is_intermediate, seek_optimized, audio_map is not None, audio_map or '', fps),
           '-frames:v', str(max(1, frames)), '-t', f'{float(max(1, frames) / fps):.6f}', str(out_path)]
    try:
        _run_tool(cmd, job, 'FFmpeg', 3600)
    except RawError as error:
        if 'FFmpeg failed' in str(error):
            raise RawError('Encoding the decoded RAW frames failed: ' + str(error).split(': ', 1)[-1]) from None
        raise


def _generate_missing_decoder_proxy(path: Path, out_path: Path, info: dict, audio_stream: int | None = None):
    """A slate proxy for RAW Splice cannot decode yet: right duration, frame rate and aspect, the
    clip's real sound, and a dark picture, so the clip can be cut by sound and timecode while the
    Inspector explains how to add a decoder or attach a proxy."""
    src_w, src_h = int(info.get('width') or 1280), int(info.get('height') or 720)
    scale = min(1.0, 640 / max(1, src_w), 360 / max(1, src_h))
    width = max(64, (int(round(src_w * scale)) // 2) * 2)
    height = max(64, (int(round(src_h * scale)) // 2) * 2)
    fps = info.get('rate') or '24/1'
    duration = max(0.2, float(info.get('duration') or 5.0))
    sound = None
    if info.get('rawAudioPath') and Path(info['rawAudioPath']).is_file():
        sound = Path(info['rawAudioPath'])
    elif info.get('audioStreams'):
        sound = path
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-threads', '2',
           '-protocol_whitelist', 'file', '-f', 'lavfi', '-i', f'color=c=0x1b221b:s={width}x{height}:r={fps}:d={duration:.6f}']
    if sound:
        cmd += ['-i', 'file:' + str(sound)]
    cmd += ['-map', '0:v:0', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26', '-g', '24', '-pix_fmt', 'yuv420p',
            '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    stream = audio_stream if isinstance(audio_stream, int) and 0 <= audio_stream < 16 else 0
    cmd += ['-map', f'1:a:{stream}?', '-c:a', 'aac', '-b:a', '160k', '-ac', '2'] if sound else ['-an']
    cmd += ['-t', f'{duration:.6f}', '-movflags', '+faststart', str(out_path)]
    result = subprocess.run(cmd, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RawError('Could not create the RAW slate proxy: ' + result.stderr.decode('utf-8', 'replace')[-300:])


def _decoder_for(fmt: str):
    if fmt == 'cinemadng':
        return _decode_cinemadng
    if fmt == 'prores_raw':
        return _decode_prores_raw
    backend = _resolve_backend('libraw' if fmt == 'still_raw' else fmt)
    if not backend['available']:
        return None

    def run(path, out_path, settings, info, **options):
        return _decode_via_cli(path, out_path, settings, backend=backend, info=info, **options)
    return run


def decode_to_proxy(path, out_path, raw_settings: dict | None = None, *, info: dict | None = None,
                    seek_optimized: bool = False, audio_stream: int | None = None):
    """Decode a camera RAW source with ``raw_settings`` into a playable Rec.709 H.264 proxy."""
    path, out_path = Path(path), Path(out_path)
    info = info or probe_raw(path)
    settings = normalize_raw_settings(raw_settings, fmt=info['rawFormat'], defaults=info.get('rawDefaults'))
    decoder = _decoder_for(info['rawFormat']) if info.get('rawDecoderAvailable') else None
    if decoder is None:
        return _generate_missing_decoder_proxy(path, out_path, info, audio_stream=audio_stream)
    decoder(path, out_path, settings, info, is_intermediate=False, seek_optimized=seek_optimized,
            audio_stream=audio_stream)


def decode_to_intermediate(path, out_path, raw_settings: dict | None = None, *, info: dict | None = None,
                           audio_stream: int | None = None, start_frame: int = 0, frame_count: int | None = None,
                           cancelled=None, heartbeat=None):
    """Decode frames ``start_frame .. start_frame+frame_count`` of a camera RAW source at full
    resolution in 16-bit RGB with ``raw_settings`` into a lossless FFV1 QuickTime intermediate."""
    path, out_path = Path(path), Path(out_path)
    info = info or probe_raw(path)
    settings = dict(normalize_raw_settings(raw_settings, fmt=info['rawFormat'], defaults=info.get('rawDefaults')),
                    debayerQuality='full')
    decoder = _decoder_for(info['rawFormat']) if info.get('rawDecoderAvailable') else None
    if decoder is None:
        raise RawError(info.get('rawDecoderMessage') or _missing_decoder_message(info['rawFormat'], path.name))
    fps = Fraction(info.get('rate') or '24/1')
    frames = int(frame_count) if frame_count else max(1, round(float(info.get('duration') or 0) * fps))
    _require_free_space(out_path.parent, intermediate_bytes(int(info.get('width') or 0), int(info.get('height') or 0), frames),
                        f'the full-resolution 16-bit intermediate of {path.name}')
    decoder(path, out_path, settings, info, is_intermediate=True, audio_stream=audio_stream,
            start_frame=start_frame, frame_count=frame_count, job=_Job(cancelled, heartbeat))


def sequence_fingerprint(path) -> str:
    """Fingerprint for a CinemaDNG/ARRIRAW sequence so added or changed frames refresh its proxies."""
    path = Path(path)
    if path.suffix.lower() in ('.dng', '.ari', '.arx'):
        seq = raw_bayer.detect_dng_sequence(path)
        if seq['isSequence']:
            first, last = seq['frames'][0].stat(), seq['frames'][-1].stat()
            wav = Path(seq['audioPath']).stat().st_mtime_ns if seq['audioPath'] else 0
            return f"seq:{seq['frameCount']}:{first.st_mtime_ns}:{last.st_mtime_ns}:{last.st_size}:{wav}"
    stat = path.stat()
    return f'file:{stat.st_size}:{stat.st_mtime_ns}'
