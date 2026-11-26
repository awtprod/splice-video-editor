"""Save to a Resolve disk library: synthetic Resolve 21.1 seeds, edited Splice timelines, the
report of what is not written, the safety rules of placing a project, framing and hostile seeds."""
import copy
import errno
import os
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
import unittest
import unicodedata
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import resolve_fixtures as F  # noqa: E402
from resolve import blobs, codec, effects, importer, items, place, save, source, writer, zstd  # noqa: E402

PIC, SND = 'E:\\Show\\pic.mov', 'E:\\Show\\snd.wav'
HAVE_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


def tree_state(folder):
    state = {}
    for path in sorted(Path(folder).rglob('*')):
        info = path.lstat()
        state[str(path.relative_to(folder))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                                                path.read_bytes() if path.is_file() else None)
    return state


@unittest.skipUnless(HAVE_FFMPEG, 'needs FFmpeg to make media')
class SaveCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = Path(tempfile.mkdtemp(prefix='splice-rw-media-'))
        cls.pic, cls.snd = F.make_media(cls.media_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='splice-rw-')
        self.dir = Path(self.temp.name)
        self.library = self.dir / 'library'
        self.db, self.ids = F.seed_library(self.library, (PIC, str(self.pic)), (SND, str(self.snd)))
        self.kept = self.dir / 'kept.db'
        shutil.copyfile(self.db, self.kept)

    def tearDown(self):
        for path in self.dir.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        self.temp.cleanup()

    def opened(self, db=None):
        model = importer.read(source.load_library(db or self.db, self.dir))
        project = importer.to_splice(model, 0, None, {'format': 'library', 'path': str(db or self.db)})
        for media in project['media']:
            local = {PIC: self.pic, SND: self.snd}.get(media.get('path'))
            if local:
                media.update(src='file:' + str(local), offline=False)
        return project

    def payload(self, project, name='Splice cut', **extra):
        out = {'library': str(self.library), 'name': name, 'timelines': [project],
               'seed': {'kind': 'library', 'sha256': project['resolveSource']['sha256'],
                        'path': project['resolveSource']['path']},
               'pathMap': [{'from': str(self.dir), 'to': 'E:\\Show'}], 'confirm': True}
        out.update(extra)
        return out

    def run_save(self, payload):
        return save.run(payload, originals=lambda kind, sha: self.kept if kind == 'library' else None,
                        scratch=self.dir, resolve_media=lambda src: src[5:])

    def projects(self):
        return self.library / 'Resolve Projects' / 'Users' / 'guest' / 'Projects'

    def clip(self, project, kind, index):
        return [c for c in project['clips'] if c['type'] == kind][index]


