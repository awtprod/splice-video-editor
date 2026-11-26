"""CMX3600 edit decision lists: one list per picture track, optional sound lists, and an importer.

An EDL is one track of events with source and record timecode, all counted at one frame rate: the
EDL's (the sequence's). Conventions written here, and read back by :func:`read`:

* reels are at most eight characters of ``A-Z 0-9 _``. A shortened or altered name is mapped in a
  ``* REEL`` comment inside the first event that uses it (comments before the first event break
  readers such as OpenTimelineIO's ``cmx_3600``); every event carries ``* FROM CLIP NAME:`` and
  ``* SOURCE FILE:`` comments;
* record timecode starts at the sequence start (01:00:00:00 by default); the first ``FCM`` line
  (the header) says whether record timecode is drop frame, and every drop-frame label uses ``;``;
* **source timecode is counted at the EDL's rate**, the CMX rule that every reader expects. A
  source at another rate has its start timecode label read at the EDL's rate (hours, minutes and
  seconds kept, frames scaled: a 29.97 source starting 01:00:00;00 starts 01:00:00:00 in a
  23.976 list) and its in point counted in EDL frames from there, so an in point between two EDL
  frames is rounded to the nearer one and reported. Every ``FCM`` line sets the source frame code
  mode of the events after it: when an event's source is non-drop in a drop-frame list (or the
  reverse, at 29.97 and 59.94 only) an ``FCM`` line precedes it, and another switches back;
* a dissolve is the CMX pair: a zero-length cut on the outgoing reel at the start of the
  dissolve, then the ``D`` event of the incoming clip; the outgoing event ends where the dissolve
  starts, and the incoming source in includes the frames before the cut;
* a speed change is an ``M2`` line with the playback rate in source frames per second at the EDL
  rate (negative for reverse) and the source in timecode written with colons only, the form the
  reference parser reads; the source range lists the media the clip really uses;
* sequence markers are Avid ``* LOC:`` locator comments on the first picture list.
"""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

from .cut import Cut, Event
from .media import base_name, is_absolute_path, path_from_url, probe_file
from .timecode import (DROP_FRAME_RATES, convert_label, frames_timecode, is_drop_frame, sequence_rate,
                       timecode_frames)

FORMAT = 'EDL'
MAX_EDL_BYTES = 10 * 1024 * 1024
MAX_EVENTS = 20_000
_TC = r'\d{2}[:;.,]\d{2}[:;.,]\d{2}[:;.,]\d{2,3}'
_EVENT = re.compile(rf'^(\d{{1,6}})\s+(\S+)\s+(\S+)\s+(C|D|W\d{{3}}|K\s*[BO]?|KB|KO)\s+(?:(\d{{1,5}})\s+)?({_TC})\s+({_TC})\s+({_TC})\s+({_TC})\s*$')
_M2 = re.compile(rf'^M2\s+(\S+)\s+(-?\d+(?:\.\d+)?)\s+({_TC})\s*$')
_LOC = re.compile(rf'^\*\s*LOC:\s*({_TC})\s+(\S+)\s*(.*)$')
COLORS = ('RED', 'GREEN', 'BLUE', 'CYAN', 'MAGENTA', 'YELLOW', 'WHITE', 'BLACK')


def reel_names(cut: Cut) -> tuple[dict, dict]:
    """Reel per source (by source id), and the mapping comment for each altered reel."""
    reels, used, comments = {}, set(), {}
    sources = sorted({id(e.source): e.source for e in cut.events if e.source is not None}.values(),
                     key=lambda s: (s.reel or s.name, s.id))
    for source in sources:
        original = source.reel or Path(base_name(source.path or source.name)).stem or source.name
        clean = re.sub(r'[^A-Z0-9_]', '', re.sub(r'[\s\-.]+', '_', original.upper())) or 'AX'
        candidate, counter = clean[:8], 1
        while candidate in used or candidate in ('BL', 'AX'):
            candidate = f'{clean[:5]}{counter:03d}'
            counter += 1
        used.add(candidate)
        reels[source.id] = candidate
        if candidate != original:
            comments[candidate] = f'* REEL {candidate} = {base_name(source.path) if source.path else source.name}'
    return reels, comments


