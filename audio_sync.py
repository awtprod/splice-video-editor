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
start and near its end with bounded local envelope tracking across the real
separation between active windows; a rate over 100 ppm or a shift over a
quarter frame across the take is reported as drift (in ppm) and flagged.

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
DRIFT_MIN_OVERLAP = 40
DRIFT_MIN_SEPARATION = 15
DRIFT_ENV_SECONDS = 8
DRIFT_FINE_STEP = 8          # 1 ms step (1000 Hz) at 8 kHz
DRIFT_FINE_SPAN = 80         # 10 ms symmetric RMS window to suppress >200 Hz carrier ripple
DRIFT_FINE_LAGS = 15         # +-15 ms fine envelope search around the 200 Hz local peak
MAX_DRIFT_RATE = .003        # bounded +-3000 ppm local envelope search for drifting clocks
MAX_DRIFT_SEARCH = .5        # cap local drift lag search at +-500 ms
MIN_LOCAL_CORRELATION = .25
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


def _local_envelope_offset(env_pic, env_snd, reference_offset, centre, max_shift):
    """Local offset (seconds) and Pearson correlation of the 200 Hz onset envelopes around
    ``centre`` within ``reference_offset +- max_shift``."""
    ref_lag = round(reference_offset*ENVELOPE_RATE)
    max_lag = max(2, int(math.ceil(max_shift*ENVELOPE_RATE)))
    min_start = max(0, max_lag-ref_lag)
    max_stop = min(len(env_pic), len(env_snd)-max_lag-ref_lag)
    if max_stop-min_start < ENVELOPE_RATE:
        return reference_offset, 0.0
    width = min(DRIFT_ENV_SECONDS*ENVELOPE_RATE, max_stop-min_start)
    start = max(min_start, min(max_stop-width, round(centre*ENVELOPE_RATE)-width//2))
    a = env_pic[start:start+width]
    mean_a = sum(a)/width
    a = [value-mean_a for value in a]
    norm_a = math.sqrt(sum(value*value for value in a))
    if norm_a < 1e-9:
        return reference_offset, 0.0
    base = start+ref_lag
    best_lag, best_score = 0, -2.0
    for lag in range(-max_lag, max_lag+1):
        b = env_snd[base+lag:base+lag+width]
        mean_b = sum(b)/width
        dot = sum(x*(y-mean_b) for x, y in zip(a, b))
        norm_b = math.sqrt(sum((y-mean_b)**2 for y in b))
        score = dot/(norm_a*norm_b) if norm_b > 1e-9 else 0.0
        if score > best_score:
            best_score, best_lag = score, lag
    if best_score < MIN_LOCAL_CORRELATION:
        return reference_offset, max(0.0, best_score)
    return (base+best_lag-start)/ENVELOPE_RATE, best_score


def _smooth_log_envelope(samples, start_idx, end_idx):
    """1000 Hz zero-mean log-RMS envelope computed with a symmetric 10 ms window via prefix sums.
    Suppresses >200 Hz carrier ripple so drifting tonal signals never cycle-slip."""
    half = DRIFT_FINE_SPAN//2
    p0 = max(0, start_idx-half)
    p1 = min(len(samples), end_idx+half)
    prefix = [0.0]
    acc = 0.0
    for value in samples[p0:p1]:
        acc += value*value
        prefix.append(acc)
    levels = []
    for index in range(start_idx, end_idx, DRIFT_FINE_STEP):
        lo = max(0, index-half-p0)
        hi = min(len(prefix)-1, index+half-p0)
        energy = (prefix[hi]-prefix[lo])/max(1, hi-lo)
        levels.append(10*math.log10(energy+1e-10))
    if not levels:
        return []
    floor = sorted(levels)[int(.95*(len(levels)-1))]-40
    levels = [max(value, floor) for value in levels]
    mean = sum(levels)/len(levels)
    return [value-mean for value in levels]


def _local_drift_offset(pic, snd, env_pic, env_snd, reference_offset, centre, max_shift):
    """Local offset (seconds), fine envelope correlation, and energy-weighted window centre
    (picture seconds). Uses bounded 200 Hz envelope search followed by 1000 Hz symmetric
    log-RMS correlation so clock drift past +-10 ms is tracked without carrier cycle-slipping."""
    coarse_local, env_score = _local_envelope_offset(env_pic, env_snd, reference_offset, centre, max_shift)
    if env_score < MIN_LOCAL_CORRELATION:
        return None, 0.0, None
    rate = DECODE_RATE//DRIFT_FINE_STEP
    half = DRIFT_FINE_SPAN//2
    pad_samples = DRIFT_FINE_LAGS*DRIFT_FINE_STEP
    shift = round(coarse_local*rate)*DRIFT_FINE_STEP
    min_first = max(half, pad_samples+half-shift)
    max_last = min(len(pic)-half, len(snd)-pad_samples-half-shift)
    if max_last-min_first < DECODE_RATE:
        return None, 0.0, None
    target_width = min(DRIFT_ENV_SECONDS*DECODE_RATE, max_last-min_first)
    first = max(min_first, min(max_last-target_width, round(centre*DECODE_RATE)-target_width//2))
    first -= first % DRIFT_FINE_STEP
    if first < min_first:
        first += DRIFT_FINE_STEP
    width = min(target_width, max_last-first)
    width -= width % DRIFT_FINE_STEP
    if width < DECODE_RATE:
        return None, 0.0, None
    last = first+width
    base = first+shift
    ea = _smooth_log_envelope(pic, first, last)
    eb_full = _smooth_log_envelope(snd, base-pad_samples, base+width+pad_samples)
    count = len(ea)
    if count < rate//2 or len(eb_full) < count+2*DRIFT_FINE_LAGS:
        return None, 0.0, None
    norm_a = math.sqrt(sum(value*value for value in ea))
    if norm_a < 1e-9:
        return None, 0.0, None
    scores = []
    for k in range(2*DRIFT_FINE_LAGS+1):
        b = eb_full[k:k+count]
        mean_b = sum(b)/count
        dot = sum(x*(y-mean_b) for x, y in zip(ea, b))
        norm_b = math.sqrt(sum((y-mean_b)**2 for y in b))
        scores.append(dot/(norm_a*norm_b) if norm_b > 1e-9 else 0.0)
    best = max(range(len(scores)), key=lambda index: scores[index])
    fraction = 0.0
    if 0 < best < len(scores)-1:
        left, middle, right = scores[best-1], scores[best], scores[best+1]
        denominator = left-2*middle+right
        if denominator < 0:
            fraction = .5*(left-right)/denominator
    lag_steps = best-DRIFT_FINE_LAGS+fraction
    energy_sum = sum(value*value for value in ea if value > 0)
    if energy_sum > 0:
        weighted = sum((index+.5)*value*value for index, value in enumerate(ea) if value > 0)/energy_sum
        actual_centre = first/DECODE_RATE+weighted/rate
    else:
        actual_centre = (first+last)/(2*DECODE_RATE)
    return shift/DECODE_RATE+lag_steps/rate, scores[best], actual_centre


def _refine(pic, snd, coarse_offset, centre, begin=0.0, end=math.inf):
    """Offset (seconds), correlation score, and actual window centre (picture seconds) at
    sub-sample precision around ``coarse_offset``, using up to REFINE_SECONDS of picture
    sound around ``centre``, inside the overlap begin..end (picture seconds); (None, 0.0, None)
    when there is not half a second to compare."""
    shift = round(coarse_offset*DECODE_RATE)
    min_first = max(0, int(math.ceil(begin*DECODE_RATE)), REFINE_LAGS-shift)
    max_last = min(len(pic), int(math.floor(end*DECODE_RATE)) if math.isfinite(end) else len(pic),
                   len(snd)-REFINE_LAGS-shift)
    if max_last-min_first < DECODE_RATE//2:
        return None, 0.0, None
    width = min(REFINE_SECONDS*DECODE_RATE, max_last-min_first)
    first = max(min_first, min(max_last-width, round(centre*DECODE_RATE)-width//2))
    last = first+width
    window = pic[first:last]
    base = first+shift
    norm_a = math.sqrt(sum(value*value for value in window)) or 1e-9
    scores = []
    for lag in range(-REFINE_LAGS, REFINE_LAGS+1):
        segment = snd[base+lag:base+lag+width]
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
    return (base+lag-first)/DECODE_RATE, scores[best], (first+last)/(2*DECODE_RATE)


def _second_energies(samples, begin, end):
    """``[(centre_seconds, energy), ...]`` for whole seconds inside ``begin..end``."""
    first = max(0, int(math.ceil(begin)))
    stop = min(len(samples)//DECODE_RATE, int(math.floor(end)))
    if stop <= first:
        first = max(0, int(begin))
        stop = min(len(samples)//DECODE_RATE, max(first+1, int(end)))
    out = []
    for start in range(first, stop):
        block = samples[start*DECODE_RATE:(start+1)*DECODE_RATE]
        if block:
            out.append((start+.5, sum(value*value for value in block[::4])))
    return out


def _loudest(samples, begin, end):
    """Centre (seconds) of the loudest second between begin and end (seconds)."""
    energies = _second_energies(samples, begin, end)
    return max(energies, key=lambda row: row[1])[0] if energies else (begin+end)/2


def _drift_centres(samples, begin, end):
    """Disjoint (early, late) active centres (picture seconds) near the ends of ``begin..end``,
    or (early, None) when active sound does not span at least DRIFT_MIN_SEPARATION seconds."""
    energies = _second_energies(samples, begin, end)
    if not energies:
        return (begin+end)/2, None
    loudest = max(energies, key=lambda row: row[1])
    if loudest[1] <= 0:
        return loudest[0], None
    zone = min(15.0, (end-begin)/4)
    threshold = .01*loudest[1]
    early_zone = [row for row in energies if row[0] <= begin+zone]
    late_zone = [row for row in energies if row[0] >= end-zone]
    early = max(early_zone, key=lambda row: row[1]) if early_zone else energies[0]
    if early[1] < threshold:
        active = [row for row in energies if row[1] >= threshold]
        early = active[0] if active else loudest
    late = max(late_zone, key=lambda row: row[1]) if late_zone else energies[-1]
    if late[1] < threshold:
        active = [row for row in energies if row[1] >= threshold]
        late = active[-1] if active else loudest
    if late[0]-early[0] < DRIFT_MIN_SEPARATION or early[1] < threshold or late[1] < threshold:
        return loudest[0], None
    return early[0], late[0]


def _drift_windows(pic, snd, env_pic, env_snd, coarse, begin, end):
    """Find two correlated windows far enough apart to measure clock drift."""
    overlap = end-begin
    max_shift = min(MAX_DRIFT_SEARCH, max(2*REFINE_LAGS/DECODE_RATE, MAX_DRIFT_RATE*overlap))

    def targets():
        yield from _drift_centres(pic, begin, end)
        # Hiss can make a quiet end look active. Try the loudest second in each
        # quarter, visiting the outer quarters first for a longer measurement.
        quarter = overlap/4
        for part in (0, 3, 1, 2):
            yield _loudest(pic, begin+part*quarter, begin+(part+1)*quarter)
        # A usable passage can start at the very first frame or run to the last.
        yield begin
        yield end

    seen, windows = set(), []
    for target in targets():
        if target is None or target in seen:
            continue
        seen.add(target)
        seed = windows[0][1] if windows else coarse
        offset, score, centre = _local_drift_offset(pic, snd, env_pic, env_snd, seed, target, max_shift)
        if centre is None or score < MIN_LOCAL_CORRELATION:
            continue
        windows.append((centre, offset))
        first, last = min(windows), max(windows)
        if last[0]-first[0] >= DRIFT_MIN_SEPARATION:
            return first, last
    return None


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
    overlap = end-begin
    ok_match = confidence >= MIN_CONFIDENCE and ratio >= MIN_PEAK_RATIO
    # A drifting recorder can make a nominal 40-second overlap measure slightly shorter.
    check_drift = ok_match and overlap >= DRIFT_MIN_OVERLAP*(1-MAX_DRIFT_RATE)
    windows = _drift_windows(pic, snd, env_pic, env_snd, coarse, begin, end) if check_drift else None
    if windows:
        centre, refine_seed = windows[0]
    else:
        centre, refine_seed = _loudest(pic, begin, end if check_drift else min(end, begin+30)), coarse
    offset, score, _ = _refine(pic, snd, refine_seed, centre, begin, end)
    result = dict(method='waveform', confidence=round(confidence, 3), peakRatio=round(min(ratio, 99), 2),
                  coarseOffsetSeconds=round(coarse, 4))
    if offset is None:
        offset, score = refine_seed, 0.0
    if windows:
        (early_centre, early_env), (late_centre, late_env) = windows
        rate = (late_env-early_env)/(late_centre-early_centre)
        ppm = rate*1e6
        drift = rate*overlap
        result['driftSeconds'] = round(drift, 6) or 0.0
        result['driftPpm'] = round(ppm, 1) or 0.0
        # Flag a clock difference beyond what two good crystals show (DRIFT_PPM), or one
        # that would move the sound a quarter frame over the take.
        if abs(ppm) > DRIFT_PPM or abs(drift) > .25/fps:
            result['drift'] = True
            offset = early_env-rate*(early_centre-begin)
    result.update(offsetSeconds=round(offset, 6) or 0.0, offsetSamples=round(offset*48000),
                  offsetFrames=round(offset*fps, 2) or 0.0, refineCorrelation=round(score, 3))
    result['ok'] = ok_match and (not check_drift or windows is not None) and not result.get('drift')
    if not ok_match:
        result['note'] = (f'Low confidence ({confidence:.2f}, peak ratio {min(ratio, 99):.1f}): these recordings may not be '
                          'the same take. Not synced.')
    elif result.get('drift'):
        result['note'] = (f"The two recordings drift apart by {result['driftSeconds']*1000:.1f} ms over the take "
                          f"({result['driftPpm']:+.0f} ppm, different sample clocks). Synced at the start only if you choose to.")
    elif check_drift and not windows:
        result['note'] = 'The waveforms match, but there is too little correlated sound across the take to verify clock drift. Not synced.'
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
