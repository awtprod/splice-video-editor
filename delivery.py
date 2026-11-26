"""Delivery formats: one definition of every codec, container and preset Splice can export.

The export dialog (through /api/delivery/capabilities), the server's validation and the FFmpeg
command builder all read the tables below, so a choice the dialog offers is exactly a choice the
renderer accepts and builds.

* ``normalize(options)`` validates export options (untrusted: they arrive from the page or from a
  shared preset file) and returns a *spec*, a plain dict the renderer uses.
* ``capabilities()`` detects, per machine, which encoders and muxers this FFmpeg has and whether a
  one-frame test encode with the exact settings succeeds. Results are cached per FFmpeg binary.
  A choice that fails detection is offered disabled with the reason; it is never a failed export.
* ``frame_problems(spec, width, height, fps)`` lists frame size/rate combinations an encoder or
  container refuses (DNxHR's minimum size, MXF's rate list, H.264 levels), so the dialog can say so
  before a job starts.
* ``BUILTIN_PRESETS`` encode real delivery specs; ``load_user_presets`` / ``save_user_presets``
  keep the user's own (validated on load: preset files are shared between people).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

# ---------------------------------------------------------------------------------------------
# Codec table. ``pix`` is (pixel format without alpha, with alpha or None); ``depth`` is bits per
# component; ``chroma`` is the working subsampling the renderer composites in.

PRORES_PROFILES = {
    'proxy': dict(label='ProRes 422 Proxy', number=0, depth=10, chroma='422', pix=('yuv422p10le', None)),
    'lt': dict(label='ProRes 422 LT', number=1, depth=10, chroma='422', pix=('yuv422p10le', None)),
    'standard': dict(label='ProRes 422', number=2, depth=10, chroma='422', pix=('yuv422p10le', None)),
    'hq': dict(label='ProRes 422 HQ', number=3, depth=10, chroma='422', pix=('yuv422p10le', None)),
    # prores_ks encodes 4444 at 10 bits per colour component (alpha up to 16 bits); it has no
    # 12-bit input format, so a 12-bit 4444 master is not possible with FFmpeg.
    '4444': dict(label='ProRes 4444', number=4, depth=10, chroma='444', pix=('yuv444p10le', 'yuva444p10le')),
    '4444xq': dict(label='ProRes 4444 XQ', number=5, depth=10, chroma='444', pix=('yuv444p10le', 'yuva444p10le')),
}
DNXHR_PROFILES = {
    'lb': dict(label='DNxHR LB', name='dnxhr_lb', depth=8, chroma='422', pix=('yuv422p', None)),
    'sq': dict(label='DNxHR SQ', name='dnxhr_sq', depth=8, chroma='422', pix=('yuv422p', None)),
    'hq': dict(label='DNxHR HQ', name='dnxhr_hq', depth=8, chroma='422', pix=('yuv422p', None)),
    'hqx': dict(label='DNxHR HQX (10-bit)', name='dnxhr_hqx', depth=10, chroma='422', pix=('yuv422p10le', None)),
    '444': dict(label='DNxHR 444 (10-bit)', name='dnxhr_444', depth=10, chroma='444', pix=('yuv444p10le', None)),
}
H264_PROFILES = {
    'high': dict(label='High (8-bit 4:2:0)', name='high', depth=8, chroma='420', pix=('yuv420p', None)),
    'high10': dict(label='High 10 (10-bit 4:2:0)', name='high10', depth=10, chroma='420', pix=('yuv420p10le', None)),
    'high422': dict(label='High 4:2:2 (10-bit)', name='high422', depth=10, chroma='422', pix=('yuv422p10le', None)),
}
HEVC_PROFILES = {
    'main': dict(label='Main (8-bit)', name='main', depth=8, chroma='420', pix=('yuv420p', None)),
    'main10': dict(label='Main 10 (10-bit)', name='main10', depth=10, chroma='420', pix=('yuv420p10le', None)),
}
AV1_PROFILES = {
    'main8': dict(label='Main (8-bit)', depth=8, chroma='420', pix=('yuv420p', None)),
    'main10': dict(label='Main (10-bit)', depth=10, chroma='420', pix=('yuv420p10le', None)),
}
PNG_PROFILES = {
    '8': dict(label='PNG 8-bit RGB', depth=8, chroma='444', pix=('rgb24', 'rgba')),
    '16': dict(label='PNG 16-bit RGB', depth=16, chroma='444', pix=('rgb48be', 'rgba64be')),
}
TIFF_PROFILES = {'16': dict(label='TIFF 16-bit RGB', depth=16, chroma='444', pix=('rgb48le', 'rgba64le'))}
DPX_PROFILES = {'10': dict(label='DPX 10-bit RGB', depth=10, chroma='444', pix=('gbrp10le', None))}
EXR_PROFILES = {'half': dict(label='OpenEXR half float', depth=16, chroma='444', pix=('gbrpf32le', 'gbrapf32le'))}

VIDEO_CODECS = {
    'h264': dict(label='H.264', encoder='libx264', kind='video', intra=False, containers=('mp4', 'mov'),
                 profiles=H264_PROFILES, default_profile='high', rate_modes=('crf', 'bitrate', 'twopass'),
                 hardware=('nvenc', 'qsv', 'vaapi', 'videotoolbox', 'amf')),
    'hevc': dict(label='HEVC (H.265)', encoder='libx265', kind='video', intra=False, containers=('mp4', 'mov'),
                 profiles=HEVC_PROFILES, default_profile='main10', rate_modes=('crf', 'bitrate', 'twopass'),
                 hardware=('nvenc', 'qsv', 'vaapi', 'videotoolbox', 'amf'), hdr=True),
    'av1': dict(label='AV1', encoder='libsvtav1', kind='video', intra=False, containers=('mp4',),
                profiles=AV1_PROFILES, default_profile='main10', rate_modes=('crf', 'bitrate'), hdr=True),
    'prores': dict(label='Apple ProRes', encoder='prores_ks', kind='video', intra=True, containers=('mov',),
                   profiles=PRORES_PROFILES, default_profile='hq', rate_modes=(), hdr=True),
    'dnxhr': dict(label='Avid DNxHR', encoder='dnxhd', kind='video', intra=True, containers=('mov', 'mxf'),
                  profiles=DNXHR_PROFILES, default_profile='hq', rate_modes=(), hdr=True),
    'png': dict(label='PNG sequence', encoder='png', kind='images', intra=True, containers=('folder',),
                profiles=PNG_PROFILES, default_profile='16', rate_modes=(), extension='png'),
    'tiff': dict(label='TIFF sequence', encoder='tiff', kind='images', intra=True, containers=('folder',),
                 profiles=TIFF_PROFILES, default_profile='16', rate_modes=(), extension='tif'),
    'dpx': dict(label='DPX sequence', encoder='dpx', kind='images', intra=True, containers=('folder',),
                profiles=DPX_PROFILES, default_profile='10', rate_modes=(), extension='dpx'),
    'exr': dict(label='OpenEXR sequence', encoder='exr', kind='images', intra=True, containers=('folder',),
                profiles=EXR_PROFILES, default_profile='half', rate_modes=(), extension='exr'),
    'wav': dict(label='Audio only (WAV)', encoder=None, kind='audio', intra=True, containers=('wav',),
                profiles={}, default_profile=None, rate_modes=()),
}
HARDWARE = {
    # encoder names per family and what FFmpeg needs to feed it system-memory frames.
    'nvenc': dict(label='NVIDIA NVENC', h264='h264_nvenc', hevc='hevc_nvenc', upload=None),
    'qsv': dict(label='Intel Quick Sync', h264='h264_qsv', hevc='hevc_qsv', upload='qsv'),
    'vaapi': dict(label='VAAPI', h264='h264_vaapi', hevc='hevc_vaapi', upload='vaapi'),
    'videotoolbox': dict(label='Apple VideoToolbox', h264='h264_videotoolbox', hevc='hevc_videotoolbox', upload=None),
    'amf': dict(label='AMD AMF', h264='h264_amf', hevc='hevc_amf', upload=None),
}
AUDIO_CODECS = {
    'aac': dict(label='AAC', encoder='aac', lossy=True, containers=('mp4', 'mov')),
    'pcm16': dict(label='PCM 16-bit', encoder='pcm_s16le', lossy=False, containers=('mov', 'mxf', 'wav', 'folder')),
    'pcm24': dict(label='PCM 24-bit', encoder='pcm_s24le', lossy=False, containers=('mov', 'mxf', 'wav', 'folder')),
    'pcm32f': dict(label='PCM 32-bit float', encoder='pcm_f32le', lossy=False, containers=('mov', 'wav', 'folder')),
}
CONTAINERS = {
    'mp4': dict(label='MP4', extension='mp4', mime='video/mp4', muxer='mp4', timecode=True, subtitles='mov_text'),
    'mov': dict(label='QuickTime MOV', extension='mov', mime='video/quicktime', muxer='mov', timecode=True, subtitles='mov_text'),
    'mxf': dict(label='MXF OP1a', extension='mxf', mime='application/mxf', muxer='mxf', timecode=True, subtitles=None),
    'folder': dict(label='Numbered files', extension=None, mime=None, muxer='image2', timecode=False, subtitles=None),
    'wav': dict(label='WAV', extension='wav', mime='audio/wav', muxer='wav', timecode=False, subtitles=None),
}
DEFAULT_AUDIO = {'mp4': 'aac', 'mov': 'pcm24', 'mxf': 'pcm24', 'folder': 'pcm24', 'wav': 'pcm24'}
X264_SPEEDS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')
COLOR_SPACES = ('source', 'rec709', 'pq', 'hlg')
# MXF frame rates FFmpeg's muxer writes without -strict unofficial.
MXF_RATES = tuple(Fraction(n, d) for n, d in ((24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1), (48000, 1001),
                                               (48, 1), (50, 1), (60000, 1001), (60, 1), (100, 1), (120, 1)))
DNXHR_MIN = (256, 120)
# H.264 levels: (name, max frame size in macroblocks, max macroblocks per second).
H264_LEVELS = (('3.1', 3600, 108000), ('3.2', 5120, 216000), ('4.0', 8192, 245760), ('4.2', 8704, 522240),
               ('5.0', 22080, 589824), ('5.1', 36864, 983040), ('5.2', 36864, 2073600), ('6.0', 139264, 4177920),
               ('6.1', 139264, 8355840), ('6.2', 139264, 16711680))
MASTERING_DISPLAYS = {
    # SMPTE ST 2086 primaries in 0.00002 units (x265 master-display order G, B, R, white point).
    'p3d65': dict(label='Display P3 (D65)', primaries='G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)'),
    'bt2020': dict(label='Rec. 2020 (D65)', primaries='G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)'),
}
LIGHT_LEVEL_MODES = ('measure', 'manual', 'source')
AV1_CODES = dict(primaries={'bt709': 1, 'bt470bg': 5, 'smpte170m': 6, 'bt2020': 9},
                 transfer={'bt709': 1, 'smpte170m': 6, 'bt2020-10': 14, 'smpte2084': 16, 'arib-std-b67': 18},
                 matrix={'bt709': 1, 'bt470bg': 5, 'smpte170m': 6, 'bt2020nc': 9, 'bt2020c': 10})
BURN_IN_POSITIONS = ('top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right')
BURN_IN_FIELDS = ('timecode', 'sourceTimecode', 'clipName', 'frame', 'watermark')
MAX_TEXT = 200
MAX_NAME = 120


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def _bool(options, key, default):
    value = options.get(key, default)
    _check(isinstance(value, bool), f'Invalid {key} setting: use true or false')
    return value


def _int(options, key, default, low, high, label):
    value = options.get(key, default)
    _check(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value == int(value),
           f'Invalid {label}')
    value = int(value)
    _check(low <= value <= high, f'Invalid {label}: {value} is outside {low} to {high}')
    return value


def _number(options, key, default, low, high, label):
    value = options.get(key, default)
    _check(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value), f'Invalid {label}')
    _check(low <= value <= high, f'Invalid {label}: {value:g} is outside {low:g} to {high:g}')
    return float(value)


def _choice(options, key, default, allowed, label):
    value = options.get(key, default)
    _check(isinstance(value, str) and value in allowed, f'Unknown {label} {value!r}' if isinstance(value, str) else f'Invalid {label}')
    return value


def _text(options, key, label, limit=MAX_TEXT):
    value = options.get(key, '')
    _check(isinstance(value, str) and len(value) <= limit, f'Invalid {label}: text of at most {limit} characters')
    _check(not any(ord(c) < 32 or ord(c) == 127 for c in value), f'Invalid {label}: control characters are not allowed')
    return value.strip()


def _burn_ins(options):
    value = options.get('burnIns')
    if value is None:
        return None
    _check(isinstance(value, dict), 'Invalid burn-in settings')
    unknown = set(value) - {'fields', 'position', 'size', 'watermark', 'watermarkOpacity', 'safeMargins', 'clipNamePosition'}
    _check(not unknown, f'Unknown burn-in setting {sorted(unknown)[0]!r}' if unknown else '')
    fields = value.get('fields', [])
    _check(isinstance(fields, list) and len(fields) <= len(BURN_IN_FIELDS) and all(f in BURN_IN_FIELDS for f in fields)
           and len(set(fields)) == len(fields), 'Invalid burn-in fields')
    result = dict(fields=list(fields),
                  position=_choice(value, 'position', 'bottom-center', BURN_IN_POSITIONS, 'burn-in position'),
                  clipNamePosition=_choice(value, 'clipNamePosition', 'top-left', BURN_IN_POSITIONS, 'clip name position'),
                  size=_int(value, 'size', 4, 2, 10, 'burn-in size (% of frame height)'),
                  watermark=_text(value, 'watermark', 'watermark text', 80),
                  watermarkOpacity=_int(value, 'watermarkOpacity', 30, 5, 100, 'watermark opacity (%)'),
                  safeMargins=_bool(value, 'safeMargins', False))
    _check('watermark' not in fields or result['watermark'], 'Enter the watermark text, or turn the watermark off')
    if not fields and not result['safeMargins']:
        return None
    return result


def _hdr(options, color_space):
    value = options.get('hdr')
    if color_space != 'pq' and color_space != 'source':
        _check(value is None, 'HDR10 metadata needs PQ (Rec. 2100 PQ) output')
        return None
    if value is None:
        return dict(display='p3d65', minLuminance=0.005, maxLuminance=1000.0, lightLevels='measure', maxCll=None, maxFall=None)
    _check(isinstance(value, dict), 'Invalid HDR settings')
    unknown = set(value) - {'display', 'minLuminance', 'maxLuminance', 'lightLevels', 'maxCll', 'maxFall'}
    _check(not unknown, f'Unknown HDR setting {sorted(unknown)[0]!r}' if unknown else '')
    result = dict(display=_choice(value, 'display', 'p3d65', tuple(MASTERING_DISPLAYS), 'mastering display'),
                  minLuminance=_number(value, 'minLuminance', 0.005, 0.0001, 5, 'mastering display minimum (nits)'),
                  maxLuminance=_number(value, 'maxLuminance', 1000, 100, 10000, 'mastering display peak (nits)'),
                  lightLevels=_choice(value, 'lightLevels', 'measure', LIGHT_LEVEL_MODES, 'MaxCLL/MaxFALL source'),
                  maxCll=None, maxFall=None)
    if result['lightLevels'] == 'manual':
        result['maxCll'] = _int(value, 'maxCll', None, 0, 10000, 'MaxCLL (nits)') if value.get('maxCll') is not None else None
        result['maxFall'] = _int(value, 'maxFall', None, 0, 10000, 'MaxFALL (nits)') if value.get('maxFall') is not None else None
        _check(result['maxCll'] is not None and result['maxFall'] is not None, 'Enter MaxCLL and MaxFALL, or measure them')
        _check(result['maxFall'] <= result['maxCll'], 'MaxFALL cannot exceed MaxCLL')
    return result


KNOWN_OPTIONS = {
    # The renderer's original options (render_movie._export_settings and the audio group).
    'codec', 'crf', 'audioBitrate', 'colorSpace', 'range', 'limiter', 'limiterCeiling', 'aacPeakSafe', 'subtitleLanguage',
    'captions',
    # Delivery options (this module).
    'profile', 'container', 'rateControl', 'videoBitrate', 'speed', 'encoder', 'alpha', 'audioCodec', 'hdr', 'timecode',
    'reel', 'title', 'comment', 'faststart', 'startNumber', 'padding', 'burnIns', 'outputSize', 'fit', 'captionFiles',
}


def normalize(options):
    """Validate export options; return the spec the renderer builds commands from."""
    _check(isinstance(options, dict), 'Invalid export options')
    codec = _choice(options, 'codec', 'h264', tuple(VIDEO_CODECS), 'export format')
    info = VIDEO_CODECS[codec]
    profiles = info['profiles']
    profile = _choice(options, 'profile', info['default_profile'], tuple(profiles) or (None,), f'{info["label"]} profile') \
        if profiles else None
    container = _choice(options, 'container', info['containers'][0], info['containers'], f'{info["label"]} container')
    details = profiles.get(profile, {}) if profile else {}
    spec = dict(codec=codec, kind=info['kind'], profile=profile, container=container, label=info['label'],
                profile_label=details.get('label', ''), intra=info['intra'], encoder=info['encoder'], hardware=None,
                depth=details.get('depth', 0), chroma=details.get('chroma', '420'))
    # Rate control (long-GOP codecs). Legacy H.264 exports keep CRF with the veryfast preset.
    spec['rate'] = None
    if info['rate_modes']:
        mode = _choice(options, 'rateControl', 'crf', info['rate_modes'], 'rate control')
        crf_range = (0, 51) if codec in ('h264', 'hevc') else (1, 63)
        default_crf = 20 if codec == 'h264' else 22 if codec == 'hevc' else 30
        crf = _int(options, 'crf', default_crf, *crf_range, 'quality (CRF)') if mode == 'crf' else None
        if codec == 'h264' and mode == 'crf' and 'crf' in options and options.get('profile') is None and \
                options.get('rateControl') is None:
            crf = _int(options, 'crf', 20, 14, 32, 'quality')  # the original dialog's range
        kbps = _int(options, 'videoBitrate', 20000, 100, 800000, 'video bitrate (kbit/s)') if mode != 'crf' else None
        speeds = X264_SPEEDS if codec in ('h264', 'hevc') else tuple(str(n) for n in range(0, 14))
        speed = _choice(options, 'speed', 'veryfast' if codec in ('h264', 'hevc') else '8', speeds, 'encoder speed')
        spec['rate'] = dict(mode=mode, crf=crf, kbps=kbps, speed=speed)
    else:
        for key in ('rateControl', 'videoBitrate', 'speed'):
            _check(key not in options, f'{info["label"]} has no {key} setting')
    hardware = options.get('encoder', 'software')
    _check(hardware == 'software' or hardware in info.get('hardware', ()), f'Unknown encoder {hardware!r} for {info["label"]}'
           if isinstance(hardware, str) else 'Invalid encoder')
    if hardware != 'software':
        _check(spec['rate']['mode'] != 'twopass', 'Two-pass encoding is software only')
        spec['hardware'] = hardware
        spec['encoder'] = HARDWARE[hardware][codec]
    alpha = _bool(options, 'alpha', False)
    _check(not alpha or (details.get('pix') or (None, None))[1], f'{spec["profile_label"] or info["label"]} cannot carry alpha')
    spec['alpha'] = alpha
    if details:
        spec['pix_fmt'] = details['pix'][1] if alpha else details['pix'][0]
    else:
        spec['pix_fmt'] = None
    spec['high_depth'] = spec['depth'] >= 10 or info['kind'] == 'images'
    # Colour. 'source' keeps a single HDR source's transfer; 'pq' conforms to Rec. 2100 PQ;
    # 'hlg' keeps HLG sources (Splice does not convert other material to HLG).
    color_space = _choice(options, 'colorSpace', 'source', COLOR_SPACES, 'export color space')
    hdr_capable = bool(info.get('hdr')) and spec['depth'] >= 10
    if color_space in ('pq', 'hlg'):
        _check(hdr_capable, f'{spec["profile_label"] or info["label"]} cannot carry HDR: choose a 10-bit HEVC, AV1, ProRes or '
                            'DNxHR HQX/444 format, or Rec. 709')
    spec['color_space'] = color_space
    spec['hdr_capable'] = hdr_capable
    spec['hdr'] = _hdr(options, color_space) if hdr_capable and codec in ('hevc', 'av1') else None
    if not (hdr_capable and codec in ('hevc', 'av1')):
        _check(options.get('hdr') is None, 'HDR10 static metadata is written in HEVC and AV1 deliveries only')
    # Audio.
    audio_default = DEFAULT_AUDIO[container]
    audio = _choice(options, 'audioCodec', audio_default, tuple(c for c, a in AUDIO_CODECS.items() if container in a['containers']),
                    f'audio format for {CONTAINERS[container]["label"]}')
    spec['audio'] = audio
    spec['audio_lossy'] = AUDIO_CODECS[audio]['lossy']
    spec['audio_bitrate'] = _int(options, 'audioBitrate', 192, 64, 320, 'audio bitrate')
    # Timecode and metadata.
    spec['timecode'] = _bool(options, 'timecode', CONTAINERS[container]['timecode']) and CONTAINERS[container]['timecode']
    spec['reel'] = _text(options, 'reel', 'reel name', 32)
    _check(re.fullmatch(r'[A-Za-z0-9_.\- ]*', spec['reel']), 'Invalid reel name: use letters, digits, space, _ . -')
    spec['title'] = _text(options, 'title', 'title metadata')
    spec['comment'] = _text(options, 'comment', 'comment metadata', 1000)
    spec['faststart'] = _bool(options, 'faststart', container == 'mp4')
    # Image sequences.
    if info['kind'] == 'images':
        spec['start_number'] = _int(options, 'startNumber', 0, 0, 99_999_999, 'first frame number')
        spec['padding'] = _int(options, 'padding', 7, 1, 9, 'frame number digits')
        spec['extension'] = info['extension']
    else:
        _check('startNumber' not in options and 'padding' not in options, 'Frame numbering is for image sequences only')
    spec['burn_ins'] = _burn_ins(options)
    spec['output_size'] = _output_size(options)
    spec['caption_files'] = _caption_files(options)
    spec['mode'] = 'audio' if info['kind'] == 'audio' else 'segments' if info['intra'] and info['kind'] == 'video' else 'stream'
    spec['segment_suffix'] = '.mxf' if container == 'mxf' else '.mov'
    spec['extension'] = spec.get('extension') or CONTAINERS[container]['extension']
    spec['mime'] = CONTAINERS[container]['mime']
    return spec


def _output_size(options):
    size = options.get('outputSize')
    if size is None:
        _check('fit' not in options, 'A fit mode needs an output size')
        return None
    _check(isinstance(size, dict) and set(size) <= {'width', 'height'}, 'Invalid output size')
    width = _int(size, 'width', None, 16, 8192, 'output width') if 'width' in size else None
    height = _int(size, 'height', None, 16, 4320, 'output height') if 'height' in size else None
    _check(width is not None and height is not None and width % 2 == 0 and height % 2 == 0,
           'Output width and height must be even numbers of pixels')
    fit = _choice(options, 'fit', 'fit', ('fit', 'fill', 'stretch'), 'fit mode')
    return dict(width=width, height=height, fit=fit)


def _caption_files(options):
    value = options.get('captionFiles', [])
    _check(isinstance(value, list) and all(v in ('srt', 'vtt') for v in value) and len(set(value)) == len(value),
           'Invalid caption files: choose srt and/or vtt')
    return list(value)


def legacy_codec(spec):
    """The original renderer's codec family for a spec: 'prores' for 10-bit intra masters, else 'h264'."""
    return 'wav' if spec['kind'] == 'audio' else 'prores' if spec['high_depth'] else 'h264'


