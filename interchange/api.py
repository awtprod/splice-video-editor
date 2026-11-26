"""Timeline interchange entry points for the loopback server.

``export_timeline(project, options)`` returns the timeline files (FCPXML 1.10, CMX3600 EDLs,
OpenTimelineIO, Final Cut Pro 7 XML), a plain-text report of what each format could not carry, and
with ``options['package']`` a turnover zip of both. The report lists every clip-level loss; nothing
is dropped without a line in it.

``import_timeline(filename, text, options)`` reads FCPXML, FCP 7 XML, a CMX3600 EDL or OTIO into a
new Splice project whose ``compatibilityWarnings`` list everything that was not imported. With
``options['linkMedia']`` (always set by :func:`handle`, the server's entry) sources that exist on
this machine are linked in place through ``media_links``; the rest arrive offline. An
import is bounded in time and in the timeline items it may visit (:mod:`.budget`) and can be
stopped with ``handle('cancel', {'jobId': ...})`` using the ``options['jobId']`` it was started with.

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

from . import edl, fcpxml, otio, xmeml
from .budget import Budget, BudgetExceeded, cancel
from .cut import InterchangeError, build_cut
from .project import link_local_media
from .timecode import frames_timecode, rate_text

FORMATS = ('fcpxml', 'edl', 'otio', 'xmeml')
TITLES = {'fcpxml': 'FCPXML', 'edl': 'EDL', 'otio': 'OTIO', 'xmeml': 'XML'}
HEADINGS = {'fcpxml': 'FCPXML 1.10 (DaVinci Resolve, Final Cut Pro)', 'edl': 'CMX3600 EDL',
            'otio': 'OpenTimelineIO', 'xmeml': 'Final Cut Pro 7 XML (Premiere Pro, Resolve, Avid)'}
REFERENCE_MOVIE_OPTION = 'referenceMovie'
MAX_IMPORT_BYTES = 30 * 1024 * 1024
#: seconds an import may spend checking local media before linking it in place
LINK_SECONDS = 60.0


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
    lines += ['', 'Media the timeline files reference:'] + (media_lines(cut) or ['  (no media)'])
    if cut.path_rules:
        lines += ['', 'Path mapping (applied to every media path in the files above):'] + path_map_lines(cut)
    lines += ['', 'Not carried by any format (all formats):']
    lines += [f'  - {entry}' for entry in report.general] or ['  (nothing)']
    for fmt in formats:
        key = TITLES[fmt]
        lines += ['', f'{HEADINGS[fmt]}:']
        lines += [f'  - {entry}' for entry in report.by_format.get(key, [])] or ['  (everything above aside, nothing further)']
    return '\n'.join(lines) + '\n'


LOCATIONS = {
    'linked': 'linked original',
    'managed': 'managed copy in Splice\'s media folder (an uploaded file, not the camera original); the receiving '
               'machine needs this file, so copy it with the turnover',
    'bundled': 'file bundled with Splice; the receiving machine needs a copy of it',
    'external': 'path recorded in the project (not linked in Splice)',
}


def _used_sources(cut):
    seen = {}
    for event in cut.events:
        source = event.source
        if source is not None and not source.nested and not source.generator:
            seen.setdefault(id(source), source)
    return sorted(seen.values(), key=lambda s: s.name.lower())


def media_lines(cut) -> list:
    lines = []
    for source in _used_sources(cut):
        if not source.path:
            lines.append(f'  - {source.name}: no file referenced (offline in Splice); relink it before turning over')
            continue
        where = LOCATIONS.get(source.location, source.location)
        if source.original_path is not None and source.path_rule is not None:
            lines.append(f'  - {source.name}: {where} {source.original_path}, written as {source.path}')
        else:
            lines.append(f'  - {source.name}: {where} {source.path}')
    return lines


def path_map_lines(cut) -> list:
    used = _used_sources(cut)
    lines = []
    for index, (source, target) in enumerate(cut.path_rules):
        count = sum(1 for s in used if s.path_rule == index)
        lines.append(f'  {index + 1}. {source} -> {target} ({count} source{"" if count == 1 else "s"})')
    missed = [s.name for s in used if s.path and s.path_rule is None]
    if missed:
        lines.append('  Not under any mapping, written unchanged: ' + ', '.join(missed[:20]) +
                     (f' and {len(missed) - 20} more' if len(missed) > 20 else ''))
    return lines


def _options(options) -> dict:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise InterchangeError('Interchange options must be an object')
    return dict(options)


def export_timeline(project: dict, options: dict | None = None) -> dict:
    options = _options(options)
    requested = options.get('formats') or ['fcpxml']
    if not isinstance(requested, list):
        raise InterchangeError('Choose the timeline formats as a list')
    formats = [f for f in FORMATS if f in requested]
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
    if 'xmeml' in formats:
        files.append((f'{base}.xml', xmeml.write(cut)))
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
    head = data[:4096].lstrip(b'\xef\xbb\xbf \t\r\n')
    if name.endswith(('.fcpxml', '.xml')) or head.startswith(b'<'):
        return 'xmeml' if re.search(rb'<xmeml[\s>]', head) else 'fcpxml'
    if name.endswith('.otio') or head.startswith(b'{'):
        return 'otio'
    if name.endswith('.edl') or b'FCM:' in data[:4096].upper() or b'TITLE:' in data[:4096].upper():
        return 'edl'
    raise InterchangeError('Choose an .fcpxml, .xml, .edl or .otio timeline file')


def _dimension(value, default):
    if value is None or value == '':
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        raise InterchangeError(f'Frame size {str(value)[:20]!r} is not a whole number of pixels') from None
    if not 16 <= number <= 16384:
        raise InterchangeError(f'Frame size {number} is outside 16-16384 pixels')
    return number


def import_timeline(filename: str, text: str, options: dict | None = None) -> dict:
    options = _options(options)
    if not isinstance(text, str) or not isinstance(filename, str):
        raise InterchangeError('Invalid timeline upload')
    data = text.encode('utf-8')
    if len(data) > MAX_IMPORT_BYTES:
        raise InterchangeError('The timeline file is larger than 30 MB')
    kind = detect(filename, data)
    with Budget(job=options.get('jobId')) as budget:
        if kind == 'fcpxml':
            project = fcpxml.read(data, budget)
        elif kind == 'xmeml':
            project = xmeml.read(data, budget)
        elif kind == 'otio':
            project = otio.read(data, budget)
        else:
            if options.get('fps') in (None, ''):
                raise InterchangeError('An EDL does not say its frame rate; choose it (options.fps) and import again')
            edl_options = dict(options, width=_dimension(options.get('width'), 1920),
                               height=_dimension(options.get('height'), 1080))
            project = edl.read(data, edl_options, budget)
        budget.check()
    if not project['clips']:
        raise InterchangeError('The timeline file contains no clips Splice can import')
    if options.get('linkMedia') is True:
        with Budget(seconds=LINK_SECONDS, job=options.get('jobId'), what='Linking the timeline\'s media') as budget:
            link_local_media(project, budget)
    label = {'xmeml': 'FCP 7 XML'}.get(kind, kind.upper())
    project['compatibilityWarnings'].insert(0, f'Imported from {filename[:120]} ({label}).')
    return project


def handle(action: str, payload: dict) -> dict:
    """Server entry: ``export`` -> files and report, ``import`` -> ``{'project': ...}``,
    ``cancel`` -> ``{'cancelled': bool}`` for a running import's ``jobId``."""
    if not isinstance(payload, dict):
        raise InterchangeError('Invalid request')
    if action == 'export':
        return export_timeline(payload.get('project'), payload.get('options'))
    if action == 'import':
        # in the server, media found on this machine is linked in place (see link_local_media)
        options = dict(_options(payload.get('options')), linkMedia=True)
        return {'project': import_timeline(payload.get('filename', ''), payload.get('text', ''), options)}
    if action == 'cancel':
        return {'cancelled': cancel(payload.get('jobId'))}
    raise InterchangeError('Unknown interchange action')
