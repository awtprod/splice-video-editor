"""Codecs for the binary values Resolve stores in project files.

Ported (without pydantic, for Python 3.10+) from ai-agent-resolve ``src/aar/drp/blobs.py`` and
``reader.py`` at commit aff6573bcba41e66e782ea2c4714290f227408b0, same owner. Layouts:

* keyed dict: ``[u32 BE 1][u32 BE count]`` then entries ``[u32 len][UTF-16BE key][u32 type]
  [u8 null][value]`` (a Qt 4 QVariantMap).
* message envelope: ``[u32 BE version][u32 BE length][codec][payload]``; codec ``0x80`` is plain
  protobuf, ``0x81`` a zstd frame holding protobuf. The flag-only form omits the two words.
* ``FrameRate``: little-endian double + 8 bytes; ``Resolution``: ``[0][u32 BE w][0][u32 BE h]``;
  ``MediaExtents``: two little-endian doubles (start, duration seconds).
* ``MediaTimemapBA``: ``[u8 kind][BE doubles]`` (1x) or a keyed dict ``Sm2TimeMap`` (retimed).
"""
import math
import re
import struct

from . import zstd

MAX_BLOB_OUTPUT = 64 * 1024 * 1024
MAX_TOTAL_OUTPUT = 400 * 1024 * 1024
MAX_PROTOBUF_FIELDS = 200_000
_HEX = re.compile(r'[0-9a-fA-F]*')


class BlobError(ValueError):
    pass


class Budget:
    """Bytes all decompression for one project may still produce."""

    def __init__(self, limit=MAX_TOTAL_OUTPUT):
        self.limit = limit
        self.used = 0
        self.refused = 0

    def charge(self, size):
        self.used += size
        if self.used > self.limit:
            self.refused += 1
            raise BlobError(f'the project needs more than {self.limit} bytes of decompressed data')


def parse_hex(text):
    if isinstance(text, (bytes, bytearray)):
        return bytes(text)
    stripped = (text or '').strip()
    if not _HEX.fullmatch(stripped) or len(stripped) % 2:
        raise BlobError('a binary value is not valid hex')
    return bytes.fromhex(stripped)


def parse_int(text):
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if -(1 << 63) <= value < (1 << 63) else None


def parse_frames(text):
    """``123`` or ``123|<16 hex>`` (a sub-frame remainder): (frames, remainder or None)."""
    whole, sep, frac = str(text or '').strip().partition('|')
    frames = parse_int(whole)
    if frames is None:
        raise BlobError('a frame count is not an integer')
    if not sep:
        return frames, None
    try:
        raw = bytes.fromhex(frac)
    except ValueError:
        raise BlobError('a sub-frame remainder is not hex') from None
    if len(raw) != 8:
        raise BlobError('a sub-frame remainder is not 8 bytes')
    fraction = struct.unpack('<d', raw)[0]
    if not 0.0 <= fraction < 1.0:
        raise BlobError('a sub-frame remainder is outside [0, 1)')
    return frames, fraction


# --------------------------------------------------------------------------- keyed dict

def _qstring(data, off):
    (n,) = struct.unpack_from('>I', data, off)
    off += 4
    if n == 0xFFFFFFFF:
        return None, off
    if n % 2 or off + n > len(data):
        raise BlobError('keyed dict: bad string length')
    return data[off:off + n].decode('utf-16-be'), off + n


def _qvariant(data, off, depth):
    if depth > 32:
        raise BlobError('keyed dict nested too deeply')
    vtype, _null = struct.unpack_from('>IB', data, off)
    off += 5
    if vtype == 0x0A:
        return _qstring(data, off)
    if vtype == 0x0C:
        (n,) = struct.unpack_from('>I', data, off)
        off += 4
        if n == 0xFFFFFFFF:
            return None, off
        if off + n > len(data):
            raise BlobError('keyed dict: byte array runs past the end')
        return data[off:off + n], off + n
    if vtype == 0x01:
        if off >= len(data):
            raise BlobError('keyed dict: truncated bool')
        return data[off] != 0, off + 1
    fixed = {0x02: '>i', 0x03: '>I', 0x04: '>q', 0x05: '>Q', 0x06: '>d', 0x87: '>d'}
    if vtype in fixed:
        (value,) = struct.unpack_from(fixed[vtype], data, off)
        return value, off + struct.calcsize(fixed[vtype])
    if vtype == 0x07:
        (code,) = struct.unpack_from('>H', data, off)
        return chr(code), off + 2
    if vtype == 0x10:
        if off + 9 > len(data):
            raise BlobError('keyed dict: truncated date')
        return data[off:off + 9], off + 9
    if vtype in (0x08, 0x09, 0x0B):
        (count,) = struct.unpack_from('>I', data, off)
        off += 4
        if count > len(data):
            raise BlobError('keyed dict: implausible element count')
        if vtype == 0x08:
            mapping = {}
            for _ in range(count):
                key, off = _qstring(data, off)
                mapping[key or ''], off = _qvariant(data, off, depth + 1)
            return mapping, off
        items = []
        for _ in range(count):
            if vtype == 0x0B:
                value, off = _qstring(data, off)
            else:
                value, off = _qvariant(data, off, depth + 1)
            items.append(value)
        return items, off
    raise BlobError(f'keyed dict: unknown value type 0x{vtype:x}')


