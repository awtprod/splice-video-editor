"""Timeline item rows: a picture or sound of a media clip at full speed, and sound crossfades.

Ported (without pydantic, for Python 3.10+) from ai-agent-resolve ``src/aar/drp/items/``
(``author.py``, ``media.py``, ``rows.py``, ``transitions.py``) at commit
baa82027bc9212d5b64572b70806f0d69f8812fa (same owner), where the field map reproduced 5,054
of Resolve 21.1's own items and 112 of its sound transitions field for field (docs/DESIGN.md
there, "Timeline items (S25c2)" and "Transitions and the latest save (S25e)").

An item copies a few facts of its media clip (name, path, start, length, rate, channels), is
placed (``Start``, ``Duration``, ``In`` as text frame counts counted from frame 0, the track and
its place in the track's ``Items`` vector, the link list in ``FieldsBlob``), and every other
column holds the one value every plain item Resolve wrote holds (:data:`ITEM_CONSTANTS`).
"""
import math
import struct
import uuid
from fractions import Fraction

from . import blobs
from .codec import (KD_STRING, CodecError, envelope, kd_decode, kd_encode, parse_envelope, pb_decode, pb_encode,
                    pb_get, unwrap)
from .mediapool import STANDARD_RATES, MediaPoolError, guarded, start_seconds

PICTURE, SOUND, TRANSITION = 'Sm2TiVideoClip', 'Sm2TiAudioClip', 'Sm2TiTransition'
ITEM_TYPES = {PICTURE: 'picture', SOUND: 'sound'}
MAX_FRAMES = 24 * 3600 * 60
CHANNELS = (1, 2)
_CHANNEL_BASE = 0x4000
ITEM_COLUMNS = (
    'Sm2TiItem_id', 'DbType', 'Name', 'Start', 'Duration', 'Track', 'LinkedItemSync', 'MarkersBA', 'UiMemento',
    'Flags', 'PriorityIndex', 'In', 'MediaRef', 'MediaStartTime', 'MediaFilePath', 'MediaReelNumber',
    'MediaTimemapBA', 'Group', 'pLmVerTable', 'pAuxLmVerTable', 'Thumbnail', 'ThumbnailDirtyFlag',
    'RenderTextEnabled', 'RenderTextGanged', 'RenderTextPrefixed', 'TextRenderItemVec', 'LastChangedTime',
    'LastRenderedTime', 'IsMarkedForCaching', 'IsForceConformed', 'MatchConflictState', 'MediaTrackIdx',
    'PrettyType', 'Resolution', 'EffectFiltersBA', 'IsPreConformed', 'PreConformMediaExtents',
    'ImportExportMetadataBA', 'MediaFrameRate', 'Sm2TiTrack_id', 'VirtualAudioTrackBA', 'MixedFrameRateAlignment',
    'UseOppositeSrcForLeftEye', 'UseOppositeSrcForRightEye', 'WasDisbanded', 'CurrentSelectorIdx', 'AlignmentType',
    'Position', 'RenderCacheBA', 'ClipGroup', 'MediaMetadata', 'FieldsBlob', 'CompositionTable', 'BlobLockSysId',
    'OriginalClip', 'Sm2TiItem_Owner_id', 'CompTableLockSysId', 'VersionTableLockSysId')
#: Columns a later edit fills in; a fresh item has them NULL (the tag names what it carries).
LATER_COLUMNS = {'pLmVerTable': 'grade', 'CompositionTable': 'Fusion composition', 'EffectFiltersBA': 'effects',
                 'ImportExportMetadataBA': 'import metadata', 'MarkersBA': 'markers', 'MediaMetadata': 'item metadata'}
