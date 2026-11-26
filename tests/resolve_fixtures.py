"""Synthetic DaVinci Resolve projects for tests: the same invented project as a .drp archive and
as a disk-library Project.db, laid out the way Resolve writes them. Run as a script to write one:

    python3 tests/resolve_fixtures.py drp OUT.drp VIDEO_PATH AUDIO_PATH [--no-generator] [--states]
    python3 tests/resolve_fixtures.py library LIBRARY_FOLDER VIDEO_PATH AUDIO_PATH [--no-generator] [--states]

``--states`` disables and locks the first timeline's second video track (Flags 3, as Resolve 21.1 writes).
"""
import io
import sqlite3
import struct
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

FPS = 24.0


# --------------------------------------------------------------------------- encoders

def qstring(text):
    data = text.encode('utf-16-be')
    return struct.pack('>I', len(data)) + data


def variant(value):
    if isinstance(value, bool):
        return struct.pack('>IB', 1, 0) + bytes([value])
    if isinstance(value, int):
        return struct.pack('>IBq', 4, 0, value)
    if isinstance(value, float):
        return struct.pack('>IBd', 6, 0, value)
    if isinstance(value, str):
        return struct.pack('>IB', 0x0A, 0) + qstring(value)
    if isinstance(value, bytes):
        return struct.pack('>IBI', 0x0C, 0, len(value)) + value
    raise TypeError(value)


def keyed(entries):
    out = struct.pack('>II', 1, len(entries))
    for key, value in entries.items():
        out += qstring(key) + variant(value)
    return out


def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb(*fields):
    """fields: (number, value) with int (varint), float (fixed64 double) or bytes/str (length)."""
    out = b''
    for number, value in fields:
        if isinstance(value, bool) or isinstance(value, int):
            out += varint(number << 3) + varint(value)
        elif isinstance(value, float):
            out += varint(number << 3 | 1) + struct.pack('<d', value)
        else:
            data = value.encode() if isinstance(value, str) else value
            out += varint(number << 3 | 2) + varint(len(data)) + data
    return out


def envelope(payload, compress=False):
    if compress:
        frame = zstd_frame(payload)
        body = b'\x81' + frame
    else:
        body = b'\x80' + payload
    return struct.pack('>II', 2, len(body)) + body


def zstd_frame(payload):
    """A valid zstd frame: the real library's when present, else raw blocks (always valid)."""
    try:
        from compression import zstd
        return zstd.compress(payload)
    except ImportError:
        out = b'\x28\xb5\x2f\xfd' + bytes([0xA0]) \
            + struct.pack('<I', len(payload))
        chunks = [payload[i:i + 65536] for i in range(0, len(payload), 65536)] or [b'']
        for i, chunk in enumerate(chunks):
            header = (len(chunk) << 3) | (1 if i == len(chunks) - 1 else 0)
            out += header.to_bytes(3, 'little') + chunk
        return out


def rate(fps):
    return struct.pack('<d', fps) + bytes(8)


def resolution(w, h):
    return struct.pack('>IIII', 0, w, 0, h)


def extents(start, duration):
    return struct.pack('<dd', start, duration)


def compact_timemap():
    return b'\x02' + struct.pack('>d', 0.0)


def retimed(speed):
    points = keyed({'0': keyed({'interp': 0, 'X': 0.0, 'Y': 0.0}),
                    '1': keyed({'interp': 0, 'X': 100.0, 'Y': 100.0 * speed})})
    return keyed({'XMax': 100.0, 'LastValidYOffset': 100.0 * abs(speed), 'KeyframesBA': points,
                  'DbType': 'Sm2TimeMap'})


def links(*partners):
    listed = keyed({str(i): p for i, p in enumerate(partners)})
    return envelope(pb((1, pb((1, pb((1, listed))), (4, 1))), (15, 4)))


def effects(*entries, compress=True, slot=None):
    """entries: (effect type, {param id: value}) -> an EffectFiltersBA envelope.

    Resolve 21.1 writes a short list uncompressed (``compress=False``) and keeps the parameters in
    fixed slots, an empty slot for each one at its default: ``slot={param id: index}`` puts each
    parameter at that index, after empty slots."""
    out = []
    for kind, params in entries:
        fields = [(1, kind), (7, 0)]
        slots = []
        for pid, value in params.items():
            index = (slot or {}).get(pid, len(slots))
            slots += [b''] * (index + 1 - len(slots))
            slots[index] = pb((1, pid), (3, pb((1, pb((2, float(value)))))))
        fields += [(9, entry) for entry in slots]
        out.append((1, pb(*fields)))
    return envelope(pb(*out), compress=compress)


def markers(entries):
    """entries: (frame, name, note, colour bit) -> BtLockableBlob FieldsBlob."""
    listed = b''
    for frame, name, note, colour in entries:
        detail = pb((1, colour), (3, note), (3, '1'), (3, name))
        body = struct.pack('>II', 2, len(pb((1, detail)))) + pb((1, detail))
        listed += pb((1, pb((1, frame), (2, body))))
    return keyed({'BlobData': envelope(pb((2, listed)))})


def clip_blob(directory, filename):
    return envelope(pb((1, directory), (2, filename), (5, 'H.264')))


# --------------------------------------------------------------------------- the project

def sample(video_path='/footage/Day 1/A001.mp4', audio_path='E:\\Shoot\\Day 1\\A002.wav',
           harbor_seconds=2.0, generator=True):
    """An invented two-timeline project. Paths are split the way Resolve stores them."""
    def split(path):
        sep = '\\' if '\\' in path else '/'
        head, _, tail = path.rpartition(sep)
        return head, tail
    v_dir, v_file = split(video_path)
    a_dir, a_file = split(audio_path)
    start = 3600 * 24  # 01:00:00:00 at 24 fps
    harbor = int(harbor_seconds * FPS)
    half = harbor // 2
    return {
        'id': 'p-1', 'name': 'Harbor Lights', 'saved': 1767225600,
        'bins': [('bin-master', 'Master', None), ('bin-day', 'Day 1', 'bin-master')],
        'media': [
            {'id': 'm-video', 'tag': 'Sm2MpVideoClip', 'name': v_file, 'bin': 'bin-day', 'dir': v_dir,
             'file': v_file, 'frames': 240, 'size': (320, 180), 'info': 'vi-1'},
            {'id': 'm-audio', 'tag': 'Sm2MpAudioClip', 'name': a_file, 'bin': 'bin-day', 'dir': a_dir,
             'file': a_file, 'samples': 48000 * 10, 'info': 'ai-1'},
        ],
        'timelines': [
            {'id': 'tl-1', 'media': 'm-tl-1', 'seq': 'seq-1', 'container': 'sc-1', 'name': 'Harbor cut',
             'fps': FPS, 'size': (1280, 720), 'start': start / FPS,
             'markers': [(12, 'Wide shot', 'Check focus', 8)],
             'tracks': [
                 {'id': 'tr-v1', 'type': 0, 'name': 'Picture', 'items': [
                     {'id': 'i-v1', 'tag': 'Sm2TiVideoClip', 'name': v_file, 'start': start, 'duration': half,
                      'in': 24, 'media': 'm-video', 'links': ['i-a1'],
                      'effects': effects((4, {40: 0.1, 41: -0.05, 42: 1.25, 43: 1.25}), (2, {1: 80.0})),
                      'grade': True},
                     {'id': 'i-tr', 'tag': 'Sm2TiTransition', 'name': 'Cross Dissolve', 'pretty': 'Cross Dissolve',
                      'start': start + half - 6, 'duration': 12, 'alignment': 2},
                     {'id': 'i-v2', 'tag': 'Sm2TiVideoClip', 'name': v_file, 'start': start + half,
                      'duration': harbor - half, 'in': 72, 'media': 'm-video', 'timemap': retimed(2.0)},
                 ]},
                 {'id': 'tr-v2', 'type': 0, 'name': '', 'items': [
                     {'id': 'i-gen', 'tag': 'Sm2TiGenerator', 'name': 'Solid Color', 'pretty': 'Solid Color',
                      'start': start, 'duration': 6},
                 ] if generator else []},
                 {'id': 'tr-a1', 'type': 1, 'name': 'Dialogue', 'items': [
                     {'id': 'i-a1', 'tag': 'Sm2TiAudioClip', 'name': v_file, 'start': start, 'duration': half,
                      'in': 24, 'media': 'm-video', 'links': ['i-v1']},
                     {'id': 'i-a2', 'tag': 'Sm2TiAudioClip', 'name': a_file, 'start': start + half,
                      'duration': harbor - half, 'in': 0, 'media': 'm-audio', 'fusion': True},
                 ]},
             ]},
            {'id': 'tl-2', 'media': 'm-tl-2', 'seq': 'seq-2', 'container': 'sc-2', 'name': 'Harbor alt',
             'fps': 30000 / 1001, 'size': (1920, 1080), 'start': 0.0, 'markers': [],
             'tracks': [
                 {'id': 'tr2-v1', 'type': 0, 'name': '', 'items': [
                     {'id': 'i2-v1', 'tag': 'Sm2TiVideoClip', 'name': v_file, 'start': 0, 'duration': 30,
                      'in': 0, 'media': 'm-video', 'timemap': retimed(-1.0)},
                 ]},
                 {'id': 'tr2-a1', 'type': 1, 'name': '', 'items': []},
             ]},
        ],
    }


