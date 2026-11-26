"""Bounded zstd decompression for Resolve project blobs.

Resolve compresses larger blobs with zstd. Python ships a zstd module only from 3.14
(``compression.zstd``), and Splice has no install step, so this module uses, in order:
the standard library module, an installed ``zstandard`` or ``pyzstd`` package, and otherwise
the compact pure-Python decoder below (RFC 8878 frames without dictionaries).

Every path refuses to produce more than ``max_output`` bytes.
"""

import struct

MAGIC = b'\x28\xb5\x2f\xfd'
MAX_BLOCK = 128 * 1024


class ZstdError(ValueError):
    """A frame that is malformed, uses an unsupported feature, or decodes too large."""


def _backend():
    try:
        from compression import zstd  # Python 3.14+
        return 'compression.zstd', zstd
    except ImportError:
        pass
    try:
        import zstandard
        return 'zstandard', zstandard
    except ImportError:
        pass
    try:
        import pyzstd
        return 'pyzstd', pyzstd
    except ImportError:
        return 'python', None


def backend_name():
    return _backend()[0]


def decompress(frame, max_output):
    """Decompress exactly one zstd frame, refusing output over ``max_output`` bytes.

    A frame that ends early (a truncated blob) is refused on every path: its block headers are
    walked here first, and the standard-library and ``pyzstd`` decoders must also report the end of
    the frame. A declared content size must match what was produced.
    """
    frame = bytes(frame)
    if frame[:4] != MAGIC:
        raise ZstdError('not a zstd frame')
    offset, declared, checksum = _header(frame)
    if declared is not None and declared > max_output:
        raise ZstdError(f'zstd frame declares {declared} bytes, over the {max_output} byte limit')
    _frame_end(frame, offset, checksum)
    name, module = _backend()
    if name == 'python':
        return decompress_python(frame, max_output)
    finished = True
    try:
        if name == 'compression.zstd':
            decoder = module.ZstdDecompressor()
            out = decoder.decompress(frame, max_length=max_output + 1)
            finished = decoder.eof
        elif name == 'pyzstd':
            decoder = module.ZstdDecompressor()
            out = decoder.decompress(frame, max_length=max_output + 1)
            finished = decoder.eof
        else:
            reader = module.ZstdDecompressor().stream_reader(frame, read_across_frames=False)
            try:
                out = reader.read(max_output + 1)
            finally:
                reader.close()
    except (module.ZstdError, ValueError, EOFError) as error:
        raise ZstdError(f'zstd: {error}') from None
    if len(out) > max_output:
        raise ZstdError(f'zstd output exceeds {max_output} bytes')
    if not finished:
        raise ZstdError('zstd: the frame is truncated')
    if declared is not None and declared != len(out):
        raise ZstdError('zstd: frame size does not match its header')
    return bytes(out)


def raw_frame(payload):
    """One zstd frame holding ``payload`` in raw (stored) blocks: single segment, the content
    size declared, no checksum. RFC 8878 section 3.1.1: every conforming decoder reads it, and it
    needs no compressor, so the bytes written depend on nothing but ``payload``."""
    payload = bytes(payload)
    size = len(payload)
    if size < 256:
        header = bytes([0x20]) + struct.pack('<B', size)
    elif size < 65536 + 256:
        header = bytes([0x60]) + struct.pack('<H', size - 256)
    elif size <= 0xFFFFFFFF:
        header = bytes([0xA0]) + struct.pack('<I', size)
    else:
        raise ZstdError('payload too large for one zstd frame written here')
    out = bytearray(MAGIC + header)
    position = 0
    while True:
        chunk = payload[position:position + MAX_BLOCK]
        position += len(chunk)
        last = position >= size
        out += ((len(chunk) << 3) | int(last)).to_bytes(3, 'little') + chunk
        if last:
            return bytes(out)


def frame_length(frame):
    """``len(frame)`` when ``frame`` is exactly one complete zstd frame without a dictionary;
    raises ZstdError otherwise (a second frame or trailing bytes included), walking the block
    headers only."""
    frame = bytes(frame)
    if len(frame) >= 5 and frame[:4] == MAGIC and frame[4] & 0x03:
        raise ZstdError('zstd frame uses a dictionary')
    offset, _declared, checksum = _header(frame)
    end = _frame_end(frame, offset, checksum)
    if end != len(frame):
        raise ZstdError(f'{len(frame) - end} byte(s) follow the zstd frame')
    return end


