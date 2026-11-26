"""Timeline interchange, fix round 1: one test (or more) per validator finding and per result from
real DaVinci Resolve and Premiere Pro imports, plus tests that pin behaviour mutation testing showed
was unguarded."""
import io
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange import api, budget, cut as cut_module, edl, fcpxml, media, otio, xmlsafe  # noqa: E402
from interchange.cut import InterchangeError, build_cut  # noqa: E402
from interchange.timecode import frames_timecode, snap_rate, timecode_frames  # noqa: E402
from interchange_fixtures import DF, RATE, alpine_project, at, make_media, synthetic_project  # noqa: E402
import fcpxml_dtd_rules  # noqa: E402

HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'


def item(identity='m1', name='x.mov', **extra):
    return {'id': identity, 'name': name, 'src': '', 'type': 'video', 'duration': 30, 'path': name,
            'sourcePath': f'/offline/{name}', 'rate': '25', 'timecode': '01:00:00:00', 'dropFrame': False, **extra}


def clip(identity, media_id, start, duration, source_in=0.0, track=1, kind='video', **extra):
    return {'id': identity, 'mediaId': media_id, 'type': kind, 'track': track, 'start': start, 'duration': duration,
            'in': source_in, 'title': '', 'color': {}, 'volume': 100, 'transition': 'none', **extra}


def project(media_items, clips, fps=25, **extra):
    return {'name': 'Regression', 'width': 1920, 'height': 1080, 'fps': fps, 'media': media_items, 'clips': clips,
            'markers': [], **extra}


def fcpxml_doc(spine, resources='', rate='1/25s'):
    return (HEADER + f'<fcpxml version="1.10"><resources><format id="r1" frameDuration="{rate}" width="1920" height="1080"/>'
            f'<asset id="r2" name="a.mov" start="0s" duration="3600s" hasVideo="1" hasAudio="1" audioChannels="2" format="r1">'
            f'<media-rep kind="original-media" src="file:///m/a.mov"/></asset>{resources}</resources><library><event name="e">'
            f'<project name="p"><sequence format="r1" duration="100s" tcStart="0s"><spine>{spine}</spine></sequence>'
            '</project></event></library></fcpxml>').encode()