#: The value every plain picture and sound item of the two Resolve 21.1 saves holds.
ITEM_CONSTANTS = {
    'Track': None, 'LinkedItemSync': None, 'UiMemento': 0, 'Flags': 0, 'PriorityIndex': 0, 'MediaReelNumber': None,
    'Group': None, 'pAuxLmVerTable': None, 'Thumbnail': None, 'ThumbnailDirtyFlag': 1, 'RenderTextEnabled': 1,
    'RenderTextGanged': 1, 'RenderTextPrefixed': 1, 'TextRenderItemVec': None, 'LastChangedTime': 0,
    'LastRenderedTime': 0, 'IsMarkedForCaching': 0, 'IsForceConformed': 1, 'MatchConflictState': 0,
    'PrettyType': None, 'Resolution': None, 'IsPreConformed': 0, 'MixedFrameRateAlignment': 0.0,
    'UseOppositeSrcForLeftEye': 0, 'UseOppositeSrcForRightEye': 0, 'WasDisbanded': 0, 'AlignmentType': None,
    'Position': None, 'RenderCacheBA': None, 'ClipGroup': None, 'BlobLockSysId': '', 'OriginalClip': None,
    'Sm2TiItem_Owner_id': None, 'CompTableLockSysId': '', 'VersionTableLockSysId': ''}
PRECONFORM = {'picture': bytes.fromhex('00000100000030c20000010000003042'), 'sound': None}
#: ``FieldsBlob`` beyond the link list: ``#1.#4`` = 1, then a picture's ``#2`` (these 16
#: bytes) or a sound's ``#15`` = 4 (every plain item of the corpus).
PICTURE_TAIL = bytes.fromhex('000000000000000600000000ffffffff')
SOUND_KINDS = {'Cross Fade 0 dB': 'Cross Fade 0DB', 'Cross Fade +3 dB': 'Cross Fade +3DB'}
#: Splice's audio crossfade names (``transitionMatchName``) -> Resolve's menu names.
SPLICE_FADES = {'Constant Gain': 'Cross Fade 0 dB', 'Constant Power': 'Cross Fade +3 dB'}
TRANSITION_RATES = frozenset({Fraction(30000, 1001)})
MIN_TRANSITION, MAX_TRANSITION = 2, 16
SOUND_FIELD = bytes.fromhex('0000002c789c636660642016000000da0005')
SOUND_PAYLOAD = bytes.fromhex('030001') + bytes(41)
TRANSITION_CONSTANTS = {
    'Name': 'Transition', 'Track': None, 'LinkedItemSync': None, 'MarkersBA': None, 'UiMemento': 0, 'Flags': 0,
    'PriorityIndex': 0, 'In': None, 'MediaRef': None, 'MediaStartTime': None, 'MediaFilePath': None,
    'MediaReelNumber': None, 'MediaTimemapBA': None, 'Group': None, 'pLmVerTable': None, 'pAuxLmVerTable': None,
    'Thumbnail': None, 'ThumbnailDirtyFlag': 1, 'RenderTextEnabled': 1, 'RenderTextGanged': 1,
    'RenderTextPrefixed': 1, 'TextRenderItemVec': None, 'LastChangedTime': None, 'LastRenderedTime': None,
    'IsMarkedForCaching': None, 'IsForceConformed': 1, 'MatchConflictState': 0, 'MediaTrackIdx': None,
    'Resolution': None, 'EffectFiltersBA': None, 'IsPreConformed': 0, 'PreConformMediaExtents': None,
    'ImportExportMetadataBA': None, 'MediaFrameRate': None, 'VirtualAudioTrackBA': None,
    'MixedFrameRateAlignment': 0.0, 'UseOppositeSrcForLeftEye': 0, 'UseOppositeSrcForRightEye': 0,
    'WasDisbanded': 0, 'CurrentSelectorIdx': 0, 'RenderCacheBA': None, 'ClipGroup': None, 'MediaMetadata': None,
    'CompositionTable': None, 'BlobLockSysId': '', 'OriginalClip': None, 'Sm2TiItem_Owner_id': None,
    'CompTableLockSysId': '', 'VersionTableLockSysId': ''}


class ItemError(MediaPoolError):
    """A refusal of the item writer (codes ``bad_media``, ``bad_placement``, ``unsupported_item``,
    ``bad_template``, ``id_collision``)."""


def _bad_media(why):
    return ItemError('bad_media', why, 'use a media clip Resolve saved, or one authored from facts')


# --------------------------------------------------------------------------- what an item takes from its clip

class MediaView:
    def __init__(self, **values):
        self.__dict__.update(values)


def _kd(value, what):
    out = {}
    for key, _t, _n, item in kd_decode(value, what):
        if key in out:
            raise _bad_media(f'{what} holds {key!r} twice')
        out[key] = item
    return out