def _frame_end(frame, offset, checksum):
    """Where the frame ends, from its block headers (raises if the frame is cut short)."""
    while True:
        if offset + 3 > len(frame):
            raise ZstdError('zstd: truncated block header')
        header = int.from_bytes(frame[offset:offset + 3], 'little')
        offset += 3
        last, kind, size = header & 1, (header >> 1) & 3, header >> 3
        if kind == 3:
            raise ZstdError('zstd: reserved block type')
        if size > MAX_BLOCK:
            raise ZstdError('zstd: block larger than 128 KiB')
        offset += 1 if kind == 1 else size
        if offset > len(frame):
            raise ZstdError('zstd: truncated block')
        if last:
            break
    if checksum:
        offset += 4
        if offset > len(frame):
            raise ZstdError('zstd: truncated frame checksum')
    return offset


# --------------------------------------------------------------------------- XXH64 (frame checksum)

_P1, _P2, _P3 = 11400714785074694791, 14029467366897019727, 1609587929392839161
_P4, _P5 = 9650029242287828579, 2870177450012600261
_M64 = (1 << 64) - 1


def xxh64(data, seed=0):
    """XXH64 of ``data`` (zstd's frame checksum is its low 32 bits)."""
    data = memoryview(bytes(data))
    n = len(data)
    offset = 0
    M, P1, P2 = _M64, _P1, _P2
    if n >= 32:
        v1, v2, v3, v4 = (seed + P1 + P2) & M, (seed + P2) & M, seed & M, (seed - P1) & M
        limit = n - n % 32
        for a, b, c, d in struct.iter_unpack('<4Q', data[:limit]):
            v1 = (v1 + a * P2) & M
            v1 = (((v1 << 31) | (v1 >> 33)) & M) * P1 & M
            v2 = (v2 + b * P2) & M
            v2 = (((v2 << 31) | (v2 >> 33)) & M) * P1 & M
            v3 = (v3 + c * P2) & M
            v3 = (((v3 << 31) | (v3 >> 33)) & M) * P1 & M
            v4 = (v4 + d * P2) & M
            v4 = (((v4 << 31) | (v4 >> 33)) & M) * P1 & M
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & M
        for v in (v1, v2, v3, v4):
            h = ((h ^ _xround(0, v)) * P1 + _P4) & M
        offset = limit
    else:
        h = (seed + _P5) & M
    h = (h + n) & M
    while offset + 8 <= n:
        (lane,) = struct.unpack_from('<Q', data, offset)
        h ^= _xround(0, lane)
        h = (_rotl(h, 27) * P1 + _P4) & M
        offset += 8
    if offset + 4 <= n:
        (lane,) = struct.unpack_from('<I', data, offset)
        h ^= (lane * P1) & M
        h = (_rotl(h, 23) * P2 + _P3) & M
        offset += 4
    while offset < n:
        h ^= (data[offset] * _P5) & M
        h = (_rotl(h, 11) * P1) & M
        offset += 1
    h ^= h >> 33
    h = (h * P2) & M
    h ^= h >> 29
    h = (h * _P3) & M
    h ^= h >> 32
    return h


def _rotl(value, bits):
    return ((value << bits) | (value >> (64 - bits))) & _M64


def _xround(acc, lane):
    acc = (acc + lane * _P2) & _M64
    return (_rotl(acc, 31) * _P1) & _M64


# --------------------------------------------------------------------------- pure Python

def _header(frame):
    """(offset of the first block, declared content size or None, has checksum)."""
    if len(frame) < 6 or frame[:4] != MAGIC:
        raise ZstdError('not a zstd frame')
    fhd = frame[4]
    if fhd & 0x08:
        raise ZstdError('zstd frame header has its reserved bit set')
    single = (fhd >> 5) & 1
    dict_size = (0, 1, 2, 4)[fhd & 3]
    fcs_size = (1 if single else 0, 2, 4, 8)[fhd >> 6]
    offset = 5 + (0 if single else 1)
    if offset + dict_size + fcs_size > len(frame):
        raise ZstdError('zstd: truncated frame header')
    if dict_size and int.from_bytes(frame[offset:offset + dict_size], 'little'):
        raise ZstdError('zstd frame needs a dictionary')
    offset += dict_size
    size = None
    if fcs_size:
        size = int.from_bytes(frame[offset:offset + fcs_size], 'little') + (256 if fcs_size == 2 else 0)
    return offset + fcs_size, size, bool(fhd & 0x04)