class Media(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-interchange-r1-')
        cls.paths = make_media(Path(cls.temp.name))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()


# -- A.1 FCPXML validity against the 1.10 DTD ------------------------------------------------------

class FcpxmlDtd(Media):
    def variety(self):
        """The synthetic timeline plus what the fuzz sets hit: a title with opacity keys, fades with
        volume keys, drop-frame sources, retimes, a disabled clip and a secondary storyline."""
        p = synthetic_project(self.paths)
        title = next(c for c in p['clips'] if c['type'] == 'title')
        title.update(opacity=60, keyframes={'opacity': [{'time': 0, 'value': 0}, {'time': 1, 'value': 60}]},
                     title='LINE ONE\nLINE TWO')
        tone = next(c for c in p['clips'] if c['id'] == 'a2-tone2')
        tone.update(fadeIn=at(6), fadeOut=at(6), volume=80, keyframes={'volume': [{'time': 0, 'value': 80}, {'time': 1, 'value': 40}]})
        return [p, alpine_project(ROOT)]

    def test_exports_follow_the_dtd_rules(self):
        for p in self.variety():
            with self.subTest(timeline=p['name']):
                root = xmlsafe.parse(fcpxml.write(build_cut(p, {'unknownTimecode': 'zero'})).encode())
                self.assertEqual(fcpxml_dtd_rules.problems(root), [])
                for asset in root.iter('asset'):
                    self.assertIsNone(asset.get('tcFormat'), 'tcFormat is not an asset attribute in FCPXML 1.10')
                for title in root.iter('title'):
                    tags = [child.tag for child in title]
                    self.assertLess(tags.index('text'), tags.index('adjust-transform'))
                    self.assertLess(tags.index('text-style-def'), tags.index('adjust-transform'))
                for param in root.iter('param'):
                    tags = [child.tag for child in param]
                    if 'fadeOut' in tags and 'keyframeAnimation' in tags:
                        self.assertLess(tags.index('fadeOut'), tags.index('keyframeAnimation'))

    def test_the_rules_checker_catches_what_the_old_exporter_wrote(self):
        bad = ET.fromstring('<fcpxml version="1.10"><resources><asset id="a" tcFormat="DF"><media-rep src="x"/></asset>'
                            '</resources><library><event><project><sequence format="f"><spine>'
                            '<title ref="e" duration="1s"><adjust-transform/><text/></title></spine></sequence></project>'
                            '</event></library></fcpxml>')
        found = ' | '.join(fcpxml_dtd_rules.problems(bad))
        self.assertIn('undeclared attribute tcFormat', found)
        self.assertIn('<title> children out of DTD order', found)
        param = ET.fromstring('<param name="amount"><fadeIn duration="1s"/><keyframeAnimation/><fadeOut duration="1s"/></param>')
        self.assertTrue(fcpxml_dtd_rules.problems(param))

    def test_drop_frame_comes_from_the_clip_as_final_cut_pro_writes_it(self):
        spine = '<asset-clip ref="r2" offset="0s" duration="5s" start="107892/30s" name="df" tcFormat="DF"/>'
        data = fcpxml_doc(spine, rate='1001/30000s').replace(b'start="0s" duration="3600s"', b'start="3596400/1000s" duration="3600s"')
        project_ = fcpxml.read(data)
        record = project_['media'][0]
        self.assertTrue(record['dropFrame'])
        self.assertIn(';', record['timecode'])
        # and an asset-level tcFormat (not in the DTD) is not what decides it
        legacy = fcpxml_doc('<asset-clip ref="r2" offset="0s" duration="5s" name="x"/>', rate='1001/30000s').replace(
            b'hasVideo="1"', b'hasVideo="1" tcFormat="DF"')
        self.assertFalse(fcpxml.read(legacy)['media'][0]['dropFrame'])


# -- A.2 media metadata contract -------------------------------------------------------------------

class MediaContract(Media):
    def asset(self, item_, options=None):
        p = project([item_], [clip('c1', item_['id'], 0, 1)])
        cut = build_cut(p, dict(options or {}, unknownTimecode='zero'))
        root = xmlsafe.parse(fcpxml.write(cut).encode())
        return root.find('resources/asset'), cut

    def test_integer_audio_stream_count_offline(self):
        asset, cut = self.asset(item(audioStreams=2))
        self.assertEqual(asset.get('hasAudio'), '1')
        self.assertEqual(asset.get('audioSources'), '2')
        self.assertTrue(any('2 audio streams' in line for line in cut.report.general), cut.report.general)

    def test_stream_details_are_read(self):
        details = [{'index': 0, 'channels': 6, 'layout': '5.1', 'codec': 'pcm_s24le'}]
        asset, _ = self.asset(item(audioStreams=1, audioStreamDetails=details))
        self.assertEqual((asset.get('hasAudio'), asset.get('audioSources'), asset.get('audioChannels')), ('1', '1', '6'))
        asset, _ = self.asset(item(audioStreams=0))
        self.assertIsNone(asset.get('hasAudio'))

    def test_complete_metadata_is_not_probed_again(self):
        path = str(self.paths['camera_a'])
        details = [{'index': 0, 'channels': 2, 'layout': 'stereo', 'codec': 'pcm_s16le'}]
        full = item(sourcePath=path, rate='24000/1001', audioStreams=1, audioStreamDetails=details)
        with mock.patch.object(media, 'probe_file', side_effect=AssertionError('probed')) as probe:
            self.asset(full)
            self.asset(dict(full, audioStreamDetails=None, audioStreams=0))
        self.assertEqual(probe.call_count, 0)

    def test_missing_fields_are_probed(self):
        calls = []
        real = media.probe_file
        with mock.patch.object(media, 'probe_file', side_effect=lambda p, *a: calls.append(p) or real(p, *a)):
            asset, _ = self.asset({'id': 'm1', 'name': 'a.mov', 'type': 'video', 'sourcePath': str(self.paths['camera_a'])})
        self.assertEqual(len(calls), 1)
        self.assertEqual(asset.get('start'), '86486400/24000s')

    def test_probing_has_a_total_time_budget(self):
        budget_ = media.ProbeBudget(count=500, seconds=0)
        self.assertIsNone(budget_.take())
        items = [{'id': f'm{i}', 'name': f'f{i}.mov', 'type': 'video', 'sourcePath': str(self.paths['camera_a'])} for i in range(3)]
        p = project(items, [clip(f'c{i}', f'm{i}', i, 1) for i in range(3)])
        with mock.patch.object(media, 'probe_file', side_effect=AssertionError('no time left to probe')):
            cut = build_cut(p, {'unknownTimecode': 'zero', 'probeSeconds': 0})
        line = next(line for line in cut.report.general if line.startswith('Probing stopped'))
        self.assertIn('f0.mov, f1.mov, f2.mov', line)

    def test_each_probe_gets_only_the_time_left(self):
        seen = []
        with mock.patch.object(media, 'probe_file', side_effect=lambda p, timeout: seen.append(timeout) or media.Probe()):
            build_cut(project([{'id': 'm1', 'name': 'a.mov', 'type': 'video', 'sourcePath': str(self.paths['camera_a'])}],
                              [clip('c1', 'm1', 0, 1)]), {'unknownTimecode': 'zero', 'probeSeconds': 3})
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], 3)


