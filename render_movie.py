"""Render a Splice timeline to a deterministic MP4 with FFmpeg."""
import collections
import heapq
import json
import math
import os
import shutil
import struct
import threading
import time
from fractions import Fraction
from functools import lru_cache
import subprocess
import tempfile
from pathlib import Path

from premiere import MEDIA_ROOT, STILL_IMAGES, _media_path
from media_proxy import AVC_REC709, HDR_LINEAR_REC709, HDR_TONE_MAP, HDR_TRANSFERS, REC709, tempo_filters
from splice_platform import data_directory, font_roots, installed_font
import delivery


PQ = ('bt2020','smpte2084','bt2020nc')

# Feature-scale limits. Every limit below produces an error that names it.
MAX_CLIPS = 20_000
MAX_MEDIA = 5_000            # distinct media referenced by the timeline
MAX_MEDIA_POOL = 50_000      # project bin entries, used or not
MAX_TIMELINE_SECONDS = 12 * 3600
MAX_SOURCE_SECONDS = 48 * 3600
MAX_FRAME_WIDTH, MAX_FRAME_HEIGHT = 8192, 4320
MAX_CAPTIONS = 50_000
MAX_KEYFRAMES = 100
AUDIO_RATE = 48000
# Audio is mixed in bounded chunks: each FFmpeg run reads only the source
# ranges its chunk needs. The preroll lets stateful filters (the limiter,
# tempo, resampling) settle before the chunk's first kept sample.
AUDIO_CHUNK_SECONDS = 60
AUDIO_CHUNK_EVENTS = 32
AUDIO_PREROLL_SECONDS = 0.5   # 10x the limiter's release time
AUDIO_POSTROLL_SECONDS = 0.1  # the limiter looks ahead past the chunk's last sample
AUDIO_SHARED_INPUT_SECONDS = 60
# atempo drops its last fragment; read this much more source (in output seconds).
TEMPO_TAIL_SECONDS = 0.25
# A reversed stateful event is pre-rendered whole only up to this much source
# (areverse holds the span in memory); longer ones restart at chunk seams.
AUDIO_REVERSE_PRERENDER_SECONDS = 600
# Pictures seek this far before the first source read so the frame on screen
# at the in point (which may start earlier) is decoded.
SEEK_MARGIN_SECONDS = .05
# MPEG transport and program streams (AVCHD .mts, .m2ts, .ts, .mpg) seek by
# decode time and can land on the keyframe after the one needed.
STREAM_FORMATS = {'mpegts', 'mpeg', 'mpegvideo'}
SEEK_MARGIN_STREAM_SECONDS = .5
# The monitor shows a reversed clip at source time t-1e-6 (engine.js sourceTime).
REVERSE_SAMPLE_BIAS = 1e-6
# A forward sample point counts a frame starting this little after it as shown.
FORWARD_SAMPLE_TOLERANCE = 1e-6
VIDEO_WINDOW_SECONDS = 6
VIDEO_WINDOW_CUTS = 24
# Reversed pictures are buffered uncompressed. Shorten windows to stay inside this.
REVERSE_BUFFER_BYTES = 768 * 1024 * 1024
DEFAULT_STALL_SECONDS = 300
DEFAULT_LIMITER_CEILING_DB = -1.0
# Lossy (AAC) deliveries limit true peak (4x oversampled) before the encoder.
# The encoder adds overshoot of its own (about 2 dB on some programs): the
# decoded file is measured once and reported, with a warning when it is over
# the ceiling. Only the opt-in aacPeakSafe option lowers the whole program to
# fit, in fixed steps from the measured excess.
TRUE_PEAK_OVERSAMPLE = 4
AAC_PEAK_ATTEMPTS = 6
AAC_PEAK_STEP_DB = .5
# A disabled clip next to a transition is kept as a transparent stand-in with
# this media id, so its neighbour's transition runs against nothing.
DISABLED_MEDIA = '\x00disabled'
WORK_PREFIXES = ('splice-export-', 'splice-segments-', 'splice-audio-', 'splice-lut-', 'splice-render-')
LOCK_NAME = '.splice-owner.lock'


def clip_enabled(clip):
    """A clip with ``enabled`` False contributes no picture and no sound (absent or True: enabled)."""
    return clip.get('enabled') is not False


def _disabled_stand_ins(clips, fps):
    """Drop disabled picture clips; keep those on either side of a transition as transparent stand-ins.

    Rule: the transition still happens, with the disabled side contributing
    nothing. A dissolve from or to a disabled clip dissolves the enabled clip
    with whatever lies beneath it (black on the lowest track); a wipe wipes the
    enabled clip on or off over the layers beneath; a dip to black still dips
    the enabled side to or from black.
    """
    joined = set()
    for clip in clips:
        if clip.get('type') == 'video' and clip.get('transition') in ('dissolve', 'wipe', 'fade'):
            joined.add((clip.get('track', 0), round(float(clip['start'])*fps)))
    kept = []
    for clip in clips:
        if clip_enabled(clip):
            kept.append(clip)
        elif clip.get('type') == 'video' and (clip.get('transition') in ('dissolve', 'wipe', 'fade') or
                                               (clip.get('track', 0), round((float(clip['start'])+float(clip['duration']))*fps)) in joined):
            stand_in = {key: value for key, value in clip.items() if key in
                        ('id', 'type', 'track', 'start', 'duration', 'transition', 'transitionBefore', 'transitionDuration',
                         'transitionMatchName')}
            stand_in.update(mediaId=DISABLED_MEDIA, enabled=False)
            kept.append(stand_in)
    return kept


# Stills the renderer loops as pictures. TIFF, OpenEXR and DPX keep their bit depth through
# the high-depth (10-bit) pipeline.
RENDER_STILLS = frozenset(STILL_IMAGES | {'.tif', '.tiff', '.exr', '.dpx'})


def _render_source(item):
    src = item.get('src')
    if not src:
        raise ValueError(f"Relink {item.get('name') or 'the offline source'} before exporting.")
    import media_links
    return media_links.resolve_media(src)


def _number(value, label, low=0, high=MAX_SOURCE_SECONDS):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'Invalid {label}')
    if not low <= value <= high:
        raise ValueError(f'Invalid {label}: {value:g} is outside the supported range {low:g} to {high:g}')
    return float(value)


def _duration_text(seconds):
    return f'{seconds/3600:g} hours' if seconds >= 3600 else f'{seconds:g} seconds'


def work_root():
    """Directory for render intermediates: SPLICE_TEMP_DIR, else under Splice's data directory."""
    override = os.environ.get('SPLICE_TEMP_DIR')
    root = Path(override).expanduser() if override else data_directory() / 'render-work'
    where = 'SPLICE_TEMP_DIR' if override else "Splice's data folder"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(f'Cannot create the render scratch folder {root} ({error.strerror or error}). '
                         f'It comes from {where}; set SPLICE_TEMP_DIR to a writable folder.') from None
    if not os.access(root, os.W_OK | os.X_OK):
        raise ValueError(f'The render scratch folder {root} is not writable. '
                         f'It comes from {where}; set SPLICE_TEMP_DIR to a writable folder.')
    return root.resolve()


def _lock_exclusive(handle):
    """Take a non-blocking exclusive lock; the OS releases it if the owner dies."""
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class WorkDirectory:
    """A render scratch directory that stays locked while its owner runs.

    Stale directories (owner gone after an unclean shutdown) are found by
    reap_stale_work because their lock can be taken.
    """

    def __init__(self, prefix, root=None):
        if not prefix.startswith(WORK_PREFIXES):
            raise ValueError('Unknown render work directory kind')
        self.name = tempfile.mkdtemp(prefix=prefix, dir=root or work_root())
        try:
            self._lock = open(Path(self.name) / LOCK_NAME, 'a+b')
            _lock_exclusive(self._lock)
        except BaseException:
            shutil.rmtree(self.name, ignore_errors=True)
            raise

    def cleanup(self):
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        if os.path.isdir(self.name):
            shutil.rmtree(self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        self.cleanup()


def reap_stale_work(root=None, *, unlocked_grace=600):
    """Delete render scratch directories whose owning process is gone. Returns the count removed."""
    root = Path(root) if root else work_root()
    removed = 0
    for entry in root.iterdir():
        if not entry.name.startswith(WORK_PREFIXES) or entry.is_symlink() or not entry.is_dir():
            continue
        lock_path = entry / LOCK_NAME
        try:
            if not lock_path.exists():
                # Created by mkdtemp a moment ago, before its lock file existed.
                if time.time() - entry.stat().st_mtime < unlocked_grace:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += not entry.exists()
                continue
            handle = open(lock_path, 'r+b')
        except FileNotFoundError:
            continue  # Its owner finished and removed it meanwhile.
        try:
            _lock_exclusive(handle)
        except OSError:
            handle.close()
            continue  # A live export owns it.
        handle.close()
        shutil.rmtree(entry, ignore_errors=True)
        removed += not entry.exists()
    return removed


def _stall_seconds():
    value = os.environ.get('SPLICE_EXPORT_STALL_SECONDS')
    if not value:
        return DEFAULT_STALL_SECONDS
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError('SPLICE_EXPORT_STALL_SECONDS must be a number of seconds') from None
    if not 1 <= seconds <= 86400:
        raise ValueError('SPLICE_EXPORT_STALL_SECONDS must be between 1 and 86400')
    return seconds


def _ensure_space(directory, needed, purpose):
    free = shutil.disk_usage(directory).free
    reserve = 256 * 1024 * 1024
    if free < needed + reserve:
        raise ValueError(f'Not enough free disk space for {purpose}: needs about {(needed+reserve)/1e9:.1f} GB '
                         f'in {directory}, {free/1e9:.1f} GB free. Free space there or set SPLICE_TEMP_DIR '
                         'to a folder on a larger drive.')


@lru_cache(maxsize=1)
def _graph_file_option():
    """FFmpeg 7+ reads any option from a file with -/option; older builds use -filter_complex_script."""
    with tempfile.TemporaryDirectory(prefix='splice-render-probe-') as directory:
        script = Path(directory) / 'graph.txt'
        script.write_text('anullsrc=d=0.01[a]', encoding='utf-8')
        for option in ('-/filter_complex', '-filter_complex_script'):
            try:
                result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', option, str(script), '-map', '[a]', '-f', 'null', '-'],
                                        capture_output=True, timeout=30)
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0:
                return option
    raise ValueError('This FFmpeg cannot read filter graphs from a file; install FFmpeg 4 or newer')


def _graph_args(graph, directory, name):
    """Pass a filter graph through a file: command lines cannot hold feature-length graphs."""
    script = Path(directory) / f'{name}.filtergraph'
    script.write_text(graph, encoding='utf-8')
    return [_graph_file_option(), str(script)]


def _stop_tree(process, force=True):
    """Stop FFmpeg. On Windows stop its whole process tree (taskkill /T); on
    POSIX anything it started stays in the export job's process group, which
    the job kills when its worker ends (export_jobs)."""
    if os.name == 'nt':
        taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
        try:
            subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            pass
    if process.poll() is None:
        process.kill() if force else process.terminate()


def _run_ffmpeg(command, cancelled=None, heartbeat=None):
    """Run FFmpeg until it exits. Fail only when it makes no progress for the stall limit."""
    if command[0] != 'ffmpeg':
        raise ValueError('Internal error: not an FFmpeg command')
    stall = _stall_seconds()
    command = [command[0], '-progress', 'pipe:1', '-stats_period', '1', *command[1:]]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    state = {'progress': time.monotonic(), 'last': None}
    lock = threading.Lock()
    errors = collections.deque(maxlen=200)

    def read_progress():
        block = []
        for raw in process.stdout:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('progress='):
                if line.startswith(('frame=', 'total_size=', 'out_time_us=')):
                    block.append(line)
                continue
            if block and tuple(block) != state['last']:
                with lock:
                    state['last'] = tuple(block)
                    state['progress'] = time.monotonic()
            block = []

    def read_errors():
        for raw in process.stderr:
            errors.append(raw.decode('utf-8', 'replace'))

    readers = [threading.Thread(target=read_progress, daemon=True), threading.Thread(target=read_errors, daemon=True)]
    for reader in readers:
        reader.start()
    seen = state['progress']
    try:
        while process.poll() is None:
            if cancelled and cancelled.is_set():
                _stop_tree(process, force=False)
                raise InterruptedError('Export cancelled')
            with lock:
                last = state['progress']
            if last != seen:
                seen = last
                if heartbeat:
                    heartbeat()
            if time.monotonic() - last > stall:
                _stop_tree(process)
                raise TimeoutError(f'FFmpeg made no progress for {stall:g} seconds, so the export was stopped '
                                   '(limit set by SPLICE_EXPORT_STALL_SECONDS)')
            time.sleep(.2)
        for reader in readers:
            reader.join(timeout=5)
        if process.returncode:
            raise ValueError('FFmpeg render failed: ' + ''.join(errors)[-1500:])
        if heartbeat:
            heartbeat()
        return ''.join(errors)
    finally:
        if process.poll() is None:
            _stop_tree(process)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass  # Killed; the OS reaps it once it leaves uninterruptible I/O.
        for reader in readers:
            reader.join(timeout=2)
        if all(not reader.is_alive() for reader in readers):
            process.stdout.close()
            process.stderr.close()
        # Otherwise a process FFmpeg started still holds its pipes: closing
        # them would block until it exits. The daemon readers end with it.


def _probe(path):
    from media_proxy import MAX_AUDIO_STREAMS
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries',
                            'stream=codec_type,channels,sample_rate,width,height,color_primaries,color_transfer,color_space,time_base'
                            ':format=duration,start_time,format_name', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(result.stdout)
    streams = data.get('streams', [])
    if not isinstance(streams, list) or any(not isinstance(stream, dict) for stream in streams):
        raise ValueError(f'Could not read the streams of {Path(path).name}')
    audio = [stream.get('channels', 2) for stream in streams if stream.get('codec_type') == 'audio']
    rates = []
    for stream in streams:
        if stream.get('codec_type') == 'audio':
            try: rates.append(int(stream.get('sample_rate') or 0))
            except (TypeError, ValueError): rates.append(0)
    if len(audio) > MAX_AUDIO_STREAMS:
        raise ValueError(f'Media has more than {MAX_AUDIO_STREAMS} audio streams')
    video = next((stream for stream in streams if stream.get('codec_type') == 'video'), {})
    try:
        duration = float(data.get('format', {}).get('duration', 0))
    except (TypeError, ValueError):
        duration = 0.0
    # FFmpeg shifts packet timestamps by (seek + container start) rounded to the
    # stream's time base; keep both so picture clocks can use the exact shift.
    try:
        start = Fraction(str(data.get('format', {}).get('start_time', '0')))
    except (TypeError, ValueError, ZeroDivisionError):
        start = Fraction(0)
    try:
        time_base = Fraction(str(video.get('time_base')))
        if time_base <= 0: raise ValueError
    except (TypeError, ValueError, ZeroDivisionError):
        time_base = None
    return {'kinds': {stream.get('codec_type') for stream in streams}, 'duration': duration, 'audio': audio,
            'width': int(video.get('width') or 0), 'height': int(video.get('height') or 0),
            'profile': tuple(video.get(key) for key in ('color_primaries', 'color_transfer', 'color_space')),
            'start': start, 'time_base': time_base, 'rates': rates,
            'format': str(data.get('format', {}).get('format_name') or '')}


def _stereo_pan(pan, channels):
    # Mirror the Web Audio StereoPannerNode used for playback.
    if channels == 1:
        angle = (pan + 1) * math.pi / 4
        return f'pan=stereo|c0={math.cos(angle):.8f}*c0|c1={math.sin(angle):.8f}*c0'
    if pan <= 0:
        angle = (pan + 1) * math.pi / 2
        matrix = f'c0=c0+{math.cos(angle):.8f}*c1|c1={math.sin(angle):.8f}*c1'
    else:
        angle = pan * math.pi / 2
        matrix = f'c0={math.cos(angle):.8f}*c0|c1=c1+{math.sin(angle):.8f}*c0'
    return 'aformat=channel_layouts=stereo,pan=stereo|' + matrix


def _font_path(family):
    bundled={'DM Sans':'dm-sans.ttf','IBM Plex Mono':'plex-mono.ttf'}
    if not isinstance(family,str) or not family or len(family)>100 or any(c in family for c in '\r\n\x00'):
        raise ValueError('Invalid title typeface')
    if family in bundled:
        return Path(__file__).resolve().parent/'assets'/'fonts'/bundled[family]
    candidate=installed_font(family)
    if candidate:
        return candidate[1]
    from shutil import which
    resolved_family, resolved = 'DM Sans', Path(__file__).resolve().parent/'assets'/'fonts'/'dm-sans.ttf'
    fontconfig = which('fc-match')
    if fontconfig:
        match=subprocess.run([fontconfig,'-f','%{family}|%{file}',family],capture_output=True,text=True,timeout=5,check=True).stdout
        name,separator,path=match.partition('|')
        if separator:
            path=Path(path).resolve()
            if path.is_file() and any(path.is_relative_to(root.resolve()) for root in font_roots()):
                resolved_family,resolved=name,path
    if family.casefold() not in resolved_family.casefold():
        import warnings
        warnings.warn(f'Title font {family!r} is unavailable; using {resolved_family!r}',stacklevel=2)
    return resolved


def _filter_path(path):
    # FFmpeg parses option values, then the filter graph itself.
    value=path.as_posix()
    option=''.join('\\'+char if char in "\\':" else char for char in value)
    return ''.join('\\'+char if char in "\\'[],;" else char for char in option)


def _grade_values(color):
    brightness=_number(color.get('brightness',100),'brightness',0,300)/100
    contrast=_number(color.get('contrast',100),'contrast',0,300)/100
    saturation=_number(color.get('saturation',100),'saturation',0,300)/100
    exposure=_number(color.get('exposure',0),'exposure',-4,4)
    temperature=_number(color.get('temperature',0),'temperature',-100,100)/100
    tint=_number(color.get('tint',0),'tint',-100,100)/100
    gain=2**exposure
    gains=(gain*(1+.35*temperature-.1*tint),gain*(1+.2*tint),gain*(1-.35*temperature-.1*tint))
    return brightness, contrast, saturation, gains


def _saturation_matrix(saturation):
    return [[saturation*(r==c)+(1-saturation)*weight for c,weight in enumerate((.213,.715,.072))] for r in range(3)]


def _channel_mixer(matrix):
    return 'colorchannelmixer='+':'.join(f'{r}{c}={matrix[i][j]:.6f}' for i,r in enumerate('rgb') for j,c in enumerate('rgb'))


def _css_color_filter(color, depth, still=False):
    brightness,contrast,saturation,gains=_grade_values(color)
    if (brightness,contrast,saturation,gains)==(1,1,1,(1,1,1)):
        return ''
    channels='rgb'
    brightness_filter=':'.join(f"{c}=val*{brightness:.6f}" for c in channels)
    contrast_filter=':'.join(f"{c}=(val-maxval/2)*{contrast:.6f}+maxval/2" for c in channels)
    mixer=_channel_mixer(_saturation_matrix(saturation))
    grade_filter=':'.join(f'{c}=val*{g:.7f}' for c,g in zip(channels,gains))
    rgb = ('gbrap' if still else 'gbrp') + f'{depth}le' if depth > 8 else ('rgba' if still else 'rgb24')
    return f',format={rgb},lutrgb={brightness_filter},lutrgb={contrast_filter},{mixer},lutrgb={grade_filter}'


def _linear_gain(gains):
    chain=''
    # Keep the channel coefficients within ±2 without clipping float pixels.
    while max(gains)>2:
        factor=min(8,max(gains))
        chain+=f',exposure=exposure={math.log2(factor):.12f}'
        gains=tuple(value/factor for value in gains)
    if gains!=(1,1,1):
        chain+=',colorchannelmixer='+':'.join(f'{c}{c}={value:.9f}' for c,value in zip('rgb',gains))
    return chain


