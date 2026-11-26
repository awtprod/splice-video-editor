"""FCPXML 1.10 export and FCPXML 1.9-1.11 import.

Layout follows what DaVinci Resolve itself writes and what Resolve 21.1 was shown to import from
the FCPXML exporter in awtprod/ai-agent-resolve (``origin/dev`` aff6573,
``src/aar/plan/export/fcpxml.py``): the picture on V1 is the primary storyline (``spine``) with
``gap`` elements for holes, so the storyline runs from the first to the last frame; every other
track is anchored to it at a fixed lane (V2 is lane 1, A1 is lane -1). A track that carries
transitions becomes a secondary storyline (``spine lane="N"``), the only place FCPXML allows a
transition off V1. Clips abut at the cut; a ``transition`` overlaps the media beyond each clip
(handles), and its ``offset`` is the cut minus the frames before it, which is exactly Splice's
``transitionBefore``.

Time: spine offsets count from the sequence's ``tcStart``. Every clip's local time starts at its
``start``, which is its asset's start (the media file's embedded start timecode) plus the source
in point; Resolve links a clip to its file only when the asset start equals that timecode.
Retimed clips keep the same linear local time and add a two-point ``timeMap``. A secondary
storyline has no ``start``, so its children count from 0.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from decimal import Decimal
from fractions import Fraction

from .cut import Cut, Event
from .media import file_url, path_from_url, base_name
from .timecode import fcpxml_time, frames_timecode, parse_fcpxml_time, rate_text, snap_rate
from . import xmlsafe
from .budget import Budget

VERSION = '1.10'
READ_VERSIONS = ('1.9', '1.10', '1.11')
HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
CROSS_DISSOLVE = ('Cross Dissolve', 'FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265')
AUDIO_CROSSFADE = ('Audio Crossfade', 'FFAudioTransition')
BASIC_TITLE = ('Basic Title', '.../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti')
SILENT_DB = -96.0
REEL_KEY = 'com.apple.proapps.studio.reel'
SPLICE_KEY = 'com.splice.'
_SIZES = {(1280, 720): '720p', (1920, 1080): '1080p', (3840, 2160): '2160p'}
_SUFFIX = {Fraction(24000, 1001): '2398', Fraction(24): '24', Fraction(25): '25', Fraction(30000, 1001): '2997',
           Fraction(30): '30', Fraction(50): '50', Fraction(60000, 1001): '5994', Fraction(60): '60'}
_MIXED = frozenset({'text', 'text-style', 'note'})
FORMAT = 'FCPXML'


def number(value: float) -> str:
    """Shortest exact decimal text, never exponent form."""
    text = format(Decimal(repr(float(value))), 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def db(percent: float) -> str:
    if percent <= 0:
        return f'{number(SILENT_DB)}dB'
    return f'{number(max(SILENT_DB, round(20 * math.log10(percent / 100), 6)))}dB'


def percent(db_text: str) -> float:
    value = float(str(db_text).strip().removesuffix('dB').strip())
    if not math.isfinite(value):
        raise ValueError('volume is not finite')
    return 0.0 if value <= SILENT_DB else min(200.0, round(100 * 10 ** (value / 20), 3))


# -- export -------------------------------------------------------------------------------------

class _Writer:
    def __init__(self, cut: Cut):
        self.cut = cut
        self.rate = cut.rate
        self.ids = {}
        self.assets = {}
        self.formats = {}
        self.styles = 0
        self.report = cut.report
        self.merged = {}    # video event id -> audio event merged into it as a J/L cut
        self.absorbed = set()

    def t(self, seconds: Fraction) -> str:
        return fcpxml_time(seconds, self.rate)

    def frames(self, count: int) -> Fraction:
        return Fraction(count) / self.rate

    def seq(self, frame: int) -> str:
        return self.t(self.frames(self.cut.tc_start + frame))

    def note(self, event: Event, message: str):
        self.report.add(FORMAT, f'{self.cut.label(event)}: {message}')

    # resources ---------------------------------------------------------------------------------
    def _format(self, resources, rate, width, height, still=False):
        key = (rate, width, height, still)
        if key in self.formats:
            return self.formats[key]
        rid = f'r{len(resources) + 1}'
        element = ET.SubElement(resources, 'format', id=rid)
        if still:
            element.set('name', 'FFVideoFormatRateUndefined')
        elif (width, height) in _SIZES and rate in _SUFFIX:
            element.set('name', f'FFVideoFormat{_SIZES[(width, height)]}{_SUFFIX[rate]}')
        if rate is not None and not still:
            element.set('frameDuration', fcpxml_time(1 / rate, rate))
        if width and height:
            element.set('width', str(width))
            element.set('height', str(height))
        element.set('colorSpace', '1-1-1 (Rec. 709)')
        self.formats[key] = rid
        return rid

    def _resources(self, root):
        resources = ET.SubElement(root, 'resources')
        cut = self.cut
        self.ids['format'] = self._format(resources, self.rate, cut.width, cut.height)
        events = cut.events
        uses = {
            'dissolve': any(e.transition and e.kind != 'audio' for e in events),
            'crossfade': any(e.transition and (e.kind == 'audio' or e.embedded_audio) for e in events),
            'title': any(e.kind == 'title' for e in events),
        }
        for key, (name, uid) in (('dissolve', CROSS_DISSOLVE), ('crossfade', AUDIO_CROSSFADE), ('title', BASIC_TITLE)):
            if uses[key]:
                rid = f'r{len(resources) + 1}'
                ET.SubElement(resources, 'effect', id=rid, name=name, uid=uid)
                self.ids[key] = rid
        for source in {id(e.source): e.source for e in events if e.source is not None}.values():
            attrs = {}
            if source.has_video and source.kind == 'video':
                attrs['format'] = self._format(resources, source.rate or self.rate, source.width, source.height, source.still)
            rid = f'r{len(resources) + 1}'
            grid = source.rate or self.rate
            duration = Fraction(0) if source.still else Fraction(math.floor(source.duration * grid)) / grid
            element = ET.SubElement(resources, 'asset', id=rid, name=source.name, start=fcpxml_time(source.start, source.tc_rate or grid),
                                    duration=fcpxml_time(duration, grid))
            if source.has_video and source.kind == 'video':
                element.set('hasVideo', '1')
                element.set('format', attrs['format'])
            if source.has_audio:
                element.set('hasAudio', '1')
                element.set('audioSources', str(max(1, len(source.audio_streams))))
                element.set('audioChannels', str(max(1, source.channels)))
                element.set('audioRate', str(source.audio_rate or 48000))
            if source.path:
                src = file_url(source.path)
            else:
                src = file_url_relative(source.name)
                self.report.add(FORMAT, f'Source {source.name}: its original path is unknown; the file references '
                                        f'"{source.name}" next to the timeline file. Relink in the receiving app.')
            ET.SubElement(element, 'media-rep', kind='original-media', src=src)
            if source.reel:
                metadata = ET.SubElement(element, 'metadata')
                ET.SubElement(metadata, 'md', key=REEL_KEY, value=source.reel)
            self.assets[id(source)] = rid

    # story elements ------------------------------------------------------------------------
    def _clip(self, parent, event: Event, offset: str, lane: int | None):
        """One ``asset-clip`` (or ``title``); returns (element, local start seconds)."""
        if event.kind == 'title':
            return self._title(parent, event, offset, lane)
        source = event.source
        start = source.start + event.source_in
        attrs = {'ref': self.assets[id(source)]}
        if lane:
            attrs['lane'] = str(lane)
        attrs.update(offset=offset, name=event.name, start=fcpxml_time(start, source.rate or self.rate),
                     duration=self.t(self.frames(event.frames)))
        if not event.enabled:
            attrs['enabled'] = '0'
        if source.has_video and source.kind == 'video' and source.timecode and not source.still:
            # the DTD puts tcFormat on the clip, not the asset; Final Cut Pro writes it there too
            attrs['tcFormat'] = 'DF' if source.drop else 'NDF'
        partner = self.merged.get(event.id)
        sound = event
        if event.kind == 'audio' and source.has_video:
            attrs['srcEnable'] = 'audio'
            self.note(event, 'written as an audio-only use of an A/V file (srcEnable="audio"); Resolve ignores srcEnable '
                             'and may also place the picture.')
        elif event.kind == 'video' and partner is not None:
            sound = partner
            audio_start = start + self.frames(partner.record_in - event.record_in)
            if (partner.record_in, partner.frames) != (event.record_in, event.frames):
                attrs['audioStart'] = fcpxml_time(audio_start, self.rate) if audio_start >= 0 else '0s'
                attrs['audioDuration'] = self.t(self.frames(partner.frames))
        elif event.kind == 'video' and source.has_audio and not event.embedded_audio:
            attrs['srcEnable'] = 'video'
            self.note(event, 'picture only (srcEnable="video"); Resolve ignores srcEnable and may import its sound too.')
        element = ET.SubElement(parent, 'asset-clip', attrs)
        if event.speed != 1 or event.reverse:
            span = event.source_span
            time_map = ET.SubElement(element, 'timeMap')
            end = start + self.frames(event.frames)
            first, last = (start + span, start) if event.reverse else (start, start + span)
            ET.SubElement(time_map, 'timept', time=self.t_any(start), value=self.t_any(first), interp='linear')
            ET.SubElement(time_map, 'timept', time=self.t_any(end), value=self.t_any(last), interp='linear')
            if event.reverse:
                self.note(event, 'reverse playback is written as a descending timeMap; Resolve imports it as a reversed speed change.')
        if event.kind != 'audio':
            self._picture(element, event, start)
        if event.kind == 'audio' or event.embedded_audio or partner is not None:
            origin = start + self.frames(sound.record_in - event.record_in)
            self._volume(element, sound, origin)
        md = []
        if event.link_group and partner is None:
            md.append(('linkGroup', event.link_group))
        if partner is not None:
            md += [('linkGroup', event.link_group or ''), ('audioTrack', str(partner.track))]
        if md:
            self._splice_metadata(element, md)
        return element, start

    def t_any(self, seconds: Fraction) -> str:
        return fcpxml_time(max(Fraction(0), seconds), self.rate)

    def _splice_metadata(self, element, pairs):
        metadata = ET.SubElement(element, 'metadata')
        for key, value in pairs:
            ET.SubElement(metadata, 'md', key=SPLICE_KEY + key, value=value)

    def _keyed(self, adjust, name, points, origin, render):
        param = ET.SubElement(adjust, 'param', name=name)
        animation = ET.SubElement(param, 'keyframeAnimation')
        for frame, value in points:
            ET.SubElement(animation, 'keyframe', time=self.t_any(origin + self.frames(frame)), value=render(value))

    def _picture(self, element, event: Event, start: Fraction):
        tf, keys = event.transform, event.keyframes
        moved = any(abs(tf.get(k, d) - d) > 1e-9 for k, d in (('x', 0), ('y', 0), ('scale', 100), ('rotation', 0)))
        animated = [k for k in ('x', 'y', 'scale', 'rotation') if keys.get(k)]
        aspect = self.cut.width / self.cut.height

        def position(x, y):
            return f'{number(round(x * aspect, 6))} {number(round(-y, 6))}'
        if moved or animated:
            adjust = ET.SubElement(element, 'adjust-transform', position=position(tf['x'], tf['y']),
                                   scale=f'{number(tf["scale"] / 100)} {number(tf["scale"] / 100)}',
                                   rotation=number(-tf['rotation']))
            if keys.get('x') or keys.get('y'):
                times = sorted({f for k in ('x', 'y') for f, _ in keys.get(k, ())})
                points = [(f, (_at(keys.get('x'), f, tf['x']), _at(keys.get('y'), f, tf['y']))) for f in times]
                self._keyed(adjust, 'position', points, start, lambda v: position(*v))
            if keys.get('scale'):
                self._keyed(adjust, 'scale', keys['scale'], start, lambda v: f'{number(v / 100)} {number(v / 100)}')
            if keys.get('rotation'):
                self._keyed(adjust, 'rotation', keys['rotation'], start, lambda v: number(-v))
        if event.opacity != 100 or keys.get('opacity'):
            blend = ET.SubElement(element, 'adjust-blend', amount=number(event.opacity / 100))
            if keys.get('opacity'):
                self._keyed(blend, 'amount', keys['opacity'], start, lambda v: number(max(0, min(100, v)) / 100))

    def _volume(self, element, event: Event, origin: Fraction):
        keys = event.keyframes.get('volume')
        if event.volume == 100 and not keys and not event.fade_in and not event.fade_out:
            return
        adjust = ET.SubElement(element, 'adjust-volume')
        if event.volume != 100:
            adjust.set('amount', db(event.volume))
        if keys or event.fade_in or event.fade_out:
            param = ET.SubElement(adjust, 'param', name='amount')
            if event.fade_in:  # DTD order: fadeIn?, fadeOut?, keyframeAnimation?
                ET.SubElement(param, 'fadeIn', type='linear', duration=self.t(self.frames(event.fade_in)))
            if event.fade_out:
                ET.SubElement(param, 'fadeOut', type='linear', duration=self.t(self.frames(event.fade_out)))
            if keys:
                animation = ET.SubElement(param, 'keyframeAnimation')
                for frame, value in keys:
                    ET.SubElement(animation, 'keyframe', time=self.t_any(origin + self.frames(frame)), value=db(value))
            if event.fade_in or event.fade_out:
                self.note(event, 'audio fades are fadeIn/fadeOut on the clip volume; Resolve imports them as volume keyframes.')

    def _title(self, parent, event: Event, offset: str, lane: int | None):
        self.styles += 1
        style_id = f'ts{self.styles}'
        start = self.frames(self.cut.tc_start + event.record_in)
        attrs = {'ref': self.ids['title']}
        if lane:
            attrs['lane'] = str(lane)
        attrs.update(offset=offset, name=(event.text.splitlines() or ['Title'])[0][:120] or 'Title',
                     start=self.t(start), duration=self.t(self.frames(event.frames)))
        if not event.enabled:
            attrs['enabled'] = '0'
        element = ET.SubElement(parent, 'title', attrs)
        style = event.title_style
        x, y = _number(style.get('x'), 50), _number(style.get('y'), 78)
        aspect = self.cut.width / self.cut.height
        ET.SubElement(element, 'adjust-transform', position=f'{number(round((x - 50) * aspect, 6))} {number(round(50 - y, 6))}')
        if event.opacity != 100 or event.keyframes.get('opacity'):
            self._picture(element, event, start)
        text = ET.SubElement(element, 'text')
        ET.SubElement(text, 'text-style', ref=style_id).text = event.text
        definition = ET.SubElement(element, 'text-style-def', id=style_id)
        color = str(style.get('color') or '#ffffff')
        rgb = color[1:] if len(color) == 7 and color.startswith('#') else 'ffffff'
        try:
            red, green, blue = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
        except ValueError:
            red = green = blue = 1.0
        ET.SubElement(definition, 'text-style', font=str(style.get('fontFamily') or 'Helvetica')[:80],
                      fontSize=number(round(_number(style.get('fontSize'), 8) * 10.8, 2)),
                      fontColor=f'{number(round(red, 4))} {number(round(green, 4))} {number(round(blue, 4))} 1',
                      alignment=style.get('align') if style.get('align') in ('left', 'center', 'right') else 'center')
        self.note(event, 'written as a Basic Title; font, size and position are approximate and the drop shadow is not carried.')
        return element, start

    def _transition(self, parent, event: Event, offset_frame_to_text):
        tr = event.transition
        if tr.kind != 'dissolve':
            self.note(event, f'{"dip to black" if tr.kind == "fade" else "wipe"} written as a cross dissolve.')
        video = event.kind != 'audio'
        element = ET.SubElement(parent, 'transition', name=CROSS_DISSOLVE[0] if video else AUDIO_CROSSFADE[0],
                                offset=offset_frame_to_text(event.record_in - tr.before), duration=self.t(self.frames(tr.frames)))
        if video:
            ET.SubElement(element, 'filter-video', ref=self.ids['dissolve'], name=CROSS_DISSOLVE[0])
        if not video or event.embedded_audio:
            ET.SubElement(element, 'filter-audio', ref=self.ids['crossfade'], name=AUDIO_CROSSFADE[0])

    # assembly ------------------------------------------------------------------------------
    def _merge_links(self):
        """A linked picture + sound pair from one file becomes one A/V clip, with ``audioStart`` and
        ``audioDuration`` for a J/L cut (the DTD's split-edit form), so Resolve, which ignores
        ``srcEnable``, does not play the sound twice."""
        groups = {}
        for event in self.cut.events:
            if event.link_group:
                groups.setdefault(event.link_group, []).append(event)
        for members in groups.values():
            if len(members) != 2:
                continue
            video = next((e for e in members if e.kind == 'video'), None)
            audio = next((e for e in members if e.kind == 'audio'), None)
            if video is None or audio is None or video.source is not audio.source or video.embedded_audio:
                continue
            if (video.speed, video.reverse) != (audio.speed, audio.reverse) or audio.transition or not video.enabled or not audio.enabled:
                continue
            drift = (audio.source_in - video.source_in) - self.frames(audio.record_in - video.record_in) * video.speed
            if video.reverse or abs(drift) > self.frames(1) / 2:
                continue
            self.merged[video.id] = audio
            self.absorbed.add(audio.id)

    def document(self) -> ET.Element:
        cut = self.cut
        if cut.frames <= 0:
            raise ValueError('The timeline is empty')
        self._merge_links()
        root = ET.Element('fcpxml', version=VERSION)
        self._resources(root)
        library = ET.SubElement(root, 'library')
        event_node = ET.SubElement(library, 'event', name=cut.name)
        project = ET.SubElement(event_node, 'project', name=cut.name)
        sequence = ET.SubElement(project, 'sequence', format=self.ids['format'], duration=self.t(self.frames(cut.frames)),
                                 tcStart=self.seq(0), tcFormat='DF' if cut.drop else 'NDF', audioLayout='stereo', audioRate='48k')
        spine = ET.SubElement(sequence, 'spine')
        items = self._spine(spine)
        self._anchor(items)
        self._markers(items)
        if cut.height > cut.width:
            self.report.add(FORMAT, 'Vertical timeline: Resolve may switch the project resolution to match on import; check it.')
        self.report.add(FORMAT, 'Premiere Pro does not import FCPXML 1.x; give Premiere the FCP 7 XML (.xml), the OTIO file or the EDLs.')
        used = sorted({e.track for e in cut.events if e.kind != 'audio'})
        if used and used != list(range(1, len(used) + 1)):
            moves = ', '.join(f'V{track} -> V{index}' for index, track in enumerate(used, 1) if index != track)
            self.report.add(FORMAT, f'DaVinci Resolve drops empty video tracks when it imports FCPXML, so tracks move down: {moves}.')
        titles = [e for e in cut.events if e.kind == 'title']
        if titles:
            self.report.add(FORMAT, 'DaVinci Resolve imports each title as a Text generator named "Text"; its text is carried, '
                                    'the clip name is not: ' + ', '.join(f'"{(e.text.splitlines() or ["Title"])[0][:40]}"' for e in titles[:20]) + '.')
        for video_id, audio in self.merged.items():
            self.report.add(FORMAT, f'{self.cut.label(audio)}: linked to its picture, so it is written inside that A/V clip '
                                    '(a J/L cut via audioStart); the receiving app shows it with the picture, not on its own track.')
        if cut.markers:
            self.report.add(FORMAT, 'Markers are written on the clips and gaps under them; Final Cut Pro shows them, '
                                    'Resolve imports few or none of them from FCPXML (the EDL carries them as locators).')
        return root

    def _spine(self, spine):
        """V1 (picture and titles on track 1) as the primary storyline, with gaps."""
        items, cursor = [], 0
        primary = [e for e in self.cut.events if e.kind != 'audio' and e.track == 1]
        for event in primary:
            if event.record_in > cursor:
                items.append(self._gap(spine, cursor, event.record_in))
            if event.transition is not None:
                self._transition(spine, event, self.seq)
            element, start = self._clip(spine, event, self.seq(event.record_in), None)
            items.append((event.record_in, event.record_out, start, element))
            cursor = event.record_out
        if cursor < self.cut.frames:
            items.append(self._gap(spine, cursor, self.cut.frames))
        return items

    def _gap(self, parent, first, last, lane=None, offset=None):
        start = self.frames(self.cut.tc_start + first)
        attrs = {}
        if lane:
            attrs['lane'] = str(lane)
        attrs.update(name='Gap', offset=offset or self.seq(first), start=self.t(start), duration=self.t(self.frames(last - first)))
        element = ET.SubElement(parent, 'gap', attrs)
        return (first, last, start, element)

    @staticmethod
    def _parent(items, frame):
        chosen = items[0]
        for item in items:
            if item[0] <= frame:
                chosen = item
        return chosen

    def _local(self, parent, frame):
        return self.t_any(parent[2] + self.frames(frame - parent[0]))

    def _anchor(self, items):
        tracks = {}
        for event in self.cut.events:
            if event.id in self.absorbed or (event.kind != 'audio' and event.track == 1):
                continue
            tracks.setdefault((event.kind == 'audio', event.track), []).append(event)
        for (_audio, track), events in sorted(tracks.items(), key=lambda kv: (kv[0][0], abs(kv[0][1]))):
            lane = track - 1 if track > 0 else track
            if any(e.transition for e in events):
                self._storyline(items, events, lane)
                continue
            for event in events:
                parent = self._parent(items, event.record_in)
                self._clip(parent[3], event, self._local(parent, event.record_in), lane)

    def _storyline(self, items, events, lane):
        first = events[0].record_in - (events[0].transition.before if events[0].transition else 0)
        parent = self._parent(items, max(0, first))
        story = ET.SubElement(parent[3], 'spine', lane=str(lane), offset=self._local(parent, max(0, first)))
        base = max(0, first)

        def local(frame):
            return self.t_any(self.frames(frame - base))
        cursor = base
        for event in events:
            if event.record_in > cursor:
                ET.SubElement(story, 'gap', name='Gap', offset=local(cursor), start='0s',
                              duration=self.t(self.frames(event.record_in - cursor)))
            if event.transition is not None:
                self._transition(story, event, local)
            self._clip(story, event, local(event.record_in), None)
            cursor = event.record_out

    def _markers(self, items):
        for marker in self.cut.markers:
            parent = self._parent(items, marker.frame)
            ET.SubElement(parent[3], 'marker', start=self._local(parent, marker.frame),
                          duration=self.t(self.frames(max(1, marker.duration))), value=marker.name)


def _number(value, default):
    try:
        number_ = float(value)
    except (TypeError, ValueError):
        return default
    return number_ if math.isfinite(number_) else default


def _at(points, frame, default):
    if not points:
        return default
    if frame <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if frame <= t1:
            return v0 + (v1 - v0) * (frame - t0) / (t1 - t0) if t1 != t0 else v1
    return points[-1][1]


def file_url_relative(name: str) -> str:
    from urllib.parse import quote
    return quote(base_name(name), safe='')


_STORY_TAGS = ('audio', 'video', 'clip', 'title', 'caption', 'mc-clip', 'ref-clip', 'sync-clip', 'asset-clip', 'audition', 'spine')
_ADJUST = ('adjust-crop', 'adjust-corners', 'adjust-conform', 'adjust-transform', 'adjust-blend', 'adjust-stabilization',
           'adjust-volume', 'adjust-panner')


def _ranks(*groups):
    return {tag: rank for rank, group in enumerate(groups) for tag in (group if isinstance(group, tuple) else (group,))}


#: Child order per parent, from the FCPXML 1.10 DTD content models of the elements written here.
_ORDER = {
    'asset-clip': _ranks('note', 'conform-rate', 'timeMap', *_ADJUST, _STORY_TAGS, 'marker',
                         'audio-channel-source', 'filter-video', 'filter-audio', 'metadata'),
    'gap': _ranks('note', _STORY_TAGS, 'marker', 'metadata'),
    'title': _ranks('param', 'text', 'text-style-def', 'note', *_ADJUST[:6], _STORY_TAGS, 'marker', 'filter-video', 'metadata'),
}


def _reorder(root):
    """Children in the DTD order of their parent (a stable sort, so same-rank siblings keep their order)."""
    for element in root.iter():
        order = _ORDER.get(element.tag)
        if order:
            element[:] = sorted(element, key=lambda child: order.get(child.tag, 1000))


def _indent(element, level=0):
    if element.tag in _MIXED or len(element) == 0:
        return
    pad = '\n' + '  ' * (level + 1)
    element.text = pad
    for child in element:
        _indent(child, level + 1)
        child.tail = pad
    element[-1].tail = '\n' + '  ' * level


def write(cut: Cut) -> str:
    root = _Writer(cut).document()
    _reorder(root)
    _indent(root)
    return HEADER + ET.tostring(root, encoding='unicode') + '\n'


# -- import -------------------------------------------------------------------------------------

STORY = frozenset({'asset-clip', 'clip', 'video', 'audio', 'title', 'gap', 'ref-clip', 'sync-clip', 'mc-clip',
                   'spine', 'live-drawing', 'caption'})
CONTAINERS = frozenset({'clip', 'sync-clip', 'ref-clip'})
MAX_NESTING = 8
INFINITE = Fraction(10 ** 9)


class _Reader:
    def __init__(self, root, budget=None):
        self.budget = budget or Budget()
        if root.tag == 'xmeml':
            raise ValueError('This is Final Cut Pro 7 XML (xmeml); open it as an .xml timeline so the FCP 7 XML reader reads it')
        if root.tag != 'fcpxml':
            raise ValueError('This is not an FCPXML document')
        self.warnings = []
        version = root.get('version', '')
        if version not in READ_VERSIONS:
            self.warnings.append(f'FCPXML version {version or "unknown"} was read with the 1.10 rules; check the result.')
        self.resources = {}
        for element in root.iterfind('resources/*'):
            rid = element.get('id')
            if not rid:
                continue
            if rid in self.resources:
                raise ValueError(f'FCPXML resource id {rid!r} is defined twice')
            self.resources[rid] = element
        self._root = root
        # Drop-frame is a property of the clip in FCPXML (asset-clip@tcFormat); the DTD has no tcFormat on
        # an asset. The first clip that names it decides for the asset's timecode label.
        self.tc_formats = {}
        for clip in root.iter('asset-clip'):
            if clip.get('tcFormat') in ('DF', 'NDF'):
                self.tc_formats.setdefault(clip.get('ref', ''), clip.get('tcFormat'))
        self.media, self.clips, self.markers = {}, [], []
        self.links = 0
        self.skipped = {}

    def time(self, element, name, default=None):
        value = element.get(name)
        if value is None:
            if default is None:
                raise ValueError(f'<{element.tag}> is missing its {name}')
            return default
        return parse_fcpxml_time(value)

    def rate_of(self, format_id):
        element = self.resources.get(format_id or '')
        if element is None or element.tag != 'format' or not element.get('frameDuration'):
            return None
        duration = parse_fcpxml_time(element.get('frameDuration'))
        return snap_rate(1 / duration) if duration > 0 else None

    def skip(self, what):
        self.skipped[what] = self.skipped.get(what, 0) + 1

    def run(self):
        sequences = []
        for project in self.root_iter('project'):
            sequence = project.find('sequence')
            if sequence is not None:
                sequences.append((project.get('name') or 'Imported timeline', sequence))
        if not sequences:
            raise ValueError('The FCPXML file contains no project with a sequence')
        if len(sequences) > 1:
            self.warnings.append(f'The file holds {len(sequences)} projects; imported "{sequences[0][0]}" only.')
        self.name, sequence = sequences[0]
        format_element = self.resources.get(sequence.get('format', ''))
        self.rate = self.rate_of(sequence.get('format')) or Fraction(25)
        if format_element is None or self.rate_of(sequence.get('format')) is None:
            self.warnings.append('The sequence frame rate is missing or non-standard; 25 fps was used.')
        self.width = int(format_element.get('width', 1920)) if format_element is not None else 1920
        self.height = int(format_element.get('height', 1080)) if format_element is not None else 1080
        if not (16 <= self.width <= 16384 and 16 <= self.height <= 16384):
            raise ValueError('The sequence frame size is invalid')
        tc_start = self.time(sequence, 'tcStart', Fraction(0))
        self.start_frames = round(tc_start * self.rate) if tc_start >= 0 else None
        self.drop = sequence.get('tcFormat') == 'DF'
        spine = sequence.find('spine')
        if spine is None:
            raise ValueError('The sequence has no spine')
        self.walk(list(spine), lambda offset: offset - tc_start, 0, (Fraction(0), INFINITE), 0, ())
        for what, count in self.skipped.items():
            self.warnings.append(f'{count} {what} not imported.')
        return self

    def root_iter(self, tag):
        return self._root.iter(tag)

    def walk(self, children, to_record, lane, window, depth, stack):
        if depth > MAX_NESTING:
            self.warnings.append('Storylines or compound clips nest too deeply; the innermost were not imported.')
            return
        pending = None
        for child in children:
            self.budget.tick()
            tag = child.tag
            if tag == 'transition':
                pending = (to_record(self.time(child, 'offset')), self.time(child, 'duration'), child)
                continue
            if tag not in STORY:
                continue
            if len(self.clips) > 20_000:
                raise ValueError('The timeline has more than 20000 clips')
            child_lane = lane + int(child.get('lane', '0'))
            record = to_record(self.time(child, 'offset', Fraction(0)))
            if tag == 'spine':
                self.walk(list(child), lambda offset, base=record: base + offset, child_lane, window, depth + 1, stack)
                continue
            if tag != 'gap' and child.get('duration') is not None:
                length = self.time(child, 'duration')
                if record >= window[1] or record + length <= window[0]:
                    # outside the visible range of its container: nothing under it can show
                    pending = None
                    continue
            produced = self.element(child, record, child_lane, window, depth, stack)
            if pending is not None:
                start, length, element = pending
                # a dissolve may end exactly on the cut (all of it before the edit point)
                if produced and start <= record <= start + length:
                    self.attach_transition(produced, record - start, length, element)
                else:
                    self.warnings.append(f'Transition "{element.get("name") or "transition"}" at {float(start):.2f} s '
                                         'does not lead into a clip and was imported as a cut.')
                pending = None
            local_start = self.local_start(child)
            for sub in child:
                # a container's own storyline (children without a lane, a spine included) was read by element()
                if sub.tag in STORY and (sub.get('lane') is not None or tag not in CONTAINERS):
                    self.walk([sub], lambda offset, r=record, s=local_start: r + offset - s, child_lane, (Fraction(0), INFINITE),
                              depth + 1, stack)
                elif sub.tag in ('marker', 'chapter-marker'):
                    at = record + self.time(sub, 'start', local_start) - local_start
                    if window[0] <= at < window[1]:
                        self.markers.append({'time': at, 'name': sub.get('value') or 'Marker'})

    def local_start(self, element):
        if element.tag in ('asset-clip', 'video', 'audio') and element.get('start') is None:
            asset = self.resources.get(element.get('ref', ''))
            return self.time(asset, 'start', Fraction(0)) if asset is not None and asset.tag == 'asset' else Fraction(0)
        return self.time(element, 'start', Fraction(0))

    def element(self, element, record, lane, window, depth, stack):
        tag = element.tag
        duration = self.time(element, 'duration', Fraction(0))
        if tag == 'gap':
            return []
        if tag in ('asset-clip', 'video', 'audio'):
            return self.asset_clip(element, record, duration, lane, window)
        if tag == 'title':
            return self.title(element, record, duration, lane, window)
        if tag in ('clip', 'sync-clip'):
            content = [c for c in element if c.tag in STORY and c.get('lane') is None]
            before = len(self.clips)
            self.walk(content, lambda offset, r=record, s=self.local_start(element): r + offset - s, lane,
                      (max(window[0], record), min(window[1], record + duration)), depth + 1, stack)
            produced = self.clips[before:]
            if element.get('name'):
                for clip in produced:
                    clip['name'] = element.get('name')
            return produced
        if tag == 'ref-clip':
            ref = element.get('ref', '')
            media = self.resources.get(ref)
            if ref in stack:
                self.warnings.append(f'Compound clip {element.get("name") or ref} refers to itself and was not imported.')
                return []
            sequence = media.find('sequence') if media is not None and media.tag == 'media' else None
            spine = sequence.find('spine') if sequence is not None else None
            if spine is None:
                self.skip('compound clips without a readable sequence were')
                return []
            inner_start = self.time(sequence, 'tcStart', Fraction(0))
            before = len(self.clips)
            local = self.local_start(element)
            self.walk(list(spine), lambda offset, r=record: r + offset - inner_start - local, lane,
                      (max(window[0], record), min(window[1], record + duration)), depth + 1, (*stack, ref))
            if len(self.clips) > before:
                self.warnings.append(f'Compound clip {element.get("name") or ref} was flattened onto the timeline.')
            return self.clips[before:]
        what = {'mc-clip': 'multicam clip', 'live-drawing': 'drawing', 'caption': 'caption'}.get(tag, f'{tag} item')
        self.skip(f'{what}s were')
        if self.skipped[f'{what}s were'] <= 50:
            self.warnings.append(f'{what.capitalize()} "{element.get("name") or element.get("ref") or tag}" at '
                                 f'{float(record):.2f} s on lane {lane} was not imported; '
                                 + ('switch it to its active angle and export again.' if tag == 'mc-clip' else 'Splice has no equivalent.'))
        return []

    def media_key(self, asset):
        rid = asset.get('id')
        if rid in self.media:
            return rid
        rep = asset.find('media-rep')
        src = rep.get('src') if rep is not None else asset.get('src')
        try:
            path = path_from_url(src) if src else ''
        except ValueError as error:
            self.warnings.append(f'{asset.get("name") or rid}: {error}; it imports offline.')
            path = ''
        rate = self.rate_of(asset.get('format'))
        start = self.time(asset, 'start', Fraction(0))
        record = {'key': rid, 'name': asset.get('name') or base_name(path) or rid, 'path': path,
                  'kind': 'video' if asset.get('hasVideo') == '1' else 'audio',
                  'duration': float(self.time(asset, 'duration', Fraction(0))) or None,
                  'start': start, 'rate': rate_text(rate) if rate else None}
        if rate is not None and asset.get('hasVideo') == '1':
            drop = self.tc_formats.get(rid) == 'DF'
            try:
                record['timecode'] = frames_timecode(round(start * rate), rate, drop and rate in (Fraction(30000, 1001), Fraction(60000, 1001)))
                record['dropFrame'] = drop
            except ValueError:
                pass
        reel = asset.find(f"metadata/md[@key='{REEL_KEY}']")
        if reel is not None and reel.get('value'):
            record['reel'] = reel.get('value')[:64]
        self.media[rid] = record
        return rid

    @staticmethod
    def metadata(element):
        values = {}
        for md in element.iterfind('metadata/md'):
            key = md.get('key', '')
            if key.startswith(SPLICE_KEY):
                values[key[len(SPLICE_KEY):]] = md.get('value', '')
        return values

    def asset_clip(self, element, record, duration, lane, window):
        asset = self.resources.get(element.get('ref', ''))
        name = element.get('name') or (asset.get('name') if asset is not None else '') or 'Clip'
        if asset is None or asset.tag != 'asset':
            self.warnings.append(f'{name} refers to a missing asset and was not imported.')
            return []
        key = self.media_key(asset)
        asset_start = self.media[key]['start']
        start = self.local_start(element)
        mapping = self.time_map(element, name)

        def source(local):
            return mapping(local) - asset_start
        has_video = asset.get('hasVideo') == '1'
        has_audio = asset.get('hasAudio') == '1'
        enable = element.get('srcEnable', 'all')
        kind = 'audio' if element.tag == 'audio' or lane < 0 or not has_video or enable == 'audio' else 'video'
        meta = self.metadata(element)
        base = {'media': key, 'name': name, 'enabled': element.get('enabled', '1') != '0'}
        if element.tag == 'video' or enable == 'video':
            has_audio = False
        clip = self.placed(dict(base, kind=kind), record, duration, start, source, window)
        if clip is None:
            return []
        clip['track'] = lane + 1 if kind == 'video' else (lane if lane < 0 else 'audio-auto')
        produced = [clip]
        audio_start = element.get('audioStart')
        if kind == 'video':
            clip['embeddedAudio'] = has_audio
            self.picture(element, clip, start)
        # a split edit (audioStart), or Splice's own linked pair written as one A/V clip (com.splice.audioTrack):
        # either way the sound comes back as its own clip, linked to the picture
        if kind == 'video' and has_audio and (audio_start is not None or meta.get('audioTrack')):
            a_start = parse_fcpxml_time(audio_start) if audio_start is not None else start
            a_duration = self.time(element, 'audioDuration', duration)
            sound = self.placed(dict(base, kind='audio'), record + a_start - start, a_duration, a_start, source, window)
            if sound is not None:
                clip['embeddedAudio'] = False
                group = meta.get('linkGroup') or f'fcpxml-link-{self.links + 1}'
                self.links += 1
                clip['linkGroup'] = sound['linkGroup'] = group
                track = meta.get('audioTrack', '')
                sound['track'] = int(track) if track.lstrip('-').isdigit() and int(track) < 0 else 'audio-auto'
                self.volume(element, sound, a_start)
                produced.append(sound)
        elif kind == 'audio' or clip.get('embeddedAudio'):
            self.volume(element, clip, start)
        if meta.get('linkGroup') and 'linkGroup' not in clip:
            clip['linkGroup'] = meta['linkGroup']
        return produced

    def placed(self, clip, record, duration, start, source, window):
        """Clip ``clip`` at ``record`` for ``duration``, trimmed to ``window`` (a compound clip's
        or container's visible range), with source in, speed and direction from ``source``."""
        head = max(Fraction(0), window[0] - record)
        tail = max(Fraction(0), record + duration - window[1])
        if duration - head - tail <= 0:
            return None
        first, last = source(start + head), source(start + duration - tail)
        span = duration - head - tail
        clip.update(start=record + head, duration=span, **{'in': min(first, last)})
        speed = abs(last - first) / span
        clip['speed'] = float(speed) if speed > 0 else 1.0
        clip['reverse'] = last < first
        if min(first, last) < 0:
            self.warnings.append(f'{clip["name"]} starts before its media; its source in was set to the first frame.')
            clip['in'] = 0
        clip['in'] = float(clip['in'])
        self.clips.append(clip)
        return clip

    def time_map(self, element, name):
        points = [(self.time(p, 'time'), self.time(p, 'value')) for p in element.iterfind('timeMap/timept')]
        if len(points) < 2:
            return lambda local: local
        points.sort()
        if len(points) > 2:
            (t0, v0), (t1, v1) = points[0], points[-1]
            linear = all(abs((v - v0) * (t1 - t0) - (v1 - v0) * (t - t0)) < Fraction(1, 1000) for t, v in points)
            if not linear:
                self.warnings.append(f'{name} has a variable speed ramp; Splice imported its average constant speed.')
            points = [points[0], points[-1]]
        (t0, v0), (t1, v1) = points
        if t1 == t0:
            return lambda local: v0
        return lambda local: v0 + (v1 - v0) * (local - t0) / (t1 - t0)

    def keyframes(self, param, origin, convert):
        points = []
        for key in param.iterfind('keyframeAnimation/keyframe'):
            try:
                points.append({'time': float(self.time(key, 'time') - origin), 'value': convert(key.get('value', ''))})
            except (ValueError, IndexError):
                continue
        return [p for p in points if math.isfinite(p['value'])][:2000]

    def picture(self, element, clip, start):
        aspect = self.width / self.height
        transform = element.find('adjust-transform')
        if transform is not None:
            try:
                x, y = (float(v) for v in transform.get('position', '0 0').split()[:2])
                sx, sy = (float(v) for v in transform.get('scale', '1 1').split()[:2])
                rotation = float(transform.get('rotation', '0'))
            except ValueError:
                x = y = rotation = 0.0
                sx = sy = 1.0
                self.warnings.append(f'{clip["name"]}: its transform could not be read.')
            if abs(sx - sy) > 1e-6:
                self.warnings.append(f'{clip["name"]}: non-uniform scale {sx:g} x {sy:g} imported as {sx:g}.')
            clip['transform'] = {'x': round(x / aspect, 6), 'y': round(-y, 6), 'scale': round(sx * 100, 6), 'rotation': round(-rotation, 6)}
            keys = {}
            for param in transform.iterfind('param'):
                if param.get('name') == 'position':
                    pairs = self.keyframes(param, start, lambda v: float(v.split()[0]))
                    keys['x'] = [{'time': p['time'], 'value': round(p['value'] / aspect, 6)} for p in pairs]
                    keys['y'] = [{'time': p['time'], 'value': -float(v)} for p, v in zip(pairs, (
                        k.get('value', '0 0').split()[1] for k in param.iterfind('keyframeAnimation/keyframe')))]
                elif param.get('name') == 'scale':
                    keys['scale'] = [{'time': p['time'], 'value': p['value'] * 100} for p in self.keyframes(param, start, lambda v: float(v.split()[0]))]
                elif param.get('name') == 'rotation':
                    keys['rotation'] = [{'time': p['time'], 'value': -p['value']} for p in self.keyframes(param, start, float)]
            if any(keys.values()):
                clip.setdefault('keyframes', {}).update({k: v for k, v in keys.items() if v})
        blend = element.find('adjust-blend')
        if blend is not None:
            try:
                clip['opacity'] = round(min(1.0, max(0.0, float(blend.get('amount', '1')))) * 100, 6)
            except ValueError:
                self.warnings.append(f'{clip["name"]}: its opacity could not be read.')
            param = blend.find("param[@name='amount']")
            if param is not None:
                points = self.keyframes(param, start, float)
                if points:
                    clip.setdefault('keyframes', {})['opacity'] = [{'time': p['time'], 'value': p['value'] * 100} for p in points]
        for tag in ('adjust-crop', 'adjust-corners', 'filter-video', 'adjust-stabilization'):
            if element.find(tag) is not None:
                self.warnings.append(f'{clip["name"]}: {tag} is not supported in Splice and was not imported.')

    def volume(self, element, clip, origin):
        adjust = element.find('adjust-volume')
        if adjust is None:
            return
        try:
            if adjust.get('amount'):
                clip['volume'] = percent(adjust.get('amount'))
        except ValueError:
            self.warnings.append(f'{clip["name"]}: its volume could not be read.')
        param = adjust.find("param[@name='amount']")
        if param is None:
            return
        fade_in, fade_out = param.find('fadeIn'), param.find('fadeOut')
        if fade_in is not None:
            clip['fadeIn'] = float(self.time(fade_in, 'duration'))
        if fade_out is not None:
            clip['fadeOut'] = float(self.time(fade_out, 'duration'))
        points = self.keyframes(param, origin, percent)
        if points:
            clip.setdefault('keyframes', {})['volume'] = points
        for tag in ('adjust-panner', 'filter-audio', 'adjust-EQ', 'adjust-loudness'):
            if element.find(tag) is not None:
                self.warnings.append(f'{clip["name"]}: {tag} is not supported in Splice and was not imported.')

    def title(self, element, record, duration, lane, window):
        text = '\n'.join(''.join(node.itertext()) for node in element.iterfind('text'))
        head = max(Fraction(0), window[0] - record)
        tail = max(Fraction(0), record + duration - window[1])
        if duration - head - tail <= 0:
            return []
        clip = {'kind': 'title', 'name': element.get('name') or 'Title', 'text': text or element.get('name') or 'Title',
                'start': record + head, 'duration': duration - head - tail, 'track': max(1, lane + 1),
                'enabled': element.get('enabled', '1') != '0'}
        style = element.find('text-style-def/text-style')
        title_style = {}
        if style is not None:
            if style.get('font'):
                title_style['fontFamily'] = style.get('font')[:80]
            try:
                if style.get('fontSize'):
                    title_style['fontSize'] = round(float(style.get('fontSize')) / 10.8, 4)
                if style.get('fontColor'):
                    red, green, blue = (min(255, max(0, round(float(v) * 255))) for v in style.get('fontColor').split()[:3])
                    title_style['color'] = f'#{red:02x}{green:02x}{blue:02x}'
            except ValueError:
                self.warnings.append(f'Title "{clip["name"]}": its text style could not be read.')
            if style.get('alignment') in ('left', 'center', 'right'):
                title_style['align'] = style.get('alignment')
        transform = element.find('adjust-transform')
        if transform is not None:
            try:
                x, y = (float(v) for v in transform.get('position', '0 0').split()[:2])
                title_style['x'] = round(50 + x / (self.width / self.height), 4)
                title_style['y'] = round(50 - y, 4)
            except ValueError:
                pass
        if title_style:
            clip['titleStyle'] = title_style
        self.clips.append(clip)
        return [clip]

    def attach_transition(self, produced, before, length, element):
        names = {f.get('name', '') for f in element if f.tag in ('filter-video', 'filter-audio')}
        name = element.get('name', '') + ' ' + ' '.join(names)
        kind = 'fade' if 'Fade To Color' in name or 'Dip' in name else 'wipe' if 'Wipe' in name else 'dissolve'
        if kind == 'dissolve' and not any(n in name for n in ('Cross Dissolve', 'Audio Crossfade', 'Dissolve')):
            self.warnings.append(f'Transition "{element.get("name")}" was imported as a cross dissolve.')
        for clip in produced:
            if clip['kind'] == 'title':
                continue
            clip['transition'] = {'transition': kind, 'transitionDuration': float(length), 'transitionBefore': float(max(Fraction(0), before))}


def read(data: bytes, budget: Budget | None = None) -> dict:
    from .project import build_project
    root = xmlsafe.parse(data)
    reader = _Reader(root, budget).run()
    clips = []
    for clip in reader.clips:
        clip = dict(clip)
        clip['start'] = float(clip['start'])
        clip['duration'] = float(clip['duration'])
        clips.append(clip)
    media = []
    for record in reader.media.values():
        record = dict(record)
        record.pop('start', None)
        media.append(record)
    return build_project(reader.name, reader.rate, reader.width, reader.height, media, clips,
                         [{'time': float(m['time']), 'name': m['name']} for m in reader.markers], reader.warnings,
                         start_frames=reader.start_frames, drop=reader.drop)