# -- A.3 compound clip fan-out, budget and cancel --------------------------------------------------

def fan_out(levels, fan, leaf):
    media_ = ''.join(f'<media id="m{k}"><sequence format="r1" duration="10s" tcStart="0s"><spine>'
                     + ''.join(f'<ref-clip ref="m{k + 1}" offset="0s" duration="10s"/>' for _ in range(fan))
                     + '</spine></sequence></media>' for k in range(1, levels))
    media_ += f'<media id="m{levels}"><sequence format="r1" duration="10s" tcStart="0s"><spine>{leaf}</spine></sequence></media>'
    return fcpxml_doc('<ref-clip ref="m1" offset="0s" duration="10s"/>', media_)


class ImportBudget(unittest.TestCase):
    def test_fan_out_outside_the_visible_range_is_not_walked(self):
        media_ = ''.join(f'<media id="m{k}"><sequence format="r1" duration="10s" tcStart="0s"><spine>'
                         + ''.join(f'<ref-clip ref="m{k + 1}" offset="{j}s" duration="1s"/>' for j in range(10))
                         + '</spine></sequence></media>' for k in range(1, 7))
        media_ += '<media id="m7"><sequence format="r1" duration="10s" tcStart="0s"><spine><asset-clip ref="r2" offset="0s" duration="1s"/></spine></sequence></media>'
        started = time.monotonic()
        project_ = fcpxml.read(fcpxml_doc('<ref-clip ref="m1" offset="0s" duration="10s"/>', media_))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(project_['clips']), 10)

    def test_visible_fan_out_stops_at_the_visit_budget(self):
        data = fan_out(7, 10, '<gap offset="0s" duration="10s"/>')   # 10^6 gaps, all visible, no clips
        started = time.monotonic()
        with self.assertRaisesRegex(budget.BudgetExceeded, 'compound clips are used so many times'):
            fcpxml.read(data)
        self.assertLess(time.monotonic() - started, 10.0)

    def test_time_budget(self):
        slow = budget.Budget(seconds=0.0, visits=10 ** 9)
        time.sleep(0.01)
        with self.assertRaisesRegex(budget.BudgetExceeded, 'took longer'):
            for _ in range(1000):
                slow.tick()

    def test_cancel_stops_a_running_import(self):
        data = fan_out(7, 10, '<gap offset="0s" duration="10s"/>')
        outcome = {}

        def run():
            try:
                api.import_timeline('big.fcpxml', data.decode(), {'jobId': 'cancel-test-0001'})
            except Exception as error:  # noqa: BLE001 - the test inspects it
                outcome['error'] = error
        with mock.patch.object(budget, 'IMPORT_VISITS', 10 ** 9), mock.patch.object(budget, 'IMPORT_SECONDS', 60.0):
            worker = threading.Thread(target=run)
            worker.start()
            for _ in range(200):
                if api.handle('cancel', {'jobId': 'cancel-test-0001'})['cancelled']:
                    break
                time.sleep(0.01)
            worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertIn('cancelled', str(outcome.get('error')))
        self.assertEqual(api.handle('cancel', {'jobId': 'cancel-test-0001'}), {'cancelled': False})


# -- A.4 sync-clip with a spine -------------------------------------------------------------------

