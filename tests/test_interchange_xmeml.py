"""Final Cut Pro 7 XML (xmeml 5): structure in Premiere Pro's own conventions, and the round trip."""
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange import api, xmeml, xmlsafe  # noqa: E402
from interchange.cut import build_cut  # noqa: E402
from interchange.timecode import snap_rate  # noqa: E402
from interchange_fixtures import DF, RATE, alpine_project, at, make_media, synthetic_project  # noqa: E402

TICKS = 254_016_000_000


class XmemlCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-xmeml-')
        cls.paths = make_media(Path(cls.temp.name))
        cls.rates = {cls.paths['camera_a'].name: RATE, cls.paths['camera_b'].name: RATE, cls.paths['drop'].name: DF,
                     cls.paths['plain'].name: Fraction(25), cls.paths['tone'].name: RATE}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def export(self, project=None, **options):
        return xmeml.write(build_cut(project or synthetic_project(self.paths), options))

    def root(self, project=None, **options):
        return xmlsafe.parse(self.export(project, **options).encode())

    def items(self, root, mediatype):
        for index, track in enumerate(root.findall(f'sequence/media/{mediatype}/track'), 1):
            for item in track:
                if item.tag in ('clipitem', 'generatoritem', 'transitionitem'):
                    yield index, track, item


