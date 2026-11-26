"""Final Cut Pro 7 XML (``xmeml`` version 5) export and import.

Premiere Pro reads no FCPXML 1.x; its XML route is FCP 7 XML, which DaVinci Resolve and Avid
import too. The layout follows what Premiere Pro 27 itself writes for a comparable timeline:

* every ``clipitem`` counts in the **sequence timebase**: ``start``/``end`` are record frames and
  ``in``/``out`` are frames of the clip's own (retimed) media timeline; ``pproTicksIn`` and
  ``pproTicksOut`` give the exact source position (254 016 000 000 ticks a second) so a source at
  another rate or an in point between sequence frames is not rounded in Premiere;
* a clip next to a transition has ``start`` or ``end`` of ``-1`` and its ``in``/``out`` run across
  the whole ``transitionitem``; the ``transitionitem`` carries ``alignment`` (``start``, ``end``,
  ``center``) and ``cutPointTicks``, the distance from its start to the edit point;
* each ``file`` is defined once with ``pathurl`` (``file://localhost/...``), its own ``rate``,
  ``duration``, ``timecode`` (``displayformat`` DF/NDF, ``reel``) and media characteristics, and
  referenced by id afterwards;
* stereo sound is two mono tracks (``premiereTrackType="Stereo"``, ``currentExplodedTrackIndex``),
  one clipitem per channel; a picture clip's own sound is written on ``V1 sound`` tracks;
  ``link`` elements tie picture and sound (``linkclipref``, ``mediatype``, ``trackindex``,
  ``clipindex``);
* speed and reverse are Premiere's ``Time Remap`` filter (``speed``, ``reverse`` and a
  ``graphdict``); in/out of a retimed clip are in its retimed timeline (source frames / speed),
  mirrored for reverse;
* clip gain, fades and volume keyframes are an ``Audio Levels`` filter (linear level, 1 = 0 dB);
  scale, position and rotation are ``Basic Motion``; opacity is ``Opacity``; keyframes keep their
  frame (``when``, in the clip's media frames);
* titles are ``generatoritem`` Text generators, markers are sequence ``marker`` elements, and the
  sequence carries its start ``timecode``.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from urllib.parse import quote, unquote, urlsplit

from . import xmlsafe
from .budget import Budget
from .cut import Cut, Event
from .media import base_name, clean
from .timecode import DROP_FRAME_RATES, frames_timecode, nominal, snap_rate, timecode_frames

FORMAT = 'XML'
VERSION = '5'
TICKS = 254_016_000_000
HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
CROSS_DISSOLVE = ('Cross Dissolve', 'Cross Dissolve', 'Dissolve')
CROSS_FADE = {True: ('Cross Fade (+3dB)', 'KGAudioTransCrossFade3dB'), False: ('Cross Fade (0dB)', 'KGAudioTransCrossFade0dB')}
MAX_LEVEL = 3.98109  # +12 dB, the Audio Levels maximum


def _text(parent, tag, value):
    element = ET.SubElement(parent, tag)
    element.text = str(value)
    return element


def _bool(value) -> str:
    return 'TRUE' if value else 'FALSE'


def _rate(parent, rate: Fraction):
    element = ET.SubElement(parent, 'rate')
    _text(element, 'timebase', nominal(rate))
    _text(element, 'ntsc', _bool(rate.denominator == 1001))
    return element


def _number(value: float) -> str:
    text = f'{float(value):.6f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def pathurl(path: str) -> str:
    """A path as FCP 7 XML writes it: ``file://localhost/...``; Windows drives as ``C%3a``."""
    drive = re.match(r'^([A-Za-z]):[\\/](.*)$', path, re.S)
    if drive:
        parts = re.split(r'[\\/]', drive.group(2))
        return f'file://localhost/{drive.group(1).upper()}%3a/' + '/'.join(quote(p, safe='') for p in parts)
    unc = re.match(r'^\\\\([^\\/]+)[\\/](.*)$', path, re.S)
    if unc:
        parts = re.split(r'[\\/]', unc.group(2))
        return f'file://{quote(unc.group(1), safe="")}/' + '/'.join(quote(p, safe='') for p in parts)
    if path.startswith('/'):
        return 'file://localhost' + quote(path, safe='/')
    return quote(path, safe='')


def path_from_pathurl(value: str) -> str:
    text = str(value or '').strip()
    if not text or len(text) > 4096:
        raise ValueError('Media path is empty or too long')
    if not text.lower().startswith('file:'):
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]+://', text):
            raise ValueError(f'Unsupported media URL scheme in {text[:80]!r}')
        return unquote(text)
    parts = urlsplit(text)
    path = unquote(parts.path)
    host = parts.netloc
    if host and host.lower() != 'localhost':
        path = '\\\\' + unquote(host) + path.replace('/', '\\')
    elif re.match(r'^/[A-Za-z]:[/\\]', path):
        path = path[1:].replace('/', '\\')
    if '\x00' in path:
        raise ValueError('Media path contains a NUL byte')
    return path


# -- export -------------------------------------------------------------------------------------

