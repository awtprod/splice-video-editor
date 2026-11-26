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
        for what in ('Keyframes', 'Splice color adjustments', 'Zoom, vertical position and rotation',
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


class RoundingReportTests(SaveCase):
    def test_rounding_is_reported_only_where_a_rounded_value_is_written(self):
        last = self.ids['pictures'][-1]
        conn = sqlite3.connect(self.db)
        start = conn.execute('SELECT Start FROM Sm2TiItem WHERE Sm2TiItem_id = ?', (last,)).fetchone()[0]
        conn.execute('UPDATE Sm2TiItem SET Start = ? WHERE Sm2TiItem_id = ?',
                     (f'{start}|' + struct.pack('<d', 0.25).hex(), last))
        conn.commit()
        conn.close()
        shutil.copyfile(self.db, self.kept)
        project = self.opened()
        result = self.run_save(self.payload(project, confirm=False))
        self.assertNotIn(writer.ROUNDED, [line['what'] for line in result['lines']])
        clip = next(c for c in project['clips'] if c['resolveItemId'] == last)
        clip['start'] += 10 / project['fps']
        result = self.run_save(self.payload(project, confirm=False))
        line = next(line for line in result['lines'] if line['what'] == writer.ROUNDED)
        self.assertEqual(line['count'], 1)


def _calls(source, name):
    """The argument lists of every call ``name(...)`` in JavaScript ``source`` (top-level commas)."""
    out = []
    for match in __import__('re').finditer(r'\b' + name + r'\(', source):
        depth, args, current, i, quote = 1, [], '', match.end(), None
        while i < len(source) and depth:
            ch = source[i]
            if quote:
                current += ch
                if ch == '\\':
                    current += source[i + 1]
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in '\'"`':
                quote = ch
                current += ch
            elif ch in '([{':
                depth += 1
                current += ch
            elif ch in ')]}':
                depth -= 1
                if depth:
                    current += ch
            elif ch == ',' and depth == 1:
                args.append(current.strip())
                current = ''
            else:
                current += ch
            i += 1
        args.append(current.strip())
        out.append(args)
    return out


class EditorFieldCoverageTests(unittest.TestCase):
    """Every field the editor can set has a line in the writer's field tables (written,
    reported, derived or bookkeeping): a field the editor gains later fails here until the writer
    says what it does with it."""

    def test_every_field_the_editor_edits_is_known_to_the_writer(self):
        import re
        engine = (ROOT / 'engine.js').read_text()
        inspector = (ROOT / 'inspector.js').read_text()
        validate = engine[engine.index('function validateProject'):engine.index('function mergeTitleTrackStates')]
        unknown = []

        def need(table, names, what):
            unknown.extend(f'{what}.{n}' for n in sorted(set(names)) if n not in table)
        need(writer.PROJECT_FIELDS, re.findall(r'value\.(\w+)', validate), 'project')
        need(writer.PROJECT_FIELDS, re.findall(r'\bproject\.(\w+)\s*=(?!=)', engine), 'project')
        need(writer.CLIP_FIELDS, re.findall(r'\bclip\.(\w+)', validate), 'clip')
        need(writer.MEDIA_FIELDS, re.findall(r'\bitem\.(\w+)', validate), 'media')
        need(writer.TRACK_STATE_FIELDS, re.findall(r'\bstate\.(\w+)', validate), 'trackState')
        mix = engine[engine.index('function validateTrackMix'):engine.index('function validateProject')]
        need(writer.TRACK_MIX_FIELDS, re.findall(r'\bmix\.(\w+)', mix), 'trackMix')
        need(writer.CLIP_FIELDS, re.findall(r'\b(?:clip|sound|picture|piece)\.(\w+)\s*=(?!=)', engine), 'clip')
        for listed in re.findall(r'for \(const key of \[([^\]]*)\]\)', engine):
            need(writer.CLIP_FIELDS, re.findall(r'"(\w+)"', listed), 'clip')
        groups = {'': writer.CLIP_FIELDS, 'color': writer.COLOR_FIELDS, 'transform': writer.TRANSFORM_FIELDS,
                  'trackMix': writer.TRACK_MIX_FIELDS, 'titleStyle': None}
        for name, group_at in (('label', 7), ('field', 6)):
            for args in _calls(inspector, name):
                if len(args) < 2 or not re.fullmatch(r"'\w+'", args[1]):
                    continue
                key = args[1].strip("'")
                group = args[group_at].strip("'") if len(args) > group_at else ''
                if key == 'speedPercent':
                    key = 'speed'
                if group not in groups:
                    unknown.append(f'inspector group {group}')
                elif groups[group] is not None:
                    need(groups[group], [key], group or 'clip')
        for key, group in re.findall(r'data-field="(\w+)"(?: data-group="(\w+)")?', inspector):
            if group not in groups:
                unknown.append(f'inspector group {group}')
            elif groups[group] is not None:
                need(groups[group], [key], group or 'clip')
        for listed in re.findall(r"\['opacity','x','y','scale','rotation'[^\]]*\]|\['volume'\]|\['opacity'\]", inspector):
            need(writer.KEYFRAME_FIELDS, re.findall(r"'(\w+)'", listed), 'keyframes')
        self.assertEqual(unknown, [])


class EveryFeatureTests(SaveCase):
    """One Splice timeline with one instance of every editable feature: each is written and read
    back, or reported."""

    def test_every_editable_feature_is_written_or_reported(self):
        project = self.opened()
        fps = project['fps']
        fr = 1 / fps
        v = [c for c in project['clips'] if c['type'] == 'video']
        a = [c for c in project['clips'] if c['type'] == 'audio']
        carrier = next(c for c in a if c.get('resolveTransitionId'))
        others = [c for c in a if c is not carrier]
        # Written exactly (or with the opt-in, allowed below).
        project['name'] = 'Renamed cut'
        project['trackNames'] = {'video:1': 'Pictures'}
        project['startTimecode'] = project['startTimecode'] + 30
        others[0]['volume'] = 50
        v[2]['enabled'] = False
        v[0]['transform'] = {'x': 10.0, 'y': 5.0, 'scale': 120, 'rotation': 15}
        project['trackState'] = {'video:2': {'hidden': True, 'syncLocked': False}, 'audio:-2': {'locked': True}}
        project['trackMix'] = {'-1': {'mute': True, 'gain': 150, 'pan': 20, 'solo': True}}
        carrier.update(transitionDuration=8 * fr, transitionBefore=4 * fr)
        # Reported.
        v[0]['keyframes'] = {k: [{'time': 0, 'value': 1}, {'time': 0.1, 'value': 2}]
                             for k in ('opacity', 'x', 'y', 'scale', 'rotation')}
        others[1]['keyframes'] = {'volume': [{'time': 0, 'value': 50}]}
        v[1]['color'] = {'brightness': 110, 'contrast': 90, 'saturation': 80, 'exposure': 0.5, 'temperature': 10,
                         'tint': -5, 'lut': {'size': 2, 'name': 'x.cube'}, 'lutIntensity': 50}
        v[1]['opacity'] = 50
        v[1]['fadeIn'] = 0.2
        others[1]['fadeOut'] = 0.2
        others[1]['pan'] = -30
        v[1]['speed'] = 2
        v[0]['reverse'] = True
        v[1]['nativeTimeRemap'] = {'points': [0, 1]}
        v[1]['transition'], v[1]['transitionDuration'], v[1]['transitionBefore'] = 'dissolve', 4 * fr, 2 * fr
        v[2]['transition'], v[2]['transitionDuration'] = 'fade', 4 * fr
        v[0]['markers'] = [{'time': 0.1, 'name': 'clip marker'}]
        v[0]['title'] = 'Renamed clip'
        v[0]['embeddedAudio'] = True
        others[1]['enabled'] = False
        others[0]['audioStream'] = 1
        project['markers'] = (project.get('markers') or []) + [{'id': 'm', 'time': 0.5, 'name': 'Marker'}]
        project['captions'] = [{'id': 'cap', 'start': 0, 'duration': 1, 'text': 'hello'}]
        project['width'], project['height'] = 3840, 2160
        project['dropFrame'] = not project.get('dropFrame')
        project['marks'] = {'in': 0.1, 'out': 0.5}
        project['trackCounts'] = {'video': project['trackCounts']['video'] + 1,
                                  'audio': project['trackCounts']['audio'] + 1}
        project['media'][0]['name'] = 'Renamed media'
        end = max(c['start'] + c['duration'] for c in project['clips'])
        project['clips'] += [
            {'id': 'title', 'type': 'title', 'track': 1, 'start': end, 'duration': 1.0, 'title': 'Hi',
             'titleStyle': {'fontSize': 10}},
            {'id': 'v3', 'type': 'video', 'track': project['trackCounts']['video'], 'start': 0, 'duration': 4 * fr,
             'in': 0, 'mediaId': v[0]['mediaId']},
            {'id': 'a3', 'type': 'audio', 'track': -project['trackCounts']['audio'], 'start': 0, 'duration': 4 * fr,
             'in': 0, 'mediaId': a[0]['mediaId']}]
        # Settings a later editor may add, unknown to the writer.
        v[2]['futureClipFeature'] = 3
        project['futureTimelineFeature'] = {'on': True}
        result = self.run_save(self.payload(project, allow={'unproven': True}))
        lines = {}
        for line in result['lines']:
            lines.setdefault(line['what'].split(' (')[0].split(':')[0], []).append(line)

        def has(prefix, status=writer.NOT_WRITTEN, *places):
            found = [line for what, group in lines.items() if what.startswith(prefix) for line in group]
            self.assertTrue(found, f'{prefix}: no line among {sorted(lines)}')
            self.assertEqual({line['status'] for line in found}, {status}, prefix)
            for place in places:
                self.assertTrue(any(place in p for line in found for p in line['all']), (prefix, place))
        has('Keyframes', writer.NOT_WRITTEN, 'opacity', 'x', 'y', 'scale', 'rotation', 'volume')
        has('Splice color adjustments', writer.NOT_WRITTEN, 'brightness', 'contrast', 'saturation', 'exposure',
            'temperature', 'tint')
        has('Splice LUTs')
        has('Zoom, vertical position and rotation', writer.NOT_WRITTEN, 'y', 'scale', 'rotation')
        has('Opacity changes')
        has('Clip fades')
        has('Clip pan')
        has('Speed changes and reversed clips')
        has('Speed ramps')
        has('Picture transitions and fades')
        has('Clip markers')
        has('Clip names changed in Splice')
        has('Sound a picture clip plays in Splice')
        has('Disabled sound clips')
        has('Changes of the audio stream')
        has('Timeline markers added, changed or removed')
        has('Captions')
        has('Timeline resolution changes')
        has('Drop-frame timecode setting changes')
        has('Timeline In and Out marks')
        has('Track sync lock', writer.NOT_WRITTEN, 'syncLocked')
        has('Track mixer gain, pan and solo', writer.NOT_WRITTEN, 'gain', 'pan', 'solo')
        has('New video tracks')
        has('New audio tracks')
        has('Media renamed in Splice')
        has('Titles and other Splice-only clips')
        has('Splice clip settings Splice cannot write to Resolve', writer.NOT_WRITTEN, 'futureClipFeature')
        has('Splice timeline settings Splice cannot write to Resolve', writer.NOT_WRITTEN, 'futureTimelineFeature')
        has('Timeline renamed', writer.EXACT)
        has('Track renamed', writer.EXACT)
        has('Timeline start timecode changed', writer.EXACT)
        has('Clip volume changes', writer.EXACT)
        has('Clips disabled or enabled', writer.EXACT)
        has('Resolve sound crossfades changed', writer.EXACT)
        has('Position X written', writer.OPT_IN)
        has('Track lock, hide or mute changed', writer.OPT_IN)
        # What is written reads back.
        again = self.opened(Path(result['project']['path']))
        self.assertEqual(again['name'], 'Renamed cut')
        self.assertEqual(again['trackNames'].get('video:1'), 'Pictures')
        self.assertEqual(again['startTimecode'], project['startTimecode'])
        by_item = {c['resolveItemId']: c for c in again['clips']}
        self.assertAlmostEqual(by_item[others[0]['resolveItemId']]['volume'], 50, places=3)
        self.assertIs(by_item[v[2]['resolveItemId']].get('enabled'), False)
        self.assertAlmostEqual(by_item[v[0]['resolveItemId']]['transform']['x'], 10.0)
        self.assertEqual(round(by_item[carrier['resolveItemId']]['transitionDuration'] * fps), 8)
        self.assertEqual(again['trackState'], {'video:2': {'hidden': True}, 'audio:-2': {'locked': True}})
        self.assertTrue((again.get('trackMix') or {}).get('-1', {}).get('mute'))

    def test_a_frame_rate_change_is_refused(self):
        project = self.opened()
        project.update(fps=25, frameRate='25/1')
        with self.assertRaises(writer.WriteError):
            self.run_save(self.payload(project, confirm=False))


class MediaStartTests(unittest.TestCase):
    def view(self, start, sound):
        return items.MediaView(media_id='m', picture=True, start_seconds=start, sound_start=sound)

    def test_the_rule_unless_picture_timecode_and_sound_start_differ(self):
        rows = [{'MediaRef': 'other', 'DbType': items.PICTURE, 'MediaStartTime': 5.0}]
        self.assertEqual(items.media_start(self.view(3600.0, 3600.0), rows), (3600.0, None))
        self.assertEqual(items.media_start(self.view(3600.0, None), rows), (3600.0, None))
        value, why = items.media_start(self.view(3599.9964, 931.5973333333334), rows)
        self.assertIsNone(value)
        self.assertIn('no item of it is on this timeline', why)

    def test_the_value_the_clips_items_on_the_timeline_hold(self):
        rows = [{'MediaRef': 'm', 'DbType': items.PICTURE, 'MediaStartTime': 931.5973333333334},
                {'MediaRef': 'm', 'DbType': items.SOUND, 'MediaStartTime': 931.5973333333334}]
        self.assertEqual(items.media_start(self.view(3599.9964, 931.5973333333334), rows), (931.5973333333334, None))
        rows[1]['MediaStartTime'] = 3599.9964
        value, why = items.media_start(self.view(3599.9964, 931.5973333333334), rows)
        self.assertIsNone(value)
        self.assertIn('different starts', why)
        # A neighbouring double the clip's items hold (a timeline imported from FCPXML) is kept.
        import math
        near = math.nextafter(3600.0, 0)
        self.assertEqual(items.media_start(self.view(3600.0, 3600.0), [{'MediaRef': 'm', 'DbType': items.SOUND,
                                                                         'MediaStartTime': near}]), (near, None))
        # A far value on a clip without a conflict is not taken: the rule stands.
        self.assertEqual(items.media_start(self.view(3600.0, 3600.0), [{'MediaRef': 'm', 'DbType': items.SOUND,
                                                                         'MediaStartTime': 12.0}]), (3600.0, None))


class MediaStartSaveTests(SaveCase):
    def test_inserts_from_media_with_two_starts_are_refused_and_reported_per_clip(self):
        project = self.opened()
        fps = project['fps']
        pic_media = next(m for m in project['media'] if m.get('path') == PIC)
        end = max(c['start'] + c['duration'] for c in project['clips'])
        project['clips'].append({'id': 'ins', 'type': 'video', 'track': 1, 'start': end, 'duration': 4 / fps,
                                 'in': 0, 'mediaId': pic_media['id']})
        real = items.media_view

        def two_starts(clip):
            view = real(clip)
            if view.picture:
                view.sound_start = view.start_seconds + 2668.4
            return view
        conn = sqlite3.connect(self.kept)
        conn.execute('UPDATE Sm2TiItem SET MediaStartTime = MediaStartTime + ? WHERE Sm2TiItem_id = ?',
                     (1.0, self.ids['pictures'][0]))
        conn.commit()
        conn.close()
        with mock.patch.object(items, 'media_view', two_starts):
            result = self.run_save(self.payload(project, confirm=False))
        line = next(line for line in result['lines'] if line['what'].startswith('New clips from media whose picture'))
        self.assertEqual((line['status'], line['count']), (writer.NOT_WRITTEN, 1))
        self.assertIn('different starts', line['all'][0])
        # When the clip's items on the timeline agree, the new item takes their value.
        shutil.copyfile(self.db, self.kept)
        with mock.patch.object(items, 'media_view', two_starts):
            result = self.run_save(self.payload(project))
        conn = sqlite3.connect(f'file:{result["project"]["path"]}?mode=ro', uri=True)
        try:
            held = {r[0] for r in conn.execute('SELECT MediaStartTime FROM Sm2TiItem WHERE DbType = ? AND MediaRef = ?',
                                               (items.PICTURE, pic_media['resolveMediaId']))}
        finally:
            conn.close()
        self.assertEqual(len(held), 1)


class StillRadiometryTests(unittest.TestCase):
    def rows(self, *fields):
        message = codec.pb_encode([[n, 0, v] for n, v in fields])
        return mock.Mock(video={'Radiometry': codec.envelope(message, version=2, what='Radiometry')})

    def test_the_second_form_of_a_still_colour_description_is_not_verified(self):
        from resolve import mediapool
        plain = self.rows((1, 102), (2, 2), (5, 16), (6, 3), (19, 0), (27, 0))
        second = self.rows((1, 102), (2, 2), (5, 16), (6, 3), (14, 2), (15, 6), (19, 0), (27, 0))
        self.assertIsNone(mediapool.still_radiometry_variant(plain))
        why = mediapool.still_radiometry_variant(second)
        self.assertIn('#14, #15', why)
        self.assertIn('not established', why)
        self.assertIn('cannot be read', mediapool.still_radiometry_variant(mock.Mock(video={'Radiometry': b'\x01'})))


class NewMediaReportTests(SaveCase):
    def add(self, project, name, local, kind, track, at):
        project['media'].append({'id': name, 'name': Path(local).name, 'src': 'file:' + str(local), 'type': kind})
        project['clips'].append({'id': 'c-' + name, 'type': 'audio' if kind == 'audio' else 'video', 'track': track,
                                 'start': at, 'duration': 4 / project['fps'], 'in': 0, 'mediaId': name})

    def test_refused_new_media_gets_one_line_per_kind_saying_what_would_make_it_writable(self):
        import subprocess
        project = self.opened()
        fps = project['fps']
        # The seed's own WAV is not linked in Splice: its file cannot be measured, so new WAVs are
        # not verified (one line for both, with the remedy).
        for media in project['media']:
            if media.get('path') == SND:
                media.pop('src', None)
                media['offline'] = True
        for n in range(2):
            wav = self.dir / f'new{n}.wav'
            shutil.copyfile(self.snd, wav)
            self.add(project, f'w{n}', wav, 'audio', -2, n * 5 / fps)
        # A ProRes HQ FFmpeg wrote without a colour range: a kind no sample covers (one line).
        for n in range(2):
            mov = self.dir / f'unset{n}.mov'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=gray:s=1920x1080:r=30000/1001:d=1',
                            '-c:v', 'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le', '-timecode', '01:00:00:00',
                            str(mov)], check=True)
            self.add(project, f'p{n}', mov, 'video', 2, 10 + n * 5 / fps)
        result = self.run_save(self.payload(project, confirm=False))
        wav_lines = [line for line in result['lines'] if line['what'].startswith('New .wav sound files')]
        self.assertEqual(len(wav_lines), 1, [line['what'] for line in result['lines']])
        self.assertEqual((wav_lines[0]['status'], wav_lines[0]['count']), (writer.OPT_IN, 2))
        self.assertIn('link the seed\'s own media of this kind', wav_lines[0]['what'])
        mov_lines = [line for line in result['lines'] if line['what'].startswith('New .mov picture')]
        self.assertEqual(len(mov_lines), 1, [line['what'] for line in result['lines']])
        self.assertEqual((mov_lines[0]['status'], mov_lines[0]['count']), (writer.NOT_WRITTEN, 2))
        self.assertTrue(all('colour range - (unset in the file)' in place for place in mov_lines[0]['all']),
                        (mov_lines[0]['what'], mov_lines[0]['all']))
        self.assertIn('unverifiedMedia', result['needs'])