class _Backward:
    """Bits read from the end of a stream towards its start (zstd's backward bitstreams)."""

    def __init__(self, data):
        if not data or not data[-1]:
            raise ZstdError('zstd: bitstream without its end marker')
        self.data = data
        self.pos = len(data) * 8 - (8 - data[-1].bit_length()) - 1

    def read(self, count):
        if not count:
            return 0
        self.pos -= count
        start = self.pos
        if start < 0:
            # Reading before the start yields zeros (as the reference decoder does).
            value = self._window(0, count + start) << -start if count + start > 0 else 0
            return value & ((1 << count) - 1)
        return self._window(start, count)

    def peek(self, count):
        value = self.read(count)
        self.pos += count
        return value

    def _window(self, start, count):
        first, last = start >> 3, (start + count + 7) >> 3
        return (int.from_bytes(self.data[first:last], 'little') >> (start & 7)) & ((1 << count) - 1)


def _read_counts(data, max_symbol, max_log):
    """FSE table description: (normalised counts, accuracy log, bytes used)."""
    bits = int.from_bytes(data[:512], 'little')
    available = min(len(data), 512) * 8
    pos = 0

    def take(count):
        nonlocal pos
        if pos + count > available:
            raise ZstdError('zstd: truncated FSE table description')
        value = (bits >> pos) & ((1 << count) - 1)
        pos += count
        return value

    log = take(4) + 5
    if log > max_log:
        raise ZstdError(f'zstd: FSE accuracy log {log} over {max_log}')
    remaining = (1 << log) + 1
    threshold = 1 << log
    width = log + 1
    counts = []
    previous_zero = False
    while remaining > 1 and len(counts) <= max_symbol:
        if previous_zero:
            repeat = take(2)
            while repeat == 3:
                counts.extend([0] * 3)
                repeat = take(2)
            counts.extend([0] * repeat)
            if len(counts) > max_symbol:
                break
        limit = 2 * threshold - 1 - remaining
        low = (bits >> pos) & (threshold - 1)
        if low < limit:
            take(width - 1)
            count = low
        else:
            count = take(width)
            if count >= threshold:
                count -= limit
        count -= 1
        remaining -= abs(count)
        counts.append(count)
        previous_zero = count == 0
        while remaining < threshold:
            width -= 1
            threshold >>= 1
    if remaining != 1 or len(counts) > max_symbol + 1:
        raise ZstdError('zstd: invalid FSE table description')
    return counts, log, (pos + 7) >> 3


def _fse_table(counts, log):
    """Decoding table: list of (symbol, bits to read, base of the next state)."""
    size = 1 << log
    symbols = [0] * size
    high = size - 1
    following = [0] * len(counts)
    for symbol, count in enumerate(counts):
        if count == -1:
            symbols[high] = symbol
            high -= 1
            following[symbol] = 1
        else:
            following[symbol] = count
    step = (size >> 1) + (size >> 3) + 3
    position = 0
    for symbol, count in enumerate(counts):
        for _ in range(max(count, 0)):
            symbols[position] = symbol
            position = (position + step) & (size - 1)
            while position > high:
                position = (position + step) & (size - 1)
    if position:
        raise ZstdError('zstd: FSE counts do not fill the table')
    table = []
    for symbol in symbols:
        state = following[symbol]
        following[symbol] += 1
        width = log - (state.bit_length() - 1)
        table.append((symbol, width, (state << width) - size))
    return table, log


def _rle_table(symbol):
    return [(symbol, 0, 0)], 0


_LL_DEFAULT = ([4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
                -1, -1, -1, -1], 6)
_ML_DEFAULT = ([1, 4, 3, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
                1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1], 6)
_OF_DEFAULT = ([1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1], 5)
_LL_BASE = list(range(16)) + [16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048, 4096,
                              8192, 16384, 32768, 65536]
_LL_BITS = [0] * 16 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
_ML_BASE = list(range(3, 35)) + [35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051,
                                 4099, 8195, 16387, 32771, 65539]
_ML_BITS = [0] * 32 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
_DEFAULTS = {}


