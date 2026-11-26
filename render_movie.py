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
import audio_dsp
import audio_loudness


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
# ranges its chunk needs. Events with state (tempo, resampling, effects) are
# pre-rendered whole, so the chunks themselves only trim, gain, pan and sum;
# the program is limited and normalized as a whole afterwards (audio_loudness).
AUDIO_CHUNK_SECONDS = 60
AUDIO_CHUNK_EVENTS = 32
AUDIO_PREROLL_SECONDS = 0.5   # a chunk starts this early (a range export's first events)
AUDIO_POSTROLL_SECONDS = 0.1  # and reads this far past its last sample
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
# Every delivery is true-peak limited (4x oversampled, audio_loudness). AAC
# adds overshoot of its own (about 2 dB on some programs): the decoded file is
# measured and reported, with a warning when it is over the ceiling. Only the
# opt-in aacPeakSafe option lowers the whole program to fit, in fixed steps
# from the measured excess.
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


def _trim_audio(filters, label, index, streams, start, length, reverse, rates=None, offset=0.0):
    """Trim a source span. ``start`` is relative to the input's seek ``offset``
    (whole seconds); both ends are snapped to the stream's own sample grid, so
    the same source sample starts the slice however the input was shared."""
    rates = rates or {}

    def grid(number, value):
        rate = rates.get(number) or AUDIO_RATE
        return round((offset+value)*rate)/rate-offset
    if len(streams) > 1:
        from media_proxy import stereo_mix_filters
        sources = []
        for number in streams:
            first, last = grid(number, start), grid(number, start+length)
            # A stream can start later than the video's source clock. Preserve the gap.
            sources.append(f'[{index}:a:{number}]atrim=start={first:.12f}:end={last:.12f},asetpts=PTS-({first:.12f})/TB,'
                           'aresample=48000:first_pts=0')
        filters.extend(stereo_mix_filters(sources, label, list(streams.values()), length))
        result = f'[{label}]'
    else:
        number = next(iter(streams))
        first, last = grid(number, start), grid(number, start+length)
        result = (f'[{index}:a:{number}]atrim=start={first:.12f}:end={last:.12f},asetpts=PTS-({first:.12f})/TB,'
                  'aresample=48000:first_pts=0,')
    return result + ('areverse,' if reverse else '')


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
            filters.append(f'[without{number}][clip{number}]overlay={position}:{overlay}[incoming{number}]')
            end = render_start+transition_duration
            progress = f'(T-{render_start:.9f})/{transition_duration:.9f}'
            # A wipe to a disabled clip uncovers the layers beneath from the left.
            mix = f'if(lt(X,W*{progress}),B,A)' if wipe_off else f'A*(1-{progress})+B*{progress}'
            # (The wipe's last frame is fully uncovered: compare with 1 us of slack, as the wipe mask does.)
            finish = end-1e-6 if wipe_off else end
            blend = f'if(lte(T,{render_start:.9f}),A,if(gte(T,{finish:.9f}),B,{mix}))'
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


SOUND_TRANSITIONS = {'dissolve': 'crossfade', 'wipe': 'crossfade', 'fade': 'dip'}
SOUND_OVERRIDES = ('auto', 'cut', 'crossfade', 'dip')
CHANNEL_MAPS = {
    'stereo': None,
    'left': 'pan=stereo|c0=c0|c1=c0',
    'right': 'pan=stereo|c0=c1|c1=c1',
    'mono': 'pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1',
    'swap': 'pan=stereo|c0=c1|c1=c0',
}
# Where a track sits in a 5.1 program (SMPTE order L R C LFE Ls Rs). A mono
# sum (0.7071*(L+R)) gives a centre-panned mono clip its own level.
TRACK_ROUTES = {
    'front': 'c0=c0|c1=c1|c2=0*c0|c3=0*c0|c4=0*c0|c5=0*c0',
    'center': 'c0=0*c0|c1=0*c0|c2=0.70710678*c0+0.70710678*c1|c3=0*c0|c4=0*c0|c5=0*c0',
    'lfe': 'c0=0*c0|c1=0*c0|c2=0*c0|c3=0.70710678*c0+0.70710678*c1|c4=0*c0|c5=0*c0',
    'surround': 'c0=0*c0|c1=0*c0|c2=0*c0|c3=0*c0|c4=c0|c5=c1',
}
AUDIO_LAYOUTS = ('stereo', '5.1', '5.1-downmix')
AUDIO_ONLY_CODECS = ('wav', 'stems', 'loudness')
# ITU-R BS.775 stereo fold-down of a 5.1 program (LFE omitted).
DOWNMIX = 'pan=stereo|c0=c0+0.70710678*c2+0.70710678*c4|c1=c1+0.70710678*c2+0.70710678*c5'
MAX_TRACK_KEYFRAMES = 2000
# Stateful events (tempo, resampling, effects) are pre-rendered from a point
# on this event-local grid, so the result does not depend on chunking.
PRERENDER_ANCHOR_SECONDS = 60
PRERENDER_WORKERS = 4


def _sound_transition(clip, pictures, fps):
    """How a clip's sound enters at its start. One rule for embedded and linked
    sound: dissolve and wipe crossfade, a dip fades the outgoing sound out over
    the part before the cut and the incoming one in over the part after it, a
    cut cuts. A linked sound clip without a transition of its own follows its
    picture; ``audioTransition`` (cut, crossfade or dip) overrides the rule."""
    override = clip.get('audioTransition', 'auto')
    if override not in SOUND_OVERRIDES:
        raise ValueError('Invalid sound transition: use auto, cut, crossfade or dip')
    source = clip
    if clip.get('type') == 'audio' and clip.get('transition') not in SOUND_TRANSITIONS and clip.get('linkGroup'):
        source = pictures.get((clip['linkGroup'], round(float(clip['start'])*fps)), clip)
    kind = SOUND_TRANSITIONS.get(source.get('transition'))
    if override == 'cut':
        return None
    if override in ('crossfade', 'dip'):
        kind = override
    if kind is None:
        return None
    duration = _number(source.get('transitionDuration', .5), 'transition duration', 1/float(fps))
    before = min(_number(source.get('transitionBefore', 0), 'transition lead'), duration)
    return dict(kind=kind, duration=duration, before=before, power=source.get('transitionMatchName') == 'Constant Power')


def _transition_curve(power, expression):
    return f'sin(PI*({expression})/2)' if power else expression