# --------------------------------------------------------------------------- .drp

def _el(tag, children='', dbid=None):
    attr = f' DbId="{dbid}"' if dbid else ''
    return f'<{tag}{attr}>{children}</{tag}>' if children != '' else f'<{tag}{attr}/>'


def _leaf(tag, value):
    if value is None:
        return f'<{tag}/>'
    if isinstance(value, bytes):
        return f'<{tag}>{value.hex()}</{tag}>'
    return f'<{tag}>{escape(str(value))}</{tag}>'


HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<!--DbAppVer="20.0.0.0049" DbPrjVer="15"-->\n'


def _media_xml(m):
    if m['tag'] == 'Sm2MpAudioClip':
        info = _el('BtAudioInfo', _leaf('Clip', clip_blob(m['dir'], m['file'])) + _leaf(
            'TracksBA', keyed({'0': keyed({'DbType': 'BtAudioTrack', 'SampleRate': 48000, 'NumChannels': 2,
                                           'Duration': m['samples']})})), m['info'])
        streams = _el('EmbeddedAudioVec', _el('Element', info))
    else:
        info = _el('BtVideoInfo', _leaf('Clip', clip_blob(m['dir'], m['file']))
                   + _leaf('Time', keyed({'NumFrames': m['frames'], 'StartFrame': 0, 'FrameRate': rate(FPS)}))
                   + _leaf('Geometry', keyed({'Resolution': resolution(*m['size'])})), m['info'])
        streams = _el('Video', info)
    return _el(m['tag'], _leaf('Name', m['name']) + _leaf('MpFolder', m['bin']) + streams, m['id'])


def _sequence_fields(t):
    return {'SeqRef': t['container']}


def _sequence(t):
    return _el('Sm2Sequence', _leaf('FieldsBlob', keyed(_sequence_fields(t)))
               + _leaf('FrameRate', rate(t['fps'])) + _leaf('Resolution', resolution(*t['size']))
               + _leaf('MediaExtents', extents(t['start'], 60.0)), t['seq'])


def _timeline_media_xml(t):
    timeline = _el('Sm2Timeline', _leaf('Name', t['name']) + _el('Sequence', _sequence(t)), t['id'])
    return _el('Sm2MpTimelineClip', _leaf('Name', t['name']) + _leaf('MpFolder', 'bin-master')
               + _el('TimelineSharedHandle', timeline), t['media'])


def _item_values(item):
    tag = item['tag']
    values = {'FieldsBlob': links(*item['links']) if item.get('links') else envelope(pb((1, pb((4, 1))))),
              'PrettyType': item.get('pretty'), 'Name': item['name'], 'Start': item['start'],
              'Duration': item['duration']}
    if tag != 'Sm2TiTransition':
        values['Flags'] = item.get('flags', 0)
        values.update({'In': item.get('in'), 'MediaRef': item.get('media'),
                       'MediaFilePath': None, 'MediaTimemapBA': item.get('timemap', compact_timemap()),
                       'MediaTrackIdx': 0 if tag == 'Sm2TiAudioClip' else None,
                       'EffectFiltersBA': item.get('effects')})
    else:
        values.update({'AlignmentType': item.get('alignment', 2), 'Position': item.get('alignment', 2)})
    return values


def lockable_blobs(spec):
    """(blob id, tag, owner, FieldsBlob) for every timeline and clip marker list in ``spec``."""
    out = [('lb-' + t['seq'], 'Sm2SequenceLockableBlob', t['seq'], markers(t['markers']))
           for t in spec['timelines'] if t['markers']]
    for t in spec['timelines']:
        for track in t['tracks']:
            for item in track['items']:
                if item.get('markers'):
                    out.append(('lb-' + item['id'], 'Sm2TiItemLockableBlob', item['id'], markers(item['markers'])))
    return out


def drp_bytes(spec):
    blobs = ''.join(_el('Element', _el(tag, _leaf('BlobOwner', owner) + _leaf('FieldsBlob', data), bid))
                    for bid, tag, owner, data in lockable_blobs(spec))
    if spec.get('marker_layout') == 'nested':
        # Older projects: LockableBlobMap/Sm2LockableBlobMap/LocableBlobSet/Element/<blob>.
        blobs = _el('Sm2LockableBlobMap', _el('LocableBlobSet', blobs), 'lbm-1')
    project = (_leaf('ProjectName', spec['name']) + _leaf('LastModTimeInSecs', spec['saved'])
               + _el('TimelineHandleVec', ''.join(_el('Element', t['id']) for t in spec['timelines']))
               + _el('LockableBlobMap', blobs))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('project.xml', HEADER + _el('SM_Project', project, spec['id']))
        for bid, name, parent in spec['bins']:
            media = [m for m in spec['media'] if m['bin'] == bid]
            body = ''.join(_el('Element', _media_xml(m)) for m in media)
            if parent is None:
                body += ''.join(_el('Element', _timeline_media_xml(t)) for t in spec['timelines'])
            folder = _el('Sm2MpFolder', _leaf('Name', name) + _leaf('MpFolder', parent) + _el('MediaVec', body), bid)
            path = 'MediaPool/Master/' + ('' if parent is None else name + '/') + 'MpFolder.xml'
            archive.writestr(path, HEADER + folder)
        for t in spec['timelines']:
            vecs = {0: [], 1: []}
            for track in t['tracks']:
                items = ''.join(_el('Element', _el(item['tag'], ''.join(
                    _leaf(k, v) for k, v in _item_values(item).items())
                    + (_el('pLmVerTable', _el('ListMgt::LmVersionTable', '', 'lm-' + item['id']))
                       if item.get('grade') else '<pLmVerTable/>')
                    + (_el('CompositionTable', _el('Sm2TiCompositionTable', '', 'ct-' + item['id']))
                       if item.get('fusion') else ''), item['id'])) for item in track['items'])
                vecs[track['type']].append(_el('Element', _el('Sm2TiTrack', _leaf('Type', track['type'])
                    + _leaf('Flags', track.get('flags', 0)) + _leaf('Sequence', t['seq']) + _el('Items', items) + _leaf('UserDefinedName', track['name'] or None),
                    track['id'])))
            body = _el('VideoTrackVec', ''.join(vecs[0])) + _el('AudioTrackVec', ''.join(vecs[1])) + '<SubtitleTrackVec/>'
            archive.writestr(f'SeqContainer/{t["container"]}.xml', HEADER + _el('Sm2SequenceContainer', body, t['container']))
        archive.writestr('Gallery.xml', HEADER + '<SM_Gallery/>')
    return out.getvalue()


