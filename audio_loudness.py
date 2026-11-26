"""Program loudness: measurement, normalization and the true-peak limiter.

Everything here works on the finished mix (48 kHz float, stereo or 5.1),
streamed through FFmpeg, so a feature-length program never sits in memory.

* ``measure``: ITU-R BS.1770-4 / EBU R128 integrated loudness, loudness range,
  maximum momentary and short-term loudness and true peak (FFmpeg ebur128,
  4x oversampled peak).
* ``finish``: optional two-pass normalization to a target (linear gain only,
  unless the user chose the limiting mode) and the true-peak limiter.

The limiter measures peaks on a 4x oversampled copy of the program and turns
them into a gain envelope (lookahead minimum, short attack ramp, exponential
release) that multiplies the original 48 kHz samples. Where the envelope is 1
the samples are multiplied by exactly 1.0, so material the limiter does not act
on passes bit-for-bit; a program that never reaches the ceiling is not
processed at all. The delivered file is then measured independently; if its
true peak is still above the ceiling (interpolation differences between the
detector and the measurement) the envelope is rebuilt with more margin.
"""
from array import array
import math
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

RATE = 48000
OVERSAMPLE = 4
# Envelope resolution: one gain value per WINDOW input samples (0.25 ms).
WINDOW = 12
ATTACK_WINDOWS = 6           # 1.5 ms ramp before a peak
RELEASE_SECONDS = .06        # exponential recovery time constant
LIMITER_ATTEMPTS = 4
LIMITER_MARGIN_DB = .05      # start this far under the ceiling
NORMALIZE_ATTEMPTS = 3
NORMALIZE_TOLERANCE_LU = .1
# Presets offered by the export dialog (LUFS). Any value in the range is accepted.
TARGETS = {'streaming': -14.0, 'podcast': -16.0, 'ebu-r128': -23.0, 'atsc-a85': -24.0, 'cinema-dialogue': -27.0}
TARGET_RANGE = (-36.0, -8.0)
READ_BYTES = 1 << 22
# ebur128 prints its true peak to 0.1 dB; the same 4x oversampled peak is read
# to 1e-6 dB from astats (the value reported and checked against the ceiling).
PRECISE_PEAK = f'aresample={OVERSAMPLE*RATE}:filter_size=64,astats=measure_perchannel=none:measure_overall=Peak_level'
_ONE = array('f', [1.0]).tobytes()


def _finite(value, digits=2):
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _run(command, cancelled, heartbeat):
    from render_movie import _run_ffmpeg
    return _run_ffmpeg(command, cancelled, heartbeat)


def measure(input_args, cancelled=None, heartbeat=None, *, gain_db=0.0, channels=2):
    """BS.1770 measurements of an FFmpeg input (``['-i', path]`` or a concat list)."""
    volume = f'volume={gain_db!r}dB:precision=double,' if gain_db else ''
    layout = 'stereo' if channels == 2 else '5.1'
    output = _run(['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'info', *input_args, '-vn', '-sn',
                   '-af', f'{volume}aformat=channel_layouts={layout},ebur128=peak=true:framelog=quiet,{PRECISE_PEAK}',
                   '-c:a', 'pcm_f32le', '-f', 'null', '-'], cancelled, heartbeat)
    return parse_summary(output)


def parse_summary(output):
    summary = output[output.rfind('Integrated loudness:'):]
    if 'Integrated loudness:' not in summary:
        raise ValueError('Could not measure the program loudness')

    def number(pattern):
        found = re.search(pattern, summary)
        if not found or found.group(1) in ('nan', '-nan'):
            return -math.inf
        return -math.inf if found.group(1) == '-inf' else float(found.group(1))
    integrated = number(r'I:\s+(-?[0-9.]+|-inf|nan)\s+LUFS')
    return {'integrated': integrated if integrated > -70 else -math.inf,
            'range': max(0.0, number(r'LRA:\s+(-?[0-9.]+|-inf|nan)\s+LU')),
            'truePeak': _precise_peak(output, number(r'True peak:\s+Peak:\s+(-?[0-9.]+|-inf)\s+dBFS'))}


def _precise_peak(output, fallback):
    found = re.findall(r'Peak level dB:\s+(-?[0-9.]+|-inf)', output)
    if not found:
        return fallback
    return -math.inf if found[-1] == '-inf' else float(found[-1])


