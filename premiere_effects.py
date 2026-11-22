"""Map Splice's static adjustments onto Premiere effect components."""
import gzip
import math
import struct
from pathlib import Path
import xml.etree.ElementTree as ET

from py_premiere.models.component import Component, ComponentParam
from py_premiere import Time
from py_premiere.enums.general import KeyframeInterpolation
from py_premiere.models.caption import (
    read_style_color, read_style_float, write_style_color, write_style_float,
    write_payload_element, _add_table_field, _field_offset, _style_table,
    _write_table_flag,
)
from py_premiere.models.color import Color

TEMPLATES = Path(__file__).resolve().parent / 'vendor' / 'splice-effects'
NEUTRAL = {'brightness': 100, 'contrast': 100, 'saturation': 100}


def _component(clip, name):
    return next((c for c in clip.components if c.match_name == name), None)


def _parameter(component, name):
    if component:
        return next((p for p in component.properties if p.display_name == name), None)


def _value(component, name, fallback):
    param = _parameter(component, name)
    value = param.value if param else None
    return value if isinstance(value, (int, float)) and math.isfinite(value) else fallback


def _write_graphic_color(payload, slot, color):
    """Add a FlatBuffer RGB table to Adobe's default graphic text payload.

    The default authored graphic has no color table to borrow. A 3-channel
    table is appended with the same scalar layout used by Adobe's styled
    caption payloads; the parent style gains a normal forward table offset.
    """
    try:
        return write_style_color(payload, slot, color)
    except NotImplementedError as error:
        if 'no colour table' not in str(error):
            raise
    if _field_offset(payload, _style_table(payload), slot) is not None:
        raise ValueError('Cannot replace malformed graphic color')
    vtable = struct.pack('<HHHHH', 10, 8, 4, 5, 6) + b'\0\0'
    table = struct.pack('<iBBB', len(vtable), color.red, color.green, color.blue) + b'\0'
    with_field = bytearray(_add_table_field(payload + vtable + table, _style_table(payload), slot, b'\0'*4))
    field = _field_offset(with_field, _style_table(with_field), slot)
    target = len(with_field) - len(table)
    if field is None or target <= field:
        raise ValueError('Cannot construct graphic color')
    struct.pack_into('<I', with_field, field, target-field)
    return bytes(with_field)


def read(clip):
    lumetri = _component(clip, 'AE.ADBE Lumetri')
    volume = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None)
    color = {'brightness': round(100 * 2 ** _value(lumetri, 'Exposure', 0), 5),
             'contrast': 100 + _value(lumetri, 'Contrast', 0),
             'saturation': _value(lumetri, 'Saturation', 100)}
    return color, round(100 * _value(volume, 'Level', 1), 5)


def _attach(clip, name):
    # These isolated component templates come from py-premiere's Adobe-saved
    # fixtures. Allocate new IDs for every parameter and all internal edges.
    template = ET.fromstring(gzip.decompress((TEMPLATES / (name + '.xml.gz')).read_bytes()))
    doc = clip.track.sequence.project._document
    chain = doc.resolve(clip._element.find('ClipTrackItem/ComponentOwner/Components'))
    core = chain.find('ComponentChain')
    if core is None:
        core = ET.SubElement(chain, 'ComponentChain', Version='3')
    holder = core.find('Components')
    if holder is None:
        holder = ET.SubElement(core, 'Components', Version='1')
    existing = [int(e.text) for ref in holder.findall('Component')
                for e in doc.resolve(ref).findall('.//Component/ID') if (e.text or '').isdigit()]
    if name == 'motion':
        for tag in ('DefaultMotionComponentID', 'DefaultMotion'):
            flag = chain.find(tag)
            if flag is not None:
                chain.remove(flag)
    if name == 'volume':
        for tag in ('DefaultVolumeComponentID', 'DefaultVolume'):
            flag = chain.find(tag)
            if flag is not None:
                chain.remove(flag)
    mapping = {}
    for obj in template:
        old = obj.get('ObjectID')
        mapping[old] = doc.add_object(obj)
    for obj in template:
        for ref in obj.iter():
            if ref.get('ObjectRef'):
                ref.set('ObjectRef', mapping[ref.get('ObjectRef')])
    root_id = mapping[template.get('root')]
    element = doc.by_object_id[root_id]
    element.find('.//Component/ID').text = '1' if name in ('motion', 'volume') else str(max(existing + [2]) + 1)
    ET.SubElement(holder, 'Component', Index=str(len(holder)), ObjectRef=root_id)
    component = Component(element, clip)
    for ref in element.findall('.//Params/Param'):
        component._properties.append(ComponentParam(doc.resolve(ref), doc, component))
    clip._components.append(component)
    return component


