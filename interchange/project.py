"""Build a Splice project from a timeline read out of FCPXML, an EDL or OTIO.

Readers hand over plain records: media (``key``, ``name``, ``path``, ``kind``, ``duration`` and
whatever timecode facts the file had) and clips in seconds with a track number, where
``track='audio-auto'`` asks for the first free audio track. Sources found inside Splice's own
media folders play at once; every other source arrives offline with its original path.
:func:`link_local_media` then links the ones that exist on this machine in place (no copy);
what is left offline is found by the existing relink flow (by file or folder). Every reader
warning becomes a ``compatibilityWarnings`` entry, shown in the Project compatibility panel.
"""
from __future__ import annotations

import math
import os
import re
import threading
from fractions import Fraction
from pathlib import Path

from .media import base_name, clean as text, is_absolute_path

MAX_IMPORT_CLIPS = 20_000
MAX_TRACKS = 64
MAX_TIMELINE_SECONDS = 24 * 3600     # a clip must end within 24 hours of the timeline start
MAX_SOURCE_SECONDS = 7 * 24 * 3600   # and start within a week of its source's first frame


def _seconds(value) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite time')
    return round(number, 9)


def _playable(path: str | None) -> str:
    """The Splice URL of a source inside Splice's own media folders, else ''. Only a path that
    names one of those folders is resolved, so a path on a slow or hung mount is never touched
    here (:func:`link_local_media` checks those under its time budget)."""
    if not path or not is_absolute_path(path) or path.startswith('\\\\') or ':' in path[:3]:
        return ''
    try:
        from premiere import MEDIA_ROOT, ROOT, _playback_url
        inside = any(os.path.normpath(path).startswith(str(base) + os.sep) for base in (ROOT / 'assets', MEDIA_ROOT))
        return _playback_url(Path(path)) if inside else ''
    except (ImportError, OSError, ValueError):
        return ''


def _bounded(work, budget):
    """``work()`` on a helper thread, waited for until ``budget`` runs out or is cancelled. A file
    system call on a hung network mount cannot be interrupted, so a call that does not return in
    time is abandoned (its thread is a daemon) and :class:`BudgetExceeded` is raised here."""
    from .budget import BudgetExceeded
    result = {}

    def run():
        try:
            result['value'] = work()
        except BaseException as error:  # handed to the waiting thread
            result['error'] = error
    thread = threading.Thread(target=run, daemon=True, name='splice-import-path-check')
    thread.start()
    while thread.is_alive():
        budget.check()
        thread.join(min(0.1, max(0.01, budget.remaining())))
        if thread.is_alive() and budget.remaining() <= 0:
            raise BudgetExceeded(f'{budget.what} took longer than {budget.seconds:g} seconds and was stopped.')
    if 'error' in result:
        raise result['error']
    return result.get('value')


KEYFRAME_KEYS = ('opacity', 'x', 'y', 'scale', 'rotation', 'volume')
LIMITS = {'volume': (0, 200), 'opacity': (0, 100), 'fadeIn': (0, 7200), 'fadeOut': (0, 7200), 'pan': (-100, 100)}
# The ranges engine.js draws (scale 0.1-4 of the fitted size, x/y within one frame width or
# height, rotation one turn) and inspector.js offers.
TRANSFORM = {'x': (-100, 100), 'y': (-100, 100), 'scale': (10, 400), 'rotation': (-180, 180)}
TRANSFORM_LABELS = {'x': 'horizontal position %', 'y': 'vertical position %', 'scale': 'scale %',
                    'rotation': 'rotation (degrees)', 'opacity': 'opacity %', 'volume': 'volume %'}
KEYFRAME_LIMITS = {**TRANSFORM, 'opacity': LIMITS['opacity'], 'volume': LIMITS['volume']}


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
            for key, (low, high) in TRANSFORM.items():
                if not low <= values[key] <= high:
                    warnings.append(f'{name}: {TRANSFORM_LABELS[key]} {values[key]:g} is outside Splice\'s '
                                    f'{low:g} to {high:g} and was set to {min(high, max(low, values[key])):g}.')
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
            low, high = KEYFRAME_LIMITS[key]
            outside = [point['value'] for point in good if not low <= point['value'] <= high]
            if outside:
                warnings.append(f'{name}: {len(outside)} {TRANSFORM_LABELS.get(key, key)} keyframe'
                                f'{"s" if len(outside) != 1 else ""} outside Splice\'s {low:g} to {high:g} '
                                f'(from {min(outside):g} to {max(outside):g}) {"were" if len(outside) != 1 else "was"} '
                                'limited.')
                for point in good:
                    point['value'] = min(high, max(low, point['value']))
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


