"""Synchronize picture with separately recorded sound (dual-system sound).

Waveform sync: both soundtracks are decoded to mono, band-limited (200 Hz to
3 kHz, where speech lives and where a camera's scratch track and a recorder
agree), and reduced to a 200 Hz onset envelope (log level minus its own
one-second average, so different gains and mics do not matter). The envelopes
are cross-correlated with an FFT over every lag the decoded spans allow; the
best lag is refined on the 8 kHz signals around the loudest passage to a
fraction of a sample. Confidence is the normalized peak height against the
best competing peak; a low-confidence match is refused, never applied
silently.

Drift: different sample clocks (a camera and a recorder) make the offset change
over a take. When the overlap is long enough the offset is measured near its
start and near its end; a difference over half a frame is reported as drift
(in ppm) and the sync is flagged.

Timecode sync: the picture's start timecode and rate (from the media probe)
against a BWF recorder file's time reference (samples since midnight).

Standard library only; every decode is bounded in time and size.
"""
import cmath
import math
import subprocess
from array import array

DECODE_RATE = 8000
ENVELOPE_RATE = 200
BLOCK = DECODE_RATE//ENVELOPE_RATE
MAX_PICTURE_SECONDS = 180
MAX_SEARCH_SECONDS = 600
DEFAULT_SEARCH_SECONDS = 60
REFINE_SECONDS = 4
REFINE_LAGS = 2*BLOCK        # +-10 ms at 8 kHz around the envelope match
MIN_CONFIDENCE = .35
MIN_PEAK_RATIO = 1.6
DECODE_TIMEOUT = 120
MAX_PAIRS = 40


def decode(path, seconds, stream=0, start=0.0):
    """Mono band-limited float samples at DECODE_RATE (at most ``seconds``)."""
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file',
               *(['-ss', f'{start:.6f}'] if start > 0 else []), '-t', f'{seconds:.6f}', '-i', str(path),
               '-map', f'0:a:{int(stream)}', '-ac', '1', '-af', 'highpass=f=200,lowpass=f=3000',
               '-ar', str(DECODE_RATE), '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1']
    result = subprocess.run(command, capture_output=True, timeout=DECODE_TIMEOUT)
    if result.returncode:
        raise ValueError('Could not decode audio for sync: ' + result.stderr.decode('utf-8', 'replace')[-300:])
    limit = int(seconds*DECODE_RATE+DECODE_RATE)*4
    samples = array('f')
    samples.frombytes(result.stdout[:limit - limit % 4])
    return samples


def envelope(samples):
    """200 Hz onset envelope: log block level minus its one-second running mean, unit variance."""
    levels = []
    for start in range(0, len(samples)-BLOCK+1, BLOCK):
        block = samples[start:start+BLOCK]
        energy = sum(value*value for value in block)/BLOCK
        levels.append(10*math.log10(energy+1e-10))
    window = ENVELOPE_RATE
    out, total, history = [], 0.0, []
    for value in levels:
        history.append(value); total += value
        if len(history) > window: total -= history.pop(0)
        out.append(max(0.0, value-total/len(history)))
    mean = sum(out)/len(out) if out else 0.0
    out = [value-mean for value in out]
    scale = math.sqrt(sum(value*value for value in out)/len(out)) if out else 0.0
    return [value/scale for value in out] if scale > 1e-9 else out


def _fft(values, inverse=False):
    """Iterative radix-2 FFT of a list of complex numbers (length a power of two)."""
    size = len(values)
    data = list(values)
    j = 0
    for i in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit; bit >>= 1
        j |= bit
        if i < j:
            data[i], data[j] = data[j], data[i]
    length = 2
    sign = 1 if inverse else -1
    while length <= size:
        step = cmath.exp(sign*2j*math.pi/length)
        half = length//2
        twiddles = [step**k for k in range(half)]
        for start in range(0, size, length):
            for k in range(half):
                a, b = data[start+k], data[start+k+half]*twiddles[k]
                data[start+k], data[start+k+half] = a+b, a-b
        length <<= 1
    if inverse:
        data = [value/size for value in data]
    return data


