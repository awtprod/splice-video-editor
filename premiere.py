"""Premiere project bridge, backed by py-premiere's Premiere object graph writer."""
import base64
import hashlib
import math
import uuid
import zlib
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse, unquote, quote
from splice_platform import data_directory

sys.path.insert(0, str(Path(__file__).parent / 'vendor'))
import py_premiere
import premiere_effects
from py_premiere.models.sequence_builder import FORMATS, SequenceFormat
from py_premiere.models.color import Color
from py_premiere.xml.mutations import insert_leaf_before, remove_child

TRANSITIONS = {
    'dissolve': ('AE.ADBE Cross Dissolve New', 'Cross Dissolve'),
    'fade': ('AE.ADBE Dip To Black', 'Dip To Black'),
    'wipe': ('ADBE Wipe', 'Wipe'),
}
TRANSITION_BY_MATCH = {match:style for style,(match,_) in TRANSITIONS.items()}
AUDIO_CROSSFADE = 'Constant Gain'
AUDIO_CROSSFADE_NAMES = {AUDIO_CROSSFADE,'Constant Power'}

STILL_IMAGES = frozenset({".png", ".bmp", ".jpg", ".jpeg"})
ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = data_directory() / 'media'


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
            return prefix + quote(resolved.relative_to(base).as_posix())
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


def _audio_track_param(track, name):
    document = track.sequence.project._document
    reference = track._element.find('AudioTrack/ComponentOwner/Components')
    if reference is None:
        return None
    chain = document.resolve(reference)
    for component_ref in chain.findall('ComponentChain/Components/Component'):
        component = document.resolve(component_ref)
        for param_ref in component.findall('AudioComponent/Component/Params/Param'):
            param = document.resolve(param_ref)
            if param.findtext('Name') == name:
                return param
    if name == 'Balance':
        panner_ref = track._element.find('AudioTrack/Panner')
        if panner_ref is not None:
            panner = document.resolve(panner_ref)
            for param_ref in panner.findall('PanProcessor/AudioComponent/Component/Params/Param'):
                param = document.resolve(param_ref)
                if param.findtext('Name') == name:
                    return param
    return None


def _static_audio_value(param, default):
    if param is None:
        return default
    raw = param.findtext('CurrentValue')
    if raw is None:
        start = param.findtext('StartKeyframe')
        raw = start.split(',')[1] if start and len(start.split(',')) > 1 else None
    return float(raw) if raw is not None else default


def _set_static_audio_value(param, value):
    if param is None:
        raise ValueError('Premiere audio track has no editable mixer parameter')
    if param.find('Keyframes') is not None:
        raise ValueError('Keyframed Premiere track mixer automation is unsupported')
    rendered = format(value, '.9g')
    start = param.find('StartKeyframe')
    keyframe = f'-91445760000000000,{rendered},0,0,0,0,0,0'
    if start is None:
        insert_leaf_before(param, 'Name', 'StartKeyframe', keyframe)
    else:
        start.text = keyframe
    current = param.find('CurrentValue')
    if current is None:
        insert_leaf_before(param, 'Name', 'CurrentValue', rendered)
    else:
        current.text = rendered


def _caption_color(value):
    if not isinstance(value, str) or len(value) != 7 or not value.startswith('#'):
        raise ValueError('Caption fill must be a #RRGGBB color')
    try:
        return Color(*(int(value[index:index + 2], 16) for index in (1, 3, 5)))
    except ValueError as exc:
        raise ValueError('Caption fill must be a #RRGGBB color') from exc