def counted(warnings: list, limit: int = 500) -> list:
    """Each distinct warning once, in first-seen order, with how many times it occurred: three
    clips of one source skipped for the same reason are one line ending "(3 times)"."""
    counts = {}
    for warning in warnings:
        counts[warning] = counts.get(warning, 0) + 1
    return [text(w, 1000) if n == 1 else text(w, 980) + f' ({n} times)' for w, n in counts.items()][:limit]


def build_project(name: str, rate: Fraction, width: int, height: int, media: list, clips: list,
                  markers: list, warnings: list, start_frames=None, drop=None) -> dict:
    """Splice's project for an imported timeline.

    ``start_frames`` is the sequence start timecode as a whole frame count at ``rate`` and
    ``drop`` whether it is drop-frame; both land in the project the way engine.js keeps them
    (``startTimecode`` frames, ``dropFrame`` only for 29.97 and 59.94)."""
    if len(clips) > MAX_IMPORT_CLIPS:
        raise ValueError(f'The timeline has more than {MAX_IMPORT_CLIPS} clips')
    warnings = list(warnings)
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
        items.append(item)
    by_id = {item['id']: item for item in items}
    out, busy = [], {}
    ordered = sorted(clips, key=lambda c: (c['start'], 0 if c['kind'] != 'audio' else 1))
    for index, clip in enumerate(ordered):
        start, duration = _seconds(clip['start']), _seconds(clip['duration'])
        label = clip.get("name") or "A clip"
        if duration <= 0:
            warnings.append(f'{label} has no length and was skipped.')
            continue
        if start < 0:
            warnings.append(f'{label} starts before the timeline start and was skipped.')
            continue
        source_in = _seconds(clip.get('in', 0) or 0) if clip['kind'] != 'title' else 0.0
        if start + duration > MAX_TIMELINE_SECONDS:
            warnings.append(f'{label} ends more than 24 hours into the timeline (Splice\'s limit; no real timeline is '
                            'that long) and was skipped.')
            continue
        if source_in > MAX_SOURCE_SECONDS:
            warnings.append(f'{label} starts more than a week into its source (Splice\'s limit; no real recording is '
                            'that long) and was skipped.')
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
            speed = _finite(clip.get('speed', 1)) or 1.0
            limited = min(16.0, max(0.1, speed))  # what Splice plays, so what it reads of the source
            used = data['in'] + duration * limited
            item['duration'] = max(item['duration'], round(used, 9))
            if abs(speed - 1) > 1e-9:
                data['speed'] = round(limited, 6)
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
    # a source no imported clip uses is not part of the project (nor linked from this machine)
    used = {c['mediaId'] for c in out if c.get('mediaId')}
    unused = [item for item in items if item['id'] not in used]
    if unused:
        warnings.append(f'{len(unused)} source{"s" if len(unused) != 1 else ""} the file lists but no imported clip uses '
                        f'{"were" if len(unused) != 1 else "was"} left out.')
        items = [item for item in items if item['id'] in used]
    total = max((c['start'] + c['duration'] for c in out), default=0)
    video_tracks = max([4] + [c['track'] for c in out if c['type'] != 'audio'])
    audio_tracks = max([3] + [-c['track'] for c in out if c['type'] == 'audio'])
    result = {'name': text(name or 'Imported timeline', 120) or 'Imported timeline', 'width': int(width), 'height': int(height),
            'fps': float(rate), 'media': items, 'clips': out, 'captions': [],
            'markers': [{'id': f'marker-{i + 1}', 'name': text(m['name'], 500), 'time': _seconds(m['time'])}
                        for i, m in enumerate(markers) if 0 <= m['time'] <= total + 1e-6][:5000],
            'trackCounts': {'video': video_tracks, 'audio': audio_tracks}, 'trackState': {},
            'duration': total, 'compatibilityWarnings': counted(warnings)}
    day = 24 * 3600 * math.ceil(float(rate))
    if isinstance(start_frames, (int, Fraction)) and not isinstance(start_frames, bool) and 0 <= start_frames < day:
        result['startTimecode'] = int(round(start_frames))
    if rate in (Fraction(30000, 1001), Fraction(60000, 1001)):
        result['dropFrame'] = bool(drop)
    return result


LINKED_LIST = 10


