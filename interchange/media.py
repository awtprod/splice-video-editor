"""Where a Splice source lives on disk, how to name it in a timeline file, and what its file says
about itself (start timecode, rate, reel, audio layout).

A media item may already carry the metadata contract fields (``sourcePath``, ``rate``,
``timecode``, ``dropFrame``, ``reel``, ``vfr``, ``audioStreams`` as an integer count of audio
streams and ``audioStreamDetails`` as ``[{index, channels, layout, codec}]``). The original file is
probed with ffprobe only for fields that are genuinely missing (``describe()`` carries no sample
rate or Broadcast WAV time reference, so sound-only sources are probed for those), and all
probes of one export share a :class:`ProbeBudget` (a count and a total time), so an export is
right on its own and never stalls a request. A list in ``audioStreams`` (an earlier draft of the contract) is read too.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote, unquote, urlsplit

from .timecode import is_drop_frame, parse_rate, snap_rate, timecode_frames

PROBE_TIMEOUT = 20
MAX_PROBES = 500
#: total seconds of probing one export may spend; sources past it are listed in the report
PROBE_SECONDS = 60.0
STILL_EXTENSIONS = frozenset({'.png', '.bmp', '.jpg', '.jpeg', '.tif', '.tiff'})
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_DRIVE = re.compile(r'^([A-Za-z]):[\\/](.*)$', re.S)
_UNC = re.compile(r'^\\\\([^\\/]+)[\\/](.*)$', re.S)


def clean(value, limit=240) -> str:
    """Text safe for XML 1.0, JSON and EDL comments: control characters removed, length bounded."""
    return _CONTROL.sub('', str(value if value is not None else ''))[:limit]


# -- paths and URLs ---------------------------------------------------------------------------

def is_absolute_path(path: str) -> bool:
    return bool(path) and (path.startswith('/') or bool(_DRIVE.match(path)) or bool(_UNC.match(path)))


def file_url(path: str) -> str:
    """An absolute Windows, UNC, macOS or Linux path as a percent-encoded ``file:`` URL."""
    drive = _DRIVE.match(path)
    if drive:
        parts = re.split(r'[\\/]', drive.group(2))
        return f'file:///{drive.group(1).upper()}:/' + '/'.join(quote(p, safe='') for p in parts)
    unc = _UNC.match(path)
    if unc:
        parts = re.split(r'[\\/]', unc.group(2))
        return f'file://{quote(unc.group(1), safe="")}/' + '/'.join(quote(p, safe='') for p in parts)
    if path.startswith('/'):
        return 'file://' + quote(path, safe='/')
    raise ValueError(f'{path!r} is not an absolute path')


def path_from_url(value: str) -> str:
    """A timeline file's media reference as a local path string: ``file:`` URLs (percent-decoded;
    ``file:///C:/x``, ``file://localhost/x``, ``file://host/share/x`` as UNC) or a bare path.
    Raises ``ValueError`` for other schemes and NUL bytes."""
    text = str(value or '').strip()
    if not text or len(text) > 4096:
        raise ValueError('Media reference is empty or too long')
    if text.lower().startswith('file:'):
        parts = urlsplit(text)
        path = unquote(parts.path)
        host = parts.netloc
        if host and host.lower() != 'localhost':
            path = '\\\\' + unquote(host) + path.replace('/', '\\')
        elif re.match(r'^/[A-Za-z]:[/\\]', path):
            path = path[1:].replace('/', '\\')
    elif re.match(r'^[A-Za-z][A-Za-z0-9+.-]+://', text):
        raise ValueError(f'Unsupported media URL scheme in {text[:80]!r}')
    else:
        path = unquote(text) if '%' in text and '/' in text else text
    if '\x00' in path:
        raise ValueError('Media reference contains a NUL byte')
    return path


def base_name(path: str) -> str:
    return PureWindowsPath(path).name if ('\\' in path or _DRIVE.match(path)) else PurePosixPath(path).name


# -- probing ----------------------------------------------------------------------------------

@dataclass
class Probe:
    rate: Fraction | None = None
    raw_rate: str | None = None
    vfr: bool = False
    timecode: str | None = None
    duration: Fraction | None = None
    width: int | None = None
    height: int | None = None
    sar: Fraction | None = None          # sample (pixel) aspect ratio
    has_video: bool = False
    has_audio: bool = False
    audio_streams: list = field(default_factory=list)
    audio_rate: int | None = None
    time_reference: int | None = None
    reel: str | None = None


def _tag(tags, *names):
    if not isinstance(tags, dict):
        return None
    for key, value in tags.items():
        if isinstance(key, str) and key.lower() in names and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_sar(value) -> Fraction | None:
    """``'N:D'`` (ffprobe), ``'N/D'`` or a number as a pixel aspect ratio; None when unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = Fraction(repr(float(value))) if math.isfinite(value) else None
    elif isinstance(value, str) and re.fullmatch(r'\s*\d{1,6}\s*[:/]\s*\d{1,6}\s*', value):
        top, bottom = (int(x) for x in re.split(r'[:/]', value))
        number = Fraction(top, bottom) if top and bottom else None
    else:
        return None
    return number if number is not None and Fraction(1, 10) <= number <= 10 else None


