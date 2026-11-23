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
    _write_table_flag, _document_table,
)
from py_premiere.models.color import Color
from py_premiere.xml.mutations import remove_child

TEMPLATES = Path(__file__).resolve().parent / 'vendor' / 'splice-effects'
NEUTRAL = {'brightness': 100, 'contrast': 100, 'saturation': 100, 'temperature': 0, 'tint': 0}
HDR_BRIGHTNESS_NAME = 'Splice HDR Brightness'


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


def _text_run_count(payload):
    document=_document_table(payload)
    field=_field_offset(payload,document,0)
    if field is None: return 0
    vector=field+struct.unpack_from('<I',payload,field)[0]
    return struct.unpack_from('<I',payload,vector)[0]


def _read_keys(param, clip, convert=lambda value: value):
    if param is None or not param.is_time_varying:
        return []
    audio=clip._element.tag == 'AudioClipTrackItem'
    base=clip.in_point.seconds if audio else clip.start.seconds
    rate=clip.speed if audio else 1
    points=[{'time': round((key.seconds - base)/rate, 5),
             'value': round(convert(param.get_value_at_key(key)), 5)} for key in param.keys]
    if any(point['time'] < -.001 or point['time'] > clip.duration.seconds+.001 for point in points):
        return []
    return points


def _apply_scalar_keys(param, clip, points, decode=lambda value: value, encode=lambda value: value,
                       low=-100000, high=100000):
    if not isinstance(points, list) or not 1 <= len(points) <= 200:
        raise ValueError('Invalid Premiere keyframe count')
    prepared=[]
    for point in points:
        if not isinstance(point, dict):
            raise ValueError('Invalid Premiere keyframe')
        time=float(point.get('time', float('nan')))
        value=float(point.get('value', float('nan')))
        if not math.isfinite(time) or not math.isfinite(value) or not 0 <= time <= clip.duration.seconds + .001 or not low <= value <= high:
            raise ValueError('Invalid Premiere keyframe time or value')
        prepared.append((round(time,5),round(value,5)))
    if len({time for time,_ in prepared}) != len(prepared):
        raise ValueError('Duplicate Premiere keyframe time')
    if _read_keys(param,clip,decode) == [{'time':t,'value':v} for t,v in prepared]:
        return
    audio=clip._element.tag == 'AudioClipTrackItem'
    base=clip.in_point.seconds if audio else clip.start.seconds
    rate=clip.speed if audio else 1
    param.set_keyframes([(Time.from_seconds(base+t*rate),encode(v),KeyframeInterpolation.LINEAR)
                         for t,v in prepared])


def _lumetri_layers(clip):
    lumetri=brightness=None
    for component in clip.components:
        if component.match_name != 'AE.ADBE Lumetri':
            continue
        if component._element.findtext('Component/InstanceName') == HDR_BRIGHTNESS_NAME:
            if brightness is None: brightness=component
        elif lumetri is None:
            lumetri=component
    return lumetri,brightness


def read(clip):
    lumetri,brightness_layer = _lumetri_layers(clip)
    volume = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None)
    brightness = round(100 * 2 ** (_value(brightness_layer,'Exposure',0)/2.4),5)
    color = {'brightness': 0 if brightness<=.01 else brightness, 'exposure': _value(lumetri, 'Exposure', 0),
             'contrast': 100 + _value(lumetri, 'Contrast', 0),
             'saturation': _value(lumetri, 'Saturation', 100),
             'temperature': _value(lumetri, 'Temperature', 0),
             'tint': _value(lumetri, 'Tint', 0)}
    return color, round(100 * _value(volume, 'Level', 1), 5)


