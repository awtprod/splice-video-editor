"""Build a Splice project from a timeline read out of FCPXML, an EDL or OTIO.

Readers hand over plain records: media (``key``, ``name``, ``path``, ``kind``, ``duration`` and
whatever timecode facts the file had) and clips in seconds with a track number, where
``track='audio-auto'`` asks for the first free audio track. Sources found inside Splice's own
media folders play at once; every other source arrives offline with its original path, so the
existing relink flow (by file or folder) can find it. Every reader warning becomes a
``compatibilityWarnings`` entry, shown in the Project compatibility panel.
"""
from __future__ import annotations

import math
import re
from fractions import Fraction
from pathlib import Path

from .media import base_name, clean as text, is_absolute_path

MAX_IMPORT_CLIPS = 20_000
MAX_TRACKS = 64


def _seconds(value) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite time')
    return round(number, 9)


def _playable(path: str | None) -> str:
    if not path or not is_absolute_path(path) or path.startswith('\\\\') or ':' in path[:3]:
        return ''
    try:
        from premiere import _playback_url
        return _playback_url(Path(path))
    except (OSError, ValueError):
        return ''


KEYFRAME_KEYS = ('opacity', 'x', 'y', 'scale', 'rotation', 'volume')
LIMITS = {'volume': (0, 200), 'opacity': (0, 100), 'fadeIn': (0, 7200), 'fadeOut': (0, 7200), 'pan': (-100, 100)}
TRANSFORM = {'x': (-100, 100), 'y': (-100, 100), 'scale': (10, 400), 'rotation': (-180, 180)}


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_effects(clip: dict, warnings: list) -> dict:
    """Only well-typed, in-range values reach Splice; anything else is dropped with a warning."""
    name = clip.get('name') or 'A clip'
    out = {}
    for key, (low, high) in LIMITS.items():
        if clip.get(key) is None:
            continue
        number = _finite(clip[key])
        if number is None:
            warnings.append(f'{name}: {key} is not a number and was not imported.')
            continue
        if not low <= number <= high:
            warnings.append(f'{name}: {key} {number:g} is outside {low:g}-{high:g} and was limited.')
        out[key] = min(high, max(low, number))
    transform = clip.get('transform')
    if transform is not None:
        values = {k: _finite(transform.get(k)) if isinstance(transform, dict) else None for k in TRANSFORM}
        if all(v is not None for v in values.values()):
            out['transform'] = {k: min(h, max(lo, values[k])) for k, (lo, h) in TRANSFORM.items()}
        else:
            warnings.append(f'{name}: its transform is incomplete and was not imported.')
    keyframes = clip.get('keyframes')
    if isinstance(keyframes, dict):
        cleaned = {}
        for key, points in keyframes.items():
            if key not in KEYFRAME_KEYS or not isinstance(points, list):
                warnings.append(f'{name}: keyframes for "{text(key, 40)}" are not supported and were not imported.')
                continue
            good = [{'time': max(0.0, _finite(p.get('time'))), 'value': _finite(p.get('value'))} for p in points[:2000]
                    if isinstance(p, dict) and _finite(p.get('time')) is not None and _finite(p.get('value')) is not None]
            if good:
                cleaned[key] = good
        if cleaned:
            out['keyframes'] = cleaned
    if isinstance(clip.get('linkGroup'), str) and clip['linkGroup']:
        out['linkGroup'] = text(clip['linkGroup'], 128)
    style = clip.get('titleStyle')
    if isinstance(style, dict):
        clean_style = {}
        if isinstance(style.get('fontFamily'), str):
            clean_style['fontFamily'] = text(style['fontFamily'], 80)
        if isinstance(style.get('color'), str) and re.fullmatch(r'#[0-9a-fA-F]{6}', style['color']):
            clean_style['color'] = style['color']
        if style.get('align') in ('left', 'center', 'right'):
            clean_style['align'] = style['align']
        for key, low, high in (('fontSize', 1, 50), ('x', 0, 100), ('y', 0, 100)):
            number = _finite(style.get(key))
            if number is not None:
                clean_style[key] = min(high, max(low, number))
        if clean_style:
            out['titleStyle'] = clean_style
    return out