def _brightness_contrast(brightness, contrast, pivot=.5):
    slope=brightness*contrast
    offset=(1-contrast)*pivot
    if not offset:
        return _linear_gain((slope,)*3)
    # FFmpeg uses (pixel - black) / abs(2**-exposure - black).
    # Combine both operations when their settings are in range.
    if slope:
        black=-offset/slope
        white=(1-offset)/slope
        if -1<=black<=1 and .125<=white<=8:
            return f',exposure=exposure={-math.log2(white):.12f}:black={black:.12f}'
    # At zero or very low slope, apply the offset with a unit denominator.
    return _linear_gain((slope,)*3)+f',exposure=exposure={-math.log2(1-offset):.12f}:black={-offset:.12f}'


def _hdr_grade(color, lut, high_depth, profile, target=None, depth=10):
    yuv = f'format=yuv444p{max(10,depth)}le'
    tone_map = HDR_TONE_MAP + ',' + yuv
    brightness,contrast,saturation,gains=_grade_values(color)
    if (brightness,contrast,saturation,gains)==(1,1,1,(1,1,1)) and (not lut or not high_depth):
        if not high_depth:
            return ','+HDR_LINEAR_REC709.format(peak=203)+','+tone_map+lut
        if not target or target==profile:
            return ''
    display_grade=(brightness,contrast,saturation)!=(1,1,1)
    scale=1
    if display_grade and high_depth and not lut:
        # Scale the working signal below reference white for zscale's fast
        # gamma tables. PQ's 10,000-nit peak remains below 8 in gamma 2.4
        # after the Rec.709 matrix, before the grading controls are applied.
        scale=8*max(1,brightness*contrast*max(1,saturation))
    peak=203*scale**2.4
    chain=','+HDR_LINEAR_REC709.format(peak=peak)
    if display_grade:
        # Brightness, contrast, and saturation retain their display-referred
        # controls. Exposure and white balance below use linear light.
        gamma=f'agamma={int(scale!=1)}'
        chain+=f',zscale=t=bt709:{gamma},format=gbrpf32le'
        chain+=_brightness_contrast(brightness,contrast,pivot=.5/scale)
        if saturation!=1:
            matrix=_saturation_matrix(saturation)
            # colorchannelmixer limits a coefficient to ±2. Preserve the
            # extra gain for imported projects with a stronger saturation.
            matrix_gain=max(1,max(abs(value) for row in matrix for value in row)/2)
            chain+=','+_channel_mixer([[value/matrix_gain for value in row] for row in matrix])+_linear_gain((matrix_gain,)*3)
        chain+=f',zscale=t=linear:{gamma},format=gbrpf32le'
    chain+=_linear_gain(gains)
    if not high_depth:
        return chain+','+tone_map+lut
    if lut:
        # The approximate transfer tables can clip extended-range RGB.
        chain+=',zscale=t=bt709:agamma=0,format=gbrpf32le'+lut+',zscale=t=linear:agamma=0,format=gbrpf32le'
    primaries,transfer,matrix=target or profile
    return chain+f',zscale=p={primaries},zscale=t={transfer}:m={matrix}:r=limited:npl={peak:.9f},'+yuv