class _Writer:
    def __init__(self, cut: Cut):
        self.cut = cut
        self.rate = cut.rate
        self.reels, self.reel_comments = reel_names(cut)

    def note(self, event, message):
        self.cut.report.add(FORMAT, f'{self.cut.label(event)}: {message}')

    def rec(self, frame):
        return self.cut.timecode(frame)

    def source_drop(self, source) -> bool:
        """Drop-frame source timecode exists in the list only when the list itself is 29.97/59.94."""
        return bool(source.drop and source.tc_frames is not None and self.rate in DROP_FRAME_RATES)

    def start_frames(self, source) -> int:
        """The source's start timecode as a frame count at the EDL's rate."""
        if source.tc_frames is not None and source.tc_rate:
            return convert_label(source.tc_frames, source.tc_rate, source.drop and source.tc_rate in DROP_FRAME_RATES,
                                 self.rate, self.source_drop(source))
        return round(source.start * self.rate)

    def src_frames(self, source, seconds: Fraction) -> int:
        return self.start_frames(source) + round(seconds * self.rate)

    def src(self, source, seconds: Fraction) -> str:
        return frames_timecode(self.src_frames(source, seconds), self.rate, self.source_drop(source))

    def line(self, number, reel, channel, kind, duration, src_in, src_out, rec_in, rec_out):
        # three digits, as CMX3600 has; event 1000 and later simply print four
        return (f'{number:03d}  {reel:<8} {channel:<5} {kind:<4} {duration:>3} '
                f'{src_in} {src_out} {rec_in} {rec_out}')

    def write(self, events, title, sound=False, markers=False) -> str:
        cut = self.cut
        lines = [f'TITLE: {title[:70]}', f'FCM: {"DROP FRAME" if cut.drop else "NON-DROP FRAME"}', '']
        mode = cut.drop
        mapped = set()
        ordered = sorted(events, key=lambda e: e.record_in)
        by_end = {e.record_out: e for e in ordered if e.enabled}
        trims = {}
        for event in ordered:
            # only a dissolve that is written trims the clip before it
            if event.enabled and event.transition is not None and event.transition.before and event.record_in in by_end:
                trims[by_end[event.record_in].id] = min(event.transition.before, by_end[event.record_in].frames - 1)
        number = 0
        locators = sorted(cut.markers, key=lambda m: m.frame) if markers else []

        def frame_mode(drop):
            nonlocal mode
            if drop != mode:
                lines.append(f'FCM: {"DROP FRAME" if drop else "NON-DROP FRAME"}')
                mode = drop
        for event in ordered:
            if not event.enabled:
                self.note(event, 'disabled; CMX3600 has no disabled events, so it was left out of the EDL.')
                continue
            source = event.source
            reel = self.reels[source.id]
            channel = ('AA' if source.channels >= 2 else 'A') if sound else 'V'
            number += 1
            visible_end = event.record_out - trims.get(event.id, 0)
            first = event.record_in
            head = Fraction(0)
            tr = event.transition
            kind, dur = 'C', ''
            outgoing = by_end.get(event.record_in) if tr is not None else None
            if tr is not None:
                first = event.record_in - tr.before
                head = Fraction(tr.before) / cut.rate * event.speed
                if outgoing is not None:
                    out_src = outgoing.source_in + Fraction(outgoing.frames - trims.get(outgoing.id, 0)) / cut.rate * outgoing.speed
                    from_reel = self.reels[outgoing.source.id]
                    frame_mode(self.source_drop(outgoing.source))
                    tc = self.src(outgoing.source, out_src)
                else:
                    frame_mode(cut.drop)
                    from_reel, tc = 'BL', frames_timecode(0, cut.rate, cut.drop)
                    if number == 1 and first > 0:
                        # black up to a fade-in that opens the list: readers (OpenTimelineIO's cmx_3600 among them)
                        # refuse a list whose first item is a transition
                        lines.append(self.line(number, 'BL', channel, 'C', '', tc, frames_timecode(first, cut.rate, cut.drop),
                                               self.rec(0), self.rec(first)))
                        number += 1
                    elif number == 1:
                        self.note(event, 'the list opens with a dissolve from black at its first frame; some EDL readers '
                                         '(OpenTimelineIO cmx_3600) refuse a list that starts with a transition.')
                lines.append(self.line(number, from_reel, channel, 'C', '', tc, tc, self.rec(first), self.rec(first)))
                kind = 'D' if tr.kind != 'wipe' else 'W001'
                dur = f'{tr.frames:03d}'
                if tr.kind == 'fade':
                    self.note(event, 'dip to black written as a dissolve.')
                if event.reverse:
                    self.note(event, 'a transition on a reversed clip uses the source frames after its out point in the EDL; check the handle.')
            start_seconds = event.source_in - (head if not event.reverse else 0)
            if event.reverse:
                start_seconds = event.source_in + Fraction(event.frames - (visible_end - event.record_in)) / cut.rate * event.speed
            start_seconds = max(Fraction(0), start_seconds)
            used = round(Fraction(visible_end - first) * event.speed)
            first_src = self.src_frames(source, start_seconds)
            drop = self.source_drop(source)
            frame_mode(drop)
            src_in = frames_timecode(first_src, self.rate, drop)
            src_out = frames_timecode(first_src + used, self.rate, drop)
            lines.append(self.line(number, reel, channel, kind, dur, src_in, src_out, self.rec(first), self.rec(visible_end)))
            if event.speed != 1 or event.reverse:
                fps = float(event.speed * self.rate) * (-1 if event.reverse else 1)
                text = f'{fps:05.1f}' if abs(fps * 10 - round(fps * 10)) < 1e-6 else f'{fps:.3f}'
                lines.append(f'M2   {reel:<8}       {text:>6}                {src_in.replace(";", ":")}')
            if reel in self.reel_comments and reel not in mapped:
                lines.append(self.reel_comments[reel])
                mapped.add(reel)
            if tr is not None and outgoing is not None:
                lines.append(f'* FROM CLIP NAME: {outgoing.name}')
                lines.append(f'* TO CLIP NAME: {event.name}')
            else:
                lines.append(f'* FROM CLIP NAME: {event.name}')
            if source.path:
                lines.append(f'* SOURCE FILE: {source.path}')
            while locators and locators[0].frame < visible_end:
                marker = locators.pop(0)
                color = marker.color.upper() if marker.color.upper() in COLORS else 'RED'
                lines.append(f'* LOC: {self.rec(marker.frame)} {color:<7} {marker.name.replace(chr(10), " ")[:60]}')
            self._report_effects(event, sound, start_seconds)
        for marker in locators:
            lines.append(f'* LOC: {self.rec(marker.frame)} RED     {marker.name.replace(chr(10), " ")[:60]}')
        if number > 999:
            cut.report.add(FORMAT, f'{title}: {number} events exceed CMX3600\'s 999; event numbers use four digits.')
        return '\n'.join(lines) + '\n'

    def _report_effects(self, event: Event, sound: bool, start_seconds: Fraction):
        if sound:
            if event.volume != 100 or event.fade_in or event.fade_out or event.keyframes.get('volume'):
                self.note(event, 'clip gain, fades and volume keyframes are not carried by an EDL.')
        else:
            moved = any(abs(event.transform.get(k, d) - d) > 1e-9 for k, d in (('x', 0), ('y', 0), ('scale', 100), ('rotation', 0)))
            if moved or event.opacity != 100 or any(event.keyframes.get(k) for k in ('x', 'y', 'scale', 'rotation', 'opacity')):
                self.note(event, 'transform, opacity and their keyframes are not carried by an EDL.')
        source = event.source
        if source is not None and source.rate and source.rate != self.rate:
            exact = start_seconds * self.rate
            message = (f'source runs at {float(source.rate):.3f} fps; its source timecode is counted at the list\'s '
                       f'{float(self.rate):.3f} fps from its start label')
            if exact.denominator != 1:
                message += f', so its in point is rounded to the nearest list frame ({float(abs(exact - round(exact)) / self.rate * source.rate):.2f} source frames)'
            self.note(event, message + '.')
        if source is not None and source.drop and source.tc_frames is not None and self.rate not in DROP_FRAME_RATES:
            self.note(event, 'its drop-frame source timecode is written as non-drop at the list\'s rate.')


