"""Encoders for the values Resolve stores, the inverses of :mod:`resolve.blobs`' decoders.

Ported (without pydantic, for Python 3.10+) from ai-agent-resolve ``src/aar/drp/modify/encode.py``
and ``src/aar/drp/mediapool/blobtree.py`` at commit baa82027bc9212d5b64572b70806f0d69f8812fa
(same owner). Every function either returns bytes or raises :class:`CodecError`; nothing is
guessed.

* keyed dict: ``[u32 BE 1][u32 BE count]`` then ``[key][u32 type][u8 null][value]`` entries,
  edited as ``[key, type, null, value]`` lists (:func:`blobs.keyed_entries`).
* protobuf: canonical encoding of ``[number, wire, value]`` lists.
* message envelope ``[u32 BE version][u32 BE length][codec][payload]``: Resolve frames a
  message of at most 64 bytes plain (codec 0x80) and a longer one as zstd (0x81). That rule was
  found on every envelope of Resolve 21.1's media and item rows (plain up to 64 bytes, zstd from
  66; no 65-byte message was seen, so that length is refused). The zstd frame is written in raw
  blocks (:func:`resolve.zstd.raw_frame`): a standard frame every zstd decoder reads, and one
  Resolve itself writes for small payloads.
"""
import struct
from collections import deque

from . import blobs, zstd
from .blobs import BlobError

KD_BOOL, KD_INT32, KD_UINT32, KD_INT64, KD_UINT64, KD_DOUBLE = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
KD_CHAR, KD_STRING, KD_BYTES, KD_DATE, KD_FLOAT = 0x07, 0x0A, 0x0C, 0x10, 0x87
CODEC_PROTOBUF, CODEC_ZSTD = 0x80, 0x81
PLAIN_MAX = 64
UNSEEN_LENGTHS = frozenset({65})
MAX_LAYER_OUTPUT = 1024 * 1024
MAX_LAYERS = 10
MAX_WALKED = 4096
_U32 = 0xFFFFFFFF
_FIXED = {KD_INT32: '>i', KD_UINT32: '>I', KD_INT64: '>q', KD_UINT64: '>Q', KD_DOUBLE: '>d', KD_FLOAT: '>d'}
INT_RANGES = {KD_INT32: (-(1 << 31), (1 << 31) - 1), KD_UINT32: (0, _U32),
              KD_INT64: (-(1 << 63), (1 << 63) - 1), KD_UINT64: (0, (1 << 64) - 1)}


class CodecError(ValueError):
    """A value that cannot be encoded the way Resolve stores it."""


# --------------------------------------------------------------------------- protobuf

def varint(n):
    if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n >= 1 << 64:
        raise CodecError(f'varint out of range: {n!r}')
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _key(number, wire):
    if not 0 < number < 1 << 29 or wire not in (0, 1, 2, 5):
        raise CodecError(f'bad protobuf key {number}/{wire}')
    return varint(number << 3 | wire)


def pb_encode(fields):
    """A canonically encoded message from ``(number, wire, value)`` fields in their order."""
    out = bytearray()
    for number, wire, value in fields:
        if wire == 0:
            out += _key(number, 0) + varint(value)
        elif wire == 2:
            if not isinstance(value, (bytes, bytearray)):
                raise CodecError(f'protobuf field {number}: a length-delimited value needs bytes')
            out += _key(number, 2) + varint(len(value)) + bytes(value)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if not isinstance(value, (bytes, bytearray)) or len(value) != size:
                raise CodecError(f'protobuf field {number}: wire {wire} needs {size} bytes')
            out += _key(number, wire) + bytes(value)
        else:
            raise CodecError(f'protobuf field {number}: wire type {wire} is not written')
    return bytes(out)


def pb_decode(data, what):
    """``[number, wire, value]`` lists of one message level; a message that does not decode, or
    does not encode back to its own bytes, is refused (an edit must change only what it means)."""
    if not isinstance(data, (bytes, bytearray)):
        raise CodecError(f'{what} is not a blob')
    try:
        fields = [list(f) for f in blobs.protobuf(bytes(data))]
    except BlobError as error:
        raise CodecError(f'{what} is not a protobuf message ({error})') from None
    if pb_encode(fields) != bytes(data):
        raise CodecError(f'{what} is not canonically encoded, so it cannot be edited exactly')
    return fields


def pb_get(fields, number, what):
    hits = [f for f in fields if f[0] == number]
    if len(hits) != 1:
        raise CodecError(f'{what}: expected one field {number}, found {len(hits)}')
    return hits[0]


def pb_remove(fields, number):
    before = len(fields)
    fields[:] = [f for f in fields if f[0] != number]
    return before - len(fields)


# --------------------------------------------------------------------------- keyed dicts

def kd_decode(data, what):
    try:
        return blobs.keyed_entries(data)
    except BlobError as error:
        raise CodecError(f'{what} is not a keyed dict ({error})') from None