def _attach(clip, name, *, instance_name=None, before=None):
    # These isolated component templates come from py-premiere's Adobe-saved
    # fixtures. Allocate new IDs for every parameter and all internal edges.
    template = ET.fromstring(gzip.decompress((TEMPLATES / (name + '.xml.gz')).read_bytes()))
    if name == 'opacity':
        # The Adobe fixture is animated. An attached default intrinsic starts
        # static so both a plain opacity value and new keyframes can be set.
        for obj in template:
            if obj.findtext('Name') == 'Opacity':
                for tag in ('Keyframes','IsTimeVarying'):
                    field=obj.find(tag)
                    if field is not None: obj.remove(field)
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
    if name == 'opacity':
        for tag in ('DefaultOpacityComponentID', 'DefaultOpacity'):
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
    element.find('.//Component/ID').text = {'motion':'1','opacity':'2','volume':'1'}.get(name,str(max(existing + [2]) + 1))
    if instance_name:
        ET.SubElement(element.find('Component'),'InstanceName').text=instance_name
    ref = ET.SubElement(holder, 'Component', Index=str(len(holder)), ObjectRef=root_id)
    if before is not None:
        original = next(node for node in holder if node.get('ObjectRef')==before._element.get('ObjectID'))
        holder.remove(ref)
        holder.insert(list(holder).index(original),ref)
        for index,node in enumerate(holder): node.set('Index',str(index))
    component = Component(element, clip)
    for ref in element.findall('.//Params/Param'):
        component._properties.append(ComponentParam(doc.resolve(ref), doc, component))
    clip._components.append(component)
    if name == 'motion':
        _parameter(component, 'Scale').value = 100.0
    return component


def apply(clip, color, volume, *, hdr=False):
    current, old_volume = read(clip)
    color = {**NEUTRAL, **(color or {})}
    if color.get('lut'):
        raise ValueError('Premiere export cannot preserve this 3D LUT. Save a Splice working copy to keep it.')
    exposure = float(color.get('exposure', 0))
    if not math.isfinite(exposure) or not -4 <= exposure <= 4:
        raise ValueError('Invalid exposure')
    if clip._element.tag == 'VideoClipTrackItem':
        lumetri,brightness_layer = _lumetri_layers(clip)
        brightness = float(color['brightness'])
        if not math.isfinite(brightness) or brightness<0:
            raise ValueError('Invalid brightness')
        if hdr or brightness_layer:
            if abs(brightness-current['brightness'])>1e-5:
                brightness_layer = brightness_layer or _attach(clip,'lumetri',instance_name=HDR_BRIGHTNESS_NAME,before=lumetri)
                _set_color_parameter(brightness_layer,'Exposure',2.4*math.log2(max(.009,brightness)/100))
        else:
            exposure += math.log2(max(.01,brightness)/100)
        values = [('Exposure',exposure,0), ('Contrast',color['contrast']-100,0),
                  ('Saturation',color['saturation'],100), ('Temperature',color['temperature'],0), ('Tint',color['tint'],0)]
        if lumetri or any(abs(value-default)>1e-5 for _,value,default in values):
            lumetri = lumetri or _attach(clip,'lumetri')
            for name,value,default in values:
                if abs(_value(lumetri,name,default)-value)>1e-5:
                    _set_color_parameter(lumetri,name,value)
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


def _set_color_parameter(component, name, value):
    param = _parameter(component,name)
    if param.is_time_varying:
        raise ValueError('An existing Premiere Lumetri animation needs manual review before replacing it.')
    param.value = float(value)


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
        for key, name in (('scale','Scale'),('rotation','Rotation')):
            points=_read_keys(_parameter(motion,name),clip)
            if points: result.setdefault('keyframes',{})[key]=points
    opacity_component = _component(clip, 'AE.ADBE Opacity')
    opacity_param = _parameter(opacity_component, 'Opacity')
    if opacity_param is not None:
        result['opacity'] = _value(opacity_component, 'Opacity', 100)
        points=_read_keys(opacity_param,clip)
        if points: result.setdefault('keyframes',{})['opacity']=points
    volume_component = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None)
    level = _parameter(volume_component, 'Level')
    if level is not None and level.is_time_varying:
        keys = [(time.seconds, level.get_value_at_key(time)) for time in level.keys]
        source_end=clip.in_point.seconds + clip.duration.seconds*clip.speed
        if not keys or any(t < clip.in_point.seconds-.001 or t > source_end+.001 for t,_ in keys):
            result['nativeVolumeOutsideTrim'] = True
            return result
        if 2 <= len(keys) <= 4 and all(isinstance(value, (int, float)) for _, value in keys):
            start, end = clip.in_point.seconds, source_end
            first, last = keys[0], keys[-1]
            if abs(first[0] - start) < .05 and abs(last[0] - end) < .05:
                peak=max(value for _,value in keys)
                close=lambda a,b:abs(a-b)<.00001
                fade_in=len(keys) in (3,4) and close(first[1],0) and close(keys[1][1],peak)
                fade_out=len(keys) in (3,4) and close(last[1],0) and close(keys[-2][1],peak)
                if ((fade_in or fade_out) and
                    (not fade_in or fade_out or close(last[1],peak)) and
                    (not fade_out or fade_in or close(first[1],peak)) and
                    all(close(value,peak) for _,value in keys[1:-1])):
                    result['volume'] = round(100*peak,4)
                    result['fadeIn'] = round((keys[1][0] - start)/clip.speed,4) if fade_in else 0
                    result['fadeOut'] = round((end - keys[-2][0])/clip.speed,4) if fade_out else 0
                    return result
        result['keyframes'] = {**result.get('keyframes',{}), 'volume':_read_keys(level,clip,lambda value:value*100)}
        result['volume'] = round(100 * max(value for _,value in keys),4)
    return result


