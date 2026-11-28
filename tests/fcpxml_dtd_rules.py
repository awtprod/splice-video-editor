"""Structural rules of the FCPXML 1.10 DTD for the elements Splice writes, checked without lxml.

The content models and attribute lists below are transcribed from Apple's FCPXML 1.10 DTD (the
file itself is not part of this repository). Only elements Splice emits are listed; any other
element in an export is itself an error. :func:`problems` returns a list of human-readable
violations: an undeclared attribute, a value outside an enumeration, a missing required
attribute, or children out of the declared order.
"""
import re

STORY = 'audio | video | clip | title | caption | mc-clip | ref-clip | sync-clip | asset-clip | audition | spine'
MARKERS = '(marker | chapter-marker | rating | keyword | analysis-marker)*'
VIDEO_ADJUST = ('object-tracker?, adjust-crop?, adjust-corners?, adjust-conform?, adjust-transform?, adjust-blend?, '
                'adjust-stabilization?, adjust-rollingShutter?, adjust-360-transform?, adjust-reorient?, '
                'adjust-orientation?, adjust-cinematic?')

#: element -> content model in DTD syntax ('EMPTY', '#PCDATA' mixes allowed)
CONTENT = {
    'fcpxml': f'(import-options?, resources?, (library | event* | (clip | audition | mc-clip | ref-clip | sync-clip | asset-clip | collection-folder | keyword-collection | smart-collection | project)*))',
    'resources': '(asset | effect | format | media | locator)*',
    'format': 'EMPTY',
    'effect': 'EMPTY',
    'asset': '(media-rep+, metadata?)',
    'media-rep': '(bookmark?)',
    'metadata': '(md*)',
    'md': '(array?)',
    'library': '(event | smart-collection)*',
    'event': '(clip | audition | mc-clip | ref-clip | sync-clip | asset-clip | collection-folder | keyword-collection | smart-collection | project)*',
    'project': '(sequence)',
    'sequence': '(note?, spine, metadata?)',
    'spine': '(audio | video | clip | title | mc-clip | ref-clip | sync-clip | asset-clip | audition | gap | transition)*',
    'gap': f'(note?, ({STORY})*, {MARKERS}, metadata?)',
    'asset-clip': (f'(note?, (conform-rate?, timeMap?), (({VIDEO_ADJUST}), (adjust-volume?, adjust-panner?)), ({STORY})*, '
                   f'{MARKERS}, audio-channel-source*, (filter-video | filter-video-mask)*, filter-audio*, metadata?)'),
    'title': (f'(param*, text*, text-style-def*, note?, ({VIDEO_ADJUST}), ({STORY})*, {MARKERS}, '
              '(filter-video | filter-video-mask)*, metadata?)'),
    'text': '(#PCDATA | text-style)*',
    'text-style': '(#PCDATA | param)*',
    'text-style-def': '(text-style)',
    'transition': f'(filter-video?, filter-audio?, {MARKERS}, metadata?, reserved?)',
    'filter-video': '(data*, param*)',
    'filter-audio': '(data?, param*)',
    'timeMap': '(timept)*',
    'timept': 'EMPTY',
    'adjust-transform': '(param*)',
    'adjust-blend': '(param*, reserved?)',
    'adjust-volume': '(param*)',
    'param': '(fadeIn?, fadeOut?, keyframeAnimation?, param*)',
    'fadeIn': 'EMPTY',
    'fadeOut': 'EMPTY',
    'keyframeAnimation': '(keyframe*)',
    'keyframe': 'EMPTY',
    'marker': 'EMPTY',
}

