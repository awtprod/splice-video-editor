"""Build a compact waveform without decoding a whole reel into browser memory."""
from array import array
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
import math
import subprocess
import sys
import threading

from media_exec import run_bounded
from media_links import resolve_media as _media_path

_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='waveform')
_pending = {}
_lock = threading.Lock()


# Resolution follows duration: 20 peaks per second (50 ms) up to a bound on
# the JSON a browser receives, never fewer than the old fixed 2,048.
PEAKS_PER_SECOND = 20
MIN_PEAKS = 2048
MAX_PEAKS = 65536
SAMPLE_RATE = 4000


def peak_count(duration):
    return max(MIN_PEAKS, min(MAX_PEAKS, math.ceil(duration * PEAKS_PER_SECOND)))


@lru_cache(maxsize=256)
def _compute(path, size, modified, stream):
    info = run_bounded(['ffprobe','-v','error','-protocol_whitelist','file','-select_streams',f'a:{stream}',
                        '-show_entries','stream=codec_type:format=duration','-of','json',path],
                       timeout=15, max_output=256 * 1024, check=True)
    data = json.loads(info.stdout)
    if not data.get('streams'):
        if stream:
            raise ValueError('The selected source audio stream is unavailable')
        return []
    duration = float(data['format']['duration'])
    if not 0 < duration <= 86400:
        raise ValueError('Waveform duration is unavailable')
    # Peak bins sampled at 4 kHz. Read at most one bin per iteration.
    block = max(1, round(duration * SAMPLE_RATE / peak_count(duration))) * 2
    cmd = ['ffmpeg','-nostdin','-loglevel','error','-threads','1','-protocol_whitelist','file',
           '-i',path,'-map',f'0:a:{stream}','-vn','-ac','1','-ar',str(SAMPLE_RATE),'-t',str(duration),
           '-f','s16le','pipe:1']
    with subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as process:
        timer = threading.Timer(180,process.kill)
        timer.start()
        try:
            peaks=[]
            while chunk := process.stdout.read(block):
                if len(peaks) > MAX_PEAKS:
                    raise ValueError('Waveform decoder produced more audio than the source duration')
                samples=array('h')
                samples.frombytes(chunk[:len(chunk)//2*2])
                if sys.byteorder=='big':
                    samples.byteswap()
                if samples:
                    peaks.append(round(max(max(samples),-min(samples))/32768, 4))
            if process.wait():
                raise ValueError('Could not decode waveform')
            return peaks
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()


def waveform(src, stream=0):
    if type(stream) is not int or not 0 <= stream < 16:
        raise ValueError('The selected source audio stream is unavailable')
    path = _media_path(src)
    import raw_decode
    if raw_decode.is_raw_extension(path.name):
        # Camera RAW sound is a sidecar WAV (CinemaDNG/ARRIRAW sequences) or the container's own track.
        info = raw_decode.probe_raw(path)
        if info.get('rawAudioPath'):
            path = type(path)(info['rawAudioPath'])
        elif not info.get('audioStreams'):
            return []
    stat = path.stat()
    key = (str(path),stat.st_size,stat.st_mtime_ns,stream)
    with _lock:
        future = _pending.get(key)
        if not future:
            if len(_pending) >= 32:
                raise ValueError('Waveform queue is busy')
            future = _workers.submit(_compute,*key)
            _pending[key] = future
    try:
        return future.result(timeout=210)
    finally:
        with _lock:
            if _pending.get(key) is future and future.done():
                del _pending[key]
