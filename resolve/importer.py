"""Interpret a Resolve project (see :mod:`resolve.source`) and build Splice's editing model.

The media pool, timeline and marker decoding follows ai-agent-resolve ``src/aar/drp/reader.py``
at commit aff6573bcba41e66e782ea2c4714290f227408b0 (same owner). Clip links, retime maps,
transitions and the transform/opacity parameters of ``EffectFiltersBA`` are read here.

Anything Splice does not represent is listed in ``compatibilityWarnings`` with the timeline,
track and clip it belongs to; nothing is dropped without a line there.
"""
import math
from fractions import Fraction
from pathlib import PurePath, PureWindowsPath

from . import blobs
from .blobs import BlobError
from .source import ResolveError

MAX_TRACKS = 64
MAX_WARNINGS = 400
MAX_MARKERS = 100_000
MAX_TIMELINES = 2_000

_MEDIA_TYPES = {'Sm2MpVideoClip': 'video', 'Sm2MpAudioClip': 'audio', 'Sm2MpStillClip': 'still',
                'Sm2MpTimelineClip': 'timeline', 'Sm2MpCompoundClip': 'compound',
                'Sm2MpMulticamClip': 'multicam'}
_NESTED = {'timeline': 'nested timeline', 'compound': 'compound clip', 'multicam': 'multicam clip'}
_AUDIO_FADES = {'Cross Fade 0DB': 'Constant Gain', 'Cross Fade +3DB': 'Constant Power',
                'Cross Fade +3 dB': 'Constant Power', 'Cross Fade 0 dB': 'Constant Gain'}
# EffectFiltersBA: effect type 4 is the Inspector's Transform, 2 its Composite (opacity), 124 a
# sound clip's Volume. Measured in projects saved by Resolve 21.1 with one setting changed at a
# time: Transform parameter 40 (Position X) holds a fraction of the timeline width (+200 px on a
# 1920-wide timeline is 0.1041667), Volume parameter 95 holds decibels (+6 dB is 6.0). Position
# Y (41), Zoom (42, 43) and Opacity (Composite 1) were not measured: their units are inferred and
# every clip that uses them says so.
_TRANSFORM, _COMPOSITE, _AUDIO_VOLUME = 4, 2, 124
_POSITION_X, _POSITION_Y, _ZOOM_X, _ZOOM_Y = 40, 41, 42, 43
_OPACITY = 1
_VOLUME_DB = 95
# Splice's clip volume is a linear gain in percent, 0 to 200 (+6.02 dB).
MAX_VOLUME = 200.0
# Sm2TiTrack/Flags and Sm2TiVideoClip/Flags, measured the same way: a track's Flags go 0 -> 1 when
# it is locked and 0 -> 2 when it is disabled (a video track) or muted (an audio track); a picture
# clip's go 0 -> 2 when it is disabled (its linked sound keeps 0). The two settings sit in
# different bits, so Flags is read as bits; any other bit (one real track holds 32) is listed with
# its value. No sample has a disabled sound clip or generator, so their Flags are only listed.
_TRACK_LOCKED, _TRACK_OFF, _CLIP_OFF = 0x1, 0x2, 0x2
# Drop-frame timecode: frames in one hour of 29.97 and 59.94 fps timecode, drop and non-drop.
_DROP_HOUR = {30000: 107892, 60000: 215784}
_NON_DROP_HOUR = {30000: 108000, 60000: 216000}
MAX_SUMMARY_FOLDERS = 4
_LOCKABLE_KINDS = {'Sm2SequenceLockableBlob': 'timeline', 'Sm2TiItemLockableBlob': 'item'}


# --------------------------------------------------------------------------- element helpers

def _text(el, tag):
    if el is None:
        return None
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    value = value.strip()
    return value or None


def _raw(el, tag):
    if el is None:
        return None
    child = el.find(tag)
    if child is None or child.text is None or (isinstance(child.text, str) and not child.text.strip()):
        return None
    return blobs.parse_hex(child.text)


def _filled(el, tag):
    """Whether a reference field holds something (a nested element in a .drp, an id in a library)."""
    child = el.find(tag)
    if child is None:
        return False
    if len(child):
        return True
    value = child.text
    if isinstance(value, bytes):
        return bool(value)
    return bool(value and value.strip() and value.strip() != '0')


def _walk(el, skip):
    stack = list(reversed(list(el)))
    while stack:
        node = stack.pop()
        yield node
        if node.tag not in skip:
            stack.extend(reversed(list(node)))


def _path(el, *tags):
    for tag in tags:
        if el is None:
            return None
        el = el.find(tag)
    return el


def _join(directory, filename):
    if not filename:
        return directory or None
    if not directory:
        return filename
    if directory.endswith(('\\', '/')):
        return directory + filename
    return directory + ('\\' if '\\' in directory and '/' not in directory else '/') + filename


def rational(fps):
    """An exact frame rate for a Resolve rate (stored as a double)."""
    for base in (24, 30, 48, 60, 96, 120, 240):
        if abs(fps - base * 1000 / 1001) < 5e-4:
            return base * 1000, 1001
    if abs(fps - round(fps)) < 1e-6:
        return int(round(fps)), 1
    value = Fraction(fps).limit_denominator(1001)
    return value.numerator, value.denominator


def _is_drop_rate(numerator, denominator):
    return denominator == 1001 and numerator in (30000, 60000)


def fps_label(numerator, denominator):
    return f'{numerator / denominator:.3f}'.rstrip('0').rstrip('.')


def timecode(frame, numerator, denominator):
    nominal = max(1, round(numerator / denominator))
    sign = '-' if frame < 0 else ''
    frame = abs(frame)
    seconds, frames = divmod(frame, nominal)
    return sign + '%02d:%02d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60, frames)


# --------------------------------------------------------------------------- the model