def cross_correlation(a, b):
    """c[L] = sum_n a[n]*b[n+L] for L in -(len(a)-1)..len(b)-1, returned as (lags, values)."""
    size = 1
    while size < len(a)+len(b):
        size <<= 1
    fa = _fft([complex(value) for value in a]+[0j]*(size-len(a)))
    fb = _fft([complex(value) for value in b]+[0j]*(size-len(b)))
    product = [x.conjugate()*y for x, y in zip(fa, fb)]
    full = [value.real for value in _fft(product, inverse=True)]
    lags = list(range(-(len(a)-1), len(b)))
    return lags, [full[lag % size] for lag in lags]


def _refine(pic, snd, coarse_offset, centre):
    """Offset (seconds) at sub-sample precision around ``coarse_offset``, using
    REFINE_SECONDS of picture sound around ``centre`` (seconds); None if out of range."""
    first = max(0, int((centre-REFINE_SECONDS/2)*DECODE_RATE))
    window = pic[first:first+REFINE_SECONDS*DECODE_RATE]
    base = first+round(coarse_offset*DECODE_RATE)
    if base-REFINE_LAGS < 0 or base+REFINE_LAGS+len(window) > len(snd) or len(window) < DECODE_RATE:
        return None, 0.0
    norm_a = math.sqrt(sum(value*value for value in window)) or 1e-9
    scores = []
    for lag in range(-REFINE_LAGS, REFINE_LAGS+1):
        segment = snd[base+lag:base+lag+len(window)]
        dot = sum(x*y for x, y in zip(window, segment))
        norm_b = math.sqrt(sum(value*value for value in segment)) or 1e-9
        scores.append(dot/(norm_a*norm_b))
    best = max(range(len(scores)), key=lambda index: scores[index])
    fraction = 0.0
    if 0 < best < len(scores)-1:
        left, middle, right = scores[best-1], scores[best], scores[best+1]
        denominator = left-2*middle+right
        if denominator < 0:
            fraction = .5*(left-right)/denominator
    lag = best-REFINE_LAGS+fraction
    return (base+lag-first)/DECODE_RATE, scores[best]


def _loudest(samples, begin, end):
    """Centre (seconds) of the loudest second between begin and end (seconds)."""
    best, at = -1.0, (begin+end)/2
    for start in range(int(begin), max(int(begin)+1, int(end-1))):
        block = samples[start*DECODE_RATE:(start+1)*DECODE_RATE]
        energy = sum(value*value for value in block[::4])
        if energy > best:
            best, at = energy, start+.5
    return at


