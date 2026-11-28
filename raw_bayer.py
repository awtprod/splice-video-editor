"""Pure-Python TIFF/DNG/EP IFD parser, Bayer CFA demosaic, color science, and CinemaDNG sequences.

Python 3.10+ standard library only. Parses uncompressed 8/10/12/14/16-bit Bayer CFA DNG frames
(single frames and numbered CinemaDNG sequences with optional sidecar WAV audio), runs a full
linear RAW pipeline (linearize -> exposure/ISO -> white balance -> optional highlight recovery ->
Malvar-He-Cutler or superpixel Bayer demosaic -> DNG XYZ/ColorMatrix -> working gamut & transfer)
and outputs packed 16-bit unsigned RGB (``rgb48le``) bytes.
"""
from __future__ import annotations

from array import array
from fractions import Fraction
from functools import lru_cache
import math
import os
from pathlib import Path
import re
import struct
import sys

MAX_DNG_BYTES = 256 * 1024 * 1024
MAX_WIDTH = 16384
MAX_HEIGHT = 16384
MAX_PIXELS = 64_000_000
MAX_IFD_ENTRIES = 1024
MAX_IFD_DEPTH = 4
MAX_IFDS = 64
MAX_SEQUENCE_FRAMES = 500_000
# Header decoding limits. Only the tags Splice reads are decoded at all; each has a count cap, and a
# file may decode at most MAX_DECODED_VALUES tag values in total across every IFD it chains.
MAX_DECODED_VALUES = 1_000_000
MAX_METADATA_VALUES = 64
MAX_STRING_BYTES = 4096
MAX_LAYOUT_VALUES = 65536
MAX_BASELINE_EXPOSURE = 16.0

# TIFF / DNG tag IDs
TAG_NEW_SUBFILE_TYPE = 254
TAG_IMAGE_WIDTH = 256
TAG_IMAGE_LENGTH = 257
TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_PHOTOMETRIC = 262
TAG_MAKE = 271
TAG_MODEL = 272
TAG_STRIP_OFFSETS = 273
TAG_ORIENTATION = 274
TAG_SAMPLES_PER_PIXEL = 277
TAG_ROWS_PER_STRIP = 278
TAG_STRIP_BYTE_COUNTS = 279
TAG_TILE_WIDTH = 322
TAG_TILE_LENGTH = 323
TAG_TILE_OFFSETS = 324
TAG_TILE_BYTE_COUNTS = 325
TAG_SUB_IFDS = 330
TAG_CFA_REPEAT_DIM = 33421
TAG_CFA_PATTERN = 33422
TAG_DNG_VERSION = 50706
TAG_UNIQUE_CAMERA_MODEL = 50708
TAG_BLACK_LEVEL_REPEAT_DIM = 50713
TAG_BLACK_LEVEL = 50714
TAG_WHITE_LEVEL = 50717
TAG_DEFAULT_CROP_ORIGIN = 50719
TAG_DEFAULT_CROP_SIZE = 50720
TAG_COLOR_MATRIX_1 = 50721
TAG_COLOR_MATRIX_2 = 50722
TAG_AS_SHOT_NEUTRAL = 50728
TAG_BASELINE_EXPOSURE = 50730
TAG_CALIBRATION_ILLUMINANT_1 = 50778
TAG_CALIBRATION_ILLUMINANT_2 = 50779
TAG_TIMECODES = 51043
TAG_FRAME_RATE = 51044
TAG_REEL_NAME = 51081
TAG_EXIF_IFD = 34665
TAG_ISO_SPEED = 34855

# Tags whose values may hold one entry per strip / tile.
_LAYOUT_TAGS = frozenset({TAG_STRIP_OFFSETS, TAG_STRIP_BYTE_COUNTS, TAG_TILE_OFFSETS, TAG_TILE_BYTE_COUNTS})
_STRING_TAGS = frozenset({TAG_MAKE, TAG_MODEL, TAG_UNIQUE_CAMERA_MODEL, TAG_REEL_NAME})
# DNG allows up to ten 8-byte SMPTE time codes.
_TAG_COUNT_LIMITS = {TAG_TIMECODES: 80}
# Every tag parse_dng_bytes reads. Anything else in an IFD is skipped without decoding its values.
_PARSED_TAGS = frozenset({
    TAG_NEW_SUBFILE_TYPE, TAG_IMAGE_WIDTH, TAG_IMAGE_LENGTH, TAG_BITS_PER_SAMPLE, TAG_COMPRESSION,
    TAG_PHOTOMETRIC, TAG_SAMPLES_PER_PIXEL, TAG_ROWS_PER_STRIP, TAG_TILE_WIDTH, TAG_TILE_LENGTH,
    TAG_SUB_IFDS, TAG_CFA_PATTERN, TAG_BLACK_LEVEL, TAG_WHITE_LEVEL, TAG_COLOR_MATRIX_1, TAG_COLOR_MATRIX_2,
    TAG_AS_SHOT_NEUTRAL, TAG_BASELINE_EXPOSURE, TAG_CALIBRATION_ILLUMINANT_1, TAG_CALIBRATION_ILLUMINANT_2,
    TAG_TIMECODES, TAG_FRAME_RATE, TAG_EXIF_IFD, TAG_ISO_SPEED,
}) | _LAYOUT_TAGS | _STRING_TAGS

# TIFF field type sizes & struct codes
_TIFF_TYPES = {
    1: (1, 'B'),   # BYTE
    2: (1, 's'),   # ASCII
    3: (2, 'H'),   # SHORT
    4: (4, 'I'),   # LONG
    5: (8, 'II'),  # RATIONAL
    6: (1, 'b'),   # SBYTE
    7: (1, 'B'),   # UNDEFINED
    8: (2, 'h'),   # SSHORT
    9: (4, 'i'),   # SLONG
    10: (8, 'ii'), # SRATIONAL
    11: (4, 'f'),  # FLOAT
    12: (8, 'd'),  # DOUBLE
    13: (4, 'I'),  # IFD (SubIFDs offsets)
}

# Standard EXIF / DNG CalibrationIlluminant Kelvin temperatures
ILLUMINANT_KELVIN = {
    1: 5500.0,   # Daylight
    2: 4200.0,   # Fluorescent
    3: 2856.0,   # Tungsten (incandescent)
    4: 5500.0,   # Flash
    9: 5500.0,   # Fine weather
    10: 6500.0,  # Cloudy weather
    11: 7500.0,  # Shade
    17: 2856.0,  # Standard Light A
    18: 4874.0,  # Standard Light B
    19: 6774.0,  # Standard Light C
    20: 5503.0,  # D55
    21: 6504.0,  # D65
    22: 7504.0,  # D75
    23: 5003.0,  # D50
    24: 3200.0,  # ISO Studio Tungsten
}

# D50 white point in CIE XYZ (Y = 1)
XYZ_D50 = (0.96422, 1.0, 0.82521)
XYZ_D65 = (0.95047, 1.0, 1.08883)

# Bradford cone response matrix & inverse
_BRADFORD = (
    (0.8951000, 0.2664000, -0.1614000),
    (-0.7502000, 1.7135000, 0.0367000),
    (0.0389000, -0.0685000, 1.0296000),
)
_BRADFORD_INV = (
    (0.9869929, -0.1470543, 0.1599627),
    (0.4323053, 0.5183603, 0.0492912),
    (-0.0085287, 0.0400428, 0.9684867),
)

# CIE XYZ (D65) -> Working Color Space RGB matrices
XYZ_D65_TO_GAMUT = {
    'rec709': (
        (3.2404542, -1.5371385, -0.4985314),
        (-0.9692660, 1.8760108, 0.0415560),
        (0.0556434, -0.2040259, 1.0572252),
    ),
    'linear': (
        (3.2404542, -1.5371385, -0.4985314),
        (-0.9692660, 1.8760108, 0.0415560),
        (0.0556434, -0.2040259, 1.0572252),
    ),
    'rec2020': (
        (1.7166512, -0.3556708, -0.2533663),
        (-0.6666844, 1.6164812, 0.0157685),
        (0.0176399, -0.0427706, 0.9421031),
    ),
    'bmd_wide_gamut_gen5': (
        (1.8663820, -0.5183970, -0.2346100),
        (-0.6003420, 1.3781490, 0.1767320),
        (-0.0024520, 0.0864000, 0.8369430),
    ),
    'red_wide_gamut': (
        (1.3728210, -0.2140170, -0.1588040),
        (-0.5621350, 1.4418190, 0.1203160),
        (0.0680800, -0.1304300, 1.0623500),
    ),
    'arri_wide_gamut_3': (
        (1.7890660, -0.4825340, -0.2000760),
        (-0.6398490, 1.3964000, 0.1944320),
        (-0.0415320, 0.0823350, 0.8788680),
    ),
    'arri_wide_gamut_4': (
        (1.6175230, -0.5372870, -0.0802370),
        (-0.0705730, 1.3346130, -0.2640400),
        (-0.0211020, -0.2269540, 1.2480560),
    ),
}