def _default(name):
    if name not in _DEFAULTS:
        _DEFAULTS[name] = _fse_table(*{'ll': _LL_DEFAULT, 'ml': _ML_DEFAULT, 'of': _OF_DEFAULT}[name])
    return _DEFAULTS[name]


class _Frame:
    def __init__(self, max_output):
        self.max_output = max_output
        self.out = bytearray()
        self.huffman = None
        self.tables = {'ll': None, 'of': None, 'ml': None}
        self.repeat = [1, 4, 8]

    # -- literals ----------------------------------------------------------------------
    def literals(self, block):
        if not block:
            raise ZstdError('zstd: empty compressed block')
        kind = block[0] & 3
        form = (block[0] >> 2) & 3
        if kind in (0, 1):
            if len(block) < (1, 2, 1, 3)[form]:
                raise ZstdError('zstd: truncated literals header')
            if form in (0, 2):
                size, used = block[0] >> 3, 1
            elif form == 1:
                size, used = (block[0] >> 4) + (block[1] << 4), 2
            else:
                size, used = (block[0] >> 4) + (block[1] << 4) + (block[2] << 12), 3
            if size > MAX_BLOCK:
                raise ZstdError('zstd: literals larger than a block')
            if kind == 0:
                if used + size > len(block):
                    raise ZstdError('zstd: truncated raw literals')
                return bytes(block[used:used + size]), used + size
            if used >= len(block):
                raise ZstdError('zstd: truncated RLE literals')
            return bytes(block[used:used + 1]) * size, used + 1
        header_size = (3, 3, 4, 5)[form]
        if header_size > len(block):
            raise ZstdError('zstd: truncated literals header')
        value = int.from_bytes(block[:header_size], 'little') >> 4
        width = (10, 10, 14, 18)[form]
        regenerated = value & ((1 << width) - 1)
        compressed = value >> width
        streams = 1 if form == 0 else 4
        if regenerated > MAX_BLOCK:
            raise ZstdError('zstd: literals larger than a block')
        end = header_size + compressed
        if end > len(block):
            raise ZstdError('zstd: truncated compressed literals')
        data = block[header_size:end]
        if kind == 2:
            self.huffman, used = _huffman_table(data)
            data = data[used:]
        elif self.huffman is None:
            raise ZstdError('zstd: treeless literals without a previous Huffman table')
        return _huffman_decode(self.huffman, data, regenerated, streams), end

    # -- sequences -----------------------------------------------------------------------
    def sequences(self, data):
        if not data:
            raise ZstdError('zstd: missing sequences header')
        count = data[0]
        used = 1
        if count >= 128:
            if count == 255:
                if len(data) < 3:
                    raise ZstdError('zstd: truncated sequences header')
                count = data[1] + (data[2] << 8) + 0x7F00
                used = 3
            else:
                if len(data) < 2:
                    raise ZstdError('zstd: truncated sequences header')
                count = ((count - 128) << 8) + data[1]
                used = 2
        if count == 0:
            if used != len(data):
                raise ZstdError('zstd: data after an empty sequences section')
            return []
        if used >= len(data):
            raise ZstdError('zstd: truncated sequences header')
        modes = data[used]
        used += 1
        if modes & 3:
            raise ZstdError('zstd: reserved sequence mode bits set')
        tables = {}
        for name, shift, max_symbol, max_log in (('ll', 6, 35, 9), ('of', 4, 31, 8), ('ml', 2, 52, 9)):
            mode = (modes >> shift) & 3
            if mode == 0:
                table = _default(name)
            elif mode == 1:
                if used >= len(data):
                    raise ZstdError('zstd: truncated RLE sequence table')
                if data[used] > max_symbol:
                    raise ZstdError('zstd: RLE sequence symbol out of range')
                table = _rle_table(data[used])
                used += 1
            elif mode == 2:
                counts, log, size = _read_counts(data[used:], max_symbol, max_log)
                table = _fse_table(counts, log)
                used += size
            else:
                table = self.tables[name]
                if table is None:
                    raise ZstdError('zstd: repeated sequence table without a previous one')
            tables[name] = self.tables[name] = table
        stream = _Backward(data[used:])
        ll_table, ll_log = tables['ll']
        of_table, of_log = tables['of']
        ml_table, ml_log = tables['ml']
        ll_state = stream.read(ll_log)
        of_state = stream.read(of_log)
        ml_state = stream.read(ml_log)
        result = []
        for index in range(count):
            of_code = of_table[of_state][0]
            ll_code = ll_table[ll_state][0]
            ml_code = ml_table[ml_state][0]
            if of_code > 31 or ll_code > 35 or ml_code > 52:
                raise ZstdError('zstd: sequence code out of range')
            offset_value = (1 << of_code) + stream.read(of_code)
            match = _ML_BASE[ml_code] + stream.read(_ML_BITS[ml_code])
            literal = _LL_BASE[ll_code] + stream.read(_LL_BITS[ll_code])
            result.append((literal, match, offset_value))
            if index + 1 < count:
                symbol, width, base = ll_table[ll_state]
                ll_state = base + stream.read(width)
                symbol, width, base = ml_table[ml_state]
                ml_state = base + stream.read(width)
                symbol, width, base = of_table[of_state]
                of_state = base + stream.read(width)
            if stream.pos < 0:
                raise ZstdError('zstd: sequence bitstream overread')
        if stream.pos != 0:
            raise ZstdError('zstd: sequence bitstream not fully consumed')
        return result

    def block(self, block):
        literals, used = self.literals(block)
        sequences = self.sequences(block[used:])
        out = self.out
        start = len(out)
        position = 0
        repeat = self.repeat
        for literal, match, offset_value in sequences:
            if position + literal > len(literals):
                raise ZstdError('zstd: sequence copies more literals than exist')
            out += literals[position:position + literal]
            position += literal
            if offset_value > 3:
                offset = offset_value - 3
                repeat[:] = [offset, repeat[0], repeat[1]]
            else:
                index = offset_value + (1 if literal == 0 else 0)
                if index == 1:
                    offset = repeat[0]
                elif index == 2:
                    offset = repeat[1]
                    repeat[:] = [offset, repeat[0], repeat[2]]
                elif index == 3:
                    offset = repeat[2]
                    repeat[:] = [offset, repeat[0], repeat[1]]
                else:
                    offset = repeat[0] - 1
                    repeat[:] = [offset, repeat[0], repeat[1]]
            if offset <= 0 or offset > len(out):
                raise ZstdError('zstd: match offset before the start of the output')
            if len(out) - start + match > MAX_BLOCK or len(out) + match > self.max_output:
                raise ZstdError('zstd: block output too large')
            source = len(out) - offset
            if offset >= match:
                out += out[source:source + match]
            else:
                while match:
                    chunk = min(match, offset)
                    out += out[source:source + chunk]
                    source += chunk
                    match -= chunk
        out += literals[position:]
        if len(out) - start > MAX_BLOCK:
            raise ZstdError('zstd: block output too large')


