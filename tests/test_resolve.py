"""DaVinci Resolve import: .drp files and disk-library projects, built synthetically here."""
import hashlib
import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import resolve  # noqa: E402
import resolve_fixtures as F  # noqa: E402
from resolve import blobs, importer, source, zstd  # noqa: E402
from resolve.media import link_local  # noqa: E402

FRAMES = json.loads((ROOT / 'tests/fixtures/resolve/zstd-frames.json').read_text())


def by_title(project, item_id):
    return next(c for c in project['clips'] if c['resolveItemId'] == item_id)


def tree_state(folder):
    """Every path under ``folder`` with its mode and content hash."""
    state = {}
    for path in sorted(Path(folder).rglob('*')):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[str(path.relative_to(folder))] = (stat.S_IFMT(info.st_mode), info.st_size, digest)
    return state


class Scratch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.spec = F.sample()

    def tearDown(self):
        for path in self.dir.rglob('*'):
            if path.is_dir():
                path.chmod(0o700)
        self.temp.cleanup()

    def library(self, spec=None):
        return F.write_library(spec or self.spec, self.dir / 'library')


class DrpImportTests(Scratch):
    def setUp(self):
        super().setUp()
        self.project = resolve.open_drp(F.drp_bytes(self.spec))

    def test_timeline_settings(self):
        p = self.project
        self.assertEqual((p['name'], p['width'], p['height'], p['fps']), ('Harbor cut', 1280, 720, 24))
        self.assertEqual((p['frameRate'], p['startFrame'], p['startTimecode']), ('24/1', 86400, '01:00:00:00'))
        self.assertEqual(p['resolveTimelines'], ['Harbor cut', 'Harbor alt'])
        other = resolve.open_drp(F.drp_bytes(self.spec), 1)
        self.assertEqual((other['name'], other['frameRate'], other['fps']), ('Harbor alt', '30000/1001', 30000 / 1001))
        self.assertEqual((other['width'], other['height'], other['startTimecode']), (1920, 1080, '00:00:00:00'))

    def test_tracks_and_clips(self):
        p = self.project
        self.assertEqual(p['trackCounts'], {'video': 2, 'audio': 1})
        self.assertEqual(p['trackNames'], {'video:1': 'Picture', 'video:2': 'Video 2', 'audio:-1': 'Dialogue'})
        cut = sorted((c['type'], c['track'], c['start'], c['duration'], c['in'], c['title']) for c in p['clips'])
        self.assertEqual(cut, [('audio', -1, 0.0, 1.0, 1.0, 'A001.mp4'), ('audio', -1, 1.0, 1.0, 0.0, 'A002.wav'),
                               ('video', 1, 0.0, 1.0, 1.0, 'A001.mp4'), ('video', 1, 1.0, 1.0, 3.0, 'A001.mp4'),
                               ('video', 2, 0.0, 0.25, 0.0, 'Solid Color')])
        self.assertEqual(by_title(p, 'i-v2')['resolveFrames'], [24, 24, 72])

    def test_links_speed_reverse_and_transitions(self):
        p = self.project
        picture, sound = by_title(p, 'i-v1'), by_title(p, 'i-a1')
        self.assertTrue(picture['linkGroup'])
        self.assertEqual(picture['linkGroup'], sound['linkGroup'])
        self.assertNotIn('linkGroup', by_title(p, 'i-a2'))
        fast = by_title(p, 'i-v2')
        self.assertEqual((fast['speed'], fast['reverse']), (2.0, False))
        self.assertEqual((fast['transition'], fast['transitionDuration'], fast['transitionBefore']), ('dissolve', 0.5, 0.25))
        backwards = resolve.open_drp(F.drp_bytes(self.spec), 1)['clips'][0]
        self.assertEqual((backwards['speed'], backwards['reverse']), (1.0, True))

    def test_transform_opacity_and_markers(self):
        picture = by_title(self.project, 'i-v1')
        self.assertEqual(picture['transform'], {'x': 10.0, 'y': 5.0, 'scale': 125.0, 'rotation': 0})
        self.assertEqual(picture['opacity'], 80.0)
        self.assertEqual(self.project['markers'][0]['name'], 'Wide shot')
        self.assertEqual(self.project['markers'][0]['time'], 0.5)
        self.assertEqual(self.project['markers'][0]['color'], 'Green')

    def test_media_pool_and_offline_paths(self):
        media = {m['name']: m for m in self.project['media']}
        self.assertEqual(media['A001.mp4']['path'], '/footage/Day 1/A001.mp4')
        self.assertEqual(media['A002.wav']['path'], 'E:\\Shoot\\Day 1\\A002.wav')
        self.assertEqual((media['A001.mp4']['bin'], media['A001.mp4']['duration'], media['A001.mp4']['type']),
                         ('Day 1', 10.0, 'video'))
        self.assertEqual((media['A002.wav']['duration'], media['A002.wav']['type']), (10.0, 'audio'))
        self.assertTrue(media['A001.mp4']['offline'] and media['A002.wav']['offline'])
        self.assertEqual(media['A002.wav']['src'], '')

    def test_unsupported_items_are_listed_with_their_place(self):
        warnings = self.project['compatibilityWarnings']
        self.assertIn('Harbor cut · Picture · A001.mp4 at 01:00:00:00: its Color page grade (nodes and versions) '
                      'is not imported.', warnings)
        self.assertIn('Harbor cut · Dialogue · A002.wav at 01:00:01:00: its Fusion composition is not imported.', warnings)
        self.assertTrue(any(w.startswith('Harbor cut · Video 2 · Solid Color') and 'generator' in w for w in warnings))
        generator = by_title(self.project, 'i-gen')
        self.assertEqual(generator['nativeUnsupported'], 'Resolve generator (Solid Color)')

    def test_original_stays_out_of_the_model(self):
        encoded = json.dumps(self.project)
        self.assertLess(len(encoded), 8000)
        self.assertEqual(self.project['resolveSource']['sha256'], hashlib.sha256(F.drp_bytes(self.spec)).hexdigest())
        self.assertEqual(self.project['resolveSource']['format'], 'drp')


