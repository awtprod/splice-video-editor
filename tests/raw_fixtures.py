"""Synthetic Camera RAW test fixtures (zero copyrighted media, zero vendor SDKs).

Generates genuine, spec-valid synthetic containers & Bayer CFA files for automated testing:
- 12-bit and 16-bit uncompressed Bayer CinemaDNG (.dng) frames & numbered sequences + sidecar WAV
- QuickTime Blackmagic RAW (.braw) container + .sidecar JSON
- RED RAW (.r3d) binary header + .RMD XML sidecar
- ARRIRAW (.ari) binary header & numbered sequences
- Canon Cinema RAW Light (.crm) ISO BMFF container
- Nikon N-RAW (.nev) QuickTime container
- Local mock Tier-2 CLI decoder emitting deterministic 16-bit rgb48le frames
"""
from __future__ import annotations

import json
import math
import stat
import struct
import subprocess
import sys
import wave
from pathlib import Path

# sRGB/Rec.709 D65 -> XYZ D65 matrix (inverse of XYZ_D65_TO_REC709) so a linear-sRGB synthetic
# sensor round-trips accurately through raw_bayer's DNG matrix pipeline.
SRGB_TO_XYZ_D65 = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
# XYZ D65 -> sRGB (for ColorMatrix1 = XYZ -> Camera)
XYZ_TO_SRGB_D65 = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)

# 6 Macbeth-inspired linear RGB patches arranged in a 3x2 grid:
# Top row:    [0] Neutral Mid-Grey (0.18), [1] Crimson Red, [2] Foliage Green
# Bottom row: [3] Sky Blue, [4] Highlight Recovery (R clips >1.0, G/B below clip), [5] Deep Neutral (0.06)
DEFAULT_PATCHES = (
    (0.18, 0.18, 0.18),
    (0.45, 0.08, 0.07),
    (0.09, 0.38, 0.10),
    (0.08, 0.16, 0.48),
    (1.25, 0.72, 0.52),
    (0.06, 0.06, 0.06),
)


