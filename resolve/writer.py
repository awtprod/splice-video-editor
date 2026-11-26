"""Write Splice timelines into a copy of a Resolve project (the seed).

The writer works on a private copy of a project Resolve saved (the seed): everything Splice does
not author is carried over from it row for row, unchanged. For each timeline written it compares
Splice's clips with the seed's items (by the Resolve item id the reader kept) and changes only
what was edited: a trim or move updates ``Start``, ``Duration`` and ``In``; a deleted clip's item
is removed; a new clip becomes a new item authored by :mod:`resolve.items` (its media from the
pool, or authored from the file's facts by :mod:`resolve.mediapool`); links, a disabled picture,
a sound clip's volume and a track's lock and on/off state are written where the bytes are
proven. Everything else is reported per clip and not written, never dropped silently. The
written copy is then read back with Splice's own reader and compared with what was meant.

Library projects are SQLite files in rollback-journal mode; the copy keeps the seed's journal
mode, page size and text encoding, and passes ``PRAGMA integrity_check``.
"""
import math
import sqlite3
import struct
import time
import uuid
from fractions import Fraction

from . import blobs, codec, effects, importer, items, mediapool
from .source import ResolveError

SUPPORTED_VERSIONS = frozenset({17})
RESOLVE_TRIGGERS = frozenset({
    ('Delete_BtVideoInfo_trigger', 'CREATE TRIGGER Delete_BtVideoInfo_trigger AFTER DELETE ON BtVideoInfo BEGIN DELETE '
     "FROM BtLockableBlob WHERE 1 = (SELECT Enable FROM TriggerControl WHERE Name = 'master_trigger') AND BlobOwner "
     '= OLD.BtVideoInfo_id; END'),
    ('Delete_BtAudioInfo_trigger', 'CREATE TRIGGER Delete_BtAudioInfo_trigger AFTER DELETE ON BtAudioInfo BEGIN DELETE '
     "FROM BtLockableBlob WHERE 1 = (SELECT Enable FROM TriggerControl WHERE Name = 'master_trigger') AND BlobOwner "
     '= OLD.BtAudioInfo_id; END')})
MAX_CLIPS = 20_000
EXACT, OPT_IN, NOT_WRITTEN = 'written', 'opt-in', 'not written'
#: Track Flags bits as the reader interprets them (calibration: 1 locked, 2 disabled or muted).
TRACK_LOCKED, TRACK_OFF, CLIP_OFF = 0x1, 0x2, 0x2
_EPSILON = 1e-4  # frames: Splice keeps times in seconds as doubles


class WriteError(ResolveError):
    """The project cannot be written as asked; nothing was placed."""


def _q(name):
    return '"' + name.replace('"', '""') + '"'


class Report:
    """What is written exactly, written only with an opt-in, and not written: one line per kind
    of thing with a count and up to five places, per-clip detail kept."""

    def __init__(self):
        self.entries = {}
        self.order = []
        self.needs = set()
        self.notes = []

    def add(self, status, what, where=None, need=None):
        key = (status, what)
        if key not in self.entries:
            self.entries[key] = []
            self.order.append(key)
        if where is not None:
            self.entries[key].append(where)
        if need:
            self.needs.add(need)

    def lines(self):
        out = []
        for status, what in self.order:
            places = self.entries[(status, what)]
            out.append({'status': status, 'what': what, 'count': max(1, len(places)), 'examples': places[:5],
                        'all': places[:2000]})
        return out


class Seed:
    """The staged copy of the seed, open for writing, with what the writer reads from it."""

    def __init__(self, path):
        self.path = path
        try:
            self.conn = sqlite3.connect(str(path), isolation_level=None)
        except sqlite3.Error as error:
            raise WriteError(f'The seed copy cannot be opened ({error}).') from None
        try:
            self._read()
        except BaseException:
            self.conn.close()
            raise

    def _read(self):
        conn = self.conn
        conn.execute('PRAGMA trusted_schema = OFF')
        self.journal = str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower()
        self.page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        self.encoding = conn.execute('PRAGMA encoding').fetchone()[0]
        if self.journal != 'delete':
            raise WriteError(f'The seed is in journal mode “{self.journal}”, not Resolve\'s rollback journal.')
        check = conn.execute('PRAGMA integrity_check').fetchall()
        if [r[0] for r in check] != ['ok']:
            raise WriteError('The seed project is damaged (SQLite integrity check failed); nothing was written.')
        self.columns = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            self.columns[name] = [r[1] for r in conn.execute(f'PRAGMA table_info({_q(name)})')]
        triggers = {(n, ' '.join((s or '').split())) for n, s in
                    conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")}
        if not triggers <= RESOLVE_TRIGGERS:
            raise WriteError('The seed holds database triggers Resolve does not create; it was not saved by Resolve.')
        for table in ('SM_Project', 'Sm2TiItem', 'Sm2TiItem_Sm2TiTrack', 'Sm2TiTrack', 'Sm2Sequence', 'Sm2Timeline'):
            if table not in self.columns:
                raise WriteError(f'The seed has no {table} table, so it is not a Resolve project.')
        projects = conn.execute('SELECT SM_Project_id, ProjectVersion FROM SM_Project').fetchall()
        if len(projects) != 1:
            raise WriteError('The seed does not hold exactly one project.')
        self.project_id, self.version = projects[0]
        if self.version not in SUPPORTED_VERSIONS:
            raise WriteError(f'Splice writes projects of Resolve 21 (project version 17); this one is version '
                             f'{self.version}. Open it in Resolve 21, save it, and try again.')
        missing = [c for c in items.ITEM_COLUMNS if c not in self.columns['Sm2TiItem']]
        if missing or len(self.columns['Sm2TiItem']) != len(items.ITEM_COLUMNS):
            raise WriteError('The seed\'s timeline items have other columns than Resolve 21.1 writes; it may come '
                             'from another Resolve version.')

    def rows(self, table, where='1', args=()):
        if table not in self.columns:
            return []
        cursor = self.conn.execute(f'SELECT * FROM {_q(table)} WHERE {where}', args)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, values)) for values in cursor]

    def row(self, table, rid):
        found = self.rows(table, f'{_q(table + "_id")} = ?', (rid,))
        return found[0] if found else None

    def ids(self):
        out = set()
        for table, columns in self.columns.items():
            if table + '_id' in columns:
                out.update(r[0] for r in self.conn.execute(f'SELECT {_q(table + "_id")} FROM {_q(table)}'))
        return {str(i) for i in out if i is not None}

    def insert(self, table, row):
        columns = self.columns.get(table)
        if columns is None:
            raise WriteError(f'The seed has no {table} table.')
        unknown = [c for c in row if c not in columns]
        if unknown:
            raise WriteError(f'The seed\'s {table} table has no column {unknown[0]}.')
        names = list(row)
        self.conn.execute(f'INSERT INTO {_q(table)} ({", ".join(_q(c) for c in names)}) VALUES '
                          f'({", ".join("?" for _ in names)})', [row[c] for c in names])

    def update(self, table, rid, values):
        if values:
            self.conn.execute(f'UPDATE {_q(table)} SET {", ".join(_q(c) + " = ?" for c in values)} '
                              f'WHERE {_q(table + "_id")} = ?', [*values.values(), rid])


# --------------------------------------------------------------------------- Splice values

def _fraction(text):
    try:
        num, _, den = str(text).partition('/')
        value = Fraction(int(num), int(den or 1))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


ROUNDED = 'Clip positions between frames rounded to the nearest frame (Resolve stores whole frames)'


