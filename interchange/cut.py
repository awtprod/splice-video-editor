"""Turn a Splice project into a frame-exact cut list that every writer shares.

Splice stores float seconds. Record positions are snapped to the sequence's frame grid, source
positions to the source's own frame grid (the sequence grid for audio-only and still media), so
every writer emits the same frames. Anything a cut list cannot hold (grades, LUTs, pan, nested
sequences, Premiere-native effects) is recorded in :class:`Report` against the clip, so writers
add only what their own format loses.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction

from .media import MAX_PROBES, Source, clean, resolve_source
from .timecode import frames_timecode, seconds_frames, sequence_rate, timecode_frames

MAX_CLIPS = 20_000
MAX_KEYFRAMES = 2_000
TRANSITIONS = ('dissolve', 'wipe', 'fade')
KEYFRAME_KEYS = ('opacity', 'x', 'y', 'scale', 'rotation', 'volume')


class InterchangeError(ValueError):
    pass


@dataclass
class Transition:
    kind: str           # 'dissolve', 'wipe' or 'fade' (dip to black)
    frames: int
    before: int         # frames of the transition before the cut
    power: bool = False  # constant-power audio crossfade


@dataclass
class Event:
    id: str
    kind: str                    # 'video', 'audio' or 'title'
    track: int                   # >0 picture/title tracks, <0 audio tracks
    record_in: int               # sequence frames from the export's first frame
    frames: int
    source: Source | None
    source_in: Fraction = Fraction(0)    # seconds from the media file's first sample
    speed: Fraction = Fraction(1)
    reverse: bool = False
    name: str = ''
    enabled: bool = True
    embedded_audio: bool = False   # a picture clip that also plays its own sound
    transition: Transition | None = None
    volume: float = 100.0
    fade_in: int = 0
    fade_out: int = 0
    keyframes: dict = field(default_factory=dict)  # key -> [(frame from clip start, value)]
    transform: dict = field(default_factory=dict)  # x, y, scale, rotation (Splice units)
    opacity: float = 100.0
    link_group: str | None = None
    text: str = ''
    title_style: dict = field(default_factory=dict)

    @property
    def record_out(self) -> int:
        return self.record_in + self.frames

    @property
    def source_span(self) -> Fraction:
        """Seconds of source the clip consumes."""
        return Fraction(self.frames) / self.rate * self.speed

    rate: Fraction = Fraction(25)


@dataclass
class Marker:
    frame: int
    name: str
    id: str = ''
    duration: int = 0
    color: str = ''


@dataclass
class Report:
    """Per-format list of what could not be represented, each entry naming the clip."""
    general: list = field(default_factory=list)
    by_format: dict = field(default_factory=dict)

    def add(self, fmt: str, message: str):
        entries = self.by_format.setdefault(fmt, [])
        if message not in entries:
            entries.append(message)

    def note(self, message: str):
        if message not in self.general:
            self.general.append(message)


@dataclass
class Cut:
    name: str
    rate: Fraction
    width: int
    height: int
    tc_start: int             # sequence start timecode, frames at rate
    drop: bool
    frames: int               # length of the exported range
    events: list
    sources: dict
    markers: list
    report: Report
    range_start: int = 0      # first exported frame of the Splice timeline
    track_mix: dict = field(default_factory=dict)

    def timecode(self, frame: int) -> str:
        return frames_timecode(self.tc_start + frame, self.rate, self.drop)

    def label(self, event: Event) -> str:
        """How a report names a clip: name, track and record timecode."""
        track = ('A' if event.track < 0 else 'V') + str(abs(event.track))
        return f'{event.name or "Clip"} ({track} at {self.timecode(event.record_in)})'


def _number(value, default, low=-math.inf, high=math.inf):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number)) if math.isfinite(number) else default


def _speed(value) -> Fraction:
    speed = _number(value, 1, .1, 16)
    return Fraction(repr(speed)).limit_denominator(10000)


def build_cut(project: dict, options: dict | None = None) -> Cut:
    options = options or {}
    if not isinstance(project, dict) or not isinstance(project.get('clips'), list) or not isinstance(project.get('media'), list):
        raise InterchangeError('Invalid project')
    if len(project['clips']) > MAX_CLIPS or len(project['media']) > MAX_CLIPS:
        raise InterchangeError(f'Timeline interchange supports up to {MAX_CLIPS} clips and sources')
    try:
        rate = sequence_rate(project.get('fps', 25))
    except ValueError as error:
        raise InterchangeError(str(error)) from None
    width, height = int(_number(project.get('width'), 1920, 16, 16384)), int(_number(project.get('height'), 1080, 16, 16384))
    drop = bool(options.get('dropFrame')) and rate in (Fraction(30000, 1001), Fraction(60000, 1001))
    tc_text = str(options.get('startTimecode') or '01:00:00:00')
    try:
        tc_start = timecode_frames(tc_text, rate, drop)
    except ValueError as error:
        raise InterchangeError(f'Sequence start timecode: {error}') from None
    report = Report()
    budget = [min(MAX_PROBES, max(0, int(_number(options.get('maxProbes'), MAX_PROBES))))]
    sources = {}
    for item in project['media']:
        if isinstance(item, dict) and item.get('id') is not None:
            sources[str(item['id'])] = resolve_source(item, rate, budget)
    total = max((_span(c, rate)[1] for c in project['clips'] if isinstance(c, dict)), default=0)
    first, last = 0, total
    span = options.get('range')
    if isinstance(span, dict):
        first = max(0, seconds_frames(_number(span.get('start'), 0, 0), rate))
        last = min(total, seconds_frames(_number(span.get('end'), total / rate, 0), rate))
        if last <= first:
            raise InterchangeError('The export range is empty')
    cut = Cut(name=clean(project.get('name') or 'Untitled', 200) or 'Untitled', rate=rate, width=width, height=height,
              tc_start=tc_start, drop=drop, frames=last - first, events=[], sources=sources,
              markers=[], report=report, range_start=first, track_mix=dict(project.get('trackMix') or {}))
    try:
        frames_timecode(tc_start + cut.frames, rate, drop)
    except ValueError:
        raise InterchangeError('The sequence runs past 24 hours of timecode; choose an earlier start timecode') from None
    raw = [c for c in project['clips'] if isinstance(c, dict)]
    for clip in raw:
        event = _event(cut, clip)
        if event is not None:
            cut.events.append(event)
    _timecode_policy(cut, options.get('unknownTimecode'))
    _separate_overlaps(cut)
    cut.events.sort(key=lambda e: (e.kind == 'audio', abs(e.track), e.record_in))
    for marker in project.get('markers') or []:
        if not isinstance(marker, dict):
            continue
        frame = seconds_frames(_number(marker.get('time'), 0, 0), rate)
        if first <= frame < max(last, first + 1):
            cut.markers.append(Marker(frame - first, clean(marker.get('name') or 'Marker', 500), clean(marker.get('id'), 64),
                                      seconds_frames(_number(marker.get('duration'), 0, 0), rate), str(marker.get('color') or '')))
        else:
            report.note(f'Marker "{marker.get("name")}" lies outside the export range and was not exported.')
    for caption in project.get('captions') or []:
        if isinstance(caption, dict):
            report.note(f'Caption "{str(caption.get("text", ""))[:40]}" at {cut.timecode(max(0, seconds_frames(_number(caption.get("start"), 0, 0), rate) - first))}: '
                        'captions are not part of timeline interchange; export them as subtitles.')
    for key, mix in cut.track_mix.items():
        if isinstance(mix, dict) and (mix.get('gain', 100) != 100 or mix.get('pan', 0) or mix.get('mute') or mix.get('solo')):
            report.note(f'Audio track A{abs(int(key))} mixer settings (gain, pan, mute or solo) are not carried; '
                        'interchange formats have no track mixer.')
    return cut


def _event(cut: Cut, clip: dict) -> Event | None:
    rate, report = cut.rate, cut.report
    kind = clip.get('type') if clip.get('type') in ('video', 'audio', 'title') else None
    source = cut.sources.get(str(clip.get('mediaId'))) if clip.get('mediaId') is not None else None
    name = clean(clip.get('title')) if kind == 'title' else (source.name if source else clean(clip.get('name') or 'Clip'))
    start, end = _span(clip, rate)
    frames = end - start
    track = int(_number(clip.get('track'), 1, -64, 64)) or 1
    label = f'{name or "Clip"} ({"A" if track < 0 else "V"}{abs(track)} at {frames_timecode(cut.tc_start + max(0, start - cut.range_start), rate, cut.drop) if start >= cut.range_start else "before the range"})'
    if kind is None or frames <= 0:
        report.note(f'{label}: not exported because it has no supported type or length.')
        return None
    if start + frames <= cut.range_start or start >= cut.range_start + cut.frames:
        return None
    if kind != 'title' and source is None:
        report.note(f'{label}: not exported because its source is missing from the project.')
        return None
    if source is not None and (source.nested or source.generator):
        what = 'a nested sequence' if source.nested else 'a Premiere-native generator'
        report.note(f'{label}: not exported because it is {what}; export that sequence or replace the item.')
        return None
    event = Event(id=str(clip.get('id')), kind=kind, track=track, record_in=start, frames=frames, source=source,
                  name=name, rate=rate)
    event.enabled = clip.get('enabled') is not False and clip.get('disabled') is not True
    event.speed = _speed(clip.get('speed', 1))
    event.reverse = bool(clip.get('reverse'))
    event.link_group = clip.get('linkGroup') if isinstance(clip.get('linkGroup'), str) else None
    if source is not None:
        grid = source.rate if source.rate and not source.still else rate
        exact = Fraction(repr(_number(clip.get('in'), 0, 0))) * grid
        frame = math.floor(exact + Fraction(1, 2))
        event.source_in = Fraction(0) if source.still else Fraction(frame) / grid
        if not source.still and abs(exact - frame) > Fraction(1, 1000):
            report.note(f'{label}: its source in point ({float(exact / grid):.4f} s) falls between source frames; '
                        f'exported at frame {frame} ({float(frame / grid):.4f} s).')
        event.embedded_audio = kind == 'video' and clip.get('embeddedAudio') is not False and source.has_audio and not source.still
    event.volume = _number(clip.get('volume'), 100, 0, 200)
    event.fade_in = seconds_frames(_number(clip.get('fadeIn'), 0, 0), rate)
    event.fade_out = seconds_frames(_number(clip.get('fadeOut'), 0, 0), rate)
    transform = clip.get('transform') if isinstance(clip.get('transform'), dict) else {}
    event.transform = {key: _number(transform.get(key), default, low, high) for key, default, low, high in (
        ('x', 0, -100, 100), ('y', 0, -100, 100), ('scale', 100, 10, 400), ('rotation', 0, -180, 180))}
    event.opacity = _number(clip.get('opacity'), 100, 0, 100)
    if kind == 'title':
        event.text = clean(clip.get('title'), 2000)
        event.title_style = clip.get('titleStyle') if isinstance(clip.get('titleStyle'), dict) else {}
    keys = clip.get('keyframes') if isinstance(clip.get('keyframes'), dict) else {}
    for key, points in keys.items():
        if key not in KEYFRAME_KEYS or not isinstance(points, list):
            report.note(f'{label}: keyframes for "{key}" are not carried by timeline interchange.')
            continue
        points_kept = sorted((seconds_frames(_number(p.get('time'), 0, 0), rate), _number(p.get('value'), 0))
                       for p in points[:MAX_KEYFRAMES] if isinstance(p, dict))
        if points_kept:
            event.keyframes[key] = points_kept
    style = clip.get('transition')
    if style in TRANSITIONS and kind != 'title':
        requested = min(max(_number(clip.get('transitionDuration'), .5, 0) or .5, 1 / float(rate)), 7200)
        before = min(_number(clip.get('transitionBefore'), 0, 0), requested)
        length = min(seconds_frames(requested, rate), frames + seconds_frames(before, rate))
        event.transition = Transition(style, max(1, length), seconds_frames(before, rate),
                                      clip.get('transitionMatchName') == 'Constant Power')
        if style == 'fade' and kind == 'audio':
            event.transition.kind = 'dissolve'
    elif style in TRANSITIONS:
        report.note(f'{label}: title transitions are not carried; the title cuts in and out.')
    elif style == 'native':
        report.note(f'{label}: its Premiere-native transition ({clip.get("transitionMatchName") or "unknown"}) is not carried; the cut is exported.')
    elif style not in (None, '', 'none') and kind != 'title':
        report.note(f'{label}: transition "{style}" is not carried; the cut is exported.')
    color = clip.get('color') if isinstance(clip.get('color'), dict) else {}
    graded = any(color.get(k, 100) != 100 for k in ('brightness', 'contrast', 'saturation')) or \
        any(color.get(k) for k in ('exposure', 'temperature', 'tint', 'lift', 'gamma', 'gain', 'curves'))
    if graded or color.get('lut'):
        report.note(f'{label}: its Splice grade{" and LUT" if color.get("lut") else ""} is not carried; grade from the source media.')
    if clip.get('pan') not in (None, 0):
        report.note(f'{label}: clip pan is not carried by timeline interchange.')
    if clip.get('nativeTimeRemap'):
        report.note(f'{label}: its Premiere time-remapping curve is exported as a constant {float(event.speed):g}x speed.')
    if clip.get('audioStream') is not None or (source is not None and len(source.audio_streams) > 1 and kind != 'title'):
        report.note(f'{label}: the source has {len(source.audio_streams) if source else "several"} audio streams; '
                    'receiving apps choose their own channel mapping.')
    if not event.enabled:
        report.note(f'{label}: disabled in Splice.')
    _trim_to_range(cut, event)
    return event


def _trim_to_range(cut: Cut, event: Event):
    first, last = cut.range_start, cut.range_start + cut.frames
    head = max(0, first - event.record_in)
    tail = max(0, event.record_out - last)
    if event.transition is not None:
        window = event.record_in - event.transition.before
        if window < first or head:
            cut.report.note(f'{event.name}: its transition starts before the export range (or the timeline) and was replaced by a cut.')
            event.transition = None
    if head or tail:
        step = Fraction(1) / cut.rate * event.speed
        if event.reverse:
            event.source_in += tail * step
        else:
            event.source_in += head * step
        if event.source is not None and not event.source.still:
            grid = event.source.rate or cut.rate  # a retimed clip can be cut between source frames
            event.source_in = Fraction(math.floor(event.source_in * grid + Fraction(1, 2))) / grid
        event.frames -= head + tail
        event.fade_in = max(0, event.fade_in - head)
        event.fade_out = max(0, event.fade_out - tail)
        event.keyframes = {k: _window(points, head, head + event.frames) for k, points in event.keyframes.items()}
    event.record_in = max(event.record_in, first) - first


def _window(points, start, end):
    def value(at):
        if at <= points[0][0]:
            return points[0][1]
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if at <= t1:
                return v0 + (v1 - v0) * (at - t0) / (t1 - t0) if t1 != t0 else v1
        return points[-1][1]
    inner = [(t - start, v) for t, v in points if start < t < end]
    return [(0, value(start)), *inner, (end - start, value(end))]


def _span(clip: dict, rate: Fraction):
    """First and end frame of a clip; both edges are rounded, so butted clips stay butted."""
    start = _number(clip.get('start'), 0, 0)
    return seconds_frames(start, rate), seconds_frames(start + _number(clip.get('duration'), 0, 0), rate)


def _timecode_policy(cut: Cut, policy):
    """Resolve links an FCPXML clip to its file only when the asset starts at the file's own
    timecode, so an unreadable start timecode is the user's decision, never a silent zero:
    ``policy`` ``'zero'`` writes 00:00:00:00 and lists each affected clip; anything else refuses."""
    used = {id(e.source): e.source for e in cut.events if e.source is not None and not e.source.still}
    unknown = sorted(s.name for s in used.values() if s.timecode_state == 'unknown')
    if unknown and policy != 'zero':
        shown = ', '.join(unknown[:8]) + (f' and {len(unknown) - 8} more' if len(unknown) > 8 else '')
        raise InterchangeError(f'Start timecode could not be read for {shown}. Relink the original files, or choose '
                               'to write 00:00:00:00 for unreadable sources (receiving apps may then show them offline).')
    for source in used.values():
        for note in source.notes:
            cut.report.note(f'Source {source.name}: {note}.')
        if source.timecode_state == 'none':
            cut.report.note(f'Source {source.name} has no embedded start timecode; written as 00:00:00:00, '
                            'which is what Resolve and Premiere assume for such files.')
    for event in cut.events:
        if event.source is not None and event.source.timecode_state == 'unknown' and not event.source.still:
            cut.report.note(f'{cut.label(event)}: source start timecode unknown, written as 00:00:00:00 by choice; '
                            'the receiving app may import this clip offline.')


def _separate_overlaps(cut: Cut):
    """Interchange formats cannot hold two clips at once on one track. Splice never stores that,
    but a hand-edited project can; such a clip moves to a new track and is reported."""
    busy = {}
    for event in sorted(cut.events, key=lambda e: (e.record_in, e.id)):
        side = -1 if event.kind == 'audio' else 1
        track = event.track
        while any(event.record_in < end and start < event.record_out for start, end in busy.get((side, track), ())):
            track += side
        if track != event.track:
            cut.report.note(f'{cut.label(event)} overlapped another clip on its track and was moved to '
                            f'{"A" if side < 0 else "V"}{abs(track)}.')
            event.track = track
        busy.setdefault((side, track), []).append((event.record_in, event.record_out))
