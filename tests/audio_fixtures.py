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