def _frames(seconds, rate, what, where, rounded):
    """Whole frames of a time in seconds; ``what`` is added to ``rounded`` when the time lies
    between frames (reported only where the rounded value is written)."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds):
        raise WriteError(f'{where}: its {what} is not a number.')
    exact = seconds * rate.numerator / rate.denominator
    whole = round(exact)
    if abs(exact - whole) > 1e-3:
        rounded.add(what)
    return whole


def _seed_frames(row, column):
    value = row.get(column)
    if value is None:
        return 0.0 if column == 'In' else None
    try:
        frames, fraction = blobs.parse_frames(value)
    except blobs.BlobError:
        return None
    return frames + (fraction or 0.0)


def _whole(row, column):
    """The whole frames of a stored ``N`` or ``N|fraction`` value (what the reader reports)."""
    value = row.get(column)
    if value is None:
        return 0
    return blobs.parse_frames(value)[0]


def _volume_db(percent):
    """Decibels of a linear gain in percent, to a billionth of a dB (so +6 dB stays 6.0)."""
    return round(20 * math.log10(percent / 100), 9)


def _position_x(percent, width):
    """Resolve's Position X (a fraction of the timeline width) of Splice's x (a percentage of it):
    through whole micro-pixels, as Resolve's Inspector holds pixels (+200 px is 200 / 1920)."""
    return round(percent / 100 * width, 6) / width


def _imported(item, kind):
    """What the reader made of a seed item: its volume, position X and opacity (Splice units)."""
    out = {'volume': 100.0, 'x': 0.0, 'y': 0.0, 'scale': 100.0, 'opacity': 100.0, 'effects': True}
    data = item.get('EffectFiltersBA')
    if not data:
        out['effects'] = False
        return out
    try:
        payload = codec.unwrap(data, 'EffectFiltersBA')
        for effect, params, _plugin, _keyed in effects.decode(payload):
            if kind == 'audio' and effect == effects.AUDIO_VOLUME and isinstance(params.get(effects.VOLUME_DB), float):
                out['volume'] = importer.volume_percent(params[effects.VOLUME_DB])[0]
            if kind == 'video' and effect == effects.TRANSFORM:
                if isinstance(params.get(effects.POSITION_X), float):
                    out['x'] = round(params[effects.POSITION_X] * 100, 6)
                if isinstance(params.get(effects.POSITION_Y), float):
                    out['y'] = round(-params[effects.POSITION_Y] * 100, 6)
                zoom = [params.get(effects.ZOOM_X), params.get(effects.ZOOM_Y)]
                if any(isinstance(z, float) for z in zoom):
                    zx, zy = (z if isinstance(z, float) else 1.0 for z in zoom)
                    out['scale'] = round(100 * (zx + zy) / 2, 4)
                out['scale'] = min(400, max(10, out['scale']))
                out['x'], out['y'] = min(100, max(-100, out['x'])), min(100, max(-100, out['y']))
            if kind == 'video' and effect == effects.COMPOSITE and isinstance(params.get(effects.OPACITY), float):
                out['opacity'] = round(params[effects.OPACITY], 4)
    except (codec.CodecError, blobs.BlobError):
        pass
    return out


# --------------------------------------------------------------------------- the plan of one timeline

class Timeline:
    """One timeline of the seed: its sequence, rate, start and tracks."""

    def __init__(self, seed, entry):
        self.entry = entry
        self.id = entry['id']
        self.name = entry['name']
        self.container = entry['container'].get('DbId')
        self.sequence = entry['sequence'].get('DbId') if entry['sequence'] is not None else None
        self.media = entry['media']
        sequence = seed.row('Sm2Sequence', self.sequence) if self.sequence else None
        if sequence is None:
            raise WriteError(f'The timeline “{self.name}” has no sequence in the seed.')
        self.sequence_row = sequence
        try:
            self.rate = Fraction(*importer.rational(blobs.frame_rate(sequence['FrameRate'])))
            self.start_seconds, self.length = blobs.extents(sequence['MediaExtents'])
        except (blobs.BlobError, TypeError) as error:
            raise WriteError(f'The timeline “{self.name}” has an unreadable rate or start ({error}).') from None
        self.start = round(self.start_seconds * self.rate)
        self.tracks = {'video': [], 'audio': []}
        joins = seed.rows('Sm2SequenceContainer_Sm2TiTrack', 'DbOwner = ? ORDER BY DbIndex', (self.container,))
        for join in joins:
            track = seed.row('Sm2TiTrack', join['DbAssociate'])
            if track is None:
                continue
            kind = {'VideoTrackVec': 'video', 'AudioTrackVec': 'audio'}.get(join['DbPropertyName'])
            if kind:
                self.tracks[kind].append(track)
        self.items = {}
        self.order = {}
        for kind in self.tracks:
            for track in self.tracks[kind]:
                links = seed.rows('Sm2TiItem_Sm2TiTrack', "DbOwner = ? AND DbPropertyName = 'Items' ORDER BY DbIndex",
                                  (track['Sm2TiTrack_id'],))
                self.order[track['Sm2TiTrack_id']] = [link['DbAssociate'] for link in links]
                for link in links:
                    row = seed.row('Sm2TiItem', link['DbAssociate'])
                    if row is not None:
                        self.items[row['Sm2TiItem_id']] = (kind, track['Sm2TiTrack_id'], row)

    def track_id(self, kind, number):
        tracks = self.tracks.get(kind, [])
        return tracks[number - 1]['Sm2TiTrack_id'] if 1 <= number <= len(tracks) else None