def _caption_time(value):
    millis = round(value * 1000)
    hours, rest = divmod(millis, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, milli = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{milli:03}'


def _retime_caption(caption, start, duration):
    sequence = caption.track.sequence
    timebase = sequence.timebase
    ticks = py_premiere.TICKS_PER_SECOND
    first = round(start * ticks / timebase) * timebase
    last = round((start + duration) * ticks / timebase) * timebase
    if first < 0 or last <= first:
        raise ValueError('Caption timing must span at least one frame')
    placement = caption._element.find('DataClipTrackItem/ClipTrackItem/TrackItem')
    start_node = placement.find('Start')
    if first == 0 and start_node is not None:
        remove_child(placement, start_node)
    elif first and start_node is None:
        insert_leaf_before(placement, 'End', 'Start', str(first))
    elif start_node is not None:
        start_node.text = str(first)
    placement.find('End').text = str(last)
    document = sequence.project._document
    subclip = document.resolve(caption._element.find('DataClipTrackItem/ClipTrackItem/SubClip'))
    source_clip = document.resolve(subclip.find('Clip'))
    core = source_clip.find('DataClip/Clip')
    base = (3600 * ticks // timebase) * timebase
    core.find('InPoint').text = str(base + first)
    core.find('OutPoint').text = str(base + last)


def _set_clip_speed(clip, speed):
    if not math.isfinite(speed) or not .1 <= speed <= 8:
        raise ValueError('Premiere clip speed must be 0.1x to 8x')
    core = clip._clip_element.find('Clip')
    element = core.find('PlaybackSpeed')
    if abs(speed - 1) < .000001:
        if element is not None:
            remove_child(core, element)
    elif element is None:
        insert_leaf_before(core, 'OutPoint', 'PlaybackSpeed', format(speed, '.9g'))
    else:
        element.text = format(speed, '.9g')


def _set_clip_reverse(clip, reverse):
    if reverse == clip.is_speed_reversed:
        return
    core = clip._clip_element.find('Clip')
    backwards = core.find('PlayBackwards')
    if reverse and backwards is None:
        insert_leaf_before(core, 'OutPoint', 'PlayBackwards', 'true')
    elif reverse:
        backwards.text = 'true'
    else:
        remove_child(core, backwards)


def _read_transition(track, item, clip_data):
    matching = next((transition for transition in track.transitions
                     if abs(transition.cut_point.seconds - item.start.seconds) < .05), None)
    if matching is None:
        return
    if clip_data['type'] == 'audio':
        clip_data['transition'] = 'dissolve' if matching.match_name in AUDIO_CROSSFADE_NAMES else 'native'
    else:
        clip_data['transition'] = TRANSITION_BY_MATCH.get(matching.match_name, 'native')
    clip_data['transitionBefore'] = matching.cut_point_offset.seconds
    clip_data['transitionDuration'] = matching.duration.seconds
    clip_data['transitionMatchName'] = matching.match_name


# MZ.Sequence.VideoTimeDisplayFormat values for the NTSC rates, drop-frame and not
# (py-premiere's TIMECODE_FORMATS, as Adobe's projects store them).
DROP_FRAME_DISPLAY = {8475667200: (102, 103), 4237833600: (106, 107)}


def _sequence_timecode(sequence):
    """Splice's startTimecode (frames) and dropFrame from the sequence's zero point and display format."""
    result = {}
    if sequence.timebase:
        frames = round(sequence.zero_point.ticks / sequence.timebase)
        if frames > 0:
            result['startTimecode'] = frames
    formats = DROP_FRAME_DISPLAY.get(sequence.timebase)
    display = sequence.video_display_format
    if formats and display in formats:
        result['dropFrame'] = display == formats[0]
    return result


def _write_sequence_timecode(sequence, data):
    """Store Splice's start timecode and drop-frame choice in the sequence (zero point, display format)."""
    start = data.get('startTimecode') or 0
    if isinstance(start, str):
        # A start written as text by an earlier build ("01:00:00:00"): read it as a label.
        from fractions import Fraction
        from interchange.timecode import DROP_FRAME_RATES, timecode_frames
        rate = Fraction(data.get('fps') or 25).limit_denominator(1001)
        drop = rate in DROP_FRAME_RATES and (';' in start if data.get('dropFrame') is None else data.get('dropFrame') is True)
        try:
            start = timecode_frames(start.replace(';', ':'), rate, drop)
        except ValueError:
            raise ValueError('Invalid sequence start timecode') from None
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        raise ValueError('Invalid sequence start timecode')
    if sequence.zero_point.ticks != start * sequence.timebase:
        sequence.zero_point = py_premiere.Time(start * sequence.timebase)
    formats = DROP_FRAME_DISPLAY.get(sequence.timebase)
    if formats:
        # Splice shows drop-frame at these rates unless the project turns it off.
        wanted = formats[0] if data.get('dropFrame') is not False else formats[1]
        if sequence.video_display_format != wanted:
            sequence.video_display_format = wanted


def open_project(data, index=0, _seen=()):
    _check_size(data)
    if index in _seen:
        raise ValueError('Premiere sequence nesting cycle is unsupported')
    if len(_seen) >= 4:
        raise ValueError('Premiere sequence nesting exceeds four levels')
    with tempfile.TemporaryDirectory(prefix='splice-open-') as directory:
        source = Path(directory) / 'source.prproj'
        source.write_bytes(data)
        project = py_premiere.parse(source).project
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
                    'media': media, 'clips': [], 'captions': [], 'markers': [], 'duration': 0,
                    'trackState': {}, 'trackCounts': {'video': 3, 'audio': 3},
                    'premiereOriginal': base64.b64encode(data).decode('ascii'), 'premiereManaged': [],
                    'premiereSequences': [], 'premiereSequence': 0}
        sequence = project.sequences[index]
        media = []
        clips = []
        managed = []
        identities = {}
        nested_identities = {}
        native_identities = {}
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
                                              'type': track_type, 'nestedSequence': nested,
                                              'premiereItemUID': source_item._element.get('ObjectUID') if source_item is not None else None})
                            identity = nested_identities[identity_key]
                            source_media = next(m for m in media if m['id'] == identity)
                            source_media['duration'] = max(source_media['duration'], item.in_point.seconds + item.duration.seconds)
                            managed.append([track_type, track_number, clip_index])
                            clips.append({'id': f'c{len(clips) + 1}', 'mediaId': identity,
                                          'start': item.start.seconds, 'duration': item.duration.seconds,
                                          'in': item.in_point.seconds, 'track': (-(track_number + 1) if track_type == 'audio' else track_number + 1),
                                          'type': track_type, 'nestedSequence': nested,
                                          'speed': item.speed, 'reverse': item.is_speed_reversed,
                                          'nativeTimeRemap': item._clip_element.find('Clip/TimeRemapping') is not None,
                                          'premiereTrack': track_number, 'premiereIndex': clip_index,
                                          'title': '', 'color': {}, 'volume': 100,
                                          'transition': 'none'})
                            _read_transition(track, item, clips[-1])
                            continue
                        uid = source_item._element.get('ObjectUID') if source_item is not None else None
                        identity_key = (uid or f'{track_type}-{track_number}-{clip_index}', track_type)
                        if identity_key not in native_identities:
                            identity = f'm{len(media) + 1}'
                            native_identities[identity_key] = identity
                            media.append({'id': identity, 'name': source_item.name if source_item is not None else item.name,
                                          'src': '', 'duration': 0, 'thumb': '', 'type': track_type,
                                          'premiereItemUID': uid,
                                          'nativeGenerator': source_item.generator_type if source_item is not None else None,
                                          'nativeUnsupported': source_item is None or source_item.generator_type is None})
                        identity = native_identities[identity_key]
                        source_media = next(m for m in media if m['id'] == identity)
                        source_media['duration'] = max(source_media['duration'], item.in_point.seconds + item.duration.seconds)
                        managed.append([track_type, track_number, clip_index])
                        clips.append({'id': f'c{len(clips) + 1}', 'mediaId': identity,
                                      'start': item.start.seconds, 'duration': item.duration.seconds,
                                      'in': item.in_point.seconds,
                                      'track': (-(track_number + 1) if track_type == 'audio' else track_number + 1),
                                      'type': track_type, 'speed': item.speed, 'reverse': item.is_speed_reversed,
                                      'nativeTimeRemap': item._clip_element.find('Clip/TimeRemapping') is not None,
                                      'nativeUnsupported': None if source_item is not None and source_item.generator_type is not None else 'Premiere source type is unavailable for preview and export',
                                      'premiereTrack': track_number, 'premiereIndex': clip_index,
                                      'title': '', 'color': {}, 'volume': 100,
                                      'transition': 'none'})
                        _read_transition(track, item, clips[-1])
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
                                  'type': track_type, 'embeddedAudio': False,
                                  'speed': item.speed, 'reverse': item.is_speed_reversed,
                                  'nativeTimeRemap': item._clip_element.find('Clip/TimeRemapping') is not None,
                                  'premiereTrack': track_number, 'premiereIndex': clip_index, 'title': '', 'color': color,
                                  'volume': volume, 'transition': 'none', **premiere_effects.read_extended(item)})
                    _read_transition(track, item, clips[-1])
        native_clip_ids = {}
        for clip_data in clips:
            kind = 'audio' if clip_data['type'] == 'audio' else 'video'
            tracks = sequence.audio_tracks if kind == 'audio' else sequence.video_tracks
            number, position = clip_data['premiereTrack'], clip_data['premiereIndex']
            native_clip_ids[tracks[number].clips[position]._element.get('ObjectID')] = clip_data
        for link_ref in sequence._element.findall('PersistentGroupContainer/LinkContainer/Links/Link'):
            link = project._document.resolve(link_ref)
            members = [native_clip_ids.get(ref.get('ObjectRef'))
                       for ref in link.findall('TrackItemGroup/TrackItems/TrackItem')]
            if len([member for member in members if member is not None]) >= 2:
                for member in members:
                    if member is not None:
                        member['linkGroup'] = f"premiere-link-{link.get('ObjectID')}"
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
                    elif path is None and source_item.generator_type is not None:
                        uid = source_item._element.get('ObjectUID')
                        identity_key = (uid, 'video')
                        if identity_key not in native_identities:
                            identity = f'm{len(media) + 1}'
                            native_identities[identity_key] = identity
                            media.append({'id': identity, 'name': source_item.name, 'src': '',
                                          'duration': max(0, source_item.out_point.seconds) if source_item.out_point else 5,
                                          'thumb': '', 'type': 'video', 'bin': _bin_path(source_item),
                                          'premiereItemUID': uid,
                                          'nativeGenerator': source_item.generator_type})
                    include_unused(source_item)
            include_unused(project.root_item)
        for item in media:
            if item.get('path') and Path(item['path']).suffix.lower() in STILL_IMAGES:
                item['still'] = True
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
        timecode = _sequence_timecode(sequence)
        track_mix = {}
        track_state = {}
        for kind, tracks in (('video', sequence.video_tracks), ('audio', sequence.audio_tracks)):
            for number, track in enumerate(tracks):
                key = f'{kind}:{number + 1 if kind == "video" else -number - 1}'
                # Premiere's track output toggle on a video track is its mute flag; Splice calls it hidden.
                hidden = kind == 'video' and track.is_muted
                if track.is_locked or not track.is_sync_locked or hidden:
                    track_state[key] = {'locked': track.is_locked, 'syncLocked': track.is_sync_locked}
                    if hidden:
                        track_state[key]['hidden'] = True
                if kind == 'video' and number == 3 and key in track_state:
                    track_state['title:4'] = dict(track_state[key])
        for number, track in enumerate(sequence.audio_tracks):
            volume = _static_audio_value(_audio_track_param(track, 'Volume'), 1)
            balance = _static_audio_value(_audio_track_param(track, 'Balance'), .5)
            state = {'gain': round(volume * 100, 4), 'pan': round((balance - .5) * 200, 4),
                     'mute': track.is_muted, 'solo': False}
            if state != {'gain': 100, 'pan': 0, 'mute': False, 'solo': False}:
                track_mix[str(-number - 1)] = state
        captions = []
        for track_index, track in enumerate(sequence.caption_tracks):
            for caption_index, caption in enumerate(track.captions):
                fill = caption.fill_color
                captions.append({'id': f'caption-{track_index}-{caption_index}',
                                 'start': caption.start.seconds,
                                 'duration': (caption.end - caption.start).seconds,
                                 'text': caption.text, 'track': track_index,
                                 'fontFamily': caption.font_family,
                                 'fontSize': caption.font_size,
                                 'fillColor': '#%02x%02x%02x' % fill[:3] if fill else None,
                                 'premiereCaptionTrack': track_index,
                                 'premiereCaptionIndex': caption_index})
        warnings = []
        if sequence._element.findtext('Node/Properties/AMM.CurrentSolo') not in (None, '', '[]'):
            warnings.append('Native Premiere track solo is preserved but cannot be edited in Splice.')
        for clip in clips:
            source_media = next((media_item for media_item in media if media_item['id'] == clip.get('mediaId')), None)
            if clip.get('nativeVolumeOutsideTrim'):
                label = (source_media or {}).get('name', 'Audio clip')
                warnings.append(f"{label} at {clip['start']:.2f}s: Premiere volume keys outside the source trim are preserved; preview and export use the clip's static volume.")
            if source_media is not None and source_media.get('nativeGenerator') is not None:
                warnings.append(f"{source_media['name']} is a native Premiere generator; preview and export are unavailable.")
        return {'name': sequence.name, 'width': width, 'height': height, 'fps': fps, **timecode,
                'premiereSequences': [item.name for item in project.sequences], 'premiereSequence': index,
                'media': media, 'clips': clips, 'captions': captions,
                'trackState': track_state,
                'trackCounts': {'video': len(sequence.video_tracks), 'audio': len(sequence.audio_tracks)},
                'compatibilityWarnings': warnings,
                'duration': max((c['start'] + c['duration'] for c in clips), default=0),
                'premiereOriginal': base64.b64encode(data).decode('ascii'), 'premiereManaged': managed,
                'trackMix': track_mix,
                'markers': [{'id': marker.guid, 'name': marker.name, 'time': marker.start.seconds} for marker in sequence.markers]}