class SyncClip(unittest.TestCase):
    def test_picture_in_a_sync_clip_spine_imports_once(self):
        res = ('<asset id="r4" name="m.wav" start="0s" duration="100s" hasAudio="1" audioChannels="2">'
               '<media-rep kind="original-media" src="file:///m/m.wav"/></asset>')
        spine = ('<sync-clip offset="0s" duration="5s" name="s"><spine><asset-clip ref="r2" offset="0s" duration="5s" name="v"/>'
                 '</spine><asset-clip ref="r4" lane="-1" offset="0s" duration="5s" name="aud"/></sync-clip>')
        clips = fcpxml.read(fcpxml_doc(spine, res))['clips']
        self.assertEqual(sorted((c['type'], c['track']) for c in clips), [('audio', -1), ('video', 1)])


# -- A.5 / A.6 EDL timecode rule and the reference parser -----------------------------------------

def otio_cmx_header_ok(text):
    """The rule OpenTimelineIO's cmx_3600 reader applies: before the first event only TITLE and FCM."""
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if line[0].isdigit():
            return True
        if not line.startswith(('TITLE:', 'FCM')):
            return False
    return True


class EdlRule(Media):
    def mixed(self):
        """A 23.976 list holding a 29.97 DF source and a 25 fps source, one retimed to 0.75x."""
        drop = item('d', 'drop.mov', rate='30000/1001', timecode='00:59:59;28', dropFrame=True, audioStreams=0)
        pal = item('p', 'pal.mov', rate='25', timecode='10:00:00:00', audioStreams=0)
        clips = [clip('c1', 'd', at(0), at(48) - at(0), float(Fraction(34) / DF)),
                 clip('c2', 'p', at(48), at(96) - at(48), 1.0, speed=.75),
                 clip('c3', 'd', at(96), at(120) - at(96), 2.0)]
        return project([drop, pal], clips, fps=float(RATE))

    def test_source_timecode_counts_at_the_list_rate(self):
        files = dict(edl.write(build_cut(self.mixed(), {})))
        for line in files['V1'].splitlines():
            if line[:3].isdigit():
                for label in line.split()[-4:]:
                    self.assertLess(int(label[-2:]), 24, f'{label} has a frame past 23 in a 23.976 list')
                    timecode_frames(label, RATE)
        # 00:59:59;28 read at 23.976 is 00:59:59:22; 34 frames at 29.97 are 27 list frames
        self.assertTrue(files['V1'].splitlines()[3].startswith('001  DROP     V     C        01:00:01:01 01:00:03:01'), files['V1'])

    def test_offline_mixed_rate_import_does_not_raise(self):
        text = dict(edl.write(build_cut(self.mixed(), {})))['V1']
        imported = api.import_timeline('mixed.edl', text, {'fps': '24000/1001'})
        self.assertEqual(len(imported['clips']), 3)
        speeds = sorted(c.get('speed', 1) for c in imported['clips'])
        self.assertEqual(speeds[0], 0.75, 'the M2 rate, not the source span, sets the speed')

    def test_fcm_lines_follow_drop_frame_changes(self):
        ndf = item('n', 'ndf.mov', rate='30000/1001', timecode='01:00:00:00', dropFrame=False, audioStreams=0)
        df = item('d', 'df.mov', rate='30000/1001', timecode='01:00:00;00', dropFrame=True, audioStreams=0)
        fps = float(DF)
        clips = [clip('a', 'n', 0, 2, 0), clip('b', 'd', 2, 2, 0, speed=2), clip('c', 'n', 4, 2, 0)]
        text = dict(edl.write(build_cut(project([ndf, df], clips, fps=fps), {'dropFrame': True})))['V1']
        lines = [l for l in text.splitlines() if l]
        self.assertEqual(lines[1], 'FCM: DROP FRAME')
        events = [i for i, l in enumerate(lines) if l[:3].isdigit()]
        self.assertEqual(lines[events[0] - 1], 'FCM: NON-DROP FRAME')
        self.assertIn(';', lines[events[1]].split()[4])               # DF source written with ';'
        self.assertEqual(lines[events[1] - 1], 'FCM: DROP FRAME')
        self.assertEqual(lines[events[2] - 1], 'FCM: NON-DROP FRAME')
        for line in lines:
            if line.startswith('M2'):
                self.assertNotIn(';', line, 'the reference parser reads M2 timecode with colons only')
        imported = edl.read(text.encode(), {'fps': '30000/1001'})
        self.assertEqual([round(c['start'] * fps) for c in imported['clips']], [0, 60, 120])

    def test_reel_map_sits_inside_events(self):
        for name, text in edl.write(build_cut(synthetic_project(self.paths), {}), audio=True):
            with self.subTest(edl=name):
                self.assertTrue(otio_cmx_header_ok(text), text[:300])
                self.assertIn('* REEL', text) if name == 'V1' else None

    def test_disabled_track_writes_no_empty_list(self):
        p = project([item()], [clip('a', 'm1', 0, 1), clip('b', 'm1', 0, 1, track=2, enabled=False)])
        files = dict(edl.write(build_cut(p, {})))
        self.assertEqual(sorted(files), ['V1'])

    def test_fade_in_that_opens_a_list_gets_black_first(self):
        p = project([item()], [clip('a', 'm1', 1, 2, 1, transition='dissolve', transitionDuration=.4)])
        text = dict(edl.write(build_cut(p, {})))['V1']
        first = next(l for l in text.splitlines() if l[:3].isdigit())
        self.assertTrue(first.startswith('001  BL') and not first.endswith('01:00:01:00 01:00:01:00'), text)
        self.assertEqual(round(edl.read(text.encode(), {'fps': 25})['clips'][0]['start'] * 25), 25)


