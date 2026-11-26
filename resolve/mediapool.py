"""Media-pool rows for a file Resolve has never imported, authored from its measured facts.

Ported (without pydantic, for Python 3.10+) from ai-agent-resolve ``src/aar/drp/mediapool/``
(``author.py``, ``formulas.py``, ``kinds.py``, ``templates.py``, ``facts.py``) and
``src/aar/media/probe.py`` at commit baa82027bc9212d5b64572b70806f0d69f8812fa (same owner),
where every rule was checked leave-one-out against Resolve 21.1's own rows (docs/DESIGN.md
there, "Media-pool rows (S25c1)").

:func:`author` takes a file's facts (``ffprobe -show_format -show_streams``, its size, its
modification time on the wall clock of the machine Resolve reads it on, its creation time) and a
template clip of the same kind that Resolve saved, and returns the rows Resolve would have written
had it imported the file: identities fresh, derived values from the facts, project-local values
from the placement, constant and unexplained values copied from the template. A kind with too
little evidence, a file outside the envelope its kind was checked on, or a template without its
own facts is ``unverified`` (written only when the caller opts in, and reported); camera RAW,
rotated pictures, camera metadata and other unproven kinds are refused with the reason.
"""
import calendar
import math
import re
import struct
import uuid
from datetime import datetime, timezone
from fractions import Fraction

from . import blobs
from .codec import (CodecError, contains_text, edit_kd, edit_message, kd_decode, kd_get, kd_set, pb_decode,
                    pb_encode, pb_get, pb_remove, rewrap, unwrap)

STANDARD_RATES = tuple(Fraction(n, d) for n, d in (
    (24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1), (48000, 1001), (48, 1), (50, 1),
    (60000, 1001), (60, 1)))