def _rate(data, what):
    if not isinstance(data, bytes) or len(data) != 16 or any(data[8:]):
        raise _bad_media(f'{what} is not a frame rate (a double and 8 zero bytes)')
    value = struct.unpack('<d', data[:8])[0]
    rate = next((r for r in STANDARD_RATES if float(r) == value), None)
    if rate is None:
        raise _bad_media(f'{what} {value!r} is not exactly a standard rate')
    return data, rate


def _number(value, what, kind):
    if not isinstance(value, kind) or isinstance(value, bool):
        raise _bad_media(f'{what} is not a {kind.__name__}')
    return value


def _first_track(info):
    tracks = _kd(info['TracksBA'], 'TracksBA')
    if len(tracks) != 1:
        raise _bad_media(f'an audio info has {len(tracks)} tracks, not one')
    return _kd(next(iter(tracks.values())), 'TracksBA track')


@guarded('bad_media')
def media_view(clip):
    """The values an item takes from ``clip`` (a :class:`ClipRows`)."""
    row = clip.video if clip.video is not None else clip.audio[0]
    fields = pb_decode(unwrap(row['Clip'], 'Clip'), 'Clip')
    folder, name = pb_get(fields, 1, 'Clip')[2], pb_get(fields, 2, 'Clip')[2]
    path = folder.decode('utf-8') + '\\' + name.decode('utf-8')
    channels = tuple(int(_number(_first_track(info).get('NumChannels'), 'NumChannels', int)) for info in clip.audio)
    sound_seconds = sound_samples = None
    if clip.audio:
        track = _first_track(clip.audio[0])
        samples = int(_number(track.get('Duration'), 'Duration', int))
        sample_rate = int(_number(track.get('SampleRate'), 'SampleRate', int))
        if sample_rate <= 0 or samples < 0:
            raise _bad_media('the first audio track has no samples or no sample rate')
        sound_seconds, sound_samples = samples / sample_rate, Fraction(samples, sample_rate)
    if clip.video is not None:
        time = _kd(clip.video['Time'], 'Time')
        rate_data, rate = _rate(time.get('FrameRate'), 'Time.FrameRate')
        frames = int(_number(time.get('NumFrames'), 'Time.NumFrames', int))
        if frames < 1:
            raise _bad_media('Time.NumFrames is below 1')
        timecode = time.get('Timecode')
        if timecode is not None and not isinstance(timecode, str):
            raise _bad_media('Time.Timecode is not text')
        double = float(rate)
        start = start_seconds(timecode, rate)
        last = frames / double - 1 / double
        picture_frames = frames
    else:
        media_fields = pb_decode(unwrap(clip.media['FieldsBlob'], 'Sm2MpMedia.FieldsBlob'), 'Sm2MpMedia.FieldsBlob')
        inner = pb_get(media_fields, 2, 'Sm2MpMedia.FieldsBlob')
        rate_field = pb_get(pb_decode(inner[2], 'FieldsBlob #2'), 1, 'FieldsBlob #2')
        if rate_field[1] != 1:
            raise _bad_media('Sm2MpMedia.FieldsBlob #2.#1 is not a double')
        rate_data, rate = _rate(rate_field[2] + bytes(8), "the sound clip's import rate")
        double = float(rate)
        start = float(_number(_first_track(clip.audio[0]).get('StartTime'), 'StartTime', float))
        if not math.isfinite(start):
            raise _bad_media("the first audio track's StartTime is not finite")
        last = sound_seconds * double / double - 1 / double
        picture_frames = None
    name_value = clip.media.get('Name')
    if not isinstance(name_value, str):
        raise _bad_media('Sm2MpMedia.Name is not text')
    sound_start = None
    if clip.video is not None and clip.audio:
        value = _first_track(clip.audio[0]).get('StartTime')
        sound_start = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return MediaView(media_id=clip.media_id, name=name_value, path=path, picture=clip.video is not None,
                     channels=channels, rate_bytes=rate_data, rate=rate, start_seconds=start, last_seconds=last,
                     sound_start=sound_start,
                     frames=picture_frames, sound_seconds=sound_seconds,
                     sound_frames=None if sound_samples is None else math.ceil(sound_samples * rate))


#: Seconds within which a clip's picture timecode and sound start count as the same start.
SAME_START = 1e-6