def _kd_string(value):
    if value is None:
        return b'\xff\xff\xff\xff'
    data = value.encode('utf-16-be', 'surrogatepass')
    if len(data) >= _U32:
        raise CodecError('keyed-dict string too long')
    return struct.pack('>I', len(data)) + data


def _kd_value(vtype, value):
    if vtype == KD_STRING:
        if value is not None and not isinstance(value, str):
            raise CodecError('a keyed-dict string must be text or null')
        return _kd_string(value)
    if vtype == KD_BYTES:
        if value is None:
            return b'\xff\xff\xff\xff'
        if not isinstance(value, (bytes, bytearray)) or len(value) >= _U32:
            raise CodecError('a keyed-dict byte array must be bytes')
        return struct.pack('>I', len(value)) + bytes(value)
    if vtype == KD_BOOL:
        if not isinstance(value, bool):
            raise CodecError('a keyed-dict bool must be a bool')
        return b'\x01' if value else b'\x00'
    if vtype in _FIXED:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CodecError(f'keyed-dict type 0x{vtype:x} needs a number')
        if vtype in INT_RANGES:
            low, high = INT_RANGES[vtype]
            if not isinstance(value, int) or not low <= value <= high:
                raise CodecError(f'keyed-dict type 0x{vtype:x} cannot hold {value!r}')
        return struct.pack(_FIXED[vtype], value)
    if vtype == KD_CHAR:
        if not isinstance(value, str) or len(value) != 1 or ord(value) > 0xFFFF:
            raise CodecError('a keyed-dict char must be one character')
        return struct.pack('>H', ord(value))
    if vtype == KD_DATE:
        if not isinstance(value, (bytes, bytearray)) or len(value) != 9:
            raise CodecError('a keyed-dict date must be its 9 stored bytes')
        return bytes(value)
    raise CodecError(f'keyed-dict type 0x{vtype:x} is not written here')


def kd_encode(entries):
    """A keyed dict from ``[key, type, null flag, value]`` entries, in their order."""
    out = bytearray(struct.pack('>II', 1, len(entries)))
    for key, vtype, null, value in entries:
        if not isinstance(key, str) or not key:
            raise CodecError('a keyed-dict key must be non-empty text')
        if null not in (0, 1):
            raise CodecError('a keyed-dict null flag must be 0 or 1')
        out += _kd_string(key) + struct.pack('>IB', vtype, null) + _kd_value(vtype, value)
    return bytes(out)


def kd_get(entries, key):
    hits = [e for e in entries if e[0] == key]
    if len(hits) > 1:
        raise CodecError(f'key {key!r} appears twice in one keyed dict')
    return hits[0] if hits else None


def kd_set(entries, key, value, what):
    """Give an existing entry a new value of its own type (refused if the type cannot hold it)."""
    entry = kd_get(entries, key)
    if entry is None:
        raise CodecError(f'{what} has no {key!r}')
    vtype = entry[1]
    if isinstance(value, bool):
        fits = vtype == KD_BOOL
    elif isinstance(value, int):
        low, high = INT_RANGES.get(vtype, (0, -1))
        fits = vtype in INT_RANGES and low <= value <= high
    elif isinstance(value, float):
        fits = vtype in (KD_DOUBLE, KD_FLOAT)
    elif isinstance(value, str):
        fits = vtype == KD_STRING
    elif isinstance(value, (bytes, bytearray)):
        fits = vtype == KD_BYTES
    else:
        fits = False
    if not fits:
        raise CodecError(f'{what}.{key} is of type 0x{vtype:x}, which cannot hold {value!r:.60}')
    entry[3] = value


def edit_kd(blob, what, change):
    entries = kd_decode(blob, what)
    change(entries)
    return kd_encode(entries)


# --------------------------------------------------------------------------- envelopes

def parse_envelope(data):
    """``(version or None, codec, payload)``: codec ``protobuf`` (0x80), ``zstd`` (0x81, payload
    still compressed) or ``none`` (a header without a codec byte); None when ``data`` is neither
    framing."""
    data = bytes(data)
    if len(data) >= 9:
        version, length = struct.unpack_from('>II', data, 0)
        if length == len(data) - 8:
            if data[8] == CODEC_ZSTD and data[9:13] == zstd.MAGIC:
                return version, 'zstd', data[9:]
            if data[8] == CODEC_PROTOBUF:
                return version, 'protobuf', data[9:]
            return version, 'none', data[8:]
    if data[:1] == bytes([CODEC_ZSTD]) and data[1:5] == zstd.MAGIC:
        return None, 'zstd', data[1:]
    if data[:1] == bytes([CODEC_PROTOBUF]):
        return None, 'protobuf', data[1:]
    return None