class Model:
    """What was read from a project: media, timelines and markers, plus problems found."""

    def __init__(self, source):
        self.source = source
        self.budget = blobs.Budget()
        self.problems = []
        self.media = {}
        self.bin_names = {}
        self.timelines = []
        self.markers = {}
        self._read()

    def note(self, message):
        if len(self.problems) < MAX_WARNINGS:
            self.problems.append(message)

    def message(self, data, what):
        try:
            return blobs.unwrap(data, self.budget)
        except BlobError as error:
            self.note(f'{what} could not be decoded ({error}).')
            return None

    # -- reading -----------------------------------------------------------------------
    def _read(self):
        project = self.source.project
        self.name = _text(project, 'ProjectName') or 'Resolve project'
        self.saved = blobs.parse_int(_text(project, 'LastModTimeInSecs'))
        handles = project.find('TimelineHandleVec')
        self.handle_order = [(_text(el, '.') or '') for el in (handles if handles is not None else [])]
        self._markers()
        parents = {}
        for folder in self.source.bins:
            fid = folder.get('DbId')
            self.bin_names[fid] = _text(folder, 'Name') or 'Bin'
            parents[fid] = _text(folder, 'MpFolder')
            vec = folder.find('MediaVec')
            for holder in vec if vec is not None else []:
                for el in holder:
                    self._media(el, fid)
        self.bin_paths = {}
        for fid in self.bin_names:
            names, seen, node = [], set(), fid
            while node in self.bin_names and node not in seen and len(seen) < 64:
                seen.add(node)
                names.append(self.bin_names[node])
                node = parents.get(node)
            # The root bin ("Master") is left out, as Splice's bins are relative to it.
            self.bin_paths[fid] = '/'.join(reversed(names[:-1])) if len(names) > 1 else ''
        self._timelines()

    def _markers(self):
        # Lockable blobs sit at different depths: LockableBlobMap/Element/<blob> in recent
        # projects and LockableBlobMap/Sm2LockableBlobMap/LocableBlobSet/Element/<blob> in older
        # ones, so every element named ...LockableBlob that has an owner is read.
        other = 0
        for el in self.source.project.iter():
            if not isinstance(el.tag, str) or not el.tag.endswith('LockableBlob') or el.find('BlobOwner') is None:
                continue
            kind = _LOCKABLE_KINDS.get(el.tag)
            owner = _text(el, 'BlobOwner')
            if kind is None:
                other += 1
                continue
            try:
                fields = blobs.keyed_dict(_raw(el, 'FieldsBlob') or b'')
            except BlobError:
                self.note('A marker list could not be decoded.')
                continue
            data = fields.get('BlobData')
            if not isinstance(data, bytes) or not owner:
                continue
            payload = self.message(data, 'A marker list')
            if payload is None:
                continue
            try:
                found = blobs.markers(payload, MAX_MARKERS)
            except BlobError as error:
                self.note(f'A marker list could not be decoded ({error}).')
                continue
            self.markers.setdefault(owner, []).extend(found)
        if other:
            self.note(f'{other} other Resolve annotation record(s) (media pool markers and clip metadata) '
                      'are not imported.')

    def _media(self, el, bin_id):
        mid = el.get('DbId')
        if not mid or mid in self.media or not el.tag.startswith('Sm2Mp'):
            return
        kind = _MEDIA_TYPES.get(el.tag, 'other')
        media = {'id': mid, 'kind': kind, 'name': _text(el, 'Name'), 'bin': bin_id, 'path': None,
                 'frames': None, 'rate': None, 'resolution': None, 'audio_seconds': None, 'tracks': 0,
                 'sequence': None, 'timeline': None}
        main = 'BtAudioInfo' if el.tag == 'Sm2MpAudioClip' else 'BtVideoInfo'
        for node in _walk(el, {'Sequence', 'TimelineSharedHandle'}):
            if node.tag not in ('BtVideoInfo', 'BtAudioInfo'):
                continue
            clip = _raw(node, 'Clip')
            if clip is not None and media['path'] is None and node.tag == main:
                payload = self.message(clip, f'The media pool entry {media["name"] or mid}')
                if payload is not None:
                    try:
                        fields = blobs.protobuf(payload)
                        media['path'] = _join(blobs.pb_text(blobs.pb_first(fields, 1, 2)),
                                              blobs.pb_text(blobs.pb_first(fields, 2, 2)))
                    except BlobError:
                        self.note(f'The file path of {media["name"] or mid} could not be decoded.')
            try:
                if node.tag == 'BtVideoInfo':
                    timing = _raw(node, 'Time')
                    if timing:
                        kd = blobs.keyed_dict(timing)
                        if isinstance(kd.get('NumFrames'), int):
                            media['frames'] = kd['NumFrames']
                        if isinstance(kd.get('FrameRate'), bytes):
                            media['rate'] = blobs.frame_rate(kd['FrameRate'])
                    geometry = _raw(node, 'Geometry')
                    if geometry:
                        kd = blobs.keyed_dict(geometry)
                        if isinstance(kd.get('Resolution'), bytes):
                            media['resolution'] = blobs.resolution(kd['Resolution'])
                else:
                    tracks = _raw(node, 'TracksBA')
                    for value in (blobs.keyed_dict(tracks).values() if tracks else ()):
                        if not isinstance(value, bytes):
                            continue
                        inner = blobs.keyed_dict(value)
                        if inner.get('DbType') != 'BtAudioTrack':
                            continue
                        media['tracks'] += 1
                        rate, samples = inner.get('SampleRate'), inner.get('Duration')
                        if isinstance(rate, int) and rate > 0 and isinstance(samples, int) and samples >= 0:
                            media['audio_seconds'] = max(media['audio_seconds'] or 0, samples / rate)
            except BlobError as error:
                self.note(f'Stream details of {media["name"] or mid} could not be decoded ({error}).')
        seq = _path(el, 'TimelineSharedHandle', 'Sm2Timeline', 'Sequence', 'Sm2Sequence')
        timeline = _path(el, 'TimelineSharedHandle', 'Sm2Timeline')
        if seq is None:
            seq, timeline = _path(el, 'Sequence', 'Sm2Sequence'), None
        if seq is not None and seq.get('DbId'):
            media['sequence'], media['timeline'] = seq, timeline
        self.media[mid] = media

    def _timelines(self):
        containers = {root.get('DbId'): root for root in self.source.containers if root.get('DbId')}
        container_for = {}
        for mid, media in self.media.items():
            seq = media['sequence']
            if seq is None:
                continue
            ref = None
            data = _raw(seq, 'FieldsBlob')
            if data and blobs.is_keyed_dict(data):
                try:
                    ref = blobs.keyed_dict(data).get('SeqRef')
                except BlobError:
                    ref = None
            if isinstance(ref, str) and ref in containers:
                container_for[mid] = ref
        claimed = set(container_for.values())
        by_sequence = {m['sequence'].get('DbId'): mid for mid, m in self.media.items() if m['sequence'] is not None}
        for cid, root in containers.items():
            if cid in claimed:
                continue
            refs = {_text(track, 'Sequence') for vec in root if vec.tag.endswith('TrackVec')
                    for holder in vec for track in holder} - {None}
            for ref in sorted(refs):
                if ref in by_sequence and by_sequence[ref] not in container_for:
                    container_for[by_sequence[ref]] = cid
                    claimed.add(cid)
                    break
        order = {tid: i for i, tid in enumerate(self.handle_order)}
        rank = {'timeline': 0, 'compound': 1, 'multicam': 2}
        found = []
        for mid, cid in container_for.items():
            media = self.media[mid]
            kind = media['kind'] if media['kind'] in ('compound', 'multicam') else 'timeline'
            timeline_el = media['timeline']
            name = _text(timeline_el, 'Name') or media['name'] or 'Timeline'
            tid = timeline_el.get('DbId') if timeline_el is not None else None
            found.append({'id': tid or cid, 'kind': kind, 'name': name, 'media': mid,
                          'sequence': media['sequence'], 'container': containers[cid]})
        for cid, root in containers.items():
            if cid not in claimed:
                found.append({'id': cid, 'kind': 'timeline', 'name': 'Unreferenced timeline',
                              'media': None, 'sequence': None, 'container': root})
                self.note('A timeline is not referenced by any media pool entry; it is listed as '
                          '“Unreferenced timeline”.')
        found.sort(key=lambda t: (rank.get(t['kind'], 3), order.get(t['id'], len(order)), t['name']))
        if len(found) > MAX_TIMELINES:
            raise ResolveError('The project has more timelines than Splice lists.')
        self.timelines = found
        # The media some timeline of the project uses: only these are looked for on this computer.
        self.used_media = {_text(item, 'MediaRef') for timeline in found for vec in timeline['container']
                           if vec.tag.endswith('TrackVec') for holder in vec for track in holder
                           for items in track.findall('Items') for item_holder in items for item in item_holder}
        self.used_media.discard(None)

    def labels(self):
        out = []
        for t in self.timelines:
            suffix = {'compound': ' · compound clip', 'multicam': ' · multicam clip'}.get(t['kind'], '')
            out.append(t['name'] + suffix)
        return out