def start_conflict(view):
    """Whether ``view`` is a picture clip whose sound track starts elsewhere than its picture
    timecode. Resolve stores either value as its items' ``MediaStartTime``: on the real samples
    the same pool clips hold the picture timecode on the items of some timelines and the sound
    start on those of another, and nothing in the clip's rows says which a new item gets."""
    if not view.picture or view.sound_start is None:
        return False
    return not math.isfinite(view.sound_start) or abs(view.sound_start - view.start_seconds) > SAME_START


def media_start(view, timeline_items):
    """``(MediaStartTime, None)`` for a new item playing ``view`` on a timeline whose items are
    ``timeline_items`` (seed rows), or ``(None, why)`` when the value cannot be known.

    The value the clip's other items on the same timeline hold, when they agree (Resolve keeps
    one start per clip and timeline: on the real samples items of a timeline imported from
    FCPXML hold the exact quotient of the timecode and the rate, one unit in the last place
    from the pool's value, and the items of one timeline hold a clip's sound start where its
    other timelines hold its picture timecode); otherwise the rule (the picture timecode over
    the rate, or a sound file's own start), unless the clip's picture timecode and sound start
    differ: then nothing shows which a new item gets, and the item is refused."""
    held = {row.get('MediaStartTime') for row in timeline_items
            if row.get('MediaRef') == view.media_id and row.get('DbType') in (PICTURE, SOUND)}
    value = next(iter(held)) if len(held) == 1 else None
    usable = isinstance(value, float) and math.isfinite(value)
    if usable and (start_conflict(view) or abs(value - view.start_seconds) <= SAME_START):
        return value, None
    if not start_conflict(view):
        return view.start_seconds, None
    why = ('its other items on this timeline hold different starts' if len(held) > 1 else
           'no item of it is on this timeline to take the start from')
    return None, why


def timemap(seconds):
    return b'\x02' + struct.pack('>d', seconds)


# --------------------------------------------------------------------------- links

def _fields(item):
    # An item's FieldsBlob can hold Fairlight clip effects of several MB: the reader's own bound.
    return pb_decode(unwrap(item.get('FieldsBlob'), 'item FieldsBlob', blobs.MAX_BLOB_OUTPUT), 'item FieldsBlob')


@guarded('bad_template')
def links_of(item):
    """The ids an item is linked to (``FieldsBlob`` #1.#1.#1, keys ``0``..), in key order."""
    first = pb_get(_fields(item), 1, 'item FieldsBlob')
    groups = [f for f in pb_decode(first[2], 'item FieldsBlob #1') if f[0] == 1]
    if not groups:
        return ()
    if len(groups) > 1:
        raise ItemError('bad_template', 'item FieldsBlob #1.#1 occurs twice')
    for number, _wire, value in pb_decode(groups[0][2], 'item FieldsBlob #1.#1'):
        if number == 1:
            entries = kd_decode(value, 'item links')
            keys = [e[0] for e in entries]
            if sorted(keys, key=int) != [str(i) for i in range(len(keys))]:
                raise ItemError('bad_template', f'item links have keys {keys[:6]}')
            by_key = {e[0]: e[3] for e in entries}
            out = tuple(by_key[str(i)] for i in range(len(keys)))
            if not all(isinstance(v, str) for v in out):
                raise ItemError('bad_template', 'an item link is not an id')
            return out
    return ()


def link_list(partners):
    """The link list: keys ``0``.. naming the partners, written highest key first."""
    return kd_encode(list(reversed([[str(i), KD_STRING, 0, p] for i, p in enumerate(partners)])))


@guarded('bad_template')
def relink(item, partners):
    """``item``'s ``FieldsBlob`` with its link list set to ``partners`` (none removes it), every
    other field kept as it is, framed the way Resolve frames a message of its length."""
    blob = item.get('FieldsBlob')
    top = _fields(item)
    first = pb_get(top, 1, 'item FieldsBlob')
    inner = pb_decode(first[2], 'item FieldsBlob #1')
    groups = [f for f in inner if f[0] == 1]
    if len(groups) > 1:
        raise ItemError('bad_template', 'item FieldsBlob #1.#1 occurs twice')
    group = groups[0] if groups else None
    members = pb_decode(group[2], 'item FieldsBlob #1.#1') if group else []
    members = [f for f in members if f[0] != 1]
    if partners:
        members.insert(0, [1, 2, link_list(partners)])
    if group is None and members:
        inner.insert(0, [1, 2, pb_encode(members)])
    elif group is not None and members:
        group[2] = pb_encode(members)
    elif group is not None:
        inner.remove(group)
    first[2] = pb_encode(inner)
    out = envelope(pb_encode(top), version=_version(blob), what='item FieldsBlob')
    if links_of({'FieldsBlob': out}) != tuple(partners):
        raise ItemError('bad_template', 'the rewritten link list does not read back')
    return out


