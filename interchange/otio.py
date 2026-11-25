"""OpenTimelineIO JSON (``.otio``) written and read without the ``opentimelineio`` package.

Schemas: ``Timeline.1`` > ``Stack.1`` > ``Track.1`` (``kind`` ``Video`` or ``Audio``) holding
``Clip.1``, ``Gap.1`` and ``Transition.1``. Times are ``RationalTime.1`` at exact NTSC rates
(``24000/1001`` as the double 23.976023976023978, which is how OTIO stores rates); clip source
ranges count from the media's start timecode, the same origin as each ``ExternalReference.1``'s
``available_range``. A transition sits between two items with ``in_offset`` (frames before the
cut) and ``out_offset`` (after it). Speed is a ``LinearTimeWarp.1`` effect (negative
``time_scalar`` for reverse) and the clip's ``source_range`` duration is its record duration, the
convention of OTIO's Premiere and Resolve adapters. Sound that plays from a picture clip is
written again on an ``Audio`` track marked ``metadata.splice.embeddedFrom`` so audio-only readers
hear it. Everything else Splice knows (volume, fades, transforms, keyframes, links, titles) is kept
under ``metadata.splice``.
"""
from __future__ import annotations

import json
import math
import re
from fractions import Fraction

from .cut import Cut, Event
from .media import base_name, file_url, path_from_url
from .timecode import frames_timecode, rate_text, snap_rate

FORMAT = 'OTIO'
MAX_OTIO_BYTES = 30 * 1024 * 1024


def rational(value: Fraction | int, rate: Fraction) -> dict:
    return {'OTIO_SCHEMA': 'RationalTime.1', 'rate': float(rate), 'value': float(value)}


def time_range(start, duration, rate) -> dict:
    return {'OTIO_SCHEMA': 'TimeRange.1', 'duration': rational(duration, rate), 'start_time': rational(start, rate)}


def _gap(frames, rate):
    return {'OTIO_SCHEMA': 'Gap.1', 'name': '', 'source_range': time_range(0, frames, rate),
            'effects': [], 'markers': [], 'enabled': True, 'metadata': {}}


