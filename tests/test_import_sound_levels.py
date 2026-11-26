"""Imported linked picture/sound pairs play the camera sound once.

A linked pair in Splice is a picture clip with no sound of its own (``embeddedAudio: false``) and
a sound clip linked to it. A project importer that leaves the picture's embedded sound on doubles
the level of every linked pair in an export (about 2x, +6 dB). Each importer here brings a linked
pair in, the production renderer exports it, and the level is measured against the source.
"""
import json
import math
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import premiere  # noqa: E402
import resolve  # noqa: E402
import resolve_fixtures as F  # noqa: E402
from interchange import api  # noqa: E402
from render_movie import render_to_file  # noqa: E402

FPS = 24


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *map(str, arguments)], check=True, timeout=60)


def rms(path, start, end):
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', '48000',
                                   '-f', 's16le', 'pipe:1'], timeout=30)
    samples = struct.unpack(f'<{len(raw) // 2}h', raw)[int(start * 48000):int(end * 48000)]
    return math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))


class ImportedPairsPlaySoundOnce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-levels-')
        cls.media = Path(cls.temp.name) / 'media'
        cls.media.mkdir()
        cls.camera = cls.media / 'A001.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'testsrc=size=64x36:rate={FPS}:d=8', '-f', 'lavfi', '-i',
               'sine=frequency=440:sample_rate=48000:d=8', '-af', 'volume=0.25', '-ac', '2', '-c:v', 'libx264',
               '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-timecode', '01:00:00:00', cls.camera)
        ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:d=10', '-af', 'volume=0.25',
               cls.media / 'A002.wav')
        cls.source_level = rms(cls.camera, 1.1, 1.9)
        cls.patch = mock.patch.object(premiere, 'MEDIA_ROOT', cls.media.resolve())
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.temp.cleanup()

    def online(self, project):
        for item in project['media']:
            name = Path(str(item.get('sourcePath') or item.get('path') or item.get('name') or '')).name
            if (self.media / name).is_file():
                item.update(src='/media/' + name, offline=False)
                item['duration'] = item.get('duration') or 8
        return project

    def level(self, project, start, end):
        output = Path(self.temp.name) / 'export.mp4'
        render_to_file(project, output)
        return rms(output, start, end) / self.source_level

    def native_pair(self):
        clip = dict(mediaId='m1', start=0, duration=2, title='', color={}, volume=100, transition='none', linkGroup='pair')
        clip['in'] = 1
        return {'name': 'Pair', 'width': 64, 'height': 36, 'fps': FPS, 'markers': [],
                'media': [{'id': 'm1', 'name': 'A001.mp4', 'src': '/media/A001.mp4', 'type': 'video', 'duration': 8,
                           'path': str(self.camera), 'sourcePath': str(self.camera), 'audioChannels': 2,
                           'audioStreams': 1, 'rate': str(FPS), 'timecode': '01:00:00:00'}],
                'clips': [dict(clip, id='v', type='video', track=1, embeddedAudio=False),
                          dict(clip, id='a', type='audio', track=-1)]}

    def assertOnce(self, project, start=.1, end=.9, label=''):
        pairs = {}
        for clip in project['clips']:
            if clip.get('linkGroup'):
                pairs.setdefault(clip['linkGroup'], []).append(clip)
        linked = [group for group in pairs.values() if {c['type'] for c in group} == {'video', 'audio'}]
        self.assertTrue(linked, label + ': the importer made no linked picture/sound pair')
        for group in linked:
            for clip in group:
                if clip['type'] == 'video':
                    self.assertIs(clip.get('embeddedAudio'), False, label + ': linked picture keeps its own sound')
        ratio = self.level(project, start, end)
        self.assertAlmostEqual(ratio, 1.0, delta=.2, msg=f'{label}: export level {ratio:.2f}x the camera sound')
        return ratio

    def test_native_pair_reference(self):
        self.assertOnce(self.native_pair(), label='native')

    def test_resolve_linked_pair(self):
        spec = F.sample(video_path=str(self.camera), audio_path=str(self.media / 'A002.wav'), generator=False)
        project = self.online(resolve.open_drp(F.drp_bytes(spec)))
        # The first second is the A001 picture and its linked A001 sound from source second 1.
        self.assertOnce(project, label='Resolve')

    def test_interchange_linked_pairs(self):
        files = api.export_timeline(self.native_pair(), {'formats': ['fcpxml', 'xmeml', 'otio', 'edl'], 'audioEdl': True})['files']
        seen = []
        for file in files:
            name = file['name']
            with self.subTest(format=name):
                options = {'fps': FPS} if name.endswith('.edl') else {}
                project = self.online(api.import_timeline(name, file['content'], options))
                if name.endswith('-V1.edl'):
                    # An EDL holds one track: the picture list brings no sound (the sound list does).
                    self.assertEqual([c.get('embeddedAudio') for c in project['clips']], [False])
                    self.assertLess(self.level(project, .1, .9), .05)
                elif name.endswith('.edl'):
                    self.assertEqual({c['type'] for c in project['clips']}, {'audio'})
                    self.assertAlmostEqual(self.level(dict(project, clips=project['clips'] + [
                        dict(project['clips'][0], id='pic', type='video', track=1, embeddedAudio=False)]), .1, .9), 1.0, delta=.2)
                else:
                    self.assertOnce(project, label=name)
                seen.append(name)
        self.assertEqual(len(seen), 5, seen)

    def test_premiere_linked_pair(self):
        project = self.online(premiere.open_project(premiere.save_project(self.native_pair())))
        self.assertOnce(project, label='Premiere')


if __name__ == '__main__':
    unittest.main()