# --------------------------------------------------------------------------- Project.db

JOIN = '(DbOwner TEXT, DbAssociate TEXT, DbPropertyName TEXT, DbIndex INTEGER)'
TABLES = {
    'SM_Project': ['SM_Project_id', 'DbType', 'ProjectName', 'LastModTimeInSecs', 'CommonConfig'],
    'SM_Config': ['SM_Config_id', 'DbType', 'FieldsBlob'],
    'BtLockableBlob': ['BtLockableBlob_id', 'DbType', 'BlobOwner', 'FieldsBlob'],
    'Sm2MpFolder': ['Sm2MpFolder_id', 'DbType', 'Name', 'MpFolder'],
    'Sm2MpMedia': ['Sm2MpMedia_id', 'DbType', 'Name', 'MpFolder', 'Video', 'Sequence', 'TimelineSharedHandle',
                   'Sm2MpFolder_id'],
    'BtVideoInfo': ['BtVideoInfo_id', 'DbType', 'Clip', 'Time', 'Geometry'],
    'BtAudioInfo': ['BtAudioInfo_id', 'DbType', 'Clip', 'TracksBA'],
    'Sm2Timeline': ['Sm2Timeline_id', 'DbType', 'Name', 'Sequence'],
    'Sm2Sequence': ['Sm2Sequence_id', 'DbType', 'FieldsBlob', 'FrameRate', 'Resolution', 'MediaExtents'],
    'Sm2SequenceContainer': ['Sm2SequenceContainer_id', 'DbType'],
    'Sm2TiTrack': ['Sm2TiTrack_id', 'DbType', 'Type', 'Flags', 'Sequence', 'UserDefinedName', 'Sm2SequenceContainer_id'],
    'Sm2TiItem': ['Sm2TiItem_id', 'DbType', 'FieldsBlob', 'PrettyType', 'Name', 'Start', 'Duration', 'In',
                  'MediaRef', 'MediaFilePath', 'MediaTimemapBA', 'MediaTrackIdx', 'EffectFiltersBA',
                  'AlignmentType', 'Position', 'pLmVerTable', 'CompositionTable', 'Flags', 'Sm2TiTrack_id'],
}
JOINS = ['SM_Project_Sm2Timeline', 'Sm2MpFolder_Sm2MpMedia', 'BtAudioInfo_Sm2MpMedia',
         'Sm2SequenceContainer_Sm2TiTrack', 'Sm2TiItem_Sm2TiTrack']


def write_db(spec, path):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute('PRAGMA journal_mode=DELETE')
        for table, columns in TABLES.items():
            conn.execute(f'CREATE TABLE "{table}" (' + ', '.join(f'"{c}"' for c in columns) + ')')
        for join in JOINS:
            conn.execute(f'CREATE TABLE "{join}" {JOIN}')

        def put(table, **values):
            cols = TABLES[table]
            conn.execute(f'INSERT INTO "{table}" VALUES (' + ','.join('?' * len(cols)) + ')',
                         [values.get(c) for c in cols])

        def link(join, owner, child, prop, index):
            conn.execute(f'INSERT INTO "{join}" VALUES (?,?,?,?)', (owner, child, prop, index))
        put('SM_Project', SM_Project_id=spec['id'], DbType='SM_Project', ProjectName=spec['name'],
            LastModTimeInSecs=spec['saved'])
        for i, t in enumerate(spec['timelines']):
            link('SM_Project_Sm2Timeline', spec['id'], t['id'], 'TimelineHandleVec', i)
        for bid, tag, owner, data in lockable_blobs(spec):
            put('BtLockableBlob', BtLockableBlob_id=bid, DbType=tag, BlobOwner=owner, FieldsBlob=data)
        for bid, name, parent in spec['bins']:
            put('Sm2MpFolder', Sm2MpFolder_id=bid, DbType='Sm2MpFolder', Name=name, MpFolder=parent)
        index = {}
        for m in spec['media']:
            n = index[m['bin']] = index.get(m['bin'], -1) + 1
            link('Sm2MpFolder_Sm2MpMedia', m['bin'], m['id'], 'MediaVec', n)
            if m['tag'] == 'Sm2MpAudioClip':
                put('Sm2MpMedia', Sm2MpMedia_id=m['id'], DbType=m['tag'], Name=m['name'], MpFolder=m['bin'],
                    Sm2MpFolder_id=m['bin'])
                put('BtAudioInfo', BtAudioInfo_id=m['info'], DbType='BtAudioInfo', Clip=clip_blob(m['dir'], m['file']),
                    TracksBA=keyed({'0': keyed({'DbType': 'BtAudioTrack', 'SampleRate': 48000, 'NumChannels': 2,
                                                'Duration': m['samples']})}))
                link('BtAudioInfo_Sm2MpMedia', m['id'], m['info'], 'EmbeddedAudioVec', 0)
            else:
                put('Sm2MpMedia', Sm2MpMedia_id=m['id'], DbType=m['tag'], Name=m['name'], MpFolder=m['bin'],
                    Video=m['info'], Sm2MpFolder_id=m['bin'])
                put('BtVideoInfo', BtVideoInfo_id=m['info'], DbType='BtVideoInfo', Clip=clip_blob(m['dir'], m['file']),
                    Time=keyed({'NumFrames': m['frames'], 'StartFrame': 0, 'FrameRate': rate(FPS)}),
                    Geometry=keyed({'Resolution': resolution(*m['size'])}))
        for t in spec['timelines']:
            n = index['bin-master'] = index.get('bin-master', -1) + 1
            link('Sm2MpFolder_Sm2MpMedia', 'bin-master', t['media'], 'MediaVec', n)
            put('Sm2MpMedia', Sm2MpMedia_id=t['media'], DbType='Sm2MpTimelineClip', Name=t['name'],
                MpFolder='bin-master', TimelineSharedHandle=t['id'], Sm2MpFolder_id='bin-master')
            put('Sm2Timeline', Sm2Timeline_id=t['id'], DbType='Sm2Timeline', Name=t['name'], Sequence=t['seq'])
            put('Sm2Sequence', Sm2Sequence_id=t['seq'], DbType='Sm2Sequence', FieldsBlob=keyed(_sequence_fields(t)),
                FrameRate=rate(t['fps']), Resolution=resolution(*t['size']), MediaExtents=extents(t['start'], 60.0))
            put('Sm2SequenceContainer', Sm2SequenceContainer_id=t['container'], DbType='Sm2SequenceContainer')
            counts = {0: 0, 1: 0}
            for track in t['tracks']:
                prop = 'VideoTrackVec' if track['type'] == 0 else 'AudioTrackVec'
                link('Sm2SequenceContainer_Sm2TiTrack', t['container'], track['id'], prop, counts[track['type']])
                counts[track['type']] += 1
                put('Sm2TiTrack', Sm2TiTrack_id=track['id'], DbType='Sm2TiTrack', Type=track['type'],
                    Flags=track.get('flags', 0), Sequence=t['seq'],
                    UserDefinedName=track['name'] or None, Sm2SequenceContainer_id=t['container'])
                for i, item in enumerate(track['items']):
                    link('Sm2TiItem_Sm2TiTrack', track['id'], item['id'], 'Items', i)
                    values = _item_values(item)
                    put('Sm2TiItem', Sm2TiItem_id=item['id'], DbType=item['tag'], Sm2TiTrack_id=track['id'],
                        pLmVerTable='lm-' + item['id'] if item.get('grade') else None,
                        CompositionTable='ct-' + item['id'] if item.get('fusion') else None, **values)
        conn.commit()
    finally:
        conn.close()


