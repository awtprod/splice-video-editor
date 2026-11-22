"""Premiere project bridge, backed by py-premiere's Premiere object graph writer."""
import base64
import zlib
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse, unquote, quote

sys.path.insert(0, str(Path(__file__).parent / 'vendor'))
import py_premiere
import premiere_effects

ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = Path.home() / '.local' / 'share' / 'splice' / 'media'


def _media_path(src):
    path = unquote(urlparse(src).path)
    if path.startswith('assets/'):
        path = '/' + path
    if path.startswith('/media/'):
        base = MEDIA_ROOT
        path = path[len('/media/'):]
    elif path.startswith('/assets/'):
        base = ROOT / 'assets'
        path = path[len('/assets/'):]
    else:
        raise ValueError('Media must be an uploaded file or bundled asset')
    resolved = (base / path).resolve()
    if not resolved.is_file() or not resolved.is_relative_to(base):
        raise ValueError('Media file is unavailable')
    return resolved


def _playback_url(path):
    resolved = path.resolve()
    for prefix, base in (('/assets/', ROOT / 'assets'), ('/media/', MEDIA_ROOT)):
        if resolved.is_relative_to(base) and resolved.is_file():
            return prefix + quote(str(resolved.relative_to(base)))
    return ''


def _check_size(data):
    if len(data) > 50 * 1024 * 1024:
        raise ValueError('Premiere project is too large')
    if data.startswith(b'\x1f\x8b'):
        reader = zlib.decompressobj(16 + zlib.MAX_WBITS)
        expanded = reader.decompress(data, 250 * 1024 * 1024 + 1)
        if len(expanded) > 250 * 1024 * 1024 or reader.unconsumed_tail:
            raise ValueError('Premiere project expands beyond the supported size')


def open_project(data):
    _check_size(data)
    with tempfile.NamedTemporaryFile(suffix='.prproj') as source:
        source.write(data)
        source.flush()
        project = py_premiere.parse(source.name).project
        sequence = next(iter(project.sequences), None)
        if sequence is None:
            raise ValueError('This Premiere project has no sequence')
        media = []
        clips = []
        managed = []
        identities = {}
        for track_type, tracks in (('video', sequence.video_tracks), ('audio', sequence.audio_tracks)):
            for track_number, track in enumerate(tracks):
                for clip_index, item in enumerate(track.clips):
                    source_item = item.project_item
                    if source_item is None or source_item.media_path is None:
                        text_component = next((component for component in item.components if component.match_name == 'AE.ADBE Text'), None)
                        if text_component is not None:
                            source_text = next((param.text for param in text_component.properties if param.display_name == 'Source Text'), '')
                            managed.append([track_type, track_number, clip_index])
                            clips.append({'id': f'c{len(clips) + 1}', 'mediaId': None,
                                          'start': item.start.seconds, 'duration': item.duration.seconds,
                                          'in': 0, 'track': track_number + 1, 'type': 'title',
                                          'premiereTrack': track_number, 'premiereIndex': clip_index,
                                          'title': source_text or '', 'color': {}, 'volume': 100,
                                          'transition': ''})
                        continue
                    path = source_item.media_path
                    if path not in identities:
                        identity = f'm{len(media) + 1}'
                        identities[path] = identity
                        src = _playback_url(path)
                        media.append({'id': identity, 'name': PureWindowsPath(path).name, 'src': src,
                                      'duration': max(0, source_item.out_point.seconds) if source_item.out_point else 0,
                                      'thumb': '', 'type': track_type, 'path': str(path)})
                    managed.append([track_type, track_number, clip_index])
                    color, volume = premiere_effects.read(item)
                    clips.append({'id': f'c{len(clips) + 1}', 'mediaId': identities[path],
                                  'start': item.start.seconds, 'duration': item.duration.seconds,
                                  'in': item.in_point.seconds, 'track': (-1 if track_type == 'audio' else track_number + 1),
                                  'type': track_type, 'embeddedAudio': False, 'premiereTrack': track_number, 'premiereIndex': clip_index, 'title': '', 'color': color,
                                  'volume': volume, 'transition': ('dissolve' if any(abs(transition.cut_point.seconds - item.start.seconds) < 0.05 for transition in track.transitions) else 'none')})
        if project.root_item:
            def include_unused(folder):
                for source_item in folder.children:
                    path = source_item.media_path
                    if path is not None and path not in identities:
                        identity = f'm{len(media) + 1}'
                        identities[path] = identity
                        src = _playback_url(path)
                        kind = 'audio' if path.suffix.lower() in {'.wav', '.mp3', '.m4a', '.aac', '.aiff'} else 'video'
                        media.append({'id': identity, 'name': PureWindowsPath(path).name, 'src': src,
                                      'duration': max(0, source_item.out_point.seconds) if source_item.out_point else 0,
                                      'thumb': '', 'type': kind, 'path': str(path)})
                    include_unused(source_item)
            include_unused(project.root_item)
        for item in media:
            if not item['duration']:
                used = [clip['in'] + clip['duration'] for clip in clips if clip.get('mediaId') == item['id']]
                item['duration'] = max(used, default=0)
        width, height = sequence.frame_size or (1920, 1080)
        fps = py_premiere.TICKS_PER_SECOND / sequence.timebase if sequence.timebase else 30
        return {'name': sequence.name, 'width': width, 'height': height, 'fps': fps,
                'media': media, 'clips': clips,
                'duration': max((c['start'] + c['duration'] for c in clips), default=0),
                'premiereOriginal': base64.b64encode(data).decode('ascii'), 'premiereManaged': managed}