class Structure(XmemlCase):
    def test_document_sequence_rate_and_timecode(self):
        text = self.export()
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="5">'))
        root = xmlsafe.parse(text.encode())
        sequence = root.find('sequence')
        self.assertEqual(sequence.findtext('name'), 'Synthetic 23976 turnover')
        self.assertEqual(sequence.findtext('duration'), '264')
        self.assertEqual((sequence.findtext('rate/timebase'), sequence.findtext('rate/ntsc')), ('24', 'TRUE'))
        timecode = sequence.find('timecode')
        self.assertEqual([timecode.findtext(t) for t in ('string', 'frame', 'displayformat')], ['01:00:00:00', '86400', 'NDF'])
        chars = sequence.find('media/video/format/samplecharacteristics')
        self.assertEqual((chars.findtext('width'), chars.findtext('height')), ('1920', '1080'))
        self.assertEqual([(m.findtext('name'), m.findtext('in'), m.findtext('out')) for m in sequence.iterfind('marker')],
                         [('Picture lock note', '30', '-1'), ('Music in', '130', '-1')])

    def test_files_defined_once_with_timecode_reel_and_characteristics(self):
        root = self.root()
        defined = [f for f in root.iter('file') if len(f)]
        self.assertEqual(len(defined), len({f.get('id') for f in defined}), 'each file is defined once')
        self.assertEqual(len(defined), 5)
        ids = {f.get('id') for f in defined}
        for file_ in root.iter('file'):
            self.assertIn(file_.get('id'), ids)
        by_name = {f.findtext('name'): f for f in defined}
        drop = by_name['B001_C001_df.mov']
        self.assertTrue(drop.findtext('pathurl').startswith('file://localhost/'))
        self.assertEqual((drop.findtext('rate/timebase'), drop.findtext('rate/ntsc')), ('30', 'TRUE'))
        self.assertEqual([drop.findtext(f'timecode/{t}') for t in ('string', 'frame', 'displayformat')], ['00;59;59;28', '107890', 'DF'])
        camera = by_name['A001_C001_tc1h.mov']
        self.assertEqual(camera.findtext('timecode/string'), '01:00:00:00')
        self.assertEqual(camera.findtext('media/audio/channelcount'), '2')
        self.assertEqual(camera.findtext('media/video/samplecharacteristics/width'), '64')
        tone = by_name['room_tone.wav']
        self.assertIsNone(tone.find('media/video'))
        self.assertEqual(tone.findtext('timecode/frame'), '86400', 'Broadcast WAV time reference as the start')

    def test_pathurl_forms(self):
        self.assertEqual(xmeml.pathurl('/Volumes/RAID/Day 1/a#1.mov'), 'file://localhost/Volumes/RAID/Day%201/a%231.mov')
        self.assertEqual(xmeml.pathurl(r'C:\Media\Day 1\a.mov'), 'file://localhost/C%3a/Media/Day%201/a.mov')
        self.assertEqual(xmeml.pathurl(r'\\nas\share\b.mxf'), 'file://nas/share/b.mxf')
        for path in ('/Volumes/RAID/Day 1/a#1.mov', r'C:\Media\Day 1\a.mov', r'\\nas\share\b.mxf'):
            self.assertEqual(xmeml.path_from_pathurl(xmeml.pathurl(path)), path)

    def test_clip_timing_transitions_and_edges(self):
        root = self.root()
        v1 = root.findall('sequence/media/video/track')[0]
        items = [i for i in v1 if i.tag in ('clipitem', 'transitionitem')]
        first, dissolve, second = items[:3]
        self.assertEqual([first.findtext(t) for t in ('start', 'end', 'in', 'out')], ['0', '-1', '0', '54'])
        self.assertEqual([dissolve.findtext(t) for t in ('start', 'end', 'alignment')], ['42', '54', 'center'])
        self.assertEqual(dissolve.findtext('cutPointTicks'), str(round(Fraction(6) / RATE * TICKS)))
        self.assertEqual(dissolve.findtext('effect/effectid'), 'Cross Dissolve')
        self.assertEqual([second.findtext(t) for t in ('start', 'end', 'in', 'out')], ['-1', '96', '18', '72'])
        self.assertEqual(second.findtext('pproTicksIn'), str(round(Fraction(18) / RATE * TICKS)))
        slow, fast, back = items[3:6]
        self.assertEqual([slow.findtext(t) for t in ('start', 'end', 'in', 'out')], ['120', '168', '48', '96'])
        self.assertEqual(slow.findtext('pproTicksIn'), str(round(Fraction(24) / RATE * TICKS)), 'ticks are source time')
        remap = slow.find("filter/effect[effectid='timeremap']")
        self.assertEqual(remap.find("parameter[parameterid='speed']").findtext('value'), '50')
        self.assertEqual(fast.find("filter/effect[effectid='timeremap']/parameter[parameterid='speed']").findtext('value'), '200')
        self.assertEqual(back.find("filter/effect[effectid='timeremap']/parameter[parameterid='reverse']").findtext('value'), 'TRUE')
        media_frames = int(back.findtext('duration'))
        self.assertEqual((int(back.findtext('in')), int(back.findtext('out'))), (media_frames - 36, media_frames - 12),
                         'reverse in/out count the mirrored media')
        audio = root.findall('sequence/media/audio/track')
        crossfade = next(i for t in audio for i in t if i.tag == 'transitionitem' and i.findtext('effect/mediatype') == 'audio'
                         and i.findtext('start') == '120')
        self.assertEqual((crossfade.findtext('alignment'), crossfade.findtext('effect/effectid')), ('start', 'KGAudioTransCrossFade0dB'))

    def test_mixed_rate_source_is_exact_in_ticks(self):
        v2 = self.root().findall('sequence/media/video/track')[1]
        drop = next(i for i in v2 if i.tag == 'clipitem')
        self.assertEqual([drop.findtext(t) for t in ('start', 'end', 'in', 'out')], ['24', '72', '5', '53'])
        self.assertEqual(drop.findtext('pproTicksIn'), str(round(Fraction(6) / DF * TICKS)))

    def test_enabled_filters_links_and_generator(self):
        root = self.root()
        clips = {(i.findtext('name'), i.findtext('start')): i for _, _, i in self.items(root, 'video') if i.tag == 'clipitem'}
        self.assertEqual(clips[('plain_25.mp4', '80')].findtext('enabled'), 'FALSE')
        drop = clips[('B001_C001_df.mov', '24')]
        opacity = drop.find("filter/effect[effectid='opacity']/parameter")
        self.assertEqual(opacity.findtext('value'), '50')
        self.assertEqual([(k.findtext('when'), k.findtext('value')) for k in opacity.iterfind('keyframe')], [('5', '20'), ('29', '50')])
        motion = drop.find("filter/effect[effectid='basic']")
        self.assertEqual(motion.find("parameter[parameterid='scale']").findtext('value'), '80')
        self.assertEqual((motion.findtext("parameter[parameterid='center']/value/horiz"),
                          motion.findtext("parameter[parameterid='center']/value/vert")), ('0.1', '-0.05'))
        audio_items = {i.get('id'): (index, i) for index, _, i in self.items(root, 'audio') if i.tag == 'clipitem'}
        tone = next(i for _, i in audio_items.values() if i.findtext('name') == 'room_tone.wav' and i.findtext('start') == '0')
        level = tone.find("filter/effect[effectid='audiolevels']/parameter")
        self.assertEqual(level.findtext('value'), '0.7')
        self.assertEqual(level.findtext('keyframe/value'), '0', 'the fade-in starts silent')
        picture = clips[('A001_C001_tc1h.mov', '216')]
        links = [(l.findtext('linkclipref'), l.findtext('mediatype'), l.findtext('trackindex')) for l in picture.iterfind('link')]
        self.assertEqual(links[0], (picture.get('id'), 'video', '3'))
        sound_ids = [ref for ref, kind, _ in links if kind == 'audio']
        self.assertEqual(len(sound_ids), 2, 'the J-cut sound: one clipitem per stereo channel')
        for ref in sound_ids:
            index, sound = audio_items[ref]
            self.assertEqual(sound.findtext('start'), '204')
            self.assertEqual([l.findtext('linkclipref') for l in sound.iterfind('link')], [r for r, _, _ in links])
        self.assertEqual(sorted(audio_items[r][1].findtext('sourcetrack/trackindex') for r in sound_ids), ['1', '2'])
        title = next(i for _, _, i in self.items(root, 'video') if i.tag == 'generatoritem')
        self.assertEqual((title.findtext('name'), title.findtext('effect/effectid')), ('SYNTHETIC TURNOVER', 'Text'))
        self.assertEqual(title.find("effect/parameter[parameterid='str']").findtext('value'), 'SYNTHETIC TURNOVER')
        tracks = root.findall('sequence/media/audio/track')
        self.assertEqual([(t.get('MZ.TrackName'), t.get('currentExplodedTrackIndex')) for t in tracks],
                         [('A1', '0'), ('A1', '1'), ('A2', '0'), ('A2', '1'), ('V1 sound', '0'), ('V1 sound', '1')])

    def test_drop_frame_sequence(self):
        p = synthetic_project(self.paths)
        p['fps'] = float(DF)
        p['clips'] = [{'id': 'x', 'mediaId': 'drop', 'type': 'video', 'track': 1, 'start': float(Fraction(1798) / DF),
                       'duration': float(Fraction(4) / DF), 'in': 0, 'volume': 100}]
        sequence = self.root(p, dropFrame=True, startTimecode='00:00:00;00').find('sequence')
        self.assertEqual([sequence.findtext(f'timecode/{t}') for t in ('string', 'frame', 'displayformat')], ['00;00;00;00', '0', 'DF'])
        self.assertEqual((sequence.findtext('rate/timebase'), sequence.findtext('rate/ntsc')), ('30', 'TRUE'))