def write_library(spec, folder, user='guest'):
    """A disk library folder holding ``spec`` as one project; returns the Project.db path.

    An existing project of the same name is replaced the way a save replaces it (a new file
    moved over the old one)."""
    project = Path(folder) / 'Resolve Projects' / 'Users' / user / 'Projects' / spec['name']
    project.mkdir(parents=True, exist_ok=True)
    fresh = project / 'Project.db.new'
    fresh.unlink(missing_ok=True)
    write_db(spec, fresh)
    fresh.replace(project / 'Project.db')
    return project / 'Project.db'


# --------------------------------------------------------------------------- Resolve 21.1 seeds
#
# A synthetic project laid out the way Resolve 21.1 (project version 17) stores a disk-library
# project, with every column the writer reads or writes, for the save-to-library tests. The values
# are invented; the layouts (keyed dicts, envelopes, protobuf fields) follow the field maps in
# resolve/mediapool.py and resolve/items.py. ``SeedSpec`` lists what it holds.

ITEM_COLUMNS = (
    'Sm2TiItem_id', 'DbType', 'Name', 'Start', 'Duration', 'Track', 'LinkedItemSync', 'MarkersBA', 'UiMemento',
    'Flags', 'PriorityIndex', 'In', 'MediaRef', 'MediaStartTime', 'MediaFilePath', 'MediaReelNumber',
    'MediaTimemapBA', 'Group', 'pLmVerTable', 'pAuxLmVerTable', 'Thumbnail', 'ThumbnailDirtyFlag',
    'RenderTextEnabled', 'RenderTextGanged', 'RenderTextPrefixed', 'TextRenderItemVec', 'LastChangedTime',
    'LastRenderedTime', 'IsMarkedForCaching', 'IsForceConformed', 'MatchConflictState', 'MediaTrackIdx',
    'PrettyType', 'Resolution', 'EffectFiltersBA', 'IsPreConformed', 'PreConformMediaExtents',
    'ImportExportMetadataBA', 'MediaFrameRate', 'Sm2TiTrack_id', 'VirtualAudioTrackBA', 'MixedFrameRateAlignment',
    'UseOppositeSrcForLeftEye', 'UseOppositeSrcForRightEye', 'WasDisbanded', 'CurrentSelectorIdx', 'AlignmentType',
    'Position', 'RenderCacheBA', 'ClipGroup', 'MediaMetadata', 'FieldsBlob', 'CompositionTable', 'BlobLockSysId',
    'OriginalClip', 'Sm2TiItem_Owner_id', 'CompTableLockSysId', 'VersionTableLockSysId')
SEED_TABLES = {
    'SM_Project': ['SM_Project_id', 'DbType', 'ProjectName', 'ProjectVersion', 'LastModTimeInSecs', 'CommonConfig'],
    'SM_Config': ['SM_Config_id', 'DbType', 'FieldsBlob', 'SM_Project_id'],
    'BtLockableBlobMap': ['BtLockableBlobMap_id', 'DbType', 'SM_Project_id'],
    'BtLockableBlob': ['BtLockableBlob_id', 'DbType', 'BtLockableBlobMap_id', 'FieldsBlob', 'BlobOwner', 'DbSavedTime'],
    'Sm2MpFolder': ['Sm2MpFolder_id', 'DbType', 'Name', 'MpFolder'],
    'Sm2MpMedia': ['Sm2MpMedia_id', 'DbType', 'Name', 'MpFolder', 'CurPlayheadPosition', 'AudioSource', 'PTZRPreset',
                   'SlateTC', 'VideoMetadata', 'MediaMetadata', 'Sm2MpFolder_id', 'Video', 'Sequence',
                   'UniqueMediaPoolItemId', 'VideoType', 'PinsBA', 'TimelineSharedHandle', 'FieldsBlob',
                   'BlobLockSysId'],
    'BtVideoInfo': ['BtVideoInfo_id', 'DbType', 'Clip', 'Time', 'Geometry', 'Radiometry', 'Proxy', 'VideoMetadata',
                    'MediaMetadata', 'Type', 'Eye', 'Sm2MpMedia_id', 'FieldsBlob', 'BlobLockSysId'],
    'BtAudioInfo': ['BtAudioInfo_id', 'DbType', 'Clip', 'TracksBA', 'MediaMetadata', 'Sm2MpMedia_id',
                    'Sm2Timeline_id', 'FieldsBlob', 'BlobLockSysId'],
    'SmPreset': ['SmPreset_id', 'DbType', 'Sm2MpMedia_id'],
    'Sm2Timeline': ['Sm2Timeline_id', 'DbType', 'Name', 'Sequence', 'MpTimelineClip', 'Sm2MpMedia_id'],
    'Sm2Sequence': ['Sm2Sequence_id', 'DbType', 'FieldsBlob', 'FrameRate', 'Resolution', 'MediaExtents',
                    'Sm2Timeline_id'],
    'Sm2SequenceContainer': ['Sm2SequenceContainer_id', 'DbType', 'Sm2Sequence_id'],
    'Sm2TiTrack': ['Sm2TiTrack_id', 'DbType', 'Type', 'SubType', 'Flags', 'Sequence', 'AudioMixerBA',
                   'UserDefinedName', 'FieldsBlob', 'Sm2SequenceContainer_id'],
    'Sm2TiItem': list(ITEM_COLUMNS),
    'ListMgt::LmVersionTable': ['ListMgt::LmVersionTable_id', 'DbType', 'Sm2TiItem_id', 'Sm2MpMedia_id'],
    'TriggerControl': ['Name', 'Enable'],
}
_BLOBS = {'FieldsBlob', 'Clip', 'Time', 'Geometry', 'Radiometry', 'Proxy', 'VideoMetadata', 'MediaMetadata',
          'TracksBA', 'FrameRate', 'Resolution', 'MediaExtents', 'MarkersBA', 'MediaTimemapBA', 'EffectFiltersBA',
          'PreConformMediaExtents', 'ImportExportMetadataBA', 'MediaFrameRate', 'VirtualAudioTrackBA', 'RenderCacheBA',
          'AudioMixerBA', 'PinsBA', 'SlateTC'}
