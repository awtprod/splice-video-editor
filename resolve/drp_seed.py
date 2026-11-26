"""A library ``Project.db`` made from a ``.drp``, so a project opened from a .drp can seed a save.

A ``.drp`` is a zip of XML documents whose elements are the rows of a library project: an element
with a ``DbId`` is a row of the table its type lives in (``Sm2TiVideoClip`` in ``Sm2TiItem``,
``Sm2MpVideoClip`` in ``Sm2MpMedia``...), its child elements are the row's columns (a binary
column as hex text), an object nested in a column is a row of its own whose id the column holds
and which names its owner in a ``<Owner>_id`` column, and a vector (``<X><Element>...``) is a
join table named after its two tables in sorted order, with ``DbOwner``, ``DbAssociate``,
``DbPropertyName`` and ``DbIndex``. The database's own layout (tables, column types, indexes,
Resolve's two triggers) is taken from a Resolve 21 project in the target library (the schema
donor), never invented; its few tables a .drp does not carry (the upgrade log and the trigger
switch) are copied from it too.

What a Resolve export changes on the way to the .drp was measured on five real pairs of the same
project saved as both (see the PR): the timeline's resolution filled in with the project's,
an extra entry in each track's keyed dict, presets and thumbnails added, a grade's version body
re-framed, and some flags written as their defaults. Those cannot all be undone exactly, so a
project made from a .drp is written only when the user allows unproven changes, and the report
says so.
"""
import re
import sqlite3
import zipfile
from pathlib import Path

from .source import MAX_DRP_BYTES, MAX_ELEMENTS, MAX_MEMBER_BYTES, ResolveError, _parse_xml

_JOIN_COLUMNS = ('DbOwner', 'DbAssociate', 'DbPropertyName', 'DbIndex')
_EXPORT_ONLY = frozenset()
_FAMILIES = (
    (re.compile(r'Sm2Ti(?!Track\Z).*'), 'Sm2TiItem'),
    (re.compile(r'Sm2Mp\w*Clip\Z'), 'Sm2MpMedia'),
    (re.compile(r'\w*LockableBlob\Z'), 'BtLockableBlob'),
    (re.compile(r'Sm2LockableBlobMap\Z'), 'BtLockableBlobMap'),
    (re.compile(r'SmPTZRPreset\Z'), 'SmPreset'),
)


class Conversion:
    """Rows per table made from a .drp's elements, and what could not be placed."""

    def __init__(self, tables):
        self.tables = tables            # table -> [(column, declared type, default)]
        self.rows = {}                  # table -> {id: row}
        self.joins = {}                 # join table -> [row]
        self.unplaced = []              # (what, why)
        self.pending = []               # (owner table, owner id, property, index, child id) of id vectors

    def table_for(self, tag):
        name = tag.replace('__', '::')
        if name in self.tables and name + '_id' in {c for c, _t, _d in self.tables[name]}:
            return name
        for pattern, table in _FAMILIES:
            if pattern.fullmatch(name) and table in self.tables:
                return table
        return None


def _value(text, declared, default):
    declared = (declared or '').lower()
    if text is None or text == '':
        if default is not None and default.strip("'") == '' and 'char' in declared:
            return ''
        return None
    if 'blob' in declared:
        try:
            return bytes.fromhex(text.strip())
        except ValueError:
            return text  # Resolve keeps some numbers as text in a BLOB column (SlateTC)
    if 'bool' in declared:
        return {'true': 1, 'false': 0}.get(text.strip(), text)
    if 'int' in declared:
        try:
            return int(text.strip())
        except ValueError:
            return text
    if 'double' in declared or 'real' in declared or 'float' in declared:
        try:
            return float(text.strip())
        except ValueError:
            return text
    return text


