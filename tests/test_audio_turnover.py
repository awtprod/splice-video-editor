"""Turnover (audio finishing item 9): what the interchange formats cannot carry of the new
audio work is listed, per clip and per track, in the turnover report."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from interchange_fixtures import make_media, synthetic_project  # noqa: E402
from interchange.api import FORMATS, export_timeline  # noqa: E402


class AudioLossReport(unittest.TestCase):
    def test_effects_automation_routing_and_stems_are_listed(self):
        with tempfile.TemporaryDirectory(prefix='splice-turnover-') as directory:
            project = synthetic_project(make_media(Path(directory)))
            sound = next(clip for clip in project['clips'] if clip['type'] == 'audio')
            sound.update(audioFx={'eq': {'bands': [{'type': 'highpass', 'freq': 80}]}, 'comp': {'threshold': -20}},
                         channelMap='left', audioTransition='dip',
                         keyframes={'volume': [{'time': 0, 'value': 100, 'ease': True}, {'time': 1, 'value': 50}]})
            track = str(sound['track'])
            project['trackMix'] = {track: {'fx': {'gate': {'threshold': -50}}, 'keyframes': {'pan': [{'time': 0, 'value': 10}]},
                                           'stem': 'Dialogue', 'route': 'center', 'channels': 'mono'}}
            project['masterFx'] = {'comp': {'threshold': -18}}
            report = export_timeline(project, {'formats': list(FORMATS)})['report']
        for phrase in ('its audio EQ, compressor are not carried', 'channel mapping (left)', 'sound transition override (dip)',
                       'eased volume keyframes are exported as linear ramps',
                       f'Audio track A{abs(int(track))}: gate, pan automation, stem "Dialogue", 5.1 placement (center), mono track not carried',
                       'Master bus compressor not carried'):
            self.assertIn(phrase, report)


if __name__ == '__main__':
    unittest.main()