def read(source):
    return Model(source)


# --------------------------------------------------------------------------- Splice project

class _Warnings:
    def __init__(self, limit=MAX_WARNINGS):
        self.lines = []
        self.limit = limit
        self.extra = 0

    def add(self, text):
        if text in self.lines:
            return
        if len(self.lines) < self.limit:
            self.lines.append(text)
        else:
            self.extra += 1

    def result(self):
        return self.lines + ([f'{self.extra} more items are not imported; they are kept in the Resolve project.']
                             if self.extra else [])


def volume_percent(db):
    """Splice's clip volume (linear gain in percent) for a Resolve clip volume in decibels, and
    whether it had to be clamped to the 0-200 % Splice plays."""
    if not math.isfinite(db):
        return (0.0, False) if db < 0 else (MAX_VOLUME, True)
    gain = 100 * 10 ** (min(db, 60.0) / 20)
    return (MAX_VOLUME, True) if gain > MAX_VOLUME + 1e-9 else (round(gain, 3), False)


def _effects(model, data, where, warn, clip, is_video):
    payload = model.message(data, where + ' effects')
    if payload is None:
        warn(where + ': its effects could not be decoded and are not imported.')
        return
    try:
        top = blobs.protobuf(payload)
        for value in blobs.pb_all(top, 1, 2):
            effect = blobs.protobuf(value)
            kind = blobs.pb_first(effect, 1, 0)
            params = {}
            plugin = None
            keyed = False
            for raw in blobs.pb_all(effect, 9, 2):
                param = blobs.protobuf(raw)
                pid = blobs.pb_first(param, 1, 0)
                number = None
                for holder in blobs.pb_all(param, 3, 2):
                    for inner in blobs.pb_all(blobs.protobuf(holder), 1, 2):
                        number = blobs.pb_double(blobs.pb_first(blobs.protobuf(inner), 2, 1)) if number is None else number
                    for inner in blobs.pb_all(blobs.protobuf(holder), 2, 2):
                        plugin = plugin or blobs.pb_text(blobs.pb_first(blobs.protobuf(inner), 2, 2))
                if blobs.pb_all(param, 4):
                    keyed = True
                if pid is not None:
                    params[pid] = number
            if not params and not plugin:
                continue  # an effect at its defaults
            if keyed:
                warn(where + ': keyframed effect parameters are not imported (the first value is used where known).')
            if is_video and kind == _TRANSFORM:
                zoom = [params.get(_ZOOM_X), params.get(_ZOOM_Y)]
                transform = {'x': 0, 'y': 0, 'scale': 100, 'rotation': 0}
                approximate = []
                if any(isinstance(z, float) for z in zoom):
                    zx, zy = (z if isinstance(z, float) else 1.0 for z in zoom)
                    transform['scale'] = round(100 * (zx + zy) / 2, 4)
                    approximate.append(f'zoom ({transform["scale"]:g} %)')
                    if abs(zx - zy) > 1e-6:
                        warn(where + ': uneven zoom is imported as an even zoom.')
                if isinstance(params.get(_POSITION_X), float):
                    # Measured: a fraction of the timeline width; Splice's x is a percentage of it.
                    transform['x'] = round(params[_POSITION_X] * 100, 6)
                if isinstance(params.get(_POSITION_Y), float):
                    transform['y'] = round(-params[_POSITION_Y] * 100, 6)
                    approximate.append(f'vertical position ({transform["y"]:g} %)')
                if not 10 <= transform['scale'] <= 400 or abs(transform['x']) > 100 or abs(transform['y']) > 100:
                    warn(where + ': its transform is outside the range Splice edits and is clamped.')
                    transform['scale'] = min(400, max(10, transform['scale']))
                    transform['x'] = min(100, max(-100, transform['x']))
                    transform['y'] = min(100, max(-100, transform['y']))
                clip['transform'] = transform
                if approximate:
                    one = len(approximate) == 1
                    warn(f'{where}: its {" and ".join(approximate)} {"is" if one else "are"} approximate: Resolve’s '
                         f'{"unit for it is" if one else "units for them are"} inferred, not measured. Check the picture.')
                if set(params) - {_POSITION_X, _POSITION_Y, _ZOOM_X, _ZOOM_Y}:
                    warn(where + ': rotation, anchor, pitch, yaw or flip settings are not imported.')
            elif is_video and kind == _COMPOSITE:
                if isinstance(params.get(_OPACITY), float) and 0 <= params[_OPACITY] <= 100:
                    clip['opacity'] = round(params[_OPACITY], 4)
                    warn(f'{where}: its opacity ({clip["opacity"]:g} %) is approximate: Resolve’s unit for it is '
                         'inferred, not measured.')
                if set(params) - {_OPACITY}:
                    warn(where + ': its composite mode is not imported.')
            elif plugin:
                warn(f'{where}: the effect {plugin} is not imported.')
            elif not is_video and kind == _AUDIO_VOLUME:
                db = params.get(_VOLUME_DB)
                if isinstance(db, float) and not math.isnan(db):
                    clip['volume'], clamped = volume_percent(db)
                    if clamped:
                        warn(f'{where}: its clip volume ({db:+.2f} dB) is above the +6.02 dB Splice plays and is '
                             'clamped to +6.02 dB.')
                if set(params) - {_VOLUME_DB}:
                    warn(where + ': clip audio settings other than volume (such as pan) are not imported.')
            elif not is_video:
                warn(f'{where}: clip audio adjustments other than volume (pan or Fairlight clip settings, Resolve '
                     f'effect type {kind}) are not imported.')
            elif kind == 6:
                warn(where + ': its cropping is not imported.')
            else:
                warn(f'{where}: an Inspector effect (type {kind}) is not imported.')
    except BlobError as error:
        warn(f'{where}: its effects could not be decoded ({error}) and are not imported.')