ANY = None
CLIP_COMMON = {'lane': ANY, 'offset': ANY, 'name': ANY, 'start': ANY, 'duration': ANY, 'enabled': ('0', '1')}
#: element -> {attribute: None (CDATA) or tuple of allowed values}; '!' marks a required attribute
ATTRIBUTES = {
    'fcpxml': {'!version': ('1.10',)},
    'resources': {},
    'format': {'!id': ANY, 'name': ANY, 'frameDuration': ANY, 'fieldOrder': ANY, 'width': ANY, 'height': ANY, 'paspH': ANY,
               'paspV': ANY, 'colorSpace': ANY, 'projection': ANY, 'stereoscopic': ANY},
    'effect': {'!id': ANY, 'name': ANY, '!uid': ANY, 'src': ANY},
    'asset': {'!id': ANY, 'name': ANY, 'uid': ANY, 'start': ANY, 'duration': ANY, 'hasVideo': ANY, 'format': ANY,
              'hasAudio': ANY, 'videoSources': ANY, 'audioSources': ANY, 'audioChannels': ANY, 'audioRate': ANY,
              'customLUTOverride': ANY, 'colorSpaceOverride': ANY, 'projectionOverride': ANY, 'stereoscopicOverride': ANY,
              'auxVideoFlags': ANY},
    'media-rep': {'kind': ('original-media', 'proxy-media'), 'sig': ANY, '!src': ANY, 'suggestedFilename': ANY},
    'metadata': {},
    'md': {'!key': ANY, 'value': ANY, 'editable': ('0', '1'),
           'type': ('string', 'boolean', 'integer', 'float', 'date', 'timecode'), 'displayName': ANY, 'description': ANY,
           'source': ANY},
    'library': {'location': ANY, 'colorProcessing': ('standard', 'wide', 'wide-hdr')},
    'event': {'name': ANY, 'uid': ANY},
    'project': {'name': ANY, 'uid': ANY, 'id': ANY, 'modDate': ANY},
    'sequence': {'!format': ANY, 'duration': ANY, 'tcStart': ANY, 'tcFormat': ('DF', 'NDF'),
                 'audioLayout': ('mono', 'stereo', 'surround'),
                 'audioRate': ('32k', '44.1k', '48k', '88.2k', '96k', '176.4k', '192k'), 'renderFormat': ANY, 'keywords': ANY},
    'spine': {'lane': ANY, 'offset': ANY, 'name': ANY, 'format': ANY},
    'gap': {'name': ANY, 'offset': ANY, 'start': ANY, '!duration': ANY, 'enabled': ('0', '1')},
    'asset-clip': {'!ref': ANY, **CLIP_COMMON, 'srcEnable': ('all', 'audio', 'video'), 'audioStart': ANY, 'audioDuration': ANY,
                   'format': ANY, 'tcStart': ANY, 'tcFormat': ('DF', 'NDF'), 'modDate': ANY, 'audioRole': ANY, 'videoRole': ANY},
    'title': {'!ref': ANY, **CLIP_COMMON, '!duration': ANY, 'role': ANY},
    'text': {'display-style': ('pop-on', 'paint-on', 'roll-up'), 'roll-up-height': ANY, 'position': ANY,
             'placement': ('left', 'right', 'top', 'bottom'), 'alignment': ('left', 'center', 'right')},
    'text-style': {'ref': ANY, 'font': ANY, 'fontSize': ANY, 'fontFace': ANY, 'fontColor': ANY, 'backgroundColor': ANY,
                   'bold': ('0', '1'), 'italic': ('0', '1'), 'strokeColor': ANY, 'strokeWidth': ANY, 'baseline': ANY,
                   'shadowColor': ANY, 'shadowOffset': ANY, 'shadowBlurRadius': ANY, 'kerning': ANY,
                   'alignment': ('left', 'center', 'right', 'justified'), 'lineSpacing': ANY, 'tabStops': ANY,
                   'baselineOffset': ANY, 'underline': ('0', '1')},
    'text-style-def': {'!id': ANY, 'name': ANY},
    'transition': {'name': ANY, 'offset': ANY, '!duration': ANY},
    'filter-video': {'!ref': ANY, 'name': ANY, 'enabled': ('0', '1')},
    'filter-audio': {'!ref': ANY, 'name': ANY, 'enabled': ('0', '1'), 'presetID': ANY},
    'timeMap': {'frameSampling': ('floor', 'nearest-neighbor', 'frame-blending', 'optical-flow-classic', 'optical-flow'),
                'preservesPitch': ('0', '1')},
    'timept': {'!time': ANY, '!value': ANY, 'interp': ('smooth2', 'linear', 'smooth'), 'inTime': ANY, 'outTime': ANY},
    'adjust-transform': {'enabled': ('0', '1'), 'position': ANY, 'scale': ANY, 'rotation': ANY, 'anchor': ANY, 'tracking': ANY},
    'adjust-blend': {'amount': ANY, 'mode': ANY},
    'adjust-volume': {'amount': ANY},
    'param': {'!name': ANY, 'key': ANY, 'value': ANY, 'enabled': ('0', '1')},
    'fadeIn': {'type': ('linear', 'easeIn', 'easeOut', 'easeInOut'), '!duration': ANY},
    'fadeOut': {'type': ('linear', 'easeIn', 'easeOut', 'easeInOut'), '!duration': ANY},
    'keyframeAnimation': {},
    'keyframe': {'!time': ANY, '!value': ANY, 'interp': ('linear', 'ease', 'easeIn', 'easeOut'), 'curve': ('linear', 'smooth')},
    'marker': {'!start': ANY, 'duration': ANY, '!value': ANY, 'completed': ANY, 'note': ANY},
}


def _regex(model: str):
    """A DTD content model as a regex over a string of ``<tag>`` tokens; None for EMPTY."""
    if model == 'EMPTY':
        return re.compile('')
    mixed = '#PCDATA' in model
    model = model.replace('#PCDATA |', '').replace('#PCDATA', '')
    out = re.sub(r'[A-Za-z][\w.-]*', lambda m: f'(?:<{m.group(0)}>)', model)
    out = out.replace(',', '').replace(' ', '').replace('(', '(?:').replace('(?:?:', '(?:')
    return re.compile(out), mixed


MODELS = {tag: _regex(model) for tag, model in CONTENT.items()}


def problems(root) -> list:
    """Violations of the transcribed rules in an ElementTree element and its descendants."""
    found = []
    for element in root.iter():
        tag = element.tag
        if tag not in CONTENT:
            found.append(f'<{tag}> is not an element Splice writes or the rules cover')
            continue
        declared = {name.lstrip('!'): values for name, values in ATTRIBUTES[tag].items()}
        for name, value in element.attrib.items():
            if name not in declared:
                found.append(f'<{tag}> has undeclared attribute {name}')
            elif declared[name] is not None and value not in declared[name]:
                found.append(f'<{tag} {name}="{value}"> is not one of {declared[name]}')
        for name in ATTRIBUTES[tag]:
            if name.startswith('!') and element.get(name[1:]) is None:
                found.append(f'<{tag}> lacks required attribute {name[1:]}')
        model = MODELS[tag]
        children = ''.join(f'<{child.tag}>' for child in element)
        if CONTENT[tag] == 'EMPTY':
            if len(element) or (element.text or '').strip():
                found.append(f'<{tag}> must be empty')
            continue
        regex, mixed = model
        if not mixed and (element.text or '').strip():
            found.append(f'<{tag}> may not hold text')
        if not regex.fullmatch(children):
            found.append(f'<{tag}> children out of DTD order: {[child.tag for child in element]}')
    return found
