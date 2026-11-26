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

from media_exec import run_bounded

DECODE_RATE = 8000
ENVELOPE_RATE = 200
BLOCK = DECODE_RATE//ENVELOPE_RATE
MAX_PICTURE_SECONDS = 180
MAX_SEARCH_SECONDS = 600
DEFAULT_SEARCH_SECONDS = 60
REFINE_SECONDS = 4
REFINE_LAGS = 2*BLOCK        # +-10 ms at 8 kHz around the envelope match
MIN_CONFIDENCE = .35
DRIFT_PPM = 100
MIN_PEAK_RATIO = 1.6
DECODE_TIMEOUT = 120
MAX_PAIRS = 40


def decode(path, seconds, stream=0, start=0.0):
    """Mono band-limited float samples at DECODE_RATE (at most ``seconds``)."""
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file',
               *(['-ss', f'{start:.6f}'] if start > 0 else []), '-t', f'{seconds:.6f}', '-i', str(path),
               '-map', f'0:a:{int(stream)}', '-ac', '1', '-af', 'highpass=f=200,lowpass=f=3000',
               '-ar', str(DECODE_RATE), '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1']
    limit = int(seconds*DECODE_RATE+DECODE_RATE)*4
    try:
        result = run_bounded(command, timeout=DECODE_TIMEOUT, max_output=limit)
    except subprocess.TimeoutExpired as error:
        raise ValueError('The audio decoder timed out during sync') from error
    if result.returncode:
        raise ValueError('Could not decode audio for sync: ' + result.stderr.decode('utf-8', 'replace')[-300:])
    samples = array('f')
    samples.frombytes(result.stdout)
    return samples


def envelope(samples):
    """200 Hz onset envelope: log block level minus its one-second running mean, unit variance."""
    levels = []
    for start in range(0, len(samples)-BLOCK+1, BLOCK):
        block = samples[start:start+BLOCK]
        energy = sum(value*value for value in block)/BLOCK
        levels.append(10*math.log10(energy+1e-10))
    if levels:
        # Silence is as quiet as the recording's own noise floor allows: floor every level 40 dB
        # under the loud passages, so a recorder's digital silence and a camera's hiss read alike.
        floor = sorted(levels)[int(.95*(len(levels)-1))]-40
        levels = [max(level, floor) for level in levels]
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