# Default sRGB/Rec.709 D50->Cam matrix when DNG lacks ColorMatrix1
DEFAULT_COLOR_MATRIX_D50 = (
    (3.1338561, -1.6168667, -0.4906146),
    (-0.9787684, 1.9161415, 0.0334540),
    (0.0719453, -0.2289914, 1.4052427),
)

_SEQ_RE = re.compile(r'^(.*?)(\d+)(\.[^.]+)$')
_CFA_NAMES = {
    (0, 1, 1, 2): 'RGGB',
    (1, 0, 2, 1): 'GRBG',
    (1, 2, 0, 1): 'GBRG',
    (2, 1, 1, 0): 'BGGR',
}


class RawError(ValueError):
    """Raised when a RAW file is malformed, unsupported, or cannot be decoded."""


# Exceptions a hostile header can provoke deep in parsing arithmetic; callers see them as RawError.
MALFORMED_ERRORS = (TypeError, IndexError, KeyError, ArithmeticError, struct.error)


def malformed(name: str, exc: BaseException) -> RawError:
    """A ``RawError`` describing ``exc`` raised while reading the malformed file ``name``."""
    return RawError(f'{name} has a malformed RAW header ({type(exc).__name__}: {str(exc)[:120]})')


# ---------------------------------------------------------------------------
# 3x3 matrix helpers
# ---------------------------------------------------------------------------

def _mat_vec(m, v):
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _mat_mul(a, b):
    return tuple(
        tuple(a[r][0] * b[0][c] + a[r][1] * b[1][c] + a[r][2] * b[2][c] for c in range(3))
        for r in range(3)
    )


