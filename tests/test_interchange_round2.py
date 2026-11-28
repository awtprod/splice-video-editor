"""Timeline interchange, fix round 2: linked sound and link indices in FCP 7 XML, bounded and
honest imports, path mapping precedence, and Basic Motion in Premiere Pro's own terms."""
import base64
import sys
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange import api, media, project as project_module, xmeml, xmlsafe  # noqa: E402
from interchange.budget import Budget, BudgetExceeded  # noqa: E402
from interchange.cut import build_cut  # noqa: E402
from interchange_fixtures import DF, RATE, at, make_media, synthetic_project  # noqa: E402

SEQUENCE = '<rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>'


def document(video='', audio='', width=1920, height=1080):
    return (f'<?xml version="1.0"?><xmeml version="5"><sequence id="s"><name>T</name><duration>500</duration>{SEQUENCE}'
            f'<media><video><format><samplecharacteristics><width>{width}</width><height>{height}</height>'
            f'</samplecharacteristics></format>{video}</video><audio>{audio}</audio></media></sequence></xmeml>').encode()


def file_element(fid='f1', width=1920, height=1080, extra=''):
    return (f'<file id="{fid}"><name>a.mov</name><pathurl>file://localhost/nowhere/a.mov</pathurl>{SEQUENCE}'
            f'<duration>500</duration><media><video><samplecharacteristics><width>{width}</width><height>{height}</height>'
            f'{extra}</samplecharacteristics></video><audio><channelcount>2</channelcount></audio></media></file>')


def clipitem(cid, start, end, links=(), filters='', fid='f1', definition=None, channel=None):
    link = ''.join(f'<link><linkclipref>{ref}</linkclipref></link>' for ref in links)
    track = f'<sourcetrack><mediatype>audio</mediatype><trackindex>{channel}</trackindex></sourcetrack>' if channel else ''
    body = definition if definition is not None else f'<file id="{fid}"/>'
    return (f'<clipitem id="{cid}"><name>{cid}</name><duration>500</duration>{SEQUENCE}<start>{start}</start><end>{end}</end>'
            f'<in>0</in><out>{end - start}</out>{body}{track}{filters}{link}</clipitem>')


def levels(value, keys=()):
    points = ''.join(f'<keyframe><when>{w}</when><value>{v}</value></keyframe>' for w, v in keys)
    return (f'<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid><parameter><parameterid>level'
            f'</parameterid><value>{value}</value>{points}</parameter></effect></filter>')


def track(items, name=None):
    attrs = f' MZ.TrackName="{name}"' if name else ''
    return f'<track{attrs}>{items}</track>'


