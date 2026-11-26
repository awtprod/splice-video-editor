"""Read a Resolve project into element trees: a ``.drp`` archive or a disk library ``Project.db``.

Both sources come out in the same shape, the XML layout of a ``.drp``: ``project.xml``
(``SM_Project``), one ``Sm2MpFolder`` tree per media pool bin and one ``Sm2SequenceContainer``
per timeline. A library project's SQLite rows are laid out the way Resolve writes them into a
``.drp`` (the tables are the XML's element types and the ``<A>_<B>`` join tables its vectors),
following ai-agent-resolve ``src/aar/drp/library.py`` at commit
aff6573bcba41e66e782ea2c4714290f227408b0 (same owner). Binary columns keep their bytes as the
element text, where a ``.drp`` holds hex.

Project files are untrusted: every read is bounded (archive members, XML bytes and elements,
database size, rows and values), and a library ``Project.db`` is never opened in place. It is
copied to a private snapshot once no journal is beside it and its size and modification time
held still during the copy; SQLite then reads the snapshot read-only and immutable, so nothing
is ever created or changed in the library folder.
"""
import hashlib
import io
import os
import re
import sqlite3
import stat
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_DRP_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_XML_BYTES = 384 * 1024 * 1024
MAX_ELEMENTS = 3_000_000
MAX_DB_BYTES = 512 * 1024 * 1024
MAX_ROWS = 600_000
MAX_LOAD_BYTES = 384 * 1024 * 1024
MAX_VALUE_BYTES = 32 * 1024 * 1024
MAX_READ_SECONDS = 120.0
SQLITE_MAGIC = b'SQLite format 3\x00'
_BIN = re.compile(r'^MediaPool/(.+/)?MpFolder\.xml$')
_SEQ = re.compile(r'(^|/)SeqContainer(\d*\.xml|/[^/]+\.xml)$')
_TAG_COLON = re.compile(rb'(</?[A-Za-z_][\w.\-]*):+')


class ResolveError(ValueError):
    """A project that cannot be read, with a message for the person opening it."""


class Source:
    def __init__(self, kind, project, bins, containers, digest, members=()):
        self.kind = kind                # 'drp' or 'library'
        self.project = project          # SM_Project element or None
        self.bins = bins                # [Sm2MpFolder element]
        self.containers = containers    # [Sm2SequenceContainer element]
        self.sha256 = digest
        self.members = list(members)    # other archive members (not imported)


# --------------------------------------------------------------------------- .drp

_XML_ENCODING = re.compile(rb'\A(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?\bencoding\s*=\s*["\']([^"\']*)["\']')
_UTF8_NAMES = {b'utf-8', b'utf8', b'us-ascii', b'ascii'}


def _parse_xml(raw, name, budget):
    # Resolve writes UTF-8. Anything else (UTF-16 with or without a byte order mark, or another
    # declared encoding) is refused before parsing, so the byte-level DOCTYPE/ENTITY check below
    # cannot be sidestepped by an encoding in which those words are not plain ASCII bytes.
    declared = _XML_ENCODING.match(raw)
    if (raw[:2] in (b'\xff\xfe', b'\xfe\xff') or b'\x00' in raw[:256]
            or (declared and declared.group(1).strip().lower() not in _UTF8_NAMES)):
        raise ResolveError(f'{name} is not UTF-8 XML, which Resolve always writes')
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise ResolveError(f'{name} declares a document type, which Resolve projects never do')
    for _ in range(8):
        raw, changed = _TAG_COLON.subn(lambda m: m.group(1) + b'__', raw)
        if not changed:
            break
    parser = ET.XMLPullParser(events=('start',))
    root = None
    try:
        for offset in range(0, len(raw), 1 << 20):
            parser.feed(raw[offset:offset + (1 << 20)])
            for _event, element in parser.read_events():
                if root is None:
                    root = element
                budget[0] -= 1
                if budget[0] < 0:
                    raise ResolveError('The project has more elements than Splice reads '
                                       f'({MAX_ELEMENTS:,}); it may be damaged.')
        parser.close()
    except ET.ParseError as error:
        raise ResolveError(f'{name} is not well-formed XML ({error}).') from None
    return root