def keyed_dict(data):
    """Decode a keyed dict strictly into an ordered ``{key: value}`` (raises BlobError)."""
    if len(data) < 8:
        raise BlobError('keyed dict too short')
    _header, count = struct.unpack_from('>II', data, 0)
    if count > len(data):
        raise BlobError('keyed dict: implausible entry count')
    off = 8
    out = {}
    try:
        for _ in range(count):
            key, off = _qstring(data, off)
            out[key or ''], off = _qvariant(data, off, 0)
    except (struct.error, UnicodeDecodeError):
        raise BlobError('keyed dict is truncated or malformed') from None
    if off != len(data):
        raise BlobError('keyed dict has trailing bytes')
    return out


def is_keyed_dict(data):
    return len(data) >= 8 and data[:4] == b'\x00\x00\x00\x01'


# --------------------------------------------------------------------------- protobuf

def _varint(data, off):
    result = shift = 0
    while True:
        if off >= len(data):
            raise BlobError('protobuf: truncated varint')
        byte = data[off]
        off += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, off
        shift += 7
        if shift > 63:
            raise BlobError('protobuf: varint too long')


def protobuf(data):
    """Wire-level protobuf: a list of (field number, wire type, int or bytes)."""
    fields = []
    off, size = 0, len(data)
    while off < size:
        key, off = _varint(data, off)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise BlobError('protobuf: field number 0')
        if wire == 0:
            value, off = _varint(data, off)
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if off + width > size:
                raise BlobError('protobuf: truncated fixed field')
            value, off = data[off:off + width], off + width
        elif wire == 2:
            length, off = _varint(data, off)
            if off + length > size:
                raise BlobError('protobuf: field runs past the end')
            value, off = data[off:off + length], off + length
        else:
            raise BlobError(f'protobuf: unsupported wire type {wire}')
        fields.append((number, wire, value))
        if len(fields) > MAX_PROTOBUF_FIELDS:
            raise BlobError('protobuf: too many fields')
    return fields


def pb_all(fields, number, wire=None):
    return [v for n, w, v in fields if n == number and (wire is None or w == wire)]


def pb_first(fields, number, wire=None, default=None):
    found = pb_all(fields, number, wire)
    return found[0] if found else default


def pb_double(value):
    return struct.unpack('<d', value)[0] if isinstance(value, bytes) and len(value) == 8 else None


def pb_text(value):
    if not isinstance(value, bytes):
        return None
    try:
        text = value.decode('utf-8')
    except UnicodeDecodeError:
        return None
    return text if text.isprintable() else None


# --------------------------------------------------------------------------- envelopes

def unwrap(data, budget, max_output=MAX_BLOB_OUTPUT):
    """Protobuf bytes inside a message envelope, decompressing zstd within the budget."""
    payload = None
    compressed = False
    if len(data) >= 9:
        _version, length = struct.unpack_from('>II', data, 0)
        if length == len(data) - 8:
            if data[8] == 0x81 and data[9:13] == zstd.MAGIC:
                payload, compressed = data[9:], True
            elif data[8] == 0x80:
                payload = data[9:]
            else:
                payload = data[8:]
    if payload is None and data[:1] == b'\x81' and data[1:5] == zstd.MAGIC:
        payload, compressed = data[1:], True
    elif payload is None and data[:1] == b'\x80':
        payload = data[1:]
    if payload is None:
        raise BlobError('not a Resolve message envelope')
    if not compressed:
        return payload
    limit = min(max_output, max(0, budget.limit - budget.used))
    try:
        out = zstd.decompress(payload, limit)
    except zstd.ZstdError as error:
        if budget.used + limit >= budget.limit:
            budget.refused += 1
        raise BlobError(str(error)) from None
    budget.charge(len(out))
    return out


