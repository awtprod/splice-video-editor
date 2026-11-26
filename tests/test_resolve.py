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
        self.assertEqual((p['frameRate'], p['startFrame'], p['startTimecode']), ('24/1', 86400, 86400))
        self.assertNotIn('dropFrame', p)
        self.assertEqual(p['resolveTimelines'], ['Harbor cut', 'Harbor alt'])
        other = resolve.open_drp(F.drp_bytes(self.spec), 1)
        self.assertEqual((other['name'], other['frameRate'], other['fps']), ('Harbor alt', '30000/1001', 30000 / 1001))
        self.assertEqual((other['width'], other['height'], other['startTimecode']), (1920, 1080, 0))
        # The drop-frame setting is not read: Resolve's default, non-drop, with a note.
        self.assertIs(other['dropFrame'], False)
        self.assertIn('Harbor alt: Splice cannot read Resolve’s drop-frame setting; this 29.97 fps timeline opens '
                      'with non-drop timecode, Resolve’s default. Change it in File → Sequence settings if Resolve '
                      'shows drop-frame timecode (semicolons).', other['compatibilityWarnings'])

    def test_drop_frame_is_inferred_only_from_a_drop_frame_hour(self):
        cases = ((30000 / 1001, 107892, True, '01:00:00;00 in drop-frame'),
                 (30000 / 1001, 108000, False, '(it starts at 01:00:00:00 in non-drop timecode)'),
                 (60000 / 1001, 2 * 215784, True, '02:00:00;00 in drop-frame'),
                 (60000 / 1001, 216000, False, 'non-drop timecode'),
                 (30000 / 1001, 107891, False, 'non-drop timecode, Resolve’s default.'))
        for fps, start, drop, text in cases:
            spec = F.sample()
            spec['timelines'][1].update(fps=fps, start=start / fps)
            for p in (resolve.open_drp(F.drp_bytes(spec), 1),
                      resolve.open_library_project(self.library(spec), 1, None, self.dir)):
                self.assertEqual((p['startTimecode'], p['dropFrame']), (start, drop), (fps, start))
                lines = [w for w in p['compatibilityWarnings'] if 'drop-frame setting' in w]
                self.assertEqual(len(lines), 1)
                self.assertIn(text, lines[0])
        # 24 fps has no drop-frame timecode: nothing is said.
        self.assertNotIn('dropFrame', self.project)
        self.assertFalse(any('drop-frame' in w for w in self.project['compatibilityWarnings']))

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
            for key in ('name', 'width', 'height', 'fps', 'frameRate', 'startFrame', 'startTimecode', 'clips', 'markers',
                        'trackCounts', 'trackNames', 'compatibilityWarnings', 'resolveTimelines'):
                self.assertEqual(from_db[key], from_drp[key], key)
            self.assertEqual(from_db.get('dropFrame'), from_drp.get('dropFrame'))
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


def use(spec, media):
    """Put a clip of each media pool entry in ``media`` (ids) on the second timeline's audio track."""
    items = spec['timelines'][1]['tracks'][1]['items']
    for mid in media:
        items.append({'id': f'i-use-{len(items)}', 'tag': 'Sm2TiAudioClip', 'name': mid, 'start': 30 * len(items),
                      'duration': 30, 'in': 0, 'media': mid})
    return spec


def wav(path, seconds=0.05):
    """A tiny real WAV (silence) that FFmpeg probes as audio."""
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b'\0\0' * int(8000 * seconds))
    return path