_INTEGERS = {'ProjectVersion', 'LastModTimeInSecs', 'DbSavedTime', 'Type', 'SubType', 'Flags', 'UiMemento',
             'PriorityIndex', 'ThumbnailDirtyFlag', 'RenderTextEnabled', 'RenderTextGanged', 'RenderTextPrefixed',
             'LastChangedTime', 'LastRenderedTime', 'IsMarkedForCaching', 'IsForceConformed', 'MatchConflictState',
             'MediaTrackIdx', 'IsPreConformed', 'UseOppositeSrcForLeftEye', 'UseOppositeSrcForRightEye', 'WasDisbanded',
             'CurrentSelectorIdx', 'AlignmentType', 'Position', 'VideoType', 'Enable'}
_DOUBLES = {'MediaStartTime', 'MixedFrameRateAlignment'}


def declared(column):
    """The type Resolve's schema declares for a column (as far as these tests need)."""
    if column in _BLOBS:
        return 'BLOB'
    if column in _INTEGERS:
        return 'integer'
    if column in _DOUBLES:
        return 'double precision'
    return 'character varying'


SEED_JOINS = ['SM_Project_Sm2Timeline', 'Sm2MpFolder_Sm2MpMedia', 'BtAudioInfo_Sm2MpMedia',
              'Sm2SequenceContainer_Sm2TiTrack', 'Sm2TiItem_Sm2TiTrack']
SEED_TRIGGERS = [
    "CREATE TRIGGER Delete_BtVideoInfo_trigger\r\n\tAFTER DELETE ON BtVideoInfo\r\nBEGIN\r\n    DELETE FROM BtLockableBlob "
    "WHERE 1 = (SELECT Enable FROM TriggerControl WHERE Name = 'master_trigger') AND BlobOwner = OLD.BtVideoInfo_id;"
    "\r\nEND",
    "CREATE TRIGGER Delete_BtAudioInfo_trigger\r\n\tAFTER DELETE ON BtAudioInfo\r\nBEGIN\r\n    DELETE FROM BtLockableBlob "
    "WHERE 1 = (SELECT Enable FROM TriggerControl WHERE Name = 'master_trigger') AND BlobOwner = OLD.BtAudioInfo_id;"
    "\r\nEND"]
_ITEM_PLAIN = {
    'Track': None, 'LinkedItemSync': None, 'UiMemento': 0, 'Flags': 0, 'PriorityIndex': 0, 'MediaReelNumber': None,
    'Group': None, 'pAuxLmVerTable': None, 'Thumbnail': None, 'ThumbnailDirtyFlag': 1, 'RenderTextEnabled': 1,
    'RenderTextGanged': 1, 'RenderTextPrefixed': 1, 'TextRenderItemVec': None, 'LastChangedTime': 0,
    'LastRenderedTime': 0, 'IsMarkedForCaching': 0, 'IsForceConformed': 1, 'MatchConflictState': 0,
    'PrettyType': None, 'Resolution': None, 'IsPreConformed': 0, 'MixedFrameRateAlignment': 0.0,
    'UseOppositeSrcForLeftEye': 0, 'UseOppositeSrcForRightEye': 0, 'WasDisbanded': 0, 'AlignmentType': None,
    'Position': None, 'RenderCacheBA': None, 'ClipGroup': None, 'BlobLockSysId': '', 'OriginalClip': None,
    'Sm2TiItem_Owner_id': None, 'CompTableLockSysId': '', 'VersionTableLockSysId': ''}
NTSC = 30000 / 1001


def kd_typed(entries):
    """A keyed dict from ``(key, type, value)``: 0x02 int32, 0x03 uint32, 0x04 int64, 0x06 double,
    0x01 bool, 0x0A text, 0x0C bytes."""
    out = struct.pack('>II', 1, len(entries))
    for key, vtype, value in entries:
        body = {0x01: lambda v: bytes([v]), 0x02: lambda v: struct.pack('>i', v), 0x03: lambda v: struct.pack('>I', v),
                0x04: lambda v: struct.pack('>q', v), 0x06: lambda v: struct.pack('>d', v),
                0x0A: lambda v: qstring(v), 0x0C: lambda v: struct.pack('>I', len(v)) + v}[vtype](value)
        out += qstring(key) + struct.pack('>IB', vtype, 0) + body
    return out


def env(payload, version=2):
    """An envelope framed the way Resolve frames a message of its length."""
    if len(payload) <= 64:
        body = b'\x80' + payload
    else:
        body = b'\x81' + zstd_frame(payload)
    return struct.pack('>II', version, len(body)) + body


