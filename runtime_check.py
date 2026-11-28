"""Check that FFmpeg has the filters, codecs and muxers Splice uses.

The bundled desktop app runs ``server.py --require-ffmpeg-features``, which
refuses to start with a readable message instead of failing in the middle of
a render. ``python runtime_check.py`` prints the same report; ``--strict``
also fails on the optional features (the installer build check).
"""
import re
import subprocess
import sys
import time

import splice_platform

# Filters the editor, renderer, audio finishing and colour pages build graphs from.
# tests/test_windows_installer.py (FFmpegFeatureListsMatchTheCode) scans the shipped
# sources and fails when a name is used but not listed here, or listed but unused.
REQUIRED_FILTERS = (
    # picture
    'alphaextract', 'alphamerge', 'blend', 'color', 'colorchannelmixer', 'copy', 'crop', 'drawbox', 'drawtext', 'exposure',
    'extractplanes', 'fade', 'format', 'fps', 'geq', 'loop', 'lut1d', 'lut3d', 'lutrgb', 'lutyuv', 'maskedmerge',
    'mergeplanes', 'metadata', 'null', 'overlay', 'pad', 'reverse', 'rotate', 'scale', 'setparams', 'setpts',
    'setsar', 'settb', 'showinfo', 'signalstats', 'split', 'tonemap', 'tpad', 'trim', 'zscale',
    # sound
    'acompressor', 'adelay', 'aeval', 'aformat', 'agate', 'ametadata', 'amix', 'amultiply', 'anull', 'anullsrc',
    'apad', 'aresample', 'areverse', 'asetpts', 'astats', 'atempo', 'atrim', 'biquad', 'ebur128', 'highpass',
    'join', 'lowpass', 'pan', 'volume',
)
REQUIRED_ENCODERS = (
    'libx264', 'libx264rgb', 'libx265', 'prores_ks', 'dnxhd', 'ffv1', 'libvpx-vp9', 'png', 'tiff', 'dpx', 'exr',
    'aac', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'mov_text',
)
REQUIRED_DECODERS = ('h264', 'hevc', 'prores', 'dnxhd', 'png', 'tiff', 'exr', 'aac', 'pcm_s24le')
# Bitstream filters that rewrite the colour description of H.264 proxies and ProRes masters.
REQUIRED_BITSTREAM_FILTERS = ('h264_metadata', 'prores_metadata')
# 'webm' is the alpha edit proxy and 'ipod' the audio-only .m4a proxy.
REQUIRED_MUXERS = ('mp4', 'mov', 'mxf', 'wav', 'webm', 'ipod', 'image2', 'null', 'rawvideo', 'f32le',
                   's16le')
# 'lavfi' generates swatches and test sources; 'concat' joins rendered segments.
REQUIRED_DEMUXERS = ('concat', 'lavfi', 'image2', 'rawvideo', 'f32le')

# Features Splice uses when present and works around (or hides) when not.
# vid.stab: the effects engine's stabilizer checks for it itself (vidstab_available()).
OPTIONAL_FILTERS = ('vidstabdetect', 'vidstabtransform')
OPTIONAL_ENCODERS = ('libsvtav1',)
OPTIONAL_DECODERS = ('prores_raw',)

# Each listing may take PROBE_TIMEOUT seconds and all of them TOTAL_TIMEOUT, which stays well
# under the desktop launcher's 40 second startup limit (main.cjs), so a hung FFmpeg is
# reported as such rather than as a server that never started.
PROBE_TIMEOUT = 10
TOTAL_TIMEOUT = 30

_LIST_PATTERNS = {
    # " T.. zscale   V->V  Apply resizing..." (flags column, then the name)
    '-filters': re.compile(r'^\s*[T.][S.]?[C.]?\s+(\S+)\s+\S+->\S+', re.M),
    # " V....D libx264   ..." (six flag characters, then the name; not the " V..... = Video" legend)
    '-encoders': re.compile(r'^\s*[VAS][A-Z.]{5}\s+(?!=)(\S+)', re.M),
    '-decoders': re.compile(r'^\s*[VAS][A-Z.]{5}\s+(?!=)(\S+)', re.M),
    # "Bitstream filters:" then one name per line
    '-bsfs': re.compile(r'^([a-z0-9_]+)\s*$', re.M),
    # " DE mp4  MP4 (MPEG-4 Part 14)" / "  E null" (FFmpeg 7+: -muxers lists E only)
    '-muxers': re.compile(r'^\s*D?E\s+(\S+)', re.M),
    # " D   concat  Virtual concatenation script" / " D d lavfi  Libavfilter virtual input device"
    '-demuxers': re.compile(r'^\s*DE?\s+(?:d\s+)?(?!=)(\S+)\s', re.M),
}
_KINDS = (('filters', '-filters'), ('encoders', '-encoders'), ('decoders', '-decoders'),
          ('bitstream filters', '-bsfs'), ('muxers', '-muxers'), ('demuxers', '-demuxers'))