def _refine(pic, snd, coarse_offset, centre, begin=0.0, end=math.inf):
    """Offset (seconds) at sub-sample precision around ``coarse_offset``, using up to
    REFINE_SECONDS of picture sound around ``centre``, inside the overlap begin..end
    (picture seconds); None when there is not half a second to compare."""
    margin = (REFINE_LAGS+1)/DECODE_RATE
    low = max(0.0, begin+margin, centre-REFINE_SECONDS/2)
    high = min(len(pic)/DECODE_RATE, end-margin, low+REFINE_SECONDS)
    first, last = int(low*DECODE_RATE), int(high*DECODE_RATE)
    window = pic[first:last]
    base = first+round(coarse_offset*DECODE_RATE)
    if base-REFINE_LAGS < 0 or base+REFINE_LAGS+len(window) > len(snd) or len(window) < DECODE_RATE//2:
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
    # Confidence: the envelopes' correlation coefficient over their overlap at the best lag.
    pairs = [(env_pic[n], env_snd[n+best_lag]) for n in range(len(env_pic)) if 0 <= n+best_lag < len(env_snd)]
    confidence = 0.0
    if len(pairs) > ENVELOPE_RATE:
        mean_a = sum(a for a, _ in pairs)/len(pairs); mean_b = sum(b for _, b in pairs)/len(pairs)
        covariance = sum((a-mean_a)*(b-mean_b) for a, b in pairs)
        spread = math.sqrt(sum((a-mean_a)**2 for a, _ in pairs)*sum((b-mean_b)**2 for _, b in pairs))
        confidence = max(0.0, covariance/spread) if spread > 1e-12 else 0.0
    ratio = best/competing if competing > 1e-9 else math.inf
    coarse = best_lag/ENVELOPE_RATE
    # The overlap in picture seconds, for the refine windows and the drift check.
    begin = max(0.0, -coarse); end = min(len(pic)/DECODE_RATE, len(snd)/DECODE_RATE-coarse)
    offset, score = _refine(pic, snd, coarse, _loudest(pic, begin, begin+min(end-begin, 30)), begin, end)
    result = dict(method='waveform', confidence=round(confidence, 3), peakRatio=round(min(ratio, 99), 2),
                  coarseOffsetSeconds=round(coarse, 4))
    if offset is None:
        offset, score = coarse, 0.0
    result.update(offsetSeconds=round(offset, 6), offsetSamples=round(offset*48000), offsetFrames=round(offset*fps, 2),
                  refineCorrelation=round(score, 3))
    if end-begin >= 40:
        late, _ = _refine(pic, snd, offset, _loudest(pic, end-20, end), begin, end)
        if late is not None:
            drift = late-offset
            result['driftSeconds'] = round(drift, 6)
            ppm = drift/max(1e-9, end-begin-20)*1e6
            result['driftPpm'] = round(ppm, 1)
            # Flag a clock difference beyond what two good crystals show (DRIFT_PPM), or one
            # that would move the sound a quarter frame over the take.
            if abs(ppm) > DRIFT_PPM or abs(ppm)*1e-6*(end-begin) > .25/fps:
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
    if not match or not math.isfinite(rate) or not 1 <= rate <= 1000:
        return None
    hours, minutes, seconds, frames = (int(match.group(index)) for index in (1, 2, 3, 5))
    nominal = round(rate)
    if hours >= 24 or minutes >= 60 or seconds >= 60 or frames >= nominal:
        return None
    count = ((hours*60+minutes)*60+seconds)*nominal+frames
    if match.group(4) in ';,':
        if nominal not in (30, 60) or abs(rate-nominal*1000/1001) > .001:
            return None
        dropped = 2 if nominal == 30 else 4
        if minutes % 10 and seconds == 0 and frames < dropped:
            return None
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


def sync_pairs(payload):
    """The /api/audio/sync request: {pairs: [{picture, sound, pictureStream?, soundStream?, pictureSeconds?}],
    method: auto|waveform|timecode, fps, searchSeconds}. ``picture`` and ``sound`` are media srcs."""
    from media_proxy import describe
    from premiere import _media_path
    pairs = payload.get('pairs')
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= MAX_PAIRS:
        raise ValueError(f'Sync takes 1 to {MAX_PAIRS} picture and sound pairs')
    method = payload.get('method', 'auto')
    if method not in ('auto', 'waveform', 'timecode'):
        raise ValueError('Invalid sync method: use auto, waveform or timecode')
    fps = payload.get('fps', 24)
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 1 <= fps <= 240:
        raise ValueError('Invalid sync frame rate')
    search = payload.get('searchSeconds', DEFAULT_SEARCH_SECONDS)
    if isinstance(search, bool) or not isinstance(search, (int, float)) or not 1 <= search <= MAX_SEARCH_SECONDS:
        raise ValueError(f'Invalid sync search range: use 1 to {MAX_SEARCH_SECONDS} seconds')
    results = []
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(pair.get('picture'), str) or not isinstance(pair.get('sound'), str):
            raise ValueError('Each sync pair names a picture and a sound source')
        streams = [pair.get('pictureStream', 0), pair.get('soundStream', 0)]
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 64 for value in streams):
            raise ValueError('Invalid sync audio stream')
        seconds = pair.get('pictureSeconds')
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 86400):
            raise ValueError('Invalid picture duration for sync')
        result = None
        if method in ('auto', 'timecode'):
            result = timecode_offset(describe(pair['picture']), describe(pair['sound']), fps)
            if method == 'timecode' or result['ok']:
                results.append(result)
                continue
        result = waveform_offset(_media_path(pair['picture']), _media_path(pair['sound']), picture_stream=streams[0],
                                 sound_stream=streams[1], picture_seconds=seconds, search_seconds=search, fps=fps)
        results.append(result)
    return results
