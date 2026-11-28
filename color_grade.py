"""The clip grade for the export: the same tables color-grade.js builds for the monitor.

Working space: Rec. 709 video-encoded R'G'B', full range 0..1 (see color-grade.js for the
chain). Each stage is written as a table FFmpeg applies with the interpolation the monitor
uses: 1D tables with ``lut1d:interp=linear`` (4,096 entries), the hue-vs-saturation lattice
with ``lut3d:interp=trilinear``, saturation with ``colorchannelmixer``. Everything runs on
float RGB (gbrapf32le) so no stage rounds to 8 or 10 bits on the way.

Python standard library only; keep every formula in step with color-grade.js
(tests/test_color_grade.py compares the tables of both).
"""
import json
import math
from pathlib import Path

TABLE = 4096
HUE_SAT_SIZE = 33
MAX_POINTS = 16
MAX_KEY_TIMES = 16
LUMA = (.213, .715, .072)
WHEELS = {'lift': (-1, 1, 0), 'gamma': (-1, 1, 0), 'gain': (0, 4, 1), 'offset': (-1, 1, 0)}
CURVES = ('master', 'red', 'green', 'blue')
ANIMATED = {'exposure': (-4, 4, 0), 'temperature': (-100, 100, 0), 'tint': (-100, 100, 0),
            'brightness': (0, 200, 100), 'contrast': (0, 200, 100), 'saturation': (0, 200, 100)}
# No alpha plane: zscale scales alpha to the video range on the way back to Y'CbCr (opaque
# becomes 0.92), so still-image alpha is carried around the grade instead (render_movie).
WORKING_FORMAT = 'gbrpf32le'
# Matrix names for setparams and zscale.
SETPARAMS_MATRIX = {'bt709': 'bt709', 'bt601': 'smpte170m', 'bt2020': 'bt2020nc'}
ZSCALE_MATRIX = {'bt709': '709', 'bt601': '170m', 'bt2020': '2020_ncl'}


def _clamp01(value):
    return 0.0 if value < 0 else 1.0 if value > 1 else value


def _finite(value, low, high):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and low <= value <= high


def _check_points(points, name, y_high):
    if not isinstance(points, list) or len(points) > MAX_POINTS:
        raise ValueError(f'Invalid clip grade: {name} has more than {MAX_POINTS} points')
    last = -1
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or not _finite(point[0], 0, 1) or not _finite(point[1], 0, y_high):
            raise ValueError(f'Invalid clip grade: {name} point')
        if point[0] <= last:
            raise ValueError(f'Invalid clip grade: {name} points must be in increasing order')
        last = point[0]


def validate_grade(grade):
    """Check a stored grade (clip['color']['grade']) at the boundary. Returns it or raises ValueError."""
    if grade is None:
        return None
    if not isinstance(grade, dict):
        raise ValueError('Invalid clip grade: not an object')
    for name, (low, high, _) in WHEELS.items():
        if name in grade and (not isinstance(grade[name], list) or len(grade[name]) != 4 or
                              not all(_finite(value, low, high) for value in grade[name])):
            raise ValueError(f'Invalid clip grade: {name}')
    if 'pivot' in grade and not _finite(grade['pivot'], .05, .95):
        raise ValueError('Invalid clip grade: pivot')
    if 'bypass' in grade and not isinstance(grade['bypass'], bool):
        raise ValueError('Invalid clip grade: bypass')
    if 'curves' in grade:
        curves = grade['curves']
        if not isinstance(curves, dict):
            raise ValueError('Invalid clip grade: curves')
        for key, points in curves.items():
            if key not in CURVES:
                raise ValueError('Invalid clip grade: curve name')
            _check_points(points, key + ' curve', 1)
            if len(points) == 1:
                raise ValueError(f'Invalid clip grade: {key} curve needs two points')
    if 'hueSat' in grade:
        _check_points(grade['hueSat'], 'hue vs saturation', 2)
        if any(point[0] >= 1 for point in grade['hueSat']):
            raise ValueError('Invalid clip grade: hue vs saturation hue')
    return grade


def _wheel(grade, name):
    default = WHEELS[name][2]
    return (grade or {}).get(name) or [default]*4