class HungVolumeTests(unittest.TestCase):
    def test_a_hung_volume_holds_its_threads_once_not_once_per_open(self):
        from resolve import media
        release = threading.Event()

        def work(text):
            if text.startswith('/mnt/hung/'):
                release.wait(30)
            return 'done ' + text
        items_ = [f'/mnt/hung/{n}.mov' for n in range(10)] + ['/mnt/fine/a.mov', '/mnt/fine/b.mov']
        before = media.threads_alive()
        try:
            import time
            first = media._bounded(work, items_, time.monotonic() + 0.4, media._volume, 'hung')
            self.assertEqual({first[10], first[11]}, {'done /mnt/fine/a.mov', 'done /mnt/fine/b.mov'})
            self.assertTrue(media.hung('mnt/hung'))
            settle = time.monotonic() + 2
            while media.threads_alive() - before > media.PER_VOLUME and time.monotonic() < settle:
                threading.Event().wait(0.02)  # idle workers end once they see the deadline
            stuck = media.threads_alive() - before
            self.assertLessEqual(stuck, media.PER_VOLUME)
            for _ in range(5):
                again = media._bounded(work, items_, time.monotonic() + 0.4, media._volume, 'hung')
                self.assertEqual([again[n] for n in range(10)], ['hung'] * 10)
                self.assertEqual(again[10], 'done /mnt/fine/a.mov')
            settle = time.monotonic() + 2
            while media.threads_alive() - before > stuck and time.monotonic() < settle:
                threading.Event().wait(0.02)
            self.assertEqual(media.threads_alive() - before, stuck)
        finally:
            release.set()
        deadline = __import__('time').monotonic() + 10
        while media.threads_alive() > before and __import__('time').monotonic() < deadline:
            threading.Event().wait(0.05)
        self.assertEqual(media.threads_alive(), before)
        self.assertFalse(media.hung('mnt/hung'))
        import time
        answered = media._bounded(work, items_, time.monotonic() + 5, media._volume, 'hung')
        self.assertEqual(answered[0], 'done /mnt/hung/0.mov')