def _conform_warnings(cut: Cut, writer: _Writer):
    """Resolve (and most conform tools) match EDL events to media by source timecode, and by reel
    name only when told to. Sources that timecode alone cannot tell apart are named here."""
    sources = sorted({id(e.source): e.source for e in cut.events if e.source is not None and e.kind != 'title'}.values(),
                     key=lambda s: s.name)
    blind = [s.name for s in sources if s.timecode_state != 'embedded' and not s.reel]
    if blind:
        shown = ', '.join(blind[:12]) + (f' and {len(blind) - 12} more' if len(blind) > 12 else '')
        cut.report.add(FORMAT, f'CONFORM WARNING: {shown} ha{"ve" if len(blind) > 1 else "s"} no embedded timecode and no reel name. '
                               'DaVinci Resolve conforms an EDL by source timecode, so it cannot tell such sources apart and '
                               'may link every event to one file. Hand these to Resolve as FCPXML or FCP 7 XML instead.')
    spans = []
    for source in sources:
        start = writer.start_frames(source)
        length = max(1, round(source.duration * cut.rate)) if source.duration else 1
        spans.append((source, start, start + length))
    clashes = []
    for index, (a, a0, a1) in enumerate(spans):
        for b, b0, b1 in spans[index + 1:]:
            picture = (a.has_video and a.kind == 'video', b.has_video and b.kind == 'video')
            if picture[0] == picture[1] and a0 < b1 and b0 < a1:   # picture and sound lists conform apart
                same = writer.reels[a.id] == writer.reels[b.id]
                clashes.append(f'{a.name} and {b.name}' + (' (same reel too)' if same else ''))
    if clashes:
        shown = '; '.join(clashes[:12]) + (f'; and {len(clashes) - 12} more pairs' if len(clashes) > 12 else '')
        cut.report.add(FORMAT, f'CONFORM WARNING: these sources share source timecode, so a timecode-only conform (Resolve\'s '
                               f'default ignores reel names) can link one to the other: {shown}. Use FCPXML or FCP 7 XML, '
                               'or turn on reel-name matching in the conform options.')
    cut.report.add(FORMAT, 'Premiere Pro shows its own "EDL information" and "New Sequence" dialogs when it imports an EDL; '
                           'that is normal and needs a click per file.')