def _identity_curve(points):
    return not points or (len(points) == 2 and points[0] == [0, 0] and points[1] == [1, 1])


def grade_active(grade):
    if not grade:
        return False
    for name, (_, _, default) in WHEELS.items():
        if any(value != default for value in _wheel(grade, name)):
            return True
    if grade.get('pivot', .5) != .5:
        return True
    if any(not _identity_curve((grade.get('curves') or {}).get(key)) for key in CURVES):
        return True
    return any(point[1] != 1 for point in grade.get('hueSat') or [])


def curve_function(points):
    """Monotone cubic (Fritsch-Carlson), flat beyond the end points. Mirrors color-grade.js."""
    if _identity_curve(points):
        return None
    x = [float(p[0]) for p in points]
    y = [float(p[1]) for p in points]
    n = len(x)
    delta = [(y[i+1]-y[i])/(x[i+1]-x[i]) for i in range(n-1)]
    slope = [0.0]*n
    slope[0] = delta[0]
    slope[n-1] = delta[n-2]
    for i in range(1, n-1):
        slope[i] = 0.0 if delta[i-1]*delta[i] <= 0 else (delta[i-1]+delta[i])/2
    for i in range(n-1):
        if delta[i] == 0:
            slope[i] = 0.0
            slope[i+1] = 0.0
            continue
        a = slope[i]/delta[i]
        b = slope[i+1]/delta[i]
        h = a*a+b*b
        if h > 9:
            t = 3/math.sqrt(h)
            slope[i] = t*a*delta[i]
            slope[i+1] = t*b*delta[i]

    def evaluate(value):
        if value <= x[0]:
            return y[0]
        if value >= x[n-1]:
            return y[n-1]
        i = 0
        while value > x[i+1]:
            i += 1
        h = x[i+1]-x[i]
        t = (value-x[i])/h
        t2 = t*t
        t3 = t2*t
        return ((2*t3-3*t2+1)*y[i]+(t3-2*t2+t)*h*slope[i]+(-2*t3+3*t2)*y[i+1]+(t3-t2)*h*slope[i+1])
    return evaluate


def gains(color):
    exposure = max(-4.0, min(4.0, float(color.get('exposure') or 0)))
    temperature = max(-100.0, min(100.0, float(color.get('temperature') or 0)))/100
    tint = max(-100.0, min(100.0, float(color.get('tint') or 0)))/100
    gain = 2**exposure
    return (gain*(1+.35*temperature-.1*tint), gain*(1+.2*tint), gain*(1-.35*temperature-.1*tint))


def _legacy(color):
    def value(key):
        return max(0.0, min(3.0, float(color.get(key, 100) if color.get(key) is not None else 100)/100))
    return value('brightness'), value('contrast'), value('saturation')


def hue_of(r, g, b):
    high = max(r, g, b)
    low = min(r, g, b)
    d = high-low
    if d <= 0:
        return 0.0
    if high == r:
        h = (g-b)/d
    elif high == g:
        h = 2+(b-r)/d
    else:
        h = 4+(r-g)/d
    h /= 6
    return h+1 if h < 0 else h


def _hue_sat_function(points):
    if not points or all(point[1] == 1 for point in points):
        return None
    if len(points) == 1:
        return lambda hue: points[0][1]

    def evaluate(hue):
        for i, a in enumerate(points):
            b = points[(i+1) % len(points)]
            span = b[0]+1-a[0] if i == len(points)-1 else b[0]-a[0]
            offset = hue-a[0]
            if offset < 0:
                offset += 1
            if offset <= span:
                return a[1]+(b[1]-a[1])*offset/span
        return points[0][1]
    return evaluate