class RoundTripTests(SaveCase):
    def test_an_edited_timeline_reopens_as_edited(self):
        project = self.opened()
        fps = project['fps']
        frame = 1 / fps
        v, a = [c for c in project['clips'] if c['type'] == 'video'], [c for c in project['clips'] if c['type'] == 'audio']
        # Trim: the first linked pair loses 2 frames at its head (the in point moves with it).
        for clip in (v[0], a[0]):
            clip['start'] += 2 * frame
            clip['in'] += 2 * frame
            clip['duration'] -= 2 * frame
        # Reorder: the second and third pairs swap places.
        for x, y in ((v[1], v[2]), (a[1], a[2])):
            x_start = x['start']
            y['start'], x['start'] = x_start, x_start + y['duration']
        for clip in (a[1],):
            clip['transition'] = 'none'
        # Disabled picture, a hidden V2, a locked A1.
        v[2]['enabled'] = False
        project['trackState'] = {'video:2': {'hidden': True}, 'audio:-1': {'locked': True}}
        # An inserted pair from media already in the pool, after the cut, linked.
        end = max(c['start'] + c['duration'] for c in project['clips'])
        pic_media = next(m['id'] for m in project['media'] if m.get('path') == PIC)
        snd_media = next(m['id'] for m in project['media'] if m.get('path') == SND)
        project['clips'] += [
            {'id': 'p4', 'type': 'video', 'track': 1, 'start': end, 'duration': 6 * frame, 'in': 4 * frame,
             'mediaId': pic_media,
             'speed': 1, 'linkGroup': 'g4'},
            {'id': 's4', 'type': 'audio', 'track': -1, 'start': end, 'duration': 6 * frame, 'in': 4 * frame,
             'mediaId': snd_media, 'speed': 1, 'linkGroup': 'g4', 'transition': 'dissolve',
             'transitionDuration': 4 * frame, 'transitionBefore': 2 * frame, 'transitionMatchName': 'Constant Power'}]
        # Close the gap the reorder left so the inserted sound meets the clip before it.
        last_a = max((c for c in project['clips'] if c['type'] == 'audio' and c['id'] != 's4'),
                     key=lambda c: c['start'])
        self.assertAlmostEqual(last_a['start'] + last_a['duration'], end)
        # An inserted clip from new media of a verified kind (a 24-bit stereo WAV) on A2.
        new = self.dir / 'new.wav'
        shutil.copyfile(self.snd, new)
        project['media'].append({'id': 'mnew', 'name': 'new.wav', 'src': 'file:' + str(new), 'type': 'audio'})
        project['clips'].append({'id': 'n1', 'type': 'audio', 'track': -2, 'start': 0.0, 'duration': 15 * frame,
                                 'in': 0, 'mediaId': 'mnew', 'speed': 1, 'volume': 50})
        result = self.run_save(self.payload(project, allow={'unproven': True}))
        self.assertTrue(result['written'])
        saved = Path(result['project']['path'])
        again = self.opened(saved)

        def shape(p):
            out = []
            for c in p['clips']:
                frames = [round(c['start'] * fps), round(c['duration'] * fps), round(c['in'] * fps)]
                out.append((c['type'], c['track'], *frames, c.get('enabled', True) is not False,
                            round(c.get('volume', 100), 1) if c['type'] == 'audio' else None))
            return sorted(out)
        self.assertEqual(shape(again), shape(project))
        groups = {}
        for c in again['clips']:
            if c.get('linkGroup'):
                groups.setdefault(c['linkGroup'], set()).add((c['type'], round(c['start'] * fps)))
        self.assertEqual(sorted(sorted(g) for g in groups.values()),
                         sorted(sorted({('video', round(c['start'] * fps)), ('audio', round(c['start'] * fps))})
                                for c in project['clips'] if c['type'] == 'video'))
        fade = next(c for c in again['clips'] if c['type'] == 'audio' and round(c['start'] * fps) == round(end * fps))
        self.assertEqual((fade['transition'], round(fade['transitionDuration'] * fps)), ('dissolve', 4))
        self.assertEqual(again['trackState'], {'video:2': {'hidden': True}, 'audio:-1': {'locked': True}})
        # The new WAV is a media-pool clip of the new project, at the mapped path.
        self.assertIn('E:\\Show\\new.wav', [m.get('path') for m in again['media']])
        # Rows the edit did not touch are the seed's, byte for byte.
        for table in ('Sm2MpFolder', 'BtVideoInfo', 'ListMgt::LmVersionTable', 'Sm2Timeline'):
            self.assertEqual(self.rows(self.kept, table), self.rows(saved, table), table)
        self.assertEqual(result['selfCheck'], 'passed')

    def rows(self, db, table):
        conn = sqlite3.connect(f'file:{db}?mode=ro&immutable=1', uri=True)
        try:
            return sorted(conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
        finally:
            conn.close()

    def test_an_untouched_timeline_saves_unchanged_but_for_its_identity(self):
        result = self.run_save(self.payload(self.opened()))
        saved = Path(result['project']['path'])
        before, after = save.fingerprint(sqlite3.connect(self.kept), self.ids['project']), None
        conn = sqlite3.connect(saved)
        try:
            new_id = conn.execute('SELECT SM_Project_id FROM SM_Project').fetchone()[0]
            after = save.fingerprint(conn, self.ids['project'], new_id)
            name = conn.execute('SELECT ProjectName FROM SM_Project').fetchone()[0]
        finally:
            conn.close()
        self.assertNotEqual(new_id, self.ids['project'])
        self.assertEqual(name, 'Splice cut')
        changed = [t for t in before if before[t] != after[t]]
        self.assertEqual(changed, ['SM_Project'])
        self.assertEqual(result['operations'], [])

    def test_a_deleted_clip_is_removed_and_a_graded_one_kept_and_reported(self):
        shutil.rmtree(self.library)
        self.db, self.ids = F.seed_library(self.library, (PIC, str(self.pic)), (SND, str(self.snd)), with_grade=True)
        shutil.copyfile(self.db, self.kept)
        project = self.opened()
        graded = self.ids['pictures'][0]
        plain_sound = self.ids['sounds'][2]
        project['clips'] = [c for c in project['clips'] if c['resolveItemId'] not in (graded, plain_sound)]
        result = self.run_save(self.payload(project))
        again = self.opened(Path(result['project']['path']))
        ids = {c['resolveItemId'] for c in again['clips']}
        self.assertIn(graded, ids)
        self.assertNotIn(plain_sound, ids)
        lines = {line['what']: line for line in result['lines']}
        kept = next(w for w in lines if w.startswith('Deleted clips that carry Resolve work'))
        self.assertEqual(lines[kept]['status'], writer.NOT_WRITTEN)


class ReportTests(SaveCase):
    def test_everything_not_written_is_listed_per_clip(self):
        project = self.opened()
        fps = project['fps']
        v = [c for c in project['clips'] if c['type'] == 'video']
        v[0]['keyframes'] = {'opacity': [[0, 100], [1, 0]]}
        v[1]['color'] = {'exposure': 0.5}
        v[1]['transform'] = {'x': 0, 'y': 10, 'scale': 120, 'rotation': 0}
        v[2]['fadeIn'] = 0.5
        project['clips'].append({'id': 't1', 'type': 'title', 'track': 1, 'start': 2.0, 'duration': 1.0,
                                 'title': 'Hello'})
        project['clips'].append({'id': 'v3', 'type': 'video', 'track': 3, 'start': 0.0, 'duration': 10 / fps,
                                 'in': 0, 'mediaId': v[0]['mediaId'], 'speed': 1})
        project['clips'].append({'id': 'sp', 'type': 'video', 'track': 2, 'start': 0.0, 'duration': 10 / fps,
                                 'in': 0, 'mediaId': v[0]['mediaId'], 'speed': 2})
        result = self.run_save(self.payload(project, confirm=False))
        self.assertFalse(result['written'])
        self.assertEqual(list((self.projects()).iterdir()), [self.db.parent])
        not_written = {line['what'].split(' (')[0]: line for line in result['lines']
                       if line['status'] == writer.NOT_WRITTEN}
        for what in ('Keyframes', 'Splice color adjustments and LUTs', 'Zoom, vertical position and rotation',
                     'Clip fades', 'Titles and other Splice-only clips',
                     'Clips on tracks the Resolve timeline does not have',
                     'New clips with a speed change or reversed'):
            self.assertIn(what, not_written, sorted(not_written))
            self.assertTrue(not_written[what]['examples'], what)

    def test_unproven_changes_wait_for_the_opt_in(self):
        project = self.opened()
        project['trackState'] = {'video:1': {'locked': True}}
        v = self.clip(project, 'video', 0)
        v['transform'] = {'x': 10.0, 'y': 0, 'scale': 100, 'rotation': 0}
        result = self.run_save(self.payload(project))
        self.assertEqual(result['needs'], ['unproven'])
        again = self.opened(Path(result['project']['path']))
        self.assertEqual(again['trackState'], {})
        self.assertEqual(self.clip(again, 'video', 0).get('transform', {}).get('x', 0), 0)
        shutil.rmtree(Path(result['project']['path']).parent)
        result = self.run_save(self.payload(project, allow={'unproven': True}))
        again = self.opened(Path(result['project']['path']))
        self.assertEqual(again['trackState'], {'video:1': {'locked': True}})
        self.assertAlmostEqual(self.clip(again, 'video', 0)['transform']['x'], 10.0)

    def test_repeated_lines_are_grouped_with_examples(self):
        report = writer.Report()
        for n in range(705):
            report.add(writer.NOT_WRITTEN, 'Keyframes', f'clip {n}')
        (line,) = report.lines()
        self.assertEqual((line['count'], line['examples']), (705, [f'clip {n}' for n in range(5)]))


class SafetyTests(SaveCase):
    def test_an_existing_name_is_refused_in_any_case_or_normal_form(self):
        before = tree_state(self.library)
        project = self.opened()
        for name in ('Seed project', 'SEED PROJECT', 'seed project'):
            with self.assertRaises(place.PlaceError):
                self.run_save(self.payload(project, name=name))
        accented = self.projects() / unicodedata.normalize('NFD', 'Café')
        accented.mkdir()
        with self.assertRaises(place.PlaceError):
            self.run_save(self.payload(project, name='CAFÉ'))
        accented.rmdir()
        self.assertEqual(tree_state(self.library), before)

    def test_names_the_file_system_or_windows_cannot_hold_are_refused(self):
        project = self.opened()
        for name in ('a/b', 'a\\b', '..', 'x:y', 'CON', 'com1.txt', 'trail.', 'trail ', ' lead', '.hidden', 'a\x00b',
                     'tab\there', 'x' * 201, 'Project.db', '', 'a*b', 'q?', '<>', 'zero\u200bwidth',
                     unicodedata.normalize('NFD', 'Café')):
            with self.subTest(name=name), self.assertRaises((place.PlaceError, source.ResolveError)):
                self.run_save(self.payload(project, name=name))
        self.assertEqual([p.name for p in self.projects().iterdir()], ['Seed project'])

    def test_a_failure_leaves_nothing_in_the_library(self):
        before = tree_state(self.library)
        project = self.opened()
        with mock.patch.object(writer.Writer, 'apply', side_effect=writer.WriteError('injected')):
            with self.assertRaises(writer.WriteError):
                self.run_save(self.payload(project))
        self.assertEqual(tree_state(self.library), before)
        with mock.patch.object(save, '_copy_seed', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            with self.assertRaises(OSError):
                self.run_save(self.payload(project))
        self.assertEqual(tree_state(self.library), before)
        with mock.patch.object(save, 'read_back', return_value=['a planted difference']):
            with self.assertRaises(writer.WriteError) as caught:
                self.run_save(self.payload(project))
        self.assertIn('a planted difference', str(caught.exception))
        self.assertEqual(tree_state(self.library), before)

    def test_the_project_is_staged_beside_and_moved_in_with_one_rename(self):
        moves = []
        real = place.move_no_replace

        def spy(folder, target):
            self.assertTrue(Path(folder).is_dir())
            self.assertEqual(Path(folder).parent.parent.name, place.STAGING)
            self.assertEqual(os.stat(folder).st_dev, os.stat(Path(target).parent).st_dev)
            moves.append((folder, target))
            return real(folder, target)
        with mock.patch.object(place, 'move_no_replace', side_effect=spy):
            result = self.run_save(self.payload(self.opened()))
        self.assertEqual(len(moves), 1)
        self.assertFalse((self.library / 'Resolve Projects' / place.STAGING).exists())
        self.assertTrue(Path(result['project']['path']).is_file())

    def test_the_rename_never_replaces_a_project(self):
        target = self.projects() / 'Taken'
        target.mkdir()
        (target / 'Project.db').write_bytes(b'theirs')
        folder = self.dir / 'mine'
        folder.mkdir()
        (folder / 'Project.db').write_bytes(b'mine')
        with self.assertRaises(FileExistsError):
            place.move_no_replace(folder, target)
        self.assertEqual((target / 'Project.db').read_bytes(), b'theirs')
        with mock.patch.object(place, '_renameat2', return_value=None):
            with self.assertRaises(FileExistsError):
                place.move_no_replace(folder, target)
        self.assertEqual((target / 'Project.db').read_bytes(), b'theirs')

    def test_symlinked_folders_out_of_the_library_are_refused(self):
        outside = self.dir / 'outside'
        projects = self.projects()
        shutil.move(str(projects), str(outside))
        projects.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(place.PlaceError):
            self.run_save(self.payload(self.opened(outside / 'Seed project' / 'Project.db')))
        self.assertEqual([p.name for p in outside.iterdir()], ['Seed project'])
        projects.unlink()
        shutil.move(str(outside), str(projects))
        users = self.library / 'Resolve Projects' / 'Users'
        moved = self.dir / 'users-elsewhere'
        shutil.move(str(users), str(moved))
        users.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(place.PlaceError):
            place.library_users(self.library)

    def test_a_folder_that_is_not_a_library_is_refused(self):
        plain = self.dir / 'plain'
        plain.mkdir()
        with self.assertRaises(place.PlaceError):
            self.run_save(dict(self.payload(self.opened()), library=str(plain)))
        self.assertEqual(list(plain.iterdir()), [])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes anywhere')
    def test_a_read_only_library_is_refused(self):
        before = tree_state(self.library)
        mode = stat.S_IMODE(self.projects().stat().st_mode)
        self.projects().chmod(0o555)
        try:
            with self.assertRaises(place.PlaceError):
                self.run_save(self.payload(self.opened()))
        finally:
            self.projects().chmod(mode)
        self.assertEqual(tree_state(self.library), before)

    def test_the_database_keeps_the_seed_layout_and_leaves_no_journal(self):
        with sqlite3.connect(self.kept) as conn:
            page, encoding = conn.execute('PRAGMA page_size').fetchone()[0], conn.execute('PRAGMA encoding').fetchone()[0]
        result = self.run_save(self.payload(self.opened()))
        saved = Path(result['project']['path'])
        conn = sqlite3.connect(f'file:{saved}?mode=ro', uri=True)
        try:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(conn.execute('PRAGMA page_size').fetchone()[0], page)
            self.assertEqual(conn.execute('PRAGMA encoding').fetchone()[0], encoding)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        finally:
            conn.close()
        self.assertEqual(sorted(p.name for p in saved.parent.iterdir()), ['Project.db'])

    def test_modes_follow_the_library_projects(self):
        self.db.parent.chmod(0o750)
        self.db.chmod(0o640)
        result = self.run_save(self.payload(self.opened()))
        saved = Path(result['project']['path'])
        self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o640)
        self.assertEqual((saved.stat().st_uid, saved.stat().st_gid), (self.db.stat().st_uid, self.db.stat().st_gid))

    def test_two_saves_of_one_name_one_wins_the_other_fails_cleanly(self):
        project = self.opened()
        entered, release = threading.Event(), threading.Event()
        real_apply = writer.Writer.apply
        outcome = {}

        def slow_apply(self_, name):
            entered.set()
            release.wait(30)
            return real_apply(self_, name)

        def first():
            try:
                outcome['first'] = self.run_save(self.payload(copy.deepcopy(project), name='Race'))
            except Exception as error:  # noqa: BLE001 - recorded for the assertion
                outcome['first'] = error
        with mock.patch.object(writer.Writer, 'apply', slow_apply):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(entered.wait(60))
            with self.assertRaises(place.PlaceError) as caught:
                self.run_save(self.payload(copy.deepcopy(project), name='RACE'))
            self.assertIn('Another save', str(caught.exception))
            release.set()
            thread.join(60)
        self.assertTrue(outcome['first']['written'])
        self.assertEqual(sorted(p.name for p in self.projects().iterdir()), ['Race', 'Seed project'])
        self.assertFalse((self.library / 'Resolve Projects' / place.STAGING).exists())


class HostileSeedTests(SaveCase):
    def replace_seed(self, change):
        conn = sqlite3.connect(self.kept)
        try:
            change(conn)
            conn.commit()
        finally:
            conn.close()

    def test_another_resolve_version_is_refused(self):
        self.replace_seed(lambda c: c.execute('UPDATE SM_Project SET ProjectVersion = 15'))
        with self.assertRaises(writer.WriteError) as caught:
            self.run_save(self.payload(self.opened()))
        self.assertIn('version 15', str(caught.exception))

    def test_a_foreign_trigger_is_refused(self):
        self.replace_seed(lambda c: c.execute('CREATE TRIGGER planted AFTER UPDATE ON Sm2TiItem BEGIN SELECT 1; END'))
        with self.assertRaises(writer.WriteError):
            self.run_save(self.payload(self.opened()))

    def test_a_damaged_seed_is_refused_and_nothing_is_left(self):
        before = tree_state(self.library)
        data = bytearray(self.kept.read_bytes())
        data[4096:8192] = os.urandom(4096)
        self.kept.write_bytes(bytes(data))
        with self.assertRaises((source.ResolveError, sqlite3.DatabaseError)):
            self.run_save(self.payload(self.opened()))
        self.assertEqual(tree_state(self.library), before)

    def test_a_seed_larger_than_splice_reads_is_refused(self):
        with mock.patch.object(source, 'MAX_DB_BYTES', 1024):
            with self.assertRaises(source.ResolveError):
                self.run_save(self.payload(self.opened()))

    def test_an_item_table_of_other_columns_is_refused(self):
        self.replace_seed(lambda c: c.execute('ALTER TABLE Sm2TiItem ADD COLUMN NewThing TEXT'))
        with self.assertRaises(writer.WriteError):
            self.run_save(self.payload(self.opened()))

    def test_overlapping_clips_are_refused(self):
        project = self.opened()
        v = [c for c in project['clips'] if c['type'] == 'video']
        v[1]['start'] = v[0]['start'] + 1 / project['fps']
        with self.assertRaises(writer.WriteError):
            self.run_save(self.payload(project))

    def test_five_thousand_clips(self):
        project = self.opened()
        fps = project['fps']
        pic = next(m['id'] for m in project['media'] if m.get('path') == PIC)
        project['clips'] = [{'id': f'c{n}', 'type': 'video', 'track': 2, 'start': n / fps, 'duration': 1 / fps,
                             'in': n % 20 / fps, 'mediaId': pic, 'speed': 1} for n in range(5000)]
        result = self.run_save(self.payload(project))
        again = self.opened(Path(result['project']['path']))
        self.assertEqual(len(again['clips']), 5000)


class TemplateTests(SaveCase):
    def test_a_splice_project_saves_into_a_template_without_its_media(self):
        project = self.opened()
        fps = project['fps']
        for key in ('resolveSource', 'resolveTimelineIds', 'resolveTimelines'):
            project.pop(key, None)
        new = self.dir / 'take.wav'
        shutil.copyfile(self.snd, new)
        project['media'] = [{'id': 'm9', 'name': 'take.wav', 'src': 'file:' + str(new), 'type': 'audio'},
                            next(dict(m, resolveMediaId=m['resolveMediaId']) for m in self.opened()['media']
                                 if m.get('path') == SND)]
        project['clips'] = [{'id': 'a', 'type': 'audio', 'track': -1, 'start': 0.0, 'duration': 30 / fps, 'in': 0,
                             'mediaId': 'm9', 'speed': 1}]
        project['resolveSource'] = {'sha256': '0' * 64, 'path': ''}
        payload = self.payload(project, seed={'kind': 'template', 'path': str(self.db)})
        result = self.run_save(payload)
        saved = Path(result['project']['path'])
        again = self.opened(saved)
        self.assertEqual([(c['type'], c['track'], round(c['duration'] * fps)) for c in again['clips']],
                         [('audio', -1, 30)])
        self.assertEqual([m.get('path') for m in again['media']], ['E:\\Show\\take.wav'])
        conn = sqlite3.connect(f'file:{saved}?mode=ro&immutable=1', uri=True)
        try:
            names = [r[0] for r in conn.execute("SELECT Name FROM Sm2MpMedia WHERE DbType != 'Sm2MpTimelineClip'")]
        finally:
            conn.close()
        self.assertEqual(names, ['take.wav'])


class FramingTests(unittest.TestCase):
    def test_messages_of_up_to_64_bytes_are_plain_and_longer_ones_zstd(self):
        for size in (0, 1, 63, 64):
            blob = codec.envelope(bytes(size))
            self.assertEqual(blob[8], 0x80, size)
            self.assertEqual(blobs.unwrap(blob, blobs.Budget()), bytes(size))
        with self.assertRaises(codec.CodecError):
            codec.envelope(bytes(65))
        for size in (66, 200, 70_000, 300_000):
            payload = os.urandom(size)
            blob = codec.envelope(payload)
            self.assertEqual(blob[8], 0x81)
            self.assertEqual(struct.unpack('>II', blob[:8]), (2, len(blob) - 8))
            frame = blob[9:]
            self.assertEqual(zstd.frame_length(frame), len(frame))
            self.assertEqual(zstd.decompress_python(frame, size), payload)
            self.assertEqual(blobs.unwrap(blob, blobs.Budget()), payload)

    @unittest.skipUnless(zstd.backend_name() == 'compression.zstd', 'needs Python 3.14 compression.zstd')
    def test_the_standard_library_reads_the_frames(self):
        from compression import zstd as stdlib
        for size in (0, 66, 255, 256, 65791, 65792, 131072, 131073, 500_000):
            payload = os.urandom(size)
            self.assertEqual(stdlib.decompress(zstd.raw_frame(payload)), payload)

    def test_a_frame_with_trailing_bytes_or_cut_short_is_refused(self):
        frame = zstd.raw_frame(b'x' * 100)
        for bad in (frame + b'\x00', frame[:-1], frame[:5]):
            with self.assertRaises(zstd.ZstdError):
                zstd.frame_length(bad)

    def test_rewrap_keeps_the_version_and_picks_the_codec_by_length(self):
        original = codec.envelope(b'\x08\x01', version=10001)
        self.assertEqual(codec.parse_envelope(codec.rewrap(original, bytes(100), 'x'))[:2], (10001, 'zstd'))
        self.assertEqual(codec.parse_envelope(codec.rewrap(original, bytes(10), 'x'))[:2], (10001, 'protobuf'))

    def test_keyed_dicts_encode_back_to_their_bytes(self):
        data = F.kd_typed([('A', 0x02, -5), ('B', 0x03, 7), ('C', 0x04, 1 << 40), ('D', 0x06, 0.5), ('E', 0x01, 1),
                           ('F', 0x0A, 'text'), ('G', 0x0C, b'\x00\x01')])
        entries = blobs.keyed_entries(data)
        self.assertEqual(codec.kd_encode(entries), data)
        with self.assertRaises(codec.CodecError):
            codec.kd_set(entries, 'A', 1 << 40, 'test')

    def test_the_calibrated_effects_bytes(self):
        # Resolve 21.1, one setting changed on a clip without effects (calibration).
        self.assertEqual(effects.single(effects.AUDIO_VOLUME, effects.VOLUME_DB, 6.0).hex(),
                         '0000000200000020800a1d087c38004a0f085f1a0b0a091100000000000018404a004a004a004a00')
        self.assertEqual(effects.single(effects.TRANSFORM, effects.POSITION_X, 200 / 1920).hex(),
                         '000000020000002e800a2b080438004a004a004a004a0f08281a0b0a0911abaaaaaaaaaaba3f4a004a004a004a00'
                         '4a004a004a004a00')
        blob = effects.single(effects.AUDIO_VOLUME, effects.VOLUME_DB, 6.0)
        changed = effects.replace(blob, effects.AUDIO_VOLUME, effects.VOLUME_DB, -3.0)
        decoded = effects.decode(codec.unwrap(changed, 'x'))
        self.assertEqual(decoded[0][1], {effects.VOLUME_DB: -3.0})

    def test_the_sound_transition_constant_inflates_to_its_value(self):
        self.assertTrue(items.transition_payload_ok())


if __name__ == '__main__':
    unittest.main()


class PathMappingTests(unittest.TestCase):
    def test_the_longest_rule_wins_whatever_the_order(self):
        from resolve import facts
        rules = facts.check_mappings([{'from': '/mnt', 'to': 'E:\\'}, {'from': '/mnt/media', 'to': 'F:\\Footage'}])
        self.assertEqual(facts.map_path('/mnt/media/x.mov', rules), 'F:\\Footage\\x.mov')
        self.assertEqual(facts.map_path('/mnt/media/x.mov', list(reversed(rules))), 'F:\\Footage\\x.mov')
        self.assertEqual(facts.map_path('/mnt/mediax/y.mov', rules), 'E:\\mediax\\y.mov')
        self.assertEqual(facts.map_path('/mnt/other/z.wav', rules), 'E:\\other\\z.wav')
        self.assertEqual(facts.map_path('/srv/z.wav', rules), '/srv/z.wav')
        self.assertEqual(facts.mapped('/mnt/media/a/b.mov', rules), ('F:\\Footage\\a\\b.mov', 1))
        self.assertEqual(facts.map_path('/mnt/media/x.mov', facts.check_mappings(
            [{'from': '/mnt/media', 'to': '\\\\nas\\share'}])), '\\\\nas\\share\\x.mov')

    def test_rules_must_be_absolute_paths(self):
        from resolve import facts
        for rule in ({'from': 'media', 'to': 'E:\\'}, {'from': '/mnt', 'to': 'E:'}, {'from': '/mnt', 'to': ''}):
            with self.assertRaises(ValueError):
                facts.check_mappings([rule])


class LibraryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='splice-rw-lib-')
        self.dir = Path(self.temp.name)
        self.library = self.dir / 'library'
        self.user = self.library / 'Resolve Projects' / 'Users' / 'guest'
        (self.user / 'Projects').mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def refused(self):
        with self.assertRaises(place.PlaceError) as caught:
            place.library_users(self.library)
        return str(caught.exception)

    def test_a_crafted_empty_library_is_refused(self):
        self.assertIn('Create the library in Resolve first', self.refused())
        (self.user / 'User.db').write_text('not a database')
        (self.user / 'Projects' / 'Fake').mkdir()
        (self.user / 'Projects' / 'Fake' / 'Project.db').write_bytes(b'SQLite format 3\x00' + bytes(1000))
        self.assertIn('Create the library in Resolve first', self.refused())
        conn = sqlite3.connect(self.user / 'Projects' / 'Fake' / 'Project.db.tmp')
        conn.execute('CREATE TABLE Other (x)')
        conn.close()
        os.replace(self.user / 'Projects' / 'Fake' / 'Project.db.tmp', self.user / 'Projects' / 'Fake' / 'Project.db')
        self.assertIn('Create the library in Resolve first', self.refused())

    def test_resolve_user_database_or_a_resolve_project_is_evidence(self):
        conn = sqlite3.connect(self.user / 'User.db')
        conn.execute('CREATE TABLE SM_User (SM_User_id)')
        conn.close()
        self.assertEqual(sorted(place.library_users(self.library)[2]), ['guest'])
        (self.user / 'User.db').unlink()
        folder = self.user / 'Projects' / 'Real'
        folder.mkdir()
        conn = sqlite3.connect(folder / 'Project.db')
        conn.execute('CREATE TABLE SM_Project (SM_Project_id)')
        conn.close()
        self.assertEqual(sorted(place.library_users(self.library)[2]), ['guest'])
        # A linked User.db is not evidence.
        (folder / 'Project.db').rename(self.dir / 'elsewhere.db')
        (self.user / 'User.db').symlink_to(self.dir / 'elsewhere.db')
        self.refused()

    def test_project_db_inside_accepts_only_project_files_of_the_library(self):
        projects = self.user / 'Projects'
        (projects / 'A').mkdir()
        (projects / 'A' / 'Project.db').write_bytes(b'x')
        inside = place.project_db_inside(projects / 'A' / 'Project.db', projects)
        self.assertEqual(inside, (projects / 'A' / 'Project.db').resolve())
        (self.dir / 'B').mkdir()
        (self.dir / 'B' / 'Project.db').write_bytes(b'x')
        (projects / 'B').symlink_to(self.dir / 'B', target_is_directory=True)
        (projects / 'A' / 'other.db').write_bytes(b'x')
        for path in ('/etc/passwd', '/etc/Project.db', str(projects / 'B' / 'Project.db'), 'Project.db',
                     str(projects / 'A' / 'other.db'), str(projects / 'A' / '..' / 'A' / 'Project.db'),
                     str(projects / 'Missing' / 'Project.db'), str(projects / 'Project.db')):
            self.assertIsNone(place.project_db_inside(path, projects), path)