class _Writer:
    def __init__(self, cut: Cut):
        self.cut = cut
        self.rate = cut.rate
        self.files = {}           # source id -> file id
        self.masters = {}
        self.items = 0
        self.rounded = Fraction(0)
        self.report = cut.report

    def note(self, event, message):
        self.report.add(FORMAT, f'{self.cut.label(event)}: {message}')

    def media_seconds(self, source) -> Fraction:
        uses = [e.source_in + e.source_span for e in self.cut.events if e.source is source]
        return max([source.duration or Fraction(0), *uses])

    def file_rate(self, source) -> Fraction:
        return source.rate if source.rate and not source.still else self.rate

    def file_frames(self, source) -> int:
        """The file's ``<duration>``: whole frames at its own rate, never shorter than a use."""
        rate = self.file_rate(source)
        uses = [e.source_in + e.source_span for e in self.cut.events if e.source is source]
        return max(1, math.floor((source.duration or 0) * rate), *[math.ceil(u * rate) for u in uses])

    def file_seconds(self, source) -> Fraction:
        """The media length a reader mirrors a reversed clip in: the file's ``<duration>``."""
        return Fraction(self.file_frames(source)) / self.file_rate(source)

    def frame(self, source):
        """(width, height) of a source's picture in square pixels, None when unknown."""
        if source is None or not source.width or not source.height:
            return None
        name, anamorphic, _exact = self.pixel_shape(source)
        # the shape the receiving app is told: an unnamed ratio is written (and shown) as square
        return Fraction(source.width) * pixel_aspect(name, anamorphic, source.width, source.height), Fraction(source.height)

    def pixel_shape(self, source):
        return pixel_aspect_name(source.sar, source.width, source.height) if source is not None else ('square', False, True)

    def fit(self, source) -> Fraction:
        """How much Splice enlarges a source to fit the frame; Premiere's Scale 100 is native size."""
        frame = self.frame(source)
        return min(self.cut.width / frame[0], self.cut.height / frame[1]) if frame else Fraction(1)

    # files -------------------------------------------------------------------------------------
    def file(self, parent, source):
        key = source.id
        if key in self.files:
            ET.SubElement(parent, 'file', id=self.files[key])
            return
        fid = f'file-{len(self.files) + 1}'
        self.files[key] = fid
        element = ET.SubElement(parent, 'file', id=fid)
        _text(element, 'name', base_name(source.path) if source.path else source.name)
        if source.path:
            _text(element, 'pathurl', pathurl(source.path))
        else:
            _text(element, 'pathurl', quote(base_name(source.name), safe=''))
            self.report.add(FORMAT, f'Source {source.name}: its original path is unknown; the file references '
                                    f'"{source.name}" next to the XML. Relink in the receiving app.')
        rate = self.file_rate(source)
        _rate(element, rate)
        _text(element, 'duration', self.file_frames(source))
        tc_rate = source.tc_rate or rate
        drop = bool(source.drop and tc_rate in DROP_FRAME_RATES)
        start = source.tc_frames if source.tc_frames is not None else round(source.start * tc_rate)
        timecode = ET.SubElement(element, 'timecode')
        _rate(timecode, tc_rate)
        label = frames_timecode(start, tc_rate, drop)
        _text(timecode, 'string', label.replace(':', ';') if drop else label)
        _text(timecode, 'frame', start)
        _text(timecode, 'displayformat', 'DF' if drop else 'NDF')
        if source.reel:
            reel = ET.SubElement(timecode, 'reel')
            _text(reel, 'name', source.reel)
        media = ET.SubElement(element, 'media')
        if source.has_video and source.kind == 'video':
            video = ET.SubElement(media, 'video')
            chars = ET.SubElement(video, 'samplecharacteristics')
            _rate(chars, rate)
            if source.width and source.height:
                _text(chars, 'width', source.width)
                _text(chars, 'height', source.height)
            name, anamorphic, exact = self.pixel_shape(source)
            _text(chars, 'anamorphic', _bool(anamorphic))
            _text(chars, 'pixelaspectratio', name)
            if not exact:
                self.report.add(FORMAT, f'Source {source.name}: its pixel aspect ratio {source.sar.numerator}:{source.sar.denominator} '
                                        'has no FCP 7 XML name and is written as square, so the receiving app shows it '
                                        'stretched horizontally; set its pixel aspect ratio there (Premiere: Modify > Interpret Footage).')
            if not (source.width and source.height):
                self.report.add(FORMAT, f'Source {source.name}: its frame size is unknown, so scale and position are written '
                                        'as if it were the sequence size; check its framing in the receiving app.')
            _text(chars, 'fielddominance', 'none')
        if source.has_audio:
            audio = ET.SubElement(media, 'audio')
            chars = ET.SubElement(audio, 'samplecharacteristics')
            _text(chars, 'depth', 16)
            _text(chars, 'samplerate', source.audio_rate or 48000)
            _text(audio, 'channelcount', max(1, source.channels))

    # timing -------------------------------------------------------------------------------------
    def timing(self, event: Event, before: int, after: int):
        """(in, out, duration, ticks in, ticks out) of a clip whose record range is widened by
        ``before`` frames at the head and ``after`` at the tail (transitions)."""
        speed = event.speed
        frames = event.frames + before + after
        source_in = event.source_in - Fraction(before) / self.rate * speed
        span = Fraction(frames) / self.rate * speed
        media = self.file_seconds(event.source)
        if event.reverse:  # in/out count the mirrored media timeline: the record head is the source's end
            first = media - (event.source_in + event.source_span) - Fraction(before) / self.rate * speed
        else:
            first = source_in
        first = max(Fraction(0), first)
        clip_in = round(first * self.rate / speed)
        duration = max(clip_in + frames, round(media * self.rate / speed))
        self.rounded = first * self.rate / speed - clip_in   # Premiere uses the integer in
        return clip_in, clip_in + frames, duration, round(first * TICKS), round((first + span) * TICKS)

    # filters -----------------------------------------------------------------------------------
    def _filter(self, parent, name, effect_id, category, kind, mediatype, parameters):
        filt = ET.SubElement(parent, 'filter')
        effect = ET.SubElement(filt, 'effect')
        _text(effect, 'name', name)
        _text(effect, 'effectid', effect_id)
        _text(effect, 'effectcategory', category)
        _text(effect, 'effecttype', kind)
        _text(effect, 'mediatype', mediatype)
        for parameter_id, label, low, high, value, keys in parameters:
            param = ET.SubElement(effect, 'parameter', authoringApp='PremierePro')
            _text(param, 'parameterid', parameter_id)
            _text(param, 'name', label)
            if low is not None:
                _text(param, 'valuemin', _number(low))
                _text(param, 'valuemax', _number(high))
            self._value(param, value)
            for when, key_value in keys:
                keyframe = ET.SubElement(param, 'keyframe')
                _text(keyframe, 'when', when)
                self._value(keyframe, key_value)
        return effect

    @staticmethod
    def _value(parent, value):
        element = ET.SubElement(parent, 'value')
        if isinstance(value, tuple):
            _text(element, 'horiz', _number(value[0]))
            _text(element, 'vert', _number(value[1]))
        elif isinstance(value, bool):
            element.text = _bool(value)
        elif isinstance(value, (int, float, Fraction)):
            element.text = _number(value)
        else:
            element.text = str(value)

    def time_remap(self, parent, event: Event, clip_in: int, duration: int):
        media_frames = round(self.file_seconds(event.source) * self.rate)
        source_in = round(event.source_in * self.rate)
        source_out = round((event.source_in + event.source_span) * self.rate)
        if event.reverse:
            points = [(0, media_frames), (clip_in, source_out), (clip_in + event.frames, source_in), (duration, 1)]
        else:
            points = [(0, 0), (clip_in, source_in), (clip_in + event.frames, source_out), (duration, media_frames)]
        self._filter(parent, 'Time Remap', 'timeremap', 'motion', 'motion', 'video', [
            ('variablespeed', 'variablespeed', 0, 1, 0, []),
            ('speed', 'speed', -100000, 100000, float(event.speed * 100), []),
            ('reverse', 'reverse', None, None, event.reverse, []),
            ('frameblending', 'frameblending', None, None, False, []),
            ('graphdict', 'graphdict', 0, media_frames, 0, points)])

    def picture_filters(self, parent, event: Event, clip_in: int):
        """Basic Motion in Premiere's terms: Scale 100 is the media at its own pixel size and the
        centre is an offset in fractions of the media's own frame (width and height in square
        pixels). Splice fits media to the sequence frame and moves it by a percentage of the
        sequence, so Scale = fit x Splice scale and centre = offset in pixels / media size."""
        tf, keys = event.transform, event.keyframes
        source = event.source if event.kind != 'title' else None
        fit = self.fit(source)
        width, height = self.frame(source) or (Fraction(self.cut.width), Fraction(self.cut.height))
        horiz = lambda x: float(Fraction(repr(float(x))) / 100 * self.cut.width / width)
        vert = lambda y: float(Fraction(repr(float(y))) / 100 * self.cut.height / height)
        scale = lambda value: float(Fraction(repr(float(value))) * fit)
        moved = any(abs(tf.get(k, d) - d) > 1e-9 for k, d in (('x', 0), ('y', 0), ('scale', 100), ('rotation', 0)))
        animated = any(keys.get(k) for k in ('x', 'y', 'scale', 'rotation'))
        if moved or animated or fit != 1:
            times = sorted({f for k in ('x', 'y') for f, _ in keys.get(k, ())})
            center = [(clip_in + f, (horiz(_at(keys.get('x'), f, tf['x'])), vert(_at(keys.get('y'), f, tf['y'])))) for f in times]
            self._filter(parent, 'Basic Motion', 'basic', 'motion', 'motion', 'video', [
                ('scale', 'Scale', 0, 10000, scale(tf['scale']), [(clip_in + f, scale(v)) for f, v in keys.get('scale', ())]),
                ('rotation', 'Rotation', -8640, 8640, tf['rotation'], [(clip_in + f, v) for f, v in keys.get('rotation', ())]),
                ('center', 'Center', None, None, (horiz(tf['x']), vert(tf['y'])), center)])
            if moved or animated:
                self.note(event, f'scale, position and rotation are a Basic Motion filter: Scale {_number(scale(tf["scale"]))} '
                                 f'(Splice {_number(tf["scale"])} % of the fitted frame) and centre offset '
                                 f'{_number(float(Fraction(repr(float(tf["x"]))) / 100 * self.cut.width))} px, '
                                 f'{_number(float(Fraction(repr(float(tf["y"]))) / 100 * self.cut.height))} px '
                                 '(right and down positive) written as fractions of the media\'s own frame.')
        if event.opacity != 100 or keys.get('opacity'):
            self._filter(parent, 'Opacity', 'opacity', 'motion', 'motion', 'video', [
                ('opacity', 'Opacity', 0, 100, event.opacity,
                 [(clip_in + f, max(0, min(100, v))) for f, v in keys.get('opacity', ())])])

    def levels(self, parent, event: Event, clip_in: int):
        keys = event.keyframes.get('volume') or []
        if event.volume == 100 and not keys and not event.fade_in and not event.fade_out:
            return
        base = [(f, v / 100) for f, v in keys]
        points = []
        if keys or event.fade_in or event.fade_out:
            frames = event.frames
            marks = sorted({0, frames, *[f for f, _ in keys],
                            *([event.fade_in] if event.fade_in else []), *([frames - event.fade_out] if event.fade_out else [])})
            for frame in marks:
                level = _at(base, frame, event.volume / 100)
                if event.fade_in and frame < event.fade_in:
                    level *= frame / event.fade_in
                if event.fade_out and frame > frames - event.fade_out:
                    level *= (frames - frame) / event.fade_out
                points.append((clip_in + frame, min(MAX_LEVEL, max(0.0, level))))
        self._filter(parent, 'Audio Levels', 'audiolevels', 'audiolevels', 'audiolevels', 'audio', [
            ('level', 'Level', 0, MAX_LEVEL, min(MAX_LEVEL, event.volume / 100), points)])
        if event.fade_in or event.fade_out:
            self.note(event, 'audio fades are written as Audio Levels keyframes.')

    # items --------------------------------------------------------------------------------------
    def clipitem(self, track, event: Event, mediatype: str, channel: int, edges: dict, links: dict):
        source = event.source
        self.items += 1
        identity = f'clipitem-{self.items}'
        attrs = {'id': identity}
        if mediatype == 'audio':
            attrs['premiereChannelType'] = 'stereo' if min(2, source.channels) == 2 else 'mono'
        item = ET.SubElement(track, 'clipitem', attrs)
        master = self.masters.setdefault(source.id, f'masterclip-{len(self.masters) + 1}')
        _text(item, 'masterclipid', master)
        _text(item, 'name', event.name)
        _text(item, 'enabled', _bool(event.enabled))
        before, after = edges.get(('in', event.id), 0), edges.get(('out', event.id), 0)
        clip_in, clip_out, duration, ticks_in, ticks_out = self.timing(event, before, after)
        _text(item, 'duration', duration)
        _rate(item, self.rate)
        # an edge that touches a transition is -1: the transitionitem says where the clip shows
        _text(item, 'start', -1 if ('in', event.id) in edges else event.record_in)
        _text(item, 'end', -1 if ('out', event.id) in edges else event.record_out)
        _text(item, 'in', clip_in)
        _text(item, 'out', clip_out)
        _text(item, 'pproTicksIn', ticks_in)
        _text(item, 'pproTicksOut', ticks_out)
        if mediatype == 'video':
            shape, anamorphic, _exact = self.pixel_shape(source)
            _text(item, 'alphatype', 'none')
            _text(item, 'pixelaspectratio', shape)
            _text(item, 'anamorphic', _bool(anamorphic))
        if self.rounded:
            error = abs(self.rounded) * event.speed / self.rate * (source.rate or self.rate)
            self.note(event, f'its in point lies between two sequence frames, and Premiere Pro reads the whole-frame <in>: '
                             f'it starts {float(error):.2f} source frames {"later" if self.rounded < 0 else "earlier"} there '
                             '(pproTicksIn keeps the exact position for applications that read it).')
        self.file(item, source)
        if mediatype == 'audio':
            sourcetrack = ET.SubElement(item, 'sourcetrack')
            _text(sourcetrack, 'mediatype', 'audio')
            _text(sourcetrack, 'trackindex', channel)
        if event.speed != 1 or event.reverse:
            self.time_remap(item, event, clip_in + before, duration)
        if mediatype == 'video':
            self.picture_filters(item, event, clip_in + before)
        else:
            self.levels(item, event, clip_in + before)
        links.setdefault(item, [])
        return item

    def generator(self, track, event: Event):
        self.items += 1
        item = ET.SubElement(track, 'generatoritem', id=f'generatoritem-{self.items}')
        first = (event.text.splitlines() or ['Title'])[0].strip()[:120] or 'Title'
        _text(item, 'name', first)
        _text(item, 'enabled', _bool(event.enabled))
        _text(item, 'duration', event.frames)
        _rate(item, self.rate)
        _text(item, 'start', event.record_in)
        _text(item, 'end', event.record_out)
        _text(item, 'in', 0)
        _text(item, 'out', event.frames)
        _text(item, 'anamorphic', 'FALSE')
        _text(item, 'alphatype', 'black')
        effect = ET.SubElement(item, 'effect')
        for tag, value in (('name', 'Text'), ('effectid', 'Text'), ('effectcategory', 'Text'), ('effecttype', 'generator'),
                           ('mediatype', 'video')):
            _text(effect, tag, value)
        style = event.title_style
        color = str(style.get('color') or '#ffffff')
        rgb = color[1:] if re.fullmatch(r'#[0-9a-fA-F]{6}', color) else 'ffffff'
        for parameter_id, label, value in (('str', 'Text', event.text), ('fontname', 'Font', str(style.get('fontFamily') or 'Helvetica')[:80]),
                                           ('fontsize', 'Size', _number(_finite(style.get('fontSize'), 8) * 10.8)),
                                           ('fontalign', 'Alignment', {'left': 1, 'right': 3}.get(style.get('align'), 2))):
            param = ET.SubElement(effect, 'parameter')
            _text(param, 'parameterid', parameter_id)
            _text(param, 'name', label)
            _text(param, 'value', value)
        param = ET.SubElement(effect, 'parameter')
        _text(param, 'parameterid', 'fontcolor')
        _text(param, 'name', 'Font Color')
        value = ET.SubElement(param, 'value')
        for tag, part in (('alpha', 'ff'), ('red', rgb[0:2]), ('green', rgb[2:4]), ('blue', rgb[4:6])):
            _text(value, tag, int(part, 16))
        if event.opacity != 100 or event.keyframes.get('opacity'):
            self.picture_filters(item, event, 0)
        self.note(event, f'title "{first}" is a Text generator; Premiere Pro 27 imports it as a graphic named after its text, '
                         'Resolve as its Text generator; font, size and position may differ. Restyle it there.')

    def transition(self, track, event: Event, mediatype: str):
        tr = event.transition
        item = ET.SubElement(track, 'transitionitem')
        start = event.record_in - tr.before
        _text(item, 'start', start)
        _text(item, 'end', start + tr.frames)
        alignment = 'start' if tr.before == 0 else 'end' if tr.before == tr.frames else 'center'
        if event.id in self.from_black:
            alignment = 'start-black'
        _text(item, 'alignment', alignment)
        _text(item, 'cutPointTicks', round(Fraction(tr.before) / self.rate * TICKS))
        _rate(item, self.rate)
        effect = ET.SubElement(item, 'effect')
        if mediatype == 'video':
            name, effect_id, category = CROSS_DISSOLVE
            values = (('name', name), ('effectid', effect_id), ('effectcategory', category), ('effecttype', 'transition'),
                      ('mediatype', 'video'))
            if tr.kind != 'dissolve':
                self.note(event, f'{"dip to black" if tr.kind == "fade" else "wipe"} written as a cross dissolve.')
        else:
            name, effect_id = CROSS_FADE[tr.power]
            values = (('name', name), ('effectid', effect_id), ('effecttype', 'transition'), ('mediatype', 'audio'))
        for tag, value in values + (('wipecode', 0), ('wipeaccuracy', 100), ('startratio', 0), ('endratio', 1), ('reverse', 'FALSE')):
            _text(effect, tag, value)

    def track_items(self, track, events, mediatype, channel, links, item_of):
        """Clipitems (and transitions) of one track; ``item_of[(event id, channel)]`` -> element."""
        edges = {}
        ordered = sorted(events, key=lambda e: e.record_in)
        by_end = {e.record_out: e for e in ordered}
        for event in ordered:
            tr = event.transition
            if tr is None:
                continue
            outgoing = by_end.get(event.record_in)
            edges[('in', event.id)] = tr.before
            if outgoing is not None:
                edges[('out', outgoing.id)] = tr.frames - tr.before
            else:
                self.from_black.add(event.id)
        for event in ordered:
            if event.transition is not None:
                self.transition(track, event, mediatype)
            if event.kind == 'title':
                self.generator(track, event)
                continue
            item_of[(event.id, mediatype, channel)] = self.clipitem(track, event, mediatype, channel, edges, links)

    def document(self) -> ET.Element:
        cut = self.cut
        self.from_black = set()
        root = ET.Element('xmeml', version=VERSION)
        sequence = ET.SubElement(root, 'sequence', id='sequence-1')
        _text(sequence, 'duration', cut.frames)
        _rate(sequence, self.rate)
        _text(sequence, 'name', cut.name)
        media = ET.SubElement(sequence, 'media')
        video = ET.SubElement(media, 'video')
        fmt = ET.SubElement(ET.SubElement(video, 'format'), 'samplecharacteristics')
        _rate(fmt, self.rate)
        _text(fmt, 'width', cut.width)
        _text(fmt, 'height', cut.height)
        _text(fmt, 'anamorphic', 'FALSE')
        _text(fmt, 'pixelaspectratio', 'square')
        _text(fmt, 'fielddominance', 'none')
        links, item_of, track_index = {}, {}, {}
        pictures = sorted({e.track for e in cut.events if e.kind != 'audio'})
        for number in range(1, (max(pictures) if pictures else 0) + 1):
            track = ET.SubElement(video, 'track', {'MZ.TrackName': f'V{number}'})
            track_index[('video', number)] = number
            self.track_items(track, [e for e in cut.events if e.kind != 'audio' and e.track == number], 'video', 1, links, item_of)
            _text(track, 'enabled', 'TRUE')
            _text(track, 'locked', 'FALSE')
        audio = ET.SubElement(media, 'audio')
        _text(audio, 'numOutputChannels', 2)
        chars = ET.SubElement(ET.SubElement(audio, 'format'), 'samplecharacteristics')
        _text(chars, 'depth', 16)
        _text(chars, 'samplerate', 48000)
        groups = []
        for number in sorted({-e.track for e in cut.events if e.kind == 'audio'}):
            groups.append((f'A{number}', [e for e in cut.events if e.kind == 'audio' and e.track == -number], False))
        for number in pictures:
            embedded = [e for e in cut.events if e.kind == 'video' and e.track == number and e.embedded_audio]
            if embedded:
                groups.append((f'V{number} sound', embedded, True))
        index = 0
        for name, events, embedded in groups:
            width = max(min(2, max(1, e.source.channels)) for e in events)
            for channel in range(1, width + 1):
                index += 1
                track = ET.SubElement(audio, 'track', {
                    'MZ.TrackName': name, 'currentExplodedTrackIndex': str(channel - 1), 'totalExplodedTrackCount': str(width),
                    'premiereTrackType': 'Stereo' if width == 2 else 'Mono'})
                chosen = [e for e in events if channel <= max(1, min(2, e.source.channels))]
                self.track_items(track, chosen, 'audio', channel, links, item_of)
                for event in chosen:
                    track_index[('audio', event.id, channel)] = index
                _text(track, 'enabled', 'TRUE')
                _text(track, 'locked', 'FALSE')
                _text(track, 'outputchannelindex', channel)
        self.parents = {}
        for track in (*video.iterfind('track'), *audio.iterfind('track')):
            # Premiere counts every item of the track, transitions included, from 1
            items = [child for child in track if child.tag in ('clipitem', 'generatoritem', 'transitionitem')]
            for position, item in enumerate(items, 1):
                self.parents[item] = position
        self._links(item_of, track_index, pictures)
        for event in cut.events:
            if event.reverse and event.kind == 'video' and (event.embedded_audio or event.link_group):
                self.note(event, 'reversed and linked to its sound; Premiere Pro imports a reversed picture clip unlinked '
                                 'from its sound (its own XML export writes that pair unlinked too). Relink them there '
                                 '(select both, Clip > Link).')
        timecode = ET.SubElement(sequence, 'timecode')
        _rate(timecode, self.rate)
        label = cut.timecode(0)
        _text(timecode, 'string', label.replace(':', ';') if cut.drop else label)
        _text(timecode, 'frame', cut.tc_start)
        _text(timecode, 'displayformat', 'DF' if cut.drop else 'NDF')
        for marker in cut.markers:
            element = ET.SubElement(sequence, 'marker')
            _text(element, 'comment', '')
            _text(element, 'name', marker.name)
            _text(element, 'in', marker.frame)
            _text(element, 'out', marker.frame + marker.duration if marker.duration else -1)
        self.report.add(FORMAT, 'FCP 7 XML is the XML Premiere Pro imports (File > Import); Resolve and Avid read it too.')
        return root

    def _links(self, item_of, track_index, pictures):
        """Tie a picture clip to its own sound, and a Splice link group's clips together."""
        groups = {}
        for (event_id, mediatype, channel), element in item_of.items():
            event = self.by_id[event_id]
            key = event.link_group or f'clip:{event_id}'
            groups.setdefault(key, []).append((event, mediatype, channel, element))
        for members in groups.values():
            if len(members) < 2:
                continue
            ordered = sorted(members, key=lambda m: (m[1] != 'video', m[2]))
            for _event, _mediatype, _channel, element in ordered:
                for event, mediatype, channel, other in ordered:
                    link = ET.SubElement(element, 'link')
                    _text(link, 'linkclipref', other.get('id'))
                    _text(link, 'mediatype', mediatype)
                    track = track_index[('video', event.track)] if mediatype == 'video' else track_index[('audio', event.id, channel)]
                    _text(link, 'trackindex', track)
                    _text(link, 'clipindex', self._clip_index(other))
                    if mediatype == 'audio':
                        _text(link, 'groupindex', 1)
        _reorder(item_of.values())

    def _clip_index(self, element):
        return self.parents[element]

    def write(self) -> str:
        self.by_id = {e.id: e for e in self.cut.events}
        root = self.document()
        _indent(root)
        return HEADER + ET.tostring(root, encoding='unicode') + '\n'