def _element(conv, el, owner=None):
    """Place element ``el`` (one with a DbId) as a row; returns (table, id) or None."""
    if el.tag in _EXPORT_ONLY:
        return None
    rid = el.get('DbId')
    table = conv.table_for(el.tag)
    if table is None:
        conv.unplaced.append((el.tag, 'no table of this type in the schema'))
        return None
    columns = {c: (t, d) for c, t, d in conv.tables[table]}
    row = {table + '_id': rid}
    if 'DbType' in columns:
        row['DbType'] = el.tag.replace('__', '::')
    if owner is not None and owner[0] + '_id' in columns:
        row[owner[0] + '_id'] = owner[1]
    for child in el:
        name = child.tag.replace('__', '::')
        nested = [g for g in child if g.get('DbId')]
        elements = [g for g in child if g.tag == 'Element']
        if nested and not elements:
            placed = _element(conv, nested[0], (table, rid))
            if name in columns and placed:
                row[name] = placed[1]
            continue
        if elements:
            for index, holder in enumerate(elements):
                inner = [g for g in holder if g.get('DbId')]
                if inner:
                    placed = _element(conv, inner[0], (table, rid))
                    if placed:
                        conv.joins.setdefault('_'.join(sorted((table, placed[0]))), []).append(
                            {'DbOwner': rid, 'DbAssociate': placed[1], 'DbPropertyName': name, 'DbIndex': index})
                    continue
                text = (holder.text or '').strip()
                if text:
                    conv.pending.append((table, rid, name, index, text))
            continue
        if len(child):
            # A wrapper without ids (an older lockable-blob map): its objects belong to the row.
            for grand in child.iter():
                if grand is not child and grand.get('DbId'):
                    _element(conv, grand, (table, rid))
            continue
        if name not in columns:
            if child.text and child.text.strip():
                conv.unplaced.append((f'{table}.{name}', 'no such column in the schema'))
            continue
        declared, default = columns[name]
        try:
            row[name] = _value(child.text if not isinstance(child.text, bytes) else child.text.decode(), declared,
                               default)
        except ValueError:
            conv.unplaced.append((f'{table}.{name}', 'a value that is not what its column holds'))
    conv.rows.setdefault(table, {})
    if rid in conv.rows[table]:
        conv.unplaced.append((f'{table} {rid[:8]}', 'the id occurs twice'))
    conv.rows[table][rid] = row
    return table, rid


def _owners(conv):
    """Owner columns the .drp's documents do not nest: a sequence container's sequence (the one
    whose keyed dict names it), a bin's media pool, the gallery's project."""
    from . import blobs
    rows = conv.rows
    for sequence_id, sequence in rows.get('Sm2Sequence', {}).items():
        try:
            ref = blobs.keyed_dict(sequence.get('FieldsBlob') or b'').get('SeqRef')
        except blobs.BlobError:
            ref = None
        container = rows.get('Sm2SequenceContainer', {}).get(ref)
        if container is not None:
            container.setdefault('Sm2Sequence_id', sequence_id)
    for owner_table, child_table in (('Sm2MediaPool', 'Sm2MpFolder'), ('SM_Project', 'Gallery::GyGallery')):
        owners = list(rows.get(owner_table, {}))
        column = owner_table + '_id'
        if len(owners) == 1 and column in {c for c, _t, _d in conv.tables.get(child_table, [])}:
            for row in rows.get(child_table, {}).values():
                row.setdefault(column, owners[0])
    for table, joined in list(conv.joins.items()):
        if table in conv.tables:
            continue
        for row in joined:
            for child_table, children in rows.items():
                child = children.get(row['DbAssociate'])
                if child is None:
                    continue
                owner_table = next((t for t, r in rows.items() if row['DbOwner'] in r), None)
                if owner_table and owner_table + '_id' in {c for c, _t, _d in conv.tables[child_table]}:
                    child[owner_table + '_id'] = row['DbOwner']
                else:
                    conv.unplaced.append((table, 'a vector with no join table and no owner column'))
        del conv.joins[table]


def read_members(drp_path):
    data = Path(drp_path).read_bytes()
    if len(data) > MAX_DRP_BYTES:
        raise ResolveError('The .drp is larger than Splice opens.')
    budget = [MAX_ELEMENTS]
    out = []
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.endswith('.xml'):
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise ResolveError('A .drp member is larger than Splice reads.')
            with archive.open(info) as stream:
                raw = stream.read(MAX_MEMBER_BYTES + 1)
            out.append((info.filename, _parse_xml(raw, info.filename, budget)))
    return out