# -- A.7 end-aligned dissolve ---------------------------------------------------------------------

class EndAlignedDissolve(unittest.TestCase):
    def test_dissolve_ending_on_the_cut_survives_a_round_trip(self):
        p = project([item()], [clip('a', 'm1', 0, 2, 1), clip('b', 'm1', 2, 2, 5, transition='dissolve',
                                                               transitionDuration=.4, transitionBefore=.4)])
        imported = fcpxml.read(fcpxml.write(build_cut(p, {})).encode())
        second = next(c for c in imported['clips'] if c['start'] == 2)
        self.assertEqual((second['transition'], second['transitionDuration'], second['transitionBefore']), ('dissolve', .4, .4))
        self.assertFalse([w for w in imported['compatibilityWarnings'] if 'does not lead into a clip' in w])


# -- A.8 EDL dissolve into a disabled clip --------------------------------------------------------

class DisabledDissolve(unittest.TestCase):
    def test_outgoing_clip_keeps_its_frames_when_the_dissolve_clip_is_disabled(self):
        p = project([item()], [clip('a', 'm1', 0, 2, 1), clip('b', 'm1', 2, 2, 5, transition='dissolve', transitionDuration=.4,
                                                               transitionBefore=.2, enabled=False)])
        text = dict(edl.write(build_cut(p, {})))['V1']
        event = next(l for l in text.splitlines() if l.startswith('001'))
        self.assertTrue(event.endswith('01:00:00:00 01:00:02:00'), event)


# -- A.9 / A.10 the smaller findings ---------------------------------------------------------------

class SmallerFindings(Media):
    def test_m2_speed_and_key_events(self):
        text = ('TITLE: t\nFCM: NON-DROP FRAME\n'
                '001  AX       V     C        00:00:10:00 00:00:10:18 01:00:00:00 01:00:01:00\n'
                'M2   AX              018.0                00:00:10:00\n'
                '002  AX       V     K    010 00:00:20:00 00:00:21:00 01:00:01:00 01:00:02:00\n')
        clips = edl.read(text.encode(), {'fps': 24})['clips']
        self.assertEqual(clips[0]['speed'], 0.75)
        self.assertEqual(clips[1]['transition'], 'none', 'a key imported as a cut has no dissolve')

    def test_api_needs_the_edl_rate_and_object_options(self):
        good = 'TITLE: t\nFCM: NON-DROP FRAME\n001  AX       V     C        00:00:00:00 00:00:01:00 01:00:00:00 01:00:01:00\n'
        with self.assertRaisesRegex(InterchangeError, 'frame rate'):
            api.import_timeline('a.edl', good, {})
        with self.assertRaisesRegex(InterchangeError, 'options must be an object'):
            api.import_timeline('a.edl', good, ['fps'])
        with self.assertRaisesRegex(InterchangeError, 'options must be an object'):
            api.handle('export', {'project': project([item()], [clip('a', 'm1', 0, 1)]), 'options': 'x'})
        with self.assertRaisesRegex(InterchangeError, "Frame size 'x'"):
            api.import_timeline('a.edl', good, {'fps': 25, 'width': 'x'})
        self.assertEqual(api.import_timeline('a.edl', good, {'fps': 25, 'width': '1280'})['width'], 1280)

    def test_each_multicam_clip_is_named(self):
        spine = ('<mc-clip ref="r9" offset="0s" duration="2s" name="Interview"/>'
                 '<asset-clip ref="r2" offset="2s" duration="1s" name="v"/>')
        warnings = fcpxml.read(fcpxml_doc(spine))['compatibilityWarnings']
        self.assertTrue(any('Multicam clip "Interview" at 0.00 s' in w for w in warnings), warnings)

    def test_silent_losses_get_report_lines(self):
        p = project([item()], [clip('a', 'm1', 0, 1, nativeVolumeOutsideTrim=True), clip('b', 'm1', 1, 1, speed=40),
                               clip('c', 'm1', 2, 1, speed=.05)])
        general = ' | '.join(build_cut(p, {}).report.general)
        self.assertIn('volume keyframes outside the trimmed part', general)
        self.assertIn('speed 40x is outside', general)
        self.assertIn('speed 0.05x is outside', general)

    def test_linked_sound_with_the_picture_range_comes_back_as_its_own_clip(self):
        clips = [clip('v', 'm1', 0, 2, 1, embeddedAudio=False, linkGroup='g'), clip('s', 'm1', 0, 2, 1, track=-1, kind='audio', linkGroup='g')]
        imported = fcpxml.read(fcpxml.write(build_cut(project([item(audioStreams=1)], clips), {})).encode())
        self.assertEqual(sorted((c['type'], c['track'], c.get('linkGroup')) for c in imported['clips']),
                         [('audio', -1, 'g'), ('video', 1, 'g')])