def stages(color):
    grade = color.get('grade') if isinstance(color.get('grade'), dict) and not color['grade'].get('bypass') else None
    brightness, contrast, saturation = _legacy(color)
    pivot = (grade or {}).get('pivot', .5)
    gain = gains(color)
    lift, gamma, gain_wheel, offset = (_wheel(grade, name) for name in ('lift', 'gamma', 'gain', 'offset'))
    curves = (grade or {}).get('curves') or {}
    master = curve_function(curves.get('master'))
    channels = [curve_function(curves.get(key)) for key in ('red', 'green', 'blue')]
    active = grade_active(grade)
    pre = None
    if (brightness, contrast) != (1, 1):
        def pre(value):
            return _clamp01((_clamp01(value*brightness)-pivot)*contrast+pivot)

    def post_for(channel):
        g = gain[channel]
        if not active and g == 1:
            return None
        L = lift[channel]+lift[3]
        G = 2**-(gamma[channel]+gamma[3])
        K = gain_wheel[channel]*gain_wheel[3]
        O = offset[channel]+offset[3]
        curve = channels[channel]

        def post(value):
            v = _clamp01(value*g)
            if not active:
                return v
            v = K*((v+O)+L*(1-(v+O)))
            v = v**G if v > 0 else v
            v = _clamp01(v)
            if master:
                v = master(v)
            if curve:
                v = curve(v)
            return _clamp01(v)
        return post
    matrix = None if saturation == 1 else [[saturation*(row == column)+(1-saturation)*LUMA[column] for column in range(3)]
                                           for row in range(3)]
    return {'pre': pre, 'matrix': matrix, 'post': [post_for(c) for c in range(3)],
            'hueSat': _hue_sat_function((grade or {}).get('hueSat'))}


def _table(fn):
    return [fn(i/(TABLE-1)) for i in range(TABLE)]


def _hue_sat_lattice(fn):
    n = HUE_SAT_SIZE
    out = []
    for b in range(n):
        for g in range(n):
            for r in range(n):
                R, G, B = r/(n-1), g/(n-1), b/(n-1)
                y = .2126*R+.7152*G+.0722*B
                s = fn(hue_of(R, G, B))
                out.extend((_clamp01(y+(R-y)*s), _clamp01(y+(G-y)*s), _clamp01(y+(B-y)*s)))
    return out


def tables(color):
    s = stages(color)
    flat = all(fn is None for fn in s['post'])
    return {'pre': _table(s['pre']) if s['pre'] else None, 'matrix': s['matrix'],
            'post': None if flat else [_table(fn or _clamp01) for fn in s['post']],
            'hueSat': _hue_sat_lattice(s['hueSat']) if s['hueSat'] else None}


def _value_at(points, local, base):
    ordered = sorted((p for p in points if isinstance(p, dict) and _finite(p.get('time'), -1e9, 1e9) and _finite(p.get('value'), -1e9, 1e9)),
                     key=lambda p: p['time'])
    if not ordered:
        return base
    if local <= ordered[0]['time']:
        return ordered[0]['value']
    for a, b in zip(ordered, ordered[1:]):
        if local <= b['time']:
            return a['value']+(b['value']-a['value'])*(local-a['time'])/(b['time']-a['time'])
    return ordered[-1]['value']


def key_times(clip):
    times = set()
    for key, (low, high, _) in ANIMATED.items():
        points = (clip.get('keyframes') or {}).get(key, [])
        if not isinstance(points, list) or len(points) > MAX_KEY_TIMES:
            raise ValueError(f'Invalid {key} keyframes')
        for point in points:
            if not isinstance(point, dict) or not _finite(point.get('time'), -1e9, 1e9) or not _finite(point.get('value'), low, high):
                raise ValueError(f'Invalid {key} keyframe')
            times.add(point['time'])
    if len(times) > MAX_KEY_TIMES:
        raise ValueError(f'A clip supports at most {MAX_KEY_TIMES} distinct grade keyframe times')
    return sorted(times)


def color_at(clip, local):
    color = dict(clip.get('color') or {})
    for key, (low, high, base) in ANIMATED.items():
        points = (clip.get('keyframes') or {}).get(key)
        if points:
            color[key] = max(low, min(high, _value_at(points, local, float(color.get(key, base) if color.get(key) is not None else base))))
    return color


def uses_grade_path(clip, item=None):
    """Whether a clip needs the table-based grade (wheels, curves, grade keyframes, an input LUT)."""
    color = clip.get('color') or {}
    grade = color.get('grade')
    if grade is not None:
        validate_grade(grade)
    if grade and grade.get('bypass'):
        return True
    if grade_active(grade) or key_times(clip):
        return True
    return bool(item and item.get('inputLut'))