def waveform_offset(picture_path, sound_path, *, picture_stream=0, sound_stream=0, picture_seconds=None,
                    search_seconds=DEFAULT_SEARCH_SECONDS, fps=24.0):
    """Offset of the sound file against the picture file's audio: a picture
    source time t plays the sound file at t + offset (seconds)."""
    search = min(MAX_SEARCH_SECONDS, max(1.0, float(search_seconds)))
    span = min(MAX_PICTURE_SECONDS, float(picture_seconds or MAX_PICTURE_SECONDS))
    pic = decode(picture_path, span, picture_stream)
    snd = decode(sound_path, span+search, sound_stream)
    if len(pic) < 2*DECODE_RATE or len(snd) < 2*DECODE_RATE:
        return dict(ok=False, method='waveform', confidence=0.0, note='Too little sound to compare (under 2 seconds).')
    env_pic, env_snd = envelope(pic), envelope(snd)
    lags, values = cross_correlation(env_pic, env_snd)
    allowed = [(lag, value) for lag, value in zip(lags, values) if -search*ENVELOPE_RATE <= lag <= search*ENVELOPE_RATE]
    best_lag, best = max(allowed, key=lambda row: row[1])
    guard = ENVELOPE_RATE//5
    competing = max((value for lag, value in allowed if abs(lag-best_lag) > guard), default=0.0)
    overlap = sum(1 for n in range(len(env_pic)) if 0 <= n+best_lag < len(env_snd))
    confidence = max(0.0, best/max(1, overlap))
    ratio = best/competing if competing > 1e-9 else math.inf
    coarse = best_lag/ENVELOPE_RATE
    # The overlap in picture seconds, for the refine windows and the drift check.
    begin = max(0.0, -coarse); end = min(len(pic)/DECODE_RATE, len(snd)/DECODE_RATE-coarse)
    offset, score = _refine(pic, snd, coarse, _loudest(pic, begin, begin+min(end-begin, 30)))
    result = dict(method='waveform', confidence=round(confidence, 3), peakRatio=round(min(ratio, 99), 2),
                  coarseOffsetSeconds=round(coarse, 4))
    if offset is None:
        offset, score = coarse, 0.0
    result.update(offsetSeconds=round(offset, 6), offsetSamples=round(offset*48000), offsetFrames=round(offset*fps, 2),
                  refineCorrelation=round(score, 3))
    if end-begin >= 40:
        late, _ = _refine(pic, snd, offset, _loudest(pic, end-20, end))
        if late is not None:
            drift = late-offset
            result['driftSeconds'] = round(drift, 6)
            result['driftPpm'] = round(drift/max(1e-9, end-begin-20)*1e6, 1)
            if abs(drift) > .5/fps:
                result['drift'] = True
    ok = confidence >= MIN_CONFIDENCE and ratio >= MIN_PEAK_RATIO
    result['ok'] = ok and not result.get('drift')
    if not ok:
        result['note'] = (f'Low confidence ({confidence:.2f}, peak ratio {min(ratio, 99):.1f}): these recordings may not be '
                          'the same take. Not synced.')
    elif result.get('drift'):
        result['note'] = (f"The two recordings drift apart by {result['driftSeconds']*1000:.1f} ms over the take "
                          f"({result['driftPpm']:+.0f} ppm, different sample clocks). Synced at the start only if you choose to.")
    return result


def timecode_seconds(text, rate):
    """Seconds since midnight of an SMPTE timecode label at ``rate`` (drop-frame aware)."""
    import re
    match = re.fullmatch(r'(\d{2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})', str(text or ''))
    if not match or not rate:
        return None
    hours, minutes, seconds, frames = (int(match.group(index)) for index in (1, 2, 3, 5))
    nominal = round(rate)
    count = ((hours*60+minutes)*60+seconds)*nominal+frames
    if match.group(4) in ';,' and nominal in (30, 60):
        dropped = 2 if nominal == 30 else 4
        total_minutes = hours*60+minutes
        count -= dropped*(total_minutes-total_minutes//10)
    return count/rate


def timecode_offset(picture_info, sound_info, fps=24.0):
    """Offset from the picture's start timecode and the sound file's BWF time reference."""
    rate = picture_info.get('rate')
    if isinstance(rate, dict):
        rate = rate.get('num', 0)/max(1, rate.get('den', 1))
    elif isinstance(rate, str) and '/' in rate:
        num, den = rate.split('/'); rate = float(num)/float(den)
    picture = timecode_seconds(picture_info.get('timecode'), float(rate or picture_info.get('fps') or 0))
    reference, sample_rate = sound_info.get('timeReference'), sound_info.get('audioSampleRate')
    if picture is None or reference is None or not sample_rate:
        return dict(ok=False, method='timecode', note='Timecode sync needs a picture start timecode and a BWF time reference '
                    'in the sound file.')
    offset = picture-reference/sample_rate
    return dict(ok=True, method='timecode', confidence=1.0, offsetSeconds=round(offset, 6), offsetSamples=round(offset*48000),
                offsetFrames=round(offset*fps, 2))
