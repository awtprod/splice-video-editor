"""One representation of the sequence start timecode across Splice.

The project format (engine.js, ``normalizeTimecodeSettings``) keeps ``startTimecode`` as a whole
number of frames at the project rate and ``dropFrame`` as true/false at 29.97 and 59.94 only.
Every producer and consumer must agree: the Resolve importer, the timeline interchange importers
and exporters, and Premiere save/open. Projects written by earlier builds hold the start as text;
they are read, never refused.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange import api  # noqa: E402
from interchange.cut import build_cut  # noqa: E402

NTSC = 30000 / 1001
CASES = [  # fps, start frames, dropFrame (None = not a drop-frame rate), what Splice shows
    (24, 86400, None, '01:00:00:00'),
    (NTSC, 107892, True, '01:00:00;00'),
    (NTSC, 108000, False, '01:00:00:00'),
    (25, 0, None, '00:00:00:00'),
]


def media_item(fps):
    rate = '30000/1001' if abs(fps - NTSC) < 1e-6 else str(fps)
    return {'id': 'm1', 'name': 'A001.mov', 'src': '', 'type': 'video', 'duration': 30, 'path': 'A001.mov',
            'sourcePath': '/offline/A001.mov', 'rate': rate, 'timecode': '01:00:00:00', 'dropFrame': False,
            'audioStreams': 1, 'audioChannels': 2}


def project(fps, start, drop, **extra):
    data = {'name': 'Start TC', 'width': 1920, 'height': 1080, 'fps': fps, 'media': [media_item(fps)],
            'clips': [{'id': 'v', 'mediaId': 'm1', 'type': 'video', 'track': 1, 'start': 0, 'duration': 2, 'in': 1,
                       'title': '', 'color': {}, 'volume': 100, 'transition': 'none', 'embeddedAudio': False,
                       'linkGroup': 'g'},
                      {'id': 'a', 'mediaId': 'm1', 'type': 'audio', 'track': -1, 'start': 0, 'duration': 2, 'in': 1,
                       'title': '', 'color': {}, 'volume': 100, 'transition': 'none', 'linkGroup': 'g'}],
            'markers': [], 'startTimecode': start, **extra}
    if drop is not None:
        data['dropFrame'] = drop
    return data


class StartTimecodeRoundTrip(unittest.TestCase):
    def test_export_follows_the_project_and_import_brings_it_back(self):
        for fps, start, drop, _label in CASES:
            with self.subTest(fps=fps, start=start, drop=drop):
                cut = build_cut(project(fps, start, drop), {})
                self.assertEqual((cut.tc_start, cut.drop), (start, bool(drop)))
                result = api.export_timeline(project(fps, start, drop), {'formats': ['fcpxml', 'xmeml', 'edl', 'otio']})
                for file in result['files']:
                    name = file['name']
                    if name.endswith('.edl') and '-A' in name:
                        continue
                    options = {'fps': fps} if name.endswith('.edl') else {}
                    back = api.import_timeline(name, file['content'], options)
                    self.assertEqual(back.get('startTimecode', 0), start, name)
                    if drop is None:
                        self.assertNotIn('dropFrame', back, name)
                    else:
                        self.assertIs(back['dropFrame'], drop, name)

    def test_dialog_options_still_win(self):
        cut = build_cut(project(NTSC, 107892, True), {'startTimecode': '00:00:00:00', 'dropFrame': False})
        self.assertEqual((cut.tc_start, cut.drop), (0, False))

    def test_text_start_from_earlier_builds_is_read_not_refused(self):
        cut = build_cut(project(24, '01:00:00:00', None), {})
        self.assertEqual(cut.tc_start, 86400)
        cut = build_cut(project(NTSC, '01:00:00;00', True), {})
        self.assertEqual((cut.tc_start, cut.drop), (107892, True))

    def test_export_range_is_the_inclusive_in_out(self):
        # Timeline In on frame 10, Out on frame 29 (inclusive): stored [10/24, 30/24).
        data = project(24, 86400, None)
        result = api.export_timeline(data, {'formats': ['edl'], 'range': {'start': 10 / 24, 'end': 30 / 24}})
        cut = build_cut(data, {'range': {'start': 10 / 24, 'end': 30 / 24}})
        self.assertEqual((cut.range_start, cut.frames), (10, 20))
        self.assertTrue(result['files'])


class PremiereStartTimecode(unittest.TestCase):
    def test_text_start_saves_as_frames(self):
        try:
            from premiere import open_project, save_project
        except ImportError as error:  # pragma: no cover - the Premiere bridge is optional
            self.skipTest(str(error))
        base = dict(name='TC', width=320, height=180, media=[], clips=[])
        saved = open_project(save_project(dict(base, fps=24, startTimecode='01:00:00:00')))
        self.assertEqual(saved['startTimecode'], 86400)
        saved = open_project(save_project(dict(base, fps=NTSC, startTimecode='01:00:00;00')))
        self.assertEqual((saved['startTimecode'], saved['dropFrame']), (107892, True))
        saved = open_project(save_project(dict(base, fps=NTSC, startTimecode=108000, dropFrame=False)))
        self.assertEqual((saved['startTimecode'], saved['dropFrame']), (108000, False))


if __name__ == '__main__':
    unittest.main()