class _Writer:
    def __init__(self, cut: Cut):
        self.cut = cut
        self.rate = cut.rate

    def note(self, event, message):
        self.cut.report.add(FORMAT, f'{self.cut.label(event)}: {message}')

    def reference(self, source):
        rate = source.rate or self.rate
        if source.path:
            url = file_url(source.path)
        else:
            url = base_name(source.name)
            self.cut.report.add(FORMAT, f'Source {source.name}: its original path is unknown; target_url is the bare file name.')
        metadata = {'splice': {'mediaId': source.id}}
        if source.timecode:
            metadata['splice']['timecode'] = source.timecode
        if source.reel:
            metadata['splice']['reel'] = source.reel
        start = source.start * rate
        return {'OTIO_SCHEMA': 'ExternalReference.1', 'name': source.name, 'target_url': url,
                'available_range': time_range(start, (source.duration * rate) if not source.still else 0, rate),
                'available_image_bounds': None, 'metadata': metadata}

    def clip(self, event: Event, sound_copy=False):
        metadata = {'trackNumber': event.track, 'clipId': event.id}
        if event.kind == 'title':
            metadata.update(title=event.text, titleStyle=event.title_style)
            self.note(event, 'titles are written as a GeneratorReference (generator_kind "SpliceTitle"); most readers show a placeholder.')
            reference = {'OTIO_SCHEMA': 'GeneratorReference.1', 'name': 'Title', 'generator_kind': 'SpliceTitle',
                         'parameters': {'text': event.text}, 'available_range': None, 'available_image_bounds': None,
                         'metadata': {}}
            source_range = time_range(0, event.frames, self.rate)
        else:
            source = event.source
            rate = source.rate or self.rate
            reference = self.reference(source)
            start = (source.start + event.source_in) * rate
            source_range = time_range(start, Fraction(event.frames) / self.rate * rate, rate)
        for key, value, default in (('volume', event.volume, 100), ('opacity', event.opacity, 100),
                                    ('fadeIn', event.fade_in, 0), ('fadeOut', event.fade_out, 0)):
            if value != default:
                metadata[key] = value if key in ('volume', 'opacity') else float(Fraction(value) / self.rate)
        if any(abs(event.transform.get(k, d) - d) > 1e-9 for k, d in (('x', 0), ('y', 0), ('scale', 100), ('rotation', 0))):
            metadata['transform'] = event.transform
        if event.keyframes:
            metadata['keyframes'] = {k: [{'time': float(Fraction(f) / self.rate), 'value': v} for f, v in points]
                                     for k, points in event.keyframes.items()}
        if event.link_group:
            metadata['linkGroup'] = event.link_group
        if event.kind == 'video':
            metadata['embeddedAudio'] = event.embedded_audio
        if sound_copy:
            metadata['embeddedFrom'] = event.id
        effects = []
        if event.speed != 1 or event.reverse:
            scalar = float(event.speed) * (-1 if event.reverse else 1)
            effects.append({'OTIO_SCHEMA': 'LinearTimeWarp.1', 'name': 'Speed', 'effect_name': 'LinearTimeWarp',
                            'time_scalar': scalar, 'metadata': {}})
        if metadata.get('transform') or metadata.get('keyframes') or event.volume != 100 or event.fade_in or event.fade_out:
            self.note(event, 'volume, fades, transform and keyframes are kept under metadata.splice; other apps ignore them.')
        return {'OTIO_SCHEMA': 'Clip.1', 'name': event.name, 'source_range': source_range, 'effects': effects,
                'markers': [], 'enabled': event.enabled, 'media_reference': reference, 'metadata': {'splice': metadata}}

    def transition(self, event):
        tr = event.transition
        if tr.kind != 'dissolve':
            self.note(event, f'{"dip to black" if tr.kind == "fade" else "wipe"} written as an SMPTE dissolve.')
        return {'OTIO_SCHEMA': 'Transition.1', 'name': 'Cross Dissolve', 'transition_type': 'SMPTE_Dissolve',
                'in_offset': rational(tr.before, self.rate), 'out_offset': rational(tr.frames - tr.before, self.rate),
                'metadata': {'splice': {'kind': tr.kind, 'constantPower': tr.power}}}

    def track(self, name, kind, events, sound_copy=False):
        children, cursor = [], 0
        for event in sorted(events, key=lambda e: e.record_in):
            if event.record_in > cursor:
                children.append(_gap(event.record_in - cursor, self.rate))
            if event.transition is not None:
                children.append(self.transition(event))
            children.append(self.clip(event, sound_copy))
            cursor = event.record_out
        return {'OTIO_SCHEMA': 'Track.1', 'name': name, 'kind': kind, 'source_range': None, 'effects': [],
                'markers': [], 'enabled': True, 'children': children, 'metadata': {}}

    def document(self) -> dict:
        cut, tracks = self.cut, []
        pictures = sorted({e.track for e in cut.events if e.kind != 'audio'})
        for track in pictures:
            tracks.append(self.track(f'V{track}', 'Video', [e for e in cut.events if e.kind != 'audio' and e.track == track]))
        for track in sorted({-e.track for e in cut.events if e.kind == 'audio'}):
            tracks.append(self.track(f'A{track}', 'Audio', [e for e in cut.events if e.kind == 'audio' and e.track == -track]))
        for track in pictures:
            embedded = [e for e in cut.events if e.kind == 'video' and e.track == track and e.embedded_audio]
            if embedded:
                tracks.append(self.track(f'V{track} sound', 'Audio', embedded, sound_copy=True))
        markers = [{'OTIO_SCHEMA': 'Marker.2', 'name': m.name, 'color': (m.color or 'RED').upper(), 'comment': '',
                    'marked_range': time_range(m.frame, m.duration, self.rate), 'metadata': {}} for m in cut.markers]
        return {'OTIO_SCHEMA': 'Timeline.1', 'name': cut.name,
                'global_start_time': rational(cut.tc_start, self.rate),
                'metadata': {'splice': {'width': cut.width, 'height': cut.height, 'rate': rate_text(self.rate),
                                        'dropFrame': cut.drop, 'startTimecode': frames_timecode(cut.tc_start, self.rate, cut.drop)}},
                'tracks': {'OTIO_SCHEMA': 'Stack.1', 'name': 'tracks', 'source_range': None, 'effects': [],
                           'markers': markers, 'enabled': True, 'children': tracks, 'metadata': {}}}


