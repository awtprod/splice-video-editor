"""Audio processing settings shared by the editor and the renderer.

One implementation of each formula lives here (Python side); audio-dsp.js
holds the browser's copy and tests/test_audio_parity.py proves they agree.

* Parametric EQ: RBJ "Audio EQ Cookbook" biquads. The renderer feeds the
  exact coefficients to FFmpeg's ``biquad`` filter (64-bit), the preview feeds
  the same coefficients to Web Audio IIRFilterNodes, so both run the same
  difference equation.
* Compressor and gate: FFmpeg's ``acompressor`` and ``agate`` (export, the
  authority). The preview runs a port of the same per-sample algorithm in an
  AudioWorklet (audio-dsp.js); the measured tolerance is in the tests.
* De-noise: FFmpeg ``afftdn`` (export only; the preview plays it bypassed and
  says so).

Settings shape (clip ``audioFx``, track ``trackMix[n].fx``, ``masterFx``)::

    {"eq": {"enabled": true, "bands": [{"type": "highpass", "freq": 80, "q": 0.707, "gain": 0}, ...]},
     "comp": {"enabled": true, "threshold": -18, "ratio": 3, "attack": 10, "release": 120, "knee": 6, "makeup": 2},
     "gate": {"enabled": true, "threshold": -50, "ratio": 4, "attack": 2, "release": 120, "range": -24},
     "denoise": {"enabled": true, "amount": 12, "floor": -50}}

Every value is validated with its unit in the error message.
"""
import math

EQ_TYPES = ('highpass', 'lowpass', 'lowshelf', 'highshelf', 'peak')
MAX_EQ_BANDS = 8
# Bands above this fraction of the sample rate are moved down to it (the
# cookbook formulas are undefined at Nyquist).
NYQUIST_GUARD = .49
# acompressor/agate knee: FFmpeg takes the knee as a level ratio (1..8).
MAX_KNEE_DB = 20*math.log10(8)
FX_KINDS = ('eq', 'comp', 'gate', 'denoise')
MASTER_KINDS = ('eq', 'comp')


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Invalid {label}: use a number from {low:g} to {high:g}')
    return float(value)


def _enabled(section, label):
    if not isinstance(section, dict):
        raise ValueError(f'Invalid {label} settings')
    enabled = section.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError(f'Invalid {label} setting: enabled must be true or false')
    return enabled


def biquad(kind, freq, gain_db, q, rate):
    """Normalized RBJ cookbook coefficients (b0, b1, b2, a1, a2) with a0 = 1."""
    freq = min(freq, NYQUIST_GUARD*rate)
    w0 = 2*math.pi*freq/rate
    cosw, sinw = math.cos(w0), math.sin(w0)
    alpha = sinw/(2*q)
    amp = 10**(gain_db/40)
    if kind == 'lowpass':
        b = ((1-cosw)/2, 1-cosw, (1-cosw)/2); a = (1+alpha, -2*cosw, 1-alpha)
    elif kind == 'highpass':
        b = ((1+cosw)/2, -(1+cosw), (1+cosw)/2); a = (1+alpha, -2*cosw, 1-alpha)
    elif kind == 'peak':
        b = (1+alpha*amp, -2*cosw, 1-alpha*amp); a = (1+alpha/amp, -2*cosw, 1-alpha/amp)
    elif kind == 'lowshelf':
        root = 2*math.sqrt(amp)*alpha
        b = (amp*((amp+1)-(amp-1)*cosw+root), 2*amp*((amp-1)-(amp+1)*cosw), amp*((amp+1)-(amp-1)*cosw-root))
        a = ((amp+1)+(amp-1)*cosw+root, -2*((amp-1)+(amp+1)*cosw), (amp+1)+(amp-1)*cosw-root)
    elif kind == 'highshelf':
        root = 2*math.sqrt(amp)*alpha
        b = (amp*((amp+1)+(amp-1)*cosw+root), -2*amp*((amp-1)+(amp+1)*cosw), amp*((amp+1)+(amp-1)*cosw-root))
        a = ((amp+1)-(amp-1)*cosw+root, 2*((amp-1)-(amp+1)*cosw), (amp+1)-(amp-1)*cosw-root)
    else:
        raise ValueError('Invalid EQ band type')
    return (b[0]/a[0], b[1]/a[0], b[2]/a[0], a[1]/a[0], a[2]/a[0])


def magnitude_db(sections, freq, rate):
    """|H(e^jw)| of a cascade of sections in dB (for tests and the UI curve)."""
    w = 2*math.pi*freq/rate
    z1, z2 = complex(math.cos(-w), math.sin(-w)), complex(math.cos(-2*w), math.sin(-2*w))
    total = 1.0
    for b0, b1, b2, a1, a2 in sections:
        total *= abs((b0+b1*z1+b2*z2)/(1+a1*z1+a2*z2))
    return 20*math.log10(max(total, 1e-30))


