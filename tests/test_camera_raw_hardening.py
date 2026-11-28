"""Camera RAW hardening regressions (PR #17 validation round 1).

1. DNG header memory bombs: only parsed tags are decoded, counts are capped, a per-file budget holds.
2. Malformed headers (type-confused, multi-valued, overflowing tags) raise RawError, never a raw
   Python error, and one bad file in a batch link does not stop the good ones.
3. Vendor decoders by name (REDline, art-cmd, dcraw_emu): the exported frames are exactly the cut's
   source frames, cancel kills the tool and its children quickly, and each tool gets every setting
   it documents. Settings a tool cannot take are reported, disabled and disclosed.
4. Look metadata the file does not record (ISO, white balance, camera) is flagged, not invented.
5. Decoder-path identity (no interpreters, no /proc), cached library lookups, MXF sound-only files,
   and free-space checks before 16-bit intermediates and TIFF staging.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import resource
import socket
import struct
import subprocess
import sys
import threading
import time
import tracemalloc
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import media_links
import media_proxy
import raw_bayer
import raw_decode
import render_movie
import server
from interchange.api import handle as interchange_handle
from interchange.media import probe_file
from tests import raw_fixtures
from tests.test_camera_raw import _IsolatedDataDirMixin, _children_of_this_process

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A small TIFF/DNG writer for hostile headers
# ---------------------------------------------------------------------------

_PACK = {1: 'B', 2: 's', 3: 'H', 4: 'I', 5: 'II', 6: 'b', 7: 'B', 8: 'h', 9: 'i', 10: 'ii', 11: 'f', 12: 'd'}
_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


class Blob:
    """An entry declaring ``count`` values stored at the shared blob."""

    def __init__(self, count):
        self.count = count


class SubIFDs:
    """IFD0's SubIFDs entry: offsets of every IFD after the first."""


class IFDRef:
    """The offset of ``ifds[index]`` (an EXIF IFD pointer, say)."""

    def __init__(self, index):
        self.index = index


def _payload(typ, values):
    if typ == 2:
        return values if isinstance(values, bytes) else values.encode() + b'\0'
    values = list(values) if isinstance(values, (list, tuple)) else [values]
    if typ in (5, 10):
        flat = [v for pair in values for v in pair]
        return struct.pack('<' + _PACK[typ][0] * len(flat), *flat), len(values)
    return struct.pack('<' + _PACK[typ] * len(values), *values), len(values)


def build_tiff(ifds, *, pixels=b'', blob=b''):
    """``ifds``: lists of ``(tag, type, values)``; values may be ``Blob(n)`` or ``SubIFDs()``.
    Layout: header | pixels (at 8) | blob | IFD0 + data | IFD1 + data ..."""
    pixel_off = 8
    blob_off = pixel_off + len(pixels) + (len(pixels) & 1)
    cursor = blob_off + len(blob) + (len(blob) & 1)

    def encoded(entries, sub_offsets, all_offsets):
        out = []
        for tag, typ, values in sorted(entries, key=lambda e: e[0]):
            if isinstance(values, Blob):
                out.append((tag, typ, values.count, None, blob_off))
                continue
            if isinstance(values, SubIFDs):
                values = sub_offsets or [0]
            if isinstance(values, IFDRef):
                values = all_offsets[values.index]
            if typ == 2:
                data = _payload(2, values)
                out.append((tag, typ, len(data), data, None))
            else:
                data, count = _payload(typ, values)
                out.append((tag, typ, count, data, None))
        return out

    # Pass 1: sizes (an IFD's size does not depend on the offsets it holds).
    sizes = []
    for entries in ifds:
        rows = encoded(entries, [0] * max(1, len(ifds) - 1), [0] * len(ifds))
        extra = sum(len(d) + (len(d) & 1) for _, _, _, d, _ in rows if d is not None and len(d) > 4)
        sizes.append(2 + len(rows) * 12 + 4 + extra)
    offsets = []
    for size in sizes:
        offsets.append(cursor)
        cursor += size + (size & 1)
    out = bytearray(b'II' + struct.pack('<HI', 42, offsets[0]) + pixels)
    out += b'\0' * (blob_off - len(out)) + blob
    for index, entries in enumerate(ifds):
        out += b'\0' * (offsets[index] - len(out))
        rows = encoded(entries, offsets[1:] if index == 0 else None, offsets)
        data_at = offsets[index] + 2 + len(rows) * 12 + 4
        head, tail = bytearray(struct.pack('<H', len(rows))), bytearray()
        for tag, typ, count, data, at in rows:
            if data is None:
                head += struct.pack('<HHII', tag, typ, count, at)
            elif len(data) <= 4:
                head += struct.pack('<HHI', tag, typ, count) + data.ljust(4, b'\0')
            else:
                head += struct.pack('<HHII', tag, typ, count, data_at + len(tail))
                tail += data + (b'\0' if len(data) & 1 else b'')
        head += struct.pack('<I', 0)
        out += head + tail
    return bytes(out)


W, H = 8, 6