def write(cut: Cut, *, audio: bool = False) -> list:
    """``[(name suffix, text)]``: one EDL per picture track, and with ``audio`` one per sound track
    plus one for the sound embedded in each picture track."""
    writer = _Writer(cut)
    files = []
    pictures = sorted({e.track for e in cut.events if e.kind == 'video'})
    for event in cut.events:
        if event.kind == 'title':
            cut.report.add(FORMAT, f'{cut.label(event)}: titles are generated in Splice and have no source reel; '
                                   'not in the EDL.')
    first_picture = pictures[0] if pictures else None
    for track in pictures:
        events = [e for e in cut.events if e.kind == 'video' and e.track == track]
        if not any(e.enabled for e in events) and not (track == first_picture and cut.markers):
            for event in events:
                writer.note(event, 'disabled; CMX3600 has no disabled events, so it was left out of the EDL.')
            cut.report.add(FORMAT, f'V{track} holds only disabled clips, so no EDL was written for it.')
            continue
        files.append((f'V{track}', writer.write(events, f'{cut.name} V{track}', markers=track == first_picture)))
    if audio:
        for track in sorted({-e.track for e in cut.events if e.kind == 'audio'}):
            events = [e for e in cut.events if e.kind == 'audio' and e.track == -track]
            files.append((f'A{track}', writer.write(events, f'{cut.name} A{track}', sound=True)))
        for track in pictures:
            events = [e for e in cut.events if e.kind == 'video' and e.track == track and e.embedded_audio]
            if events:
                files.append((f'V{track}-sound', writer.write(events, f'{cut.name} V{track} camera sound', sound=True)))
        if any(e.kind == 'audio' or e.embedded_audio for e in cut.events):
            cut.report.add(FORMAT, 'Sound EDLs: DaVinci Resolve 21 refuses an EDL that holds only audio events ("Ignoring audio '
                                   'event", import fails); give Resolve the FCPXML or FCP 7 XML for sound. Avid, Pro Tools '
                                   'conform tools and Premiere read sound EDLs.')
    if not files:
        if first_picture is None and cut.markers:
            files.append(('V1', writer.write([], f'{cut.name} V1', markers=True)))
        else:
            raise ValueError('The timeline has no clips an EDL can list')
    elif not audio and any(e.kind == 'audio' or e.embedded_audio for e in cut.events):
        cut.report.add(FORMAT, 'Sound is not in the picture EDLs; choose the audio EDL option for sound lists.')
    _conform_warnings(cut, writer)
    return files