def measure_full(input_args, cancelled=None, heartbeat=None, *, channels=2, directory=None):
    """``measure`` plus the loudest momentary and short-term values (Analyze program)."""
    layout = 'stereo' if channels == 2 else '5.1'
    log = Path(directory)/'loudness-frames.txt'
    output = _run(['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'info', *input_args, '-vn', '-sn',
                   '-af', f'aformat=channel_layouts={layout},ebur128=peak=true:framelog=quiet:metadata=1,'
                   f'ametadata=mode=print:file={_escape(log)},{PRECISE_PEAK}', '-c:a', 'pcm_f32le', '-f', 'null', '-'],
                  cancelled, heartbeat)
    result = parse_summary(output)
    loudest = {'M': -math.inf, 'S': -math.inf}
    pattern = re.compile(r'lavfi\.r128\.([MS])=(-?[0-9.]+|-inf|nan)')
    with open(log, encoding='utf-8', errors='replace') as handle:
        for line in handle:
            found = pattern.search(line)
            if found and found.group(2) not in ('nan', '-inf'):
                key = found.group(1)
                loudest[key] = max(loudest[key], float(found.group(2)))
    log.unlink(missing_ok=True)
    result['maxMomentary'], result['maxShortTerm'] = loudest['M'], loudest['S']
    return result


def _escape(path):
    return str(path).replace('\\', '/').replace(':', r'\:').replace("'", r"\'")


def public(measurement):
    """JSON-safe measurement with units in the key names (null = silence)."""
    names = {'integrated': 'integratedLufs', 'range': 'loudnessRangeLu', 'truePeak': 'truePeakDbtp',
             'maxMomentary': 'maxMomentaryLufs', 'maxShortTerm': 'maxShortTermLufs'}
    return {names[key]: _finite(value) for key, value in measurement.items() if key in names}


def normalization_options(options):
    """Validated (target LUFS, mode) or None. Off unless the user chose a target."""
    target = options.get('normalize')
    if target is None or target is False or target == 'off':
        return None
    if isinstance(target, str):
        if target not in TARGETS:
            raise ValueError('Invalid loudness target: use ' + ', '.join(TARGETS) + ' or a number of LUFS')
        target = TARGETS[target]
    if isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) \
            or not TARGET_RANGE[0] <= target <= TARGET_RANGE[1]:
        raise ValueError(f'Invalid loudness target (LUFS): use a number from {TARGET_RANGE[0]:g} to {TARGET_RANGE[1]:g}')
    mode = options.get('normalizeMode', 'linear')
    if mode not in ('linear', 'limit'):
        raise ValueError('Invalid normalization mode: use linear (gain only) or limit (gain, then true-peak limiting)')
    return float(target), mode


# ---- the true-peak limiter ----