# ---------------------------------------------------------------------------------------------
# Frame checks and conversions announced before rendering.

def frame_problems(spec, width, height, fps):
    """Combinations this machine's FFmpeg refuses, said before a job starts."""
    problems = []
    rate = Fraction(fps).limit_denominator(1001)
    out_w, out_h = (spec['output_size']['width'], spec['output_size']['height']) if spec.get('output_size') else (width, height)
    if spec['codec'] == 'dnxhr' and (out_w < DNXHR_MIN[0] or out_h < DNXHR_MIN[1]):
        problems.append(f'DNxHR needs a frame of at least {DNXHR_MIN[0]} × {DNXHR_MIN[1]}; this export is {out_w} × {out_h}.')
    if spec['container'] == 'mxf' and rate not in MXF_RATES:
        problems.append(f'MXF does not carry {float(rate):g} fps; choose MOV, or a sequence at a broadcast frame rate.')
    if spec['codec'] == 'h264' and h264_level(out_w, out_h, rate) is None:
        problems.append(f'{out_w} × {out_h} at {float(rate):g} fps is beyond H.264 level 6.2.')
    if spec['codec'] == 'h264' and spec['hardware'] and (out_w > 4096 or out_h > 4096):
        problems.append('Hardware H.264 encoders stop at 4096 pixels; choose HEVC or software H.264.')
    if spec['codec'] == 'av1' and (out_w > 16384 or out_h > 8704):
        problems.append('AV1 (SVT) frame is too large.')
    return problems