class MarkerLayoutTests(unittest.TestCase):
    def test_both_marker_entry_layouts(self):
        detail = F.pb((1, 2), (3, ''), (3, '1'), (3, 'Marker 1'))
        body = F.struct.pack('>II', 2, len(F.pb((1, detail)))) + F.pb((1, detail))
        newer = F.pb((2, F.pb((2, F.pb((1, F.pb((1, 1968))), (2, body))))))
        older = F.pb((2, F.pb((1, F.pb((1, 1968), (2, body))))))
        for payload in (newer, older):
            self.assertEqual(blobs.markers(payload, 10),
                             [{'frame': 1968, 'note': '', 'name': 'Marker 1', 'duration': 1, 'color': 'Blue'}])


class LibraryTests(Scratch):
    def test_listing_and_open_match_the_drp(self):
        db = self.library()
        projects, truncated = resolve.list_library(self.dir / 'library')
        self.assertFalse(truncated)
        self.assertEqual([(p['name'], p['timelines'], p['saved'], p['error']) for p in projects],
                         [('Harbor Lights', 2, 1767225600, None)])
        for index in (0, 1):
            from_db = resolve.open_library_project(Path(projects[0]['path']), index, None, self.dir)
            from_drp = resolve.open_drp(F.drp_bytes(self.spec), index)
            self.assertEqual(from_db['resolveSource']['format'], 'library')
            self.assertEqual(from_db['resolveSource']['path'], str(db))
            for key in ('name', 'width', 'height', 'fps', 'frameRate', 'startFrame', 'clips', 'markers',
                        'trackCounts', 'trackNames', 'compatibilityWarnings', 'resolveTimelines'):
                self.assertEqual(from_db[key], from_drp[key], key)
            self.assertEqual([{k: v for k, v in m.items()} for m in from_db['media']], from_drp['media'])

    def test_project_folder_or_users_folder_lists(self):
        self.library()
        users = self.dir / 'library' / 'Resolve Projects' / 'Users'
        self.assertEqual(len(resolve.list_library(users)[0]), 1)
        project = resolve.open_library_project(users / 'guest' / 'Projects' / 'Harbor Lights', 0, None, self.dir)
        self.assertEqual(project['name'], 'Harbor cut')

    def test_reading_never_changes_the_library(self):
        self.library()
        root = self.dir / 'library'
        before = tree_state(root)
        projects, _ = resolve.list_library(root)
        scratch = self.dir / 'scratch'
        scratch.mkdir()
        resolve.open_library_project(Path(projects[0]['path']), 1, None, scratch)
        self.assertEqual(tree_state(root), before)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_read_only_library(self):
        db = self.library()
        root = self.dir / 'library'
        for path in sorted(root.rglob('*'), reverse=True):
            path.chmod(0o444 if path.is_file() else 0o555)
        root.chmod(0o555)
        before = tree_state(root)
        projects, _ = resolve.list_library(root)
        self.assertEqual(projects[0]['error'], None)
        project = resolve.open_library_project(db, 0, None, self.dir)
        self.assertEqual(len(project['clips']), 5)
        self.assertEqual(tree_state(root), before)

    def test_project_being_saved_is_refused_cleanly(self):
        db = self.library()
        Path(str(db) + '-journal').write_bytes(b'')
        with mock.patch('time.sleep'):
            with self.assertRaisesRegex(resolve.ResolveError, 'saving this project'):
                resolve.open_library_project(db, 0, None, self.dir)
        projects, _ = resolve.list_library(self.dir / 'library')
        self.assertEqual(projects[0]['error'], 'Resolve is saving this project')

    def test_project_changing_during_copy_is_retried(self):
        db = self.library()
        real = os.stat
        calls = {'n': 0}

        def moving(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if str(path) == str(db):
                calls['n'] += 1
                if calls['n'] <= 3:
                    return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_mtime_ns=calls['n'])
            return info
        with mock.patch('time.sleep'), mock.patch('os.stat', moving):
            project = resolve.open_library_project(db, 0, None, self.dir)
        self.assertEqual(project['name'], 'Harbor cut')
        self.assertGreater(calls['n'], 3)