class PlaceMutantTests(SaveCase):
    """Each rule of placing a project, alone (the validator's surviving mutants)."""

    def test_names_fold_after_decomposition_as_a_share_may(self):
        (self.projects() / unicodedata.normalize('NFD', 'Café')).mkdir()
        with self.assertRaises(place.PlaceError):
            place.check_new(self.projects(), 'CAFÉ')
        with self.assertRaises(place.PlaceError):
            place.check_new(self.projects(), 'café')

    def test_names_fold_as_windows_upper_cases_them(self):
        # Windows upper-cases a dotless i to I: “ıı” and “II” are one folder name on a share.
        (self.projects() / 'Kıyı').mkdir()
        with self.assertRaises(place.PlaceError):
            place.check_new(self.projects(), 'KIYI')

    def test_a_linked_library_folder_is_refused_as_a_link(self):
        root = self.library / 'Resolve Projects'
        real = self.library / 'Real Resolve Projects'
        shutil.move(str(root), str(real))
        root.symlink_to(real, target_is_directory=True)
        with self.assertRaises(place.PlaceError) as caught:
            place.library_users(self.library)
        self.assertIn('is a link', str(caught.exception))

    def test_a_projects_link_inside_the_library_is_refused_too(self):
        projects = self.projects()
        real = projects.parent / 'Real projects'
        shutil.move(str(projects), str(real))
        projects.symlink_to(real, target_is_directory=True)
        with self.assertRaises(place.PlaceError) as caught:
            place.library_users(self.library)
        self.assertIn('no user', str(caught.exception))

    def test_a_projects_folder_resolving_outside_the_library_is_refused(self):
        outside = self.dir / 'outside'
        shutil.move(str(self.projects()), str(outside))
        self.projects().symlink_to(outside, target_is_directory=True)
        with mock.patch.object(place, '_no_link', return_value=None):
            with self.assertRaises(place.PlaceError) as caught:
                place.library_users(self.library)
        self.assertIn('outside the library', str(caught.exception))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'renameat2 is Linux')
    def test_the_rename_never_replaces_even_an_empty_folder(self):
        source_dir, target = self.dir / 'src', self.dir / 'target'
        source_dir.mkdir()
        (source_dir / 'Project.db').write_bytes(b'x')
        target.mkdir()
        with self.assertRaises(FileExistsError):
            place.move_no_replace(source_dir, target)
        self.assertTrue((source_dir / 'Project.db').exists())

    def test_a_project_of_a_name_that_folds_alike_appearing_during_the_save_wins(self):
        staging = place.Staging(self.library, 'Late arrival')
        try:
            staging.db.write_bytes(b'x' * 1024)
            (self.projects() / 'LATE ARRIVAL').mkdir()
            with self.assertRaises(place.PlaceError) as caught:
                staging.finish()
            self.assertIn('already has a project named', str(caught.exception))
        finally:
            staging.close()
        self.assertFalse((self.projects() / 'Late arrival').exists())

    def test_staging_on_another_file_system_is_refused(self):
        real_stat = os.stat
        projects = str(self.projects())

        def other_device(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path) == projects:
                values = list(info)
                values[stat.ST_DEV] = info.st_dev + 1
                return os.stat_result(values)
            return info
        with mock.patch('resolve.place.os.stat', side_effect=other_device):
            with self.assertRaises(place.PlaceError) as caught:
                place.Staging(self.library, 'Elsewhere')
        self.assertIn('another file system', str(caught.exception))
        self.assertFalse((self.library / 'Resolve Projects' / place.STAGING).exists())

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes anywhere')
    def test_a_projects_folder_splice_cannot_write_is_refused_before_anything_is_staged(self):
        mode = stat.S_IMODE(self.projects().stat().st_mode)
        self.projects().chmod(0o555)
        try:
            with self.assertRaises(place.PlaceError) as caught:
                place.Staging(self.library, 'Read only')
        finally:
            self.projects().chmod(mode)
        self.assertIn('not writable by Splice', str(caught.exception))
        self.assertFalse((self.library / 'Resolve Projects' / place.STAGING).exists())