def h264_level(width, height, rate):
    macroblocks = math.ceil(width / 16) * math.ceil(height / 16)
    per_second = macroblocks * float(rate)
    for name, frame_size, rate_limit in H264_LEVELS:
        if macroblocks <= frame_size and per_second <= rate_limit and max(width, height) <= math.sqrt(frame_size * 8) * 16:
            return name
    return None


def conversions(spec, width, height, fps):
    """Every change to frame size, rate or level this delivery makes, in words. Nothing silent."""
    notes = []
    size = spec.get('output_size')
    if size and (size['width'], size['height']) != (width, height):
        how = {'fit': 'fitted inside the frame (letterbox or pillarbox bars)', 'fill': 'scaled to fill the frame (edges cropped)',
               'stretch': 'stretched to the frame (aspect ratio changes)'}[size['fit']]
        notes.append(f'Frame size changes from {width} × {height} to {size["width"]} × {size["height"]}: the picture is {how}.')
    if spec['codec'] == 'h264' and spec['chroma'] == '420' and spec['depth'] == 8 and spec['color_space'] in ('pq', 'hlg'):
        notes.append('HDR is tone-mapped to Rec. 709.')
    return notes


# ---------------------------------------------------------------------------------------------
# FFmpeg arguments.

def timecode_label(frames, fps, drop_frame):
    """SMPTE label for a frame count at the sequence rate (drop-frame only at 29.97 and 59.94)."""
    from interchange.timecode import DROP_FRAME_RATES, frames_timecode
    rate = Fraction(fps).limit_denominator(1001)
    drop = bool(drop_frame) and rate in DROP_FRAME_RATES
    per_second = round(rate)
    day = per_second * 86400 - (per_second // 15) * (1440 - 144) if drop else per_second * 86400
    return frames_timecode(int(frames) % day, rate, drop)


def light_level_params(spec, levels):
    """x265 HDR10 parameters (mastering display + content light levels)."""
    hdr = spec['hdr']
    display = MASTERING_DISPLAYS[hdr['display']]['primaries']
    luminance = f'L({round(hdr["maxLuminance"] * 10000)},{round(hdr["minLuminance"] * 10000)})'
    return f'master-display={display}{luminance}:max-cll={levels[0]},{levels[1]}'


def encoder_args(spec, fps, profile_tags, *, light_levels=None, pass_number=None, passlog=None, threads=None):
    """Video encoder options for the delivery (after -i ... -map). ``profile_tags`` is the
    renderer's (primaries, transfer, matrix) for the program."""
    codec, rate = spec['codec'], spec['rate']
    args = ['-c:v', spec['encoder']]
    if spec['codec'] == 'prores':
        args += ['-profile:v', str(PRORES_PROFILES[spec['profile']]['number']), '-vendor', 'apl0']
        if spec['alpha']:
            args += ['-alpha_bits', '16']
    elif codec == 'dnxhr':
        args += ['-profile:v', DNXHR_PROFILES[spec['profile']]['name']]
    elif codec in ('h264', 'hevc') and not spec['hardware']:
        args += ['-preset', rate['speed']]
        if codec == 'h264':
            args += ['-profile:v', H264_PROFILES[spec['profile']]['name']]
        else:
            args += ['-profile:v', HEVC_PROFILES[spec['profile']]['name']]
        args += _rate_args(rate, pass_number, passlog, codec)
    elif codec == 'av1':
        args += ['-preset', rate['speed']]
        args += ['-crf', str(rate['crf'])] if rate['mode'] == 'crf' else ['-b:v', f'{rate["kbps"]}k']
    elif spec['hardware']:
        if rate['mode'] == 'crf':
            quality = str(rate['crf'])
            args += {'nvenc': ['-rc', 'vbr', '-cq', quality, '-b:v', '0'], 'qsv': ['-global_quality', quality],
                     'vaapi': ['-rc_mode', 'CQP', '-qp', quality], 'videotoolbox': ['-q:v', str(max(1, 100 - 2 * rate['crf']))],
                     'amf': ['-rc', 'cqp', '-qp_i', quality, '-qp_p', quality]}[spec['hardware']]
        else:
            args += ['-b:v', f'{rate["kbps"]}k', '-maxrate', f'{rate["kbps"]}k']
        if codec == 'hevc' and spec['profile'] == 'main10' and spec['hardware'] != 'vaapi':
            args += ['-profile:v', 'main10']
    elif spec['kind'] == 'images':
        if codec == 'exr':
            args += ['-format', 'half', '-compression', 'zip1']
        elif codec == 'tiff':
            args += ['-compression_algo', 'deflate']
    if spec['pix_fmt'] and not (spec['hardware'] and HARDWARE[spec['hardware']]['upload']):
        args += ['-pix_fmt', spec['pix_fmt']]
    if codec == 'h264' and not spec['hardware']:
        level = h264_level(*fps_size(spec, fps))
        if level:
            args += ['-level:v', level]
    if codec == 'hevc' and not spec['hardware']:
        params = ['repeat-headers=1']
        if spec.get('hdr') and profile_tags[1] == 'smpte2084':
            params.insert(0, 'hdr10=1')
            params.append(light_level_params(spec, light_levels or (0, 0)))
        params += [f'colorprim={profile_tags[0]}', f'transfer={profile_tags[1]}', f'colormatrix={profile_tags[2]}', 'range=limited']
        if pass_number:
            params += [f'pass={pass_number}', f'stats={passlog}.x265']
        args += ['-x265-params', ':'.join(params), '-tag:v', 'hvc1']
    if codec == 'av1':
        # SVT-AV1 takes its colour description (ISO/IEC 23091-2 code points) as its own parameters.
        params = [f'color-primaries={AV1_CODES["primaries"].get(profile_tags[0], 2)}',
                  f'transfer-characteristics={AV1_CODES["transfer"].get(profile_tags[1], 2)}',
                  f'matrix-coefficients={AV1_CODES["matrix"].get(profile_tags[2], 2)}', 'color-range=0']
        if spec.get('hdr') and profile_tags[1] == 'smpte2084':
            hdr = spec['hdr']
            primaries = MASTERING_DISPLAYS[hdr['display']]['primaries']
            values = [int(v) / 50000 for v in re.findall(r'\d+', primaries)]
            params.append('mastering-display=G({:.4f},{:.4f})B({:.4f},{:.4f})R({:.4f},{:.4f})WP({:.4f},{:.4f})'
                          'L({:.4f},{:.4f})'.format(*values, hdr['maxLuminance'], hdr['minLuminance']))
            params.append('content-light={},{}'.format(*(light_levels or (0, 0))))
        args += ['-svtav1-params', ':'.join(params)]
    if threads:
        args += ['-threads', str(threads)]
    return args


def fps_size(spec, fps):
    size = spec.get('output_size') or spec.get('_frame')
    return size['width'], size['height'], Fraction(fps).limit_denominator(1001)


def _rate_args(rate, pass_number, passlog, codec):
    if rate['mode'] == 'crf':
        return ['-crf', str(rate['crf'])]
    args = ['-b:v', f'{rate["kbps"]}k']
    if rate['mode'] == 'bitrate':
        # One pass cannot look ahead: a VBV buffer keeps the average near the target.
        args += ['-maxrate', f'{round(rate["kbps"] * 1.25)}k', '-bufsize', f'{rate["kbps"]}k']
    if rate['mode'] == 'twopass' and codec == 'h264':
        args += ['-pass', str(pass_number), '-passlogfile', str(passlog)]
    return args


def hardware_input_args(spec):
    """Device setup for encoders that take hardware frames (before the inputs)."""
    upload = spec['hardware'] and HARDWARE[spec['hardware']]['upload']
    if upload == 'vaapi':
        return ['-vaapi_device', os.environ.get('SPLICE_VAAPI_DEVICE', '/dev/dri/renderD128')]
    if upload == 'qsv':
        return ['-init_hw_device', 'qsv=hw', '-filter_hw_device', 'hw']
    return []


def hardware_filter(spec):
    upload = spec['hardware'] and HARDWARE[spec['hardware']]['upload']
    if upload == 'vaapi':
        return 'format=' + ('p010' if spec['depth'] >= 10 else 'nv12') + ',hwupload'
    if upload == 'qsv':
        return 'format=' + ('p010' if spec['depth'] >= 10 else 'nv12') + ',hwupload=extra_hw_frames=64,format=qsv'
    return None


def audio_args(spec, rate):
    encoder = AUDIO_CODECS[spec['audio']]['encoder']
    if spec['audio'] == 'aac':
        return ['-c:a', 'copy']  # encoded and measured by the renderer before the mux
    return ['-c:a', encoder, '-ar', str(rate)]


def metadata_args(spec, timecode):
    args = []
    if timecode:
        args += ['-timecode', timecode]
    if spec['reel']:
        args += ['-metadata:s:v:0', f'reel_name={spec["reel"]}']
    if spec['title']:
        args += ['-metadata', f'title={spec["title"]}']
    if spec['comment']:
        args += ['-metadata', f'comment={spec["comment"]}']
    return args


def container_flags(spec):
    if spec['container'] in ('mp4', 'mov'):
        return ['-movflags', '+write_colr' + ('+faststart' if spec['faststart'] else '')]
    return []


# ---------------------------------------------------------------------------------------------
# Capability detection.

_lock = threading.Lock()
_cache = {}


def _ffmpeg_identity():
    path = shutil.which('ffmpeg')
    if not path:
        return None, None
    stat = Path(path).resolve().stat()
    return path, f'{Path(path).resolve()}:{stat.st_size}:{int(stat.st_mtime)}'


def _test_encode(spec):
    """Encode one generated frame with the delivery's exact encoder options."""
    size = (256, 144) if spec['codec'] == 'dnxhr' else (64, 64)
    spec = dict(spec, _frame=dict(width=size[0], height=size[1]))
    tags = ('bt709', 'bt709', 'bt709')
    if spec['kind'] == 'images':
        target = ['-frames:v', '1', '-f', 'image2', '-update', '1']
        container = None
    else:
        container = CONTAINERS[spec['container']]['muxer']
        target = ['-frames:v', '1', '-f', container]
    with tempfile.TemporaryDirectory(prefix='splice-capability-') as directory:
        output = Path(directory) / ('probe.' + (spec.get('extension') or 'bin'))
        source = f'color=c=gray:s={size[0]}x{size[1]}:r=24:d=1' + (',format=yuva444p10le' if spec['alpha'] else '')
        hw_filter = hardware_filter(spec)
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y', *hardware_input_args(spec), '-f', 'lavfi', '-i', source,
                   *(['-vf', hw_filter] if hw_filter else []),
                   *encoder_args(spec, 24, tags, light_levels=(1000, 400) if spec.get('hdr') else None), *target, str(output)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return 'the test encode did not finish in 30 seconds'
        except OSError as error:
            return f'FFmpeg could not start ({error})'
        if result.returncode or not output.exists() or not output.stat().st_size:
            return _failure_reason(spec, result.stderr, result.returncode)
    return ''


def _failure_reason(spec, stderr, code):
    """The most telling line of a failed test encode, without FFmpeg's '[name @ 0x...]' prefixes."""
    lines = [re.sub(r'^(\[[^]]*@ 0x[0-9a-f]+\] ?)+', '', line).strip() for line in stderr.splitlines()]
    lines = [line for line in lines if line and not line.startswith(('Error parsing global options', 'Nothing was written',
                                                                     'Terminating thread', 'Task finished'))]
    telling = next((line for line in lines if re.search(r'cannot|could not|no device|not found|failed|unsupported|denied|'
                                                         r'not supported|invalid', line, re.I)), lines[-1] if lines else '')
    telling = telling[:120] or f'exit code {code}'
    if spec.get('hardware'):
        return f'no usable {HARDWARE[spec["hardware"]]["label"]} device or driver ({telling})'
    return f'the test encode failed ({telling})'


def _lists():
    def names(flag, pattern):
        result = subprocess.run(['ffmpeg', '-hide_banner', flag], capture_output=True, text=True, timeout=30)
        return set(re.findall(pattern, result.stdout, re.M))
    return names('-encoders', r'^\s*[VAS][A-Z.]{5}\s+(\S+)'), names('-muxers', r'^\s*D?E\s+(\S+)')


def _variants():
    """Every (codec, profile, alpha, container, hardware) choice to test, as option dicts."""
    for codec, info in VIDEO_CODECS.items():
        if info['kind'] == 'audio':
            continue
        for profile, details in info['profiles'].items():
            for alpha in ((False, True) if details['pix'][1] else (False,)):
                for container in info['containers']:
                    yield dict(codec=codec, profile=profile, alpha=alpha, container=container)
        for hardware in info.get('hardware', ()):
            for profile in info['profiles']:
                if codec == 'h264' and profile != 'high':
                    continue
                yield dict(codec=codec, profile=profile, encoder=hardware, container=info['containers'][0])


def variant_key(options):
    return '|'.join(str(options.get(k, '')) for k in ('codec', 'profile', 'alpha', 'container', 'encoder'))


def capabilities(refresh=False):
    """{'ffmpeg': path, 'variants': {key: reason or ''}, 'audio': {...}, 'muxers': {...}}. Cached per FFmpeg build."""
    path, identity = _ffmpeg_identity()
    if not path:
        return dict(ffmpeg=None, variants={}, audio={}, error='FFmpeg is not installed or not on PATH')
    with _lock:
        if not refresh and _cache.get('identity') == identity:
            return _cache['result']
        stored = _cache_file()
        if not refresh and stored:
            try:
                data = json.loads(stored.read_text(encoding='utf-8'))
                if data.get('identity') == identity and isinstance(data.get('result'), dict):
                    _cache.update(identity=identity, result=data['result'])
                    return data['result']
            except (OSError, ValueError):
                pass
        encoders, muxers = _lists()
        variants = {}
        tests = []
        for options in _variants():
            spec = normalize(options)
            missing = [name for name in (spec['encoder'],) if name not in encoders]
            muxer = CONTAINERS[spec['container']]['muxer']
            if missing:
                variants[variant_key(options)] = f'this FFmpeg has no {missing[0]} encoder'
            elif muxer not in muxers:
                variants[variant_key(options)] = f'this FFmpeg cannot write {CONTAINERS[spec["container"]]["label"]}'
            else:
                tests.append((variant_key(options), spec))
        with ThreadPoolExecutor(max_workers=4) as pool:
            for key, reason in zip([key for key, _ in tests], pool.map(_test_encode, [spec for _, spec in tests])):
                variants[key] = reason
        audio = {key: ('' if info['encoder'] in encoders else f'this FFmpeg has no {info["encoder"]} encoder')
                 for key, info in AUDIO_CODECS.items()}
        subtitles = {name: name in encoders for name in ('mov_text', 'webvtt', 'srt')}
        result = dict(ffmpeg=path, variants=variants, audio=audio, subtitles=subtitles)
        _cache.update(identity=identity, result=result)
        if stored:
            try:
                temporary = stored.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(identity=identity, result=result)), encoding='utf-8')
                os.replace(temporary, stored)
            except OSError:
                pass  # The in-memory cache still holds for this run.
        return result