class FFmpegUnavailable(Exception):
    pass


def _listing(ffmpeg, flag, timeout):
    try:
        result = splice_platform.run([ffmpeg, '-hide_banner', flag], stdin=subprocess.DEVNULL, capture_output=True,
                                     timeout=timeout)
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


def report(ffmpeg=None, ffprobe=None, timeout=None, total=None):
    """{'ffmpeg', 'ffprobe', 'missing': {kind: [...]}, 'optional': {kind: [...]}}; raises FFmpegUnavailable.
    Each listing gets ``timeout`` seconds (PROBE_TIMEOUT), and all of them together ``total``
    (TOTAL_TIMEOUT)."""
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    total = TOTAL_TIMEOUT if total is None else total
    ffmpeg = ffmpeg or splice_platform.ffmpeg_tool('ffmpeg')
    ffprobe = ffprobe or splice_platform.ffmpeg_tool('ffprobe')
    if not ffmpeg:
        raise FFmpegUnavailable('ffmpeg was not found.')
    if not ffprobe:
        raise FFmpegUnavailable('ffprobe was not found next to ffmpeg.')
    deadline = time.monotonic() + total
    found = {}
    for _, flag in _KINDS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FFmpegUnavailable(f'FFmpeg did not list its features within {total:g} seconds.')
        found[flag] = _listing(ffmpeg, flag, min(timeout, remaining))
    required = dict(zip((flag for _, flag in _KINDS), (
        REQUIRED_FILTERS, REQUIRED_ENCODERS, REQUIRED_DECODERS, REQUIRED_BITSTREAM_FILTERS, REQUIRED_MUXERS,
        REQUIRED_DEMUXERS)))
    optional = {'-filters': OPTIONAL_FILTERS, '-encoders': OPTIONAL_ENCODERS, '-decoders': OPTIONAL_DECODERS}

    def absent(wanted):
        groups = {kind: [name for name in wanted.get(flag, ()) if name not in found[flag]] for kind, flag in _KINDS}
        return {kind: names for kind, names in groups.items() if names}

    return dict(ffmpeg=ffmpeg, ffprobe=ffprobe, missing=absent(required), optional=absent(optional))


def _describe(groups):
    return '; '.join(f'{kind}: {", ".join(names)}' for kind, names in groups.items())


def problem_in(result, strict=False):
    """The message for a ``report()`` result, or '' when it has everything required."""
    missing = dict(result['missing'])
    if strict:
        for kind, names in result['optional'].items():
            missing[kind] = missing.get(kind, []) + names
    if not missing:
        return ''
    return ('Splice cannot start: its FFmpeg (' + result['ffmpeg'] + ') is missing ' + _describe(missing) +
            '. Reinstall Splice to restore its bundled FFmpeg, or install an FFmpeg build that includes them.')


def _unavailable(error):
    return f'Splice cannot start: {error} Reinstall Splice to restore its bundled FFmpeg.'


def startup_problem(ffmpeg=None, ffprobe=None, timeout=None, strict=False, total=None):
    """A message to show instead of starting, or '' when FFmpeg has everything required."""
    try:
        return problem_in(report(ffmpeg, ffprobe, timeout, total), strict)
    except FFmpegUnavailable as error:
        return _unavailable(error)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--strict', action='store_true', help='Also fail when an optional feature is missing.')
    args = parser.parse_args(argv)
    try:
        result = report()
    except FFmpegUnavailable as error:
        print(_unavailable(error), file=sys.stderr)
        return 3
    problem = problem_in(result, args.strict)
    if problem:
        print(problem, file=sys.stderr)
        return 3
    print(f'FFmpeg {result["ffmpeg"]} has every required filter, encoder, decoder, bitstream filter, muxer and '
          'demuxer.')
    if result['optional']:
        print('Optional features not available: ' + _describe(result['optional']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
