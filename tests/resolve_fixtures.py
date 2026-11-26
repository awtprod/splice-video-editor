"""Synthetic DaVinci Resolve projects for tests: the same invented project as a .drp archive and
as a disk-library Project.db, laid out the way Resolve writes them. Run as a script to write one:

    python3 tests/resolve_fixtures.py drp OUT.drp VIDEO_PATH AUDIO_PATH [--no-generator]
    python3 tests/resolve_fixtures.py library LIBRARY_FOLDER VIDEO_PATH AUDIO_PATH [--no-generator]
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


def effects(*entries):
    """entries: (effect type, {param id: value}) -> an EffectFiltersBA envelope."""
    out = []
    for kind, params in entries:
        fields = [(1, kind), (7, 0)]
        for pid, value in params.items():
            fields.append((9, pb((1, pid), (3, pb((1, pb((2, float(value)))))))))
        out.append((1, pb(*fields)))
    return envelope(pb(*out), compress=True)


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


def _sequence(t):
    return _el('Sm2Sequence', _leaf('FieldsBlob', keyed({'SeqRef': t['container']}))
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
            put('Sm2Sequence', Sm2Sequence_id=t['seq'], DbType='Sm2Sequence', FieldsBlob=keyed({'SeqRef': t['container']}),
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


if __name__ == '__main__':
    mode, out, video, audio = sys.argv[1:5]
    spec = sample(video, audio, generator='--no-generator' not in sys.argv[5:])
    if '--reverse' in sys.argv[5:]:  # Resolve reordered the timelines and saved again
        spec['timelines'].reverse()
        spec['saved'] += 60
    if mode == 'drp':
        Path(out).write_bytes(drp_bytes(spec))
    else:
        print(write_library(spec, out))