def _version(blob):
    parsed = parse_envelope(blob)
    if parsed is None or parsed[0] is None:
        raise ItemError('bad_template', 'item FieldsBlob is not a versioned envelope')
    return parsed[0]


def picture_tail(rows):
    """The ``FieldsBlob`` #2 the picture items of a project hold (the most common one; it is
    ``...06...`` on Resolve 21.1's answer-key saves and ``...05...`` on others), or
    :data:`PICTURE_TAIL` when the project has no picture item."""
    from collections import Counter
    found = Counter()
    for row in rows:
        if row.get('DbType') != PICTURE:
            continue
        try:
            for number, wire, value in blobs.protobuf(unwrap(row.get('FieldsBlob'), 'item FieldsBlob',
                                                              blobs.MAX_BLOB_OUTPUT)):
                if number == 2 and wire == 2 and len(value) == 16:
                    found[value] += 1
        except (CodecError, blobs.BlobError):
            continue
    return found.most_common(1)[0][0] if found else PICTURE_TAIL


def fresh_fields(kind, partners, tail=None):
    """A new item's ``FieldsBlob``: the link list (when linked), ``#1.#4`` = 1, then a picture's
    ``#2`` (:data:`PICTURE_TAIL`) or a sound's ``#15`` = 4, version 2, framed by length."""
    inner = ([[1, 2, pb_encode([[1, 2, link_list(partners)]])]] if partners else []) + [[4, 0, 1]]
    top = [[1, 2, pb_encode(inner)], [2, 2, tail or PICTURE_TAIL] if kind == 'picture' else [15, 0, 4]]
    return envelope(pb_encode(top), version=2, what='item FieldsBlob')


# --------------------------------------------------------------------------- authoring items

def virtual_audio(count):
    """``VirtualAudioTrackBA``: ``ChannelsBA`` = 2, the count, then one id per channel;
    ``AudioType`` 1 for mono, 0 for stereo."""
    if count not in CHANNELS:
        raise ItemError('unsupported_item', f'a sound of {count} channels; only mono and stereo items were seen',
                        'use a mono or stereo file')
    ids = b''.join(struct.pack('>I', _CHANNEL_BASE + i + 1) for i in range(count))
    return kd_encode([['ChannelsBA', 0x0C, 0, struct.pack('>II', 2, count) + ids],
                      ['AudioType', 0x02, 0, 1 if count == 1 else 0]])