class SelfCheckMutantTests(SaveCase):
    """Each self-check, alone: a planted fault after the writer's own changes must stop the save."""

    def planted(self, fault, confirm=True, edit=None):
        project = self.opened()
        if edit:
            edit(project)
        original = writer.Writer.apply

        def apply(self_, name):
            new_id = original(self_, name)
            fault(self_.seed.conn, self_)
            return new_id
        before = tree_state(self.library)
        with mock.patch.object(writer.Writer, 'apply', apply):
            with self.assertRaises((writer.WriteError, place.PlaceError)) as caught:
                self.run_save(self.payload(project, confirm=confirm))
        self.assertEqual(tree_state(self.library), before)
        return str(caught.exception)

    def trim(self, project):
        clip = self.clip(project, 'video', 0)
        clip['duration'] -= 2 / project['fps']

    def test_an_unmeant_row_change_fails_the_self_check(self):
        message = self.planted(lambda conn, w: conn.execute("UPDATE Sm2MpFolder SET Name = 'Changed'"))
        self.assertIn('did not mean to change', message)

    def test_an_item_read_back_elsewhere_than_meant_fails_the_self_check(self):
        item = self.ids['pictures'][0]
        message = self.planted(lambda conn, w: conn.execute(
            "UPDATE Sm2TiItem SET Duration = CAST(Duration AS INTEGER) - 1 WHERE Sm2TiItem_id = ?", (item,)),
            edit=self.trim)
        self.assertIn('read back as', message)

    def test_a_disabled_flag_read_back_otherwise_fails_the_self_check(self):
        item = self.ids['pictures'][0]
        message = self.planted(lambda conn, w: conn.execute(
            'UPDATE Sm2TiItem SET Flags = Flags | 2 WHERE Sm2TiItem_id = ?', (item,)), edit=self.trim)
        self.assertIn('disabled is True', message)

    def test_a_lost_journal_mode_fails_the_self_check(self):
        message = self.planted(lambda conn, w: conn.execute('PRAGMA journal_mode = WAL').fetchall())
        self.assertIn('journal mode', message)

    def test_a_changed_page_size_fails_the_self_check(self):
        def resize(conn, w):
            size = conn.execute('PRAGMA page_size').fetchone()[0]
            conn.execute(f'PRAGMA page_size = {8192 if size != 8192 else 4096}')
            conn.execute('VACUUM')
        message = self.planted(resize)
        self.assertIn('page size', message)

    def test_a_sidecar_left_beside_the_project_fails_even_a_check(self):
        def sidecar(conn, w):
            Path(str(w.seed.path) + '-journal').write_bytes(b'')
        message = self.planted(sidecar, confirm=False)
        self.assertIn('Project.db-journal', message)

    def test_a_changed_original_is_not_used_as_the_seed(self):
        project = self.opened()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE SM_Project SET ProjectName = 'Saved again in Resolve'")
        conn.commit()
        conn.close()
        with self.assertRaises(source.ResolveError) as caught:
            save.run(self.payload(project, confirm=False), originals=lambda kind, sha: None, scratch=self.dir,
                     resolve_media=lambda src: src[5:])
        self.assertIn('saved the original project again', str(caught.exception))