def _bcd(value: int) -> int:
    v = max(0, min(99, int(value)))
    return ((v // 10) << 4) | (v % 10)


def _pack_12bit_be(samples: list[int]) -> bytes:
    """Pack 12-bit unsigned samples in TIFF/DNG standard big-endian bit order (2 pixels -> 3 bytes)."""
    out = bytearray((len(samples) * 3 + 1) // 2)
    o = 0
    n = len(samples)
    i = 0
    while i + 1 < n:
        a = samples[i] & 0xFFF
        b = samples[i + 1] & 0xFFF
        out[o] = (a >> 4) & 0xFF
        out[o + 1] = ((a & 0xF) << 4) | ((b >> 8) & 0xF)
        out[o + 2] = b & 0xFF
        o += 3
        i += 2
    if i < n:
        a = samples[i] & 0xFFF
        out[o] = (a >> 4) & 0xFF
        out[o + 1] = (a & 0xF) << 4
    return bytes(out)


def _pack_16bit(samples: list[int], endian: str = '<') -> bytes:
    return struct.pack(f'{endian}{len(samples)}H', *(max(0, min(65535, s)) for s in samples))


def write_synthetic_dng(
    path: Path | str,
    *,
    width: int = 96,
    height: int = 64,
    bit_depth: int = 12,
    cfa_pattern: tuple[int, int, int, int] = (0, 1, 1, 2),
    black_level: int = 64,
    white_level: int | None = None,
    patches: tuple[tuple[float, float, float], ...] = DEFAULT_PATCHES,
    frame_rate: tuple[int, int] = (24, 1),
    timecode: tuple[int, int, int, int] = (1, 0, 0, 0),
    reel: str = 'A001',
    camera: str = 'Splice Synthetic CinemaDNG',
    as_shot_neutral: tuple[float, float, float] | None = None,
    baseline_exposure: float = 0.0,
    frame_index: int = 0,
    big_endian: bool = False,
    iso: int | None = 800,
) -> Path:
    """Write a genuine, uncompressed 12-bit or 16-bit Bayer CFA CinemaDNG (.dng) file. ``iso``
    goes in the TIFF/EP ISOSpeedRatings tag (``None`` leaves it out, as some cameras do)."""
    path = Path(path)
    if width % 2 or height % 2:
        raise ValueError('Bayer DNG dimensions must be even')
    if bit_depth not in (12, 16):
        raise ValueError('Synthetic DNG generator supports 12-bit and 16-bit')
    if white_level is None:
        white_level = 4095 if bit_depth == 12 else 65535
    endian = '>' if big_endian else '<'
    span = float(white_level - black_level)

    # Render 3x2 Macbeth-style color patches into the Bayer mosaic
    # Optional small bright frame-index marker bar along top-left 8x4 block so sequence frame order is verifiable
    samples = [0] * (width * height)
    col_w = width // 3
    row_h = height // 2
    marker_x = (frame_index * 4) % max(4, width - 8)
    for y in range(height):
        py = 0 if y < row_h else 1
        row_off = y * width
        cfa_row = (y & 1) * 2
        for x in range(width):
            px = min(2, x // col_w)
            rgb = patches[(py * 3 + px) % len(patches)]
            # Tiny 4x4 frame-counter indicator in the very top edge (y < 4) so patch centers are untouched
            if y < 4 and marker_x <= x < marker_x + 4:
                rgb = (0.95, 0.95, 0.95)
            ch = cfa_pattern[cfa_row + (x & 1)]
            lin = max(0.0, rgb[ch])
            code = int(round(black_level + lin * span))
            samples[row_off + x] = max(0, min(white_level, code))

    pixel_bytes = _pack_12bit_be(samples) if bit_depth == 12 else _pack_16bit(samples, endian)

    # Assemble TIFF/DNG IFD entries (must be sorted by tag id per TIFF 6.0 spec)
    # Using AsShotNeutral = camera response to D65 so default Camera Metadata is neutral D65
    if as_shot_neutral is None:
        as_shot_neutral = (0.95047, 1.0, 1.08883)
        # Convert D65 XYZ -> camera RGB via XYZ_TO_SRGB_D65 (which maps D65 -> (1, 1, 1))
        as_shot_neutral = (1.0, 1.0, 1.0)

    def _rational_pair(val: float, denom: int = 10000) -> tuple[int, int]:
        return (int(round(val * denom)), denom)

    tc_h, tc_m, tc_s, tc_f = timecode
    tc_bytes = bytes([_bcd(tc_f), _bcd(tc_s), _bcd(tc_m), _bcd(tc_h), 0, 0, 0, 0])

    # Tag definitions: (tag, ttype, count, raw_payload_bytes)
    # Types: 1=BYTE, 2=ASCII, 3=SHORT, 4=LONG, 5=RATIONAL, 10=SRATIONAL
    def make_str(s: str) -> bytes:
        return s.encode('ascii', 'replace') + b'\x00'

    cm1_flat = [v for row in XYZ_TO_SRGB_D65 for v in row]
    cm1_bytes = b''.join(struct.pack(f'{endian}ii', *_rational_pair(v, 10000)) for v in cm1_flat)
    asn_bytes = b''.join(struct.pack(f'{endian}II', max(1, int(round(v * 10000))), 10000) for v in as_shot_neutral)
    be_bytes = struct.pack(f'{endian}ii', *_rational_pair(baseline_exposure, 1000))
    fps_bytes = struct.pack(f'{endian}ii', int(frame_rate[0]), int(frame_rate[1]))

    entries: list[tuple[int, int, int, bytes]] = [
        (254, 4, 1, struct.pack(f'{endian}I', 0)),                       # NewSubfileType (full-res)
        (256, 4, 1, struct.pack(f'{endian}I', width)),                   # ImageWidth
        (257, 4, 1, struct.pack(f'{endian}I', height)),                  # ImageLength
        (258, 3, 1, struct.pack(f'{endian}H', bit_depth)),               # BitsPerSample
        (259, 3, 1, struct.pack(f'{endian}H', 1)),                       # Compression = Uncompressed
        (262, 3, 1, struct.pack(f'{endian}H', 32803)),                   # PhotometricInterpretation = CFA
        (271, 2, len(make_str('Splice')), make_str('Splice')),           # Make
        (272, 2, len(make_str(camera)), make_str(camera)),               # Model
        (273, 4, 1, b'\x00\x00\x00\x00'),                                # StripOffsets (patched below)
        (277, 3, 1, struct.pack(f'{endian}H', 1)),                       # SamplesPerPixel
        (278, 4, 1, struct.pack(f'{endian}I', height)),                  # RowsPerStrip
        (279, 4, 1, struct.pack(f'{endian}I', len(pixel_bytes))),        # StripByteCounts
        (33421, 3, 2, struct.pack(f'{endian}HH', 2, 2)),                 # CFARepeatPatternDim
        (33422, 1, 4, bytes(cfa_pattern)),                               # CFAPattern
        *([(34855, 3, 1, struct.pack(f'{endian}H', iso))] if iso else []),  # ISOSpeedRatings
        (50706, 1, 4, bytes([1, 4, 0, 0])),                              # DNGVersion 1.4.0.0
        (50708, 2, len(make_str(camera)), make_str(camera)),             # UniqueCameraModel
        (50714, 4, 1, struct.pack(f'{endian}I', black_level)),           # BlackLevel
        (50717, 4, 1, struct.pack(f'{endian}I', white_level)),           # WhiteLevel
        (50721, 10, 9, cm1_bytes),                                       # ColorMatrix1
        (50728, 5, 3, asn_bytes),                                        # AsShotNeutral
        (50730, 10, 1, be_bytes),                                        # BaselineExposure
        (50778, 3, 1, struct.pack(f'{endian}H', 21)),                    # CalibrationIlluminant1 = D65
        (51043, 1, 8, tc_bytes),                                         # TimeCodes
        (51044, 10, 1, fps_bytes),                                       # FrameRate
        (51081, 2, len(make_str(reel)), make_str(reel)),                 # ReelName
    ]
    entries.sort(key=lambda e: e[0])

    header_size = 8
    ifd_size = 2 + len(entries) * 12 + 4
    extra_offset = header_size + ifd_size
    extra_chunks = bytearray()
    ifd_buf = bytearray()
    ifd_buf.extend(struct.pack(f'{endian}H', len(entries)))

    strip_offset_patch_pos = None
    for tag, ttype, count, payload in entries:
        if tag == 273:
            strip_offset_patch_pos = header_size + len(ifd_buf) + 8
        if len(payload) <= 4:
            padded = payload.ljust(4, b'\x00')
            ifd_buf.extend(struct.pack(f'{endian}HHI', tag, ttype, count))
            ifd_buf.extend(padded)
        else:
            val_off = extra_offset + len(extra_chunks)
            extra_chunks.extend(payload)
            if len(extra_chunks) % 2:
                extra_chunks.append(0)
            ifd_buf.extend(struct.pack(f'{endian}HHII', tag, ttype, count, val_off))
    ifd_buf.extend(struct.pack(f'{endian}I', 0))  # Next IFD offset = 0

    pixel_offset = extra_offset + len(extra_chunks)
    magic = b'MM' if big_endian else b'II'
    out = bytearray(magic + struct.pack(f'{endian}HI', 42, header_size))
    out.extend(ifd_buf)
    out.extend(extra_chunks)
    out.extend(pixel_bytes)
    assert strip_offset_patch_pos is not None
    out[strip_offset_patch_pos:strip_offset_patch_pos + 4] = struct.pack(f'{endian}I', pixel_offset)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)
    return path


def write_sidecar_wav(path: Path | str, *, duration: float = 0.5, sample_rate: int = 48000, freq: float = 440.0) -> Path:
    """Write a deterministic 16-bit stereo PCM WAV tone."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(round(duration * sample_rate)))
    samples = bytearray(n_frames * 4)
    for i in range(n_frames):
        val = int(round(12000.0 * math.sin(2.0 * math.pi * freq * i / sample_rate)))
        struct.pack_into('<hh', samples, i * 4, val, val)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples)
    return path


def write_cinemadng_sequence(
    folder: Path | str,
    *,
    prefix: str = 'A001_',
    frames: int = 12,
    fps: int = 24,
    bit_depth: int = 12,
    width: int = 96,
    height: int = 64,
    with_wav: bool = True,
    reel: str = 'A001',
    timecode: tuple[int, int, int, int] = (1, 0, 0, 0),
    patches: tuple[tuple[float, float, float], ...] = DEFAULT_PATCHES,
) -> Path:
    """Write a numbered CinemaDNG sequence + optional sidecar WAV. Returns path to first frame."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    first = None
    for idx in range(frames):
        p = folder / f'{prefix}{idx + 1:06d}.dng'
        tc_h, tc_m, tc_s, tc_f = timecode
        total_f = tc_f + idx
        tc = (tc_h, tc_m, tc_s + total_f // fps, total_f % fps)
        write_synthetic_dng(
            p,
            width=width,
            height=height,
            bit_depth=bit_depth,
            frame_rate=(fps, 1),
            timecode=tc,
            reel=reel,
            patches=patches,
            frame_index=idx,
        )
        if first is None:
            first = p
    if with_wav:
        write_sidecar_wav(folder / f'{prefix.rstrip("_")}.wav', duration=frames / float(fps))
    assert first is not None
    return first


def _make_qt_with_patched_fourcc(
    path: Path,
    *,
    fourcc: bytes,
    width: int = 160,
    height: int = 90,
    fps: int = 24,
    duration: float = 0.5,
    timecode: str = '01:02:03:04',
    reel: str = 'B001',
    with_audio: bool = True,
    brand: bytes = b'qt  ',
) -> Path:
    """Generate a genuine QuickTime MOV with FFmpeg and patch its video sample FourCC to ``fourcc``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_mov = path.with_suffix('.tmp.mov')
    cmd = [
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', f'color=c=0x408040:s={width}x{height}:r={fps}:d={duration}',
    ]
    if with_audio:
        cmd += ['-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={duration}']
    cmd += [
        '-c:v', 'rawvideo', '-pix_fmt', 'uyvy422',
        '-timecode', timecode,
        '-movflags', 'use_metadata_tags',
        '-metadata', f'reel_name={reel}',
        '-metadata', 'com.apple.quicktime.model=Synthetic Camera',
    ]
    if with_audio:
        cmd += ['-c:a', 'pcm_s16le', '-ac', '2', '-ar', '48000']
    cmd += ['-f', 'mov', str(tmp_mov)]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    data = bytearray(tmp_mov.read_bytes())
    tmp_mov.unlink(missing_ok=True)
    # Patch the sample entry FourCC ('2vuy' -> target fourcc) inside stsd
    idx = data.find(b'2vuy')
    if idx < 0:
        raise RuntimeError('Could not locate video sample entry FourCC in synthetic MOV')
    data[idx:idx + 4] = fourcc
    if brand != b'qt  ' and len(data) >= 12 and data[4:8] == b'ftyp':
        data[8:12] = brand
    path.write_bytes(data)
    return path


def write_synthetic_braw(
    path: Path | str,
    *,
    width: int = 160,
    height: int = 90,
    fps: int = 24,
    duration: float = 0.5,
    timecode: str = '01:02:03:04',
    reel: str = 'B001',
    with_audio: bool = True,
    with_sidecar: bool = True,
) -> Path:
    path = _make_qt_with_patched_fourcc(
        Path(path),
        fourcc=b'brhq',
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        timecode=timecode,
        reel=reel,
        with_audio=with_audio,
    )
    if with_sidecar:
        sidecar = path.with_suffix('.sidecar')
        sidecar.write_text(json.dumps({
            'iso': 1600,
            'colorTemp': 4800,
            'tint': 5.0,
            'exposure': 0.5,
            'gamut': 'bmd_wide_gamut_gen5',
            'gamma': 'bmd_film_gen5',
            'highlightRecovery': True,
        }), encoding='utf-8')
    return path


def write_synthetic_r3d(
    path: Path | str,
    *,
    width: int = 4096,
    height: int = 2160,
    fps: tuple[int, int] = (24000, 1001),
    frames: int = 12,
    timecode: str = '01:05:10:12',
    reel: str = 'A002',
    with_rmd: bool = True,
) -> Path:
    """A RED ``.r3d`` shell laid out the way FFmpeg's r3d demuxer reads it: a ``RED1`` header atom
    (width/height at 52/56, frame rate at 62/64, audio channels at 66, clip name at 67), ``frames``
    empty ``REDV`` atoms, an ``RDVO`` frame index and a trailing 56-byte ``REOB`` atom."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = bytearray(512)
    struct.pack_into('>I', head, 0, len(head))
    head[4:8] = b'RED1'
    head[8], head[9] = 1, 4
    struct.pack_into('>I', head, 12, fps[0])
    struct.pack_into('>II', head, 52, width, height)
    struct.pack_into('>HH', head, 62, *fps)
    head[66] = 0
    name = path.stem.encode('ascii', 'replace')[:256]
    head[67:67 + len(name)] = name
    body = bytearray(head)
    offsets = []
    for _ in range(frames):
        offsets.append(len(body))
        atom = bytearray(32)
        struct.pack_into('>I', atom, 0, len(atom))
        atom[4:8] = b'REDV'
        body += atom
    rdvo = len(body)
    body += struct.pack('>I4s', 8 + 4 * frames, b'RDVO') + b''.join(struct.pack('>I', o) for o in offsets)
    reob = bytearray(56)
    struct.pack_into('>I4sI', reob, 0, 56, b'REOB', rdvo)
    struct.pack_into('>I', reob, 24, frames)
    body += reob
    path.write_bytes(bytes(body))
    if with_rmd:
        path.with_suffix('.RMD').write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<rmd>\n  <iso>1280</iso>\n  <kelvin>5200</kelvin>\n  <tint>-3.5</tint>\n'
            f'  <exposure>0.25</exposure>\n  <timecode>{timecode}</timecode>\n  <reel>{reel}</reel>\n</rmd>\n',
            encoding='utf-8')
    return path


def write_synthetic_ari(
    path: Path | str,
    *,
    width: int = 2880,
    height: int = 1620,
    model: str = 'ALEXA Synthetic',
) -> Path:
    """An ARRIRAW frame header with the fields LibRaw documents: ``ARRI`` magic, little-endian
    width/height at 20/24, camera model at 668; picture data would start at 4096."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray(4096)
    buf[0:4] = b'ARRI'
    struct.pack_into('<I', buf, 4, 0x12345678)
    struct.pack_into('<I', buf, 8, 4096)
    struct.pack_into('<II', buf, 20, width, height)
    encoded = model.encode('ascii')[:63]
    buf[668:668 + len(encoded)] = encoded
    path.write_bytes(bytes(buf))
    return path


def write_synthetic_crm(
    path: Path | str,
    *,
    width: int = 160,
    height: int = 90,
    fps: int = 24,
    duration: float = 0.5,
    timecode: str = '03:00:00:00',
    reel: str = 'D001',
) -> Path:
    return _make_qt_with_patched_fourcc(
        Path(path),
        fourcc=b'crx ',
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        timecode=timecode,
        reel=reel,
        with_audio=True,
        brand=b'crx ',
    )


def write_synthetic_nev(
    path: Path | str,
    *,
    width: int = 160,
    height: int = 90,
    fps: int = 24,
    duration: float = 0.5,
    timecode: str = '04:00:00:00',
    reel: str = 'E001',
) -> Path:
    return _make_qt_with_patched_fourcc(
        Path(path),
        fourcc=b'nraw',
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        timecode=timecode,
        reel=reel,
        with_audio=True,
    )


def write_mock_raw_cli(path: Path | str) -> Path:
    """Create a local executable mock RAW CLI decoder conforming to Splice's rgb48le streaming contract."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    script = f"""#!{sys.executable}
import argparse, json, os, struct, sys

parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--pix-fmt', default='rgb48le')
parser.add_argument('--width', type=int, required=True)
parser.add_argument('--height', type=int, required=True)
parser.add_argument('--start-frame', type=int, default=0)
parser.add_argument('--frames', type=int, default=12)
parser.add_argument('--decode-using', default='camera')
parser.add_argument('--iso', type=int, default=800)
parser.add_argument('--exposure', type=float, default=0.0)
parser.add_argument('--kelvin', type=int, default=5600)
parser.add_argument('--tint', type=float, default=0.0)
parser.add_argument('--colorspace', default='rec709')
parser.add_argument('--gamma', default='rec709')
parser.add_argument('--highlight-recovery', type=int, default=0)
parser.add_argument('--quality', default='full')
parser.add_argument('--output', default='-')
args = parser.parse_args()

log_path = os.environ.get('SPLICE_MOCK_DECODER_LOG')
if log_path:
    with open(log_path, 'w', encoding='utf-8') as fh:
        json.dump({{'argv': sys.argv[1:], 'args': vars(args)}}, fh)

# Deterministic 16-bit RGB modulated by exposure, ISO, kelvin, tint
gain = (args.iso / 800.0) * (2.0 ** args.exposure)
warm = max(0.5, min(1.5, args.kelvin / 5600.0))
r_lin = max(0.0, min(1.0, 0.25 * gain * warm))
g_lin = max(0.0, min(1.0, 0.25 * gain * (1.0 - args.tint * 0.002)))
b_lin = max(0.0, min(1.0, 0.25 * gain / warm))
r16 = int(round(r_lin * 65535))
g16 = int(round(g_lin * 65535))
b16 = int(round(b_lin * 65535))
pixel = struct.pack('<HHH', r16, g16, b16)
frame = pixel * (args.width * args.height)
out = sys.stdout.buffer
for index in range(max(1, args.frames)):
    # The top-left pixel carries the absolute source frame number so tests can check frame accuracy.
    marker = struct.pack('<HHH', min(65535, (args.start_frame + index) * 2048), 0, 0)
    out.write(marker + frame[6:])
out.flush()
"""
    path.write_text(script, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