class RoundTrip(XmemlCase):
    def cut_list(self, project):
        rate = snap_rate(project['fps'])
        names = {m['id']: m['name'] for m in project['media']}
        rows, transitions = [], []
        for c in project['clips']:
            first = round(Fraction(repr(c['start'])) * rate)
            length = round(Fraction(repr(c['start'] + c['duration'])) * rate) - first
            if c['type'] == 'title':
                rows.append(('title', c['track'], first, length, c['title']))
                continue
            name = names[c['mediaId']]
            rows.append((c['type'], c['track'], first, length, round(Fraction(repr(c['in'])) * self.rates.get(name, rate)), name,
                         round(float(c.get('speed', 1)), 4), bool(c.get('reverse')), c.get('enabled') is not False,
                         c.get('linkGroup') is not None, c.get('embeddedAudio', None) if c['type'] == 'video' else None))
            if c.get('transition') in ('dissolve', 'wipe', 'fade'):
                transitions.append((c['type'], c['track'], first, round(Fraction(repr(c['transitionDuration'])) * rate),
                                    round(Fraction(repr(c.get('transitionBefore', 0))) * rate)))
        return sorted(rows, key=repr), sorted(transitions)

    def test_synthetic_round_trip_keeps_the_cut_list(self):
        original = synthetic_project(self.paths)
        for c in original['clips']:
            if c['type'] == 'video' and c.get('embeddedAudio') is None:
                # Splice's default: a picture clip plays its own sound, when its file has any
                c['embeddedAudio'] = c['mediaId'] in ('camera_a', 'camera_b')
        imported = xmeml.read(self.export(original).encode())
        self.assertEqual(self.cut_list(imported), self.cut_list(original))
        tone = next(c for c in imported['clips'] if c['type'] == 'audio' and c['start'] == 0)
        self.assertAlmostEqual(tone['volume'], 70, places=3)
        self.assertEqual(sorted(round(m['time'], 6) for m in imported['markers']),
                         sorted(round(m['time'], 6) for m in original['markers']))
        drop = next(c for c in imported['clips'] if c['track'] == 2 and c.get('opacity'))
        self.assertEqual(drop['opacity'], 50)
        self.assertAlmostEqual(drop['transform']['scale'], 80)
        self.assertAlmostEqual(drop['transform']['x'], 10)
        self.assertEqual([p['value'] for p in drop['keyframes']['opacity']], [20, 50])
        media_ = {m['name']: m for m in imported['media']}
        self.assertEqual((media_['B001_C001_df.mov']['timecode'], media_['B001_C001_df.mov']['dropFrame']), ('00:59:59;28', True))
        self.assertEqual(media_['A001_C001_tc1h.mov']['timecode'], '01:00:00:00')

    def test_alpine_round_trip(self):
        original = alpine_project(ROOT)
        imported = api.import_timeline('alpine.xml', api.export_timeline(original, {'formats': ['xmeml'], 'unknownTimecode': 'zero'})['files'][0]['content'])
        self.assertEqual(len([c for c in imported['clips'] if c.get('transition') == 'dissolve']), 3)
        self.assertEqual([c['title'] for c in imported['clips'] if c['type'] == 'title'], ['ALPINE PASSAGE'])
        self.assertTrue(all(m['src'].startswith('/assets/') for m in imported['media']), 'bundled media relinks by path')

    def test_hostile_and_wrong_documents(self):
        for bad, message in ((b'<fcpxml version="1.10"/>', 'FCPXML'), (b'<xmeml version="5"/>', 'no sequence'),
                             (b'<!DOCTYPE xmeml [<!ENTITY a "b">]><xmeml/>', 'entities')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                xmeml.read(bad)
        with self.assertRaisesRegex(ValueError, 'not a number'):
            xmeml.read(b'<xmeml version="5"><sequence><rate><timebase>25</timebase></rate><media><video><track>'
                       b'<clipitem><start>x</start></clipitem></track></video></media></sequence></xmeml>')


if __name__ == '__main__':
    unittest.main()