# -- B: what real Resolve and Premiere showed ------------------------------------------------------

class RealApplications(Media):
    def test_resolve_fcpxml_title_name_and_track_compaction(self):
        p = alpine_project(ROOT)
        p['clips'][4]['title'] = 'ALPINE PASSAGE\nA SECOND LINE ' + 'X' * 300
        result = api.export_timeline(p, {'formats': ['fcpxml'], 'unknownTimecode': 'zero'})
        root = xmlsafe.parse(result['files'][0]['content'].encode())
        self.assertEqual(root.find('.//title').get('name'), 'ALPINE PASSAGE')
        self.assertIn('DaVinci Resolve drops empty video tracks when it imports FCPXML, so tracks move down: V4 -> V2.', result['report'])
        self.assertIn('Text generator named "Text"', result['report'])

    def test_resolve_edl_conform_warnings(self):
        report = api.export_timeline(alpine_project(ROOT), {'formats': ['edl'], 'audioEdl': True})['report']
        warning = next(line for line in report.splitlines() if 'no embedded timecode and no reel name' in line)
        for name in ('The Ridge', 'Into the Valley', 'Forest Light', 'The Lake'):
            self.assertIn(name, warning)
        self.assertIn('share source timecode', report)
        self.assertIn('refuses an EDL that holds only audio events', report)
        self.assertIn('Premiere Pro shows its own "EDL information"', report)

    def test_sources_with_timecode_and_reels_draw_no_conform_warning(self):
        items = [item('a', 'a.mov', reel='A001', timecode='01:00:00:00', duration=10),
                 item('b', 'b.mov', reel='B001', timecode='02:00:00:00', duration=10)]
        report = api.export_timeline(project(items, [clip('1', 'a', 0, 1), clip('2', 'b', 1, 1)]), {'formats': ['edl']})['report']
        self.assertNotIn('CONFORM WARNING', report)

    def test_premiere_otio_sequence_name_titles_and_metadata_report(self):
        p = synthetic_project(self.paths)
        result = api.export_timeline(p, {'formats': ['otio']})
        doc = json.loads(result['files'][0]['content'])
        self.assertEqual(doc['tracks']['name'], p['name'], 'Premiere names the sequence after the top-level stack')
        title = next(c for t in doc['tracks']['children'] for c in t['children'] if c['OTIO_SCHEMA'] == 'Clip.2'
                     and c['media_references']['DEFAULT_MEDIA']['OTIO_SCHEMA'] == 'GeneratorReference.1')
        self.assertEqual(title['name'], 'SYNTHETIC TURNOVER')
        self.assertEqual(title['media_references']['DEFAULT_MEDIA']['name'], 'Splice title: SYNTHETIC TURNOVER')
        report = result['report']
        self.assertIn('B001_C001_df.mov (V2 at 01:00:01:00): opacity 50 %; scale 80 %', report)
        self.assertIn('room_tone.wav (A2 at 01:00:00:00): gain 70 %', report)
        self.assertIn('offline placeholder named after its text', report)