def load_drp(data):
    """Read a ``.drp`` archive (bytes)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ResolveError('The file is empty.')
    if len(data) > MAX_DRP_BYTES:
        raise ResolveError('The project file is larger than Splice opens '
                           f'({MAX_DRP_BYTES // (1024 * 1024)} MB).')
    if data[:len(SQLITE_MAGIC)] == SQLITE_MAGIC:
        raise ResolveError('This is a Resolve library database. Use File → Open from Resolve library.')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise ResolveError('This is not a DaVinci Resolve project (.drp) file.') from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ResolveError('The project archive has too many members.')
        names = [info.filename for info in infos]
        if 'project.xml' not in names:
            raise ResolveError('This archive has no project.xml, so it is not a Resolve project.')
        budget = [MAX_ELEMENTS]
        xml_left = MAX_XML_BYTES
        docs = {}
        others = []
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if not (name == 'project.xml' or _BIN.match(name) or _SEQ.search(name)):
                others.append(name)
                continue
            if info.flag_bits & 0x1:
                raise ResolveError('The project archive is encrypted.')
            limit = min(MAX_MEMBER_BYTES, xml_left)
            try:
                with archive.open(info) as stream:
                    raw = stream.read(limit + 1)
            except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, RuntimeError) as error:
                raise ResolveError(f'{name} cannot be read from the archive ({error}).') from None
            if len(raw) > limit:
                raise ResolveError('The project holds more XML than Splice reads; it may be damaged '
                                   'or not a Resolve project.')
            xml_left -= len(raw)
            docs[name] = _parse_xml(raw, name, budget)
    project = docs.get('project.xml')
    if project is None or project.tag != 'SM_Project':
        raise ResolveError('project.xml is not a Resolve project description.')
    bins = [root for name, root in sorted(docs.items()) if _BIN.match(name) and root is not None]
    containers = [root for name, root in sorted(docs.items())
                  if _SEQ.search(name) and root is not None and root.tag == 'Sm2SequenceContainer']
    return Source('drp', project, bins, containers, hashlib.sha256(data).hexdigest(), others)


# --------------------------------------------------------------------------- library snapshot

def _sidecar(path):
    for suffix in ('-journal', '-wal'):
        if os.path.lexists(str(path) + suffix):
            return True
    return False


def _regular_file(path):
    try:
        mode = os.stat(path).st_mode
    except OSError as error:
        raise ResolveError(f'Cannot read {path.name}: {error.strerror or error}.') from None
    if not stat.S_ISREG(mode):
        raise ResolveError(f'{path.name} is not a regular file.')


def snapshot(path, folder, *, attempts=6, pause=0.5):
    """Copy ``path`` to a new file in ``folder`` once it holds still; returns (copy, sha256)."""
    path = Path(path)
    _regular_file(path)
    for attempt in range(attempts):
        if attempt:
            time.sleep(pause)
        if _sidecar(path):
            continue
        before = os.stat(path)
        if before.st_size > MAX_DB_BYTES:
            raise ResolveError('This project database is larger than Splice opens '
                               f'({MAX_DB_BYTES // (1024 * 1024)} MB).')
        fd, name = tempfile.mkstemp(prefix='resolve-', suffix='.db', dir=folder)
        copy = Path(name)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, 'wb') as sink, open(path, 'rb') as source:
                copied = 0
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_DB_BYTES:
                        raise ResolveError('This project database grew past the size Splice opens.')
                    digest.update(chunk)
                    sink.write(chunk)
            after = os.stat(path)
        except BaseException:
            copy.unlink(missing_ok=True)
            raise
        unchanged = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        if unchanged and copied == after.st_size and not _sidecar(path):
            return copy, digest.hexdigest()
        copy.unlink(missing_ok=True)
    raise ResolveError('Resolve is saving this project right now. Wait for the save to finish, then open it again.')


def connect(path, deadline):
    """A read-only, immutable connection: SQLite creates no journal, WAL or lock file."""
    uri = Path(path).resolve().as_uri() + '?mode=ro&immutable=1'
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    except sqlite3.Error as error:
        raise ResolveError(f'The project database cannot be opened ({error}).') from None
    try:
        if hasattr(conn, 'setlimit'):  # Python 3.11+
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        conn.execute('PRAGMA trusted_schema = OFF')
        conn.execute('PRAGMA query_only = ON')
    except sqlite3.Error as error:
        conn.close()
        raise ResolveError(f'The project database cannot be read ({error}).') from None
    except BaseException:
        conn.close()
        raise
    return conn


def _q(name):
    return '"' + name.replace('"', '""') + '"'


class _Rows:
    """Bounded table reads from a library database."""

    def __init__(self, conn):
        self.conn = conn
        self.rows_left = MAX_ROWS
        self.bytes_left = MAX_LOAD_BYTES
        self.tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def read(self, table, required=True):
        if table not in self.tables:
            if required:
                raise ResolveError(f'This database has no {table} table, so it is not a Resolve project.')
            return []
        (count,) = self.conn.execute(f'SELECT count(*) FROM {_q(table)}').fetchone()
        if count > self.rows_left:
            raise ResolveError('The project has more rows than Splice reads; it may be damaged.')
        cursor = self.conn.execute(f'SELECT * FROM {_q(table)}')
        columns = [d[0] for d in cursor.description]
        out = []
        for values in cursor:
            self.rows_left -= 1
            for value in values:
                if isinstance(value, (bytes, str)):
                    self.bytes_left -= len(value) + 16
                else:
                    self.bytes_left -= 16
            if self.rows_left < 0 or self.bytes_left < 0:
                raise ResolveError('The project holds more data than Splice reads; it may be damaged.')
            out.append(dict(zip(columns, values)))
        return out


def _key(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_SKIP_COLUMNS = {'DbType', 'DbSavedTime'}
_TAG = re.compile(r'[A-Za-z_][A-Za-z0-9_.\-]*\Z')


class _Layout:
    """Lays library rows out as the element trees a ``.drp`` holds."""

    def __init__(self, rows):
        self.rows = rows
        self.index = {}
        self.joins = {}
        self.placed = set()
        self.elements = 0

    def table(self, name, required=True):
        if name not in self.index:
            out = {}
            for row in self.rows.read(name, required):
                rid = _key(row.get(name + '_id'))
                if rid is not None and rid not in out:
                    out[rid] = row
            self.index[name] = out
        return self.index[name]

    def links(self, join, required=False):
        if join not in self.joins:
            out = {}
            for row in self.rows.read(join, required):
                owner, child = _key(row.get('DbOwner')), _key(row.get('DbAssociate'))
                if owner is None or child is None:
                    continue
                index = row.get('DbIndex')
                index = index if isinstance(index, int) and not isinstance(index, bool) else 1 << 62
                out.setdefault(owner, {}).setdefault(_key(row.get('DbPropertyName')) or '', []).append((index, child))
            for props in out.values():
                for children in props.values():
                    children.sort()
            self.joins[join] = out
        return self.joins[join]

    def vector(self, join, owner, prop):
        return [child for _i, child in self.links(join).get(owner, {}).get(prop, [])]

    def element(self, table, rid, tag=None, nested=()):
        """``row`` as an element: one child per column; ``nested`` columns are filled by the caller."""
        row = self.table(table)[rid]
        tag = tag or _key(row.get('DbType')) or table
        if not _TAG.match(tag):
            tag = table
        self.elements += 1
        if self.elements > MAX_ELEMENTS // 8:
            raise ResolveError('The project has more objects than Splice reads; it may be damaged.')
        node = ET.Element(tag.replace('::', '__'), {'DbId': rid})
        for column, value in row.items():
            if column in _SKIP_COLUMNS or column == table + '_id' or column in nested or not _TAG.match(column):
                continue
            child = ET.SubElement(node, column)
            if isinstance(value, bytes):
                child.text = value  # raw bytes where a .drp holds hex
            elif value is not None:
                child.text = str(value)
        return node

    def project(self):
        projects = self.table('SM_Project')
        if len(projects) != 1:
            raise ResolveError('The database does not hold exactly one Resolve project.')
        (pid,) = projects
        root = self.element('SM_Project', pid, 'SM_Project', nested=('CommonConfig', 'LockableBlobMap'))
        handles = ET.SubElement(root, 'TimelineHandleVec')
        for child in self.vector('SM_Project_Sm2Timeline', pid, 'TimelineHandleVec'):
            ET.SubElement(handles, 'Element').text = child
        config = _key(projects[pid].get('CommonConfig'))
        if config and config in self.table('SM_Config', False):
            ET.SubElement(root, 'CommonConfig').append(self.element('SM_Config', config, 'SM_Config'))
        holder = ET.SubElement(root, 'LockableBlobMap')
        for bid in self.table('BtLockableBlob', False):
            ET.SubElement(holder, 'Element').append(self.element('BtLockableBlob', bid))
        return root

    def _info(self, parent, column, info_id):
        for table in ('BtVideoInfo', 'BtAudioInfo'):
            if info_id in self.table(table, False):
                ET.SubElement(parent, column).append(self.element(table, info_id, table))
                return

    def _sequence(self, parent, seq_id):
        if seq_id and seq_id in self.table('Sm2Sequence', False):
            ET.SubElement(parent, 'Sequence').append(self.element('Sm2Sequence', seq_id, 'Sm2Sequence'))

    def media(self, mid):
        row = self.table('Sm2MpMedia')[mid]
        refs = ('Video', 'Left', 'Right', 'LeftEye', 'RightEye', 'Sequence', 'TimelineSharedHandle')
        node = self.element('Sm2MpMedia', mid, nested=refs + ('EmbeddedAudio',))
        for column in ('Video', 'Left', 'Right', 'LeftEye', 'RightEye'):
            info = _key(row.get(column))
            if info:
                self._info(node, column, info)
        audio = self.vector('BtAudioInfo_Sm2MpMedia', mid, 'EmbeddedAudioVec')
        if audio:
            vec = ET.SubElement(node, 'EmbeddedAudioVec')
            for aid in audio:
                if aid in self.table('BtAudioInfo', False):
                    ET.SubElement(vec, 'Element').append(self.element('BtAudioInfo', aid, 'BtAudioInfo'))
        self._sequence(node, _key(row.get('Sequence')))
        tl = _key(row.get('TimelineSharedHandle'))
        if tl and tl in self.table('Sm2Timeline', False):
            handle = ET.SubElement(node, 'TimelineSharedHandle')
            timeline = self.element('Sm2Timeline', tl, 'Sm2Timeline', nested=('Sequence',))
            handle.append(timeline)
            self._sequence(timeline, _key(self.table('Sm2Timeline')[tl].get('Sequence')))
        return node

    def bins(self):
        out = []
        for fid in self.table('Sm2MpFolder'):
            folder = self.element('Sm2MpFolder', fid, 'Sm2MpFolder')
            vec = ET.SubElement(folder, 'MediaVec')
            for mid in self.vector('Sm2MpFolder_Sm2MpMedia', fid, 'MediaVec'):
                if mid in self.table('Sm2MpMedia') and ('media', mid) not in self.placed:
                    self.placed.add(('media', mid))
                    ET.SubElement(vec, 'Element').append(self.media(mid))
            out.append(folder)
        return out

    def containers(self):
        out = []
        for cid in self.table('Sm2SequenceContainer'):
            root = self.element('Sm2SequenceContainer', cid, 'Sm2SequenceContainer')
            for prop in ('VideoTrackVec', 'AudioTrackVec', 'SubtitleTrackVec'):
                vec = ET.SubElement(root, prop)
                for tid in self.vector('Sm2SequenceContainer_Sm2TiTrack', cid, prop):
                    if tid not in self.table('Sm2TiTrack') or ('track', tid) in self.placed:
                        continue
                    self.placed.add(('track', tid))
                    track = self.element('Sm2TiTrack', tid, 'Sm2TiTrack', nested=('Items',))
                    items = ET.SubElement(track, 'Items')
                    for iid in self.vector('Sm2TiItem_Sm2TiTrack', tid, 'Items'):
                        if iid in self.table('Sm2TiItem') and ('item', iid) not in self.placed:
                            self.placed.add(('item', iid))
                            ET.SubElement(items, 'Element').append(self.element('Sm2TiItem', iid))
                    ET.SubElement(vec, 'Element').append(track)
            out.append(root)
        return out


def load_library(path, scratch=None, *, deadline_seconds=MAX_READ_SECONDS):
    """Read a library ``Project.db`` through a private snapshot (deleted before returning)."""
    path = Path(path)
    if path.is_dir():
        path = path / 'Project.db'
    with tempfile.TemporaryDirectory(prefix='splice-resolve-', dir=scratch) as folder:
        copy, digest = snapshot(path, folder)
        with copy.open('rb') as head:
            if head.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                raise ResolveError(f'{path.name} is not a Resolve project database.')
        conn = connect(copy, time.monotonic() + deadline_seconds)
        try:
            check = conn.execute('PRAGMA quick_check(1)').fetchone()
            if not check or check[0] != 'ok':
                raise ResolveError('The project database is damaged (SQLite integrity check failed).')
            layout = _Layout(_Rows(conn))
            project = layout.project()
            bins = layout.bins()
            containers = layout.containers()
        except sqlite3.Error as error:
            raise ResolveError(f'The project database cannot be read ({error}).') from None
        finally:
            conn.close()
    return Source('library', project, bins, containers, digest)


# --------------------------------------------------------------------------- library listing

MAX_LISTED = 500
MAX_SCANNED = 20_000


def _project_files(root):
    """``Project.db`` files under a library folder (the folder itself, its ``Resolve Projects``,
    a ``Users/<user>`` or ``Projects`` folder, or one project's folder)."""
    root = Path(root)
    found, scanned = [], 0
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > MAX_SCANNED or len(found) >= MAX_LISTED:
                return found, True
            try:
                if entry.is_file() and entry.name == 'Project.db':
                    found.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False) and depth < 8 and not entry.name.startswith('.'):
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return found, False