class LinkedSound(unittest.TestCase):
    """A1: a linked sound clip keeps its gain, fades and keyframes."""

    def test_linked_sound_on_an_audio_track_stays_a_sound_clip(self):
        picture = clipitem('v', 0, 100, links=('v', 'a'), definition=file_element())
        sound = clipitem('a', 0, 100, links=('v', 'a'), filters=levels(0.4, [(0, 0), (12, 0.4)]), channel=1)
        result = xmeml.read(document(track(picture), track(sound, 'A2')))
        kinds = sorted(c['type'] for c in result['clips'])
        self.assertEqual(kinds, ['audio', 'video'], 'the linked sound is not folded into the picture')
        audio = next(c for c in result['clips'] if c['type'] == 'audio')
        video = next(c for c in result['clips'] if c['type'] == 'video')
        self.assertEqual(audio['volume'], 40)
        self.assertEqual([p['value'] for p in audio['keyframes']['volume']], [0, 40], 'the fade-in survives')
        self.assertEqual(audio['linkGroup'], video['linkGroup'])
        self.assertFalse(video['embeddedAudio'])

    def test_premiere_av_clip_with_audio_levels_keeps_its_sound(self):
        picture = clipitem('v', 0, 100, links=('v', 'l', 'r'), definition=file_element())
        left = clipitem('l', 0, 100, links=('v', 'l', 'r'), filters=levels(0.5), channel=1)
        right = clipitem('r', 0, 100, links=('v', 'l', 'r'), filters=levels(0.5), channel=2)
        result = xmeml.read(document(track(picture, 'V1'), track(left, 'A1') + track(right, 'A1')))
        audio = [c for c in result['clips'] if c['type'] == 'audio']
        self.assertEqual(len(audio), 1, 'two channels of one stereo clip are one Splice clip')
        self.assertEqual(audio[0]['volume'], 50)

    def test_own_sound_track_collapses_into_embedded_audio_with_its_level(self):
        picture = clipitem('v', 0, 100, links=('v', 'l', 'r'), definition=file_element())
        left = clipitem('l', 0, 100, links=('v', 'l', 'r'), filters=levels(0.7), channel=1)
        right = clipitem('r', 0, 100, links=('v', 'l', 'r'), filters=levels(0.7), channel=2)
        result = xmeml.read(document(track(picture, 'V1'), track(left, 'V1 sound') + track(right, 'V1 sound')))
        self.assertEqual([c['type'] for c in result['clips']], ['video'])
        self.assertTrue(result['clips'][0]['embeddedAudio'])
        self.assertEqual(result['clips'][0]['volume'], 70, 'nothing is lost: the level moves to the picture clip')

    def test_unlinked_reversed_pair_on_its_own_sound_track_is_one_clip(self):
        # Premiere's own export of a reversed picture clip and its sound: no link, but "V1 sound"
        remap = ('<filter><effect><effectid>timeremap</effectid><parameter><parameterid>reverse</parameterid>'
                 '<value>TRUE</value></parameter></effect></filter>')
        picture = clipitem('v', 0, 100, filters=remap, definition=file_element())
        sound = clipitem('a', 0, 100, links=('a',), filters=remap + levels(1), channel=1)
        result = xmeml.read(document(track(picture, 'V1'), track(sound, 'V1 sound')))
        self.assertEqual([(c['type'], c.get('reverse'), c.get('embeddedAudio')) for c in result['clips']],
                         [('video', True, True)])