# -- OTIO as OpenTimelineIO 0.18 writes it (always runs; the real package is optional) ------------

OTIO_018 = {  # field order of OpenTimelineIO 0.18.1's serializer, recorded from the package
    'Timeline.1': ['OTIO_SCHEMA', 'metadata', 'name', 'global_start_time', 'tracks'],
    'Stack.1': ['OTIO_SCHEMA', 'metadata', 'name', 'source_range', 'effects', 'markers', 'enabled', 'color', 'children'],
    'Track.1': ['OTIO_SCHEMA', 'metadata', 'name', 'source_range', 'effects', 'markers', 'enabled', 'color', 'children', 'kind'],
    'Clip.2': ['OTIO_SCHEMA', 'metadata', 'name', 'source_range', 'effects', 'markers', 'enabled', 'color', 'media_references',
               'active_media_reference_key'],
    'Gap.1': ['OTIO_SCHEMA', 'metadata', 'name', 'source_range', 'effects', 'markers', 'enabled', 'color'],
    'Transition.1': ['OTIO_SCHEMA', 'metadata', 'name', 'in_offset', 'out_offset', 'transition_type'],
    'LinearTimeWarp.1': ['OTIO_SCHEMA', 'metadata', 'name', 'effect_name', 'enabled', 'time_scalar'],
    'ExternalReference.1': ['OTIO_SCHEMA', 'metadata', 'name', 'available_range', 'available_image_bounds', 'target_url'],
    'GeneratorReference.1': ['OTIO_SCHEMA', 'metadata', 'name', 'available_range', 'available_image_bounds', 'generator_kind',
                             'parameters'],
    'Marker.2': ['OTIO_SCHEMA', 'metadata', 'name', 'color', 'marked_range', 'comment'],
    'TimeRange.1': ['OTIO_SCHEMA', 'duration', 'start_time'],
    'RationalTime.1': ['OTIO_SCHEMA', 'rate', 'value'],
}


class OtioStructure(Media):
    def test_every_object_matches_the_serializer_of_opentimelineio_018(self):
        text = otio.write(build_cut(synthetic_project(self.paths), {}))
        self.assertFalse(text.endswith('\n'), 'OTIO writes no trailing newline')
        seen = set()

        def walk(value, in_metadata=False):
            if isinstance(value, dict):
                if 'OTIO_SCHEMA' in value and not in_metadata:
                    schema = value['OTIO_SCHEMA']
                    seen.add(schema)
                    self.assertEqual(list(value), OTIO_018[schema], schema)
                    if schema == 'Clip.2':
                        self.assertEqual(list(value['media_references']), ['DEFAULT_MEDIA'])
                        self.assertEqual(value['active_media_reference_key'], 'DEFAULT_MEDIA')
                for key, child in value.items():
                    if key == 'metadata':
                        self.assertEqual(set(child) - {'splice'}, set(), 'Splice data stays under metadata.splice')
                        self.assertEqual(list(child.get('splice', {})), sorted(child.get('splice', {})), 'OTIO sorts metadata keys')
                    walk(child, in_metadata or key == 'metadata')
            elif isinstance(value, list):
                for child in value:
                    walk(child, in_metadata)
        walk(json.loads(text))
        self.assertLessEqual({'Timeline.1', 'Stack.1', 'Track.1', 'Clip.2', 'Gap.1', 'Transition.1', 'LinearTimeWarp.1',
                              'ExternalReference.1', 'GeneratorReference.1', 'Marker.2'}, seen)
        self.assertEqual(json.dumps(json.loads(text), indent=4, ensure_ascii=False), text, 'four-space JSON, UTF-8 kept')

    @unittest.skipUnless(__import__('importlib').util.find_spec('opentimelineio'), 'opentimelineio is not installed')
    def test_opentimelineio_round_trips_it_unchanged(self):
        import opentimelineio
        for p in (synthetic_project(self.paths), alpine_project(ROOT)):
            text = otio.write(build_cut(p, {'unknownTimecode': 'zero'}))
            again = opentimelineio.adapters.write_to_string(opentimelineio.adapters.read_from_string(text, 'otio_json'), 'otio_json')
            self.assertEqual(json.loads(again), json.loads(text))


# -- mutation-test gaps ----------------------------------------------------------------------------

