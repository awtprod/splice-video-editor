"""Map Splice's static adjustments onto Premiere effect components."""
import gzip
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from py_premiere.models.component import Component, ComponentParam

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
    element.find('.//Component/ID').text = str(max(existing + ([2] if name == 'lumetri' else [0])) + 1)
    ET.SubElement(holder, 'Component', Index=str(len(holder)), ObjectRef=root_id)
    component = Component(element, clip)
    for ref in element.findall('.//Params/Param'):
        component._properties.append(ComponentParam(doc.resolve(ref), doc, component))
    clip._components.append(component)
    return component


def apply(clip, color, volume):
    current, old_volume = read(clip)
    color = {**NEUTRAL, **(color or {})}
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
        _parameter(component, 'Level').value = float(volume) / 100