def apply(clip, color, volume):
    current, old_volume = read(clip)
    color = {**NEUTRAL, **(color or {})}
    if color.get('lut') or color.get('temperature', 0) or color.get('tint', 0):
        raise ValueError('Premiere export cannot preserve this LUT or white balance edit. Save a Splice working copy to keep it.')
    exposure = float(color.get('exposure', 0))
    if not math.isfinite(exposure) or not -4 <= exposure <= 4:
        raise ValueError('Invalid exposure')
    color['brightness'] *= 2 ** exposure
    color = {key: color[key] for key in NEUTRAL}
    if clip._element.tag == 'VideoClipTrackItem' and color != current:
        component = _component(clip, 'AE.ADBE Lumetri')
        created = component is None
        if created:
            component = _attach(clip, 'lumetri')
        # Exposure is logarithmic. Contrast and saturation use percentage points.
        values = [('brightness', 'Exposure', math.log2(max(.01, color['brightness']) / 100)),
                  ('contrast', 'Contrast', color['contrast'] - 100),
                  ('saturation', 'Saturation', color['saturation'])]
        for key, name, value in values:
            if created or color[key] != current[key]:
                _parameter(component, name).value = float(value)
    if clip._element.tag == 'AudioClipTrackItem' and abs(volume - old_volume) > .00001:
        component = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None)
        if component is None:
            component = _attach(clip, 'volume')
        level = _parameter(component, 'Level')
        if level.is_time_varying:
            peak = max((level.get_value_at_key(time) for time in level.keys), default=1)
            if abs(peak - float(volume)/100) > .0001:
                raise ValueError('Adjust this keyframed Premiere audio envelope in Premiere or save a Splice working copy.')
        else:
            level.value = float(volume) / 100


def read_extended(clip):
    """Read fields whose native Premiere representation is verified."""
    result = {}
    text_component = _component(clip, 'AE.ADBE Text')
    if text_component is not None:
        position_param = _parameter(text_component, 'Position')
        position = position_param.value if position_param and isinstance(position_param.value, list) else [.5, .5]
        source = _parameter(text_component, 'Source Text')
        fill = read_style_color(source._payload(), 2) if source and source._payload() else None
        stroke = read_style_color(source._payload(), 4) if source and source._payload() else None
        stroke_width = read_style_float(source._payload(), 6) if source and source._payload() else None
        result['titleStyle'] = {
            'x': round(position[0] * 100, 4),
            'y': round(position[1] * 100, 4),
            'fontFamily': source.font_family or 'DM Sans',
            'fontSize': round(_value(text_component, 'Scale', 100) * .08, 4),
            'color': '#%02x%02x%02x' % fill[:3] if fill else '#ffffff',
            'strokeColor': '#%02x%02x%02x' % stroke[:3] if stroke else '#000000',
            'strokeWidth': round(stroke_width, 4) if stroke_width else 0,
        }
    motion = _component(clip, 'AE.ADBE Motion')
    if motion is not None:
        position_param = _parameter(motion, 'Position')
        position = position_param.value if position_param and isinstance(position_param.value, list) else [.5, .5]
        result['transform'] = {
            'x': round((position[0] - .5) * 100, 4),
            'y': round((position[1] - .5) * 100, 4),
            'scale': _value(motion, 'Scale', 100),
            'rotation': _value(motion, 'Rotation', 0),
        }
    volume_component = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None)
    level = _parameter(volume_component, 'Level')
    if level is not None and level.is_time_varying:
        keys = [(time.seconds, level.get_value_at_key(time)) for time in level.keys]
        if 2 <= len(keys) <= 4 and all(isinstance(value, (int, float)) for _, value in keys):
            start, end = clip.start.seconds, clip.end.seconds
            first, last = keys[0], keys[-1]
            if abs(first[0] - start) < .05 and abs(last[0] - end) < .05:
                result['volume'] = round(100 * max(value for _, value in keys), 4)
                result['fadeIn'] = round(keys[1][0] - start, 4) if first[1] == 0 and len(keys) > 2 else 0
                result['fadeOut'] = round(end - keys[-2][0], 4) if last[1] == 0 and len(keys) > 2 else 0
    return result