def link_local_media(project: dict, budget, limit: int = 2000) -> None:
    """Link offline sources whose recorded path is a file on this machine through the server's
    link-in-place registry: no bytes are copied, and only files FFmpeg decodes as audio or video
    are registered (the same check as File > Link media in place), so a timeline cannot expose a
    text file, a key or any other non-media file through ``/media/linked/``. Files that fail the
    check, or that the time budget does not reach, stay offline with a compatibility warning.

    Every file-system call runs under ``budget`` (:func:`_bounded`): a path on a hung mount costs
    at most the budget, and Cancel stops the import between paths. What was linked is listed in
    a compatibility entry, because the timeline file, not the user, chose those paths and names."""
    from .budget import BudgetExceeded
    try:
        import media_links
        from media_proxy import decodable
    except ImportError:
        return
    warnings = project['compatibilityWarnings']
    candidates = [item for item in project['media'] if item.get('offline') and isinstance(item.get('sourcePath'), str)
                  and os.path.isabs(item['sourcePath']) and not item['sourcePath'].startswith('\\\\')]
    if len(candidates) > limit:
        warnings.append(f'{len(candidates) - limit} sources beyond the first {limit} were not looked for on this computer; '
                        'relink them by folder.')
        candidates = candidates[:limit]

    def precheck(path):
        """The file's identity once it passed the media check: (resolved path, size, mtime)."""
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file():
            raise FileNotFoundError(path)
        decodable(resolved, resolved.name)
        stat = resolved.stat()
        return str(resolved), stat.st_size, stat.st_mtime_ns

    checked, unchecked = {}, 0
    for index, item in enumerate(candidates):
        if budget.cancelled.is_set():
            raise BudgetExceeded(f'{budget.what} was cancelled.')
        if budget.remaining() <= 0:
            unchecked = len(candidates) - index
            break
        try:
            checked[id(item)] = _bounded(lambda: precheck(item['sourcePath']), budget)
        except BudgetExceeded:
            if budget.cancelled.is_set():
                raise
            unchecked = len(candidates) - index
            break
        except (OSError, ValueError) as error:
            if not isinstance(error, OSError):
                warnings.append(text(f'{item["name"]} at its recorded path was not linked ({error}); it stays offline '
                                     'for relinking.', 1000))
    if unchecked:
        warnings.append(f'{unchecked} source{"s" if unchecked != 1 else ""} could not be looked for on this computer within '
                        f'the import\'s {budget.seconds:g}-second limit (a drive may be slow or disconnected); '
                        f'{"they stay" if unchecked != 1 else "it stays"} offline for relinking.')
    wanted = [item for item in candidates if id(item) in checked]
    if not wanted:
        return
    identity = {checked[id(item)][0]: checked[id(item)][1:] for item in wanted}

    def unchanged(path, _name):
        # the file registered is the one that passed the media check above
        stat = Path(path).stat()
        if identity.get(str(path)) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError('it changed while the import was checking it')

    try:
        results = _bounded(lambda: media_links.link_many([checked[id(item)][0] for item in wanted], validate=unchanged), budget)
    except BudgetExceeded:
        if budget.cancelled.is_set():
            raise
        warnings.append(f'Linking {len(wanted)} sources found on this computer did not finish within the import\'s '
                        f'{budget.seconds:g}-second limit; they stay offline for relinking.')
        return
    linked = []
    for item, result in zip(wanted, results):
        if 'src' in result:
            item.update(src=result['src'], originalSrc=result['src'], path=result['sourcePath'],
                        sourcePath=result['sourcePath'], linked=True, sourceFingerprint=result['fingerprint'],
                        offline=False)
            linked.append(result['sourcePath'])
        else:
            warnings.append(text(f'{item["name"]} at its recorded path was not linked ({result.get("error")}); '
                                 'it stays offline for relinking.', 1000))
    sources = len(linked)
    linked = list(dict.fromkeys(linked))
    if linked:
        shown, room = [], 900
        for path in linked[:LINKED_LIST]:
            if len(path) + 2 > room:
                break
            shown.append(path)
            room -= len(path) + 2
        more = len(linked) - len(shown)
        used_by = f' (used by {sources} sources)' if sources != len(linked) else ''
        warnings.insert(0, f'Linked {len(linked)} file{"s" if len(linked) != 1 else ""}{used_by} from this computer in place, '
                           f'as the timeline file names {"them" if len(linked) != 1 else "it"} (nothing was copied): '
                           + '; '.join(shown) + (f'; and {more} more' if more else '') + '. Check that these are the media '
                           'you expect before you edit or share the project.')
