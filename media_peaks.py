"""Build a compact waveform without decoding a whole reel into browser memory."""
from array import array
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys
import threading

from premiere import _media_path
from splice_platform import HIDE_CONSOLE

_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='waveform')
_pending = {}
_lock = threading.Lock()


@lru_cache(maxsize=256)
def _compute(path, size, modified):
    info = subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file','-select_streams','a:0',
                           '-show_entries','stream=codec_type:format=duration','-of','json',path],
                          capture_output=True,timeout=15,check=True,creationflags=HIDE_CONSOLE)
    data = json.loads(info.stdout)
    if not data.get('streams'):
        return []
    duration = float(data['format']['duration'])
    if not 0 < duration <= 86400:
        raise ValueError('Waveform duration is unavailable')
    # 2048 peak bins, sampled at 4 kHz. Read at most one bin per iteration.
    block = max(1, round(duration * 4000 / 2048)) * 2
    cmd = ['ffmpeg','-nostdin','-loglevel','error','-threads','1','-protocol_whitelist','file',
           '-i',path,'-map','0:a:0','-vn','-ac','1','-ar','4000','-t',str(duration),
           '-f','s16le','pipe:1']
    with subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=HIDE_CONSOLE) as process:
        timer = threading.Timer(180,process.kill)
        timer.start()
        try:
            peaks=[]
            while chunk := process.stdout.read(block):
                samples=array('h')
                samples.frombytes(chunk[:len(chunk)//2*2])
                if sys.byteorder=='big':
                    samples.byteswap()
                if samples:
                    peaks.append(max(max(samples),-min(samples))/32768)
            if process.wait():
                raise ValueError('Could not decode waveform')
            return peaks
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()


def waveform(src):
    path = _media_path(src)
    stat = path.stat()
    key = (str(path),stat.st_size,stat.st_mtime_ns)
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