def _links(model, item):
    data = _raw(item, 'FieldsBlob')
    if not data:
        return ()
    payload = model.message(data, 'A clip link')
    if payload is None:
        return ()
    try:
        first = blobs.pb_first(blobs.protobuf(payload), 1, 2)
        if not first:
            return ()
        group = blobs.pb_first(blobs.protobuf(first), 1, 2)
        if not group:
            return ()
        listed = blobs.pb_first(blobs.protobuf(group), 1, 2)
        if not listed:
            return ()
        return tuple(v for v in blobs.keyed_dict(listed).values() if isinstance(v, str))
    except BlobError:
        return ()


def _clip_fx(model, item):
    """Whether an item's FieldsBlob holds Fairlight clip effects (FL::ClipFX)."""
    data = _raw(item, 'FieldsBlob')
    if not data:
        return False
    payload = model.message(data, 'A clip description')
    if payload is None:
        return False
    try:
        first = blobs.pb_first(blobs.protobuf(payload), 1, 2)
        group = blobs.pb_first(blobs.protobuf(first), 1, 2) if first else None
        return bool(group and blobs.pb_all(blobs.protobuf(group), 2, 2))
    except BlobError:
        return False


def _frames(item, tag, default=None):
    value = _text(item, tag)
    if value is None:
        return default, 0.0
    frames, fraction = blobs.parse_frames(value)
    return frames, fraction or 0.0


def timeline_index(model, timeline_id):
    """The index of the timeline whose Resolve id is ``timeline_id`` (raises if it is gone)."""
    for index, timeline in enumerate(model.timelines):
        if timeline['id'] == timeline_id:
            return index
    raise ResolveError('That timeline is no longer in this Resolve project; it may have been deleted '
                       'since the project was opened.')


def _drop_frame(tl_name, start_frame, num, den, warnings):
    """Whether a 29.97 or 59.94 fps timeline uses drop-frame timecode, from what can be defended.

    Resolve keeps the setting in the project's ``SetupBA`` (unnamed numbered fields; no sample has
    drop-frame switched on, so the field is not known). A start on a whole hour of drop-frame
    timecode that is not a whole hour of non-drop timecode (107892 frames is 01:00:00;00 drop-frame
    and 00:59:56:12 non-drop) is taken as drop-frame; anything else opens as non-drop, Resolve's
    default."""
    rate = fps_label(num, den)
    drop_hour, plain_hour = _DROP_HOUR[num], _NON_DROP_HOUR[num]
    if start_frame > 0 and start_frame % drop_hour == 0 and start_frame % plain_hour != 0:
        warnings.add(f'{tl_name}: Splice cannot read Resolve’s drop-frame setting. This {rate} fps timeline starts '
                     f'at {start_frame // drop_hour:02d}:00:00;00 in drop-frame timecode, a whole hour only in '
                     'drop-frame, so drop-frame is assumed. Change it in File → Sequence settings if that is wrong.')
        return True
    reason = (f' (it starts at {start_frame // plain_hour:02d}:00:00:00 in non-drop timecode)'
              if start_frame > 0 and start_frame % plain_hour == 0 else '')
    warnings.add(f'{tl_name}: Splice cannot read Resolve’s drop-frame setting; this {rate} fps timeline opens with '
                 f'non-drop timecode, Resolve’s default{reason}. Change it in File → Sequence settings if Resolve '
                 'shows drop-frame timecode (semicolons).')
    return False