class Media(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-r2-')
        cls.paths = make_media(Path(cls.temp.name))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def export(self, project, **options):
        return xmeml.write(build_cut(project, options))

    def root(self, project, **options):
        return xmlsafe.parse(self.export(project, **options).encode())

    def report(self, project, **options):
        return api.export_timeline(project, dict(options, formats=['xmeml'], package=False))['report']


class LinkIndices(Media):
    """A2: ``clipindex`` counts every item of the track, transitions included, from 1."""

    def test_clipindex_counts_transitions(self):
        root = self.root(synthetic_project(self.paths))
        tracks = {('video', i): t for i, t in enumerate(root.findall('sequence/media/video/track'), 1)}
        tracks.update({('audio', i): t for i, t in enumerate(root.findall('sequence/media/audio/track'), 1)})
        items = {}
        for (kind, index), element in tracks.items():
            children = [c for c in element if c.tag in ('clipitem', 'generatoritem', 'transitionitem')]
            for position, child in enumerate(children, 1):
                if child.get('id'):
                    items[child.get('id')] = (kind, index, position)
        checked = 0
        for element in root.iter('clipitem'):
            for link in element.iterfind('link'):
                kind, index, position = items[link.findtext('linkclipref')]
                self.assertEqual((link.findtext('mediatype'), int(link.findtext('trackindex')), int(link.findtext('clipindex'))),
                                 (kind, index, position))
                checked += 1
        self.assertGreater(checked, 20)
        v1 = [c for c in tracks[('video', 1)] if c.tag == 'clipitem']
        own = [int(next(l for l in c.iterfind('link') if l.findtext('linkclipref') == c.get('id')).findtext('clipindex'))
               for c in v1 if c.find('link') is not None]
        self.assertEqual(own[:2], [1, 3], 'the dissolve at position 2 is counted, as Premiere counts it')


class Bounds(unittest.TestCase):
    """A3: non-finite and absurd numbers are clean refusals, never a crash or a 4e13 s clip."""

    def test_inf_in_start_is_a_value_error(self):
        data = document(track(clipitem('v', 0, 100, definition=file_element()))).replace(b'<start>0</start>', b'<start>inf</start>')
        with self.assertRaisesRegex(ValueError, 'not a number'):
            xmeml.read(data)
        with self.assertRaises(ValueError):
            api.handle('import', {'filename': 'x.xml', 'text': data.decode()})

    def test_enormous_clips_are_skipped_in_every_importer(self):
        rate = Fraction(25)
        records = [{'key': 'k', 'name': 'a.mov', 'path': '', 'kind': 'video'}]
        clips = [{'kind': 'video', 'media': 'k', 'name': 'huge', 'start': 4e13, 'duration': 10, 'in': 0, 'track': 1},
                 {'kind': 'video', 'media': 'k', 'name': 'deep', 'start': 0, 'duration': 10, 'in': 4e13, 'track': 1},
                 {'kind': 'video', 'media': 'k', 'name': 'fine', 'start': 20, 'duration': 10, 'in': 0, 'track': 1}]
        result = project_module.build_project('T', rate, 1920, 1080, records, clips, [], [])
        self.assertEqual([c['start'] for c in result['clips']], [20])
        self.assertTrue(any('huge' in w and '24 hours' in w for w in result['compatibilityWarnings']))
        self.assertTrue(any('deep' in w for w in result['compatibilityWarnings']))
        self.assertLess(result['media'][0]['duration'], 100)


class ReverseAndLevels(Media):
    """A4 and the volume keyframes: exact source positions across a round trip."""

    def project(self, clip, **media_extra):
        p = synthetic_project(self.paths)
        p['clips'] = [clip]
        return p

    def test_reversed_mixed_rate_clip_round_trips_exactly(self):
        # a 29.97 source in a 23.976 sequence: the clipitem <duration> is a rounded count of sequence frames
        clip = {'id': 'r', 'mediaId': 'drop', 'type': 'video', 'track': 1, 'start': at(10), 'duration': at(37) - at(10),
                'in': float(Fraction(41) / DF), 'reverse': True, 'speed': .75, 'volume': 100}
        back = xmeml.read(self.export(self.project(clip)).encode())['clips'][0]
        self.assertTrue(back['reverse'])
        self.assertEqual(round(Fraction(repr(back['in'])) * DF), 41)

    def test_reversed_clip_is_mirrored_in_the_files_own_length(self):
        # 7200 s of 23.976 media is 172627.4 frames; at 2x the clipitem <duration> rounds to 86314
        # retimed frames (172628 source frames), so mirroring in it put the clip a frame off
        p = synthetic_project(self.paths)
        p['media'] = [{'id': 'long', 'name': 'long.mov', 'type': 'video', 'duration': 7200, 'rate': '24000/1001',
                       'timecode': '01:00:00:00', 'audioStreams': 0, 'width': 1920, 'height': 1080,
                       'path': 'long.mov', 'sourcePath': '/nowhere/long.mov'}]
        for speed in (2, 1, .5, .75):
            p['clips'] = [{'id': 'r', 'mediaId': 'long', 'type': 'video', 'track': 1, 'start': at(10), 'duration': at(34) - at(10),
                           'in': at(825), 'reverse': True, 'speed': speed, 'volume': 100}]
            with self.subTest(speed=speed):
                back = xmeml.read(self.export(p).encode())['clips'][0]
                self.assertEqual(round(Fraction(repr(back['in'])) * RATE), 825)

    def test_reversed_clip_with_a_dissolve_at_its_head(self):
        p = synthetic_project(self.paths)
        p['clips'] = [
            {'id': 'a', 'mediaId': 'camera_a', 'type': 'video', 'track': 1, 'start': 0, 'duration': at(48), 'in': at(10), 'volume': 100},
            {'id': 'b', 'mediaId': 'camera_b', 'type': 'video', 'track': 1, 'start': at(48), 'duration': at(96) - at(48),
             'in': at(30), 'reverse': True, 'volume': 100, 'transition': 'dissolve', 'transitionDuration': at(12),
             'transitionBefore': at(6)}]
        clips = sorted(xmeml.read(self.export(p).encode())['clips'], key=lambda c: c['start'])
        self.assertEqual([round(Fraction(repr(c['in'])) * RATE) for c in clips], [10, 30])
        self.assertEqual([round(Fraction(repr(c['start'])) * RATE) for c in clips], [0, 48])
        self.assertEqual(round(Fraction(repr(clips[1]['transitionBefore'])) * RATE), 6)

    def test_volume_keyframes_round_trip(self):
        p = synthetic_project(self.paths)
        tone = next(c for c in p['clips'] if c['id'] == 'a2-tone')
        p['clips'] = [dict(tone, fadeIn=0, fadeOut=0)]
        root = self.root(p)
        level = root.find(".//clipitem/filter/effect[effectid='audiolevels']/parameter")
        self.assertEqual([(k.findtext('when'), k.findtext('value')) for k in level.iterfind('keyframe')],
                         [('0', '0.7'), ('24', '0.7'), ('48', '0.35'), ('120', '0.35')])
        back = xmeml.read(self.export(p).encode())['clips'][0]
        keys = [(round(Fraction(repr(k['time'])) * RATE), k['value']) for k in back['keyframes']['volume']]
        self.assertEqual(keys, [(0, 70), (24, 70), (48, 35), (120, 35)])


class PathMapping(unittest.TestCase):
    """C: the most specific rule wins; names Windows cannot hold are reported."""

    def rules(self, *pairs):
        return media.path_rules([{'from': a, 'to': b} for a, b in pairs])

    def test_longest_prefix_wins_in_any_order(self):
        for order in ((('/mnt', 'Y:\\'), ('/mnt/media', 'X:\\M')), (('/mnt/media', 'X:\\M'), ('/mnt', 'Y:\\'))):
            path, _rule = media.map_path('/mnt/media/day1/a.mov', self.rules(*order))
            self.assertEqual(path, 'X:\\M\\day1\\a.mov')
        self.assertEqual(media.map_path('/mnt/other/a.mov', self.rules(('/mnt', 'Y:\\'), ('/mnt/media', 'X:\\M')))[0],
                         'Y:\\other\\a.mov')

    def test_whole_components_only(self):
        rules = self.rules(('/mnt/media', 'X:\\M'))
        self.assertEqual(media.map_path('/mnt/media2/a.mov', rules), ('/mnt/media2/a.mov', None))
        self.assertEqual(media.map_path('/mnt/media/a.mov', rules), ('X:\\M\\a.mov', 0))

    def test_windows_prefixes_ignore_case_and_slash(self):
        rules = self.rules(('X:\\Media', '/Volumes/RAID/Media'))
        self.assertEqual(media.map_path('x:/MEDIA/Day1/a.mov', rules), ('/Volumes/RAID/Media/Day1/a.mov', 0))
        self.assertEqual(media.map_path('/Volumes/MEDIA/a.mov', self.rules(('/Volumes/Media', 'X:\\M')))[1], None,
                         'POSIX paths compare case-sensitively')

    def test_names_windows_refuses_are_reported(self):
        rule = ('/mnt/media', 'X:\\M')
        self.assertIn(':', media.invalid_on_target('/mnt/media/take:1?.mov', rule))
        self.assertIsNone(media.invalid_on_target('/mnt/media/take 1.mov', rule))
        self.assertIsNone(media.invalid_on_target('/mnt/media/take:1.mov', ('/mnt/media', '/Volumes/M')))
        self.assertIsNone(media.invalid_on_target('/mnt:x/media/a.mov', ('/mnt:x/media', 'X:\\M')),
                          'only the part carried over is checked')


class PathMappingReport(Media):
    def test_report_explains_precedence_bad_names_and_relinking(self):
        p = synthetic_project(self.paths)
        folder = str(self.paths['camera_a'].parent)
        odd = self.paths['camera_a'].parent / 'take:1.mov'
        odd.write_bytes(self.paths['camera_a'].read_bytes())
        p['media'][0] = dict(p['media'][0], sourcePath=str(odd), name=odd.name)
        report = self.report(p, pathMap=[{'from': '/', 'to': 'Y:\\'}, {'from': folder, 'to': 'X:\\Turnover'}])
        self.assertIn('the longest "from" folder wins', report)
        self.assertIn('X:\\Turnover\\take:1.mov', report)
        self.assertIn('Windows does not allow', report)
        self.assertIn('will ask to locate media if the mapped paths do not exist there', report)


class LinkBudget(unittest.TestCase):
    """B1 and B2: what an import links is listed, and path checks share one deadline."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='splice-r2-link-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def project(self, count):
        items = []
        for index in range(count):
            path = self.root / f'clip{index}.mov'
            path.write_bytes(b'x')
            items.append({'id': f'm{index}', 'name': f'clip{index}', 'offline': True, 'sourcePath': str(path)})
        return {'media': items, 'compatibilityWarnings': []}

    def test_a_slow_path_costs_at_most_the_budget(self):
        p = self.project(3)
        with mock.patch('media_proxy.decodable', side_effect=lambda *a: time.sleep(5)):
            started = time.monotonic()
            with Budget(seconds=0.5) as budget:
                project_module.link_local_media(p, budget)
        self.assertLess(time.monotonic() - started, 2, 'the import does not wait for a hung check')
        self.assertTrue(all(item['offline'] for item in p['media']))
        self.assertTrue(any('3 sources could not be looked for' in w for w in p['compatibilityWarnings']))

    def test_cancel_stops_between_paths(self):
        p = self.project(4)
        budget = Budget(seconds=30, job='cancel-r2-test')
        calls = []

        def slow(*_args):
            calls.append(1)
            time.sleep(0.3)
            raise ValueError('not media')
        with mock.patch('media_proxy.decodable', side_effect=slow), budget:
            threading.Timer(0.1, budget.cancelled.set).start()
            with self.assertRaisesRegex(BudgetExceeded, 'cancelled'):
                project_module.link_local_media(p, budget)
        self.assertLessEqual(len(calls), 1)

    def test_linked_files_are_listed(self):
        p = self.project(2)
        results = [{'src': f'/media/linked/{i}/clip{i}.mov', 'sourcePath': item['sourcePath'], 'fingerprint': 'f'}
                   for i, item in enumerate(p['media'])]
        with mock.patch('media_proxy.decodable'), mock.patch('media_links.link_many', return_value=results):
            with Budget(seconds=10) as budget:
                project_module.link_local_media(p, budget)
        notice = p['compatibilityWarnings'][0]
        self.assertTrue(notice.startswith('Linked 2 files from this computer'))
        for item in p['media']:
            self.assertIn(item['sourcePath'], notice)

    def test_the_list_is_bounded(self):
        p = self.project(14)
        results = [{'src': f'/media/linked/{i}/x.mov', 'sourcePath': item['sourcePath'], 'fingerprint': 'f'}
                   for i, item in enumerate(p['media'])]
        with mock.patch('media_proxy.decodable'), mock.patch('media_links.link_many', return_value=results):
            with Budget(seconds=10) as budget:
                project_module.link_local_media(p, budget)
        notice = p['compatibilityWarnings'][0]
        shown = sum(1 for item in p['media'] if item['sourcePath'] in notice)
        self.assertLessEqual(shown, project_module.LINKED_LIST)
        self.assertIn(f'; and {14 - shown} more.', notice)
        self.assertLess(len(notice), 1200)

    def test_unused_sources_are_not_imported(self):
        records = [{'key': 'a', 'name': 'used.mov', 'path': str(self.root / 'used.mov'), 'kind': 'video'},
                   {'key': 'b', 'name': 'private.mov', 'path': str(self.root / 'private.mov'), 'kind': 'video'}]
        clips = [{'kind': 'video', 'media': 'a', 'name': 'used', 'start': 0, 'duration': 1, 'in': 0, 'track': 1}]
        result = project_module.build_project('T', Fraction(25), 1920, 1080, records, clips, [], [])
        self.assertEqual([m['name'] for m in result['media']], ['used.mov'])

    def test_paths_outside_splice_folders_are_not_resolved_while_building(self):
        with mock.patch('premiere._playback_url', side_effect=AssertionError('resolved')) as resolved:
            self.assertEqual(project_module._playable('/net/hung-mount/a.mov'), '')
        resolved.assert_not_called()


class BasicMotion(Media):
    """P1 and P2: Premiere's Scale 100 is native size and the centre is a fraction of the media's frame."""

    def one(self, width=None, height=None, sar=None, transform=None):
        p = synthetic_project(self.paths)
        item = dict(p['media'][1])  # camera_b
        for key, value in (('width', width), ('height', height), ('sampleAspectRatio', sar)):
            if value is not None:
                item[key] = value
        p['media'] = [item]
        p['clips'] = [{'id': 'c', 'mediaId': item['id'], 'type': 'video', 'track': 1, 'start': 0, 'duration': at(24),
                       'in': 0, 'volume': 100, 'embeddedAudio': False, **({'transform': transform} if transform else {})}]
        return p

    def motion(self, root):
        effect = root.find(".//clipitem/filter/effect[effectid='basic']")
        if effect is None:
            return None
        return (effect.findtext("parameter[parameterid='scale']/value"),
                effect.findtext("parameter[parameterid='center']/value/horiz"),
                effect.findtext("parameter[parameterid='center']/value/vert"))

    def test_media_smaller_than_the_sequence(self):
        p = self.one(960, 540, transform={'x': 10, 'y': -5, 'scale': 80, 'rotation': 0})
        self.assertEqual(self.motion(self.root(p)), ('160', '0.2', '-0.1'))
        back = xmeml.read(self.export(p).encode())['clips'][0]['transform']
        self.assertEqual((back['scale'], back['x'], back['y']), (80, 10, -5))

    def test_fitted_media_without_a_transform_still_fills_the_frame(self):
        self.assertEqual(self.motion(self.root(self.one(960, 540))), ('200', '0', '0'))
        self.assertEqual(self.motion(self.root(self.one(3840, 2160))), ('50', '0', '0'))
        self.assertIsNone(self.motion(self.root(self.one(1920, 1080))), 'nothing to say for media of the sequence size')
        self.assertIsNone(self.motion(self.root(self.one(1080, 1080))), 'a square picture already fills the height')
        self.assertEqual(self.motion(self.root(self.one(1920, 2160))), ('50', '0', '0'))

    def test_media_larger_than_the_sequence(self):
        p = self.one(3840, 2160, transform={'x': -25, 'y': 20, 'scale': 150, 'rotation': 0})
        # 25 % left = -480 px of 3840; 20 % down = 216 px of 2160
        self.assertEqual(self.motion(self.root(p)), ('75', '-0.125', '0.1'))
        back = xmeml.read(self.export(p).encode())['clips'][0]['transform']
        self.assertEqual((back['scale'], back['x'], back['y']), (150, -25, 20))

    def test_named_non_square_pixels(self):
        root = self.root(self.one(1440, 1080, '4:3'))
        chars = root.find('.//file/media/video/samplecharacteristics')
        self.assertEqual((chars.findtext('pixelaspectratio'), chars.findtext('anamorphic')), ('HD-(1440x1080)', 'FALSE'))
        self.assertIsNone(self.motion(root), '1440 x 4/3 = 1920: already fills the frame')
        root = self.root(self.one(720, 576, '16:11'))
        chars = root.find('.//file/media/video/samplecharacteristics')
        self.assertEqual((chars.findtext('pixelaspectratio'), chars.findtext('anamorphic')), ('PAL-601', 'TRUE'))
        self.assertEqual(self.motion(root)[0], '183.333333', '720 x 16/11 = 1047 px wide: 1920 / 1047')

    def test_unnamed_non_square_pixels_are_reported(self):
        p = self.one(1000, 1080, '9:5')
        chars = self.root(p).find('.//file/media/video/samplecharacteristics')
        self.assertEqual(chars.findtext('pixelaspectratio'), 'square')
        self.assertIn('pixel aspect ratio 9:5 has no FCP 7 XML name', self.report(p))

    def test_nearly_square_pixels_are_square(self):
        chars = self.root(self.one(960, 540, '2560:2559')).find('.//file/media/video/samplecharacteristics')
        self.assertEqual(chars.findtext('pixelaspectratio'), 'square')

    def reader(self, filters, width=960, height=540, extra=''):
        item = clipitem('v', 0, 100, filters=filters, definition=file_element(width=width, height=height, extra=extra))
        result = xmeml.read(document(track(item)))
        return result['clips'][0].get('transform'), result['compatibilityWarnings']

    def test_reader_inverts_the_rule(self):
        basic = ('<filter><effect><effectid>basic</effectid><parameter><parameterid>scale</parameterid><value>100</value>'
                 '</parameter><parameter><parameterid>center</parameterid><value><horiz>0.1</horiz><vert>-0.05</vert>'
                 '</value></parameter></effect></filter>')
        transform, _ = self.reader(basic, 640, 360)
        # Premiere: native size (a third of the frame), moved 64 px right (3.33 %) and 18 px up (1.67 %)
        self.assertEqual((transform['scale'], transform['x'], transform['y']), (33.3333, 3.3333, -1.6667))
        transform, _ = self.reader('')
        self.assertEqual(transform['scale'], 50, 'no Basic Motion: native size, half the frame')

    def test_premiere_pixel_aspect_compensation_is_not_a_user_scale(self):
        distort = ('<filter><effect><name>Distort</name><effectid>deformation</effectid><parameter><parameterid>aspect'
                   '</parameterid><value>-77.8058</value></parameter></effect></filter>'
                   '<filter><effect><effectid>basic</effectid><parameter><parameterid>scale</parameterid>'
                   '<value>355.6116</value></parameter></effect></filter>')
        transform, warnings = self.reader(distort)
        self.assertEqual(transform['scale'], 100, 'Scale 200 (fills the frame) with the 1.778 compensation folded out')
        self.assertFalse([w for w in warnings if 'Distort' in w])


class PremiereLimits(Media):
    """P3 and P4: what Premiere Pro does differently is in the report, clip by clip."""

    def test_reversed_linked_clip_is_reported(self):
        report = self.report(synthetic_project(self.paths))
        self.assertIn('Premiere Pro imports a reversed picture clip unlinked from its sound', report)

    def test_rounded_in_points_are_listed(self):
        report = self.report(synthetic_project(self.paths))
        # B001 (29.97) in a 23.976 sequence: source frame 6 is 4.8 sequence frames in
        line = next(l for l in report.splitlines() if 'B001_C001_df.mov' in l and 'whole-frame <in>' in l)
        self.assertIn('starts 0.25 source frames later', line)


def source_text(text, font='Helvetica'):
    """A "Source Text" value shaped like Premiere's: binary, strings as length + UTF-8, zero padded."""
    def string(value):
        data = value.encode()
        return len(data).to_bytes(4, 'little') + data + b'\x00' * 4
    blob = b'\x68\x01\x00\x00' + b'\x00' * 20 + string(font) + b'\x10\x00\x00\x00' * 3 + string(text) + b'\x00' * 40
    return base64.b64encode(blob).decode()


class PremiereGraphics(unittest.TestCase):
    """P5: a Premiere graphic comes back as a Splice title."""

    def graphic(self, value):
        effect = (f'<filter><effect><name>OPENING</name><effectid>GraphicAndType</effectid><parameter><name>Source Text'
                  f'</name><value>{value}</value></parameter></effect></filter>')
        definition = (f'<file id="g"><name>Graphic</name><mediaSource>GraphicAndType</mediaSource>{SEQUENCE}'
                      f'<media><video/></media></file>')
        item = clipitem('t', 10, 60, filters=effect, definition=definition)
        return xmeml.read(document(track(item, 'V4')))

    def test_text_is_read(self):
        result = self.graphic(source_text('Hello, Night 3'))
        self.assertEqual([(c['type'], c['title']) for c in result['clips']], [('title', 'Hello, Night 3')])
        self.assertEqual(result['media'], [])
        self.assertFalse([w for w in result['compatibilityWarnings'] if 'was not imported' in w and 'filter' in w])

    def test_unreadable_text_is_a_named_placeholder(self):
        result = self.graphic('not base64 at all!')
        self.assertEqual([(c['type'], c['title']) for c in result['clips']], [('title', 'OPENING')])
        self.assertTrue(any('could not be read' in w for w in result['compatibilityWarnings']))


if __name__ == '__main__':
    unittest.main()
