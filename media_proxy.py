"""Probe camera originals and produce reusable local edit proxies."""
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import threading

from premiere import MEDIA_ROOT, STILL_IMAGES, _playback_url
from media_exec import run_bounded
from media_links import camera_raw_error, mark, resolve_media as _media_path

_encoders = threading.BoundedSemaphore(2)
_locks = tuple(threading.Lock() for _ in range(32))
MAX_AUDIO_STREAMS = 16
HDR_TRANSFERS = frozenset({'arib-std-b67', 'smpte2084'})
REC709 = ('bt709', 'bt709', 'bt709')
AVC_REC709 = 'h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1'
HDR_PREVIEW_VERSION = 3
STILL_PREVIEW_VERSION = 1
# BT.2408 reference white is 203 cd/m². Leave enough room in the SDR
# highlight shoulder to distinguish bright patches on an 8-bit display.
HDR_LINEAR_REC709 = 'zscale=t=linear:npl={peak:.9f},format=gbrpf32le,zscale=p=bt709'
HDR_TONE_MAP = 'tonemap=mobius:param=0.78:desat=0:peak=4.926108,zscale=t=bt709:m=bt709:r=limited'
HDR_TO_REC709 = HDR_LINEAR_REC709.format(peak=203) + ',' + HDR_TONE_MAP + ',format=yuv444p10le'
# Pack wide-gamut PQ channel values into a browser-decodable proxy. Its
# Rec.709 tags prevent the browser from tone-mapping these data samples.
HDR_GRADE_DATA = ('zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt2020,'
                  'zscale=t=smpte2084:m=bt709:r=limited:npl=203,format=yuv420p')
AUDITION_SECONDS = 4
_audition_decoders = threading.BoundedSemaphore(4)


def tempo_filters(speed):
    """Stay within the pitch-preserving range of each FFmpeg tempo stage."""
    filters = []
    while speed > 2:
        filters.append('atempo=2')
        speed /= 2
    while speed < .5:
        filters.append('atempo=0.5')
        speed *= 2
    if abs(speed-1) > 1e-6:
        filters.append(f'atempo={speed:.6f}')
    return filters


AUDITION_RATE = 48000
# Two channels of 16-bit PCM for the window plus the tempo overlap, a WAV
# header and generous slack. Nothing larger is ever held in memory.
AUDITION_LIMIT = 1024 * 1024
PROBE_LIMIT = 8 * 1024 * 1024
SCAN_LIMIT = 256 * 1024


def _silence(seconds):
    frames = round(seconds * AUDITION_RATE)
    data = frames * 4
    header = (b'RIFF' + (36 + data).to_bytes(4, 'little') + b'WAVEfmt ' + (16).to_bytes(4, 'little') +
              (1).to_bytes(2, 'little') + (2).to_bytes(2, 'little') + AUDITION_RATE.to_bytes(4, 'little') +
              (AUDITION_RATE * 4).to_bytes(4, 'little') + (4).to_bytes(2, 'little') + (16).to_bytes(2, 'little') +
              b'data' + data.to_bytes(4, 'little'))
    return header + bytes(data)


def audition_audio(src, index, rate=1, reverse=False, stream=0):
    """Read a bounded span and apply the same tempo processing as delivery."""
    if not math.isfinite(rate) or not .0625 <= rate <= 16:
        raise ValueError('Invalid audio preview speed')
    span = AUDITION_SECONDS * rate
    if type(index) is not int or not 0 <= index < 86400 / span:
        raise ValueError('Invalid audio preview position')
    path = _media_path(src)
    info = describe(src)
    streams = info['audioStreams']
    if type(stream) is not int or not 0 <= stream < max(1, streams):
        raise ValueError('The selected source audio stream is unavailable')
    if not streams:
        raise ValueError('The source has no audio')
    start = index * span
    overlap = .02 if rate != 1 else 0
    if start >= info['duration']:
        # Past the end of the source: answer with the silence the tail
        # padding would have produced instead of decoding nothing forever.
        return _silence(AUDITION_SECONDS + overlap)
    # Give the tempo filter extra source after the requested window so its
    # analysis latency cannot leave a silent tail on an ordinary boundary.
    extra = .25 * max(1, rate) if overlap else 0
    seek = max(0, start-extra) if reverse else start
    length = start+span-seek + (0 if reverse else extra)
    filters = [f'aresample={AUDITION_RATE}:first_pts=0', f'apad=whole_dur={length:.9f}', f'atrim=duration={length:.9f}']
    if reverse:
        filters.append('areverse')
    filters.extend(tempo_filters(rate))
    filters.append(f'apad=whole_dur={AUDITION_SECONDS+overlap:.9f}')
    if not _audition_decoders.acquire(timeout=2):
        raise ValueError('Audio preview is busy')
    try:
        result = run_bounded([
            'ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file', '-threads', '1',
            '-ss', f'{seek:.9f}', '-i', str(path), '-map', f'0:a:{stream}',
            '-af', ','.join(filters), '-ac', '2', '-ar', str(AUDITION_RATE), '-c:a', 'pcm_s16le',
            '-t', str(AUDITION_SECONDS+overlap), '-f', 'wav', 'pipe:1',
        ], timeout=8, max_output=AUDITION_LIMIT)
        if result.returncode:
            raise ValueError('Could not decode audio for preview')
        return result.stdout
    finally:
        _audition_decoders.release()