def schema(donor):
    """``(CREATE statements in order, {table: [(column, type, default)]}, page size, encoding)``."""
    conn = sqlite3.connect(f'file:{Path(donor).resolve()}?mode=ro&immutable=1', uri=True)
    try:
        statements = [(t, n, s) for t, n, s in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid")]
        tables = {}
        for kind, name, _sql in statements:
            if kind == 'table':
                tables[name] = [(r[1], r[2], r[4]) for r in conn.execute(f'PRAGMA table_info("{name}")')]
        page = conn.execute('PRAGMA page_size').fetchone()[0]
        encoding = conn.execute('PRAGMA encoding').fetchone()[0]
        version = conn.execute('SELECT ProjectVersion FROM SM_Project').fetchone()[0]
        carried = {}
        for name, columns in tables.items():
            names = {c for c, _t, _d in columns}
            if name + '_id' not in names and not set(_JOIN_COLUMNS) <= names:
                carried[name] = conn.execute(f'SELECT * FROM "{name}"').fetchall()
    finally:
        conn.close()
    return statements, tables, page, encoding, version, carried


def convert(drp_path, donor):
    statements, tables, page, encoding, version, carried = schema(donor)
    conv = Conversion(tables)
    for name, root in read_members(drp_path):
        if root is None:
            continue
        if root.get('DbId'):
            _element(conv, root)
        else:
            conv.unplaced.append((name, 'a document without an object at its root'))
    where = {rid: table for table, rows in conv.rows.items() for rid in rows}
    values = {}
    for table, rid, prop, index, child in conv.pending:
        other = where.get(child)
        if other is None:
            values.setdefault((table, rid, prop), []).append((index, child))
            continue
        conv.joins.setdefault('_'.join(sorted((table, other))), []).append(
            {'DbOwner': rid, 'DbAssociate': child, 'DbPropertyName': prop, 'DbIndex': index})
    for (table, rid, prop), listed in values.items():
        # A vector of plain values is one text column in the library, as PostgreSQL writes an
        # array: {a,b}.
        if prop in {c for c, _t, _d in conv.tables[table]}:
            conv.rows[table][rid][prop] = '{' + ','.join(v for _i, v in sorted(listed)) + '}'
        else:
            conv.unplaced.append((f'{table}.{prop}', 'a vector of values with no column'))
    _owners(conv)
    return conv, statements, page, encoding, version, carried


def write(conv, statements, page, encoding, carried, target):
    conn = sqlite3.connect(str(target), isolation_level=None)
    try:
        conn.execute(f'PRAGMA page_size = {int(page)}')
        conn.execute(f"PRAGMA encoding = '{encoding}'")
        conn.execute('PRAGMA journal_mode = DELETE')
        conn.execute('BEGIN')
        for kind, _name, sql in statements:
            if kind == 'table':
                conn.execute(sql)
        for table, rows in carried.items():
            for row in rows:
                conn.execute(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(row))})', row)
        for table, rows in conv.rows.items():
            for row in rows.values():
                names = list(row)
                conn.execute(f'INSERT INTO "{table}" ({",".join(chr(34) + n + chr(34) for n in names)}) VALUES '
                             f'({",".join("?" * len(names))})', [row[n] for n in names])
        for table, rows in conv.joins.items():
            if table not in conv.tables:
                conv.unplaced.append((table, 'no such join table in the schema'))
                continue
            for row in rows:
                conn.execute(f'INSERT INTO "{table}" (DbOwner, DbAssociate, DbPropertyName, DbIndex) VALUES (?,?,?,?)',
                             (row['DbOwner'], row['DbAssociate'], row['DbPropertyName'], row['DbIndex']))
        for kind, _name, sql in statements:
            if kind in ('index', 'trigger'):
                conn.execute(sql)
        conn.execute('COMMIT')
        if [r[0] for r in conn.execute('PRAGMA integrity_check')] != ['ok']:
            raise ResolveError('The project made from the .drp fails SQLite\'s integrity check.')
    finally:
        conn.close()


def make_seed(drp_path, donor, target, report, allow_unproven):
    """Write ``target`` (a Project.db) from the .drp at ``drp_path`` with ``donor``'s layout."""
    from .writer import OPT_IN, WriteError
    if not allow_unproven:
        report.add(OPT_IN, 'A project opened from a .drp is first turned into a library project; that conversion '
                   'is not proven exact (a Resolve export changes a few values on the way out). Allow unproven '
                   'changes to save it', None, need='unproven')
        raise WriteError('Saving a project opened from a .drp needs “Allow unproven changes”: the .drp is turned '
                         'into a library project first, and that conversion is not proven exact.')
    conv, statements, page, encoding, version, carried = convert(drp_path, donor)
    project = conv.rows.get('SM_Project', {})
    versions = {str(r.get('ProjectVersion')) for r in project.values()}
    if versions != {str(version)}:
        raise WriteError(f'The .drp is of another project version ({sorted(map(str, versions))}) than this library\'s '
                         f'Resolve ({version}); open it in Resolve 21 and export it again.')
    write(conv, statements, page, encoding, carried, target)
    report.add(OPT_IN, 'Seed made from the .drp (converted into a library project; not proven exact; you allowed it)',
               f'{sum(len(r) for r in conv.rows.values())} objects', need='unproven')
    if conv.unplaced:
        report.add(OPT_IN, 'Parts of the .drp the conversion could not place (left out of the new project)',
                   '; '.join(f'{w} ({y})' for w, y in conv.unplaced[:20]), need='unproven')
