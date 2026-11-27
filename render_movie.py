"""Render a Splice timeline to a deterministic MP4 with FFmpeg."""
import collections
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
from media_proxy import AVC_REC709, HDR_LINEAR_REC709, HDR_TONE_MAP, HDR_TRANSFERS, HDR_TO_REC709, REC709, tempo_filters
from splice_platform import data_directory, font_roots, installed_font


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
                         'transitionMatchName', 'transitionClock')}
            stand_in.update(mediaId=DISABLED_MEDIA, enabled=False)
            kept.append(stand_in)
    return kept


def _render_source(item):
    src = item.get('src')
    if not src:
        raise ValueError(f"Relink {item.get('name') or 'the offline source'} before exporting.")
    return _media_path(src)


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


def _css_color_filter(color, high_depth, still=False):
    brightness,contrast,saturation,gains=_grade_values(color)
    if (brightness,contrast,saturation,gains)==(1,1,1,(1,1,1)):
        return ''
    channels='rgb'
    brightness_filter=':'.join(f"{c}=val*{brightness:.6f}" for c in channels)
    contrast_filter=':'.join(f"{c}=(val-maxval/2)*{contrast:.6f}+maxval/2" for c in channels)
    mixer=_channel_mixer(_saturation_matrix(saturation))
    grade_filter=':'.join(f'{c}=val*{g:.7f}' for c,g in zip(channels,gains))
    rgb = ('gbrap10le' if still else 'gbrp10le') if high_depth else ('rgba' if still else 'rgb24')
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


def _hdr_grade(color, lut, high_depth, profile, target=None):
    brightness,contrast,saturation,gains=_grade_values(color)
    if (brightness,contrast,saturation,gains)==(1,1,1,(1,1,1)) and (not lut or not high_depth):
        if not high_depth:
            return ','+HDR_TO_REC709+lut
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
        return chain+','+HDR_TONE_MAP+lut
    if lut:
        # The approximate transfer tables can clip extended-range RGB.
        chain+=',zscale=t=bt709:agamma=0,format=gbrpf32le'+lut+',zscale=t=linear:agamma=0,format=gbrpf32le'
    primaries,transfer,matrix=target or profile
    return chain+f',zscale=p={primaries},zscale=t={transfer}:m={matrix}:r=limited:npl={peak:.9f},format=yuv444p10le'


def _hdr_still_filter(profile):
    primaries, transfer, matrix = profile
    # zscale applies the requested legal range to every plane. Keep the
    # converted picture in legal range and restore full-range opacity.
    limited = (f':m={matrix}:r=limited,format=yuva444p10le,'
               "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='clip((p(X,Y)-64)*1023/876,0,1023)'")
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


def _fit_picture(width, height, color, lut, alpha_format, high_depth, profile=None, rec709=False,
                 *, still_frames=0, hdr_profile=None, conform_pq=False):
    if profile and profile[1] in HDR_TRANSFERS:
        grade = _hdr_grade(color,lut,high_depth and not rec709,profile,PQ if conform_pq else None)
    else:
        convert = (',zscale=p=bt709:t=bt709:m=bt709:r=limited,format=yuv444p10le'
                   if rec709 and profile and profile!=REC709
                   and all(value not in (None,'unknown','unspecified') for value in profile) else '')
        grade = convert+_css_color_filter(color,high_depth,bool(still_frames))+lut
        if still_frames and hdr_profile:
            grade += _hdr_still_filter(hdr_profile)
        elif conform_pq and profile:
            primaries,transfer,matrix=(value if value not in (None,'unknown','unspecified') else default
                                      for value,default in zip(profile,REC709))
            grade += (f',zscale=pin={primaries}:tin={transfer}:min={matrix}:p=bt2020:t=linear,format=gbrpf32le,'
                      'zscale=t=smpte2084:m=bt2020nc:r=limited:npl=203,format=yuv444p10le')
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
    # Resample with source handles before cutting. Resetting a resampler at an
    # edit otherwise changes the samples immediately on either side of it.
    trim = (f'aresample=48000:first_pts=0,atrim=start_sample={round(start*AUDIO_RATE)}:'
            f'end_sample={round((start+length)*AUDIO_RATE)},asetpts=PTS-STARTPTS')
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


def _fade_clock(clip, key):
    clock = clip.get(key)
    if not isinstance(clock, dict):
        raise ValueError('Invalid retimed fade')
    origin = _number(clock.get('origin'), 'fade origin', -MAX_TIMELINE_SECONDS, MAX_TIMELINE_SECONDS)
    rate = _number(clock.get('rate'), 'fade rate', -16, 16)
    duration = _number(clock.get('duration'), 'fade duration', .001, MAX_TIMELINE_SECONDS)
    if not rate or (key == 'transitionClock' and rate != 1):
        raise ValueError('Invalid fade rate')
    return origin, rate, duration


def _sample_clock(origin, rate):
    first = round(origin*AUDIO_RATE) - (1 if rate < 0 else 0)
    return lambda t: f'(({first}+{rate:.15g}*round({t}*{AUDIO_RATE}))/{AUDIO_RATE})'


