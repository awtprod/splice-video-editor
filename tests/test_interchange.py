"""Timeline interchange: timecode arithmetic, FCPXML/EDL/OTIO export, round trips and hostile input."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange import edl, fcpxml, otio, xmlsafe  # noqa: E402
from interchange.api import import_timeline  # noqa: E402
from interchange.cut import InterchangeError, build_cut  # noqa: E402
from interchange.media import file_url, path_from_url  # noqa: E402
from interchange.timecode import (fcpxml_time, frames_timecode, parse_fcpxml_time, snap_rate,  # noqa: E402
                                  timecode_frames)
from interchange_fixtures import DF, RATE, alpine_project, make_media, synthetic_project  # noqa: E402
import fcpxml_dtd_rules  # noqa: E402

NTSC24, NTSC30, NTSC60 = Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)


class TimecodeArithmetic(unittest.TestCase):
    GOLDEN = [  # (label, rate, drop, frames)
        ('00:00:59:23', NTSC24, False, 1439), ('00:01:00:00', NTSC24, False, 1440),
        ('01:00:00:00', NTSC24, False, 86400), ('00:10:00:00', NTSC24, False, 14400),
        ('01:00:00:00', Fraction(24), False, 86400), ('23:59:59:23', Fraction(24), False, 2073599),
        ('01:00:00:00', Fraction(25), False, 90000), ('00:10:00:00', Fraction(25), False, 15000),
        ('00:00:59:29', NTSC30, False, 1799), ('00:01:00:00', NTSC30, False, 1800),
        ('01:00:00:00', NTSC30, False, 108000),
        ('00:00:59;29', NTSC30, True, 1799), ('00:01:00;02', NTSC30, True, 1800),
        ('00:01:59;29', NTSC30, True, 3597), ('00:02:00;02', NTSC30, True, 3598),
        ('00:09:59;29', NTSC30, True, 17981), ('00:10:00;00', NTSC30, True, 17982),
        ('00:10:00;01', NTSC30, True, 17983), ('00:11:00;02', NTSC30, True, 19782),
        ('01:00:00;00', NTSC30, True, 107892), ('23:59:59;29', NTSC30, True, 2589407),
        ('00:00:59;59', NTSC60, True, 3599), ('00:01:00;04', NTSC60, True, 3600),
        ('00:10:00;00', NTSC60, True, 35964), ('01:00:00;00', NTSC60, True, 215784),
        ('23:59:59;59', NTSC60, True, 5178815),
    ]

    def test_golden_labels_both_ways(self):
        for label, rate, drop, frames in self.GOLDEN:
            with self.subTest(label=label, rate=str(rate)):
                self.assertEqual(timecode_frames(label, rate), frames)
                self.assertEqual(frames_timecode(frames, rate, drop), label)

    def test_drop_frame_round_trips_every_frame_of_the_first_eleven_minutes(self):
        for rate in (NTSC30, NTSC60):
            labels = [frames_timecode(n, rate, True) for n in range(int(rate * 660))]
            self.assertEqual(len(set(labels)), len(labels))
            self.assertEqual([timecode_frames(label, rate) for label in labels], list(range(len(labels))))

    def test_drop_frame_refuses_skipped_labels_and_wrong_rates(self):
        for bad in ('00:01:00;00', '00:01:00;01', '00:02:00;01'):
            with self.assertRaises(ValueError):
                timecode_frames(bad, NTSC30)
        with self.assertRaises(ValueError):
            timecode_frames('00:01:00;03', NTSC60)
        self.assertEqual(timecode_frames('00:10:00;00', NTSC30), 17982)  # tenth minute keeps 00 and 01
        with self.assertRaises(ValueError):
            timecode_frames('01:00:00;00', Fraction(25))
        with self.assertRaises(ValueError):
            frames_timecode(10, NTSC24, True)

    def test_malformed_and_out_of_range_labels(self):
        for bad in ('1:00:00:00', '24:00:00:00', '00:60:00:00', '00:00:60:00', '00:00:00:24', 'aa:bb:cc:dd', '', '00:00:00'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                timecode_frames(bad, NTSC24)
        with self.assertRaises(ValueError):
            frames_timecode(24 * 86400, Fraction(24))
        with self.assertRaises(ValueError):
            frames_timecode(-1, Fraction(24))

    def test_rates_snap_to_standard_rationals(self):
        self.assertEqual(snap_rate('2953125/98536'), NTSC30)
        self.assertEqual(snap_rate(23.976), NTSC24)
        self.assertEqual(snap_rate('2997/100'), NTSC30)
        self.assertEqual(snap_rate(25.0), Fraction(25))
        self.assertIsNone(snap_rate(12.5))
        self.assertIsNone(snap_rate('120'))
        self.assertIsNone(snap_rate('x/0'))

    def test_fcpxml_rational_time_strings(self):
        self.assertEqual(fcpxml_time(Fraction(1001, 24000), NTSC24), '1001/24000s')
        self.assertEqual(fcpxml_time(Fraction(24) / NTSC24, NTSC24), '24024/24000s')
        self.assertEqual(fcpxml_time(Fraction(86400) / NTSC24, NTSC24), '86486400/24000s')
        self.assertEqual(fcpxml_time(Fraction(107892) / NTSC30, NTSC30), '107999892/30000s')
        self.assertEqual(fcpxml_time(Fraction(3600), Fraction(25)), '3600s')
        self.assertEqual(fcpxml_time(Fraction(12, 25), Fraction(25)), '12/25s')
        self.assertEqual(fcpxml_time(Fraction(1, 48000), NTSC24), '1/48000s')
        self.assertEqual(parse_fcpxml_time('1001/24000s'), Fraction(1001, 24000))
        self.assertEqual(parse_fcpxml_time('3600s'), Fraction(3600))
        for bad in ('0.0417s', '1/0s', '1001/24000', 's', '99999999999999999999999s'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_fcpxml_time(bad)
        with self.assertRaises(ValueError):
            fcpxml_time(Fraction(-1))


class MediaReferences(unittest.TestCase):
    def test_file_urls_for_windows_unc_and_mac_paths_round_trip(self):
        cases = {
            r'C:\Media\Day 1\A001 #1%.mov': 'file:///C:/Media/Day%201/A001%20%231%25.mov',
            r'\\nas\share\Reel 2\b.mxf': 'file://nas/share/Reel%202/b.mxf',
            '/Volumes/RAID/Proj ect/c.mov': 'file:///Volumes/RAID/Proj%20ect/c.mov',
            '/home/editor/clips/é.mov': 'file:///home/editor/clips/%C3%A9.mov',
        }
        for path, url in cases.items():
            with self.subTest(path=path):
                self.assertEqual(file_url(path), url)
                self.assertEqual(path_from_url(url), path)
        self.assertEqual(path_from_url('file://localhost/Users/x/a%20b.mov'), '/Users/x/a b.mov')
        self.assertEqual(path_from_url('file:/Users/x/a.mov'), '/Users/x/a.mov')
        for bad in ('http://example.invalid/a.mov', 'file:///a%00b.mov', ''):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                path_from_url(bad)


class InterchangeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-interchange-')
        cls.paths = make_media(Path(cls.temp.name))
        cls.rates = {cls.paths['camera_a'].name: RATE, cls.paths['camera_b'].name: RATE, cls.paths['drop'].name: DF,
                     cls.paths['plain'].name: Fraction(25), cls.paths['tone'].name: RATE}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def project(self):
        return synthetic_project(self.paths)

    def cut_list(self, project, include_disabled=True):
        rate = snap_rate(project['fps'])
        names = {m['id']: m['name'] for m in project['media']}
        rows, transitions = [], []
        for c in project['clips']:
            first = round(Fraction(repr(c['start'])) * rate)
            length = round(Fraction(repr(c['start'] + c['duration'])) * rate) - first
            if c['type'] == 'title':
                rows.append(('title', c['track'], first, length, c['title']))
                continue
            if c.get('enabled') is False and not include_disabled:
                continue
            name = names[c['mediaId']]
            rows.append((c['type'], c['track'], first, length, round(Fraction(repr(c['in'])) * self.rates[name]), name,
                         round(float(c.get('speed', 1)), 4), bool(c.get('reverse')), c.get('enabled') is not False))
            if c.get('transition') in ('dissolve', 'wipe', 'fade'):
                transitions.append((c['type'], c['track'], first, c['transition'], round(Fraction(repr(c['transitionDuration'])) * rate),
                                    round(Fraction(repr(c.get('transitionBefore', 0))) * rate)))
        return sorted(rows, key=repr), sorted(transitions)


class FcpxmlExport(InterchangeCase):
    def export(self, project=None, **options):
        return fcpxml.write(build_cut(project or self.project(), options))

    def test_color_finishing_loss_report(self):
        project = self.project()
        clip = next(c for c in project['clips'] if c['type'] == 'video')
        clip['color'] = {'grade': {'gain': [1.2, 1, 1, 1], 'pivot': .4, 'bypass': True}}
        clip['keyframes'] = {'exposure': [{'time': 0, 'value': 1}]}
        notes = ' '.join(build_cut(project, {}).report.general)
        for phrase in ('colour wheels', 'contrast pivot', 'Export .cube', 'grade keyframes', 'bypassed'):
            self.assertIn(phrase, notes)

    def test_structure_follows_the_dtd_rules_it_relies_on(self):
        text = self.export()
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'))
        root = xmlsafe.parse(text.encode())
        self.assertEqual(root.get('version'), '1.10')
        resources = {e.get('id'): e for e in root.find('resources')}
        self.assertEqual(len(resources), len(root.find('resources')), 'resource ids are unique')
        expected_ref = {'asset-clip': 'asset', 'title': 'effect', 'filter-video': 'effect', 'filter-audio': 'effect'}
        required = {'format': ('id',), 'asset': ('id', 'name', 'start', 'duration'), 'effect': ('id', 'name', 'uid'),
                    'asset-clip': ('ref', 'offset', 'name', 'start', 'duration'), 'gap': ('offset', 'duration'),
                    'transition': ('offset', 'duration'), 'title': ('ref', 'offset', 'duration'), 'marker': ('start', 'value'),
                    'sequence': ('format', 'duration', 'tcStart'), 'keyframe': ('time', 'value'), 'timept': ('time', 'value'),
                    'media-rep': ('kind', 'src'), 'md': ('key', 'value')}
        for element in root.iter():
            for attribute in required.get(element.tag, ()):
                self.assertIsNotNone(element.get(attribute), f'<{element.tag}> lacks {attribute}')
            if element.tag in expected_ref:
                self.assertEqual(resources[element.get('ref')].tag, expected_ref[element.tag])
            times = ('offset', 'duration', 'start', 'tcStart', 'time', 'audioStart', 'audioDuration', 'frameDuration')
            for attribute in times + (('value',) if element.tag == 'timept' else ()):
                if element.get(attribute) is not None:
                    parse_fcpxml_time(element.get(attribute))  # raises unless an exact rational time
        for asset in root.iterfind('resources/asset'):
            if asset.get('format'):
                self.assertEqual(resources[asset.get('format')].tag, 'format')
            self.assertTrue(asset.find('media-rep').get('src').startswith('file:///'))
        sequence = root.find('library/event/project/sequence')
        self.assertEqual(resources[sequence.get('format')].get('frameDuration'), '1001/24000s')
        step = Fraction(1001, 24000)
        for element in root.iter():
            if element.tag in ('asset-clip', 'gap', 'transition', 'title', 'spine') and element.get('offset'):
                for attribute in ('offset', 'duration'):
                    if element.get(attribute):
                        self.assertEqual((parse_fcpxml_time(element.get(attribute)) / step).denominator, 1,
                                         f'{element.tag} {attribute} {element.get(attribute)} is off the frame grid')
        self.assertEqual(fcpxml_dtd_rules.problems(root), [], 'element order and attributes follow the FCPXML 1.10 DTD')
        spine = sequence.find('spine')
        cursor = parse_fcpxml_time(sequence.get('tcStart'))
        for element in spine:
            if element.tag == 'transition':
                continue
            self.assertEqual(parse_fcpxml_time(element.get('offset')), cursor, 'primary storyline is contiguous')
            cursor += parse_fcpxml_time(element.get('duration'))
        self.assertEqual(cursor - parse_fcpxml_time(sequence.get('tcStart')), parse_fcpxml_time(sequence.get('duration')))

    def probe_timecode(self, path):
        data = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                                         capture_output=True, check=True, timeout=20).stdout)
        tags = [s.get('tags', {}) for s in data['streams']] + [data['format'].get('tags', {})]
        return next((t['timecode'] for t in tags if 'timecode' in t), None), data['format'].get('tags', {}).get('time_reference')

    def test_asset_start_equals_the_probed_timecode(self):
        root = xmlsafe.parse(self.export().encode())
        starts = {a.get('name'): a.get('start') for a in root.iterfind('resources/asset')}
        expected = {'camera_a': ('01:00:00:00', '86486400/24000s'), 'camera_b': ('02:00:00:00', '172972800/24000s'),
                    'drop': ('00:59:59;28', '107997890/30000s'), 'tone': (None, '86486400/24000s')}
        for key, (label, start) in expected.items():
            with self.subTest(source=key):
                timecode, reference = self.probe_timecode(self.paths[key])
                self.assertEqual(timecode, label)
                self.assertEqual(starts[self.paths[key].name], start)
                if label:
                    rate = DF if ';' in label else RATE
                    self.assertEqual(parse_fcpxml_time(start), Fraction(timecode_frames(label, rate)) / rate)
                else:
                    self.assertEqual(parse_fcpxml_time(start), Fraction(int(reference), 48000))
        # the DTD has tcFormat on the clip, not the asset
        self.assertIsNone(root.find("resources/asset[@name='B001_C001_df.mov']").get('tcFormat'))
        self.assertEqual({c.get('tcFormat') for c in root.iter('asset-clip') if c.get('name') == 'B001_C001_df.mov'}, {'DF'})
        self.assertEqual({c.get('tcFormat') for c in root.iter('asset-clip') if c.get('name') == 'A001_C001_tc1h.mov'}, {'NDF'})

    def test_unknown_timecode_is_a_visible_decision(self):
        project = self.project()
        project['media'][0]['sourcePath'] = '/nonexistent/A001_C001_tc1h.mov'
        with self.assertRaisesRegex(InterchangeError, 'Start timecode could not be read for A001_C001_tc1h.mov'):
            build_cut(project, {})
        cut = build_cut(project, {'unknownTimecode': 'zero'})
        self.assertTrue(any('written as 00:00:00:00 by choice' in line for line in cut.report.general))
        self.assertIn('start="0s"', fcpxml.write(cut))

    def test_clip_level_features_in_the_document(self):
        root = xmlsafe.parse(self.export().encode())
        clips = {c.get('name') + '@' + c.get('offset'): c for c in root.iter('asset-clip')}
        by_name = lambda name: [c for k, c in clips.items() if k.startswith(name)]
        slow = next(c for c in by_name('A001_C001_tc1h.mov') if c.find('timeMap') is not None and
                    parse_fcpxml_time(c.find('timeMap')[1].get('value')) > parse_fcpxml_time(c.find('timeMap')[0].get('value')))
        points = [(parse_fcpxml_time(p.get('time')), parse_fcpxml_time(p.get('value'))) for p in slow.find('timeMap')]
        self.assertEqual((points[1][1] - points[0][1]) / (points[1][0] - points[0][0]), Fraction(1, 2))
        reverse = [c for c in root.iter('asset-clip') if c.find('timeMap') is not None and
                   parse_fcpxml_time(c.find('timeMap')[1].get('value')) < parse_fcpxml_time(c.find('timeMap')[0].get('value'))]
        self.assertEqual(len(reverse), 1)
        disabled = [c for c in root.iter('asset-clip') if c.get('enabled') == '0']
        self.assertEqual([c.get('name') for c in disabled], ['plain_25.mp4'])
        jcut = next(c for c in root.iter('asset-clip') if c.get('audioStart'))
        self.assertEqual(parse_fcpxml_time(jcut.get('start')) - parse_fcpxml_time(jcut.get('audioStart')), Fraction(12) / RATE)
        self.assertEqual(jcut.get('lane'), '2')
        transitions = list(root.iter('transition'))
        self.assertEqual(len(transitions), 2)
        dissolve = next(t for t in transitions if t.find('filter-video') is not None)
        self.assertEqual(dissolve.get('offset'), fcpxml_time(Fraction(86400 + 48 - 6) / RATE, RATE))
        self.assertEqual(dissolve.get('duration'), '12012/24000s')
        storyline = [s for s in root.iter('spine') if s.get('lane') == '-2']
        self.assertEqual(len(storyline), 1, 'a track with a crossfade becomes a secondary storyline')
        tone = storyline[0].find('asset-clip')
        volume = tone.find('adjust-volume')
        self.assertEqual(volume.get('amount'), '-3.098039dB')
        self.assertIsNotNone(volume.find('param/fadeIn'))
        self.assertEqual(len(volume.findall('param/keyframeAnimation/keyframe')), 2)
        drop = next(c for c in root.iter('asset-clip') if c.get('name') == 'B001_C001_df.mov')
        self.assertEqual(drop.find('adjust-blend').get('amount'), '0.5')
        self.assertEqual(drop.find('adjust-transform').get('position'), f'{10 * 1920 / 1080:.6f}'.rstrip('0') + ' 5')
        self.assertEqual(len(list(root.iter('marker'))), 2)
        self.assertEqual(root.find('.//title/text/text-style').text, 'SYNTHETIC TURNOVER')

    def test_round_trip_keeps_the_cut_list(self):
        original = self.project()
        imported = fcpxml.read(self.export(original).encode())
        self.assertEqual(self.cut_list(imported), self.cut_list(original))
        links = {c['linkGroup'] for c in imported['clips'] if c.get('linkGroup')}
        self.assertEqual(links, {'jcut'})
        tone = next(c for c in imported['clips'] if c['start'] == 0 and c['type'] == 'audio')
        self.assertAlmostEqual(tone['volume'], 70, places=3)
        self.assertAlmostEqual(tone['fadeOut'], float(Fraction(24) / RATE), places=9)
        self.assertEqual([round(p['value'], 3) for p in tone['keyframes']['volume']], [70, 35])
        drop = next(c for c in imported['clips'] if c['track'] == 2 and c.get('opacity'))
        self.assertAlmostEqual(drop['transform']['x'], 10, places=5)
        self.assertEqual([p['value'] for p in drop['keyframes']['opacity']], [20, 50])
        self.assertEqual(sorted(round(m['time'], 6) for m in imported['markers']),
                         sorted(round(m['time'], 6) for m in original['markers']))
        self.assertEqual({m['path'] for m in imported['media']}, {str(p) for k, p in self.paths.items()})

    def test_range_export_trims_to_in_and_out(self):
        project = self.project()
        cut = build_cut(project, {'range': {'start': float(Fraction(60) / RATE), 'end': float(Fraction(180) / RATE)}})
        self.assertEqual(cut.frames, 120)
        first = min((e for e in cut.events if e.kind == 'video' and e.track == 1), key=lambda e: e.record_in)
        self.assertEqual((first.record_in, first.name), (0, 'A001_C002_tc2h.mov'))
        self.assertEqual(first.source_in, Fraction(24 + 12) / RATE)
        imported = fcpxml.read(fcpxml.write(cut).encode())
        self.assertEqual(max(c['start'] + c['duration'] for c in imported['clips']), float(Fraction(120) / RATE))
        self.assertTrue(all(c['start'] >= 0 for c in imported['clips']))
        with self.assertRaises(InterchangeError):
            build_cut(project, {'range': {'start': 5, 'end': 5}})

    def test_alpine_passage(self):
        project = alpine_project(ROOT)
        text = self.export(project)
        self.assertIn('FFVideoFormat1080p25', text)
        imported = fcpxml.read(text.encode())
        self.assertEqual(len([c for c in imported['clips'] if c.get('transition') == 'dissolve']), 3)
        self.assertTrue(all(m['src'].startswith('/assets/') for m in imported['media']), 'bundled media relinks by path')


class EdlExport(InterchangeCase):
    def files(self, project=None, **options):
        cut = build_cut(project or self.project(), options)
        return dict(edl.write(cut, audio=True)), cut

    def test_layout_reels_dissolves_speed_and_comments(self):
        files, cut = self.files()
        self.assertEqual(sorted(files), ['A1', 'A2', 'V1', 'V1-sound', 'V2', 'V3'])
        v1 = files['V1']
        self.assertIn('FCM: NON-DROP FRAME', v1)
        self.assertIn('* REEL A001_C00 = A001_C001_tc1h.mov', v1)
        self.assertIn('* REEL A001_001 = A001_C002_tc2h.mov', v1)
        lines = v1.splitlines()
        self.assertIn('001  A001_C00 V     C        01:00:00:00 01:00:01:18 01:00:00:00 01:00:01:18', lines)
        self.assertIn('002  A001_C00 V     C        01:00:01:18 01:00:01:18 01:00:01:18 01:00:01:18', lines)
        self.assertIn('002  A001_001 V     D    012 02:00:00:18 02:00:03:00 01:00:01:18 01:00:04:00', lines)
        self.assertIn('M2   A001_C00       11.988                01:00:01:00', lines)  # 50% of 23.976
        self.assertIn('M2   A001_001       47.952                02:00:00:00', lines)  # 200%
        self.assertIn('M2   A001_C00       -23.976                01:00:00:12', lines)  # reverse
        self.assertIn('* SOURCE FILE: ' + str(self.paths['camera_a']), lines)
        self.assertIn('* LOC: 01:00:01:06 RED     Picture lock note', lines)
        # the 29.97 DF source counts at the list's 23.976: start label 00:59:59;28 -> 00:59:59:22, in 6 frames -> 5
        self.assertTrue(any(line.startswith('001  B001_C00 V     C        01:00:00:03 01:00:02:03 01:00:01:00 01:00:03:00') for line in files['V2'].splitlines()), files['V2'])
        self.assertNotIn('plain_25', files['V2'])
        self.assertTrue(any('disabled' in line for line in cut.report.by_format['EDL']))
        for name, text in files.items():
            for line in text.splitlines():
                if line[:3].isdigit():
                    reel = line.split()[1]
                    self.assertLessEqual(len(reel), 8)
                    self.assertRegex(reel, r'^[A-Z0-9_]+$')

    def normalized(self, project, track, kind='video', as_kind=None, as_track=None):
        rate = RATE
        clips = [dict(c) for c in project['clips'] if c['type'] == kind and c['track'] == track and c.get('enabled') is not False]
        by_end = {round(Fraction(repr(c['start'] + c['duration'])) * rate): c for c in clips}
        for c in clips:
            before = c.get('transitionBefore', 0)
            if c.get('transition') == 'dissolve' and before:
                out = by_end[round(Fraction(repr(c['start'])) * rate)]
                out['duration'] -= before
                c['start'] -= before
                c['duration'] += before
                c['in'] -= before * c.get('speed', 1)
                c['transitionBefore'] = 0
            c['type'], c['track'] = as_kind or c['type'], as_track or 1 if (as_kind or kind) == 'video' else (as_track or -1)
            c.pop('linkGroup', None)
        return dict(project, clips=clips)

    def test_round_trip_per_track(self):
        original = self.project()
        files, _cut = self.files(original)
        for name, track, kind, as_kind, as_track in (('V1', 1, 'video', None, 1), ('V2', 2, 'video', None, 1),
                                                     ('V3', 3, 'video', None, 1), ('A1', -1, 'audio', None, -1),
                                                     ('A2', -2, 'audio', None, -1), ('V1-sound', 1, 'video', 'audio', -1)):
            with self.subTest(edl=name):
                imported = edl.read(files[name].encode(), {'fps': '24000/1001'})
                expected = self.normalized(original, track, kind, as_kind, as_track)
                got = self.cut_list(imported)[0]
                want = [tuple(row[:6]) + (row[6], row[7], True) for row in self.cut_list(expected)[0]]
                self.assertEqual([tuple(r) for r in got], sorted(want, key=repr))
                self.assertEqual([t[3:5] for t in self.cut_list(imported)[1]], [t[3:5] for t in self.cut_list(expected)[1]])
                self.assertFalse([w for w in imported['compatibilityWarnings'] if 'unknown' in w])

    def test_drop_frame_sequence_record_timecode(self):
        project = self.project()
        project['fps'] = float(DF)
        project['clips'] = [{'id': 'x', 'mediaId': 'drop', 'type': 'video', 'track': 1, 'start': float(Fraction(1798) / DF),
                             'duration': float(Fraction(4) / DF), 'in': 0, 'volume': 100}]
        files, cut = self.files(project, dropFrame=True, startTimecode='00:00:00;00')
        lines = files['V1'].splitlines()
        self.assertIn('FCM: DROP FRAME', lines)
        self.assertTrue(any(line.endswith('00:00:59;28 00:01:00;04') for line in lines), files['V1'])
        imported = edl.read(files['V1'].encode(), {'fps': '30000/1001'})
        self.assertEqual(round(Fraction(repr(imported['clips'][0]['start'])) * DF), 1798)

    def test_reel_names_shorten_uniquely(self):
        project = self.project()
        files, _ = self.files(project)
        reels = {line.split()[2]: line.split('= ')[1] for text in files.values() for line in text.splitlines() if line.startswith('* REEL')}
        self.assertEqual(len(set(reels.values())), len(reels))


class OtioExport(InterchangeCase):
    def document(self, project=None):
        return json.loads(otio.write(build_cut(project or self.project(), {})))

    def test_schema_names_and_fields(self):
        doc = self.document()
        self.assertEqual(doc['OTIO_SCHEMA'], 'Timeline.1')
        self.assertEqual(doc['global_start_time'], {'OTIO_SCHEMA': 'RationalTime.1', 'rate': 24000 / 1001, 'value': 86400.0})
        stack = doc['tracks']
        self.assertEqual(stack['OTIO_SCHEMA'], 'Stack.1')
        kinds = [(t['name'], t['kind']) for t in stack['children']]
        self.assertEqual(kinds, [('V1', 'Video'), ('V2', 'Video'), ('V3', 'Video'), ('V4', 'Video'),
                                 ('A1', 'Audio'), ('A2', 'Audio'), ('V1 sound', 'Audio')])
        fields = {'Clip.2': {'name', 'source_range', 'media_references', 'active_media_reference_key', 'effects', 'markers', 'metadata', 'enabled'},
                  'Gap.1': {'name', 'source_range', 'effects', 'markers', 'metadata'},
                  'Transition.1': {'name', 'transition_type', 'in_offset', 'out_offset', 'metadata'}}
        for track in stack['children']:
            self.assertEqual(track['OTIO_SCHEMA'], 'Track.1')
            items = track['children']
            for index, item in enumerate(items):
                self.assertLessEqual(fields[item['OTIO_SCHEMA']], set(item))
                if item['OTIO_SCHEMA'] == 'Transition.1':
                    self.assertNotEqual(items[index + 1]['OTIO_SCHEMA'], 'Transition.1')
                if item['OTIO_SCHEMA'] == 'Clip.2' and item['media_references']['DEFAULT_MEDIA']['OTIO_SCHEMA'] == 'ExternalReference.1':
                    ref = item['media_references']['DEFAULT_MEDIA']
                    self.assertTrue(ref['target_url'].startswith('file:///'))
                    self.assertEqual(ref['available_range']['OTIO_SCHEMA'], 'TimeRange.1')
                    start = ref['available_range']['start_time']
                    self.assertLessEqual(start['value'], item['source_range']['start_time']['value'])
                    self.assertEqual(start['rate'], item['source_range']['start_time']['rate'])
        camera = next(i for t in stack['children'] for i in t['children'] if i.get('name') == 'A001_C001_tc1h.mov')
        self.assertEqual(camera['media_references']['DEFAULT_MEDIA']['available_range']['start_time']['value'], 86400.0)
        speeds = sorted(e['time_scalar'] for t in stack['children'] if t['kind'] == 'Video'
                        for i in t['children'] for e in i.get('effects', []))
        self.assertEqual(speeds, [-1.0, 0.5, 2.0])
        self.assertEqual([m['OTIO_SCHEMA'] for m in stack['markers']], ['Marker.2', 'Marker.2'])

    def test_round_trip(self):
        original = self.project()
        imported = otio.read(otio.write(build_cut(original, {})).encode())
        self.assertEqual(self.cut_list(imported), self.cut_list(original))

    @unittest.skipUnless(__import__('importlib').util.find_spec('opentimelineio'), 'opentimelineio is not installed')
    def test_opentimelineio_reads_it(self):
        import opentimelineio
        timeline = opentimelineio.adapters.read_from_string(otio.write(build_cut(self.project(), {})), 'otio_json')
        self.assertEqual(len(timeline.tracks), 7)


class HostileInput(unittest.TestCase):
    LAUGHS = b'<?xml version="1.0"?><!DOCTYPE fcpxml [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><fcpxml version="1.10">&b;</fcpxml>'
    EXTERNAL = b'<?xml version="1.0"?><!DOCTYPE fcpxml [<!ENTITY x SYSTEM "file:///etc/hostname">]><fcpxml version="1.10">&x;</fcpxml>'
    DTD = b'<?xml version="1.0"?><!DOCTYPE fcpxml SYSTEM "http://example.invalid/fcpxml.dtd"><fcpxml version="1.10"/>'
    PARAMETER = b'<?xml version="1.0"?><!DOCTYPE fcpxml [<!ENTITY % p SYSTEM "file:///etc/hostname"> %p;]><fcpxml version="1.10"/>'

    def test_entities_and_external_dtds_are_refused(self):
        for name in ('LAUGHS', 'EXTERNAL', 'DTD', 'PARAMETER'):
            with self.subTest(name), self.assertRaises(xmlsafe.UnsafeXML):
                xmlsafe.parse(getattr(self, name))
        self.assertEqual(xmlsafe.parse(b'<!DOCTYPE fcpxml><fcpxml version="1.10"/>').tag, 'fcpxml')

    def test_limits(self):
        many = b'<fcpxml>' + b'<a/>' * 1001 + b'</fcpxml>'
        with self.assertRaises(xmlsafe.UnsafeXML):
            xmlsafe.parse(many, max_elements=1000)
        deep = b'<a>' * 100 + b'</a>' * 100
        with self.assertRaises(xmlsafe.UnsafeXML):
            xmlsafe.parse(deep)
        with self.assertRaises(ValueError):
            xmlsafe.parse(b'<fcpxml><unclosed></fcpxml>')
        with self.assertRaises(xmlsafe.UnsafeXML):
            xmlsafe.parse(b' ' * (xmlsafe.MAX_XML_BYTES + 1))

    def wrap(self, spine, resources=''):
        return (f'<!DOCTYPE fcpxml><fcpxml version="1.10"><resources><format id="r1" frameDuration="1/25s" width="1920" height="1080"/>'
                f'{resources}</resources><library><event name="e"><project name="p"><sequence format="r1" tcStart="0s" duration="10s">'
                f'<spine>{spine}</spine></sequence></project></event></library></fcpxml>').encode()

    def test_cyclic_and_dangling_references(self):
        media = ('<media id="m1" name="loop"><sequence format="r1" tcStart="0s" duration="5s"><spine>'
                 '<ref-clip ref="m1" offset="0s" duration="5s" name="inner"/></spine></sequence></media>'
                 '<asset id="a1" name="x" start="0s" duration="5s" hasVideo="1" format="r1"><media-rep kind="original-media" src="file:///x.mov"/></asset>')
        data = self.wrap('<ref-clip ref="m1" offset="0s" duration="5s" name="outer"/><asset-clip ref="nope" offset="5s" duration="1s" name="ghost"/>'
                         '<asset-clip ref="a1" offset="6s" duration="1s" start="0s" name="real"/>', media)
        project = fcpxml.read(data)
        self.assertEqual([c['start'] for c in project['clips']], [6.0])
        warnings = ' '.join(project['compatibilityWarnings'])
        self.assertIn('refers to itself', warnings)
        self.assertIn('ghost refers to a missing asset', warnings)
        with self.assertRaisesRegex(ValueError, 'defined twice'):
            fcpxml.read(self.wrap('', '<format id="r1"/>'))

    def test_malformed_times_and_counts(self):
        asset = '<asset id="a1" name="x" start="0s" duration="5s" hasVideo="1" format="r1"><media-rep kind="original-media" src="file:///x.mov"/></asset>'
        with self.assertRaises(ValueError):
            fcpxml.read(self.wrap('<asset-clip ref="a1" offset="1.5s" duration="1s" name="x"/>', asset))
        with self.assertRaises(ValueError):
            fcpxml.read(self.wrap('<asset-clip ref="a1" offset="0s" duration="1s" name="x" lane="x"/>', asset))
        crowd = ''.join(f'<asset-clip ref="a1" offset="{i}s" duration="1s" start="0s" name="c" lane="1"/>' for i in range(20002))
        with self.assertRaises(ValueError):
            fcpxml.read(self.wrap(f'<gap offset="0s" duration="30000s">{crowd}</gap>', asset))
        with self.assertRaises(ValueError):
            fcpxml.read(b'<?xml version="1.0"?><notfcpxml/>')

    def test_edl_rejects_malformed_timecode_and_binary(self):
        good = '001  AX       V     C        00:00:00:00 00:00:01:00 01:00:00:00 01:00:01:00\n'
        self.assertEqual(len(edl.read(('TITLE: t\nFCM: NON-DROP FRAME\n' + good).encode())['clips']), 1)
        for bad in ('001  AX  V  C  00:00:00:00 00:00:01:00 25:00:00:00 25:00:01:00',
                    '001  AX  V  C  00:00:00:00 00:00:99:00 01:00:00:00 01:00:01:00',
                    '001  AX  V  C  00:00:00:00 00:00:01:00 01:00:00:30 01:00:01:00'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                edl.read(bad.encode(), {'fps': '25'})
        with self.assertRaises(ValueError):
            edl.read(b'\x00\x01binary')
        with self.assertRaises(ValueError):
            edl.read(b'TITLE: nothing\n')
        garbage = edl.read(('TITLE: t\n' + good + 'this is not an event\n').encode())
        self.assertTrue(any('could not be read' in w for w in garbage['compatibilityWarnings']))
        with self.assertRaises(ValueError):
            edl.read(('TITLE: t\n' + good * 20002).encode())

    def test_otio_hostile_json(self):
        for bad in (b'[' * 100000, b'{"OTIO_SCHEMA": "Timeline.1"}', b'not json',
                    b'{"OTIO_SCHEMA":"Timeline.1","tracks":{"children":[{"OTIO_SCHEMA":"Track.1","children":[{"OTIO_SCHEMA":"Clip.1","source_range":{"start_time":{"OTIO_SCHEMA":"RationalTime.1","rate":0,"value":1}}}]}]}}'):
            with self.subTest(bad=bad[:30]), self.assertRaises(ValueError):
                import_timeline('hostile.otio', bad.decode())


class ImportBoundary(InterchangeCase):
    def test_injected_values_are_typed_and_bounded(self):
        clip = {'OTIO_SCHEMA': 'Clip.1', 'name': 'bad\x07name', 'enabled': True,
                'source_range': {'OTIO_SCHEMA': 'TimeRange.1', 'start_time': {'OTIO_SCHEMA': 'RationalTime.1', 'rate': 25, 'value': 0},
                                 'duration': {'OTIO_SCHEMA': 'RationalTime.1', 'rate': 25, 'value': 25}},
                'media_reference': {'OTIO_SCHEMA': 'ExternalReference.1', 'target_url': 'file:///m/x.mov', 'name': 'x.mov'},
                'metadata': {'splice': {'volume': 'loud', 'opacity': 900, 'transform': 'left', 'linkGroup': 'g' * 500,
                                        'keyframes': {'x': [{'time': 'soon', 'value': 1}, {'time': 0, 'value': 5}], 'evil': [1]}}}}
        doc = {'OTIO_SCHEMA': 'Timeline.1', 'name': 'n', 'tracks': {'OTIO_SCHEMA': 'Stack.1', 'children': [
            {'OTIO_SCHEMA': 'Track.1', 'name': 'V1', 'kind': 'Video', 'children': [clip]}]}}
        project = otio.read(json.dumps(doc).encode())
        imported = project['clips'][0]
        self.assertEqual(imported['volume'], 100)
        self.assertEqual(imported['opacity'], 100)
        self.assertNotIn('transform', imported)
        self.assertEqual(len(imported['linkGroup']), 128)
        self.assertEqual(imported['keyframes'], {'x': [{'time': 0.0, 'value': 5.0}]})
        warnings = ' '.join(project['compatibilityWarnings'])
        for expected in ('volume is not a number', 'opacity 900 is outside', 'transform is incomplete', '"evil"'):
            self.assertIn(expected, warnings)

    def test_control_characters_never_reach_xml(self):
        project = self.project()
        project['clips'][-1]['title'] = 'TITLE\x00\x1bWITH\x07CONTROLS'
        project['markers'][0]['name'] = 'mark\x0b'
        text = fcpxml.write(build_cut(project, {}))
        root = xmlsafe.parse(text.encode())
        self.assertEqual(root.find('.//title/text/text-style').text, 'TITLEWITHCONTROLS')


class DesktopPackage(unittest.TestCase):
    def test_desktop_archives_ship_the_interchange_package(self):
        import package_desktop
        tracked = subprocess.run(['git', 'ls-files', 'interchange'], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.split()
        if not tracked:
            self.skipTest('interchange/ is not committed in this checkout yet')
        shipped = {path.as_posix() for path in package_desktop.source_files()}
        self.assertLessEqual({'interchange/api.py', 'interchange/fcpxml.py', 'interchange-dialogs.js', 'interchange-dialogs.css'}, shipped)


class ServerRoutes(InterchangeCase):
    """The interchange routes run behind the same request checks as every other /api route."""

    def test_export_and_import_over_http(self):
        from test_media_server import ServerProcess
        data = Path(self.temp.name) / 'server-data'
        server = ServerProcess(data)
        self.addCleanup(server.close)

        def post(route, payload, expect=200):
            status, body = server.json('POST', '/api/interchange/' + route, payload)
            self.assertEqual(status, expect, body)
            return body
        result = post('export', {'project': self.project(), 'options': {'formats': ['fcpxml', 'edl', 'otio', 'xmeml'], 'package': True}})
        self.assertEqual([f['name'].rsplit('.', 1)[1] for f in result['files']], ['fcpxml', 'edl', 'edl', 'edl', 'otio', 'xml'])
        self.assertIn('FCPXML 1.10 (DaVinci Resolve, Final Cut Pro):', result['report'])
        self.assertIn('Final Cut Pro 7 XML (Premiere Pro, Resolve, Avid):', result['report'])
        import base64
        import io
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(base64.b64decode(result['package']['base64']))).namelist()
        self.assertIn('Synthetic-23976-turnover/Synthetic-23976-turnover-REPORT.txt', names)
        self.assertIn('Synthetic-23976-turnover/Synthetic-23976-turnover.xml', names)
        self.assertEqual(post('cancel', {'jobId': 'no-such-import-1'}), {'cancelled': False})
        from_xml = post('import', {'filename': 'x.xml', 'text': result['files'][-1]['content']})['project']
        self.assertIn('(FCP 7 XML)', from_xml['compatibilityWarnings'][0])
        imported = post('import', {'filename': 'x.fcpxml', 'text': result['files'][0]['content']})['project']
        self.assertEqual(len(imported['clips']), len(self.project()['clips']))
        post('import', {'filename': 'x.edl', 'text': 'garbage'}, expect=400)

    def test_every_interchange_route_refuses_requests_without_the_session(self):
        from test_media_server import ServerProcess
        server = ServerProcess(Path(self.temp.name) / 'refuse-data')
        self.addCleanup(server.close)
        payloads = {'export': {'project': self.project(), 'options': {'formats': ['edl']}},
                    'import': {'filename': 'x.edl', 'text': 'TITLE: x\nFCM: NON-DROP FRAME\n', 'options': {'fps': '25'}},
                    'cancel': {'jobId': 'no-such-import-2'}}
        import server as server_module
        self.assertLessEqual({'/api/interchange/export', '/api/interchange/import', '/api/interchange/cancel'},
                             set(server_module.POST_ROUTES))
        for route, payload in payloads.items():
            path = '/api/interchange/' + route
            with self.subTest(route=route):
                for label, options in (('no cookie or token', dict(auth=False)),
                                       ('cookie without the page token', dict(token=False)),
                                       ('a cross-site request', dict(headers={'Sec-Fetch-Site': 'cross-site'})),
                                       ('another origin', dict(headers={'Origin': 'http://127.0.0.1:1'}))):
                    status, _, body = server.request('POST', path, payload, **options)
                    self.assertEqual(status, 403, f'{route} with {label}: {body[:200]}')
                status, body = server.json('POST', path, payload)
                self.assertIn(status, (200, 400), body)    # the app's own call reaches the handler
                if route != 'import':
                    self.assertEqual(status, 200, body)
                else:
                    self.assertNotIn('refused', body.get('error', ''))


if __name__ == '__main__':
    unittest.main()