class MediaTests(Scratch):
    def test_local_media_links_and_foreign_paths_stay_offline(self):
        footage = self.dir / 'footage' / 'Day 1'
        footage.mkdir(parents=True)
        (footage / 'A001.mp4').write_bytes(b'not really video')
        media_root = self.dir / 'media'
        spec = F.sample(str(footage / 'A001.mp4'), 'E:\\Shoot\\Day 1\\A002.wav')
        project = resolve.open_drp(F.drp_bytes(spec), 0, lambda path: link_local(path, media_root))
        media = {m['name']: m for m in project['media']}
        self.assertFalse(media['A001.mp4']['offline'])
        self.assertRegex(media['A001.mp4']['src'], r'^/media/[a-f0-9]{32}/A001\.mp4$')
        linked = media_root / media['A001.mp4']['src'].split('/')[2] / 'A001.mp4'
        self.assertEqual(os.stat(linked).st_ino, os.stat(footage / 'A001.mp4').st_ino)
        self.assertTrue(media['A002.wav']['offline'])
        self.assertEqual(link_local('E:\\Shoot\\Day 1\\A002.wav', media_root), '')
        self.assertEqual(link_local(str(self.dir), media_root), '')
        (self.dir / 'notes.txt').write_text('x')
        self.assertEqual(link_local(str(self.dir / 'notes.txt'), media_root), '')


