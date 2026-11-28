"""Exact frame rates, SMPTE timecode and FCPXML rational time, with no float arithmetic.

Ported from awtprod/ai-agent-resolve ``origin/dev`` at aff6573 (``src/aar/media/probe.py`` and
``src/aar/plan/export/timebase.py``), whose FCPXML output was imported into DaVinci Resolve 21.1.
What those import runs taught:

* camera files report averaged rates such as ``2953125/98536``; they are snapped to the standard
  rate within 0.1 % (:func:`snap_rate`) and written as exact rationals (one 23.976 frame is
  ``1001/24000s``, never ``0.0417s``);
* drop-frame timecode exists only at 29.97 and 59.94; it skips frame numbers 0-1 (0-3 at 59.94)
  at the start of every minute except each tenth.
"""
from __future__ import annotations

import re
from fractions import Fraction

STANDARD_RATES = tuple(Fraction(n, d) for n, d in (
    (24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1),
    (48000, 1001), (48, 1), (50, 1), (60000, 1001), (60, 1)))
DROP_FRAME_RATES = (Fraction(30000, 1001), Fraction(60000, 1001))
SNAP_TOLERANCE = Fraction(1, 1000)
#: Timecode labels stop at 24 hours; nothing in an edit decision list may pass that.
DAY_SECONDS = 86400

_TIMECODE = re.compile(r'(\d{2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})', re.ASCII)
_TIME = re.compile(r'(-?)(0|[1-9][0-9]{0,18})(?:/([1-9][0-9]{0,9}))?s', re.ASCII)