def _reorder(elements):
    """``link`` elements after the filters, as Premiere writes them."""
    for element in elements:
        links = [child for child in element if child.tag == 'link']
        for link in links:
            element.remove(link)
        element.extend(links)


def _at(points, frame, default):
    if not points:
        return default
    if frame <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if frame <= t1:
            return v0 + (v1 - v0) * (frame - t0) / (t1 - t0) if t1 != t0 else v1
    return points[-1][1]


def _finite(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _indent(element, level=0):
    if len(element) == 0:
        return
    pad = '\n' + '\t' * (level + 1)
    element.text = pad
    for child in element:
        _indent(child, level + 1)
        child.tail = pad
    element[-1].tail = '\n' + '\t' * level


def write(cut: Cut) -> str:
    if cut.frames <= 0:
        raise ValueError('The timeline is empty')
    return _Writer(cut).write()


# -- import -------------------------------------------------------------------------------------

def _int(element, tag, default=None):
    value = element.findtext(tag)
    if value is None or not value.strip():
        return default
    try:
        number = int(float(value.strip()))
    except (ValueError, OverflowError):  # OverflowError: 'inf'
        raise ValueError(f'<{tag}> {value.strip()[:20]!r} is not a number') from None
    if abs(number) > 10 ** 18:
        raise ValueError(f'<{tag}> is out of range')
    return number


def _rate_of(element, default: Fraction) -> Fraction:
    rate = element.find('rate') if element is not None else None
    if rate is None:
        return default
    base = _int(rate, 'timebase', 0) or 0
    if not 1 <= base <= 1000:
        return default
    ntsc = (rate.findtext('ntsc') or '').strip().upper() == 'TRUE'
    value = Fraction(base * 1000, 1001) if ntsc else Fraction(base)
    return snap_rate(value) or value


def _param(effect, parameter_id):
    for param in effect.iterfind('parameter'):
        if (param.findtext('parameterid') or '').strip() == parameter_id:
            return param
    return None


def _float(text, default=None):
    try:
        number = float(str(text).strip())
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


class _Reader:
    def __init__(self, root, budget):
        if root.tag == 'fcpxml':
            raise ValueError('This is FCPXML, not Final Cut Pro 7 XML')
        if root.tag != 'xmeml':
            raise ValueError('This is not a Final Cut Pro 7 XML (xmeml) document')
        self.budget = budget or Budget()
        self.warnings = []
        if root.get('version') not in ('4', '5'):
            self.warnings.append(f'FCP 7 XML version {root.get("version") or "unknown"} was read with the version 5 rules.')
        sequences = list(root.iter('sequence'))
        if not sequences:
            raise ValueError('The XML file contains no sequence')
        if len(sequences) > 1:
            self.warnings.append(f'The file holds {len(sequences)} sequences; imported the first only.')
        self.sequence = sequences[0]
        self.files = {}
        for element in root.iter('file'):
            if element.get('id') and len(element):
                self.files.setdefault(element.get('id'), element)
        self.media, self.clips, self.markers = {}, [], []
        self.named = set()

    def run(self):
        sequence = self.sequence
        self.name = (sequence.findtext('name') or 'Imported XML').strip()
        self.rate = _rate_of(sequence, Fraction(25))
        if sequence.find('rate') is None:
            self.warnings.append('The sequence frame rate is missing; 25 fps was used.')
        chars = sequence.find('media/video/format/samplecharacteristics')
        self.width = _int(chars, 'width', 1920) if chars is not None else 1920
        self.height = _int(chars, 'height', 1080) if chars is not None else 1080
        if not (16 <= self.width <= 16384 and 16 <= self.height <= 16384):
            raise ValueError('The sequence frame size is invalid')
        video_tracks = sequence.findall('media/video/track')
        for number, track in enumerate(video_tracks[:64], 1):
            self.track(track, 'video', number)
        audio_number = 0
        for track in sequence.findall('media/audio/track')[:256]:
            exploded = track.get('currentExplodedTrackIndex')
            if exploded in (None, '0'):
                audio_number += 1
            self.track(track, 'audio', audio_number)
        self.merge_sound()
        for marker in sequence.iterfind('marker'):
            at = _int(marker, 'in', None)
            if at is not None and at >= 0:
                self.markers.append({'time': Fraction(at) / self.rate, 'name': (marker.findtext('name') or 'Marker').strip()})
        return self

    def track(self, track, kind, number):
        items = list(track)
        if len(items) > 20_000:
            raise ValueError('An XML track has more than 20000 items')
        for index, item in enumerate(items):
            self.budget.tick()
            if item.tag not in ('clipitem', 'generatoritem'):
                continue
            start, end = _int(item, 'start', -1), _int(item, 'end', -1)
            lead = items[index - 1] if index > 0 and items[index - 1].tag == 'transitionitem' else None
            trail = items[index + 1] if index + 1 < len(items) and items[index + 1].tag == 'transitionitem' else None
            if start == -1 and lead is not None:
                start = _int(lead, 'start', 0)
            elif lead is not None and start != _int(lead, 'start', 0):
                lead = None if start >= _int(lead, 'end', 0) else lead
            if end == -1 and trail is not None:
                end = _int(trail, 'end', 0)
            elif trail is not None and end <= _int(trail, 'start', 0):
                trail = None
            if start is None or end is None or start < 0 or end <= start:
                self.warnings.append(f'{item.findtext("name") or "A clip"} has no readable record range and was not imported.')
                continue
            if item.tag == 'generatoritem':
                self.generator(item, start, end, number)
                continue
            clip = self.clipitem(item, kind, number, start, end, trail)
            if clip is None:
                continue
            clip['_trackname'] = (track.get('MZ.TrackName') or '').strip()
            if lead is not None:
                cut_ticks = _int(lead, 'cutPointTicks', None)
                lead_start, lead_end = _int(lead, 'start', 0), _int(lead, 'end', 0)
                alignment = (lead.findtext('alignment') or 'center').strip()
                length = max(1, lead_end - lead_start)
                if cut_ticks is not None:
                    before = Fraction(round(Fraction(cut_ticks, TICKS) * self.rate))
                else:
                    before = {'start': 0, 'start-black': 0, 'end': length, 'end-black': length}.get(alignment, Fraction(length, 2))
                before = min(Fraction(length), max(Fraction(0), before))
                # the clip begins at the cut; the transition's frames before it are handles
                cut = lead_start + before
                self.trim_head(clip, cut - start)
                effect = (lead.findtext('effect/name') or '') + ' ' + (lead.findtext('effect/effectid') or '')
                transition = 'fade' if 'Dip' in effect else 'wipe' if 'Wipe' in effect else 'dissolve'
                if not any(n in effect for n in ('Dissolve', 'Cross Fade', 'CrossFade', 'Dip')):
                    self.warnings.append(f'Transition "{effect.strip()}" was imported as a cross dissolve.')
                clip['transition'] = {'transition': transition, 'transitionDuration': float(Fraction(length) / self.rate),
                                      'transitionBefore': float(before / self.rate)}
                clip['transitionPower'] = '3dB' in effect
            if trail is not None:
                trail_start, trail_end = _int(trail, 'start', 0), _int(trail, 'end', 0)
                cut_ticks = _int(trail, 'cutPointTicks', None)
                length = max(1, trail_end - trail_start)
                alignment = (trail.findtext('alignment') or 'center').strip()
                if cut_ticks is not None:
                    before = round(Fraction(cut_ticks, TICKS) * self.rate)
                else:
                    before = {'start': 0, 'end': length, 'end-black': length}.get(alignment, length // 2)
                cut = trail_start + min(length, max(0, before))
                self.trim_tail(clip, end - cut)

    def trim_head(self, clip, frames):
        if frames <= 0:
            return
        seconds = Fraction(frames) / self.rate
        clip['start'] += seconds
        clip['duration'] -= seconds
        if not clip['reverse']:
            clip['in'] += seconds * clip['speed']

    def trim_tail(self, clip, frames):
        if frames <= 0:
            return
        seconds = Fraction(frames) / self.rate
        clip['duration'] -= seconds
        if clip['reverse']:
            clip['in'] += seconds * clip['speed']

    def media_key(self, file_element, kind):
        fid = file_element.get('id') or f'anonymous-{len(self.media) + 1}'
        definition = self.files.get(fid, file_element)
        if fid in self.media:
            if kind == 'video':
                self.media[fid]['kind'] = 'video'
            return fid
        path = ''
        if definition.findtext('pathurl'):
            try:
                path = path_from_pathurl(definition.findtext('pathurl'))
            except ValueError as error:
                self.warnings.append(f'{definition.findtext("name") or fid}: {error}; it imports offline.')
        rate = _rate_of(definition, self.rate)
        has_video = definition.find('media/video') is not None
        record = {'key': fid, 'name': clean(definition.findtext('name') or base_name(path) or fid), 'path': path,
                  'kind': 'video' if has_video or kind == 'video' else 'audio',
                  'rate': f'{rate.numerator}/{rate.denominator}' if rate.denominator != 1 else str(rate.numerator)}
        duration = _int(definition, 'duration', None)
        if duration and duration > 0:
            record['duration'] = float(Fraction(duration) / rate)
        timecode = definition.find('timecode')
        if timecode is not None:
            tc_rate = _rate_of(timecode, rate)
            drop = (timecode.findtext('displayformat') or '').strip().upper() == 'DF' and tc_rate in DROP_FRAME_RATES
            frame = _int(timecode, 'frame', None)
            try:
                if frame is not None and frame >= 0:
                    record['timecode'] = frames_timecode(frame, tc_rate, drop)
                elif timecode.findtext('string'):
                    label = timecode.findtext('string').strip().replace(';', ':')
                    record['timecode'] = frames_timecode(timecode_frames(label, tc_rate, drop), tc_rate, drop)
                record['dropFrame'] = drop
            except ValueError:
                self.warnings.append(f'{record["name"]}: its timecode could not be read.')
            reel = timecode.findtext('reel/name')
            if reel and reel.strip():
                record['reel'] = clean(reel.strip(), 64)
        self.media[fid] = record
        return fid

    def clipitem(self, item, kind, number, start, end, trail):
        name = clean(item.findtext('name') or 'Clip')
        file_element = item.find('file')
        definition = self.files.get(file_element.get('id'), file_element) if file_element is not None else None
        graphic = item.find("filter/effect[effectid='GraphicAndType']")
        if kind == 'video' and (graphic is not None or (definition is not None and
                                (definition.findtext('mediaSource') or '').strip() == 'GraphicAndType')):
            return self.graphic(item, graphic, name, start, end, number)
        if file_element is None:
            self.warnings.append(f'{name} has no file and was not imported.')
            return None
        key = self.media_key(file_element, kind)
        if key not in self.named:
            # <file><name> is the file's own name; the clip name is what the editor called the source
            self.named.add(key)
            self.media[key]['name'] = name
        speed, reverse = Fraction(1), False
        for effect in item.iterfind('filter/effect'):
            effect_id = (effect.findtext('effectid') or '').strip()
            if effect_id == 'timeremap':
                value = _float(_param(effect, 'speed').findtext('value') if _param(effect, 'speed') is not None else None, 100)
                reverse_param = _param(effect, 'reverse')
                reverse = reverse_param is not None and (reverse_param.findtext('value') or '').strip().upper() == 'TRUE'
                if value < 0:
                    reverse, value = True, -value
                speed = Fraction(repr(value or 100)) / 100
                variable = _param(effect, 'variablespeed')
                if variable is not None and _float(variable.findtext('value'), 0):
                    self.warnings.append(f'{name} has a variable speed ramp; Splice imported its constant speed.')
        speed = speed.limit_denominator(100000)
        clip_in, clip_out = _int(item, 'in', 0), _int(item, 'out', 0)
        ticks_in, ticks_out = _int(item, 'pproTicksIn', None), _int(item, 'pproTicksOut', None)
        if ticks_in is not None and ticks_out is not None and ticks_out > ticks_in:
            first = Fraction(ticks_in, TICKS)
        else:
            first = Fraction(clip_in) / self.rate * speed
        if reverse:
            # the mirror is the file's own length at the file's rate when the file gives it (the
            # clipitem <duration> is that length rounded to sequence frames of the retimed clip)
            file_frames = _int(definition, 'duration', None) if definition is not None else None
            if file_frames and file_frames > 0:
                media = Fraction(file_frames) / _rate_of(definition, self.rate)
            else:
                duration_frames = _int(item, 'duration', clip_out) or clip_out
                media = Fraction(duration_frames) / self.rate * speed
            span = Fraction(end - start) / self.rate * speed
            first = max(Fraction(0), media - first - span)
        sourcetrack = _int(item, 'sourcetrack/trackindex', 1) if kind == 'audio' else 1
        clip = {'kind': kind, 'media': key, 'name': name, 'start': Fraction(start) / self.rate,
                'duration': Fraction(end - start) / self.rate, 'in': first, 'speed': speed, 'reverse': reverse,
                'enabled': (item.findtext('enabled') or 'TRUE').strip().upper() != 'FALSE',
                'track': number if kind == 'video' else -number, '_id': item.get('id'), '_channel': sourcetrack,
                '_links': [(link.findtext('linkclipref') or '').strip() for link in item.iterfind('link')]}
        if kind == 'video':
            clip['embeddedAudio'] = False
            self.picture(item, clip, clip_in, self.frame_of(definition))
        else:
            self.levels(item, clip, clip_in)
        self.clips.append(clip)
        return clip

    def frame_of(self, definition):
        """(display width, height) of a file's picture in square pixels, or None when unknown."""
        chars = definition.find('media/video/samplecharacteristics') if definition is not None else None
        if chars is None:
            return None
        width, height = _int(chars, 'width', 0) or 0, _int(chars, 'height', 0) or 0
        if not (1 <= width <= 16384 and 1 <= height <= 16384):
            return None
        par = pixel_aspect(chars.findtext('pixelaspectratio'), (chars.findtext('anamorphic') or '').strip().upper() == 'TRUE',
                           width, height)
        return float(width * par), float(height)

    def picture(self, item, clip, origin, frame):
        """Basic Motion as Premiere means it: Scale 100 shows the media at its own size and the
        centre is a fraction of the media's own frame. Splice fits media to the sequence frame and
        moves it by a percentage of the sequence, so both are converted with the fit factor."""
        width, height = frame or (float(self.width), float(self.height))
        fit = min(self.width / width, self.height / height)
        aspect = None
        for effect in item.iterfind('filter/effect'):
            if (effect.findtext('effectid') or '').strip() == 'deformation':
                param = _param(effect, 'aspect')
                aspect = _float(param.findtext('value')) if param is not None else None
        # Premiere's own XML export writes a pixel aspect ratio it cannot name as a Distort
        # "aspect" of 100 - k and a Basic Motion scale k times the user's: fold it back out
        compensation = (100 - aspect) / 100 if aspect is not None and -99 < aspect < 0 else 1.0
        if abs(fit - 1) > 1e-9 and item.find("filter/effect[effectid='basic']") is None:
            # no Basic Motion: Premiere shows the media at its own size, which in Splice is 100 / fit
            clip['transform'] = {'x': 0.0, 'y': 0.0, 'scale': round(100 / fit, 4), 'rotation': 0.0}
        for effect in item.iterfind('filter/effect'):
            effect_id = (effect.findtext('effectid') or '').strip()
            if effect_id == 'basic':
                transform, keys = {'x': 0.0, 'y': 0.0, 'scale': 100.0, 'rotation': 0.0}, {}
                to_splice = {'scale': lambda v: round(v / compensation / fit, 4), 'rotation': lambda v: v}
                for parameter_id, field in (('scale', 'scale'), ('rotation', 'rotation')):
                    param = _param(effect, parameter_id)
                    if param is not None:
                        value = _float(param.findtext('value'))
                        transform[field] = to_splice[field](value) if value is not None else transform[field]
                        points = [(_int(k, 'when', 0), _float(k.findtext('value'))) for k in param.iterfind('keyframe')]
                        points = [{'time': float(Fraction(w - origin) / self.rate), 'value': to_splice[field](v)}
                                  for w, v in points if v is not None]
                        if points:
                            keys[field] = points[:2000]
                center = _param(effect, 'center')
                # centre fraction of the media frame -> percent of the sequence frame
                to_x = lambda h: round(h * width / self.width * 100, 4)
                to_y = lambda v: round(v * height / self.height * 100, 4)
                if center is not None:
                    transform['x'] = to_x(_float(center.findtext('value/horiz'), 0) or 0)
                    transform['y'] = to_y(_float(center.findtext('value/vert'), 0) or 0)
                    xs, ys = [], []
                    for key in center.iterfind('keyframe'):
                        when = float(Fraction(_int(key, 'when', 0) - origin) / self.rate)
                        horiz, vert = _float(key.findtext('value/horiz')), _float(key.findtext('value/vert'))
                        if horiz is not None and vert is not None:
                            xs.append({'time': when, 'value': to_x(horiz)})
                            ys.append({'time': when, 'value': to_y(vert)})
                    if xs:
                        keys['x'], keys['y'] = xs[:2000], ys[:2000]
                clip['transform'] = transform
                if keys:
                    clip.setdefault('keyframes', {}).update(keys)
            elif effect_id == 'deformation' and aspect is not None and compensation != 1.0:
                continue
            elif effect_id == 'opacity':
                param = _param(effect, 'opacity')
                if param is not None:
                    value = _float(param.findtext('value'))
                    if value is not None:
                        clip['opacity'] = value
                    points = [{'time': float(Fraction(_int(k, 'when', 0) - origin) / self.rate), 'value': _float(k.findtext('value'))}
                              for k in param.iterfind('keyframe')]
                    points = [p for p in points if p['value'] is not None]
                    if points:
                        clip.setdefault('keyframes', {})['opacity'] = points[:2000]
            elif effect_id not in ('timeremap', ''):
                self.warnings.append(f'{clip["name"]}: filter {(effect.findtext("name") or effect_id)[:60]} was not imported.')

    def levels(self, item, clip, origin):
        for effect in item.iterfind('filter/effect'):
            effect_id = (effect.findtext('effectid') or '').strip()
            if effect_id == 'audiolevels':
                param = _param(effect, 'level')
                if param is None:
                    continue
                value = _float(param.findtext('value'))
                if value is not None:
                    clip['volume'] = round(value * 100, 6)
                points = [{'time': float(Fraction(_int(k, 'when', 0) - origin) / self.rate), 'value': _float(k.findtext('value'))}
                          for k in param.iterfind('keyframe')]
                points = [{'time': p['time'], 'value': round(p['value'] * 100, 6)} for p in points if p['value'] is not None]
                if points:
                    clip.setdefault('keyframes', {})['volume'] = points[:2000]
            elif effect_id not in ('timeremap', ''):
                self.warnings.append(f'{clip["name"]}: filter {(effect.findtext("name") or effect_id)[:60]} was not imported.')

    def generator(self, item, start, end, number):
        text = ''
        for param in item.iterfind('effect/parameter'):
            if (param.findtext('parameterid') or '').strip() == 'str':
                text = param.findtext('value') or ''
        name = clean(item.findtext('name') or 'Title')
        clip = {'kind': 'title', 'name': name, 'text': text or name, 'start': Fraction(start) / self.rate,
                'duration': Fraction(end - start) / self.rate, 'track': number,
                'enabled': (item.findtext('enabled') or 'TRUE').strip().upper() != 'FALSE'}
        self.clips.append(clip)

    def graphic(self, item, effect, name, start, end, number):
        """A Premiere graphic (Essential Graphics text) as a Splice title. Premiere stores the text
        in a binary "Source Text" value; when it cannot be read the title shows the graphic's name."""
        text = ''
        if effect is not None:
            for param in effect.iterfind('parameter'):
                if (param.findtext('name') or '').strip() == 'Source Text':
                    text = graphic_text(param.findtext('value') or '')
            name = clean(effect.findtext('name') or name) or name
        if not text:
            self.warnings.append(f'Premiere graphic "{name}": its text could not be read from the file, so it was imported '
                                 'as a title showing the graphic\'s name. Retype the text.')
        else:
            self.warnings.append(f'Premiere graphic "{name}" was imported as a Splice title with its text; its font, '
                                 'style and position were not imported.')
        clip = {'kind': 'title', 'name': name, 'text': text or name, 'start': Fraction(start) / self.rate,
                'duration': Fraction(end - start) / self.rate, 'track': number,
                'enabled': (item.findtext('enabled') or 'TRUE').strip().upper() != 'FALSE'}
        self.clips.append(clip)
        return None

    def merge_sound(self):
        """Stereo arrives as one clipitem per channel, and Splice's own export writes a picture
        clip's embedded sound on that picture track's "V<n> sound" tracks. Those collapse back
        into Splice's single clips; every other sound clip, linked or not, stays a sound clip (in
        the picture's link group), so its gain, fades and keyframes are kept."""
        by_id = {c['_id']: c for c in self.clips if c.get('_id')}
        pictures = {}
        for clip in self.clips:
            if clip['kind'] == 'video':
                pictures.setdefault((clip['track'], clip['media'], clip['start'], clip['duration'], clip['in']), clip)
        keep, groups = [], 0
        levels = lambda c: (c.get('volume', 100), repr(c.get('keyframes', {}).get('volume')))
        for clip in self.clips:
            if clip['kind'] != 'audio':
                keep.append(clip)
                continue
            partners = [by_id[ref] for ref in clip['_links'] if ref in by_id and by_id[ref] is not clip]
            same = lambda other: (other['media'], other['start'], other['duration'], other['in']) == \
                (clip['media'], clip['start'], clip['duration'], clip['in'])
            own = re.fullmatch(r'V(\d+) sound', clip.get('_trackname') or '')
            picture = None
            if own:
                key = (int(own.group(1)), clip['media'], clip['start'], clip['duration'], clip['in'])
                linked = next((p for p in partners if p['kind'] == 'video' and same(p)), None)
                # Premiere drops the link of a reversed picture/sound pair; the track still says whose sound it is
                picture = linked or (pictures.get(key) if not any(p['kind'] == 'video' for p in partners) else None)
                if picture is not None and picture['track'] != int(own.group(1)):
                    picture = None
            if picture is not None and picture.get('_sound') in (None, levels(clip)) and picture.get('enabled') == clip.get('enabled'):
                if picture.get('_sound') is None:
                    picture['_sound'] = levels(clip)
                    picture['embeddedAudio'] = True
                    if clip.get('volume', 100) != 100:
                        picture['volume'] = clip['volume']
                    if clip.get('keyframes', {}).get('volume'):
                        picture.setdefault('keyframes', {})['volume'] = clip['keyframes']['volume']
                continue
            twin = next((p for p in partners if p['kind'] == 'audio' and same(p) and p.get('_kept')
                         and levels(p) == levels(clip)), None)
            if twin is not None:
                continue
            clip['_kept'] = True
            keep.append(clip)
        for clip in keep:
            partners = [by_id[ref] for ref in clip.get('_links', []) if ref in by_id and by_id[ref] is not clip]
            linked = [p for p in partners if p in keep and p['kind'] != clip['kind']]
            if linked and 'linkGroup' not in clip:
                groups += 1
                group = f'xml-link-{groups}'
                for member in [clip, *linked]:
                    member.setdefault('linkGroup', group)
        self.clips = keep


def pixel_aspect(name, anamorphic: bool, width: int, height: int) -> Fraction:
    """The pixel aspect ratio an FCP 7 XML ``pixelaspectratio`` name stands for."""
    key = (name or 'square').strip()
    table = {'NTSC-601': Fraction(10, 11), 'PAL-601': Fraction(12, 11), 'HD-(960x720)': Fraction(4, 3),
             'HD-(1280x1080)': Fraction(3, 2), 'HD-(1440x1080)': Fraction(4, 3), 'DVCPROHD-720p': Fraction(4, 3),
             'DVCPROHD-1080i50': Fraction(4, 3), 'DVCPROHD-1080i60': Fraction(3, 2)}
    par = table.get(key, Fraction(1))
    if anamorphic and key in ('NTSC-601', 'PAL-601'):
        par = par * 4 / 3
    return par


def pixel_aspect_name(sar: Fraction | None, width: int | None, height: int | None):
    """``(pixelaspectratio, anamorphic, exact)`` for a sample aspect ratio. ``exact`` is False when
    FCP 7 XML has no name for it and "square" was chosen."""
    if not sar or abs(sar - 1) < Fraction(1, 100):
        return 'square', False, True
    for name, value, anamorphic, sizes in (
            ('HD-(1440x1080)', Fraction(4, 3), False, {(1440, 1080)}), ('HD-(1280x1080)', Fraction(3, 2), False, {(1280, 1080)}),
            ('HD-(960x720)', Fraction(4, 3), False, {(960, 720)}),
            ('NTSC-601', Fraction(10, 11), False, {(720, 480), (720, 486)}), ('NTSC-601', Fraction(40, 33), True, {(720, 480), (720, 486)}),
            ('PAL-601', Fraction(12, 11), False, {(720, 576)}), ('PAL-601', Fraction(16, 11), True, {(720, 576)})):
        if (width, height) in sizes and abs(sar - value) < Fraction(1, 50):
            return name, anamorphic, True
    return 'square', False, False


def graphic_text(value: str) -> str:
    """The text of a Premiere "Source Text" value (base64 of a binary record in which each string
    is a little-endian 32-bit length and UTF-8 bytes, zero padded): the last string in it, as the
    font name comes before the text. '' when nothing readable is there."""
    import base64
    import binascii
    try:
        data = base64.b64decode(re.sub(r'\s+', '', value)[:400_000], validate=True)
    except (binascii.Error, ValueError):
        return ''
    found = ''
    for offset in range(0, max(0, len(data) - 4)):
        length = int.from_bytes(data[offset:offset + 4], 'little')
        end = offset + 4 + length
        if not 1 <= length <= 4000 or end > len(data) or (end < len(data) and data[end] != 0):
            continue
        try:
            text = data[offset + 4:end].decode('utf-8')
        except UnicodeDecodeError:
            continue
        if text.strip() and all(ch.isprintable() or ch in '\r\n\t' for ch in text):
            found = text
    return clean(found.replace('\r\n', '\n').replace('\r', '\n'), 2000).strip() if found else ''


def read(data: bytes, budget: Budget | None = None) -> dict:
    from .project import build_project
    root = xmlsafe.parse(data)
    reader = _Reader(root, budget).run()
    clips = []
    for clip in reader.clips:
        clip = {k: v for k, v in clip.items() if not k.startswith('_') and k != 'transitionPower'}
        for key in ('start', 'duration', 'in', 'speed'):
            if key in clip:
                clip[key] = float(clip[key])
        clips.append(clip)
    return build_project(reader.name, reader.rate, reader.width, reader.height, list(reader.media.values()), clips,
                         [{'time': float(m['time']), 'name': m['name']} for m in reader.markers], reader.warnings)