def source_matrix(profile, override=None):
    """The Y'CbCr matrix a source is read with: its tag; for an untagged source the per-source
    choice (media['colorMatrix']: 'bt709' or 'bt601'), else Rec. 601, which is what FFmpeg (and so
    every Splice export) has always assumed for untagged video. color-grade.js matrixName mirrors this."""
    tag = profile[2] if profile else None
    if tag == 'bt709':
        return 'bt709'
    if tag in ('smpte170m', 'bt470bg', 'bt601', 'fcc', 'smpte240m'):
        return 'bt601'
    if tag in ('bt2020nc', 'bt2020c'):
        return 'bt2020'
    return override if override in ('bt709', 'bt601') else 'bt601'


def output_matrix(profile):
    return source_matrix(profile, 'bt709')


# ---- Writing the tables for FFmpeg ----

def _write_1d(path, columns):
    lines = [f'LUT_1D_SIZE {len(columns[0])}']
    lines.extend(f'{columns[0][i]:.9f} {columns[1][i]:.9f} {columns[2][i]:.9f}' for i in range(len(columns[0])))
    Path(path).write_text('\n'.join(lines)+'\n')


def _write_3d(path, size, data):
    lines = [f'LUT_3D_SIZE {size}']
    lines.extend(f'{data[i]:.9f} {data[i+1]:.9f} {data[i+2]:.9f}' for i in range(0, len(data), 3))
    Path(path).write_text('\n'.join(lines)+'\n')


def _filter_path(path):
    value = Path(path).as_posix()
    option = ''.join('\\'+char if char in "\\':" else char for char in value)
    return ''.join('\\'+char if char in "\\'[],;" else char for char in option)


def _mixers(matrix):
    """colorchannelmixer limits each coefficient to +-2: a stronger saturation is applied as two
    matrices with the square root of the saturation (saturation matrices about one luma compose)."""
    def text(m):
        return 'colorchannelmixer='+':'.join(f'{r}{c}={m[i][j]:.9f}' for i, r in enumerate('rgb') for j, c in enumerate('rgb'))
    if max(abs(value) for row in matrix for value in row) <= 2:
        return ','+text(matrix)
    # Recover s from the diagonal: m00 = s + (1-s)*LUMA[0].
    saturation = (matrix[0][0]-LUMA[0])/(1-LUMA[0])
    root = math.sqrt(saturation)
    half = [[root*(row == column)+(1-root)*LUMA[column] for column in range(3)] for row in range(3)]
    return ','+text(half)+','+text(half)


def float_lut_domain(domain_min, domain_max):
    """Map each float RGB plane to the LUT domain before the lookup clamps its coordinates."""
    channels = ':'.join(f"{channel}='(p(X,Y)-({low!r}))/({high-low!r})'"
                        for channel, low, high in zip('rgb', domain_min, domain_max))
    return f",geq={channels}:a='p(X,Y)':interpolation=nearest"


def look_filter(lut, intensity, directory, name):
    """Normalize float RGB to the LUT domain, apply its lattice or curve, then mix with the input."""
    from lut import cube_text, cube1d_text
    if not lut:
        return ''
    if isinstance(intensity, bool) or not isinstance(intensity, (int, float)) or not math.isfinite(intensity) or not 0 <= intensity <= 100:
        raise ValueError('Invalid LUT intensity')
    if not intensity:
        return ''
    directory = Path(directory)
    domain_min = lut.get('domainMin', [0, 0, 0])
    domain_max = lut.get('domainMax', [1, 1, 1])
    custom = domain_min != [0, 0, 0] or domain_max != [1, 1, 1]
    serialize, lookup, interp = (cube1d_text, 'lut1d', 'linear') if lut.get('curve') else (cube_text, 'lut3d', 'trilinear')
    path = directory/f'{name}.cube'
    path.write_text(serialize(lut, 100, normalized_domain=custom))
    chain = float_lut_domain(domain_min, domain_max) if custom else ''
    chain += f',{lookup}=file={_filter_path(path)}:interp={interp}'
    if intensity == 100:
        return chain
    # Only the lookup is domain-limited. Its dry signal retains the incoming headroom.
    label = '_look_'+name.replace('-', '_')
    return (f',split[{label}_a][{label}_b];[{label}_b]null{chain}[{label}_g];'
            f'[{label}_g][{label}_a]blend=all_mode=normal:all_opacity={intensity/100:.12g}')


