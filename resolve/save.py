"""Save Splice timelines as a new project in a Resolve disk library.

One path for the dialog's check and its save: the seed is copied into a staging folder (for a
check, Splice's own private folder; for a save, the library's staging folder on the projects'
file system), the writer changes the copy, the copy is verified, and only a save that passed
every check is moved into ``Projects`` with one rename (:mod:`resolve.place`). The checks after
writing: SQLite's integrity check and the seed's journal mode, page size and encoding kept;
every seed row the writer did not mean to change unchanged (compared row by row, the new project
id standing for the old); and the project read back with Splice's own reader, each written
timeline compared with what was meant (tracks, starts, durations, in points, media, links,
disabled clips, transitions).
"""
import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import importer, items, mediapool
from .facts import check_mappings, map_path, measure
from .place import PlaceError, Staging, check_name, check_new, library_users, pick_user
from .source import ResolveError, load_library, snapshot
from .writer import EXACT, NOT_WRITTEN, OPT_IN, Report, Seed, WriteError, Writer, prepare_template

MAX_TIMELINES = 64


def _digest(row, project_id, new_id):
    h = hashlib.sha256()
    for column in sorted(row):
        value = row[column]
        if isinstance(value, str) and value in (project_id, new_id):
            value = '\x00project id'
        kind = type(value).__name__
        if isinstance(value, float):
            value = value.hex()
        h.update(repr((column, kind, value)).encode('utf-8', 'backslashreplace'))
    return h.hexdigest()


def fingerprint(conn, project_id, new_id=None):
    """``{table: {key: digest}}``: a row's key is its id, or for a join row its owner, child and
    property; digests mask the project id (which the writer replaces everywhere)."""
    out = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cursor = conn.execute(f'SELECT * FROM "{table}"')
        names = [d[0] for d in cursor.description]
        rows = {}
        for values in cursor:
            row = dict(zip(names, values))
            if table + '_id' in row:
                key = row[table + '_id']
            elif {'DbOwner', 'DbAssociate', 'DbPropertyName'} <= set(row):
                key = (row['DbOwner'], row['DbAssociate'], row['DbPropertyName'])
            else:
                key = repr(sorted(row.items(), key=lambda kv: kv[0]))
            if key in (project_id, new_id):
                key = '\x00project'
            elif isinstance(key, tuple):
                key = tuple('\x00project' if k in (project_id, new_id) else k for k in key)
            rows.setdefault(key, []).append(_digest(row, project_id, new_id))
        out[table] = rows
    return out


def unchanged_rows(before, after, writer):
    """Every seed row the writer was not meant to change, unchanged: the first difference found
    as text, or None."""
    items_changed = {r[1] for r in writer.updates} | set(writer.deletes) | {row['Sm2TiItem_id'] for _t, row in
                                                                            writer.inserts}
    tracks = set(writer.track_orders) | set(writer.track_updates)
    sequences = set(writer.sequence_updates)
    renamed = {t.id for t, _n in writer.renames} | {t.media for t, _n in writer.renames if t.media}
    media_ids = set()
    for clip in writer.removed_media:
        media_ids |= clip.ids()
    for authored in writer.authored.values():
        if authored is not None:
            media_ids |= mediapool.ClipRows.from_tables(authored.rows).ids()
    folders = {c.folder_link['DbOwner'] for c in writer.removed_media} | \
        {a.rows['Sm2MpFolder_Sm2MpMedia'][0]['DbOwner'] for a in writer.authored.values() if a}
    allowed = {'Sm2TiItem': items_changed, 'Sm2TiTrack': set(writer.track_updates), 'Sm2Sequence': sequences,
               'Sm2Timeline': renamed, 'Sm2MpMedia': renamed | media_ids, 'SM_Project': {'\x00project'}}
    for table in mediapool.MEDIA_TABLES:
        allowed.setdefault(table, set()).update(media_ids)
    for table in set(before) | set(after):
        old, new = before.get(table, {}), after.get(table, {})
        free = allowed.get(table, set())
        for key in set(old) | set(new):
            if old.get(key) == new.get(key):
                continue
            if key in free:
                continue
            if isinstance(key, tuple):
                owner, child, prop = key
                if table == 'Sm2TiItem_Sm2TiTrack' and (owner in tracks or child in items_changed):
                    continue
                if table == 'Sm2MpFolder_Sm2MpMedia' and (owner in folders or child in media_ids):
                    continue
                if table == 'BtAudioInfo_Sm2MpMedia' and (owner in media_ids or child in media_ids):
                    continue
            return f'{table} row {str(key)[:80]} changed though the writer did not mean to change it'
    return None