class _Stream:
    """FFmpeg with its output read here; cancellable, stopped when stalled."""

    def __init__(self, command, cancelled, heartbeat, stdin=None):
        from render_movie import _stall_seconds, _stop_tree
        self.stop_tree = _stop_tree
        self.stall = _stall_seconds()
        self.cancelled, self.heartbeat = cancelled, heartbeat
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.errors = []
        self.last = time.monotonic()
        self.done = threading.Event()
        threading.Thread(target=self._errors, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _errors(self):
        for raw in self.process.stderr:
            self.errors = (self.errors + [raw.decode('utf-8', 'replace')])[-50:]

    def _watch(self):
        while not self.done.wait(.2):
            if (self.cancelled and self.cancelled.is_set()) or time.monotonic()-self.last > self.stall:
                self.stop_tree(self.process)
                return

    def beat(self):
        self.last = time.monotonic()
        if self.heartbeat:
            self.heartbeat()

    def finish(self):
        try:
            code = self.process.wait(timeout=self.stall)
        except subprocess.TimeoutExpired:
            self.stop_tree(self.process)
            code = self.process.wait(timeout=30)
        finally:
            self.done.set()
        if self.cancelled and self.cancelled.is_set():
            raise InterruptedError('Export cancelled')
        if code:
            raise ValueError('FFmpeg limiter pass failed: ' + ''.join(self.errors)[-1500:])

    def close(self):
        self.done.set()
        if self.process.poll() is None:
            self.stop_tree(self.process)
            self.process.wait(timeout=30)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe:
                pipe.close()


def _over_windows(input_args, gain_db, threshold, channels, cancelled, heartbeat):
    """{window: oversampled peak} for every 0.25 ms window whose 4x peak exceeds ``threshold``."""
    volume = f'volume={gain_db!r}dB:precision=double,' if gain_db else ''
    stream = _Stream(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', *input_args, '-vn', '-sn',
                      '-af', f'{volume}aresample={OVERSAMPLE*RATE}:filter_size=64', '-ac', str(channels),
                      '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1'], cancelled, heartbeat)
    over = {}
    span = WINDOW*OVERSAMPLE*channels      # floats per window
    width = span*4
    position = 0                           # window index of the next byte read
    pending = b''
    try:
        while True:
            data = stream.process.stdout.read(READ_BYTES)
            if not data:
                break
            stream.beat()
            data = pending + data
            usable = len(data)//width*width
            pending = data[usable:]
            block = array('f')
            block.frombytes(data[:usable])
            if block and (max(block) > threshold or -min(block) > threshold):
                for start in range(0, len(block), span):
                    window = block[start:start+span]
                    peak = max(max(window), -min(window))
                    if peak > threshold:
                        over[position+start//span] = peak
            position += len(block)//span
        if pending:
            block = array('f')
            block.frombytes(pending[:len(pending)//4*4])
            if block:
                peak = max(max(block), -min(block))
                if peak > threshold:
                    over[position] = peak
        stream.finish()
    finally:
        stream.close()
    return over


def _envelope(over, target):
    """Gain per window (sparse runs) that keeps every window's peak at ``target``.

    Returns a list of (first window, [gains...]); windows outside the runs have
    gain exactly 1. The gain is the lookahead minimum of the required gain (held
    one window either side), averaged over the attack (so it ramps down before
    the peak and never exceeds what any window requires), then released
    exponentially."""
    if not over:
        return []
    release = 1-math.exp(-WINDOW/(RELEASE_SECONDS*RATE))
    tail = math.ceil(17*RELEASE_SECONDS*RATE/WINDOW)   # e^-17: back to 1 within float precision
    keys = sorted(over)
    runs, begin, end = [], keys[0], keys[0]
    for key in keys[1:]:
        if key <= end+tail+ATTACK_WINDOWS+2:
            end = key
        else:
            runs.append((begin, end)); begin = end = key
    runs.append((begin, end))
    result = []
    for begin, end in runs:
        first = max(0, begin-ATTACK_WINDOWS-1)
        last = end+tail+1
        size = last-first+1
        required = [1.0]*size
        for key in range(begin, end+1):
            peak = over.get(key)
            if peak:
                need = target/peak
                for neighbour in (key-1, key, key+1):
                    index = neighbour-first
                    if 0 <= index < size and need < required[index]:
                        required[index] = need
        # lookahead minimum over ATTACK windows ahead, then the attack average
        ahead = [min(required[index:index+ATTACK_WINDOWS+1]) for index in range(size)]
        gains = []
        total = 0.0
        history = []
        for value in ahead:
            history.append(value); total += value
            if len(history) > ATTACK_WINDOWS+1:
                total -= history.pop(0)
            gains.append(min(value, total/(ATTACK_WINDOWS+1)) if len(history) == ATTACK_WINDOWS+1 else value)
        current = 1.0
        for index, value in enumerate(gains):
            current = min(value, current+(1-current)*release)
            gains[index] = current
        while gains and gains[-1] >= 1-1e-7:
            gains.pop()
        if gains:
            result.append((first, gains))
    return result


def _envelope_bytes(runs, gain, total_samples):
    """Yield the per-sample gain stream (float32 mono): ``gain`` times the limiter envelope."""
    unity = array('f', [gain]).tobytes()
    chunk = 1 << 16
    position = 0
    for first, gains in runs:
        start = first*WINDOW
        while position < start:
            count = min(chunk, start-position)
            yield unity*count
            position += count
        samples = array('f')
        for index, value in enumerate(gains):
            following = gains[index+1] if index+1 < len(gains) else 1.0
            step = (following-value)/WINDOW
            samples.extend(gain*(value+step*k) for k in range(WINDOW))
        yield samples.tobytes()
        position = start+len(gains)*WINDOW
    while position < total_samples:
        count = min(chunk, total_samples-position)
        yield unity*count
        position += count


def _apply(input_args, runs, gain_db, channels, total_samples, target, cancelled, heartbeat):
    """Multiply the program by the envelope (and gain) into a float WAV at ``target``."""
    gain = 10**(gain_db/20) if gain_db else 1.0
    layout = 'stereo' if channels == 2 else '5.1'
    spread = '|'.join(f'c{index}=c0' for index in range(channels))
    graph = (f'[0:a]aformat=sample_fmts=flt:channel_layouts={layout}[program];'
             f'[1:a]pan={layout}|{spread}[gain];[program][gain]amultiply,'
             f'atrim=end_sample={total_samples}[out]')
    stream = _Stream(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *input_args,
                      '-f', 'f32le', '-ar', str(RATE), '-ac', '1', '-i', 'pipe:0',
                      '-filter_complex', graph, '-map', '[out]', '-c:a', 'pcm_f32le', '-ar', str(RATE), str(target)],
                     cancelled, heartbeat, stdin=True)
    failure = []

    def feed():
        try:
            for data in _envelope_bytes(runs, gain, total_samples):
                if stream.done.is_set():
                    return
                stream.process.stdin.write(data)
                stream.beat()
            stream.process.stdin.close()
        except (BrokenPipeError, ValueError, OSError) as error:
            failure.append(error)
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        while writer.is_alive():
            writer.join(.5)
            if cancelled and cancelled.is_set():
                raise InterruptedError('Export cancelled')
        stream.finish()
        if failure:
            raise ValueError(f'The limiter could not write its gain stream: {failure[0]}')
    finally:
        stream.close()
        writer.join(5)


def finish(input_args, total_samples, directory, *, ceiling=None, normalization=None, channels=2,
           cancelled=None, heartbeat=None, progress=None):
    """Normalize and limit the mix. Returns (ffmpeg input args of the finished
    float program, report dict, output measurement). With nothing to do the
    input is returned as is (bit-exact)."""
    report = {}
    measured = measure(input_args, cancelled, heartbeat, channels=channels)
    report['input'] = public(measured)
    gain = 0.0
    if normalization:
        target, mode = normalization
        if not math.isfinite(measured['integrated']):
            report['normalization'] = {'targetLufs': target, 'mode': mode, 'reached': False,
                                       'note': 'The program is silent (below the -70 LUFS gate); no gain applied.'}
            normalization = None
        else:
            gain = target-measured['integrated']
            if mode == 'linear' and ceiling is not None and math.isfinite(measured['truePeak']) \
                    and measured['truePeak']+gain > ceiling-LIMITER_MARGIN_DB:
                # Linear mode never changes dynamics: stop where the peak meets the ceiling.
                gain = ceiling-LIMITER_MARGIN_DB-measured['truePeak']
            report['normalization'] = {'targetLufs': target, 'mode': mode}
    if progress: progress(.25)
    peak_after_gain = measured['truePeak']+gain
    needs_limit = ceiling is not None and math.isfinite(peak_after_gain) and peak_after_gain > ceiling
    limiter = report['limiter'] = None if ceiling is None else {'ceilingDbtp': ceiling, 'acted': False}
    if not needs_limit and abs(gain) < 1e-9:
        report['gainDb'] = 0.0
        report['output'] = report['input']
        _close_normalization(report, measured)
        return input_args, report, measured
    target = Path(directory)/'finished.wav'
    margin = LIMITER_MARGIN_DB
    output = None
    for attempt in range(NORMALIZE_ATTEMPTS if normalization and normalization[1] == 'limit' else 1):
        for retry in range(LIMITER_ATTEMPTS):
            runs = []
            peak = measured['truePeak']+gain
            if ceiling is not None and (retry or (math.isfinite(peak) and peak > ceiling-margin)):
                threshold = 10**((ceiling-margin)/20)
                over = _over_windows(input_args, gain, threshold, channels, cancelled, heartbeat)
                runs = _envelope(over, threshold)
            _apply(input_args, runs, gain, channels, total_samples, target, cancelled, heartbeat)
            output = measure(['-i', str(target)], cancelled, heartbeat, channels=channels)
            if limiter is not None:
                limiter.update(acted=bool(runs), regions=len(runs),
                               maxReductionDb=_finite(min((20*math.log10(min(g)) for _, g in runs), default=0.0)))
            if ceiling is None or not math.isfinite(output['truePeak']) or output['truePeak'] <= ceiling:
                break
            margin += output['truePeak']-ceiling+.05
        if not normalization or normalization[1] != 'limit' or not math.isfinite(output['integrated']):
            break
        miss = normalization[0]-output['integrated']
        if abs(miss) <= NORMALIZE_TOLERANCE_LU:
            break
        gain += miss
    report['gainDb'] = _finite(gain, 3)
    report['output'] = public(output)
    _close_normalization(report, output)
    if progress: progress(1.0)
    return ['-i', str(target)], report, output


def _close_normalization(report, output):
    normalization = report.get('normalization')
    if not normalization or 'reached' in normalization:
        return
    loudness = output['integrated']
    shortfall = normalization['targetLufs']-loudness if math.isfinite(loudness) else None
    normalization['reached'] = shortfall is not None and abs(shortfall) <= .2
    normalization['shortfallLu'] = _finite(shortfall)
    if not normalization['reached'] and normalization['mode'] == 'linear':
        normalization['note'] = ('Linear gain stopped at the true-peak ceiling, so the program is '
                                 f'{abs(shortfall):.1f} LU short of the target. Choose the limiting mode to reach it.')
