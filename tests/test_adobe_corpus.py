"""Adobe-authored projects from py-premiere's MIT-licensed regression corpus.

Source: https://github.com/Pouf/py-premiere/tree/ce75c9935cbf75f8b7492eb7c92ffed66d32677a/samples/models/minimal
The .prproj files were produced in Premiere; the .json files are upstream model
snapshots of those projects. These checks do not verify opening in Adobe Premiere.
"""
import gzip
import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import premiere
from premiere import open_project, save_project

CORPUS = Path(__file__).parent / 'fixtures'
NAMES = ('07_transitions', '10_audio_volume', '13_track_mute', '17_audio_mute',
         '20_track_lock', '21_audio_track_lock', '25_time_remap', '29_captions',
         '32_sync_lock', '34_generators', '39_transition_wipe',
         '63_audio_keyframes', '64_caption_style', '66_eg_text',
         '68_rate_override', '74_subsequence', '78_multicam_3angle',
         '82_caption_style_sweep')


def objects(data):
    root = ET.fromstring(gzip.decompress(data))
    return {item.get('ObjectID'): hashlib.sha256(ET.tostring(item)).hexdigest()
            for item in root if item.get('ObjectID')}


class AdobeCorpus(unittest.TestCase):
    def test_native_track_lock_and_sync_lock_fixture_roundtrip(self):
        cases = (('20_track_lock', 'video:1', 'locked'),
                 ('21_audio_track_lock', 'audio:-1', 'locked'),
                 ('32_sync_lock', 'video:1', 'syncLocked'))
        for name, key, field in cases:
            with self.subTest(name=name):
                source = (CORPUS / (name + '.prproj')).read_bytes()
                project = open_project(source, 1)
                self.assertEqual(project['trackState'][key][field], field != 'syncLocked')
                self.assertEqual(objects(source), objects(save_project(project)))
                project['trackState'][key][field] = field == 'syncLocked'
                changed = open_project(save_project(project), 1)
                self.assertEqual(changed['trackState'].get(key, {}).get(field, field == 'syncLocked'),
                                 field == 'syncLocked')

    def test_native_caption_text_style_add_remove_and_retime(self):
        source = (CORPUS / '29_captions.prproj').read_bytes()
        project = open_project(source)
        project['captions'][0].update(text='Edited line', start=.5, duration=1.2,
                                      fontSize=56, fillColor='#ff8800')
        project['captions'].pop()
        project['captions'].append({'id': 'new', 'start': 3, 'duration': 1.5,
                                    'text': 'New line', 'fontSize': 48})
        with tempfile.TemporaryDirectory() as folder, patch.object(premiere, 'MEDIA_ROOT', Path(folder)):
            reopened = open_project(save_project(project))
            self.assertEqual([caption['text'] for caption in reopened['captions']],
                             ['Edited line', 'New line'])
            self.assertAlmostEqual(reopened['captions'][0]['start'], .5, delta=.02)
            self.assertAlmostEqual(reopened['captions'][0]['duration'], 1.2, delta=.02)
            self.assertEqual(reopened['captions'][0]['fillColor'], '#ff8800')
            self.assertTrue(list((Path(folder) / 'captions').glob('*.srt')))

    def test_generator_bin_and_constant_speed_native_roundtrip(self):
        source = (CORPUS / '34_generators.prproj').read_bytes()
        project = open_project(source)
        generators = {item['name']: item for item in project['media']
                      if item.get('nativeGenerator') is not None}
        self.assertIn('Black Video', generators)
        self.assertIn('Color Matte', generators)
        self.assertIn('HD Bars and Tone', generators)
        clip = next(item for item in project['clips'] if item['type'] == 'video')
        clip['speed'] = 2
        clip['reverse'] = True
        project['clips'].append({'id': 'black', 'mediaId': generators['Black Video']['id'],
                                 'type': 'video', 'start': 6, 'duration': 2,
                                 'in': 0, 'track': 2, 'color': {}, 'volume': 100,
                                 'transition': 'none'})
        reopened = open_project(save_project(project))
        changed = next(item for item in reopened['clips'] if item['start'] == 0 and item['type'] == 'video')
        self.assertEqual((changed['speed'], changed['reverse']), (2, True))
        self.assertTrue(any(item['start'] == 6 and item['type'] == 'video' for item in reopened['clips']))

    def test_nested_native_time_remap_and_wipe_are_visible_to_export_gate(self):
        remap = open_project((CORPUS / '25_time_remap.prproj').read_bytes(), 1)
        self.assertTrue(any(clip['nativeTimeRemap'] for clip in remap['clips']))
        wipe = open_project((CORPUS / '39_transition_wipe.prproj').read_bytes(), 1)
        self.assertTrue(any(clip['transition'] == 'native' and
                            clip['transitionMatchName'] == 'ADBE Band Wipe'
                            for clip in wipe['clips']))

    def test_adobe_audio_track_mute_fixture_imports_and_roundtrips(self):
        source = (CORPUS / '17_audio_mute.prproj').read_bytes()
        opened = open_project(source)
        self.assertTrue(opened['trackMix']['-1']['mute'])
        saved = save_project(opened)
        self.assertEqual(objects(source), objects(saved))
        opened['trackMix']['-1']['mute'] = False
        self.assertFalse(open_project(save_project(opened))['trackMix'].get('-1', {}).get('mute', False))

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

    def test_track_mixer_roundtrip_and_unsupported_edits_fail_explicitly(self):
        source = (CORPUS / '63_audio_keyframes.prproj').read_bytes()
        project = open_project(source)
        project['trackMix'] = {'-1': {'gain': 70, 'pan': -30, 'mute': True, 'solo': False}}
        changed = save_project(project)
        self.assertEqual(open_project(changed)['trackMix']['-1'], project['trackMix']['-1'])
        self.assertEqual(open_project(save_project(open_project(changed)))['trackMix']['-1'], project['trackMix']['-1'])
        project['trackMix']['-1']['solo'] = True
        with self.assertRaisesRegex(ValueError, 'solo'):
            save_project(project)
        project.pop('trackMix')
        project['clips'][0]['color']['lut'] = {'title': 'Look'}
        with self.assertRaisesRegex(ValueError, 'LUT'):
            save_project(project)

    def test_new_premiere_sequence_writes_track_mixer(self):
        project = {'name': 'Mix', 'width': 1920, 'height': 1080, 'fps': 25,
                   'media': [], 'clips': [], 'trackMix': {
                       '-2': {'gain': 125, 'pan': 40, 'mute': True, 'solo': False}}}
        saved = save_project(project)
        self.assertEqual(open_project(saved)['trackMix']['-2'], project['trackMix']['-2'])

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