def apply_extended(clip, item):
    """Write supported static effects and reject values that would be lost."""
    automation = item.get('keyframes') or {}
    if not isinstance(automation, dict):
        raise ValueError('Invalid transform keyframes')
    if any(automation.get(key) for key in ('x','y')):
        raise ValueError('Premiere export cannot preserve animated position yet. Save a Splice working copy to keep it.')
    style = item.get('titleStyle') or {}
    if style.get('align', 'center') != 'center':
        raise ValueError('Premiere export cannot preserve left or right title alignment yet. Save a Splice working copy to keep it.')
    text_component = _component(clip, 'AE.ADBE Text')
    if text_component is not None:
        pos = _parameter(text_component, 'Position')
        if pos is not None and ('x' in style or 'y' in style):
            wanted = [float(style.get('x', 50)) / 100, float(style.get('y', 50)) / 100]
            if not isinstance(pos.value, list) or any(abs(a-b) > .00001 for a,b in zip(wanted,pos.value)):
                if pos.is_time_varying:
                    raise ValueError('An existing Premiere title position animation needs manual review before replacing it.')
                pos.value = wanted
        size = _parameter(text_component, 'Scale')
        if size is not None and 'fontSize' in style:
            wanted = float(style['fontSize']) / .08
            if abs(wanted - float(size.value or 0)) > .0001:
                if size.is_time_varying:
                    raise ValueError('An existing Premiere title size animation needs manual review before replacing it.')
                size.value = wanted
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
                wanted=Color(*channels)
                if read_style_color(payload,slot)==wanted: continue
                if _text_run_count(payload)>1:
                    raise ValueError('Premiere export cannot recolor a title with multiple text styles. Save a Splice working copy to keep this edit.')
                payload = _write_graphic_color(payload, slot, wanted); changed = True
                if slot == 4:
                    payload = _write_table_flag(payload, _style_table, 5)
            if 'strokeWidth' in style:
                width = float(style['strokeWidth'])
                if not math.isfinite(width) or not 0 <= width <= 8:
                    raise ValueError('Invalid title outline width')
                prior=read_style_float(payload,6)
                if (width or prior is not None) and (prior is None or abs(width-prior)>.0001):
                    if _text_run_count(payload)>1:
                        raise ValueError('Premiere export cannot change outlines on a title with multiple text styles. Save a Splice working copy to keep this edit.')
                    payload = write_style_float(payload, 6, width)
                    if width: payload = _write_table_flag(payload, _style_table, 5)
                    changed = True
            if changed:
                write_payload_element(source._document, source._element.find('StartKeyframeValue'), payload)
    transform = item.get('transform') or {}
    if transform:
        motion = _component(clip, 'AE.ADBE Motion')
        neutral = {'x': 0, 'y': 0, 'scale': 100, 'rotation': 0}
        if motion is None and (any(float(transform.get(key, default)) != default for key, default in neutral.items()) or
                               any(automation.get(key) for key in ('scale','rotation'))):
            motion = _attach(clip, 'motion')
        if motion is not None:
            position = _parameter(motion, 'Position')
            if position is not None and ('x' in transform or 'y' in transform):
                wanted = [.5 + float(transform.get('x', 0)) / 100, .5 + float(transform.get('y', 0)) / 100]
                if not isinstance(position.value, list) or any(abs(a-b) > .00001 for a,b in zip(wanted,position.value)):
                    if position.is_time_varying:
                        raise ValueError('An existing Premiere Motion animation needs manual review before replacing it.')
                    position.value = wanted
            for key, name in (('scale', 'Scale'), ('rotation', 'Rotation')):
                value = _parameter(motion, name)
                if value is not None and key in transform:
                    wanted = float(transform[key])
                    if abs(wanted - float(value.value or 0)) > .0001:
                        if value.is_time_varying:
                            raise ValueError('An existing Premiere Motion animation needs manual review before replacing it.')
                        value.value = wanted
    for key,name in (('scale','Scale'),('rotation','Rotation')):
        if automation.get(key):
            motion = _component(clip, 'AE.ADBE Motion') or _attach(clip,'motion')
            param=_parameter(motion,name)
            base=float(transform.get(key,100 if key=='scale' else 0))
            if not param.is_time_varying and abs(float(param.value or 0)-base)>.0001:
                param.value=base
            _apply_scalar_keys(param,clip,automation[key],low=10 if key=='scale' else -180,
                               high=400 if key=='scale' else 180)
    opacity = float(item.get('opacity',100))
    if not math.isfinite(opacity) or not 0 <= opacity <= 100:
        raise ValueError('Invalid clip opacity')
    component = _component(clip,'AE.ADBE Opacity')
    if automation.get('opacity') or opacity != 100 or (component and 'opacity' in item):
        component = component or _attach(clip,'opacity')
        param = _parameter(component,'Opacity')
        if automation.get('opacity'):
            if not param.is_time_varying and abs(float(param.value or 0)-opacity)>.0001:
                param.value=opacity
            _apply_scalar_keys(param,clip,automation['opacity'],low=0,high=100)
        elif automation.get('opacity') == [] and param.is_time_varying:
            for tag in ('Keyframes','IsTimeVarying'):
                field = param._element.find(tag)
                if field is not None:
                    remove_child(param._element,field)
            param.value=opacity
        elif abs(float(param.value or 0)-opacity)>.0001:
            if param.is_time_varying:
                raise ValueError('An existing Premiere opacity animation needs manual review before replacing it.')
            param.value=opacity
    if item.get('pan', 0):
        raise ValueError('Premiere export cannot preserve audio pan yet. Save a Splice working copy to keep this edit.')
    fade_in, fade_out = float(item.get('fadeIn', 0)), float(item.get('fadeOut', 0))
    if not math.isfinite(fade_in) or not math.isfinite(fade_out) or min(fade_in,fade_out)<0:
        raise ValueError('Invalid audio fade')
    if automation.get('volume'):
        if fade_in or fade_out:
            raise ValueError('Premiere export cannot combine audio gain keyframes with clip fades. Save a Splice working copy to keep both.')
        if clip._element.tag != 'AudioClipTrackItem':
            raise ValueError('Premiere export cannot preserve camera sound gain keyframes on a video-only clip.')
        component = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None) or _attach(clip,'volume')
        _apply_scalar_keys(_parameter(component,'Level'),clip,automation['volume'],
                           decode=lambda value:value*100,encode=lambda value:value/100,low=0,high=200)
    if fade_in or fade_out:
        if clip._element.tag != 'AudioClipTrackItem':
            raise ValueError('Premiere export cannot preserve camera sound fades on a video-only clip.')
        component = next((c for c in clip.components if c.match_name.startswith('Internal Volume')), None)
        if component is None:
            component = _attach(clip, 'volume')
        level = _parameter(component, 'Level')
        if level.is_time_varying:
            original = read_extended(clip)
            if not (original.get('fadeIn') or original.get('fadeOut')):
                raise ValueError('An existing Premiere audio envelope needs manual review before replacing it.')
        duration = clip.duration.seconds
        head, tail = min(fade_in, duration), min(fade_out, duration)
        volume = float(item.get('volume', 100))
        points = {0, duration}
        if head+tail > duration:
            points.add(duration*head/(head+tail))
        else:
            points.update([head,duration-tail])
        keys = [{'time':t,'value':volume*min(1,t/head if head else 1,(duration-t)/tail if tail else 1)}
                for t in sorted(points)]
        _apply_scalar_keys(level,clip,keys,decode=lambda v:v*100,encode=lambda v:v/100,low=0,high=200)