MAX_LIST_SECONDS = 60.0
LIST_QUERY_SECONDS = 5.0


def _list_entry(path, deadline):
    """Name, save time and timeline count of one library project, read in place.

    Listing reads each ``Project.db`` where it is, through :func:`connect` (``mode=ro`` and
    ``immutable=1``: SQLite takes no locks and creates no journal, WAL or shared-memory file), and
    only after checking its size and SQLite header. A file that is mid-save or damaged gives an
    error entry, never a failed listing; opening a project reads a verified private snapshot.
    """
    entry = {'path': str(path), 'folder': path.parent.name, 'name': path.parent.name,
             'saved': None, 'timelines': None, 'error': None}
    try:
        info = os.stat(path)
        entry['saved'] = info.st_mtime
        if not stat.S_ISREG(info.st_mode):
            entry['error'] = 'Not a regular file'
        elif info.st_size > MAX_DB_BYTES:
            entry['error'] = f'Larger than Splice opens ({MAX_DB_BYTES // (1024 * 1024)} MB)'
        elif _sidecar(path):
            entry['error'] = 'Resolve is saving this project'
        elif time.monotonic() > deadline:
            entry['error'] = 'Not read: the library took too long to list'
        else:
            with open(path, 'rb') as head:
                if head.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                    entry['error'] = 'Not a Resolve project database'
                    return entry
            conn = connect(path, min(deadline, time.monotonic() + LIST_QUERY_SECONDS))
            try:
                row = conn.execute('SELECT ProjectName, LastModTimeInSecs FROM SM_Project LIMIT 2').fetchone()
                if row:
                    entry['name'] = (_key(row[0]) or entry['name'])[:256]
                    if isinstance(row[1], int) and row[1] > 0:
                        entry['saved'] = row[1]
                (entry['timelines'],) = conn.execute(
                    "SELECT count(*) FROM SM_Project_Sm2Timeline WHERE DbPropertyName='TimelineHandleVec'").fetchone()
            finally:
                conn.close()
    except (sqlite3.Error, ResolveError, OSError) as error:
        entry['error'] = 'Cannot read this project (%s)' % (error,)
    return entry