def _hdr_still_filter(profile, depth):
    primaries, transfer, matrix = profile
    # zscale applies the requested legal range to every plane. Keep the
    # converted picture in legal range and restore full-range opacity.
    scale, maximum = 1 << (depth-8), (1 << depth)-1
    limited = (f':m={matrix}:r=limited,format=yuva444p{depth}le,'
               f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='clip((p(X,Y)-{16*scale})*{maximum}/{219*scale},0,{maximum})'")
    chain = (',format=gbrapf32le,setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc,'
             f'zscale=t=linear:p={primaries},format=gbrapf32le')
    if transfer != 'arib-std-b67' or primaries != 'bt2020':
        return chain+f',zscale=t={transfer}:npl=203'+limited
    # Match the reference-display inverse OOTF used for solid title swatches.
    luma = 'max(.000001,.203*(.2627*r(X,Y)+.6780*g(X,Y)+.0593*b(X,Y)))'
    channels = [
        f"{c}='st(0,.203*max(0,{c}(X,Y))*pow({luma},-1/6));"
        "if(lte(ld(0),1/12),sqrt(3*ld(0)),.17883277*log(12*ld(0)-.28466892)+.55991073)'"
        for c in 'rgb'
    ]
    return (chain+',geq='+':'.join(channels)+":a='alpha(X,Y)',"
            f'setparams=color_primaries={primaries}:color_trc={transfer}:colorspace=gbr:range=pc,'
            'zscale=t='+transfer+limited)


def _fit_picture(width, height, color, lut, alpha_format, depth, profile=None, rec709=False,
                 *, still_frames=0, hdr_profile=None, conform_pq=False):
    wide_yuv = f'format=yuv444p{max(10,depth)}le'
    if profile and profile[1] in HDR_TRANSFERS:
        keep_hdr = depth > 8 and not rec709 and (depth <= 10 or hdr_profile is not None)
        grade = _hdr_grade(color,lut,keep_hdr,profile,PQ if conform_pq else None,depth)
    else:
        convert = (',zscale=p=bt709:t=bt709:m=bt709:r=limited,'+wide_yuv
                   if rec709 and profile and profile!=REC709
                   and all(value not in (None,'unknown','unspecified') for value in profile) else '')
        grade = convert+_css_color_filter(color,depth,bool(still_frames))+lut
        if still_frames and hdr_profile:
            grade += _hdr_still_filter(hdr_profile,depth)
        elif conform_pq and profile:
            primaries,transfer,matrix=(value if value not in (None,'unknown','unspecified') else default
                                      for value,default in zip(profile,REC709))
            grade += (f',zscale=pin={primaries}:tin={transfer}:min={matrix}:p=bt2020:t=linear,format=gbrpf32le,'
                      'zscale=t=smpte2084:m=bt2020nc:r=limited:npl=203,'+wide_yuv)
    repeat = f',loop=loop={still_frames-1}:size=1:start=0,setpts=N' if still_frames>1 else ''
    return (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2'
            f'{grade},format={alpha_format},'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0{repeat}')


def _picture_input(index, source, length, speed, width, height, fps, reverse, still_frames, *, frames=None, origin=0.0):
    """Sample a source onto the sequence frame grid.

    Output frame f shows what a player displays at source time
    source + (f/fps + origin)*speed: the last frame whose presentation time is
    at or before that point. ``source`` is relative to the input's seek point
    and the input is opened without accurate seek, so a held frame that starts
    before the seek point is still decoded. ``origin`` is the offset of the
    first output frame from the clip's own clock (fractional clip starts).
    For a reversed clip the frames are sampled forward here and the caller
    reverses their order after scaling.
    """
    if still_frames:
        return f'[{index}:v]trim=end_frame=1,setpts=PTS-STARTPTS'
    if frames is None:
        frames = max(1, math.ceil(length*float(fps)-1e-6))
    if index is None:
        return f'color=c=black:s={width}x{height}:r={fps}:d={frames/float(fps):.6f},setpts=PTS-STARTPTS,trim=end_frame={frames}'
    # Timestamps are kept in nanoseconds so rounding stays far below the biases,
    # which are in source seconds. Forward: a frame that starts up to 1
    # microsecond after the sample point counts as on screen (absorbs
    # floating-point in points). Reversed: the monitor samples 1 microsecond
    # before the point, so a frame that starts exactly there is not yet shown
    # (engine.js sourceTime).
    bias = f'+{REVERSE_SAMPLE_BIAS/speed:.12f}' if reverse else f'-{FORWARD_SAMPLE_TOLERANCE/speed:.12f}'
    clock = f"(T-({source:.9f}))/{speed:.9f}-({origin:.9f}){bias}"
    return (f"[{index}:v]settb=1/1000000000,setpts='({clock})/TB',"
            f'fps=fps={fps}:round=up:start_time=0,trim=end_frame={frames}')


def _lut_filter(color, destination, *, preserve_headroom=False):
    lut = color.get('lut')
    if not lut:
        return ''
    intensity = _number(color.get('lutIntensity',100),'LUT intensity',0,100)
    if not intensity:
        return ''
    from lut import cube_text
    domain_min = lut.get('domainMin', [0,0,0]) if isinstance(lut, dict) else [0,0,0]
    domain_max = lut.get('domainMax', [1,1,1]) if isinstance(lut, dict) else [1,1,1]
    custom_domain = domain_min != [0,0,0] or domain_max != [1,1,1]
    mix_after_lookup = custom_domain or preserve_headroom
    destination.write_text(cube_text(lut,100 if mix_after_lookup else intensity,normalized_domain=custom_domain))
    apply_lut = f'lut3d=file={_filter_path(destination)}:interp=trilinear'
    if not mix_after_lookup:
        return ','+apply_lut
    # Apply the input offset and scale explicitly. FFmpeg's .cube reader
    # ignores the offset and caps the input scale at one.
    if preserve_headroom:
        # Keep both branches in float RGB. Adding an alpha plane or using
        # integer RGB here clips HDR values before the LUT can read them.
        working_format = 'gbrpf32le'
        normalize = 'format='+working_format
        if domain_min == [0,0,0] and min(domain_max) >= .5:
            normalize += _linear_gain(tuple(1/maximum for maximum in domain_max))
        else:
            channels = ':'.join(f"{channel}='clip(({channel}(X,Y)-({low!r}))/({high-low!r}),0,1)'"
                                for channel, low, high in zip('rgb',domain_min,domain_max))
            normalize += ',geq='+channels
    else:
        working_format = 'gbrapf32le'
        channels = ':'.join(f"{channel}='clip((val-maxval*({low!r}))/({high-low!r}),0,maxval)'"
                            for channel, low, high in zip('rgb',domain_min,domain_max))
        normalize = f'format=gbrap16le,lutrgb={channels},format={working_format}'
    graded = f'{normalize},{apply_lut}'
    if intensity == 100:
        return ','+graded
    label = '_lut_'+destination.stem.replace('-','_')
    return (f',format={working_format},split[{label}_original][{label}_input];'
            f'[{label}_input]{graded}[{label}_graded];'
            f'[{label}_graded][{label}_original]blend=all_mode=normal:all_opacity={intensity/100:.12g}')


def _mask_alpha(expression, alpha_format, full_alpha):
    return f",format={full_alpha},geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*({expression})',format={alpha_format}"


def _trim_audio(filters, label, index, streams, start, length, reverse):
    # A stream can start later than the video's source clock. Preserve the gap.
    clock = f'PTS-({start:.9f})/TB'
    trim = f'atrim=start={start:.9f}:end={start+length:.9f},asetpts={clock},aresample=48000:first_pts=0'
    if len(streams)>1:
        from media_proxy import stereo_mix_filters
        sources = [f'[{index}:a:{number}]{trim}' for number in streams]
        filters.extend(stereo_mix_filters(sources,label,list(streams.values()),length))
        result = f'[{label}]'
    else:
        result = f'[{index}:a:{next(iter(streams))}]{trim},'
    return result + ('areverse,' if reverse else '')


def _audio_transition_gain(clip, expression):
    return f'sin(PI*({expression})/2)' if clip.get('transitionMatchName')=='Constant Power' else expression


def _horizontal_position(clip, width, overlay_width, clock):
    x = _number((clip.get('transform') or {}).get('x',0),'horizontal position',-100,100)
    keyframes = _keyframe_expr(clip,'x',x,-100,100,clock)
    return f'({width}-{overlay_width})/2+{width/100:.3f}*({keyframes})'


def _frames(seconds, rate):
    """``seconds`` in frames, snapped to a whole frame when it is within rounding of one."""
    value = seconds*rate
    return round(value) if abs(value-round(value)) < 1e-6 else value


def _keyframe_clock(clip, fps, variable, zero):
    """Clip time for keyframes, as an FFmpeg expression of a filter's time ``variable``.

    ``zero`` is the filter time at which the clip's keyframe clock reads 0 in
    this window. A window that starts inside a clip keeps the clip's own
    keyframes and shifts the clock by ``_keyframeShift`` instead of re-basing
    the keyframe values. The time is counted in whole frames before the one
    division, so the same timeline frame evaluates to the same number in
    every window and positions, scales and opacities cannot change by a
    rounding step at a window seam.
    """
    shift = float(clip.get('_keyframeShift', 0) or 0)
    if fps is None:
        return f'({variable}-{zero:.9f}+{shift:.9f})'
    rate = float(fps)
    constant = _frames(shift, rate) - _frames(zero, rate)
    if abs(constant-round(constant)) < 1e-6:
        constant = round(constant)
    return f'((round({variable}*{rate!r})+({constant!r}))/{rate!r})'


def _transform(clip, width, height, local_offset=0, fps=None, *, place_on_canvas=False):
    t = clip.get('transform') or {}
    scale = _number(t.get('scale',100),'scale',10,400)/100
    y = _number(t.get('y',0),'vertical position',-100,100)/100
    angle = _number(t.get('rotation',0),'rotation',-180,180)
    local_clock=_keyframe_clock(clip,fps,'t',local_offset)
    scale_expr=_keyframe_expr(clip,'scale',scale*100,10,400,local_clock)
    timeline_clock=_keyframe_clock(clip,fps,'t',float(clip['start']))
    x = _number(t.get('x',0),'horizontal position',-100,100)
    x_expr=_keyframe_expr(clip,'x',x,-100,100,timeline_clock)
    y_expr=_keyframe_expr(clip,'y',y*100,-100,100,timeline_clock)
    angle_expr=_keyframe_expr(clip,'rotation',angle,-180,180,local_clock)
    scale_keys = (clip.get('keyframes') or {}).get('scale')
    angle_keys = (clip.get('keyframes') or {}).get('rotation')
    if scale_keys:
        filters=f",scale=w='trunc(iw*({scale_expr})/200)*2':h='trunc(ih*({scale_expr})/200)*2':eval=frame"
    else:
        filters=f',scale=trunc(iw*{scale:.5f}/2)*2:trunc(ih*{scale:.5f}/2)*2'
    if angle or angle_keys:
        # FFmpeg configures rotate's output dimensions from the first input
        # frame. Reserve space for the largest animated scale up front.
        # The corners outside the rotated picture are filled transparent:
        # rotate's c=none leaves them unwritten, so a reused frame buffer
        # shows the previous frame's pixels there (and a window's first
        # frame, with a fresh buffer, would not match a single render).
        if scale_keys:
            peak = max(float(point['value']) for point in scale_keys)/100
            if angle_keys:
                canvas_width = canvas_height = math.hypot(width,height)*peak
            else:
                sin, cos = abs(math.sin(math.radians(angle))), abs(math.cos(math.radians(angle)))
                canvas_width = (width*cos+height*sin)*peak
                canvas_height = (width*sin+height*cos)*peak
            out_width, out_height = 2*math.ceil(canvas_width/2), 2*math.ceil(canvas_height/2)
            filters += f",rotate=a='({angle_expr})*PI/180':ow={out_width}:oh={out_height}:c=black@0"
        elif angle_keys:
            filters += f",rotate=a='({angle_expr})*PI/180':ow=hypot(iw\\,ih):oh=ow:c=black@0"
        else:
            filters += f',rotate={angle:.5f}*PI/180:ow=rotw(iw):oh=roth(ih):c=black@0'
    elif scale_keys:
        # Filters after an animated scale (opacity, wipe and dip masks, the
        # overlay) keep the frame size of the first frame they see, which
        # depends on where the window starts. Center every frame on a canvas
        # sized for the largest scale so the size never changes.
        # Windows keep the whole clip's keyframes, so every window uses one
        # canvas and rounds the centering offset the same way.
        peak = max(float(point['value']) for point in scale_keys)/100
        canvas_width, canvas_height = 2*math.ceil(width*peak/2), 2*math.ceil(height*peak/2)
        filters += (f',pad=w={canvas_width}:h={canvas_height}:x=(ow-iw)/2:y=(oh-ih)/2'
                    f':color=black@0:eval=frame')
    dx, dy = f'{width/100:.3f}*({x_expr})', f'{height/100:.3f}*({y_expr})'
    if place_on_canvas:
        # Center the transformed layer on transparent padding, then move the crop window
        # in the opposite direction. Reserve only the travel used by this clip's keyframes.
        keys = clip.get('keyframes') or {}
        mx = math.ceil(width/100 * max([abs(x)] + [abs(p['value']) for p in keys.get('x') or []]))
        my = math.ceil(height/100 * max([abs(y*100)] + [abs(p['value']) for p in keys.get('y') or []]))
        position = (f"pad=w='max(iw,{width+2*mx})':h='max(ih,{height+2*my})':x=(ow-iw)/2:y=(oh-ih)/2:color=black@0,"
                    f"crop={width}:{height}:x='(iw-ow)/2-trunc({dx})':y='(ih-oh)/2-trunc({dy})'")
    else:
        position = f"x='({width}-overlay_w)/2+{dx}':y='(main_h-overlay_h)/2+{dy}'"
    return filters, position


def _keyframe_expr(clip, key, base, low, high, clock):
    points=(clip.get('keyframes') or {}).get(key) or []
    if not points: return f'{base:.6f}'
    if not isinstance(points,list): raise ValueError(f'Invalid {key} keyframes')
    if len(points)>MAX_KEYFRAMES: raise ValueError(f'A clip has more than {MAX_KEYFRAMES} {key} keyframes, the export limit')
    ordered=[]
    for point in points:
        if not isinstance(point,dict): raise ValueError(f'Invalid {key} keyframe')
        # A keyframe after the clip's end (left there by a trim) still shapes the
        # ramp up to the end, as in the monitor.
        at=_number(point.get('time'),f'{key} keyframe time',0,MAX_TIMELINE_SECONDS)
        value=_number(point.get('value'),f'{key} keyframe value',low,high)
        ordered.append((at,value))
    ordered.sort()
    if any(b[0]<=a[0] for a,b in zip(ordered,ordered[1:])): raise ValueError(f'Duplicate {key} keyframes')
    expr=f'{ordered[-1][1]:.6f}'
    for (at,value),(next_at,next_value) in reversed(list(zip(ordered,ordered[1:]))):
        ramp=f'{value:.6f}+({next_value-value:.6f})*({clock}-{at:.6f})/{next_at-at:.6f}'
        expr=f'if(lte({clock},{next_at:.6f}),{ramp},{expr})'
    return f'if(lte({clock},{ordered[0][0]:.6f}),{ordered[0][1]:.6f},{expr})'


def _slice_keyframes(keyframes, offset, length):
    if not keyframes: return keyframes
    if not isinstance(keyframes,dict) or len(keyframes)>6: raise ValueError('Invalid keyframes')
    result={}
    for key,points in keyframes.items():
        bounds={'opacity':(0,100),'x':(-100,100),'y':(-100,100),'scale':(10,400),'rotation':(-180,180),'volume':(0,200)}
        if key not in bounds or not isinstance(points,list): raise ValueError('Invalid keyframes')
        if len(points)>MAX_KEYFRAMES: raise ValueError(f'A clip has more than {MAX_KEYFRAMES} {key} keyframes, the export limit')
        if not points: continue
        if any(not isinstance(point,dict) for point in points): raise ValueError('Invalid keyframes')
        ordered=sorted(({'time':_number(point.get('time'),'keyframe time',0,MAX_SOURCE_SECONDS),
                         'value':_number(point.get('value'),'keyframe value',*bounds[key])} for point in points),key=lambda point:point['time'])
        if any(right['time']<=left['time'] for left,right in zip(ordered,ordered[1:])): raise ValueError('Duplicate keyframes')
        def value_at(at):
            if at<=ordered[0]['time']: return ordered[0]['value']
            for left,right in zip(ordered,ordered[1:]):
                if at<=right['time']:
                    fraction=(at-left['time'])/(right['time']-left['time'])
                    return left['value']+(right['value']-left['value'])*fraction
            return ordered[-1]['value']
        sliced=[{'time':0,'value':value_at(offset)}]
        sliced.extend({'time':point['time']-offset,'value':point['value']} for point in ordered if offset<point['time']<offset+length)
        sliced.append({'time':length,'value':value_at(offset+length)})
        result[key]=sliced
    return result


@lru_cache(maxsize=256)
def _hdr_graphic_color(color, primaries, transfer):
    if color == '000000':
        return color
    # Convert the authored RGB swatch to the master color space before
    # drawtext blends its glyph mask. BT.2408 places graphics white at 203 nits.
    swatch = (f'color=0x{color}:s=2x2,format=gbrp,'
              'setparams=color_primaries=bt709:color_trc=bt709:colorspace=gbr:range=pc,'
              f'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p={primaries}')
    hlg = transfer=='arib-std-b67' and primaries=='bt2020'
    if not hlg:
        swatch += f',zscale=t={transfer}:m=gbr:r=full:npl=203,format=gbrp'
    pixel_format = 'gbrpf32le' if hlg else 'rgb24'
    result = subprocess.check_output(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',swatch,
                                      '-frames:v','1','-pix_fmt',pixel_format,'-f','rawvideo','pipe:1'],timeout=5)
    if len(result) != (48 if hlg else 12):
        raise ValueError('Could not convert the HDR title color')
    if hlg:
        samples = struct.unpack('<12f',result)
        return _hlg_graphic_rgb(samples[8],samples[0],samples[4])
    return result[:3].hex()


def _hlg_graphic_rgb(red, green, blue):
    # BT.2100 inverse reference OOTF for a 1,000-nit display. Use the same
    # luminance-derived gain for all channels before applying the HLG OETF.
    display = [max(0,channel)*.203 for channel in (red,green,blue)]
    luminance = sum(channel*weight for channel,weight in zip(display,(.2627,.6780,.0593)))
    if not luminance:
        return '000000'
    inverse_gain = luminance**(1/1.2-1)
    a,b,c = .17883277,.28466892,.55991073
    encoded = []
    for channel in display:
        scene = channel*inverse_gain
        value = math.sqrt(3*scene) if scene<=1/12 else a*math.log(12*scene-b)+c
        encoded.append(round(255*min(1,value)))
    return bytes(encoded).hex()


def _draw_title(filters, active, number, clip, height, fps, directory, hdr_profile):
    begin = float(clip['start']); end = begin + float(clip['duration'])
    first_frame = math.ceil(begin * fps - 1e-6)
    out_frame = math.ceil(end * fps - 1e-6)
    style=clip.get('titleStyle') or {}
    font_path=_font_path(style.get('fontFamily','DM Sans'))
    fontsize=round(height*_number(style.get('fontSize',8),'title size',2,100)/100)
    x=_number(style.get('x',50),'title horizontal',0,100)/100
    y=_number(style.get('y',78),'title vertical',0,100)/100
    opacity = _number(clip.get('opacity',100),'title opacity',0,100)
    alpha = _keyframe_expr(clip,'opacity',opacity,0,100,_keyframe_clock(clip,fps,'t',begin))
    color=str(style.get('color','#ffffff'))
    if len(color)!=7 or not color.startswith('#') or any(c not in '0123456789abcdefABCDEF' for c in color[1:]):
        raise ValueError('Invalid title color')
    align=style.get('align','center')
    if align not in ('left','center','right'): raise ValueError('Invalid title alignment')
    xpos={'left':f'w*{x:.5f}','center':f'w*{x:.5f}-text_w/2','right':f'w*{x:.5f}-text_w'}[align]
    stroke=str(style.get('strokeColor','#000000'))
    if len(stroke)!=7 or not stroke.startswith('#') or any(c not in '0123456789abcdefABCDEF' for c in stroke[1:]):
        raise ValueError('Invalid title outline color')
    border=int(_number(style.get('strokeWidth',0),'title outline',0,8)*height/180)
    fill, outline = color[1:], stroke[1:]
    if hdr_profile:
        primaries, transfer, _ = hdr_profile
        fill = _hdr_graphic_color(fill,primaries,transfer)
        if border:
            outline = _hdr_graphic_color(outline,primaries,transfer)
    lines = str(clip['title'])[:500].split('\n')[:20]
    for row, line in enumerate(lines):
        if not line.strip():
            continue
        text_file = Path(directory) / f'title-{number}-{row}.txt'
        text_file.write_text(line.rstrip('\r'), encoding='utf-8')
        offset = (row - (len(lines) - 1) / 2) * fontsize * 1.2
        label = f'title{number}line{row}'
        filters.append(
            f'[{active}]drawtext=fontfile={_filter_path(font_path)}:textfile={_filter_path(text_file)}:expansion=none:'
            f'fontsize={fontsize}:fontcolor=0x{fill}:borderw={border}:bordercolor=0x{outline}:'
            f'shadowcolor=black:shadowx=3:shadowy=3:x={xpos}:y=h*{y:.5f}+({offset:.3f})-text_h/2:'
            f"alpha='({alpha})/100':"
            f'enable=gte(n\\,{first_frame})*lt(n\\,{out_frame})[{label}]')
        active = label
    return active


def _render_window(project, output_path, directory, *, options=None, cancelled=None):
    window_directory = tempfile.mkdtemp(prefix='window-', dir=directory)
    try:
        return _render_window_inner(project, output_path, window_directory, options=options, cancelled=cancelled)
    finally:
        shutil.rmtree(window_directory, ignore_errors=True)


def _picture_transitions(clips, media_by_id, fps):
    by_end = {}
    predecessors, followers = {}, {}
    for number, clip in enumerate(clips):
        if media_by_id.get(clip.get('mediaId'), {}).get('nativeGenerator') == 1414680150:
            continue
        track = clip.get('track',0)
        start = float(clip['start'])
        prior = by_end.get((track,round(start*fps)))
        if prior is not None and clip.get('transition') in ('dissolve','wipe','fade'):
            predecessors[number] = prior
            followers[prior] = number
        by_end[track,round((start+float(clip['duration']))*fps)] = number
    return predecessors, followers


def _picture_range_media(clips, media_by_id, fps, first, stop):
    _, followers = _picture_transitions(clips, media_by_id, fps)
    active = set()
    for number, clip in enumerate(clips):
        cut = float(clip['start'])
        lead = float(clip.get('transitionBefore',0) or 0) if clip.get('transition') in ('dissolve','wipe') else 0
        end = cut+float(clip['duration'])
        following = clips[followers[number]] if number in followers else None
        if following and following.get('transition') in ('dissolve','wipe'):
            before = float(following.get('transitionBefore',0) or 0)
            tail = min(float(following.get('transitionDuration',.5))-before,float(following['duration']))
            if tail >= 1/fps-1e-6:
                end = max(end,float(following['start'])+tail)
        if math.ceil((cut-lead)*fps-1e-6)<stop and math.ceil(end*fps-1e-6)>first:
            active.add(clip.get('mediaId'))
    return active


def _frame_size(project):
    size = []
    for key, limit in (('width', MAX_FRAME_WIDTH), ('height', MAX_FRAME_HEIGHT)):
        value = project.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
            raise ValueError(f'Invalid frame {key}')
        value = int(value)
        if not 16 <= value <= limit:
            raise ValueError(f'Frame {key} {value} is outside the supported range 16 to {limit} pixels')
        if value % 2:
            raise ValueError(f'Frame {key} {value} must be an even number of pixels')
        size.append(value)
    return tuple(size)


def _sequence_fps(project):
    return Fraction(_number(project.get('fps'), 'frame rate', 1, 120)).limit_denominator(1001)


def _media_info(item, probes, heartbeat=None):
    """Probe a media item once per export and cache what the renderer needs on the item."""
    if '_kinds' in item:
        return item
    generator = item.get('nativeGenerator')
    if generator not in (None,1112293707,1414680150) or item.get('nativeUnsupported'):
        raise ValueError(f"Premiere generator {item.get('name','')} is not supported for export")
    if item.get('nestedSequence') is not None:
        raise ValueError('Nested Premiere sequences are not yet supported for export')
    if generator is not None:
        _number(item.get('duration'),'generator duration',0,MAX_SOURCE_SECONDS)
        item.update(_kinds={'video'}, _actualDuration=float(MAX_SOURCE_SECONDS), _audioStreams=[])
        return item
    path = _render_source(item)
    item['_path'] = path
    if path.suffix.lower() in RENDER_STILLS:
        item.update(_kinds={'video'}, _actualDuration=math.inf, _audioStreams=[], _renderStill=True)
        return item
    probe = probes.get(path)
    if probe is None:
        probe = probes[path] = _probe(path)
        if heartbeat: heartbeat()
    item.update(_kinds=probe['kinds'], _actualDuration=probe['duration'], _audioStreams=probe['audio'], _audioRates=probe['rates'],
                _sourceProfile=probe['profile'], _startTime=probe['start'], _videoTimeBase=probe['time_base'],
                _seekMargin=SEEK_MARGIN_STREAM_SECONDS if set(probe['format'].split(',')) & STREAM_FORMATS else SEEK_MARGIN_SECONDS)
    return item


def _check_clip(clip, media_by_id, probes, heartbeat=None):
    """Validate one timeline item against its media. Returns the media item (None for titles)."""
    if clip.get('type') not in ('video', 'audio', 'title'):
        raise ValueError('Unsupported timeline item')
    if clip.get('nativeTimeRemap'):
        raise ValueError('Keyframed Premiere time remapping is not supported for export')
    if clip.get('transition') == 'native':
        raise ValueError('This Premiere transition is not supported for export')
    before = _number(clip.get('transitionBefore',0),'transition lead')
    if before and clip.get('transition') not in ('dissolve','wipe','fade'):
        raise ValueError('Unsupported centered Premiere transition')
    if before > float(clip.get('start',0))+.001:
        raise ValueError('Transition begins before the sequence')
    _number(clip.get('in', 0), 'source in')
    if clip['type'] == 'title':
        return None
    item = media_by_id.get(clip.get('mediaId'))
    if not item:
        raise ValueError('A clip references missing media')
    if clip.get('nestedSequence') is not None:
        raise ValueError('Nested Premiere sequences are not yet supported for export')
    _media_info(item, probes, heartbeat)
    if item.get('nativeGenerator') is not None:
        if clip['type'] != 'video': raise ValueError('Premiere generator has no audio stream')
        return item
    speed = _number(clip.get('speed',1),'speed',.1,16)
    if before and not item.get('_renderStill') and not clip.get('reverse') and float(clip.get('in',0))-before*speed < -.01:
        raise ValueError('Incoming clip lacks pre-cut footage for its transition')
    required = float(clip.get('in',0))+(float(clip.get('duration',0))+(before if clip.get('reverse') else 0))*speed
    if required > item['_actualDuration'] + .05:
        raise ValueError(f"{item.get('name', 'Media')} ends before this clip's out point")
    if clip['type'] not in item['_kinds']:
        raise ValueError(f"{item.get('name', 'Media')} lacks a {clip['type']} stream")
    return item


def _input_time_shift(item, seek_us):
    """Source time (from the container start) of timestamp zero in a seeked input.

    FFmpeg subtracts the seek point plus the container start time, converted to
    the stream time base with round-to-nearest, from every packet. With a
    coarse time base (1/600, 1/1000) that rounding moves frames by up to half a
    tick, so the picture clock uses the shift FFmpeg actually applies.
    """
    start = item.get('_startTime') or Fraction(0)
    time_base = item.get('_videoTimeBase')
    total = Fraction(seek_us, 1_000_000) + Fraction(round(start*1_000_000), 1_000_000)
    if time_base:
        ticks = total/time_base
        # av_rescale_q rounds to nearest, halves away from zero.
        whole = math.floor(ticks + Fraction(1, 2)) if ticks >= 0 else -math.floor(-ticks + Fraction(1, 2))
        total = whole*time_base
    return float(total - start)


def _frame_span(render_start, render_length, fps):
    first = math.ceil(render_start*fps-1e-6)
    stop = math.ceil((render_start+render_length)*fps-1e-6)
    return first, max(1, stop-first)


def _sample_origin(render_start, render_length, first, frames, fps, reverse):
    """Clip-clock time of the first sampled frame (see _picture_input)."""
    if reverse:
        # Reversed playback at clip time u shows source time length-u; sample
        # that forward from the last output frame, then reverse the order.
        return render_length-((first+frames-1)/fps-render_start)
    return first/fps-render_start


def _alpha_bytes_per_pixel(spec):
    if spec['chroma'] == '444':
        return 8 if spec['high_depth'] else 4
    return 6 if spec['high_depth'] else 3 if spec['chroma'] == '422' else 2.5


def _composite_layer(filters, base, layer, output, position, overlay, first, frames, window_frames, base_format):
    if overlay:
        filters.append(f'[{base}][{layer}]overlay={position}:{overlay}[{output}]')
        return
    # FFmpeg's software overlay tops out at 10 bits. maskedmerge blends at 16 bits,
    # with the positioned layer's alpha as the mask on every component.
    alpha = base_format.startswith('yuva')
    timing = f'setpts=PTS-STARTPTS,tpad=start={max(0,first)}:stop={max(0,window_frames-first-frames)}:color=black@0'
    if first < 0:
        timing = f'trim=start_frame={-first},' + timing
    filters.append(f'[{layer}]format=yuva444p16le,{position},{timing},split[{output}_fg][{output}_a]')
    if alpha:
        # For straight alpha, weight foreground colour by As/Aout, where Aout = As + Ab*(1-As).
        # Use As itself on the alpha plane to blend Ab towards opaque.
        filters.append(f'[{base}]split[{output}_bg][{output}_ba];[{output}_ba]extractplanes=a[{output}_ab]')
        filters.append(f'[{output}_a]extractplanes=a,split[{output}_as][{output}_opacity]')
        filters.append(f"[{output}_ab][{output}_as]blend=all_expr='if(B,round(65535*B/(B+A*(1-B/65535))),0)'[{output}_weight]")
        filters.append(f'[{output}_weight][{output}_opacity]mergeplanes=0x00000010:format={base_format}[{output}_mask]')
        filters.append(f'[{output}_fg]lutyuv=y=val:u=val:v=val:a=maxval[{output}_color]')
        base = f'{output}_bg'
    else:
        filters.append(f'[{output}_a]extractplanes=a,mergeplanes=0:format={base_format}[{output}_mask]')
        filters.append(f'[{output}_fg]format={base_format}[{output}_color]')
    filters.append(f'[{base}][{output}_color][{output}_mask]maskedmerge[{output}]')


def _render_window_inner(project, output_path, directory, *, options=None, cancelled=None):
    if not isinstance(project, dict):
        raise ValueError('Invalid project')
    options = options or {}
    width, height = _frame_size(project)
    fps = _sequence_fps(project)
    if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
    clips = project.get('clips')
    media = project.get('media')
    if not isinstance(clips, list) or not isinstance(media, list):
        raise ValueError('Invalid project timeline')
    if any(not isinstance(c, dict) for c in clips) or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) for m in media):
        raise ValueError('Invalid project entries')
    media_by_id = {item.get('id'): item for item in media}
    if len(media_by_id) != len(media):
        raise ValueError('Invalid project media')
    duration = max((_number(c.get('start'), 'start') + _number(c.get('duration'), 'duration', .001) for c in clips), default=0)
    if duration <= 0 or duration > MAX_TIMELINE_SECONDS:
        raise ValueError(f'Timeline duration must be more than 0 and at most {_duration_text(MAX_TIMELINE_SECONDS)}')
    render_options = project.get('renderOptions') or {}
    spec = options.get('_spec') or _delivery_spec(options)
    if spec['kind'] == 'audio': raise ValueError('Unsupported export codec')
    high_depth = spec['high_depth']
    rec709 = options.get('colorSpace')=='rec709'
    conform_pq = options.get('colorSpace')=='pq'
    # Composite in 4:4:4 for 4:4:4 and RGB deliveries, with an alpha base when alpha is delivered.
    # High-depth paths work in at least 4:2:2 even when the final encoder uses 4:2:0.
    chroma = spec['chroma']
    if high_depth and chroma == '420':
        chroma = '422'
    wide = spec['depth'] > 10
    depth = 16 if wide else 10 if high_depth else 8
    depth_suffix = f'p{depth}le' if high_depth else 'p'
    alpha_format = f'yuva{chroma}{depth_suffix}'
    base_format = alpha_format if spec['alpha'] else f'yuv{chroma}{depth_suffix}'
    full_alpha = 'yuva444' + depth_suffix
    dip_rgb = 'gbra' + depth_suffix
    overlay_format = f'yuv{chroma}p10' if high_depth else f'yuv{chroma}'
    overlay = None if wide else 'eof_action=pass:shortest=0:format=' + overlay_format
    window_frames = math.ceil(duration*fps-1e-6)
    if not isinstance(render_options, dict) or not isinstance(render_options.get('mutedTracks', {}), dict):
        raise ValueError('Invalid render options')
    muted = render_options.get('mutedTracks', {})
    probes = options.setdefault('_probes', {})
    for clip in clips:
        _check_clip(clip, media_by_id, probes)
    video_clips = [] if muted.get('video') else sorted((c for c in clips if c['type']=='video'),
                                                       key=lambda c: (c.get('track',0),c['start']))
    titles = [] if muted.get('title') else [c for c in clips if c['type']=='title' and c.get('title')]

    # Describe each picture clip once, then give every nearby run of source
    # reads its own seeked input so no input decodes far outside its window.
    predecessors, followers = _picture_transitions(video_clips,media_by_id,fps)
    plans = {}
    uses = []
    for number, clip in enumerate(video_clips):
        item = media_by_id[clip['mediaId']]
        if item.get('nativeGenerator')==1414680150: continue
        start = float(clip['start']); length = float(clip['duration']); source_in = float(clip.get('in', 0))
        speed = _number(clip.get('speed',1),'speed',.1,16)
        before = _number(clip.get('transitionBefore',0),'transition lead')
        transition_duration = min(float(clip.get('transitionDuration',.5)),length+before)
        incoming_dip = clip.get('transition') == 'fade'
        dip_head = transition_duration-before if incoming_dip else 0
        if incoming_dip:
            before = 0
        plan = dict(start=start, length=length, speed=speed, before=before, transition_duration=transition_duration,
                    dip_head=dip_head, render_start=start-before, render_length=length+before,
                    render_source=source_in if clip.get('reverse') else source_in-before*speed)
        plans[number] = plan
        if item.get('nativeGenerator') is None and not item.get('_renderStill'):
            uses.append((item['id'], plan['render_source'], plan['render_source']+plan['render_length']*speed, ('main', number)))
        previous = video_clips[predecessors[number]] if number in predecessors else None
        if previous is not None and clip.get('transition') in ('dissolve','wipe'):
            prior_item = media_by_id[previous['mediaId']]
            prior_speed = _number(previous.get('speed',1),'speed',.1,16)
            tail = max(0, transition_duration - before)
            plan['tail'] = tail
            prior_in = float(previous.get('in',0))
            prior_source = prior_in-tail*prior_speed if previous.get('reverse') else prior_in+float(previous['duration'])*prior_speed
            plan['prior_source'] = prior_source
            if prior_item.get('nativeGenerator') is None and not prior_item.get('_renderStill'):
                uses.append((prior_item['id'], prior_source, prior_source+tail*prior_speed, ('tail', number)))
    inputs = []
    input_of = {}
    by_media = {}
    for use in uses:
        by_media.setdefault(use[0], []).append(use)
    for identity, group in by_media.items():
        group.sort(key=lambda use: use[1])
        runs = []
        for use in group:
            if runs and use[1] <= runs[-1]['high'] + 10:
                runs[-1]['high'] = max(runs[-1]['high'], use[2]); runs[-1]['keys'].append(use[3])
            else:
                runs.append({'low': use[1], 'high': use[2], 'keys': [use[3]]})
        for run in runs:
            # Seek to the keyframe before the run without dropping earlier
            # frames: a frame that starts before the in point is still on screen.
            margin = media_by_id[identity].get('_seekMargin', SEEK_MARGIN_SECONDS)
            seek_us = math.floor(max(0, run['low']-margin)*1_000_000)
            index = len(inputs)
            seek = ['-noaccurate_seek', '-ss', f'{seek_us/1_000_000:.6f}'] if seek_us > 0 else []
            # No input -t: without accurate seek it counts from the keyframe,
            # not the seek point. Each chain's trim ends its reads instead.
            inputs.append([*seek, '-threads', '2', '-protocol_whitelist', 'file', '-i', str(media_by_id[identity]['_path'])])
            shift = _input_time_shift(media_by_id[identity], seek_us)
            for key in run['keys']:
                input_of[key] = (index, shift)
    stills = {}
    for number in plans:
        for identity in (video_clips[number]['mediaId'], video_clips[predecessors[number]]['mediaId'] if number in predecessors else None):
            if identity and media_by_id[identity].get('_renderStill') and identity not in stills:
                stills[identity] = len(inputs)
                inputs.append(['-protocol_whitelist','file','-f','image2','-loop','1','-framerate',str(fps),
                               '-i',str(media_by_id[identity]['_path'])])

    def source_input(identity, key, source):
        item = media_by_id[identity]
        if item.get('_renderStill'):
            return stills[identity], 0
        if item.get('nativeGenerator') is not None:
            return None, 0
        index, shift = input_of[key]
        return index, source-shift

    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-filter_complex_threads', '3', '-y',
               *(argument for entry in inputs for argument in entry),
               '-f', 'lavfi', '-i', f'color=c=black{"@0" if spec["alpha"] else ""}:s={width}x{height}:r={fps}:d={duration:.6f}'
               # The color source keeps its alpha only through an RGBA output.
               + (',format=rgba' if spec['alpha'] else '')]
    base_index = len(inputs)
    profile = options.get('_color_profile') or REC709
    hdr_profile = profile if high_depth and profile[1] in HDR_TRANSFERS else None
    picture_params = (f',setparams=color_primaries={profile[0]}:color_trc={profile[1]}:colorspace={profile[2]}:range=tv'
                      if hdr_profile else '')
    filters = [f'[{base_index}:v]format={base_format}{picture_params}[base0]']
    active = 'base0'
    picture_layers = [(clip.get('track',0),False,number,clip) for number,clip in enumerate(video_clips)]
    picture_layers.extend((clip.get('track',4),True,number,clip) for number,clip in enumerate(titles))
    picture_layers.sort(key=lambda row: (row[0],row[1],row[3]['start']))
    for _, is_title, number, clip in picture_layers:
        if is_title:
            active = _draw_title(filters,active,number,clip,height,fps,directory,hdr_profile)
            continue
        item = media_by_id[clip['mediaId']]
        if item.get('nativeGenerator')==1414680150: continue
        plan = plans[number]
        start, length, speed, before = plan['start'], plan['length'], plan['speed'], plan['before']
        transition_duration, dip_head = plan['transition_duration'], plan['dip_head']
        render_start, render_length = plan['render_start'], plan['render_length']
        reverse = bool(clip.get('reverse'))
        index, render_source = source_input(item['id'], ('main', number), plan['render_source'])
        color = clip.get('color') or {}
        transform,position=_transform(clip,width,height,before,fps,place_on_canvas=wide)
        lut_filter=_lut_filter(color,Path(directory)/f'clip-{number}.cube',preserve_headroom=bool(
            hdr_profile and (item.get('_sourceProfile') or REC709)[1] in HDR_TRANSFERS))
        opacity=_number(clip.get('opacity',100),'opacity',0,100)/100
        opacity_keys=(clip.get('keyframes') or {}).get('opacity')
        first_frame, frames = _frame_span(render_start, render_length, fps)
        still_frames = frames if item.get('_renderStill') else 0
        origin = _sample_origin(render_start, render_length, first_frame, frames, fps, reverse)
        disabled = bool(item.get('_disabled'))
        if disabled:
            # Nothing, for as long as the clip lasts: its neighbour's transition
            # mixes with the layers beneath.
            chain = (f'color=c=black@0:s={width}x{height}:r={fps}:d={frames/float(fps):.6f},format={alpha_format},'
                     f'setpts=PTS-STARTPTS,trim=end_frame={frames}')
            transform, position = '', 'null' if wide else 'x=0:y=0'
        else:
            prefix = _picture_input(index,render_source,render_length,speed,width,height,fps,reverse,still_frames,
                                    frames=frames,origin=origin)
            fit = _fit_picture(width,height,color,lut_filter,alpha_format,depth,item.get('_sourceProfile'),rec709,
                               still_frames=still_frames,hdr_profile=hdr_profile,conform_pq=conform_pq)
            # Reverse after scaling so the buffered window holds sequence-size frames.
            flip = ',reverse' if reverse and not still_frames and index is not None else ''
            chain = f'{prefix},{fit}{flip}{transform}'
        if disabled:
            pass
        elif opacity_keys:
            opacity_expr=_keyframe_expr(clip,'opacity',opacity*100,0,100,_keyframe_clock(clip,fps,'T',before))
            chain += _mask_alpha(f'({opacity_expr})/100',alpha_format,full_alpha)
        elif opacity!=1:
            chain += _mask_alpha(f'{opacity:.6f}',alpha_format,full_alpha)
        prior_number = predecessors.get(number)
        previous = video_clips[prior_number] if prior_number is not None else None
        next_number = followers.get(number)
        following = video_clips[next_number] if next_number is not None else None
        if following and (following.get('transition') == 'dissolve' or
                          following.get('transition') == 'wipe' and following.get('mediaId') == DISABLED_MEDIA):
            # Use the same lower layers under both sides of the dissolve (or of
            # a wipe to a disabled clip, which wipes this clip off).
            filters.append(f'[{active}]split[with{number}][without{next_number}]')
            active = f'with{number}'
        dipped = False
        if disabled:
            pass
        elif dip_head>0:
            chain += f',format={dip_rgb},fade=t=in:st=0:d={dip_head:.6f}'
            dipped = True
        if following and following.get('transition') == 'fade' and not disabled:
            dip_tail = float(following.get('transitionBefore',0) or 0)
            if dip_tail>0:
                chain += f',format={dip_rgb},fade=t=out:st={max(0,render_length-dip_tail):.6f}:d={dip_tail:.6f}'
                dipped = True
        if clip.get('_dipFormat') and not dipped and not disabled:
            # The rest of a clip that dips elsewhere: take the same RGB round
            # trip so its pixels match the window that holds the dip.
            chain += f',format={dip_rgb}'
        overlap_transition = previous is not None and clip.get('transition') in ('dissolve','wipe')
        if overlap_transition:
            prior_item = media_by_id[previous['mediaId']]
            prior_speed=_number(previous.get('speed',1),'speed',.1,16)
            prior_available=(float(previous.get('in',0))/prior_speed if previous.get('reverse') else
                             (prior_item['_actualDuration']-(float(previous.get('in',0))+float(previous['duration'])*prior_speed))/prior_speed)
            frame_duration = 1 / float(fps)
            tail = plan['tail']
            if tail >= frame_duration - 1e-6 and prior_available + 1e-6 < tail:
                raise ValueError(f'Transition at {start:.2f}s needs {tail:.2f}s of unused outgoing footage')
            prior_reverse = bool(previous.get('reverse'))
            prior_index, prior_source = source_input(previous['mediaId'], ('tail', number), plan['prior_source'])
            prior_tail={**previous,'keyframes':{},'transform':dict(previous.get('transform') or {})}
            for key in ('x','y','scale','rotation','opacity'):
                points=(previous.get('keyframes') or {}).get(key) or []
                if points:
                    terminal=sorted(points,key=lambda point:point['time'])[-1]['value']
                    if key=='opacity': prior_tail['opacity']=terminal
                    else: prior_tail['transform'][key]=terminal
            prior_transform,prior_position=_transform(prior_tail,width,height,fps=fps,place_on_canvas=wide)
            prior_color=previous.get('color') or {}
            prior_lut=_lut_filter(prior_color,Path(directory)/f'tail-{number}.cube',preserve_headroom=bool(
                hdr_profile and (prior_item.get('_sourceProfile') or REC709)[1] in HDR_TRANSFERS))
            prior_opacity=_number(prior_tail.get('opacity',100),'opacity',0,100)/100
            # A disabled outgoing clip has no tail: the incoming clip dissolves or wipes in over what lies beneath.
            if tail + 1e-6 >= frame_duration and not prior_item.get('_disabled'):
                cut_frame, tail_frames = _frame_span(start, tail, fps)
                still_tail = tail_frames if prior_item.get('_renderStill') else 0
                tail_origin = _sample_origin(start, tail, cut_frame, tail_frames, fps, prior_reverse)
                prior_prefix = _picture_input(prior_index,prior_source,tail,prior_speed,width,height,fps,prior_reverse,
                                              still_tail,frames=tail_frames,origin=tail_origin)
                prior_fit = _fit_picture(width,height,prior_color,prior_lut,alpha_format,depth,
                                         prior_item.get('_sourceProfile'),rec709,still_frames=still_tail,
                                         hdr_profile=hdr_profile,conform_pq=conform_pq)
                prior_flip = ',reverse' if prior_reverse and not still_tail and prior_index is not None else ''
                prior_alpha = _mask_alpha(f'{prior_opacity:.6f}',alpha_format,full_alpha) if prior_opacity!=1 else ''
                filters.append(f'{prior_prefix},{prior_fit}{prior_flip}{prior_transform}{prior_alpha},setpts=PTS+{cut_frame}[tail{number}]')
                _composite_layer(filters, active, f'tail{number}', f'under{number}', prior_position, overlay,
                                 cut_frame, tail_frames, window_frames, base_format)
                active = f'under{number}'
        elif clip.get('transition') == 'dissolve' and not disabled:
            chain += f',fade=t=in:st=0:d={transition_duration:.6f}:alpha=1'
        if disabled:
            pass
        elif clip.get('transition') == 'wipe':
            left = _horizontal_position(clip,width,'W',_keyframe_clock(clip,fps,'T',before))
            # The frame at the wipe's end is fully revealed: T and the printed
            # duration can differ by rounding, so compare with 1 us of slack.
            mask = f'if(gte(T,{transition_duration-1e-6:.9f}),1,lt(X+({left}),{width}*T/{transition_duration:.9f}))'
            chain += _mask_alpha(mask,alpha_format,full_alpha)
        elif clip.get('_wipeMask'):
            # Same format round trip as the window that holds the wipe.
            chain += _mask_alpha('1',alpha_format,full_alpha)
        chain += f',setpts=PTS+{first_frame}[clip{number}]'
        filters.append(chain)
        wipe_off = overlap_transition and disabled and clip.get('transition') == 'wipe'
        if overlap_transition and clip.get('transition') == 'dissolve' or wipe_off:
            _composite_layer(filters, f'without{number}', f'clip{number}', f'incoming{number}', position, overlay,
                             first_frame, frames, window_frames, base_format)
            end = render_start+transition_duration
            progress = f'(T-{render_start:.9f})/{transition_duration:.9f}'
            # A wipe to a disabled clip uncovers the layers beneath from the left.
            mix = f'if(lt(X,W*{progress}),B,A)' if wipe_off else f'A*(1-{progress})+B*{progress}'
            # (The wipe's last frame is fully uncovered: compare with 1 us of slack, as the wipe mask does.)
            finish = end-1e-6 if wipe_off else end
            blend = f'if(lte(T,{render_start:.9f}),A,if(gte(T,{finish:.9f}),B,{mix}))'
            filters.append(f"[{active}][incoming{number}]blend=all_expr='{blend}'[base{number+1}]")
        else:
            _composite_layer(filters, active, f'clip{number}', f'base{number+1}', position, overlay,
                             first_frame, frames, window_frames, base_format)
        active = f'base{number+1}'
    window = options.get('_output_frames')
    if window is not None:
        first, stop = window
        filters.append(f'[{active}]trim=start_frame={first}:end_frame={stop},setpts=PTS-STARTPTS[vwindow]')
        active = 'vwindow'
        duration = (stop-first)/fps
    tail = _delivery_tail(spec, width, height, fps, profile, options.get('_burn'), directory)
    if tail:
        filters.append(f'[{active}]{tail}[delivered]')
        active = 'delivered'
    command.extend([*_graph_args(';'.join(filters), directory, 'picture'), '-map', f'[{active}]', '-an',
                    '-t', f'{float(duration):.6f}', '-r', str(fps)])
    if spec['mode'] == 'stream':
        # Raw frames in the delivery's pixel format for the one encoder (see _stream_picture).
        command.extend(['-threads:v', '1', '-f', 'rawvideo', 'pipe:1'])
        return command
    output = Path(output_path)
    command.extend([*delivery.encoder_args(spec, fps, profile, threads=4), *_color_tags(profile, spec)])
    if spec['container'] != 'mxf':
        command.extend(['-movflags', '+write_colr'])
    command.append(str(output))
    _run_ffmpeg(command,cancelled,options.get('_heartbeat'))
    return output.stat().st_size