def _merged_track_states(states):
    """Fold title-lane states into their video track: Premiere has one track for both.

    Locked or hidden if either lane was; sync-locked only if both were.
    """
    merged = {}
    for key, state in states.items():
        if not isinstance(key, str) or not isinstance(state, dict):
            raise ValueError('Invalid Premiere track state target')
        target = 'video:' + key[6:] if key.startswith('title:') else key
        current = merged.get(target)
        if current is None:
            merged[target] = dict(state)
            continue
        merged[target] = {
            'locked': bool(current.get('locked')) or bool(state.get('locked')),
            'syncLocked': current.get('syncLocked', True) is not False and state.get('syncLocked', True) is not False,
            'hidden': bool(current.get('hidden')) or bool(state.get('hidden')),
        }
    return merged


def save_project(data):
    if not isinstance(data, dict):
        raise ValueError('Invalid project')
    lanes = {}
    for item in data.get('clips', []):
        lane = ('audio' if item.get('type') == 'audio' else 'video', int(item.get('track', 1)))
        lanes.setdefault(lane, []).append(item)
    for lane, clips in lanes.items():
        ordered = sorted(clips, key=lambda item: float(item['start']))
        for previous, current in zip(ordered, ordered[1:]):
            if float(current['start']) < float(previous['start']) + float(previous['duration']) - .001:
                raise ValueError(f'Premiere track {lane[1]} contains overlapping clips')
    for clip in data.get('clips', []):
        if (clip.get('color') or {}).get('lut'):
            raise ValueError('Premiere export cannot write imported LUTs; save a Splice project or remove the LUT')
    if any(mix.get('solo', False) for mix in (data.get('trackMix') or {}).values()):
        raise ValueError('Premiere export cannot write track solo; save a Splice project or disable solo')
    original = data.get('premiereOriginal')
    if original and len(original) > 70 * 1024 * 1024:
        raise ValueError('Original Premiere project is too large')
    if original:
        raw = base64.b64decode(original, validate=True)
        _check_size(raw)
        with tempfile.TemporaryDirectory(prefix='splice-handoff-') as directory:
            source = Path(directory) / 'source.prproj'
            source.write_bytes(raw)
            app = py_premiere.parse(source)
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
        highest_video = max(4, max((int(clip['track']) for clip in data['clips'] if clip.get('type') in ('video','title')), default=4))
        if highest_video > 64:
            raise ValueError('Unsupported video track number')
        custom = base._replace(frame_rate=chosen[0], width=width, height=height, time_display=chosen[1], video_tracks=highest_video)
        sequence = project.add_sequence(str(data.get('name') or 'Untitled')[:120], sequence_format=custom)
    _write_sequence_timecode(sequence, data)
    track_states = _merged_track_states(data.get('trackState') or {})
    for key, state in track_states.items():
        try:
            kind, raw_track = key.split(':', 1)
            number = int(raw_track)
        except (ValueError, AttributeError) as exc:
            raise ValueError('Invalid Premiere track state target') from exc
        tracks = sequence.audio_tracks if kind == 'audio' else sequence.video_tracks if kind == 'video' else None
        if tracks is None:
            raise ValueError('Invalid Premiere track state target')
        index = -number - 1 if kind == 'audio' else number - 1
        if not 0 <= index < len(tracks):
            raise ValueError('Premiere track state target is unavailable')
        track = tracks[index]
        if bool(state.get('locked', False)) != track.is_locked:
            track.is_locked = bool(state['locked'])
        if bool(state.get('syncLocked', True)) != track.is_sync_locked:
            track.is_sync_locked = bool(state['syncLocked'])
        if kind != 'audio' and bool(state.get('hidden', False)) != track.is_muted:
            track.is_muted = bool(state.get('hidden', False))
    camera_mix = {}
    for key, mix in (data.get('trackMix') or {}).items():
        number = int(key)
        if not number or number > len(sequence.video_tracks) or -number > len(sequence.audio_tracks):
            raise ValueError('Premiere audio track mixer target is unavailable')
        gain = float(mix.get('gain', 100))
        pan = float(mix.get('pan', 0))
        if not math.isfinite(gain) or not 0 <= gain <= 200 or not math.isfinite(pan) or not -100 <= pan <= 100:
            raise ValueError('Premiere audio mixer value is out of range')
        if number > 0:
            if pan:
                raise ValueError('Premiere export cannot preserve embedded camera track pan. Save a Splice working copy to keep it.')
            camera_mix[number] = 0 if mix.get('mute') else gain / 100
            continue
        track = sequence.audio_tracks[-number - 1]
        current_gain = _static_audio_value(_audio_track_param(track, 'Volume'), 1) * 100
        current_pan = (_static_audio_value(_audio_track_param(track, 'Balance'), .5) - .5) * 200
        if abs(gain - current_gain) > .0001:
            _set_static_audio_value(_audio_track_param(track, 'Volume'), gain / 100)
        if abs(pan - current_pan) > .0001:
            _set_static_audio_value(_audio_track_param(track, 'Balance'), (pan + 100) / 200)
        if bool(mix.get('mute', False)) != track.is_muted:
            track.is_muted = bool(mix['mute'])
    native_captions = {(track_index, caption_index): caption
                       for track_index, track in enumerate(sequence.caption_tracks)
                       for caption_index, caption in enumerate(track.captions)}
    incoming_captions = data.get('captions', [])
    retained_captions = set()
    new_captions = []
    for item in incoming_captions:
        if item.get('premiereCaptionIndex') is None:
            new_captions.append(item)
            continue
        key = (item.get('premiereCaptionTrack'), item.get('premiereCaptionIndex'))
        caption = native_captions.get(key)
        if caption is None or key in retained_captions:
            raise ValueError('Native Premiere caption reference is unavailable')
        retained_captions.add(key)
        start = float(item.get('start', 0))
        duration = float(item.get('duration', 0))
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
            raise ValueError('Caption timing is invalid')
        if abs(start - caption.start.seconds) > .0001 or abs(duration - (caption.end - caption.start).seconds) > .0001:
            _retime_caption(caption, start, duration)
        if item.get('text') != caption.text:
            caption.text = str(item.get('text') or '')
        if item.get('fontFamily') and item['fontFamily'] != caption.font_family:
            caption.font_family = str(item['fontFamily'])
        if item.get('fontSize') is not None and abs(float(item['fontSize']) - caption.font_size) > .0001:
            caption.font_size = float(item['fontSize'])
        if item.get('fillColor'):
            color = _caption_color(item['fillColor'])
            if caption.fill_color != color:
                caption.fill_color = color
    for key, caption in native_captions.items():
        if key in retained_captions:
            continue
        items = caption.track._element.find('DataClipTrack/ClipTrack/ClipItems/TrackItems')
        reference = next((node for node in items if node.get('ObjectRef') == caption._element.get('ObjectID')), None)
        if reference is None:
            raise ValueError('Native Premiere caption track item is unavailable')
        remove_child(items, reference)
        for number, node in enumerate(items):
            node.set('Index', str(number))
    if new_captions:
        lines = []
        for number, item in enumerate(sorted(new_captions, key=lambda value: float(value.get('start', 0))), 1):
            start = float(item.get('start', 0))
            duration = float(item.get('duration', 0))
            caption_text = str(item.get('text') or '').strip()
            if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0 or not caption_text:
                raise ValueError('New caption needs text and valid timing')
            lines.extend([str(number), f'{_caption_time(start)} --> {_caption_time(start + duration)}', caption_text, ''])
        content = '\n'.join(lines) + '\n'
        folder = MEDIA_ROOT / 'captions'
        folder.mkdir(parents=True, exist_ok=True)
        source_path = folder / (hashlib.sha256(content.encode('utf-8')).hexdigest() + '.srt')
        if not source_path.exists():
            source_path.write_text(content, encoding='utf-8')
        imported = project.import_files([source_path])[0]
        caption_track = sequence.create_caption_track(imported)
        for item, caption in zip(sorted(new_captions, key=lambda value: float(value.get('start', 0))), caption_track.captions):
            if item.get('fontFamily') and item['fontFamily'] != caption.font_family:
                caption.font_family = str(item['fontFamily'])
            if item.get('fontSize') is not None and abs(float(item['fontSize']) - caption.font_size) > .0001:
                caption.font_size = float(item['fontSize'])
            if item.get('fillColor'):
                caption.fill_color = _caption_color(item['fillColor'])
    existing_markers = {marker.guid: marker for marker in sequence.markers}
    if 'markers' in data:
        retained_markers = set()
        for marker_data in data['markers']:
            identity = marker_data.get('id')
            marker = existing_markers.get(identity)
            name = str(marker_data.get('name', 'Marker'))
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
    intact_links = set()
    if original:
        for kind, tracks in (('video', sequence.video_tracks), ('audio', sequence.audio_tracks)):
            for track_index, track in enumerate(tracks):
                for clip_index, clip in enumerate(track.clips):
                    original_clips[(kind, track_index, clip_index)] = clip
        original_by_object = {clip._element.get('ObjectID'): key for key, clip in original_clips.items()}
        incoming_by_key = {(('audio' if item.get('type') == 'audio' else 'video'),
                            item.get('premiereTrack'), item.get('premiereIndex')): item
                           for item in data.get('clips', []) if 'premiereIndex' in item}
        links = sequence._element.find('PersistentGroupContainer/LinkContainer/Links')
        for link_ref in list(links) if links is not None else []:
            link = project._document.resolve(link_ref)
            keys = [original_by_object.get(ref.get('ObjectRef'))
                    for ref in link.findall('TrackItemGroup/TrackItems/TrackItem')]
            if any(key is None for key in keys):
                continue
            present = [incoming_by_key.get(key) for key in keys]
            group = present[0].get('linkGroup') if all(present) else None
            members = [item for item in data.get('clips', []) if group and item.get('linkGroup') == group]
            # Keep the native link only while Splice still links exactly these clips. Linked clips may
            # drift out of sync (J- and L-cuts), as they can in Premiere; an unlink or a partial delete
            # removes the link, and any regrouped clips are linked again after placement.
            if group and len(members) == len(present) and all(item.get('linkGroup') == group for item in present):
                intact_links.add(group)
                continue
            remove_child(links, link_ref)
            project._document.remove_object(link)
    available = {}
    existing = {}
    existing_uid = {}
    if original and project.root_item:
        def collect(folder):
            for child in folder.children:
                if child.media_path:
                    existing[str(child.media_path)] = child
                if child._element.get('ObjectUID'):
                    existing_uid[child._element.get('ObjectUID')] = child
                collect(child)
        collect(project.root_item)
    bin_cache = {}
    for item in data.get('media', []):
        src = item.get('src', '')
        if original and item.get('premiereItemUID') in existing_uid:
            available[item['id']] = existing_uid[item['premiereItemUID']]
            continue
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
    media_by_id = {item.get('id'): item for item in data.get('media',[])}
    generated_audio = {}

    def source_audio(clip):
        identity = clip.get('mediaId')
        item = media_by_id.get(identity,{})
        stream = clip.get('audioStream',item.get('audioStream'))
        if stream is not None and type(stream) is not int:
            raise ValueError('The selected source audio stream is unavailable')
        if stream is None and (not isinstance(item.get('audioStreams'),int) or item['audioStreams']<=1):
            return available.get(identity)
        key = (identity,stream)
        if key not in generated_audio:
            from media_proxy import premiere_audio
            handoff = premiere_audio(item.get('src',''),stream)
            generated_audio[key] = project.import_files([handoff])[0]
        return generated_audio[key]

    def place_linked(picture_clip, position, video_track, audio_track, sound_clip=None):
        video_source = available[picture_clip['mediaId']]
        audio_source = source_audio(sound_clip if sound_clip is not None else picture_clip)
        if audio_source is video_source:
            return sequence.add_linked_clip(video_source,position,video_track,audio_track)
        picture = sequence.video_tracks[video_track].add_clip(video_source,position)
        sound = sequence.audio_tracks[audio_track].add_clip(audio_source,position)
        sequence._link([picture,sound])
        return picture,sound

    placed_clips = {}
    # A new, in-sync picture/sound pair is placed the way Premiere places a linked clip.
    # Other new link groups (J- and L-cuts, merged clips) are placed clip by clip and linked after.
    group_members = {}
    for item in data.get('clips', []):
        if item.get('linkGroup'):
            group_members.setdefault(item['linkGroup'], []).append(item)
    linked_pairs = {}
    for group, members in group_members.items():
        video = next((item for item in members if item.get('type') == 'video'), None)
        audio = next((item for item in members if item.get('type') == 'audio'), None)
        if (len(members) == 2 and video and audio and not any('premiereIndex' in item for item in members) and
                all(abs(float(video[key]) - float(audio[key])) <= .001 for key in ('start', 'duration', 'in'))):
            video_track, audio_track = int(video['track']) - 1, -int(audio['track']) - 1
            if not 0 <= video_track < len(sequence.video_tracks) or not 0 <= audio_track < len(sequence.audio_tracks):
                raise ValueError('Native linked placement track is unavailable')
            if available.get(video.get('mediaId')) is None or available.get(audio.get('mediaId')) is None:
                raise ValueError('Relink both sources before saving linked audio and video to Premiere')
            linked_pairs[group] = (video, audio)
    linked_placements = {}
    # Put linked camera sound on a free audio track alongside the explicit mix.
    audio_regions = [[] for _ in sequence.audio_tracks]
    for region in data.get('clips', []):
        if region['type'] == 'audio':
            track = -int(region.get('track', -1)) - 1
            if not 0 <= track < len(audio_regions):
                raise ValueError('Unsupported audio track')
            audio_regions[track].append((region['start'], region['start'] + region['duration']))
    pending_transitions = []
    camera_audio = {}
    items = data.get('clips', [])
    video_ends = {(item['track'], round((item['start'] + item['duration']) * requested_fps)): item
                  for item in items if item.get('type') == 'video'}
    for item in sorted(items, key=lambda clip: ('premiereIndex' not in clip, clip['start'])):
        companion = None
        source = available.get(item.get('mediaId'))
        is_audio = item.get('type') == 'audio'
        tracks = sequence.audio_tracks if is_audio else sequence.video_tracks
        ui_track = int(item.get('track', -1 if is_audio else 1))
        index = -ui_track - 1 if is_audio else ui_track - 1
        if index < 0 or index >= len(tracks):
            raise ValueError('Unsupported track number')
        pair = linked_pairs.get(item.get('linkGroup'))
        if pair and item.get('id') not in linked_placements:
            video, audio = pair
            linked_placements[video['id']], linked_placements[audio['id']] = place_linked(
                video, py_premiere.Time.from_seconds(float(video['start'])),
                int(video['track']) - 1, -int(audio['track']) - 1, audio)
        if item.get('id') in linked_placements:
            clip = linked_placements[item['id']]
        elif original and 'premiereIndex' in item:
            original_track = int(item.get('premiereTrack', index))
            if original_track >= len(tracks):
                raise ValueError('Premiere track is unavailable')
            source_index = int(item['premiereIndex'])
            clip = original_clips.get(('audio' if is_audio else 'video', original_track, source_index))
            if clip is None:
                raise ValueError('Original Premiere clip is unavailable')
            if index != original_track:
                for transition in list(clip.track.transitions):
                    if abs(transition.cut_point.seconds - clip.start.seconds) < .05 or abs(transition.cut_point.seconds - clip.end.seconds) < .05:
                        clip.track.remove_transition(transition)
                clip.track.move_clip(clip, tracks[index])
            original_start = clip.start.seconds
            if item.get('transition') == 'none' or abs(float(item.get('start', 0)) - original_start) > 0.001:
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
                # The Adobe fixture used to author a new graphic defaults to
                # LucidaConsole, which is absent on Linux. Write the bundled
                # face only for new Splice titles; retain native title fonts.
                item = {**item, 'titleStyle': {
                    'fontFamily': 'DM Sans', 'x': 50, 'y': 78, 'fontSize': 8,
                    'strokeWidth': 0, 'align': 'center', **(item.get('titleStyle') or {}),
                }}
            else:
                if source is None:
                    raise ValueError('A clip has media that must be relinked before saving to Premiere')
                placement = py_premiere.Time.from_seconds(float(item['start']))
                has_audio = any(element.tag == 'AudioClip' for element in source._clip_elements)
                if not is_audio and has_audio and item.get('embeddedAudio', True):
                    begin, end = item['start'], item['start'] + item['duration']
                    previous = video_ends.get((ui_track, round(begin * requested_fps)))
                    paired = camera_audio.get(id(previous)) if item.get('transition') in ('dissolve', 'wipe') else None
                    candidates = [paired[1]] if paired else range(len(audio_regions))
                    free = next((i for i in candidates if all(a >= end or b <= begin for a, b in audio_regions[i])), None)
                    if free is None:
                        if paired:
                            raise ValueError('Linked camera sound needs one free audio track across the crossfade')
                        raise ValueError('No free audio track for linked camera sound')
                    clip, companion = place_linked(item, placement, index, free)
                    audio_regions[free].append((begin, end))
                    camera_audio[id(item)] = (companion, free)
                    if paired or (previous is None and item.get('transition') in ('dissolve','wipe','fade')):
                        pending_transitions.append((companion, item, AUDIO_CROSSFADE, AUDIO_CROSSFADE))
                else:
                    clip = tracks[index].add_clip(source_audio(item) if is_audio else source, placement)
        speed = float(item.get('speed', clip.speed))
        if item.get('type') != 'title' and abs(speed - clip.speed) > .000001:
            if clip._clip_element.find('Clip/TimeRemapping') is not None:
                raise ValueError('Changing speed on a keyframed Premiere time remap is unsupported')
            _set_clip_speed(clip, speed)
        reverse = bool(item.get('reverse',clip.is_speed_reversed))
        if item.get('type') != 'title':
            _set_clip_reverse(clip,reverse)
        start = max(0, float(item.get('in', 0)))
        duration = max(0.001, float(item.get('duration', 1)))
        # A retimed Premiere clip can have a different source span than its
        # timeline duration. Leave its original out point alone on unrelated edits.
        source_changed = abs(clip.in_point.seconds - start) > 1e-9
        length_changed = abs(clip.duration.seconds - duration) > 1e-9
        if source_changed:
            clip.in_point = py_premiere.Time.from_seconds(start)
        if source_changed or length_changed or 'premiereIndex' not in item:
            clip.out_point = py_premiere.Time.from_seconds(start + duration * speed)
            clip.end = py_premiere.Time.from_seconds(float(item.get('start', 0)) + duration)
        if companion is not None:
            if abs(speed-companion.speed)>.000001:
                _set_clip_speed(companion,speed)
            _set_clip_reverse(companion,reverse)
            companion.in_point = clip.in_point
            companion.out_point = clip.out_point
            companion.end = clip.end
            audio_item = {key: item[key] for key in ('volume','fadeIn','fadeOut','pan') if key in item}
            mix_gain = camera_mix.get(ui_track,1)
            audio_item['volume'] = float(item.get('volume',100))*mix_gain
            if (item.get('keyframes') or {}).get('volume'):
                audio_item['keyframes'] = {'volume': [{**point,'value':point['value']*mix_gain}
                                                      for point in item['keyframes']['volume']]}
            if audio_item['volume']>200:
                raise ValueError('Premiere export cannot combine this camera clip and track gain above 200%.')
            premiere_effects.apply(companion, {}, audio_item['volume'])
            premiere_effects.apply_extended(companion, audio_item)
            item = {**item, 'fadeIn': 0, 'fadeOut': 0, 'pan': 0,
                    'keyframes': {key: points for key, points in (item.get('keyframes') or {}).items() if key != 'volume'}}
        placed_clips[item.get('id')] = clip
        premiere_effects.apply(clip, item.get('color') or {}, float(item.get('volume', 100)))
        premiere_effects.apply_extended(clip, item)
        if item.get('transition') in TRANSITIONS and not is_audio:
            pending_transitions.append((clip, item, *TRANSITIONS[item['transition']]))
        elif is_audio and item.get('transition') == 'dissolve':
            match = item.get('transitionMatchName')
            if match not in AUDIO_CROSSFADE_NAMES:
                match = AUDIO_CROSSFADE
            pending_transitions.append((clip, item, match, match))
    # Link every Splice link group that is not an untouched native link, including groups
    # whose picture and sound are out of sync.
    link_groups = {}
    for item in data.get('clips', []):
        if item.get('linkGroup') and item.get('id') in placed_clips:
            link_groups.setdefault(item['linkGroup'], []).append(placed_clips[item['id']])
    for group, members in link_groups.items():
        if group not in intact_links and group not in linked_pairs and len(members) >= 2:
            sequence._link(members)
    if original:
        retained = {(('video' if c.get('type') == 'title' else c.get('type')), c.get('premiereTrack'), c.get('premiereIndex')) for c in data.get('clips', []) if 'premiereIndex' in c}
        for key, clip in original_clips.items():
            if list(key) in data.get('premiereManaged', []) and key not in retained:
                for transition in list(clip.track.transitions):
                    if abs(transition.cut_point.seconds - clip.start.seconds) < 0.05 or abs(transition.cut_point.seconds - clip.end.seconds) < 0.05:
                        clip.track.remove_transition(transition)
                clip.track.remove_clip(clip)
    for clip, item, match, label in pending_transitions:
        length = py_premiere.Time.from_seconds(float(item.get('transitionDuration', .5)))
        before = py_premiere.Time.from_seconds(float(item.get('transitionBefore', 0)))
        existing = next((t for t in clip.track.transitions if abs(t.cut_point.seconds-clip.start.seconds)<.5/requested_fps), None)
        if existing:
            if (existing.match_name == match and abs(existing.duration.seconds-length.seconds)<.5/requested_fps and
                    abs(existing.cut_point_offset.seconds-before.seconds)<.5/requested_fps):
                continue
            clip.track.remove_transition(existing)
        clip.track.add_transition(clip, match, at_start=True, name=label, duration=length,cut_offset=before)
    with tempfile.TemporaryDirectory() as temp:
        destination = Path(temp) / 'project.prproj'
        project.save(destination)
        return destination.read_bytes()