def _transition_envelope(clip, origin, rate, duration):
    # Use the same integer sample clock in both directions, including the last
    # sample before a reversed edit's exclusive out point.
    clock = _sample_clock(origin, rate)
    return lambda t: _audio_transition_gain(clip, f'min(1,max(0,{clock(t)}/{duration:.9f}))')



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


def _transform(clip, width, height, local_offset=0, fps=None):
    t = clip.get('transform') or {}
    scale = _number(t.get('scale',100),'scale',10,400)/100
    y = _number(t.get('y',0),'vertical position',-100,100)/100
    angle = _number(t.get('rotation',0),'rotation',-180,180)
    local_clock=_keyframe_clock(clip,fps,'t',local_offset)
    scale_expr=_keyframe_expr(clip,'scale',scale*100,10,400,local_clock)
    timeline_clock=_keyframe_clock(clip,fps,'t',float(clip['start']))
    horizontal=_horizontal_position(clip,width,'overlay_w',timeline_clock)
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
    position = f"x='{horizontal}':y='(main_h-overlay_h)/2+{height/100:.3f}*({y_expr})'"
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
    if path.suffix.lower() in STILL_IMAGES:
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
        if clip.get('_optionalAudio') and clip['type'] == 'audio':
            return item
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


def _alpha_bytes_per_pixel(high_depth):
    return 6 if high_depth else 2.5


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
    codec=options.get('codec','h264')
    if codec not in ('h264','prores'): raise ValueError('Unsupported export codec')
    high_depth = codec=='prores'
    rec709 = options.get('colorSpace')=='rec709'
    conform_pq = options.get('colorSpace')=='pq'
    base_format = 'yuv422p10le' if high_depth else 'yuv420p'
    alpha_format = 'yuva422p10le' if high_depth else 'yuva420p'
    full_alpha = 'yuva444p10le' if high_depth else 'yuva444p'
    dip_rgb = 'gbrap10le' if high_depth else 'gbrap'
    overlay = 'eof_action=pass:shortest=0:format=' + ('yuv422p10' if high_depth else 'yuv420')
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
               '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}:d={duration:.6f}']
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
        transform,position=_transform(clip,width,height,before,fps)
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
            transform, position = '', 'x=0:y=0'
        else:
            prefix = _picture_input(index,render_source,render_length,speed,width,height,fps,reverse,still_frames,
                                    frames=frames,origin=origin)
            fit = _fit_picture(width,height,color,lut_filter,alpha_format,high_depth,item.get('_sourceProfile'),rec709,
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
        elif clip.get('pictureDip'):
            origin, rate, dip_duration = _fade_clock(clip, 'pictureDip')
            origin += rate * clip.get('_keyframeShift', 0)
            if rate < 0:
                origin -= 1/float(fps)
            # Shift the filter clock so partial fades retain their original slope.
            fade_start = (-origin if rate > 0 else dip_duration-origin) / rate
            shift = max(0, -fade_start)
            direction = 'in' if rate > 0 else 'out'
            chain += (f',format={dip_rgb},setpts=PTS+{shift:.9f}/TB,fade=t={direction}:'
                      f'st={fade_start+shift:.9f}:d={dip_duration/abs(rate):.9f},setpts=PTS-STARTPTS')
            dipped = True
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
        progress_origin, _, progress_duration = (_fade_clock(clip, 'transitionClock')
            if clip.get('transitionClock') else (0, 1, transition_duration))
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
            prior_transform,prior_position=_transform(prior_tail,width,height,fps=fps)
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
                prior_fit = _fit_picture(width,height,prior_color,prior_lut,alpha_format,high_depth,
                                         prior_item.get('_sourceProfile'),rec709,still_frames=still_tail,
                                         hdr_profile=hdr_profile,conform_pq=conform_pq)
                prior_flip = ',reverse' if prior_reverse and not still_tail and prior_index is not None else ''
                prior_alpha = _mask_alpha(f'{prior_opacity:.6f}',alpha_format,full_alpha) if prior_opacity!=1 else ''
                filters.append(f'{prior_prefix},{prior_fit}{prior_flip}{prior_transform}{prior_alpha},setpts=PTS+{cut_frame}[tail{number}]')
                filters.append(f'[{active}][tail{number}]overlay={prior_position}:{overlay}[under{number}]')
                active = f'under{number}'
        elif clip.get('transition') == 'dissolve' and not disabled:
            if clip.get('transitionClock'):
                chain += _mask_alpha(f'min(1,(T+{progress_origin:.9f})/{progress_duration:.9f})', alpha_format, full_alpha)
            else:
                chain += f',fade=t=in:st=0:d={transition_duration:.6f}:alpha=1'
        if disabled:
            pass
        elif clip.get('transition') == 'wipe':
            left = _horizontal_position(clip,width,'W',_keyframe_clock(clip,fps,'T',before))
            # The frame at the wipe's end is fully revealed: T and the printed
            # duration can differ by rounding, so compare with 1 us of slack.
            t = f'(T+{progress_origin:.9f})'
            mask = f'if(gte({t},{progress_duration-1e-6:.9f}),1,lt(X+({left}),{width}*{t}/{progress_duration:.9f}))'
            chain += _mask_alpha(mask,alpha_format,full_alpha)
        elif clip.get('_wipeMask'):
            # Same format round trip as the window that holds the wipe.
            chain += _mask_alpha('1',alpha_format,full_alpha)
        chain += f',setpts=PTS+{first_frame}[clip{number}]'
        filters.append(chain)
        wipe_off = overlap_transition and disabled and clip.get('transition') == 'wipe'
        if overlap_transition and clip.get('transition') == 'dissolve' or wipe_off:
            filters.append(f'[without{number}][clip{number}]overlay={position}:{overlay}[incoming{number}]')
            progress_start = render_start - progress_origin
            end = progress_start + progress_duration
            progress = f'(T-({progress_start:.9f}))/{progress_duration:.9f}'
            # A wipe to a disabled clip uncovers the layers beneath from the left.
            mix = f'if(lt(X,W*{progress}),B,A)' if wipe_off else f'A*(1-{progress})+B*{progress}'
            # (The wipe's last frame is fully uncovered: compare with 1 us of slack, as the wipe mask does.)
            finish = end-1e-6 if wipe_off else end
            blend = f'if(lte(T,{progress_start:.9f}),A,if(gte(T,{finish:.9f}),B,{mix}))'
            filters.append(f"[{active}][incoming{number}]blend=all_expr='{blend}'[base{number+1}]")
        else:
            filters.append(f'[{active}][clip{number}]overlay={position}:{overlay}[base{number+1}]')
        active = f'base{number+1}'
    window = options.get('_output_frames')
    if window is not None:
        first, stop = window
        filters.append(f'[{active}]trim=start_frame={first}:end_frame={stop},setpts=PTS-STARTPTS[vwindow]')
        active = 'vwindow'
        duration = (stop-first)/fps
    output = Path(output_path)
    command.extend([*_graph_args(';'.join(filters), directory, 'picture'), '-map', f'[{active}]', '-an'])
    if codec=='prores':
        command.extend(['-t',f'{float(duration):.6f}','-r',str(fps),'-c:v','prores_ks','-profile:v','3','-pix_fmt','yuv422p10le','-threads','4'])
    else:
        command.extend(['-t',f'{float(duration):.6f}','-r',str(fps),'-c:v','libx264','-threads','4','-preset','veryfast',
                        '-crf',str(int(_number(options.get('crf',20),'quality',14,32))),'-pix_fmt','yuv420p'])
    command.extend(_color_tags(profile, codec))
    command.extend(['-movflags','+write_colr',str(output)])
    _run_ffmpeg(command,cancelled,options.get('_heartbeat'))
    return output.stat().st_size


def _color_tags(profile, codec):
    """Explicit stream color signaling: primaries, transfer, matrix and (legal) range."""
    tags = ['-color_primaries',profile[0],'-color_trc',profile[1],'-colorspace',profile[2],'-color_range','tv']
    if codec == 'prores':
        # ProRes carries its color description in each frame header as well.
        tags += ['-bsf:v',f'prores_metadata=color_primaries={profile[0]}:color_trc={profile[1]}:colorspace={profile[2]}']
    elif codec == 'h264' and profile == REC709:
        tags += ['-bsf:v',AVC_REC709]
    return tags


def _audio_events(clips, media_by_id, fps, probes, track_mix, solo, span, heartbeat=None, audio_stems=()):
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
        origin, rate, fade_length = _fade_clock(clip, 'fadeClock') if clip.get('fadeClock') else (0, 1, length)
        fade_out = min(fade_length,_number(clip.get('fadeOut',0),'fade out',0,MAX_TIMELINE_SECONDS))
        after = 0
        if next_clip:
            next_before=_number(next_clip.get('transitionBefore',0),'transition lead')
            next_duration=_number(next_clip.get('transitionDuration',.5),'transition duration',1/float(fps))
            next_duration=min(next_duration,next_before+float(next_clip['duration']))
            if not fade_out:
                after = max(0,next_duration-next_before)
        if start-before >= end or start+length+(after if after>=1/float(fps) else 0) <= begin:
            continue
        _check_clip({**clip, 'type':'audio'} if clip.get('mediaId') in audio_stems else clip, media_by_id, probes, heartbeat)
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
        # The parent's pan acts on stereo after the child has been panned.
        stages = mix.get('outputStages', [])
        if not isinstance(stages, list) or len(stages) > 64:
            raise ValueError('Invalid nested output mixer settings')
        for stage in stages:
            if not isinstance(stage, dict):
                raise ValueError('Invalid nested output mixer stage')
            gain *= _number(stage.get('gain'), 'nested output gain', 0, 200)/100
            output_pan = _number(stage.get('pan'), 'nested output pan', -100, 100)/100
            if output_pan:
                panner += ',' + _stereo_pan(output_pan, 2)
        fade_in = min(fade_length,_number(clip.get('fadeIn',0),'fade in',0,MAX_TIMELINE_SECONDS))
        processing=','.join([*tempo_filters(speed),f'volume={gain:.4f}',panner])
        envelope=[]
        if clip.get('pictureDip'):
            envelope.append(_transition_envelope(clip, *_fade_clock(clip, 'pictureDip')))
        if volume_keys:
            envelope.append(lambda t, clip=clip, before=before: '({})/100'.format(
                _keyframe_expr(clip,'volume',float(clip.get('volume',100)),0,200,f'({t}-{before:.6f})')))
        if clip.get('transition') in ('dissolve','wipe','fade'):
            incoming=min(_number(clip.get('transitionDuration',.5),'transition duration',1/float(fps)),render_length)
            clock = _fade_clock(clip, 'transitionClock') if clip.get('transitionClock') else (0, 1, incoming)
            envelope.append(_transition_envelope(clip, *clock))
        if next_clip:
            shift, _, next_span = (_fade_clock(next_clip, 'transitionClock') if next_clip.get('transitionClock')
                              else (0, 1, next_duration))
            fade_start=before+length-next_before-shift
            envelope.append(lambda t, n=next_clip, s=fade_start, d=next_span:
                            _audio_transition_gain(n,f'max(0,min(1,1-({t}-({s:.9f}))/{d:.9f}))'))
        if fade_in or fade_out:
            head = (lambda t, b=before, f=fade_in: f'max(0,({t}-{b:.9f})/{f:.9f})') if fade_in else (lambda t: '1')
            tail = (lambda t, r=before+fade_length, f=fade_out: f'max(0,({r:.9f}-{t})/{f:.9f})') if fade_out else (lambda t: '1')
            local = _sample_clock(origin, rate)
            envelope.append(lambda t, head=head, tail=tail, local=local:
                            f'min(1,min({head(local(t))},{tail(local(t))}))')
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
        offset = math.floor(max(0.0, run['low']-.25))
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
    offset = math.floor(max(0.0, low-.25))
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


def _resolve_subclip(media_id, media_by_id):
    """Walk parentMediaId links for a subclip, returning (root_media_id, total_in_offset)."""
    total_in = 0.0
    seen = set()
    current_id = media_id
    while True:
        if current_id in seen:
            raise ValueError('Circular subclip reference')
        seen.add(current_id)
        item = media_by_id.get(current_id)
        if not isinstance(item, dict) or not item.get('parentMediaId'):
            return current_id, total_in
        sub_in = _number(item.get('subclipIn', 0), 'subclip in', 0, MAX_SOURCE_SECONDS)
        if item.get('subclipOut') is not None:
            sub_out = _number(item.get('subclipOut'), 'subclip out', 0, MAX_SOURCE_SECONDS)
            if sub_out <= sub_in:
                raise ValueError('Subclip out point must be after its in point')
        parent_id = item.get('parentMediaId')
        if not isinstance(parent_id, str) or parent_id not in media_by_id:
            raise ValueError('Subclip references missing parent media')
        total_in += sub_in
        current_id = parent_id


def _expand_editorial_clips(project):
    """Expand un-flattened multicam clips and subclips into plain clips referencing underlying media.

    Returns a new project dict when expansion is needed, or the original dict unchanged otherwise.
    """
    import copy
    media = project.get('media')
    clips = project.get('clips')
    if not isinstance(media, list) or not isinstance(clips, list):
        return project
    has_special = any(
        isinstance(item, dict) and (item.get('multicam') or item.get('parentMediaId'))
        for item in media
    )
    if not has_special:
        return project
    prepared = copy.deepcopy(project)
    media_by_id = {m['id']: m for m in prepared['media'] if isinstance(m, dict) and isinstance(m.get('id'), str)}

    def angle_clip(base_clip, angle, kind, track, *, embedded_audio=None, suffix=''):
        ang_media_id = angle.get('mediaId')
        if not isinstance(ang_media_id, str) or ang_media_id not in media_by_id:
            raise ValueError('Multicam angle references missing media')
        ang_media = media_by_id[ang_media_id]
        if ang_media.get('multicam'):
            raise ValueError('Nested multicam clips inside multicam angles are unsupported')
        offset = _number(angle.get('offset', 0), 'multicam angle offset', 0, MAX_SOURCE_SECONDS)
        ang_in = _number(angle.get('in', 0), 'multicam angle in', 0, MAX_SOURCE_SECONDS)
        raw_dur = angle.get('duration')
        if raw_dur is None or raw_dur == 0:
            raw_dur = ang_media.get('duration')
        ang_dur = _number(raw_dur, 'multicam angle duration', 0.001, MAX_SOURCE_SECONDS) if raw_dur else math.inf
        speed = _number(base_clip.get('speed', 1), 'speed', 0.1, 16)
        mc_in = _number(base_clip.get('in', 0), 'source in', 0, MAX_SOURCE_SECONDS)
        mc_dur = _number(base_clip.get('duration', 0), 'duration', 0.001, MAX_TIMELINE_SECONDS)
        mc_start = _number(base_clip.get('start', 0), 'start', 0, MAX_TIMELINE_SECONDS)
        mc_span = mc_dur * speed
        ov_start = max(mc_in, offset)
        ov_end = min(mc_in + mc_span, offset + ang_dur)
        if ov_end <= ov_start + 1e-6:
            return None
        out = copy.deepcopy(base_clip)
        out['id'] = f"{base_clip.get('id', 'mc')}{suffix}"
        out['type'] = kind
        out['track'] = track
        out['mediaId'] = ang_media_id
        if base_clip.get('reverse'):
            out['start'] = mc_start + (mc_in + mc_span - ov_end) / speed
        else:
            out['start'] = mc_start + (ov_start - mc_in) / speed
        out['duration'] = (ov_end - ov_start) / speed
        head = out['start'] - mc_start
        if head > 1e-6 or out['duration'] < mc_dur - 1e-6:
            if base_clip.get('keyframes'):
                out['keyframes'] = _slice_keyframes(base_clip['keyframes'], head, out['duration'])
            for key in ('fadeClock', 'pictureDip'):
                if base_clip.get(key):
                    origin, rate, length = _fade_clock(base_clip, key)
                    out[key] = dict(origin=origin + rate * head, rate=rate, duration=length)
                elif key == 'fadeClock' and (base_clip.get('fadeIn') or base_clip.get('fadeOut')):
                    out[key] = dict(origin=head, rate=1, duration=mc_dur)
            transition = base_clip.get('transition')
            if transition in ('fade', 'wipe', 'dissolve'):
                before = _number(base_clip.get('transitionBefore', 0), 'transition lead')
                span = min(mc_dur + before, _number(base_clip.get('transitionDuration', .5), 'transition duration'))
                if head >= span - before or (transition == 'fade' and (head > 0 or out['duration'] < span - before)):
                    if transition == 'fade' and head < span - before and not out.get('pictureDip'):
                        out['pictureDip'] = (dict(origin=head + before, rate=1, duration=span) if kind == 'audio'
                                             else dict(origin=head, rate=1, duration=span - before))
                    out['transition'] = 'none'
                    out.pop('transitionBefore', None)
                    out.pop('transitionDuration', None)
                    out.pop('transitionClock', None)
                elif head > 0 or out['duration'] < span - before:
                    lead = 0 if head > 0 else before
                    origin, _, duration = (_fade_clock(base_clip, 'transitionClock')
                                           if base_clip.get('transitionClock') else (0, 1, span))
                    out['transitionBefore'] = lead
                    out['transitionDuration'] = min(span - before - head, out['duration']) + lead
                    out['transitionClock'] = dict(origin=origin + before + head - lead, rate=1, duration=duration)
        out['in'] = ang_in + (ov_start - offset)
        out.pop('multicamAngle', None)
        out.pop('multicamAudioAngle', None)
        if embedded_audio is not None:
            out['embeddedAudio'] = embedded_audio
        if kind == 'audio':
            out['_optionalAudio'] = True
        return out

    # Step 1: expand un-flattened multicam clips.
    after_mc = []
    for clip in prepared['clips']:
        if not isinstance(clip, dict) or clip.get('type') == 'title':
            after_mc.append(clip)
            continue
        item = media_by_id.get(clip.get('mediaId'))
        mc = item.get('multicam') if isinstance(item, dict) else None
        if not mc:
            after_mc.append(clip)
            continue
        if not isinstance(mc, dict) or not isinstance(mc.get('angles'), list) or not (1 <= len(mc['angles']) <= 9):
            raise ValueError('Invalid multicam clip: must have between 1 and 9 angles')
        angles = mc['angles']
        if any(not isinstance(a, dict) for a in angles):
            raise ValueError('Invalid multicam angle entry')
        angle_idx = clip.get('multicamAngle', 0)
        if type(angle_idx) is not int or not (0 <= angle_idx < len(angles)):
            raise ValueError(f'Invalid multicam angle index: {angle_idx}')
        audio_mode = mc.get('audioMode', 'camera1')
        if audio_mode not in ('camera1', 'follow', 'all', 'fixed'):
            raise ValueError(f'Invalid multicam audio mode: {audio_mode}')
        audio_angle = mc.get('audioAngle', 0)
        if type(audio_angle) is not int or not (0 <= audio_angle < len(angles)):
            raise ValueError(f'Invalid multicam audio angle index: {audio_angle}')
        clip_audio_override = clip.get('multicamAudioAngle')
        if clip_audio_override is not None:
            if type(clip_audio_override) is not int or not (0 <= clip_audio_override < len(angles)):
                raise ValueError(f'Invalid multicam clip audio angle index: {clip_audio_override}')
            audio_indices = [clip_audio_override]
        elif audio_mode == 'follow':
            audio_indices = [angle_idx]
        elif audio_mode == 'camera1':
            audio_indices = [0]
        elif audio_mode == 'fixed':
            audio_indices = [audio_angle]
        else:
            audio_indices = list(range(len(angles)))

        if clip.get('type') == 'video':
            if clip.get('embeddedAudio') is False:
                v_clip = angle_clip(clip, angles[angle_idx], 'video', clip['track'], embedded_audio=False)
                if v_clip:
                    after_mc.append(v_clip)
            elif audio_mode == 'follow' and clip_audio_override is None:
                v_clip = angle_clip(clip, angles[angle_idx], 'video', clip['track'])
                if v_clip:
                    after_mc.append(v_clip)
            else:
                v_clip = angle_clip(clip, angles[angle_idx], 'video', clip['track'], embedded_audio=False)
                if v_clip:
                    after_mc.append(v_clip)
                for idx in audio_indices:
                    a_clip = angle_clip(clip, angles[idx], 'audio', -64, suffix=f'-mca{idx}')
                    if a_clip:
                        after_mc.append(a_clip)
        elif clip.get('type') == 'audio':
            for idx in audio_indices:
                a_clip = angle_clip(clip, angles[idx], 'audio', clip['track'],
                                    suffix=(f'-mca{idx}' if len(audio_indices) > 1 else ''))
                if a_clip:
                    after_mc.append(a_clip)
        else:
            after_mc.append(clip)

    # Step 2: resolve subclips to their parent media + subclipIn offset.
    prepared['clips'] = [resolve_subclip_source(clip, media_by_id) for clip in after_mc]
    return prepared


def resolve_subclip_source(clip, media_by_id):
    """Translate a subclip's local In to the original media for file delivery."""
    if not isinstance(clip, dict) or clip.get('type') == 'title':
        return clip
    mid = clip.get('mediaId')
    if not (media_by_id.get(mid) or {}).get('parentMediaId'):
        return clip
    root, offset = _resolve_subclip(mid, media_by_id)
    return {**clip, 'mediaId': root, 'in': _number(clip.get('in', 0), 'source in') + offset}


def render_to_file(project, output_path, *, options=None, progress=None, cancelled=None, heartbeat=None,
                   work_directory=None, report=None, _nested_depth=0, _seen_sequences=frozenset(), _minimum_duration=0):
    """Resolve multicam clips, subclips, and nested sequences, then render the parent timeline.

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
    if _nested_depth>4: raise ValueError('Nested sequences exceed four levels')
    expanded = _expand_editorial_clips(project)
    if expanded is not project:
        # Missing angle footage contributes black and silence through the original edit's Out.
        _minimum_duration = _validate_timeline(project, _minimum_duration)
        project = expanded
    # Nested material used only by disabled clips is not rendered (it would contribute nothing).
    enabled_media = {clip.get('mediaId') for clip in project.get('clips') or [] if isinstance(clip, dict) and clip_enabled(clip)}
    if not any(isinstance(item,dict) and item.get('nestedProject') for item in project['media']):
        return _render_prepared(project,output_path,options=options,progress=progress,cancelled=cancelled,heartbeat=heartbeat,
                                work_directory=work_directory,report=report,minimum_duration=_minimum_duration)
    MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='splice-nested-',dir=MEDIA_ROOT) as directory:
        prepared=copy.deepcopy(project)
        for index,item in enumerate(prepared['media']):
            child=item.get('nestedProject')
            if not child or item.get('id') not in enabled_media: continue
            seq_id = item.get('nestedSequence')
            if type(seq_id) not in (int, str) or seq_id == '':
                seq_id = None
            if seq_id is not None and seq_id in _seen_sequences:
                raise ValueError('Circular nested sequence reference')
            if _nested_depth>=4: raise ValueError('Nested sequences exceed four levels')
            next_seen = _seen_sequences | ({seq_id} if seq_id is not None else set())
            codec = options.get('codec', 'h264')
            suffix = {'prores': '.mov', 'wav': '.wav'}.get(codec, '.mp4')
            target=Path(directory)/f'nested-{index}{suffix}'
            child_options={**options,'codec':codec}
            child_options.pop('range',None)
            span = _number(item.get('duration', 0), 'nested media duration', 0, MAX_TIMELINE_SECONDS) if isinstance(seq_id, str) else 0
            render_to_file(child,target,options=child_options,cancelled=cancelled,heartbeat=heartbeat,work_directory=work_directory,
                           _nested_depth=_nested_depth+1,_seen_sequences=next_seen,_minimum_duration=span)
            item['src']='/media/'+str(target.relative_to(MEDIA_ROOT))
            item['duration']=max(span, max((float(c['start'])+float(c['duration']) for c in child['clips']),default=0))
            item.pop('nestedSequence',None)
            item.pop('nestedProject',None)
            for clip in prepared['clips']:
                if clip.get('mediaId') != item['id']:
                    continue
                clip.pop('nestedSequence', None)
            if codec == 'wav':
                options.setdefault('_audioStems', set()).add(item['id'])
        return _render_prepared(prepared,output_path,options=options,progress=progress,cancelled=cancelled,heartbeat=heartbeat,
                                work_directory=work_directory,report=report,minimum_duration=_minimum_duration)


def _track_hidden(track_state, kind, track):
    # Mirror the editor: a title track without its own state follows the video track.
    state = track_state.get(f'{kind}:{track}')
    if state is None and kind == 'title':
        state = track_state.get(f'video:{track}')
    return bool(state and state.get('hidden') is True)


def _export_settings(options):
    codec = options.get('codec','h264')
    if codec not in ('h264','prores','wav'): raise ValueError('Unsupported export codec')
    color_space = options.get('colorSpace','source')
    if color_space!='source' and (codec!='prores' or color_space not in ('rec709','pq')):
        raise ValueError('Invalid export color space')
    enabled = options.get('limiter', True)
    if not isinstance(enabled, bool): raise ValueError('Invalid limiter setting: use true or false')
    ceiling = _number(options.get('limiterCeiling', DEFAULT_LIMITER_CEILING_DB), 'limiter ceiling (dBFS)', -24, 0)
    peak_safe = options.get('aacPeakSafe', False)
    if not isinstance(peak_safe, bool): raise ValueError('Invalid aacPeakSafe setting: use true or false')
    _number(options.get('crf',20),'quality',14,32)
    _number(options.get('audioBitrate',192),'audio bitrate',64,320)
    language = options.get('subtitleLanguage', 'und')
    if not isinstance(language, str) or len(language) != 3 or not language.isascii() or not language.isalpha() or not language.islower():
        raise ValueError('Invalid subtitle language: use a three-letter ISO 639-2 code such as eng, spa or und')
    return codec, color_space, (ceiling if enabled else None), language, peak_safe


def _validate_timeline(project, minimum_duration=0):
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
    total = _number(minimum_duration, 'nested media duration', 0, MAX_TIMELINE_SECONDS)
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


def _estimated_bytes(width, height, fps, seconds, codec):
    bits_per_pixel = 3.6 if codec == 'prores' else .3
    video = 0 if codec == 'wav' else width*height*fps*seconds*bits_per_pixel/8
    return video, seconds*AUDIO_RATE*2*4


def _render_prepared(project, output_path, *, options=None, progress=None, cancelled=None, heartbeat=None, work_directory=None,
                     report=None, minimum_duration=0):
    """Render bounded picture windows and audio chunks, then mux them in one pass."""
    import copy
    options = dict(options or {})
    codec, color_space, limiter, language, peak_safe = _export_settings(options)
    # One private copy: probe results are cached on its media items.
    project = copy.deepcopy(project)
    total = _validate_timeline(project, minimum_duration)
    width, height = _frame_size(project) if codec != 'wav' else (16, 16)
    fps_fraction = _sequence_fps(project)
    fps = float(fps_fraction)
    total = math.ceil((total-1e-9)*fps)/fps
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
    probes = options['_probes'] = {}
    suffix='.mov' if codec=='prores' else '.mp4'
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
    media_by_id = {item['id']:item for item in project['media']}
    render_options = project.get('renderOptions') or {}
    if not isinstance(render_options, dict) or not isinstance(render_options.get('mutedTracks', {}), dict):
        raise ValueError('Invalid render options')
    muted = render_options.get('mutedTracks', {})
    profiles = set()
    video_clips = sorted((clip for clip in picture_clips if clip.get('type')=='video'),
                         key=lambda clip:(clip.get('track',0),float(clip['start'])))
    if codec!='wav' and not muted.get('video'):
        for identity in _picture_range_media(video_clips,media_by_id,fps,first,stop):
            item = media_by_id.get(identity)
            if item is None or item.get('nativeGenerator') is not None: continue
            _media_info(item, probes, heartbeat)
            if not item.get('_renderStill'):
                profiles.add(item['_sourceProfile'])
    hdr = any(profile[1] in HDR_TRANSFERS for profile in profiles)
    if hdr and codec=='prores' and len(profiles)!=1 and color_space=='source':
        raise ValueError('A ProRes HDR master needs one HDR color space. Select Rec. 2100 PQ or Rec. 709 to combine HDR and SDR sources.')
    profile = next(iter(profiles)) if len(profiles)==1 else None
    if profile and not all(value and value not in ('unknown','unspecified') for value in profile):profile=None
    # Untagged or mixed SDR sources are delivered as explicitly tagged Rec. 709.
    if profile is None or color_space=='rec709' or hdr and codec=='h264': profile=REC709
    if color_space=='pq': profile=PQ
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
    def srt_time(seconds):
        ms=round(seconds*1000)
        return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'
    track_mix=project.get('trackMix') or {}
    if not isinstance(track_mix, dict): raise ValueError('Invalid track mix')
    solo=any(v.get('solo') for v in track_mix.values() if isinstance(v,dict))
    first_sample = round(Fraction(first)*AUDIO_RATE/fps_fraction)
    stop_sample = round(Fraction(stop)*AUDIO_RATE/fps_fraction)
    if muted.get('audio'):
        events = []
    else:
        events = _audio_events(project['clips'], media_by_id, fps, probes, track_mix, solo,
                               (max(0, out_start-AUDIO_PREROLL_SECONDS), out_end), heartbeat, options.get('_audioStems', ()))
    root = Path(work_directory) if work_directory else work_root()
    video_bytes, audio_bytes = _estimated_bytes(width, height, fps, out_duration, codec)
    # A speed-changed or resampled clip that crosses a chunk seam is rendered
    # whole first (32-bit float stereo). Count the most of those files that are
    # on disk at once, following the mix's own schedule (overlapping ones add up).
    # Lossy deliveries also keep an AAC file until the mux.
    audio_bytes += _prerender_peak_bytes(events, first_sample, stop_sample)
    if codec == 'h264':
        audio_bytes += out_duration*40_000
    output = Path(output_path)
    final_bytes = video_bytes + (out_duration*AUDIO_RATE*2*3 if codec != 'h264' else out_duration*40_000)
    same_volume = output.parent.exists() and os.stat(output.parent).st_dev == os.stat(root).st_dev
    _ensure_space(root, video_bytes+audio_bytes+(final_bytes if same_volume else 0), 'this export')
    if not same_volume and output.parent.exists():
        _ensure_space(output.parent, final_bytes, 'the exported file')
    def stage(low, high):
        return (lambda fraction: progress(low+(high-low)*fraction)) if progress else None
    with WorkDirectory('splice-segments-', root) as directory:
        if codec=='wav':
            chunks = _render_audio(events, first_sample, stop_sample, None if limiter is None else (limiter, False),
                                   directory, cancelled, heartbeat, stage(0, .95))
            _run_ffmpeg(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0',
                         '-i',str(_concat_list(directory,'audio.txt',chunks)),'-c:a','pcm_s24le','-ar',str(AUDIO_RATE),
                         str(output)],cancelled,heartbeat)
            if progress: progress(1.0)
            return output.stat().st_size
        segments = _render_picture(project, picture_clips, first, stop, total_frames, fps, width, height, codec, options,
                                   directory, suffix, cancelled, heartbeat, stage(0, .8))
        lossy = codec != 'prores'
        mix_limiter = None if limiter is None else (limiter, lossy)
        chunks = _render_audio(events, first_sample, stop_sample, mix_limiter, directory, cancelled, heartbeat, stage(.8, .92))
        audio_list = _concat_list(directory,'audio.txt',chunks)
        if lossy:
            bitrate = str(int(_number(options.get('audioBitrate',192),'audio bitrate',64,320)))+'k'
            audio_file, measured = _encode_aac(audio_list, bitrate, limiter, peak_safe, directory, cancelled, heartbeat)
            if report is not None and measured:
                report['audio'] = measured
            audio_input = ['-i',str(audio_file)]
        else:
            audio_input = ['-f','concat','-safe','0','-i',str(audio_list)]
        if progress: progress(.96)
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
                 '-f','concat','-safe','0','-i',str(_concat_list(directory,'segments.txt',segments)),
                 *audio_input]
        with_captions = validated_captions and options.get('captions',True)
        if with_captions:
            subtitle=Path(directory)/'captions.srt'
            subtitle.write_text(''.join(f'{index}\n{srt_time(start)} --> {srt_time(end)}\n{text.replace(chr(13)," ").replace("-->","→").strip()}\n\n' for index,(start,end,text) in enumerate(validated_captions,1)),encoding='utf-8')
            command.extend(['-i',str(subtitle)])
        command.extend(['-map','0:v:0','-map','1:a:0'])
        if with_captions: command.extend(['-map','2:s:0'])
        command.extend(['-c:v','copy'])
        if codec=='prores':
            command.extend(['-c:a','pcm_s24le','-ar',str(AUDIO_RATE)])
        else:
            command.extend(['-c:a','copy'])
        if with_captions:
            command.extend(['-c:s','mov_text','-metadata:s:s:0',f'language={language}','-disposition:s:0','default'])
        command.extend(_color_tags(profile, codec))
        flags = '+write_colr' if codec=='prores' else '+faststart+write_colr'
        # The segments and chunks already have the exact frame and sample
        # counts; -t only guards against a stray packet, so give it half a
        # frame (a -t of the exact duration drops the last audio sample).
        command.extend(['-t',f'{out_duration+.5/fps:.6f}','-movflags',flags,str(output)])
        _run_ffmpeg(command,cancelled,heartbeat)
        if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
        if progress: progress(1.0)
        return output.stat().st_size


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


def _render_picture(project, picture, first, stop, total_frames, fps, width, height, codec, options, directory,
                    suffix, cancelled, heartbeat, progress):
    """Render the picture in bounded windows; returns the segment files in order."""
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
    frame_bytes = width*height*_alpha_bytes_per_pixel(codec=='prores')
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
    segments = []
    for index,(a_frame,b_frame) in enumerate(windows):
        if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
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
                if overlap_start > cut+.5/fps:
                    clip['transition']='none'
                    clip.pop('transitionClock', None)
                clip['_keyframeShift']=overlap_start-cut
            if id(original) in dip_format: clip['_dipFormat'] = True
            if id(original) in wipe_mask: clip['_wipeMask'] = True
            clips.append(clip)
        # Gaps still need a black base track to keep exact segment duration.
        clips.append({'type':'title','start':0,'duration':end-begin,'in':0,'title':''})
        used = {clip.get('mediaId') for clip in clips}
        segment = {**project, 'clips': clips, 'media': [media_by_id[identity] for identity in used if identity in media_by_id]}
        target=Path(directory)/f'{index:05d}{suffix}'
        output_window = (max(first,a_frame)-a_frame,min(stop,b_frame)-a_frame)
        window_options={**options,'_output_frames':output_window if output_window!=(0,b_frame-a_frame) else None}
        _render_window(segment,target,directory,options=window_options,cancelled=cancelled)
        segments.append(target)
        if heartbeat: heartbeat()
        if progress: progress((index+1)/len(windows))
    return segments