def _cache_file():
    try:
        from splice_platform import data_directory
        root = data_directory()
        root.mkdir(parents=True, exist_ok=True)
        return root / 'delivery-capabilities.json'
    except OSError:
        return None


_lists_cache = {}


def unavailable_reason(options):
    """'' when this machine can encode the choice (tested once per FFmpeg build), else why not."""
    spec = normalize(options)
    if spec['kind'] == 'audio':
        return ''
    path, identity = _ffmpeg_identity()
    if not path:
        return 'FFmpeg is not installed or not on PATH'
    variant = dict(codec=spec['codec'], profile=spec['profile'], alpha=spec['alpha'], container=spec['container'])
    if spec['hardware']:
        variant = dict(codec=spec['codec'], profile=spec['profile'], encoder=spec['hardware'],
                       container=VIDEO_CODECS[spec['codec']]['containers'][0])
    key = variant_key(variant)
    with _lock:
        known = _cache.get('result') if _cache.get('identity') == identity else None
        if known is None:
            stored = _cache_file()
            try:
                data = json.loads(stored.read_text(encoding='utf-8')) if stored else None
                if data and data.get('identity') == identity:
                    _cache.update(identity=identity, result=data['result'])
                    known = data['result']
            except (OSError, ValueError):
                pass
        if known is not None and key in known['variants']:
            reason = known['variants'][key]
        else:
            singles = _cache.setdefault('singles', {})
            if (identity, key) not in singles:
                if _lists_cache.get('identity') != identity:
                    _lists_cache.update(identity=identity, lists=_lists())
                encoders, muxers = _lists_cache['lists']
                probe = normalize(variant)
                if probe['encoder'] not in encoders:
                    singles[identity, key] = f'this FFmpeg has no {probe["encoder"]} encoder'
                elif CONTAINERS[probe['container']]['muxer'] not in muxers:
                    singles[identity, key] = f'this FFmpeg cannot write {CONTAINERS[probe["container"]]["label"]}'
                else:
                    singles[identity, key] = _test_encode(probe)
            reason = singles[identity, key]
    return reason