def _automation(points, key, low, high, limit):
    """Validated keyframes [(time, value, ease)] sorted by time."""
    if points is None:
        return []
    if not isinstance(points, list): raise ValueError(f'Invalid {key} keyframes')
    if len(points) > limit: raise ValueError(f'More than {limit} {key} keyframes, the export limit')
    ordered = []
    for point in points:
        if not isinstance(point, dict): raise ValueError(f'Invalid {key} keyframe')
        ease = point.get('ease', False)
        if not isinstance(ease, bool): raise ValueError(f'Invalid {key} keyframe: ease must be true or false')
        ordered.append((_number(point.get('time'), f'{key} keyframe time', 0, MAX_TIMELINE_SECONDS),
                        _number(point.get('value'), f'{key} keyframe value', low, high), ease))
    ordered.sort()
    if any(b[0] <= a[0] for a, b in zip(ordered, ordered[1:])): raise ValueError(f'Duplicate {key} keyframes')
    return ordered


def _curve_expr(ordered, clock, base):
    """FFmpeg expression of a keyframe curve at ``clock``: linear or eased
    (smoothstep 3u^2-2u^3) segments, held before the first and after the last key.
    Mirrors engine.js curveValue."""
    if not ordered:
        return f'{base:.6f}'
    expr = f'{ordered[-1][1]:.9f}'
    for (at, value, ease), (next_at, next_value, _) in reversed(list(zip(ordered, ordered[1:]))):
        fraction = f'(({clock})-{at:.9f})/{next_at-at:.9f}'
        if ease:
            fraction = f'({fraction})*({fraction})*(3-2*({fraction}))'
        expr = f'if(lte({clock},{next_at:.9f}),{value:.9f}+({next_value-value:.9f})*{fraction},{expr})'
    return f'if(lte({clock},{ordered[0][0]:.9f}),{ordered[0][1]:.9f},{expr})'


def _curve_window(ordered, begin, end):
    """Only the keys that shape begin..end (one either side): short expressions per event."""
    if not ordered:
        return ordered
    first = max(0, next((index for index, point in enumerate(ordered) if point[0] >= begin), len(ordered))-1)
    last = next((index for index, point in enumerate(ordered) if point[0] > end), len(ordered)-1)
    return ordered[first:last+1]


def _pan_expressions(pan_expr, mono):
    """Web Audio StereoPannerNode for a time-varying pan (-1..1) as aeval channels."""
    p = f'({pan_expr})'
    if mono:
        return (f'cos(({p}+1)*PI/4)*val(0)', f'sin(({p}+1)*PI/4)*val(0)')
    return (f'if(lte({p},0),val(0)+cos(({p}+1)*PI/2)*val(1),cos({p}*PI/2)*val(0))',
            f'if(lte({p},0),sin(({p}+1)*PI/2)*val(1),val(1)+sin({p}*PI/2)*val(0))')


def _track_settings(track_mix, track):
    mix = track_mix.get(str(track), {})
    if not isinstance(mix, dict): raise ValueError('Invalid track mix')
    return mix


def _track_label(track):
    number = int(track)
    return f'A{-number}' if number < 0 else f'V{number}'


def track_plan(track_mix, track):
    """Validated processing of one audio track (cached per export by the caller)."""
    mix = _track_settings(track_mix, track)
    fx = audio_dsp.normalize(mix.get('fx'), label=f'track {_track_label(track)} effects')
    keys = mix.get('keyframes', {})
    if not isinstance(keys, dict) or any(key not in ('volume', 'pan') for key in keys):
        raise ValueError('Invalid track automation: use volume and pan keyframes')
    route = mix.get('route', 'front')
    if not isinstance(route, str) or route not in TRACK_ROUTES: raise ValueError('Invalid track routing: use ' + ', '.join(TRACK_ROUTES))
    channels = mix.get('channels', 'stereo')
    if channels not in ('stereo', 'mono'): raise ValueError('Invalid track channels: use stereo or mono')
    stem = mix.get('stem', '')
    if not isinstance(stem, str) or len(stem) > 32 or any(ord(char) < 32 for char in stem):
        raise ValueError('Invalid stem name: use up to 32 printable characters')
    return dict(fx=fx, dynamics=audio_dsp.has_dynamics(fx), route=route, mono=channels == 'mono', stem=stem.strip(),
                gain=_number(mix.get('gain', 100), 'track gain', 0, 200)/100,
                pan=_number(mix.get('pan', 0), 'track pan', -100, 100),
                volume_keys=_automation(keys.get('volume'), 'track volume', 0, 200, MAX_TRACK_KEYFRAMES),
                pan_keys=_automation(keys.get('pan'), 'track pan', -100, 100, MAX_TRACK_KEYFRAMES))