def image_tags(*, omit=(), **extra):
    """A valid uncompressed 16-bit RGGB CFA image IFD (``W`` x ``H``)."""
    tags = {
        254: (4, 0), 256: (4, W), 257: (4, H), 258: (3, 16), 259: (3, 1), 262: (3, 32803),
        273: (4, 8), 277: (3, 1), 278: (4, H), 279: (4, W * H * 2), 33421: (3, (2, 2)),
        33422: (1, (0, 1, 1, 2)), 50706: (1, (1, 4, 0, 0)), 50717: (4, 65535),
        50721: (10, tuple((1, 1) if i % 4 == 0 else (0, 1) for i in range(9))),
    }
    for tag in omit:
        tags.pop(tag, None)
    for key, value in extra.items():
        tags[int(key.lstrip('t'))] = value
    return [(tag, typ, values) for tag, (typ, values) in tags.items()]


PIXELS = struct.pack('<' + 'H' * (W * H), *[20000 + 97 * i for i in range(W * H)])


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# B1: memory bombs
# ---------------------------------------------------------------------------

_PROBE_CHILD = r'''
import json, resource, sys, time
sys.path[:0] = [sys.argv[2], sys.argv[2] + "/tests"]
import raw_decode
start = time.monotonic()
try:
    record = raw_decode.probe_raw(sys.argv[1])
    result = {"ok": True, "width": record["width"]}
except raw_decode.RawError as error:
    result = {"ok": False, "error": str(error)}
result["seconds"] = time.monotonic() - start
result["maxrssMB"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(json.dumps(result))
'''


class TestDngHeaderBombs(_IsolatedDataDirMixin, unittest.TestCase):
    def _probe_in_child(self, path: Path) -> dict:
        env = {**os.environ, 'SPLICE_DATA_DIR': str(self.data_dir)}
        done = subprocess.run([sys.executable, '-c', _PROBE_CHILD, str(path), str(ROOT)], capture_output=True,
                              text=True, timeout=120, env=env)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_1024_tags_of_a_million_doubles_sharing_one_blob_stay_small_and_fast(self):
        # The validator's bomb: every entry declares 1,000,000 DOUBLEs at the same 8 MB blob. The
        # old parser unpacked each into a tuple (about 40 GB at 1024 tags).
        blob = bytes(8 * 1_000_000)
        image = image_tags()
        bombs = [(60000 - 1024 + i, 12, Blob(1_000_000)) for i in range(1024 - len(image))]
        dng = write(self.tmp / 'bomb' / 'B001_000001.dng', build_tiff([image + bombs], pixels=PIXELS, blob=blob))
        self.assertGreater(dng.stat().st_size, 7 * 1024 * 1024)
        result = self._probe_in_child(dng)
        self.assertTrue(result['ok'], result)  # the tags Splice never reads are skipped, not refused
        self.assertEqual(result['width'], W)
        self.assertLess(result['maxrssMB'], 200, result)
        self.assertLess(result['seconds'], 5, result)

        # Bombs in tags Splice does read are refused before any value is unpacked.
        parsed = [(tag, 12, Blob(1_000_000)) for tag in (50721, 50722, 50728, 50714, 50717, 33422)]
        dng = write(self.tmp / 'bomb2' / 'B002_000001.dng',
                    build_tiff([image_tags(omit=(50721, 50714, 50717, 33422)) + parsed], pixels=PIXELS, blob=blob))
        result = self._probe_in_child(dng)
        self.assertFalse(result['ok'])
        self.assertIn('declares 1000000 values', result['error'])
        self.assertLess(result['maxrssMB'], 200, result)
        self.assertLess(result['seconds'], 5, result)

        tracemalloc.start()
        try:
            with self.assertRaises(raw_bayer.RawError):
                raw_bayer.parse_dng(dng)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 64 * 1024 * 1024, f'parse peaked at {peak / 2**20:.0f} MB')

    def test_strip_tables_across_many_ifds_hit_the_per_file_budget(self):
        # Each sub-IFD is within the 65,536-value cap per tag; together they pass 1M decoded values.
        blob = bytes(4 * 65536)
        layout = [(tag, 4, Blob(65536)) for tag in (273, 279, 324, 325)]
        subs = [[(254, 4, 1), (256, 4, W), (257, 4, H)] + layout for _ in range(5)]
        ifd0 = image_tags() + [(330, 4, SubIFDs())]
        dng = write(self.tmp / 'budget' / 'C001_000001.dng', build_tiff([ifd0] + subs, pixels=PIXELS, blob=blob))
        with self.assertRaises(raw_bayer.RawError) as ctx:
            raw_bayer.parse_dng(dng)
        self.assertIn(f'more than {raw_bayer.MAX_DECODED_VALUES} tag values', str(ctx.exception))
        result = self._probe_in_child(dng)
        self.assertFalse(result['ok'])
        self.assertLess(result['maxrssMB'], 200, result)

    def test_ifd_chains_and_strip_tables_are_bounded(self):
        # More IFDs than MAX_IFDS (each a valid tiny IFD): refused, not walked.
        subs = [[(254, 4, 1), (256, 4, 2), (257, 4, 2)] for _ in range(raw_bayer.MAX_IFDS + 2)]
        dng = write(self.tmp / 'chain.dng', build_tiff([image_tags() + [(330, 4, SubIFDs())]] + subs, pixels=PIXELS))
        with self.assertRaises(raw_bayer.RawError):
            raw_bayer.parse_dng(dng)
        # More strips than rows.
        dng = write(self.tmp / 'strips.dng', build_tiff([image_tags(t273=(4, tuple([8] * (H + 1))))], pixels=PIXELS))
        with self.assertRaisesRegex(raw_bayer.RawError, 'more strips than image rows'):
            raw_bayer.parse_dng(dng)


