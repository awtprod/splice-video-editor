"""Check that FFmpeg has the filters, codecs and muxers Splice uses.

The bundled desktop app runs ``server.py --require-ffmpeg-features``, which
refuses to start with a readable message instead of failing in the middle of
a render. ``python runtime_check.py`` prints the same report; ``--strict``
also fails on the optional features (the installer build check).
"""
import re
import shutil
import subprocess
import sys

from splice_platform import HIDE_CONSOLE

# Filters the editor, renderer, audio finishing and colour pages build graphs from.
REQUIRED_FILTERS = (
    # picture
    'alphaextract', 'alphamerge', 'blend', 'color', 'colorchannelmixer', 'colorspace', 'concat', 'crop',
    'curves', 'drawbox', 'drawtext', 'eq', 'exposure', 'extractplanes', 'fade', 'format', 'fps', 'geq', 'hue',
    'loop', 'lut', 'lut1d', 'lut3d', 'lutrgb', 'lutyuv', 'maskedmerge', 'mergeplanes', 'metadata', 'null',
    'overlay', 'pad', 'rotate', 'scale', 'setparams', 'setpts', 'setsar', 'settb', 'signalstats', 'split',
    'tonemap', 'tpad', 'trim', 'zscale',
    # sound
    'acompressor', 'adelay', 'aeval', 'aformat', 'agate', 'ametadata', 'amix', 'amultiply', 'anull',
    'anullsrc', 'apad', 'aresample', 'areverse', 'asetpts', 'astats', 'atempo', 'atrim', 'biquad', 'ebur128',
    'highpass', 'highshelf', 'lowpass', 'lowshelf', 'pan', 'volume',
)
REQUIRED_ENCODERS = (
    'libx264', 'libx264rgb', 'libx265', 'prores_ks', 'dnxhd', 'ffv1', 'libvpx-vp9', 'png', 'tiff', 'dpx', 'exr',
    'aac', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'mov_text',
)
REQUIRED_DECODERS = ('h264', 'hevc', 'prores', 'dnxhd', 'png', 'tiff', 'exr', 'aac', 'pcm_s24le')
REQUIRED_MUXERS = ('mp4', 'mov', 'mxf', 'wav', 'image2', 'null', 'rawvideo', 'f32le', 's16le')

# Features Splice uses when present and works around (or hides) when not.
OPTIONAL_FILTERS = ('vidstabdetect', 'vidstabtransform', 'rubberband')
OPTIONAL_ENCODERS = ('libsvtav1',)
OPTIONAL_DECODERS = ('prores_raw',)

_LIST_PATTERNS = {
    # " T.. zscale   V->V  Apply resizing..." (flags column, then the name)
    '-filters': re.compile(r'^\s*[T.][S.]?[C.]?\s+(\S+)\s+\S+->\S+', re.M),
    # " V....D libx264   ..." (six flag characters, then the name)
    '-encoders': re.compile(r'^\s*[VAS][A-Z.]{5}\s+(\S+)', re.M),
    '-decoders': re.compile(r'^\s*[VAS][A-Z.]{5}\s+(\S+)', re.M),
    # " DE mp4  MP4 (MPEG-4 Part 14)" / "  E null" (FFmpeg 7+: -muxers lists E only)
    '-muxers': re.compile(r'^\s*D?E\s+(\S+)', re.M),
}


class FFmpegUnavailable(Exception):
    pass


def _listing(ffmpeg, flag, timeout):
    try:
        result = subprocess.run([ffmpeg, '-hide_banner', flag], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, creationflags=HIDE_CONSOLE)
    except subprocess.TimeoutExpired:
        raise FFmpegUnavailable(f'FFmpeg did not answer "{flag}" within {timeout:g} seconds.') from None
    except OSError as error:
        raise FFmpegUnavailable(f'FFmpeg could not start ({error.strerror or error}).') from None
    if result.returncode:
        tail = result.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:] or [f'exit code {result.returncode}']
        raise FFmpegUnavailable(f'FFmpeg failed to list its {flag[1:]} ({tail[0][:160]}).')
    names = set(_LIST_PATTERNS[flag].findall(result.stdout.decode('utf-8', 'replace')))
    if not names:
        raise FFmpegUnavailable(f'FFmpeg printed no {flag[1:]}; it may be damaged.')
    return names


def report(ffmpeg=None, ffprobe=None, timeout=20):
    """{'ffmpeg', 'ffprobe', 'missing': {kind: [...]}, 'optional': {kind: [...]}}; raises FFmpegUnavailable."""
    ffmpeg = ffmpeg or shutil.which('ffmpeg')
    ffprobe = ffprobe or shutil.which('ffprobe')
    if not ffmpeg:
        raise FFmpegUnavailable('ffmpeg was not found.')
    if not ffprobe:
        raise FFmpegUnavailable('ffprobe was not found next to ffmpeg.')
    found = {flag: _listing(ffmpeg, flag, timeout) for flag in _LIST_PATTERNS}

    def absent(flag, wanted):
        return [name for name in wanted if name not in found[flag]]

    return dict(
        ffmpeg=ffmpeg, ffprobe=ffprobe,
        missing={kind: names for kind, names in (
            ('filters', absent('-filters', REQUIRED_FILTERS)), ('encoders', absent('-encoders', REQUIRED_ENCODERS)),
            ('decoders', absent('-decoders', REQUIRED_DECODERS)), ('muxers', absent('-muxers', REQUIRED_MUXERS))) if names},
        optional={kind: names for kind, names in (
            ('filters', absent('-filters', OPTIONAL_FILTERS)), ('encoders', absent('-encoders', OPTIONAL_ENCODERS)),
            ('decoders', absent('-decoders', OPTIONAL_DECODERS))) if names},
    )


def _describe(groups):
    return '; '.join(f'{kind}: {", ".join(names)}' for kind, names in groups.items())


def startup_problem(ffmpeg=None, ffprobe=None, timeout=20, strict=False):
    """A message to show instead of starting, or '' when FFmpeg has everything required."""
    try:
        result = report(ffmpeg, ffprobe, timeout)
    except FFmpegUnavailable as error:
        return f'Splice cannot start: {error} Reinstall Splice to restore its bundled FFmpeg.'
    missing = dict(result['missing'])
    if strict:
        for kind, names in result['optional'].items():
            missing[kind] = missing.get(kind, []) + names
    if not missing:
        return ''
    return ('Splice cannot start: its FFmpeg (' + result['ffmpeg'] + ') is missing ' + _describe(missing) +
            '. Reinstall Splice to restore its bundled FFmpeg, or install an FFmpeg build that includes them.')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--strict', action='store_true', help='Also fail when an optional feature is missing.')
    args = parser.parse_args(argv)
    problem = startup_problem(strict=args.strict)
    if problem:
        print(problem, file=sys.stderr)
        return 3
    result = report()
    print(f'FFmpeg {result["ffmpeg"]} has every required filter, encoder, decoder and muxer.')
    if result['optional']:
        print('Optional features not available: ' + _describe(result['optional']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