def _huffman_table(data):
    """Huffman tree description: ((table, max bits), bytes used)."""
    if not data:
        raise ZstdError('zstd: missing Huffman tree description')
    header = data[0]
    if header < 128:
        size = header
        if 1 + size > len(data) or size == 0:
            raise ZstdError('zstd: truncated Huffman weights')
        counts, log, used = _read_counts(data[1:1 + size], 255, 6)
        table, log = _fse_table(counts, log)
        stream = _Backward(data[1 + used:1 + size])
        weights = []
        state1 = stream.read(log)
        state2 = stream.read(log)
        while True:
            if len(weights) > 255:
                raise ZstdError('zstd: too many Huffman weights')
            symbol, width, base = table[state1]
            weights.append(symbol)
            state1 = base + stream.read(width)
            if stream.pos < 0:
                weights.append(table[state2][0])
                break
            symbol, width, base = table[state2]
            weights.append(symbol)
            state2 = base + stream.read(width)
            if stream.pos < 0:
                weights.append(table[state1][0])
                break
        used = 1 + size
    else:
        count = header - 127
        used = 1 + (count + 1) // 2
        if used > len(data):
            raise ZstdError('zstd: truncated Huffman weights')
        weights = []
        for byte in data[1:used]:
            weights.extend((byte >> 4, byte & 15))
        weights = weights[:count]
    if len(weights) > 255 or any(w > 11 for w in weights):
        raise ZstdError('zstd: invalid Huffman weights')
    total = sum(1 << (w - 1) for w in weights if w)
    if not total:
        raise ZstdError('zstd: empty Huffman table')
    max_bits = total.bit_length()
    remainder = (1 << max_bits) - total
    if remainder & (remainder - 1) or max_bits > 11:
        raise ZstdError('zstd: invalid Huffman weights')
    weights.append(remainder.bit_length())
    size = 1 << max_bits
    table = [None] * size
    position = 0
    for weight in range(1, max_bits + 1):
        for symbol, w in enumerate(weights):
            if w != weight:
                continue
            span = 1 << (w - 1)
            entry = (symbol, max_bits + 1 - w)
            table[position:position + span] = [entry] * span
            position += span
    if position != size:
        raise ZstdError('zstd: invalid Huffman weights')
    return (table, max_bits), used