def _mat_inv(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise RawError('Singular color matrix in DNG metadata')
    inv = 1.0 / det
    return (
        ((e * i - f * h) * inv, (c * h - b * i) * inv, (b * f - c * e) * inv),
        ((f * g - d * i) * inv, (a * i - c * g) * inv, (c * d - a * f) * inv),
        ((d * h - e * g) * inv, (b * g - a * h) * inv, (a * e - b * d) * inv),
    )


def _bradford_adapt(src_xyz, dst_xyz):
    src_cone = _mat_vec(_BRADFORD, src_xyz)
    dst_cone = _mat_vec(_BRADFORD, dst_xyz)
    scale = (
        (dst_cone[0] / max(1e-9, src_cone[0]), 0.0, 0.0),
        (0.0, dst_cone[1] / max(1e-9, src_cone[1]), 0.0),
        (0.0, 0.0, dst_cone[2] / max(1e-9, src_cone[2])),
    )
    return _mat_mul(_BRADFORD_INV, _mat_mul(scale, _BRADFORD))


# ---------------------------------------------------------------------------
# Color science: Kelvin/Tint <-> CIE XYZ <-> Camera WB, Transfer OETFs
# ---------------------------------------------------------------------------

def kelvin_tint_to_xyz(kelvin: float, tint: float = 0.0):
    """Convert correlated color temperature (2000..15000 K) and tint (-100..+100) to CIE XYZ (Y=1)."""
    t = max(2000.0, min(15000.0, float(kelvin)))
    if t <= 4000.0:
        x = -0.2661239e9 / (t ** 3) - 0.2343589e6 / (t ** 2) + 0.8776956e3 / t + 0.179910
        y = -1.1063814 * (x ** 3) - 1.34811020 * (x ** 2) + 2.18555832 * x - 0.20219683
    else:
        if t <= 7000.0:
            x = -4.6070e9 / (t ** 3) + 2.9678e6 / (t ** 2) + 0.09911e3 / t + 0.244063
        else:
            x = -2.0064e9 / (t ** 3) + 1.9018e6 / (t ** 2) + 0.24748e3 / t + 0.237040
        y = -3.000 * (x ** 2) + 2.870 * x - 0.275
    if tint:
        # Apply tint perpendicular to the Planckian locus in CIE 1960 UCS (u, v).
        # Positive tint = magenta (negative Duv -> less green); negative tint = green.
        denom = -2.0 * x + 12.0 * y + 3.0
        u = 4.0 * x / denom
        v = 6.0 * y / denom
        # Local locus tangent via finite step in Kelvin
        t2 = min(15000.0, t + 50.0) if t < 14950.0 else t - 50.0
        x2, y2 = _locus_xy(t2)
        d2 = -2.0 * x2 + 12.0 * y2 + 3.0
        du = 4.0 * x2 / d2 - u
        dv = 6.0 * y2 / d2 - v
        norm = math.hypot(du, dv) or 1.0
        # Normal pointing towards +v (green): dividing out a +Duv illuminant shifts image magenta (+tint).
        sign = 1.0 if t2 > t else -1.0
        nx, ny = -sign * dv / norm, sign * du / norm
        if ny < 0:
            nx, ny = -nx, -ny
        duv = max(-100.0, min(100.0, float(tint))) * 0.0003
        u = max(0.05, min(0.65, u + nx * duv))
        v = max(0.05, min(0.55, v + ny * duv))
        uv_denom = 2.0 * u - 8.0 * v + 4.0
        if uv_denom > 1e-6:
            x = 3.0 * u / uv_denom
            y = 2.0 * v / uv_denom
    y = max(0.05, y)
    return (x / y, 1.0, max(0.01, (1.0 - x - y) / y))


def _locus_xy(t: float):
    if t <= 4000.0:
        x = -0.2661239e9 / (t ** 3) - 0.2343589e6 / (t ** 2) + 0.8776956e3 / t + 0.179910
        y = -1.1063814 * (x ** 3) - 1.34811020 * (x ** 2) + 2.18555832 * x - 0.20219683
    else:
        if t <= 7000.0:
            x = -4.6070e9 / (t ** 3) + 2.9678e6 / (t ** 2) + 0.09911e3 / t + 0.244063
        else:
            x = -2.0064e9 / (t ** 3) + 1.9018e6 / (t ** 2) + 0.24748e3 / t + 0.237040
        y = -3.000 * (x ** 2) + 2.870 * x - 0.275
    return x, y


def interpolate_color_matrix(cm1, cm2, illum1, illum2, kelvin):
    """Interpolate XYZ->Camera matrix by inverse temperature (mireds), per the DNG specification."""
    if cm1 is None:
        return DEFAULT_COLOR_MATRIX_D50
    if cm2 is None:
        return cm1
    t1 = ILLUMINANT_KELVIN.get(illum1, 2856.0)
    t2 = ILLUMINANT_KELVIN.get(illum2, 6504.0)
    if abs(t1 - t2) < 1.0:
        return cm1
    inv_t = 1.0 / max(2000.0, min(15000.0, float(kelvin)))
    inv_1, inv_2 = 1.0 / t1, 1.0 / t2
    alpha = (inv_t - inv_1) / (inv_2 - inv_1)
    alpha = 0.0 if alpha < 0.0 else 1.0 if alpha > 1.0 else alpha
    return tuple(
        tuple(cm1[r][c] * (1.0 - alpha) + cm2[r][c] * alpha for c in range(3))
        for r in range(3)
    )


def as_shot_to_kelvin_tint(as_shot_neutral, cm1, cm2=None, illum1=17, illum2=21):
    """Estimate (kelvin, tint) from DNG AsShotNeutral and ColorMatrix1/2."""
    if not as_shot_neutral or len(as_shot_neutral) != 3:
        return 5600.0, 0.0
    neutral = tuple(max(1e-5, float(v)) for v in as_shot_neutral)
    kelvin = 5600.0
    xyz = XYZ_D65
    for _ in range(6):
        cm = interpolate_color_matrix(cm1, cm2, illum1, illum2, kelvin)
        try:
            xyz = _mat_vec(_mat_inv(cm), neutral)
        except RawError:
            return 5600.0, 0.0
        total = xyz[0] + xyz[1] + xyz[2]
        if total <= 1e-6:
            return 5600.0, 0.0
        x, y = xyz[0] / total, xyz[1] / total
        # McCamy CCT approximation
        n = (x - 0.3320) / (0.1858 - y) if abs(0.1858 - y) > 1e-6 else 0.0
        est = 449.0 * (n ** 3) + 3525.0 * (n ** 2) + 6823.3 * n + 5520.33
        kelvin = max(2000.0, min(15000.0, est))
    # Estimate tint from Duv against locus at estimated kelvin
    total = xyz[0] + xyz[1] + xyz[2]
    x, y = xyz[0] / total, xyz[1] / total
    lx, ly = _locus_xy(kelvin)
    d1, d2 = -2.0 * x + 12.0 * y + 3.0, -2.0 * lx + 12.0 * ly + 3.0
    v1, v2 = 6.0 * y / max(1e-6, d1), 6.0 * ly / max(1e-6, d2)
    tint = max(-100.0, min(100.0, (v1 - v2) / 0.0003))
    return round(kelvin), round(tint, 1)


def compute_wb_and_matrix(meta, kelvin, tint, color_space='rec709', use_as_shot=False):
    """Return ``(wb_gains_rgb, cam_to_working_3x3)`` for the requested white balance & gamut."""
    cm1 = meta.get('colorMatrix1')
    cm2 = meta.get('colorMatrix2')
    illum1 = meta.get('calibrationIlluminant1', 17)
    illum2 = meta.get('calibrationIlluminant2', 21)
    if use_as_shot and meta.get('asShotNeutral'):
        neutral = tuple(max(1e-5, float(v)) for v in meta['asShotNeutral'])
        est_k, _ = as_shot_to_kelvin_tint(neutral, cm1, cm2, illum1, illum2)
        xyz_to_cam = interpolate_color_matrix(cm1, cm2, illum1, illum2, est_k)
        cam_to_xyz = _mat_inv(xyz_to_cam)
        xyz_wb = _mat_vec(cam_to_xyz, neutral)
        if xyz_wb[1] > 1e-6:
            xyz_wb = (xyz_wb[0] / xyz_wb[1], 1.0, xyz_wb[2] / xyz_wb[1])
        else:
            xyz_wb = XYZ_D65
    else:
        xyz_wb = kelvin_tint_to_xyz(kelvin, tint)
        xyz_to_cam = interpolate_color_matrix(cm1, cm2, illum1, illum2, kelvin)
        cam_to_xyz = _mat_inv(xyz_to_cam)
        # Camera response to the chosen scene illuminant (adapted to D50 matrix domain)
        adapt_to_d50 = _bradford_adapt(xyz_wb, XYZ_D50)
        # An object reflecting xyz_wb has camera neutral = xyz_to_cam @ xyz_wb
        neutral = _mat_vec(xyz_to_cam, xyz_wb)
        neutral = tuple(max(1e-4, v) for v in neutral)
        _ = adapt_to_d50

    # WB gains: normalize green gain to 1.0
    wb = (neutral[1] / neutral[0], 1.0, neutral[1] / neutral[2])
    # Map balanced camera RGB -> XYZ D50 -> adapt to D65 -> Working Gamut
    # Since neutral camera pixel becomes (n_g, n_g, n_g) after wb, undo wb diagonal before cam_to_xyz
    diag_neutral = (
        (neutral[0] / neutral[1], 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, neutral[2] / neutral[1]),
    )
    cam_balanced_to_xyz = _mat_mul(cam_to_xyz, diag_neutral)
    adapt = _bradford_adapt(xyz_wb, XYZ_D65)
    xyz_to_work = XYZ_D65_TO_GAMUT.get(color_space, XYZ_D65_TO_GAMUT['rec709'])
    full = _mat_mul(xyz_to_work, _mat_mul(adapt, cam_balanced_to_xyz))
    # Row-normalize so achromatic (1, 1, 1) maps exactly to (1, 1, 1)
    norm_rows = []
    for row in full:
        s = row[0] + row[1] + row[2]
        if abs(s) > 1e-6:
            norm_rows.append((row[0] / s, row[1] / s, row[2] / s))
        else:
            norm_rows.append(row)
    return wb, tuple(norm_rows)


def transfer_encode(value: float, gamma: str) -> float:
    """Apply working OETF to linear light value in ``[0, 1+]``."""
    v = 0.0 if value < 0.0 else value
    if gamma == 'linear':
        return 1.0 if v > 1.0 else v
    if gamma == 'rec709':
        if v >= 1.0:
            return 1.0
        return 4.5 * v if v < 0.018 else 1.099 * (v ** 0.45) - 0.099
    if gamma == 'cinemadng_log':
        out = math.log2(v * 15.0 + 1.0) / 4.0
        return 1.0 if out > 1.0 else out
    if gamma == 'bmd_film_gen5':
        # Published Blackmagic Film Gen 5 OETF
        a, b, c, d, e, cut = 0.08692876065491224, 0.005494072432257808, 0.5300133392291939, 8.283605932402494, 0.09246575342465753, 0.005
        out = d * v + e if v < cut else a * math.log(v + b) + c
        return 0.0 if out < 0.0 else 1.0 if out > 1.0 else out
    if gamma == 'bmd_extended_video':
        # Smooth filmic shoulder
        if v >= 1.0:
            return 1.0
        return (1.15 * v) / (v + 0.15) if v > 0.0 else 0.0
    if gamma == 'log3g10':
        # Published RED Log3G10 OETF
        a, b, c, g = 0.224282, 155.975327, 0.01, 15.1927
        x = v + c
        out = x * g if x < 0.0 else a * math.log10(x * b + 1.0)
        return 0.0 if out < 0.0 else 1.0 if out > 1.0 else out
    if gamma == 'arri_logc3':
        # Published ARRI LogC3 (EI 800) OETF
        cut, a, b, c, d, e, f = 0.010591, 5.555556, 0.052272, 0.247190, 0.385537, 5.367655, 0.092809
        out = c * math.log10(a * v + b) + d if v > cut else e * v + f
        return 0.0 if out < 0.0 else 1.0 if out > 1.0 else out
    if gamma == 'arri_logc4':
        # Published ARRI LogC4 OETF
        a = (2.0 ** 18 - 16.0) / 117.45
        b = (1023.0 - 95.0) / 1023.0
        c = 95.0 / 1023.0
        s = (7.0 * math.log(2.0) * (2.0 ** (7.0 - 14.0 * c / b))) / (a * b)
        t = (2.0 ** (14.0 * (-c / b) + 6.0) - 64.0) / a
        out = (v - t) / s if v < t else (math.log2(a * v + 64.0) - 6.0) / 14.0 * b + c
        return 0.0 if out < 0.0 else 1.0 if out > 1.0 else out
    # Default Rec.709
    if v >= 1.0:
        return 1.0
    return 4.5 * v if v < 0.018 else 1.099 * (v ** 0.45) - 0.099


@lru_cache(maxsize=16)
def _oetf_lut16(gamma: str):
    """65,536-entry 16-bit OETF lookup table (maps linear 0..1 -> uint16 0..65535)."""
    inv = 1.0 / 65535.0
    return array('H', (max(0, min(65535, round(transfer_encode(i * inv, gamma) * 65535.0))) for i in range(65536)))


# ---------------------------------------------------------------------------
# TIFF / DNG / DNG-EP IFD parser
# ---------------------------------------------------------------------------

def _tag_limit(tag: int) -> int:
    if tag in _LAYOUT_TAGS:
        return MAX_LAYOUT_VALUES
    if tag in _STRING_TAGS:
        return MAX_STRING_BYTES
    return _TAG_COUNT_LIMITS.get(tag, MAX_METADATA_VALUES)


def _read_ifd(data: memoryview, endian: str, offset: int, seen: set, budget: list | None = None):
    """Read one IFD. Returns ``(tags, next_offset)``.

    Only tags in ``_PARSED_TAGS`` are decoded; every other entry is skipped unread, however large
    its declared count. A parsed tag over its count cap, or a file whose parsed tags together pass
    ``MAX_DECODED_VALUES`` (``budget`` is a one-item list shared across a file's IFDs), raises
    ``RawError``. RATIONAL values decode to ``Fraction`` (``None`` for a zero denominator); a tag
    with more than one value decodes to a tuple.
    """
    if budget is None:
        budget = [MAX_DECODED_VALUES]
    if offset <= 0 or offset + 2 > len(data):
        raise RawError('Truncated DNG IFD header')
    if offset in seen:
        raise RawError('Cyclic IFD chain in DNG file')
    if len(seen) >= MAX_IFDS:
        raise RawError(f'DNG file chains more than {MAX_IFDS} IFDs')
    seen.add(offset)
    count = struct.unpack_from(endian + 'H', data, offset)[0]
    if count > MAX_IFD_ENTRIES:
        raise RawError('DNG IFD exceeds maximum entry count')
    end = offset + 2 + count * 12 + 4
    if end > len(data):
        raise RawError('Truncated DNG IFD entries')
    tags = {}
    pos = offset + 2
    for _ in range(count):
        tag, typ, cnt, val_bytes = struct.unpack_from(endian + 'HHI4s', data, pos)
        pos += 12
        if tag not in _PARSED_TAGS:
            continue
        spec = _TIFF_TYPES.get(typ)
        if spec is None or cnt == 0:
            continue
        limit = _tag_limit(tag)
        if cnt > limit:
            raise RawError(f'DNG tag {tag} declares {cnt} values (the limit is {limit})')
        budget[0] -= cnt
        if budget[0] < 0:
            raise RawError(f'DNG header declares more than {MAX_DECODED_VALUES} tag values')
        unit_size, code = spec
        total = unit_size * cnt
        if total <= 4:
            raw_slice = val_bytes[:total]
        else:
            val_offset = struct.unpack_from(endian + 'I', val_bytes, 0)[0]
            if val_offset + total > len(data):
                continue
            raw_slice = data[val_offset:val_offset + total]
        if typ == 2:  # ASCII
            tags[tag] = bytes(raw_slice).split(b'\x00', 1)[0].decode('utf-8', 'replace').strip()
        elif typ in (5, 10):  # RATIONAL / SRATIONAL
            flat = struct.unpack_from(endian + code[0] * (2 * cnt), raw_slice, 0)
            vals = tuple(Fraction(flat[i], flat[i + 1]) if flat[i + 1] else None for i in range(0, 2 * cnt, 2))
            tags[tag] = vals[0] if cnt == 1 else vals
        else:
            vals = struct.unpack_from(endian + code * cnt, raw_slice, 0)
            tags[tag] = vals[0] if cnt == 1 else vals
    next_offset = struct.unpack_from(endian + 'I', data, pos)[0]
    return tags, next_offset


def _as_list(val):
    if val is None:
        return []
    return list(val) if isinstance(val, (list, tuple)) else [val]


def _rat_float(val, default=0.0):
    """A finite float from an integer, float, ``Fraction`` or legacy ``(num, den)`` pair, else ``default``."""
    if isinstance(val, tuple) and len(val) == 2 and all(isinstance(v, int) for v in val):
        val = Fraction(val[0], val[1]) if val[1] else None
    if isinstance(val, bool) or not isinstance(val, (int, float, Fraction)):
        return default
    try:
        out = float(val)
    except (OverflowError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _tag_int(value, tag: int, name: str) -> int:
    """Coerce one decoded TIFF value to an int, or raise ``RawError``."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction) and value.denominator == 1:
        return int(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise RawError(f'{name}: TIFF tag {tag} is not an integer ({type(value).__name__})')


def _scalar_int(ifd: dict, tag: int, default, name: str, *, first_of_many: bool = False):
    """Read a tag that must hold one integer. Tuples raise ``RawError`` unless ``first_of_many``
    (BitsPerSample, which repeats per sample)."""
    value = ifd.get(tag)
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        if not value:
            return default
        if len(value) != 1 and not first_of_many:
            raise RawError(f'{name}: TIFF tag {tag} holds {len(value)} values where one is expected')
        value = value[0]
    return _tag_int(value, tag, name)


def _int_list(ifd: dict, tag: int, name: str) -> list[int]:
    return [_tag_int(v, tag, name) for v in _as_list(ifd.get(tag))]


def _area_or_zero(ifd: dict) -> int:
    """Image area for choosing the primary IFD; a malformed size counts as empty."""
    try:
        return _scalar_int(ifd, TAG_IMAGE_WIDTH, 0, '') * _scalar_int(ifd, TAG_IMAGE_LENGTH, 0, '')
    except RawError:
        return 0


def _parse_matrix(val):
    if not val:
        return None
    items = list(val) if isinstance(val, (list, tuple)) else [val]
    if len(items) != 9:
        return None
    floats = [_rat_float(v, 0.0) for v in items]
    return (
        (floats[0], floats[1], floats[2]),
        (floats[3], floats[4], floats[5]),
        (floats[6], floats[7], floats[8]),
    )


def _parse_dng_timecode(val):
    """Decode DNG TimeCodes tag (8-byte SMPTE 12M BCD)."""
    if not val:
        return None, False
    if isinstance(val, (list, tuple)) and all(isinstance(v, int) and 0 <= v <= 255 for v in val):
        raw = bytes(val)
    elif isinstance(val, (bytes, bytearray)):
        raw = bytes(val)
    else:
        raw = b''
    if len(raw) < 4:
        return None, False
    b0, b1, b2, b3 = raw[0], raw[1], raw[2], raw[3]
    drop = bool(b0 & 0x40)
    frames = (b0 & 0x0F) + 10 * ((b0 >> 4) & 0x03)
    secs = (b1 & 0x0F) + 10 * ((b1 >> 4) & 0x07)
    mins = (b2 & 0x0F) + 10 * ((b2 >> 4) & 0x07)
    hours = (b3 & 0x0F) + 10 * ((b3 >> 4) & 0x03)
    if hours > 23 or mins > 59 or secs > 59 or frames > 120:
        return None, False
    sep = ';' if drop else ':'
    return f'{hours:02d}:{mins:02d}:{secs:02d}{sep}{frames:02d}', drop


def parse_dng(path) -> dict:
    """Parse a TIFF/DNG file's IFDs and return structured metadata and image layout."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RawError(f'Cannot read DNG file {path.name}: {exc}') from None
    if size < 16 or size > MAX_DNG_BYTES:
        raise RawError(f'{path.name} is not a valid CinemaDNG file (size {size} bytes)')
    raw_bytes = path.read_bytes()
    return parse_dng_bytes(raw_bytes, name=path.name)


def parse_dng_bytes(raw_bytes: bytes, name: str = 'frame.dng') -> dict:
    """Parse DNG metadata from raw bytes. Any malformed header raises ``RawError``."""
    try:
        return _parse_dng_bytes(raw_bytes, name)
    except MALFORMED_ERRORS as exc:
        raise malformed(name, exc) from None


def _parse_dng_bytes(raw_bytes: bytes, name: str) -> dict:
    if len(raw_bytes) < 16:
        raise RawError(f'{name} is truncated (not a valid TIFF/DNG header)')
    view = memoryview(raw_bytes)
    magic = bytes(view[:2])
    if magic == b'II':
        endian = '<'
    elif magic == b'MM':
        endian = '>'
    else:
        raise RawError(f'{name} is not a valid TIFF/DNG file (bad byte-order mark)')
    version, first_ifd = struct.unpack_from(endian + 'HI', view, 2)
    if version != 42:
        raise RawError(f'{name} is not a standard TIFF/DNG file (version {version})')
    seen = set()
    budget = [MAX_DECODED_VALUES]
    ifd0, next_ifd = _read_ifd(view, endian, first_ifd, seen, budget)
    candidates = [ifd0]

    def walk_sub(ifd, depth):
        if depth >= MAX_IFD_DEPTH:
            return
        for sub_off in _as_list(ifd.get(TAG_SUB_IFDS)):
            if isinstance(sub_off, int) and sub_off > 0 and sub_off not in seen:
                sub, _ = _read_ifd(view, endian, sub_off, seen, budget)
                candidates.append(sub)
                walk_sub(sub, depth + 1)

    walk_sub(ifd0, 0)
    exif = {}
    exif_offset = ifd0.get(TAG_EXIF_IFD)
    if isinstance(exif_offset, int) and exif_offset > 0 and exif_offset not in seen:
        try:  # Only ISO is read from it: a damaged EXIF IFD leaves ISO unknown, not the frame unreadable.
            exif, _ = _read_ifd(view, endian, exif_offset, seen, budget)
        except RawError:
            exif = {}
    while next_ifd and len(candidates) < 16:
        if next_ifd in seen:
            break
        nxt, next_ifd = _read_ifd(view, endian, next_ifd, seen, budget)
        candidates.append(nxt)
        walk_sub(nxt, 0)

    # Prefer full-res CFA IFD (Photometric 32803 and SubfileType 0), then largest CFA IFD, then IFD0
    cfa_ifds = [i for i in candidates if i.get(TAG_PHOTOMETRIC) == 32803]
    if cfa_ifds:
        primary = max(
            cfa_ifds,
            key=lambda i: (i.get(TAG_NEW_SUBFILE_TYPE, 0) == 0, _area_or_zero(i)),
        )
    else:
        primary = max(candidates, key=_area_or_zero)

    # Merge IFD0 metadata with primary image IFD
    merged = {**ifd0, **primary}
    width = _scalar_int(merged, TAG_IMAGE_WIDTH, 0, name)
    height = _scalar_int(merged, TAG_IMAGE_LENGTH, 0, name)
    if not (2 <= width <= MAX_WIDTH and 2 <= height <= MAX_HEIGHT and width * height <= MAX_PIXELS):
        raise RawError(f'{name} has unsupported dimensions ({width}x{height})')
    bps = _scalar_int(merged, TAG_BITS_PER_SAMPLE, 12, name, first_of_many=True)
    compression = _scalar_int(merged, TAG_COMPRESSION, 1, name)
    photometric = _scalar_int(merged, TAG_PHOTOMETRIC, 32803, name)
    samples = _scalar_int(merged, TAG_SAMPLES_PER_PIXEL, 1, name)

    cfa_raw = _int_list(merged, TAG_CFA_PATTERN, name)
    if len(cfa_raw) == 4:
        cfa_tuple = tuple(cfa_raw)
    else:
        cfa_tuple = (0, 1, 1, 2)  # Default RGGB
    cfa_name = _CFA_NAMES.get(cfa_tuple, 'RGGB')

    # Black & White levels
    bl_raw = _as_list(merged.get(TAG_BLACK_LEVEL))
    if not bl_raw:
        black_levels = (0.0, 0.0, 0.0, 0.0)
    else:
        bl_floats = [_rat_float(v, 0.0) for v in bl_raw]
        if len(bl_floats) == 1:
            black_levels = (bl_floats[0],) * 4
        elif len(bl_floats) >= 4:
            black_levels = tuple(bl_floats[:4])
        else:
            black_levels = (bl_floats[0],) * 4

    wl_raw = _as_list(merged.get(TAG_WHITE_LEVEL))
    max_val = float((1 << bps) - 1) if 1 <= bps <= 16 else 65535.0
    if not wl_raw:
        white_level = max_val
    else:
        white_level = float(_rat_float(wl_raw[0], max_val))
        if white_level <= max(black_levels):
            white_level = max_val

    cm1 = _parse_matrix(merged.get(TAG_COLOR_MATRIX_1))
    cm2 = _parse_matrix(merged.get(TAG_COLOR_MATRIX_2))
    illum1 = _scalar_int(merged, TAG_CALIBRATION_ILLUMINANT_1, 17, name) or 17
    illum2 = _scalar_int(merged, TAG_CALIBRATION_ILLUMINANT_2, 21, name) or 21

    asn_raw = _as_list(merged.get(TAG_AS_SHOT_NEUTRAL))
    as_shot_neutral = None
    if len(asn_raw) == 3:
        vals = tuple(_rat_float(v, 1.0) for v in asn_raw)
        if all(v > 0 for v in vals):
            as_shot_neutral = vals

    baseline_exposure = _rat_float(merged.get(TAG_BASELINE_EXPOSURE), 0.0)
    baseline_exposure = max(-MAX_BASELINE_EXPOSURE, min(MAX_BASELINE_EXPOSURE, baseline_exposure))
    fps_val = merged.get(TAG_FRAME_RATE)
    fps_fraction = None
    if isinstance(fps_val, Fraction) and 0 < fps_val <= 1000:
        fps_fraction = fps_val
    elif isinstance(fps_val, (int, float)) and not isinstance(fps_val, bool) and 0 < fps_val <= 1000:
        fps_fraction = Fraction(str(fps_val)).limit_denominator(1001)

    timecode, drop_frame = _parse_dng_timecode(merged.get(TAG_TIMECODES))
    reel = merged.get(TAG_REEL_NAME) if isinstance(merged.get(TAG_REEL_NAME), str) else None
    camera_model = (
        (merged.get(TAG_UNIQUE_CAMERA_MODEL) if isinstance(merged.get(TAG_UNIQUE_CAMERA_MODEL), str) else '')
        or ' '.join(p for p in (merged.get(TAG_MAKE), merged.get(TAG_MODEL)) if isinstance(p, str) and p)
    ).strip()
    camera = camera_model or 'CinemaDNG Camera'
    iso = None
    for source in (merged, exif):
        try:
            value = _scalar_int(source, TAG_ISO_SPEED, None, name, first_of_many=True)
        except RawError:
            value = None
        if value and 0 < value <= 409600:
            iso = value
            break

    strip_offsets = _int_list(primary, TAG_STRIP_OFFSETS, name)
    strip_counts = _int_list(primary, TAG_STRIP_BYTE_COUNTS, name)
    rows_per_strip = _scalar_int(primary, TAG_ROWS_PER_STRIP, 0, name) or height
    tile_width = _scalar_int(primary, TAG_TILE_WIDTH, 0, name)
    tile_length = _scalar_int(primary, TAG_TILE_LENGTH, 0, name)
    tile_offsets = _int_list(primary, TAG_TILE_OFFSETS, name)
    tile_counts = _int_list(primary, TAG_TILE_BYTE_COUNTS, name)
    if len(strip_offsets) > height or len(strip_counts) > height:
        raise RawError(f'{name} declares more strips than image rows')
    if tile_width < 0 or tile_length < 0 or tile_width > MAX_WIDTH or tile_length > MAX_HEIGHT:
        raise RawError(f'{name} has unsupported tile size ({tile_width}x{tile_length})')
    if tile_width and tile_length:
        tile_total = ((width + tile_width - 1) // tile_width) * ((height + tile_length - 1) // tile_length)
        if len(tile_offsets) > tile_total or len(tile_counts) > tile_total:
            raise RawError(f'{name} declares more tiles than its {tile_total}-tile layout')

    builtin_decodable = (
        compression == 1
        and photometric == 32803
        and samples == 1
        and bps in (8, 10, 12, 14, 16)
        and (bool(strip_offsets) or (bool(tile_offsets) and tile_width > 0 and tile_length > 0))
    )

    kelvin, tint = as_shot_to_kelvin_tint(as_shot_neutral, cm1, cm2, illum1, illum2)
    return {
        'endian': endian,
        'width': width,
        'height': height,
        'bitsPerSample': bps,
        'compression': compression,
        'photometric': photometric,
        'samplesPerPixel': samples,
        'cfaPattern': cfa_tuple,
        'cfaName': cfa_name,
        'blackLevel': black_levels,
        'whiteLevel': white_level,
        'colorMatrix1': cm1,
        'colorMatrix2': cm2,
        'calibrationIlluminant1': illum1,
        'calibrationIlluminant2': illum2,
        'asShotNeutral': as_shot_neutral,
        'baselineExposure': baseline_exposure,
        'kelvin': kelvin,
        'tint': tint,
        'fps': fps_fraction,
        'timecode': timecode,
        'dropFrame': drop_frame,
        'reel': reel or None,
        'camera': str(camera),
        # What the file itself recorded; the rest above is a placeholder the probe flags as estimated.
        'cameraKnown': bool(camera_model),
        'whiteBalanceKnown': as_shot_neutral is not None,
        'iso': iso,
        'builtinDecodable': builtin_decodable,
        'compressed': compression != 1,
        'stripOffsets': strip_offsets,
        'stripByteCounts': strip_counts,
        'rowsPerStrip': rows_per_strip,
        'tileWidth': tile_width,
        'tileLength': tile_length,
        'tileOffsets': tile_offsets,
        'tileByteCounts': tile_counts,
    }


# ---------------------------------------------------------------------------
# Bayer Unpacking (8 / 10 / 12 / 14 / 16-bit, strips & tiles)
# ---------------------------------------------------------------------------

def _unpack_samples(buf: memoryview, count: int, bps: int, endian: str) -> array:
    """Unpack ``count`` Bayer samples from ``buf`` into ``array('H')``."""
    if bps == 16:
        need = count * 2
        if len(buf) < need:
            raise RawError('Truncated 16-bit Bayer strip/tile')
        arr = array('H')
        arr.frombytes(buf[:need])
        native_little = sys.byteorder == 'little'
        if (endian == '<') != native_little:
            arr.byteswap()
        return arr
    if bps == 8:
        if len(buf) < count:
            raise RawError('Truncated 8-bit Bayer strip/tile')
        return array('H', buf[:count])
    if bps == 12:
        # Standard TIFF/DNG MSB-first packed 12-bit: 2 pixels per 3 bytes
        pairs = count // 2
        rem = count & 1
        need = pairs * 3 + (2 if rem else 0)
        if len(buf) < need:
            raise RawError('Truncated 12-bit Bayer strip/tile')
        out = [0] * count
        b = buf
        oi = 0
        for bi in range(0, pairs * 3, 3):
            b0, b1, b2 = b[bi], b[bi + 1], b[bi + 2]
            out[oi] = (b0 << 4) | (b1 >> 4)
            out[oi + 1] = ((b1 & 0x0F) << 8) | b2
            oi += 2
        if rem:
            bi = pairs * 3
            out[oi] = (b[bi] << 4) | (b[bi + 1] >> 4)
        return array('H', out)
    if bps == 10:
        # MSB-first packed 10-bit: 4 pixels per 5 bytes
        groups = count // 4
        rem = count & 3
        need = groups * 5 + ((rem * 10 + 7) // 8)
        if len(buf) < need:
            raise RawError('Truncated 10-bit Bayer strip/tile')
        out = [0] * count
        b = buf
        oi = 0
        for bi in range(0, groups * 5, 5):
            b0, b1, b2, b3, b4 = b[bi], b[bi + 1], b[bi + 2], b[bi + 3], b[bi + 4]
            out[oi] = (b0 << 2) | (b1 >> 6)
            out[oi + 1] = ((b1 & 0x3F) << 4) | (b2 >> 4)
            out[oi + 2] = ((b2 & 0x0F) << 6) | (b3 >> 2)
            out[oi + 3] = ((b3 & 0x03) << 8) | b4
            oi += 4
        if rem:
            bi = groups * 5
            bits = int.from_bytes(b[bi:need], 'big')
            total_bits = (need - bi) * 8
            for k in range(rem):
                shift = total_bits - (k + 1) * 10
                out[oi + k] = (bits >> shift) & 0x3FF
        return array('H', out)
    if bps == 14:
        # MSB-first packed 14-bit: 4 pixels per 7 bytes
        groups = count // 4
        rem = count & 3
        need = groups * 7 + ((rem * 14 + 7) // 8)
        if len(buf) < need:
            raise RawError('Truncated 14-bit Bayer strip/tile')
        out = [0] * count
        b = buf
        oi = 0
        for bi in range(0, groups * 7, 7):
            b0, b1, b2, b3, b4, b5, b6 = b[bi:bi + 7]
            out[oi] = (b0 << 6) | (b1 >> 2)
            out[oi + 1] = ((b1 & 0x03) << 12) | (b2 << 4) | (b3 >> 4)
            out[oi + 2] = ((b3 & 0x0F) << 10) | (b4 << 2) | (b5 >> 6)
            out[oi + 3] = ((b5 & 0x3F) << 8) | b6
            oi += 4
        if rem:
            bi = groups * 7
            bits = int.from_bytes(b[bi:need], 'big')
            total_bits = (need - bi) * 8
            for k in range(rem):
                shift = total_bits - (k + 1) * 14
                out[oi + k] = (bits >> shift) & 0x3FFF
        return array('H', out)
    raise RawError(f'Unsupported CinemaDNG bit depth: {bps}')


def _read_bayer_plane(raw_bytes: bytes, meta: dict) -> array:
    """Extract the full W*H Bayer sensor array('H') from strips or tiles."""
    w, h = meta['width'], meta['height']
    bps, endian = meta['bitsPerSample'], meta['endian']
    view = memoryview(raw_bytes)
    if meta['stripOffsets']:
        offsets = meta['stripOffsets']
        counts = meta['stripByteCounts']
        rps = max(1, meta['rowsPerStrip'])
        if len(offsets) == 1:
            off = offsets[0]
            byte_len = counts[0] if counts else len(raw_bytes) - off
            if off < 0 or off + byte_len > len(raw_bytes):
                raise RawError('DNG strip offset out of bounds')
            return _unpack_samples(view[off:off + byte_len], w * h, bps, endian)
        plane = array('H')
        rows_left = h
        for idx, off in enumerate(offsets):
            rows_here = min(rps, rows_left)
            if rows_here <= 0:
                break
            byte_len = counts[idx] if idx < len(counts) else len(raw_bytes) - off
            if off < 0 or off + byte_len > len(raw_bytes):
                raise RawError('DNG strip offset out of bounds')
            plane.extend(_unpack_samples(view[off:off + byte_len], w * rows_here, bps, endian))
            rows_left -= rows_here
        if len(plane) < w * h:
            raise RawError('Incomplete DNG strip data')
        return plane
    # Tile layout
    tw, tl = meta['tileWidth'], meta['tileLength']
    offsets, counts = meta['tileOffsets'], meta['tileByteCounts']
    tiles_x = (w + tw - 1) // tw
    tiles_y = (h + tl - 1) // tl
    plane = array('H', [0]) * (w * h)
    idx = 0
    for ty in range(tiles_y):
        y0 = ty * tl
        rh = min(tl, h - y0)
        for tx in range(tiles_x):
            x0 = tx * tw
            rw = min(tw, w - x0)
            if idx >= len(offsets):
                raise RawError('Missing DNG tile offsets')
            off = offsets[idx]
            byte_len = counts[idx] if idx < len(counts) else len(raw_bytes) - off
            if off < 0 or off + byte_len > len(raw_bytes):
                raise RawError('DNG tile offset out of bounds')
            tile = _unpack_samples(view[off:off + byte_len], tw * tl, bps, endian)
            for row in range(rh):
                dst = (y0 + row) * w + x0
                src = row * tw
                plane[dst:dst + rw] = tile[src:src + rw]
            idx += 1
    return plane


# ---------------------------------------------------------------------------
# Bayer Demosaic + Color Pipeline -> 16-bit RGB (rgb48le)
# ---------------------------------------------------------------------------

def _highlight_recover_pixel(r: float, g: float, b: float, clip_r: float, clip_g: float, clip_b: float):
    """Reconstruct clipped highlight channels smoothly from unclipped channels.

    When 1 or 2 channels reach sensor saturation (after WB), standard clamping shifts highlights
    toward magenta/cyan. This blends clipped channels smoothly toward highlight luminance so
    speculars roll off neutrally without affecting midtones.
    """
    cr = r >= clip_r
    cg = g >= clip_g
    cb = b >= clip_b
    n_clip = (1 if cr else 0) + (1 if cg else 0) + (1 if cb else 0)
    if n_clip == 0:
        return r, g, b
    if n_clip == 3:
        m = max(r, g, b)
        return m, m, m
    # Average of unclipped channels gives neutral highlight luminance estimate
    unclipped = (0.0 if cr else r) + (0.0 if cg else g) + (0.0 if cb else b)
    unclipped_avg = unclipped / (3 - n_clip)
    peak = max(r, g, b)
    target = max(peak, unclipped_avg)
    # Desaturate towards neutral highlight luminance proportionally to how far past clip
    mix = 0.65 if n_clip == 1 else 0.85
    r = r + (target - r) * mix
    g = g + (target - g) * mix
    b = b + (target - b) * mix
    return r, g, b


def decode_dng_frame(path, settings: dict | None = None, *, scale: int = 1) -> tuple[int, int, bytes]:
    """Decode one uncompressed Bayer DNG file into ``(out_width, out_height, rgb48le_bytes)``.

    ``scale``: ``1`` (full-res Malvar-He-Cutler 5x5 demosaic), ``2`` (half-res 2x2 superpixel),
    ``4`` (quarter-res), ``8`` (eighth-res).
    """
    path = Path(path)
    raw_bytes = path.read_bytes()
    return decode_dng_bytes(raw_bytes, settings=settings, scale=scale, name=path.name)


def decode_dng_bytes(raw_bytes: bytes, settings: dict | None = None, *, scale: int = 1, name: str = 'frame.dng'):
    """Decode uncompressed Bayer DNG bytes into ``(out_w, out_h, rgb48le_bytes)``."""
    meta = parse_dng_bytes(raw_bytes, name=name)
    if not meta['builtinDecodable']:
        raise RawError(f'{name} uses compressed DNG (compression={meta["compression"]}); requires LibRaw/dcraw_emu')
    try:
        return decode_plane(_read_bayer_plane(raw_bytes, meta), meta, settings, scale=scale, name=name)
    except MALFORMED_ERRORS as exc:
        raise malformed(name, exc) from None


CFA_BY_PIX_FMT = {'rggb': (0, 1, 1, 2), 'bggr': (2, 1, 1, 0), 'grbg': (1, 0, 2, 1), 'gbrg': (1, 2, 0, 1)}


def decode_plane(plane, meta: dict, settings: dict | None = None, *, scale: int = 1, name: str = 'frame'):
    """Develop one W*H Bayer ``plane`` (``array('H')``) described by ``meta`` (``parse_dng`` keys:
    width, height, cfaPattern, blackLevel, whiteLevel, color matrices, as-shot neutral, baseline
    exposure) into ``(out_w, out_h, rgb48le_bytes)``."""
    if len(plane) < meta['width'] * meta['height']:
        raise RawError(f'{name} holds fewer Bayer samples than its {meta["width"]}x{meta["height"]} size')
    settings = settings or {}
    decode_using = settings.get('decodeUsing', 'camera')
    use_as_shot = decode_using == 'camera'
    base_iso = 800.0
    iso = float(settings.get('iso', base_iso)) if not use_as_shot else base_iso
    exposure = float(settings.get('exposure', 0.0)) if not use_as_shot else 0.0
    kelvin = float(settings.get('kelvin', meta['kelvin'])) if not use_as_shot else meta['kelvin']
    tint = float(settings.get('tint', meta['tint'])) if not use_as_shot else meta['tint']
    color_space = str(settings.get('colorSpace') or 'rec709')
    gamma = str(settings.get('gamma') or 'rec709')
    highlight_recovery = bool(settings.get('highlightRecovery', False)) if not use_as_shot else False

    wb, mat = compute_wb_and_matrix(meta, kelvin, tint, color_space=color_space, use_as_shot=use_as_shot)
    # BaselineExposure is clamped at parse time; clamp the sum too so no header or setting overflows.
    stops = exposure + _rat_float(meta.get('baselineExposure'), 0.0)
    stops = max(-2.0 * MAX_BASELINE_EXPOSURE, min(2.0 * MAX_BASELINE_EXPOSURE, stops))
    exp_gain = (2.0 ** stops) * (max(1.0, min(iso, 1_000_000.0)) / base_iso)
    # A hostile colour matrix can invert to huge or non-finite gains; per-pixel maths would then
    # overflow to inf and NaN. Real camera matrices and white balance gains are small numbers.
    if not math.isfinite(exp_gain) or not all(math.isfinite(v) and abs(v) < 1e6 for v in (*wb, *mat[0], *mat[1], *mat[2])):
        raise RawError(f'{name} has a colour matrix or white balance Splice cannot use')

    w, h = meta['width'], meta['height']
    cfa = meta['cfaPattern']  # (c00, c01, c10, c11) where 0=R, 1=G, 2=B
    bl = meta['blackLevel']
    wl = meta['whiteLevel']
    inv_span = [1.0 / max(1.0, wl - bl[i]) for i in range(4)]

    # Channel clip thresholds after WB and exposure (where raw hit whiteLevel)
    clip_r = wb[0] * exp_gain * 0.995
    clip_g = wb[1] * exp_gain * 0.995
    clip_b = wb[2] * exp_gain * 0.995

    # Combine exposure gain into WB gains for speed
    g_r, g_g, g_b = wb[0] * exp_gain, wb[1] * exp_gain, wb[2] * exp_gain
    m00, m01, m02 = mat[0]
    m10, m11, m12 = mat[1]
    m20, m21, m22 = mat[2]
    lut = _oetf_lut16(gamma)

    # Map CFA quad positions (00, 01, 10, 11) to R, G1, G2, B offsets
    if tuple(cfa) not in _CFA_NAMES:
        raise RawError(f'{name} has unsupported Bayer CFA pattern: {tuple(cfa)}')
    r_pos = cfa.index(0)
    b_pos = cfa.index(2)
    g_positions = [i for i, ch in enumerate(cfa) if ch == 1]
    if len(g_positions) != 2:
        raise RawError(f'{name} has invalid Bayer CFA pattern: {cfa}')
    g1_pos, g2_pos = g_positions

    if scale >= 2:
        step = 2 if scale == 2 else 4 if scale == 4 else 8
        out_w = max(2, (w // step) & ~1) if step > 2 else max(2, w // 2)
        out_h = max(2, (h // step) & ~1) if step > 2 else max(2, h // 2)
        if out_w * step > w:
            out_w = max(1, w // step)
        if out_h * step > h:
            out_h = max(1, h // step)
        out = array('H', [0]) * (out_w * out_h * 3)
        bl_r, sp_r = bl[r_pos], inv_span[r_pos] * g_r
        bl_g1, sp_g1 = bl[g1_pos], inv_span[g1_pos] * g_g * 0.5
        bl_g2, sp_g2 = bl[g2_pos], inv_span[g2_pos] * g_g * 0.5
        bl_b, sp_b = bl[b_pos], inv_span[b_pos] * g_b
        r_dy, r_dx = r_pos >> 1, r_pos & 1
        g1_dy, g1_dx = g1_pos >> 1, g1_pos & 1
        g2_dy, g2_dx = g2_pos >> 1, g2_pos & 1
        b_dy, b_dx = b_pos >> 1, b_pos & 1
        oi = 0
        for oy in range(out_h):
            y0 = oy * step
            row0 = y0 * w
            row1 = (y0 + 1) * w
            r_row = row0 if r_dy == 0 else row1
            g1_row = row0 if g1_dy == 0 else row1
            g2_row = row0 if g2_dy == 0 else row1
            b_row = row0 if b_dy == 0 else row1
            for ox in range(out_w):
                x0 = ox * step
                rv = (plane[r_row + x0 + r_dx] - bl_r) * sp_r
                gv = (plane[g1_row + x0 + g1_dx] - bl_g1) * sp_g1 + (plane[g2_row + x0 + g2_dx] - bl_g2) * sp_g2
                bv = (plane[b_row + x0 + b_dx] - bl_b) * sp_b
                if rv < 0.0:
                    rv = 0.0
                if gv < 0.0:
                    gv = 0.0
                if bv < 0.0:
                    bv = 0.0
                if highlight_recovery:
                    rv, gv, bv = _highlight_recover_pixel(rv, gv, bv, clip_r, clip_g, clip_b)
                rr = m00 * rv + m01 * gv + m02 * bv
                gg = m10 * rv + m11 * gv + m12 * bv
                bb = m20 * rv + m21 * gv + m22 * bv
                out[oi] = lut[0 if rr <= 0.0 else 65535 if rr >= 1.0 else int(rr * 65535.0 + 0.5)]
                out[oi + 1] = lut[0 if gg <= 0.0 else 65535 if gg >= 1.0 else int(gg * 65535.0 + 0.5)]
                out[oi + 2] = lut[0 if bb <= 0.0 else 65535 if bb >= 1.0 else int(bb * 65535.0 + 0.5)]
                oi += 3
        if sys.byteorder != 'little':
            out.byteswap()
        return out_w, out_h, out.tobytes()

    # Full-resolution (scale == 1): Linearize sensor plane first, then Malvar-He-Cutler (2004) 5x5 demosaic
    lin = [0.0] * (w * h)
    ch_gains = (g_r, g_g, g_b)
    row_scales = [
        (bl[0], inv_span[0] * ch_gains[cfa[0]], bl[1], inv_span[1] * ch_gains[cfa[1]]),
        (bl[2], inv_span[2] * ch_gains[cfa[2]], bl[3], inv_span[3] * ch_gains[cfa[3]]),
    ]
    for y in range(h):
        b0, s0, b1, s1 = row_scales[y & 1]
        base = y * w
        for x in range(0, w - 1, 2):
            v0 = (plane[base + x] - b0) * s0
            v1 = (plane[base + x + 1] - b1) * s1
            lin[base + x] = v0 if v0 > 0.0 else 0.0
            lin[base + x + 1] = v1 if v1 > 0.0 else 0.0
        if w & 1:
            v0 = (plane[base + w - 1] - b0) * s0
            lin[base + w - 1] = v0 if v0 > 0.0 else 0.0

    # Pre-classify row/col parity -> 0=R, 1=G_at_R_row, 2=G_at_B_row, 3=B
    r_row_par = r_pos >> 1
    cell_kind = [0, 0, 0, 0]
    for pos in range(4):
        ch = cfa[pos]
        if ch == 0:
            cell_kind[pos] = 0
        elif ch == 2:
            cell_kind[pos] = 3
        else:
            cell_kind[pos] = 1 if (pos >> 1) == r_row_par else 2

    out = array('H', [0]) * (w * h * 3)
    w2 = w * 2
    wm1, hm1 = w - 1, h - 1
    oi = 0
    for y in range(h):
        y_par = (y & 1) << 1
        row = y * w
        interior_y = 2 <= y <= h - 3
        k0 = cell_kind[y_par]
        k1 = cell_kind[y_par | 1]
        for x in range(w):
            idx = row + x
            kind = k1 if (x & 1) else k0
            c = lin[idx]
            if interior_y and 2 <= x <= w - 3:
                # Malvar-He-Cutler (2004) 5x5 gradient-corrected linear filters
                l1, r1 = lin[idx - 1], lin[idx + 1]
                u1, d1 = lin[idx - w], lin[idx + w]
                l2, r2 = lin[idx - 2], lin[idx + 2]
                u2, d2 = lin[idx - w2], lin[idx + w2]
                ul, ur = lin[idx - w - 1], lin[idx - w + 1]
                dl, dr = lin[idx + w - 1], lin[idx + w + 1]
                if kind == 0:  # R pixel
                    rv = c
                    gv = (4.0 * c + 2.0 * (u1 + d1 + l1 + r1) - (u2 + d2 + l2 + r2)) * 0.125
                    bv = (6.0 * c + 2.0 * (ul + ur + dl + dr) - 1.5 * (u2 + d2 + l2 + r2)) * 0.125
                elif kind == 3:  # B pixel
                    bv = c
                    gv = (4.0 * c + 2.0 * (u1 + d1 + l1 + r1) - (u2 + d2 + l2 + r2)) * 0.125
                    rv = (6.0 * c + 2.0 * (ul + ur + dl + dr) - 1.5 * (u2 + d2 + l2 + r2)) * 0.125
                elif kind == 1:  # G on R row (R left/right, B up/down)
                    gv = c
                    rv = (5.0 * c + 4.0 * (l1 + r1) - (ul + ur + dl + dr + l2 + r2) + 0.5 * (u2 + d2)) * 0.125
                    bv = (5.0 * c + 4.0 * (u1 + d1) - (ul + ur + dl + dr + u2 + d2) + 0.5 * (l2 + r2)) * 0.125
                else:  # G on B row (B left/right, R up/down)
                    gv = c
                    bv = (5.0 * c + 4.0 * (l1 + r1) - (ul + ur + dl + dr + l2 + r2) + 0.5 * (u2 + d2)) * 0.125
                    rv = (5.0 * c + 4.0 * (u1 + d1) - (ul + ur + dl + dr + u2 + d2) + 0.5 * (l2 + r2)) * 0.125
            else:
                # Safe clamped bilinear border demosaic
                xm = x - 1 if x > 0 else x + 1
                xp = x + 1 if x < wm1 else x - 1
                ym = (y - 1 if y > 0 else y + 1) * w
                yp = (y + 1 if y < hm1 else y - 1) * w
                if kind == 0:
                    rv = c
                    gv = 0.25 * (lin[row + xm] + lin[row + xp] + lin[ym + x] + lin[yp + x])
                    bv = 0.25 * (lin[ym + xm] + lin[ym + xp] + lin[yp + xm] + lin[yp + xp])
                elif kind == 3:
                    bv = c
                    gv = 0.25 * (lin[row + xm] + lin[row + xp] + lin[ym + x] + lin[yp + x])
                    rv = 0.25 * (lin[ym + xm] + lin[ym + xp] + lin[yp + xm] + lin[yp + xp])
                elif kind == 1:
                    gv = c
                    rv = 0.5 * (lin[row + xm] + lin[row + xp])
                    bv = 0.5 * (lin[ym + x] + lin[yp + x])
                else:
                    gv = c
                    bv = 0.5 * (lin[row + xm] + lin[row + xp])
                    rv = 0.5 * (lin[ym + x] + lin[yp + x])

            if rv < 0.0:
                rv = 0.0
            if gv < 0.0:
                gv = 0.0
            if bv < 0.0:
                bv = 0.0
            if highlight_recovery:
                rv, gv, bv = _highlight_recover_pixel(rv, gv, bv, clip_r, clip_g, clip_b)
            rr = m00 * rv + m01 * gv + m02 * bv
            gg = m10 * rv + m11 * gv + m12 * bv
            bb = m20 * rv + m21 * gv + m22 * bv
            out[oi] = lut[0 if rr <= 0.0 else 65535 if rr >= 1.0 else int(rr * 65535.0 + 0.5)]
            out[oi + 1] = lut[0 if gg <= 0.0 else 65535 if gg >= 1.0 else int(gg * 65535.0 + 0.5)]
            out[oi + 2] = lut[0 if bb <= 0.0 else 65535 if bb >= 1.0 else int(bb * 65535.0 + 0.5)]
            oi += 3
    if sys.byteorder != 'little':
        out.byteswap()
    return w, h, out.tobytes()


# ---------------------------------------------------------------------------
# CinemaDNG Sequence Discovery
# ---------------------------------------------------------------------------

def sequence_pattern(name: str):
    """If ``name`` ends in digits before its extension, return ``(prefix, digits, ext_lower)``."""
    match = _SEQ_RE.match(name)
    if not match:
        return None
    prefix, digits, ext = match.groups()
    if len(digits) < 2:
        return None
    return prefix, digits, ext.lower()


def detect_dng_sequence(path) -> dict:
    """Given any ``.dng`` file, find all sibling frames of its CinemaDNG sequence & sidecar WAV."""
    path = Path(path)
    parent = path.parent
    pat = sequence_pattern(path.name)
    frames = [path]
    missing = 0
    prefix = path.stem
    if pat is not None and parent.is_dir():
        pfx, digits, ext = pat
        prefix = pfx.rstrip('_- .') or pfx
        found = []
        try:
            with os.scandir(parent) as listing:
                for entry in listing:
                    if not entry.is_file():
                        continue
                    ep = sequence_pattern(entry.name)
                    if ep and ep[0] == pfx and ep[2] == ext:
                        found.append((int(ep[1]), Path(entry.path)))
                        if len(found) >= MAX_SEQUENCE_FRAMES:
                            break
        except OSError:
            found = []
        if len(found) > 1:
            found.sort(key=lambda item: (item[0], item[1].name))
            frames = [item[1] for item in found]
            missing = found[-1][0] - found[0][0] + 1 - len({number for number, _ in found})

    # Look for optional sidecar WAV audio in the same directory
    audio_path = None
    if len(frames) > 1 and parent.is_dir():
        wavs = []
        try:
            with os.scandir(parent) as listing:
                for entry in listing:
                    if entry.is_file() and Path(entry.name).suffix.lower() == '.wav':
                        wavs.append(Path(entry.path))
        except OSError:
            wavs = []
        if wavs:
            # Prefer a WAV matching the sequence prefix, otherwise the single WAV in the folder
            pfx_low = prefix.lower()
            matching = [w for w in wavs if w.stem.lower() == pfx_low or w.stem.lower().startswith(pfx_low)]
            if matching:
                audio_path = sorted(matching)[0]
            elif len(wavs) == 1:
                audio_path = wavs[0]

    return {
        'frames': frames,
        'isSequence': len(frames) > 1,
        'frameCount': len(frames),
        'firstFrame': frames[0],
        'prefix': prefix,
        'audioPath': str(audio_path) if audio_path else None,
        # Frame numbers absent between the first and last frame (dropped or deleted files).
        'missingFrames': missing,
    }