def envelope_header(data):
    """The ``[u32][u32]`` header form without a codec byte (nested marker messages)."""
    if len(data) >= 8:
        _version, length = struct.unpack_from('>II', data, 0)
        if length == len(data) - 8:
            return data[8:]
    return None


# --------------------------------------------------------------------------- fixed values

def frame_rate(data):
    if len(data) != 16:
        raise BlobError('frame rate value is not 16 bytes')
    fps = struct.unpack_from('<d', data, 0)[0]
    if not math.isfinite(fps) or fps <= 0 or fps > 1000:
        raise BlobError('frame rate value is implausible')
    return fps


def resolution(data):
    if len(data) != 16:
        raise BlobError('resolution value is not 16 bytes')
    pad_w, width, pad_h, height = struct.unpack('>IIII', data)
    if pad_w or pad_h or not 0 < width <= 32768 or not 0 < height <= 32768:
        raise BlobError('resolution value is implausible')
    return width, height


def extents(data):
    if len(data) != 16:
        raise BlobError('extents value is not 16 bytes')
    start, duration = struct.unpack('<dd', data)
    if not (math.isfinite(start) and math.isfinite(duration)):
        raise BlobError('extents value is not finite')
    return start, duration


def timemap(data):
    """``('identity', 1.0)`` or ``('retimed', speed or None, keyed dict)``."""
    if is_keyed_dict(data):
        kd = keyed_dict(data)
        if kd.get('DbType') != 'Sm2TimeMap':
            raise BlobError('retime map has an unexpected type')
        record, source = kd.get('XMax'), kd.get('LastValidYOffset')
        speed = None
        if isinstance(record, float) and isinstance(source, float):
            if not (math.isfinite(record) and math.isfinite(source)):
                raise BlobError('retime map is not finite')
            if record > 0:
                speed = source / record
        return 'retimed', speed, kd
    if len(data) < 9 or (len(data) - 1) % 8:
        raise BlobError('retime map has an unexpected length')
    return 'identity', 1.0, None


# --------------------------------------------------------------------------- markers

MARKER_COLORS = {2: 'Blue', 4: 'Cyan', 8: 'Green', 16: 'Yellow', 32: 'Red', 64: 'Pink', 128: 'Purple',
                 512: 'Fuchsia', 1024: 'Rose', 2048: 'Lavender', 4096: 'Sky', 8192: 'Mint',
                 16384: 'Lemon', 32768: 'Sand', 65536: 'Cocoa', 131072: 'Cream'}


def markers(payload, limit):
    """Decode a lockable-blob marker list: [{frame, name, note, color, duration}]."""
    out = []
    for number, _wire, holder in protobuf(payload):
        if number != 2 or not isinstance(holder, bytes):
            raise BlobError('marker list has an unexpected field')
        for entry_number, _w, entry in protobuf(holder):
            # Entries are field 1 with the frame as a varint, or (Resolve 21 saves) field 2 with
            # the frame inside a nested message.
            if entry_number not in (1, 2) or not isinstance(entry, bytes):
                raise BlobError('marker list has an unexpected entry')
            parts = protobuf(entry)
            frame = pb_first(parts, 1, 0)
            if frame is None and isinstance(pb_first(parts, 1, 2), bytes):
                frame = pb_first(protobuf(pb_first(parts, 1, 2)), 1, 0)
            body = pb_first(parts, 2, 2)
            if not isinstance(frame, int) or not isinstance(body, bytes):
                raise BlobError('marker without a frame or body')
            inner = envelope_header(body)
            if inner is None:
                raise BlobError('marker body without its header')
            detail = pb_first(protobuf(inner), 1, 2, b'')
            fields = protobuf(detail)
            color = pb_first(fields, 1, 0)
            texts = [s.decode('utf-8', 'replace') for s in pb_all(fields, 3, 2)] + ['', '', '']
            duration = parse_int(texts[1])
            out.append({'frame': frame, 'note': texts[0], 'name': texts[2],
                        'duration': duration if duration is not None and duration >= 0 else None,
                        'color': MARKER_COLORS.get(color) if isinstance(color, int) else None})
            if len(out) > limit:
                raise BlobError('too many markers')
    return out
