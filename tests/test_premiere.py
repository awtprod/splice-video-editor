"""Guard edits that cross the native Premiere object graph boundary."""
import unittest
from pathlib import Path

from premiere import open_project, save_project

FIXTURE = Path(__file__).parent / 'fixtures' / '10_audio_volume.prproj'


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


if __name__ == '__main__':
    unittest.main()
