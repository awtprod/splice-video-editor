"""Timeline interchange hardening: malformed files answer as client errors (a fixed-seed fuzz of
all four importers), clamps on import are reported, repeated warnings carry a count, Windows
device names are caught by path mapping, an EDL import says where its timeline starts, and the
media-link step runs under its own time budget."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange import api, media, otio, project as project_module, xmlsafe  # noqa: E402
import interchange_fuzz  # noqa: E402

SEQUENCE = '<rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>'


def xmeml(clips, path='/nowhere/a.mov'):
    file = (f'<file id="f1"><name>a.mov</name><pathurl>file://localhost{path}</pathurl>{SEQUENCE}<duration>500</duration>'
            '<media><video><samplecharacteristics><width>1920</width><height>1080</height></samplecharacteristics>'
            '</video></media></file>')
    reference = '<file id="f1"/>'
    items = ''.join(f'<clipitem id="c{i}"><name>{name}</name><duration>500</duration>{SEQUENCE}<start>{start}</start>'
                    f'<end>{end}</end><in>0</in><out>{end - start}</out>{file if i == 0 else reference}{extra}'
                    '</clipitem>' for i, (name, start, end, extra) in enumerate(clips))
    return (f'<?xml version="1.0"?><xmeml version="5"><sequence id="s"><name>T</name><duration>500</duration>{SEQUENCE}'
            f'<media><video><format><samplecharacteristics><width>1920</width><height>1080</height>'
            f'</samplecharacteristics></format><track>{items}</track></video></media></sequence></xmeml>')


def motion(scale, x=0.0, y=0.0):
    return ('<filter><effect><name>Basic Motion</name><effectid>basic</effectid>'
            f'<parameter><parameterid>scale</parameterid><value>{scale}</value></parameter>'
            f'<parameter><parameterid>center</parameterid><value><horiz>{x}</horiz><vert>{y}</vert></value></parameter>'
            '</effect></filter>')


def otio_document(rate=25, value=0, extra_clip=None):
    rt = lambda v, r=rate: {'OTIO_SCHEMA': 'RationalTime.1', 'rate': r, 'value': v}
    clip = {'OTIO_SCHEMA': 'Clip.2', 'name': 'a', 'source_range': {'OTIO_SCHEMA': 'TimeRange.1', 'start_time': rt(value),
                                                                   'duration': rt(50)},
            'media_references': {'DEFAULT_MEDIA': {'OTIO_SCHEMA': 'ExternalReference.1', 'target_url': 'file:///nowhere/a.mov'}},
            'active_media_reference_key': 'DEFAULT_MEDIA', **(extra_clip or {})}
    import json
    return json.dumps({'OTIO_SCHEMA': 'Timeline.1', 'name': 'T', 'tracks': {'OTIO_SCHEMA': 'Stack.1', 'children': [
        {'OTIO_SCHEMA': 'Track.1', 'kind': 'Video', 'name': 'V1', 'children': [clip]}]}})


class MalformedFiles(unittest.TestCase):
    """Every malformed file is a client error (ValueError), never a server error."""

    def test_an_unknown_xml_encoding_is_a_client_error(self):
        for encoding in ('nan', 'x', 'ebcdic', 'zlib', 'rot13'):
            for name, root in (('a.fcpxml', '<fcpxml version="1.10"/>'), ('a.xml', '<xmeml version="5"/>')):
                with self.subTest(encoding=encoding, name=name):
                    with self.assertRaisesRegex(ValueError, 'character encoding'):
                        api.import_timeline(name, f'<?xml version="1.0" encoding="{encoding}"?>{root}')
        with self.assertRaisesRegex(ValueError, 'character encoding'):
            xmlsafe.parse(b'<?xml version="1.0" encoding="x"?><a/>')
        self.assertEqual(xmlsafe.parse(b'<?xml version="1.0" encoding="latin-1"?><a>\xe9</a>').text, '\xe9')

    def test_an_otio_rate_too_small_to_be_a_frame_rate_is_a_client_error(self):
        for rate in (1e-300, 5e-324, 1e-5):
            with self.subTest(rate=rate), self.assertRaisesRegex(ValueError, 'no clips|out of range'):
                api.import_timeline('a.otio', otio_document(rate=rate))
        with self.assertRaisesRegex(ValueError, 'out of range'):
            otio._rt({'OTIO_SCHEMA': 'RationalTime.1', 'rate': 1e-300, 'value': 1}, 'start')
        self.assertAlmostEqual(api.import_timeline('a.otio', otio_document(rate=48000))['clips'][0]['duration'], 50 / 48000, 6)

    def test_otio_values_of_the_wrong_type_are_read_as_missing(self):
        for extra in ({'metadata': 1.5}, {'metadata': {'splice': []}}, {'media_references': [1]},
                      {'active_media_reference_key': ['x']}, {'effects': 7},
                      {'effects': [{'OTIO_SCHEMA': 'LinearTimeWarp.1', 'time_scalar': {}}]}):
            with self.subTest(extra=extra):
                try:
                    api.import_timeline('a.otio', otio_document(extra_clip=extra))
                except ValueError:
                    pass  # a clean refusal is fine; an AttributeError or TypeError is not

    def test_fixed_seed_fuzz_of_every_importer(self):
        """A small, deterministic sample of tests/interchange_fuzz.py (run it with 3000 for the long version)."""
        failures, slow, counts = interchange_fuzz.run(interchange_fuzz.seeds_from_export(), 120, seed=20261126, slow=10)
        self.assertEqual([(kind, repr(error)) for kind, error, _ in failures], [])
        self.assertEqual(slow, [])
        self.assertEqual(sum(counts.values()), 480)
        for kind in interchange_fuzz.FILENAMES:  # the mutants reach the readers, not only the parsers
            self.assertGreater(sum(n for (k, outcome), n in counts.items() if k == kind and outcome == 'ok'), 0, kind)


class ImportWarnings(unittest.TestCase):
    def test_a_scale_outside_splices_range_is_reported_with_its_value(self):
        project = api.import_timeline('a.xml', xmeml([('tiny', 0, 50, motion(3.3)), ('wide', 50, 100, motion(100, x=2.5))]))
        warnings = '\n'.join(project['compatibilityWarnings'])
        self.assertIn('tiny: scale % 3.3 is outside Splice\'s 10 to 400 and was set to 10.', warnings)
        self.assertRegex(warnings, r"wide: horizontal position % \d+ is outside Splice's -100 to 100 and was set to 100\.")
        scales = [c['transform']['scale'] for c in project['clips']]
        self.assertEqual(scales, [10, 100])

    def test_keyframes_outside_the_engine_range_are_reported_and_limited(self):
        warnings = []
        out = project_module._clean_effects({'name': 'K', 'keyframes': {'scale': [
            {'time': 0, 'value': 2}, {'time': 1, 'value': 100}, {'time': 2, 'value': 900}]}}, warnings)
        self.assertEqual([p['value'] for p in out['keyframes']['scale']], [10, 100, 400])
        self.assertEqual(warnings, ["K: 2 scale % keyframes outside Splice's 10 to 400 (from 2 to 900) were limited."])

    def test_repeated_skips_carry_a_count_and_the_limit_that_was_hit(self):
        day = 25 * 3600 * 25
        clips = [('long', day + i * 100, day + i * 100 + 50, '') for i in range(3)] + [('ok', 0, 50, '')]
        project = api.import_timeline('a.xml', xmeml(clips))
        skipped = [w for w in project['compatibilityWarnings'] if w.startswith('long ')]
        self.assertEqual(skipped, ['long ends more than 24 hours into the timeline (Splice\'s limit; no real timeline '
                                   'is that long) and was skipped. (3 times)'])
        self.assertEqual(project_module.counted(['a', 'b', 'a']), ['a (2 times)', 'b'])

    def test_an_edl_import_says_where_the_timeline_starts(self):
        edl = ('TITLE: T\nFCM: NON-DROP FRAME\n\n'
               '001  AX       V     C        00:00:10:00 00:00:12:00 10:20:30:00 10:20:32:00\n'
               '* FROM CLIP NAME: a.mov\n')
        project = api.import_timeline('a.edl', edl, {'fps': '25'})
        notice = next(w for w in project['compatibilityWarnings'] if 'sequence start' in w)
        self.assertIn('10:00:00:00', notice)
        self.assertIn('10:20:30:00', notice)
        self.assertEqual(project['startTimecode'], 10 * 3600 * 25)
        given = api.import_timeline('a.edl', edl, {'fps': '25', 'startTimecode': '10:20:00:00'})
        self.assertFalse(any('sequence start' in w for w in given['compatibilityWarnings']))


class WindowsNames(unittest.TestCase):
    RULE = ('/mnt/shoot', 'X:\\Shoot')

    def problem(self, name):
        return media.invalid_on_target('/mnt/shoot/day1/' + name, self.RULE)

    def test_device_names_trailing_dots_and_spaces_are_caught(self):
        for name in ('CON', 'con.mov', 'PRN.wav', 'aux', 'NUL.tar.gz', 'COM1.mov', 'com9', 'LPT1', 'lpt9.mxf', 'COM\u00b9.mov'):
            with self.subTest(name=name):
                self.assertIn('reserves for a device', self.problem(name))
        self.assertIn('ends in a dot', self.problem('take.'))
        self.assertIn('ends in a space', self.problem('take '))
        self.assertIn('ends in a dot', media.invalid_on_target('/mnt/shoot/day1./a.mov', self.RULE))
        for name in ('console.mov', 'COM0.mov', 'COM10.mov', 'LPT.mov', 'nul_take.mov', 'a.con', 'take.mov'):
            with self.subTest(name=name):
                self.assertIsNone(self.problem(name))
        self.assertIsNone(media.invalid_on_target('/mnt/shoot/CON.mov', ('/mnt/shoot', '/Volumes/Shoot')))


class TestedInReport(unittest.TestCase):
    def test_each_format_section_states_what_real_applications_did(self):
        from interchange_fixtures import synthetic_project
        paths = {name: Path('/fuzz/card') / f'{name}.mov' for name in ('camera_a', 'camera_b', 'drop', 'plain', 'tone')}
        report = api.export_timeline(synthetic_project(paths), {'formats': list(api.FORMATS),
                                                                'unknownTimecode': 'zero'})['report']
        sections = {}
        for block in report.split('\n\n'):
            heading = block.split('\n', 1)[0]
            sections[heading] = block
        expected = {
            'Final Cut Pro 7 XML (Premiere Pro, Resolve, Avid):': [
                "Adobe Premiere Pro 27 imported Splice's Final Cut 7 XML for three test timelines with zero cut differences.",
                "A reversed linked pair arrives unlinked in Premiere (Premiere's own export does the same).",
                'Premiere shows its own Link Media dialog when mapped paths do not exist.'],
            'FCPXML 1.10 (DaVinci Resolve, Final Cut Pro):': ['DaVinci Resolve 21.1 imported the FCPXML frame-exact.'],
            'CMX3600 EDL:': ['DaVinci Resolve 21.1 conforms EDLs by timecode only and refuses audio-only EDLs.'],
            'OpenTimelineIO:': ['DaVinci Resolve 21.1 cancelled an OTIO import in the one attempt made (unverified since '
                                'the OTIO writer was changed to match OpenTimelineIO 0.18).'],
        }
        for heading, facts in expected.items():
            section = sections[heading]
            self.assertIn('  Tested in other applications:\n', section, heading)
            for fact in facts:
                self.assertIn('    ' + fact + '\n', section + '\n', heading)
        only_fcpxml = api.export_timeline(synthetic_project(paths), {'unknownTimecode': 'zero'})['report']
        self.assertNotIn('Premiere Pro 27 imported', only_fcpxml, 'facts appear only for the formats exported')


class LinkBudget(unittest.TestCase):
    """The link step of an import runs under its own LINK_SECONDS budget, separate from parsing."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='splice-link-budget-')
        self.addCleanup(temp.cleanup)
        self.clip = Path(temp.name) / 'a.mov'
        self.clip.write_bytes(b'x')

    def test_the_link_step_gets_its_own_named_budget_and_the_job_id(self):
        seen = []
        with mock.patch.object(api, 'link_local_media', side_effect=lambda project, budget: seen.append(
                (budget.seconds, budget.what, budget.job, budget.remaining()))):
            api.import_timeline('a.xml', xmeml([('a', 0, 50, '')], str(self.clip)),
                                {'linkMedia': True, 'jobId': 'link-budget-test'})
        (seconds, what, job, remaining), = seen
        self.assertEqual(seconds, api.LINK_SECONDS)
        self.assertGreater(remaining, api.LINK_SECONDS - 5, 'a fresh budget, not what parsing left over')
        self.assertEqual(what, "Linking the timeline's media")
        self.assertEqual(job, 'link-budget-test')

    def test_a_slow_media_check_stops_at_the_link_budget(self):
        with mock.patch.object(api, 'LINK_SECONDS', 0.4), \
                mock.patch('media_proxy.decodable', side_effect=lambda *a: time.sleep(5)):
            started = time.monotonic()
            project = api.import_timeline('a.xml', xmeml([('a', 0, 50, '')], str(self.clip)), {'linkMedia': True})
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(any("within the import's 0.4-second limit" in w for w in project['compatibilityWarnings']))
        self.assertTrue(project['media'][0]['offline'])
        with mock.patch.object(api, 'link_local_media') as linker:
            api.import_timeline('a.xml', xmeml([('a', 0, 50, '')], str(self.clip)), {})
        linker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