def apply_extended(clip, item):
    """Write supported static effects and reject values that would be lost."""
    style = item.get('titleStyle') or {}
    text_component = _component(clip, 'AE.ADBE Text')
    if text_component is not None:
        pos = _parameter(text_component, 'Position')
        if pos is not None and ('x' in style or 'y' in style):
            pos.value = [float(style.get('x', 50)) / 100, float(style.get('y', 50)) / 100]
        size = _parameter(text_component, 'Scale')
        if size is not None and 'fontSize' in style:
            size.value = float(style['fontSize']) / .08
        source = _parameter(text_component, 'Source Text')
        if source is not None and style.get('fontFamily'):
            source.font_family = style['fontFamily']
        if source is not None:
            payload = source._payload()
            changed = False
            for key, slot, default in (('color', 2, '#ffffff'), ('strokeColor', 4, '#000000')):
                value = style.get(key)
                if value is None or str(value).lower() == default and read_style_color(payload, slot) is None:
                    continue
                if not isinstance(value, str) or len(value) != 7 or value[0] != '#' or any(c not in '0123456789abcdefABCDEF' for c in value[1:]):
                    raise ValueError('Invalid title '+key)
                channels = [int(value[i:i+2], 16) for i in (1, 3, 5)]
                payload = _write_graphic_color(payload, slot, Color(*channels)); changed = True
                if slot == 4:
                    payload = _write_table_flag(payload, _style_table, 5)
            if 'strokeWidth' in style:
                width = float(style['strokeWidth'])
                if not math.isfinite(width) or not 0 <= width <= 8:
                    raise ValueError('Invalid title outline width')
                if width or read_style_float(payload, 6) is not None:
                    payload = write_style_float(payload, 6, width)
                    if width: payload = _write_table_flag(payload, _style_table, 5)
                    changed = True
            if changed:
                write_payload_element(source._document, source._element.find('StartKeyframeValue'), payload)
    transform = item.get('transform') or {}
    if transform:
        motion = _component(clip, 'AE.ADBE Motion')
        neutral = {'x': 0, 'y': 0, 'scale': 100, 'rotation': 0}
        if motion is None and any(float(transform.get(key, default)) != default for key, default in neutral.items()):
            motion = _attach(clip, 'motion')
        if motion is not None:
            position = _parameter(motion, 'Position')
            if position is not None and ('x' in transform or 'y' in transform):
                position.value = [.5 + float(transform.get('x', 0)) / 100, .5 + float(transform.get('y', 0)) / 100]
            for key, name in (('scale', 'Scale'), ('rotation', 'Rotation')):
                value = _parameter(motion, name)
                if value is not None and key in transform:
                    value.value = float(transform[key])
    if item.get('pan', 0):
        raise ValueError('Premiere export cannot preserve audio pan yet. Save a Splice working copy to keep this edit.')
    fade_in, fade_out = float(item.get('fadeIn', 0)), float(item.get('fadeOut', 0))
    if fade_in or fade_out:
        if clip._element.tag != 'AudioClipTrackItem':
            raise ValueError('Premiere export cannot preserve camera sound fades on a video-only clip.')
        component = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None)
        if component is None:
            component = _attach(clip, 'volume')
        level = _parameter(component, 'Level')
        if level.is_time_varying:
            raise ValueError('An existing Premiere audio envelope needs manual review before replacing it.')
        start = clip.start.seconds
        duration = clip.duration.seconds
        volume = float(item.get('volume', 100)) / 100
        points = [(start, 0 if fade_in else volume)]
        if fade_in:
            points.append((start + min(fade_in, duration/2), volume))
        if fade_out:
            points.append((start + duration - min(fade_out, duration/2), volume))
            points.append((start + duration, 0))
        else:
            points.append((start + duration, volume))
        level.set_keyframes([(Time.from_seconds(t), v, KeyframeInterpolation.LINEAR) for t, v in points])