def _color_tags(profile, spec):
    """Explicit stream color signaling: primaries, transfer, matrix and range (legal for video, full for RGB)."""
    codec = spec if isinstance(spec, str) else spec['codec']
    rgb = not isinstance(spec, str) and spec['kind'] == 'images'
    tags = ['-color_primaries',profile[0],'-color_trc',profile[1],'-colorspace','rgb' if rgb else profile[2],
            '-color_range','pc' if rgb else 'tv']
    if codec == 'prores':
        # ProRes carries its color description in each frame header as well.
        tags += ['-bsf:v',f'prores_metadata=color_primaries={profile[0]}:color_trc={profile[1]}:colorspace={profile[2]}']
    elif codec == 'h264' and profile == REC709:
        tags += ['-bsf:v',AVC_REC709]
    return tags


def _audio_events(clips, media_by_id, fps, probes, track_mix, solo, span, heartbeat=None):
    """Describe each audible clip, and each crossfade tail, as a timeline event.

    Only events that overlap ``span`` (timeline seconds) are returned, so a
    range export probes and reads only the media it needs.
    """
    begin, end = span
    upcoming = {}
    for candidate in clips:
        if candidate.get('type') in ('audio','video') and candidate.get('transition') in ('dissolve','wipe'):
            upcoming.setdefault((candidate['type'], candidate.get('track'), round(float(candidate['start'])*fps)), candidate)
    events = []
    for clip in clips:
        if clip.get('type') not in ('audio','video') or not clip_enabled(clip):
            # A disabled clip is silent (its embedded sound too). It stays in
            # `upcoming`, so a neighbour's crossfade still fades against it.
            continue
        item = media_by_id.get(clip.get('mediaId'))
        if item is None:
            raise ValueError('A clip references missing media')
        if item.get('nativeGenerator') in (1112293707,1414680150): continue
        mix = track_mix.get(str(clip.get('track', -1))) or {}
        if not isinstance(mix, dict): raise ValueError('Invalid track mix')
        if mix.get('mute') or (solo and not mix.get('solo')): continue
        if clip['type'] == 'video' and clip.get('embeddedAudio') is False: continue
        source_in = float(clip.get('in', 0)); length = float(clip['duration']); start = float(clip['start'])
        speed = _number(clip.get('speed',1),'speed',.1,16)
        before = _number(clip.get('transitionBefore',0),'transition lead')
        reverse = bool(clip.get('reverse'))
        render_length = length+before
        next_clip = upcoming.get((clip['type'], clip.get('track'), round((start+length)*fps)))
        if next_clip is clip: next_clip = None
        fade_out = min(length,_number(clip.get('fadeOut',0),'fade out',0,MAX_TIMELINE_SECONDS))
        after = 0
        if next_clip:
            next_before=_number(next_clip.get('transitionBefore',0),'transition lead')
            next_duration=_number(next_clip.get('transitionDuration',.5),'transition duration',1/float(fps))
            next_duration=min(next_duration,next_before+float(next_clip['duration']))
            if not fade_out:
                after = max(0,next_duration-next_before)
        if start-before >= end or start+length+(after if after>=1/float(fps) else 0) <= begin:
            continue
        _check_clip(clip, media_by_id, probes, heartbeat)
        if 'audio' not in item['_kinds']:
            continue
        volume_keys=(clip.get('keyframes') or {}).get('volume')
        gain = (100 if volume_keys else _number(clip.get('volume',100),'volume',0,200)) * _number(mix.get('gain',100),'track gain',0,200) / 10000
        pan = max(-1,min(1,(_number(clip.get('pan', 0), 'pan', -100, 100) + _number(mix.get('pan',0),'track pan',-100,100))/100))
        streams = dict(enumerate(item['_audioStreams']))
        selection = clip.get('audioStream',item.get('audioStream'))
        if selection is not None:
            if type(selection) is not int or selection not in streams:
                raise ValueError('The selected source audio stream is unavailable')
            streams = {selection:streams[selection]}
        panner = _stereo_pan(pan,2 if len(streams)>1 else next(iter(streams.values())))
        fade_in = min(length,_number(clip.get('fadeIn',0),'fade in',0,MAX_TIMELINE_SECONDS))
        processing=','.join([*tempo_filters(speed),f'volume={gain:.4f}',panner])
        envelope=[]
        if volume_keys:
            envelope.append(lambda t, clip=clip, before=before: '({})/100'.format(
                _keyframe_expr(clip,'volume',float(clip.get('volume',100)),0,200,f'({t}-{before:.6f})')))
        if clip.get('transition') in ('dissolve','wipe','fade'):
            incoming=min(_number(clip.get('transitionDuration',.5),'transition duration',1/float(fps)),render_length)
            envelope.append(lambda t, clip=clip, incoming=incoming: _audio_transition_gain(clip,f'min(1,{t}/{incoming:.9f})'))
        if next_clip:
            fade_start=before+length-next_before
            envelope.append(lambda t, n=next_clip, s=fade_start, d=next_duration:
                            _audio_transition_gain(n,f'max(0,min(1,1-({t}-{s:.9f})/{d:.9f}))'))
        if fade_in or fade_out:
            head = (lambda t, b=before, f=fade_in: f'max(0,({t}-{b:.9f})/{f:.9f})') if fade_in else (lambda t: '1')
            tail = (lambda t, r=render_length, f=fade_out: f'max(0,({r:.9f}-{t})/{f:.9f})') if fade_out else (lambda t: '1')
            envelope.append(lambda t, head=head, tail=tail: f'min(1,min({head(t)},{tail(t)}))')
        rates = item.get('_audioRates') or []
        # Tempo and resampling carry state from sample to sample: such an event
        # is rendered in one piece when a chunk seam falls inside it.
        stateful = abs(speed-1) > 1e-9 or any((rates[number] if number < len(rates) else 0) != AUDIO_RATE for number in streams)
        events.append(dict(path=item['_path'], streams=streams, source=source_in if reverse else source_in-before*speed,
                           length=render_length, speed=speed, reverse=reverse, start=start-before,
                           processing=processing, envelope=envelope, stateful=stateful))
        if after>=1/float(fps):
            available=(source_in/speed if reverse else (item['_actualDuration']-source_in-length*speed)/speed)
            if available+1e-3<after: raise ValueError('Audio crossfade needs more outgoing source footage')
            tail_processing=processing
            if volume_keys:
                terminal=sorted(volume_keys,key=lambda point:point['time'])[-1]['value']
                tail_processing+=f",volume={_number(terminal,'volume keyframe',0,200)/100:.6f}"
            events.append(dict(path=item['_path'], streams=streams, stateful=stateful,
                               source=source_in-after*speed if reverse else source_in+length*speed,
                               length=after, speed=speed, reverse=reverse, start=start+length, processing=tail_processing,
                               envelope=[lambda t, n=next_clip, b=next_before, d=next_duration:
                                         _audio_transition_gain(n,f'max(0,1-({b:.9f}+{t})/{d:.9f})')]))
    events.sort(key=lambda event: event['start'])
    return events


