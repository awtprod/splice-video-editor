"""Premiere project bridge, backed by py-premiere's Premiere object graph writer."""
import base64
import math
import uuid
import zlib
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse, unquote, quote

sys.path.insert(0, str(Path(__file__).parent / 'vendor'))
import py_premiere
import premiere_effects
from py_premiere.models.sequence_builder import FORMATS, SequenceFormat

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


def _bin_path(item):
    names = []
    parent = item._parent
    while parent is not None and parent._parent is not None:
        names.append(parent.name)
        parent = parent._parent
    return '/'.join(reversed(names))


def _ensure_bin(project, folder, cache):
    if not folder:
        return project.root_item
    if folder in cache:
        return cache[folder]
    parent, _, name = folder.rpartition('/')
    if not name or name in ('.', '..'):
        raise ValueError('Invalid media bin')
    target = _ensure_bin(project, parent, cache)
    child = next((item for item in target.children if item.name == name and item.media_path is None), None)
    if child is None:
        child = target.add_bin(name)
    cache[folder] = child
    return child


def _nested_sequence_index(clip, project):
    """Resolve a sequence-backed source by Premiere's UID, never by its name."""
    source_ref = clip._clip_element.find('Clip/Source')
    if source_ref is None:
        return None
    source = project._document.resolve(source_ref)
    reference = source.find('SequenceSource/Sequence')
    if reference is None:
        return None
    uid = reference.get('ObjectURef')
    return next((index for index, sequence in enumerate(project.sequences)
                 if sequence._element.get('ObjectUID') == uid), None)


def open_project(data, index=0, _seen=()):
    _check_size(data)
    if index in _seen:
        raise ValueError('Premiere sequence nesting cycle is unsupported')
    if len(_seen) >= 4:
        raise ValueError('Premiere sequence nesting exceeds four levels')
    with tempfile.NamedTemporaryFile(suffix='.prproj') as source:
        source.write(data)
        source.flush()
        project = py_premiere.parse(source.name).project
        if not isinstance(index, int) or index < 0 or (project.sequences and index >= len(project.sequences)):
            raise ValueError('Choose a sequence in this Premiere project')
        if not project.sequences:
            media = []
            def visit(folder):
                for item in folder.children:
                    if item.media_path:
                        path = item.media_path
                        media.append({'id': f'm{len(media)+1}', 'name': PureWindowsPath(path).name,
                                      'src': _playback_url(path), 'duration': item.out_point.seconds if item.out_point else 0,
                                      'thumb': '', 'type': 'audio' if path.suffix.lower() in {'.wav', '.mp3', '.m4a', '.aac', '.aiff'} else 'video',
                                      'path': str(path), 'bin': _bin_path(item)})
                    visit(item)
            if project.root_item:
                visit(project.root_item)
            return {'name': 'New sequence', 'width': 1920, 'height': 1080, 'fps': 25,
                    'media': media, 'clips': [], 'markers': [], 'duration': 0,
                    'premiereOriginal': base64.b64encode(data).decode('ascii'), 'premiereManaged': [],
                    'premiereSequences': [], 'premiereSequence': 0}
        sequence = project.sequences[index]
        media = []
        clips = []
        managed = []
        identities = {}
        nested_identities = {}
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
                                          'transition': 'none', **premiere_effects.read_extended(item)})
                            continue
                        nested = _nested_sequence_index(item, project)
                        if nested is not None:
                            identity_key = (nested, track_type)
                            if identity_key not in nested_identities:
                                identity = f'm{len(media) + 1}'
                                nested_identities[identity_key] = identity
                                media.append({'id': identity, 'name': project.sequences[nested].name,
                                              'src': '', 'duration': 0, 'thumb': '',
                                              'type': track_type, 'nestedSequence': nested})
                            identity = nested_identities[identity_key]
                            source_media = next(m for m in media if m['id'] == identity)
                            source_media['duration'] = max(source_media['duration'], item.in_point.seconds + item.duration.seconds)
                            managed.append([track_type, track_number, clip_index])
                            clips.append({'id': f'c{len(clips) + 1}', 'mediaId': identity,
                                          'start': item.start.seconds, 'duration': item.duration.seconds,
                                          'in': item.in_point.seconds, 'track': (-(track_number + 1) if track_type == 'audio' else track_number + 1),
                                          'type': track_type, 'nestedSequence': nested,
                                          'premiereTrack': track_number, 'premiereIndex': clip_index,
                                          'title': '', 'color': {}, 'volume': 100,
                                          'transition': 'none'})
                        continue
                    path = source_item.media_path
                    if path not in identities:
                        identity = f'm{len(media) + 1}'
                        identities[path] = identity
                        src = _playback_url(path)
                        media.append({'id': identity, 'name': PureWindowsPath(path).name, 'src': src,
                                      'duration': max(0, source_item.out_point.seconds) if source_item.out_point else 0,
                                      'thumb': '', 'type': track_type, 'path': str(path), 'bin': _bin_path(source_item)})
                    managed.append([track_type, track_number, clip_index])
                    color, volume = premiere_effects.read(item)
                    clips.append({'id': f'c{len(clips) + 1}', 'mediaId': identities[path],
                                  'start': item.start.seconds, 'duration': item.duration.seconds,
                                  'in': item.in_point.seconds, 'track': (-(track_number + 1) if track_type == 'audio' else track_number + 1),
                                  'type': track_type, 'embeddedAudio': False, 'premiereTrack': track_number, 'premiereIndex': clip_index, 'title': '', 'color': color,
                                  'volume': volume, 'transition': 'none', **premiere_effects.read_extended(item)})
                    matching = next((t for t in track.transitions if abs(t.cut_point.seconds - item.start.seconds) < .05), None)
                    if matching is not None:
                        clips[-1]['transition'] = 'fade' if matching.match_name == 'AE.ADBE Dip To Black' else 'dissolve' if matching.match_name == 'AE.ADBE Cross Dissolve New' else 'native'
                        clips[-1]['transitionDuration'] = matching.duration.seconds
                        clips[-1]['transitionMatchName'] = matching.match_name
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
                                      'thumb': '', 'type': kind, 'path': str(path), 'bin': _bin_path(source_item)})
                    include_unused(source_item)
            include_unused(project.root_item)
        for item in media:
            if not item['duration']:
                used = [clip['in'] + clip['duration'] for clip in clips if clip.get('mediaId') == item['id']]
                item['duration'] = max(used, default=0)
            if item.get('nestedSequence') is not None:
                child = open_project(data, item['nestedSequence'], (*_seen, index))
                child.pop('premiereOriginal', None)
                child.pop('premiereManaged', None)
                item['nestedProject'] = child
        width, height = sequence.frame_size or (1920, 1080)
        fps = py_premiere.TICKS_PER_SECOND / sequence.timebase if sequence.timebase else 30
        return {'name': sequence.name, 'width': width, 'height': height, 'fps': fps,
                'premiereSequences': [item.name for item in project.sequences], 'premiereSequence': index,
                'media': media, 'clips': clips,
                'duration': max((c['start'] + c['duration'] for c in clips), default=0),
                'premiereOriginal': base64.b64encode(data).decode('ascii'), 'premiereManaged': managed,
                'markers': [{'id': marker.guid, 'name': marker.name, 'time': marker.start.seconds} for marker in sequence.markers]}