def to_splice(model, index=0, link_media=None, source_ref=None):
    """Splice's project for timeline ``index`` of ``model``.

    ``link_media(paths)`` is called once with every media path the timeline names and returns one
    result per path: a dict with ``src`` (and ``fingerprint``) when the file was found here, else a
    falsy value; that media stays offline with its original path for relinking.
    """
    if not model.timelines:
        raise ResolveError('This Resolve project has no timelines.')
    if not isinstance(index, int) or not 0 <= index < len(model.timelines):
        raise ResolveError('That timeline does not exist in this project.')
    timeline = model.timelines[index]
    tl_name = timeline['name']
    warnings = _Warnings()
    for problem in model.problems:
        warnings.add(problem)
    seq = timeline['sequence']
    fps_value, size, start_seconds = None, None, None
    if seq is not None:
        for tag in ('FrameRate', 'Resolution', 'MediaExtents'):
            try:
                data = _raw(seq, tag)
                if not data:
                    continue
                if tag == 'FrameRate':
                    fps_value = blobs.frame_rate(data)
                elif tag == 'Resolution':
                    if data == bytes(16):
                        # Unset: the timeline follows the project, which Resolve's own .drp export
                        # writes as its default 1920 x 1080.
                        size = (1920, 1080)
                        warnings.add(f'{tl_name}: the timeline uses the project resolution, read as '
                                     'Resolve’s default 1920 × 1080.')
                    else:
                        size = blobs.resolution(data)
                else:
                    start_seconds = blobs.extents(data)[0]
            except BlobError as error:
                warnings.add(f'{tl_name}: a timeline setting could not be decoded ({error}).')
    if fps_value is None:
        fps_value = 24.0
        warnings.add(f'{tl_name}: the frame rate is unknown; 24 fps is assumed.')
    if size is None:
        size = (1920, 1080)
        warnings.add(f'{tl_name}: the resolution is unknown; 1920 × 1080 is assumed.')
    num, den = rational(fps_value)
    fps = num / den
    start_frame = round(start_seconds * num / den) if start_seconds is not None and math.isfinite(start_seconds) else None
    if start_frame is None or abs(start_frame) > 10 ** 9:
        start_frame = 0
        warnings.add(f'{tl_name}: the start timecode is unknown; 00:00:00:00 is assumed.')
    elif start_frame < 0:
        warnings.add(f'{tl_name}: the timeline starts before 00:00:00:00 (at {timecode(start_frame, num, den)}). '
                     'Splice’s timecode cannot go below zero, so the timeline opens at 00:00:00:00 and every '
                     f'timecode reads {timecode(-start_frame, num, den)} later than in Resolve.')
    if timeline['kind'] != 'timeline':
        warnings.add(f'{tl_name} is a Resolve {_NESTED[timeline["kind"]]}; its angles or layers are opened '
                     'as ordinary tracks.')
    if seq is not None:
        data = _raw(seq, 'FieldsBlob')
        try:
            fields = blobs.keyed_dict(data) if data and blobs.is_keyed_dict(data) else {}
        except BlobError:
            fields = {}
        if fields.get('FLStudioModelBA'):
            warnings.add(f'{tl_name}: Fairlight mixer settings, buses and track automation are not imported.')
    drop_frame = None
    if _is_drop_rate(num, den):
        drop_frame = _drop_frame(tl_name, start_frame, num, den, warnings)

    def seconds(frames, fraction=0.0):
        return (frames + fraction) * den / num

    media_out, media_ids = [], {}

    def media_for(mid, fallback_path, kind, name):
        key = mid or ('path:' + (fallback_path or name or ''))
        if key in media_ids:
            return media_ids[key]
        info = model.media.get(mid) if mid else None
        identity = f'm{len(media_out) + 1}'
        media_ids[key] = identity
        path = (info or {}).get('path') or fallback_path
        nested = (info or {}).get('kind') in _NESTED
        entry = {'id': identity, 'name': (PureWindowsPath(path).name if path else None) or (info or {}).get('name') or name or 'Media',
                 'src': '', 'duration': 0, 'thumb': '',
                 'type': 'audio' if (info or {}).get('kind') == 'audio' or (not info and kind == 'audio') else 'video'}
        if info:
            entry['resolveMediaId'] = info['id']
            entry['bin'] = model.bin_paths.get(info['bin'], '')
            if info['frames'] is not None and info['rate']:
                entry['duration'] = info['frames'] / info['rate']
            elif info['audio_seconds']:
                entry['duration'] = info['audio_seconds']
            if info['tracks']:
                # Resolve's count of audio tracks. audioStreams (an integer count, with the list in
                # audioStreamDetails) is left to Splice's media probe, which only runs while it is unset.
                entry['resolveAudioTracks'] = info['tracks']
        if nested:
            entry['nativeUnsupported'] = 'Resolve ' + _NESTED[info['kind']]
            entry['name'] = info.get('name') or entry['name']
        elif path:
            entry['path'] = path
            entry['offline'] = True  # until the batch below finds it on this computer
        else:
            entry['nativeUnsupported'] = 'Resolve media without a file path'
        media_out.append(entry)
        return identity

    clips, by_item, links, track_names, track_flags = [], {}, {}, {}, {}
    track_state, track_mix = {}, {}
    states = {'disabled clips': 0, 'hidden tracks': 0, 'muted tracks': 0, 'locked tracks': 0}
    counts = {'video': 0, 'audio': 0}
    transitions = []
    for vec in timeline['container']:
        if not vec.tag.endswith('TrackVec'):
            continue
        kind = vec.tag[:-len('TrackVec')].lower()
        for holder in vec:
            for track in holder:
                if track.tag != 'Sm2TiTrack':
                    continue
                if kind not in counts:
                    items = track.find('Items')
                    if items is not None and len(items):
                        warnings.add(f'{tl_name}: a {kind} track with {len(items)} items is not imported.')
                    continue
                counts[kind] += 1
                number = counts[kind]
                label = _text(track, 'UserDefinedName') or ('Video ' if kind == 'video' else 'Audio ') + str(number)
                if number > MAX_TRACKS:
                    items = track.find('Items')
                    warnings.add(f'{tl_name} · {label}: Splice edits up to {MAX_TRACKS} tracks of each kind; '
                                 f'this track ({len(items) if items is not None else 0} items) is not imported.')
                    continue
                splice_track = number if kind == 'video' else -number
                track_names[f'{kind}:{splice_track}'] = label
                key = f'{kind}:{splice_track}'
                flags = blobs.parse_int(_text(track, 'Flags'))
                if flags is not None:
                    track_flags[key] = flags
                if flags and flags > 0:
                    if flags & _TRACK_LOCKED:
                        track_state.setdefault(key, {})['locked'] = True
                        states['locked tracks'] += 1
                    if flags & _TRACK_OFF and kind == 'video':
                        track_state.setdefault(key, {})['hidden'] = True
                        states['hidden tracks'] += 1
                    elif flags & _TRACK_OFF:
                        track_mix[str(splice_track)] = {'mute': True}
                        states['muted tracks'] += 1
                    flags &= ~(_TRACK_LOCKED | _TRACK_OFF)
                if flags:
                    warnings.add(f'{tl_name} · {label}: the track carries other Resolve track flags ({flags}) that '
                                 'Splice does not interpret.')
                items = track.find('Items')
                for item_holder in items if items is not None else []:
                    for item in item_holder:
                        _item(model, item, kind, splice_track, label, tl_name, start_frame, num, den, seconds,
                              media_for, clips, by_item, links, transitions, warnings, states)

    # Linked picture/sound partners become Splice link groups.
    parent = {}

    def root(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    for item_id, partners in links.items():
        for partner in partners:
            if partner in by_item and item_id in by_item:
                parent[root(item_id)] = root(partner)
    groups = {}
    for item_id, clip in by_item.items():
        groups.setdefault(root(item_id), []).append(clip)
    for n, members in enumerate(sorted(groups.values(), key=lambda m: m[0]['id']), 1):
        if len(members) > 1:
            for clip in members:
                clip['linkGroup'] = f'resolve-link-{n}'

    for transition in transitions:
        _transition(transition, clips, seconds, warnings)

    markers = []
    for marker in model.markers.get(seq.get('DbId') if seq is not None else '', []):
        time = seconds(marker['frame'])
        if time < 0:
            warnings.add(f'{tl_name}: the marker “{marker["name"]}” lies before the timeline start and is not imported.')
            continue
        markers.append({'id': f'marker-{len(markers) + 1}', 'name': marker['name'] or marker['note'] or 'Marker',
                        'time': time, 'color': marker['color'], 'note': marker['note'],
                        'duration': seconds(marker['duration']) if marker['duration'] else 0})
    if markers:
        warnings.add(f'{tl_name}: timeline markers are placed as frames from the timeline start, as Resolve 19 '
                     'stores them; this is not yet confirmed for projects saved by later versions of Resolve.')
    for clip in clips:
        found = model.markers.get(clip.get('resolveItemId'), [])
        if found:
            # Resolve 19 projects store a clip marker as a frame of the source media (markers sit on
            # source in-points, and every clip cut from one file carries the same ones); a Resolve
            # 21.1 project stores a marker the scripting API put at clip frame 10 as 20.
            warnings.add(f'{clip["_where"]}: {len(found)} clip marker(s) are not imported. Where Resolve places a '
                         'clip marker is not settled: Resolve 19 stores it as a frame of the source media, and a '
                         'Resolve 21 project stores twice the frame number the scripting API reports.')
    for clip in clips:
        clip.pop('_where', None)

    # The media pool: what some timeline of the project uses. Media no timeline uses is left out.
    unused = 0
    for entry in model.media.values():
        if entry['kind'] in ('video', 'audio', 'still') and entry['id'] not in media_ids and entry.get('path'):
            if entry['id'] in model.used_media:
                media_for(entry['id'], None, entry['kind'], entry['name'])
            else:
                unused += 1
    if unused:
        warnings.add(f'{unused} media pool file(s) that no timeline of this project uses are not imported.')

    # One call for the whole project, so the link registry is written once.
    pending = [m for m in media_out if m.get('path')]
    found = list(link_media([m['path'] for m in pending])) if link_media and pending else []
    if len(found) != (len(pending) if link_media else 0):
        raise ResolveError('Linking the project’s media gave an unexpected result.')
    offline = {'missing': 0, 'not-media': 0, 'unchecked': 0, 'refused': 0}
    reasons = {}
    for entry, result in zip(pending, found):
        if isinstance(result, dict) and result.get('src'):
            entry.update(src=result['src'], offline=False, linked=True, sourcePath=result.get('sourcePath'),
                         sourceFingerprint=result.get('fingerprint'))
            continue
        reason = result.get('offline') if isinstance(result, dict) else None
        reason = reason if reason in offline else 'missing'
        offline[reason] += 1
        if isinstance(result, dict) and isinstance(result.get('message'), str):
            reasons.setdefault(reason, result['message'][:300])
    relink = ('Relink them without copying with File → Link media in place → Relink offline from folder; '
              'Windows and Mac paths are matched by folder and file name.')
    if pending and not link_media:
        warnings.add(f'{len(pending)} media file(s) are offline: Splice did not look for them on this computer. '
                     + relink)
    if offline['missing']:
        warnings.add(f'{offline["missing"]} media file(s) are offline on this computer: nothing is at the path the '
                     'project names (it may be on another computer or have moved). ' + relink)
    if offline['not-media']:
        warnings.add(f'{offline["not-media"]} file(s) the project names were found on this computer but are not '
                     'media Splice can play (not a media file, or FFmpeg cannot decode it); they stay offline.')
    if offline['unchecked']:
        warnings.add(f'{offline["unchecked"]} media file(s) were not looked for ({reasons.get("unchecked", "time ran out")}); '
                     'they stay offline. ' + relink)
    if offline['refused']:
        warnings.add(f'{offline["refused"]} media file(s) could not be linked ({reasons.get("refused", "refused")}); '
                     'they stay offline.')
    linked = [m for m in media_out if m.get('linked')]
    summary = []
    if any(states.values()):
        summary.append('From Resolve: ' + ', '.join(f'{n} {what}' if n != 1 else f'1 {what[:-1]}'
                                                    for what, n in states.items() if n) + '.')
    if linked:
        folders = []
        for m in linked:
            parent = PurePath(m.get('sourcePath') or m['path']).parent
            top = str(PurePath(*parent.parts[:3])) if parent.parts else str(parent)
            if top not in folders:
                folders.append(top)
        shown = folders[:MAX_SUMMARY_FOLDERS]
        more = f' and {len(folders) - len(shown)} more folder(s)' if len(folders) > len(shown) else ''
        summary.append(f'{len(linked)} local file(s) linked in place from {", ".join(shown)}{more}.')
    project = {
        'name': tl_name, 'width': size[0], 'height': size[1], 'fps': fps,
        'frameRate': f'{num}/{den}', 'startFrame': start_frame,
        # Splice's project format: the start timecode is a whole frame count at the timeline rate
        # (engine.js), with ``dropFrame`` only for the NTSC rates. Before the timeline starts at
        # frame 0 (negative Resolve starts) there is no timecode, so 0 is used.
        'startTimecode': max(0, start_frame) % (24 * 3600 * max(1, round(num / den))),
        'media': media_out, 'clips': clips, 'captions': [], 'markers': markers,
        'trackState': track_state, 'trackCounts': {'video': max(1, min(MAX_TRACKS, counts['video'])),
                                                   'audio': max(1, min(MAX_TRACKS, counts['audio']))},
        'trackNames': track_names,
        'compatibilityWarnings': warnings.result(),
        'duration': max((c['start'] + c['duration'] for c in clips), default=0),
        'resolveTimelines': model.labels(), 'resolveTimelineIds': [t['id'] for t in model.timelines],
        'resolveTimeline': index, 'resolveTrackFlags': track_flags,
        'resolveSource': dict(source_ref or {}, kind=model.source.kind, sha256=model.source.sha256,
                              project=model.name, timelineId=timeline['id']),
    }
    if track_mix:
        project['trackMix'] = track_mix
    if drop_frame is not None:
        project['dropFrame'] = drop_frame
    project['resolveSource']['linkedMedia'] = len(linked)
    project['resolveSource']['summary'] = summary
    return project


def _item(model, item, kind, track, label, tl_name, start_frame, num, den, seconds, media_for,
          clips, by_item, links, transitions, warnings, states):
    tag = item.tag
    name = _text(item, 'Name') or ''
    try:
        start, start_fraction = _frames(item, 'Start')
        duration, duration_fraction = _frames(item, 'Duration')
    except BlobError as error:
        warnings.add(f'{tl_name} · {label}: an item with an unreadable position is not imported ({error}).')
        return
    if start is None or duration is None:
        warnings.add(f'{tl_name} · {label}: an item without a position is not imported.')
        return
    where = f'{tl_name} · {label} · {name or "Clip"} at {timecode(start, num, den)}'
    if tag == 'Sm2TiTransition':
        transitions.append({'where': where, 'kind': kind, 'track': track, 'start': start - start_frame,
                            'duration': duration + duration_fraction, 'type': _text(item, 'PrettyType') or name,
                            'id': item.get('DbId')})
        return
    relative = start - start_frame
    if relative < 0:
        warnings.add(f'{where}: the clip starts before the timeline start and is not imported.')
        return
    if duration <= 0 and duration_fraction <= 0:
        warnings.add(f'{where}: a clip without duration is not imported.')
        return
    try:
        source_in, in_fraction = _frames(item, 'In', 0)
    except BlobError:
        source_in, in_fraction = 0, 0.0
        warnings.add(f'{where}: the source in point could not be read; the clip starts at its first frame.')
    media_ref = _text(item, 'MediaRef')
    info = model.media.get(media_ref) if media_ref else None
    clip = {'id': f'c{len(clips) + 1}', 'type': kind, 'track': track,
            'start': seconds(relative, start_fraction), 'duration': seconds(duration, duration_fraction),
            'in': max(0.0, seconds(source_in, in_fraction)), 'speed': 1, 'reverse': False,
            'title': name, 'color': {}, 'volume': 100, 'transition': 'none',
            'resolveItemId': item.get('DbId'), 'resolveFrames': [relative, duration, source_in],
            '_where': where}
    if kind == 'video':
        # A Resolve video item never plays sound: the camera sound is its own audio item (linked
        # to the picture when Resolve links them), so the picture carries no embedded sound.
        clip['embeddedAudio'] = False
    flags = blobs.parse_int(_text(item, 'Flags'))
    if flags is not None:
        clip['resolveFlags'] = flags
    disabled = False
    if flags and tag == 'Sm2TiVideoClip' and flags > 0:
        disabled = bool(flags & _CLIP_OFF)
        flags &= ~_CLIP_OFF
    if flags:
        what = 'other Resolve clip flags' if tag == 'Sm2TiVideoClip' else 'Resolve clip flags'
        warnings.add(f'{where}: the clip carries {what} ({flags}) that Splice does not interpret'
                     + ('' if tag == 'Sm2TiVideoClip' else ' (it may be disabled in Resolve; it opens enabled)') + '.')
    pretty = _text(item, 'PrettyType')
    if tag == 'Sm2TiGenerator' or (tag == 'Sm2TiVideoClip' and pretty == 'Fusion Title'):
        what = 'Fusion title' if pretty == 'Fusion Title' else f'generator ({pretty or "unknown"})'
        identity = media_for(None, None, kind, f'Resolve {what}')
        clip['mediaId'] = identity
        clip['nativeUnsupported'] = f'Resolve {what}'
        warnings.add(f'{where}: the Resolve {what} is not imported (a placeholder keeps its place).')
    elif tag in ('Sm2TiVideoClip', 'Sm2TiAudioClip'):
        clip['mediaId'] = media_for(media_ref if info else None, _text(item, 'MediaFilePath'), kind, name)
        if info and info['kind'] in _NESTED:
            clip['nativeUnsupported'] = 'Resolve ' + _NESTED[info['kind']]
            warnings.add(f'{where}: the {_NESTED[info["kind"]]} is not imported (a placeholder keeps its place).')
        elif media_ref and not info and not _text(item, 'MediaFilePath'):
            clip['nativeUnsupported'] = 'Resolve media not in this project'
            warnings.add(f'{where}: its media is not in this project.')
        track_index = blobs.parse_int(_text(item, 'MediaTrackIdx'))
        if kind == 'audio' and track_index and track_index > 0:
            clip['audioStream'] = track_index
    else:
        warnings.add(f'{where}: a Resolve {tag} item is not imported.')
        return
    # Speed and reverse.
    timemap = _raw(item, 'MediaTimemapBA')
    if timemap:
        try:
            form, speed, kd = blobs.timemap(timemap)
            if form == 'retimed':
                points = None
                keys = kd.get('KeyframesBA') if kd else None
                if isinstance(keys, bytes):
                    try:
                        points = [p for p in blobs.keyed_dict(keys).values() if isinstance(p, bytes)]
                        points = [blobs.keyed_dict(p) for p in points]
                    except BlobError:
                        points = None
                ratio = None
                if points and len(points) == 2:
                    ordered = sorted(points, key=lambda p: p.get('X', 0))
                    dx = ordered[1].get('X', 0) - ordered[0].get('X', 0)
                    dy = ordered[1].get('Y', 0) - ordered[0].get('Y', 0)
                    if isinstance(dx, float) and isinstance(dy, float) and dx > 0 and math.isfinite(dy / dx):
                        ratio = dy / dx
                if ratio is None:
                    ratio = speed
                    warnings.add(f'{where}: its speed curve is imported as a constant average speed.')
                if ratio is None or abs(ratio) < 1e-6:
                    warnings.add(f'{where}: its freeze frame is not imported.')
                else:
                    clip['speed'] = round(min(16.0, max(0.1, abs(ratio))), 6)
                    clip['reverse'] = ratio < 0
                    if not 0.1 <= abs(ratio) <= 16:
                        warnings.add(f'{where}: its speed is outside the 10–1600% Splice plays and is clamped.')
                    warnings.add(f'{where}: its speed ({clip["speed"] * 100:g} %{", reversed" if clip["reverse"] else ""}) '
                                 'is approximate: it is read from Resolve’s retime curve, which has not been measured '
                                 'against Resolve’s own speed setting.')
        except BlobError as error:
            warnings.add(f'{where}: its speed could not be decoded ({error}); it plays at 100%.')
    effects = _raw(item, 'EffectFiltersBA')
    if effects and not clip.get('nativeUnsupported'):
        _effects(model, effects, where, warnings.add, clip, kind == 'video')
    if _filled(item, 'pLmVerTable'):
        warnings.add(f'{where}: its Color page grade (nodes and versions) is not imported.')
    if _filled(item, 'CompositionTable'):
        warnings.add(f'{where}: its Fusion composition is not imported.')
    if _clip_fx(model, item):
        warnings.add(f'{where}: its Fairlight clip effects are not imported.')
    if disabled:
        # Splice's own representation of a disabled clip (as from FCPXML, Final Cut 7 XML and OTIO):
        # ``enabled: false``, which the interchange exports keep. Splice's player and renderer have
        # no disabled switch yet, so the picture is also made transparent: a Resolve picture clip
        # has no sound of its own, so it then plays as nothing, as in Resolve.
        clip['enabled'] = False
        before = clip.get('opacity', 100)
        clip['opacity'] = 0
        states['disabled clips'] += 1
        warnings.add(f'{where}: the clip is disabled in Resolve. It opens marked disabled (exports leave it out or '
                     f'mark it off) and transparent, as Splice has no disabled switch yet; set its opacity back to '
                     f'{before:g} % to show it.')
    partners = _links(model, item)
    if partners and clip['resolveItemId']:
        links[clip['resolveItemId']] = partners
    clips.append(clip)
    if clip['resolveItemId']:
        by_item[clip['resolveItemId']] = clip


def _transition(transition, clips, seconds, warnings):
    where = transition['where']
    style = transition['type'] or 'transition'
    same = [c for c in clips if c['track'] == transition['track'] and c['type'] == transition['kind']]
    start, length = transition['start'], transition['duration']
    incoming = next((c for c in sorted(same, key=lambda c: c['resolveFrames'][0])
                     if start <= c['resolveFrames'][0] <= start + length), None)
    if transition['kind'] == 'video' and style == 'Cross Dissolve':
        mapped, match = 'dissolve', None
    elif transition['kind'] == 'audio' and style in _AUDIO_FADES:
        mapped, match = 'dissolve', _AUDIO_FADES[style]
    else:
        warnings.add(f'{where}: the {style} transition is not imported; the edit is a cut.')
        return
    if incoming is None:
        warnings.add(f'{where}: a {style} at the end of a clip (fade out) is not imported.')
        return
    cut = incoming['resolveFrames'][0]
    outgoing = any(c['resolveFrames'][0] + c['resolveFrames'][1] == cut for c in same if c is not incoming)
    incoming['transitionDuration'] = seconds(length)
    incoming['transitionBefore'] = seconds(cut - start)
    if not outgoing:
        if transition['kind'] == 'video' and cut == start:
            incoming['transition'] = 'fade'
            incoming['transitionBefore'] = 0
            incoming['resolveTransitionId'] = transition['id']
            return
        warnings.add(f'{where}: a {style} without a clip on one side is not imported.')
        incoming.pop('transitionDuration')
        incoming.pop('transitionBefore')
        return
    incoming['transition'] = mapped
    if match:
        incoming['transitionMatchName'] = match
    incoming['resolveTransitionId'] = transition['id']