def _limiter_filter(limiter):
    """``limiter`` is None or (ceiling dBFS, true_peak).

    Sample-peak limiting leaves material below the ceiling bit-exact (PCM
    deliveries). True-peak limiting runs the limiter at 4x the sample rate so
    peaks between samples are held too (lossy deliveries).
    """
    if limiter is None:
        return ''
    ceiling, true_peak = limiter
    limit = f'alimiter=limit={10**(ceiling/20):.9f}:level=0:latency=1'
    if true_peak:
        return f',aresample={TRUE_PEAK_OVERSAMPLE*AUDIO_RATE},{limit},aresample={AUDIO_RATE}'
    return ','+limit


def _render_audio_chunk(events, origin_sample, keep_from, keep_samples, limiter, target, directory, cancelled, heartbeat):
    """Mix the events that touch one chunk into a float WAV of exactly keep_samples samples."""
    origin = origin_sample/AUDIO_RATE
    chunk_end = (origin_sample+keep_from+keep_samples)/AUDIO_RATE+AUDIO_POSTROLL_SECONDS
    slices = []
    for event in events:
        u0 = max(0.0, origin-event['start'])
        u1 = min(event['length'], chunk_end-event['start'])
        if u1-u0 <= 1/AUDIO_RATE:
            continue
        low, high = _event_source_span(event, u0, u1)
        slices.append((event, u0, u1, low, high))
    # Slices of one file that sit close together in the source share a seeked
    # input; the shared span is bounded so buffered audio stays small.
    inputs, offsets = [], {}
    runs = {}
    for number, (event, _, _, low, high) in sorted(enumerate(slices), key=lambda row: (str(row[1][0]['path']), row[1][3])):
        run = runs.get(event['path'])
        if run is None or low > run['high']+5 or max(high, run['high'])-run['low'] > AUDIO_SHARED_INPUT_SECONDS:
            run = runs[event['path']] = {'low': low, 'high': high, 'numbers': []}
            run['index'] = len(inputs)
            inputs.append(run)
        run['high'] = max(run['high'], high)
        run['numbers'].append(number)
    arguments = []
    for run in inputs:
        offset = math.floor(max(0.0, run['low']-.25)*1_000_000)/1_000_000
        for number in run['numbers']:
            offsets[number] = (run['index'], offset)
        seek = ['-ss', f'{offset:.6f}'] if offset > 0 else []
        path = slices[run['numbers'][0]][0]['path']
        arguments.append([*seek, '-t', f"{run['high']-offset+.25:.6f}", '-protocol_whitelist', 'file', '-i', str(path)])
    filters, labels = [], []
    # Every value below comes from this slice's own tuple: nothing is carried
    # over from the loop above (a stale end silenced speed-changed clips).
    for number, (event, u0, u1, low, high) in enumerate(slices):
        index, offset = offsets[number]
        chain = _trim_audio(filters, f'mix{number}', index, event['streams'], low-offset, high-low, event['reverse'])
        chain += event['processing']
        delay = round((event['start']+u0-origin)*AUDIO_RATE)
        if abs(event['speed']-1) > 1e-9:
            # The tempo filter was given extra source past the slice (it drops
            # its last fragment); keep exactly the slice's own samples.
            chain += f",apad,atrim=end_sample={max(1, round((event['start']+u1-origin)*AUDIO_RATE)-max(0, delay))}"
        clock = f'(t+{u0:.9f})' if u0 else 't'
        envelope = [term(clock) for term in event['envelope']]
        if envelope:
            chain += f",aeval=exprs='val(ch)*{'*'.join(envelope)}':channel_layout=stereo"
        filters.append(chain + f',adelay={max(0, delay)}S:all=1[audio{number}]')
        labels.append(f'[audio{number}]')
    total = keep_from+keep_samples
    if labels:
        mix = f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0"
    else:
        mix = f'anullsrc=r={AUDIO_RATE}:cl=stereo'
    filters.append(f'{mix},aformat=sample_fmts=flt:sample_rates={AUDIO_RATE}:channel_layouts=stereo{_limiter_filter(limiter)},'
                   f'apad,atrim=start_sample={keep_from}:end_sample={total},asetpts=PTS-STARTPTS[aout]')
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               *(argument for entry in arguments for argument in entry),
               *_graph_args(';'.join(filters), directory, Path(target).stem), '-map', '[aout]',
               '-c:a', 'pcm_f32le', '-ar', str(AUDIO_RATE), '-ac', '2', str(target)]
    _run_ffmpeg(command, cancelled, heartbeat)


def _event_source_span(event, u0, u1):
    """Source seconds read for event-local times u0..u1 (with tempo margin)."""
    speed = event['speed']
    if event['reverse']:
        low, high = event['source']+(event['length']-u1)*speed, event['source']+(event['length']-u0)*speed
    else:
        low, high = event['source']+u0*speed, event['source']+u1*speed
    if abs(speed-1) > 1e-9 and u1 >= event['length']-1e-9:
        # FFmpeg's atempo drops the last part of its input; feed it more of
        # the source than it must play (trimmed afterwards).
        if event['reverse']: low = max(0.0, low-TEMPO_TAIL_SECONDS*speed)
        else: high += TEMPO_TAIL_SECONDS*speed
    return low, high


def _prerender_event(event, u0, u1, target, directory, cancelled, heartbeat):
    """Render event-local u0..u1 of a stateful event (tempo, resampling) in one
    FFmpeg run, reading only that span of its source. Returns a plain 48 kHz
    stereo event that chunks can slice without restarting any filter state."""
    low, high = _event_source_span(event, u0, u1)
    offset = math.floor(max(0.0, low-.25)*1_000_000)/1_000_000
    seek = ['-ss', f'{offset:.6f}'] if offset > 0 else []
    filters = []
    chain = _trim_audio(filters, 'pre', 0, event['streams'], low-offset, high-low, event['reverse'])
    samples = math.ceil((u1-u0)*AUDIO_RATE)+2
    filters.append(chain + event['processing'] +
                   f',aformat=sample_fmts=flt:sample_rates={AUDIO_RATE}:channel_layouts=stereo,apad,atrim=end_sample={samples},'
                   'asetpts=PTS-STARTPTS[aout]')
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               *seek, '-t', f'{high-offset+.25:.6f}', '-protocol_whitelist', 'file', '-i', str(event['path']),
               *_graph_args(';'.join(filters), directory, Path(target).stem), '-map', '[aout]',
               '-c:a', 'pcm_f32le', '-ar', str(AUDIO_RATE), '-ac', '2', str(target)]
    _run_ffmpeg(command, cancelled, heartbeat)
    shift = u0
    envelope = [(lambda t, term=term, shift=shift: term(f'({t}+{shift:.9f})')) for term in event['envelope']]
    return dict(path=Path(target), streams={0: 2}, source=0.0, length=u1-u0, speed=1.0, reverse=False,
                start=event['start']+u0, processing='anull', envelope=envelope, stateful=False, prerendered=True)


def _audio_chunk_spans(events, first_sample, stop_sample):
    """The (begin, end) sample spans the mix is rendered in: a minute at most,
    shorter where more than AUDIO_CHUNK_EVENTS events start in one."""
    import bisect
    starts = [event['start'] for event in events]
    preroll = round(AUDIO_PREROLL_SECONDS*AUDIO_RATE)
    chunks = []
    current = first_sample
    while current < stop_sample:
        end = min(current+AUDIO_CHUNK_SECONDS*AUDIO_RATE, stop_sample)
        low = bisect.bisect_right(starts, (current-preroll)/AUDIO_RATE)
        high = bisect.bisect_left(starts, end/AUDIO_RATE)
        if high-low > AUDIO_CHUNK_EVENTS:
            limit = math.ceil(starts[low+AUDIO_CHUNK_EVENTS]*AUDIO_RATE)
            end = max(current+AUDIO_RATE, min(end, limit))
        chunks.append((current, min(end, stop_sample)))
        current = chunks[-1][1]
    return chunks


def _audio_schedule(events, first_sample, stop_sample):
    """Walk the chunks in render order. Yields (number, begin, end, origin,
    arrivals, chunk count): arrivals are (event, prerender span or None) for the
    events that join at this chunk. A stateful event (tempo, resampling) that
    crosses a seam is pre-rendered from u0 to u1 when it arrives."""
    chunks = _audio_chunk_spans(events, first_sample, stop_sample)
    preroll = round(AUDIO_PREROLL_SECONDS*AUDIO_RATE)
    origins = [max(0, begin-preroll) for begin, _ in chunks]
    last_needed = stop_sample/AUDIO_RATE+AUDIO_POSTROLL_SECONDS
    pointer = 0
    for number, (begin, end) in enumerate(chunks):
        origin = origins[number]
        arrivals = []
        while pointer < len(events) and events[pointer]['start'] < end/AUDIO_RATE+AUDIO_POSTROLL_SECONDS:
            event = events[pointer]; pointer += 1
            span = None
            u0 = max(0.0, origin/AUDIO_RATE-event['start'])
            spans_seam = number+1 < len(origins) and event['start']+event['length'] > origins[number+1]/AUDIO_RATE
            reversed_span = event['length']*event['speed'] if event['reverse'] else 0
            if event.get('stateful') and (u0 > 0 or spans_seam) and reversed_span <= AUDIO_REVERSE_PRERENDER_SECONDS:
                u1 = min(event['length'], last_needed-event['start'])
                if u1-u0 > 1/AUDIO_RATE:
                    span = (u0, u1)
            arrivals.append((event, span))
        yield number, begin, end, origin, arrivals, len(chunks)


def _prerender_bytes(u0, u1):
    """Size of a pre-rendered event file (32-bit float stereo WAV)."""
    return (math.ceil((u1-u0)*AUDIO_RATE)+2)*2*4 + 4096


def _prerender_peak_bytes(events, first_sample, stop_sample):
    """Most pre-rendered event bytes on disk at once, following _render_audio's
    order exactly: files are made as their events arrive and deleted once a
    chunk starts after their end."""
    alive, peak = [], 0
    for _, _, _, origin, arrivals, _ in _audio_schedule(events, first_sample, stop_sample):
        for event, span in arrivals:
            if span:
                alive.append((event['start']+span[1], _prerender_bytes(*span)))
        peak = max(peak, sum(size for _, size in alive))
        alive = [(finish, size) for finish, size in alive if finish > origin/AUDIO_RATE]
    return peak


def _render_audio(events, first_sample, stop_sample, limiter, directory, cancelled, heartbeat, progress=None):
    """Render the mix as consecutive chunk files; returns their paths in order."""
    paths, active = [], []
    prerendered = 0
    for number, begin, end, origin, arrivals, count in _audio_schedule(events, first_sample, stop_sample):
        if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
        for event, span in arrivals:
            if span:
                target = Path(directory)/f'event-{prerendered:06d}.wav'; prerendered += 1
                event = _prerender_event(event, *span, target, directory, cancelled, heartbeat)
            active.append(event)
        for event in active:
            if event.get('prerendered') and event['start']+event['length'] <= origin/AUDIO_RATE:
                Path(event['path']).unlink(missing_ok=True)
        active = [event for event in active if event['start']+event['length'] > origin/AUDIO_RATE]
        target = Path(directory)/f'audio-{number:05d}.wav'
        _render_audio_chunk([event for event in active if event['start'] < end/AUDIO_RATE+AUDIO_POSTROLL_SECONDS], origin, begin-origin, end-begin,
                            limiter, target, directory, cancelled, heartbeat)
        paths.append(target)
        if progress: progress((number+1)/count)
    for event in active:
        if event.get('prerendered'):
            Path(event['path']).unlink(missing_ok=True)
    return paths


def _concat_list(directory, name, paths):
    manifest = Path(directory)/name
    manifest.write_text(''.join(f"file '{path.name}'\n" for path in paths), encoding='utf-8')
    return manifest


def render_to_file(project, output_path, *, options=None, progress=None, cancelled=None, heartbeat=None,
                   work_directory=None, report=None, _nested_depth=0, _lossless_intermediate=False):
    """Resolve imported nested Premiere sequences, then render the parent timeline.

    ``heartbeat`` is called whenever FFmpeg makes progress, so a supervisor
    can tell a long render from a stuck one. ``work_directory`` is where the
    render's scratch folder is made (default: work_root()); an export job
    passes its own folder so killing the job leaves nothing elsewhere.
    ``report``, when a dict, receives measurements of the delivered file
    (``report['audio']``: decoded AAC true peak, loudness, any gain applied).
    """
    import copy
    if not isinstance(project,dict) or not isinstance(project.get('media'),list): raise ValueError('Invalid project')
    if options is not None and not isinstance(options, dict): raise ValueError('Invalid export options')
    # Keys with a leading underscore are internal to the renderer.
    options = {key: value for key, value in (options or {}).items() if isinstance(key, str) and not key.startswith('_')}
    if _lossless_intermediate:
        # Use the child's MOV/PCM settings but encode losslessly. ProRes accepts only 10-bit picture.
        spec = _delivery_spec(options)
        options['_spec'] = dict(spec, codec='ffv1', encoder='ffv1', depth=16,
                                pix_fmt='yuva444p16le' if spec['alpha'] else 'yuv444p16le')
    if _nested_depth>4: raise ValueError('Nested Premiere sequences exceed four levels')
    # Nested material used only by disabled clips is not rendered (it would contribute nothing).
    enabled_media = {clip.get('mediaId') for clip in project.get('clips') or [] if isinstance(clip, dict) and clip_enabled(clip)}
    if not any(isinstance(item,dict) and item.get('nestedProject') for item in project['media']):
        return _render_prepared(project,output_path,options=options,progress=progress,cancelled=cancelled,heartbeat=heartbeat,
                                work_directory=work_directory,report=report)
    MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='splice-nested-',dir=MEDIA_ROOT) as directory:
        prepared=copy.deepcopy(project)
        for index,item in enumerate(prepared['media']):
            child=item.get('nestedProject')
            if not child or item.get('id') not in enabled_media: continue
            if _nested_depth>=4: raise ValueError('Nested Premiere sequences exceed four levels')
            # Chroma detail lost in an intermediate cannot be recovered by the parent compositor.
            parent_spec = options.get('_spec') or _delivery_spec(options)
            child_codec = 'prores' if parent_spec['high_depth'] or parent_spec['chroma'] != '420' else 'h264'
            suffix='.mov' if child_codec=='prores' else '.mp4'
            target=Path(directory)/f'nested-{index}{suffix}'
            child_options={key:options[key] for key in ('limiter','limiterCeiling','aacPeakSafe','audioBitrate','subtitleLanguage',
                                                         'captions') if key in options}
            child_options['codec']=child_codec
            if parent_spec['chroma'] == '444':
                child_options.update(profile='4444', alpha=parent_spec['alpha'])
            if child_codec=='prores' and options.get('colorSpace') in ('rec709','pq'):
                child_options['colorSpace']=options['colorSpace']
            if child_codec=='h264' and options.get('codec','h264')=='h264' and 'crf' in options and not options.get('rateControl'):
                child_options['crf']=options['crf']
            render_to_file(child,target,options=child_options,cancelled=cancelled,heartbeat=heartbeat,work_directory=work_directory,
                           _nested_depth=_nested_depth+1, _lossless_intermediate=parent_spec['depth'] > 10)
            item['src']='/media/'+str(target.relative_to(MEDIA_ROOT))
            item['duration']=max((float(c['start'])+float(c['duration']) for c in child['clips']),default=0)
            item.pop('nestedSequence',None)
            item.pop('nestedProject',None)
            for clip in prepared['clips']:
                if clip.get('mediaId')==item['id']: clip.pop('nestedSequence',None)
        return _render_prepared(prepared,output_path,options=options,progress=progress,cancelled=cancelled,heartbeat=heartbeat,
                                work_directory=work_directory,report=report)


def _track_hidden(track_state, kind, track):
    # Mirror the editor: a title track without its own state follows the video track.
    state = track_state.get(f'{kind}:{track}')
    if state is None and kind == 'title':
        state = track_state.get(f'video:{track}')
    return bool(state and state.get('hidden') is True)


def _delivery_spec(options):
    """The delivery spec (delivery.normalize) for renderer options; underscore keys are internal."""
    return delivery.normalize({key: value for key, value in options.items() if not key.startswith('_')})


def _export_settings(options, *, spec=None):
    unknown = [key for key in options if not key.startswith('_') and key not in delivery.KNOWN_OPTIONS]
    if unknown: raise ValueError(f'Unknown export option {sorted(unknown)[0]!r}')
    spec = spec or _delivery_spec(options)
    color_space = spec['color_space']
    enabled = options.get('limiter', True)
    if not isinstance(enabled, bool): raise ValueError('Invalid limiter setting: use true or false')
    ceiling = _number(options.get('limiterCeiling', DEFAULT_LIMITER_CEILING_DB), 'limiter ceiling (dBFS)', -24, 0)
    peak_safe = options.get('aacPeakSafe', False)
    if not isinstance(peak_safe, bool): raise ValueError('Invalid aacPeakSafe setting: use true or false')
    if 'captions' in options and not isinstance(options['captions'], bool): raise ValueError('Invalid captions setting: use true or false')
    language = options.get('subtitleLanguage', 'und')
    if not isinstance(language, str) or len(language) != 3 or not language.isascii() or not language.isalpha() or not language.islower():
        raise ValueError('Invalid subtitle language: use a three-letter ISO 639-2 code such as eng, spa or und')
    return spec, color_space, (ceiling if enabled else None), language, peak_safe


def _validate_timeline(project):
    """Shape and size checks over the whole project; cheap, no media access."""
    clips, media = project.get('clips'), project.get('media')
    if not isinstance(clips, list) or not isinstance(media, list):
        raise ValueError('Invalid project timeline')
    if len(clips) > MAX_CLIPS:
        raise ValueError(f'The timeline has {len(clips):,} items; export supports at most {MAX_CLIPS:,}')
    if len(media) > MAX_MEDIA_POOL:
        raise ValueError(f'The project has {len(media):,} media items; export supports at most {MAX_MEDIA_POOL:,}')
    if any(not isinstance(c, dict) for c in clips) or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) for m in media):
        raise ValueError('Invalid project entries')
    if len({item['id'] for item in media}) != len(media):
        raise ValueError('Invalid project media')
    used = {clip.get('mediaId') for clip in clips if clip.get('type') != 'title'}
    if len(used) > MAX_MEDIA:
        raise ValueError(f'The timeline uses {len(used):,} media files; export supports at most {MAX_MEDIA:,}')
    total = 0.0
    for clip in clips:
        if clip.get('type') not in ('video', 'audio', 'title'):
            raise ValueError('Unsupported timeline item')
        start = _number(clip.get('start'), 'start', 0, MAX_TIMELINE_SECONDS)
        length = _number(clip.get('duration'), 'duration', .001, MAX_TIMELINE_SECONDS)
        total = max(total, start+length)
    if total <= 0:
        raise ValueError('The timeline is empty')
    if total > MAX_TIMELINE_SECONDS + 1e-6:
        raise ValueError(f'The timeline is {total-MAX_TIMELINE_SECONDS:.3f} seconds longer than the export limit of '
                         f'{_duration_text(MAX_TIMELINE_SECONDS)} ({total/3600:.4f} hours)')
    track_state = project.get('trackState') or {}
    if not isinstance(track_state, dict) or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in track_state.items()):
        raise ValueError('Invalid track state')
    if any('hidden' in value and not isinstance(value['hidden'], bool) for value in track_state.values()):
        raise ValueError('Invalid track visibility')
    return total