def unwrap(data, what, max_output=MAX_LAYER_OUTPUT):
    """The payload of an envelope, a zstd payload checked to be exactly one frame first."""
    parsed = parse_envelope(data) if isinstance(data, (bytes, bytearray)) else None
    if parsed is None:
        raise CodecError(f'{what} is not an enveloped message')
    _version, codec, payload = parsed
    if codec != 'zstd':
        return payload
    try:
        zstd.frame_length(payload)
        return zstd.decompress(payload, max_output)
    except zstd.ZstdError as error:
        raise CodecError(f'{what} does not decompress ({error})') from None


def codec_for(payload, what):
    """The codec Resolve gives a message of ``payload``'s length (:data:`PLAIN_MAX`)."""
    if len(payload) in UNSEEN_LENGTHS:
        raise CodecError(f'{what}: a message of {len(payload)} bytes, a length Resolve was never seen to '
                         'frame, so its framing is not known')
    return 'protobuf' if len(payload) <= PLAIN_MAX else 'zstd'


def envelope(payload, version=2, codec=None, what='a message'):
    """``payload`` framed with ``version``; ``codec`` None picks Resolve's codec by length."""
    payload = bytes(payload)
    codec = codec or codec_for(payload, what)
    if codec == 'zstd':
        body = bytes([CODEC_ZSTD]) + zstd.raw_frame(payload)
    elif codec == 'protobuf':
        body = bytes([CODEC_PROTOBUF]) + payload
    elif codec == 'none':
        body = payload
    else:
        raise CodecError(f'unknown codec {codec!r}')
    if version is None:
        return body
    if not 0 <= version <= _U32 or len(body) > _U32:
        raise CodecError('envelope out of range')
    return struct.pack('>II', version, len(body)) + body


def rewrap(original, payload, what):
    """``payload`` framed the way ``original`` was (version and form), with the codec Resolve
    uses for its length; a codec-less framing stays codec-less."""
    parsed = parse_envelope(original) if isinstance(original, (bytes, bytearray)) else None
    if parsed is None:
        raise CodecError(f'{what} is not an envelope')
    version, codec, _payload = parsed
    return envelope(payload, version, None if codec in ('zstd', 'protobuf') else codec, what)


def message_of(blob, what):
    return blob, unwrap(blob, what)


def edit_message(blob, what, change):
    """Decode an enveloped message, let ``change`` edit its fields, frame it again."""
    payload = unwrap(blob, what)
    fields = pb_decode(payload, what)
    change(fields)
    return rewrap(blob, pb_encode(fields), what)


# --------------------------------------------------------------------------- searching layers

def layers(value):
    """Every layer of ``value``: itself and, as far as they decode, envelope payloads (zstd
    decompressed), keyed-dict values and length-delimited protobuf fields; at most
    :data:`MAX_LAYERS` deep and :data:`MAX_WALKED` layers. A zstd layer that does not decompress,
    or more layers than the bounds, is refused: a layer that cannot be searched could hide an id."""
    queue = deque()
    if isinstance(value, str):
        queue.append((value.encode('utf-8', 'surrogatepass'), 0))
    elif isinstance(value, (bytes, bytearray)):
        queue.append((bytes(value), 0))
    seen, count = set(), 0
    while queue:
        data, depth = queue.popleft()
        if data in seen:
            continue
        seen.add(data)
        count += 1
        if count > MAX_WALKED:
            raise CodecError(f'a value has more than {MAX_WALKED} layers')
        yield data
        if len(data) < 2:
            continue
        children = [c for c in _children(data) if c not in seen]
        if children and depth >= MAX_LAYERS:
            raise CodecError(f'a value has layers more than {MAX_LAYERS} deep')
        queue.extend((child, depth + 1) for child in children)


def _children(data):
    out = []
    parsed = parse_envelope(data)
    if parsed is not None:
        out.append(unwrap(data, 'a nested value'))
    if len(data) >= 8 and data[:4] == b'\x00\x00\x00\x01':
        try:
            entries = blobs.keyed_entries(data)
        except BlobError:
            entries = []
        for entry in entries:
            if isinstance(entry[3], (bytes, bytearray)):
                out.append(bytes(entry[3]))
            elif isinstance(entry[3], str):
                out.append(entry[3].encode('utf-8', 'surrogatepass'))
    try:
        fields = blobs.protobuf(data)
    except BlobError:
        fields = []
    out.extend(v for _n, w, v in fields if w == 2 and isinstance(v, bytes) and v)
    return out


def contains_text(value, needles):
    """The label of the first of ``needles`` (text -> label) found in any layer of ``value``, in
    UTF-8, UTF-16BE or UTF-16LE; None when none is."""
    forms = [(form, label) for text, label in needles.items() if text
             for form in (text.encode('utf-8', 'surrogatepass'), text.encode('utf-16-be', 'surrogatepass'),
                          text.encode('utf-16-le', 'surrogatepass'))]
    for layer in layers(value):
        for form, label in forms:
            if form in layer:
                return label
    return None