def list_library(root):
    """Projects in a disk library: [{path, name, folder, saved, timelines, error}], truncated flag."""
    root = Path(root).expanduser()
    if not root.is_dir():
        raise ResolveError('Choose the library folder (the folder that holds “Resolve Projects”).')
    files, truncated = _project_files(root)
    if not files:
        raise ResolveError('No Resolve projects were found in this folder. Choose the library folder '
                           'that holds “Resolve Projects”.')
    deadline = time.monotonic() + MAX_LIST_SECONDS
    return [_list_entry(path, deadline) for path in sorted(files)], truncated


STALE_SNAPSHOT_SECONDS = 15 * 60
_reaped = set()


def reap_snapshots(folder, older_than=STALE_SNAPSHOT_SECONDS):
    """Remove snapshot folders a crashed server left in ``folder`` (Splice's own private folder).

    Only entries named the way :func:`load_library` names them are touched, never through a
    symbolic link, and only once they are older than any read can last, so a read in progress in
    another Splice window is left alone.
    """
    import shutil
    removed = 0
    cutoff = time.time() - older_than
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith('splice-resolve-'):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_mtime > cutoff:
                continue
            shutil.rmtree(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


def scratch_folder():
    """A private folder for library snapshots, beside Splice's media (never in the library).

    The first use in a server process clears snapshots a crashed server left behind.
    """
    from splice_platform import data_directory
    folder = data_directory() / 'resolve-snapshots'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if str(folder) not in _reaped:
        _reaped.add(str(folder))
        reap_snapshots(folder)
    return folder