# ---------------------------------------------------------------------------
# B2: malformed headers
# ---------------------------------------------------------------------------

def _only_raw_errors(test, path: Path):
    """probe_raw and decode either succeed or raise RawError: nothing else escapes."""
    raw_decode._probe_cache.clear()
    for label, call in (('probe', lambda: raw_decode.probe_raw(path)),
                        ('parse', lambda: raw_bayer.parse_dng(path)),
                        ('decode', lambda: raw_bayer.decode_dng_frame(path, {'decodeUsing': 'camera'}, scale=2)),
                        ('decode clip', lambda: raw_bayer.decode_dng_frame(
                            path, {'decodeUsing': 'clip', 'exposure': 4.0, 'iso': 102400}, scale=1))):
        try:
            call()
        except raw_bayer.RawError:
            pass
        except Exception as error:  # noqa: BLE001 - the point of the test
            test.fail(f'{path.name} {label}: {type(error).__name__}: {error}')


class TestMalformedRawHeaders(_IsolatedDataDirMixin, unittest.TestCase):
    def test_validator_fuzz_repros_raise_raw_errors(self):
        cases = {
            # fuzz-repro-142: RowsPerStrip turned into a multi-value table -> int(tuple) TypeError
            'rows-per-strip-pair': image_tags(t278=(4, (H, H))),
            'rows-per-strip-table': image_tags(t278=(4, tuple(range(210)))),
            # "retag 256 type5 cnt2": ImageWidth as two RATIONALs -> int(list) TypeError
            'width-two-rationals': image_tags(t256=(5, ((1, 1), (2, 1)))),
            'width-rational-zero-denominator': image_tags(t256=(5, ((7, 0),))),
            'width-ascii': image_tags(t256=(2, 'abc')),
            'bits-ascii': image_tags(t258=(2, 'sixteen')),
            'compression-float-nan': image_tags(t259=(11, float('nan'))),
            'cfa-not-bayer': image_tags(t33422=(1, (3, 3, 3, 3))),
            'cfa-rational': image_tags(t33422=(5, ((0, 1), (1, 1), (1, 1), (2, 1)))),
            'tile-huge': image_tags(t322=(4, 65535), t323=(4, 65535), t324=(4, 8), t325=(4, 0xFFFFFF), omit=(273, 279, 278)),
            'strip-offsets-rational': image_tags(t273=(5, ((8, 1),))),
            'white-nan': image_tags(t50717=(11, float('nan'))),
            'black-inf': image_tags(t50714=(12, float('inf'))),
            'as-shot-zero': image_tags(t50728=(5, ((0, 1), (0, 0), (1, 0)))),
            'matrix-huge': image_tags(t50721=(12, tuple([1e308] * 9))),
            'framerate-zero-den': image_tags(t51044=(10, ((24, 0),))),
            'illuminant-pair': image_tags(t50778=(3, (17, 21))),
            'timecode-shorts': image_tags(t51043=(3, (0xFFFF,) * 8)),
        }
        for label, entries in cases.items():
            with self.subTest(label):
                _only_raw_errors(self, write(self.tmp / label / f'{label}_000001.dng', build_tiff([entries], pixels=PIXELS)))
        with self.assertRaisesRegex(raw_bayer.RawError, 'holds 2 values where one is expected'):
            raw_bayer.parse_dng(self.tmp / 'rows-per-strip-pair' / 'rows-per-strip-pair_000001.dng')

    def test_hostile_baseline_exposure_is_clamped_not_overflowed(self):
        # fuzz-repro-2668: 2.0 ** BaselineExposure overflowed in decode_plane.
        for value in (1e308, -1e308, 1100.0):
            dng = write(self.tmp / f'be{value:g}.dng', build_tiff([image_tags(t50730=(12, value))], pixels=PIXELS))
            meta = raw_bayer.parse_dng(dng)
            self.assertEqual(abs(meta['baselineExposure']), raw_bayer.MAX_BASELINE_EXPOSURE)
            width, height, rgb = raw_bayer.decode_dng_frame(dng, {'decodeUsing': 'clip', 'exposure': 4.0}, scale=2)
            self.assertEqual(len(rgb), width * height * 6)
        # Even a meta dict that bypassed parsing cannot overflow the gain.
        meta = {**raw_bayer.parse_dng(dng), 'baselineExposure': 1e308}
        plane = raw_bayer._read_bayer_plane(dng.read_bytes(), meta)
        raw_bayer.decode_plane(plane, meta, {'decodeUsing': 'camera'}, scale=2)

    def test_seeded_header_mutations_and_truncations_only_raise_raw_error(self):
        import random
        rng = random.Random(1617)
        base = raw_fixtures.write_synthetic_dng(self.tmp / 'base' / 'F001_000001.dng', width=16, height=12)
        data = base.read_bytes()
        ifd_end = 8 + 2 + struct.unpack_from('<H', data, 8)[0] * 12 + 4 + 400
        work = self.tmp / 'mut'
        work.mkdir()
        target = work / 'M001_000001.dng'
        for n in list(range(0, 300, 3)) + list(range(300, len(data), 37)):
            target.write_bytes(data[:n])
            _only_raw_errors(self, target)
        for _ in range(400):
            mutated = bytearray(data)
            for _ in range(rng.randint(1, 8)):
                pos = rng.randrange(0, min(len(mutated), ifd_end))
                mutated[pos] = rng.choice([0, 0xFF, rng.randrange(256), mutated[pos] ^ (1 << rng.randrange(8))])
            target.write_bytes(bytes(mutated))
            os.utime(target, ns=(time.time_ns(), time.time_ns()))
            _only_raw_errors(self, target)

    def test_one_malformed_file_does_not_fail_a_batch_link(self):
        bad = write(self.tmp / 'Bad Roll' / 'X001_000001.dng', build_tiff([image_tags(t256=(5, ((1, 1), (2, 1))))], pixels=PIXELS))
        good = raw_fixtures.write_synthetic_dng(self.tmp / 'Good Roll' / 'G001_000001.dng')
        results = media_links.link_many([str(bad), str(good)], validate=media_proxy.decodable)
        self.assertIn('error', results[0])
        self.assertIn('X001_000001.dng', results[0]['error'])
        self.assertNotIn('int()', results[0]['error'])
        self.assertIn('src', results[1])

        def explode(path, name):
            if name.startswith('X'):
                raise TypeError("'NoneType' object is not subscriptable")
            return media_proxy.decodable(path, name)
        results = media_links.link_many([str(bad), str(good)], validate=explode)
        self.assertEqual(results[0], {'error': 'X001_000001.dng could not be read (TypeError)'})
        self.assertIn('src', results[1])
        with self.assertRaisesRegex(ValueError, 'Could not probe RAW'):
            probe_file(bad)

    def test_link_endpoint_reports_the_bad_file_and_links_the_good_one(self):
        bad = write(self.tmp / 'Bad' / 'X002_000001.dng', build_tiff([image_tags(t278=(4, (H, H)))], pixels=PIXELS))
        good = raw_fixtures.write_synthetic_dng(self.tmp / 'Good' / 'G002_000001.dng')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        httpd = server.create_server(port)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
            conn.request('POST', '/api/media/link', body=json.dumps({'paths': [str(bad), str(good)]}).encode(),
                         headers={'Host': f'127.0.0.1:{port}', 'Cookie': f'{httpd.cookie_name}={httpd.session}',
                                  server.TOKEN_HEADER: httpd.page_token, 'Origin': f'http://127.0.0.1:{port}',
                                  'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json'})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(response.status, 200, body)
        files = body['files']
        self.assertIn('error', files[0])
        self.assertIn('X002_000001.dng', files[0]['error'])
        self.assertIn('src', files[1])