def drp_with(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


class HostileInputTests(Scratch):
    def refused(self, data, pattern):
        with self.assertRaisesRegex(resolve.ResolveError, pattern):
            resolve.open_drp(data)

    def test_not_a_project(self):
        self.refused(b'', 'empty')
        self.refused(b'PK\x03\x04garbage', 'not a DaVinci Resolve project')
        self.refused(drp_with({'readme.txt': 'hello'}), 'no project.xml')
        self.refused(source.SQLITE_MAGIC + bytes(100), 'library database')
        self.refused(drp_with({'project.xml': '<SM_Project><ProjectName>x'}), 'not well-formed')
        self.refused(drp_with({'project.xml': '<Other/>'}), 'not a Resolve project description')

    def test_entity_expansion_is_refused(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
                '<SM_Project><ProjectName>&b;</ProjectName></SM_Project>')
        self.refused(drp_with({'project.xml': bomb}), 'document type')

    def test_zip_bomb_member_is_bounded(self):
        data = drp_with({'project.xml': '<SM_Project>' + ' ' * 5_000_000 + '</SM_Project>'})
        self.assertLess(len(data), 100_000)
        with mock.patch.object(source, 'MAX_MEMBER_BYTES', 1_000_000):
            self.refused(data, 'more XML than Splice reads')

    def test_element_count_is_bounded(self):
        data = drp_with({'project.xml': '<SM_Project>' + '<a/>' * 5000 + '</SM_Project>'})
        with mock.patch.object(source, 'MAX_ELEMENTS', 1000):
            self.refused(data, 'more elements')

    def test_oversized_file_and_member_count(self):
        with mock.patch.object(source, 'MAX_DRP_BYTES', 100):
            self.refused(F.drp_bytes(self.spec), 'larger than Splice opens')
        with mock.patch.object(source, 'MAX_MEMBERS', 3):
            self.refused(F.drp_bytes(self.spec), 'too many members')

    def test_no_timelines_and_bad_index(self):
        empty = drp_with({'project.xml': '<SM_Project DbId="p"><ProjectName>Empty</ProjectName></SM_Project>'})
        self.refused(empty, 'no timelines')
        with self.assertRaisesRegex(resolve.ResolveError, 'does not exist'):
            resolve.open_drp(F.drp_bytes(self.spec), 7)

    def test_damaged_blobs_become_warnings(self):
        spec = F.sample()
        items = spec['timelines'][0]['tracks'][0]['items']
        items[0]['effects'] = F.envelope(b'\xff\xff\xff', compress=False)
        items[2]['timemap'] = b'\x00\x00\x00\x01' + b'\xff' * 12
        items[0]['links'] = None
        spec['timelines'][0]['markers'] = []
        project = resolve.open_drp(F.drp_bytes(spec))
        warnings = '\n'.join(project['compatibilityWarnings'])
        self.assertIn('its effects could not be decoded', warnings)
        self.assertIn('its speed could not be decoded', warnings)
        self.assertEqual(len(project['clips']), 5)

    def test_zstd_bomb_and_budget(self):
        frame = zstd.MAGIC + bytes([0xE0]) + (10 ** 9).to_bytes(8, 'little') + b'\x01\x00\x00'
        with self.assertRaisesRegex(blobs.BlobError, 'declares'):
            blobs.unwrap(b'\x81' + frame, blobs.Budget())
        budget = blobs.Budget(limit=10)
        with self.assertRaises(blobs.BlobError):
            blobs.unwrap(F.envelope(b'x' * 100, compress=True), budget)

    def test_hostile_keyed_dicts(self):
        for data in (b'\x00\x00\x00\x01\xff\xff\xff\xff', b'\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x05abcde',
                     F.keyed({'a': 1}) + b'trailing'):
            with self.assertRaises(blobs.BlobError):
                blobs.keyed_dict(data)
        deep = b'\x00\x00\x00\x09\x00\x00\x00\x00\x01' * 40
        with self.assertRaises(blobs.BlobError):
            blobs.keyed_dict(b'\x00\x00\x00\x01\x00\x00\x00\x01' + F.qstring('k') + deep)

    def test_cyclic_bins_and_orphan_containers(self):
        spec = F.sample()
        spec['bins'] = [('bin-master', 'Master', 'bin-day'), ('bin-day', 'Day 1', 'bin-master')]
        project = resolve.open_drp(F.drp_bytes(spec))
        self.assertEqual(len(project['clips']), 5)

    def test_damaged_library_databases(self):
        folder = self.dir / 'bad'
        folder.mkdir()
        (folder / 'Project.db').write_bytes(b'not sqlite at all' * 10)
        with self.assertRaisesRegex(resolve.ResolveError, 'not a Resolve project database'):
            resolve.open_library_project(folder / 'Project.db', 0, None, self.dir)
        import sqlite3
        conn = sqlite3.connect(folder / 'Other.db')
        conn.execute('CREATE TABLE t (x)')
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(resolve.ResolveError, 'no SM_Project table'):
            resolve.open_library_project(folder / 'Other.db', 0, None, self.dir)
        db = self.library()
        with mock.patch.object(source, 'MAX_ROWS', 5):
            with self.assertRaisesRegex(resolve.ResolveError, 'more rows'):
                resolve.open_library_project(db, 0, None, self.dir)
        with mock.patch.object(source, 'MAX_DB_BYTES', 10):
            with self.assertRaisesRegex(resolve.ResolveError, 'larger than Splice opens'):
                resolve.open_library_project(db, 0, None, self.dir)
        self.assertEqual(list(self.dir.glob('splice-resolve-*')), [])

    def test_listing_a_folder_without_projects(self):
        with self.assertRaisesRegex(resolve.ResolveError, 'No Resolve projects'):
            resolve.list_library(self.dir)
        with self.assertRaisesRegex(resolve.ResolveError, 'library folder'):
            resolve.list_library(self.dir / 'missing')


class ZstdTests(unittest.TestCase):
    def test_python_decoder_against_library_frames(self):
        for name, case in FRAMES.items():
            with self.subTest(name):
                out = zstd.decompress_python(bytes.fromhex(case['frame']), case['size'])
                self.assertEqual((len(out), hashlib.sha256(out).hexdigest()), (case['size'], case['sha256']))

    def test_python_decoder_bounds_output(self):
        case = FRAMES['multi-block-repetitive']
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress_python(bytes.fromhex(case['frame']), case['size'] - 1)
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress(bytes.fromhex(case['frame']), 1000)

    def test_python_decoder_rejects_damage(self):
        frame = bytearray.fromhex(FRAMES['fse-tables']['frame'])
        for cut in (5, 12, len(frame) // 2):
            with self.assertRaises(zstd.ZstdError):
                zstd.decompress_python(bytes(frame[:cut]), 1 << 20)
        original = zstd.decompress_python(bytes(frame), 1 << 20)
        damaged = bytearray(frame)
        for i in range(20, len(damaged), 97):
            damaged[i] ^= 0x5A
        try:  # damage is either refused or decodes to different bytes, never another exception
            self.assertNotEqual(zstd.decompress_python(bytes(damaged), 1 << 20), original)
        except zstd.ZstdError:
            pass
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress_python(zstd.MAGIC + bytes([0x21, 0x01, 0x02, 0x03, 0x04, 0x05]), 100)

    @unittest.skipUnless(importlib.util.find_spec('compression') and importlib.util.find_spec('compression.zstd'),
                         'needs the real zstd library to make frames')
    def test_python_decoder_against_fresh_frames(self):
        from compression import zstd as real
        import random
        rng = random.Random(3)
        words = [b'alpha', b'beta', b'clip', b'\x00\x01', b'timeline']
        for size in (0, 1, 500, 70_000, 300_000):
            data = b' '.join(rng.choice(words) for _ in range(size // 5)) + rng.randbytes(size % 997)
            for level in (1, 9, 19):
                frame = real.compress(data, level=level)
                self.assertEqual(zstd.decompress_python(frame, len(data)), data)

    def test_fallback_when_no_library_can_be_imported(self):
        blocked = {'compression': None, 'compression.zstd': None, 'zstandard': None, 'pyzstd': None}
        with mock.patch.dict(sys.modules, blocked):
            self.assertEqual(zstd.backend_name(), 'python')
            case = FRAMES['repeat-offsets']
            out = zstd.decompress(bytes.fromhex(case['frame']), case['size'])
            self.assertEqual(hashlib.sha256(out).hexdigest(), case['sha256'])
            spec = F.sample()
            project = resolve.open_drp(F.drp_bytes(spec))
            self.assertEqual(by_title(project, 'i-v1')['transform']['scale'], 125.0)


class ServerTests(Scratch):
    def setUp(self):
        super().setUp()
        import threading
        os.environ['SPLICE_DATA_DIR'] = str(self.dir / 'data')
        import server
        self.server = server.create_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop('SPLICE_DATA_DIR', None)
        super().tearDown()

    def post(self, route, body, kind='application/octet-stream'):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        try:
            conn.request('POST', route, body, {'Content-Type': kind})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_routes(self):
        status, data = self.post('/api/resolve/open?timeline=1', F.drp_bytes(self.spec))
        self.assertEqual((status, data['project']['name']), (200, 'Harbor alt'))
        status, data = self.post('/api/resolve/open', b'junk')
        self.assertEqual(status, 400)
        self.assertIn('not a DaVinci Resolve project', data['error'])
        self.library()
        status, data = self.post('/api/resolve/library/list', json.dumps({'path': str(self.dir / 'library')}),
                                 'application/json')
        self.assertEqual((status, len(data['projects'])), (200, 1))
        status, data = self.post('/api/resolve/library/open', json.dumps({'path': data['projects'][0]['path'],
                                                                          'timeline': 0}), 'application/json')
        self.assertEqual((status, data['project']['name']), (200, 'Harbor cut'))
        for payload in ('[]', '{"path": 5}', '{"path": "x", "timeline": -1}', 'not json'):
            status, data = self.post('/api/resolve/library/open', payload, 'application/json')
            self.assertEqual(status, 400, payload)
        self.assertEqual(list((self.dir / 'data' / 'resolve-snapshots').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
