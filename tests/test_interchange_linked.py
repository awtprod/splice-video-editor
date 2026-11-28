"""Timeline interchange with the link-in-place media registry, through the running server.

A project mixes a linked camera original with start timecode 01:00:00:00, a managed upload and a
linked ProRes original that plays through an edit proxy. Every format must name the original
file (never Splice's ``/media/...`` URL, never a proxy), managed copies by their real path with a
note in the report, and an optional path mapping must rewrite every path. Importing a timeline
links the media it names that exists here (no copy, media only) and leaves the rest offline.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange.media import file_url  # noqa: E402
from interchange.xmeml import pathurl  # noqa: E402
from test_media_server import ServerProcess  # noqa: E402

FORMATS = ['fcpxml', 'xmeml', 'edl', 'otio']


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *map(str, arguments)], check=True, timeout=60)


def make_camera(path, timecode='01:00:00:00', codec=('-c:v', 'mpeg4', '-q:v', '10'), seconds=2):
    ffmpeg('-f', 'lavfi', '-i', f'testsrc=size=64x36:rate=24000/1001:d={seconds}',
           '-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:d={seconds}', '-ac', '2', *codec,
           '-c:a', 'pcm_s16le', *(('-timecode', timecode) if timecode else ()), path)


class LinkedMediaInterchange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-ic-linked-')
        base = Path(cls.temp.name)
        cls.data = base / 'data'
        cls.card = (base / 'Shoot Day 1' / 'CARD_A')
        cls.card.mkdir(parents=True)
        cls.card = cls.card.resolve()
        cls.camera = cls.card / 'A001_C001.mov'
        make_camera(cls.camera)
        cls.prores = cls.card / 'A001_C002_prores.mov'
        make_camera(cls.prores, '02:00:00:00', ('-c:v', 'prores_ks', '-profile:v', '0'))
        cls.upload_source = base / 'phone clip.mp4'
        make_camera(cls.upload_source, None, ('-c:v', 'mpeg4', '-q:v', '10'))
        cls.server = ServerProcess(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.temp.cleanup()

    # -- helpers ------------------------------------------------------------------------------

    def post(self, route, payload, expect=200):
        status, body = self.server.json('POST', route, payload)
        self.assertEqual(status, expect, body)
        return body

    def info(self, src):
        status, body = self.server.json('GET', '/api/media/info?src=' + quote(src, safe=''))
        self.assertEqual(status, 200, body)
        return body

    def item(self, identity, original, src=None, **extra):
        """A media item as media-store.js importFile stores it: describe() spread, then src."""
        info = self.info(original)
        return {**info, 'id': identity, 'src': src or original, 'originalSrc': original, **extra}

    def project(self):
        linked = self.post('/api/media/link', {'paths': [str(self.camera), str(self.prores)]})['files']
        self.assertTrue(all('src' in f for f in linked), linked)
        camera_src, prores_src = linked[0]['src'], linked[1]['src']
        status, _, body = self.server.request('POST', '/api/media/upload?name=' + quote(self.upload_source.name),
                                              self.upload_source.read_bytes(),
                                              headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(status, 200, body)
        upload = json.loads(body)
        proxy = self.post('/api/media/proxy', {'src': prores_src})['src']
        self.assertRegex(proxy, r'^/media/[a-f0-9]{32}/edit\.mp4$')
        media = [
            self.item('cam', camera_src, name=self.camera.name, path=str(self.camera), linked=True, type='video'),
            self.item('upl', upload['src'], name=self.upload_source.name, path=self.upload_source.name, linked=False),
            self.item('pro', prores_src, src=proxy, name=self.prores.name, path=str(self.prores), linked=True),
        ]
        clip = lambda identity, media_id, start: {
            'id': identity, 'mediaId': media_id, 'type': 'video', 'track': 1, 'start': start, 'duration': 0.5, 'in': 0.25,
            'title': '', 'color': {'brightness': 100, 'contrast': 100, 'saturation': 100}, 'volume': 100, 'transition': 'none'}
        return {'name': 'Linked turnover', 'fps': 24000 / 1001, 'width': 64, 'height': 36, 'media': media,
                'clips': [clip('c1', 'cam', 0), clip('c2', 'upl', 0.5), clip('c3', 'pro', 1.0)],
                'markers': [], 'captions': []}, upload

    def export(self, project, **options):
        return self.post('/api/interchange/export', {'project': project, 'options': {
            'formats': FORMATS, 'unknownTimecode': 'zero', **options}})

    @staticmethod
    def files(result):
        found = {}
        for entry in result['files']:
            found.setdefault(entry['name'].rsplit('.', 1)[1], []).append(entry['content'])
        return found

    # -- export -------------------------------------------------------------------------------

    def test_metadata_fields_match_describe(self):
        info = self.info(self.post('/api/media/link', {'paths': [str(self.camera)]})['files'][0]['src'])
        self.assertEqual(info['sourcePath'], str(self.camera))
        self.assertEqual(info['rate'], '24000/1001')
        self.assertEqual(info['timecode'], '01:00:00:00')
        self.assertIs(info['dropFrame'], False)
        self.assertIs(info['vfr'], False)
        self.assertEqual(info['audioStreams'], 1)
        self.assertEqual(info['audioStreamDetails'][0]['channels'], 2)
        self.assertIn('reel', info)

    def test_every_format_names_the_original_never_a_url_or_proxy(self):
        project, upload = self.project()
        managed = Path(upload['path']).resolve()
        self.assertTrue(managed.is_relative_to(self.data.resolve()))
        result = self.export(project, package=True)
        files = self.files(result)
        originals = {'cam': str(self.camera), 'upl': str(managed), 'pro': str(self.prores)}
        everything = '\n'.join(text for texts in files.values() for text in texts)
        self.assertNotIn('/media/linked/', everything)
        self.assertNotRegex(everything, r'(?<![\w%])/media/[a-f0-9]{32}/')   # only inside the managed copy's real path
        self.assertNotRegex(everything, r'(edit|seek|grade)\.mp4')
        # FCPXML: file URLs on media-rep; the camera asset starts at its own 01:00:00:00
        root = ET.fromstring(files['fcpxml'][0])
        reps = {rep.get('src') for rep in root.iter('media-rep')}
        self.assertEqual(reps, {file_url(p) for p in originals.values()})
        camera_asset = next(a for a in root.iter('asset') if a.find('media-rep').get('src') == file_url(str(self.camera)))
        self.assertEqual(camera_asset.get('start'), '86486400/24000s')     # 86400 frames at 23.976
        # FCP 7 XML: pathurl
        self.assertEqual(set(re.findall(r'<pathurl>([^<]+)</pathurl>', files['xml'][0])),
                         {pathurl(p) for p in originals.values()})
        # EDL: SOURCE FILE comments and the camera's source timecode
        edl = files['edl'][0]
        self.assertEqual(set(re.findall(r'^\* SOURCE FILE: (.+)$', edl, re.M)), set(originals.values()))
        event = next(line for line in edl.splitlines() if re.match(r'^\d{3} ', line))
        self.assertIn(' 01:00:00:06 ', event)           # 0.25 s in at 23.976 = 6 frames past 01:00:00:00
        # OTIO: target_url
        urls = set(re.findall(r'"target_url": "([^"]+)"', files['otio'][0]))
        self.assertEqual(urls, {file_url(p) for p in originals.values()})
        # the report says what each reference is
        report = result['report']
        self.assertIn(f'A001_C001.mov: linked original {self.camera}', report)
        self.assertIn(f'A001_C002_prores.mov: linked original {self.prores}', report)
        self.assertIn(f'phone clip.mp4: managed copy in Splice\'s media folder', report)
        self.assertIn(f'the receiving machine needs this file, so copy it with the turnover {managed}', report)

    def test_present_metadata_is_not_probed_again(self):
        """The stored describe() fields are enough: an in-process export with ffprobe disabled
        writes the same timeline files as the server."""
        import interchange.media as media
        import media_links
        import premiere
        project, _ = self.project()
        served = self.files(self.export(project))
        saved = (media.probe_file, media_links.MEDIA_ROOT, media_links.REGISTRY, premiere.MEDIA_ROOT)
        calls = []

        def refuse(path, timeout=0):
            calls.append(path)
            raise AssertionError(f'probed {path}')
        try:
            import media_proxy  # noqa: F401  (installs the link-aware resolver)
            media.probe_file = refuse
            media_links.MEDIA_ROOT = premiere.MEDIA_ROOT = (self.data / 'media').resolve()
            media_links.REGISTRY = self.data.resolve() / 'linked-media.json'
            from interchange.api import export_timeline
            local = self.files(export_timeline(project, {'formats': FORMATS, 'unknownTimecode': 'zero'}))
            self.assertEqual(calls, [])
            for kind in ('fcpxml', 'xml', 'edl', 'otio'):
                self.assertEqual(local[kind], served[kind], kind)
            # control: drop a field and the probe runs
            del project['media'][0]['timecode']
            with self.assertRaises(AssertionError):
                export_timeline(project, {'formats': ['edl'], 'unknownTimecode': 'zero'})
            self.assertEqual(len(calls), 1)
        finally:
            media.probe_file, media_links.MEDIA_ROOT, media_links.REGISTRY, premiere.MEDIA_ROOT = saved

    def test_sound_files_keep_their_broadcast_wav_start_and_sample_rate(self):
        """A WAV with stored BWF metadata starts at 01:00:00:00 at 23.976."""
        wav = self.card / 'room_tone_bwf.wav'
        ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=22050:d=2', '-c:a', 'pcm_s16le', '-write_bext', '1',
               '-metadata', 'time_reference=79459380', wav)       # 3603.6 s at 22050 Hz
        src = self.post('/api/media/link', {'paths': [str(wav)]})['files'][0]['src']
        item = self.item('wav', src, name=wav.name, path=str(wav), linked=True)
        self.assertEqual((item['type'], item['timecode']), ('audio', None))
        project = {'name': 'Sound', 'fps': 24000 / 1001, 'width': 64, 'height': 36, 'media': [item], 'captions': [],
                   'markers': [], 'clips': [{'id': 'a', 'mediaId': 'wav', 'type': 'audio', 'track': -1, 'start': 0,
                                             'duration': .5, 'in': 0, 'transition': 'none'}]}
        files = self.files(self.export(project, audioEdl=True))
        asset = re.search(r'<asset [^>]*name="room_tone_bwf.wav"[^>]*>', files['fcpxml'][0]).group(0)
        self.assertIn('audioRate="22050"', asset)
        self.assertIn('start="86486400/24000s"', asset)
        self.assertRegex('\n'.join(files['edl']), r'\n001 +\S+ +A +C +01:00:00:00 01:00:00:12 01:00:00:00 ')

    def test_offline_linked_original_still_names_its_recorded_path(self):
        moved = self.card / 'offline_A002.mov'
        make_camera(moved)
        src = self.post('/api/media/link', {'paths': [str(moved)]})['files'][0]['src']
        item = self.item('off', src, name=moved.name, path=str(moved), linked=True)
        moved.rename(moved.with_suffix('.moved'))
        try:
            project = {'name': 'Offline', 'fps': 24000 / 1001, 'width': 64, 'height': 36, 'media': [item], 'captions': [],
                       'markers': [], 'clips': [{'id': 'c', 'mediaId': 'off', 'type': 'video', 'track': 1, 'start': 0,
                                                 'duration': .5, 'in': 0, 'transition': 'none'}]}
            result = self.export(project)
            self.assertIn(f'* SOURCE FILE: {moved}', self.files(result)['edl'][0])
            self.assertIn('the linked original is offline on this machine', result['report'])
        finally:
            moved.with_suffix('.moved').rename(moved)

    def test_a_proxy_alone_is_never_turned_over(self):
        project, _ = self.project()
        proxy = project['media'][2]['src']
        item = {**self.info(proxy), 'id': 'px', 'src': proxy, 'name': 'proxy only.mov'}   # describe() of the proxy itself
        self.assertIn('/edit.mp4', item['sourcePath'])
        project['media'].append(item)
        project['clips'].append({'id': 'c4', 'mediaId': 'px', 'type': 'video', 'track': 1, 'start': 1.5, 'duration': .5,
                                 'in': 0, 'transition': 'none'})
        result = self.export(project)
        everything = '\n'.join(text for texts in self.files(result).values() for text in texts)
        self.assertNotIn('edit.mp4', everything)
        self.assertIn('only an edit proxy is on record; proxies are never turned over', result['report'])
        self.assertIn('proxy only.mov: no file referenced', result['report'])

    def test_path_mapping_rewrites_every_format_and_is_reported(self):
        project, upload = self.project()
        managed = Path(upload['path']).resolve()
        result = self.export(project, pathMap=[{'from': str(self.card.parent), 'to': 'X:\\Shoot'}])
        files = self.files(result)
        camera_win = 'X:\\Shoot\\CARD_A\\A001_C001.mov'
        self.assertIn('src="file:///X:/Shoot/CARD_A/A001_C001.mov"', files['fcpxml'][0])
        self.assertIn('<pathurl>file://localhost/X%3a/Shoot/CARD_A/A001_C001.mov</pathurl>', files['xml'][0])
        self.assertIn(f'* SOURCE FILE: {camera_win}', files['edl'][0])
        self.assertIn('"target_url": "file:///X:/Shoot/CARD_A/A001_C002_prores.mov"', files['otio'][0])
        everything = '\n'.join(text for texts in files.values() for text in texts)
        self.assertNotIn(str(self.card), everything)
        self.assertIn(file_url(str(managed)), files['fcpxml'][0])      # not under the rule: unchanged
        report = result['report']
        self.assertIn(f'1. {self.card.parent} -> X:\\Shoot (2 sources)', report)
        self.assertIn(f'linked original {self.camera}, written as {camera_win}', report)
        self.assertIn('Not under any mapping, written unchanged: phone clip.mp4', report)
        # to a POSIX mount, and bad rules are refused
        posix = self.files(self.export(project, formats=['fcpxml'],
                                       pathMap=[{'from': str(self.card), 'to': '/Volumes/RAID/Day 1'}]))
        self.assertIn('src="file:///Volumes/RAID/Day%201/A001_C001.mov"', posix['fcpxml'][0])
        for rule in ([{'from': 'relative/dir', 'to': 'X:\\'}], [{'from': '/a'}], 'x', [{'from': '/a', 'to': 'X:\\\x01'}]):
            status, body = self.server.json('POST', '/api/interchange/export', {'project': project, 'options': {
                'formats': ['edl'], 'pathMap': rule}})
            self.assertEqual(status, 400, (rule, body))

    # -- import -------------------------------------------------------------------------------

    def registry_paths(self):
        registry = self.data / 'linked-media.json'
        if not registry.exists():
            return set()
        return {entry['path'] for entry in json.loads(registry.read_text())['links'].values()}

    def managed_files(self):
        root = self.data / 'media'
        return [p for p in root.rglob('*') if p.is_file()] if root.exists() else []

    def test_imports_link_local_media_in_place_and_refuse_non_media(self):
        fresh = self.card / 'B001_C001.mov'
        make_camera(fresh, '03:00:00:00')
        notes = self.card / 'notes.mov'
        notes.write_text('Scene 4 take 2: the good one.\n' * 20)
        key = self.card / 'id_ed25519'
        key.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\n' + 'b3BlbnNzaC1rZXktdjEAAAAA' * 8 + '\n-----END OPENSSH PRIVATE KEY-----\n')
        missing = self.card / 'C001_missing.mov'
        sources = {'fresh': fresh, 'notes': notes, 'key': key, 'missing': missing}
        media = [{'id': name, 'name': path.name, 'src': '', 'type': 'video', 'duration': 2, 'path': str(path),
                  'sourcePath': str(path), 'rate': '24000/1001', 'timecode': None, 'audioStreams': 0,
                  'audioStreamDetails': []} for name, path in sources.items()]
        clips = [{'id': f'c{i}', 'mediaId': name, 'type': 'video', 'track': 1, 'start': i * .5, 'duration': .5,
                  'in': 0, 'transition': 'none'} for i, name in enumerate(sources)]
        project = {'name': 'Import check', 'fps': 24000 / 1001, 'width': 64, 'height': 36, 'media': media, 'clips': clips,
                   'markers': [], 'captions': []}
        files = self.files(self.export(project, formats=['fcpxml', 'xmeml', 'edl']))
        timelines = {'x.fcpxml': files['fcpxml'][0], 'x.xml': files['xml'][0], 'x.edl': files['edl'][0]}
        size = fresh.stat().st_size
        for filename, text in timelines.items():
            with self.subTest(format=filename):
                before = len(self.managed_files())
                imported = self.post('/api/interchange/import', {'filename': filename, 'text': text,
                                                                 'options': {'fps': '24000/1001'}})['project']
                by_path = {m.get('sourcePath'): m for m in imported['media']}
                linked = by_path[str(fresh)]
                self.assertIs(linked['linked'], True)
                self.assertIs(linked['offline'], False)
                self.assertRegex(linked['src'], r'^/media/linked/[a-f0-9]{32}/B001_C001\.mov$')
                self.assertEqual(linked['originalSrc'], linked['src'])
                status, headers, body = self.server.request('GET', linked['src'], token=False)
                self.assertEqual((status, len(body)), (200, size), 'the linked original is served by its URL')
                self.assertEqual(len(self.managed_files()), before, 'no copy is made')
                self.assertFalse([p for p in self.managed_files() if p.stat().st_size == size])
                for name in ('notes', 'key', 'missing'):
                    item = by_path[str(sources[name])]
                    self.assertIs(item['offline'], True, name)
                    self.assertEqual(item['src'], '', name)
                    self.assertNotIn('linked', item)
                warnings = '\n'.join(imported['compatibilityWarnings'])
                self.assertIn('notes.mov at its recorded path was not linked', warnings)
                self.assertIn('id_ed25519 at its recorded path was not linked', warnings)
                self.assertNotIn('C001_missing.mov at its recorded path', warnings)   # simply offline: relink
        paths = self.registry_paths()
        self.assertIn(str(fresh), paths)
        self.assertFalse({str(notes), str(key), str(missing)} & paths, 'non-media files are never registered')
        status, cache = self.server.json('GET', '/api/media/cache')
        self.assertEqual(status, 200)
        self.assertFalse({str(notes), str(key)} & {link['sourcePath'] for link in cache['links']})


if __name__ == '__main__':
    unittest.main()