class WriterMutantTests(SaveCase):
    def crossfade(self, frames, before):
        project = self.opened()
        fps = project['fps']
        carrier = next(c for c in project['clips'] if c.get('resolveTransitionId'))
        carrier.update(transitionDuration=frames / fps, transitionBefore=before / fps)
        result = self.run_save(self.payload(project, confirm=False))
        return [line for line in result['lines'] if 'crossfade' in line['what'] or 'transitions' in line['what']]

    def test_crossfade_lengths_two_to_sixteen_frames_centred(self):
        self.assertEqual(self.crossfade(2, 1)[0]['status'], writer.EXACT)
        self.assertEqual(self.crossfade(16, 8)[0]['status'], writer.EXACT)
        for frames, before in ((1, 1), (17, 9)):
            line = self.crossfade(frames, before)[0]
            self.assertEqual(line['status'], writer.NOT_WRITTEN)
            self.assertIn('outside the lengths measured', line['what'])
        line = self.crossfade(8, 2)[0]
        self.assertEqual(line['status'], writer.NOT_WRITTEN)
        self.assertIn('not centred', line['what'])

    def test_resolve_centres_an_odd_crossfade_with_the_extra_frame_before_the_cut(self):
        self.assertEqual([items.frames_before(n) for n in (2, 3, 4, 5, 7, 16)], [1, 2, 2, 3, 4, 8])

    def test_crossfades_only_at_the_measured_rate(self):
        from fractions import Fraction
        self.assertIsNone(items.transition_problem('Cross Fade 0 dB', 8, Fraction(30000, 1001)))
        for rate in (Fraction(24000, 1001), Fraction(25), Fraction(24), Fraction(60000, 1001)):
            self.assertIn('29.97', items.transition_problem('Cross Fade 0 dB', 8, rate))

    def test_a_deleted_clip_with_markers_is_kept_and_reported(self):
        item = self.ids['sounds'][2]
        conn = sqlite3.connect(self.kept)
        lmap = conn.execute('SELECT BtLockableBlobMap_id FROM BtLockableBlobMap').fetchone()[0]
        conn.execute('INSERT INTO BtLockableBlob (BtLockableBlob_id, DbType, BtLockableBlobMap_id, FieldsBlob, BlobOwner, '
                     'DbSavedTime) VALUES (?, ?, ?, ?, ?, ?)', ('00000000-0000-4000-8000-00000000abcd',
                                                               'Sm2TiItemLockableBlob', lmap,
                                                               F.markers([(3, 'M', 'note', 1)]), item, 0))
        conn.commit()
        conn.close()
        shutil.copyfile(self.kept, self.db)
        project = self.opened()
        project['clips'] = [c for c in project['clips'] if c['resolveItemId'] != item]
        result = self.run_save(self.payload(project))
        again = self.opened(Path(result['project']['path']))
        self.assertIn(item, {c['resolveItemId'] for c in again['clips']})
        line = next(line for line in result['lines'] if line['what'].startswith('Deleted clips that carry Resolve work'))
        self.assertIn('markers', line['all'][0])

    def test_a_link_group_links_a_picture_only_with_sounds(self):
        project = self.opened()
        fps = project['fps']
        end = max(c['start'] + c['duration'] for c in project['clips'])
        pic = next(m['id'] for m in project['media'] if m.get('path') == PIC)
        snd = next(m['id'] for m in project['media'] if m.get('path') == SND)
        project['clips'] += [
            {'id': 'p1', 'type': 'video', 'track': 1, 'start': end, 'duration': 4 / fps, 'in': 0, 'mediaId': pic,
             'linkGroup': 'g'},
            {'id': 'p2', 'type': 'video', 'track': 2, 'start': end, 'duration': 4 / fps, 'in': 0, 'mediaId': pic,
             'linkGroup': 'g'},
            {'id': 's1', 'type': 'audio', 'track': -1, 'start': end, 'duration': 4 / fps, 'in': 0, 'mediaId': snd,
             'linkGroup': 'g'}]
        result = self.run_save(self.payload(project))
        conn = sqlite3.connect(f'file:{result["project"]["path"]}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            start = str(round(end * fps) + project['startFrame'])
            rows = [dict(r) for r in conn.execute('SELECT * FROM Sm2TiItem WHERE Start = ?', (start,))]
        finally:
            conn.close()
        kinds = {r['Sm2TiItem_id']: r['DbType'] for r in rows}
        for row in rows:
            partners = items.links_of(row)
            self.assertTrue(partners, row['DbType'])
            self.assertTrue(all(kinds[p] != row['DbType'] for p in partners), (row['DbType'], partners))
        self.assertTrue(any(line['what'].startswith('Links between two pictures') for line in result['lines']))


class SeedAndKindMutantTests(unittest.TestCase):
    def test_a_drp_seed_waits_for_the_unproven_opt_in(self):
        from resolve import drp_seed
        report = writer.Report()
        with self.assertRaises(writer.WriteError) as caught:
            drp_seed.make_seed('/nonexistent.drp', '/nonexistent.db', '/nonexistent-target.db', report, False)
        self.assertIn('Allow unproven changes', str(caught.exception))
        self.assertEqual(report.needs, {'unproven'})

    def test_sixteen_bit_mono_wav_is_not_a_verified_kind(self):
        from resolve import mediapool
        status = mediapool.KINDS.get(mediapool._audio_kind('wav', mediapool._pcm(1, 2, False)))
        self.assertEqual(status[0], 'unverified')
        status = mediapool.KINDS.get(mediapool._audio_kind('wav', mediapool._pcm(2, 2, True)))
        self.assertEqual(status[0], 'verified')