def describe():
    """The codec table for the dialog (plain JSON)."""
    codecs = {}
    for key, info in VIDEO_CODECS.items():
        codecs[key] = dict(label=info['label'], kind=info['kind'], containers=list(info['containers']), intra=info['intra'],
                           rateModes=list(info['rate_modes']), hardware=list(info.get('hardware', ())),
                           defaultProfile=info['default_profile'], hdr=bool(info.get('hdr')),
                           profiles={name: dict(label=d['label'], depth=d['depth'], chroma=d['chroma'], alpha=bool(d['pix'][1]))
                                     for name, d in info['profiles'].items()})
    return dict(codecs=codecs, containers={k: dict(label=v['label'], extension=v['extension'], timecode=v['timecode'],
                                                    subtitles=v['subtitles']) for k, v in CONTAINERS.items()},
                audio={k: dict(label=v['label'], containers=list(v['containers'])) for k, v in AUDIO_CODECS.items()},
                defaultAudio=DEFAULT_AUDIO, speeds=list(X264_SPEEDS), hardware={k: v['label'] for k, v in HARDWARE.items()},
                displays={k: v['label'] for k, v in MASTERING_DISPLAYS.items()}, burnInFields=list(BURN_IN_FIELDS),
                burnInPositions=list(BURN_IN_POSITIONS), mxfRates=[float(r) for r in MXF_RATES], dnxhrMin=list(DNXHR_MIN))