def parse_probe(data: dict) -> Probe:
    streams = [s for s in data.get('streams') or [] if isinstance(s, dict)]
    fmt = data.get('format') or {}
    video = next((s for s in streams if s.get('codec_type') == 'video'
                  and not (s.get('disposition') or {}).get('attached_pic')), None)
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    result = Probe(has_video=video is not None, has_audio=bool(audio))
    if video is not None:
        average, base = parse_rate(video.get('avg_frame_rate')), parse_rate(video.get('r_frame_rate'))
        result.raw_rate = video.get('avg_frame_rate') or video.get('r_frame_rate')
        result.rate = snap_rate(average) or snap_rate(base)
        result.vfr = bool(average and base and abs(average - base) > base / 1000) or result.rate is None
        result.width, result.height = video.get('width'), video.get('height')
        result.sar = parse_sar(video.get('sample_aspect_ratio'))
    timecodes = [_tag(video.get('tags') if video else None, 'timecode')]
    timecodes += [_tag(s.get('tags'), 'timecode') for s in streams if s is not video]
    timecodes.append(_tag(fmt.get('tags'), 'timecode'))
    result.timecode = next((tc for tc in timecodes if tc), None)
    result.reel = _tag(fmt.get('tags'), 'reel_name', 'reel', 'com.apple.proapps.reel') or \
        _tag(video.get('tags') if video else None, 'reel_name', 'reel')
    reference = _tag(fmt.get('tags'), 'time_reference')
    if reference and reference.isdigit():
        result.time_reference = int(reference)
    for index, stream in enumerate(audio[:64]):
        result.audio_streams.append({'index': index, 'channels': int(stream.get('channels') or 0),
                                     'layout': stream.get('channel_layout') or '', 'codec': stream.get('codec_name') or ''})
    if audio:
        try:
            result.audio_rate = int(audio[0].get('sample_rate') or 0) or None
        except ValueError:
            result.audio_rate = None
    try:
        duration = Fraction(str(fmt.get('duration') or (video or {}).get('duration') or '0'))
        result.duration = duration if 0 < duration <= 86400 * 2 else None
    except (ValueError, ZeroDivisionError):
        result.duration = None
    return result


class ProbeBudget:
    """Probes one export may still run: at most ``count`` files and ``seconds`` in total."""

    def __init__(self, count: int = MAX_PROBES, seconds: float = PROBE_SECONDS):
        import time
        self._clock = time.monotonic
        self.count = max(0, int(count))
        self.deadline = self._clock() + max(0.0, float(seconds))
        self.seconds = seconds
        self.skipped = []       # names of sources left unprobed because the budget ran out

    def remaining(self) -> float:
        return max(0.0, self.deadline - self._clock())

    def take(self) -> float | None:
        """Seconds the next probe may take, or None when the budget is spent."""
        left = self.remaining()
        if self.count <= 0 or left < 0.5:
            return None
        self.count -= 1
        return min(PROBE_TIMEOUT, left)


