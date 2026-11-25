"""Timeline interchange entry points for the loopback server.

``export_timeline(project, options)`` returns the timeline files, a plain-text report of what each
format could not carry, and with ``options['package']`` a turnover zip of both. The report lists
every clip-level loss; nothing is dropped without a line in it.

``import_timeline(filename, text, options)`` reads FCPXML, a CMX3600 EDL or OTIO into a new Splice
project whose ``compatibilityWarnings`` list everything that was not imported.

``REFERENCE_MOVIE_OPTION``: the turnover hook for a reference movie with burnt-in timecode. This
module does not render; when the option is set the package carries
``REFERENCE-MOVIE-REQUEST.json`` (range, start timecode, frame rate) for the renderer to fulfil.
"""
from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from datetime import datetime, timezone

from . import edl, fcpxml, otio
from .cut import InterchangeError, build_cut
from .timecode import frames_timecode, rate_text

FORMATS = ('fcpxml', 'edl', 'otio')
REFERENCE_MOVIE_OPTION = 'referenceMovie'
MAX_IMPORT_BYTES = 30 * 1024 * 1024


def _safe(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_-]+', '-', name).strip('-')[:80] or 'timeline'


def report_text(cut, formats, files) -> str:
    report = cut.report
    lines = [f'Splice timeline turnover report: {cut.name}',
             f'Written {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}',
             f'Frame rate {rate_text(cut.rate)} ({float(cut.rate):.3f} fps), {cut.width} x {cut.height}, '
             f'{"drop" if cut.drop else "non-drop"} frame timecode',
             f'Range {frames_timecode(cut.tc_start, cut.rate, cut.drop)} - {cut.timecode(cut.frames)} '
             f'({cut.frames} frames' + (f', starting {cut.range_start} frames into the Splice timeline' if cut.range_start else '') + ')',
             f'Clips {sum(1 for e in cut.events)}, sources {len({id(e.source) for e in cut.events if e.source})}, markers {len(cut.markers)}',
             '', 'Files:'] + [f'  {name}' for name, _ in files]
    lines += ['', 'Not carried by any format (all formats):']
    lines += [f'  - {entry}' for entry in report.general] or ['  (nothing)']
    titles = {'fcpxml': 'FCPXML', 'edl': 'EDL', 'otio': 'OTIO'}
    for fmt in formats:
        key = titles[fmt]
        lines += ['', f'{key}:']
        lines += [f'  - {entry}' for entry in report.by_format.get(key, [])] or ['  (everything above aside, nothing further)']
    return '\n'.join(lines) + '\n'


def export_timeline(project: dict, options: dict | None = None) -> dict:
    options = dict(options or {})
    formats = [f for f in options.get('formats') or ['fcpxml'] if f in FORMATS]
    if not formats:
        raise InterchangeError('Choose at least one timeline format')
    cut = build_cut(project, options)
    if not cut.events and not cut.markers:
        raise InterchangeError('The timeline has nothing to export')
    base = _safe(cut.name)
    files = []
    if 'fcpxml' in formats:
        files.append((f'{base}.fcpxml', fcpxml.write(cut)))
    if 'edl' in formats:
        for suffix, text in edl.write(cut, audio=bool(options.get('audioEdl'))):
            files.append((f'{base}-{suffix}.edl', text))
    if 'otio' in formats:
        files.append((f'{base}.otio', otio.write(cut)))
    report = report_text(cut, formats, files)
    result = {'files': [{'name': name, 'content': text} for name, text in files], 'report': report,
              'issues': len(cut.report.general) + sum(len(v) for v in cut.report.by_format.values())}
    if options.get('package'):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, text in files:
                archive.writestr(f'{base}/{name}', text)
            archive.writestr(f'{base}/{base}-REPORT.txt', report)
            if options.get(REFERENCE_MOVIE_OPTION):
                request = {'render': 'reference movie with burnt-in timecode', 'status': 'not rendered by the timeline exporter',
                           'range': {'startFrame': cut.range_start, 'frames': cut.frames},
                           'startTimecode': frames_timecode(cut.tc_start, cut.rate, cut.drop),
                           'rate': rate_text(cut.rate), 'dropFrame': cut.drop}
                archive.writestr(f'{base}/REFERENCE-MOVIE-REQUEST.json', json.dumps(request, indent=2) + '\n')
        result['package'] = {'name': f'{base}-turnover.zip', 'base64': base64.b64encode(buffer.getvalue()).decode('ascii')}
    return result


def detect(filename: str, data: bytes) -> str:
    name = filename.lower()
    head = data[:2048].lstrip()
    if name.endswith(('.fcpxml', '.xml')) or head.startswith(b'<'):
        return 'fcpxml'
    if name.endswith('.otio') or head.startswith(b'{'):
        return 'otio'
    if name.endswith('.edl') or b'FCM:' in data[:4096].upper() or b'TITLE:' in data[:4096].upper():
        return 'edl'
    raise InterchangeError('Choose an .fcpxml, .edl or .otio timeline file')


def import_timeline(filename: str, text: str, options: dict | None = None) -> dict:
    if not isinstance(text, str) or not isinstance(filename, str):
        raise InterchangeError('Invalid timeline upload')
    data = text.encode('utf-8')
    if len(data) > MAX_IMPORT_BYTES:
        raise InterchangeError('The timeline file is larger than 30 MB')
    kind = detect(filename, data)
    if kind == 'fcpxml':
        project = fcpxml.read(data)
    elif kind == 'otio':
        project = otio.read(data)
    else:
        project = edl.read(data, options or {})
    if not project['clips']:
        raise InterchangeError('The timeline file contains no clips Splice can import')
    project['compatibilityWarnings'].insert(0, f'Imported from {filename[:120]} ({kind.upper()}).')
    return project


def handle(action: str, payload: dict) -> dict:
    """Server entry: ``export`` -> files and report, ``import`` -> ``{'project': ...}``."""
    if not isinstance(payload, dict):
        raise InterchangeError('Invalid request')
    if action == 'export':
        return export_timeline(payload.get('project'), payload.get('options') or {})
    if action == 'import':
        return {'project': import_timeline(payload.get('filename', ''), payload.get('text', ''), payload.get('options') or {})}
    raise InterchangeError('Unknown interchange action')