# ---------------------------------------------------------------------------------------------
# Presets. Each states what it is for; none changes frame rate or loudness, and a size change is
# announced by conversions() before rendering.

BUILTIN_PRESETS = [
    dict(id='youtube-1080', name='YouTube / Vimeo 1080p (SDR)', group='Web',
         purpose='Upload master for YouTube and Vimeo: H.264 High, high quality, MP4 with faststart.',
         options=dict(codec='h264', profile='high', rateControl='crf', crf=18, speed='medium', container='mp4',
                      audioCodec='aac', audioBitrate=320, colorSpace='rec709', outputSize=dict(width=1920, height=1080), fit='fit')),
    dict(id='youtube-uhd', name='YouTube / Vimeo UHD (SDR)', group='Web',
         purpose='UHD upload master: H.264 High at 3840 × 2160, 8-bit Rec. 709.',
         options=dict(codec='h264', profile='high', rateControl='crf', crf=18, speed='medium', container='mp4',
                      audioCodec='aac', audioBitrate=320, colorSpace='rec709', outputSize=dict(width=3840, height=2160), fit='fit')),
    dict(id='youtube-uhd-hdr', name='YouTube UHD HDR10', group='Web',
         purpose='HDR upload: HEVC Main 10, Rec. 2100 PQ, HDR10 mastering display and measured MaxCLL/MaxFALL.',
         options=dict(codec='hevc', profile='main10', rateControl='crf', crf=18, speed='medium', container='mp4',
                      audioCodec='aac', audioBitrate=320, colorSpace='pq',
                      hdr=dict(display='p3d65', minLuminance=0.005, maxLuminance=1000, lightLevels='measure'))),
    dict(id='web-av1', name='Web AV1 (small file)', group='Web',
         purpose='Efficient web file: AV1 10-bit in MP4, for players that support AV1.',
         options=dict(codec='av1', profile='main10', rateControl='crf', crf=30, speed='8', container='mp4', audioCodec='aac',
                      audioBitrate=192)),
    dict(id='festival-hq', name='Festival / DCP source (ProRes 422 HQ)', group='Festival',
         purpose='Screening copy and DCP source: ProRes 422 HQ, PCM 24-bit. DCP packaging is done in a DCP tool such as '
                 'DCP-o-matic; Splice does not make DCPs.',
         options=dict(codec='prores', profile='hq', container='mov', audioCodec='pcm24')),
    dict(id='festival-4444', name='Festival / DCP source (ProRes 4444)', group='Festival',
         purpose='DCP source with 4:4:4 colour: ProRes 4444, PCM 24-bit. DCP packaging is done in a DCP tool.',
         options=dict(codec='prores', profile='4444', container='mov', audioCodec='pcm24')),
    dict(id='festival-tiff', name='Festival / DCP source (16-bit TIFF sequence)', group='Festival',
         purpose='Frames for a DCP encoder: 16-bit TIFF files numbered from 0, with a 24-bit WAV beside them.',
         options=dict(codec='tiff', profile='16', startNumber=0, padding=7, audioCodec='pcm24')),
    dict(id='broadcast-prores', name='Broadcast master (ProRes 422 HQ)', group='Broadcast',
         purpose='Broadcast/streamer master: ProRes 422 HQ in MOV, PCM 24-bit, timecode track. ProRes is intra-frame, '
                 'so closed-GOP rules do not apply.',
         options=dict(codec='prores', profile='hq', container='mov', audioCodec='pcm24', timecode=True)),
    dict(id='broadcast-dnxhr', name='Broadcast master (DNxHR HQX MXF)', group='Broadcast',
         purpose='Avid/broadcast master: DNxHR HQX 10-bit in MXF OP1a, PCM 24-bit, timecode.',
         options=dict(codec='dnxhr', profile='hqx', container='mxf', audioCodec='pcm24', timecode=True)),
    dict(id='archive', name='Archive master (ProRes 4444 XQ)', group='Archive',
         purpose='Highest-quality archive: ProRes 4444 XQ, PCM 24-bit, alpha kept when the sequence has transparency.',
         options=dict(codec='prores', profile='4444xq', container='mov', audioCodec='pcm24')),
    dict(id='proxy-prores', name='Editorial proxy (ProRes Proxy, half size)', group='Editorial',
         purpose='Offline/review proxy at half size with timecode and clip-name burn-ins.',
         options=dict(codec='prores', profile='proxy', container='mov', audioCodec='pcm16', outputSize='half', fit='fit',
                      burnIns=dict(fields=['timecode', 'clipName'], position='bottom-center', size=5))),
    dict(id='proxy-dnxhr', name='Editorial proxy (DNxHR LB, half size)', group='Editorial',
         purpose='Avid offline proxy at half size with timecode and clip-name burn-ins.',
         options=dict(codec='dnxhr', profile='lb', container='mxf', audioCodec='pcm16', outputSize='half', fit='fit',
                      burnIns=dict(fields=['timecode', 'clipName'], position='bottom-center', size=5))),
    dict(id='social-vertical-crop', name='Social vertical 1080 × 1920 (crop)', group='Social',
         purpose='Reels/Shorts/TikTok: the centre of the frame is cropped to 9:16 (edges are lost).',
         options=dict(codec='h264', profile='high', rateControl='crf', crf=20, speed='medium', container='mp4', audioCodec='aac',
                      audioBitrate=256, outputSize=dict(width=1080, height=1920), fit='fill')),
    dict(id='social-vertical-pad', name='Social vertical 1080 × 1920 (pad)', group='Social',
         purpose='Reels/Shorts/TikTok: the whole frame fits inside 9:16 with black bars above and below.',
         options=dict(codec='h264', profile='high', rateControl='crf', crf=20, speed='medium', container='mp4', audioCodec='aac',
                      audioBitrate=256, outputSize=dict(width=1080, height=1920), fit='fit')),
]
MAX_USER_PRESETS = 200
PRESET_FILE_LIMIT = 256 * 1024