def _audio_events(clips, media_by_id, fps, probes, track_mix, solo, span, heartbeat=None, *, layout='stereo', tracks=None,
                  plans=None):
    """Describe each audible clip as one timeline event: its body, any crossfade
    lead-in before its start and any crossfade tail after its end, rendered by
    one chain (one tempo filter), so a speed change never restarts at the cut.

    Only events that overlap ``span`` (timeline seconds) are returned, so a
    range export probes and reads only the media it needs. ``tracks`` limits
    the events to those audio track keys (stems, track groups).
    """
    begin, end = span
    fps = float(fps)
    pictures = {(clip['linkGroup'], round(float(clip['start'])*fps)): clip for clip in clips
                if clip.get('type') == 'video' and clip.get('linkGroup')}
    upcoming = {}
    for candidate in clips:
        if candidate.get('type') in ('audio', 'video'):
            sound = _sound_transition(candidate, pictures, fps)
            if sound:
                upcoming.setdefault((candidate['type'], candidate.get('track'), round(float(candidate['start'])*fps)),
                                    (candidate, sound))
    plans = {} if plans is None else plans
    events = []
    for clip in clips:
        if clip.get('type') not in ('audio', 'video') or not clip_enabled(clip):
            # A disabled clip is silent (its embedded sound too). It stays in
            # `upcoming`, so a neighbour's crossfade still fades against it.
            continue
        item = media_by_id.get(clip.get('mediaId'))
        if item is None:
            raise ValueError('A clip references missing media')
        if item.get('nativeGenerator') in (1112293707, 1414680150): continue
        key = str(clip.get('track', -1))
        if tracks is not None and key not in tracks: continue
        mix = _track_settings(track_mix, key)
        if mix.get('mute') or (solo and not mix.get('solo')): continue
        if clip['type'] == 'video' and clip.get('embeddedAudio') is False: continue
        if key not in plans: plans[key] = track_plan(track_mix, key)
        plan = plans[key]
        source_in = float(clip.get('in', 0)); length = float(clip['duration']); start = float(clip['start'])
        speed = _number(clip.get('speed', 1), 'speed', .1, 16)
        reverse = bool(clip.get('reverse'))
        sound = _sound_transition(clip, pictures, fps)
        before = sound['before'] if sound and sound['kind'] == 'crossfade' else 0.0
        # A dip starts the incoming sound at the cut; only a crossfade pre-rolls it.
        render_length = length+before
        following = upcoming.get((clip['type'], clip.get('track'), round((start+length)*fps)))
        if following and following[0] is clip: following = None
        fade_out = min(length, _number(clip.get('fadeOut', 0), 'fade out', 0, MAX_TIMELINE_SECONDS))
        after = 0.0
        if following:
            next_clip, next_sound = following
            next_duration = min(next_sound['duration'], next_sound['before']+float(next_clip['duration']))
            if next_sound['kind'] == 'crossfade' and not fade_out:
                after = max(0.0, next_duration-next_sound['before'])
        if after < 1/fps: after = 0.0
        if start-before >= end or start+length+after <= begin:
            continue
        _check_clip(clip, media_by_id, probes, heartbeat)
        if 'audio' not in item['_kinds']:
            continue
        clip_keys = _automation((clip.get('keyframes') or {}).get('volume'), 'volume', 0, 200, MAX_KEYFRAMES)
        # Keyframes replace the static level they animate (clip volume, track volume).
        static = ((1.0 if clip_keys else _number(clip.get('volume', 100), 'volume', 0, 200)/100) *
                  (1.0 if plan['volume_keys'] else plan['gain']))
        streams = dict(enumerate(item['_audioStreams']))
        selection = clip.get('audioStream', item.get('audioStream'))
        if selection is not None:
            if type(selection) is not int or selection not in streams:
                raise ValueError('The selected source audio stream is unavailable')
            streams = {selection: streams[selection]}
        rates = item.get('_audioRates') or []
        stream_rates = {number: (rates[number] if number < len(rates) and rates[number] else AUDIO_RATE) for number in streams}
        mapping = clip.get('channelMap', 'stereo')
        if not isinstance(mapping, str) or mapping not in CHANNEL_MAPS: raise ValueError('Invalid channel mapping: use ' + ', '.join(CHANNEL_MAPS))
        source_channels = 2 if len(streams) > 1 else next(iter(streams.values()))
        mono = source_channels == 1 or plan['mono']
        mapping_filter = None
        if source_channels != 1 and (plan['mono'] or CHANNEL_MAPS[mapping]):
            filters = ['aformat=channel_layouts=stereo']
            if CHANNEL_MAPS[mapping]:
                filters.append(CHANNEL_MAPS[mapping])
            if plan['mono']:
                filters.append('pan=mono|c0=0.5*c0+0.5*c1')
            mapping_filter = ','.join(filters)
        clip_pan = _number(clip.get('pan', 0), 'pan', -100, 100)
        fx = audio_dsp.normalize(clip.get('audioFx'), label='clip audio effects')
        processing = [*([mapping_filter] if mapping_filter else []),
                      *tempo_filters(speed), *audio_dsp.ffmpeg_filters(fx), f'volume={static:.6f}']
        timeline0 = start-before   # timeline time of event-local t = 0
        pan_keys = _curve_window(plan['pan_keys'], timeline0, start+length+after)
        pan = None
        if pan_keys:
            pan = (lambda t, keys=pan_keys, offset=timeline0, own=clip_pan, mono=mono:
                   _pan_expressions(f'max(-1,min(1,({own:.6f}+{_curve_expr(keys, f"({t}+{offset:.9f})", 0)})/100))', mono))
            processing.append('aformat=channel_layouts=stereo' if not mono else 'aformat=channel_layouts=mono')
        else:
            processing.append(_stereo_pan(max(-1, min(1, (clip_pan+plan['pan'])/100)), 1 if mono else 2))
        envelope = []
        if clip_keys:
            window = _curve_window(clip_keys, -before, length+after)
            envelope.append(lambda t, keys=window, before=before: f"({_curve_expr(keys, f'({t}-{before:.9f})', 100)})/100")
        volume_keys = _curve_window(plan['volume_keys'], timeline0, start+length+after)
        if volume_keys:
            envelope.append(lambda t, keys=volume_keys, offset=timeline0: f"({_curve_expr(keys, f'({t}+{offset:.9f})', 100)})/100")
        if sound:
            if sound['kind'] == 'crossfade':
                incoming = min(sound['duration'], render_length)
            else:
                incoming = min(sound['duration']-sound['before'], length)
            if incoming > 1e-9:
                envelope.append(lambda t, power=sound['power'], incoming=incoming:
                                _transition_curve(power, f'min(1,max(0,{t})/{incoming:.9f})'))
        if following:
            next_clip, next_sound = following
            if next_sound['kind'] == 'crossfade':
                fade_start = before+length-next_sound['before']
                next_duration = min(next_sound['duration'], next_sound['before']+float(next_clip['duration']))
                envelope.append(lambda t, power=next_sound['power'], s=fade_start, d=next_duration:
                                _transition_curve(power, f'max(0,min(1,1-({t}-{s:.9f})/{d:.9f}))'))
            elif next_sound['before'] > 1e-9:
                cut, lead = before+length, next_sound['before']
                envelope.append(lambda t, power=next_sound['power'], cut=cut, lead=lead:
                                _transition_curve(power, f'max(0,min(1,({cut:.9f}-{t})/{lead:.9f}))'))
        fade_in = min(length, _number(clip.get('fadeIn', 0), 'fade in', 0, MAX_TIMELINE_SECONDS))
        if fade_in or fade_out:
            head = (lambda t, b=before, f=fade_in: f'max(0,({t}-{b:.9f})/{f:.9f})') if fade_in else (lambda t: '1')
            tail = (lambda t, r=render_length, f=fade_out: f'max(0,({r:.9f}-{t})/{f:.9f})') if fade_out else (lambda t: '1')
            envelope.append(lambda t, head=head, tail=tail: f'min(1,min({head(t)},{tail(t)}))')
        post = []
        if not plan['dynamics']:
            post.extend(audio_dsp.ffmpeg_filters(plan['fx']))
        if layout != 'stereo':
            post.append('aformat=channel_layouts=stereo,pan=5.1|' + TRACK_ROUTES[plan['route']])
        total = render_length+after
        if before and not item.get('_renderStill'):
            if not reverse and source_in-before*speed < -.01:
                raise ValueError('Incoming sound lacks pre-cut footage for its crossfade')
            if reverse and source_in+(length+before)*speed > item['_actualDuration']+.05:
                raise ValueError('Incoming sound lacks pre-cut footage for its crossfade')
        if after:
            available = (source_in/speed if reverse else (item['_actualDuration']-source_in-length*speed)/speed)
            if available+1e-3 < after: raise ValueError('Audio crossfade needs more outgoing source footage')
        # Tempo, resampling and effects carry state from sample to sample: such an
        # event is always rendered in one piece (see _audio_schedule).
        stateful = (abs(speed-1) > 1e-9 or any(rate != AUDIO_RATE for rate in stream_rates.values())
                    or bool(fx) or (bool(plan['fx']) and not plan['dynamics']))
        events.append(dict(path=item['_path'], streams=streams, rates=stream_rates,
                           source=source_in-after*speed if reverse else source_in-before*speed,
                           length=total, speed=speed, reverse=reverse, start=timeline0,
                           processing=','.join(processing), envelope=envelope, pan=pan, post=','.join(post),
                           stateful=stateful, track=key))
    events.sort(key=lambda event: event['start'])
    return events