def _track_numbers(timeline):
    out = {}
    for kind in ('video', 'audio'):
        for number, track in enumerate(timeline.tracks[kind], 1):
            out[track['Sm2TiTrack_id']] = (kind, number if kind == 'video' else -number)
    return out


def read_back(path, writer, scratch):
    """The written project read with Splice's own reader and compared with what was meant; the
    differences as text (an empty list when there are none)."""
    model = importer.read(load_library(path, scratch))
    seed = Seed(path)
    differences = []
    try:
        for timeline, expected, transitions, start, name in writer.expected:
            index = next((i for i, t in enumerate(model.timelines) if t['id'] == timeline.id), None)
            if index is None:
                differences.append(f'{name}: the timeline is missing')
                continue
            project = importer.to_splice(model, index)
            if project['startFrame'] != start:
                differences.append(f'{name}: starts at frame {project["startFrame"]}, not {start}')
            numbers = _track_numbers(timeline)
            found = {c.get('resolveItemId'): c for c in project['clips'] if c.get('resolveItemId')}
            for item_id, want in expected.items():
                clip = found.get(item_id)
                where = f'{name} · item {item_id[:8]}'
                if clip is None:
                    differences.append(f'{where}: not read back')
                    continue
                kind, number = numbers[want['track']]
                got = (clip['type'], clip['track'], clip['resolveFrames'][0], clip['resolveFrames'][1],
                       clip['resolveFrames'][2])
                meant = (kind, number, want['start'], want['duration'], want['in'])
                if got != meant:
                    differences.append(f'{where}: read back as {got}, meant {meant}')
                if want['disabled'] != (clip.get('enabled') is False):
                    differences.append(f'{where}: disabled is {clip.get("enabled") is False}, meant {want["disabled"]}')
                row = seed.row('Sm2TiItem', item_id)
                if row is not None and row['DbType'] in (items.PICTURE, items.SOUND) and \
                        set(items.links_of(row)) != want['partners']:
                    differences.append(f'{where}: linked to {sorted(items.links_of(row))}, meant '
                                       f'{sorted(want["partners"])}')
            extra = set(found) - set(expected) - {i for i, (_k, _t, r) in timeline.items.items()}
            if extra:
                differences.append(f'{name}: {len(extra)} item(s) read back that were not written')
            for transition in transitions:
                row = seed.row('Sm2TiItem', transition['id'])
                if row is None or row['Sm2TiTrack_id'] != transition['track'] or \
                        str(row['Start']).split('|')[0] != str(transition['start']) and not transition['seed'] or \
                        str(row['Duration']).split('|')[0] != str(transition['duration']) and not transition['seed']:
                    differences.append(f'{name}: a transition is not where it was meant to be')
    finally:
        seed.conn.close()
    return differences


class Request:
    """A checked save request (see :func:`run`)."""

    def __init__(self, payload):
        self.library = payload.get('library')
        if not isinstance(self.library, str) or not self.library.strip() or len(self.library) > 4096 \
                or '\x00' in self.library:
            raise ResolveError('Choose the Resolve library folder.')
        self.library = self.library.strip()
        self.name = check_name(payload.get('name'))
        self.user = payload.get('user') if isinstance(payload.get('user'), str) and payload.get('user') else None
        self.seed = payload.get('seed') if isinstance(payload.get('seed'), dict) else {}
        if self.seed.get('kind') not in ('library', 'drp', 'template'):
            raise ResolveError('Say which project the new one is made from (its seed).')
        timelines = payload.get('timelines')
        if not isinstance(timelines, list) or not timelines or len(timelines) > MAX_TIMELINES or \
                not all(isinstance(t, dict) for t in timelines):
            raise ResolveError('Choose at least one timeline to save.')
        self.timelines = timelines
        self.mappings = check_mappings(payload.get('pathMap'))
        allow = payload.get('allow') if isinstance(payload.get('allow'), dict) else {}
        self.allow_unverified = allow.get('unverifiedMedia') is True
        self.allow_unproven = allow.get('unproven') is True
        self.keep_media = payload.get('keepTemplateMedia') is True
        self.confirm = payload.get('confirm') is True