# Approximate bits per pixel of each delivery, for the free-space check.
BITS_PER_PIXEL = {'prores': {'proxy': .5, 'lt': 1.1, 'standard': 1.6, 'hq': 2.4, '4444': 3.6, '4444xq': 5.4},
                  'dnxhr': {'lb': .6, 'sq': 1.9, 'hq': 2.9, 'hqx': 2.9, '444': 5.8}}


def _estimated_bytes(width, height, fps, seconds, spec):
    if spec['kind'] == 'audio':
        bits = 0
    elif spec['kind'] == 'images':
        bits = {'png': 24 if spec['profile'] == '8' else 40, 'tiff': 48, 'dpx': 32, 'exr': 30}[spec['codec']]
    elif spec['codec'] == 'ffv1':
        bits = 64 if spec['alpha'] else 48
    elif spec['codec'] in BITS_PER_PIXEL:
        bits = BITS_PER_PIXEL[spec['codec']][spec['profile']] * 1.5
    else:
        rate = spec['rate']
        bits = rate['kbps']*1000/(width*height*fps)*1.2 if rate and rate['kbps'] else .3
    video = width*height*fps*seconds*bits/8
    return video, seconds*AUDIO_RATE*2*4


def _start_timecode(project, first, fps_fraction):
    """The delivered file's first-frame timecode: the sequence start (``startTimecode`` frames,
    ``dropFrame``) plus the export range's first frame."""
    start = project.get('startTimecode', 0)
    if start is None:
        start = 0
    if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start < 24*3600*math.ceil(fps_fraction):
        raise ValueError('Invalid sequence start timecode')
    drop = project.get('dropFrame')
    if drop is not None and not isinstance(drop, bool):
        raise ValueError('Invalid drop-frame setting')
    from interchange.timecode import DROP_FRAME_RATES
    drop = fps_fraction in DROP_FRAME_RATES and drop is not False
    return start + first, drop


def _render_prepared(project, output_path, *, options=None, progress=None, cancelled=None, heartbeat=None, work_directory=None,
                     report=None):
    """Render the picture (bounded windows), the audio (bounded chunks), then mux once."""
    import warnings
    options = dict(options or {})
    spec = _export_settings(options)[0]
    for attempt in range(2):
        try:
            return _render_delivery(project, output_path, options=dict(options), progress=progress, cancelled=cancelled,
                                    heartbeat=heartbeat, work_directory=work_directory, report=report)
        except HardwareEncoderFailed as error:
            if attempt:
                raise
            warnings.warn(f'The {delivery.HARDWARE[spec["hardware"]]["label"]} encoder failed during the export ({error}); '
                          'the export was restarted with the software encoder.', stacklevel=2)
            options = {**options, 'encoder': 'software'}
            spec = _export_settings(options)[0]
            if report is not None:
                report['hardwareFallback'] = True


class HardwareEncoderFailed(RuntimeError):
    """A hardware encoder stopped with an error: the export restarts in software."""


def _render_delivery(project, output_path, *, options, progress, cancelled, heartbeat, work_directory, report):
    import copy
    spec, color_space, limiter, language, peak_safe = _export_settings(options, spec=options.get('_spec'))
    codec = spec['codec']
    audio_only = spec['kind'] == 'audio'
    # One private copy: probe results are cached on its media items.
    project = copy.deepcopy(project)
    total = _validate_timeline(project)
    width, height = _frame_size(project) if not audio_only else (16, 16)
    fps_fraction = _sequence_fps(project)
    fps = float(fps_fraction)
    total = math.ceil((total-1e-9)*fps)/fps
    out_size = spec['output_size'] or dict(width=width, height=height)
    spec['_frame'] = dict(out_size)
    if not audio_only:
        problems = delivery.frame_problems(spec, width, height, fps_fraction)
        if problems:
            raise ValueError(' '.join(problems))
    track_state = project.get('trackState') or {}
    # A hidden track contributes no picture; a hidden video track still plays its audio.
    picture_clips = _disabled_stand_ins([clip for clip in project['clips'] if clip.get('type') in ('video','title') and not
                                         _track_hidden(track_state, clip['type'], clip.get('track', 0 if clip['type'] == 'video' else 4))],
                                        fps)
    if any(clip.get('mediaId') == DISABLED_MEDIA for clip in picture_clips):
        project['media'].append({'id': DISABLED_MEDIA, 'name': 'Disabled clip', 'nativeGenerator': 1112293707, '_disabled': True,
                                 '_kinds': {'video'}, '_actualDuration': float(MAX_SOURCE_SECONDS), '_audioStreams': []})
    options['_output_frames'] = None
    options['_heartbeat'] = heartbeat
    options['_spec'] = spec
    probes = options['_probes'] = {}
    total_frames = round(total*fps)
    first,stop = 0,total_frames
    requested = options.get('range')
    if requested is not None:
        if not isinstance(requested,dict):raise ValueError('Invalid export range')
        first = round(_number(requested.get('start'),'range start',0,total+.000001)*fps)
        stop = round(_number(requested.get('end'),'range end',0,total+.000001)*fps)
        if stop<=first or stop>total_frames:raise ValueError('The export range must contain frames inside the sequence')
    out_start,out_end = first/fps,stop/fps
    out_duration = (stop-first)/fps
    timecode_frames, drop_frame = _start_timecode(project, first, fps_fraction)
    media_by_id = {item['id']:item for item in project['media']}
    render_options = project.get('renderOptions') or {}
    if not isinstance(render_options, dict) or not isinstance(render_options.get('mutedTracks', {}), dict):
        raise ValueError('Invalid render options')
    muted = render_options.get('mutedTracks', {})
    profiles = set()
    video_clips = sorted((clip for clip in picture_clips if clip.get('type')=='video'),
                         key=lambda clip:(clip.get('track',0),float(clip['start'])))
    if not audio_only and not muted.get('video'):
        for identity in _picture_range_media(video_clips,media_by_id,fps,first,stop):
            item = media_by_id.get(identity)
            if item is None or item.get('nativeGenerator') is not None: continue
            _media_info(item, probes, heartbeat)
            if not item.get('_renderStill'):
                profiles.add(item['_sourceProfile'])
    hdr = any(profile[1] in HDR_TRANSFERS for profile in profiles)
    if hdr and spec['hdr_capable'] and len(profiles)!=1 and color_space=='source':
        raise ValueError(f'A {spec["label"]} HDR master needs one HDR color space. Select Rec. 2100 PQ or Rec. 709 to combine HDR and SDR sources.')
    profile = next(iter(profiles)) if len(profiles)==1 else None
    if profile and not all(value and value not in ('unknown','unspecified') for value in profile):profile=None
    # Untagged or mixed SDR sources are delivered as explicitly tagged Rec. 709; HDR sources are
    # tone-mapped to Rec. 709 in deliveries that cannot carry HDR (8-bit, image sequences).
    if profile is None or color_space=='rec709' or hdr and not spec['hdr_capable']: profile=REC709
    if color_space=='pq': profile=PQ
    if color_space=='hlg' and profile[1]!='arib-std-b67':
        raise ValueError('An HLG delivery keeps HLG sources as they are; this range has no single HLG source. '
                         'Splice does not convert other material to HLG: choose Rec. 2100 PQ or Rec. 709.')
    if spec['hdr'] and profile[1]!='smpte2084':
        spec['hdr'] = None  # HDR10 static metadata describes PQ programs only.
    elif spec['hdr']:
        delivery.select_hdr10_encoder(spec)  # A source-matched delivery can resolve to PQ here.
    if spec.get('encoder_note'):
        import warnings
        warnings.warn(spec['encoder_note'], stacklevel=2)
        if report is not None:
            report['hardwareFallback'] = True
    options['_color_profile'] = profile
    captions=project.get('captions') or []
    if not isinstance(captions,list): raise ValueError('Invalid captions')
    if len(captions)>MAX_CAPTIONS: raise ValueError(f'The project has {len(captions):,} captions; export supports at most {MAX_CAPTIONS:,}')
    validated_captions=[]
    for caption in captions:
        if not isinstance(caption,dict) or not isinstance(caption.get('text'),str) or len(caption['text'])>5000:
            raise ValueError('Invalid caption')
        start=_number(caption.get('start'),'caption start',0,total)
        length=_number(caption.get('duration'),'caption duration',.001,total)
        if start+length>total+.001: raise ValueError('Caption extends past timeline')
        begin,end = max(start,out_start),min(start+length,out_end)
        if end>begin:validated_captions.append((begin-out_start,end-out_start,caption['text']))
    validated_captions.sort(key=lambda item:item[0])
    track_mix=project.get('trackMix') or {}
    if not isinstance(track_mix, dict): raise ValueError('Invalid track mix')
    solo=any(v.get('solo') for v in track_mix.values() if isinstance(v,dict))
    first_sample = round(Fraction(first)*AUDIO_RATE/fps_fraction)
    stop_sample = round(Fraction(stop)*AUDIO_RATE/fps_fraction)
    if muted.get('audio'):
        events = []
    else:
        events = _audio_events(project['clips'], media_by_id, fps, probes, track_mix, solo,
                               (max(0, out_start-AUDIO_PREROLL_SECONDS), out_end), heartbeat)
    root = Path(work_directory) if work_directory else work_root()
    video_bytes, audio_bytes = _estimated_bytes(out_size['width'], out_size['height'], fps, out_duration, spec)
    # A speed-changed or resampled clip that crosses a chunk seam is rendered
    # whole first (32-bit float stereo). Count the most of those files that are
    # on disk at once, following the mix's own schedule (overlapping ones add up).
    # Lossy deliveries also keep an AAC file until the mux.
    audio_bytes += _prerender_peak_bytes(events, first_sample, stop_sample)
    if spec['audio_lossy']:
        audio_bytes += out_duration*40_000
    output = Path(output_path)
    final_bytes = video_bytes + (out_duration*AUDIO_RATE*2*4 if not spec['audio_lossy'] else out_duration*40_000)
    destination = output if spec['kind'] == 'images' else output.parent
    same_volume = destination.exists() and os.stat(destination).st_dev == os.stat(root).st_dev
    # Window segments (intra codecs) and the streamed video file are on disk until the mux.
    _ensure_space(root, (video_bytes if spec['kind'] != 'images' else 0)+audio_bytes+(final_bytes if same_volume else 0), 'this export')
    if not same_volume and destination.exists():
        _ensure_space(destination, final_bytes, 'the exported file')
    if report is not None:
        report['delivery'] = _delivery_report(spec, profile, out_size, fps_fraction, out_duration, width, height,
                                              delivery.timecode_label(timecode_frames, fps_fraction, drop_frame)
                                              if spec['timecode'] else None)
    def stage(low, high):
        return (lambda fraction: progress(low+(high-low)*fraction)) if progress else None
    burn = None
    if spec['burn_ins'] and not audio_only:
        burn = dict(settings=spec['burn_ins'], timecode=timecode_frames - first, drop=drop_frame, media=media_by_id,
                    font=_font_path('IBM Plex Mono'))
    with WorkDirectory('splice-segments-', root) as directory:
        if audio_only:
            chunks = _render_audio(events, first_sample, stop_sample, None if limiter is None else (limiter, False),
                                   directory, cancelled, heartbeat, stage(0, .95))
            _run_ffmpeg(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0',
                         '-i',str(_concat_list(directory,'audio.txt',chunks)),
                         *delivery.audio_args(spec, AUDIO_RATE), str(output)],cancelled,heartbeat)
            if progress: progress(1.0)
            return output.stat().st_size
        if spec['mode'] == 'segments':
            segments = _render_picture(project, picture_clips, first, stop, total_frames, fps, width, height, spec, options,
                                       directory, cancelled, heartbeat, stage(0, .8), burn)
            video_input = ['-f','concat','-safe','0','-i',str(_concat_list(directory,'segments.txt',segments))]
        else:
            if spec['kind'] == 'images':
                output.mkdir(parents=True, exist_ok=True)
            video_file = _stream_picture(project, picture_clips, first, stop, total_frames, fps, width, height, spec, options,
                                         directory, output, cancelled, heartbeat, stage(0, .8), burn, report)
            # Muxed through a one-entry concat list, like the window segments.
            video_input = ['-f','concat','-safe','0','-i',str(_concat_list(directory,'segments.txt',[video_file]))] if video_file else []
        lossy = spec['audio_lossy']
        mix_limiter = None if limiter is None else (limiter, lossy)
        chunks = _render_audio(events, first_sample, stop_sample, mix_limiter, directory, cancelled, heartbeat, stage(.8, .92))
        audio_list = _concat_list(directory,'audio.txt',chunks)
        if lossy:
            bitrate = str(spec['audio_bitrate'])+'k'
            audio_file, measured = _encode_aac(audio_list, bitrate, limiter, peak_safe, directory, cancelled, heartbeat)
            if report is not None and measured:
                report['audio'] = measured
            audio_input = ['-i',str(audio_file)]
        else:
            audio_input = ['-f','concat','-safe','0','-i',str(audio_list)]
        if progress: progress(.96)
        sidecars = []
        caption_text = lambda kind: _caption_text(validated_captions, kind)
        with_captions = validated_captions and options.get('captions',True)
        subtitle_codec = delivery.CONTAINERS[spec['container']]['subtitles']
        wanted = list(spec['caption_files'])
        if with_captions and not subtitle_codec and 'srt' not in wanted:
            # MXF and image sequences carry no subtitle stream: captions travel as a sidecar file.
            wanted.insert(0, 'srt')
            import warnings
            carrier = 'An image sequence' if spec['kind'] == 'images' else delivery.CONTAINERS[spec['container']]['label']
            warnings.warn(f'{carrier} carries no subtitle track: the captions were written to a sidecar SRT file beside it.',
                          stacklevel=2)
        if spec['kind'] == 'images':
            # The sound travels beside the numbered frames as a WAV.
            sound = output / (output.name + '.wav')
            _run_ffmpeg(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',*audio_input,
                         '-c:a',delivery.AUDIO_CODECS[spec['audio']]['encoder'],'-ar',str(AUDIO_RATE),
                         '-t',f'{out_duration:.6f}',str(sound)],cancelled,heartbeat)
            sidecar_base = output / output.name
        else:
            sidecar_base = output.with_suffix('')
        if with_captions:
            for kind in wanted:
                target = Path(str(sidecar_base) + '.' + kind)
                target.write_text(caption_text(kind), encoding='utf-8')
                sidecars.append(target)
        if report is not None and sidecars:
            report['sidecars'] = [path.name for path in sidecars]
        if spec['kind'] == 'images':
            if progress: progress(1.0)
            return sum(path.stat().st_size for path in output.iterdir())
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',*video_input,*audio_input]
        embed = with_captions and subtitle_codec
        if embed:
            subtitle=Path(directory)/'captions.srt'
            subtitle.write_text(caption_text('srt'),encoding='utf-8')
            command.extend(['-i',str(subtitle)])
        command.extend(['-map','0:v:0','-map','1:a:0'])
        if embed: command.extend(['-map','2:s:0'])
        command.extend(['-c:v','copy',*delivery.audio_args(spec, AUDIO_RATE)])
        if embed:
            command.extend(['-c:s',subtitle_codec,'-metadata:s:s:0',f'language={language}','-disposition:s:0','default'])
        command.extend(_color_tags(profile, spec))
        command.extend(delivery.metadata_args(spec, delivery.timecode_label(timecode_frames, fps_fraction, drop_frame)
                                              if spec['timecode'] else None))
        if spec['codec'] == 'hevc':
            command.extend(['-tag:v', 'hvc1'])
        # The segments and chunks already have the exact frame and sample
        # counts; -t only guards against a stray packet, so give it half a
        # frame (a -t of the exact duration drops the last audio sample).
        command.extend(['-t',f'{out_duration+.5/fps:.6f}',*delivery.container_flags(spec),'-f',delivery.CONTAINERS[spec['container']]['muxer'],
                        str(output)])
        _run_ffmpeg(command,cancelled,heartbeat)
        if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
        if progress: progress(1.0)
        return output.stat().st_size


def _caption_text(captions, kind):
    """SubRip or WebVTT text for (start, end, text) cues relative to the export's first frame."""
    def stamp(seconds, separator):
        ms = round(seconds*1000)
        return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d}{separator}{ms%1000:03d}'
    clean = lambda text: '\n'.join(line for line in text.replace('\r', '').replace('-->', '→').strip().split('\n') if line.strip())
    if kind == 'vtt':
        return 'WEBVTT\n\n' + ''.join(f'{index}\n{stamp(start,".")} --> {stamp(end,".")}\n{clean(text)}\n\n'
                                      for index, (start, end, text) in enumerate(captions, 1))
    return ''.join(f'{index}\n{stamp(start,",")} --> {stamp(end,",")}\n{text.replace(chr(13)," ").replace("-->","→").strip()}\n\n'
                   for index, (start, end, text) in enumerate(captions, 1))


def _delivery_report(spec, profile, size, fps, seconds, width, height, timecode):
    notes = delivery.conversions(spec, width, height, fps)
    if spec['codec'] in ('prores', 'tiff') and fps == Fraction(24000, 1001):
        notes.append('The sequence is 23.976 fps. A DCP plays at 24.000 fps: conform the speed in the DCP tool '
                     '(about 0.1% faster, audio pitch shifts accordingly) or keep 23.976 if the festival accepts it.')
    if spec['kind'] == 'images' or spec['codec'] in ('prores', 'dnxhr'):
        notes.append('DCP and IMF packaging is done in a mastering tool such as DCP-o-matic; Splice does not make DCPs.')
    return dict(container=delivery.CONTAINERS[spec['container']]['label'], codec=spec['label'], profile=spec['profile_label'],
                encoder=spec['encoder'], pixelFormat=spec['pix_fmt'], bitDepth=spec['depth'], alpha=spec['alpha'],
                color=dict(primaries=profile[0], transfer=profile[1], matrix='rgb' if spec['kind'] == 'images' else profile[2],
                           range='full' if spec['kind'] == 'images' else 'limited'),
                width=size['width'], height=size['height'], frameRate=f'{fps.numerator}/{fps.denominator}',
                seconds=round(seconds, 6), audio=delivery.AUDIO_CODECS[spec['audio']]['label'], timecode=timecode,
                hardware=bool(spec['hardware']), notes=notes)


def _measure_decoded(path, cancelled, heartbeat):
    """Decoded true peak (dBTP, 4x oversampled) and integrated loudness (LUFS), ITU-R BS.1770."""
    report = _run_ffmpeg(['ffmpeg','-nostdin','-hide_banner','-nostats','-loglevel','info','-i',str(path),
                          '-af','ebur128=peak=true:framelog=quiet','-f','null','-'],cancelled,heartbeat)
    import re
    summary = report[report.rfind('Integrated loudness:'):]
    peak = re.search(r'True peak:\s+Peak:\s+(-?[0-9.]+|-inf)\s+dBFS', summary)
    loudness = re.search(r'I:\s+(-?[0-9.]+|-inf|nan)\s+LUFS', summary)
    if not peak:
        raise ValueError('Could not measure the exported audio peak')
    value = lambda text: -math.inf if text == '-inf' else float(text)
    return value(peak.group(1)), (value(loudness.group(1)) if loudness and loudness.group(1) != 'nan' else -math.inf)


