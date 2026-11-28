"""Guard edits that cross the native Premiere object graph boundary."""
import gzip
import subprocess
import xml.etree.ElementTree as ET
import tempfile
import unittest
from pathlib import Path

from premiere import MEDIA_ROOT, open_project, save_project

FIXTURE = Path(__file__).parent / 'fixtures' / '10_audio_volume.prproj'
TRACK_MUTE = Path(__file__).parent / 'fixtures' / '13_track_mute.prproj'


class PremiereRoundTrip(unittest.TestCase):
    def test_edit_adobe_saved_offline_audio(self):
        project = open_project(FIXTURE.read_bytes())
        clip = next(c for c in project['clips'] if c['type'] == 'audio')
        self.assertAlmostEqual(clip['volume'], 35, places=3)
        clip.update(volume=45, duration=1.2)
        reopened = open_project(save_project(project))
        edited = next(c for c in reopened['clips'] if c['type'] == 'audio')
        self.assertAlmostEqual(edited['duration'], 1.2)
        self.assertEqual(edited['volume'], 45)
        self.assertEqual(project['media'][0]['path'], reopened['media'][0]['path'])

    def test_wipe_alignment_resize_and_style_survive_native_save(self):
        media = [dict(id=n, src=f'assets/{n}.mp4') for n in ('ridge','valley')]
        clips = [dict(type='video', mediaId=m['id'], start=2*i, duration=2, track=1, **{'in':1})
                 for i,m in enumerate(media)]
        clips[1].update(transition='wipe',transitionDuration=.8)
        native = open_project(save_project(dict(name='Wipe',fps=25,clips=clips,media=media)))
        wipe = native['clips'][1]
        self.assertEqual((wipe['transition'],wipe['transitionMatchName'],wipe['transitionBefore']),('wipe','ADBE Wipe',0))
        self.assertAlmostEqual(wipe['transitionDuration'],.8)
        wipe.update(transition='dissolve',transitionDuration=.4,transitionBefore=.2)
        edited = open_project(save_project(native))['clips'][1]
        self.assertEqual(edited['transition'],'dissolve')
        self.assertAlmostEqual(edited['transitionDuration'],.4)
        self.assertAlmostEqual(edited['transitionBefore'],.2)

    def test_color_titles_and_dissolve(self):
        media = [{'id': name, 'src': f'assets/{name}.mp4'} for name in ('ridge', 'valley')]
        clips = [dict(type='video', mediaId=item['id'], start=i * 2, duration=2,
                      track=1, color={'saturation': 73}, transition='dissolve' if i else 'none',
                      **{'in': 1}) for i, item in enumerate(media)]
        clips.append(dict(type='title', title='Test title', start=.4, duration=1, track=2))
        saved = save_project(dict(media=media, clips=clips, name='Cut', fps=25))
        reopened = open_project(saved)
        self.assertEqual([c['type'] for c in reopened['clips']], ['video', 'video', 'title'])
        self.assertEqual(reopened['clips'][1]['transition'], 'dissolve')
        self.assertEqual(reopened['clips'][0]['color']['saturation'], 73)
        self.assertEqual(reopened['clips'][2]['title'], 'Test title')
        self.assertEqual(reopened['fps'], 25)


    def test_muted_premiere_video_track_imports_hidden_and_saves_back(self):
        project = open_project(TRACK_MUTE.read_bytes())
        self.assertIs(project['trackState']['video:1']['hidden'], True)
        project['trackState']['video:1'].pop('hidden')
        shown = open_project(save_project(project))
        self.assertNotIn('hidden', shown['trackState'].get('video:1', {}))
        shown.setdefault('trackState', {})['video:1'] = {'locked': False, 'syncLocked': True, 'hidden': True}
        hidden = open_project(save_project(shown))
        self.assertIs(hidden['trackState']['video:1']['hidden'], True)

    def test_new_project_saves_hidden_video_track(self):
        media = [dict(id='ridge', src='assets/ridge.mp4')]
        clips = [dict(type='video', mediaId='ridge', start=0, duration=2, track=2, **{'in': 0})]
        state = {'video:2': {'locked': False, 'syncLocked': True, 'hidden': True}}
        reopened = open_project(save_project(dict(name='Hidden', fps=25, clips=clips, media=media, trackState=state)))
        self.assertIs(reopened['trackState']['video:2']['hidden'], True)
        self.assertNotIn('video:1', reopened['trackState'])

    def test_sequence_start_timecode_and_drop_frame_round_trip(self):
        # Adobe-authored corpus: Seq A starts 29 frames in and displays 29.97 drop-frame (format 102).
        adobe = open_project(TRACK_MUTE.read_bytes())
        self.assertEqual((adobe['startTimecode'], adobe['dropFrame']), (29, True))
        second = open_project(TRACK_MUTE.read_bytes(), 1)
        self.assertNotIn('startTimecode', second)
        # Editing the native sequence keeps or changes both, as Splice asks.
        self.assertEqual(open_project(save_project(adobe))['startTimecode'], 29)
        adobe.update(startTimecode=107892, dropFrame=False)
        changed = open_project(save_project(adobe))
        self.assertEqual((changed['startTimecode'], changed['dropFrame']), (107892, False))
        # A new sequence: 01:00:00;00 at 29.97 drop-frame is frame 107892; non-drop is 108000.
        media = [dict(id='ridge', src='assets/ridge.mp4')]
        clips = [dict(type='video', mediaId='ridge', start=0, duration=2, track=1, **{'in': 0})]
        for drop, start in ((True, 107892), (False, 108000)):
            project = dict(name='NTSC', fps=30000 / 1001, clips=clips, media=media, startTimecode=start, dropFrame=drop)
            reopened = open_project(save_project(project))
            self.assertEqual((reopened['startTimecode'], reopened['dropFrame']), (start, drop))
        # Rates without drop-frame keep their start and report no drop-frame setting.
        reopened = open_project(save_project(dict(name='PAL', fps=25, clips=clips, media=media, startTimecode=90000)))
        self.assertEqual(reopened['startTimecode'], 90000)
        self.assertNotIn('dropFrame', reopened)

    def test_title_and_video_lanes_save_as_one_track(self):
        media = [dict(id='ridge', src='assets/ridge.mp4')]
        clips = [dict(type='video', mediaId='ridge', start=0, duration=2, track=1, **{'in': 0}),
                 dict(type='title', title='Lower third', start=0, duration=2, track=2)]
        # A hidden title lane hides its Premiere track; it reopens as that video track, hidden.
        hidden = dict(name='Titles', fps=25, clips=clips, media=media,
                      trackState={'title:2': {'locked': False, 'syncLocked': True, 'hidden': True}})
        reopened = open_project(save_project(hidden))
        self.assertIs(reopened['trackState']['video:2']['hidden'], True)
        # Different states on the two lanes of one track merge instead of failing the save.
        mixed = dict(hidden, trackState={'video:2': {'locked': False, 'syncLocked': True, 'hidden': True},
                                         'title:2': {'locked': True, 'syncLocked': True}})
        merged = open_project(save_project(mixed))['trackState']['video:2']
        self.assertEqual((merged['locked'], merged.get('hidden')), (True, True))

    def test_disabled_clips_save_as_disabled_track_items_and_reopen(self):
        """Premiere keeps Clip > Enable off as ClipTrackItem/IsMuted on the track item (py-premiere's
        is_disabled); the corpus has no disabled clip, so this writes one and reads it back."""
        media = [dict(id=n, src=f'assets/{n}.mp4') for n in ('ridge', 'valley')]
        clips = [dict(id='low', type='video', mediaId='ridge', start=0, duration=2, track=1, **{'in': 1}),
                 dict(id='alt', type='video', mediaId='valley', start=0, duration=2, track=2, enabled=False, **{'in': 1}),
                 dict(id='after', type='video', mediaId='valley', start=2, duration=1, track=1, enabled=True, **{'in': 1})]
        saved = save_project(dict(name='Alternates', fps=25, clips=clips, media=media))
        root = ET.fromstring(gzip.decompress(saved))
        flags = [item.findtext('ClipTrackItem/IsMuted') for item in root.iter('VideoClipTrackItem')]
        self.assertEqual(sorted(flag or '' for flag in flags), ['', '', 'true'])
        reopened = open_project(saved)
        by_place = {(clip['track'], round(clip['start'], 3)): clip for clip in reopened['clips']}
        self.assertIs(by_place[(2, 0)]['enabled'], False)
        self.assertNotIn('enabled', by_place[(1, 0)])
        self.assertNotIn('enabled', by_place[(1, 2)])
        # Re-enabled in Splice: the native flag goes away on the next save; disabling another works too.
        by_place[(2, 0)].pop('enabled')
        by_place[(1, 2)]['enabled'] = False
        again = open_project(save_project(reopened))
        states = {(clip['track'], round(clip['start'], 3)): clip.get('enabled', True) for clip in again['clips']}
        self.assertEqual(states, {(1, 0): True, (2, 0): True, (1, 2): False})

    def test_track_mute_is_not_a_disabled_clip(self):
        project = open_project(TRACK_MUTE.read_bytes())
        self.assertFalse([clip for clip in project['clips'] if clip.get('enabled') is False])

    def test_disabled_camera_clip_disables_its_placed_sound(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            camera = Path(directory) / 'camera.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x36:r=25:d=3',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(camera)], check=True)
            media = [dict(id='camera', type='video', src='/media/' + str(camera.relative_to(MEDIA_ROOT)), duration=3)]
            clips = [dict(id='v1', type='video', mediaId='camera', start=0, duration=2, track=1, enabled=False, **{'in': 0})]
            saved = save_project(dict(name='Camera', fps=25, clips=clips, media=media))
        root = ET.fromstring(gzip.decompress(saved))
        flags = [item.findtext('ClipTrackItem/IsMuted') for tag in ('VideoClipTrackItem', 'AudioClipTrackItem') for item in root.iter(tag)]
        self.assertEqual(flags, ['true', 'true'])
        self.assertTrue(all(clip.get('enabled') is False for clip in open_project(saved)['clips']))

    def test_split_camera_sound_saves_linked_and_out_of_sync(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            camera = Path(directory) / 'camera.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x36:r=25:d=6',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(camera)], check=True)
            media = [dict(id='camera', type='video', src='/media/' + str(camera.relative_to(MEDIA_ROOT)), duration=6)]
            # An L-cut: the sound runs one second past the picture; a J-cut on the next pair leads by half a second.
            clips = [dict(id='v1', type='video', mediaId='camera', start=0, duration=2, track=1, embeddedAudio=False,
                          linkGroup='first', **{'in': 1}),
                     dict(id='a1', type='audio', mediaId='camera', start=0, duration=3, track=-1, linkGroup='first', **{'in': 1}),
                     dict(id='v2', type='video', mediaId='camera', start=2, duration=2, track=1, embeddedAudio=False,
                          linkGroup='second', **{'in': 3}),
                     dict(id='a2', type='audio', mediaId='camera', start=3, duration=1, track=-2, linkGroup='second', **{'in': 4})]
            reopened = open_project(save_project(dict(name='Split edits', fps=25, clips=clips, media=media)))
        found = {(clip['type'], round(clip['start'], 3), round(clip['duration'], 3), round(clip['in'], 3)): clip
                 for clip in reopened['clips']}
        self.assertEqual(sorted(found), [('audio', 0, 3, 1), ('audio', 3, 1, 4), ('video', 0, 2, 1), ('video', 2, 2, 3)])
        groups = {}
        for (kind, start, _, _), clip in found.items():
            groups.setdefault(clip.get('linkGroup'), set()).add((kind, start))
        self.assertNotIn(None, groups)
        self.assertEqual(sorted(map(sorted, groups.values())),
                         [[('audio', 0), ('video', 0)], [('audio', 3), ('video', 2)]])
        self.assertTrue(all(clip.get('embeddedAudio') is False for clip in reopened['clips'] if clip['type'] == 'video'))


if __name__ == '__main__':
    unittest.main()