class Measurer:
    """Facts of the files the save needs, measured once each."""

    def __init__(self, request, resolve_media):
        self.request = request
        self.resolve_media = resolve_media
        self.cache = {}
        self.local_of = {}
        for project in request.timelines:
            for media in project.get('media') or []:
                if isinstance(media, dict) and media.get('resolveMediaId') and media.get('src'):
                    self.local_of.setdefault(media['resolveMediaId'], media)

    def __call__(self, media, template=False):
        if template:
            entry = self.local_of.get(media.get('resolveMediaId'))
            if entry is None:
                return None, 'the template file is not linked in Splice'
            resolve_path = media['templatePath']
            local = self._local(entry)
        else:
            local = self._local(media)
            path = media.get('path')
            if isinstance(path, str) and (path[1:3] == ':\\' or path.startswith('\\\\')) and media.get('resolveMediaId'):
                resolve_path = path
            else:
                resolve_path = map_path(local, self.request.mappings) if local else None
        if local is None:
            return None, 'its file is not on this computer'
        key = (str(local), resolve_path)
        if key not in self.cache:
            try:
                self.cache[key] = (measure(local, resolve_path), str(local))
            except (mediapool.MediaPoolError, OSError, ValueError) as error:
                self.cache[key] = (None, getattr(error, 'message', None) or str(error))
        return self.cache[key]

    def _local(self, media):
        src = media.get('src')
        if isinstance(src, str) and src:
            try:
                return Path(self.resolve_media(src))
            except (ValueError, OSError):
                return None
        return None


def _copy_seed(source, target):
    shutil.copyfile(source, target)


def run(payload, *, originals, scratch, resolve_media, drp_seed=None):
    """Check (``confirm`` false) or save one request; returns the report as a dict.

    ``originals(kind, sha256)`` gives the path of a kept original (a ``Project.db`` copy, or a
    ``.drp``), ``scratch`` is Splice's private folder, ``resolve_media(src)`` the local file of a
    Splice media URL, ``drp_seed(drp path, schema donor, target)`` makes a ``Project.db`` of a
    ``.drp``."""
    request = Request(payload)
    report = Report()
    library, _root, users = library_users(request.library)
    user = pick_user(users, request.user)
    check_new(users[user], request.name)
    staging = None
    private = None
    try:
        if request.confirm:
            staging = Staging(library, request.name, user)
            target = staging.db
        else:
            private = Path(tempfile.mkdtemp(prefix='splice-resolve-save-', dir=scratch))
            target = private / 'Project.db'
        seed_label = _stage_seed(request, users[user], target, originals, scratch, drp_seed, report)
        result = _write(request, target, scratch, resolve_media, report)
        result['seed'] = seed_label
        result['library'] = str(library)
        result['user'] = user
        if request.confirm:
            placed = staging.finish()
            result['written'] = True
            result['project'] = {'name': request.name, 'path': str(placed / 'Project.db')}
        else:
            result['written'] = False
        return result
    finally:
        if staging is not None:
            left = staging.close()
            if left and staging.placed is None:
                report.notes.append('Could not remove ' + ', '.join(left))
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)


def _stage_seed(request, projects, target, originals, scratch, drp_seed, report):
    kind = request.seed['kind']
    if kind == 'template':
        path = Path(str(request.seed.get('path') or ''))
        if path.name != 'Project.db' or path.parent.parent.resolve() != projects.resolve():
            raise ResolveError('Choose the template among the projects of this library (and user).')
        with tempfile.TemporaryDirectory(prefix='splice-resolve-', dir=scratch) as folder:
            copy, _digest_ = snapshot(path, folder)
            _copy_seed(copy, target)
        return {'kind': 'template', 'name': path.parent.name}
    sha = request.seed.get('sha256')
    if not isinstance(sha, str) or len(sha) != 64 or not all(c in '0123456789abcdef' for c in sha):
        raise ResolveError('The project this timeline came from is not identified; open it again from Resolve.')
    if kind == 'library':
        kept = originals('library', sha)
        if kept is None:
            live = request.seed.get('path')
            if isinstance(live, str) and live:
                with tempfile.TemporaryDirectory(prefix='splice-resolve-', dir=scratch) as folder:
                    copy, digest = snapshot(Path(live), folder)
                    if digest != sha:
                        raise ResolveError('Resolve saved the original project again since Splice opened it, and '
                                           'Splice no longer holds the version it opened. Open the project again '
                                           'from the library and redo the save.')
                    _copy_seed(copy, target)
                return {'kind': 'library', 'name': request.seed.get('project') or 'the original project'}
            raise ResolveError('Splice no longer holds the original project; open it again from the library.')
        _copy_seed(kept, target)
        return {'kind': 'library', 'name': request.seed.get('project') or 'the original project'}
    kept = originals('drp', sha)
    if kept is None:
        raise ResolveError('The original .drp is not on the server; send it again (the dialog does this itself).')
    if drp_seed is None:
        raise ResolveError('Saving a project opened from a .drp needs a Resolve 21 project in this library to take '
                           'the database layout from.')
    donor = _schema_donor(projects, scratch)
    drp_seed(kept, donor, target, report, request.allow_unproven)
    return {'kind': 'drp', 'name': request.seed.get('project') or 'the original .drp'}


