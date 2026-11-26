"""Exports leave disabled clips out: no picture, no sound (their own camera sound included).

The editor sends ``enabled: false`` to the renderer unchanged; the renderer is the one place that
decides what a disabled clip contributes to a file. A tiny probe render (``disabled_probe``) checks
that the renderer honours the flag, and then a timeline with a disabled camera clip over a lower
track and a disabled audio clip is rendered and its decoded picture and sound are checked.
"""
import math
import statistics
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import premiere  # noqa: E402
from render_movie import render_to_file  # noqa: E402

FPS = 25


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1', *map(str, arguments)], check=True, timeout=60)


def frame_rgb(path, seconds):
    pixel = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{seconds:.3f}', '-i', str(path),
                                     '-frames:v', '1', '-vf', 'scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                    timeout=30)
    return list(pixel[:3])


def tone_levels(path, start, end, frequencies):
    """Goertzel magnitude of each frequency between two times of the file's (mono) sound."""
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', '8000',
                                   '-f', 's16le', 'pipe:1'], timeout=30)
    samples = struct.unpack(f'<{len(raw) // 2}h', raw)[int(start * 8000):int(end * 8000)]
    levels = {}
    for frequency in frequencies:
        coefficient = 2 * math.cos(2 * math.pi * frequency / 8000)
        previous = before = 0.0
        for sample in samples:
            previous, before = sample + coefficient * previous - before, previous
        power = previous * previous + before * before - coefficient * previous * before
        levels[frequency] = math.sqrt(max(power, 0)) / max(1, len(samples))
    return levels


def disabled_probe():
    """'honors' when the renderer leaves a disabled clip out, 'ignores' when it renders it."""
    clip = lambda identity, media, track, **extra: dict(id=identity, mediaId=media, type='video', track=track, start=0,
                                                        duration=.4, **{'in': 1}, color=extra.pop('color', {}),
                                                        volume=100, transition='none', **extra)
    project = dict(name='Disabled probe', width=64, height=36, fps=FPS,
                   media=[{'id': name, 'name': name, 'src': f'assets/{name}.mp4'} for name in ('ridge', 'valley')],
                   clips=[clip('dark', 'ridge', 1, color={'brightness': 0}), clip('top', 'valley', 2, enabled=False)])
    with tempfile.TemporaryDirectory(prefix='splice-disabled-probe-') as directory:
        output = Path(directory) / 'probe.mp4'
        render_to_file(project, output)
        gray = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-ss', '0.2', '-i', str(output),
                                        '-frames:v', '1', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'], timeout=30)
    return 'honors' if statistics.mean(gray) < 24 else 'ignores'


class DisabledClipsRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-disabled-')
        media = Path(cls.temp.name) / 'media'
        media.mkdir()
        # V1: red picture with a 300 Hz camera tone. V2 (disabled): blue with 700 Hz. A1 (disabled): 1100 Hz.
        for name, color, frequency in (('red', 'red', 300), ('blue', 'blue', 700)):
            ffmpeg('-f', 'lavfi', '-i', f'color=c={color}:s=64x36:r={FPS}:d=3', '-f', 'lavfi', '-i',
                   f'sine=frequency={frequency}:sample_rate=48000:d=3', '-c:v', 'libx264', '-preset', 'ultrafast',
                   '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', media / f'{name}.mp4')
        ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=1100:sample_rate=48000:d=3', media / 'music.wav')
        cls.patch = mock.patch.object(premiere, 'MEDIA_ROOT', media.resolve())
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.temp.cleanup()

    def test_disabled_clips_have_no_picture_or_sound(self):
        verdict = disabled_probe()
        self.assertEqual(verdict, 'honors',
                         'The renderer on this tree renders clips with `enabled: false`. It must leave them out of the '
                         'picture and the sound.')
        clip = lambda identity, media, kind, track, **extra: dict(id=identity, mediaId=media, type=kind, track=track,
                                                                  start=0, duration=2, **{'in': .5}, color={}, volume=100,
                                                                  transition='none', title='', **extra)
        item = lambda identity, name, kind: {'id': identity, 'name': name, 'src': f'/media/{name}', 'type': kind,
                                             'duration': 3, 'audioChannels': 1, 'audioStreams': 1}
        project = dict(name='Disabled', width=64, height=36, fps=FPS,
                       media=[item('red', 'red.mp4', 'video'), item('blue', 'blue.mp4', 'video'), item('music', 'music.wav', 'audio')],
                       clips=[clip('low', 'red', 'video', 1), clip('alt', 'blue', 'video', 2, enabled=False),
                              clip('score', 'music', 'audio', -1, enabled=False)])
        output = Path(self.temp.name) / 'disabled.mp4'
        render_to_file(project, output)
        red, green, blue = frame_rgb(output, 1)
        self.assertGreater(red, 150, 'V1 shows through the disabled V2 clip')
        self.assertLess(blue, 80, 'the disabled V2 clip adds no picture')
        levels = tone_levels(output, .5, 1.5, (300, 700, 1100))
        self.assertGreater(levels[300], 100, f'V1 camera sound plays: {levels}')
        self.assertLess(levels[700], levels[300] / 20, f'no camera sound from the disabled V2 clip: {levels}')
        self.assertLess(levels[1100], levels[300] / 20, f'no sound from the disabled audio clip: {levels}')
        # Re-enabled, the same clips are on top and audible: the checks above see the flag, not a broken file.
        for entry in project['clips']:
            entry.pop('enabled', None)
        render_to_file(project, output)
        self.assertGreater(frame_rgb(output, 1)[2], 150)
        enabled = tone_levels(output, .5, 1.5, (300, 700, 1100))
        self.assertGreater(min(enabled[700], enabled[1100]), 50, f'enabled clips are heard: {enabled}')


if __name__ == '__main__':
    print(disabled_probe())
