"""Synthetic media and timelines for the timeline interchange tests and evidence exports.

All media is generated with FFmpeg's test sources: nothing here comes from a real production.
"""
import subprocess
from fractions import Fraction
from pathlib import Path

RATE = Fraction(24000, 1001)
DF = Fraction(30000, 1001)


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *args], check=True, timeout=60)


def make_media(directory: Path, seconds: int = 12) -> dict:
    """Tiny files with embedded start timecode: two 23.976 A/V clips (01:00:00:00 and
    02:00:00:00), a 29.97 drop-frame clip (00:59:59;28), a 25 fps clip without timecode and a
    Broadcast WAV whose time reference is 01:00:00:00 at 23.976."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {name: directory / file for name, file in (
        ('camera_a', 'A001_C001_tc1h.mov'), ('camera_b', 'A001_C002_tc2h.mov'),
        ('drop', 'B001_C001_df.mov'), ('plain', 'plain_25.mp4'), ('tone', 'room_tone.wav'))}
    tone = ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
    if not paths['camera_a'].exists():
        ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=64x36:rate=24000/1001', *tone, '-t', str(seconds), '-ac', '2',
               '-c:v', 'mpeg4', '-q:v', '10', '-c:a', 'pcm_s16le', '-timecode', '01:00:00:00', str(paths['camera_a']))
    if not paths['camera_b'].exists():
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=64x36:rate=24000/1001', *tone, '-t', str(seconds), '-ac', '2',
               '-c:v', 'mpeg4', '-q:v', '10', '-c:a', 'pcm_s16le', '-timecode', '02:00:00:00', str(paths['camera_b']))
    if not paths['drop'].exists():
        ffmpeg('-f', 'lavfi', '-i', 'smptebars=size=64x36:rate=30000/1001', '-t', str(seconds),
               '-c:v', 'mpeg4', '-q:v', '10', '-timecode', '00:59:59;28', str(paths['drop']))
    if not paths['plain'].exists():
        ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=64x36:rate=25', '-t', str(seconds), '-c:v', 'mpeg4', '-q:v', '10',
               str(paths['plain']))
    if not paths['tone'].exists():
        ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000', '-t', str(seconds), '-ac', '2',
               '-c:a', 'pcm_s16le', '-write_bext', '1', '-metadata', 'time_reference=172972800', str(paths['tone']))
    return paths


def at(frames, rate=RATE) -> float:
    """Seconds of ``frames`` at ``rate``, as the float Splice stores."""
    return float(Fraction(frames) / rate)


def synthetic_project(paths: dict) -> dict:
    """A 23.976 timeline that touches every interchange feature."""
    def media(key, kind='video', **extra):
        return {'id': key, 'name': paths[key].name, 'src': '', 'type': kind, 'duration': 12,
                'path': paths[key].name, 'sourcePath': str(paths[key]), **extra}

    def clip(identity, key, start, length, source_in, track=1, kind='video', rate=RATE, **extra):
        return {'id': identity, 'mediaId': key, 'type': kind, 'track': track, 'start': at(start),
                'duration': at(start + length) - at(start), 'in': at(source_in, rate), 'title': '',
                'color': {'brightness': 100, 'contrast': 100, 'saturation': 100}, 'volume': 100,
                'transition': 'none', **extra}
    clips = [
        clip('v1-a', 'camera_a', 0, 48, 0),
        clip('v1-b', 'camera_b', 48, 48, 24, transition='dissolve', transitionDuration=at(12), transitionBefore=at(6)),
        clip('v1-slow', 'camera_a', 120, 48, 24, speed=.5),
        clip('v1-fast', 'camera_b', 168, 24, 0, speed=2),
        clip('v1-back', 'camera_a', 192, 24, 12, reverse=True),
        clip('v2-drop', 'drop', 24, 48, 6, track=2, rate=DF, opacity=50, transform={'x': 10, 'y': -5, 'scale': 80, 'rotation': 0},
             keyframes={'opacity': [{'time': 0, 'value': 20}, {'time': at(24), 'value': 50}]}),
        clip('v2-off', 'plain', 80, 20, 25, track=2, rate=Fraction(25), enabled=False),
        clip('v3-pic', 'camera_a', 216, 48, 48, track=3, embeddedAudio=False, linkGroup='jcut'),
        clip('a1-snd', 'camera_a', 204, 60, 36, track=-1, kind='audio', linkGroup='jcut'),
        clip('a2-tone', 'tone', 0, 120, 0, track=-2, kind='audio', volume=70, fadeIn=at(12), fadeOut=at(24),
             keyframes={'volume': [{'time': at(24), 'value': 70}, {'time': at(48), 'value': 35}]}),
        clip('a2-tone2', 'tone', 120, 80, 120, track=-2, kind='audio', transition='dissolve', transitionDuration=at(12)),
        {'id': 'title', 'mediaId': None, 'type': 'title', 'track': 4, 'start': at(0), 'duration': at(48), 'in': 0,
         'title': 'SYNTHETIC TURNOVER', 'color': {}, 'volume': 100, 'transition': 'none'},
    ]
    return {'name': 'Synthetic 23976 turnover', 'width': 1920, 'height': 1080, 'fps': float(RATE),
            'media': [media('camera_a'), media('camera_b'), media('drop'), media('plain'), media('tone', 'audio')],
            'clips': clips, 'markers': [{'id': 'mk1', 'name': 'Picture lock note', 'time': at(30)},
                                        {'id': 'mk2', 'name': 'Music in', 'time': at(130)}],
            'duration': at(264)}


def alpine_project(root: Path) -> dict:
    """The bundled starter project as engine.js builds it."""
    names = ['ridge', 'valley', 'forest', 'lake']
    labels = ['The Ridge', 'Into the Valley', 'Forest Light', 'The Lake']
    media = [{'id': n, 'name': l, 'src': f'assets/{n}.mp4', 'thumb': f'assets/{n}.jpg', 'duration': 9, 'type': 'video'}
             for n, l in zip(names, labels)]
    media.append({'id': 'score', 'name': 'Ambient score', 'src': 'assets/score.wav', 'duration': 32, 'type': 'audio'})
    clips = [{'id': f'clip-{i}', 'mediaId': n, 'start': i * 8, 'duration': 8, 'in': .5, 'track': 1, 'type': 'video',
              'title': '', 'color': {'brightness': 100, 'contrast': 100, 'saturation': 100}, 'volume': 100,
              'transition': 'dissolve' if i else 'none', 'transitionDuration': .48} for i, n in enumerate(names)]
    clips.append({'id': 'opening-title', 'mediaId': None, 'start': .8, 'duration': 3, 'in': 0, 'track': 4, 'type': 'title',
                  'title': 'ALPINE PASSAGE', 'color': {}, 'volume': 100, 'transition': 'none'})
    clips.append({'id': 'clip-score', 'mediaId': 'score', 'start': 0, 'duration': 32, 'in': 0, 'track': -1, 'type': 'audio',
                  'title': '', 'volume': 55, 'transition': 'none'})
    return {'name': 'Alpine Passage', 'width': 1920, 'height': 1080, 'fps': 25, 'media': media, 'clips': clips, 'duration': 32}