def stereo_mix_filters(sources, output, channels, duration):
    if channels==[1,1]:
        labels = [f'{output}mono0',f'{output}mono1']
        filters = [f'{source},apad=whole_dur={duration:.9f},aformat=channel_layouts=mono[{label}]'
                   for source,label in zip(sources,labels)]
        inputs = ''.join(f'[{label}]' for label in labels)
        filters.append(f'{inputs}join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[{output}]')
        return filters
    labels = [f'{output}stream{index}' for index in range(len(sources))]
    filters = [f'{source},aformat=channel_layouts=stereo[{label}]' for source,label in zip(sources,labels)]
    inputs = ''.join(f'[{label}]' for label in labels)
    filters.append(f'{inputs}amix=inputs={len(labels)}:duration=longest:normalize=0,aformat=channel_layouts=stereo[{output}]')
    return filters


STANDARD_RATES = tuple(Fraction(r) for r in ('24000/1001', '24', '25', '30000/1001', '30', '48', '50',
                                                '60000/1001', '60', '100', '120000/1001', '120'))
LONG_GOP = frozenset({'h264', 'hevc', 'av1', 'vp8', 'vp9', 'mpeg2video', 'mpeg4'})
STILL_FORMATS = frozenset({'image2', 'png_pipe', 'tiff_pipe', 'jpeg_pipe', 'bmp_pipe', 'exr_pipe', 'dpx_pipe',
                           'webp_pipe', 'image2pipe'})
_TIMECODE = re.compile(r'(\d{2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})')


def _fraction(value):
    try:
        rate = Fraction(str(value or '0'))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if 0 < rate <= 1000 else None


def _rational(value):
    return f'{value.numerator}/{value.denominator}'


def _snap(rate):
    """A probed average such as 2997/100 is the 30000/1001 a camera wrote."""
    for standard in STANDARD_RATES:
        if abs(rate - standard) / standard < Fraction(1, 2000):
            return standard
    return rate.limit_denominator(1001)


def _tag(tags, *names):
    lowered = {str(k).lower(): v for k, v in (tags or {}).items()}
    for name in names:
        value = lowered.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class ProbeTimeout(ValueError):
    """FFprobe did not answer in time: a slow or disconnected drive, not a
    file FFmpeg cannot decode."""


def _ffprobe(path, *arguments, limit=PROBE_LIMIT, timeout=25):
    try:
        result = run_bounded(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', *arguments, '-of', 'json', str(path)],
                             timeout=timeout, max_output=limit)
    except subprocess.TimeoutExpired:
        raise ProbeTimeout(f'Reading {Path(path).name} timed out after {timeout:g} s. Its drive may be slow or '
                           'disconnected; try again once it responds.') from None
    if result.returncode:
        raise ValueError('Could not read media streams in ' + Path(path).name)
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError('Could not read media streams in ' + Path(path).name)
    return data


VFR_PACKETS = 300
VFR_MIN_STEPS = 8


