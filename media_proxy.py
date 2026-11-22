"""Probe camera originals and produce reusable local edit proxies."""
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import threading

from premiere import MEDIA_ROOT, _media_path, _playback_url

_encoders = threading.BoundedSemaphore(2)
_locks = tuple(threading.Lock() for _ in range(32))
_STILLS = {'.png', '.bmp', '.jpg', '.jpeg'}


def describe(src):
    path = _media_path(src)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
                            '-show_entries', 'format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,duration:stream_disposition=attached_pic',
                            '-of', 'json', str(path)], capture_output=True, timeout=25)
    if probe.returncode:
        raise ValueError('Could not read media streams in ' + path.name)
    data = json.loads(probe.stdout)
    streams = data.get('streams', [])
    video = next((s for s in streams if s['codec_type']=='video' and not s.get('disposition',{}).get('attached_pic')), {})
    audio = next((s for s in streams if s['codec_type']=='audio'), {})
    if not video and not audio:
        raise ValueError('The source contains no editable audio or video stream')
    still = path.suffix.lower() in _STILLS
    duration = 5 if still else float(data.get('format',{}).get('duration') or video.get('duration') or audio.get('duration') or 0)
    if not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError('No supported media duration in ' + path.name)
    try:
        fps = float(Fraction(video.get('avg_frame_rate') or '0'))
    except (ValueError, ZeroDivisionError):
        fps = 0
    vcodec, acodec = video.get('codec_name'), audio.get('codec_name')
    native_video = not video or vcodec in ('h264','vp8','vp9','av1')
    native_audio = not audio or acodec in ('aac','mp3','opus','vorbis','flac','pcm_s16le','pcm_s24le','pcm_f32le')
    native_container = path.suffix.lower() in ('.mp4','.m4v','.m4a','.webm','.wav','.mp3','.ogg','.flac','.aac')
    return dict(type='video' if video else 'audio', duration=duration, width=video.get('width'), height=video.get('height'),
                fps=fps, videoCodec=vcodec, audioCodec=acodec, still=still, proxyRecommended=not (native_video and native_audio and native_container))


def proxy(src):
    original = _media_path(src)
    info = describe(src)
    stat = original.stat()
    key = hashlib.sha256(f'proxy-v1:{original}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()
    directory = MEDIA_ROOT / key[:32]
    video = info['type']=='video'
    target = directory / ('edit.mp4' if video else 'edit.m4a')
    if target.is_file():
        return _playback_url(target)
    if not _encoders.acquire(timeout=5):
        raise ValueError('Both proxy encoders are busy. Retry after the current import completes.')
    try:
        with _locks[int(key[:2],16) % len(_locks)]:
            if target.is_file():
                return _playback_url(target)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if shutil.disk_usage(directory).free < 512 * 1024**2 + info['duration'] * (525000 if video else 24000):
                raise ValueError('Insufficient free space for an edit proxy')
            with tempfile.NamedTemporaryFile(dir=directory,suffix=target.suffix) as out:
                cmd = ['ffmpeg','-nostdin','-v','error','-y','-threads','2','-filter_threads','1','-protocol_whitelist','file']
                if info['still']:
                    cmd += ['-loop','1','-framerate','25']
                cmd += ['-i',str(original)]
                if video:
                    cmd += ['-map','0:V:0','-map','0:a:0?', '-vf',"scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",'-c:v','libx264','-threads','2','-preset','veryfast','-crf','19','-maxrate','4M','-bufsize','8M','-g','24','-pix_fmt','yuv420p']
                else:
                    cmd += ['-map','0:a:0','-vn']
                cmd += ['-t',str(info['duration']),'-c:a','aac','-b:a','160k','-ac','2','-movflags','+faststart',out.name]
                completed = subprocess.run(cmd,capture_output=True,timeout=min(3600,max(60,info['duration']*3)))
                if completed.returncode:
                    raise ValueError('Proxy creation failed for '+original.name+': '+completed.stderr.decode(errors='replace')[-400:])
                os.link(out.name,target)
            return _playback_url(target)
    finally:
        _encoders.release()