def probe_file(path: Path, timeout: float = PROBE_TIMEOUT) -> Probe:
    """ffprobe one local regular file. Raises ``ValueError`` when it is unreadable."""
    if not path.is_file():
        raise ValueError(f'{path.name} is not a readable file')
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
                                 '-show_streams', '-show_format', '-of', 'json', 'file:' + str(path)],
                                capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise ValueError(f'Could not probe {path.name}: {error}') from None
    if result.returncode:
        raise ValueError(f'ffprobe could not read {path.name}')
    try:
        return parse_probe(json.loads(result.stdout))
    except (json.JSONDecodeError, TypeError, AttributeError) as error:
        raise ValueError(f'ffprobe returned unreadable data for {path.name}: {error}') from None


# -- one Splice media item ----------------------------------------------------------------------

@dataclass
class Source:
    """Everything a writer needs about one source, in exact terms."""
    id: str
    name: str
    path: str | None            # absolute path written into the timeline file
    local: Path | None          # readable file on this machine, if any
    kind: str                   # 'video' or 'audio'
    still: bool = False
    rate: Fraction | None = None        # picture rate (snapped)
    timecode: str | None = None         # start timecode label as found
    drop: bool = False
    tc_frames: int | None = None        # start timecode as frames at tc_rate
    tc_rate: Fraction | None = None
    start: Fraction = Fraction(0)       # media start in seconds (the asset start)
    timecode_state: str = 'unknown'     # 'embedded', 'none' (file has no tag), 'unknown'
    duration: Fraction = Fraction(0)
    width: int | None = None
    height: int | None = None
    sar: Fraction | None = None         # sample aspect ratio; None = square / unknown
    has_video: bool = True
    has_audio: bool = False
    audio_streams: list = field(default_factory=list)
    audio_rate: int | None = None
    reel: str | None = None
    vfr: bool = False
    notes: list = field(default_factory=list)
    nested: bool = False
    generator: bool = False
    location: str = 'none'      # see Reference.kind
    original_path: str | None = None   # path on this machine, when export path mapping rewrote it
    path_rule: int | None = None        # index of the mapping rule that applied

    @property
    def channels(self) -> int:
        return sum(int(s.get('channels') or 0) for s in self.audio_streams) if self.audio_streams else (2 if self.has_audio else 0)


def _registry():
    """The server's link-in-place registry (``media_links``); None outside a Splice checkout."""
    try:
        import media_links
    except ImportError:
        return None
    return media_links


def _is_proxy(path: Path) -> bool:
    """A file the server derived (edit/seek/grade proxy, nested preview) rather than a user's media."""
    registry = _registry()
    if registry is None:
        return False
    try:
        directory = path.parent
        if not directory.parent.resolve() == registry.MEDIA_ROOT.resolve():
            return False
        names = sorted(p.name for p in directory.iterdir() if p.is_file() and not p.name.startswith('.'))
        return registry._kind(directory, names) == 'proxy'
    except OSError:
        return False


@dataclass
class Reference:
    """Which file a timeline file should name for one media item, and whether it is readable here."""
    path: str | None
    local: Path | None
    kind: str           # 'linked', 'managed', 'bundled', 'external' or 'none'
    note: str | None = None


def media_reference(item: dict) -> Reference:
    """The original behind a media item, never Splice's internal URL and never a proxy.

    A linked original (``/media/linked/<id>/<name>``) is named by the absolute path recorded in the
    link-in-place registry, even while its drive is disconnected. Uploaded media is named by the
    managed copy's real path. ``originalSrc`` is the original when ``src`` is a proxy; a proxy
    itself is skipped. Paths the item carries (``sourcePath``, ``path``) are used only when no
    Splice URL resolves, for example for offline media from an imported timeline."""
    registry = _registry()
    proxy_only = False
    for key in ('originalSrc', 'src'):
        value = item.get(key)
        if not isinstance(value, str) or not value or registry is None:
            continue
        if registry.is_linked(value):
            try:
                _, entry = registry._entry(value)
            except ValueError:
                continue
            try:
                local = registry.resolve_media(value)
            except ValueError:
                local = None
            return Reference(entry['path'], local, 'linked', None if local else
                             'the linked original is offline on this machine (moved or its drive is disconnected); '
                             'its recorded path is written')
        try:
            from premiere import _media_path
            resolved = _media_path(value)
        except (ImportError, ValueError, OSError):
            continue
        if _is_proxy(resolved):
            proxy_only = True
            continue
        kind = 'managed' if resolved.is_relative_to(registry.MEDIA_ROOT.resolve()) else 'bundled'
        return Reference(str(resolved), resolved, kind)
    for key in ('sourcePath', 'path'):
        value = item.get(key)
        if isinstance(value, str) and is_absolute_path(value):
            local = Path(value) if not value.startswith('\\\\') and Path(value).is_file() else None
            if local is not None and _is_proxy(local):
                proxy_only = True
                continue
            return Reference(value, local, 'external')
    if proxy_only:
        return Reference(None, None, 'none', 'only an edit proxy is on record; proxies are never turned over, '
                                             'so relink the original')
    return Reference(None, None, 'none')