def save_project(data):
    if not isinstance(data, dict):
        raise ValueError('Invalid project')
    original = data.get('premiereOriginal')
    if original and len(original) > 70 * 1024 * 1024:
        raise ValueError('Original Premiere project is too large')
    if original:
        raw = base64.b64decode(original, validate=True)
        _check_size(raw)
        with tempfile.NamedTemporaryFile(suffix='.prproj') as source:
            source.write(raw)
            source.flush()
            app = py_premiere.parse(source.name)
    else:
        app = py_premiere.new()
    project = app.project
    fps = round(float(data.get('fps', 30)))
    preset = {24: '1080p23976', 25: '1080p25', 30: '1080p2997',
              50: '1080p50', 60: '1080p5994'}.get(fps, '1080p2997')
    if original:
        sequence = next(iter(project.sequences), None)
        if sequence is None:
            raise ValueError('Original Premiere project has no sequence')
        sequence.name = str(data.get('name') or sequence.name)[:120]
    else:
        sequence = project.add_sequence(str(data.get('name') or 'Untitled')[:120], preset)
    original_clips = {}
    if original:
        for kind, tracks in (('video', sequence.video_tracks), ('audio', sequence.audio_tracks)):
            for track_index, track in enumerate(tracks):
                for clip_index, clip in enumerate(track.clips):
                    original_clips[(kind, track_index, clip_index)] = clip
    available = {}
    existing = {}
    if original and project.root_item:
        def collect(folder):
            for child in folder.children:
                if child.media_path:
                    existing[str(child.media_path)] = child
                collect(child)
        collect(project.root_item)
    for item in data.get('media', []):
        src = item.get('src', '')
        if original and item.get('path') in existing:
            previous = existing[item['path']]
            if src.startswith('/media/'):
                updated = str(_media_path(src))
                if updated != item['path']:
                    media_element = previous._media_element()
                    if media_element is None:
                        raise ValueError('Cannot relink this Premiere media object')
                    for node in media_element.iter():
                        if node.tag in ('FilePath', 'ActualMediaFilePath', 'RelativePath'):
                            node.text = updated
                    previous._media_path = Path(updated)
            available[item['id']] = previous
            continue
        if not src:
            continue
        path = _media_path(src)
        available[item['id']] = project.import_files([path])[0]
    # Put linked camera sound on a free audio track alongside the explicit mix.
    audio_regions = [[] for _ in sequence.audio_tracks]
    for region in data.get('clips', []):
        if region['type'] == 'audio':
            track = int(region.get('premiereTrack', 0))
            if not 0 <= track < len(audio_regions):
                raise ValueError('Unsupported audio track')
            audio_regions[track].append((region['start'], region['start'] + region['duration']))
    for item in data.get('clips', []):
        companion = None
        source = available.get(item.get('mediaId'))
        is_audio = item.get('type') == 'audio'
        tracks = sequence.audio_tracks if is_audio else sequence.video_tracks
        ui_track = int(item.get('track', -1 if is_audio else 1))
        index = int(item.get('premiereTrack', 0)) if is_audio and 'premiereIndex' in item else (0 if is_audio else ui_track - 1)
        if index < 0 or index >= len(tracks):
            raise ValueError('Unsupported track number')
        if original and 'premiereIndex' in item:
            original_track = int(item.get('premiereTrack', index))
            if original_track >= len(tracks):
                raise ValueError('Premiere track is unavailable')
            source_index = int(item['premiereIndex'])
            clip = original_clips.get(('audio' if is_audio else 'video', original_track, source_index))
            if clip is None:
                raise ValueError('Original Premiere clip is unavailable')
            if index != original_track:
                raise ValueError('Moving imported clips between tracks is unavailable')
            original_start = clip.start.seconds
            if not is_audio and (item.get('transition') != 'dissolve' or abs(float(item.get('start', 0)) - original_start) > 0.001):
                for transition in list(tracks[index].transitions):
                    if abs(transition.cut_point.seconds - original_start) < 0.05:
                        tracks[index].remove_transition(transition)
            if original_start != item['start']:
                clip.start = py_premiere.Time.from_seconds(float(item.get('start', 0)))
            if item.get('type') == 'title':
                text_component = next((component for component in clip.components if component.match_name == 'AE.ADBE Text'), None)
                if text_component is not None:
                    source_text = next((param for param in text_component.properties if param.display_name == 'Source Text'), None)
                    if source_text is not None and source_text.text != item.get('title'):
                        source_text.text = str(item.get('title') or '')
        else:
            if item.get('type') == 'title':
                clip = tracks[index].add_graphic(str(item.get('title') or 'Title'),
                    py_premiere.Time.from_seconds(float(item.get('start', 0))),
                    py_premiere.Time.from_seconds(max(0.001, float(item.get('duration', 1)))))
            else:
                if source is None:
                    raise ValueError('A clip has media that must be relinked before saving to Premiere')
                placement = py_premiere.Time.from_seconds(float(item['start']))
                has_audio = any(element.tag == 'AudioClip' for element in source._clip_elements)
                if not is_audio and has_audio and item.get('embeddedAudio', True):
                    begin, end = item['start'], item['start'] + item['duration']
                    free = next((i for i, regions in enumerate(audio_regions)
                                 if all(a >= end or b <= begin for a, b in regions)), None)
                    if free is None:
                        raise ValueError('No free audio track for linked camera sound')
                    clip, companion = sequence.add_linked_clip(source, placement, index, free)
                    audio_regions[free].append((begin, end))
                else:
                    clip = tracks[index].add_clip(source, placement)
        start = max(0, float(item.get('in', 0)))
        duration = max(0.001, float(item.get('duration', 1)))
        # A retimed Premiere clip can have a different source span than its
        # timeline duration. Leave its original out point alone on unrelated edits.
        source_changed = abs(clip.in_point.seconds - start) > 1e-9
        length_changed = abs(clip.duration.seconds - duration) > 1e-9
        if source_changed:
            clip.in_point = py_premiere.Time.from_seconds(start)
        if source_changed or length_changed or 'premiereIndex' not in item:
            clip.out_point = py_premiere.Time.from_seconds(start + duration)
            clip.end = py_premiere.Time.from_seconds(float(item.get('start', 0)) + duration)
        if companion is not None:
            companion.in_point = clip.in_point
            companion.out_point = clip.out_point
            companion.end = clip.end
            premiere_effects.apply(companion, {}, float(item.get('volume', 100)))
        premiere_effects.apply(clip, item.get('color') or {}, float(item.get('volume', 100)))
        if item.get('transition') == 'dissolve' and not is_audio:
            if not any(abs(transition.cut_point.seconds - clip.start.seconds) < 0.05 for transition in tracks[index].transitions):
                tracks[index].add_transition(clip, 'AE.ADBE Cross Dissolve New', at_start=True, name='Cross Dissolve',
                    duration=py_premiere.Time.from_seconds(min(1, duration)))
    if original:
        retained = {(('video' if c.get('type') == 'title' else c.get('type')), c.get('premiereTrack'), c.get('premiereIndex')) for c in data.get('clips', []) if 'premiereIndex' in c}
        for key, clip in original_clips.items():
            if list(key) in data.get('premiereManaged', []) and key not in retained:
                for transition in list(clip.track.transitions):
                    if abs(transition.cut_point.seconds - clip.start.seconds) < 0.05 or abs(transition.cut_point.seconds - clip.end.seconds) < 0.05:
                        clip.track.remove_transition(transition)
                clip.track.remove_clip(clip)
    with tempfile.TemporaryDirectory() as temp:
        destination = Path(temp) / 'project.prproj'
        project.save(destination)
        return destination.read_bytes()