class MediaTests(Scratch):
    """Media a project names is referenced in place through the link registry, never copied."""

    def setUp(self):
        super().setUp()
        import media_links
        self.ml = media_links
        saved = dict(MEDIA_ROOT=media_links.MEDIA_ROOT, REGISTRY=media_links.REGISTRY)
        media_links.MEDIA_ROOT = self.dir / 'data' / 'media'
        media_links.REGISTRY = self.dir / 'data' / 'linked-media.json'
        media_links._cache.update(stamp=None, links={}, recovered=None)

        def restore():
            for key, value in saved.items():
                setattr(media_links, key, value)
            media_links._cache.update(stamp=None, links={}, recovered=None)
        self.addCleanup(restore)

    def registered(self):
        return sorted(entry['path'] for entry in self.ml._links().values())

    def test_local_media_is_linked_in_place_and_foreign_paths_stay_offline(self):
        clip = wav(self.dir / 'footage' / 'Day 1' / 'A001.wav')
        spec = F.sample(str(clip), 'E:\\Shoot\\Day 1\\A002.wav')
        project = resolve.open_drp(F.drp_bytes(spec), 0, link_local)
        media = {m['name']: m for m in project['media']}
        found = media['A001.wav']
        self.assertEqual((found['offline'], found['linked'], found['path']), (False, True, str(clip)))
        self.assertRegex(found['src'], r'^/media/linked/[a-f0-9]{32}/A001\.wav$')
        self.assertEqual(self.ml.resolve_media(found['src']), clip.resolve())
        self.assertTrue(found['sourceFingerprint'])
        self.assertEqual(self.registered(), [str(clip.resolve())])
        self.assertFalse((self.dir / 'data' / 'media').exists())  # nothing copied or hard-linked
        offline = media['A002.wav']
        self.assertEqual((offline['offline'], offline['src'], offline['path']), (True, '', 'E:\\Shoot\\Day 1\\A002.wav'))
        self.assertEqual(project['resolveSource']['linkedMedia'], 1)
        # Opening the project again reuses the same link.
        again = resolve.open_drp(F.drp_bytes(spec), 0, link_local)
        self.assertEqual(next(m['src'] for m in again['media'] if m['name'] == 'A001.wav'), found['src'])

    def test_a_crafted_project_registers_only_real_media(self):
        text = self.dir / 'notes.mp4'
        text.write_text('ssh-ed25519 AAAA not really video\n' * 20)
        key = self.dir / 'id_ed25519'
        key.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n')
        notes = self.dir / 'notes.txt'
        notes.write_text('hello')
        named = [str(text), str(key), str(notes), '/etc/passwd', str(self.dir / 'gone' / 'A009.mov'),
                 str(self.dir), 'relative/A001.wav', 'E:\\Shoot\\A002.wav']
        self.assertEqual([r.get('offline') for r in link_local(named)],
                         ['not-media', 'not-media', 'not-media', 'not-media', 'missing', 'not-media', 'missing', 'missing'])
        spec = F.sample(str(text), str(self.dir / 'gone' / 'A009.wav'))
        spec['media'] += [dict(spec['media'][1], id=f'm-x{n}', info=f'ai-x{n}', name=Path(path).name,
                               dir=str(Path(path).parent), file=Path(path).name)
                          for n, path in enumerate(('/etc/passwd', str(key), str(notes)))]
        use(spec, ['m-x0', 'm-x1', 'm-x2'])
        project = resolve.open_drp(F.drp_bytes(spec), 0, link_local)
        self.assertEqual(self.registered(), [])
        by_name = {m['name']: m for m in project['media']}
        for name in ('notes.mp4', 'A009.wav', 'passwd', 'id_ed25519', 'notes.txt'):
            self.assertEqual((by_name[name]['offline'], by_name[name]['src']), (True, ''), name)
        self.assertEqual(by_name['A009.wav']['path'], str(self.dir / 'gone' / 'A009.wav'))
        self.assertFalse(self.ml.REGISTRY.exists())

    def test_a_project_with_500_media_writes_the_registry_once(self):
        import time
        clips = [wav(self.dir / 'card' / f'c{n:03}.wav') for n in range(500)]
        spec = F.sample(str(clips[0]), str(clips[1]))
        spec['media'] += [dict(spec['media'][1], id=f'm-c{n}', info=f'ai-c{n}', name=clip.name,
                               dir=str(clip.parent), file=clip.name) for n, clip in enumerate(clips[2:])]
        use(spec, [f'm-c{n}' for n in range(498)])
        writes = []
        real = self.ml._write
        with mock.patch.object(self.ml, '_write', lambda *a: (writes.append(1), real(*a))[1]):
            began = time.monotonic()
            project = resolve.open_drp(F.drp_bytes(spec), 0, link_local)
            elapsed = time.monotonic() - began
        print(f'\n500-media Resolve project: {elapsed:.2f} s, registry writes: {len(writes)}', file=sys.stderr)
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(self.registered()), 500)
        self.assertEqual(sum(1 for m in project['media'] if m.get('linked')), 500)