def eq_bands(eq):
    """Validated, enabled EQ bands as (type, freq, gain dB, q)."""
    if not _enabled(eq, 'EQ'):
        return []
    bands = eq.get('bands', [])
    if not isinstance(bands, list) or len(bands) > MAX_EQ_BANDS:
        raise ValueError(f'Invalid EQ: use at most {MAX_EQ_BANDS} bands')
    result = []
    for band in bands:
        if not isinstance(band, dict) or band.get('type') not in EQ_TYPES:
            raise ValueError('Invalid EQ band: type must be ' + ', '.join(EQ_TYPES))
        if band.get('enabled', True) is False:
            continue
        if not isinstance(band.get('enabled', True), bool):
            raise ValueError('Invalid EQ band: enabled must be true or false')
        freq = _number(band.get('freq'), 'EQ frequency (Hz)', 20, 20000)
        q = _number(band.get('q', .7071), 'EQ Q', .1, 18)
        gain = _number(band.get('gain', 0), 'EQ gain (dB)', -24, 24)
        if band['type'] in ('highpass', 'lowpass'):
            gain = 0.0
        elif abs(gain) < 1e-9:
            continue  # a 0 dB bell or shelf is the identity
        result.append((band['type'], freq, gain, q))
    return result


def eq_sections(eq, rate):
    return [biquad(kind, freq, gain, q, rate) for kind, freq, gain, q in eq_bands(eq)]


def compressor(comp):
    """Validated compressor settings, or None when off."""
    if not _enabled(comp, 'compressor'):
        return None
    return dict(threshold=_number(comp.get('threshold', -18), 'compressor threshold (dBFS)', -60, 0),
                ratio=_number(comp.get('ratio', 3), 'compressor ratio', 1, 20),
                attack=_number(comp.get('attack', 10), 'compressor attack (ms)', .01, 2000),
                release=_number(comp.get('release', 120), 'compressor release (ms)', .01, 9000),
                knee=_number(comp.get('knee', 6), 'compressor knee (dB)', 0, MAX_KNEE_DB),
                makeup=_number(comp.get('makeup', 0), 'compressor make-up gain (dB)', 0, 36))


def gate(settings):
    if not _enabled(settings, 'gate'):
        return None
    return dict(threshold=_number(settings.get('threshold', -50), 'gate threshold (dBFS)', -80, 0),
                ratio=_number(settings.get('ratio', 4), 'gate ratio', 1, 9000),
                attack=_number(settings.get('attack', 2), 'gate attack (ms)', .01, 9000),
                release=_number(settings.get('release', 120), 'gate release (ms)', .01, 9000),
                range=_number(settings.get('range', -24), 'gate range (dB)', -90, 0))


def denoise(settings):
    if not _enabled(settings, 'de-noise'):
        return None
    return dict(amount=_number(settings.get('amount', 12), 'de-noise amount (dB)', 1, 40),
                floor=_number(settings.get('floor', -50), 'de-noise noise floor (dBFS)', -80, -20))


GATE_KNEE = 2.828427125


def normalize(fx, kinds=FX_KINDS, label='audio effects'):
    """Validated settings: {'eq': [...bands], 'comp': {...}|None, 'gate': ..., 'denoise': ...}."""
    if fx is None:
        return {}
    if not isinstance(fx, dict) or any(key not in kinds for key in fx):
        raise ValueError(f'Invalid {label}: use ' + ', '.join(kinds))
    result = {}
    if 'denoise' in fx and (value := denoise(fx['denoise'])):
        result['denoise'] = value
    if 'gate' in fx and (value := gate(fx['gate'])):
        result['gate'] = value
    if 'eq' in fx and (value := eq_bands(fx['eq'])):
        result['eq'] = value
    if 'comp' in fx and (value := compressor(fx['comp'])):
        result['comp'] = value
    return result


def has_dynamics(normalized):
    return any(key in normalized for key in ('comp', 'gate', 'denoise'))


def ffmpeg_filters(normalized, rate=48000):
    """FFmpeg filters for validated settings, in processing order:
    de-noise, gate, EQ, compressor (the order the preview uses)."""
    filters = []
    if 'denoise' in normalized:
        value = normalized['denoise']
        filters.append(f"afftdn=nr={value['amount']:.3f}:nf={value['floor']:.3f}:tn=1")
    if 'gate' in normalized:
        value = normalized['gate']
        filters.append(f"agate=threshold={10**(value['threshold']/20):.9f}:ratio={value['ratio']:.6f}:"
                       f"attack={value['attack']:.6f}:release={value['release']:.6f}:range={10**(value['range']/20):.9f}:"
                       f'knee={GATE_KNEE}:detection=peak:link=maximum')
    for kind, freq, gain, q in normalized.get('eq', []):
        b0, b1, b2, a1, a2 = biquad(kind, freq, gain, q, rate)
        filters.append(f'biquad=b0={b0!r}:b1={b1!r}:b2={b2!r}:a0=1:a1={a1!r}:a2={a2!r}:precision=f64')
    if 'comp' in normalized:
        value = normalized['comp']
        filters.append(f"acompressor=threshold={max(10**(value['threshold']/20), 0.000976563):.9f}:ratio={value['ratio']:.6f}:"
                       f"attack={value['attack']:.6f}:release={value['release']:.6f}:"
                       f"knee={min(8.0, max(1.0, 10**(value['knee']/20))):.9f}:makeup={10**(value['makeup']/20):.9f}:"
                       'detection=peak:link=maximum')
    return filters


def describe(normalized):
    """Plain words for the interchange loss report."""
    parts = []
    if 'denoise' in normalized: parts.append(f"de-noise {normalized['denoise']['amount']:g} dB")
    if 'gate' in normalized: parts.append(f"gate {normalized['gate']['threshold']:g} dBFS")
    if 'eq' in normalized: parts.append(f"EQ ({len(normalized['eq'])} band{'s' if len(normalized['eq']) != 1 else ''})")
    if 'comp' in normalized: parts.append(f"compressor {normalized['comp']['threshold']:g} dBFS {normalized['comp']['ratio']:g}:1")
    return ', '.join(parts)