# -- import -------------------------------------------------------------------------------------

def _channel(text: str):
    """(kind, audio track, has picture sound) for an EDL channel field."""
    upper = text.upper()
    if upper in ('V', 'VIDEO'):
        return 'video', None, False
    if upper in ('B', 'AA/V', 'A/V', 'A2/V', 'A1/V'):
        return 'video', None, True
    match = re.fullmatch(r'A(\d*)', upper)
    if match:
        return 'audio', -(int(match.group(1) or 1)), False
    if upper in ('AA', 'A1A2', 'A12'):
        return 'audio', -1, False
    if upper == 'NONE':
        return None, None, False
    return 'video', None, False


def _dimension(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'Frame size {str(value)[:20]!r} is not a whole number of pixels') from None
    if not 16 <= number <= 16384:
        raise ValueError(f'Frame size {number} is outside 16-16384 pixels')
    return number


def read(data: bytes, options: dict | None = None, budget=None) -> dict:
    """Read a CMX3600 EDL at ``options['fps']``. Source timecode is counted at that rate (the
    convention :func:`write` follows); ``FCM`` lines after the first event switch the source
    timecode between drop and non-drop frame for the events that follow."""
    from .project import build_project
    options = options if options is not None else {}
    if not isinstance(options, dict):
        raise ValueError('EDL import options must be an object')
    if len(data) > MAX_EDL_BYTES:
        raise ValueError('The EDL is larger than 10 MB')
    text = data.decode('utf-8-sig', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data)
    if '\x00' in text:
        raise ValueError('The EDL contains binary data')
    try:
        rate = sequence_rate(options.get('fps', 25))
    except ValueError as error:
        raise ValueError(str(error)) from None
    width, height = _dimension(options.get('width', 1920)), _dimension(options.get('height', 1080))
    warnings, title, record_drop, source_drop, header_fcm = [], None, False, False, None
    events, current, markers = [], None, []
    for number, raw in enumerate(text.splitlines(), 1):
        if budget is not None:
            budget.tick()
        line = raw.strip()
        if not line:
            continue
        if len(events) > MAX_EVENTS:
            raise ValueError(f'The EDL has more than {MAX_EVENTS} events')
        if line.upper().startswith('TITLE:'):
            title = line[6:].strip()[:120]
            continue
        if line.upper().startswith('FCM:'):
            drop = 'NON' not in line.upper() and 'DROP' in line.upper()
            if header_fcm is None and not events:   # the first FCM is the list's (record) mode
                header_fcm = record_drop = drop
            source_drop = drop                       # every FCM sets the source mode of the events after it
            continue
        match = _EVENT.match(line)
        if match:
            current = {'number': int(match.group(1)), 'reel': match.group(2), 'channel': match.group(3),
                       'kind': match.group(4).replace(' ', ''), 'dur': int(match.group(5) or 0),
                       'tc': match.groups()[5:9], 'comments': {}, 'line': number, 'm2': None, 'drop': source_drop}
            events.append(current)
            continue
        m2 = _M2.match(line)
        if m2 and current is not None:
            current['m2'] = float(m2.group(2))
            continue
        loc = _LOC.match(line)
        if loc:
            markers.append((loc.group(1), (loc.group(3).strip() or loc.group(2))[:500]))
            continue
        if line.startswith('*'):
            body = line[1:].strip()
            key, _, value = body.partition(':')
            if re.match(r'REEL\s+(\S+)\s*=\s*(.+)$', body):
                continue
            if current is not None and key.strip().upper() in ('FROM CLIP NAME', 'TO CLIP NAME', 'SOURCE FILE', 'CLIP NAME'):
                current['comments'][key.strip().upper()] = value.strip()[:1000]
            continue
        if re.match(r'^(SPLIT|SWM|AUD|EFFECTS NAME)', line.upper()):
            warnings.append(f'EDL line {number} ({line.split()[0]}) is not supported and was ignored.')
            continue
        warnings.append(f'EDL line {number} could not be read and was ignored: {line[:60]}')
    if not events:
        raise ValueError('The EDL contains no events')

    def frames(label, drop_default=False):
        return timecode_frames(label, rate, is_drop_frame(label) or (drop_default and rate in DROP_FRAME_RATES))

    try:
        first_record = min(frames(e['tc'][2], record_drop) for e in events)
        if options.get('startTimecode'):
            label = str(options['startTimecode'])
            tc_start = timecode_frames(label, rate, (is_drop_frame(label) or record_drop) and rate in DROP_FRAME_RATES)
        else:
            per_hour = round(rate) * 3600 - (108 * round(rate) // 30 if record_drop else 0)
            tc_start = (first_record // per_hour) * per_hour
    except ValueError as error:
        raise ValueError(f'Malformed timecode in the EDL: {error}') from None

    clips, media = [], {}
    asked = options.get('maxProbes', 200)
    probes = [min(200, asked) if isinstance(asked, int) and not isinstance(asked, bool) and asked >= 0 else 200]
    index = 0
    while index < len(events):
        event = events[index]
        partner = events[index + 1] if index + 1 < len(events) else None
        # a zero-length cut followed by D/W with the same number is the outgoing side of a transition
        if (partner is not None and partner['number'] == event['number'] and event['kind'] == 'C'
                and partner['kind'] != 'C' and event['tc'][2] == event['tc'][3]):
            index += 1
            continue
        index += 1
        keyed = event['kind'].startswith('K')
        if keyed:
            warnings.append(f'Event {event["number"]}: keys are not supported; imported as a cut.')
        kind, audio_track, picture_sound = _channel(event['channel'])
        if kind is None or event['reel'].upper() in ('BL', 'BLK', 'BLACK'):
            continue
        try:
            src_in_label, src_out_label, rec_in_label, rec_out_label = event['tc']
            rec_in = frames(rec_in_label, record_drop) - tc_start
            rec_out = frames(rec_out_label, record_drop) - tc_start
        except ValueError as error:
            raise ValueError(f'Event {event["number"]}: {error}') from None
        if rec_out <= rec_in:
            warnings.append(f'Event {event["number"]} has no record length and was skipped.')
            continue
        if rec_in < 0:
            raise ValueError(f'Event {event["number"]} starts before the sequence start timecode')
        comments = event['comments']
        path = ''
        if comments.get('SOURCE FILE'):
            try:
                path = path_from_url(comments['SOURCE FILE'])
            except ValueError as error:
                warnings.append(f'Event {event["number"]}: {error}')
        name = comments.get('TO CLIP NAME') if event['kind'] != 'C' else None
        name = name or comments.get('FROM CLIP NAME') or comments.get('CLIP NAME') or (base_name(path) if path else event['reel'])
        key = path or name
        if key not in media:
            media[key] = _media_for(key, name, path, event['reel'], kind, rate, probes)
        record = media[key]
        if kind == 'video' and not record['known']:
            record['kind'] = 'video'
        try:
            source_in = frames(src_in_label, event['drop'])
            source_out = frames(src_out_label, event['drop'])
        except ValueError as error:
            raise ValueError(f'Event {event["number"]} source timecode: {error}') from None
        record['uses'].append(min(source_in, source_out))
        span = abs(source_out - source_in)
        record_frames = rec_out - rec_in
        if event['m2'] is not None and event['m2'] != 0:
            # M2: source frames per second at the list's rate, written to 0.001 fps; Splice speeds have 4 decimals
            speed = Fraction(round(float(Fraction(repr(abs(event['m2']))) / rate), 4)).limit_denominator(10000)
        else:
            speed = Fraction(span, record_frames) if span else Fraction(1)
        reverse = event['m2'] is not None and event['m2'] < 0
        clip = {'kind': kind, 'media': key, 'name': name, 'start': Fraction(rec_in) / rate,
                'duration': Fraction(record_frames) / rate, '_source_frame': min(source_in, source_out),
                'speed': float(speed), 'reverse': reverse,
                'track': audio_track if kind == 'audio' else 1, 'embeddedAudio': picture_sound}
        if event['kind'] != 'C' and not keyed:
            clip['transition'] = {'transition': 'wipe' if event['kind'].startswith('W') else 'dissolve',
                                  'transitionDuration': float(Fraction(max(1, event['dur'])) / rate), 'transitionBefore': 0}
        clips.append(clip)
    for record in media.values():
        if record['startFrames'] is None:
            record['startFrames'] = min(record['uses'], default=0)
            warnings.append(f'{record["name"]}: its start timecode is unknown, so its first used frame was taken as the '
                            'start of the file. After relinking, slip the clips if the picture is out of place.')
    out = []
    for clip in clips:
        record = media[clip['media']]
        clip['in'] = float(Fraction(clip.pop('_source_frame') - record['startFrames']) / rate)
        clip['start'], clip['duration'] = float(clip['start']), float(clip['duration'])
        if clip['in'] < 0:
            warnings.append(f'{clip["name"]}: its source timecode is before the file\'s start timecode; set to the first frame.')
            clip['in'] = 0.0
        out.append(clip)
    marker_list = []
    for label, name in markers:
        try:
            marker_list.append({'time': float(Fraction(frames(label, record_drop) - tc_start) / rate), 'name': name})
        except ValueError:
            warnings.append(f'Locator "{name}" has malformed timecode and was skipped.')
    media_list = []
    for record in media.values():
        rate_value = record.pop('rateFraction')
        record['rate'] = f'{rate_value.numerator}/{rate_value.denominator}' if rate_value.denominator != 1 else str(rate_value.numerator)
        if record.get('label'):
            record['timecode'] = record['label']
            record['dropFrame'] = record['drop']
        for extra in ('uses', 'startFrames', 'drop', 'hasAudio', 'known', 'label'):
            record.pop(extra, None)
        media_list.append(record)
    return build_project(title or 'Imported EDL', rate, width, height,
                         media_list, out, [m for m in marker_list if m['time'] >= 0], warnings)


def _media_for(key, name, path, reel, kind, rate, probes):
    """What the file says about itself, when it is on this machine: its rate, its start timecode
    label and that label as a frame count at the list's ``rate`` (``startFrames``)."""
    record = {'key': key, 'name': name, 'path': path, 'kind': kind, 'reel': reel,
              'rateFraction': rate, 'startFrames': None, 'known': False, 'drop': False, 'uses': [], 'duration': None}
    local = Path(path) if path and is_absolute_path(path) and not path.startswith('\\\\') and ':' not in path[:3] else None
    if local is not None and local.is_file() and probes[0] > 0:
        probes[0] -= 1
        try:
            probe = probe_file(local)
        except ValueError:
            return record
        record['kind'] = 'video' if probe.has_video else 'audio'
        record['rateFraction'] = probe.rate or rate
        record['duration'] = float(probe.duration) if probe.duration else None
        record['known'] = True
        if probe.timecode:
            try:
                drop = is_drop_frame(probe.timecode)
                own = timecode_frames(probe.timecode, record['rateFraction'], drop)
                record['startFrames'] = convert_label(own, record['rateFraction'], drop, rate, drop and rate in DROP_FRAME_RATES)
                record['label'], record['drop'] = probe.timecode, drop
            except ValueError:
                record['known'] = False
        elif probe.time_reference is not None and probe.audio_rate and not probe.has_video:
            record['startFrames'] = round(Fraction(probe.time_reference, probe.audio_rate) * rate)
        else:
            record['startFrames'] = 0
            record['label'] = '00:00:00:00'
    return record