def _true_peak_db(path, cancelled, heartbeat):
    """Decoded true peak (dBTP, 4x oversampled, ITU-R BS.1770) of an audio file."""
    return _measure_decoded(path, cancelled, heartbeat)[0]


def _db_text(value):
    return f'{value:+.1f}'.replace('+', '+' if value > 0 else '').replace('-', '\u2212') if math.isfinite(value) else '-inf'


def _encode_aac(audio_list, bitrate, ceiling, peak_safe, directory, cancelled, heartbeat):
    """Encode the limited mix to AAC once at unity gain and measure the decoded file.

    Returns (path, measurements). Without a ceiling (limiter off) nothing is
    measured. By default the program level is never changed here: a decoded
    true peak over the ceiling (AAC overshoot) is reported with a warning.
    With ``peak_safe`` (export option aacPeakSafe) the whole program is
    re-encoded quieter until the decoded true peak fits: the first gain is the
    measured excess rounded up to the next AAC_PEAK_STEP_DB, then one step more
    per attempt. Every step depends only on the decoded measurements, so the
    same mix always gets the same gain.
    """
    import warnings
    target = Path(directory)/'audio.m4a'

    def encode(gain):
        volume = ['-af', f'volume={gain:.2f}dB'] if gain else []
        _run_ffmpeg(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0',
                     '-i',str(audio_list),*volume,'-c:a','aac','-b:a',bitrate,str(target)],cancelled,heartbeat)
    encode(0)
    if ceiling is None:
        return target, None
    peak, loudness = _measure_decoded(target, cancelled, heartbeat)
    # JSON has no infinities: silence reports null peak and loudness.
    finite = lambda value: round(value, 2) if math.isfinite(value) else None
    measured = {'ceilingDb': ceiling, 'decodedTruePeakDb': finite(peak), 'loudnessLufs': finite(loudness),
                'aacPeakSafe': peak_safe, 'gainDb': 0.0}
    if peak <= ceiling:
        return target, measured
    if not peak_safe:
        warnings.warn(f'The MP4 audio peaks at {_db_text(peak)} dBTP once decoded, above the {_db_text(ceiling)} dBTP ceiling: '
                      'the mix is limited to the ceiling, and AAC encoding adds the overshoot. Program loudness was left '
                      f'unchanged ({_db_text(loudness)} LUFS). To deliver under the ceiling, lower the limiter ceiling, export with '
                      'the aacPeakSafe option (it lowers the whole program until the decoded peak fits), or export WAV or ProRes.',
                      stacklevel=2)
        return target, measured
    excess = peak-ceiling
    gain = -math.ceil(excess/AAC_PEAK_STEP_DB-1e-9)*AAC_PEAK_STEP_DB
    for attempt in range(AAC_PEAK_ATTEMPTS):
        encode(gain)
        peak, loudness = _measure_decoded(target, cancelled, heartbeat)
        if peak <= ceiling:
            break
        if attempt+1 < AAC_PEAK_ATTEMPTS:
            gain -= AAC_PEAK_STEP_DB
    measured.update(decodedTruePeakDb=finite(peak), loudnessLufs=finite(loudness), gainDb=round(gain, 2))
    if peak <= ceiling:
        warnings.warn(f'aacPeakSafe lowered the whole program by {-gain:.1f} dB '
                      f'(now {_db_text(loudness)} LUFS) so the decoded MP4 audio peaks at {_db_text(peak)} dBTP, '
                      f'under the {_db_text(ceiling)} dBTP ceiling.', stacklevel=2)
    else:
        warnings.warn(f'aacPeakSafe lowered the whole program by {-gain:.1f} dB, but the decoded MP4 audio still '
                      f'peaks at {_db_text(peak)} dBTP, above the {_db_text(ceiling)} dBTP ceiling.', stacklevel=2)
    return target, measured


