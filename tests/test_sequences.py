import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))
import py_premiere
from premiere import open_project, save_project


class NativeSequenceTests(unittest.TestCase):
    def test_supported_rates(self):
        for rate in (24000/1001, 24, 25, 30000/1001, 30, 50, 60000/1001, 60):
            with self.subTest(rate=rate):
                project = {'name': 'Rate', 'width': 800, 'height': 600,
                           'fps': rate, 'media': [], 'clips': []}
                opened = open_project(save_project(project))
                self.assertAlmostEqual(opened['fps'], rate, places=3)

    def test_audio_move_preserves_title(self):
        project = {'name': 'Tracks', 'fps': 25,
                   'media': [{'id': 's', 'src': 'assets/score.wav'}],
                   'clips': [{'id': 'c', 'type': 'audio', 'mediaId': 's',
                              'track': -2, 'start': 0, 'in': 0, 'duration': 2},
                             {'id': 't', 'type': 'title', 'track': 4,
                              'start': 0, 'duration': 1, 'title': 'Hello'}]}
        opened = open_project(save_project(project))
        audio = next(c for c in opened['clips'] if c['type'] == 'audio')
        audio['track'] = -3
        reopened = open_project(save_project(opened))
        self.assertEqual(next(c for c in reopened['clips'] if c['type'] == 'audio')['track'], -3)
        self.assertEqual(next(c for c in reopened['clips'] if c['type'] == 'title')['title'], 'Hello')

    def test_selected_sequence_preserves_other_sequence(self):
        with tempfile.TemporaryDirectory() as folder:
            project = py_premiere.new().project
            project.add_sequence('First', '1080p25')
            project.add_sequence('Second', '1080p25')
            path = Path(folder) / 'two.prproj'
            project.save(path)
            edit = open_project(path.read_bytes(), index=1)
            self.assertEqual(edit['premiereSequences'], ['First', 'Second'])
            edit['name'] = 'Second edit'
            saved = save_project(edit)
            self.assertEqual(open_project(saved, index=0)['name'], 'First')
            self.assertEqual(open_project(saved, index=1)['name'], 'Second edit')

    def test_new_dimensions_track_move_and_bin(self):
        source = {'name': 'Wide edit', 'width': 1440, 'height': 900, 'fps': 25,
                  'media': [{'id': 'r', 'src': 'assets/ridge.mp4', 'bin': 'Day 1/Camera A'}],
                  'clips': [{'id': 'c', 'type': 'video', 'mediaId': 'r', 'track': 2,
                             'start': 0, 'duration': 2, 'in': 1}],
                  'markers': [{'id': 'm', 'name': 'Good take', 'time': 1}]}
        opened = open_project(save_project(source))
        self.assertEqual((opened['width'], opened['height'], opened['fps']), (1440, 900, 25))
        self.assertEqual(opened['media'][0]['bin'], 'Day 1/Camera A')
        self.assertEqual(opened['clips'][0]['track'], 2)
        self.assertEqual(opened['markers'][0]['name'], 'Good take')
        opened['clips'][0]['track'] = 3
        reopened = open_project(save_project(opened))
        self.assertEqual(reopened['clips'][0]['track'], 3)
        self.assertEqual(reopened['media'][0]['bin'], 'Day 1/Camera A')


if __name__ == '__main__':
    unittest.main()