def resolve_preset_options(options, width, height):
    """A preset's options for a sequence: 'half' size becomes numbers. Returns a new dict."""
    options = json.loads(json.dumps(options))
    if options.get('outputSize') == 'half':
        options['outputSize'] = dict(width=max(16, width // 2 // 2 * 2), height=max(16, height // 2 // 2 * 2))
    return options


def validate_preset(preset):
    """A preset from a file or the page: name, purpose and options that normalize() accepts."""
    _check(isinstance(preset, dict), 'Invalid preset')
    unknown = set(preset) - {'id', 'name', 'purpose', 'options', 'group'}
    _check(not unknown, f'Unknown preset field {sorted(unknown)[0]!r}' if unknown else '')
    name = _text(preset, 'name', 'preset name', MAX_NAME)
    _check(name, 'A preset needs a name')
    purpose = _text(preset, 'purpose', 'preset description', 400)
    options = preset.get('options')
    _check(isinstance(options, dict) and len(options) <= 40, 'Invalid preset options')
    unknown = set(options) - KNOWN_OPTIONS
    _check(not unknown, f'Unknown preset option {sorted(unknown)[0]!r}' if unknown else '')
    for key in ('range',):
        _check(key not in options, 'A preset cannot carry an export range')
    normalize(resolve_preset_options(options, 1920, 1080))
    identifier = preset.get('id') or 'user-' + hashlib.sha256(name.encode()).hexdigest()[:12]
    _check(isinstance(identifier, str) and re.fullmatch(r'user-[a-z0-9-]{1,40}', identifier), 'Invalid preset id')
    return dict(id=identifier, name=name, purpose=purpose, group='My presets', options=options)


def user_presets_path():
    from splice_platform import data_directory
    return data_directory() / 'delivery-presets.json'


def load_user_presets():
    """The user's presets; invalid entries are skipped and reported, never trusted."""
    path = user_presets_path()
    try:
        if path.stat().st_size > PRESET_FILE_LIMIT * 4:
            return [], ['The presets file is too large and was not read']
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as error:
        return [], [f'The presets file could not be read ({error})']
    if not isinstance(data, dict) or not isinstance(data.get('presets'), list):
        return [], ['The presets file is not a Splice presets file']
    presets, problems = [], []
    for entry in data['presets'][:MAX_USER_PRESETS]:
        try:
            presets.append(validate_preset(entry))
        except ValueError as error:
            problems.append(f'{entry.get("name") if isinstance(entry, dict) else "A preset"}: {error}')
    return presets, problems


def save_user_presets(presets):
    _check(isinstance(presets, list) and len(presets) <= MAX_USER_PRESETS, f'At most {MAX_USER_PRESETS} presets')
    clean = [validate_preset(p) for p in presets]
    ids = [p['id'] for p in clean]
    _check(len(set(ids)) == len(ids), 'Two presets share an id')
    names = [p['name'].casefold() for p in clean]
    _check(len(set(names)) == len(names), 'Two presets share a name')
    builtin = {p['name'].casefold() for p in BUILTIN_PRESETS}
    _check(not set(names) & builtin, 'A preset cannot take the name of a built-in preset')
    text = json.dumps({'splicePresets': 1, 'presets': clean}, indent=2) + '\n'
    _check(len(text) <= PRESET_FILE_LIMIT * 4, 'The presets are too large to save')
    path = user_presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)
    return clean


def import_presets(text):
    """Presets from a shared file (``{"splicePresets": 1, "presets": [...]}`` or one preset)."""
    _check(isinstance(text, str) and len(text) <= PRESET_FILE_LIMIT, 'The preset file is too large')
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError('The preset file is not JSON') from None
    entries = data.get('presets') if isinstance(data, dict) and 'presets' in data else [data]
    _check(isinstance(entries, list) and 0 < len(entries) <= MAX_USER_PRESETS, 'The preset file holds no presets')
    result = []
    for entry in entries:
        _check(isinstance(entry, dict), 'Invalid preset')
        entry = {k: v for k, v in entry.items() if k != 'id'}
        result.append(validate_preset(entry))
    return result