class DesktopPackageTests(unittest.TestCase):
    def test_the_desktop_archives_carry_the_resolve_package(self):
        import package_desktop
        files = {path.as_posix() for path in package_desktop.source_files()}
        expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'resolve').glob('*.py')}
        self.assertTrue(expected and expected <= files, sorted(expected - files))
        self.assertTrue({'server.py', 'resolve-dialogs.js', 'resolve-dialogs.css'} <= files)


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
    def test_python_decoder_against_committed_frames(self):
        # Frames made by the reference zstd library at several levels (committed so 3.10-3.13
        # run them too); the repeat-offset-* frames each reach one repeat-offset rule.
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
        frame = bytearray.fromhex(FRAMES['checksummed']['frame'])
        for cut in (5, 12, len(frame) // 2):
            with self.assertRaises(zstd.ZstdError):
                zstd.decompress_python(bytes(frame[:cut]), 1 << 20)
        original = zstd.decompress_python(bytes(frame), 1 << 20)
        # The frame carries a checksum, so damage is refused; never different bytes.
        self.assertTrue(frame[4] & 0x04)
        for i in range(20, len(frame), 7):
            damaged = bytearray(frame)
            damaged[i] ^= 0x5A
            with self.subTest(byte=i):
                try:
                    self.assertEqual(zstd.decompress_python(bytes(damaged), 1 << 20), original)
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


# --------------------------------------------------------------------------- hand-built zstd frames

def _bits(reads, padding=0):
    """A zstd backward bitstream whose reads, in order, return ``reads`` [(value, width)]."""
    value, total = 0, 0
    for item, width in reads:
        value = (value << width) | item
        total += width
    value <<= padding
    total += padding
    value |= 1 << total
    return value.to_bytes((total + 8) // 8, 'little')


def _sequences_block(literals, count, codes, reads, padding=0, last=True):
    """A compressed block: raw literals, ``count`` sequences coded with RLE tables (ll, of, ml)."""
    assert len(literals) < 32
    body = bytes([len(literals) << 3]) + literals
    body += bytes([count, 0x54]) + bytes(codes) + _bits(reads, padding)
    return (len(body) << 3 | 2 << 1 | int(last)).to_bytes(3, 'little') + body


def _frame(*blocks, size=None, flags=0, dict_id=b''):
    if size is None:
        header = bytes([flags | (len(dict_id) and {1: 1, 2: 2, 4: 3}[len(dict_id)]), 0x70]) + dict_id
    else:
        header = bytes([0x20 | flags | (len(dict_id) and {1: 1, 2: 2, 4: 3}[len(dict_id)])]) + dict_id + bytes([size])
    return zstd.MAGIC + header + b''.join(blocks)


def _raw_block(data, last=True):
    return (len(data) << 3 | int(last)).to_bytes(3, 'little') + data


class ZstdBoundaryTests(unittest.TestCase):
    """Each rule the fallback decoder enforces, on frames built byte by byte."""

    def test_hand_built_sequences_decode(self):
        # One sequence: 1 literal, offset code 2 (explicit offset 1), match length 3.
        frame = _frame(_sequences_block(b'A', 1, (1, 2, 0), [(0, 2)]))
        self.assertEqual(zstd.decompress_python(frame, 100), b'AAAA')

    def test_bitstream_must_be_fully_consumed(self):
        frame = _frame(_sequences_block(b'A', 1, (1, 2, 0), [(0, 2)], padding=3))
        with self.assertRaisesRegex(zstd.ZstdError, 'not fully consumed'):
            zstd.decompress_python(frame, 100)

    def test_block_output_is_bounded_while_matches_are_copied(self):
        # The first sequence alone (1 + 131,074 bytes) overflows a block; the second would need a
        # literal that does not exist. The bound must stop the copy at the first one.
        reads = [(0, 2), (65535, 16), (0, 2), (65535, 16)]
        frame = _frame(_sequences_block(b'A', 2, (1, 2, 52), reads))
        with self.assertRaisesRegex(zstd.ZstdError, 'block output too large'):
            zstd.decompress_python(frame, 1 << 30)

    def test_rle_blocks_are_bounded(self):
        frame = _frame((200_000 << 3 | 1 << 1 | 1).to_bytes(3, 'little') + b'A')
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress_python(frame, 1 << 30)
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress(frame, 1 << 30)

    def test_declared_size_must_match(self):
        for size in (2, 4):
            frame = _frame(_raw_block(b'abc'), size=size)
            with self.subTest(size=size):
                with self.assertRaisesRegex(zstd.ZstdError, 'does not match'):
                    zstd.decompress_python(frame, 100)
                with self.assertRaises(zstd.ZstdError):
                    zstd.decompress(frame, 100)
        self.assertEqual(zstd.decompress_python(_frame(_raw_block(b'abc'), size=3), 100), b'abc')

    def test_dictionary_frames_are_refused(self):
        frame = _frame(_raw_block(b'abc'), size=3, dict_id=b'\x07')
        with self.assertRaisesRegex(zstd.ZstdError, 'dictionary'):
            zstd.decompress_python(frame, 100)
        with self.assertRaisesRegex(zstd.ZstdError, 'dictionary'):
            zstd.decompress(frame, 100)

    def test_checksum_is_verified(self):
        good = _frame(_raw_block(b'abc'), size=3, flags=0x04) + (zstd.xxh64(b'abc') & 0xFFFFFFFF).to_bytes(4, 'little')
        self.assertEqual(zstd.decompress_python(good, 100), b'abc')
        bad = good[:-1] + bytes([good[-1] ^ 1])
        with self.assertRaisesRegex(zstd.ZstdError, 'checksum'):
            zstd.decompress_python(bad, 100)
        for cut in (1, 2, 3, 4):
            with self.assertRaisesRegex(zstd.ZstdError, 'checksum'):
                zstd.decompress_python(good[:-cut], 100)

    def test_xxh64_reference_values(self):
        self.assertEqual(zstd.xxh64(b''), 0xEF46DB3751D8E999)
        self.assertEqual(zstd.xxh64(b'a'), 0xD24EC4F1A98C6E5B)
        self.assertEqual(zstd.xxh64(b'abc'), 0x44BC2CF5AD770999)
        self.assertEqual(zstd.xxh64(b'Nobody inspects the spammish repetition'), 0xFBCEA83C8A378BF1)

    def test_malformed_blocks_raise_zstd_errors_only(self):
        # Found by the validator: a block too short for its literals header (was IndexError).
        frame = bytes.fromhex('28b52ffd206405000010676701003f012c')
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress_python(frame, 1 << 20)
        with self.assertRaisesRegex(zstd.ZstdError, 'truncated literals header'):
            zstd.decompress_python(_frame((1 << 3 | 2 << 1 | 1).to_bytes(3, 'little') + b'\x04'), 100)
        for body in (b'', b'\x04', b'\x0c\x01', b'\x02\x00\x00'):
            block = (len(body) << 3 | 2 << 1 | 1).to_bytes(3, 'little') + body
            with self.subTest(body=body.hex()), self.assertRaises(zstd.ZstdError):
                zstd.decompress_python(_frame(block), 1 << 20)
        with mock.patch.object(zstd, '_Frame', side_effect=IndexError('slip')):
            with self.assertRaisesRegex(zstd.ZstdError, 'damaged'):
                zstd.decompress_python(_frame(_raw_block(b'x')), 10)

    def test_one_bad_blob_is_one_warning(self):
        spec = F.sample()
        bad = F.struct.pack('>II', 2, 18) + b'\x81' + bytes.fromhex('28b52ffd206405000010676701003f012c')
        spec['timelines'][0]['tracks'][0]['items'][0]['effects'] = bad
        with mock.patch.object(zstd, '_backend', lambda: ('python', None)):
            project = resolve.open_drp(F.drp_bytes(spec))
        self.assertEqual(len(project['clips']), 5)
        self.assertTrue(any('i-v1' not in w and 'effects could not be decoded' in w
                            for w in project['compatibilityWarnings']))

    def test_truncated_frames_are_refused_on_every_backend(self):
        frame = bytes.fromhex(FRAMES['fse-tables']['frame'])
        for cut in range(4, len(frame)):
            with self.subTest(cut=cut):
                with self.assertRaises(zstd.ZstdError):
                    zstd.decompress(frame[:cut], 1 << 20)
                with self.assertRaises(zstd.ZstdError):
                    zstd.decompress_python(frame[:cut], 1 << 20)

        class Partial:
            """A library decoder that hands back partial output for a cut frame, as some do."""
            ZstdError = RuntimeError

            class ZstdDecompressor:
                eof = True

                def stream_reader(self, data, read_across_frames=False):
                    return io.BytesIO(b'partial')

                def decompress(self, data, max_length=-1):
                    return b'partial'
        # No content size, cut inside its second block: only the frame's own structure shows it.
        cut = _frame(_raw_block(b'abc', last=False), _raw_block(b'defgh'))[:-2]
        for name in ('zstandard', 'pyzstd', 'compression.zstd'):
            with self.subTest(backend=name), mock.patch.object(zstd, '_backend', lambda: (name, Partial)):
                with self.assertRaisesRegex(zstd.ZstdError, 'truncated block'):
                    zstd.decompress(cut, 1 << 20)
                # A library that returns other bytes than the frame declares is refused too.
                with self.assertRaisesRegex(zstd.ZstdError, 'does not match'):
                    zstd.decompress(_frame(_raw_block(b'abc'), size=3), 1 << 20)
        Partial.ZstdDecompressor.eof = False
        whole = _frame(_raw_block(b'abc'))
        for name in ('pyzstd', 'compression.zstd'):
            with self.subTest(backend=name), mock.patch.object(zstd, '_backend', lambda: (name, Partial)):
                with self.assertRaisesRegex(zstd.ZstdError, 'truncated'):
                    zstd.decompress(whole, 1 << 20)

    @unittest.skipUnless(importlib.util.find_spec('compression') and importlib.util.find_spec('compression.zstd'),
                         'needs the standard-library zstd module')
    def test_standard_library_output_is_bounded_while_decoding(self):
        from compression import zstd as real
        seen = []

        original = real.ZstdDecompressor

        class Recording:
            def __init__(self):
                self.inner = original()

            @property
            def eof(self):
                return self.inner.eof

            def decompress(self, data, max_length=-1):
                seen.append(max_length)
                return self.inner.decompress(data, max_length=max_length)
        frame = real.compress(b'\0' * 1_000_000, options={real.CompressionParameter.content_size_flag: 0})
        with mock.patch.object(real, 'ZstdDecompressor', Recording):
            with self.assertRaisesRegex(zstd.ZstdError, 'exceeds'):
                zstd.decompress(frame, 1000)
        self.assertEqual(seen, [1001])

    @unittest.skipUnless(importlib.util.find_spec('compression') and importlib.util.find_spec('compression.zstd'),
                         'needs the standard-library zstd module to make frames')
    def test_python_decoder_matches_the_reference_across_settings(self):
        from compression import zstd as real
        import random
        P = real.CompressionParameter
        rng = random.Random(11)
        for trial in range(60):
            period = rng.randbytes(rng.randint(1, 40))
            data = b''.join(period if rng.random() < .7 else rng.randbytes(rng.randint(1, 9))
                            for _ in range(rng.choice((1, 30, 400, 3000))))
            options = {P.compression_level: rng.choice((-5, -1, 1, 3, 7, 12, 17, 19, 22)),
                       P.checksum_flag: trial % 2, P.content_size_flag: (trial // 2) % 2}
            if trial % 5 == 0:
                options[P.window_log] = rng.choice((10, 17, 20))
            frame = real.compress(data, options=options)
            with self.subTest(trial=trial, options=str(options)):
                self.assertEqual(zstd.decompress_python(frame, len(data)), data)
                cut = rng.randrange(4, len(frame))
                with self.assertRaises(zstd.ZstdError):
                    zstd.decompress(frame[:cut], 1 << 22)


class BoundsTests(Scratch):
    def test_budget_refuses_past_its_limit(self):
        budget = blobs.Budget(limit=10)
        budget.charge(10)
        with self.assertRaisesRegex(blobs.BlobError, 'more than 10 bytes'):
            budget.charge(1)
        self.assertEqual(budget.refused, 1)

    def test_keyed_dict_nesting_is_bounded(self):
        def nested(depth):
            value = F.variant(1)
            for _ in range(depth):
                value = F.struct.pack('>IBI', 0x08, 0, 1) + F.qstring('k') + value
            return F.struct.pack('>II', 1, 1) + F.qstring('root') + value
        self.assertIn('root', blobs.keyed_dict(nested(30)))
        with self.assertRaisesRegex(blobs.BlobError, 'nested too deeply'):
            blobs.keyed_dict(nested(40))

    def test_loaded_bytes_are_bounded(self):
        db = self.library()
        with mock.patch.object(source, 'MAX_LOAD_BYTES', 2000):
            with self.assertRaisesRegex(resolve.ResolveError, 'more data'):
                resolve.open_library_project(db, 0, None, self.dir)

    def test_a_journal_stops_the_copy_before_it_starts(self):
        db = self.library()
        Path(str(db) + '-wal').write_bytes(b'')
        with mock.patch('time.sleep'), mock.patch.object(source.tempfile, 'mkstemp', wraps=source.tempfile.mkstemp) as made:
            with self.assertRaisesRegex(resolve.ResolveError, 'saving this project'):
                source.snapshot(db, self.dir)
        made.assert_not_called()

    def test_a_save_that_keeps_the_size_is_still_noticed(self):
        db = self.library()
        real_open = open
        opened = {'n': 0}

        def opening(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if str(path) == str(db):
                opened['n'] += 1
                if opened['n'] == 1:  # saved during the first copy: same size, new time
                    info = os.stat(db)
                    os.utime(db, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
            return handle
        with mock.patch('time.sleep'), mock.patch('builtins.open', opening):
            copy, _digest = source.snapshot(db, self.dir)
        self.assertEqual(opened['n'], 2)
        self.assertEqual(len([p for p in self.dir.iterdir() if p.name.startswith('resolve-')]), 1)
        copy.unlink()

    def test_reads_take_no_lock(self):
        # immutable=1: a library Resolve holds an exclusive lock on still reads, and nothing is created.
        import sqlite3
        db = self.library()
        holder = sqlite3.connect(db, isolation_level=None)
        try:
            holder.execute('BEGIN EXCLUSIVE')
            conn = source.connect(db, source.time.monotonic() + 5)
            try:
                self.assertEqual(conn.execute('SELECT ProjectName FROM SM_Project').fetchone()[0], 'Harbor Lights')
            finally:
                conn.close()
            projects, _ = resolve.list_library(self.dir / 'library')
            self.assertEqual((projects[0]['name'], projects[0]['error']), ('Harbor Lights', None))
        finally:
            holder.execute('ROLLBACK')
            holder.close()

    def test_listing_checks_size_and_header_first(self):
        db = self.library()
        other = db.parent.parent / 'Broken' / 'Project.db'
        other.parent.mkdir()
        other.write_bytes(b'not sqlite' * 10)
        with mock.patch.object(source, 'connect', wraps=source.connect) as connected:
            projects, _ = resolve.list_library(self.dir / 'library')
        by_name = {p['folder']: p for p in projects}
        self.assertEqual(by_name['Broken']['error'], 'Not a Resolve project database')
        self.assertEqual(by_name['Harbor Lights']['error'], None)
        self.assertEqual(connected.call_count, 1)
        with mock.patch.object(source, 'MAX_DB_BYTES', 50):
            projects, _ = resolve.list_library(self.dir / 'library')
        self.assertTrue(all(p['error'].startswith('Larger than') for p in projects))
        with mock.patch.object(source, 'MAX_LIST_SECONDS', -1):
            projects, _ = resolve.list_library(self.dir / 'library')
        self.assertTrue(all('too long' in p['error'] or 'Not a Resolve' in p['error'] for p in projects))

    def test_xml_members_are_read_with_a_bound(self):
        import tracemalloc
        data = drp_with({'project.xml': '<SM_Project>' + ' ' * 40_000_000 + '</SM_Project>'})
        tracemalloc.start()
        try:
            with mock.patch.object(source, 'MAX_MEMBER_BYTES', 1_000_000):
                with self.assertRaisesRegex(resolve.ResolveError, 'more XML'):
                    resolve.open_drp(data)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 12_000_000)

    def test_tracks_past_the_limit_are_listed(self):
        spec = F.sample()
        tracks = spec['timelines'][1]['tracks']
        tracks[1:1] = [{'id': f'tr2-v{n}', 'type': 0, 'name': '', 'items': []} for n in range(2, 67)]
        project = resolve.open_drp(F.drp_bytes(spec), 1)
        self.assertEqual(project['trackCounts']['video'], importer.MAX_TRACKS)
        self.assertNotIn('video:65', project['trackNames'])
        self.assertEqual(sum('tracks of each kind' in w for w in project['compatibilityWarnings']), 2)

    def test_stale_snapshots_are_reaped_only_in_the_private_folder(self):
        folder = self.dir / 'snapshots'
        folder.mkdir()
        keep = self.dir / 'elsewhere'
        keep.mkdir()
        (keep / 'Project.db').write_bytes(b'precious')
        old = folder / 'splice-resolve-old'
        old.mkdir()
        (old / 'resolve-1.db').write_bytes(b'x')
        recent = folder / 'splice-resolve-recent'
        recent.mkdir()
        other = folder / 'unrelated-old'
        other.mkdir()
        link = folder / 'splice-resolve-link'
        link.symlink_to(keep, target_is_directory=True)
        hour_ago = source.time.time() - 3600
        for path in (old, other):
            os.utime(path, (hour_ago, hour_ago))
        os.utime(link, (hour_ago, hour_ago), follow_symlinks=False)
        self.assertEqual(source.reap_snapshots(folder), 1)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ['splice-resolve-link', 'splice-resolve-recent', 'unrelated-old'])
        self.assertEqual((keep / 'Project.db').read_bytes(), b'precious')

    def test_scratch_folder_reaps_once_per_process(self):
        os.environ['SPLICE_DATA_DIR'] = str(self.dir / 'data')
        try:
            stale = self.dir / 'data' / 'resolve-snapshots' / 'splice-resolve-crashed'
            stale.mkdir(parents=True)
            then = source.time.time() - 3600
            os.utime(stale, (then, then))
            with mock.patch.object(source, '_reaped', set()):
                folder = source.scratch_folder()
            self.assertEqual(list(folder.iterdir()), [])
        finally:
            os.environ.pop('SPLICE_DATA_DIR', None)

    def test_non_utf8_xml_is_refused(self):
        bomb = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE p [<!ENTITY a "aaaa">]>'
                '<SM_Project><ProjectName>&a;</ProjectName></SM_Project>')
        for data in (bomb.encode('utf-16'), bomb.encode('utf-16-le'), bomb.encode('utf-16-be'),
                     '<?xml version="1.0" encoding="ISO-8859-1"?><SM_Project/>'.encode('latin-1')):
            with self.subTest(data=data[:12]):
                with self.assertRaisesRegex(resolve.ResolveError, 'not UTF-8 XML'):
                    resolve.open_drp(drp_with({'project.xml': data}))
        spec_xml = F.drp_bytes(F.sample())
        with zipfile.ZipFile(io.BytesIO(spec_xml)) as archive:
            members = {i.filename: archive.read(i) for i in archive.infolist()}
        members['project.xml'] = b'\xef\xbb\xbf' + members['project.xml']
        self.assertEqual(resolve.open_drp(drp_with(members))['name'], 'Harbor cut')


class MarkerAndFlagTests(Scratch):
    def nested_spec(self):
        spec = F.sample()
        spec['marker_layout'] = 'nested'
        spec['timelines'][0]['markers'] = [(12, 'Wide shot', 'Check focus', 8), (30, 'Pickup', '', 32)]
        spec['timelines'][0]['tracks'][0]['items'][0]['markers'] = [(2, 'Focus', '', 2), (5, 'Hold', '', 2)]
        return spec

    def test_markers_in_the_older_nested_layout(self):
        project = resolve.open_drp(F.drp_bytes(self.nested_spec()))
        self.assertEqual([(m['name'], m['time'], m['color']) for m in project['markers']],
                         [('Wide shot', 0.5, 'Green'), ('Pickup', 1.25, 'Red')])
        self.assertIn('Harbor cut · Picture · A001.mp4 at 01:00:00:00: 2 clip marker(s) are not imported. Where '
                      'Resolve places a clip marker is not settled: Resolve 19 stores it as a frame of the source '
                      'media, and a Resolve 21 project stores twice the frame number the scripting API reports.',
                      project['compatibilityWarnings'])
        self.assertIn('Harbor cut: timeline markers are placed as frames from the timeline start, as Resolve 19 '
                      'stores them; this is not yet confirmed for projects saved by later versions of Resolve.',
                      project['compatibilityWarnings'])

    def test_both_layouts_and_the_library_agree(self):
        spec = self.nested_spec()
        nested = resolve.open_drp(F.drp_bytes(spec))
        spec['marker_layout'] = 'flat'
        flat = resolve.open_drp(F.drp_bytes(spec))
        library = resolve.open_library_project(self.library(spec), 0, None, self.dir)
        for other in (flat, library):
            self.assertEqual(other['markers'], nested['markers'])
            self.assertEqual(other['compatibilityWarnings'], nested['compatibilityWarnings'])

    def test_a_timeline_without_markers_says_nothing_about_them(self):
        project = resolve.open_drp(F.drp_bytes(F.sample()), 1)
        self.assertEqual(project['markers'], [])
        self.assertFalse(any('marker' in w for w in project['compatibilityWarnings']))


class ResolveStateTests(Scratch):
    """Enabled, hidden, muted and locked state, clip volume and position, as measured in Resolve 21.1
    calibration projects (one setting changed at a time) and rebuilt here synthetically."""

    def both(self, spec, index=0):
        return (resolve.open_drp(F.drp_bytes(spec), index),
                resolve.open_library_project(self.library(spec), index, None, self.dir))

    def tracks(self, spec):
        return {t['id']: t for t in spec['timelines'][0]['tracks']}

    def items(self, spec):
        return {i['id']: i for t in spec['timelines'][0]['tracks'] for i in t['items']}

    def test_a_disabled_picture_clip_opens_disabled_and_transparent(self):
        spec = F.sample()
        self.items(spec)['i-v1']['flags'] = 2
        for project in self.both(spec):
            picture, sound = by_title(project, 'i-v1'), by_title(project, 'i-a1')
            self.assertEqual((picture['enabled'], picture['opacity'], picture['resolveFlags']), (False, 0, 2))
            # Its linked sound is a clip of its own, unchanged (Resolve leaves it enabled too).
            self.assertNotIn('enabled', sound)
            self.assertEqual((sound['volume'], picture['linkGroup']), (100, sound['linkGroup']))
            self.assertNotIn('enabled', by_title(project, 'i-v2'))
            self.assertIn('Harbor cut · Picture · A001.mp4 at 01:00:00:00: the clip is disabled in Resolve. It opens '
                          'marked disabled (exports leave it out or mark it off) and transparent, as Splice has no '
                          'disabled switch yet; set its opacity back to 80 % to show it.', project['compatibilityWarnings'])
            self.assertFalse(any('flags' in w for w in project['compatibilityWarnings']))
            self.assertEqual(project['resolveSource']['summary'], ['From Resolve: 1 disabled clip.'])

    def test_disabled_tracks_hide_or_mute_and_locked_tracks_lock(self):
        spec = F.sample()
        tracks = self.tracks(spec)
        tracks['tr-v1']['flags'], tracks['tr-v2']['flags'], tracks['tr-a1']['flags'] = 2, 1, 3
        for project in self.both(spec):
            self.assertEqual(project['trackState'], {'video:1': {'hidden': True}, 'video:2': {'locked': True},
                                                     'audio:-1': {'locked': True}})
            self.assertEqual(project['trackMix'], {'-1': {'mute': True}})
            self.assertEqual(project['resolveTrackFlags'], {'video:1': 2, 'video:2': 1, 'audio:-1': 3})
            self.assertFalse(any('flags' in w for w in project['compatibilityWarnings']))
            self.assertEqual(project['resolveSource']['summary'],
                             ['From Resolve: 1 hidden track, 1 muted track, 2 locked tracks.'])
        # Nothing set: no track state, no mixer settings, no summary.
        plain = resolve.open_drp(F.drp_bytes(F.sample()))
        self.assertEqual((plain['trackState'], 'trackMix' in plain, plain['resolveSource']['summary']), ({}, False, []))

    def test_unexplained_flags_are_listed_with_their_value(self):
        spec = F.sample()
        tracks, items = self.tracks(spec), self.items(spec)
        tracks['tr-a1']['flags'] = 32
        tracks['tr-v2']['flags'] = 34
        items['i-v2']['flags'] = 4 | 2
        items['i-a2']['flags'] = 2  # no sample has a disabled sound clip: listed, not interpreted
        items['i-gen']['flags'] = 2
        for project in self.both(spec):
            warnings = project['compatibilityWarnings']
            self.assertIn('Harbor cut · Dialogue: the track carries other Resolve track flags (32) that Splice does '
                          'not interpret.', warnings)
            self.assertIn('Harbor cut · Video 2: the track carries other Resolve track flags (32) that Splice does '
                          'not interpret.', warnings)
            self.assertIn('Harbor cut · Picture · A001.mp4 at 01:00:01:00: the clip carries other Resolve clip flags '
                          '(4) that Splice does not interpret.', warnings)
            self.assertIn('Harbor cut · Dialogue · A002.wav at 01:00:01:00: the clip carries Resolve clip flags (2) '
                          'that Splice does not interpret (it may be disabled in Resolve; it opens enabled).', warnings)
            self.assertTrue(any(w.startswith('Harbor cut · Video 2 · Solid Color at 01:00:00:00: the clip carries '
                                             'Resolve clip flags (2)') for w in warnings))
            self.assertEqual(project['trackState'], {'video:2': {'hidden': True}})
            self.assertNotIn('trackMix', project)
            self.assertIs(by_title(project, 'i-v2')['enabled'], False)
            self.assertNotIn('enabled', by_title(project, 'i-a2'))
            self.assertEqual(by_title(project, 'i-a2')['resolveFlags'], 2)

    def test_clip_volume_in_decibels(self):
        cases = ((6.0, 199.526, None), (-6.0, 50.119, None), (0.0, 100.0, None), (-120.0, 0.0, None),
                 (12.0, 200.0, 'its clip volume (+12.00 dB) is above the +6.02 dB Splice plays and is clamped'))
        for db, volume, warning in cases:
            spec = F.sample()
            self.items(spec)['i-a2']['effects'] = F.effects((124, {95: db}), compress=False)
            for project in self.both(spec):
                clip = by_title(project, 'i-a2')
                self.assertEqual(clip['volume'], volume, db)
                lines = [w for w in project['compatibilityWarnings'] if 'A002.wav at 01:00:01:00' in w
                         and 'Fusion' not in w]
                self.assertEqual(lines, [f'Harbor cut · Dialogue · A002.wav at 01:00:01:00: {warning} to +6.02 dB.']
                                 if warning else [], db)
                self.assertEqual(by_title(project, 'i-a1')['volume'], 100)
        # A pan or other setting in the same effect is listed; another sound effect is listed with its type.
        spec = F.sample()
        self.items(spec)['i-a2']['effects'] = F.effects((124, {95: -3.0, 96: 0.5}), (130, {1: 1.0}), compress=False)
        project = resolve.open_drp(F.drp_bytes(spec))
        self.assertEqual(by_title(project, 'i-a2')['volume'], 70.795)
        self.assertIn('Harbor cut · Dialogue · A002.wav at 01:00:01:00: clip audio settings other than volume (such as '
                      'pan) are not imported.', project['compatibilityWarnings'])
        self.assertIn('Harbor cut · Dialogue · A002.wav at 01:00:01:00: clip audio adjustments other than volume (pan '
                      'or Fairlight clip settings, Resolve effect type 130) are not imported.', project['compatibilityWarnings'])

    def test_position_x_is_measured_and_the_rest_is_marked_approximate(self):
        spec = F.sample()
        items = self.items(spec)
        # As Resolve 21.1 writes +200 px on a 1920-wide timeline: an uncompressed envelope, Position X
        # in the fourth parameter slot, as a fraction of the width.
        items['i-v1']['effects'] = F.effects((4, {40: 200 / 1920}), compress=False, slot={40: 3})
        items['i-v2']['effects'] = F.effects((4, {40: -0.25, 41: 0.1, 42: 1.5, 43: 1.5}))
        for project in self.both(spec):
            first, second = by_title(project, 'i-v1'), by_title(project, 'i-v2')
            self.assertEqual(first['transform'], {'x': 10.416667, 'y': 0, 'scale': 100, 'rotation': 0})
            self.assertAlmostEqual(first['transform']['x'] / 100 * 1280 * 1920 / 1280, 200, places=3)
            self.assertEqual(second['transform'], {'x': -25.0, 'y': -10.0, 'scale': 150.0, 'rotation': 0})
            warnings = project['compatibilityWarnings']
            self.assertFalse(any(w.startswith('Harbor cut · Picture · A001.mp4 at 01:00:00:00') and 'approximate' in w
                                 for w in warnings))
            self.assertIn('Harbor cut · Picture · A001.mp4 at 01:00:01:00: its zoom (150 %) and vertical position '
                          '(-10 %) are approximate: Resolve’s units for them are inferred, not measured. Check the '
                          'picture.', warnings)
            self.assertIn('Harbor cut · Picture · A001.mp4 at 01:00:01:00: its speed (200 %) is approximate: it is read '
                          'from Resolve’s retime curve, which has not been measured against Resolve’s own speed '
                          'setting.', warnings)

    def test_a_timeline_that_starts_before_zero(self):
        spec = F.sample()
        spec['timelines'][0]['start'] = -48 / 24
        for track in spec['timelines'][0]['tracks']:
            for item in track['items']:
                item['start'] -= 86400 + 48
        for project in self.both(spec):
            self.assertEqual((project['startFrame'], project['startTimecode']), (-48, 0))
            self.assertIn('Harbor cut: the timeline starts before 00:00:00:00 (at -00:00:02:00). Splice’s timecode '
                          'cannot go below zero, so the timeline opens at 00:00:00:00 and every timecode reads '
                          '00:00:02:00 later than in Resolve.', project['compatibilityWarnings'])
            self.assertEqual(sorted(c['start'] for c in project['clips'])[:2], [0.0, 0.0])

    def test_only_media_some_timeline_uses_is_imported_and_looked_for(self):
        spec = F.sample()
        spare = dict(spec['media'][1], id='m-spare', info='ai-spare', name='Spare.wav', file='Spare.wav')
        other = dict(spec['media'][1], id='m-other', info='ai-other', name='Other.wav', file='Other.wav')
        spec['media'] += [spare, other]
        spec['timelines'][1]['tracks'][1]['items'].append(
            {'id': 'i2-a1', 'tag': 'Sm2TiAudioClip', 'name': 'Other.wav', 'start': 0, 'duration': 30, 'in': 0,
             'media': 'm-other'})
        asked = []
        link = lambda paths: (asked.extend(paths), [{'offline': 'missing'}] * len(paths))[1]
        project = resolve.open_drp(F.drp_bytes(spec), 0, link)
        names = sorted(m['name'] for m in project['media'])
        self.assertEqual(names, ['A001.mp4', 'A002.wav', 'Other.wav', 'Resolve generator (Solid Color)'])
        self.assertNotIn('E:\\Shoot\\Day 1\\Spare.wav', asked)
        self.assertEqual(len(asked), 3)
        self.assertIn('1 media pool file(s) that no timeline of this project uses are not imported.',
                      project['compatibilityWarnings'])
        self.assertIn('3 media file(s) are offline on this computer: nothing is at the path the project names (it may '
                      'be on another computer or have moved). Relink them without copying with File → Link media in '
                      'place → Relink offline from folder; Windows and Mac paths are matched by folder and file name.',
                      project['compatibilityWarnings'])
        self.assertFalse(any('Relink from folder' in w for w in project['compatibilityWarnings']))

    def test_offline_reasons_are_told_apart(self):
        spec = F.sample()
        spec['media'] += [dict(spec['media'][1], id=f'm-{n}', info=f'ai-{n}', name=f'{n}.wav', file=f'{n}.wav')
                          for n in ('a', 'b', 'c')]
        spec['timelines'][1]['tracks'][1]['items'] += [
            {'id': f'i2-{n}', 'tag': 'Sm2TiAudioClip', 'name': n, 'start': 0, 'duration': 30, 'in': 0, 'media': f'm-{n}'}
            for n in ('a', 'b', 'c')]
        answers = iter([{'offline': 'missing'}, {'offline': 'not-media', 'message': 'cannot decode'},
                        {'offline': 'unchecked', 'message': 'Splice looks for at most 2 files'},
                        {'offline': 'refused', 'message': 'The linked media list is full'}, {'offline': 'missing'}])
        project = resolve.open_drp(F.drp_bytes(spec), 0, lambda paths: [next(answers) for _ in paths])
        warnings = project['compatibilityWarnings']
        self.assertTrue(any(w.startswith('2 media file(s) are offline on this computer: nothing is at the path') for w in warnings))
        self.assertIn('1 file(s) the project names were found on this computer but are not media Splice can play (not '
                      'a media file, or FFmpeg cannot decode it); they stay offline.', warnings)
        self.assertTrue(any(w.startswith('1 media file(s) were not looked for (Splice looks for at most 2 files); they '
                                         'stay offline. Relink them without copying') for w in warnings))
        self.assertIn('1 media file(s) could not be linked (The linked media list is full); they stay offline.', warnings)
        with self.assertRaisesRegex(resolve.ResolveError, 'unexpected result'):
            resolve.open_drp(F.drp_bytes(spec), 0, lambda paths: [])


class TimelineIdTests(Scratch):
    def test_timelines_open_by_id_whatever_their_order(self):
        spec = F.sample()
        first = resolve.open_drp(F.drp_bytes(spec))
        self.assertEqual(first['resolveTimelineIds'], ['tl-1', 'tl-2'])
        spec['timelines'].reverse()
        moved = F.drp_bytes(spec)
        self.assertEqual(resolve.open_drp(moved, 1)['name'], 'Harbor cut')
        self.assertEqual(resolve.open_drp(moved, timeline_id='tl-2')['name'], 'Harbor alt')
        db = self.library(spec)
        self.assertEqual(resolve.open_library_project(db, 0, None, self.dir, timeline_id='tl-1')['name'], 'Harbor cut')
        with self.assertRaisesRegex(resolve.ResolveError, 'no longer in this Resolve project'):
            resolve.open_drp(moved, timeline_id='tl-9')


class ServerTests(Scratch):
    """The /api/resolve/* routes, through ``python3 server.py`` and its request checks."""

    ROUTES = (('/api/resolve/open?timeline=0', 'drp'), ('/api/resolve/library/list', 'list'),
              ('/api/resolve/library/open', 'open'))

    def setUp(self):
        super().setUp()
        from test_media_server import ServerProcess
        self.server = ServerProcess(self.dir / 'data')
        self.addCleanup(self.server.close)

    def post(self, route, body, kind='application/octet-stream', **options):
        status, _, data = self.server.request('POST', route, body, headers={'Content-Type': kind}, **options)
        try:
            return status, json.loads(data or b'{}')
        except ValueError:
            return status, {}

    def bodies(self):
        self.library()
        project = next((self.dir / 'library').rglob('Project.db'))
        return {'drp': (F.drp_bytes(self.spec), 'application/octet-stream'),
                'list': (json.dumps({'path': str(self.dir / 'library')}), 'application/json'),
                'open': (json.dumps({'path': str(project), 'timeline': 0}), 'application/json')}

    def test_resolve_routes_refuse_requests_without_the_session_or_token(self):
        bodies = self.bodies()
        for route, which in self.ROUTES:
            body, kind = bodies[which]
            for label, options in (('no cookie', dict(auth=False)), ('no token', dict(token=False)),
                                   ('cross-site', dict(extra={'Sec-Fetch-Site': 'cross-site'})),
                                   ('other origin', dict(extra={'Origin': 'http://127.0.0.1:1'})),
                                   ('wrong token', dict(extra={'X-Splice-Token': 'x' * 43}))):
                extra = options.pop('extra', {})
                status, _, data = self.server.request('POST', route, body, headers={'Content-Type': kind, **extra},
                                                      **options)
                self.assertEqual(status, 403, (route, label))
                self.assertNotIn(b'Harbor', data, (route, label))
            status, data = self.post(route, body, kind)
            self.assertEqual(status, 200, (route, data))
        self.assertEqual(list((self.dir / 'data' / 'resolve-snapshots').iterdir()), [])

    def test_routes(self):
        status, data = self.post('/api/resolve/open?timeline=1', F.drp_bytes(self.spec))
        self.assertEqual((status, data['project']['name']), (200, 'Harbor alt'))
        status, data = self.post('/api/resolve/open?timelineId=tl-2', F.drp_bytes(self.spec))
        self.assertEqual((status, data['project']['name']), (200, 'Harbor alt'))
        status, data = self.post('/api/resolve/open?timelineId=gone', F.drp_bytes(self.spec))
        self.assertEqual(status, 400)
        self.assertIn('no longer in this Resolve project', data['error'])
        status, data = self.post('/api/resolve/open', b'junk')
        self.assertEqual(status, 400)
        self.assertIn('not a DaVinci Resolve project', data['error'])
        status, data = self.post('/api/resolve/open', b'')
        self.assertEqual(status, 400)
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
        self.assertEqual(self.post('/api/resolve/library/list', json.dumps({'path': 'x' * 70000}),
                                   'application/json')[0], 400)
        self.assertEqual(list((self.dir / 'data' / 'resolve-snapshots').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