def _layout_channels(layout):
    return 2 if layout == 'stereo' else 6


def _layout_name(layout):
    return 'stereo' if layout == 'stereo' else '5.1'


def _envelope_filter(event, clock):
    """The event's gain envelope and any pan automation as one aeval."""
    terms = [term(clock) for term in event['envelope']]
    gain = '*'.join(terms)
    if event.get('pan'):
        left, right = event['pan'](clock)
        if gain:
            left, right = f'({left})*{gain}', f'({right})*{gain}'
        return f",aeval=exprs='{left}|{right}':channel_layout=stereo"
    if gain:
        return f",aeval=exprs='val(ch)*{gain}':channel_layout=stereo"
    return ''


def _seek_offset(low):
    """Inputs are seeked to whole seconds (on every sample grid), so a slice's
    first sample is the same whichever slices share the input."""
    return float(max(0, math.floor(low-.25)))


def _render_audio_chunk(events, origin_sample, keep_from, keep_samples, target, directory, cancelled, heartbeat,
                        layout='stereo'):
    """Mix the events that touch one chunk into a float WAV of exactly keep_samples samples."""
    origin = origin_sample/AUDIO_RATE
    chunk_end = (origin_sample+keep_from+keep_samples)/AUDIO_RATE+AUDIO_POSTROLL_SECONDS
    channels, name = _layout_channels(layout), _layout_name(layout)
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
        offset = _seek_offset(run['low'])
        for number in run['numbers']:
            offsets[number] = (run['index'], offset)
        seek = ['-ss', f'{offset:.0f}'] if offset > 0 else []
        path = slices[run['numbers'][0]][0]['path']
        arguments.append([*seek, '-t', f"{run['high']-offset+.25:.6f}", '-protocol_whitelist', 'file', '-i', str(path)])
    filters, labels = [], []
    # Every value below comes from this slice's own tuple: nothing is carried
    # over from the loop above (a stale end silenced speed-changed clips).
    for number, (event, u0, u1, low, high) in enumerate(slices):
        index, offset = offsets[number]
        chain = _trim_audio(filters, f'mix{number}', index, event['streams'], low-offset, high-low, event['reverse'],
                            event.get('rates'), offset)
        chain += event['processing']
        delay = round((event['start']+u0-origin)*AUDIO_RATE)
        if abs(event['speed']-1) > 1e-9:
            # The tempo filter was given extra source past the slice (it drops
            # its last fragment); keep exactly the slice's own samples.
            chain += f",apad,atrim=end_sample={max(1, round((event['start']+u1-origin)*AUDIO_RATE)-max(0, delay))}"
        chain += _envelope_filter(event, f'(t+{u0:.9f})' if u0 else 't')
        if event.get('post'):
            chain += ',' + event['post']
        filters.append(chain + f',aformat=channel_layouts={name},adelay={max(0, delay)}S:all=1[audio{number}]')
        labels.append(f'[audio{number}]')
    total = keep_from+keep_samples
    if labels:
        mix = f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0"
    else:
        mix = f'anullsrc=r={AUDIO_RATE}:cl={name}'
    filters.append(f'{mix},aformat=sample_fmts=flt:sample_rates={AUDIO_RATE}:channel_layouts={name},'
                   f'apad,atrim=start_sample={keep_from}:end_sample={total},asetpts=PTS-STARTPTS[aout]')
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               *(argument for entry in arguments for argument in entry),
               *_graph_args(';'.join(filters), directory, Path(target).stem), '-map', '[aout]',
               '-c:a', 'pcm_f32le', '-ar', str(AUDIO_RATE), '-ac', str(channels), str(target)]
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