def build_project(name: str, rate: Fraction, width: int, height: int, media: list, clips: list,
                  markers: list, warnings: list) -> dict:
    if len(clips) > MAX_IMPORT_CLIPS:
        raise ValueError(f'The timeline has more than {MAX_IMPORT_CLIPS} clips')
    warnings = list(dict.fromkeys(warnings))
    ids, items = {}, []
    for record in media:
        identity = f'm{len(items) + 1}'
        ids[record['key']] = identity
        path = record.get('path') or ''
        src = _playable(path)
        item = {'id': identity, 'name': text(record.get('name') or base_name(path) or identity),
                'src': src, 'thumb': '', 'type': 'audio' if record.get('kind') == 'audio' else 'video',
                'duration': 0.0, 'path': path or str(record.get('name') or identity), 'offline': not src}
        if path:
            item['sourcePath'] = path
        for key in ('timecode', 'rate', 'dropFrame', 'reel'):
            if isinstance(record.get(key), (str, bool)) and record.get(key) != '':
                item[key] = text(record[key], 64) if isinstance(record[key], str) else record[key]
        if record.get('duration'):
            item['duration'] = _seconds(record['duration'])
        if src:
            item['originalSrc'] = src
        elif path and is_absolute_path(path) and not path.startswith('\\\\') and Path(path).is_file():
            warnings.append(f'{item["name"]} was found at its original path; use Relink to bring it into Splice.')
        items.append(item)
    by_id = {item['id']: item for item in items}
    out, busy = [], {}
    ordered = sorted(clips, key=lambda c: (c['start'], 0 if c['kind'] != 'audio' else 1))
    for index, clip in enumerate(ordered):
        start, duration = _seconds(clip['start']), _seconds(clip['duration'])
        if duration <= 0 or start < 0:
            warnings.append(f'{clip.get("name") or "A clip"} has no length or starts before the timeline and was skipped.')
            continue
        side = -1 if clip['kind'] == 'audio' else 1
        track = clip.get('track')
        if track == 'audio-auto' or not isinstance(track, int) or track == 0 or (track < 0) != (side < 0):
            track = side
        wanted = track
        while any(start < end - 1e-6 and begin < start + duration - 1e-6 for begin, end in busy.get((side, track), ())):
            track += side
        if abs(track) > MAX_TRACKS:
            raise ValueError(f'The timeline needs more than {MAX_TRACKS} tracks')
        if track != wanted and clip.get('track') != 'audio-auto':
            warnings.append(f'{clip.get("name") or "A clip"} overlapped another clip on its track and was placed on '
                            f'{"A" if side < 0 else "V"}{abs(track)}.')
        busy.setdefault((side, track), []).append((start, start + duration))
        data = {'id': f'c{index + 1}', 'mediaId': None, 'start': start, 'duration': duration, 'in': 0, 'track': track,
                'type': clip['kind'], 'title': '', 'color': {}, 'volume': 100, 'transition': 'none'}
        if clip['kind'] != 'title':
            media_id = ids[clip['media']]
            data['mediaId'] = media_id
            data['in'] = max(0.0, _seconds(clip.get('in', 0)))
            data['color'] = {'brightness': 100, 'contrast': 100, 'saturation': 100}
            item = by_id[media_id]
            speed = float(clip.get('speed', 1) or 1)
            used = data['in'] + duration * speed
            item['duration'] = max(item['duration'], round(used, 9))
            if abs(speed - 1) > 1e-9:
                data['speed'] = round(min(16.0, max(0.1, speed)), 6)
                if speed < .1 or speed > 16:
                    warnings.append(f'{clip.get("name")}: speed {speed:g}x is outside Splice\'s 0.1x-16x and was limited.')
            if clip.get('reverse'):
                data['reverse'] = True
            if clip['kind'] == 'video':
                data['embeddedAudio'] = bool(clip.get('embeddedAudio', False))
        else:
            data['title'] = text(clip.get('text'), 2000).strip() or 'Title'
        data.update(_clean_effects(clip, warnings))
        if clip.get('enabled') is False:
            data['enabled'] = False
            warnings.append(f'{clip.get("name") or "A clip"} at {start:.2f} s is disabled in the source timeline; '
                            'Splice has no disabled state and plays it. Delete it if it should stay hidden.')
        transition = clip.get('transition')
        if isinstance(transition, dict) and transition.get('transition') in ('dissolve', 'wipe', 'fade'):
            length, before = _finite(transition.get('transitionDuration')), _finite(transition.get('transitionBefore', 0))
            if length and length > 0 and before is not None and 0 <= before <= length:
                data.update(transition=transition['transition'], transitionDuration=length, transitionBefore=before)
            else:
                warnings.append(f'{clip.get("name") or "A clip"}: its transition has an invalid length and was imported as a cut.')
        out.append(data)
    total = max((c['start'] + c['duration'] for c in out), default=0)
    video_tracks = max([4] + [c['track'] for c in out if c['type'] != 'audio'])
    audio_tracks = max([3] + [-c['track'] for c in out if c['type'] == 'audio'])
    return {'name': text(name or 'Imported timeline', 120) or 'Imported timeline', 'width': int(width), 'height': int(height),
            'fps': float(rate), 'media': items, 'clips': out, 'captions': [],
            'markers': [{'id': f'marker-{i + 1}', 'name': text(m['name'], 500), 'time': _seconds(m['time'])}
                        for i, m in enumerate(markers) if 0 <= m['time'] <= total + 1e-6][:5000],
            'trackCounts': {'video': video_tracks, 'audio': audio_tracks}, 'trackState': {},
            'duration': total, 'compatibilityWarnings': [text(w, 1000) for w in dict.fromkeys(warnings)][:500]}
