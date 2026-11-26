"""Generated test material for the audio finishing tests (no recorded media in the repo)."""
import math
import random
import subprocess
import wave
from array import array


def speech_like(path, seconds, seed=1, rate=48000, channels=2):
    """Syllable-like bursts of three drifting partials (200 Hz to 3 kHz) with random lengths,
    gaps and levels: no repeating pattern, so a correlation has one clear peak."""
    rng = random.Random(seed)
    total = int(seconds*rate)
    samples = array('f', bytes(4*total))
    at = int(rng.uniform(.05, .3)*rate)
    while at < total:
        length = int(rng.uniform(.08, .32)*rate)
        level = rng.uniform(.08, .45)
        partials = [(rng.uniform(180, 420), rng.uniform(-200, 200)), (rng.uniform(600, 1400), rng.uniform(-400, 400)),
                    (rng.uniform(1800, 3000), rng.uniform(-500, 500))]
        phases = [rng.uniform(0, 2*math.pi) for _ in partials]
        for k in range(min(length, total-at)):
            u = k/length
            shape = math.sin(math.pi*u)**2
            t = k/rate
            value = sum(math.sin(phase+2*math.pi*(f+glide*u/2)*t)*weight
                        for (f, glide), phase, weight in zip(partials, phases, (1, .6, .3)))
            samples[at+k] = level*shape*value/1.9
        at += length+int(rng.uniform(.04, .6)*rate)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(channels); handle.setsampwidth(2); handle.setframerate(rate)
        pcm = array('h', (max(-32767, min(32767, round(value*32767))) for value in samples for _ in range(channels)))
        handle.writeframes(pcm.tobytes())
    return path


def camera_take(recorder, target, offset, seconds, *, video=True, fps=25, gain_db=-12, hiss=.004, encode='aac'):
    """A camera clip whose scratch sound starts ``offset`` seconds into ``recorder`` (negative:
    the camera rolled first), quieter, band-limited, with hiss, encoded lossy."""
    before = max(0.0, -offset)
    sound = (f'[1:a]volume={gain_db}dB,highpass=f=150,lowpass=f=7000' + (f',adelay={round(before*48000)}S:all=1' if before else '')
             + f'[s];[s][2:a]amix=inputs=2:normalize=0:duration=longest,atrim=end={seconds}[a]')
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-y']
    if video:
        command += ['-f', 'lavfi', '-i', f'color=c=gray:s=160x90:r={fps}:d={seconds}']
    else:
        command += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=0.01']
    command += [*(['-ss', f'{offset:.6f}'] if offset > 0 else []), '-i', str(recorder),
                '-f', 'lavfi', '-i', f'anoisesrc=c=white:a={hiss}:r=48000:d={seconds}', '-filter_complex', sound]
    command += (['-map', '0:v', '-c:v', 'libx264', '-pix_fmt', 'yuv420p'] if video else []) + ['-map', '[a]', '-t', str(seconds),
                '-c:a', encode] + (['-b:a', '128k'] if encode == 'aac' else []) + [str(target)]
    subprocess.run(command, check=True, timeout=120)
    return target


if __name__ == '__main__':
    import sys
    if sys.argv[1] == 'speech':
        speech_like(sys.argv[2], float(sys.argv[3]), int(sys.argv[4]))
    elif sys.argv[1] == 'take':
        camera_take(sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]))


# ---- shared helpers for tests/test_audio_*.py ----
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def ffmpeg(*arguments, timeout=180):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def pcm(path, channels=2, start=None, duration=None):
    """Interleaved float samples of a file's first audio stream (48 kHz)."""
    window = [*(['-ss', str(start)] if start is not None else []), *(['-t', str(duration)] if duration is not None else [])]
    return array('f', subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', *window, '-i', str(path), '-vn', '-ac', str(channels),
                                               '-ar', '48000', '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1'], timeout=300))


def independent_true_peak(path):
    """4x oversampled peak (dBTP) with the SoX resampler: a different interpolator from the
    limiter's own detector (libswresample) and from ebur128's."""
    log = subprocess.run(['ffmpeg', '-nostdin', '-v', 'info', '-i', str(path), '-vn', '-af',
                          'aresample=resampler=soxr:precision=28:osr=192000,astats=measure_perchannel=none:measure_overall=Peak_level',
                          '-f', 'null', '-'], capture_output=True, text=True, timeout=300).stderr
    import re
    found = re.findall(r'Peak level dB:\s+(-?[0-9.]+|-inf)', log)
    return -math.inf if found[-1] == '-inf' else float(found[-1])


def loudness(path, channels=2):
    """(integrated LUFS to 0.001, LRA LU, ebur128 true peak dBTP) as FFmpeg measures them."""
    import re
    log = Path(tempfile.mkdtemp(dir=Path(path).parent))/'r128.txt'
    report = subprocess.run(['ffmpeg', '-nostdin', '-nostats', '-v', 'info', '-i', str(path), '-vn', '-af',
                             'ebur128=peak=true:framelog=quiet:metadata=1,ametadata=mode=print:file='+str(log).replace(':', r'\:'),
                             '-f', 'null', '-'], capture_output=True, text=True, timeout=300).stderr
    values = re.findall(r'lavfi\.r128\.I=(-?[\d.]+|-inf|nan)', log.read_text())
    log.unlink(); log.parent.rmdir()
    summary = report[report.rfind('Integrated loudness:'):]
    lra = float(re.search(r'LRA:\s+(-?[\d.]+)', summary).group(1))
    peak = re.search(r'Peak:\s+(-?[\d.]+|-inf) dBFS', summary[summary.rfind('True peak:'):]).group(1)
    integrated = float(values[-1]) if values and values[-1] not in ('-inf', 'nan') else -math.inf
    return integrated, lra, (-math.inf if peak == '-inf' else float(peak))


def tone_level(samples, frequency, begin, end, channels=2, channel=0, rate=48000):
    """Amplitude of a steady tone in one channel between two seconds (single-bin DFT)."""
    first, stop = round(begin*rate), round(end*rate)
    step = 2*math.pi*frequency/rate
    real = imaginary = 0.0
    for k in range(first, stop):
        value = samples[k*channels+channel]
        real += value*math.cos(step*k); imaginary += value*math.sin(step*k)
    return 2*math.hypot(real, imaginary)/max(1, stop-first)


class AudioCase(unittest.TestCase):
    """A scratch folder under the media root and a one-call render."""
    def setUp(self):
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.media_root = MEDIA_ROOT

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(self.media_root))

    def source(self, name, graph, seconds=10, channels=2, rate=48000):
        path = self.directory/name
        if not path.exists():
            ffmpeg('-f', 'lavfi', '-i', graph, '-t', str(seconds), '-ar', str(rate), '-ac', str(channels), '-c:a', 'pcm_f32le', str(path))
        return path

    def render(self, clips, media, *, codec='wav', name='mix', chunk=None, events=None, report=None, fps=25, project=None, **options):
        import render_movie
        suffix = {'wav': '.wav', 'h264': '.mp4', 'prores': '.mov', 'stems': '.zip', 'loudness': '.json'}[codec]
        output = self.directory/(name+suffix)
        patches = {}
        if chunk: patches['AUDIO_CHUNK_SECONDS'] = chunk
        if events: patches['AUDIO_CHUNK_EVENTS'] = events
        body = dict(width=32, height=18, fps=fps, media=media, clips=clips, **(project or {}))
        with mock.patch.multiple(render_movie, **patches) if patches else contextlib.nullcontext():
            render_movie.render_to_file(body, output, options={'codec': codec, **options}, report=report)
        return output