class PinnedArithmetic(unittest.TestCase):
    def event(self, p, options=None, index=0):
        return sorted(build_cut(p, options or {}).events, key=lambda e: e.id)[index]

    def test_snap_tolerance_is_a_tenth_of_a_percent(self):
        self.assertEqual(snap_rate(24.02), Fraction(24))
        for off in (24.5, 25.6, 23.5, 29.0):
            with self.subTest(rate=off):
                self.assertIsNone(snap_rate(off))

    def test_source_in_rounds_to_the_nearest_frame(self):
        e = self.event(project([item()], [clip('a', 'm1', 0, 1, 0.07)]))   # 1.75 frames at 25
        self.assertEqual(e.source_in, Fraction(2, 25))

    def test_transition_before_and_length_are_clamped(self):
        e = self.event(project([item()], [clip('a', 'm1', 0, 2), clip('b', 'm1', 2, 2, 1, transition='dissolve',
                                                                     transitionDuration=.4, transitionBefore=3)]), index=1)
        self.assertEqual((e.transition.frames, e.transition.before), (10, 10))
        e = self.event(project([item()], [clip('a', 'm1', 0, 2), clip('b', 'm1', 2, .2, 1, transition='dissolve',
                                                                     transitionDuration=5, transitionBefore=.12)]), index=1)
        self.assertEqual(e.transition.frames, 5 + 3, 'no longer than the clip plus its frames before the cut')

    def test_transition_starting_before_the_range_becomes_a_cut(self):
        p = project([item()], [clip('a', 'm1', 0, 2.08), clip('b', 'm1', 2.08, 2, 1, transition='dissolve', transitionDuration=.4,
                                                              transitionBefore=.2)])
        e = self.event(p, {'range': {'start': 2.0, 'end': 4}}, index=1)
        self.assertEqual(e.record_in, 2)
        self.assertIsNone(e.transition)

    def test_reverse_clip_trimmed_at_its_tail_moves_its_source_in(self):
        p = project([item()], [clip('a', 'm1', 0, 4, 1, reverse=True)])
        e = self.event(p, {'range': {'start': 0, 'end': 3}})
        self.assertEqual((e.frames, e.source_in), (75, Fraction(2)))
        e = self.event(p, {'range': {'start': 1, 'end': 4}})
        self.assertEqual((e.frames, e.source_in), (75, Fraction(1)))

    def test_speed_keeps_four_decimals(self):
        self.assertEqual(self.event(project([item()], [clip('a', 'm1', 0, 1, speed=.3333)])).speed, Fraction(3333, 10000))

    def test_edl_event_numbers_widen_past_999(self):
        p = project([item()], [clip(f'c{i}', 'm1', i * .08, .08, 1) for i in range(1001)])
        cut = build_cut(p, {})
        lines = [l for l in dict(edl.write(cut))['V1'].splitlines() if l[:1].isdigit()]
        self.assertTrue(lines[0].startswith('001  '))
        self.assertTrue(lines[998].startswith('999  '))
        self.assertTrue(lines[999].startswith('1000  '))
        self.assertTrue(any('exceed CMX3600' in line for line in cut.report.by_format['EDL']))

    def test_edl_zero_length_event_is_skipped(self):
        text = ('TITLE: t\nFCM: NON-DROP FRAME\n001  AX       V     C        00:00:00:00 00:00:00:00 01:00:00:00 01:00:00:00\n'
                '002  AX       V     C        00:00:00:00 00:00:01:00 01:00:00:00 01:00:01:00\n')
        imported = edl.read(text.encode(), {'fps': 25})
        self.assertEqual(len(imported['clips']), 1)
        self.assertTrue(any('Event 1 has no record length' in w for w in imported['compatibilityWarnings']))

    def test_fcpxml_does_not_merge_a_linked_pair_that_drifts(self):
        clips = [clip('v', 'm1', 0, 2, 1, embeddedAudio=False, linkGroup='g'), clip('s', 'm1', 0, 2, 1.4, track=-1, kind='audio', linkGroup='g')]
        cut = build_cut(project([item(audioStreams=1)], clips), {})
        text = fcpxml.write(cut)
        self.assertNotIn('audioStart', text)
        self.assertIn('srcEnable="audio"', text)
        self.assertFalse(any('linked to its picture' in line for line in cut.report.by_format['FCPXML']))


if __name__ == '__main__':
    unittest.main()
