"""Adobe-authored projects from py-premiere's MIT-licensed regression corpus.

Source: https://github.com/Pouf/py-premiere/tree/ce75c9935cbf75f8b7492eb7c92ffed66d32677a/samples/models/minimal
The .prproj files were produced in Premiere; the .json files are upstream model
snapshots of those projects. These checks do not verify opening in Adobe Premiere.
"""
import gzip
import hashlib
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from premiere import open_project, save_project

CORPUS = Path(__file__).parent / 'fixtures'
NAMES = ('07_transitions', '10_audio_volume', '25_time_remap',
         '39_transition_wipe', '63_audio_keyframes', '66_eg_text',
         '74_subsequence', '78_multicam_3angle')


def objects(data):
    root = ET.fromstring(gzip.decompress(data))
    return {item.get('ObjectID'): hashlib.sha256(ET.tostring(item)).hexdigest()
            for item in root if item.get('ObjectID')}


class AdobeCorpus(unittest.TestCase):
    def test_nested_sequence_has_renderable_child_model_and_keeps_native_link(self):
        for name in ('74_subsequence', '78_multicam_3angle'):
            with self.subTest(name=name):
                source = (CORPUS / (name + '.prproj')).read_bytes()
                opened = open_project(source, 1)
                self.assertEqual(len(opened['clips']), 2)
                for clip in opened['clips']:
                    self.assertEqual(clip['nestedSequence'], 0)
                    media = next(item for item in opened['media'] if item['id'] == clip['mediaId'])
                    self.assertEqual(media['nestedSequence'], 0)
                    self.assertEqual(media['nestedProject']['name'], 'Seq A')
                    self.assertEqual(len(media['nestedProject']['clips']), 2)
                    self.assertNotIn('premiereOriginal', media['nestedProject'])
                saved = save_project(opened)
                self.assertEqual(objects(source), objects(saved))

    def test_unsupported_lut_and_track_mixer_edits_fail_explicitly(self):
        source = (CORPUS / '63_audio_keyframes.prproj').read_bytes()
        project = open_project(source)
        project['trackMix'] = {'-1': {'gain': 70, 'pan': 0, 'mute': False, 'solo': False}}
        with self.assertRaisesRegex(ValueError, 'track mixer'):
            save_project(project)
        project.pop('trackMix')
        project['clips'][0]['color']['lut'] = {'title': 'Look'}
        with self.assertRaisesRegex(ValueError, 'LUT'):
            save_project(project)

    def test_upstream_model_json_matches_opened_primary_sequence(self):
        """Compare timeline facts with upstream's model snapshots."""
        for reference in sorted(CORPUS.glob('*.json')):
            with self.subTest(name=reference.stem):
                expected = json.loads(reference.read_text())['sequences'][0]
                opened = open_project(reference.with_suffix('.prproj').read_bytes())
                self.assertEqual(opened['name'], expected['name'])
                self.assertEqual((opened['width'], opened['height']),
                                 (expected['frameSizeHorizontal'], expected['frameSizeVertical']))
                self.assertAlmostEqual(opened['fps'], 254016000000 / int(expected['timebase']), places=4)
                for kind, tracks in (('video', expected['videoTracks']),
                                     ('audio', expected['audioTracks'])):
                    for ordinal, track in enumerate(tracks):
                        actual = [clip for clip in opened['clips']
                                  if clip['type'] == kind and clip['premiereTrack'] == ordinal]
                        self.assertEqual(len(actual), len(track['clips']))
                        for source_clip, clip in zip(track['clips'], actual):
                            self.assertAlmostEqual(clip['start'], source_clip['start']['seconds'], places=4)
                            self.assertAlmostEqual(clip['duration'], source_clip['duration']['seconds'], places=4)

    def test_unchanged_save_preserves_every_native_object(self):
        for name in NAMES:
            with self.subTest(name=name):
                source = (CORPUS / (name + '.prproj')).read_bytes()
                reopened = save_project(open_project(source))
                self.assertEqual(objects(source), objects(reopened))

    def test_targeted_timing_edit_preserves_unrelated_objects(self):
        for name in NAMES:
            with self.subTest(name=name):
                source = (CORPUS / (name + '.prproj')).read_bytes()
                project = open_project(source)
                clip = next(c for c in project['clips'] if c['type'] == 'video')
                clip['start'] += 1
                saved = save_project(project)
                before, after = objects(source), objects(saved)
                changed = {identity for identity, digest in before.items()
                           if after.get(identity) != digest}
                self.assertTrue(changed)
                self.assertLessEqual(len(changed), 6)
                self.assertLessEqual(len(set(before) - set(after)), 2)
                original_root = ET.fromstring(gzip.decompress(source))
                changed_tags = {node.tag for node in original_root
                                if node.get('ObjectID') in changed}
                self.assertLessEqual(changed_tags, {
                    'AudioSequenceSource', 'VideoSequenceSource',
                    'VideoClipTrackItem', 'VideoTransitionTrackItem',
                })
                played = open_project(saved)
                moved = next(c for c in played['clips'] if c['id'] == clip['id'])
                self.assertAlmostEqual(moved['start'], clip['start'], places=4)

    def test_proxies_survive_creating_first_sequence(self):
        source = (CORPUS / '18_proxy.prproj').read_bytes()
        project = open_project(source)
        self.assertFalse(project['premiereSequences'])
        saved = save_project(project)
        self.assertEqual(len(open_project(saved)['premiereSequences']), 1)
        self.assertTrue(set(objects(source)).issubset(set(objects(saved))))


if __name__ == '__main__':
    unittest.main()