def _prerender_event(event, u0, u1, target, directory, cancelled, heartbeat, layout='stereo'):
    """Render event-local u0..u1 of a stateful event (tempo, resampling, effects)
    in one FFmpeg run, reading only that span of its source, envelope and
    track processing included. Returns a plain 48 kHz event that chunks can
    slice without restarting any filter state."""
    low, high = _event_source_span(event, u0, u1)
    offset = _seek_offset(low)
    seek = ['-ss', f'{offset:.0f}'] if offset > 0 else []
    channels, name = _layout_channels(layout), _layout_name(layout)
    filters = []
    chain = _trim_audio(filters, 'pre', 0, event['streams'], low-offset, high-low, event['reverse'], event.get('rates'), offset)
    samples = math.ceil((u1-u0)*AUDIO_RATE)+2
    chain += event['processing'] + f',aformat=sample_fmts=flt:sample_rates={AUDIO_RATE},apad,atrim=end_sample={samples}'
    chain += _envelope_filter(event, f'(t+{u0:.9f})' if u0 else 't')
    if event.get('post'):
        chain += ',' + event['post']
    filters.append(chain + f',aformat=sample_fmts=flt:sample_rates={AUDIO_RATE}:channel_layouts={name},asetpts=PTS-STARTPTS[aout]')
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               *seek, '-t', f'{high-offset+.25:.6f}', '-protocol_whitelist', 'file', '-i', str(event['path']),
               *_graph_args(';'.join(filters), directory, Path(target).stem), '-map', '[aout]',
               '-c:a', 'pcm_f32le', '-ar', str(AUDIO_RATE), '-ac', str(channels), str(target)]
    _run_ffmpeg(command, cancelled, heartbeat)
    return dict(path=Path(target), streams={0: channels}, rates={0: AUDIO_RATE}, source=0.0, length=u1-u0, speed=1.0,
                reverse=False, start=event['start']+u0, processing='anull', envelope=[], pan=None, post='',
                stateful=False, prerendered=True, track=event.get('track'))


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
    events that join at this chunk. Every stateful event (tempo, resampling,
    effects) is pre-rendered when it arrives, from a point on a fixed
    event-local grid, so its waveform does not depend on where chunks fall."""
    chunks = _audio_chunk_spans(events, first_sample, stop_sample)
    preroll = round(AUDIO_PREROLL_SECONDS*AUDIO_RATE)
    origins = [max(0, begin-preroll) for begin, _ in chunks]
    last_needed = stop_sample/AUDIO_RATE+AUDIO_POSTROLL_SECONDS
    first_needed = origins[0]/AUDIO_RATE if origins else 0.0
    pointer = 0
    for number, (begin, end) in enumerate(chunks):
        origin = origins[number]
        arrivals = []
        while pointer < len(events) and events[pointer]['start'] < end/AUDIO_RATE+AUDIO_POSTROLL_SECONDS:
            event = events[pointer]; pointer += 1
            span = None
            reversed_span = event['length']*event['speed'] if event['reverse'] else 0
            if event.get('stateful') and reversed_span <= AUDIO_REVERSE_PRERENDER_SECONDS:
                needed = max(0.0, first_needed-event['start'])
                u0 = math.floor(needed/PRERENDER_ANCHOR_SECONDS)*PRERENDER_ANCHOR_SECONDS
                u1 = min(event['length'], last_needed-event['start'])
                if u1-u0 > 1/AUDIO_RATE:
                    span = (u0, u1)
            arrivals.append((event, span))
        yield number, begin, end, origin, arrivals, len(chunks)


def _prerender_bytes(u0, u1, channels=2):
    """Size of a pre-rendered event file (32-bit float WAV)."""
    return (math.ceil((u1-u0)*AUDIO_RATE)+2)*channels*4 + 4096


def _prerender_peak_bytes(events, first_sample, stop_sample, channels=2):
    """Most pre-rendered event bytes on disk at once, following _render_audio's
    order exactly: files are made as their events arrive and deleted once a
    chunk starts after their end."""
    alive, peak = [], 0
    for _, _, _, origin, arrivals, _ in _audio_schedule(events, first_sample, stop_sample):
        for event, span in arrivals:
            if span:
                alive.append((event['start']+span[1], _prerender_bytes(*span, channels)))
        peak = max(peak, sum(size for _, size in alive))
        alive = [(finish, size) for finish, size in alive if finish > origin/AUDIO_RATE]
    return peak


def _render_audio(events, first_sample, stop_sample, directory, cancelled, heartbeat, progress=None, layout='stereo',
                  prefix='audio'):
    """Render the mix as consecutive chunk files; returns their paths in order.
    Pre-renders that arrive together run in parallel (bounded)."""
    from concurrent.futures import ThreadPoolExecutor
    paths, active = [], []
    prerendered = 0
    with ThreadPoolExecutor(max_workers=PRERENDER_WORKERS) as pool:
        for number, begin, end, origin, arrivals, count in _audio_schedule(events, first_sample, stop_sample):
            if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
            jobs = []
            for event, span in arrivals:
                if span:
                    target = Path(directory)/f'{prefix}-event-{prerendered:06d}.wav'; prerendered += 1
                    jobs.append(pool.submit(_prerender_event, event, *span, target, directory, cancelled, heartbeat, layout))
                else:
                    jobs.append(None)
            for (event, _), job in zip(arrivals, jobs):
                active.append(job.result() if job else event)
            for event in active:
                if event.get('prerendered') and event['start']+event['length'] <= origin/AUDIO_RATE:
                    Path(event['path']).unlink(missing_ok=True)
            active = [event for event in active if event['start']+event['length'] > origin/AUDIO_RATE]
            target = Path(directory)/f'{prefix}-{number:05d}.wav'
            _render_audio_chunk([event for event in active if event['start'] < end/AUDIO_RATE+AUDIO_POSTROLL_SECONDS], origin,
                                begin-origin, end-begin, target, directory, cancelled, heartbeat, layout)
            paths.append(target)
            if progress: progress((number+1)/count)
    for event in active:
        if event.get('prerendered'):
            Path(event['path']).unlink(missing_ok=True)
    return paths


def _audio_options(options, codec):
    layout = options.get('audioLayout', 'stereo')
    if layout not in AUDIO_LAYOUTS:
        raise ValueError('Invalid audio layout: use stereo, 5.1 or 5.1-downmix')
    iso = options.get('isoTracks', False)
    if not isinstance(iso, bool): raise ValueError('Invalid isoTracks setting: use true or false')
    if codec != 'stems' and iso: raise ValueError('Track isos are part of the stems export')
    return layout, audio_loudness.normalization_options(options), iso


def _concat_input(directory, name, paths):
    return ['-f', 'concat', '-safe', '0', '-i', str(_concat_list(directory, name, paths))]


def _program_bus(events, plans, master_fx, first_sample, stop_sample, layout, directory, cancelled, heartbeat, progress,
                 name='mix', master_linear_only=False):
    """Mix ``events`` into the program bus. Tracks with dynamics (compressor,
    gate, de-noise) are mixed on their own and processed as a whole; the
    master effects follow. Returns FFmpeg input arguments of the float program."""
    mixed = 'stereo' if layout == 'stereo' else '5.1'
    dynamic = sorted({event['track'] for event in events if plans[event['track']]['dynamics']}, key=lambda key: abs(int(key)))
    groups = [(None, [event for event in events if event['track'] not in dynamic])]
    groups += [(track, [event for event in events if event['track'] == track]) for track in dynamic]
    inputs, chains = [], []
    for number, (track, members) in enumerate(groups):
        if track is not None or members or len(groups) == 1:
            share = (lambda fraction, number=number: progress((number+fraction)/len(groups))) if progress else None
            prefix = 'audio' if (name, number) == ('mix', 0) else f'{name}-{number:03d}'
            chunks = _render_audio(members, first_sample, stop_sample, directory, cancelled, heartbeat, share, mixed,
                                   prefix=prefix)
            inputs.append(_concat_input(directory, f'{name}-{number:03d}.txt', chunks))
            chains.append(audio_dsp.ffmpeg_filters(plans[track]['fx']) if track is not None else [])
    master = audio_dsp.ffmpeg_filters({key: value for key, value in master_fx.items()
                                       if not master_linear_only or key == 'eq'})
    downmix = [DOWNMIX] if layout == '5.1-downmix' else []
    if len(inputs) == 1 and not chains[0] and not master and not downmix:
        return inputs[0]
    total = stop_sample-first_sample
    graph = [f"[{index}:a]{','.join(chain) or 'anull'}[g{index}]" for index, chain in enumerate(chains)]
    labels = ''.join(f'[g{index}]' for index in range(len(chains)))
    tail = ','.join([*master, *downmix, f"aformat=sample_fmts=flt:channel_layouts={'stereo' if layout != '5.1' else '5.1'}",
                     f'atrim=end_sample={total}'])
    graph.append(f'{labels}amix=inputs={len(chains)}:duration=longest:normalize=0,{tail}[out]' if len(chains) > 1 else
                 f'[g0]{tail}[out]')
    target = Path(directory)/f'{name}-program.wav'
    _run_ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                 *(argument for entry in inputs for argument in entry),
                 *_graph_args(';'.join(graph), directory, f'{name}-program'), '-map', '[out]',
                 '-c:a', 'pcm_f32le', '-ar', str(AUDIO_RATE), str(target)], cancelled, heartbeat)
    for entry in inputs:
        for chunk in Path(entry[-1]).read_text(encoding='utf-8').splitlines():
            (Path(directory)/chunk[6:-1]).unlink(missing_ok=True)
    return ['-i', str(target)]


def _program_audio(project, events, plans, codec, options, settings, first_sample, stop_sample, directory, cancelled,
                   heartbeat, progress, output):
    """Mix, finish (normalize, limit) and encode the program sound, measured as
    delivered. Returns (FFmpeg input args for the mux, or None when ``output``
    itself was written; the audio report)."""
    layout, normalization, limiter, peak_safe = settings
    channels = 6 if layout == '5.1' else 2
    total = stop_sample-first_sample
    master_fx = audio_dsp.normalize(project.get('masterFx'), audio_dsp.MASTER_KINDS, 'master effects')
    share = lambda low, high: (lambda fraction: progress(low+(high-low)*fraction)) if progress else None
    program = _program_bus(events, plans, master_fx, first_sample, stop_sample, layout, directory, cancelled, heartbeat,
                           share(0, .7))
    finished, report, finished_measurement = audio_loudness.finish(
        program, total, directory, ceiling=limiter, normalization=normalization, channels=channels,
        cancelled=cancelled, heartbeat=heartbeat, progress=share(.7, .85))
    report = {'container': {'wav': 'WAV', 'prores': 'MOV (PCM)', 'h264': 'MP4 (AAC)', 'loudness': 'analysis'}.get(codec, codec),
              'layout': layout, 'channels': channels, **report}
    pcm = ['-c:a', 'pcm_s24le', '-ar', str(AUDIO_RATE), '-ac', str(channels)]
    if codec in ('wav', 'prores'):
        target = Path(output) if codec == 'wav' else Path(directory)/'delivery.wav'
        _run_ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *finished, *pcm, str(target)],
                    cancelled, heartbeat)
        delivered = audio_loudness.measure(['-i', str(target)], cancelled, heartbeat, channels=channels)
        report['output'] = audio_loudness.public(delivered)
        _legacy_report(report, delivered, limiter)
        if progress: progress(1.0)
        return (None if codec == 'wav' else ['-i', str(target)]), report
    bitrate = int(_number(options.get('audioBitrate', 192), 'audio bitrate', 64, 320))*(2 if channels == 6 else 1)
    audio_file, measured = _encode_aac(finished, f'{bitrate}k', limiter, peak_safe, directory, cancelled, heartbeat, channels)
    report['output'] = audio_loudness.public(measured['measurement'])
    report['aacPeakSafe'] = peak_safe
    report['aacGainDb'] = measured['gainDb']
    report['gainDb'] = round((report.get('gainDb') or 0)+measured['gainDb'], 3)
    _legacy_report(report, measured['measurement'], limiter)
    if progress: progress(1.0)
    return ['-i', str(audio_file)], report


def _legacy_report(report, delivered, ceiling):
    """The keys the export dialog has always shown (decoded = as delivered)."""
    finite = lambda value: round(value, 2) if math.isfinite(value) else None
    report.update(ceilingDb=ceiling, decodedTruePeakDb=finite(delivered['truePeak']), loudnessLufs=finite(delivered['integrated']))



def _concat_list(directory, name, paths):
    manifest = Path(directory)/name
    manifest.write_text(''.join(f"file '{path.name}'\n" for path in paths), encoding='utf-8')
    return manifest


def render_to_file(project, output_path, *, options=None, progress=None, cancelled=None, heartbeat=None,
                   work_directory=None, report=None, _nested_depth=0):
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
            suffix='.mov' if options.get('codec')=='prores' else '.mp4'
            target=Path(directory)/f'nested-{index}{suffix}'
            child_options={**options,'codec':'prores' if suffix=='.mov' else 'h264'}
            for key in ('range', 'isoTracks', 'normalize', 'normalizeMode'):
                child_options.pop(key, None)
            render_to_file(child,target,options=child_options,cancelled=cancelled,heartbeat=heartbeat,work_directory=work_directory,
                           _nested_depth=_nested_depth+1)
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


def _export_settings(options):
    codec = options.get('codec','h264')
    if codec not in ('h264','prores','wav','stems','loudness'): raise ValueError('Unsupported export codec')
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


def _estimated_bytes(width, height, fps, seconds, codec):
    bits_per_pixel = 3.6 if codec == 'prores' else .3
    video = 0 if codec in AUDIO_ONLY_CODECS else width*height*fps*seconds*bits_per_pixel/8
    return video, seconds*AUDIO_RATE*2*4


def _render_prepared(project, output_path, *, options=None, progress=None, cancelled=None, heartbeat=None, work_directory=None,
                     report=None):
    """Render bounded picture windows and audio chunks, then mux them in one pass."""
    import copy
    options = dict(options or {})
    codec, color_space, limiter, language, peak_safe = _export_settings(options)
    layout, normalization, iso = _audio_options(options, codec)
    if normalization and normalization[1] == 'limit' and limiter is None:
        raise ValueError('Normalizing with limiting needs the limiter: turn it on or choose linear normalization')
    audio_only = codec in AUDIO_ONLY_CODECS
    # One private copy: probe results are cached on its media items.
    project = copy.deepcopy(project)
    total = _validate_timeline(project)
    width, height = _frame_size(project) if not audio_only else (16, 16)
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
    if not audio_only and not muted.get('video'):
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
    plans = {}
    if muted.get('audio'):
        events = []
    else:
        events = _audio_events(project['clips'], media_by_id, fps, probes, track_mix, solo,
                               (max(0, out_start-AUDIO_PREROLL_SECONDS), out_end), heartbeat, layout=layout, plans=plans)
    channels = 2 if layout == 'stereo' else 6
    root = Path(work_directory) if work_directory else work_root()
    video_bytes, audio_bytes = _estimated_bytes(width, height, fps, out_duration, codec)
    # A speed-changed or resampled clip that crosses a chunk seam is rendered
    # whole first (32-bit float stereo). Count the most of those files that are
    # on disk at once, following the mix's own schedule (overlapping ones add up).
    # Lossy deliveries also keep an AAC file until the mux.
    # Chunks, the program bus, the finished program and the delivered PCM (float, float, float, 24-bit).
    audio_bytes = audio_bytes*channels/2*3.75 + _prerender_peak_bytes(events, first_sample, stop_sample, channels)
    if codec == 'stems':
        audio_bytes += out_duration*AUDIO_RATE*channels*4*(len({plan['stem'] for plan in plans.values()})+
                                                           (len(plans) if iso else 0)+1)
    if codec == 'h264':
        audio_bytes += out_duration*40_000
    output = Path(output_path)
    final_bytes = video_bytes + (out_duration*AUDIO_RATE*channels*3 if codec != 'h264' else out_duration*40_000)
    same_volume = output.parent.exists() and os.stat(output.parent).st_dev == os.stat(root).st_dev
    _ensure_space(root, video_bytes+audio_bytes+(final_bytes if same_volume else 0), 'this export')
    if not same_volume and output.parent.exists():
        _ensure_space(output.parent, final_bytes, 'the exported file')
    def stage(low, high):
        return (lambda fraction: progress(low+(high-low)*fraction)) if progress else None
    with WorkDirectory('splice-segments-', root) as directory:
        settings = (layout, normalization, limiter, peak_safe)
        if codec in ('wav', 'loudness', 'stems'):
            if codec == 'loudness':
                result = _analyze_program(project, events, plans, settings, first_sample, stop_sample, directory, cancelled,
                                          heartbeat, progress, output)
            elif codec == 'stems':
                result = _render_stems(project, events, plans, settings, iso, first_sample, stop_sample, directory, cancelled,
                                       heartbeat, progress, output)
            else:
                result = _program_audio(project, events, plans, codec, options, settings, first_sample, stop_sample,
                                        directory, cancelled, heartbeat, progress, output)[1]
            if report is not None:
                report['audio'] = result
            if progress: progress(1.0)
            return output.stat().st_size
        segments = _render_picture(project, picture_clips, first, stop, total_frames, fps, width, height, codec, options,
                                   directory, suffix, cancelled, heartbeat, stage(0, .8))
        audio_input, audio_report = _program_audio(project, events, plans, codec, options, settings, first_sample, stop_sample,
                                                   directory, cancelled, heartbeat, stage(.8, .95), None)
        if report is not None:
            report['audio'] = audio_report
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


def _db_text(value):
    return f'{value:+.1f}'.replace('+', '+' if value > 0 else '').replace('-', '\u2212') if math.isfinite(value) else '-inf'


def _analyze_program(project, events, plans, settings, first_sample, stop_sample, directory, cancelled, heartbeat,
                     progress, output):
    """Analyze program: the real mix (master bus included, before normalization
    and limiting) measured to BS.1770 / EBU R128. Writes the numbers as JSON."""
    layout = settings[0]
    master_fx = audio_dsp.normalize(project.get('masterFx'), audio_dsp.MASTER_KINDS, 'master effects')
    share = (lambda fraction: progress(.8*fraction)) if progress else None
    program = _program_bus(events, plans, master_fx, first_sample, stop_sample, layout, directory, cancelled, heartbeat, share)
    measured = audio_loudness.measure_full(program, cancelled, heartbeat, channels=6 if layout == '5.1' else 2,
                                           directory=directory)
    result = {'container': 'analysis', 'layout': layout, 'seconds': round((stop_sample-first_sample)/AUDIO_RATE, 6),
              'startSeconds': round(first_sample/AUDIO_RATE, 6), 'program': audio_loudness.public(measured)}
    Path(output).write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def _render_stems(project, events, plans, settings, iso, first_sample, stop_sample, directory, cancelled, heartbeat,
                  progress, output):
    """Stems: one WAV per stem (tracks grouped by their stem name), the finished
    mix and optional per-track isos, full length and sample-aligned, in one
    uncompressed zip. Stems carry the master EQ and the program gain but no
    master compressor and no limiter, so with a linear master they sum to the
    mix as long as the limiter does not act."""
    import zipfile
    layout, normalization, limiter, _ = settings
    channels = 6 if layout == '5.1' else 2
    total = stop_sample-first_sample
    master_fx = audio_dsp.normalize(project.get('masterFx'), audio_dsp.MASTER_KINDS, 'master effects')
    stems = {}
    for key in sorted(plans, key=lambda key: abs(int(key))):
        stems.setdefault(plans[key]['stem'] or 'Other', []).append(key)
    steps = len(stems)+2+(len(plans) if iso else 0)
    done = [0]

    def step():
        done[0] += 1
        if progress: progress(done[0]/steps)
    buses = {}
    for number, (name, tracks) in enumerate(stems.items()):
        buses[name] = _program_bus([event for event in events if event['track'] in tracks], plans, {}, first_sample,
                                   stop_sample, '5.1' if layout != 'stereo' else 'stereo', directory, cancelled, heartbeat,
                                   None, name=f'stem{number:02d}')
        step()
    master = audio_dsp.ffmpeg_filters(master_fx)
    linear_master = audio_dsp.ffmpeg_filters({key: value for key, value in master_fx.items() if key == 'eq'})
    downmix = [DOWNMIX] if layout == '5.1-downmix' else []
    name = 'stereo' if layout != '5.1' else '5.1'
    sources = list(buses.values()) or [_program_bus([], plans, {}, first_sample, stop_sample, layout, directory, cancelled,
                                                    heartbeat, None, name='silence')]
    labels = ''.join(f'[{index}:a]' for index in range(len(sources)))
    mixed = f'{labels}amix=inputs={len(sources)}:duration=longest:normalize=0,' if len(sources) > 1 else '[0:a]'
    program = Path(directory)/'stems-program.wav'
    tail = ','.join([*master, *downmix, f'aformat=sample_fmts=flt:channel_layouts={name}', f'atrim=end_sample={total}'])
    _run_ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *(a for entry in sources for a in entry),
                 *_graph_args(f'{mixed}{tail}[out]', directory, 'stems-program'), '-map', '[out]', '-c:a', 'pcm_f32le',
                 str(program)], cancelled, heartbeat)
    finished, report, _ = audio_loudness.finish(['-i', str(program)], total, directory, ceiling=limiter,
                                                normalization=normalization, channels=channels, cancelled=cancelled,
                                                heartbeat=heartbeat)
    step()
    gain = report.get('gainDb') or 0.0
    files = []

    def deliver(arguments, filters, target, codec='pcm_f32le'):
        chain = ','.join([*filters, f'aformat=channel_layouts={name}']) if filters else f'aformat=channel_layouts={name}'
        _run_ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *arguments, '-af', chain,
                     '-c:a', codec, '-ar', str(AUDIO_RATE), '-ac', str(channels),
                     str(target)], cancelled, heartbeat)
        files.append(target)
    mix_file = Path(directory)/'mix.wav'
    deliver(finished, [], mix_file, 'pcm_s24le')
    delivered = audio_loudness.measure(['-i', str(mix_file)], cancelled, heartbeat, channels=channels)
    report.update(container='stems', layout=layout, channels=channels, output=audio_loudness.public(delivered))
    _legacy_report(report, delivered, limiter)
    gain_filter = [f'volume={gain!r}dB:precision=double'] if gain else []
    report['stems'] = []
    for number, (stem, source) in enumerate(buses.items()):
        safe = ''.join(char if char.isalnum() or char in '-_' else '_' for char in stem)
        target = Path(directory)/f'stem-{number+1:02d}-{safe}.wav'
        deliver(source, [*linear_master, *downmix, *gain_filter], target)
        report['stems'].append({'name': stem, 'file': target.name, 'tracks': [_track_label(key) for key in stems[stem]]})
    report['stemsSumToMix'] = 'comp' not in master_fx and not (report.get('limiter') or {}).get('acted')
    if not report['stemsSumToMix']:
        report['stemsNote'] = ('Stems are taken before the master compressor and the limiter, which acted on this mix, '
                               'so they do not sum exactly to it.')
    if iso:
        report['isos'] = []
        for key in sorted(plans, key=lambda key: abs(int(key))):
            label = _track_label(key)
            bus = _program_bus([event for event in events if event['track'] == key], plans, {}, first_sample, stop_sample,
                               '5.1' if layout != 'stereo' else 'stereo', directory, cancelled, heartbeat, None,
                               name=f'iso-{label}')
            target = Path(directory)/f'iso-{label}.wav'
            deliver(bus, downmix, target)
            report['isos'].append({'track': label, 'file': target.name})
            step()
    report['startSeconds'] = round(first_sample/AUDIO_RATE, 6)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in files:
            archive.write(path, path.name)
            path.unlink()
        archive.writestr('report.json', json.dumps(report, indent=2))
    return report


def _encode_aac(finished, bitrate, ceiling, peak_safe, directory, cancelled, heartbeat, channels=2):
    """Encode the finished mix to AAC once at unity gain and measure the decoded file.

    Returns (path, {'measurement', 'gainDb'}). By default the program level is
    never changed here: a decoded true peak over the ceiling (AAC overshoot) is
    reported with a warning. With ``peak_safe`` (export option aacPeakSafe) the
    whole program is re-encoded quieter until the decoded true peak fits: the
    first gain is the measured excess rounded up to the next AAC_PEAK_STEP_DB,
    then one step more per attempt. Every step depends only on the decoded
    measurements, so the same mix always gets the same gain.
    """
    import warnings
    target = Path(directory)/'audio.m4a'

    def encode(gain):
        volume = ['-af', f'volume={gain:.2f}dB'] if gain else []
        _run_ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *finished, *volume,
                     '-c:a', 'aac', '-b:a', bitrate, '-ac', str(channels), str(target)], cancelled, heartbeat)

    def decoded():
        return audio_loudness.measure(['-i', str(target)], cancelled, heartbeat, channels=channels)
    encode(0)
    measurement = decoded()
    gain = 0.0
    peak, loudness = measurement['truePeak'], measurement['integrated']
    if ceiling is None or peak <= ceiling:
        return target, {'measurement': measurement, 'gainDb': gain}
    if not peak_safe:
        warnings.warn(f'The MP4 audio peaks at {_db_text(peak)} dBTP once decoded, above the {_db_text(ceiling)} dBTP ceiling: '
                      'the mix is limited to the ceiling, and AAC encoding adds the overshoot. Program loudness was left '
                      f'unchanged ({_db_text(loudness)} LUFS). To deliver under the ceiling, lower the limiter ceiling, export with '
                      '“Keep decoded AAC under the ceiling” (it lowers the whole program until the decoded peak fits), or export WAV or ProRes.',
                      stacklevel=2)
        return target, {'measurement': measurement, 'gainDb': gain}
    excess = peak-ceiling
    gain = -math.ceil(excess/AAC_PEAK_STEP_DB-1e-9)*AAC_PEAK_STEP_DB
    for attempt in range(AAC_PEAK_ATTEMPTS):
        encode(gain)
        measurement = decoded()
        peak, loudness = measurement['truePeak'], measurement['integrated']
        if peak <= ceiling:
            break
        if attempt+1 < AAC_PEAK_ATTEMPTS:
            gain -= AAC_PEAK_STEP_DB
    if peak <= ceiling:
        warnings.warn(f'“Keep decoded AAC under the ceiling” lowered the whole program by {-gain:.1f} dB '
                      f'(now {_db_text(loudness)} LUFS) so the decoded MP4 audio peaks at {_db_text(peak)} dBTP, '
                      f'under the {_db_text(ceiling)} dBTP ceiling.', stacklevel=2)
    else:
        warnings.warn(f'“Keep decoded AAC under the ceiling” lowered the whole program by {-gain:.1f} dB, but the decoded MP4 audio still '
                      f'peaks at {_db_text(peak)} dBTP, above the {_db_text(ceiling)} dBTP ceiling.', stacklevel=2)
    return target, {'measurement': measurement, 'gainDb': round(gain, 2)}


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
                if overlap_start > cut+.5/fps: clip['transition']='none'
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