def parse_rate(value) -> Fraction | None:
    """A rate from ``"24000/1001"``, ``"23.976"``, a number or a Fraction; None when absent or
    not positive. Never raises for junk: callers decide what an unknown rate means."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text or len(text) > 32:
                return None
            if '/' in text:
                num, den = text.split('/', 1)
                rate = Fraction(int(num), int(den))
            else:
                rate = Fraction(text)
        else:
            rate = Fraction(value)
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None
    return rate if 0 < rate <= 1000 else None


def snap_rate(value) -> Fraction | None:
    """The standard rate nearest ``value`` within 0.1 %, else None. ``2953125/98536`` and
    ``29.97`` snap to ``30000/1001``; ``12.5`` and ``120`` snap to nothing."""
    rate = parse_rate(value)
    if rate is None:
        return None
    nearest = min(STANDARD_RATES, key=lambda standard: abs(rate - standard))
    return nearest if abs(rate - nearest) <= nearest * SNAP_TOLERANCE else None


def sequence_rate(value) -> Fraction:
    """A timeline rate: a standard rate, or a whole number of frames per second (1-120).
    Anything else cannot be written exactly in an EDL or FCPXML and is refused."""
    snapped = snap_rate(value)
    if snapped is not None:
        return snapped
    rate = parse_rate(value)
    if rate is not None and abs(rate - round(rate)) < Fraction(1, 10000) and 1 <= round(rate) <= 120:
        return Fraction(round(rate))
    raise ValueError(f'Timeline rate {value!r} is not a standard or whole frame rate')


def rate_text(rate: Fraction) -> str:
    return f'{rate.numerator}/{rate.denominator}' if rate.denominator != 1 else str(rate.numerator)


def nominal(rate: Fraction) -> int:
    """Whole frames per timecode second: 24 for 23.976, 30 for 29.97."""
    value = round(rate)
    if value < 1:
        raise ValueError(f'{rate} fps rounds to no frames a second')
    return value


def is_drop_frame(text: str) -> bool:
    match = _TIMECODE.fullmatch(str(text).strip())
    return match is not None and match.group(4) != ':'


def timecode_frames(text: str, rate: Fraction, drop: bool | None = None) -> int:
    """A SMPTE label as a frame count from 00:00:00:00 at ``rate``.

    ``HH:MM:SS:FF`` is non-drop; ``;``, ``.`` or ``,`` before the frames is drop-frame, unless
    ``drop`` forces one reading (an EDL's FCM line). Raises ``ValueError`` naming the problem."""
    match = _TIMECODE.fullmatch(str(text).strip())
    if match is None:
        raise ValueError(f'{text!r} is not a HH:MM:SS:FF timecode')
    hours, minutes, seconds, frame = (int(match.group(n)) for n in (1, 2, 3, 5))
    count_per_second = nominal(rate)
    for value, limit, unit in ((hours, 24, 'hour'), (minutes, 60, 'minute'),
                               (seconds, 60, 'second'), (frame, count_per_second, 'frame')):
        if value >= limit:
            raise ValueError(f'{text!r} has {unit} {value}; the limit is {limit - 1}')
    count = ((hours * 60 + minutes) * 60 + seconds) * count_per_second + frame
    dropped = match.group(4) != ':' if drop is None else drop
    if not dropped:
        return count
    if rate not in DROP_FRAME_RATES:
        raise ValueError(f'{text!r} is drop-frame, which exists only at 29.97 and 59.94 fps')
    skip = count_per_second // 15
    if seconds == 0 and frame < skip and minutes % 10:
        raise ValueError(f'{text!r} names a frame number drop-frame timecode skips')
    total_minutes = hours * 60 + minutes
    return count - skip * (total_minutes - total_minutes // 10)


def frames_timecode(frames: int, rate: Fraction, drop: bool = False) -> str:
    """The SMPTE label of frame ``frames`` counted from 00:00:00:00, the inverse of
    :func:`timecode_frames`. Refuses negative counts and labels at or past 24 hours."""
    if frames < 0:
        raise ValueError(f'frame {frames} is before 00:00:00:00')
    count_per_second = nominal(rate)
    separator = ':'
    if drop:
        if rate not in DROP_FRAME_RATES:
            raise ValueError('drop-frame exists only at 29.97 and 59.94 fps')
        skip = count_per_second // 15
        tens, rest = divmod(frames, 600 * count_per_second - 9 * skip)
        frames += 9 * skip * tens
        if rest > skip:
            frames += skip * ((rest - skip) // (60 * count_per_second - skip))
        separator = ';'
    seconds, frame = divmod(frames, count_per_second)
    if seconds >= DAY_SECONDS:
        raise ValueError('timecode passes 23:59:59:xx')
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}{separator}{frame:02d}'


def seconds_frames(seconds, rate: Fraction) -> int:
    """Nearest whole frame for a time in seconds (Splice stores float seconds)."""
    value = seconds if isinstance(seconds, Fraction) else Fraction(repr(float(seconds)))
    return round(value * rate)


def fcpxml_time(seconds: Fraction, rate: Fraction | None = None) -> str:
    """An exact FCPXML time value. On ``rate``'s frame grid it is written over the frame
    duration's denominator the way Final Cut Pro writes it (``24024/24000s``); whole seconds
    are ``Ns``. Negative times are refused: nothing here writes one."""
    seconds = Fraction(seconds)
    if seconds < 0:
        raise ValueError(f'FCPXML time {seconds} is negative')
    if seconds.denominator == 1:
        return f'{seconds.numerator}s'
    if rate is not None:
        step = 1 / Fraction(rate)
        frames = seconds / step
        if frames.denominator == 1:
            return f'{frames.numerator * step.numerator}/{step.denominator}s'
    return f'{seconds.numerator}/{seconds.denominator}s'


def parse_fcpxml_time(value: str) -> Fraction:
    """``N/Ds`` or ``Ns`` as exact seconds; decimals and huge numbers are refused."""
    match = _TIME.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f'{value!r} is not an FCPXML rational time such as 1001/24000s')
    result = Fraction(int(match.group(2)), int(match.group(3) or 1))
    return -result if match.group(1) else result


def convert_label(frames: int, from_rate: Fraction, from_drop: bool, to_rate: Fraction, to_drop: bool) -> int:
    """Frame ``frames`` (a timecode at ``from_rate``) as the same label read at ``to_rate``: hours,
    minutes and seconds are kept and the frame digits scaled, the way an edit system shows a
    29.97 source's timecode in a 23.976 list. A label drop-frame skips moves to the next valid one."""
    if from_rate == to_rate and from_drop == to_drop:
        return frames
    label = frames_timecode(frames, from_rate, from_drop)
    hours, minutes, seconds, frame = (int(part) for part in re.split(r'[:;]', label))
    top = nominal(to_rate)
    frame = min(top - 1, round(Fraction(frame * top, nominal(from_rate))))
    if to_drop:
        skip = top // 15
        if seconds == 0 and frame < skip and minutes % 10:
            frame = skip
    separator = ';' if to_drop else ':'
    return timecode_frames(f'{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{frame:02d}', to_rate, to_drop)