def file_facts(path):
    """What the rows of a file hold about it, measured here (mtime as wall-clock text, creation
    time from the container's tag or the file system, ffprobe)."""
    import json as _json
    import os
    import subprocess
    import time as _time
    info = os.stat(path)
    probe = _json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json',
                                        str(path)], capture_output=True, check=True).stdout)
    local = _time.localtime(info.st_mtime)
    days, months = 'Mon Tue Wed Thu Fri Sat Sun'.split(), 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
    mtime = (f'{days[local.tm_wday]} {months[local.tm_mon - 1]} {local.tm_mday:2d} {local.tm_hour:02d}:'
             f'{local.tm_min:02d}:{local.tm_sec:02d} {local.tm_year:04d}')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from resolve.facts import _birth_ns
    tag = (probe['format'].get('tags') or {}).get('creation_time')
    if tag:
        from datetime import datetime, timezone
        whole = datetime.strptime(tag[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        created = int(whole.timestamp()) * 1_000_000 + int((tag[20:26] or '0').ljust(6, '0'))
    else:
        created = _birth_ns(path) // 1000
    return {'mtime': mtime, 'created': created, 'probe': probe, 'size': info.st_size}


def clip_message(folder, name, mtime, created, codec, picture, fresh):
    fields = [(1, folder), (2, name), (3, mtime), (5, codec)]
    if picture:
        fields += [(6, name), (7, fresh())]
    fields += [(13, created), (14, 1234), (15, 2), (16, 100), (18, 16384 if picture else 32768)]
    return env(pb(*fields))


def wav_rows(ids, folder, name, facts, bin_id, index, map_id, added=1767225600, layout=True):
    """A 24-bit stereo WAV's media-pool rows (Sm2MpAudioClip), with its metadata blob."""
    stream = next(s for s in facts['probe']['streams'] if s['codec_type'] == 'audio')
    rate = int(stream['sample_rate'])
    samples = stream['duration_ts']
    media, info, uid = ids(), ids(), ids()
    clip = clip_message(folder, name, facts['mtime'], facts['created'], 'Linear PCM', False, ids)
    track = [('UniqueId', 0x0A, ids()), ('StartTime', 0x06, 0.0), ('SampleRate', 0x03, rate),
             ('NumChannels', 0x02, stream['channels']), ('IdxTrack', 0x02, 0), ('Duration', 0x04, samples),
             ('DbType', 0x0A, 'BtAudioTrack'), ('CodecName', 0x0A, 'Linear PCM')]
    if layout:
        track.append(('ChannelLayout', 0x02, 2))
    track.append(('BitDepth', 0x03, 2))
    channels = kd_typed([('1', 0x0C, kd_typed([('MediaRef', 0x0A, info), ('ChannelIdx', 0x02, 1)])),
                         ('0', 0x0C, kd_typed([('MediaRef', 0x0A, info)]))])
    extents = kd_typed([('0', 0x0C, kd_typed([('Type', 0x03, 0), ('MediaExtents', 0x0C, struct.pack('<dd', 0.0,
                                                                                                   samples / rate)),
                                              ('CodecName', 0x0A, 'Linear PCM'), ('ChannelVecBA', 0x0C, channels)]))])
    level = pb((1, pb((1, pb((2, (added << 1) ^ (added >> 63)))), (6, 4))), (5, extents))
    fields = env(pb((1, level), (2, pb((1, NTSC)))))
    entries = pb((2, pb((1, 215), (2, name))), (2, pb((1, 16), (2, b''))), (2, pb((1, 422), (2, b''))))
    inner = struct.pack('>II', 1000001, len(entries)) + entries
    metadata = keyed({'BlobData': env(pb((2, inner)), 10001)})
    return {
        'Sm2MpMedia': [{'Sm2MpMedia_id': media, 'DbType': 'Sm2MpAudioClip', 'Name': name, 'MpFolder': bin_id,
                        'CurPlayheadPosition': '0', 'AudioSource': 'AUDIO_SOURCE_EMBEDDED', 'PTZRPreset': None,
                        'SlateTC': '-68719476737', 'Sm2MpFolder_id': bin_id, 'Video': None,
                        'UniqueMediaPoolItemId': uid, 'VideoType': 0, 'FieldsBlob': fields, 'BlobLockSysId': ''}],
        'BtAudioInfo': [{'BtAudioInfo_id': info, 'DbType': 'BtAudioInfo', 'Clip': clip,
                         'TracksBA': kd_typed([('0', 0x0C, kd_typed(track))]), 'Sm2MpMedia_id': media,
                         'BlobLockSysId': ''}],
        'BtAudioInfo_Sm2MpMedia': [{'DbOwner': media, 'DbAssociate': info, 'DbPropertyName': 'EmbeddedAudioVec',
                                    'DbIndex': 0}],
        'Sm2MpFolder_Sm2MpMedia': [{'DbOwner': bin_id, 'DbAssociate': media, 'DbPropertyName': 'MediaVec',
                                    'DbIndex': index}],
        'BtLockableBlob': [{'BtLockableBlob_id': ids(), 'DbType': 'BtMetadataLockableBlob',
                            'BtLockableBlobMap_id': map_id, 'FieldsBlob': metadata, 'BlobOwner': info,
                            'DbSavedTime': 5}]}


def prores_rows(ids, folder, name, facts, bin_id, index, added=1767225600):
    """A ProRes 422 HQ picture's media-pool rows (Sm2MpVideoClip, no sound)."""
    stream = next(s for s in facts['probe']['streams'] if s['codec_type'] == 'video')
    tc = next((s['tags']['timecode'] for s in facts['probe']['streams'] if (s.get('tags') or {}).get('timecode')), None)
    base = stream['time_base'].split('/')
    frames = stream['duration_ts'] * 30000 * int(base[0]) // (int(base[1]) * 1001)
    media, info = ids(), ids()
    clip = clip_message(folder, name, facts['mtime'], facts['created'], 'apch', True, ids)
    time_entries = [('UniqueId', 0x0A, ids())] + ([('Timecode', 0x0A, tc)] if tc else []) + [
        ('StartFrame', 0x02, 0), ('NumFrames', 0x02, frames), ('FrameRate', 0x0C, rate(NTSC)),
        ('DbType', 0x0A, 'BtVideoTime')]
    geometry = kd_typed([('UniqueId', 0x0A, ids()), ('Resolution', 0x0C, resolution(stream['width'],
                                                                                    stream['height'])),
                         ('FrameSize', 0x02, stream['width'] * stream['height'] * 8 // 3 + 8192),
                         ('DbType', 0x0A, 'BtGeometry')])
    fields = env(pb((1, pb((1, pb((1, pb((1, pb((2, (added << 1) ^ (added >> 63)))), (6, 4))))),
                       (2, bytes.fromhex('000000000000000600000000ffffffff'))))))
    return {
        'Sm2MpMedia': [{'Sm2MpMedia_id': media, 'DbType': 'Sm2MpVideoClip', 'Name': name, 'MpFolder': bin_id,
                        'CurPlayheadPosition': '0', 'AudioSource': 'AUDIO_SOURCE_EMBEDDED', 'PTZRPreset': None,
                        'SlateTC': '-68719476737', 'Sm2MpFolder_id': bin_id, 'Video': info,
                        'UniqueMediaPoolItemId': ids(), 'VideoType': 0, 'FieldsBlob': fields, 'BlobLockSysId': ''}],
        'BtVideoInfo': [{'BtVideoInfo_id': info, 'DbType': 'BtVideoInfo', 'Clip': clip,
                         'Time': kd_typed(time_entries), 'Geometry': geometry,
                         'Radiometry': b'\x80' + pb((1, 404), (2, 2), (5, 16)),
                         'Proxy': kd_typed([('UniqueId', 0x0A, ids()), ('DbType', 0x0A, 'BtVideoProxy'),
                                            ('DataManagerID', 0x02, 50)]),
                         'VideoMetadata': kd_typed([('UniqueId', 0x0A, ids()), ('DbType', 0x0A, 'BtVideoMetadata')]),
                         'Type': 'STANDARD_CLIP', 'Eye': 'SUB_TRACK_MONO', 'Sm2MpMedia_id': media, 'BlobLockSysId': ''}],
        'Sm2MpFolder_Sm2MpMedia': [{'DbOwner': bin_id, 'DbAssociate': media, 'DbPropertyName': 'MediaVec',
                                    'DbIndex': index}]}


def item_row(ids, kind, track, start, duration, src_in, media_id, path, frames_or_samples, links=(), **extra):
    """A plain picture (``video``) or sound (``audio``) item, as Resolve writes one."""
    item = ids()
    picture = kind == 'video'
    inner = ([(1, pb((1, keyed_links(links))))] if links else []) + [(4, 1)]
    tail = (2, bytes.fromhex('000000000000000600000000ffffffff')) if picture else (15, 4)
    if picture:
        last = frames_or_samples / NTSC - 1 / NTSC
    else:
        seconds = frames_or_samples / 48000
        last = seconds * NTSC / NTSC - 1 / NTSC
    row = dict(_ITEM_PLAIN, Sm2TiItem_id=item, DbType='Sm2TiVideoClip' if picture else 'Sm2TiAudioClip',
               Name=path.rpartition('\\')[2], Start=str(start), Duration=str(duration),
               In=str(src_in) if src_in else None, MediaRef=media_id, MediaStartTime=3600.0 * 1.001 if picture else 0.0,
               MediaFilePath=path, MediaTimemapBA=b'\x02' + struct.pack('>d', last),
               MediaFrameRate=rate(NTSC), Sm2TiTrack_id=track, CurrentSelectorIdx=0,
               PreConformMediaExtents=bytes.fromhex('00000100000030c20000010000003042') if picture else None,
               MediaTrackIdx=None if picture else 0, FieldsBlob=env(pb((1, pb(*inner)), tail)),
               VirtualAudioTrackBA=None if picture else keyed({'ChannelsBA': struct.pack('>IIII', 2, 2, 0x4001, 0x4002),
                                                               'AudioType': 0}),
               pLmVerTable=None, CompositionTable=None, EffectFiltersBA=None, ImportExportMetadataBA=None,
               MarkersBA=None, MediaMetadata=None)
    row.update(extra)
    return row


def kd_frames(time_blob):
    """``NumFrames`` of a ``Time`` keyed dict."""
    count = struct.unpack_from('>I', time_blob, 4)[0]
    offset = 8
    for _ in range(count):
        (length,) = struct.unpack_from('>I', time_blob, offset)
        key = time_blob[offset + 4:offset + 4 + length].decode('utf-16-be')
        offset += 4 + length
        vtype = struct.unpack_from('>I', time_blob, offset)[0]
        offset += 5
        if vtype == 0x02:
            value = struct.unpack_from('>i', time_blob, offset)[0]
            offset += 4
            if key == 'NumFrames':
                return value
        else:
            (size,) = struct.unpack_from('>I', time_blob, offset)
            offset += 4 + size
    raise ValueError('no NumFrames')


def keyed_links(partners):
    out = struct.pack('>II', 1, len(partners))
    for i in reversed(range(len(partners))):
        out += qstring(str(i)) + struct.pack('>IB', 0x0A, 0) + qstring(partners[i])
    return out


def write_seed(path, pic, snd, *, name='Seed project', with_grade=False, timeline_name='Cut 1'):
    """A Resolve 21.1-shaped ``Project.db`` at ``path`` with one 29.97 fps timeline starting at
    01:00:00:00: V1 and V2 (picture) and A1 and A2 (sound) tracks; on V1 three picture items of
    ``pic`` (a ProRes 422 HQ file) linked to their sound items on A1 of ``snd`` (a 24-bit stereo
    WAV), and a 4-frame Cross Fade +3 dB on A1's first cut. ``pic`` and ``snd`` are
    ``(Resolve path, local path)``. Returns a dict of the ids made."""
    import itertools
    counter = itertools.count(1)

    def ids():
        n = next(counter)
        return f'{n:08x}-5eed-4000-8000-{n:012x}'
    out = {}
    pic_facts, snd_facts = file_facts(pic[1]), file_facts(snd[1])
    project, config, lmap = ids(), ids(), ids()
    master, day = ids(), ids()
    timeline, sequence, container, tl_media = ids(), ids(), ids(), ids()
    v1, v2, a1, a2 = ids(), ids(), ids(), ids()
    pic_rows = prores_rows(ids, pic[0].rpartition('\\')[0], pic[0].rpartition('\\')[2], pic_facts, day, 0)
    snd_rows = wav_rows(ids, snd[0].rpartition('\\')[0], snd[0].rpartition('\\')[2], snd_facts, day, 1, lmap)
    pic_id, snd_id = pic_rows['Sm2MpMedia'][0]['Sm2MpMedia_id'], snd_rows['Sm2MpMedia'][0]['Sm2MpMedia_id']
    frames = kd_frames(pic_rows['BtVideoInfo'][0]['Time'])
    samples = next(s for s in snd_facts['probe']['streams'] if s['codec_type'] == 'audio')['duration_ts']
    start = 108000  # 01:00:00:00 in non-drop timecode at 29.97 fps
    conn = sqlite3.connect(str(path))
    try:
        conn.execute('PRAGMA journal_mode=DELETE')
        for table, columns in SEED_TABLES.items():
            conn.execute(f'CREATE TABLE "{table}" (' + ', '.join(f'"{c}" {declared(c)}' for c in columns) + ')')
        for join in SEED_JOINS:
            conn.execute(f'CREATE TABLE "{join}" {JOIN}')
        for trigger in SEED_TRIGGERS:
            conn.execute(trigger)

        def put(table, **values):
            cols = SEED_TABLES[table]
            unknown = set(values) - set(cols)
            assert not unknown, (table, unknown)
            conn.execute(f'INSERT INTO "{table}" VALUES (' + ','.join('?' * len(cols)) + ')',
                         [values.get(c) for c in cols])

        def link(join, owner, child, prop, index):
            conn.execute(f'INSERT INTO "{join}" VALUES (?,?,?,?)', (owner, child, prop, index))
        put('TriggerControl', Name='master_trigger', Enable=1)
        put('SM_Project', SM_Project_id=project, DbType='SM_Project', ProjectName=name, ProjectVersion=17,
            LastModTimeInSecs=1767225600, CommonConfig=config)
        setup = env(pb((1, '1920 x 1080 HD'), (69, '29.97')))
        put('SM_Config', SM_Config_id=config, DbType='SM_Config', FieldsBlob=keyed({'SetupBA': setup}),
            SM_Project_id=project)
        put('BtLockableBlobMap', BtLockableBlobMap_id=lmap, DbType='Sm2LockableBlobMap', SM_Project_id=project)
        put('Sm2MpFolder', Sm2MpFolder_id=master, DbType='Sm2MpFolder', Name='Master', MpFolder=None)
        put('Sm2MpFolder', Sm2MpFolder_id=day, DbType='Sm2MpFolder', Name='Day 1', MpFolder=master)
        for rows in (pic_rows, snd_rows):
            for table, table_rows in rows.items():
                for row in table_rows:
                    if table in SEED_TABLES:
                        put(table, **row)
                    else:
                        conn.execute(f'INSERT INTO "{table}" VALUES (?,?,?,?)',
                                     (row['DbOwner'], row['DbAssociate'], row['DbPropertyName'], row['DbIndex']))
        link('SM_Project_Sm2Timeline', project, timeline, 'TimelineHandleVec', 0)
        link('Sm2MpFolder_Sm2MpMedia', master, tl_media, 'MediaVec', 0)
        put('Sm2MpMedia', Sm2MpMedia_id=tl_media, DbType='Sm2MpTimelineClip', Name=timeline_name, MpFolder=master,
            Sm2MpFolder_id=master, TimelineSharedHandle=timeline, BlobLockSysId='')
        put('Sm2Timeline', Sm2Timeline_id=timeline, DbType='Sm2Timeline', Name=timeline_name, Sequence=sequence,
            MpTimelineClip=tl_media, Sm2MpMedia_id=tl_media)
        put('Sm2Sequence', Sm2Sequence_id=sequence, DbType='Sm2Sequence',
            FieldsBlob=keyed({'SeqRef': container}), FrameRate=rate(NTSC), Resolution=bytes(16),
            MediaExtents=extents(start / NTSC, 0.0), Sm2Timeline_id=timeline)
        put('Sm2SequenceContainer', Sm2SequenceContainer_id=container, DbType='Sm2SequenceContainer',
            Sm2Sequence_id=sequence)
        for index, (track, kind) in enumerate(((v1, 0), (v2, 0))):
            link('Sm2SequenceContainer_Sm2TiTrack', container, track, 'VideoTrackVec', index)
        for index, track in enumerate((a1, a2)):
            link('Sm2SequenceContainer_Sm2TiTrack', container, track, 'AudioTrackVec', index)
        for track, kind in ((v1, 0), (v2, 0), (a1, 1), (a2, 1)):
            put('Sm2TiTrack', Sm2TiTrack_id=track, DbType='Sm2TiTrack', Type=kind, SubType=0, Flags=0,
                Sequence=sequence, UserDefinedName='', FieldsBlob=keyed({'Height': 1}),
                Sm2SequenceContainer_id=container)
        pic_path, snd_path = pic[0], snd[0]
        cut = [(0, 10, 2), (10, 8, 14), (18, 6, 0)]
        pictures, sounds = [], []
        for n, (at, length, src) in enumerate(cut):
            p_id, s_id = ids(), ids()
            pictures.append((p_id, s_id, at, length, src))
        rows = []
        for p_id, s_id, at, length, src in pictures:
            p = item_row(lambda: p_id, 'video', v1, start + at, length, src, pic_id, pic_path, frames, (s_id,))
            s = item_row(lambda: s_id, 'audio', a1, start + at, length, src, snd_id, snd_path, samples, (p_id,))
            rows.append((v1, p))
            rows.append((a1, s))
        if with_grade:
            rows[0][1]['pLmVerTable'] = 'grade-0000-4000-8000-000000000001'
            put('ListMgt::LmVersionTable', **{'ListMgt::LmVersionTable_id': 'grade-0000-4000-8000-000000000001',
                                             'DbType': 'ListMgt::LmVersionTable',
                                             'Sm2TiItem_id': rows[0][1]['Sm2TiItem_id']})
        transition = dict(_ITEM_PLAIN, Sm2TiItem_id=ids(), DbType='Sm2TiTransition', Name='Transition',
                          Start=str(start + 10 - 2), Duration='4', PrettyType='Cross Fade +3DB', AlignmentType=2,
                          Position=2, Sm2TiTrack_id=a1, LastChangedTime=None, LastRenderedTime=None,
                          IsMarkedForCaching=None, CurrentSelectorIdx=0,
                          FieldsBlob=env(pb((2, bytes.fromhex('0000002c789c636660642016000000da0005')))),
                          MarkersBA=None, In=None, MediaRef=None, MediaStartTime=None, MediaFilePath=None,
                          MediaTimemapBA=None, pLmVerTable=None, EffectFiltersBA=None, PreConformMediaExtents=None,
                          ImportExportMetadataBA=None, MediaFrameRate=None, VirtualAudioTrackBA=None,
                          MediaMetadata=None, CompositionTable=None)
        order = {v1: [r for t, r in rows if t == v1], a1: [r for t, r in rows if t == a1]}
        order[a1].insert(1, transition)
        for track, track_rows in order.items():
            for index, row in enumerate(track_rows):
                put('Sm2TiItem', **row)
                link('Sm2TiItem_Sm2TiTrack', track, row['Sm2TiItem_id'], 'Items', index)
        conn.execute('UPDATE Sm2Sequence SET MediaExtents = ? WHERE Sm2Sequence_id = ?',
                     (extents(start / NTSC, (start + 24) / NTSC - start / NTSC), sequence))
        conn.commit()
    finally:
        conn.close()
    out.update(project=project, timeline=timeline, sequence=sequence, v1=v1, v2=v2, a1=a1, a2=a2, pic=pic_id,
               snd=snd_id, pictures=[p[0] for p in pictures], sounds=[p[1] for p in pictures],
               transition=transition['Sm2TiItem_id'], start=start, bin=day, frames=frames, samples=samples)
    return out


def seed_library(folder, pic, snd, name='Seed project', user='guest', **kwargs):
    """A library holding one seed project; returns (Project.db path, ids)."""
    project = Path(folder) / 'Resolve Projects' / 'Users' / user / 'Projects' / name
    project.mkdir(parents=True, exist_ok=True)
    made = write_seed(project / 'Project.db', pic, snd, name=name, **kwargs)
    return project / 'Project.db', made


def make_media(folder):
    """A 1-second ProRes 422 HQ picture with timecode and a 3-second 24-bit stereo WAV, made with
    FFmpeg in ``folder``."""
    import subprocess
    pic, snd = Path(folder) / 'pic.mov', Path(folder) / 'snd.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=gray:s=1920x1080:r=30000/1001:d=1',
                    '-vf', 'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv', '-c:v',
                    'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le', '-timecode', '01:00:00:00',
                    '-metadata', 'creation_time=2026-01-02T03:04:05.000000Z', str(pic)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=f=440:r=48000:d=3', '-ac', '2',
                    '-c:a', 'pcm_s24le', str(snd)], check=True)
    return pic, snd


def drp_from_db(db_path, out_path):
    """A ``.drp`` laid out from a library ``Project.db`` the way the reader lays rows out as XML
    (binary columns as hex), so a test can open the seed as a .drp."""
    import xml.etree.ElementTree as ET
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from resolve import source
    conn = source.connect(db_path, 10 ** 12)
    try:
        layout = source._Layout(source._Rows(conn))
        project, bins, containers = layout.project(), layout.bins(), layout.containers()
    finally:
        conn.close()

    def hexed(node):
        for el in node.iter():
            if isinstance(el.text, bytes):
                el.text = el.text.hex()
        return HEADER.replace('DbPrjVer="15"', 'DbPrjVer="17"') + ET.tostring(node, encoding='unicode')
    parents = {f.get('DbId'): (f.findtext('Name'), f.findtext('MpFolder')) for f in bins}

    def folder_path(fid):
        names, seen = [], set()
        while fid in parents and fid not in seen:
            seen.add(fid)
            names.append(parents[fid][0])
            fid = parents[fid][1]
        return '/'.join(reversed(names))
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('project.xml', hexed(project))
        for folder in bins:
            archive.writestr(f'MediaPool/{folder_path(folder.get("DbId"))}/MpFolder.xml', hexed(folder))
        for container in containers:
            archive.writestr(f'SeqContainer/{container.get("DbId")}.xml', hexed(container))


def _seed_main(argv):
    """``seed LIBRARY MEDIA_FOLDER DRP_OUT``: FFmpeg media in MEDIA_FOLDER, a library holding a
    Resolve 21.1-shaped seed project that names them by their paths here, the same project as a
    .drp at DRP_OUT, and a second project in the library (the schema donor a .drp save needs)."""
    library, folder, drp_out = argv
    pic, snd = make_media(folder)
    db, made = seed_library(library, (str(pic), str(pic)), (str(snd), str(snd)), name='Seed project')
    import shutil
    donor = Path(library) / 'Resolve Projects' / 'Users' / 'guest' / 'Projects' / 'Template'
    donor.mkdir()
    shutil.copyfile(db, donor / 'Project.db')
    drp_from_db(db, drp_out)
    print(json.dumps({'db': str(db), 'pic': str(pic), 'snd': str(snd), 'start': made['start']}))


if __name__ == '__main__' and sys.argv[1:2] == ['seed']:
    import json
    _seed_main(sys.argv[2:])
elif __name__ == '__main__':
    mode, out, video, audio = sys.argv[1:5]
    spec = sample(video, audio, generator='--no-generator' not in sys.argv[5:])
    if '--states' in sys.argv[5:]:
        spec['timelines'][0]['tracks'][1]['flags'] = 3
    if '--reverse' in sys.argv[5:]:  # Resolve reordered the timelines and saved again
        spec['timelines'].reverse()
        spec['saved'] += 60
    if mode == 'drp':
        Path(out).write_bytes(drp_bytes(spec))
    else:
        print(write_library(spec, out))