def _variable_rate(path, stream_index, nominal, average):
    """Whether the first packets' timestamps step irregularly. Always
    scanned: Matroska/WebM report matching nominal and average rates even
    when the rate changes. Too few packets to judge falls back to whether
    the nominal and average rates disagree."""
    mismatch = nominal is not None and average is not None and abs(nominal - average) / nominal >= Fraction(1, 1000)
    data = _ffprobe(path, '-select_streams', str(stream_index), '-read_intervals', f'%+#{VFR_PACKETS}',
                    '-show_entries', 'packet=pts_time', limit=SCAN_LIMIT, timeout=15)
    times = []
    for packet in data.get('packets', []):
        try:
            times.append(float(packet['pts_time']))
        except (KeyError, TypeError, ValueError):
            continue
    times.sort()
    steps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    if len(steps) < VFR_MIN_STEPS:
        return mismatch
    median = steps[len(steps) // 2]
    irregular = sum(abs(step - median) > median * .1 for step in steps)
    return irregular / len(steps) > .05


def _rotation(stream):
    for side in stream.get('side_data_list') or []:
        if isinstance(side, dict) and 'rotation' in side:
            try:
                return round(-float(side['rotation'])) % 360
            except (TypeError, ValueError):
                break
    try:
        return round(float(_tag(stream.get('tags'), 'rotate') or 0)) % 360
    except ValueError:
        return 0


def _color(value):
    return value if isinstance(value, str) and value not in ('unknown', 'unspecified', 'reserved') else None


@lru_cache(maxsize=512)
def _describe_file(path, size, modified):
    path = Path(path)
    data = _ffprobe(path, '-show_streams', '-show_format')
    streams = [s for s in data.get('streams', []) if isinstance(s, dict)]
    fmt = data.get('format') if isinstance(data.get('format'), dict) else {}
    video = next((s for s in streams if s.get('codec_type') == 'video' and not (s.get('disposition') or {}).get('attached_pic')), {})
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
    if len(audio_streams) > MAX_AUDIO_STREAMS:
        raise ValueError(f'Media has more than {MAX_AUDIO_STREAMS} audio streams')
    audio = audio_streams[0] if audio_streams else {}
    if not video and not audio:
        raise ValueError('The source contains no editable audio or video stream')
    if _text_art(fmt, streams):
        raise ValueError(f'The installed FFmpeg cannot decode {path.name}: it is a text file, not media')
    still = bool(video) and (path.suffix.lower() in STILL_IMAGES or fmt.get('format_name') in STILL_FORMATS)
    try:
        duration = 5 if still else float(fmt.get('duration') or video.get('duration') or audio.get('duration') or 0)
    except ValueError:
        duration = 0
    if not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError('No supported media duration in ' + path.name)
    nominal, average = _fraction(video.get('r_frame_rate')), _fraction(video.get('avg_frame_rate'))
    fps = float(average) if average else 0
    vfr = bool(video) and not still and _variable_rate(path, video.get('index', 0), nominal, average)
    rate = None
    if video and not still and (average or nominal):
        if vfr or not nominal:
            rate = _snap(average or nominal)
        elif average and abs(nominal - average) / average >= Fraction(1, 1000):
            rate = _snap(average)  # Field-rate nominal values on interlaced sources.
        else:
            rate = _snap(nominal)
    timecode_stream = next((s for s in streams if s.get('codec_tag_string') == 'tmcd' or
                            (s.get('codec_type') == 'data' and _tag(s.get('tags'), 'timecode'))), {})
    timecode = None
    for tags in (timecode_stream.get('tags'), video.get('tags'), fmt.get('tags'), *(s.get('tags') for s in streams)):
        match = _TIMECODE.fullmatch(_tag(tags, 'timecode') or '')
        if match:
            timecode = match
            break
    drop = bool(timecode) and timecode.group(4) in ';,'
    reel = None
    for tags in (timecode_stream.get('tags'), video.get('tags'), fmt.get('tags')):
        reel = _tag(tags, 'reel_name', 'reel', 'tape_name', 'tape')
        if reel:
            break
    vcodec, acodec = video.get('codec_name'), audio.get('codec_name')
    width, height = video.get('width'), video.get('height')
    native_video = not video or vcodec in ('h264','vp8','vp9','av1')
    native_audio = not audio or acodec in ('aac','mp3','opus','vorbis','flac','pcm_s16le','pcm_s24le','pcm_f32le')
    native_container = path.suffix.lower() in ('.mp4','.m4v','.m4a','.webm','.wav','.mp3','.ogg','.flac','.aac')
    hdr = video.get('color_transfer') in HDR_TRANSFERS
    uhd_long_gop = vcodec in LONG_GOP and isinstance(width, int) and isinstance(height, int) and max(width, height) >= 3840
    return dict(type='video' if video else 'audio', duration=duration, width=width, height=height, hdr=hdr,
                hdrPreviewVersion=HDR_PREVIEW_VERSION if hdr else None,
                fps=fps, rate=_rational(rate) if rate else None, vfr=vfr,
                timecode=(':'.join(timecode.group(1, 2, 3)) + (';' if drop else ':') + timecode.group(5)) if timecode else None,
                dropFrame=drop, reel=reel,
                videoCodec=vcodec, audioCodec=acodec, audioChannels=2 if len(audio_streams)>1 else audio.get('channels',0),
                audioStreams=len(audio_streams),
                audioStreamDetails=[dict(index=i, streamIndex=s.get('index'), channels=s.get('channels', 1),
                                   layout=s.get('channel_layout'), codec=s.get('codec_name'))
                              for i, s in enumerate(audio_streams)],
                audioStreamChannels=[stream.get('channels',1) for stream in audio_streams], still=still,
                stillPreviewVersion=STILL_PREVIEW_VERSION if still else None,
                pixelFormat=video.get('pix_fmt'), colorPrimaries=_color(video.get('color_primaries')),
                colorTransfer=_color(video.get('color_transfer')), colorSpace=_color(video.get('color_space')),
                colorRange=_color(video.get('color_range')), rotation=_rotation(video) if video else 0,
                proxyRecommended=hdr or vfr or uhd_long_gop or len(audio_streams)>1 or
                not (native_video and native_audio and native_container))


def describe_path(path):
    path = Path(path)
    stat = path.stat()
    return {**_describe_file(str(path), stat.st_size, stat.st_mtime_ns), 'sourcePath': str(path)}


def describe(src):
    return describe_path(_media_path(src))


# FFmpeg renders any text file as a picture through its tty demuxer (and
# reads BBS art formats): never media a user meant to edit.
TEXT_FORMATS = frozenset({'tty', 'bin', 'xbin', 'adf', 'idf'})
# Playlist and script demuxers read other files (or URLs) named inside a text file: a renamed
# playlist must never be registered or served as media.
TEXT_FORMATS |= frozenset({'concat', 'ffconcat', 'hls', 'applehttp', 'dash', 'imf', 'webm_dash_manifest', 'mpl2'})
TEXT_CODECS = frozenset({'ansi', 'bintext', 'xbin', 'idf'})


def _text_art(fmt, streams):
    names = set(str(fmt.get('format_name') or '').split(','))
    return bool(names & TEXT_FORMATS) or any(s.get('codec_name') in TEXT_CODECS for s in streams if isinstance(s, dict))


def decodable(path, name):
    """Whether the installed FFmpeg can edit this file. The probe decides,
    not the filename extension, except for camera RAW it never decodes and
    text files FFmpeg would draw as a picture. A probe that times out is
    reported as a timeout."""
    message = camera_raw_error(name)
    if message:
        raise ValueError(message)
    try:
        data = _ffprobe(path, '-show_entries', 'stream=codec_type,codec_name:format=format_name',
                        timeout=25, limit=SCAN_LIMIT)
    except ProbeTimeout:
        raise
    except (ValueError, OSError, subprocess.SubprocessError):
        data = {}
    streams = [s for s in data.get('streams', []) if isinstance(s, dict)]
    fmt = data.get('format') if isinstance(data.get('format'), dict) else {}
    if _text_art(fmt, streams) or not any(s.get('codec_type') in ('video', 'audio') for s in streams):
        raise ValueError(f'The installed FFmpeg cannot decode {Path(name).name}')


def _select_stream(channels, selection):
    if selection is not None and (type(selection) is not int or not 0<=selection<len(channels)):
        raise ValueError('The selected source audio stream is unavailable')
    return list(range(len(channels))) if selection is None else [selection]


def _source_audio_map(channels, selection, duration):
    chosen = _select_stream(channels,selection)
    if len(chosen)>1:
        sources = [f'[0:a:{i}]aresample=48000:first_pts=0' for i in chosen]
        return ['-filter_complex',';'.join(stereo_mix_filters(sources,'mixed',[channels[i] for i in chosen],duration)),'-map','[mixed]']
    return ['-map',f'0:a:{chosen[0]}' if chosen else '0:a:0?']


def proxy(src, seek_optimized=False, audio_stream=None, grading_data=False):
    original = _media_path(src)
    info = describe(src)
    if grading_data and not info['hdr']:
        raise ValueError('An HDR grading proxy requires HDR video')
    if seek_optimized and (info['type']!='video' or info['still']):
        raise ValueError('A seek proxy requires moving video')
    stat = original.stat()
    profile = 'proxy-seek-v1' if seek_optimized else 'proxy-v3'
    alpha = info['still'] and original.suffix.lower() in ('.png','.bmp')
    if alpha:
        profile += f':still-alpha-v{STILL_PREVIEW_VERSION}'
    if info['hdr']:
        profile += ':hdr-grading-v1' if grading_data else f':hdr-rec709-v{HDR_PREVIEW_VERSION}'
    audio_map = _source_audio_map(info['audioStreamChannels'],audio_stream,info['duration'])
    if info['audioStreams']>1:
        profile += f':audio-v2:{audio_stream}'
    key = hashlib.sha256(f'{profile}:{original}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()
    directory = MEDIA_ROOT / key[:32]
    video = info['type']=='video'
    target = directory / ('edit.webm' if alpha else 'grade.mp4' if grading_data else 'seek.mp4' if seek_optimized else 'edit.mp4' if video else 'edit.m4a')
    if target.is_file():
        return _playback_url(target)
    if not _encoders.acquire(timeout=5):
        raise ValueError('Both proxy encoders are busy. Retry after the current import completes.')
    try:
        with _locks[int(key[:2],16) % len(_locks)]:
            if target.is_file():
                return _playback_url(target)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            mark(directory, 'derived')
            if shutil.disk_usage(directory).free < 512 * 1024**2 + info['duration'] * (525000 if video else 24000):
                raise ValueError('Insufficient free space for an edit proxy')
            with tempfile.TemporaryDirectory(dir=directory,prefix='.proxy-') as staging:
                out = Path(staging) / target.name
                cmd = ['ffmpeg','-nostdin','-v','error','-y','-threads','2','-filter_threads','1','-filter_complex_threads','2','-protocol_whitelist','file']
                if info['still']:
                    cmd += ['-f','image2','-loop','1','-framerate','25']
                cmd += ['-i',str(original)]
                if video:
                    width,height = (960,540) if seek_optimized else (1280,720)
                    fit = f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1"
                    picture = (HDR_GRADE_DATA if grading_data else HDR_TO_REC709)+','+fit if info['hdr'] else fit
                    if grading_data:
                        picture += ',setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv'
                    cmd += ['-map','0:V:0','-vf',picture]
                    if alpha:
                        cmd += ['-c:v','libvpx-vp9','-threads','2','-deadline','realtime','-cpu-used','5',
                                '-b:v','0','-crf','18','-auto-alt-ref','0','-pix_fmt','yuva420p']
                    else:
                        cmd += ['-c:v','libx264','-threads','2','-preset','veryfast',
                                '-crf','22' if seek_optimized else '19','-maxrate','4M','-bufsize','8M',
                                '-g','1' if seek_optimized else '24','-pix_fmt','yuv420p']
                    if info['hdr']:
                        cmd += ['-bsf:v',AVC_REC709,'-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709']
                else:
                    cmd += ['-vn']
                cmd += ['-t',str(info['duration'])]
                if alpha:
                    cmd += ['-an']
                else:
                    cmd += audio_map + ['-c:a','aac','-b:a','160k','-ac',str(min(2,max(1,info['audioChannels']))),'-movflags','+faststart']
                cmd.append(str(out))
                completed = run_bounded(cmd,timeout=min(3600,max(60,info['duration']*3)),max_output=SCAN_LIMIT)
                if completed.returncode:
                    raise ValueError('Proxy creation failed for '+original.name+': '+completed.stderr.decode(errors='replace')[-400:])
                os.link(out,target)
            return _playback_url(target)
    finally:
        _encoders.release()


def premiere_audio(src, audio_stream=None):
    original = _media_path(src)
    info = describe(src)
    stat = original.stat()
    key = hashlib.sha256(f'premiere-audio-v1:{original}:{stat.st_size}:{stat.st_mtime_ns}:{audio_stream}'.encode()).hexdigest()
    target = MEDIA_ROOT/key[:32]/(original.stem+'-audio.wav')
    if target.is_file():return target
    if not _encoders.acquire(timeout=5):raise ValueError('Audio encoders are busy. Retry Premiere save after the current job.')
    try:
        with _locks[int(key[:2],16)%len(_locks)]:
            if target.is_file():return target
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            mark(target.parent, 'derived')
            duration = info['duration']
            if shutil.disk_usage(target.parent).free < 512*1024**2 + duration*288000:
                raise ValueError('Insufficient free space for Premiere audio media')
            chosen = _select_stream(info['audioStreamChannels'],audio_stream)
            if not chosen:raise ValueError('The camera source contains no audio streams')
            sources = [f'[0:a:{i}]aresample=48000:first_pts=0' for i in chosen]
            filters = (stereo_mix_filters(sources,'mixed',[info['audioStreamChannels'][i] for i in chosen],duration)
                       if len(chosen)>1 else [sources[0]+'[mixed]'])
            filters.append(f'[mixed]apad=whole_dur={duration:.9f}[audio]')
            with tempfile.TemporaryDirectory(dir=target.parent,prefix='.audio-') as staging:
                output = Path(staging) / target.name
                cmd=['ffmpeg','-nostdin','-v','error','-y','-threads','2','-filter_complex_threads','2',
                     '-protocol_whitelist','file','-i',str(original),'-filter_complex',';'.join(filters),
                     '-map','[audio]','-c:a','pcm_s24le','-t',str(duration),str(output)]
                result=run_bounded(cmd,timeout=min(3600,max(60,duration*3)),max_output=SCAN_LIMIT)
                if result.returncode:
                    raise ValueError('Could not write Premiere audio: '+result.stderr.decode(errors='replace')[-400:])
                os.link(output,target)
            return target
    finally:
        _encoders.release()


def nested_preview(project):
    """Cache a review-size render by timeline content and original file timestamps."""
    from render_movie import render_to_file

    if not isinstance(project, dict) or not isinstance(project.get('clips'),list) or not isinstance(project.get('media'),list):
        raise ValueError('Invalid nested sequence')
    if not project['clips']:
        raise ValueError('The nested sequence has no playable clips')
    fingerprint = hashlib.sha256(b'nested-preview-v2')
    def add_sources(sequence,depth=0):
        if depth>4:
            raise ValueError('Nested sequence exceeds four levels')
        used = {c.get('mediaId') for c in sequence['clips']}
        for item in sequence['media']:
            if item.get('id') not in used:
                continue
            if item.get('nestedProject'):
                add_sources(item['nestedProject'],depth+1)
            elif item.get('src'):
                path = _media_path(item['src']); stat = path.stat()
                fingerprint.update(f'{path}:{stat.st_size}:{stat.st_mtime_ns}'.encode())
            elif item.get('nativeGenerator') is None:
                raise ValueError('Relink '+str(item.get('name','the offline source'))+' inside the nested sequence.')
    add_sources(project)
    fingerprint.update(json.dumps(project,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
    key=fingerprint.hexdigest()
    directory=MEDIA_ROOT/key[:32]
    target=directory/'nested.mp4'
    if target.is_file():
        return _playback_url(target)
    if not _encoders.acquire(timeout=5):
        raise ValueError('Proxy encoders are busy. Reopen the nested sequence preview to retry.')
    try:
        with _locks[int(key[:2],16)%len(_locks)]:
            if target.is_file():
                return _playback_url(target)
            width,height=int(project['width']),int(project['height'])
            if width<=0 or height<=0: raise ValueError('Invalid nested sequence size')
            ratio=min(1,1280/width,720/height)
            prepared={**project,'width':max(16,2*round(width*ratio/2)),'height':max(16,2*round(height*ratio/2))}
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            mark(directory, 'derived')
            with tempfile.TemporaryDirectory(dir=directory,prefix='.nested-') as staging:
                output = Path(staging) / target.name
                render_to_file(prepared,output,options={'codec':'h264','crf':19})
                os.link(output,target)
            return _playback_url(target)
    finally:
        _encoders.release()