def write(cut: Cut) -> str:
    if cut.frames <= 0:
        raise ValueError('The timeline is empty')
    return json.dumps(_Writer(cut).document(), indent=4, ensure_ascii=False) + '\n'


# -- import -------------------------------------------------------------------------------------

def _rt(value, what) -> tuple:
    if not isinstance(value, dict) or not str(value.get('OTIO_SCHEMA', '')).startswith('RationalTime'):
        raise ValueError(f'{what} is not a RationalTime')
    rate, amount = float(value.get('rate', 0)), float(value.get('value', 0))
    if not (math.isfinite(rate) and math.isfinite(amount)) or rate <= 0 or abs(amount) > 1e12:
        raise ValueError(f'{what} is out of range')
    exact = snap_rate(Fraction(repr(rate))) or Fraction(repr(rate)).limit_denominator(1001)
    return Fraction(repr(amount)), exact


def _seconds(value, what) -> Fraction:
    amount, rate = _rt(value, what)
    return amount / rate


def _range(value, what):
    if not isinstance(value, dict):
        raise ValueError(f'{what} is missing')
    return _seconds(value.get('start_time'), what), _seconds(value.get('duration'), what)


def read(data: bytes) -> dict:
    from .project import build_project
    if len(data) > MAX_OTIO_BYTES:
        raise ValueError('The OTIO file is larger than 30 MB')
    try:
        document = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
        raise ValueError(f'The OTIO file is not valid JSON: {error}') from None
    if not isinstance(document, dict) or not str(document.get('OTIO_SCHEMA', '')).startswith('Timeline.'):
        raise ValueError('The OTIO file does not hold a Timeline')
    stack = document.get('tracks')
    if not isinstance(stack, dict) or not isinstance(stack.get('children'), list):
        raise ValueError('The OTIO timeline has no tracks')
    splice = (document.get('metadata') or {}).get('splice') or {}
    _start, rate = _rt(document.get('global_start_time') or {'OTIO_SCHEMA': 'RationalTime.1', 'rate': 25, 'value': 0}, 'global start')
    rate = snap_rate(rate) or rate
    warnings, clips, media, markers = [], [], {}, []
    counts = {'Video': 0, 'Audio': 0}
    for track in stack['children'][:128]:
        if not isinstance(track, dict) or not str(track.get('OTIO_SCHEMA', '')).startswith('Track.'):
            warnings.append('A nested stack or unknown track type was not imported.')
            continue
        kind = 'audio' if track.get('kind') == 'Audio' else 'video'
        counts[track.get('kind', 'Video')] = counts.get(track.get('kind', 'Video'), 0) + 1
        named = re.fullmatch(r'[VA](\d{1,2})', str(track.get('name') or ''))
        number = int(named.group(1)) if named else counts.get(track.get('kind', 'Video'), 1)
        cursor, pending = Fraction(0), None
        for item in (track.get('children') or [])[:20000]:
            schema = str(item.get('OTIO_SCHEMA', '')) if isinstance(item, dict) else ''
            if schema.startswith('Transition.'):
                pending = item
                continue
            try:
                source_start, duration = _range(item.get('source_range'), f'{item.get("name")} source range')
            except ValueError as error:
                warnings.append(f'{item.get("name") or "An item"}: {error}; skipped.')
                pending = None
                continue
            if schema.startswith('Gap.'):
                cursor += duration
                pending = None
                continue
            if not schema.startswith('Clip.'):
                warnings.append(f'{item.get("name") or schema}: {schema} items are not supported; left as a gap.')
                cursor += duration
                pending = None
                continue
            meta = ((item.get('metadata') or {}).get('splice') or {}) if isinstance(item.get('metadata'), dict) else {}
            reference = item.get('media_reference') or (item.get('media_references') or {}).get(item.get('active_media_reference_key', 'DEFAULT_MEDIA')) or {}
            clip = {'name': str(item.get('name') or 'Clip')[:240], 'start': cursor, 'duration': duration,
                    'enabled': item.get('enabled', True) is not False}
            if meta.get('embeddedFrom'):  # a copy of picture-clip sound; the picture clip carries it
                cursor += duration
                pending = None
                continue
            if str(reference.get('OTIO_SCHEMA', '')).startswith('GeneratorReference') and reference.get('generator_kind') == 'SpliceTitle':
                clip.update(kind='title', text=str(meta.get('title') or (reference.get('parameters') or {}).get('text') or ''),
                            track=int(meta.get('trackNumber') or number))
                if isinstance(meta.get('titleStyle'), dict):
                    clip['titleStyle'] = meta['titleStyle']
            elif str(reference.get('OTIO_SCHEMA', '')).startswith('ExternalReference'):
                try:
                    path = path_from_url(reference.get('target_url', ''))
                except ValueError as error:
                    warnings.append(f'{clip["name"]}: {error}')
                    path = ''
                key = path or reference.get('name') or clip['name']
                available = reference.get('available_range')
                origin = Fraction(0)
                if isinstance(available, dict):
                    try:
                        origin, length = _range(available, 'available range')
                    except ValueError:
                        length = None
                else:
                    length = None
                if key not in media:
                    media_rate = _rt((available or {}).get('start_time'), 'rate')[1] if isinstance(available, dict) else rate
                    record = {'key': key, 'name': reference.get('name') or base_name(path) or clip['name'], 'path': path,
                              'kind': kind, 'duration': float(length) if length else None, 'rate': rate_text(media_rate)}
                    if isinstance(available, dict) and kind == 'video':
                        try:
                            record['timecode'] = frames_timecode(round(origin * media_rate), media_rate)
                        except ValueError:
                            pass
                    media[key] = record
                elif kind == 'video':
                    media[key]['kind'] = 'video'
                speed, reverse = 1.0, False
                for effect in item.get('effects') or []:
                    if isinstance(effect, dict) and str(effect.get('OTIO_SCHEMA', '')).startswith('LinearTimeWarp'):
                        scalar = float(effect.get('time_scalar', 1))
                        speed, reverse = abs(scalar) or 1.0, scalar < 0
                    elif isinstance(effect, dict):
                        warnings.append(f'{clip["name"]}: effect {effect.get("effect_name") or effect.get("OTIO_SCHEMA")} was not imported.')
                placed = int(meta['trackNumber']) if isinstance(meta.get('trackNumber'), int) and meta['trackNumber'] else None
                placed = placed if placed is not None and (placed < 0) == (kind == 'audio') else (-number if kind == 'audio' else number)
                clip.update(kind=kind, media=key, speed=speed, reverse=reverse, track=placed,
                            **{'in': float(source_start - origin)})
                if kind == 'video':
                    clip['embeddedAudio'] = bool(meta.get('embeddedAudio', False))
            else:
                warnings.append(f'{clip["name"]}: its media reference is missing; left as a gap.')
                cursor += duration
                pending = None
                continue
            for key in ('volume', 'opacity', 'fadeIn', 'fadeOut', 'transform', 'linkGroup'):
                if key in meta:
                    clip[key] = meta[key]
            if isinstance(meta.get('keyframes'), dict):
                clip['keyframes'] = meta['keyframes']
            if pending is not None:
                try:
                    before = _seconds(pending.get('in_offset'), 'transition in offset')
                    after = _seconds(pending.get('out_offset'), 'transition out offset')
                    kind_name = ((pending.get('metadata') or {}).get('splice') or {}).get('kind', 'dissolve')
                    clip['transition'] = {'transition': kind_name if kind_name in ('dissolve', 'wipe', 'fade') else 'dissolve',
                                          'transitionDuration': float(before + after), 'transitionBefore': float(before)}
                except ValueError as error:
                    warnings.append(f'{clip["name"]}: transition {error}; imported as a cut.')
                pending = None
            clip['start'], clip['duration'] = float(clip['start']), float(clip['duration'])
            clips.append(clip)
            cursor += duration
    for marker in stack.get('markers') or []:
        try:
            at, _length = _range(marker.get('marked_range'), 'marker')
            markers.append({'time': float(at), 'name': str(marker.get('name') or 'Marker')})
        except (ValueError, AttributeError):
            warnings.append('A marker with an unreadable range was skipped.')
    width, height = int(splice.get('width') or 1920), int(splice.get('height') or 1080)
    return build_project(str(document.get('name') or 'Imported OTIO'), rate, width, height, list(media.values()),
                         clips, markers, warnings)