class Writer:
    """Plans and applies the changes of every timeline, then the project's own."""

    def __init__(self, seed, model, report, *, facts_for, allow_unverified=False, allow_unproven=False,
                 template_mode=False, keep_template_media=False, now=None):
        self.seed, self.model, self.report = seed, model, report
        self.facts_for = facts_for
        self.allow_unverified, self.allow_unproven = allow_unverified, allow_unproven
        self.template_mode, self.keep_template_media = template_mode, keep_template_media
        self.now = int(now if now is not None else time.time())
        self.taken = seed.ids()
        self.clips, skipped = mediapool.clips_from_rows({t: seed.rows(t) for t in mediapool.MEDIA_TABLES})
        self.skipped_clips = skipped
        self.authored = {}       # local path -> AuthoredClip
        self.expected = []       # per timeline: (Timeline, {item id: expectation}, transitions)
        self.inserts, self.updates, self.deletes = [], [], []
        self.track_orders = {}   # track id -> [(start, is clip, item id)]
        self.track_updates = {}
        self.sequence_updates = {}
        self.renames = []
        self.removed_media = []
        self.operations = set()
        self.delta = 0
        self.tail = items.picture_tail(seed.rows('Sm2TiItem', "DbType = 'Sm2TiVideoClip'"))

    # -- ids -----------------------------------------------------------------------------
    def fresh(self):
        while True:
            value = str(uuid.uuid4())
            if value not in self.taken:
                self.taken.add(value)
                return value

    # -- media ---------------------------------------------------------------------------
    def clip_for_media(self, media, where):
        """The ClipRows a Splice media entry plays: the seed's pool clip, or one authored from the
        file's facts (None, reported, when neither can be had)."""
        rid = media.get('resolveMediaId')
        if rid and rid in self.clips:
            return self.clips[rid]
        if rid and rid in self.skipped_clips:
            self.report.add(NOT_WRITTEN, 'Clips whose media-pool entry Splice cannot read as a file clip (its rows '
                            f'are not a complete clip: {self.skipped_clips[rid]})', where)
            return None
        if media.get('nativeUnsupported'):
            self.report.add(NOT_WRITTEN, f'{media["nativeUnsupported"]} placeholders (Splice does not author them)',
                            where)
            return None
        return self.author_media(media, where)

    def author_media(self, media, where):
        facts, local = self.facts_for(media)
        if facts is None:
            self.report.add(NOT_WRITTEN, f'Clips whose file could not be measured ({local})', where)
            return None
        key = facts.path
        if key in self.authored:
            return self.authored[key]
        kind = mediapool.kind_of_facts(facts)
        templates = []
        for clip in self.clips.values():
            try:
                if mediapool.kind_of_rows(clip)[:7] == kind[:7]:
                    templates.append(clip)
            except mediapool.MediaPoolError:
                continue
        if not templates:
            status = mediapool.KINDS.get(kind)
            why = ('no clip of this kind is in the seed\'s media pool to serve as the template' if status else
                   f'no evidence yet that a {mediapool.kind_label(kind)} can be authored')
            self.report.add(NOT_WRITTEN, f'Clips of new media Splice cannot add to the Resolve media pool ({why})', where)
            return None
        bin_id = self.timeline_bin()
        index = len(self.seed.rows('Sm2MpFolder_Sm2MpMedia', 'DbOwner = ?', (bin_id,))) + \
            sum(1 for a in self.authored.values() if a)
        maps = self.seed.rows('BtLockableBlobMap')
        placement = mediapool.Placement(bin_id, index, self.project_rate(), self.now,
                                        maps[0]['BtLockableBlobMap_id'] if len(maps) == 1 else None, 0)
        last = None
        for template in templates:
            template_facts = self.facts_for_template(template)
            try:
                authored = mediapool.author(facts, mediapool.Template(template, template_facts), placement,
                                            taken=frozenset(self.taken), allow_unverified=self.allow_unverified)
            except mediapool.MediaPoolError as error:
                last = error
                if error.code in ('kind_mismatch', 'bad_template'):
                    continue
                break
            self.authored[key] = authored
            self.taken |= mediapool.ClipRows.from_tables(authored.rows).ids()
            if authored.status == 'unverified':
                self.report.add(OPT_IN, 'New media written although its rows are not verified (you allowed it): '
                                + '; '.join(authored.unverified)[:400], facts.name, need='unverifiedMedia')
            else:
                self.report.add(EXACT, 'New media added to the media pool (rows authored from the file\'s measured '
                                'facts, a verified kind)', facts.name)
            self.operations.add('add_media')
            return authored
        if last is not None and last.code == 'unverified':
            self.report.add(OPT_IN, 'New media not written: its rows would not be verified. Allow unverified media to '
                            f'write it anyway ({last.message[:300]})', where, need='unverifiedMedia')
        else:
            self.report.add(NOT_WRITTEN, 'Clips of new media Splice cannot author ('
                            + (last.message[:300] if last else 'no template') + ')', where)
        self.authored[key] = None
        return None

    def facts_for_template(self, clip):
        try:
            folder, name = mediapool.clip_name(clip)
        except mediapool.MediaPoolError:
            return None
        separator = '\\' if '\\' in folder or (len(folder) > 1 and folder[1] == ':') else '/'
        return self.facts_for({'resolveMediaId': clip.media_id, 'templatePath': folder + separator + name},
                              template=True)[0]

    def timeline_bin(self):
        timeline_clip = self.seed.row('Sm2MpMedia', self.current.media) if self.current.media else None
        if timeline_clip and timeline_clip.get('MpFolder'):
            return timeline_clip['MpFolder']
        folders = self.seed.rows('Sm2MpFolder', 'MpFolder IS NULL')
        if not folders:
            raise WriteError('The seed has no media pool bin to add media to.')
        return folders[0]['Sm2MpFolder_id']

    def project_rate(self):
        rate = mediapool.project_frame_rate(self.seed.rows('SM_Config'))
        return rate if rate is not None else self.current.rate

    def view(self, clip_rows):
        if isinstance(clip_rows, mediapool.AuthoredClip):
            clip_rows = mediapool.ClipRows.from_tables(clip_rows.rows)
        return items.media_view(clip_rows)

    # -- one timeline --------------------------------------------------------------------
    def write_timeline(self, project, entry):
        self.current = timeline = Timeline(self.seed, entry)
        report = self.report
        name = project.get('name') or timeline.name
        rate = _fraction(project.get('frameRate')) or (Fraction(project['fps']).limit_denominator(1001)
                                                       if isinstance(project.get('fps'), (int, float)) else None)
        if rate != timeline.rate:
            raise WriteError(f'“{name}” runs at {float(rate or 0):g} fps in Splice and {float(timeline.rate):g} fps in '
                             'the seed. Changing a timeline\'s frame rate is not written; use a seed of the same rate.')
        start = timeline.start
        wanted_tc = project.get('startTimecode')
        day = 24 * 3600 * max(1, round(timeline.rate))
        if isinstance(wanted_tc, int) and not isinstance(wanted_tc, bool) and timeline.start >= 0 \
                and wanted_tc != timeline.start % day and 0 <= wanted_tc < day:
            start = wanted_tc
            report.add(EXACT, 'Timeline start timecode changed', name)
        self.delta = start - timeline.start
        width = project.get('width')
        self.width = width if isinstance(width, int) and not isinstance(width, bool) and 0 < width <= 32768 else 1920
        if name != timeline.name:
            self.renames.append((timeline, name))
            report.add(EXACT, 'Timeline renamed', f'{timeline.name} → {name}')
        clips = [c for c in project.get('clips') or [] if isinstance(c, dict)]
        if len(clips) > MAX_CLIPS:
            raise WriteError(f'“{name}” has more than {MAX_CLIPS:,} clips.')
        media = {m.get('id'): m for m in project.get('media') or [] if isinstance(m, dict)}
        self._tracks(project, timeline, name)
        expected, claimed = {}, set()
        placed = []  # (clip, item id, kind, track id, start, duration, in, new row or None)
        for clip in sorted(clips, key=lambda c: (c.get('start') or 0, str(c.get('id')))):
            outcome = self._clip(clip, media.get(clip.get('mediaId')) or {}, timeline, start, claimed, name)
            if outcome:
                placed.append(outcome)
        self._links(placed, timeline, name)
        self._effects(placed, timeline, name)
        transitions = self._transitions(placed, timeline, start, name)
        # Seed items no Splice clip claims: removed when the reader made a clip of them, kept
        # otherwise (items Splice never imported stay as they are).
        for item_id, (kind, track_id, row) in timeline.items.items():
            if item_id not in claimed and row['DbType'] != items.TRANSITION and self._imported_item(row, timeline):
                self._delete(item_id, row, timeline, name)
        self._orders(placed, transitions, timeline, start)
        for clip, item_id, kind, track_id, s, d, i, new, view, partners in placed:
            expected[item_id] = {'kind': kind, 'track': track_id, 'start': s - start, 'duration': d, 'in': i,
                                 'partners': None if partners is None else set(partners),
                                 'disabled': kind == 'video' and clip.get('enabled') is False and
                                 'disabled' not in clip.get('_skip', ())}
        self.expected.append((timeline, expected, transitions, start, name))
        if start != timeline.start or self._timeline_changed(timeline):
            self.sequence_updates[timeline.sequence] = (start, timeline)

    def _imported_item(self, row, timeline):
        """Whether the reader turned this seed item into a Splice clip (so its absence in Splice
        means the clip was deleted)."""
        start = _seed_frames(row, 'Start')
        duration = _seed_frames(row, 'Duration')
        if start is None or duration is None or duration <= 0 or start < timeline.start:
            return False
        return row['DbType'] in ('Sm2TiVideoClip', 'Sm2TiAudioClip', 'Sm2TiGenerator')

    def _tracks(self, project, timeline, name):
        state = project.get('trackState') if isinstance(project.get('trackState'), dict) else {}
        mix = project.get('trackMix') if isinstance(project.get('trackMix'), dict) else {}
        names = project.get('trackNames') if isinstance(project.get('trackNames'), dict) else {}
        counts = project.get('trackCounts') if isinstance(project.get('trackCounts'), dict) else {}
        for kind in ('video', 'audio'):
            seed_tracks = timeline.tracks[kind]
            extra = (counts.get(kind) or 0) - len(seed_tracks)
            for number, track in enumerate(seed_tracks, 1):
                if number > importer.MAX_TRACKS:
                    break
                key = f'{kind}:{number if kind == "video" else -number}'
                flags = track.get('Flags') or 0
                if not isinstance(flags, int):
                    continue
                st = state.get(key) or {}
                locked = bool(st.get('locked'))
                off = bool(st.get('hidden')) if kind == 'video' else bool((mix.get(str(-number)) or {}).get('mute'))
                wanted = (flags & ~(TRACK_LOCKED | TRACK_OFF)) | (TRACK_LOCKED if locked else 0) | \
                    (TRACK_OFF if off else 0)
                label = f'{name} · {kind.title()} {number}'
                if wanted != flags:
                    if self.allow_unproven:
                        self.track_updates.setdefault(track['Sm2TiTrack_id'], {})['Flags'] = wanted
                        self.report.add(OPT_IN, 'Track lock, hide or mute changed: written to the track (Resolve also '
                                        'keeps these switches in its Fairlight mixer, which Splice does not update; '
                                        'you allowed it)', label, need='unproven')
                    else:
                        self.report.add(OPT_IN, 'Track lock, hide or mute changes not written: Resolve also keeps them '
                                        'in its Fairlight mixer, which Splice does not update. Allow unproven changes '
                                        'to write them to the track anyway', label, need='unproven')
                default = ('Video ' if kind == 'video' else 'Audio ') + str(number)
                stored = track.get('UserDefinedName')
                wanted_name = names.get(key)
                current = stored or default
                if isinstance(wanted_name, str) and wanted_name and wanted_name != current:
                    self.track_updates.setdefault(track['Sm2TiTrack_id'], {})['UserDefinedName'] = wanted_name[:256]
                    self.report.add(EXACT, 'Track renamed', f'{label} → {wanted_name[:80]}')
            if extra > 0:
                self.report.add(NOT_WRITTEN, f'New {kind} tracks (adding a track to a Resolve timeline also needs its '
                                'Fairlight mixer entries, which are not proven): their clips are listed below',
                                f'{name}: {extra} track(s)')

    def _clip(self, clip, media, timeline, start, claimed, name):
        """``(clip, item id, kind, track id, start, duration, in, new row, view, partners)`` of a
        Splice clip that will be on the timeline, or None (reported)."""
        report = self.report
        kind = clip.get('type')
        track_number = clip.get('track')
        where = f'{name} · {clip.get("title") or media.get("name") or "Clip"} at {clip.get("start", 0):.2f} s'
        clip['_skip'] = []
        if kind not in ('video', 'audio'):
            report.add(NOT_WRITTEN, 'Titles and other Splice-only clips (Resolve titles are Fusion compositions)', where)
            return None
        if type(track_number) is not int:
            report.add(NOT_WRITTEN, 'Clips without a track', where)
            return None
        number = track_number if kind == 'video' else -track_number
        track_id = timeline.track_id(kind, number)
        rid = clip.get('resolveItemId')
        seed = timeline.items.get(rid) if isinstance(rid, str) else None
        if seed is not None and rid in claimed:
            seed = None  # a second part of a split clip: a new item
        if seed is not None and (seed[0] != kind or (seed[2].get('MediaRef') or None) !=
                                 (media.get('resolveMediaId') or seed[2].get('MediaRef') or None)):
            seed = None
        if track_id is None:
            report.add(NOT_WRITTEN, 'Clips on tracks the Resolve timeline does not have (new tracks are not written)'
                       + ('; the Resolve item stays where it was' if seed else ''), where)
            if seed is not None:
                claimed.add(rid)
            return None
        rounded = set()
        rel = _frames(clip.get('start'), timeline.rate, 'start', where, rounded)
        duration = _frames(clip.get('duration'), timeline.rate, 'duration', where, rounded)
        source_in = _frames(clip.get('in') or 0, timeline.rate, 'source in point', where, rounded)
        if rel < 0 or duration < 1 or source_in < 0:
            report.add(NOT_WRITTEN, 'Clips before the timeline start or shorter than one frame', where)
            return None
        self._not_written(clip, kind, seed, where)
        if seed is not None:
            claimed.add(rid)
            _kind, old_track, row = seed
            update = {}
            old = (_seed_frames(row, 'Start'), _seed_frames(row, 'Duration'), _seed_frames(row, 'In'))
            exact = (clip.get('start') * timeline.rate, clip.get('duration') * timeline.rate,
                     (clip.get('in') or 0) * timeline.rate)
            retimed = not (isinstance(row.get('MediaTimemapBA'), bytes) and len(row['MediaTimemapBA']) == 9) \
                and row.get('MediaTimemapBA') is not None
            if old[0] is None or abs((old[0] - timeline.start) - exact[0]) > _EPSILON or start != timeline.start:
                update['Start'] = str(start + rel)
            else:
                rel = _whole(row, 'Start') - timeline.start
            if old[1] is None or abs(old[1] - exact[1]) > _EPSILON:
                if retimed:
                    report.add(NOT_WRITTEN, 'Trims of retimed (speed-changed) Resolve clips (the clip keeps Resolve\'s '
                               'length and in point)', where)
                    duration = _whole(row, 'Duration')
                else:
                    update['Duration'] = str(duration)
            else:
                duration = _whole(row, 'Duration')
            if old[2] is not None and abs(old[2] - exact[2]) > _EPSILON:
                if retimed or row['DbType'] not in ('Sm2TiVideoClip', 'Sm2TiAudioClip'):
                    if not retimed:
                        report.add(NOT_WRITTEN, 'Changes to the in point of generators and titles', where)
                    source_in = _whole(row, 'In')
                else:
                    update['In'] = str(source_in) if source_in else None
            elif old[2] is not None:
                source_in = _whole(row, 'In')
            if old_track != track_id:
                update['Sm2TiTrack_id'] = track_id
            if row['DbType'] == 'Sm2TiVideoClip' and isinstance(row.get('Flags'), int):
                flags = row['Flags']
                wanted = (flags & ~CLIP_OFF) | (CLIP_OFF if clip.get('enabled') is False else 0)
                if wanted != flags:
                    update['Flags'] = wanted
                    report.add(EXACT, 'Clips disabled or enabled', where)
            elif clip.get('enabled') is False and row['DbType'] == 'Sm2TiAudioClip':
                if not (isinstance(row.get('Flags'), int) and row['Flags'] & CLIP_OFF):
                    report.add(NOT_WRITTEN, 'Disabled sound clips (no Resolve sample shows how a sound clip is '
                               'disabled)', where)
                clip['_skip'].append('disabled')
            if not row.get('MediaRef') and row['DbType'] in ('Sm2TiVideoClip', 'Sm2TiAudioClip'):
                view = None
            elif row['DbType'] in ('Sm2TiVideoClip', 'Sm2TiAudioClip') and row.get('MediaRef') in self.clips:
                view = self.view(self.clips[row['MediaRef']])
            else:
                view = None
            written = {'start': 'Start', 'duration': 'Duration', 'source in point': 'In'}
            if any(written[w] in update for w in rounded):
                report.add(EXACT, ROUNDED, where)
            if update:
                self.updates.append(('Sm2TiItem', rid, update))
                if set(update) & {'Start', 'Sm2TiTrack_id'}:
                    self.operations.add('move_items')
                if set(update) & {'Duration', 'In'}:
                    self.operations.add('trim_items')
            try:
                current = list(items.links_of(row)) if row['DbType'] in (items.PICTURE, items.SOUND) else []
            except mediapool.MediaPoolError:
                current = None  # link data Splice cannot read: the item's links are kept as they are
            return (clip, rid, kind, track_id, start + rel, duration, source_in, None, view, current)
        # A new item.
        if clip.get('speed', 1) != 1 or clip.get('reverse'):
            report.add(NOT_WRITTEN, 'New clips with a speed change or reversed (speed changes are not written)', where)
            return None
        if media.get('nativeUnsupported') or clip.get('nativeUnsupported'):
            report.add(NOT_WRITTEN, 'Copies of Resolve generators, titles, compound and multicam clips', where)
            return None
        rows = self.clip_for_media(media, where)
        if rows is None:
            return None
        try:
            view = self.view(rows)
        except mediapool.MediaPoolError as error:
            report.add(NOT_WRITTEN, f'Clips whose media-pool entry cannot be read ({error.message[:200]})', where)
            return None
        picture = 'picture' if kind == 'video' else 'sound'
        item_id = self.fresh()
        stream = clip.get('audioStream')
        if kind == 'audio' and isinstance(stream, int) and stream > 0:
            report.add(NOT_WRITTEN, 'New sound clips playing an audio stream other than the first', where)
            return None
        try:
            row, join = items.author_item(view, picture, track_id, 0, start + rel, duration, source_in, (), item_id,
                                          self.seed.columns['Sm2TiItem'], tail=self.tail)
        except mediapool.MediaPoolError as error:
            report.add(NOT_WRITTEN, f'New clips the item writer refuses ({error.message[:200]})', where)
            return None
        if rounded:
            report.add(EXACT, ROUNDED, where)
        if kind == 'video' and clip.get('enabled') is False:
            row['Flags'] = CLIP_OFF
            report.add(EXACT, 'Clips disabled or enabled', where)
        elif kind == 'audio' and clip.get('enabled') is False:
            report.add(NOT_WRITTEN, 'Disabled sound clips (no Resolve sample shows how a sound clip is disabled)', where)
            clip['_skip'].append('disabled')
        self.inserts.append(('Sm2TiItem', row))
        self.operations.add('add_items')
        return (clip, item_id, kind, track_id, start + rel, duration, source_in, row, view, [])

    def _not_written(self, clip, kind, seed, where):
        """Report every edit of ``clip`` the writer does not carry into Resolve."""
        report = self.report
        before = _imported(seed[2], kind) if seed else None
        speed = clip.get('speed', 1)
        if (speed != 1 or clip.get('reverse')) and seed is None:
            pass  # reported where new clips are refused
        elif seed is not None and not (isinstance(seed[2].get('MediaTimemapBA'), bytes) and
                                       len(seed[2]['MediaTimemapBA']) == 9) and seed[2].get('MediaTimemapBA'):
            pass  # a retimed Resolve clip keeps its own speed
        elif speed != 1 or clip.get('reverse'):
            report.add(NOT_WRITTEN, 'Speed changes and reversed clips', where)
        if clip.get('keyframes'):
            report.add(NOT_WRITTEN, 'Keyframes', where)
        if isinstance(clip.get('color'), dict) and any(v not in (0, None, '', False) for v in clip['color'].values()):
            report.add(NOT_WRITTEN, 'Splice color adjustments and LUTs (Resolve grades are not written)', where)
        if clip.get('lut'):
            report.add(NOT_WRITTEN, 'Splice color adjustments and LUTs (Resolve grades are not written)', where)
        if (clip.get('fadeIn') or 0) > 0 or (clip.get('fadeOut') or 0) > 0:
            report.add(NOT_WRITTEN, 'Clip fades (Resolve fade handles are not written)', where)
        if kind == 'video':
            transform = clip.get('transform') if isinstance(clip.get('transform'), dict) else {}
            old = before or {'x': 0.0, 'y': 0.0, 'scale': 100.0}
            changed = [k for k, d in (('y', 0.0), ('scale', 100.0), ('rotation', 0.0))
                       if abs(float(transform.get(k, d) or 0) - float((old or {}).get(k, d) if k != 'rotation' else 0))
                       > _EPSILON and not (k == 'rotation' and not transform.get(k))]
            if changed:
                report.add(NOT_WRITTEN, 'Zoom, vertical position and rotation (transforms beyond position X are not '
                           'proven)', where)
            opacity = clip.get('opacity', 100)
            imported = (before or {}).get('opacity', 100.0)
            stand_in = clip.get('enabled') is False and opacity == 0
            if isinstance(opacity, (int, float)) and abs(opacity - imported) > _EPSILON and not stand_in:
                report.add(NOT_WRITTEN, 'Opacity changes', where)
        elif clip.get('pan'):
            report.add(NOT_WRITTEN, 'Clip pan', where)

    def _links(self, placed, timeline, name):
        """Links follow Splice's link groups. A group whose members are linked in the seed exactly
        as it groups them (the reader groups linked items transitively) keeps its link lists as
        they are; a changed group gets lists naming each item's partners of the other kind."""
        groups = {}
        for entry in placed:
            if entry[0].get('linkGroup'):
                groups.setdefault(entry[0]['linkGroup'], []).append(entry)
        component = self._components(placed)
        for n, entry in enumerate(placed):
            clip, item_id, kind = entry[0], entry[1], entry[2]
            where = f'{name} · {clip.get("title") or "Clip"}'
            group = groups.get(clip.get('linkGroup'), []) if clip.get('linkGroup') else [entry]
            members = {other[1] for other in group}
            current = entry[9]
            if entry[7] is None and current is not None and component.get(item_id) == members:
                continue  # the seed's own links, unchanged
            if current is None:
                self.report.add(EXACT, 'Clips whose link data Splice cannot read keep their Resolve links as they are',
                                where)
                continue
            partners = [other[1] for other in sorted(group, key=lambda o: (o[2] != 'video', o[4], o[3]))
                        if other[1] != item_id and other[2] != kind]
            if any(other[2] == kind and other[1] != item_id for other in group):
                self.report.add(NOT_WRITTEN, 'Links between two pictures or two sounds (Resolve links a picture with '
                                'its sound)', where)
            placed[n] = entry[:9] + (partners,)
            if entry[7] is not None:
                entry[7]['FieldsBlob'] = items.fresh_fields('picture' if kind == 'video' else 'sound', partners,
                                                            self.tail)
                if partners:
                    self.operations.add('add_linked_items')
                continue
            if set(current) == set(partners):
                placed[n] = entry[:9] + (list(current),)
                continue
            row = self.current.items[item_id][2]
            try:
                blob = items.relink(row, partners)
            except mediapool.MediaPoolError as error:
                self.report.add(NOT_WRITTEN, f'Link changes on clips whose link data cannot be edited '
                                f'({error.message[:120]})', where)
                placed[n] = entry[:9] + (list(current),)
                continue
            self._update('Sm2TiItem', item_id, {'FieldsBlob': blob})
            self.report.add(EXACT, 'Clips linked or unlinked', where)

    @staticmethod
    def _components(placed):
        """Each placed seed item's group under its current link lists (transitively, as the
        reader groups them), over the placed items."""
        ids = {entry[1] for entry in placed}
        parent = {i: i for i in ids}

        def root(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for entry in placed:
            for partner in entry[9] or ():
                if partner in ids:
                    parent[root(entry[1])] = root(partner)
        out = {}
        for i in ids:
            out.setdefault(root(i), set()).add(i)
        return {i: out[root(i)] for i in ids}

    def _update(self, table, rid, values):
        for entry in self.updates:
            if entry[0] == table and entry[1] == rid:
                entry[2].update(values)
                return
        self.updates.append((table, rid, dict(values)))

    def _effects(self, placed, timeline, name):
        for entry in placed:
            clip, item_id, kind, new_row = entry[0], entry[1], entry[2], entry[7]
            row = new_row if new_row is not None else timeline.items[item_id][2]
            where = f'{name} · {clip.get("title") or "Clip"} at {clip.get("start", 0):.2f} s'
            if new_row is None and (entry[8] is None or clip.get('nativeUnsupported')):
                continue  # Splice did not import this clip's effects (a generator, compound or multicam clip)
            before = _imported(row, kind)
            if kind == 'audio':
                volume = clip.get('volume', 100)
                if not isinstance(volume, (int, float)) or abs(volume - before['volume']) <= _EPSILON:
                    continue
                if volume <= 0:
                    self.report.add(NOT_WRITTEN, 'Silenced clips (a volume of 0 % has no decibel value)', where)
                    continue
                value = _volume_db(volume)
                try:
                    blob = effects.single(effects.AUDIO_VOLUME, effects.VOLUME_DB, value) if not row.get(
                        'EffectFiltersBA') else effects.replace(row['EffectFiltersBA'], effects.AUDIO_VOLUME,
                                                                effects.VOLUME_DB, value)
                except codec.CodecError:
                    self.report.add(NOT_WRITTEN, 'Volume changes on clips that carry other Resolve audio effects', where)
                    continue
                self._set(entry, item_id, 'EffectFiltersBA', blob)
                self.report.add(EXACT, 'Clip volume changes (written in decibels, as Resolve stores them)', where)
                entry[0]['_volume'] = volume
            else:
                transform = clip.get('transform') if isinstance(clip.get('transform'), dict) else {}
                x = transform.get('x', 0) or 0
                if not isinstance(x, (int, float)) or abs(x - before['x']) <= _EPSILON:
                    continue
                if not self.allow_unproven:
                    self.report.add(OPT_IN, 'Position X changes not written: Resolve also stamps the clip when its '
                                    'Inspector changes, and that stamp is not reproduced. Allow unproven changes to '
                                    'write the position anyway', where, need='unproven')
                    continue
                value = _position_x(x, self.width)
                try:
                    blob = effects.single(effects.TRANSFORM, effects.POSITION_X, value) if not row.get(
                        'EffectFiltersBA') else effects.replace(row['EffectFiltersBA'], effects.TRANSFORM,
                                                                effects.POSITION_X, value)
                except codec.CodecError:
                    self.report.add(NOT_WRITTEN, 'Position X changes on clips that carry other Resolve effects', where)
                    continue
                self._set(entry, item_id, 'EffectFiltersBA', blob)
                self.report.add(OPT_IN, 'Position X written (the Inspector change stamp Resolve adds is not; you '
                                'allowed it)', where, need='unproven')

    def _set(self, entry, item_id, column, value):
        if entry[7] is not None:
            entry[7][column] = value
        else:
            self._update('Sm2TiItem', item_id, {column: value})

    def _crossfade_gate(self, entry, by_track, rate):
        """``(problem or None, frames, frames before the cut, Resolve name, outgoing entry)`` of
        the sound crossfade a Splice clip asks for at its head: the one gate every sound
        crossfade the writer authors passes (new ones and changed Resolve ones alike)."""
        clip, kind, track_id, s = entry[0], entry[2], entry[3], entry[4]
        style = clip.get('transition')
        frames = round(clip['transitionDuration'] * rate)
        before = round((clip.get('transitionBefore') or 0) * rate)
        outgoing = [o for o in by_track.get(track_id, []) if o is not entry and o[4] + o[5] == s]
        resolve_name = items.SPLICE_FADES.get(clip.get('transitionMatchName') or 'Constant Power')
        problem = items.transition_problem(resolve_name or str(clip.get('transitionMatchName')), frames, rate)
        if problem is None and style != 'dissolve':
            problem = 'a fade at the start of a clip is not a crossfade between two clips'
        if problem is None and len(outgoing) != 1:
            problem = 'it does not sit between two clips that meet'
        if problem is None and before != items.frames_before(frames):
            problem = f'it is not centred on the cut the way Resolve centres one ({items.frames_before(frames)} ' \
                      f'frames before it)'
        if problem is None:
            other = outgoing[0]
            after = frames - before
            if other[7] is None and other[8] is None or entry[8] is None:
                problem = 'the media its neighbours play cannot be read'
            elif entry[6] < before:
                problem = 'the incoming clip has too few frames before its in point'
            elif other[6] + other[5] + after > (other[8].frames if other[8].picture and other[2] == 'video'
                                                 else other[8].sound_frames or 0):
                problem = 'the outgoing clip has too few frames after its out point'
            elif after > entry[5] or before > other[5]:
                problem = 'it is longer than one of its clips'
        return problem, frames, before, resolve_name, outgoing

    @staticmethod
    def _asked(clip, rate):
        """What a Splice clip asks for at its head: ``(style, frames, frames before, match name)``
        in the reader's terms, or None for a cut."""
        style = clip.get('transition')
        if style in (None, 'none') or not (clip.get('transitionDuration') or 0) > 0:
            return None
        return (style, round(clip['transitionDuration'] * rate), round((clip.get('transitionBefore') or 0) * rate),
                clip.get('transitionMatchName') if clip.get('type') == 'audio' else None)

    @staticmethod
    def _as_read(row, kind, s, d, cut):
        """What the reader made of a seed transition at ``cut`` (as :meth:`_asked` puts it)."""
        pretty = row.get('PrettyType')
        if kind == 'audio':
            style, match = 'dissolve', importer._AUDIO_FADES.get(pretty)
        else:
            style, match = ('fade' if cut == s else 'dissolve') if pretty in (None, 'Cross Dissolve') else pretty, None
        return (style, round(d), round(cut - s) if style != 'fade' else 0, match)

    def _transitions(self, placed, timeline, start, name):
        """Seed transitions that still sit on their cut are kept as they are, unless Splice changed
        them: a changed sound crossfade the evidence covers is written anew (the seed's removed),
        any other change is reported and the seed's transition kept. Sound crossfades Splice added
        are written where the evidence covers them; the rest is reported. A Splice crossfade on a
        cut that keeps a Resolve transition never adds a second one."""
        out = []
        by_track = {}
        for entry in placed:
            by_track.setdefault(entry[3], []).append(entry)
        kept = set()
        replaced = set()      # Splice clips whose seed transition is written anew
        carriers = {entry[0].get('resolveTransitionId'): entry for entry in placed
                    if entry[0].get('resolveTransitionId')}
        touched = {u[1] for u in self.updates if set(u[2]) & {'Start', 'Duration', 'In', 'Sm2TiTrack_id'}}
        kept_spans = {}       # track id -> [(start, end, seed item id)]
        for item_id, (kind, track_id, row) in timeline.items.items():
            if row['DbType'] != items.TRANSITION:
                continue
            s, d = _seed_frames(row, 'Start'), _seed_frames(row, 'Duration')
            if s is not None:
                s += self.delta
            neighbours = by_track.get(track_id, [])
            order = timeline.order.get(track_id, [])
            at = order.index(item_id) if item_id in order else -1
            around = [order[at - 1] if at > 0 else None, order[at + 1] if 0 <= at < len(order) - 1 else None]
            placed_ids = {e[1] for e in neighbours}
            if not self.delta and all(n in placed_ids and n not in touched for n in around):
                meets = True  # both clips it joins are where they were
            else:
                meets = s is not None and d is not None and any(
                    e[4] <= s + d and s <= e[4] and any(o[4] + o[5] == e[4] for o in neighbours if o is not e)
                    for e in neighbours)
            carrier = carriers.get(item_id)
            removed = carrier is not None and carrier[0].get('transition') in (None, 'none')
            where = f'{name} · {row.get("PrettyType") or "Transition"}'
            if meets and not removed and carrier is not None and s is not None and d is not None:
                asked = self._asked(carrier[0], timeline.rate)
                seen = self._as_read(row, kind, _whole(row, 'Start') + self.delta, d, carrier[4])
                if asked is not None and asked != seen:
                    if kind == 'audio' and asked[0] == 'dissolve':
                        problem = self._crossfade_gate(carrier, by_track, timeline.rate)[0]
                    else:
                        problem = 'picture transitions are not written (no Resolve sample proves their rows)'
                    if problem is None and self._work(item_id, row):
                        problem = 'the Resolve transition carries other Resolve work'
                    if problem is None:
                        self._delete(item_id, row, timeline, name, transition=True, changed=True)
                        replaced.add(carrier[0].get('id'))
                        continue
                    self.report.add(NOT_WRITTEN, 'Changes to Resolve transitions (length, alignment or kind) not '
                                    f'written: the transition stays as it is in Resolve ({problem})',
                                    f'{where} at {carrier[0].get("start", 0):.2f} s')
            if meets and not removed:
                kept.add(item_id)
                out.append({'id': item_id, 'track': track_id, 'start': _whole(row, 'Start') + self.delta,
                            'duration': round(d), 'seed': True,
                            'kind': kind, 'pretty': row.get('PrettyType')})
                kept_spans.setdefault(track_id, []).append((s, s + d, item_id))
                if self.delta:
                    self._shift(item_id, row)
                if carrier is None and row.get('PrettyType') not in items.SOUND_KINDS.values():
                    self.report.add(EXACT, 'Resolve transitions Splice does not show, kept on their cut', where)
            else:
                self._delete(item_id, row, timeline, name, transition=True, removed=removed)
        for entry in placed:
            clip, item_id, kind, track_id, s = entry[0], entry[1], entry[2], entry[3], entry[4]
            style = clip.get('transition')
            if style not in ('dissolve', 'fade') or not (clip.get('transitionDuration') or 0) > 0:
                continue
            seed_id = clip.get('resolveTransitionId')
            if seed_id in kept:
                continue
            where = f'{name} · {clip.get("title") or "Clip"} at {clip.get("start", 0):.2f} s'
            frames = round(clip['transitionDuration'] * timeline.rate)
            before = round((clip.get('transitionBefore') or 0) * timeline.rate)
            span = (s - before, s - before + frames)
            on_cut = [k for k in kept_spans.get(track_id, ()) if k[0] < span[1] and span[0] < k[1]]
            if on_cut and clip.get('id') not in replaced:
                # A Splice crossfade on a cut where a Resolve transition is kept (a clip that lost
                # its link to it): the Resolve one stays, and a second one is never added.
                seed_row = timeline.items[on_cut[0][2]][2]
                seen = self._as_read(seed_row, kind, _whole(seed_row, 'Start') + self.delta,
                                     on_cut[0][1] - on_cut[0][0], s)
                if self._asked(clip, timeline.rate) != seen:
                    self.report.add(NOT_WRITTEN, 'Transitions on a cut that keeps a Resolve transition (the Resolve '
                                    'one stays as it is; a second one is not added)', where)
                continue
            if kind == 'video':
                self.report.add(NOT_WRITTEN, 'Picture transitions and fades (no Resolve sample proves their rows)', where)
                continue
            problem, frames, before, resolve_name, _outgoing = self._crossfade_gate(entry, by_track, timeline.rate)
            if problem:
                self.report.add(NOT_WRITTEN, f'Sound crossfades Resolve cannot take as written ({problem})', where)
                continue
            transition_id = self.fresh()
            row, _join = items.author_transition(resolve_name, track_id, 0, s, frames, timeline.rate, transition_id)
            self.inserts.append(('Sm2TiItem', row))
            out.append({'id': transition_id, 'track': track_id, 'start': s - items.frames_before(frames),
                        'duration': frames, 'seed': False, 'kind': 'audio', 'name': resolve_name,
                        'pretty': items.SOUND_KINDS[resolve_name]})
            kept_spans.setdefault(track_id, []).append((s - before, s - before + frames, transition_id))
            self.operations.add('add_sound_transitions')
            if clip.get('id') in replaced:
                self.report.add(EXACT, 'Resolve sound crossfades changed (length or kind): written anew the way '
                                'Resolve writes one, centred on the cut', where)
            else:
                self.report.add(EXACT, 'Sound crossfades (Cross Fade 0 dB or +3 dB, centred on the cut)', where)
        return out

    def _work(self, item_id, row):
        """The Resolve work an item carries (grade, Fusion, effects, markers, rows that belong to
        it, items it owns), named; empty when it carries none."""
        owned = [table for table, columns in self.seed.columns.items()
                 if 'Sm2TiItem_id' in columns and table != 'Sm2TiItem' and
                 self.seed.conn.execute(f'SELECT 1 FROM {_q(table)} WHERE Sm2TiItem_id = ? LIMIT 1', (item_id,)).fetchone()]
        later = [tag for column, tag in items.LATER_COLUMNS.items() if row.get(column) is not None]
        markers = self.seed.rows('BtLockableBlob', 'BlobOwner = ?', (item_id,))
        children = self.seed.rows('Sm2TiItem', 'Sm2TiItem_Owner_id = ?', (item_id,))
        return sorted(set(later + (['markers'] if markers else []) + owned + (['owned items'] if children else [])))

    def _delete(self, item_id, row, timeline, name, transition=False, removed=False, changed=False):
        work = self._work(item_id, row)
        where = f'{name} · {row.get("Name") or "Item"}'
        if work:
            what = ', '.join(work)
            self.report.add(NOT_WRITTEN, 'Deleted clips that carry Resolve work (grade, Fusion, effects, markers): '
                            'Splice does not delete them, so they stay in the Resolve timeline', f'{where} ({what})')
            self.kept_decorated = getattr(self, 'kept_decorated', set()) | {item_id}
            return False
        self.deletes.append(item_id)
        self.operations.add('remove_items')
        if changed:
            return True  # reported with the crossfade written in its place
        what = ('Transitions removed in Splice' if removed else 'Transitions removed because their cut changed') \
            if transition else 'Clips deleted'
        self.report.add(EXACT, what, where)
        return True

    def _orders(self, placed, transitions, timeline, start):
        """Every track's ``Items`` vector in record order (a transition before a clip starting on
        the same frame), and a check that no two clips overlap."""
        kept = getattr(self, 'kept_decorated', set())
        tracks = {t['Sm2TiTrack_id']: [] for kind in timeline.tracks for t in timeline.tracks[kind]}
        handled = {entry[1] for entry in placed} | {t['id'] for t in transitions} | set(self.deletes)
        for entry in placed:
            tracks[entry[3]].append((entry[4], 1, entry[1], entry[4] + entry[5], True))
        for t in transitions:
            tracks[t['track']].append((t['start'], 0, t['id'], t['start'] + t['duration'], False))
        for item_id, (kind, track_id, row) in timeline.items.items():
            if item_id in handled and item_id not in kept:
                continue
            if item_id in kept or item_id not in handled:
                s = _seed_frames(row, 'Start')
                d = _seed_frames(row, 'Duration')
                if s is not None and self.delta:
                    s += self.delta
                    self._shift(item_id, row)
                is_clip = row['DbType'] != items.TRANSITION
                tracks[track_id].append((s if s is not None else -1, 1 if is_clip else 0, item_id,
                                         (s or 0) + (d or 0), is_clip and item_id in kept))
        touched = {u[1] for u in self.updates if set(u[2]) & {'Start', 'Sm2TiTrack_id'}} | set(self.deletes) | \
            {row['Sm2TiItem_id'] for _t, row in self.inserts}
        for track_id, entries in tracks.items():
            seed_order = {item_id: n for n, item_id in enumerate(timeline.order.get(track_id, []))}
            entries.sort(key=lambda e: (e[0], e[1], seed_order.get(e[2], len(seed_order)), e[2]))
            clips = sorted((e for e in entries if e[1] == 1 and e[0] >= 0), key=lambda e: e[0])
            for a, b in zip(clips, clips[1:]):
                if b[0] < a[3] - _EPSILON:
                    raise WriteError(f'Two clips overlap on one track of “{timeline.name}” after the edit (at frame '
                                     f'{int(b[0]) - start}); Resolve keeps one clip per place on a track.')
            order = [e[2] for e in entries]
            seed = timeline.order.get(track_id, [])
            if order != seed and (set(order) != set(seed) or touched & set(order) or self.delta):
                self.track_orders[track_id] = order

    def _shift(self, item_id, row):
        """Move an item the edit did not touch by the change of the timeline's start."""
        text = str(row.get('Start'))
        whole, sep, fraction = text.partition('|')
        try:
            moved = str(int(whole) + self.delta) + (sep + fraction if sep else '')
        except ValueError:
            raise WriteError('An item of the timeline has an unreadable start, so the start timecode cannot be '
                             'changed.') from None
        self._update('Sm2TiItem', item_id, {'Start': moved})

    def _timeline_changed(self, timeline):
        ids = set(timeline.items)
        return bool(self.track_orders.keys() & {t['Sm2TiTrack_id'] for k in timeline.tracks
                                                for t in timeline.tracks[k]}) or \
            any(u[1] in ids and set(u[2]) & {'Start', 'Duration', 'Sm2TiTrack_id'} for u in self.updates) or \
            any(d in ids for d in self.deletes)

    # -- the project's own changes -------------------------------------------------------
    def apply(self, project_name):
        """Change the copy's rows in one transaction."""
        conn = self.seed.conn
        project_id = self.fresh()
        try:
            conn.execute('BEGIN IMMEDIATE')
            for table, columns in self.seed.columns.items():
                for column in columns:
                    conn.execute(f'UPDATE {_q(table)} SET {_q(column)} = ? WHERE typeof({_q(column)}) = \'text\' '
                                 f'AND {_q(column)} = ?', (project_id, self.seed.project_id))
            conn.execute('UPDATE SM_Project SET ProjectName = ?', (project_name,))
            for timeline, name in self.renames:
                conn.execute('UPDATE Sm2Timeline SET Name = ? WHERE Sm2Timeline_id = ?', (name, timeline.id))
                if timeline.media:
                    conn.execute('UPDATE Sm2MpMedia SET Name = ? WHERE Sm2MpMedia_id = ?', (name, timeline.media))
            self._remove_media(conn)
            for authored in self.authored.values():
                if authored is None:
                    continue
                for table in mediapool.MEDIA_TABLES:
                    for row in authored.rows.get(table, ()):
                        self.seed.insert(table, row)
            for item_id in self.deletes:
                conn.execute('DELETE FROM Sm2TiItem WHERE Sm2TiItem_id = ?', (item_id,))
                conn.execute('DELETE FROM Sm2TiItem_Sm2TiTrack WHERE DbAssociate = ?', (item_id,))
            for table, row in self.inserts:
                self.seed.insert(table, row)
            for table, rid, values in self.updates:
                self.seed.update(table, rid, values)
            for track_id, values in self.track_updates.items():
                self.seed.update('Sm2TiTrack', track_id, values)
            for track_id, order in self.track_orders.items():
                conn.execute("DELETE FROM Sm2TiItem_Sm2TiTrack WHERE DbOwner = ? AND DbPropertyName = 'Items'",
                             (track_id,))
                for index, item_id in enumerate(order):
                    self.seed.insert('Sm2TiItem_Sm2TiTrack', {'DbOwner': track_id, 'DbAssociate': item_id,
                                                              'DbPropertyName': 'Items', 'DbIndex': index})
            for sequence_id, (start, timeline) in self.sequence_updates.items():
                conn.execute('UPDATE Sm2Sequence SET MediaExtents = ? WHERE Sm2Sequence_id = ?',
                             (self._extents(timeline, start), sequence_id))
            conn.execute('COMMIT')
        except sqlite3.Error as error:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise WriteError(f'Writing the project failed ({error}); nothing was placed.') from None
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        check = conn.execute('PRAGMA integrity_check').fetchall()
        if [r[0] for r in check] != ['ok']:
            raise WriteError('The written project fails SQLite\'s integrity check; nothing was placed.')
        return project_id

    def _extents(self, timeline, start):
        """``MediaExtents``: the start in seconds, and the end of the last item counted from
        frame 0 over the rate (a double) minus the start (Resolve's formula on 15 real sequences)."""
        rate = float(timeline.rate)
        ends = []
        for track_id in [t['Sm2TiTrack_id'] for k in timeline.tracks for t in timeline.tracks[k]]:
            for row in self.seed.rows('Sm2TiItem', 'Sm2TiTrack_id = ?', (track_id,)):
                s, d = _seed_frames(row, 'Start'), _seed_frames(row, 'Duration')
                if s is not None and d is not None:
                    ends.append(s + d)
        start_seconds = start / rate if start != timeline.start else timeline.start_seconds
        length = max(ends) / rate - start_seconds if ends else 0.0
        return struct.pack('<dd', start_seconds, length)

    def _remove_media(self, conn):
        for clip in self.removed_media:
            for table, rows in clip.tables().items():
                key = table + '_id'
                for row in rows:
                    if key in row:
                        conn.execute(f'DELETE FROM {_q(table)} WHERE {_q(key)} = ?', (row[key],))
                    else:
                        conn.execute(f'DELETE FROM {_q(table)} WHERE DbOwner = ? AND DbAssociate = ? AND '
                                     'DbPropertyName = ?', (row['DbOwner'], row['DbAssociate'], row['DbPropertyName']))
        folders = {c.folder_link['DbOwner'] for c in self.removed_media}
        for folder in folders:
            links = self.seed.rows('Sm2MpFolder_Sm2MpMedia', "DbOwner = ? AND DbPropertyName = 'MediaVec' ORDER BY "
                                   'DbIndex', (folder,))
            for index, link in enumerate(links):
                if link['DbIndex'] != index:
                    conn.execute("UPDATE Sm2MpFolder_Sm2MpMedia SET DbIndex = ? WHERE DbOwner = ? AND DbAssociate = ? "
                                 "AND DbPropertyName = 'MediaVec'", (index, folder, link['DbAssociate']))


def prepare_template(seed, model, writer, report):
    """A template project (not the open project's own): its one timeline is emptied and its
    media are left out unless asked; its settings, color management and bins are kept."""
    timelines = [t for t in model.timelines if t['kind'] == 'timeline']
    if len(timelines) != 1:
        raise WriteError('Choose a template project with exactly one timeline (in Resolve: a new project with the '
                         'settings you want and one empty timeline, saved in this library).')
    entry = timelines[0]
    timeline = Timeline(seed, entry)
    for item_id, (_kind, _track, row) in timeline.items.items():
        if row['DbType'] not in (items.PICTURE, items.SOUND, items.TRANSITION):
            raise WriteError('The template\'s timeline holds titles, generators or other items Splice does not '
                             'remove; choose a template with an empty timeline.')
        if any(row.get(c) is not None for c in items.LATER_COLUMNS) or seed.rows('BtLockableBlob', 'BlobOwner = ?',
                                                                                  (item_id,)):
            raise WriteError('The template\'s timeline holds clips with Resolve work (grades, Fusion, effects or '
                             'markers); choose a template with an empty timeline.')
    if not writer.keep_template_media:
        writer.removed_media = list(writer.clips.values())
        if writer.removed_media:
            report.add(EXACT, 'Template media left out of the new project (as you asked)',
                       f'{len(writer.removed_media)} clip(s)')
    return entry