# ---------------------------------------------------------------------------
# B3: REDline / art-cmd / dcraw_emu
# ---------------------------------------------------------------------------

_MOCK_TOOL = r'''#!{python}
"""Test double for a vendor RAW tool: logs its argv, writes 16-bit RGB TIFFs whose grey level
encodes the source frame (1000 + 2500 * frame)."""
import json, os, re, struct, subprocess, sys, time
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
def opt(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default
entry = {{'tool': name, 'argv': args, 'pid': os.getpid()}}
if name == 'art-cmd' and opt('--parameters'):
    with open(opt('--parameters')) as fh:
        entry['parameters'] = json.load(fh)
with open(os.environ['SPLICE_MOCK_TOOL_LOG'], 'a') as fh:
    fh.write(json.dumps(entry) + '\n')
if os.environ.get('SPLICE_MOCK_TOOL_PIDS'):
    child = subprocess.Popen(['sleep', '30'])
    with open(os.environ['SPLICE_MOCK_TOOL_PIDS'], 'w') as fh:
        fh.write(f'{{os.getpid()}} {{child.pid}}')
slow = float(os.environ.get('SPLICE_MOCK_TOOL_SLOW', '0'))
total = int(os.environ.get('SPLICE_MOCK_TOOL_TOTAL', '24'))
w, h = 64, 48
def tiff(path, frame):
    v = min(65535, 1000 + 2500 * frame)
    data = struct.pack('<HHH', v, v, v) * (w * h)
    tags = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, 3, None), (259, 3, 1, 1), (262, 3, 1, 2),
            (273, 4, 1, None), (277, 3, 1, 3), (278, 4, 1, h), (279, 4, 1, len(data))]
    bps_off = 8 + 2 + len(tags) * 12 + 4
    out = bytearray(b'II' + struct.pack('<HIH', 42, 8, len(tags)))
    for tag, typ, cnt, val in tags:
        if tag == 258: out += struct.pack('<HHII', tag, typ, cnt, bps_off)
        elif tag == 273: out += struct.pack('<HHII', tag, typ, cnt, bps_off + 6)
        elif typ == 3: out += struct.pack('<HHIHH', tag, typ, cnt, val, 0)
        else: out += struct.pack('<HHII', tag, typ, cnt, val)
    out += struct.pack('<I', 0) + struct.pack('<HHH', 16, 16, 16) + data
    with open(path, 'wb') as fh:
        fh.write(out)
    if slow:
        time.sleep(slow)
if name == 'REDline':
    start, count = int(opt('--start', '0')), int(opt('--frameCount', str(total)))
    if os.environ.get('SPLICE_MOCK_TOOL_IGNORE_RANGE'):
        start, count = 0, total
    for frame in range(start, start + count):
        tiff(os.path.join(opt('--outDir'), f"{{opt('--o')}}.{{frame:06d}}.tif"), frame)
elif name == 'art-cmd':
    start, count = int(opt('--start')), int(opt('--duration'))
    for frame in range(start, start + count):
        tiff(opt('--output') % frame, frame)
else:
    digits = re.findall(r'(\d+)\.', os.path.basename(args[-1]))
    tiff(opt('-Z'), int(digits[-1]) if digits else 0)
'''