def _stream_list(value) -> list | None:
    if not isinstance(value, list):
        return None
    streams = []
    for index, stream in enumerate(value[:64]):
        if isinstance(stream, dict):
            try:
                channels = max(0, min(64, int(stream.get('channels') or 0)))
            except (TypeError, ValueError):
                channels = 0
            streams.append({'index': stream.get('index', index), 'channels': channels,
                            'layout': clean(stream.get('layout'), 40), 'codec': clean(stream.get('codec'), 40)})
    return streams


def _sample_rate(item: dict) -> int | None:
    for value in [item.get('audioSampleRate'), item.get('sampleRate')] + [d.get('sampleRate') for d in (
            item.get('audioStreamDetails') if isinstance(item.get('audioStreamDetails'), list) else []) if isinstance(d, dict)]:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 1000 <= value <= 768000:
            return int(value)
    return None


def _dimension(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return int(value) if 1 <= value <= 16384 else None


def _stream_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(64, int(value)))


def resolve_source(item: dict, sequence_rate: Fraction, budget: ProbeBudget) -> Source:
    """Describe one Splice media item; probing, when a field is missing, spends ``budget``."""
    name = clean(item.get('name') or item.get('id') or 'Media') or 'Media'
    source = Source(id=str(item.get('id')), name=name, path=None, local=None,
                    kind='audio' if item.get('type') == 'audio' else 'video')
    if isinstance(item.get('nestedSequence'), int):
        source.nested = True
        return source
    if item.get('nativeGenerator') is not None or item.get('nativeUnsupported'):
        source.generator = True
        return source
    reference = media_reference(item)
    local = reference.local
    source.local, source.path, source.location = local, reference.path, reference.kind
    if reference.note:
        source.notes.append(reference.note)
    suffix = Path(base_name(source.path or name)).suffix.lower()
    source.still = bool(item.get('still')) or suffix in STILL_EXTENSIONS
    details = _stream_list(item.get('audioStreamDetails'))
    if details is None:
        details = _stream_list(item.get('audioStreams'))      # earlier draft: the list itself
    count = _stream_count(item.get('audioStreams'))
    # a count with no per-stream list leaves the channel layout open: probe it when the file is here
    audio_known = details is not None or count == 0
    rate_known = bool(snap_rate(item.get('rate'))) or source.kind == 'audio' or source.still
    timecode_known = 'timecode' in item
    # the pixel shape decides the scale that reproduces Splice's fitted picture in FCP 7 XML
    # (a record with a frame size and no sampleAspectRatio is Splice's own: square pixels)
    shape_known = source.kind != 'video' or source.still or parse_sar(item.get('sampleAspectRatio')) is not None or \
        bool(_dimension(item.get('width')) and _dimension(item.get('height')))
    probe = None
    sample_rate = _sample_rate(item)
    # describe() records neither the sample rate nor a Broadcast WAV time reference, so a sound
    # file (music, location sound) is probed for them; picture sources need no probe for these
    sound_known = sample_rate is not None or source.kind != 'audio'
    needs_probe = not (rate_known and audio_known and timecode_known and sound_known and shape_known)
    if needs_probe and local is not None:
        timeout = budget.take()
        if timeout is None:
            budget.skipped.append(name)
            source.notes.append(f'not probed: this export\'s probe budget ({budget.seconds:g} s, {MAX_PROBES} files) ran out')
        else:
            try:
                probe = probe_file(local, timeout)
            except ValueError as error:
                source.notes.append(str(error))
    elif needs_probe and local is None:
        source.notes.append('the original file is not on this machine, so its metadata could not be read')
    # rate
    rate = snap_rate(item.get('rate')) or (probe.rate if probe else None) or snap_rate(item.get('fps'))
    source.vfr = bool(item.get('vfr')) or bool(probe and probe.vfr and probe.has_video)
    if source.vfr:
        source.notes.append('variable frame rate source' + (f' ({probe.raw_rate})' if probe and probe.raw_rate else ''))
    source.has_video = source.kind == 'video' and not (probe is not None and not probe.has_video)
    source.rate = rate if source.has_video and not source.still else None
    # audio layout: the per-stream list when there is one, else the count, else the probe
    if details:
        source.audio_streams = details
    elif probe is not None and probe.audio_streams and not audio_known:
        source.audio_streams = probe.audio_streams
    elif count:
        channels = _stream_count(item.get('audioChannels')) or 2
        source.audio_streams = [{'index': i, 'channels': channels, 'layout': '', 'codec': ''} for i in range(count)]
        if not _stream_count(item.get('audioChannels')):
            source.notes.append(f'{count} audio stream{"s" if count > 1 else ""} with no channel layout on record; '
                                'written as stereo')
    elif isinstance(item.get('audioStreamChannels'), list):
        source.audio_streams = [{'index': i, 'channels': _stream_count(c) or 0, 'layout': '', 'codec': ''}
                                for i, c in enumerate(item['audioStreamChannels'][:64])]
    elif _stream_count(item.get('audioChannels')):
        source.audio_streams = [{'index': 0, 'channels': _stream_count(item['audioChannels']), 'layout': '', 'codec': ''}]
    source.has_audio = bool(source.audio_streams) or source.kind == 'audio'
    source.audio_rate = (probe.audio_rate if probe else None) or sample_rate
    # size and length
    source.width = _dimension(item.get('width')) or (_dimension(probe.width) if probe else None)
    source.height = _dimension(item.get('height')) or (_dimension(probe.height) if probe else None)
    source.sar = parse_sar(item.get('sampleAspectRatio')) or (probe.sar if probe else None)
    duration = (probe.duration if probe else None) or Fraction(repr(float(item.get('duration') or 0)))
    source.duration = duration
    source.reel = clean((item.get('reel') if isinstance(item.get('reel'), str) else None) or (probe.reel if probe else None) or '', 64) or None
    # start timecode
    tc_rate = source.rate or sequence_rate
    label = item.get('timecode') if isinstance(item.get('timecode'), str) and item.get('timecode') else None
    if label is None and probe is not None:
        label = probe.timecode
    if label:
        try:
            drop = bool(item.get('dropFrame')) if 'dropFrame' in item and label == item.get('timecode') else is_drop_frame(label)
            frames = timecode_frames(label, tc_rate, drop)
            source.timecode, source.drop, source.tc_frames, source.tc_rate = label, drop, frames, tc_rate
            source.start = Fraction(frames) / tc_rate
            source.timecode_state = 'embedded'
        except ValueError as error:
            source.notes.append(f'start timecode {label!r} is unreadable: {error}')
    elif probe is not None and probe.time_reference is not None and probe.audio_rate and not source.has_video:
        source.start = Fraction(probe.time_reference, probe.audio_rate)
        source.tc_rate = sequence_rate
        source.tc_frames = int(source.start * sequence_rate)
        source.timecode_state = 'embedded'
        source.notes.append('start time read from the Broadcast WAV time reference')
    elif probe is not None or 'timecode' in item:
        source.timecode_state = 'none'
    return source