def _huffman_decode(huffman, data, regenerated, streams):
    table, max_bits = huffman
    if streams == 1:
        parts, sizes = [data], [regenerated]
    else:
        if len(data) < 6:
            raise ZstdError('zstd: truncated Huffman jump table')
        lengths = [int.from_bytes(data[i:i + 2], 'little') for i in (0, 2, 4)]
        lengths.append(len(data) - 6 - sum(lengths))
        if lengths[3] < 0:
            raise ZstdError('zstd: invalid Huffman jump table')
        parts, offset = [], 6
        for length in lengths:
            parts.append(data[offset:offset + length])
            offset += length
        each = (regenerated + 3) // 4
        sizes = [each, each, each, regenerated - 3 * each]
        if sizes[3] < 0:
            raise ZstdError('zstd: invalid Huffman stream sizes')
    out = bytearray()
    for part, size in zip(parts, sizes):
        stream = _Backward(part)
        for _ in range(size):
            symbol, width = table[stream.peek(max_bits)]
            out.append(symbol)
            stream.pos -= width
        if stream.pos != 0:
            raise ZstdError('zstd: Huffman stream not fully consumed')
    return bytes(out)


def decompress_python(frame, max_output):
    """The pure-Python decoder (used when no zstd library is available).

    Any malformed input raises ZstdError: a slip inside the decoder (an index or unpack past the
    data) is reported as a damaged frame rather than escaping as another exception.
    """
    try:
        return _decompress_python(frame, max_output)
    except ZstdError:
        raise
    except (IndexError, KeyError, TypeError, ValueError, OverflowError, struct.error) as error:
        raise ZstdError(f'zstd: the frame is damaged ({type(error).__name__})') from None


def _decompress_python(frame, max_output):
    frame = memoryview(bytes(frame))
    offset, declared, checksum = _header(frame)
    if declared is not None and declared > max_output:
        raise ZstdError(f'zstd frame declares {declared} bytes, over the {max_output} byte limit')
    state = _Frame(max_output)
    while True:
        if offset + 3 > len(frame):
            raise ZstdError('zstd: truncated block header')
        header = int.from_bytes(frame[offset:offset + 3], 'little')
        offset += 3
        last, kind, size = header & 1, (header >> 1) & 3, header >> 3
        if kind == 3:
            raise ZstdError('zstd: reserved block type')
        if kind == 1:
            if offset >= len(frame):
                raise ZstdError('zstd: truncated RLE block')
            if size > MAX_BLOCK or len(state.out) + size > max_output:
                raise ZstdError(f'zstd output exceeds {max_output} bytes')
            state.out += bytes(frame[offset:offset + 1]) * size
            offset += 1
        else:
            if size > MAX_BLOCK or offset + size > len(frame):
                raise ZstdError('zstd: truncated or oversized block')
            if kind == 0:
                if len(state.out) + size > max_output:
                    raise ZstdError(f'zstd output exceeds {max_output} bytes')
                state.out += frame[offset:offset + size]
            else:
                state.block(bytes(frame[offset:offset + size]))
            offset += size
        if len(state.out) > max_output:
            raise ZstdError(f'zstd output exceeds {max_output} bytes')
        if last:
            break
    if declared is not None and declared != len(state.out):
        raise ZstdError('zstd: frame size does not match its header')
    if checksum:
        if offset + 4 > len(frame):
            raise ZstdError('zstd: truncated frame checksum')
        stored = int.from_bytes(frame[offset:offset + 4], 'little')
        if stored != xxh64(state.out) & 0xFFFFFFFF:
            raise ZstdError('zstd: frame checksum does not match (the data is damaged)')
    return bytes(state.out)