class SeedPathTests(SaveCase):
    def test_the_live_seed_path_is_read_only_inside_the_library_and_refused_alike_otherwise(self):
        project = self.opened()
        no_kept = lambda kind, sha: None  # noqa: E731
        answers = set()
        secret = self.dir / 'secret.txt'
        secret.write_text('x')
        for path in ('/etc/passwd', str(secret), str(self.dir / 'missing' / 'Project.db'), str(self.dir),
                     str(self.kept)):
            payload = self.payload(project, confirm=False)
            payload['seed']['path'] = path
            with self.assertRaises(source.ResolveError) as caught:
                save.run(payload, originals=no_kept, scratch=self.dir, resolve_media=lambda src: src[5:])
            answers.add(str(caught.exception))
        self.assertEqual(len(answers), 1, answers)
        # The project's own Project.db in the library still works when the kept copy is gone.
        result = save.run(self.payload(project, confirm=False), originals=no_kept, scratch=self.dir,
                          resolve_media=lambda src: src[5:])
        self.assertTrue(result['ok'])


class StaleStagingTests(SaveCase):
    CHILD = ('import sys, time\nsys.path.insert(0, {root!r})\nfrom resolve import place\n'
             's = place.Staging({library!r}, {name!r})\ns.db.write_bytes(b"x" * 4096)\n'
             'print("ready", flush=True)\ntime.sleep(60)\n')

    def killed_save(self, name):
        import subprocess
        code = self.CHILD.format(root=str(ROOT), library=str(self.library), name=name)
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        child.stdout.close()
        return child

    def staging(self):
        return self.library / 'Resolve Projects' / place.STAGING

    def age(self, seconds):
        for path in self.staging().iterdir():
            info = path.lstat()
            os.utime(path, (info.st_atime - seconds, info.st_mtime - seconds), follow_symlinks=False)

    def test_what_a_killed_save_left_is_removed_by_the_next_save(self):
        before = tree_state(self.library)
        root = self.library / 'Resolve Projects'
        child = self.killed_save('Killed one')
        try:
            self.age(3600)
            # A live save's staging is never removed, however old.
            self.assertEqual(place.clean_stale(root), [])
            self.assertEqual(len(list(self.staging().iterdir())), 2)
        finally:
            child.kill()
            child.wait()
        # Killed: the next save removes the old pair when it starts; a young pair stays until it
        # is older than the safe age ...
        child = self.killed_save('Killed two')
        child.kill()
        child.wait()
        self.assertEqual(len(list(self.staging().iterdir())), 2)
        self.assertEqual(place.clean_stale(root), [])
        self.assertEqual(len(list(self.staging().iterdir())), 2)
        # ... a link with Splice's naming pointing out of the library is neither followed nor removed.
        outside = self.dir / 'outside'
        (outside / 'keep').mkdir(parents=True)
        (self.staging() / ('build-' + 'a' * 16)).symlink_to(outside, target_is_directory=True)
        self.age(place.STALE_SECONDS + 1)
        result = self.run_save(self.payload(self.opened(), name='After the kills'))
        self.assertTrue(result['written'])
        self.assertTrue((outside / 'keep').is_dir())
        self.assertEqual(sorted(p.name for p in self.staging().iterdir()), ['build-' + 'a' * 16])
        (self.staging() / ('build-' + 'a' * 16)).unlink()
        place.clean_stale(root)
        self.assertFalse(self.staging().exists())
        after = tree_state(self.library)
        added = sorted(set(after) - set(before))
        self.assertEqual(added, ['Resolve Projects/Users/guest/Projects/After the kills',
                                 'Resolve Projects/Users/guest/Projects/After the kills/Project.db'])
        self.assertEqual({k: after[k] for k in before if not k.endswith('Resolve Projects')},
                         {k: before[k] for k in before if not k.endswith('Resolve Projects')})
        self.assertTrue(any('interrupted save' in note for note in result['notes']))

    def test_a_second_save_of_the_same_name_waits_for_a_live_one(self):
        child = self.killed_save('Same name')
        try:
            self.age(3600)
            with self.assertRaises(place.PlaceError) as caught:
                place.Staging(self.library, 'Same name')
            self.assertIn('is running', str(caught.exception))
        finally:
            child.kill()
            child.wait()
        self.age(3600)
        staging = place.Staging(self.library, 'Same name')
        staging.close()
        self.assertFalse(self.staging().exists())

    def test_the_server_cleans_the_libraries_it_saved_into_when_it_starts(self):
        from resolve import http as resolve_http
        child = self.killed_save('Killed')
        child.kill()
        child.wait()
        self.age(3600)
        with mock.patch('resolve.http._remembered_file', return_value=self.dir / 'known.json'):
            resolve_http.remember_library(self.library / 'Resolve Projects')
            resolve_http.remember_library(self.dir / 'gone' / 'Resolve Projects')
            resolve_http.clean_staging_at_start().join(10)
        self.assertFalse(self.staging().exists())