# -- path mapping for cross-machine turnovers ---------------------------------------------------

MAX_PATH_RULES = 32


def _windows_style(path: str) -> bool:
    return bool(_DRIVE.match(path) or _UNC.match(path))


def path_rules(value) -> list:
    """Validated ``[(from, to), ...]`` from the export option ``pathMap``: a list of
    ``{'from': prefix, 'to': prefix}``. Both must be absolute paths (POSIX, drive letter or UNC)."""
    if value in (None, '', []):
        return []
    if not isinstance(value, list) or len(value) > MAX_PATH_RULES:
        raise ValueError(f'Path mapping must be a list of up to {MAX_PATH_RULES} rules')
    rules = []
    for rule in value:
        if not isinstance(rule, dict):
            raise ValueError('Each path mapping rule needs a "from" and a "to" path')
        source, target = rule.get('from'), rule.get('to')
        for side, text in (('from', source), ('to', target)):
            if not isinstance(text, str) or not text.strip() or len(text) > 1024 or _CONTROL.search(text) or '\x00' in text:
                raise ValueError(f'Path mapping "{side}" must be a path of up to 1024 characters')
        source, target = source.strip(), target.strip()
        if not is_absolute_path(source) or not is_absolute_path(target):
            raise ValueError(f'Path mapping {source[:60]!r} -> {target[:60]!r}: both sides must be absolute paths '
                             '(/Volumes/..., /mnt/..., X:\\... or \\\\server\\share\\...)')
        rules.append((source, target))
    return rules