def _picture_windows(project, picture, first, stop, total_frames, fps, width, height, spec, options, burn):
    """Plan the picture windows. Yields (index, count, segment project, window options, output frames)."""
    # Keep transitions and the preceding frame in a single window. Work in
    # frame numbers so snapping cannot move a boundary back into an effect.
    starts = []
    protected = []
    reversed_spans = []
    for clip in picture:
        start = float(clip['start']) - float(clip.get('transitionBefore', 0) or 0)
        frame = math.ceil(start * fps - 1e-6)
        starts.append(frame)
        if clip.get('transition') in ('dissolve', 'wipe', 'fade'):
            end = start + float(clip.get('transitionDuration', .5))
            # Keep at least the cut's first frame with the transition: a
            # transition that ends at the cut ("end at cut") would otherwise
            # leave a window holding only the lead-in, with no clip after it.
            cut_frame = math.ceil(float(clip['start']) * fps - 1e-6)
            protected.append((frame - 1, max(math.ceil(end * fps - 1e-6), cut_frame + 1)))
        if clip.get('type') == 'video' and clip.get('reverse'):
            reversed_spans.append((frame, math.ceil((float(clip['start'])+float(clip['duration']))*fps-1e-6)+1))
    starts.sort()
    protected.sort()
    reversed_spans.sort()
    import bisect
    frame_bytes = width*height*_alpha_bytes_per_pixel(spec)
    reverse_frames = max(1, int(REVERSE_BUFFER_BYTES // frame_bytes))
    frames = [0]
    reversed_active, reversed_next = [], 0
    while frames[-1] < total_frames:
        current = frames[-1]
        proposed = min(current + round(VIDEO_WINDOW_SECONDS * fps), total_frames)
        low = bisect.bisect_right(starts, current)
        high = bisect.bisect_left(starts, proposed)
        if high-low > VIDEO_WINDOW_CUTS:
            proposed = starts[low+VIDEO_WINDOW_CUTS]
        while reversed_next < len(reversed_spans) and reversed_spans[reversed_next][0] < proposed:
            reversed_active.append(reversed_spans[reversed_next]); reversed_next += 1
        reversed_active = [span for span in reversed_active if span[1] > current]
        overlapping = sum(1 for span in reversed_active if span[0] < proposed)
        if overlapping:
            # Each reversed clip buffers its frames in this window.
            proposed = min(proposed, current + max(1, reverse_frames // overlapping))
        for protect_from, protect_until in protected:
            if protect_from <= proposed < protect_until:
                proposed = protect_until
        frames.append(min(total_frames, proposed))
    windows = [(a,b) for a,b in zip(frames,frames[1:]) if b>first and a<stop]
    media_by_id = {item['id']: item for item in project['media']}
    # A clip's chain takes the same pixel-format round trips in every window
    # (dips go through RGB, wipes through 4:4:4), or its pixels would change
    # at a window seam. Record which clips have them in the whole timeline.
    video = sorted((clip for clip in picture if clip.get('type') == 'video'),
                   key=lambda clip: (clip.get('track', 0), float(clip['start'])))
    fps_value = float(fps)
    _, followers = _picture_transitions(video, media_by_id, fps_value)
    dip_format, wipe_mask = set(), set()
    for number, clip in enumerate(video):
        before = float(clip.get('transitionBefore', 0) or 0)
        if clip.get('transition') == 'fade':
            duration = min(float(clip.get('transitionDuration', .5)), float(clip['duration'])+before)
            if duration-before > 0:
                dip_format.add(id(clip))
        if clip.get('transition') == 'wipe':
            wipe_mask.add(id(clip))
        following = video[followers[number]] if number in followers else None
        if following is not None and following.get('transition') == 'fade' and float(following.get('transitionBefore', 0) or 0) > 0:
            dip_format.add(id(clip))
    # Sweep the clips once: each window sees only the clips that overlap it.
    ordered = sorted(picture, key=lambda clip: float(clip['start'])-float(clip.get('transitionBefore',0) or 0))
    active, pointer = [], 0
    for index,(a_frame,b_frame) in enumerate(windows):
        begin,end = a_frame/fps,b_frame/fps
        while pointer < len(ordered) and float(ordered[pointer]['start'])-float(ordered[pointer].get('transitionBefore',0) or 0) < end:
            active.append(ordered[pointer]); pointer += 1
        active = [clip for clip in active if float(clip['start'])+float(clip['duration']) > begin]
        clips = []
        for original in active:
            cut=float(original['start']);before=float(original.get('transitionBefore',0) or 0)
            a = cut-before; b = cut + float(original['duration'])
            overlap_start=max(begin,a); overlap_end=min(end,b)
            if overlap_end - overlap_start < .5/fps: continue
            clip=dict(original)
            speed=_number(clip.get('speed',1),'speed',.1,16)
            for key in [key for key in clip if isinstance(key, str) and key.startswith('_')]:
                del clip[key]
            # Keyframes stay whole: the window's clip reads them through a
            # shifted clock (_keyframe_clock), so values match every window.
            if clip.get('keyframes'):
                _slice_keyframes(clip['keyframes'], 0, float(original['duration']))  # validation only
            if before and overlap_start <= cut+.5/fps:
                clip['start']=cut-begin
                clip['duration']=overlap_end-cut
                if clip.get('reverse'):
                    # Reversed playback ends at the in point: dropping the
                    # clip's end moves the in point, not the start.
                    clip['in']=float(clip.get('in',0)) + (b-overlap_end)*speed
            else:
                clip['start']=overlap_start-begin
                clip['duration']=overlap_end-overlap_start
                clip['in']=float(clip.get('in',0)) + ((b-overlap_end) if clip.get('reverse') else (overlap_start-cut))*speed
                clip['transitionBefore']=0
                if overlap_start > cut+.5/fps: clip['transition']='none'
                clip['_keyframeShift']=overlap_start-cut
            if id(original) in dip_format: clip['_dipFormat'] = True
            if id(original) in wipe_mask: clip['_wipeMask'] = True
            clips.append(clip)
        # Gaps still need a black base track to keep exact segment duration.
        clips.append({'type':'title','start':0,'duration':end-begin,'in':0,'title':''})
        used = {clip.get('mediaId') for clip in clips}
        segment = {**project, 'clips': clips, 'media': [media_by_id[identity] for identity in used if identity in media_by_id]}
        output_window = (max(first,a_frame)-a_frame,min(stop,b_frame)-a_frame)
        window_options={**options,'_output_frames':output_window if output_window!=(0,b_frame-a_frame) else None}
        if burn:
            window_options['_burn'] = _window_burn(burn, clips, a_frame, output_window[0], fps)
        yield index, len(windows), segment, window_options, output_window[1]-output_window[0]


class _AnyEvent:
    """Set when either event is set (a job's cancel, or a sibling window's failure)."""

    def __init__(self, *events):
        self.events = [event for event in events if event is not None]

    def is_set(self):
        return any(event.is_set() for event in self.events)


def _memory_available():
    """Bytes of memory available for new work (Linux MemAvailable; elsewhere a quarter of RAM)."""
    try:
        with open('/proc/meminfo', encoding='ascii') as handle:
            for line in handle:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') // 4
    except (ValueError, OSError, AttributeError):
        return 4 * 1024**3


# A window's FFmpeg peaks near 1.2 GB at 1920 x 1080 with 24 cuts (measured); scale by area.
WINDOW_MEMORY_BYTES_1080 = 1_300_000_000
MAX_PARALLEL_WINDOWS = 4


def _parallel_windows(width, height):
    """How many picture windows render at once: SPLICE_EXPORT_PARALLEL, else what CPU and free memory allow."""
    value = os.environ.get('SPLICE_EXPORT_PARALLEL')
    if value:
        if not value.isdigit() or not 1 <= int(value) <= 16:
            raise ValueError('SPLICE_EXPORT_PARALLEL must be a whole number from 1 to 16')
        return int(value)
    per_window = max(300_000_000, WINDOW_MEMORY_BYTES_1080 * width * height / (1920*1080))
    by_memory = int(_memory_available() * .5 // per_window)
    by_cpu = (os.cpu_count() or 2) // 4
    return max(1, min(MAX_PARALLEL_WINDOWS, by_memory, by_cpu))


def _render_picture(project, picture, first, stop, total_frames, fps, width, height, spec, options, directory,
                    cancelled, heartbeat, progress, burn=None):
    """Render intra-frame deliveries (ProRes, DNxHR) in bounded windows, several at once when
    memory and cores allow; returns the segment files in order for a stream-copy concat."""
    from concurrent.futures import ThreadPoolExecutor, FIRST_EXCEPTION, wait
    import threading as _threading
    suffix = spec['segment_suffix']
    plans = _picture_windows(project, picture, first, stop, total_frames, fps, width, height, spec, options, burn)
    workers = _parallel_windows(width, height)
    failed = _threading.Event()
    stop_all = _AnyEvent(cancelled, failed)
    segments, done = {}, [0]
    lock = _threading.Lock()

    def render(index, count, segment, window_options):
        target = Path(directory)/f'{index:05d}{suffix}'
        try:
            _render_window(segment, target, directory, options=window_options, cancelled=stop_all)
        except BaseException:
            failed.set()
            raise
        with lock:
            segments[index] = target
            done[0] += 1
            if progress: progress(done[0]/count)
        if heartbeat: heartbeat()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='splice-window') as pool:
        pending = set()
        for index, count, segment, window_options, _ in plans:
            if cancelled and cancelled.is_set(): break
            if failed.is_set(): break
            pending.add(pool.submit(render, index, count, segment, window_options))
            if len(pending) >= workers*2:
                finished, pending = wait(pending, return_when=FIRST_EXCEPTION)
                for future in finished: future.result()
        finished, pending = wait(pending, return_when=FIRST_EXCEPTION)
        if pending:
            failed.set()
            wait(pending)
        errors = [future.exception() for future in finished | pending if future.exception() is not None]
    if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
    # The first real error, not a sibling's InterruptedError from stopping it.
    real = [error for error in errors if not isinstance(error, InterruptedError)]
    if real or errors: raise (real or errors)[0]
    return [segments[index] for index in sorted(segments)]


PIXEL_BYTES = {'yuv420p': 1.5, 'yuv420p10le': 3, 'yuv422p': 2, 'yuv422p10le': 4, 'yuv444p10le': 6, 'yuva444p10le': 8,
               'rgb24': 3, 'rgba': 4, 'rgb48be': 6, 'rgba64be': 8, 'rgb48le': 6, 'rgba64le': 8, 'gbrp10le': 6,
               'gbrpf32le': 12, 'gbrapf32le': 16, 'gbrp16le': 6}
READ_AHEAD_BYTES = 32 * 1024 * 1024
PIPE_CHUNK = 1 << 20


class _Window:
    """One window FFmpeg writing raw frames; read ahead into memory up to a cap while an earlier window streams."""

    def __init__(self, command, expected, cap, activity):
        self.expected, self.cap, self.activity = expected, cap, activity
        self.chunks = collections.deque()
        self.buffered = 0
        self.received = 0
        self.eof = False
        self.read_error = None
        self.errors = collections.deque(maxlen=100)
        self.cond = threading.Condition()
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.threads = [threading.Thread(target=self._read, daemon=True), threading.Thread(target=self._errors, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _read(self):
        try:
            while True:
                with self.cond:
                    while self.buffered >= self.cap and not self.eof:
                        self.cond.wait(.5)
                data = self.process.stdout.read(PIPE_CHUNK)
                if not data:
                    break
                self.activity[0] = time.monotonic()
                with self.cond:
                    self.chunks.append(data)
                    self.buffered += len(data)
                    self.received += len(data)
                    self.cond.notify_all()
        except (OSError, ValueError) as error:
            self.read_error = error
        finally:
            with self.cond:
                self.eof = True
                self.cond.notify_all()

    def _errors(self):
        try:
            for raw in self.process.stderr:
                self.errors.append(raw.decode('utf-8', 'replace'))
        except (OSError, ValueError) as error:
            self.errors.append(str(error))

    def take(self):
        """The next chunk, or None at the end of this window's output."""
        with self.cond:
            while not self.chunks and not self.eof:
                self.cond.wait(.5)
            if not self.chunks:
                return None
            data = self.chunks.popleft()
            self.buffered -= len(data)
            self.cond.notify_all()
            return data

    def finish(self):
        """Wait for the process; raise if it failed or delivered the wrong number of bytes."""
        code = self.process.wait()
        self._close(5)
        if code:
            raise ValueError('FFmpeg render failed: ' + ''.join(self.errors)[-1500:])
        if self.read_error is not None:
            raise ValueError(f'Could not read the picture window: {self.read_error}') from self.read_error
        if self.received != self.expected:
            raise ValueError(f'A picture window produced {self.received} bytes instead of {self.expected}')

    def stop(self):
        if self.process.poll() is None:
            _stop_tree(self.process)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        with self.cond:
            self.eof = True
            self.chunks.clear()
            self.cond.notify_all()
        self._close(2)

    def _close(self, timeout):
        for thread in self.threads:
            thread.join(timeout=timeout)
        # A process FFmpeg started may still hold the pipes: closing them under a blocked reader
        # would block too. The daemon readers end with that process.
        if all(not thread.is_alive() for thread in self.threads):
            for stream in (self.process.stdout, self.process.stderr):
                stream.close()


def _encoder_command(spec, fps, profile, size, target, *, light_levels=None, pass_number=None, passlog=None, sink=None):
    pix = spec['pix_fmt']
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *delivery.hardware_input_args(spec),
               '-f', 'rawvideo', '-pix_fmt', pix, '-s', f'{size["width"]}x{size["height"]}', '-r', str(fps), '-i', 'pipe:0']
    if sink == 'measure':
        return command + light_level_filter_args(profile, target)
    hw_filter = delivery.hardware_filter(spec)
    if hw_filter:
        command += ['-vf', hw_filter]
    command += delivery.encoder_args(spec, fps, profile, light_levels=light_levels, pass_number=pass_number, passlog=passlog,
                                     threads=6 if spec['encoder'] == 'libx264' else None)
    if spec['kind'] != 'images':
        command += _color_tags(profile, spec)
    if pass_number == 1:
        return command + ['-an', '-f', 'null', '-']
    if spec['kind'] == 'images':
        name = Path(target).name
        return command + ['-start_number', str(spec['start_number']), '-f', 'image2',
                          str(Path(target) / f'{name}_%0{spec["padding"]}d.{spec["extension"]}')]
    return command + ['-movflags', '+write_colr', '-f', 'mp4', str(target)]


def _stream_picture(project, picture, first, stop, total_frames, fps, width, height, spec, options, directory, output,
                    cancelled, heartbeat, progress, burn, report):
    """Long-GOP and image-sequence deliveries: every window streams raw frames into ONE encoder.

    One encoder sees the whole program (one GOP structure, real two-pass and HDR light levels);
    several windows decode and composite at once, reading ahead into a bounded buffer. Returns the
    video-only file to mux, or None for image sequences (written into ``output``)."""
    size = spec['_frame']
    profile = options['_color_profile']
    video = Path(directory) / 'video.mp4'
    light = None
    hdr = spec.get('hdr')
    passes = []
    if hdr:
        if hdr['lightLevels'] == 'manual':
            light = (hdr['maxCll'], hdr['maxFall'])
        else:
            passes.append(('measure', None))
    if spec['rate'] and spec['rate']['mode'] == 'twopass':
        passes.append(('encode', 1))
    passes.append(('encode', 2 if spec['rate'] and spec['rate']['mode'] == 'twopass' else None))
    passlog = Path(directory) / 'twopass'
    for number, (kind, pass_number) in enumerate(passes):
        low, high = number/len(passes), (number+1)/len(passes)
        stage = (lambda fraction, low=low, high=high: progress(low+(high-low)*fraction)) if progress else None
        if kind == 'measure':
            measured = Path(directory) / 'light-levels.txt'
            command = _encoder_command(spec, fps, profile, size, measured, sink='measure')
            _pump_windows(project, picture, first, stop, total_frames, fps, width, height, spec, options, directory, command,
                          cancelled, heartbeat, stage, burn)
            light = _read_light_levels(measured)
            if report is not None:
                report['lightLevels'] = dict(maxCll=light[0], maxFall=light[1], method='measured')
            if hdr['lightLevels'] == 'source':
                light = _source_light_levels(project, light)
        else:
            command = _encoder_command(spec, fps, profile, size, output if spec['kind'] == 'images' else video,
                                       light_levels=light, pass_number=pass_number, passlog=passlog)
            _pump_windows(project, picture, first, stop, total_frames, fps, width, height, spec, options, directory, command,
                          cancelled, heartbeat, stage, burn)
    if hdr and report is not None:
        report.setdefault('lightLevels', {}).update(maxCll=light[0], maxFall=light[1],
                                                    **({'method': 'entered'} if hdr['lightLevels'] == 'manual' else {}))
    return None if spec['kind'] == 'images' else video


def _pump_windows(project, picture, first, stop, total_frames, fps, width, height, spec, options, directory, command,
                  cancelled, heartbeat, progress, burn):
    """Run the window FFmpegs (several ahead) and forward their raw frames, in order, to ``command``."""
    size = spec['_frame']
    frame_bytes = round(size['width'] * size['height'] * PIXEL_BYTES[spec['pix_fmt']])
    stall = _stall_seconds()
    workers = min(2, _parallel_windows(width, height))
    cap = max(PIPE_CHUNK, min(READ_AHEAD_BYTES, int(_memory_available() * .25)) // workers)
    activity = [time.monotonic()]
    errors = collections.deque(maxlen=200)
    encoder = subprocess.Popen(['ffmpeg', '-progress', 'pipe:1', '-stats_period', '1', *command[1:]],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    state = dict(error=None, done=False, window=None, stop=False)
    windows = collections.deque()
    plans = _picture_windows(project, picture, first, stop, total_frames, fps, width, height, spec, options, burn)
    lock = threading.Lock()

    def read_progress():
        previous = None
        for raw in encoder.stdout:
            if raw.startswith(b'frame=') and raw != previous:
                previous = raw
                activity[0] = time.monotonic()

    def read_errors():
        for raw in encoder.stderr:
            errors.append(raw.decode('utf-8', 'replace'))

    def launch():
        """Start the next window, if any; returns False when the plan is exhausted."""
        try:
            index, count, segment, window_options, frames = next(plans)
        except StopIteration:
            return False
        window_directory = tempfile.mkdtemp(prefix='window-', dir=directory)
        try:
            window_command = _render_window_inner(segment, None, window_directory, options=window_options, cancelled=cancelled)
            window = _Window(window_command, frames * frame_bytes, cap, activity)
        except BaseException:
            shutil.rmtree(window_directory, ignore_errors=True)
            raise
        window.directory, window.index, window.count = window_directory, index, count
        with lock:
            windows.append(window)
        return True

    def forward():
        try:
            more = True
            while True:
                with lock:
                    in_flight = len(windows)
                while more and in_flight < workers:
                    if cancelled and cancelled.is_set() or state['stop']:
                        return
                    more = launch()
                    in_flight += more
                with lock:
                    if not windows:
                        break
                    window = windows[0]
                state['window'] = window
                while True:
                    data = window.take()
                    if data is None:
                        break
                    encoder.stdin.write(data)
                    activity[0] = time.monotonic()
                window.finish()
                with lock:
                    windows.popleft()
                shutil.rmtree(window.directory, ignore_errors=True)
                if heartbeat: heartbeat()
                if progress: progress((window.index+1)/window.count)
            encoder.stdin.close()
            state['done'] = True
        except BaseException as error:  # reported by the monitor below
            state['error'] = error

    readers = [threading.Thread(target=read_progress, daemon=True), threading.Thread(target=read_errors, daemon=True)]
    for reader in readers:
        reader.start()
    forwarder = threading.Thread(target=forward, daemon=True)
    forwarder.start()
    seen = activity[0]
    failure = None
    try:
        while forwarder.is_alive() or encoder.poll() is None:
            if cancelled and cancelled.is_set():
                raise InterruptedError('Export cancelled')
            if state['error'] is not None:
                break
            if not forwarder.is_alive() and state['done'] and encoder.poll() is not None:
                break
            if encoder.poll() is not None and not state['done']:
                break  # The encoder stopped while frames were still coming.
            if activity[0] != seen:
                seen = activity[0]
                if heartbeat: heartbeat()
            if time.monotonic() - activity[0] > stall:
                raise TimeoutError(f'FFmpeg made no progress for {stall:g} seconds, so the export was stopped '
                                   '(limit set by SPLICE_EXPORT_STALL_SECONDS)')
            time.sleep(.1)
        encoder_code = encoder.poll()
        if state['error'] is not None and encoder_code is None:
            failure = state['error']
        elif encoder_code:
            message = 'FFmpeg encoder failed: ' + ''.join(errors)[-1500:]
            if spec['hardware']:
                raise HardwareEncoderFailed(''.join(errors)[-300:].strip() or f'exit code {encoder_code}')
            raise ValueError(message)
        elif state['error'] is not None:
            failure = state['error']
        elif not state['done']:
            raise ValueError('FFmpeg encoder exited before the final picture window')
        if failure is not None:
            if isinstance(failure, (BrokenPipeError, OSError)) and not isinstance(failure, (InterruptedError, TimeoutError)):
                raise ValueError('FFmpeg encoder stopped: ' + ''.join(errors)[-1500:])
            raise failure
    finally:
        state['stop'] = True
        if encoder.poll() is None:
            _stop_tree(encoder)
        for _ in range(2):
            # Twice: the forwarder may have started one more window before it saw the stop.
            with lock:
                remaining = list(windows)
                windows.clear()
            for window in remaining:
                window.stop()
                shutil.rmtree(window.directory, ignore_errors=True)
            try:
                encoder.stdin.close()
            except OSError:
                pass
            forwarder.join(timeout=10)
        try:
            encoder.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for reader in readers:
            reader.join(timeout=2)
        if all(not reader.is_alive() for reader in readers):
            encoder.stdout.close()
            encoder.stderr.close()
        if not forwarder.is_alive():
            plans.close()


# PQ (SMPTE ST 2084) constants.
_PQ_M1, _PQ_M2, _PQ_C1, _PQ_C2, _PQ_C3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875


def pq_to_nits(signal):
    """ST 2084 EOTF: a PQ signal (0..1) as absolute luminance in cd/m² (nits)."""
    p = max(0.0, min(1.0, signal)) ** (1 / _PQ_M2)
    return 10000 * (max(p - _PQ_C1, 0) / (_PQ_C2 - _PQ_C3 * p)) ** (1 / _PQ_M1)


def light_level_filter_args(profile, target):
    """Measure MaxCLL and MaxFALL (CTA-861.3) of a PQ program.

    Each frame is converted from Y'CbCr (the program's matrix, legal range) to full-range 16-bit
    R'G'B' in the PQ domain. max(R', G', B') per pixel is formed with two ``lighten`` blends on
    planes moved by ``mergeplanes`` (no range conversion), so a frame's brightest pixel is that
    plane's maximum (signalstats YMAX), turned into nits by the ST 2084 EOTF: MaxCLL is the
    largest over all frames. For MaxFALL the same max(R', G', B') plane goes through the EOTF per
    pixel (a lookup table, linear nits scaled so 65535 = 10,000 nits) and is averaged
    (signalstats YAVG): MaxFALL is the largest frame average.
    """
    lut = (f"65535*pow(max(pow(val/65535\\,1/{_PQ_M2})-{_PQ_C1}\\,0)/({_PQ_C2}-{_PQ_C3}*pow(val/65535\\,1/{_PQ_M2}))\\,1/{_PQ_M1})")
    peak, average = Path(str(target) + '.peak'), Path(str(target) + '.average')
    graph = (f'{_to_full_rgb(profile)},format=gbrp16le,split=3[g][b][r];'
             '[r]mergeplanes=0x020202:yuv444p16le[rr];[g]mergeplanes=0x000000:yuv444p16le[gg];'
             '[b]mergeplanes=0x010101:yuv444p16le[bb];[rr][gg]blend=all_mode=lighten[rg];'
             '[rg][bb]blend=all_mode=lighten,split[p][q];'
             f'[p]signalstats,metadata=print:key=lavfi.signalstats.YMAX:file={_filter_path(peak)}[po];'
             f"[q]lutyuv=y='{lut}',signalstats,metadata=print:key=lavfi.signalstats.YAVG:file={_filter_path(average)}[qo]")
    return ['-filter_complex', graph, '-map', '[po]', '-f', 'null', '-', '-map', '[qo]', '-f', 'null', '-']


@lru_cache(maxsize=1)
def _has_zscale():
    try:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == ['zscale'] for line in result.stdout.splitlines() if line.strip())


def _to_full_rgb(profile):
    """Legal-range Y'CbCr to full-range R'G'B' with the program's matrix. zscale (zimg) is exact to
    the last 16-bit code; swscale's limited-to-full path is about 0.4 % low at the top, so it is
    only the fallback when this FFmpeg has no zscale."""
    primaries, transfer, matrix = profile
    if _has_zscale():
        name = {'bt2020nc': '2020_ncl', 'bt2020c': '2020_cl', 'smpte170m': '170m', 'bt470bg': '470bg'}.get(matrix, '709')
        # Primaries and transfer stay as they are (stated so zimg needs no guess).
        prim = {'bt2020': '2020', 'smpte170m': '170m', 'bt470bg': '470bg'}.get(primaries, '709')
        trc = {'smpte2084': 'smpte2084', 'arib-std-b67': 'arib-std-b67', 'smpte170m': '601'}.get(transfer, '709')
        return (f'zscale=matrixin={name}:rangein=limited:range=full:primariesin={prim}:primaries={prim}:'
                f'transferin={trc}:transfer={trc}:dither=none')
    name = {'bt2020nc': 'bt2020', 'bt2020c': 'bt2020', 'smpte170m': 'smpte170m', 'bt470bg': 'bt470bg'}.get(matrix, 'bt709')
    return f'scale=in_color_matrix={name}:in_range=limited:out_range=full'


def _read_light_levels(target):
    import re
    peak_file, average_file = Path(str(target) + '.peak'), Path(str(target) + '.average')
    peaks = [int(value) for value in re.findall(r'YMAX=(\d+)', peak_file.read_text(encoding='ascii'))]
    averages = [float(value) for value in re.findall(r'YAVG=([0-9.]+)', average_file.read_text(encoding='ascii'))]
    if not peaks or not averages:
        raise ValueError('Could not measure the HDR light levels')
    max_cll = max(pq_to_nits(value / 65535) for value in peaks)
    max_fall = max(averages) / 65535 * 10000
    return round(max_cll), min(round(max_cll), round(max_fall))


def _source_light_levels(project, measured):
    """A single untouched HDR10 source's own MaxCLL/MaxFALL, when it carries them; else the measurement."""
    import warnings
    sources = {item.get('_path') for item in project['media'] if item.get('_path') and not item.get('_renderStill')}
    if len(sources) == 1:
        path = next(iter(sources))
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-read_intervals', '%+#1',
                                     '-show_frames', '-of', 'json', str(path)],
                                    capture_output=True, text=True, timeout=30, check=True)
            for frame in json.loads(result.stdout).get('frames', []):
                for side in frame.get('side_data_list', []):
                    if side.get('side_data_type') == 'Content light level metadata':
                        return int(side.get('max_content', 0)), int(side.get('max_average', 0))
        except (subprocess.SubprocessError, OSError, ValueError, TypeError):
            pass
    warnings.warn('The source carries no MaxCLL/MaxFALL: the measured values were written instead.', stacklevel=2)
    return measured


def _window_burn(burn, clips, a_frame, skip, fps):
    """Burn-in data for one window: its first output frame's sequence frame and the clips on top."""
    first_out = a_frame + skip
    rows = []
    media = burn['media']
    for clip in clips:
        if clip.get('type') != 'video' or clip.get('mediaId') == DISABLED_MEDIA:
            continue
        begin = max(0, math.ceil(float(clip['start'])*fps - 1e-6) - skip)
        end = math.ceil((float(clip['start'])+float(clip['duration']))*fps - 1e-6) - skip
        if end <= begin:
            continue
        item = media.get(clip.get('mediaId')) or {}
        source = None
        if 'sourceTimecode' in burn['settings']['fields'] and not clip.get('reverse') and float(clip.get('speed', 1)) == 1:
            source_tc = _media_timecode(item, fps)
            if source_tc is not None:
                start_tc, source_rate, source_drop = source_tc
                # drawtext counts from the window origin, including frames before this clip is visible.
                window_offset = round((float(clip.get('in', 0)) - float(clip['start'])) * fps) + skip
                source = delivery.timecode_label(start_tc + window_offset, source_rate, source_drop)
        rows.append((clip.get('track', 0), begin, end, str(item.get('name') or 'Clip')[:120], source))
    # Sweep only clip edges. Each interval has one label, from its uppermost active track.
    starts = collections.defaultdict(list)
    edges = set()
    for index, (track, begin, end, _, _) in enumerate(rows):
        starts[begin].append((-track, -index, end))
        edges.update((begin, end))
    active, spans = [], []
    previous = None
    boundaries = sorted(edges)
    for begin, end in zip(boundaries, boundaries[1:]):
        for entry in starts.get(begin, ()):
            heapq.heappush(active, entry)
        while active and active[0][2] <= begin:
            heapq.heappop(active)
        if not active:
            previous = None
            continue
        index = -active[0][1]
        if index == previous:
            spans[-1][1] = end
        else:
            _, _, _, name, source = rows[index]
            spans.append([begin, end, name, source])
        previous = index
    return dict(settings=burn['settings'], timecode=burn['timecode'] + first_out, drop=burn['drop'], font=burn['font'],
                frame=first_out, clips=spans)


def _media_timecode(item, fps):
    """The source start frame, rate and drop-frame convention when its rate matches the sequence."""
    if '_sourceTimecodeInfo' in item:
        return item['_sourceTimecodeInfo']
    item['_sourceTimecodeInfo'] = None
    path = item.get('_path')
    if not path:
        return None
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=timecode:stream_tags=timecode:stream=r_frame_rate,codec_type',
                                 '-of', 'json', str(path)], capture_output=True, text=True, timeout=30, check=True)
        data = json.loads(result.stdout)
        labels = [data.get('format', {}).get('tags', {}).get('timecode')] + [s.get('tags', {}).get('timecode') for s in data.get('streams', [])]
        rate = next((Fraction(s['r_frame_rate']) for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
        label = next((value for value in labels if value), None)
        if label and rate and abs(float(rate) - fps) < 1e-3:
            from interchange.timecode import is_drop_frame, snap_rate, timecode_frames
            rate = snap_rate(rate) or rate
            item['_sourceTimecodeInfo'] = (timecode_frames(label, rate), rate, is_drop_frame(label))
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, ZeroDivisionError, KeyError):
        item['_sourceTimecodeInfo'] = None
    return item['_sourceTimecodeInfo']


BURN_IN_XY = {'top-left': ('{m}', '{m}'), 'top-center': ('(w-text_w)/2', '{m}'), 'top-right': ('w-text_w-{m}', '{m}'),
              'bottom-left': ('{m}', 'h-text_h-{m}'), 'bottom-center': ('(w-text_w)/2', 'h-text_h-{m}'),
              'bottom-right': ('w-text_w-{m}', 'h-text_h-{m}')}


def _delivery_tail(spec, width, height, fps, profile, burn, directory):
    """Filters after compositing: output size, burn-ins, safe margins and the delivery pixel format."""
    steps = []
    size = spec.get('output_size')
    out_w, out_h = (size['width'], size['height']) if size else (width, height)
    if size and (out_w, out_h) != (width, height):
        if size['fit'] == 'fit':
            pad = 'black@0' if spec['alpha'] else 'black'
            steps.append(f'scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,'
                         f'pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color={pad}')
        elif size['fit'] == 'fill':
            steps.append(f'scale={out_w}:{out_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={out_w}:{out_h}')
        else:
            steps.append(f'scale={out_w}:{out_h}:flags=lanczos')
        steps.append('setsar=1')
    if burn:
        steps.extend(_burn_in_filters(burn, out_w, out_h, fps, directory, profile))
    if spec['mode'] == 'stream' or spec['kind'] == 'images':
        if spec['kind'] == 'images':
            # zimg writes planar 16-bit RGB; FFmpeg repacks it into the file's pixel format.
            steps.append(_to_full_rgb(profile) + (',format=gbrap16le' if spec['alpha'] else ',format=gbrp16le'))
        steps.append(f'format={spec["pix_fmt"]}')
    return ','.join(steps)


def _burn_in_filters(burn, width, height, fps, directory, profile):
    settings = burn['settings']
    fields = settings['fields']
    size = max(10, round(height * settings['size'] / 100))
    margin = round(height * (0.06 if settings['safeMargins'] else 0.03))
    font = _filter_path(burn['font'])
    def graphic(rgb):
        if profile[1] in HDR_TRANSFERS:
            rgb = _hdr_graphic_color(rgb, profile[0], profile[1])
        return '0x' + rgb
    white = graphic('ffffff')
    box = f'fontfile={font}:fontsize={size}:fontcolor={white}:box=1:boxcolor=black@0.6:boxborderw={max(2, size//6)}'
    rate = Fraction(fps).limit_denominator(1001)
    filters = []
    if settings['safeMargins']:
        for share, color in ((0.93, white), (0.90, graphic('ffff00'))):
            inset = (1 - share) / 2
            filters.append(f'drawbox=x=iw*{inset:.4f}:y=ih*{inset:.4f}:w=iw*{share:.4f}:h=ih*{share:.4f}:color={color}@0.5:t=2')
    def place(position, row=0):
        x, y = BURN_IN_XY[position]
        shift = f'-{row*round(size*1.5)}' if position.startswith('bottom') else f'+{row*round(size*1.5)}'
        return f'x={x.format(m=margin)}:y={y.format(m=margin)}{shift}'
    row = 0
    if 'timecode' in fields:
        label = delivery.timecode_label(burn['timecode'], rate, burn['drop']).replace(':', '\\:').replace(';', '\\;')
        filters.append(f"drawtext={box}:timecode='{label}':rate={rate.numerator}/{rate.denominator}:{place(settings['position'], row)}")
        row += 1
    if 'frame' in fields:
        filters.append(f"drawtext={box}:text='%{{frame_num}}':start_number={burn['frame']}:{place(settings['position'], row)}")
        row += 1
    name_row = 0
    for number, (begin, end, name, source) in enumerate(burn['clips']):
        enable = f'enable=between(n\\,{begin}\\,{end - 1})'
        if 'clipName' in fields:
            text = Path(directory) / f'burn-name-{number}.txt'
            text.write_text(name, encoding='utf-8')
            filters.append(f"drawtext={box}:textfile={_filter_path(text)}:expansion=none:{place(settings['clipNamePosition'], name_row)}:{enable}")
        if 'sourceTimecode' in fields and source is not None:
            label = source.replace(':', '\\:').replace(';', '\\;')
            filters.append(f"drawtext={box}:timecode='{label}':rate={rate.numerator}/{rate.denominator}:"
                           f"{place(settings['clipNamePosition'], name_row + ('clipName' in fields))}:{enable}")
    if 'watermark' in fields and settings['watermark']:
        text = Path(directory) / 'burn-watermark.txt'
        text.write_text(settings['watermark'], encoding='utf-8')
        filters.append(f"drawtext=fontfile={font}:textfile={_filter_path(text)}:expansion=none:fontsize={round(height/8)}:"
                       f"fontcolor={white}@{settings['watermarkOpacity']/100:.2f}:x=(w-text_w)/2:y=(h-text_h)/2")
    return filters