def _schema_donor(projects, scratch):
    for entry in sorted(os.scandir(projects), key=lambda e: e.name):
        db = Path(entry.path) / 'Project.db'
        if entry.is_dir(follow_symlinks=False) and db.is_file():
            try:
                connection = sqlite3.connect(f'file:{db}?mode=ro&immutable=1', uri=True)
                try:
                    version = connection.execute('SELECT ProjectVersion FROM SM_Project').fetchone()
                finally:
                    connection.close()
            except sqlite3.Error:
                continue
            if version and version[0] == 17:
                return db
    raise ResolveError('Saving a project opened from a .drp needs one Resolve 21 project in this library to take the '
                       'database layout from; save any project in this library from Resolve 21 first.')


def _write(request, target, scratch, resolve_media, report):
    model = importer.read(load_library(target, scratch))
    seed = Seed(target)
    try:
        page_size, encoding = seed.page_size, seed.encoding
        writer = Writer(seed, model, report, facts_for=Measurer(request, resolve_media),
                        allow_unverified=request.allow_unverified, allow_unproven=request.allow_unproven,
                        template_mode=request.seed['kind'] == 'template', keep_template_media=request.keep_media)
        if request.seed['kind'] == 'template':
            if len(request.timelines) != 1:
                raise WriteError('A template project takes one timeline; choose one.')
            entries = [prepare_template(seed, model, writer, report)]
        else:
            entries = []
            for project in request.timelines:
                tid = (project.get('resolveSource') or {}).get('timelineId')
                entry = next((t for t in model.timelines if t['id'] == tid), None)
                if entry is None:
                    raise WriteError(f'The timeline “{project.get("name")}” is not in the original project.')
                if entry['kind'] != 'timeline':
                    raise WriteError(f'“{project.get("name")}” is a Resolve compound or multicam clip; Splice writes '
                                     'timelines only.')
                entries.append(entry)
        if len({e['id'] for e in entries}) != len(entries):
            raise WriteError('The same timeline was chosen twice.')
        for project, entry in zip(request.timelines, entries):
            writer.write_timeline(project, entry)
        before = fingerprint(seed.conn, seed.project_id)
        new_id = writer.apply(request.name)
        after = fingerprint(seed.conn, seed.project_id, new_id)
        if (seed.conn.execute('PRAGMA page_size').fetchone()[0], seed.conn.execute('PRAGMA encoding').fetchone()[0],
                seed.conn.execute('PRAGMA journal_mode').fetchone()[0].lower()) != (page_size, encoding, 'delete'):
            raise WriteError('The written project lost the seed\'s page size, encoding or journal mode.')
    finally:
        seed.conn.close()
    for suffix in ('-journal', '-wal', '-shm'):
        if os.path.lexists(str(target) + suffix):
            raise WriteError(f'Project.db{suffix} was left beside the written project; nothing was placed.')
    problem = unchanged_rows(before, after, writer)
    if problem:
        raise WriteError('Self-check failed: ' + problem + '. Nothing was placed.')
    differences = read_back(target, writer, scratch)
    if differences:
        raise WriteError('Self-check failed: the written project does not read back as meant: '
                         + '; '.join(differences[:5]) + '. Nothing was placed.')
    report.add(EXACT, 'Everything else in the seed (bins, grades, Fusion, Fairlight, project settings) carried over '
               'unchanged, row for row')
    return {'ok': True, 'lines': report.lines(), 'needs': sorted(report.needs), 'notes': report.notes,
            'operations': sorted(writer.operations), 'selfCheck': 'passed'}


__all__ = ['run', 'PlaceError', 'WriteError', 'NOT_WRITTEN', 'OPT_IN', 'EXACT']