def _components(path: str):
    """``(style, parts)``: style 'posix', 'drive' or 'unc'; drive letters upper-cased."""
    if _DRIVE.match(path):
        parts = [p for p in re.split(r'[\\/]', path) if p]
        return 'drive', [parts[0].upper()] + parts[1:]
    if _UNC.match(path):
        return 'unc', [p for p in re.split(r'[\\/]', path) if p]
    return 'posix', [p for p in path.split('/') if p]


def _join(style: str, parts: list) -> str:
    if style == 'drive':
        return parts[0] + '\\' + '\\'.join(parts[1:])
    if style == 'unc':
        return '\\\\' + '\\'.join(parts)
    return '/' + '/'.join(parts)


WINDOWS_RESERVED = re.compile(r'[\\:?*"<>|]')
#: device names Windows reserves in every folder, with or without an extension (``nul.txt`` too)
#: and with spaces before it (Windows drops them: ``con .mov`` is CON); COM0 and LPT0, superscript
#: digits (COM\u00b9) and the console's CONIN$ and CONOUT$ count as well
WINDOWS_DEVICE = re.compile(r'(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3]) *'
                            r'(?:\..*)?', re.IGNORECASE)


def map_path(path: str, rules: list):
    """``(new path, rule index)`` for the rule whose ``from`` is the LONGEST whole-component
    prefix of ``path`` (the most specific folder wins, whatever the rule order; on a tie, the
    earlier rule), else ``(path, None)``. Windows prefixes compare case-insensitively with either
    slash; the rest of the path is written in the ``to`` side's style (backslashes for X:\\ and UNC)."""
    style, parts = _components(path)
    best = None
    for index, (source, target) in enumerate(rules):
        source_style, head = _components(source)
        if source_style != style or len(head) > len(parts):
            continue
        same = (lambda a, b: a.lower() == b.lower()) if style != 'posix' else (lambda a, b: a == b)
        if all(same(a, b) for a, b in zip(head, parts)) and (best is None or len(head) > best[0]):
            best = (len(head), index, target)
    if best is None:
        return path, None
    length, index, target = best
    target_style, base = _components(target)
    return _join(target_style, base + parts[length:]), index


def invalid_on_target(path: str, rule) -> str | None:
    """Why the mapped form of ``path`` cannot exist on the target system, or None. ``rule`` is
    the ``(from, to)`` that applied. A POSIX name may hold characters Windows refuses
    (``\\ : ? * " < > |``); carried under a drive or UNC folder it names a file that cannot exist."""
    if rule is None or not _windows_style(rule[1]) or _windows_style(path):
        return None
    _style, parts = _components(path)
    _source_style, head = _components(rule[0])
    carried = parts[len(head):]
    bad = sorted({ch for part in carried for ch in part if WINDOWS_RESERVED.match(ch)})
    if bad:
        return 'its name contains ' + ' '.join(bad) + ', which Windows does not allow in a path'
    for part in carried:
        if WINDOWS_DEVICE.fullmatch(part):
            return (f'"{part}" is a name Windows reserves for a device (CON, PRN, AUX, NUL, CONIN$, CONOUT$, COM0-9, '
                    'LPT0-9)')
        if part.endswith(('.', ' ')):
            return f'"{part}" ends in a {"dot" if part.endswith(".") else "space"}, which Windows removes from a name'
    return None