def _frame_levels(path: Path) -> list[int]:
    """Source frame per exported frame, read back from the grey level the mock tools write."""
    gray = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'rawvideo', '-pix_fmt', 'gray16le', '-'],
                          capture_output=True, check=True).stdout
    size = 64 * 48 * 2
    levels = []
    for index in range(len(gray) // size):
        samples = struct.unpack_from('<' + 'H' * (64 * 48), gray, index * size)
        levels.append(round((sum(samples) / len(samples) - 1000) / 2500))
    return levels


def _alive(pid: int) -> bool:
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as fh:
            return fh.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


class TestVendorDecodersByName(_IsolatedDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for name in ('REDline', 'art-cmd', 'dcraw_emu'):
            tool = self.bin / name
            tool.write_text(_MOCK_TOOL.format(python=sys.executable), encoding='utf-8')
            tool.chmod(0o755)
        self.log = self.tmp / 'tools.jsonl'
        env = mock.patch.dict(os.environ, {'SPLICE_MOCK_TOOL_LOG': str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        raw_decode._probe_cache.clear()

    def calls(self, tool=None):
        if not self.log.is_file():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if tool is None or row['tool'] == tool]

    def _project(self, path: Path, *, clip_in=0.5, duration=0.25, settings=None):
        src = media_links.link_many([str(path)], validate=media_proxy.decodable)[0]['src']
        info = media_proxy.describe(src)
        return info, {'name': 'vendor', 'width': 64, 'height': 48, 'fps': 24,
                      'media': [{**info, 'id': 'm1', 'name': path.name, 'src': src}],
                      'clips': [{'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1', 'start': 0, 'in': clip_in,
                                 'duration': duration, 'transition': 'none',
                                 'rawSettings': settings or {'decodeUsing': 'camera'}}]}

    def _export(self, project, name='out.mov', **kwargs):
        out = self.tmp / name
        render_movie.render_to_file(project, out, options={'codec': 'prores', 'profile': 'hq', 'limiter': False}, **kwargs)
        return out

    def _r3d(self):
        return raw_fixtures.write_synthetic_r3d(self.tmp / 'A002_C003_0101AB_001.R3D', width=64, height=48, fps=(24, 1),
                                                frames=24, with_rmd=False)

    def test_redline_exports_the_cut_frames_with_its_documented_flags(self):
        raw_decode.save_decoder_config({'r3d': str(self.bin / 'REDline')})
        info, project = self._project(self._r3d())
        self.assertEqual(info['rawDecoderName'], 'REDline (cli)')
        caps = info['rawCapabilities']
        self.assertEqual(caps['tool'], 'REDline')
        self.assertNotIn('highlightRecovery', caps['settings'])
        self.assertEqual(_frame_levels(self._export(project)), [12, 13, 14, 15, 16, 17])
        argv = self.calls('REDline')[-1]['argv']
        flags = dict(zip(argv[::2], argv[1::2]))
        start, count = int(flags['--start']), int(flags['--frameCount'])
        self.assertLessEqual(start, 12)
        self.assertGreaterEqual(start + count, 18)
        self.assertLess(count, 24, 'only the window is decoded')
        self.assertEqual((flags['--format'], flags['--res']), ('1', '1'))
        self.assertEqual((flags['--colorSpace'], flags['--gammaCurve']), ('25', '34'), 'REDWideGamutRGB / Log3G10')
        self.assertNotIn('--iso', flags, 'camera metadata mode leaves the look to the R3D')

        project['clips'][0]['rawSettings'] = {'decodeUsing': 'clip', 'iso': 1600, 'exposure': 1.0, 'kelvin': 4300,
                                              'tint': 10, 'colorSpace': 'rec2020', 'gamma': 'rec709',
                                              'highlightRecovery': True}
        self.assertEqual(_frame_levels(self._export(project, 'clip.mov')), [12, 13, 14, 15, 16, 17])
        argv = self.calls('REDline')[-1]['argv']
        flags = dict(zip(argv[::2], argv[1::2]))
        self.assertEqual([flags[k] for k in ('--colorSpace', '--gammaCurve', '--iso', '--kelvin', '--tint', '--exposure')],
                         ['24', '1', '1600', '4300', '10.00', '1.000'])
        self.assertEqual(raw_decode.unapplied_settings(project['clips'][0]['rawSettings'], caps), ['highlightRecovery'])

    def test_redline_writing_the_whole_clip_is_trimmed_to_the_window(self):
        raw_decode.save_decoder_config({'r3d': str(self.bin / 'REDline')})
        _, project = self._project(self._r3d(), clip_in=0.5)
        with mock.patch.dict(os.environ, {'SPLICE_MOCK_TOOL_IGNORE_RANGE': '1'}):
            self.assertEqual(_frame_levels(self._export(project)), [12, 13, 14, 15, 16, 17])

    def test_cancel_stops_redline_and_its_children_within_half_a_second(self):
        raw_decode.save_decoder_config({'r3d': str(self.bin / 'REDline')})
        _, project = self._project(self._r3d(), clip_in=0.0, duration=1.0)
        pids = self.tmp / 'pids'
        cancel = threading.Event()
        errors = []

        def run():
            try:
                self._export(project, 'cancel.mov', cancelled=cancel)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        with mock.patch.dict(os.environ, {'SPLICE_MOCK_TOOL_PIDS': str(pids), 'SPLICE_MOCK_TOOL_SLOW': '0.25'}):
            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 20
            while not (pids.is_file() and pids.read_text().strip()) and time.monotonic() < deadline:
                time.sleep(0.02)
            tool_pid, child_pid = map(int, pids.read_text().split())
            time.sleep(0.3)
            self.assertTrue(_alive(tool_pid) and _alive(child_pid))
            started = time.monotonic()
            cancel.set()
            worker.join(10)
            elapsed = time.monotonic() - started
        self.assertFalse(worker.is_alive())
        self.assertLess(elapsed, 0.5, f'cancel took {elapsed:.2f} s')
        self.assertTrue(errors and isinstance(errors[0], InterruptedError), errors)
        deadline = time.monotonic() + 2
        while (_alive(tool_pid) or _alive(child_pid)) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_alive(tool_pid), 'REDline survived the cancel')
        self.assertFalse(_alive(child_pid), "REDline's child process survived the cancel")
        self.assertEqual(_children_of_this_process(), [])

    def test_art_cmd_gets_the_frame_range_target_colorspace_and_look_overrides(self):
        folder = self.tmp / 'ARRI'
        for frame in range(24):
            raw_fixtures.write_synthetic_ari(folder / f'A001C001_{frame:06d}.ari', width=64, height=48)
        raw_decode.save_decoder_config({'arriraw': str(self.bin / 'art-cmd')})
        info, project = self._project(folder / 'A001C001_000000.ari')
        self.assertEqual(info['rawCapabilities']['tool'], 'art-cmd')
        self.assertNotIn('exposure', info['rawCapabilities']['settings'])
        self.assertEqual(_frame_levels(self._export(project)), [12, 13, 14, 15, 16, 17])
        call = self.calls('art-cmd')[-1]
        argv = call['argv']
        self.assertEqual(argv[0], 'process')
        flags = dict(zip(argv[1::2], argv[2::2]))
        self.assertEqual(flags['--input'], str(folder.resolve() / 'A001C001_%06d.ari'))
        self.assertTrue(flags['--output'].endswith('frame.%07d.tif'))
        self.assertLessEqual(int(flags['--start']), 12)
        self.assertGreaterEqual(int(flags['--start']) + int(flags['--duration']), 18)
        self.assertEqual(flags['--target-colorspace'], 'AWG4/D65/LogC4')
        self.assertNotIn('--parameters', flags)

        settings = {'decodeUsing': 'clip', 'iso': 1280, 'kelvin': 4300, 'tint': -3, 'exposure': 1.0,
                    'colorSpace': 'rec709', 'gamma': 'rec709'}
        project['clips'][0]['rawSettings'] = settings
        self._export(project, 'clip.mov')
        call = self.calls('art-cmd')[-1]
        flags = dict(zip(call['argv'][1::2], call['argv'][2::2]))
        self.assertEqual(flags['--target-colorspace'], 'Rec.709/D65/BT.1886')
        self.assertEqual(call['parameters'], {'exposureIndexOverride': 1280, 'colorTemperatureOverride': 4300,
                                              'tintOverride': -3.0})
        self.assertEqual(raw_decode.unapplied_settings(settings, info['rawCapabilities']), ['exposure'])
        self.assertEqual(raw_decode.unapplied_settings({'colorSpace': 'arri_wide_gamut_4', 'gamma': 'rec709'},
                                                       info['rawCapabilities']), ['colorSpace', 'gamma'])

    def test_dcraw_emu_develops_each_frame_of_a_compressed_dng_window(self):
        first = raw_fixtures.write_cinemadng_sequence(self.tmp / 'C001', prefix='C001_', frames=24, width=64, height=48,
                                                      with_wav=False)
        meta = raw_bayer.parse_dng(first)
        raw_decode.save_decoder_config({'libraw': str(self.bin / 'dcraw_emu')})
        settings = {'decodeUsing': 'clip', 'iso': 1600, 'exposure': 1.0, 'kelvin': 3200, 'colorSpace': 'rec2020',
                    'gamma': 'linear', 'highlightRecovery': True}
        with mock.patch('raw_bayer.parse_dng', return_value={**meta, 'compression': 34892, 'compressed': True,
                                                              'builtinDecodable': False}):
            info, project = self._project(first, settings=settings)
            self.assertEqual(info['rawCapabilities']['tool'], 'dcraw_emu')
            out = self._export(project)
        # Frame files are numbered from 1: source frames 12..17 are C001_000013..18.
        self.assertEqual(_frame_levels(out), [13, 14, 15, 16, 17, 18])
        calls = self.calls('dcraw_emu')
        sources = [Path(call['argv'][-1]).name for call in calls]
        lowest = int(sources[0][5:11])
        self.assertEqual(sources, [f'C001_{n:06d}.dng' for n in range(lowest, lowest + len(sources))],
                         'one dcraw_emu run per window frame, in order')
        self.assertLessEqual(lowest, 13)
        self.assertGreaterEqual(lowest + len(sources) - 1, 18)
        self.assertLess(len(sources), 24, 'only the window is developed')
        argv = calls[-1]['argv']
        self.assertEqual(argv[:5], ['-6', '-T', '-W', '-w', '-b'])
        self.assertEqual(argv[5], '4.000000', '+1 EV at ISO 1600 is a 4x brightness gain')
        self.assertEqual(argv[6:13], ['-o', '8', '-g', '1', '1', '-H', '2'])
        self.assertNotIn('-h', argv, 'export decodes full size')
        self.assertEqual(raw_decode.unapplied_settings(settings, info['rawCapabilities']), ['kelvin'])

    def test_dcraw_emu_still_is_developed_once_and_held(self):
        nef = self.tmp / 'DSC_0005.NEF'
        nef.write_bytes(b'\0' * 64)
        raw_decode.save_decoder_config({'libraw': str(self.bin / 'dcraw_emu')})
        raw_decode._probe_cache.clear()
        info = raw_decode.probe_raw(nef)
        info.update(width=64, height=48)
        project = {'name': 'still', 'width': 64, 'height': 48, 'fps': 24,
                   'media': [{**info, 'id': 'm1', 'name': nef.name,
                              'src': media_links.link_many([str(nef)], validate=media_proxy.decodable)[0]['src']}],
                   'clips': [{'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1', 'start': 0, 'in': 0.5,
                              'duration': 0.25, 'transition': 'none', 'rawSettings': {'decodeUsing': 'camera'}}]}
        with mock.patch('raw_decode.probe_raw', return_value=info):
            levels = _frame_levels(self._export(project))
        self.assertEqual(levels, [5] * 6)
        self.assertEqual(len(self.calls('dcraw_emu')), 1)
        self.assertIn('-w', self.calls('dcraw_emu')[0]['argv'])

    def test_turnover_names_settings_the_decoder_ignored(self):
        raw_decode.save_decoder_config({'r3d': str(self.bin / 'REDline')})
        info, project = self._project(self._r3d(), settings={'decodeUsing': 'clip', 'iso': 1600, 'kelvin': 4300,
                                                             'highlightRecovery': True})
        report = interchange_handle('export', {'project': project, 'options': {'formats': ['edl']}})['report']
        override = next(line for line in report.splitlines() if 'decode overrides' in line)
        self.assertIn('ISO 1600', override)
        self.assertNotIn('highlight recovery', override)
        self.assertIn('REDline cannot take highlight recovery', report)
        # And the look REDline used came from placeholders (no .RMD next to the clip).
        self.assertIn('ISO, white balance (Kelvin), tint not recorded in the file', report)

    def test_free_space_is_checked_before_tiff_staging_and_intermediates(self):
        raw_decode.save_decoder_config({'r3d': str(self.bin / 'REDline')})
        _, project = self._project(self._r3d())
        tight = mock.Mock(free=1024 ** 2, total=10 * 1024 ** 3, used=0)
        with mock.patch('raw_decode.shutil.disk_usage', return_value=tight):
            with self.assertRaisesRegex(raw_bayer.RawError, 'Not enough free disk space for the full-resolution 16-bit'):
                self._export(project)
            with self.assertRaisesRegex(raw_bayer.RawError, "Not enough free disk space for REDline's 16-bit TIFF frames"):
                raw_decode.decode_to_proxy(self._r3d(), self.tmp / 'proxy.mp4', {'decodeUsing': 'camera'})
        self.assertEqual(self.calls('REDline'), [], 'nothing was decoded')
        first = raw_fixtures.write_cinemadng_sequence(self.tmp / 'D001', prefix='D001_', frames=4, with_wav=False)
        with mock.patch('raw_decode.shutil.disk_usage', return_value=tight):
            with self.assertRaisesRegex(raw_bayer.RawError, 'Not enough free disk space'):
                raw_decode.decode_to_intermediate(first, self.tmp / 'i.mov', {'decodeUsing': 'camera'})


# ---------------------------------------------------------------------------
# B4: look metadata not in the file
# ---------------------------------------------------------------------------

class TestEstimatedLookMetadata(_IsolatedDataDirMixin, unittest.TestCase):
    def test_dng_without_as_shot_neutral_model_or_iso_flags_them(self):
        bare = write(self.tmp / 'bare' / 'E001_000001.dng', build_tiff([image_tags()], pixels=PIXELS))
        info = raw_decode.probe_raw(bare)
        self.assertEqual(set(info['rawEstimated']) & {'iso', 'kelvin', 'tint', 'camera'}, {'iso', 'kelvin', 'tint', 'camera'})
        self.assertEqual(info['rawMetadata']['camera'], 'CinemaDNG Camera')
        self.assertEqual(info['rawDefaults']['kelvin'], 5600)

        full = write(self.tmp / 'full' / 'E002_000001.dng', build_tiff([image_tags(
            t50708=(2, 'Splice Test Cam'), t50728=(5, ((5, 10), (1, 1), (7, 10))), t34855=(3, 1250))], pixels=PIXELS))
        info = raw_decode.probe_raw(full)
        self.assertEqual(set(info['rawEstimated']) & {'iso', 'kelvin', 'tint', 'camera'}, set())
        self.assertEqual(info['rawDefaults']['iso'], 1250)
        self.assertEqual(info['rawMetadata']['camera'], 'Splice Test Cam')

        # ISO in the EXIF IFD (where most DNG writers put it) is read too.
        exif = build_tiff([image_tags(t50708=(2, 'Cam'), t34665=(4, IFDRef(1))), [(34855, 3, 640)]], pixels=PIXELS)
        path = write(self.tmp / 'exif' / 'E003_000001.dng', exif)
        self.assertEqual(raw_bayer.parse_dng(path)['iso'], 640)
        self.assertNotIn('iso', raw_decode.probe_raw(path)['rawEstimated'])
        # A dangling EXIF pointer only loses the ISO; the frame still reads.
        dangling = write(self.tmp / 'exif2' / 'E004_000001.dng',
                         build_tiff([image_tags(t50708=(2, 'Cam'), t34665=(4, 0x7FFFFFF0))], pixels=PIXELS))
        self.assertIsNone(raw_bayer.parse_dng(dangling)['iso'])
        self.assertIn('iso', raw_decode.probe_raw(dangling)['rawEstimated'])

    def test_prores_raw_and_still_raw_placeholders_are_flagged(self):
        record = raw_decode.enrich_prores_raw(self.tmp / 'x.mov', {'type': 'video', 'camera': None})
        self.assertEqual(record['rawEstimated'], ['iso', 'kelvin', 'tint', 'camera'])

    def test_interchange_report_discloses_estimated_look(self):
        braw = raw_fixtures.write_synthetic_braw(self.tmp / 'B009_C001.braw', with_sidecar=False)
        src = media_links.link_many([str(braw)], validate=media_proxy.decodable)[0]['src']
        info = media_proxy.describe(src)
        self.assertEqual(info['rawEstimated'][-3:], ['iso', 'kelvin', 'tint'])
        project = {'name': 'Look', 'width': 160, 'height': 90, 'fps': 24,
                   'media': [{**info, 'id': 'm1', 'name': braw.name, 'src': src}],
                   'clips': [{'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1', 'start': 0, 'in': 0,
                              'duration': 0.25, 'rawSettings': {'decodeUsing': 'camera'}}]}
        report = interchange_handle('export', {'project': project, 'options': {'formats': ['fcpxml']}})['report']
        self.assertIn('camera RAW ISO, white balance (Kelvin), tint not recorded in the file or a sidecar', report)


# ---------------------------------------------------------------------------
# N2-N4: decoder identity, library lookups, sound-only MXF
# ---------------------------------------------------------------------------

class TestDecoderIdentityAndDetection(_IsolatedDataDirMixin, unittest.TestCase):
    def test_interpreters_and_proc_paths_are_not_decoders(self):
        for path in ('/proc/self/exe', sys.executable, os.path.realpath(sys.executable), '/bin/sh'):
            if not os.path.exists(path):
                continue
            with self.subTest(path), self.assertRaisesRegex(ValueError, 'not a RAW decoder'):
                raw_decode.validate_decoder_path('braw', path)
            with self.subTest(f'env {path}'), mock.patch.dict(os.environ, {'SPLICE_BRAW_SDK': path}):
                row = next(d for d in raw_decode.decoder_status()['decoders'] if d['id'] == 'braw')
                self.assertFalse(row['available'], f'{path} must not be listed as an available decoder')

    def test_parentheses_are_allowed_in_decoder_paths(self):
        folder = self.tmp / 'Program Files (x86)' / 'REDCINE-X PRO'
        folder.mkdir(parents=True)
        tool = folder / 'REDline'
        tool.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        tool.chmod(0o755)
        self.assertEqual(raw_decode.validate_decoder_path('r3d', str(tool)), str(tool.resolve()))
        raw_decode.save_decoder_config({'r3d': str(tool)})
        row = next(d for d in raw_decode.decoder_status()['decoders'] if d['id'] == 'r3d')
        self.assertTrue(row['available'])
        self.assertIsNone(raw_decode._SHELL_META.search(r'C:\Program Files (x86)\REDCINE-X PRO 64-bit\REDline.exe'))
        for bad in ('/opt/red/REDline; rm -rf x', '/opt/$(id)/REDline', '/opt/red/REDline`id`'):
            with self.assertRaisesRegex(ValueError, 'disallowed characters'):
                raw_decode.validate_decoder_path('r3d', bad)

    def test_library_lookups_are_cached(self):
        raw_decode._find_library.cache_clear()
        with mock.patch('ctypes.util.find_library', return_value=None) as finder:
            for _ in range(5):
                raw_decode.decoder_status()
                raw_decode.decoder_cache_salt('braw')
                raw_decode.decoder_cache_salt('r3d')
        names = [call.args[0] for call in finder.call_args_list]
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)), f'find_library ran more than once per name: {names}')
        raw_decode._find_library.cache_clear()

    def test_a_sound_only_mxf_naming_a_raw_camera_is_ordinary_media(self):
        mxf = self.tmp / 'F55_sound.mxf'
        made = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=d=0.5:r=48000', '-c:a', 'pcm_s16le',
                               '-ar', '48000', '-ac', '1', '-metadata', 'product_name=Sony F55RAW',
                               '-metadata', 'company_name=Sony', '-f', 'mxf_opatom', str(mxf)], capture_output=True)
        if made.returncode or not mxf.is_file():
            self.skipTest('this FFmpeg cannot write an OP-Atom sound MXF')
        tags = raw_decode._ffprobe_tags(raw_decode._ffprobe_optional(mxf))
        self.assertIn('f55raw', ' '.join(tags.values()).lower(), 'the marker must be in the file for this test to mean anything')
        self.assertIsNone(raw_decode.detect_raw_format(mxf))
        self.assertFalse(raw_decode.is_raw_candidate(mxf))
        media_proxy.decodable(mxf, mxf.name)


if __name__ == '__main__':
    unittest.main()