def _static_chain(color, directory, name, input_lut=None):
    """The grade at fixed control values, as filters on float RGB (starting with a comma, or '')."""
    directory = Path(directory)
    chain = ''
    if input_lut:
        chain += look_filter(input_lut, 100, directory, name+'-input')
    bypass = bool((color.get('grade') or {}).get('bypass'))
    if bypass:
        return chain + (',lut1d=file=' + _filter_path(_clamp_table(directory)) + ':interp=linear' if chain else '')
    t = tables(color)
    if t['pre']:
        _write_1d(directory/f'{name}-pre.cube', [t['pre']]*3)
        chain += f',lut1d=file={_filter_path(directory/f"{name}-pre.cube")}:interp=linear'
    if t['matrix']:
        chain += _mixers(t['matrix'])
    if t['post']:
        _write_1d(directory/f'{name}-post.cube', t['post'])
        chain += f',lut1d=file={_filter_path(directory/f"{name}-post.cube")}:interp=linear'
    if t['hueSat']:
        _write_3d(directory/f'{name}-huesat.cube', HUE_SAT_SIZE, t['hueSat'])
        chain += f',lut3d=file={_filter_path(directory/f"{name}-huesat.cube")}:interp=trilinear'
    if color.get('lut'):
        intensity = color.get('lutIntensity', 100)
        chain += look_filter(color['lut'], 100 if intensity is None else intensity, directory, name+'-look')
    # The monitor clamps the graded signal to 0..1 before compositing; so does the export.
    return chain + ',lut1d=file=' + _filter_path(_clamp_table(directory)) + ':interp=linear'


def _clamp_table(directory):
    path = Path(directory)/'clamp.cube'
    if not path.exists():
        _write_1d(path, [[0.0, 1.0]]*3)
    return path


def grade_filter(clip, directory, name, *, input_lut=None, clock='T', offset=0.0):
    """Filters that grade a clip's picture on float RGB, starting with a comma.

    With grade keyframes the grade at each keyframe time is a branch of its own and adjacent
    branches are mixed linearly over time with blend, exactly as the monitor mixes the two
    graded pictures (color-grade.js keySegment). ``clock`` is the blend expression's clip-local
    time and ``offset`` is added to it (seconds between the chain's start and the clip start).
    """
    directory = Path(directory)
    times = key_times(clip)
    bypass = bool(((clip.get('color') or {}).get('grade') or {}).get('bypass'))
    if not times or bypass:
        return _static_chain(clip.get('color') or {}, directory, name, input_lut)
    if len(times) == 1:
        return _static_chain(color_at(clip, times[0]), directory, name, input_lut)
    label = '_grade_'+name.replace('-', '_')
    outputs = ''.join(f'[{label}_in{i}]' for i in range(len(times)))
    parts = [f',split={len(times)}{outputs}']
    for i, moment in enumerate(times):
        parts.append(f';[{label}_in{i}]null{_static_chain(color_at(clip, moment), directory, f"{name}-k{i}", input_lut)}[{label}_k{i}]')
    local = f'({clock}-({offset:.9f}))'
    current = f'{label}_k0'
    for i in range(1, len(times)):
        a, b = times[i-1], times[i]
        # Eased grade keyframes ease the mix (effects_engine.grade_weight_expr, effects.js gradeSegmentKind).
        from effects_engine import grade_weight_expr
        weight = f'({grade_weight_expr(clip, a, f"(({local}-({a:.9f}))/({b-a:.9f}))")})'
        expression = f'if(lt({local},{a:.9f}),A,if(gte({local},{b:.9f}),B,A+(B-A)*{weight}))'
        target = f'{label}_m{i}'
        parts.append(f";[{current}][{label}_k{i}]blend=all_expr='{expression}'[{target}]")
        current = target
    parts.append(f';[{current}]null')
    return ''.join(parts)


def dumps_tables(color):
    """Tables as JSON, for the parity tests."""
    return json.dumps(tables(color))