DROP_FRAME_RATES = (Fraction(30000, 1001), Fraction(60000, 1001))
STILL_EXTENSIONS = frozenset({'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'dpx', 'exr'})
MAX_SECONDS = 24 * 3600
FIRST_YEAR, LAST_YEAR = 1970, 2079
LATEST_SECONDS = calendar.timegm((LAST_YEAR, 12, 31, 23, 59, 59))
_MAX_US = LATEST_SECONDS * 1_000_000 + 999_999
MAX_STREAMS = 64
MAX_TEXT = 4096
MAX_PATH = 1024
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_DIGITS = re.compile(r'[0-9]{1,9}\Z')
_RATIONAL = re.compile(r'-?[0-9]{1,12}/-?[0-9]{1,12}\Z')
TIMECODE_TAG = re.compile(r'[0-9]{2}:[0-9]{2}:[0-9]{2}[:;.][0-9]{2,3}\Z')
_TIMECODE = re.compile(r'([0-9]{2}):([0-9]{2}):([0-9]{2})([:;.])([0-9]{2,3})\Z')
_ISO = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z\Z')
_LOCAL = re.compile(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +'
                    r'([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4})\Z')
_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_WINDOWS_FORBIDDEN = set('<>:"/\\|?*')

PICTURE_CLASSES = {
    'yuv420p': ('8-bit 4:2:0', Fraction(3, 2)), 'yuvj420p': ('8-bit 4:2:0', Fraction(3, 2)),
    'yuv420p10le': ('10-bit 4:2:0', Fraction(3)), 'yuv422p10le': ('10-bit 4:2:2', Fraction(8, 3)),
    'yuva444p12le': ('12-bit 4:4:4:4', Fraction(8)),
}
_AUDIO_CODECS = {'pcm_s16le': ('Linear PCM', 1), 'pcm_s16be': ('Linear PCM', 1), 'pcm_s24le': ('Linear PCM', 2),
                 'pcm_s24be': ('Linear PCM', 2), 'aac': ('AAC', 3), 'mp3': ('MP3', 3)}
_LAYOUTS = {'mono': 1, 'stereo': 2}
_PIXELS = {'yuv420p': ('8', '4:2:0'), 'yuvj420p': ('8', '4:2:0'), 'yuv420p10le': ('10', '4:2:0'),
           'yuv422p10le': ('10', '4:2:2'), 'yuva444p12le': ('12', '4:4:4:4'), 'rgba': ('8', 'RGBA'),
           'rgb24': ('8', 'RGB'), 'gray': ('8', 'gray')}
CLIP_TYPES = frozenset({'Sm2MpVideoClip', 'Sm2MpAudioClip'})
METADATA_TYPE = 'BtMetadataLockableBlob'
MEDIA_TABLES = ('Sm2MpMedia', 'BtVideoInfo', 'BtAudioInfo', 'BtAudioInfo_Sm2MpMedia', 'Sm2MpFolder_Sm2MpMedia',
                'BtLockableBlob', 'SmPreset')
_JOIN = ('DbOwner', 'DbAssociate', 'DbPropertyName', 'DbIndex')
REQUIRED_COLUMNS = {
    'Sm2MpMedia': ('Sm2MpMedia_id', 'DbType', 'Name', 'MpFolder', 'Sm2MpFolder_id', 'Video',
                   'UniqueMediaPoolItemId', 'PTZRPreset', 'FieldsBlob'),
    'BtVideoInfo': ('BtVideoInfo_id', 'Sm2MpMedia_id', 'Clip', 'Time', 'Geometry', 'Proxy', 'VideoMetadata',
                    'MediaMetadata'),
    'BtAudioInfo': ('BtAudioInfo_id', 'Sm2MpMedia_id', 'Clip', 'TracksBA', 'MediaMetadata'),
    'BtAudioInfo_Sm2MpMedia': _JOIN, 'Sm2MpFolder_Sm2MpMedia': _JOIN,
    'BtLockableBlob': ('BtLockableBlob_id', 'DbType', 'BtLockableBlobMap_id', 'FieldsBlob', 'BlobOwner',
                       'DbSavedTime'),
    'SmPreset': ('SmPreset_id', 'Sm2MpMedia_id'),
}
CLIP_26_NOTE = ('Clip field 26 (the folder without its drive) is not written: Resolve writes it on some clips '
                'only, and what decides that is an open question')
_CLIP_WIRES = {1: 2, 2: 2, 3: 2, 5: 2, 6: 2, 7: 2, 13: 0, 14: 0, 15: 0, 16: 0, 18: 0, 26: 2}
_CLIP_PROJECT_LOCAL = (14, 26)
_TIME_KEYS = frozenset({'UniqueId', 'Timecode', 'StartFrame', 'NumFrames', 'FrameRate', 'DbType'})
_GEOMETRY_KEYS = frozenset({'UniqueId', 'Resolution', 'IsResolutionTransposed', 'FrameSize', 'ScanType', 'DbType'})
_PROXY_KEYS = frozenset({'UniqueId', 'DbType', 'DataManagerID'})
_VIDEO_METADATA_KEYS = frozenset({'UniqueId', 'DbType', 'ActualReelNumber'})
_TRACK_KEYS = frozenset({'UniqueId', 'StartTime', 'SampleRate', 'NumChannels', 'IdxTrack', 'Duration', 'DbType',
                         'CodecName', 'ChannelLayout', 'BitDepth'})
_EXTENTS_KEYS = frozenset({'Type', 'MediaExtents', 'CodecName', 'ChannelVecBA', 'BitDepth', 'SampleRate'})
_MEDIA_CLEARED = ('CurPlayheadPosition', 'PTZRPreset', 'PinsBA', 'VideoMetadata', 'MediaMetadata')
_UNVERIFIED_HINT = 'allow unverified media to write it anyway (the report lists why), or import the file in Resolve'


class MediaPoolError(ValueError):
    """A refusal with a code: ``bad_facts``, ``cannot_derive``, ``not_exact``, ``vfr_without_timecode``,
    ``unsupported_kind``, ``kind_mismatch``, ``bad_template``, ``no_template``, ``bad_placement``,
    ``id_collision``, ``unverified``."""

    def __init__(self, code, message, hint=''):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


def _refuse(code, message, hint='measure the file again'):
    return MediaPoolError(code, message, hint)


def _bad(why):
    return MediaPoolError('bad_template', why, 'use a template clip from a project Resolve saved, of the same kind')


def guarded(code):
    """Turn whatever a damaged value raises below a public entry point into its typed refusal."""
    def wrap(function):
        def run(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except MediaPoolError:
                raise
            except (CodecError, blobs.BlobError, struct.error, ArithmeticError, IndexError, KeyError, TypeError,
                    ValueError, AttributeError, UnicodeError) as error:
                raise MediaPoolError(code, f'{type(error).__name__}: {error}'[:600]) from None
        run.__name__, run.__doc__ = function.__name__, function.__doc__
        return run
    return wrap


# --------------------------------------------------------------------------- facts

class Stream:
    """One ffprobe stream, checked: only the fields the writer reads."""

    _TEXT = ('codec_type', 'codec_name', 'codec_tag_string', 'profile', 'pix_fmt', 'color_space', 'color_transfer',
             'color_primaries', 'color_range', 'chroma_location', 'field_order', 'sample_aspect_ratio',
             'display_aspect_ratio', 'sample_fmt', 'sample_rate', 'channel_layout', 'bits_per_raw_sample',
             'r_frame_rate', 'avg_frame_rate', 'time_base', 'nb_frames')
    _INT = {'width': (1, 65536), 'height': (1, 65536), 'level': (-(1 << 31), (1 << 31) - 1),
            'has_b_frames': (0, (1 << 31) - 1), 'coded_width': (0, 65536), 'coded_height': (0, 65536),
            'initial_padding': (0, (1 << 31) - 1), 'channels': (1, 64), 'bits_per_sample': (0, 64),
            'duration_ts': (0, (1 << 62) - 1)}

    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise _refuse('bad_facts', 'a stream is not an object')
        index = raw.get('index')
        if type(index) is not int or not 0 <= index <= 1023:
            raise _refuse('bad_facts', 'a stream has no index')
        self.index = index
        for name in self._TEXT:
            value = raw.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT or _CONTROL.search(value)):
                raise _refuse('bad_facts', f'stream {index}: {name} is not plain text')
            setattr(self, name, value)
        for name in ('sample_rate', 'bits_per_raw_sample', 'nb_frames'):
            value = getattr(self, name)
            if value is not None and not _DIGITS.match(value):
                raise _refuse('bad_facts', f'stream {index}: {name} {value[:20]!r} is not decimal digits')
        for name in ('r_frame_rate', 'avg_frame_rate', 'time_base'):
            value = getattr(self, name)
            if value is not None and not _RATIONAL.match(value):
                raise _refuse('bad_facts', f'stream {index}: {name} {value[:20]!r} is not a rational')
        for name, (low, high) in self._INT.items():
            value = raw.get(name)
            if value is not None and (type(value) is not int or not low <= value <= high):
                raise _refuse('bad_facts', f'stream {index}: {name} is not a whole number in range')
            setattr(self, name, value)
        disposition = raw.get('disposition') or {}
        tags = raw.get('tags') or {}
        if not isinstance(disposition, dict) or not isinstance(tags, dict) or len(tags) > 256:
            raise _refuse('bad_facts', f'stream {index}: disposition or tags are not objects')
        self.attached_picture = disposition.get('attached_pic') == 1
        self.tags = _checked_tags(tags)
        side = raw.get('side_data_list') or []
        if not isinstance(side, list) or len(side) > 32:
            raise _refuse('bad_facts', f'stream {index}: side data is not a short list')
        self.rotation = 0
        for entry in side:
            value = entry.get('rotation') if isinstance(entry, dict) else None
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                        or abs(value) > 360:
                    raise _refuse('bad_facts', f'stream {index}: a rotation is not a number of degrees')
                self.rotation = round(float(value))
        rotate = next((v for k, v in self.tags.items() if k.lower() == 'rotate'), None)
        if self.rotation == 0 and rotate is not None:
            self.rotation = int(rotate)
        self.rotation %= 360


def _checked_tags(tags):
    out = {}
    for key, value in tags.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(key) > 256 or len(value) > MAX_TEXT:
            raise _refuse('bad_facts', 'a tag is not text')
        lower = key.lower()
        if lower == 'timecode' and not TIMECODE_TAG.match(value):
            raise _refuse('bad_facts', f'the {key} tag {value[:40]!r} is not HH:MM:SS:FF in ASCII digits')
        if lower == 'rotate' and not re.match(r'-?[0-9]{1,4}\Z', value):
            raise _refuse('bad_facts', f'the rotate tag {value[:40]!r} is not whole degrees')
        out[key] = value
    return out


def check_path(path):
    """The path Resolve will see, as the facts state it: an absolute Windows path (a drive or a
    share) or an absolute POSIX path, with no control character, no empty, ``.`` or ``..`` part,
    and on Windows no character Windows forbids in a name, no trailing dot or space."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH or _CONTROL.search(path):
        raise _refuse('bad_facts', 'the media path is not plain text of at most 1,024 characters')
    if re.match(r'[A-Za-z]:\\', path) or path.startswith('\\\\'):
        body = path[3:] if path[1] == ':' else path[2:]
        parts = body.split('\\')
        if path.startswith('\\\\') and len(parts) < 3:
            raise _refuse('bad_facts', f'the share path {path!r} names no folder on the share')
        for part in parts:
            if part in ('', '.', '..') or part[-1] in ' .' or set(part) & _WINDOWS_FORBIDDEN:
                raise _refuse('bad_facts', f'the Windows path {path!r} has a part Windows refuses')
        if len(parts) < 2:
            raise _refuse('bad_facts', f'the file {path!r} sits at the root of its drive, a form Resolve was not '
                                       'seen to store')
        return 'windows'
    if path.startswith('/') and '\\' not in path:
        parts = path[1:].split('/')
        if any(part in ('', '.', '..') for part in parts) or len(parts) < 2:
            raise _refuse('bad_facts', f'the path {path!r} has an empty, . or .. part, or sits at the root')
        return 'posix'
    raise _refuse('bad_facts', f'the media path {path!r} is neither an absolute Windows path nor an absolute '
                               'Mac or Linux path')


class Facts:
    """Checked facts of one file (the shape ai-agent-resolve's facts script writes)."""

    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise _refuse('bad_facts', 'the facts are not an object')
        self.path = raw.get('path')
        self.style = check_path(self.path)
        separator = '\\' if self.style == 'windows' else '/'
        self.folder, _, self.name = self.path.rpartition(separator)
        stem, dot, extension = self.name.rpartition('.')
        self.extension = extension.lower() if dot and stem else ''
        size = raw.get('size')
        if type(size) is not int or not 0 <= size < 1 << 50:
            raise _refuse('bad_facts', 'the size is not a whole number of bytes')
        self.size = size
        self.mtime_local = _local_time(raw.get('mtime_local'))
        self.ctime_utc_ns = _utc_ns(raw.get('ctime_utc'), 'ctime_utc') if raw.get('ctime_utc') is not None else None
        probe = raw.get('ffprobe')
        if not isinstance(probe, dict):
            raise _refuse('bad_facts', 'the facts have no ffprobe output')
        streams = probe.get('streams') or []
        fmt = probe.get('format') or {}
        if not isinstance(streams, list) or len(streams) > MAX_STREAMS or not isinstance(fmt, dict):
            raise _refuse('bad_facts', 'the ffprobe output is not streams and a format')
        self.streams = [Stream(s) for s in streams]
        self.format_tags = _checked_tags(fmt.get('tags') or {})


def _local_time(text):
    match = _LOCAL.match(text) if isinstance(text, str) else None
    if match is None:
        raise _refuse('bad_facts', 'the modification time is not in the form "Fri Mar 13 04:05:06 2026"')
    day, hour, minute, second, year = (int(match.group(n)) for n in (3, 4, 5, 6, 7))
    try:
        value = datetime(year, _MONTHS.index(match.group(2)) + 1, day, hour, minute, second)
    except ValueError:
        raise _refuse('bad_facts', 'the modification time names a date that does not exist') from None
    if _DAYS[value.weekday()] != match.group(1):
        raise _refuse('bad_facts', 'the modification time names the wrong weekday for its date')
    return value


def _utc_ns(text, what):
    match = _ISO.match(text) if isinstance(text, str) else None
    if match is None:
        raise _refuse('bad_facts', f'{what} is not a UTC time')
    try:
        whole = datetime(*(int(match.group(n)) for n in range(1, 7)), tzinfo=timezone.utc)
    except ValueError:
        raise _refuse('bad_facts', f'{what} names a date that does not exist') from None
    fraction = (match.group(7) or '0').ljust(9, '0')[:9]
    return calendar.timegm(whole.utctimetuple()) * 1_000_000_000 + int(fraction)


# --------------------------------------------------------------------------- formulas

def qt_local_time(value):
    if not FIRST_YEAR <= value.year <= LAST_YEAR:
        raise _refuse('bad_facts', f'the modification time lies in {value.year}, not from {FIRST_YEAR} to {LAST_YEAR}')
    return (f'{_DAYS[value.weekday()]} {_MONTHS[value.month - 1]} {value.day:2d} '
            f'{value.hour:02d}:{value.minute:02d}:{value.second:02d} {value.year:04d}')


def video_stream(facts):
    return next((s for s in facts.streams if s.codec_type == 'video' and not s.attached_picture), None)


def audio_streams(facts):
    return [s for s in facts.streams if s.codec_type == 'audio']


def _rational(text, what):
    if text is None:
        raise _refuse('cannot_derive', f'{what} is missing')
    num, _, den = text.partition('/')
    if int(den) <= 0 or int(num) <= 0:
        raise _refuse('cannot_derive', f'{what} is {text!r}, not a positive rate')
    return Fraction(int(num), int(den))


def video_rate(facts):
    video = video_stream(facts)
    if video is None:
        raise _refuse('cannot_derive', 'the file has no video stream to take a frame rate from')
    tmcd = next((s for s in facts.streams if s.codec_tag_string == 'tmcd'), None)
    if tmcd is not None:
        rate = _rational(tmcd.avg_frame_rate, "the timecode track's avg_frame_rate")
    else:
        base = _rational(video.r_frame_rate, "the video stream's r_frame_rate")
        avg = _rational(video.avg_frame_rate, "the video stream's avg_frame_rate")
        if base != avg:
            raise _refuse('vfr_without_timecode', f'the frame rate varies (r_frame_rate {base}, avg_frame_rate {avg}) '
                          'and the file has no timecode track', 'transcode the file to a constant frame rate')
        rate = avg
    if rate not in STANDARD_RATES:
        raise _refuse('not_exact', f'the frame rate {rate} is not one Resolve was seen to store')
    return rate


def video_frames(facts, rate):
    video = video_stream(facts)
    if video is None or video.duration_ts is None:
        raise _refuse('cannot_derive', 'the video stream has no duration_ts')
    exact = video.duration_ts * _rational(video.time_base, "the video stream's time_base") * rate
    if exact.denominator != 1:
        raise _refuse('not_exact', f'the video lasts {exact.numerator}/{exact.denominator} frames, not a whole number')
    if exact < 1 or exact > rate * MAX_SECONDS:
        raise _refuse('cannot_derive', 'the video is shorter than a frame or longer than 24 hours')
    return int(exact)


def _timecode_tag(tags):
    for key, value in tags.items():
        if key.lower() == 'timecode' and value.strip():
            return value.strip()
    return None


def start_timecode(facts):
    video = video_stream(facts)
    if video is not None and _timecode_tag(video.tags):
        return _timecode_tag(video.tags)
    for stream in facts.streams:
        if _timecode_tag(stream.tags):
            return _timecode_tag(stream.tags)
    return _timecode_tag(facts.format_tags)


def timecode_frames(text, fps):
    match = _TIMECODE.match(text.strip())
    if match is None:
        raise ValueError(f'{text!r} is not a HH:MM:SS:FF timecode')
    hours, minutes, seconds, frame = (int(match.group(n)) for n in (1, 2, 3, 5))
    nominal = round(fps)
    for value, limit in ((hours, 24), (minutes, 60), (seconds, 60), (frame, nominal)):
        if value >= limit:
            raise ValueError(f'{text!r} is out of range at {fps} fps')
    count = ((hours * 60 + minutes) * 60 + seconds) * nominal + frame
    if match.group(4) == ':':
        return count
    if fps not in DROP_FRAME_RATES:
        raise ValueError(f'{text!r} is drop-frame, which exists only at 29.97 and 59.94 fps')
    skip = nominal // 15
    if seconds == 0 and frame < skip and minutes % 10:
        raise ValueError(f'{text!r} names a frame number drop-frame timecode skips')
    total = hours * 60 + minutes
    return count - skip * (total - total // 10)


def start_seconds(timecode, rate):
    if timecode is None:
        return 0.0
    try:
        frames = timecode_frames(timecode, rate)
    except ValueError as error:
        raise _refuse('cannot_derive', f'the start timecode cannot be read: {error}') from None
    return frames / float(rate)


def sample_rate(stream):
    if stream.sample_rate is None:
        raise _refuse('cannot_derive', f'audio stream {stream.index} has no sample_rate')
    rate = int(stream.sample_rate)
    if not 1000 <= rate <= 384000:
        raise _refuse('cannot_derive', f'audio stream {stream.index} has a sample rate of {rate}')
    return rate


def audio_samples(stream):
    if stream.duration_ts is None:
        raise _refuse('cannot_derive', f'audio stream {stream.index} has no duration_ts')
    rate = sample_rate(stream)
    samples = math.floor(stream.duration_ts * _rational(stream.time_base, 'the audio time_base') * rate)
    if samples < 1 or samples > rate * MAX_SECONDS:
        raise _refuse('cannot_derive', f'audio stream {stream.index} holds no samples or more than 24 hours')
    return samples


def channels(stream):
    if stream.channels is None:
        raise _refuse('cannot_derive', f'audio stream {stream.index} has no channel count')
    return stream.channels


def audio_codec(stream):
    known = _AUDIO_CODECS.get(stream.codec_name or '')
    if known is None:
        raise _refuse('cannot_derive', f'audio codec {stream.codec_name!r} has no known Resolve codec name')
    return known


def channel_layout(stream):
    if stream.channel_layout is None:
        return None
    layout = _LAYOUTS.get(stream.channel_layout)
    if layout is None:
        raise _refuse('cannot_derive', f'channel layout {stream.channel_layout!r} is not known')
    if layout != stream.channels:
        raise _refuse('bad_facts', f'audio stream {stream.index}: layout and channel count disagree')
    return layout


def creation_us(facts):
    tag = facts.format_tags.get('creation_time')
    if tag is not None:
        match = _ISO.match(tag)
        value = None
        if match is not None:
            try:
                value = _utc_ns(tag, 'creation_time') // 1000
            except MediaPoolError:
                value = None
        if value is None or not 0 <= value <= _MAX_US:
            raise _refuse('cannot_derive', f'the creation_time tag {tag[:40]!r} is not a UTC time from {FIRST_YEAR} to '
                                           f'{LAST_YEAR}')
        return value
    if facts.ctime_utc_ns is None:
        raise _refuse('cannot_derive', 'the file has no creation_time tag and its creation time is not known on this '
                                       'computer', 'add a creation_time tag, or import the file in Resolve')
    value = facts.ctime_utc_ns // 1000
    if not 0 <= value <= _MAX_US:
        raise _refuse('cannot_derive', f'the file\'s creation time does not lie from {FIRST_YEAR} to {LAST_YEAR}')
    return value


def picture_class(stream):
    known = PICTURE_CLASSES.get(stream.pix_fmt or '')
    if known is None:
        raise _refuse('cannot_derive', f'pixel format {stream.pix_fmt!r} has no known frame size rule')
    return known


def frame_size(width, height, bytes_per_pixel):
    value = width * (height + (height & 1)) * bytes_per_pixel
    if value.denominator != 1:
        raise _refuse('not_exact', f'a {width}x{height} frame is not a whole number of bytes')
    return int(value) + 8192


def resolution(stream):
    if stream.width is None or stream.height is None:
        raise _refuse('cannot_derive', f'video stream {stream.index} has no width or height')
    if stream.rotation in (90, 270):
        return stream.height, stream.width, True
    return stream.width, stream.height, False


# --------------------------------------------------------------------------- kinds

ENVELOPE_PROPERTIES = (
    'resolution', 'coded_size', 'frame_rate', 'drop_frame', 'bit_depth', 'chroma', 'video_profile', 'video_level',
    'field_order', 'sample_aspect_ratio', 'display_aspect_ratio', 'has_b_frames', 'color_range', 'chroma_location',
    'video_raw_bits', 'video_time_base', 'video_stream_rates', 'audio', 'audio_profile', 'audio_codec',
    'audio_sample_format', 'audio_bits_per_sample', 'audio_raw_bits', 'audio_channel_layout',
    'audio_initial_padding', 'audio_time_base')


def kind_label(kind):
    media, container, codec, picture, transposed, timecode, audio, look = kind
    parts = [media, container] + [p for p in (codec, picture) if p]
    if transposed:
        parts.append('transposed')
    parts.append('timecode' if timecode else 'no timecode')
    parts += [f'{a[0]}/{a[1]}/{a[2]}ch/{a[3]}{"+layout" if a[4] else ""}' for a in audio]
    return ' '.join(parts)


def _v(value):
    return '-' if value is None else str(value)


def envelope_of(facts):
    video = video_stream(facts)
    out = dict.fromkeys(ENVELOPE_PROPERTIES, '-')
    if video is not None:
        if video.width is not None and video.height is not None:
            out['resolution'] = f'{video.width}x{video.height}'
        if video.coded_width is not None or video.coded_height is not None:
            out['coded_size'] = f'{_v(video.coded_width)}x{_v(video.coded_height)}'
        out['bit_depth'], out['chroma'] = _PIXELS.get(video.pix_fmt or '', (f'?{video.pix_fmt}', f'?{video.pix_fmt}'))
        for prop, value in (('video_profile', video.profile), ('video_level', video.level),
                            ('field_order', video.field_order), ('sample_aspect_ratio', video.sample_aspect_ratio),
                            ('display_aspect_ratio', video.display_aspect_ratio),
                            ('has_b_frames', video.has_b_frames), ('color_range', video.color_range),
                            ('chroma_location', video.chroma_location),
                            ('video_raw_bits', video.bits_per_raw_sample), ('video_time_base', video.time_base)):
            out[prop] = _v(value)
        out['video_stream_rates'] = f'{_v(video.r_frame_rate)} {_v(video.avg_frame_rate)}'
        if facts.extension not in STILL_EXTENSIONS:
            rate = video_rate(facts)
            out['frame_rate'] = f'{rate.numerator}/{rate.denominator}'
            timecode = start_timecode(facts)
            if timecode is not None:
                out['drop_frame'] = 'drop-frame' if timecode.strip()[8] != ':' else 'non-drop'
    audio = audio_streams(facts)
    if audio:
        out['audio'] = '+'.join(f'{s.sample_rate} Hz {s.channels} ch' for s in audio)
        for prop, read in (('audio_profile', lambda s: s.profile or None), ('audio_codec', lambda s: s.codec_name),
                           ('audio_sample_format', lambda s: s.sample_fmt),
                           ('audio_bits_per_sample', lambda s: s.bits_per_sample),
                           ('audio_raw_bits', lambda s: s.bits_per_raw_sample),
                           ('audio_channel_layout', lambda s: s.channel_layout),
                           ('audio_initial_padding', lambda s: s.initial_padding),
                           ('audio_time_base', lambda s: s.time_base)):
            out[prop] = '+'.join(_v(read(s)) for s in audio)
    return out


def _look(facts):
    video = video_stream(facts)
    if video is None:
        return ()
    return (video.pix_fmt or '-', video.color_space or '-', video.color_transfer or '-', video.color_primaries or '-',
            video.color_range or '-', str(video.rotation))


@guarded('bad_facts')
def kind_of_facts(facts):
    """``(media, container, codec, picture class, transposed, timecode, audio kinds, look)``."""
    video = video_stream(facts)
    media = 'audio' if video is None else ('still' if facts.extension in STILL_EXTENSIONS else 'video')
    codec = picture = None
    transposed = False
    if video is not None:
        tag = video.codec_tag_string or ''
        if media == 'video':
            codec = tag if len(tag) == 4 and tag.isascii() and tag.isalnum() else f'?{video.codec_name}'
            known = PICTURE_CLASSES.get(video.pix_fmt or '')
            picture = known[0] if known else f'?{video.pix_fmt}'
        transposed = video.rotation in (90, 270)
    audio = []
    for stream in audio_streams(facts):
        try:
            name, depth = audio_codec(stream)
        except MediaPoolError:
            name, depth = f'?{stream.codec_name}', 0
        audio.append((name, depth, stream.channels or 0, int(stream.sample_rate or 0), stream.channel_layout is not None))
    return (media, facts.extension, codec, picture, transposed,
            media == 'video' and start_timecode(facts) is not None, tuple(audio), _look(facts))


def _envelope(**values):
    return {prop: frozenset((v,) if isinstance(v, str) else v)
            for prop in ENVELOPE_PROPERTIES for v in (values.get(prop, '-'),)}


_BT709 = ('bt709', 'bt709', 'bt709', 'tv', '0')
_UHD, _HD, _NTSC, _FILM = '3840x2160', '1920x1080', '30000/1001', '24000/1001'
_WAV_24 = {'audio_codec': 'pcm_s24le', 'audio_sample_format': 's32', 'audio_bits_per_sample': '24',
           'audio_raw_bits': '24', 'audio_initial_padding': '0', 'audio_time_base': '1/48000'}
_PRORES = {'resolution': _HD, 'coded_size': _HD, 'frame_rate': _NTSC, 'video_level': '-99',
           'field_order': 'progressive', 'has_b_frames': '0', 'color_range': 'tv', 'video_time_base': '1/30000',
           'video_stream_rates': f'{_NTSC} {_NTSC}'}


def _pcm(depth, count, layout):
    return ('Linear PCM', depth, count, 48000, layout)


def _audio_kind(container, *streams):
    return ('audio', container, None, None, False, False, streams, ())


def _video_kind(codec, picture, timecode, look, *audio):
    return ('video', 'mp4' if codec == 'avc1' else 'mov', codec, picture, False, timecode, audio, look)


def _wav(count, layout='-'):
    return _envelope(audio=f'48000 Hz {count} ch', audio_channel_layout=layout, **_WAV_24)


_AAC_44, _AAC_48 = ('AAC', 3, 2, 44100, True), ('AAC', 3, 2, 48000, True)
#: The kinds that may be authored: (status, files checked, note, envelope). From the other
#: repository's corpus gate on two Resolve 21.1 saves (its docs/DESIGN.md, "Kinds").
KINDS = {
    _video_kind('avc1', '8-bit 4:2:0', True, ('yuv420p',) + _BT709, _pcm(1, 2, False)): ('verified', 68, '', _envelope(
        resolution=_UHD, coded_size=_UHD, frame_rate=_FILM, drop_frame='non-drop', bit_depth='8', chroma='4:2:0',
        video_profile='High', video_level='51', field_order='progressive', sample_aspect_ratio='1:1',
        display_aspect_ratio='16:9', has_b_frames='1', color_range='tv', chroma_location='left', video_raw_bits='8',
        video_time_base='1/24000', video_stream_rates=f'{_FILM} {_FILM}', audio='48000 Hz 2 ch',
        audio_codec='pcm_s16be', audio_sample_format='s16', audio_bits_per_sample='16', audio_initial_padding='0',
        audio_time_base='1/48000')),
    _audio_kind('wav', _pcm(2, 2, False)): ('verified', 29, '', _wav(2)),
    _audio_kind('wav', _pcm(2, 2, True)): ('verified', 29, '', _wav(2, 'stereo')),
    _video_kind('apch', '10-bit 4:2:2', True, ('yuv422p10le',) + _BT709): ('verified', 17, '', _envelope(
        drop_frame='non-drop', bit_depth='10', chroma='4:2:2', video_profile='HQ', video_raw_bits='10', **_PRORES)),
    _video_kind('ap4h', '12-bit 4:4:4:4', False, ('yuva444p12le', '-', '-', '-', 'tv', '0')): (
        'unverified', 7, 'Sm2MpMedia.FieldsBlob #1.#3 is on every ProRes 4444 clip with values no rule derives',
        _envelope(bit_depth='12', chroma='4:4:4:4', video_profile='4444', video_raw_bits='12',
                  sample_aspect_ratio=('-', '1:1'), display_aspect_ratio=('-', '16:9'), **_PRORES)),
    _audio_kind('wav', _pcm(2, 1, False)): ('verified', 6, '', _wav(1)),
    _audio_kind('wav', _pcm(2, 1, True)): ('verified', 4, '', _wav(1, 'mono')),
    ('still', 'png', None, None, False, False, (), ('rgba', 'gbr', '-', '-', 'pc', '0')): (
        'verified', 3, 'a still whose name holds a digit is refused', _envelope(
            resolution=_UHD, coded_size=_UHD, bit_depth='8', chroma='RGBA', video_level='-99',
            sample_aspect_ratio='1:1', display_aspect_ratio='16:9', has_b_frames='0', color_range='pc',
            video_time_base='1/25', video_stream_rates='25/1 25/1')),
    _audio_kind('mp3', ('MP3', 3, 2, 44100, True)): ('verified', 2, '', _envelope(
        audio='44100 Hz 2 ch', audio_codec='mp3', audio_sample_format='fltp', audio_bits_per_sample='0',
        audio_channel_layout='stereo', audio_initial_padding='0', audio_time_base='1/14112000')),
    _audio_kind('m4a', _AAC_44): ('unverified', 1, 'one AAC .m4a file (with cover art)', None),
    _audio_kind('mp3', ('MP3', 3, 2, 48000, True)): ('unverified', 1, 'one 48 kHz .mp3 file (with cover art)', None),
    _audio_kind('wav', _pcm(1, 2, False)): ('unverified', 1, 'one 16-bit .wav file', None),
    _video_kind('apch', '10-bit 4:2:2', False, ('yuv422p10le', 'bt709', '-', '-', 'tv', '0')): (
        'unverified', 1, 'one ProRes file without a timecode or transfer and primaries tags', None),
    _video_kind('hvc1', '10-bit 4:2:0', True, ('yuv420p10le',) + _BT709, _AAC_48): (
        'unverified', 1, 'one 10-bit HEVC file Resolve rendered', None),
}


def outside_envelope(envelope, facts):
    if envelope is None:
        return []
    values = envelope_of(facts)
    reasons = []
    for prop in ENVELOPE_PROPERTIES:
        allowed = envelope.get(prop)
        if allowed is None or values[prop] not in allowed:
            checked = ', '.join(sorted(allowed)) if allowed else 'nothing'
            reasons.append(f"the file's {prop.replace('_', ' ')} {values[prop]} lies outside the evidence for its kind "
                           f'(checked: {checked})')
    return reasons


# --------------------------------------------------------------------------- template clips

class ClipRows:
    """Every row one media clip owns (``audio`` in ``DbIndex`` order)."""

    def __init__(self, media, video, audio, audio_links, folder_link, lockables=(), presets=()):
        self.media, self.video, self.audio, self.audio_links = media, video, tuple(audio), tuple(audio_links)
        self.folder_link, self.lockables, self.presets = folder_link, tuple(lockables), tuple(presets)

    @property
    def media_id(self):
        return str(self.media['Sm2MpMedia_id'])

    @property
    def is_audio_clip(self):
        return self.media.get('DbType') == 'Sm2MpAudioClip'

    def tables(self):
        out = {'Sm2MpMedia': [self.media], 'BtVideoInfo': [self.video] if self.video is not None else [],
               'BtAudioInfo': list(self.audio), 'BtAudioInfo_Sm2MpMedia': list(self.audio_links),
               'Sm2MpFolder_Sm2MpMedia': [self.folder_link], 'BtLockableBlob': list(self.lockables),
               'SmPreset': list(self.presets)}
        return {t: rows for t, rows in out.items() if rows}

    @classmethod
    def from_tables(cls, rows):
        return cls(rows['Sm2MpMedia'][0], rows['BtVideoInfo'][0] if rows.get('BtVideoInfo') else None,
                   rows.get('BtAudioInfo', ()), rows.get('BtAudioInfo_Sm2MpMedia', ()),
                   rows['Sm2MpFolder_Sm2MpMedia'][0], rows.get('BtLockableBlob', ()), rows.get('SmPreset', ()))

    def ids(self):
        found = {self.media_id, str(self.media.get('UniqueMediaPoolItemId') or '')}
        if self.video is not None:
            found.add(str(self.video['BtVideoInfo_id']))
        found.update(str(a['BtAudioInfo_id']) for a in self.audio)
        found.update(str(r['BtLockableBlob_id']) for r in self.lockables)
        found.update(str(r['SmPreset_id']) for r in self.presets)
        found.discard('')
        return found


def _text(value):
    return value if isinstance(value, str) and value else None


@guarded('bad_template')
def clips_from_rows(tables):
    """Group media rows by clip: ``({media id: ClipRows}, {media id: reason not a template})``."""
    for table in MEDIA_TABLES:
        for row in tables.get(table, ()):
            missing = [c for c in REQUIRED_COLUMNS[table] if c not in row]
            if missing:
                raise _bad(f'the {table} table has no column {missing[0]}')
    owned = {}

    def own(key, slot, row):
        owned.setdefault(key, {}).setdefault(slot, []).append(row)
    for row in tables.get('BtVideoInfo', ()):
        own(row['Sm2MpMedia_id'], 'video', row)
    for row in tables.get('BtAudioInfo', ()):
        own(row['Sm2MpMedia_id'], 'audio', row)
    for row in tables.get('BtAudioInfo_Sm2MpMedia', ()):
        own(row['DbOwner'], 'audio_links', row)
    for row in tables.get('Sm2MpFolder_Sm2MpMedia', ()):
        own(row['DbAssociate'], 'folder_links', row)
    for row in tables.get('SmPreset', ()):
        own(row['Sm2MpMedia_id'], 'presets', row)
    lockables = {}
    for row in tables.get('BtLockableBlob', ()):
        lockables.setdefault(row['BlobOwner'], []).append(row)
    clips, skipped = {}, {}
    for media in tables.get('Sm2MpMedia', ()):
        mid = _text(media.get('Sm2MpMedia_id')) or ''
        if media.get('DbType') not in CLIP_TYPES:
            continue
        try:
            clips[mid] = _clip(media, owned.get(mid, {}), lockables)
        except MediaPoolError as error:
            skipped[mid] = error.message
    return clips, skipped


def _clip(media, owned, lockables):
    mid = _text(media.get('Sm2MpMedia_id'))
    if mid is None:
        raise _bad('the clip has no Sm2MpMedia_id')
    video_rows, audio_rows = owned.get('video', []), owned.get('audio', [])
    links, folders = owned.get('audio_links', []), owned.get('folder_links', [])
    if media.get('DbType') == 'Sm2MpVideoClip':
        if len(video_rows) != 1:
            raise _bad(f'a video clip needs one BtVideoInfo row, it has {len(video_rows)}')
        video = video_rows[0]
        if _text(video['BtVideoInfo_id']) is None or media['Video'] != video['BtVideoInfo_id']:
            raise _bad('Sm2MpMedia.Video does not name its BtVideoInfo row')
    else:
        if video_rows or media.get('Video') is not None:
            raise _bad('a sound clip has a BtVideoInfo row')
        if not audio_rows:
            raise _bad('a sound clip has no BtAudioInfo row')
        video = None
    if len(audio_rows) > 16:
        raise _bad(f'the clip has {len(audio_rows)} audio infos')
    if any(link.get('DbPropertyName') != 'EmbeddedAudioVec' for link in links):
        raise _bad('an audio join row has another property than EmbeddedAudioVec')
    by_id = {a.get('BtAudioInfo_id'): a for a in audio_rows}
    if len(by_id) != len(audio_rows) or None in by_id:
        raise _bad('two BtAudioInfo rows share an id, or one has none')
    if any(type(r.get('DbIndex')) is not int for r in links):
        raise _bad("an EmbeddedAudioVec join row's DbIndex is not a whole number")
    order = sorted(links, key=lambda r: r['DbIndex'])
    if [r['DbIndex'] for r in order] != list(range(len(audio_rows))) or len(links) != len(audio_rows):
        raise _bad('the EmbeddedAudioVec join rows are missing, repeated or out of order')
    if {r.get('DbAssociate') for r in order} != set(by_id):
        raise _bad("the EmbeddedAudioVec join rows do not name the clip's audio infos")
    audio = tuple(by_id[r['DbAssociate']] for r in order)
    if len(folders) != 1:
        raise _bad(f'the clip has {len(folders)} MediaVec join rows, not one')
    folder = folders[0]
    if folder.get('DbPropertyName') != 'MediaVec' or folder.get('DbOwner') != media.get('Sm2MpFolder_id') \
            or media.get('MpFolder') != media.get('Sm2MpFolder_id'):
        raise _bad("the clip's bin join row does not agree with its MpFolder")
    if any(_text(a['BtAudioInfo_id']) is None for a in audio):
        raise _bad("a BtAudioInfo row's id is not text")
    owner = video['BtVideoInfo_id'] if video is not None else audio[0]['BtAudioInfo_id']
    found = []
    keys = {mid, *(a['BtAudioInfo_id'] for a in audio)} | ({video['BtVideoInfo_id']} if video is not None else set())
    for key in sorted(keys):
        found.extend(lockables.get(key, []))
    if any(r.get('DbType') != METADATA_TYPE or r.get('BlobOwner') != owner for r in found):
        raise _bad('the clip owns a lockable blob this writer does not know')
    if len(found) > 1:
        raise _bad('the clip has more than one metadata blob')
    presets = owned.get('presets', [])
    if media.get('PTZRPreset') is not None and media.get('PTZRPreset') not in {p.get('SmPreset_id') for p in presets}:
        raise _bad('Sm2MpMedia.PTZRPreset names no preset of the clip')
    return ClipRows(media, video, audio, order, folder, found, presets)


def _clip_fields(blob, what):
    out = {}
    for number, _wire, value in pb_decode(unwrap(blob, what), what):
        out.setdefault(number, value)
    return out


def clip_name(clip):
    """``(folder, file name)`` of a clip's file, from its first info's ``Clip`` blob."""
    row = clip.video if clip.video is not None else clip.audio[0]
    fields = _clip_fields(row['Clip'], 'Clip')
    folder, name = fields.get(1), fields.get(2)
    if not isinstance(folder, bytes) or not isinstance(name, bytes):
        raise _bad("the clip's folder or file name is not text")
    return folder.decode('utf-8'), name.decode('utf-8')


def _picture_of_rows(geometry):
    res, size = kd_get(geometry, 'Resolution'), kd_get(geometry, 'FrameSize')
    transposed = kd_get(geometry, 'IsResolutionTransposed')
    is_transposed = transposed is not None and transposed[3] is True
    if res is None or size is None or not isinstance(res[3], bytes) or type(size[3]) is not int:
        raise _bad('Geometry has no Resolution or FrameSize')
    width, height = blobs.resolution(res[3])
    if is_transposed:
        width, height = height, width
    for name, bpp in {v[0]: v[1] for v in PICTURE_CLASSES.values()}.items():
        try:
            if frame_size(width, height, bpp) == size[3]:
                return name, is_transposed
        except MediaPoolError:
            continue
    return f'?{size[3]}/{width}x{height}', is_transposed


@guarded('bad_template')
def kind_of_rows(clip):
    """The kind of a saved clip from its rows (no look: rows do not record it)."""
    _folder, name = clip_name(clip)
    stem, dot, ext = name.rpartition('.')
    container = ext.lower() if dot and stem else ''
    codec = picture = None
    transposed = timecode = False
    if clip.video is None:
        media = 'audio'
    else:
        media = 'still' if container in STILL_EXTENSIONS else 'video'
        picture, transposed = _picture_of_rows(kd_decode(clip.video.get('Geometry'), 'Geometry'))
        time = kd_decode(clip.video.get('Time'), 'Time')
        if media == 'video':
            tag = _clip_fields(clip.video.get('Clip'), 'Clip').get(5)
            codec = tag.decode('ascii', 'replace') if isinstance(tag, bytes) else None
            timecode = kd_get(time, 'Timecode') is not None
        else:
            picture = None
    audio = []
    for info in clip.audio:
        tracks = kd_decode(info.get('TracksBA'), 'TracksBA')
        if [e[0] for e in tracks] != ['0'] or not isinstance(tracks[0][3], bytes):
            raise _bad('TracksBA does not hold one track')
        track = kd_decode(tracks[0][3], 'TracksBA track')

        def number(key, entries=track):
            entry = kd_get(entries, key)
            if entry is None or type(entry[3]) is not int:
                raise _bad(f"the audio track's {key} is not a whole number")
            return entry[3]
        codec_name = kd_get(track, 'CodecName')
        if codec_name is None or not isinstance(codec_name[3], str):
            raise _bad('the audio track has no codec name')
        audio.append((codec_name[3], number('BitDepth'), number('NumChannels'), number('SampleRate'),
                      kd_get(track, 'ChannelLayout') is not None))
    return (media, container, codec, picture, transposed, timecode, tuple(audio), None)


def _without_look(kind):
    return kind[:7]


# --------------------------------------------------------------------------- authoring

class Placement:
    """Where the new clip goes, and the project-local values a fresh import takes."""

    def __init__(self, folder_id, folder_index, project_rate, added_at, lockable_map_id=None, saved_age_ms=0):
        self.folder_id, self.folder_index, self.project_rate = folder_id, folder_index, project_rate
        self.added_at, self.lockable_map_id, self.saved_age_ms = added_at, lockable_map_id, saved_age_ms

    def check(self):
        def refuse(why):
            return MediaPoolError('bad_placement', f"the placement's {why}")
        if not isinstance(self.folder_id, str) or not _UUID.match(self.folder_id):
            raise refuse('bin id is not a uuid text')
        if type(self.folder_index) is not int or not 0 <= self.folder_index < 1 << 31:
            raise refuse('index in the bin is out of range')
        if self.project_rate not in STANDARD_RATES:
            raise refuse(f'project frame rate {self.project_rate!r} is not a standard rate')
        if type(self.added_at) is not int or not 0 <= self.added_at <= LATEST_SECONDS:
            raise refuse('date added is out of range')
        if self.lockable_map_id is not None and not _UUID.match(str(self.lockable_map_id)):
            raise refuse('lockable map id is not a uuid text')
        if type(self.saved_age_ms) is not int or not 0 <= self.saved_age_ms < 1 << 63:
            raise refuse('metadata save age is out of range')


class Template:
    def __init__(self, rows, facts=None, source=''):
        self.rows, self.facts, self.source = rows, facts, source


class AuthoredClip:
    def __init__(self, kind, status, media_id, rows, unverified=(), notes=(), template_media_id=''):
        self.kind, self.status, self.media_id, self.rows = kind, status, media_id, rows
        self.unverified, self.notes, self.template_media_id = tuple(unverified), tuple(notes), template_media_id


class _Ids:
    def __init__(self, taken, template, new_id):
        self.taken, self.template, self.new_id, self.made = taken, template, new_id, set()

    def __call__(self):
        value = self.new_id()
        if not isinstance(value, str) or not _UUID.match(value):
            raise MediaPoolError('id_collision', f'the id source gave {value!r}, not a uuid text')
        if value in self.made or value in self.taken or value in self.template:
            raise MediaPoolError('id_collision', f'the new id {value} is already used')
        self.made.add(value)
        return value


def _closed(entries, known, what):
    unknown = [e[0] for e in entries if e[0] not in known]
    if unknown:
        raise _bad(f'{what} holds keys this writer does not know: {unknown[:5]}')


def rate_bytes(rate):
    return struct.pack('<d', float(rate)) + bytes(8)


def _zigzag(n):
    return (n << 1) ^ (n >> 63)


class _Derived:
    def __init__(self, facts, kind, placement):
        self.name, self.folder = facts.name, facts.folder
        self.mtime_text = qt_local_time(facts.mtime_local)
        self.created_us = creation_us(facts)
        self.rate = self.frames = self.timecode = self.resolution = self.frame_size = self.codec_tag = None
        self.start = 0.0
        video = video_stream(facts)
        if kind[0] == 'video':
            self.rate = video_rate(facts)
            self.frames = video_frames(facts, self.rate)
            self.timecode = start_timecode(facts)
            self.start = start_seconds(self.timecode, self.rate)
            w, h, _ = resolution(video)
            self.resolution = (w, h)
            self.frame_size = frame_size(video.width, video.height, picture_class(video)[1])
            self.codec_tag = video.codec_tag_string
        elif kind[0] == 'still':
            if any(c.isdigit() for c in facts.name):
                raise _refuse('cannot_derive', f"the still's name {facts.name!r} holds a number, which Resolve may read "
                              'as a frame number; that rule is not established', 'rename the still without digits')
            if facts.size < 1:
                raise _refuse('bad_facts', 'the still is an empty file')
            self.rate, self.frames = placement.project_rate, 1
            w, h, _ = resolution(video)
            self.resolution, self.frame_size = (w, h), facts.size
        self.audio = []
        for stream in audio_streams(facts):
            samples, rate = audio_samples(stream), sample_rate(stream)
            name, depth = audio_codec(stream)
            self.audio.append({'codec': name, 'bit_depth': depth, 'channels': channels(stream), 'sample_rate': rate,
                               'samples': samples, 'layout': channel_layout(stream), 'extent': samples / rate})


def _check_picture_decoded(facts):
    pictures = [s for s in facts.streams if s.codec_type == 'video' and not s.attached_picture]
    if len(pictures) > 1:
        raise MediaPoolError('unsupported_kind', f'{facts.name} has {len(pictures)} video streams; how Resolve records '
                             'a second picture was not seen', 'import the file in Resolve by hand')
    video = video_stream(facts)
    if video is not None and (video.codec_name is None or video.width is None or video.pix_fmt is None):
        raise MediaPoolError('cannot_derive', f'ffprobe does not decode the picture of {facts.name} (camera RAW such as '
                             'BRAW), so its size and frame count cannot be derived', 'author its proxy instead')
    if video is not None and video.rotation:
        raise MediaPoolError('unsupported_kind', f'{facts.name} is rotated ({video.rotation} degrees, as phones record '
                             'it); how Resolve stores a rotated picture is not proven', 'import the file in Resolve')


@guarded('bad_template')
def author(facts, template, placement, *, taken=frozenset(), new_id=lambda: str(uuid.uuid4()), allow_unverified=False):
    """The rows of a new media clip for ``facts`` built from ``template`` (see the module
    docstring). A result that would not be verified is refused (``unverified``) unless
    ``allow_unverified``; then its ``status`` is ``unverified`` with the reasons."""
    if not isinstance(facts, Facts):
        raise MediaPoolError('bad_facts', 'the facts are not checked facts')
    if not isinstance(template, Template) or not isinstance(template.rows, ClipRows):
        raise MediaPoolError('no_template', 'no template clip was given')
    placement.check()
    _check_picture_decoded(facts)
    file_kind = kind_of_facts(facts)
    rows_kind = kind_of_rows(template.rows)
    if _without_look(file_kind) != _without_look(rows_kind):
        raise MediaPoolError('kind_mismatch', f'the template is a {kind_label(rows_kind)}, the file a '
                             f'{kind_label(file_kind)}', 'give a template clip of the same kind')
    look_note = None
    if template.facts is None:
        look_note = ("the template's own facts are missing: its rows cannot be checked against its file (nor its "
                     'colour and pixel format)')
    elif kind_of_facts(template.facts) != file_kind:
        raise MediaPoolError('kind_mismatch', "the template's file has another pixel format, colour tags or rotation",
                             'give a template clip whose file is of the same kind')
    status = KINDS.get(file_kind)
    if status is None:
        raise MediaPoolError('unsupported_kind', f'no evidence yet that the rows of a {kind_label(file_kind)} can be '
                             'authored', 'import the file in Resolve by hand')
    derived = _Derived(facts, file_kind, placement)
    writer = _Writer(facts, template.rows, placement, derived, taken, new_id)
    built = writer.build()
    unverified = []
    if status[0] != 'verified':
        unverified.append(f'kind not verified: {status[2] or "a single file of this kind was checked"}')
    unverified += outside_envelope(status[3], facts)
    if facts.style != 'windows':
        unverified.append('the path is not a Windows path: every verified clip was read by Resolve on Windows')
    if look_note:
        unverified.append(look_note)
    if template.facts is not None:
        mismatch = _template_mismatch(template, placement)
        if mismatch:
            unverified.append(mismatch)
    if unverified and not allow_unverified:
        raise MediaPoolError('unverified', f'the rows of {facts.name} would not be verified: ' + '; '.join(unverified),
                             _UNVERIFIED_HINT)
    return AuthoredClip(file_kind, 'unverified' if unverified else 'verified', writer.media_id, built, unverified,
                        tuple(writer.notes) + (CLIP_26_NOTE,), template.rows.media_id)


def _template_mismatch(template, placement):
    """Why the template's own facts are not trusted: authored from them (itself as the template),
    the template must equal its own rows in every field the authoring derives."""
    try:
        kind = kind_of_facts(template.facts)
        derived = _Derived(template.facts, kind, placement)
        own = _Writer(template.facts, template.rows, placement, derived, frozenset(), lambda: str(uuid.uuid4()),
                      keep_ids=True).build()
    except MediaPoolError as error:
        return f"the template's facts are not trusted: authoring the template from them is refused ({error.message})"
    first = compare_clip(template.rows, ClipRows.from_tables(own))
    return None if first is None else f"the template's facts are not trusted: authored from them it differs in {first}"


def compare_clip(expected, got):
    """The first derived field in which two clips' rows differ, decoded (``Clip`` fields 14 and
    26 are project-local and left out), or None."""
    def clip_fields(blob):
        return [f for f in pb_decode(unwrap(blob, 'Clip'), 'Clip') if f[0] not in _CLIP_PROJECT_LOCAL]

    def kd(blob, what):
        entries = kd_decode(blob, what)
        return [[k, t, n, kd(v, what) if isinstance(v, bytes) and v[:4] == b'\x00\x00\x00\x01' else v]
                for k, t, n, v in entries]
    pairs = [('Sm2MpMedia.Name', lambda c: c.media.get('Name'))]
    still = clip_name(expected)[1].rpartition('.')[2].lower() in STILL_EXTENSIONS
    if expected.video is not None:
        # A still's Time.FrameRate is the project's rate when it was imported, not the file's.
        pairs += [('BtVideoInfo.Clip', lambda c: clip_fields(c.video['Clip'])),
                  ('BtVideoInfo.Time', lambda c: [e for e in kd(c.video['Time'], 'Time')
                                                  if not (still and e[0] == 'FrameRate')]),
                  ('BtVideoInfo.Geometry', lambda c: kd(c.video['Geometry'], 'Geometry'))]
    for i in range(len(expected.audio)):
        pairs += [(f'BtAudioInfo[{i}].Clip', lambda c, i=i: clip_fields(c.audio[i]['Clip'])),
                  (f'BtAudioInfo[{i}].TracksBA', lambda c, i=i: kd(c.audio[i]['TracksBA'], 'TracksBA'))]
    for name, read in pairs:
        try:
            if read(expected) != read(got):
                return name
        except (CodecError, IndexError, KeyError, TypeError):
            return name
    return None


class _Writer:
    def __init__(self, facts, tpl, placement, derived, taken, new_id, keep_ids=False):
        self.facts, self.tpl, self.placement, self.d = facts, tpl, placement, derived
        self.notes = []
        self.template_ids = set(tpl.ids())
        self.keep_ids = keep_ids
        self.ids = _Ids(taken, set() if keep_ids else self.template_ids, new_id)
        self.media_id = tpl.media_id if keep_ids else self.ids()
        self.video_id = (tpl.video['BtVideoInfo_id'] if keep_ids else self.ids()) if tpl.video is not None else None
        self.audio_ids = [a['BtAudioInfo_id'] if keep_ids else self.ids() for a in tpl.audio]
        if len(self.audio_ids) != len(derived.audio):
            raise MediaPoolError('kind_mismatch', f'the template has {len(self.audio_ids)} audio streams, the file '
                                 f'{len(derived.audio)}', 'give a template of the same kind')

    def _fresh(self, entries, what):
        entry = kd_get(entries, 'UniqueId')
        if entry is None or not isinstance(entry[3], str):
            raise _bad(f'{what} has no UniqueId')
        self.template_ids.add(entry[3])
        if not self.keep_ids:
            entry[3] = self.ids()

    def _clip(self, blob, what, video, audio):
        def change(fields):
            numbers = [f[0] for f in fields]
            unknown = sorted(set(numbers) - set(_CLIP_WIRES))
            if unknown or len(numbers) != len(set(numbers)):
                raise _bad(f'{what} holds fields this writer does not know or repeats: {unknown}')
            if any(f[1] != _CLIP_WIRES[f[0]] for f in fields):
                raise _bad(f'{what} has a field of an unexpected wire type')
            for number in _CLIP_PROJECT_LOCAL:
                if pb_remove(fields, number):
                    self.notes.append(f'{what} field {number} (project-local) left out, as on a fresh import')
            for f in fields:
                if f[0] == 1:
                    f[2] = self.d.folder.encode()
                elif f[0] in (2, 6):
                    f[2] = self.d.name.encode()
                elif f[0] == 3:
                    f[2] = self.d.mtime_text.encode()
                elif f[0] == 5:
                    if video:
                        if self.d.codec_tag is None:
                            raise _bad(f'{what} has a codec field but the file has no codec tag')
                        f[2] = self.d.codec_tag.encode()
                    else:
                        f[2] = audio['codec'].encode()
                elif f[0] == 7:
                    if not video:
                        raise _bad(f'{what}: an audio Clip with field 7')
                    self.template_ids.add(f[2].decode('utf-8', 'replace'))
                    if not self.keep_ids:
                        f[2] = self.ids().encode()
                elif f[0] == 13:
                    f[2] = self.d.created_us
            for number in (1, 2, 3, 13):
                pb_get(fields, number, what)
        return edit_message(blob, what, change)

    def _video(self):
        tpl = self.tpl.video
        row = dict(tpl)
        row['BtVideoInfo_id'], row['Sm2MpMedia_id'], row['MediaMetadata'] = self.video_id, self.media_id, None
        row['Clip'] = self._clip(tpl['Clip'], 'BtVideoInfo.Clip', True, None)

        def time(entries):
            _closed(entries, _TIME_KEYS, 'Time')
            self._fresh(entries, 'Time')
            if kd_get(entries, 'Timecode') is not None:
                if self.d.timecode is None:
                    raise MediaPoolError('kind_mismatch', 'the template has a timecode, the file none')
                kd_set(entries, 'Timecode', self.d.timecode, 'Time')
            start = kd_get(entries, 'StartFrame')
            if start is None or start[3] != 0:
                raise _bad('Time.StartFrame is not 0')
            kd_set(entries, 'NumFrames', self.d.frames, 'Time')
            kd_set(entries, 'FrameRate', rate_bytes(self.d.rate), 'Time')
        row['Time'] = edit_kd(tpl['Time'], 'Time', time)

        def geometry(entries):
            _closed(entries, _GEOMETRY_KEYS, 'Geometry')
            self._fresh(entries, 'Geometry')
            w, h = self.d.resolution
            kd_set(entries, 'Resolution', struct.pack('>IIII', 0, w, 0, h), 'Geometry')
            kd_set(entries, 'FrameSize', self.d.frame_size, 'Geometry')
        row['Geometry'] = edit_kd(tpl['Geometry'], 'Geometry', geometry)

        def proxy(entries):
            _closed(entries, _PROXY_KEYS, 'Proxy')
            self._fresh(entries, 'Proxy')
        row['Proxy'] = edit_kd(tpl['Proxy'], 'Proxy', proxy)

        def metadata(entries):
            _closed(entries, _VIDEO_METADATA_KEYS, 'VideoMetadata')
            self._fresh(entries, 'VideoMetadata')
            if kd_get(entries, 'ActualReelNumber') is not None:
                raise MediaPoolError('cannot_derive', 'the template stores a reel number, and no rule for it is proven',
                                     'import the file in Resolve by hand')
        row['VideoMetadata'] = edit_kd(tpl['VideoMetadata'], 'VideoMetadata', metadata)
        return row

    def _audio(self, index):
        tpl, info = self.tpl.audio[index], self.d.audio[index]
        row = dict(tpl)
        row['BtAudioInfo_id'], row['Sm2MpMedia_id'], row['MediaMetadata'] = self.audio_ids[index], self.media_id, None
        row['Clip'] = self._clip(tpl['Clip'], f'BtAudioInfo[{index}].Clip', False, info)

        def tracks(entries):
            if [e[0] for e in entries] != ['0'] or not isinstance(entries[0][3], bytes):
                raise _bad('TracksBA does not hold one track')

            def track(t):
                _closed(t, _TRACK_KEYS, 'TracksBA track')
                self._fresh(t, 'TracksBA track')
                kd_set(t, 'StartTime', self.d.start, 'TracksBA track')
                kd_set(t, 'SampleRate', info['sample_rate'], 'TracksBA track')
                kd_set(t, 'NumChannels', info['channels'], 'TracksBA track')
                kd_set(t, 'Duration', info['samples'], 'TracksBA track')
                kd_set(t, 'CodecName', info['codec'], 'TracksBA track')
                kd_set(t, 'BitDepth', info['bit_depth'], 'TracksBA track')
                layout = kd_get(t, 'ChannelLayout')
                if (layout is None) != (info['layout'] is None):
                    raise MediaPoolError('kind_mismatch', "the template's channel layout differs")
                if layout is not None:
                    layout[3] = info['layout']
            entries[0][3] = edit_kd(entries[0][3], 'TracksBA track', track)
        row['TracksBA'] = edit_kd(tpl['TracksBA'], 'TracksBA', tracks)
        return row

    def _extents(self, blob):
        def change(entries):
            if [e[0] for e in entries] != [str(i) for i in range(len(self.d.audio))]:
                raise _bad("the clip's audio extents do not list its audio streams in order")
            for i, entry in enumerate(entries):
                info, old_ref = self.d.audio[i], self.tpl.audio[i]['BtAudioInfo_id']

                def one(t, info=info, old_ref=old_ref, i=i):
                    _closed(t, _EXTENTS_KEYS, "the clip's audio extents")
                    kd_set(t, 'MediaExtents', struct.pack('<dd', self.d.start, info['extent']), 'extents')
                    kd_set(t, 'CodecName', info['codec'], 'extents')
                    if kd_get(t, 'BitDepth') is not None:
                        kd_set(t, 'BitDepth', info['bit_depth'], 'extents')
                    if kd_get(t, 'SampleRate') is not None:
                        kd_set(t, 'SampleRate', info['sample_rate'], 'extents')

                    def channel_list(c):
                        if sorted(e[0] for e in c) != sorted(str(n) for n in range(info['channels'])):
                            raise _bad("ChannelVecBA does not list the stream's channels")

                        def ref(r, n):
                            entry = kd_get(r, 'MediaRef')
                            if entry is None or entry[3] != old_ref:
                                raise _bad('a ChannelVecBA entry does not name its audio info')
                            entry[3] = self.audio_ids[i]
                            idx = kd_get(r, 'ChannelIdx')
                            if (idx is None) != (n == 0) or (idx is not None and idx[3] != n):
                                raise _bad('a ChannelVecBA entry has an unexpected ChannelIdx')
                            if [e[0] for e in r if e[0] not in ('MediaRef', 'ChannelIdx')]:
                                raise _bad('a ChannelVecBA entry holds keys this writer does not know')
                        for ch in c:
                            ch[3] = edit_kd(ch[3], 'ChannelVecBA entry', lambda r, n=int(ch[0]): ref(r, n))
                    vec = kd_get(t, 'ChannelVecBA')
                    if vec is None:
                        raise _bad("the clip's audio extents have no ChannelVecBA")
                    vec[3] = edit_kd(vec[3], 'ChannelVecBA', channel_list)
                entry[3] = edit_kd(entry[3], "the clip's audio extents", one)
        return edit_kd(blob, "the clip's audio extents", change)

    def _media_fields(self, blob):
        def change(outer):
            first = pb_get(outer, 1, 'Sm2MpMedia.FieldsBlob')
            others = [f for f in outer if f[0] != 1]
            if self.tpl.is_audio_clip:
                rate = pb_get(outer, 2, 'Sm2MpMedia.FieldsBlob')
                inner = pb_decode(rate[2], "the sound clip's frame rate")
                if [(f[0], f[1]) for f in inner] != [(1, 1)]:
                    raise _bad("the sound clip's frame rate field is not one double")
                inner[0][2] = struct.pack('<d', float(self.placement.project_rate))
                rate[2] = pb_encode(inner)
                if len(others) != 1:
                    raise _bad("a sound clip's FieldsBlob holds fields this writer does not know")
                self._media_level(first)
                return
            if others:
                if len(others) != 1 or others[0][0] != 2 or not _is_relink_history(others[0][2]):
                    raise _bad("a video clip's FieldsBlob holds fields this writer does not know")
                pb_remove(outer, 2)
                self.notes.append("the template's relink history (FieldsBlob field 2) left out, as on a fresh import")
            v = pb_decode(first[2], 'Sm2MpMedia.FieldsBlob #1')
            if pb_remove(v, 3):
                self.notes.append('FieldsBlob #1.#3 (project-local) left out, as on a fresh import')
            if sorted(f[0] for f in v) != [1, 2]:
                raise _bad("a video clip's FieldsBlob #1 holds fields this writer does not know")
            self._media_level(pb_get(v, 1, 'FieldsBlob #1.#1'))
            first[2] = pb_encode(v)
        return edit_message(blob, 'Sm2MpMedia.FieldsBlob', change)

    def _media_level(self, level):
        w = pb_decode(level[2], "the clip's media fields")
        allowed = {1, 5} if self.d.audio else {1}
        if not {f[0] for f in w} <= allowed or len({f[0] for f in w}) != len(w):
            raise _bad("the clip's media fields hold fields this writer does not know")
        x = pb_get(w, 1, "the clip's media fields")
        xf = pb_decode(x[2], "the clip's date fields")
        if sorted(f[0] for f in xf) != [1, 6]:
            raise _bad("the clip's date fields are not the known pair")
        date = pb_get(xf, 1, "the clip's date fields")
        df = pb_decode(date[2], "the clip's date added")
        if [(f[0], f[1]) for f in df] != [(2, 0)]:
            raise _bad("the clip's date added is not one varint")
        df[0][2] = _zigzag(self.placement.added_at)
        date[2] = pb_encode(df)
        x[2] = pb_encode(xf)
        extents = [f for f in w if f[0] == 5]
        if self.d.audio:
            if len(extents) != 1:
                raise _bad('the clip has audio but its FieldsBlob lists no audio extents')
            extents[0][2] = self._extents(extents[0][2])
        level[2] = pb_encode(w)

    def _media(self):
        row = dict(self.tpl.media)
        row['Sm2MpMedia_id'] = self.media_id
        row['UniqueMediaPoolItemId'] = row['UniqueMediaPoolItemId'] if self.keep_ids else self.ids()
        row['Name'] = self.d.name
        row['MpFolder'] = row['Sm2MpFolder_id'] = self.placement.folder_id
        row['Video'] = self.video_id
        for column in _MEDIA_CLEARED:
            if column in row:
                row[column] = None
        row['FieldsBlob'] = self._media_fields(self.tpl.media['FieldsBlob'])
        return row

    def _metadata(self, tpl, owner):
        if self.placement.lockable_map_id is None:
            raise MediaPoolError('cannot_derive', 'the kind has a metadata blob and the project has no lockable map')
        row = dict(tpl)
        row['BtLockableBlob_id'] = tpl['BtLockableBlob_id'] if self.keep_ids else self.ids()
        row['BtLockableBlobMap_id'], row['BlobOwner'] = self.placement.lockable_map_id, owner
        row['DbSavedTime'] = self.placement.saved_age_ms

        def outer(entries):
            if [e[0] for e in entries] != ['BlobData']:
                raise _bad('the metadata blob is not one BlobData')

            def data(fields):
                holder = pb_get(fields, 2, 'metadata BlobData')
                original, payload = holder[2], unwrap(holder[2], 'metadata entries')
                listed = pb_decode(payload, 'metadata entries')
                for entry in listed:
                    if entry[0] != 2 or entry[1] != 2:
                        raise _bad('a metadata entry is not field 2')
                    sub = pb_decode(entry[2], 'metadata entry')
                    if [(f[0], f[1]) for f in sub] != [(1, 0), (2, 2)]:
                        raise _bad('a metadata entry is not a key and a value')
                    if sub[0][2] == 215:
                        sub[1][2] = self.facts.name.encode()
                    elif sub[1][2] != b'':
                        raise MediaPoolError('cannot_derive', f'metadata entry {sub[0][2]} holds a value in the template '
                                             'and no rule derives it (camera metadata)', 'import the file in Resolve')
                    entry[2] = pb_encode(sub)
                holder[2] = rewrap(original, pb_encode(listed), 'metadata entries')
            entries[0][3] = edit_message(entries[0][3], 'metadata BlobData', data)
        row['FieldsBlob'] = edit_kd(tpl['FieldsBlob'], 'the metadata blob', outer)
        return row

    def build(self):
        out = {'Sm2MpMedia': [self._media()]}
        if self.tpl.video is not None:
            out['BtVideoInfo'] = [self._video()]
        out['BtAudioInfo'] = [self._audio(i) for i in range(len(self.tpl.audio))]
        out['BtAudioInfo_Sm2MpMedia'] = [dict(link, DbOwner=self.media_id, DbAssociate=self.audio_ids[i])
                                         for i, link in enumerate(self.tpl.audio_links)]
        out['Sm2MpFolder_Sm2MpMedia'] = [dict(self.tpl.folder_link, DbOwner=self.placement.folder_id,
                                              DbAssociate=self.media_id, DbIndex=self.placement.folder_index)]
        owner = self.video_id if self.video_id is not None else self.audio_ids[0]
        out['BtLockableBlob'] = [self._metadata(r, owner) for r in self.tpl.lockables]
        if self.tpl.presets:
            self.notes.append("the template's pan/tilt/zoom preset left out, as on a fresh import")
        result = {t: tuple(rows) for t, rows in out.items() if rows}
        if not self.keep_ids:
            self._check_no_template_left(result)
        return result

    def _check_no_template_left(self, rows):
        needles = {i: f'template id {i}' for i in self.template_ids if i}
        media = self.tpl.media
        for value, label in ((media.get('MpFolder'), "the template's bin id"),
                             (self.tpl.lockables[0].get('BtLockableBlobMap_id') if self.tpl.lockables else None,
                              "the template's lockable map id")):
            if isinstance(value, str) and value and value not in (self.placement.folder_id,
                                                                    self.placement.lockable_map_id):
                needles[value] = label
        folder, name = clip_name(self.tpl)
        for text, label in ((folder, "the template's folder"), (name, "the template's file name")):
            if text and text not in self.d.folder and text not in self.d.name:
                needles[text] = label
        for table, table_rows in rows.items():
            for row in table_rows:
                for column, value in row.items():
                    found = contains_text(value, needles)
                    if found is not None:
                        raise _bad(f'{found} is still in the new {table}.{column}: somewhere this writer does not '
                                   'rewrite')


def _is_relink_history(value):
    if not isinstance(value, bytes):
        return False
    try:
        entries = kd_decode(value, 'relink history')
    except CodecError:
        return False
    return {e[0] for e in entries} <= {'VIDEO_INFO', 'AUDIO_INFO_VEC', 'AUDIO_TRACKS_EXTENTS'}


def project_frame_rate(config_rows):
    """The project's frame rate from ``SM_Config`` ``SetupBA`` field 69 (text such as ``29.97``)."""
    if len(config_rows) != 1 or not isinstance(config_rows[0].get('FieldsBlob'), bytes):
        return None
    try:
        setup = blobs.keyed_dict(config_rows[0]['FieldsBlob']).get('SetupBA')
        if not isinstance(setup, bytes):
            return None
        fields = blobs.protobuf(unwrap(setup, 'SetupBA'))
    except (blobs.BlobError, CodecError):
        return None
    texts = [v for n, _w, v in fields if n == 69 and isinstance(v, bytes)]
    if len(texts) != 1 or not re.match(rb'[0-9]{1,3}(?:\.[0-9]{1,6})?\Z', texts[0]):
        return None
    value = Fraction(texts[0].decode('ascii'))
    nearest = min(STANDARD_RATES, key=lambda s: abs(value - s))
    return nearest if abs(value - nearest) <= nearest / 1000 else None

