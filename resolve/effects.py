"""A clip's Inspector effects (``EffectFiltersBA``): read the parameters, and write the two values
calibration measured byte for byte.

``EffectFiltersBA`` is an envelope around a list of filters (field 1), each ``{1: effect type,
7: 0, 9: parameter slot...}``; a slot is ``{1: parameter id, 3: {1: {2: double}}}`` and an empty
slot is a parameter at its default. Measured in projects Resolve 21.1 saved with one setting
changed at a time (a clip without effects before): +6 dB on a sound clip is type 124 with slot 0
= parameter 95 (decibels) followed by four empty slots; Position X +200 px on a 1920-wide
timeline is type 4 with slot 3 = parameter 40 (a fraction of the timeline width, 0.1041667),
three empty slots before it and eight after. Both are framed version 2, plain (they are under 65 bytes).
"""
from . import blobs
from .codec import CodecError, envelope, pb_decode, pb_encode

TRANSFORM, COMPOSITE, AUDIO_VOLUME = 4, 2, 124
POSITION_X, POSITION_Y, ZOOM_X, ZOOM_Y = 40, 41, 42, 43
OPACITY = 1
VOLUME_DB = 95
#: The slot layouts calibration showed: effect type -> (slots in all, {parameter: slot}).
LAYOUTS = {AUDIO_VOLUME: (5, {VOLUME_DB: 0}), TRANSFORM: (12, {POSITION_X: 3})}


def decode(payload):
    """``[(effect type, {parameter id: first value or None}, plugin name or None, keyframed)]``
    of a decoded ``EffectFiltersBA`` payload (raises BlobError on damage)."""
    out = []
    for value in blobs.pb_all(blobs.protobuf(payload), 1, 2):
        effect = blobs.protobuf(value)
        kind = blobs.pb_first(effect, 1, 0)
        params, plugin, keyed = {}, None, False
        for raw in blobs.pb_all(effect, 9, 2):
            param = blobs.protobuf(raw)
            pid = blobs.pb_first(param, 1, 0)
            number = None
            for holder in blobs.pb_all(param, 3, 2):
                for inner in blobs.pb_all(blobs.protobuf(holder), 1, 2):
                    if number is None:
                        number = blobs.pb_double(blobs.pb_first(blobs.protobuf(inner), 2, 1))
                for inner in blobs.pb_all(blobs.protobuf(holder), 2, 2):
                    plugin = plugin or blobs.pb_text(blobs.pb_first(blobs.protobuf(inner), 2, 2))
            if blobs.pb_all(param, 4):
                keyed = True
            if pid is not None:
                params[pid] = number
        out.append((kind, params, plugin, keyed))
    return out


def _slot(pid, value):
    import struct
    return pb_encode([[1, 0, pid], [3, 2, pb_encode([[1, 2, pb_encode([[2, 1, struct.pack('<d', value)]])]])]])


def single(kind, pid, value):
    """The whole ``EffectFiltersBA`` of a clip that had no effects, holding one parameter."""
    slots, where = LAYOUTS[kind]
    fields = [[1, 0, kind], [7, 0, 0]]
    for index in range(slots):
        fields.append([9, 2, _slot(pid, value) if where.get(pid) == index else b''])
    return envelope(pb_encode([[1, 2, pb_encode(fields)]]), version=2)


def replace(blob, kind, pid, value):
    """``blob`` with the first value of parameter ``pid`` of its one effect of type ``kind``
    replaced, everything else kept; CodecError when the blob is not that simple shape."""
    from .codec import parse_envelope, unwrap
    parsed = parse_envelope(blob)
    if parsed is None:
        raise CodecError('the effects are not an envelope')
    top = pb_decode(unwrap(blob, 'EffectFiltersBA'), 'EffectFiltersBA')
    effects = [f for f in top if f[0] == 1]
    if len(top) != len(effects) or len(effects) != 1:
        raise CodecError('the clip holds more than one effect')
    fields = pb_decode(effects[0][2], 'the effect')
    if [f[2] for f in fields if f[0] == 1] != [kind]:
        raise CodecError('the clip holds another effect')
    found = False
    for slot in fields:
        if slot[0] != 9 or not slot[2]:
            continue
        param = pb_decode(slot[2], 'an effect parameter')
        if [f[2] for f in param if f[0] == 1] == [pid]:
            if [f[0] for f in param] != [1, 3]:
                raise CodecError('the parameter is keyframed or holds more than one value')
            slot[2] = _slot(pid, value)
            found = True
    if not found:
        raise CodecError('the effect does not hold that parameter')
    effects[0][2] = pb_encode(fields)
    return envelope(pb_encode(top), version=parsed[0])