@guarded('bad_placement')
def author_item(view, kind, track_id, index, start, duration, src_in, partners=(), item_id=None, columns=ITEM_COLUMNS,
                name=None, tail=None):
    """The ``Sm2TiItem`` row and ``Items`` join row of one item playing ``view`` (a
    :class:`MediaView`) at full speed. Every column not derived or placed holds
    :data:`ITEM_CONSTANTS` (what a template item of a supported shape holds; the other
    repository copied it from one, and refused a template holding anything else)."""
    if kind not in ('picture', 'sound'):
        raise ItemError('bad_placement', f'kind {kind!r} is not picture or sound')
    for label, value in (('index', index), ('start', start), ('duration', duration), ('source in', src_in)):
        if type(value) is not int or not 0 <= value <= MAX_FRAMES:
            raise ItemError('bad_placement', f'the {label} {value!r} is not a whole number from 0 to {MAX_FRAMES}')
    if duration < 1:
        raise ItemError('bad_placement', 'the duration is below one frame')
    if kind == 'picture':
        if not view.picture or src_in + duration > view.frames:
            raise ItemError('bad_placement', f'the picture reads frames {src_in}..{src_in + duration}, past the '
                            f'media\'s {view.frames or 0} frames')
    elif not view.channels or view.sound_frames is None or src_in + duration > view.sound_frames:
        raise ItemError('bad_placement', f'the sound reads frames {src_in}..{src_in + duration}, past the media\'s '
                        f'{view.sound_frames or 0} frames of sound')
    unknown = [c for c in ITEM_COLUMNS if c not in columns]
    if unknown or len(columns) != len(ITEM_COLUMNS):
        raise ItemError('bad_template', f'the seed\'s Sm2TiItem table does not have the columns of Resolve 21.1 '
                        f'({(unknown or [c for c in columns if c not in ITEM_COLUMNS])[:3]})')
    item_id = item_id or str(uuid.uuid4())
    row = dict(ITEM_CONSTANTS)
    row.update({c: None for c in LATER_COLUMNS})
    row.update(Sm2TiItem_id=item_id, DbType=PICTURE if kind == 'picture' else SOUND,
               Name=name if name is not None else view.name, Start=str(start), Duration=str(duration),
               In=str(src_in) if src_in else None, MediaRef=view.media_id, MediaStartTime=view.start_seconds,
               MediaFilePath=view.path, MediaTimemapBA=timemap(view.last_seconds), MediaFrameRate=view.rate_bytes,
               Sm2TiTrack_id=track_id, FieldsBlob=fresh_fields(kind, partners, tail), CurrentSelectorIdx=0,
               PreConformMediaExtents=PRECONFORM[kind], MediaTrackIdx=0 if kind == 'sound' else None,
               VirtualAudioTrackBA=virtual_audio(view.channels[0]) if kind == 'sound' else None)
    return row, {'DbOwner': track_id, 'DbAssociate': item_id, 'DbPropertyName': 'Items', 'DbIndex': index}


# --------------------------------------------------------------------------- sound crossfades

def frames_before(frames):
    """Centred on the cut, Resolve puts ``(length + 1) // 2`` frames before it."""
    return (frames + 1) // 2


def transition_problem(name, frames, rate):
    """Why a sound crossfade cannot be written (None when it can)."""
    if name not in SOUND_KINDS:
        return f'the {name} crossfade is not a kind the evidence covers (Cross Fade 0 dB and +3 dB)'
    if rate not in TRANSITION_RATES:
        return f'sound crossfades were only measured on 29.97 fps timelines, not {float(rate):g} fps'
    if type(frames) is not int or not MIN_TRANSITION <= frames <= MAX_TRANSITION:
        return f'{frames} frames is outside the lengths measured ({MIN_TRANSITION} to {MAX_TRANSITION})'
    return None


def author_transition(name, track_id, index, cut, frames, rate, item_id=None):
    """A centred sound crossfade's ``Sm2TiItem`` row and ``Items`` join row."""
    problem = transition_problem(name, frames, rate)
    if problem:
        raise ItemError('unsupported_item', problem)
    item_id = item_id or str(uuid.uuid4())
    row = dict(TRANSITION_CONSTANTS)
    row.update(Sm2TiItem_id=item_id, DbType=TRANSITION, Start=str(cut - frames_before(frames)), Duration=str(frames),
               PrettyType=SOUND_KINDS[name], AlignmentType=2, Position=2, Sm2TiTrack_id=track_id,
               FieldsBlob=envelope(pb_encode([[2, 2, SOUND_FIELD]]), version=2, codec='protobuf'))
    return row, {'DbOwner': track_id, 'DbAssociate': item_id, 'DbPropertyName': 'Items', 'DbIndex': index}


def transition_payload_ok():
    """Whether :data:`SOUND_FIELD` inflates to :data:`SOUND_PAYLOAD` (Qt's qCompress)."""
    import zlib
    size = struct.unpack('>I', SOUND_FIELD[:4])[0]
    return zlib.decompress(SOUND_FIELD[4:]) == SOUND_PAYLOAD and size == len(SOUND_PAYLOAD)


__all__ = ['ITEM_COLUMNS', 'ITEM_CONSTANTS', 'LATER_COLUMNS', 'ItemError', 'MediaView', 'author_item',
           'author_transition', 'fresh_fields', 'links_of', 'media_start', 'media_view', 'picture_tail', 'relink',
           'start_conflict', 'timemap',
           'transition_problem']