def save_project(data):
    if not isinstance(data, dict):
        raise ValueError('Invalid project')
    for clip in data.get('clips', []):
        if (clip.get('color') or {}).get('lut'):
            raise ValueError('Premiere export cannot write imported LUTs; save a Splice project or remove the LUT')
    for mix in (data.get('trackMix') or {}).values():
        if (float(mix.get('gain', 100)) != 100 or float(mix.get('pan', 0)) != 0
                or mix.get('mute', False) or mix.get('solo', False)):
            raise ValueError('Premiere export cannot write track mixer changes; save a Splice project or reset the mixer')
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
    requested_fps = float(data.get('fps', 25))
    width, height = int(data.get('width', 1920)), int(data.get('height', 1080))
    if not (16 <= width <= 3840 and 16 <= height <= 2160 and math.isfinite(requested_fps)):
        raise ValueError('Unsupported sequence dimensions or frame rate')
    rates = {24000/1001: (10594584000, 110), 24: (10584000000, 100),
             25: (10160640000, 101), 30000/1001: (8475667200, 103),
             30: (8467200000, 104), 50: (5080320000, 105),
             60000/1001: (4237833600, 107), 60: (4233600000, 108)}
    chosen = next(((ticks, display) for rate, (ticks, display) in rates.items()
                   if abs(requested_fps-rate) < .001), None)
    if not original and chosen is None:
        raise ValueError('Premiere export supports 23.976, 24, 25, 29.97, 30, 50, 59.94, or 60 fps')
    if original and project.sequences:
        sequence_index = int(data.get('premiereSequence', 0))
        if not 0 <= sequence_index < len(project.sequences):
            raise ValueError('Premiere sequence is unavailable')
        sequence = project.sequences[sequence_index]
        sequence.name = str(data.get('name') or sequence.name)[:120]
        if (width,height) != tuple(sequence.frame_size or (1920,1080)) or abs(requested_fps-py_premiere.TICKS_PER_SECOND/sequence.timebase) > .001:
            raise ValueError('Changing an existing Premiere sequence format is unsupported')
    else:
        base = FORMATS['1080p25']
        custom = base._replace(frame_rate=chosen[0], width=width, height=height, time_display=chosen[1], video_tracks=4)
        sequence = project.add_sequence(str(data.get('name') or 'Untitled')[:120], sequence_format=custom)
    existing_markers = {marker.guid: marker for marker in sequence.markers}
    if 'markers' in data:
        retained_markers = set()
        for marker_data in data['markers']:
            identity = marker_data.get('id')
            marker = existing_markers.get(identity)
            name = str(marker_data.get('name') or 'Marker')
            when = py_premiere.Time.from_seconds(float(marker_data.get('time', 0)))
            if marker is None:
                marker = sequence.add_marker(name, when)
            elif marker.name != name or marker.start != when:
                marker.name = name
                marker.start = when
            retained_markers.add(marker.guid)
        for identity, marker in existing_markers.items():
            if identity not in retained_markers:
                sequence.remove_marker(marker)
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
    bin_cache = {}
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
            if 'bin' in item:
                previous.move_to(_ensure_bin(project, item['bin'], bin_cache))
            available[item['id']] = previous
            continue
        if not src:
            continue
        path = _media_path(src)
        available[item['id']] = project.import_files([path])[0]
        if item.get('bin'):
            available[item['id']].move_to(_ensure_bin(project, item['bin'], bin_cache))
    # Put linked camera sound on a free audio track alongside the explicit mix.
    audio_regions = [[] for _ in sequence.audio_tracks]
    for region in data.get('clips', []):
        if region['type'] == 'audio':
            track = -int(region.get('track', -1)) - 1
            if not 0 <= track < len(audio_regions):
                raise ValueError('Unsupported audio track')
            audio_regions[track].append((region['start'], region['start'] + region['duration']))
    pending_transitions = []
    for item in data.get('clips', []):
        companion = None
        source = available.get(item.get('mediaId'))
        is_audio = item.get('type') == 'audio'
        tracks = sequence.audio_tracks if is_audio else sequence.video_tracks
        ui_track = int(item.get('track', -1 if is_audio else 1))
        index = -ui_track - 1 if is_audio else ui_track - 1
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
                if is_audio:
                    clip.track.move_clip(clip, tracks[index])
                else:
                    for transition in list(clip.track.transitions):
                        if abs(transition.cut_point.seconds - clip.start.seconds) < .05 or abs(transition.cut_point.seconds - clip.end.seconds) < .05:
                            clip.track.remove_transition(transition)
                    clip.track.move_clip(clip, tracks[index])
            original_start = clip.start.seconds
            if not is_audio and (item.get('transition') == 'none' or abs(float(item.get('start', 0)) - original_start) > 0.001):
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
            if item.get('nestedSequence') is not None:
                raise ValueError('Adding a nested Premiere sequence clip is unsupported')
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
        premiere_effects.apply_extended(clip, item)
        if item.get('transition') in ('dissolve', 'fade') and not is_audio:
            pending_transitions.append((clip, item))
    if original:
        retained = {(('video' if c.get('type') == 'title' else c.get('type')), c.get('premiereTrack'), c.get('premiereIndex')) for c in data.get('clips', []) if 'premiereIndex' in c}
        for key, clip in original_clips.items():
            if list(key) in data.get('premiereManaged', []) and key not in retained:
                for transition in list(clip.track.transitions):
                    if abs(transition.cut_point.seconds - clip.start.seconds) < 0.05 or abs(transition.cut_point.seconds - clip.end.seconds) < 0.05:
                        clip.track.remove_transition(transition)
                clip.track.remove_clip(clip)
    for clip, item in pending_transitions:
        match = 'AE.ADBE Dip To Black' if item['transition'] == 'fade' else 'AE.ADBE Cross Dissolve New'
        if any(abs(t.cut_point.seconds - clip.start.seconds) < .05 for t in clip.track.transitions):
            continue
        clip.track.add_transition(clip, match, at_start=True,
            name='Dip To Black' if item['transition'] == 'fade' else 'Cross Dissolve',
            duration=py_premiere.Time.from_seconds(float(item.get('transitionDuration', .6))))
    with tempfile.TemporaryDirectory() as temp:
        destination = Path(temp) / 'project.prproj'
        project.save(destination)
        return destination.read_bytes()