class TransitionChangeTests(SaveCase):
    def carrier(self, project):
        return next(c for c in project['clips'] if c.get('resolveTransitionId'))

    def transitions(self, db):
        conn = sqlite3.connect(f'file:{db}?mode=ro&immutable=1', uri=True)
        try:
            return sorted(conn.execute("SELECT Start, Duration, PrettyType FROM Sm2TiItem WHERE DbType = "
                                       "'Sm2TiTransition'").fetchall())
        finally:
            conn.close()

    def test_a_changed_resolve_crossfade_is_written_anew_or_reported(self):
        project = self.opened()
        fps = project['fps']
        carrier = self.carrier(project)
        seed = self.transitions(self.db)
        cut = round(carrier['start'] * fps) + project['startFrame']
        carrier.update(transitionDuration=12 / fps, transitionBefore=6 / fps)
        result = self.run_save(self.payload(project, name='Length'))
        got = self.transitions(result['project']['path'])
        self.assertEqual(len(got), len(seed))
        self.assertIn((str(cut - 6), '12', 'Cross Fade +3DB'), got)
        self.assertTrue(any(line['what'].startswith('Resolve sound crossfades changed') for line in result['lines']))
        again = self.carrier(self.opened(Path(result['project']['path'])))
        self.assertEqual(round(again['transitionDuration'] * fps), 12)
        # The kind.
        project = self.opened()
        carrier = self.carrier(project)
        kind = 'Constant Gain' if carrier['transitionMatchName'] == 'Constant Power' else 'Constant Power'
        carrier['transitionMatchName'] = kind
        result = self.run_save(self.payload(project, name='Kind'))
        self.assertEqual(self.carrier(self.opened(Path(result['project']['path'])))['transitionMatchName'], kind)
        # Outside the evidence: the Resolve crossfade stays as it was, and the report says so.
        project = self.opened()
        self.carrier(project).update(transitionDuration=18 / fps, transitionBefore=9 / fps)
        result = self.run_save(self.payload(project, name='Too long'))
        self.assertEqual(self.transitions(result['project']['path']), seed)
        line = next(line for line in result['lines'] if line['what'].startswith('Changes to Resolve transitions'))
        self.assertEqual(line['status'], writer.NOT_WRITTEN)

    def test_a_crossfade_without_its_resolve_id_never_adds_a_second_one(self):
        project = self.opened()
        fps = project['fps']
        carrier = self.carrier(project)
        seed = self.transitions(self.db)
        carrier.pop('resolveTransitionId')
        result = self.run_save(self.payload(project, name='Same'))
        self.assertEqual(self.transitions(result['project']['path']), seed)
        project = self.opened()
        carrier = self.carrier(project)
        carrier.pop('resolveTransitionId')
        carrier.update(transitionDuration=8 / fps, transitionBefore=4 / fps)
        result = self.run_save(self.payload(project, name='Other'))
        self.assertEqual(self.transitions(result['project']['path']), seed)
        self.assertTrue(any(line['what'].startswith('Transitions on a cut that keeps a Resolve transition')
                            for line in result['lines']))

    def test_the_self_check_compares_transitions_per_cut(self):
        project = self.opened()
        original = writer.Writer._transitions

        def doubled(self_, placed, timeline, start, name):
            # The writer's old fault: a second crossfade written on a cut that keeps one.
            out = original(self_, placed, timeline, start, name)
            first = next(t for t in out if t['kind'] == 'audio')
            row, _join = items.author_transition('Cross Fade +3 dB', first['track'], 0,
                                                 first['start'] + items.frames_before(4), 4, timeline.rate,
                                                 self_.fresh())
            self_.inserts.append(('Sm2TiItem', row))
            return out + [{'id': row['Sm2TiItem_id'], 'track': first['track'], 'start': first['start'],
                           'duration': 4, 'seed': False, 'kind': 'audio', 'pretty': row['PrettyType']}]
        before = tree_state(self.library)
        with mock.patch.object(writer.Writer, '_transitions', doubled):
            with self.assertRaises(writer.WriteError) as caught:
                self.run_save(self.payload(project, name='Doubled'))
        self.assertIn('overlap', str(caught.exception))
        self.assertEqual(tree_state(self.library), before)
