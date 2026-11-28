"""Clip effects for the export: keyframe easing, speed ramps, crop, masks, chroma key, blend
modes and stabilization.

effects.js holds the same maths for the program monitor (WebGL2 and Canvas 2D). Each function
here has a twin there with the same name in camelCase, the same operations in the same order,
so the two agree to floating-point rounding (tests/test_effects.py, tests/effects-parity.cjs).

Data model (all optional, saved in the project):

  clip.effects = {
    crop:  {left, right, top, bottom: % of the picture, feather: % of its height, enabled},
    masks: [{shape: rectangle|ellipse|polygon, x, y: centre in % of the picture,
             width, height: % of the picture (a polygon's scale), rotation: degrees,
             feather, expansion: % of the picture height, opacity: %, invert, enabled,
             points: [[x, y], ...] polygon corners in % of the picture, relative to the centre}],
    key:   {color: '#rrggbb', similarity, smoothness, spill, choke, feather: %, invert, enabled},
    blend: normal|multiply|screen|overlay|add|darken|lighten|softLight|hardLight|difference,
    stabilize: {analysis: id, smoothing, maxShift, zoom, autoZoom, tripod, enabled},
    speedAudio: pitch|varispeed|mute,
  }
  clip.keyframes.speed: a speed ramp, % of normal speed (0 is a freeze) at clip-local times.
  clip.keyframes['crop.left'], ['mask.0.x'], ...: animated crop and mask properties.

Every keyframe may carry an interpolation for the segment that leaves it:
  interp: linear|hold|easeIn|easeOut|easeInOut|bezier, outHandle: [x, y] (with bezier) and the
  next key's inHandle: [x, y]. Handles are in the segment's unit square (x: time, y: value).
  The legacy ``ease: true`` (smoothstep) still reads as it always has.
"""
import array
import math
import re

import splice_platform

# ---------------------------------------------------------------- keyframe curves
INTERPOLATIONS = ('linear', 'hold', 'easeIn', 'easeOut', 'easeInOut', 'bezier')
PRESET_HANDLES = {'easeIn': (0.42, 0.0, 1.0, 1.0), 'easeOut': (0.0, 0.0, 0.58, 1.0), 'easeInOut': (0.42, 0.0, 0.58, 1.0)}
SOLVE_STEPS = 40
HANDLE_Y = (-2.0, 3.0)
MAX_RAMP_FRAMES = 200_000

BLEND_MODES = ('normal', 'multiply', 'screen', 'overlay', 'add', 'darken', 'lighten', 'softLight', 'hardLight', 'difference')
MASK_SHAPES = ('rectangle', 'ellipse', 'polygon')
MAX_MASKS = 8
MAX_POLYGON_POINTS = 16
SPEED_AUDIO = ('pitch', 'varispeed', 'mute')
SPEED_RANGE = (0.0, 1600.0)
MASK_PROPS = {'x': (-100, 200, 50), 'y': (-100, 200, 50), 'width': (0, 400, 50), 'height': (0, 400, 50),
              'rotation': (-360, 360, 0), 'feather': (0, 100, 0), 'expansion': (-100, 100, 0), 'opacity': (0, 100, 100)}
CROP_PROPS = {'left': (0, 100, 0), 'right': (0, 100, 0), 'top': (0, 100, 0), 'bottom': (0, 100, 0), 'feather': (0, 100, 0)}
KEY_PROPS = {'similarity': (0, 100, 20), 'smoothness': (0, 100, 10), 'spill': (0, 100, 50), 'choke': (0, 100, 0),
             'feather': (0, 100, 0)}
STABILIZE_PROPS = {'smoothing': (1, 120, 15), 'maxShift': (-1, 500, -1), 'zoom': (-50, 100, 0), 'autoZoom': (0, 2, 1)}
# Animated effect properties: their keyframe names and bounds.
EFFECT_KEY_BOUNDS = {'speed': SPEED_RANGE, **{'crop.'+name: bounds[:2] for name, bounds in CROP_PROPS.items()},
                     **{f'mask.{index}.{name}': bounds[:2] for index in range(MAX_MASKS) for name, bounds in MASK_PROPS.items()}}
ANALYSIS_ID = re.compile(r'^[0-9a-f]{32}$')
# Refusals effects.js repeats word for word at load, in edits and on paste (MESSAGES there).
MULTICAM_RAMP = 'Speed ramps on multicam clips are not supported. Flatten the multicam clip first.'
STABILIZE_SOURCE = 'Stabilize the clips inside a nested or multicam clip, or a video clip'
# Motion analysis reads the file as FFmpeg decodes it, not the clip's developed RAW picture.
STABILIZE_RAW = 'Splice cannot stabilize a camera RAW clip yet. Stabilize an exported copy of the clip instead.'


def _is_raw(item):
    from render_movie import _is_raw_media_item
    return _is_raw_media_item(item)


def effect_key_message(key):
    if re.match(r'^mask\.\d+\.', key):
        return f'Invalid keyframes: {key[:40]} (a clip has at most {MAX_MASKS} masks)'
    return f'Invalid keyframes: {key[:40]}'

HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')


def _finite(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Invalid {label}')
    return float(value)


def validate_point_shape(point, label):
    """The interpolation fields of one keyframe (time and value are checked by the caller)."""
    interp = point.get('interp')
    if interp is not None and interp not in INTERPOLATIONS:
        raise ValueError(f'Invalid {label} keyframe interpolation')
    for name in ('inHandle', 'outHandle'):
        handle = point.get(name)
        if handle is None:
            continue
        if not isinstance(handle, list) or len(handle) != 2:
            raise ValueError(f'Invalid {label} keyframe {name}')
        _finite(handle[0], f'{label} keyframe {name}', 0, 1)
        _finite(handle[1], f'{label} keyframe {name}', *HANDLE_Y)
    if 'ease' in point and not isinstance(point['ease'], bool):
        raise ValueError(f'Invalid {label} keyframe: ease must be true or false')


def eased(points):
    """Whether any segment of a curve is not a straight line."""
    return any(point.get('ease') is True or point.get('interp') not in (None, 'linear') for point in points[:-1])


def segment_kind(a, b):
    """(kind, x1, y1, x2, y2) of the segment from key ``a`` to key ``b``."""
    interp = a.get('interp')
    if interp == 'hold':
        return ('hold', 0.0, 0.0, 1.0, 1.0)
    if interp in PRESET_HANDLES:
        return ('bezier', *PRESET_HANDLES[interp])
    if interp == 'bezier':
        x1, y1 = a.get('outHandle') or (1/3, 1/3)
        x2, y2 = b.get('inHandle') or (2/3, 2/3)
        return ('bezier', float(x1), float(y1), float(x2), float(y2))
    if interp is None and a.get('ease') is True:
        return ('smooth', 0.0, 0.0, 1.0, 1.0)
    return ('linear', 0.0, 0.0, 1.0, 1.0)


def _coefficients(p1, p2):
    c = 3*p1
    b = 3*(p2-p1)-c
    return 1-c-b, b, c


def bezier_component(p1, p2, s):
    a, b, c = _coefficients(p1, p2)
    return ((a*s+b)*s+c)*s


def bezier_solve(x1, x2, u):
    """The curve parameter s at which x(s) = u, by a fixed bisection (the same steps in effects.js)."""
    if u <= 0:
        return 0.0
    if u >= 1:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(SOLVE_STEPS):
        middle = (low+high)/2
        if bezier_component(x1, x2, middle) < u:
            low = middle
        else:
            high = middle
    return (low+high)/2


def segment_fraction(kind, u):
    """Eased fraction 0..1 at segment time u in 0..1."""
    name, x1, y1, x2, y2 = kind
    if name == 'hold':
        return 1.0 if u >= 1 else 0.0
    if name == 'smooth':
        return u*u*(3-2*u)
    if name == 'bezier':
        return bezier_component(y1, y2, bezier_solve(x1, x2, u))
    return u


GRADE_KEYS = ('exposure', 'temperature', 'tint', 'brightness', 'contrast', 'saturation')


def grade_segment_kind(clip, start):
    """The easing of the grade mix from key time ``start``: effects.js gradeSegmentKind."""
    for key in GRADE_KEYS:
        points = ordered_points((clip.get('keyframes') or {}).get(key))
        for index, point in enumerate(points[:-1]):
            if point['time'] == start:
                return segment_kind(point, points[index+1])
    return None


def grade_weight_expr(clip, start, fraction):
    """FFmpeg expression of the eased grade mix weight at segment fraction ``fraction`` (0..1)."""
    kind = grade_segment_kind(clip, start)
    if kind is None or kind[0] == 'linear':
        return fraction
    if kind[0] == 'hold':
        return '0'
    knots = [(index/200, segment_fraction(kind, index/200)) for index in range(201)]
    return _tree(knots, fraction)


def ordered_points(points):
    return sorted((point for point in points or [] if isinstance(point, dict)), key=lambda point: point['time'])


def curve_value(points, local, base):
    """A keyframe curve at clip-local time ``local``: held before the first key and after the last."""
    ordered = ordered_points(points)
    if not ordered:
        return base
    if local <= ordered[0]['time']:
        return ordered[0]['value']
    for a, b in zip(ordered, ordered[1:]):
        if local <= b['time']:
            if local >= b['time'] and a.get('interp') == 'hold':
                return b['value']
            u = (local-a['time'])/(b['time']-a['time'])
            return a['value']+(b['value']-a['value'])*segment_fraction(segment_kind(a, b), u)
    return ordered[-1]['value']


def split_segment(kind, u):
    """Handles of the two halves of a bezier segment cut at segment time u, each in its own unit
    square: ((x1, y1, x2, y2) left, (x1, y1, x2, y2) right, y at the cut). de Casteljau."""
    _, x1, y1, x2, y2 = kind
    s = bezier_solve(x1, x2, u)
    def cut(p1, p2):
        a = (0.0, p1, p2, 1.0)
        l1 = a[0]+(a[1]-a[0])*s; m = a[1]+(a[2]-a[1])*s; r2 = a[2]+(a[3]-a[2])*s
        l2 = l1+(m-l1)*s; r1 = m+(r2-m)*s
        mid = l2+(r1-l2)*s
        return (l1, l2, mid), (r1, r2, mid)
    (lx1, lx2, xm), (rx1, rx2, _) = cut(x1, x2)
    (ly1, ly2, ym), (ry1, ry2, _) = cut(y1, y2)
    def norm(value, low, high):
        return (value-low)/(high-low) if abs(high-low) > 1e-12 else value
    left = (norm(lx1, 0, xm), norm(ly1, 0, ym) if abs(ym) > 1e-12 else 1/3,
            norm(lx2, 0, xm), norm(ly2, 0, ym) if abs(ym) > 1e-12 else 2/3)
    right = (norm(rx1, xm, 1), norm(ry1, ym, 1) if abs(1-ym) > 1e-12 else 1/3,
             norm(rx2, xm, 1), norm(ry2, ym, 1) if abs(1-ym) > 1e-12 else 2/3)
    clamp_x = lambda value: min(1.0, max(0.0, value))
    clamp_y = lambda value: min(HANDLE_Y[1], max(HANDLE_Y[0], value))
    return ((clamp_x(left[0]), clamp_y(left[1]), clamp_x(left[2]), clamp_y(left[3])),
            (clamp_x(right[0]), clamp_y(right[1]), clamp_x(right[2]), clamp_y(right[3])), ym)


def _control_points(a, b, kind):
    _, x1, y1, x2, y2 = ('bezier', 1/3, 0.0, 2/3, 1.0) if kind[0] == 'smooth' else kind
    dt, dv = b['time']-a['time'], b['value']-a['value']
    return [(a['time'], a['value']), (a['time']+x1*dt, a['value']+y1*dv), (a['time']+x2*dt, a['value']+y2*dv), (b['time'], b['value'])]


def _casteljau(points, s):
    def lerp(p, q):
        return (p[0]+(q[0]-p[0])*s, p[1]+(q[1]-p[1])*s)
    a, b, c = lerp(points[0], points[1]), lerp(points[1], points[2]), lerp(points[2], points[3])
    d, e = lerp(a, b), lerp(b, c)
    m = lerp(d, e)
    return [points[0], a, d, m], [m, e, c, points[3]]


def _curve_param(points, at):
    dt = points[3][0]-points[0][0]
    if dt <= 0:
        return 0.0
    return bezier_solve((points[1][0]-points[0][0])/dt, (points[2][0]-points[0][0])/dt, (at-points[0][0])/dt)


def _handles_of(points):
    dt, dv = points[3][0]-points[0][0], points[3][1]-points[0][1]
    spread = max(abs(p[1]-points[0][1]) for p in points)
    x1 = min(1.0, max(0.0, (points[1][0]-points[0][0])/dt))
    x2 = min(1.0, max(0.0, (points[2][0]-points[0][0])/dt))
    if abs(dv) <= 1e-12:
        return (x1, 1/3, x2, 2/3) if spread <= 1e-9 else None
    y1, y2 = (points[1][1]-points[0][1])/dv, (points[2][1]-points[0][1])/dv
    if HANDLE_Y[0] <= y1 <= HANDLE_Y[1] and HANDLE_Y[0] <= y2 <= HANDLE_Y[1]:
        return (x1, y1, x2, y2)
    return None


def _value_extremum(points):
    p1, p2, p3 = (points[i][1]-points[0][1] for i in (1, 2, 3))
    a, b, c = p3-3*p2+3*p1, 3*p2-6*p1, 3*p1
    roots = []
    if abs(a) < 1e-12:
        if abs(b) > 1e-12:
            roots = [-c/(2*b)]
    else:
        disc = 4*b*b-12*a*c
        if disc >= 0:
            roots = [(-2*b-math.sqrt(disc))/(6*a), (-2*b+math.sqrt(disc))/(6*a)]
    inside = sorted(r for r in roots if 1e-6 < r < 1-1e-6)
    return inside[0] if inside else 0.5


def _pieces(points, depth=0):
    handles = _handles_of(points)
    if handles or depth >= 4:
        return [(points, handles or (1/3, 1/3, 2/3, 2/3))]
    left, right = _casteljau(points, _value_extremum(points))
    return _pieces(left, depth+1)+_pieces(right, depth+1)


def window_points(points, start, end):
    """The keys that shape start..end, re-based to 0; a segment cut by the window keeps its exact
    shape (pieces whose handles cannot be written are split at their value extremum).
    effects.js windowPoints, step for step."""
    ordered = ordered_points(points)
    if not ordered:
        return []
    def plain(point):
        return {key: value for key, value in point.items() if not (key in ('interp', 'ease', 'inHandle', 'outHandle') and value is None)}
    out = []
    def push(point):
        if out and abs(out[-1]['time']-point['time']) < 1e-9:
            keep = out[-1].get('inHandle', point.get('inHandle'))
            out[-1].update(point)
            if keep is not None:
                out[-1]['inHandle'] = keep
            return
        out.append(point)
    if start < ordered[0]['time']:
        push({'time': 0.0, 'value': ordered[0]['value']})
    for i, a in enumerate(ordered):
        b = ordered[i+1] if i+1 < len(ordered) else None
        if b is None:
            if start-1e-9 <= a['time'] <= end+1e-9:
                push(plain({**a, 'time': a['time']-start}))
            break
        if b['time'] <= start or a['time'] >= end:
            continue
        kind = segment_kind(a, b)
        lo, hi = max(start, a['time']), min(end, b['time'])
        if kind[0] not in ('bezier', 'smooth'):
            head = {'time': lo-start, 'value': curve_value(ordered, lo, 0)}
            if lo == a['time']:
                head.update(plain({**a, 'time': a['time']-start}))
            elif kind[0] == 'hold':
                head['interp'] = 'hold'
            push(head)
            continue
        control = _control_points(a, b, kind)
        if hi < b['time']:
            control = _casteljau(control, _curve_param(control, hi))[0]
        if lo > a['time']:
            control = _casteljau(control, _curve_param(control, lo))[1]
        for piece, (x1, y1, x2, y2) in _pieces(control):
            head = {'time': piece[0][0]-start, 'value': piece[0][1], 'interp': 'bezier', 'outHandle': [x1, y1]}
            if piece[0][0] == a['time'] and a.get('inHandle'):
                head['inHandle'] = a['inHandle']
            push(head)
            out.append({'time': piece[3][0]-start, 'value': piece[3][1], 'inHandle': [x2, y2]})
    if not out or out[-1]['time'] < end-start-1e-9:
        push({'time': end-start, 'value': curve_value(ordered, end, 0)})
    if out[0]['time'] > 1e-9:
        out.insert(0, {'time': 0.0, 'value': curve_value(ordered, start, 0)})
    for point in out:
        if abs(point['time']) < 1e-9:
            point['time'] = 0.0
        if abs(point['time']-(end-start)) < 1e-9:
            point['time'] = end-start
    return [plain(point) for point in out]


# ---------------------------------------------------------------- speed ramps
def ramp_points(clip):
    points = (clip.get('keyframes') or {}).get('speed') if isinstance(clip.get('keyframes'), dict) else None
    return ordered_points(points) if points else []


def _segment_integral(kind, v0, dv, u):
    """Integral over 0..u of the segment's value (in segment-time units)."""
    name, x1, y1, x2, y2 = kind
    if name == 'hold':
        return v0*u
    if name == 'linear':
        return v0*u+dv*u*u/2
    if name == 'smooth':
        return v0*u+dv*(u**3-u**4/2)
    s = bezier_solve(x1, x2, u)
    ax, bx, cx = _coefficients(x1, x2)
    ay, by, cy = _coefficients(y1, y2)
    p5 = 3*ax*ay
    p4 = 3*ax*by+2*bx*ay
    p3 = 3*ax*cy+2*bx*by+cx*ay
    p2 = 2*bx*cy+cx*by
    p1 = cx*cy
    j = ((((p5/6*s+p4/5)*s+p3/4)*s+p2/3)*s+p1/2)*s*s
    return v0*u+dv*j


def _offset_at(points, local):
    """Offset from the first key's time to ``local`` (local >= first key time is not required)."""
    first = points[0]
    if local <= first['time']:
        return first['value']/100*(local-first['time'])
    total = 0.0
    for a, b in zip(points, points[1:]):
        span = b['time']-a['time']
        kind = segment_kind(a, b)
        if local <= b['time']:
            return total+span*_segment_integral(kind, a['value']/100, (b['value']-a['value'])/100, (local-a['time'])/span)
        total += span*_segment_integral(kind, a['value']/100, (b['value']-a['value'])/100, 1.0)
    return total+points[-1]['value']/100*(local-points[-1]['time'])


def ramp_source(points, local):
    """Source offset from the clip's in point at clip-local time ``local``."""
    return _offset_at(points, local)-_offset_at(points, 0.0)


def ramp_speed(points, local):
    """Instantaneous speed factor at ``local``."""
    return max(0.0, curve_value(points, local, 100.0))/100


def ramp_duration(points, source_span, limit=7200.0):
    """The clip-local time at which ``source_span`` seconds of source have played (None if never)."""
    if source_span <= 0:
        return 0.0
    if ramp_source(points, limit) < source_span:
        return None
    low, high = 0.0, limit
    for _ in range(80):
        middle = (low+high)/2
        if ramp_source(points, middle) < source_span:
            low = middle
        else:
            high = middle
    return high


def validate_ramp(clip, label='speed'):
    points = (clip.get('keyframes') or {}).get('speed')
    if not points:
        return []
    if clip.get('reverse'):
        raise ValueError('Speed ramps play forward. Turn off Reverse to ramp this clip.')
    if clip.get('nativeTimeRemap'):
        raise ValueError('Edit this Premiere speed curve in Premiere.')
    return ordered_points(points)


# ---------------------------------------------------------------- effects validation
def _section(value, label):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f'Invalid {label}')
    return value


def _flag(section, name, label):
    value = section.get(name, False if name != 'enabled' else True)
    if not isinstance(value, bool):
        raise ValueError(f'Invalid {label} {name}')
    return value


def validate_effects(effects):
    """Check a clip's effects; returns a normalized copy (defaults filled) or None."""
    if effects is None:
        return None
    if not isinstance(effects, dict):
        raise ValueError('Invalid clip effects')
    allowed = {'crop', 'masks', 'key', 'blend', 'stabilize', 'speedAudio', 'bypass'}
    if set(effects) - allowed:
        raise ValueError('Unknown clip effect: ' + ', '.join(sorted(set(effects) - allowed)))
    out = {'bypass': _flag(effects, 'bypass', 'effects')}
    crop = _section(effects.get('crop'), 'crop')
    if crop is not None:
        out['crop'] = {name: _finite(crop.get(name, default), 'crop '+name, low, high)
                       for name, (low, high, default) in CROP_PROPS.items()}
        out['crop']['enabled'] = _flag(crop, 'enabled', 'crop')
        if out['crop']['left']+out['crop']['right'] > 100 or out['crop']['top']+out['crop']['bottom'] > 100:
            raise ValueError('The crop removes more than the whole picture')
    masks = effects.get('masks')
    if masks is not None:
        if not isinstance(masks, list) or len(masks) > MAX_MASKS:
            raise ValueError(f'A clip has at most {MAX_MASKS} masks')
        out['masks'] = []
        for number, mask in enumerate(masks):
            if not isinstance(mask, dict) or mask.get('shape') not in MASK_SHAPES:
                raise ValueError(f'Invalid mask {number+1}')
            entry = {'shape': mask['shape'], 'invert': _flag(mask, 'invert', 'mask'), 'enabled': _flag(mask, 'enabled', 'mask')}
            for name, (low, high, default) in MASK_PROPS.items():
                entry[name] = _finite(mask.get(name, default), f'mask {number+1} {name}', low, high)
            if mask['shape'] == 'polygon':
                points = mask.get('points')
                if not isinstance(points, list) or not 3 <= len(points) <= MAX_POLYGON_POINTS:
                    raise ValueError(f'A polygon mask has 3 to {MAX_POLYGON_POINTS} points')
                entry['points'] = []
                for point in points:
                    if not isinstance(point, list) or len(point) != 2:
                        raise ValueError(f'Invalid mask {number+1} point')
                    entry['points'].append([_finite(point[0], 'mask point', -200, 200), _finite(point[1], 'mask point', -200, 200)])
            out['masks'].append(entry)
    key = _section(effects.get('key'), 'chroma key')
    if key is not None:
        color = key.get('color', '#00ff00')
        if not isinstance(color, str) or not HEX_COLOR.match(color):
            raise ValueError('Invalid key color')
        out['key'] = {'color': color.lower(), 'invert': _flag(key, 'invert', 'key'), 'enabled': _flag(key, 'enabled', 'key')}
        for name, (low, high, default) in KEY_PROPS.items():
            out['key'][name] = _finite(key.get(name, default), 'key '+name, low, high)
    blend = effects.get('blend', 'normal')
    if blend not in BLEND_MODES:
        raise ValueError('Invalid blend mode')
    out['blend'] = blend
    stabilize = _section(effects.get('stabilize'), 'stabilization')
    if stabilize is not None:
        analysis = stabilize.get('analysis')
        if analysis is not None and (not isinstance(analysis, str) or not ANALYSIS_ID.match(analysis)):
            raise ValueError('Invalid stabilization analysis')
        out['stabilize'] = {'analysis': analysis, 'tripod': _flag(stabilize, 'tripod', 'stabilization'),
                            'enabled': _flag(stabilize, 'enabled', 'stabilization')}
        for name, (low, high, default) in STABILIZE_PROPS.items():
            out['stabilize'][name] = _finite(stabilize.get(name, default), 'stabilization '+name, low, high)
        if out['stabilize']['autoZoom'] != int(out['stabilize']['autoZoom']):
            raise ValueError('Invalid stabilization autoZoom')
        source = stabilize.get('source')
        if source is not None:
            if not isinstance(source, list) or len(source) != 2:
                raise ValueError('Invalid stabilization source range')
            low = _finite(source[0], 'stabilization source range', 0, 48*3600)
            high = _finite(source[1], 'stabilization source range', 0, 48*3600)
            if high <= low:
                raise ValueError('Invalid stabilization source range')
            out['stabilize']['source'] = [low, high]
    speed_audio = effects.get('speedAudio', 'pitch')
    if speed_audio not in SPEED_AUDIO:
        raise ValueError('Invalid ramp audio mode')
    out['speedAudio'] = speed_audio
    return out


def active(effects, keyframes=None):
    """The effects that render: bypassed and disabled ones are kept in the project but skipped."""
    if not effects or effects.get('bypass'):
        return {'blend': 'normal', 'masks': []}
    crop = effects.get('crop')
    keyed = isinstance(keyframes, dict) and any(keyframes.get('crop.'+name) for name in ('left', 'right', 'top', 'bottom'))
    crop = crop if crop and crop.get('enabled', True) and (keyed or any(crop[name] for name in ('left', 'right', 'top', 'bottom'))) else None
    masks = [mask for mask in effects.get('masks') or [] if mask.get('enabled', True)]
    key = effects.get('key')
    key = key if key and key.get('enabled', True) else None
    stabilize = effects.get('stabilize')
    stabilize = stabilize if stabilize and stabilize.get('enabled', True) and stabilize.get('analysis') else None
    return {'crop': crop, 'masks': masks, 'key': key, 'blend': effects.get('blend', 'normal'), 'stabilize': stabilize}


def has_picture_effects(effects):
    live = active(effects)
    return bool(live.get('crop') or live['masks'] or live.get('key') or live['blend'] != 'normal' or live.get('stabilize'))


def describe(effects, keyframes=None):
    """Names of a clip's configured effects (bypassed or not), for loss reports and unnest."""
    names = []
    if keyframes and isinstance(keyframes, dict) and keyframes.get('speed'):
        names.append('speed ramp')
    if not effects:
        return names
    if effects.get('crop'):
        names.append('crop')
    if effects.get('masks'):
        names.append('masks')
    if effects.get('key'):
        names.append('chroma key')
    if effects.get('blend', 'normal') != 'normal':
        names.append(f"{effects['blend']} blend mode")
    if effects.get('stabilize'):
        names.append('stabilization')
    if effects.get('bypass') and names:
        names = [name+' (bypassed)' for name in names]
    return names


# ---------------------------------------------------------------- per-pixel maths (reference)
# Coordinates: u, v in 0..1 over the clip's picture (pixel centres), aspect = width/height.
# Distances are in units of the picture height.
def _box(qx, qy, hx, hy):
    dx, dy = abs(qx)-hx, abs(qy)-hy
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    return outside+min(max(dx, dy), 0.0)


def _segment_distance(px, py, ax, ay, bx, by):
    ex, ey = bx-ax, by-ay
    wx, wy = px-ax, py-ay
    length = ex*ex+ey*ey
    h = min(1.0, max(0.0, (wx*ex+wy*ey)/length)) if length > 0 else 0.0
    return math.hypot(wx-ex*h, wy-ey*h)


def mask_params(mask, values):
    """A mask's numbers at one frame: ``values`` maps animated property names to their values."""
    get = lambda name: values.get(name, mask[name])
    return {'shape': mask['shape'], 'x': get('x')/100, 'y': get('y')/100, 'w': max(get('width'), 0)/100,
            'h': max(get('height'), 0)/100, 'angle': math.radians(get('rotation')), 'feather': get('feather')/100,
            'expansion': get('expansion')/100, 'opacity': get('opacity')/100, 'invert': mask['invert'],
            'points': [(px/100, py/100) for px, py in mask.get('points') or []]}


def mask_coverage(m, u, v, aspect):
    px, py = (u-m['x'])*aspect, v-m['y']
    c, s = math.cos(m['angle']), math.sin(m['angle'])
    qx, qy = c*px+s*py, -s*px+c*py
    if m['shape'] == 'rectangle':
        d = _box(qx, qy, m['w']/2*aspect, m['h']/2)
    elif m['shape'] == 'ellipse':
        hx, hy = max(m['w']/2*aspect, 1e-6), max(m['h']/2, 1e-6)
        d = (math.hypot(qx/hx, qy/hy)-1)*min(hx, hy)
    else:
        corners = [(x*m['w']*aspect, y*m['h']) for x, y in m['points']]
        d = 1e9
        inside = False
        for (ax, ay), (bx, by) in zip(corners, corners[1:]+corners[:1]):
            d = min(d, _segment_distance(qx, qy, ax, ay, bx, by))
            if (ay > qy) != (by > qy) and qx < (bx-ax)*(qy-ay)/(by-ay)+ax:
                inside = not inside
        d = -d if inside else d
    d -= m['expansion']
    if m['feather'] > 0:
        coverage = min(1.0, max(0.0, 0.5-d/m['feather']))
    else:
        coverage = 1.0 if d <= 0 else 0.0
    if m['invert']:
        coverage = 1-coverage
    return coverage*m['opacity']


def masks_alpha(params, u, v, aspect):
    if not params:
        return 1.0
    keep = 1.0
    for m in params:
        keep *= 1-mask_coverage(m, u, v, aspect)
    return 1-keep


def crop_params(crop, values):
    get = lambda name: values.get(name, crop[name])
    return {name: get(name)/100 for name in CROP_PROPS}


def crop_alpha(c, u, v, aspect):
    d = min((u-c['left'])*aspect, (1-c['right']-u)*aspect, v-c['top'], 1-c['bottom']-v)
    if c['feather'] > 0:
        return min(1.0, max(0.0, d/c['feather']))
    return 1.0 if d >= 0 else 0.0


KR_KB = {'bt709': (0.2126, 0.0722), 'bt601': (0.299, 0.114), 'bt2020': (0.2627, 0.0593)}


def key_chroma(color, matrix):
    """Cb, Cr (8-bit video-range codes / 255) of an sRGB key colour under the source matrix."""
    kr, kb = KR_KB.get(matrix, KR_KB['bt709'])
    r, g, b = (int(color[i:i+2], 16)/255 for i in (1, 3, 5))
    y = kr*r+(1-kr-kb)*g+kb*b
    cb = (128+224*(b-y)/(2*(1-kb)))/255
    cr = (128+224*(r-y)/(2*(1-kr)))/255
    return cb, cr


NEUTRAL = 128/255


def key_params(key, matrix):
    cb, cr = key_chroma(key['color'], matrix)
    dx, dy = cb-NEUTRAL, cr-NEUTRAL
    length = math.hypot(dx, dy)
    return {'cb': cb, 'cr': cr, 'dx': dx/length if length > 1e-9 else 0.0, 'dy': dy/length if length > 1e-9 else 0.0,
            'similarity': key['similarity']/100*0.4, 'smoothness': key['smoothness']/100*0.4,
            'spill': key['spill']/100, 'choke': key['choke']/100*0.9, 'feather': key['feather']/100, 'invert': key['invert']}


def key_alpha(k, cb, cr):
    """Matte from the chroma distance to the key (cb, cr: codes / 255)."""
    d = math.hypot(cb-k['cb'], cr-k['cr'])
    if k['smoothness'] > 0:
        a = min(1.0, max(0.0, (d-k['similarity'])/k['smoothness']))
    else:
        a = 1.0 if d > k['similarity'] else 0.0
    a = min(1.0, max(0.0, (a-k['choke'])/(1-k['choke'])))
    a = a+k['feather']*(a*a*(3-2*a)-a)
    return 1-a if k['invert'] else a


def despill(k, cb, cr):
    """Chroma with the key colour's direction pulled toward neutral by the spill amount."""
    x, y = cb-NEUTRAL, cr-NEUTRAL
    p = x*k['dx']+y*k['dy']
    if p > 0:
        x -= k['dx']*p*k['spill']
        y -= k['dy']*p*k['spill']
    return NEUTRAL+x, NEUTRAL+y


def _soft_light_d(b):
    return ((16*b-12)*b+4)*b if b <= 0.25 else math.sqrt(b)


def blend_channel(mode, b, s):
    """W3C separable blend of source s over backdrop b (0..1)."""
    if mode == 'multiply':
        return b*s
    if mode == 'screen':
        return b+s-b*s
    if mode == 'overlay':
        return 2*s*b if b <= 0.5 else 1-2*(1-s)*(1-b)
    if mode == 'add':
        return min(1.0, b+s)
    if mode == 'darken':
        return min(b, s)
    if mode == 'lighten':
        return max(b, s)
    if mode == 'hardLight':
        return 2*s*b if s <= 0.5 else 1-2*(1-s)*(1-b)
    if mode == 'softLight':
        return b-(1-2*s)*b*(1-b) if s <= 0.5 else b+(2*s-1)*(_soft_light_d(b)-b)
    if mode == 'difference':
        return abs(b-s)
    return s


# ---------------------------------------------------------------- FFmpeg expressions
def _tree(knots, clock):
    """Piecewise-linear interpolation through (x, y) knots sorted by x as a balanced FFmpeg
    expression (FFmpeg's parser nests about 100 levels), held flat outside the knots."""
    def build(low, high):
        if high-low == 1:
            (x0, y0), (x1, y1) = knots[low], knots[high]
            if abs(y1-y0) < 1e-12:
                return f'{y0:.9g}'
            return f'({y0:.12g}+({y1-y0:.12g})*(({clock})-({x0:.12g}))/{x1-x0:.12g})'
        middle = (low+high)//2
        return f'if(lt({clock},{knots[middle][0]:.12g}),{build(low, middle)},{build(middle, high)})'
    if not knots:
        raise ValueError('Empty curve')
    if len(knots) == 1:
        return f'{knots[0][1]:.9g}'
    inner = build(0, len(knots)-1)
    return f'if(lte({clock},{knots[0][0]:.12g}),{knots[0][1]:.9g},if(gte({clock},{knots[-1][0]:.12g}),{knots[-1][1]:.9g},{inner}))'


def linear_knots(points, step, low, high):
    """Knots of a curve expanded to straight lines: eased segments are sampled every ``step``
    seconds (the frame or audio-block grid), held segments jump at their end."""
    ordered = ordered_points(points)
    knots = []
    for a, b in zip(ordered, ordered[1:]):
        kind = segment_kind(a, b)
        span = b['time']-a['time']
        clamp = lambda value: min(high, max(low, value))
        if kind[0] == 'linear':
            knots.append((a['time'], clamp(a['value'])))
        elif kind[0] == 'hold':
            knots.append((a['time'], clamp(a['value'])))
            knots.append((b['time']-min(1e-6, span/4), clamp(a['value'])))
        else:
            count = max(2, min(4000, math.ceil(span/step)))
            for index in range(count):
                u = index/count
                knots.append((a['time']+span*u, clamp(a['value']+(b['value']-a['value'])*segment_fraction(kind, u))))
    if ordered:
        knots.append((ordered[-1]['time'], min(high, max(low, ordered[-1]['value']))))
    return knots


def keyframe_expr(points, base, low, high, clock, step):
    """FFmpeg expression of a keyframe curve (any interpolation) at ``clock``."""
    if not points:
        return f'{base:.6f}'
    return _tree(linear_knots(points, step, low, high), clock)


def ramp_knots(source_times, output_times):
    """setpts knots mapping input timestamps to output times for a speed ramp: the frame shown at
    output time t_k is the last one at or before source time s_k (exact frame sampling). A freeze
    (equal source times from t_a to t_b) maps its source time to t_a and anything later to after
    t_b, so the next source frame appears only once the freeze is over."""
    knots = []
    run_end = None
    for s, t in zip(source_times, output_times):
        if knots and s <= knots[-1][0]+1e-12:
            run_end = t
            continue
        if run_end is not None:
            knots.append((knots[-1][0]+1e-9, run_end))
            run_end = None
        knots.append((s, t))
    if run_end is not None:
        knots.append((knots[-1][0]+1e-9, run_end))
    if len(knots) == 1:
        s, t = knots[0]
        knots.append((s+1.0, t+1.0))
    # Extend beyond the ends at unit slope so earlier frames map before 0 and later ones past the end.
    (s0, t0), (s1, t1) = knots[0], knots[-1]
    return [(s0-3600.0, t0-3600.0), *knots, (s1+3600.0, t1+3600.0)]


def ramp_setpts(knots):
    """The expression (seconds) for setpts. Unlike _tree it extrapolates at the ends."""
    def build(low, high):
        if high-low == 1:
            (x0, y0), (x1, y1) = knots[low], knots[high]
            return f'({y0:.12f}+({y1-y0:.12f})*(T-({x0:.12f}))/{x1-x0:.12f})'
        middle = (low+high)//2
        return f'if(lt(T,{knots[middle][0]:.12f}),{build(low, middle)},{build(middle, high)})'
    return build(0, len(knots)-1)


def _clamp01(expression):
    return f'clip({expression},0,1)'


def _mask_expr(m, u, v, aspect, values, pixel):
    """Coverage of one mask (0..1) in FFmpeg terms. ``values`` maps the animated properties to
    expressions; ``pixel`` is a prefix for st/ld scratch slots (unused, kept for clarity)."""
    def num(name, scale):
        return values[name] if name in values else f'{m[name]*scale:.12g}'
    cx, cy = num('x', 0.01), num('y', 0.01)
    w, h = num('width', 0.01), num('height', 0.01)
    angle = values.get('rotation', None)
    angle = f'({angle})*PI/180' if angle is not None else f'{math.radians(m["rotation"]):.12g}'
    px, py = f'(({u})-({cx}))*({aspect})', f'(({v})-({cy}))'
    # st(0..1): rotated point, st(2..3): half sizes / scales, st(4): distance.
    setup = (f'st(5,{angle});st(0,cos(ld(5))*{px}+sin(ld(5))*{py});st(1,-sin(ld(5))*{px}+cos(ld(5))*{py});'
             f'st(2,max({w},0)*({aspect}));st(3,max({h},0))')
    if m['shape'] == 'rectangle':
        dist = ('st(6,abs(ld(0))-ld(2)/2);st(7,abs(ld(1))-ld(3)/2);'
                'st(4,hypot(max(ld(6),0),max(ld(7),0))+min(max(ld(6),ld(7)),0))')
    elif m['shape'] == 'ellipse':
        dist = ('st(6,max(ld(2)/2,0.000001));st(7,max(ld(3)/2,0.000001));'
                'st(4,(hypot(ld(0)/ld(6),ld(1)/ld(7))-1)*min(ld(6),ld(7)))')
    else:
        corners = [(x/100, y/100) for x, y in m['points']]
        terms = ['st(4,1000000000);st(8,0)']
        for (ax, ay), (bx, by) in zip(corners, corners[1:]+corners[:1]):
            axe, aye, bxe, bye = f'({ax:.12g}*ld(2))', f'({ay:.12g}*ld(3))', f'({bx:.12g}*ld(2))', f'({by:.12g}*ld(3))'
            ex, ey = f'({bxe}-{axe})', f'({bye}-{aye})'
            wx, wy = f'(ld(0)-{axe})', f'(ld(1)-{aye})'
            length = f'({ex}*{ex}+{ey}*{ey})'
            terms.append(f'st(9,if(gt({length},0),clip(({wx}*{ex}+{wy}*{ey})/{length},0,1),0))')
            terms.append(f'st(4,min(ld(4),hypot({wx}-{ex}*ld(9),{wy}-{ey}*ld(9))))')
            terms.append(f'st(8,if(not(eq(gt({aye},ld(1)),gt({bye},ld(1))))*lt(ld(0),{ex}*(ld(1)-{aye})/if(eq({ey},0),1,{ey})+{axe}),1-ld(8),ld(8)))')
        terms.append('st(4,if(ld(8),-ld(4),ld(4)))')
        dist = ';'.join(terms)
    expansion = num('expansion', 0.01)
    feather = num('feather', 0.01)
    coverage = f'if(gt({feather},0),clip(0.5-(ld(4)-({expansion}))/({feather}),0,1),lte(ld(4)-({expansion}),0))'
    if m['invert']:
        coverage = f'(1-{coverage})'
    opacity = num('opacity', 0.01)
    return f'({setup};{dist};{coverage}*({opacity}))'


def masks_expr(masks, u, v, aspect, animated):
    """Union of the masks' coverage. ``animated``: per-mask {property: expression}."""
    if not masks:
        return '1'
    keep = '*'.join(f'(1-{_mask_expr(m, u, v, aspect, animated.get(index, {}), index)})' for index, m in masks)
    return f'(1-{keep})'


def crop_expr(crop, u, v, aspect, values):
    def num(name):
        return values[name] if name in values else f'{crop[name]/100:.12g}'
    d = (f'min(min((({u})-{num("left")})*({aspect}),(1-{num("right")}-({u}))*({aspect})),'
         f'min(({v})-{num("top")},1-{num("bottom")}-({v})))')
    feather = num('feather')
    return f'if(gt({feather},0),clip(({d})/({feather}),0,1),gte({d},0))'


def key_alpha_expr(k, cb, cr):
    d = f'hypot(({cb})-{k["cb"]:.12g},({cr})-{k["cr"]:.12g})'
    if k['smoothness'] > 0:
        a = f'clip(({d}-{k["similarity"]:.12g})/{k["smoothness"]:.12g},0,1)'
    else:
        a = f'gt({d},{k["similarity"]:.12g})'
    a = f'clip(({a}-{k["choke"]:.12g})/{1-k["choke"]:.12g},0,1)'
    if k['feather'] > 0:
        a = f'(st(3,{a})+{k["feather"]:.12g}*(ld(3)*ld(3)*(3-2*ld(3))-ld(3)))'
    return f'(1-{a})' if k['invert'] else a


def despill_expr(k, cb, cr, channel):
    """Despilled Cb (channel 0) or Cr (channel 1), as codes / 255."""
    p = f'max((({cb})-{NEUTRAL:.12g})*{k["dx"]:.12g}+(({cr})-{NEUTRAL:.12g})*{k["dy"]:.12g},0)'
    if channel == 0:
        return f'(({cb})-{k["dx"]*k["spill"]:.12g}*{p})'
    return f'(({cr})-{k["dy"]*k["spill"]:.12g}*{p})'


def blend_expr(mode):
    """blend all_expr computing the blend result: A is the layer (source), B the backdrop, both
    float 0..1 (gbrpf32le)."""
    s, b = 'A', 'B'
    return {
        'multiply': f'{b}*{s}',
        'screen': f'{b}+{s}-{b}*{s}',
        'overlay': f'if(lte({b},0.5),2*{s}*{b},1-2*(1-{s})*(1-{b}))',
        'add': f'min(1,{b}+{s})',
        'darken': f'min({b},{s})',
        'lighten': f'max({b},{s})',
        'hardLight': f'if(lte({s},0.5),2*{s}*{b},1-2*(1-{s})*(1-{b}))',
        'softLight': f'if(lte({s},0.5),{b}-(1-2*{s})*{b}*(1-{b}),{b}+(2*{s}-1)*(if(lte({b},0.25),((16*{b}-12)*{b}+4)*{b},sqrt({b}))-{b}))',
        'difference': f'abs({b}-{s})',
    }[mode]


# ---------------------------------------------------------------- stabilization
def stabilize_transform_args(settings, trf):
    """vidstabtransform options; ``trf`` is a path the caller resolved inside its own store."""
    from render_movie import _filter_path
    parts = [f'input={_filter_path(trf)}', f'smoothing={int(round(settings["smoothing"]))}',
             f'optzoom={int(settings["autoZoom"])}', f'zoom={settings["zoom"]:.3f}', 'interpol=bicubic', 'crop=black']
    if settings['maxShift'] >= 0:
        parts.append(f'maxshift={int(round(settings["maxShift"]))}')
    if settings.get('tripod'):
        parts.append('tripod=1')
    return 'vidstabtransform=' + ':'.join(parts)


# ---------------------------------------------------------------- export: speed ramps
class Ramp:
    """A clip's speed ramp, read through the window clock (``_keyframeShift``) like keyframes."""
    def __init__(self, clip, points):
        self.points = points
        self.shift = float(clip.get('_keyframeShift', 0) or 0)
        self.zero = ramp_source(points, self.shift)

    def source(self, local):
        """Source seconds from the (window) clip's in point to clip-local time ``local``."""
        return ramp_source(self.points, local+self.shift)-self.zero

    def speed(self, local):
        return ramp_speed(self.points, local+self.shift)


class JoinedRamp:
    """Speed curves of clips played back to back as one clock: the pieces of a clip split at a cut,
    whose sound renders as one (render_movie._sound_joins). ``pieces``: (duration, curve) in order,
    each curve a Ramp or a constant speed; before the first piece and after the last, their own
    curves carry on."""
    def __init__(self, pieces):
        self.pieces = []
        start = offset = 0.0
        for duration, curve in pieces:
            curve = curve if isinstance(curve, Ramp) else _ConstantSpeed(curve)
            self.pieces.append((start, offset, curve))
            start += duration
            offset += curve.source(duration)

    def _piece(self, local):
        chosen = self.pieces[0]
        for piece in self.pieces[1:]:
            if local >= piece[0]:
                chosen = piece
        return chosen

    def source(self, local):
        start, offset, curve = self._piece(local)
        return offset+curve.source(local-start)

    def speed(self, local):
        start, _, curve = self._piece(local)
        return curve.speed(local-start)


class _ConstantSpeed:
    def __init__(self, speed):
        self.value = float(speed)

    def source(self, local):
        return local*self.value

    def speed(self, local):
        return self.value


def clip_ramp(clip):
    points = validate_ramp(clip)
    if not points:
        return None
    from render_movie import MAX_KEYFRAMES, _number
    if len(points) > MAX_KEYFRAMES:
        raise ValueError(f'A clip has more than {MAX_KEYFRAMES} speed keyframes, the export limit')
    for point in points:
        _number(point.get('time'), 'speed keyframe time', 0, 48*3600)
        _number(point.get('value'), 'speed keyframe value', *SPEED_RANGE)
        validate_point_shape(point, 'speed')
    if any(b['time'] <= a['time'] for a, b in zip(points, points[1:])):
        raise ValueError('Duplicate speed keyframes')
    return Ramp(clip, points)


def source_delta(clip, a, b):
    """Source seconds a clip plays between clip-local times a and b (ramped or constant)."""
    ramp = clip_ramp(clip)
    if ramp:
        return ramp.source(b)-ramp.source(a)
    return (b-a)*float(clip.get('speed', 1) or 1)


def ramp_input(index, ramp, origin_source, first_local, frames, fps, tolerance):
    """Sample a ramped source onto the sequence frame grid: output frame k (clip-local time
    first_local + k/fps) shows the last source frame at or before origin_source + S(t)."""
    if frames > MAX_RAMP_FRAMES:
        raise ValueError('A speed-ramped clip is too long to export in one piece')
    rate = float(fps)
    sources = [origin_source+ramp.source(first_local+k/rate)+tolerance for k in range(frames)]
    knots = ramp_knots(sources, [k/rate for k in range(frames)])
    return (f"[{index}:v]settb=1/1000000000,setpts='({ramp_setpts(knots)})/TB',"
            f'fps=fps={fps}:round=up:start_time=0,trim=end_frame={frames}')


# ---------------------------------------------------------------- export: picture effects
def _animated(clip, prefix, names, clock, fps, scale):
    """{property: FFmpeg expression} for the animated properties of one effect."""
    from render_movie import MAX_KEYFRAMES
    values = {}
    keyframes = clip.get('keyframes') or {}
    for name in names:
        points = keyframes.get(prefix+name)
        if not points:
            continue
        if not isinstance(points, list) or len(points) > MAX_KEYFRAMES:
            raise ValueError(f'Invalid {prefix+name} keyframes')
        low, high = EFFECT_KEY_BOUNDS[prefix+name]
        checked = []
        for point in points:
            if not isinstance(point, dict):
                raise ValueError(f'Invalid {prefix+name} keyframe')
            validate_point_shape(point, prefix+name)
            checked.append({**point, 'time': _finite(point.get('time'), 'keyframe time', -48*3600, 48*3600),
                            'value': _finite(point.get('value'), 'keyframe value', low, high)})
        values[name] = f'(({keyframe_expr(checked, 0, low, high, clock, 1/float(fps))})*{scale!r})'
    return values


def picture_alpha(clip, effects, u, v, aspect, clock, fps):
    """FFmpeg expression of the crop and mask alpha (0..1) of a clip at picture coordinates u, v
    (``aspect``: an expression of the picture's width/height)."""
    terms = []
    crop = effects.get('crop')
    if crop:
        terms.append(crop_expr(crop, u, v, aspect, _animated(clip, 'crop.', CROP_PROPS, clock, fps, 0.01)))
    masks = effects.get('masks') or []
    if masks:
        numbers = effects.get('_maskIndex') or list(range(len(masks)))
        animated = {}
        for number in numbers:
            values = _animated(clip, f'mask.{number}.', [name for name in MASK_PROPS if name != 'rotation'], clock, fps, 0.01)
            values.update(_animated(clip, f'mask.{number}.', ['rotation'], clock, fps, 1.0))
            animated[number] = values
        terms.append(masks_expr(list(zip(numbers, masks)), u, v, aspect, animated))
    return '*'.join(f'({term})' for term in terms) if terms else None


def effects_for(clip):
    """The validated, live effects of a clip (None when nothing renders)."""
    checked = validate_effects(clip.get('effects'))
    if not checked or checked.get('bypass'):
        return None
    live = active(checked, clip.get('keyframes'))
    numbers = [number for number, mask in enumerate(checked.get('masks') or []) if mask.get('enabled', True)]
    live['masks'] = [checked['masks'][number] for number in numbers]
    live['_maskIndex'] = numbers
    if not (live.get('crop') or live['masks'] or live.get('key') or live['blend'] != 'normal' or live.get('stabilize')):
        return None
    return live


GEQ_NORM = 255*256      # yuv444p16le codes per (8-bit code / 255)


def fit_effects(clip, effects, matrix, clock, fps):
    """A geq that keys, despills, crops and masks the fitted picture (before its grade): it sets
    the alpha plane and the despilled chroma of a yuva444p16le frame. '' when nothing applies."""
    if not effects:
        return ''
    key = effects.get('key')
    alpha = picture_alpha(clip, effects, '((X+0.5)/W)', '((Y+0.5)/H)', '(W/H)', clock, fps)
    if not key and not alpha:
        return ''
    k = key_params(key, matrix) if key else None
    cb, cr = f'(cb(X,Y)/{GEQ_NORM})', f'(cr(X,Y)/{GEQ_NORM})'
    parts = ([key_alpha_expr(k, cb, cr)] if k else [])+([alpha] if alpha else [])
    matte = '*'.join(f'({part})' for part in parts)
    chroma = (f":cb='round({GEQ_NORM}*{despill_expr(k, cb, cr, 0)})':cr='round({GEQ_NORM}*{despill_expr(k, cb, cr, 1)})'"
              if k and k['spill'] > 0 else ":cb='cb(X,Y)':cr='cr(X,Y)'")
    return f",format=yuva444p16le,geq=lum='lum(X,Y)'{chroma}:a='clip(round(alpha(X,Y)*({matte})),0,65535)'"


def blend_composite(filters, base, layer, output, position, first, frames, window_frames, base_format, mode, overlay=None,
                    matrix='bt709'):
    """Composite a positioned layer over ``base`` with a blend mode (W3C separable formulas on
    float R'G'B'): the result is mixed with the layer's own colour by the backdrop's alpha, then
    composited by the layer's alpha, as the monitor's blend pass does."""
    from color_grade import SETPARAMS_MATRIX, WORKING_FORMAT, ZSCALE_MATRIX
    from render_movie import _composite_layer
    timing = f'setpts=PTS-STARTPTS,tpad=start={max(0,first)}:stop={max(0,window_frames-first-frames)}:color=black@0'
    if first < 0:
        timing = f'trim=start_frame={-first},' + timing
    to_rgb = f'setparams=colorspace={SETPARAMS_MATRIX[matrix]}:range=tv,zscale,format={WORKING_FORMAT}'
    layer_rgb = to_rgb
    alpha = base_format.startswith('yuva')
    o = output
    filters.append(f'[{layer}]format=yuva444p16le,{position},setsar=1,{timing},split[{o}_fg][{o}_a]')
    filters.append(f'[{base}]split[{o}_bg][{o}_b]')
    if alpha:
        filters.append(f'[{o}_fg]{layer_rgb},split[{o}_s][{o}_own]')
        filters.append(f'[{o}_b]split[{o}_b1][{o}_b2]')
        filters.append(f'[{o}_b1]{to_rgb}[{o}_d]')
        filters.append(f"[{o}_s][{o}_d]blend=all_expr='{blend_expr(mode)}'[{o}_m0]")
        filters.append(f'[{o}_b2]extractplanes=a,format={WORKING_FORMAT}[{o}_bm]')
        filters.append(f'[{o}_own][{o}_m0][{o}_bm]maskedmerge[{o}_m]')
    else:
        filters.append(f'[{o}_fg]{layer_rgb}[{o}_s]')
        filters.append(f'[{o}_b]{to_rgb}[{o}_d]')
        filters.append(f"[{o}_s][{o}_d]blend=all_expr='{blend_expr(mode)}'[{o}_m]")
    # zscale quantizes to the composite's own depth: swscale's 16- to 10-bit step inside overlay
    # moves non-code-aligned chroma by about two 10-bit steps.
    if 'p16' in base_format:
        # At 16 bits (a ProRes 4444 alpha delivery, 16-bit deliveries) the layer's own alpha plane
        # goes to the compositor beside the colour: alphamerge takes only 8-bit formats, so attaching
        # it rounded a 16-bit blend to 8 bits.
        filters.append(f'[{o}_m]zscale=m={ZSCALE_MATRIX[matrix]}:r=limited,format=yuv444p16le[{o}_layer]')
        filters.append(f'[{o}_a]extractplanes=a[{o}_alpha]')
        _composite_layer(filters, f'{o}_bg', f'{o}_layer', o, 'null', None, 0, window_frames, window_frames, base_format,
                         alpha_stream=f'{o}_alpha')
        return
    filters.append(f'[{o}_m]zscale=m={ZSCALE_MATRIX[matrix]}:r=limited,format=yuv444p10le[{o}_c]')
    filters.append(f'[{o}_a]alphaextract,format=gray10le[{o}_alpha]')
    filters.append(f'[{o}_c]format=yuva444p10le[{o}_ca];[{o}_ca][{o}_alpha]alphamerge[{o}_layer]')
    # The layer is already on the frame and timed: composite it in place.
    _composite_layer(filters, f'{o}_bg', f'{o}_layer', o, 'x=0:y=0' if overlay else 'null', overlay, 0, window_frames,
                     window_frames, base_format)


def adjustment_alpha(clip, clock, fps):
    """A masked adjustment clip's crop and masks (frame coordinates) as an alpha expression."""
    live = effects_for(clip)
    if not live:
        return None
    return picture_alpha(clip, live, '((X+0.5)/W)', '((Y+0.5)/H)', '(W/H)', clock, fps)


# ---------------------------------------------------------------- export: ramp audio
RAMP_AUDIO_GRID = 0.001     # the curve is read on this grid (seconds), linear between
RAMP_HELD_SPEED = 0.1       # slower than this, sound is silent (a freeze has no sound)
RAMP_GRAIN = 0.020          # pitch-preserving stretch: grain hop (Hann grains two hops long, 50 % overlap)
RAMP_SEEK = 0.0125          # the most a grain's read point moves off the curve to continue the waveform
RAMP_MIN_GRAIN = 0.004      # the shortest grain (between a transient and the curve)
RAMP_LEAD = 0.001           # a transient's attack: read this much before its onset
RAMP_HEAD = 0.006           # a transient plays at normal speed this long after its onset,
RAMP_HEAD_FADE = 0.004      # then hands over to the stretched sound over this long
RAMP_CHUNK = 2.0            # seconds of output finished (and held) at a time
RAMP_SOURCE_WINDOW = 4.0    # seconds of source held in memory at a time (at least)
SINC_ZEROS = 8              # varispeed: windowed-sinc half-width, in zero crossings
SINC_PHASES = 256           # varispeed: sub-sample positions the kernel is tabulated at
SINC_STEPS = 8              # varispeed: anti-alias cutoffs per octave of speed


class _SourceReader:
    """Interleaved float32 source frames, held a window at a time: from an array (in memory
    already) or a raw f32 file (the export reads its source span to disk first), so a long
    clip never sits in memory whole. Frames outside the source read as silence."""
    def __init__(self, channels, frames, rate, data=None, path=None):
        self.channels, self.frames, self.rate = channels, frames, rate
        self.data = data
        self.handle = open(path, 'rb') if path is not None else None
        self.window = frames if data is not None else max(1, int(RAMP_SOURCE_WINDOW*rate))
        self.lo = self.hi = 0
        self.buf = self.mono = array.array('f')

    def close(self):
        if self.handle is not None:
            self.handle.close()

    def ensure(self, lo, hi):
        """Hold source frames lo..hi (as far as the source has them)."""
        lo, hi = max(0, int(lo)), min(self.frames, int(hi))
        if hi <= lo or (lo >= self.lo and hi <= self.hi):
            return
        # A file is read from a little before the frames asked for (a grain's alignment looks back).
        lo = 0 if self.data is not None else max(0, lo-self.window//4)
        hi = min(self.frames, max(hi, lo+self.window))
        ch = self.channels
        if self.data is not None:
            buf = self.data if (lo, hi) == (0, self.frames) and isinstance(self.data, array.array) else array.array('f', self.data[lo*ch:hi*ch])
        else:
            self.handle.seek(lo*ch*4)
            buf = array.array('f')
            buf.frombytes(self.handle.read((hi-lo)*ch*4))
        if ch == 1:
            mono = buf
        else:
            from operator import add
            from functools import reduce
            mono = array.array('f', map((1/ch).__mul__, reduce(lambda a, b: map(add, a, b), (buf[c::ch] for c in range(ch)))))
        self.lo, self.hi, self.buf, self.mono = lo, lo+len(mono), buf, mono

    def frames_at(self, start, length):
        """Interleaved frames start..start+length (silence outside the source)."""
        ch = self.channels
        self.ensure(start, start+length)
        first, last = max(start, self.lo), min(start+length, self.hi)
        if first == start and last == start+length:
            return self.buf[(start-self.lo)*ch:(last-self.lo)*ch]
        out = array.array('f', bytes(4*length*ch))
        if last > first:
            out[(first-start)*ch:(last-start)*ch] = self.buf[(first-self.lo)*ch:(last-self.lo)*ch]
        return out

    def mono_at(self, start, length, step=1):
        """Mono samples start, start+step, ... (length samples); None when not all inside the source."""
        if start < 0 or start+length > self.frames:
            return None
        self.ensure(start, start+length)
        return self.mono[start-self.lo:start-self.lo+length:step]


def _transients(reader):
    """Sorted source frames where the sound jumps (a 1 ms block much louder than the 20 ms before
    it), at most one per 10 ms: the first frame of the jump that reaches half the block's peak.
    Read a window at a time."""
    block = max(1, reader.rate//1000)
    history, found = [], []
    step = block*max(1, reader.window//block)
    for start in range(0, reader.frames, step):
        reader.ensure(start, min(reader.frames, start+step))
        for first in range(start, min(reader.frames, start+step), block):
            samples = reader.mono_at(first, min(block, reader.frames-first))
            level = max(map(abs, samples))
            back = history[-20:-2] if len(history) >= 3 else []
            # One onset per 10 ms: a noisy attack keeps jumping for a few blocks.
            if level > 4*(sum(back)/len(back) if back else 0.0)+0.01 and not (found and found[-1] >= first-10*block):
                found.append(first+next(i for i, v in enumerate(samples) if abs(v) >= level/2))
            history.append(level)
            if len(history) > 40:
                del history[:20]
    return found


def _hann(length, cache={}):
    window = cache.get(length)
    if window is None:
        window = cache[length] = array.array('d', [0.5-0.5*math.cos(2*math.pi*(i+0.5)/length) for i in range(length)])
    return window


class _Accumulator:
    """Overlap-add of weighted grains into output frames 0..count, finished a chunk at a time and
    handed to ``write``. Stretched grains are normalised by their summed windows; transient grains
    (``head``) are kept apart and duck them: where a transient's window is 1 only it sounds, and
    over its fades the two cross."""
    def __init__(self, channels, count, rate, write):
        self.channels, self.count, self.write = channels, count, write
        self.chunk = max(1, int(RAMP_CHUNK*rate))
        self.size = 2*self.chunk
        self.base = 0
        self.out = [array.array('d', bytes(8*self.size*channels)) for _ in range(2)]
        self.weight = [array.array('d', bytes(8*self.size)) for _ in range(2)]

    def add(self, origin, frames, window, head=False):
        """Add ``frames`` (interleaved, len(window) frames; None: silence) times ``window`` at
        output frame ``origin``."""
        from operator import add, mul
        length = len(window)
        first, last = max(origin, self.base, 0), min(origin+length, self.count)
        if last <= first:
            return
        if last-self.base > self.size:
            raise RuntimeError('A grain reached past the overlap-add window')
        ch, out, weight = self.channels, self.out[head], self.weight[head]
        part = window[first-origin:last-origin]
        at = first-self.base
        weight[at:at+len(part)] = array.array('d', map(add, weight[at:at+len(part)], part))
        if frames is not None:
            for c in range(ch):
                to = slice(at*ch+c, (at+len(part))*ch, ch)
                out[to] = array.array('d', map(add, out[to], map(mul, frames[(first-origin)*ch+c:(last-origin)*ch:ch], part)))

    def finish(self, upto):
        """Output frames before ``upto`` are final: write them (a chunk at a time). True when written."""
        upto = min(upto, self.count)
        if upto <= self.base or (upto-self.base < self.chunk and upto < self.count):
            return False
        done = upto-self.base
        ch = self.channels
        # Each frame: duck*heads/head weight + (1-duck)*grains/grain weight, duck = min(1, head weight).
        grains, heads = self.weight
        own = [(1-min(1.0, h))/g if g > 1e-3 else 0.0 for g, h in zip(grains[:done], heads[:done])]
        lent = [min(1.0, h)/h if h > 1e-9 else 0.0 for h in heads[:done]]
        block = array.array('f', bytes(4*done*ch))
        for c in range(ch):
            block[c::ch] = array.array('f', map(float.__add__, map(float.__mul__, self.out[0][c:done*ch:ch], own),
                                                map(float.__mul__, self.out[1][c:done*ch:ch], lent)))
        self.write(block)
        for k in range(2):
            self.out[k] = self.out[k][done*ch:]+array.array('d', bytes(8*done*ch))
            self.weight[k] = self.weight[k][done:]+array.array('d', bytes(8*done))
        self.base = upto
        return True


def _stretch_along(source, channels, frames, count, rate, place, speed, playing, *, sink=None, checkpoint=None):
    """Pitch-preserving playback along a speed curve (WSOLA). Output frame n plays the source around
    frame ``place(n)`` (a non-decreasing float index) at normal pitch; ``speed(n)`` is the curve's
    speed there; where ``playing(n)`` is false (a freeze) the sound fades to silence.

    Hann grains two hops long overlap by half (RAMP_GRAIN hops), each read on the curve and moved
    by at most RAMP_SEEK so its waveform continues the previous grain's. A transient (an onset)
    plays once, at normal speed, exactly where the curve reaches it: its own grain (RAMP_HEAD,
    then a RAMP_HEAD_FADE hand-over) replaces the stretched sound beneath it, no stretched grain
    reads across an onset (none plays it early, none plays it again later: no echo, no flam), and
    a stretched grain beside one is shortened so it stays centred on the curve. The result is
    exactly ``count`` frames. ``source``: an interleaved float array or a _SourceReader;
    ``sink``: called with each finished block (interleaved float array), else the whole result is
    returned. ``checkpoint`` is called between blocks (to cancel)."""
    import bisect
    reader = source if isinstance(source, _SourceReader) else _SourceReader(channels, frames, rate, data=source)
    blocks = []
    accumulator = _Accumulator(channels, count, rate, sink or blocks.append)
    hop = max(32, int(round(RAMP_GRAIN*rate)))
    reach = int(round(RAMP_SEEK*rate))
    minimum = max(16, int(round(RAMP_MIN_GRAIN*rate))) & ~1
    lead, head, fade = (int(round(seconds*rate)) for seconds in (RAMP_LEAD, RAMP_HEAD, RAMP_HEAD_FADE))
    last = count-1

    def at(n):
        return place(min(max(n, 0), last))

    def landing(target):
        """The output frame (float) where place() reaches source frame ``target``."""
        low, high = 0.0, float(last)
        for _ in range(48):
            middle = (low+high)/2
            if at(middle) < target:
                low = middle
            else:
                high = middle
        return high

    transients = _transients(reader)
    floors = [t+head+fade//2 for t in transients]       # stretched grains read no earlier than this after onset k
    heads = []      # (output frame, source frame, frames after the onset, fade, transient index)
    first_place, last_place = at(0), at(last)
    marks = [(landing(t), t, k) for k, t in enumerate(transients) if first_place <= t <= last_place]
    for number, (when, onset, k) in enumerate(marks):
        if not playing(min(last, int(when))):
            continue
        after = head+fade
        if number+1 < len(marks):
            after = min(after, int(marks[number+1][0]-when)-lead)
        if k+1 < len(transients):
            after = min(after, transients[k+1]-lead-onset)
        if after < 2:
            continue
        tail = min(fade, after//2)
        heads.append((int(round(when)), onset, after, tail, k))
        floors[k] = onset+after-tail//2

    def score(start, target, step):
        candidate = reader.mono_at(start, len(target)*step, step)
        if candidate is None:
            return None
        return sum(map(float.__mul__, target, candidate))/math.sqrt(sum(map(float.__mul__, candidate, candidate))+1e-12)

    def align(start, natural, span, low, high):
        """``start`` moved within low..high (at most RAMP_SEEK) to best continue the source from ``natural``."""
        coarse = reader.mono_at(natural, span, 8)
        if coarse is None or sum(v*v for v in coarse) <= 1e-9:
            return start
        coarse = array.array('d', coarse)
        best, shift = score(start, coarse, 8), 0
        for d in range(max(-reach, low-start), min(reach, high-start)+1, 4):
            value = score(start+d, coarse, 8)
            if value is not None and (best is None or value > best+1e-6 or (value > best-1e-6 and abs(d) < abs(shift))):
                best, shift = value, d
        if shift:
            fine = array.array('d', reader.mono_at(natural, span))
            best = score(start+shift, fine, 1)
            for d in range(max(shift-2, low-start), min(shift+2, high-start)+1):
                value = score(start+d, fine, 1)
                if value is not None and best is not None and value > best+1e-6:
                    best, shift = value, d
        return start+shift

    next_head = 0
    previous = None        # (output origin, source start, length) of the last grain that played
    handover = None        # the last transient grain, which the next stretched grain continues
    n = 0
    while n < count+hop:
        while next_head < len(heads) and heads[next_head][0]-lead < n+hop:
            when, onset, after, tail, _ = heads[next_head]
            length = lead+after
            window = array.array('d', [1.0]*length)
            for i in range(lead):
                window[i] = 0.5-0.5*math.cos(math.pi*(i+0.5)/lead)
            for i in range(tail):
                window[length-tail+i] = 0.5+0.5*math.cos(math.pi*(i+0.5)/tail)
            accumulator.add(when-lead, reader.frames_at(onset-lead, length), window, head=True)
            handover = (when-lead, onset-lead, length, when)
            next_head += 1
        centre = at(n)
        k = bisect.bisect_right(transients, centre)
        floor = floors[k-1] if k else None
        ceiling = transients[k]-lead if k < len(transients) else None
        length = 2*hop
        if floor is not None:
            length = min(length, int(2*(centre-floor)))
        if ceiling is not None:
            length = min(length, int(2*(ceiling-centre)))
        length = max(minimum, length) & ~1
        origin = n-length//2
        if not playing(min(max(n, 0), last)):
            accumulator.add(origin, None, _hann(length))
            previous = handover = None
        else:
            low = floor if floor is not None else -10**12
            high = ceiling-length if ceiling is not None else 10**12
            high = max(high, low)
            start = min(max(int(round(centre))-length//2, low), high)
            if handover is not None and n >= handover[3]:
                previous, handover = handover[:3], None
            if previous is not None:
                overlap = previous[0]+previous[2]-origin
                span = min(length//2, overlap, 480)
                if span >= 16:
                    start = align(start, previous[1]+(origin-previous[0]), span, low, high)
            reader.ensure(start-reach, start+length+reach)
            accumulator.add(origin, reader.frames_at(start, length), _hann(length))
            previous = (origin, start, length)
        n += max(1, length//2)
        if accumulator.finish(n-2*hop-lead-head-fade) and checkpoint:
            checkpoint()
    accumulator.finish(count)
    if accumulator.base < count:
        raise RuntimeError('The stretch did not finish its output')
    if sink is None:
        out = array.array('f')
        for block in blocks:
            out.extend(block)
        return out
    return None


_SINC = {}


def _sinc_table(step):
    """Windowed-sinc kernels (Blackman) for a cutoff of 0.95/2**(step/SINC_STEPS) of the source's
    Nyquist frequency, at SINC_PHASES sub-sample positions: (half width, [taps per phase]). Each
    kernel sums to 1."""
    table = _SINC.get(step)
    if table is None:
        cutoff = 0.95/2**(step/SINC_STEPS)
        half = int(math.ceil(SINC_ZEROS/cutoff))
        kernels = []
        for phase in range(SINC_PHASES):
            fraction = phase/SINC_PHASES
            taps = []
            for j in range(-half+1, half+1):
                x = j-fraction
                window = 0.42+0.5*math.cos(math.pi*x/half)+0.08*math.cos(2*math.pi*x/half) if abs(x) < half else 0.0
                taps.append(cutoff*(math.sin(math.pi*cutoff*x)/(math.pi*cutoff*x) if x else 1.0)*window)
            total = sum(taps)
            kernels.append(array.array('f', [t/total for t in taps]))
        table = _SINC[step] = (half, kernels)
    return table


def _varispeed(reader, count, rate, place, speed, sink, checkpoint=None):
    """Varispeed along the curve: output frame n is the source at frame ``place(n)`` (exact on a
    RAMP_AUDIO_GRID grid, linear between), band-limited by a windowed sinc whose cutoff follows the
    speed (a 9 kHz tone played at 4x is 36 kHz: removed, not folded back to 12 kHz). Below 5 %
    speed the sound fades out (a freeze is silent), read every grid step."""
    from operator import mul
    ch = reader.channels
    step = max(1, round(RAMP_AUDIO_GRID*rate))
    chunk = max(step, int(RAMP_CHUNK*rate)//step*step)
    for begin in range(0, count, chunk):
        end = min(count, begin+chunk)
        block = array.array('f', bytes(4*(end-begin)*ch))
        for g in range(begin, end, step):
            a, b = place(g), place(g+step)
            gate = min(1.0, abs(speed(g))/0.05)
            rise = abs(b-a)/step
            octave = SINC_STEPS*math.log2(rise) if rise > 1 else 0.0
            half, kernels = _sinc_table(max(0, math.ceil(octave-1e-9)))
            reader.ensure(int(min(a, b))-half-2, int(max(a, b))+half+2)
            for n in range(g, min(end, g+step)):
                p = a+(b-a)*(n-g)/step
                k0 = math.floor(p)
                phase = int((p-k0)*SINC_PHASES)
                if phase >= SINC_PHASES:
                    k0, phase = k0+1, 0
                taps = kernels[phase]
                frames = reader.frames_at(k0-half+1, 2*half)
                at = (n-begin)*ch
                for c in range(ch):
                    block[at+c] = sum(map(mul, taps, frames[c::ch]))*gate if gate else 0.0
        sink(block)
        if checkpoint:
            checkpoint()


def prerender_ramp(event, u0, u1, target, directory, cancelled, heartbeat, layout='stereo'):
    """Render event-local u0..u1 of a speed-ramped clip's sound: the source is read along the
    same integral as the picture (ramp_source), then played as varispeed (_varispeed: resampled
    along the curve), pitch-preserving (_stretch_along: grains read on the curve) or muted. The
    source span goes to a file first and both are read and written a window at a time."""
    from pathlib import Path
    from render_movie import (AUDIO_RATE, _envelope_filter, _graph_args, _layout_channels, _layout_name,
                              _run_ffmpeg, _seek_offset, _trim_audio)
    info = event['ramp']
    ramp, before, mode = info['ramp'], info['before'], info['mode']
    rate = AUDIO_RATE
    count = max(1, math.ceil((u1-u0)*rate))
    origin = info['origin']
    low = origin+ramp.source(u0-before)
    high = origin+ramp.source(u1-before)
    streams = event['streams']
    channels_in = 2 if len(streams) > 1 else next(iter(streams.values()))
    stem = Path(directory)/Path(target).stem
    raw_source, raw_warped = Path(f'{stem}-source.f32'), Path(f'{stem}-warped.f32')
    margin = 0.05
    read_low = max(0.0, low-margin)
    offset = _seek_offset(read_low)

    def checkpoint():
        if cancelled and cancelled.is_set():
            raise InterruptedError('Export cancelled')
        if heartbeat:
            heartbeat()

    def local(n):
        return u0+n/rate-before
    reader = None
    try:
        silent = mode == 'mute' or high-low < 1/rate
        if not silent:
            filters = []
            chain = _trim_audio(filters, 'ramp', 0, streams, read_low-offset, high-read_low+margin, False, event.get('rates'), offset)
            filters.append(chain+f'aformat=sample_fmts=flt:sample_rates={rate}[aout]')
            seek = ['-ss', f'{offset:.0f}'] if offset > 0 else []
            _run_ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *seek,
                         '-t', f'{high-offset+margin+.25:.6f}', '-protocol_whitelist', 'file', '-i', str(event['path']),
                         *_graph_args(';'.join(filters), directory, Path(target).stem+'-read'), '-map', '[aout]',
                         '-f', 'f32le', '-ac', str(channels_in), '-ar', str(rate), str(raw_source)], cancelled, heartbeat)
            reader = _SourceReader(channels_in, raw_source.stat().st_size//(4*channels_in), rate, path=raw_source)
        with open(raw_warped, 'wb') as warped:
            def write(block):
                warped.write(block.tobytes())

            def place(n):
                return (origin+ramp.source(local(n))-read_low)*rate

            def speed(n):
                return ramp.speed(local(n))
            if silent:
                for begin in range(0, count, rate):
                    write(array.array('f', bytes(4*min(rate, count-begin)*channels_in)))
            elif mode == 'varispeed':
                _varispeed(reader, count, rate, place, speed, write, checkpoint)
            else:
                _stretch_along(reader, channels_in, reader.frames, count, rate, place, speed,
                               lambda n: ramp.speed(local(n)) >= RAMP_HELD_SPEED, sink=write, checkpoint=checkpoint)
        if reader is not None:
            reader.close()
            reader = None
            raw_source.unlink(missing_ok=True)
        channels, name = _layout_channels(layout), _layout_name(layout)
        chain = (f"[0:a]{event['processing']},aformat=sample_fmts=flt:sample_rates={rate},apad,atrim=end_sample={count+2}"
                 + _envelope_filter(event, f'(t+{u0:.9f})' if u0 else 't'))
        if event.get('post'):
            chain += ','+event['post']
        filters = [chain+f',aformat=sample_fmts=flt:sample_rates={rate}:channel_layouts={name},asetpts=PTS-STARTPTS[aout]']
        _run_ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'f32le', '-ar', str(rate),
                     '-ac', str(channels_in), '-i', str(raw_warped),
                     *_graph_args(';'.join(filters), directory, Path(target).stem), '-map', '[aout]',
                     '-c:a', 'pcm_f32le', '-ar', str(rate), '-ac', str(channels), str(target)], cancelled, heartbeat)
    finally:
        if reader is not None:
            reader.close()
        raw_source.unlink(missing_ok=True)
        raw_warped.unlink(missing_ok=True)
    return dict(path=Path(target), streams={0: channels}, rates={0: rate}, source=0.0, length=u1-u0, speed=1.0,
                reverse=False, start=event['start']+u0, processing='anull', envelope=[], pan=None, post='',
                stateful=False, prerendered=True, track=event.get('track'))


# ---------------------------------------------------------------- stabilization (vidstab, two passes)
# Analyses live in the media store, named by a hash of the source file and its trimmed range: a
# project names only the 32-hex analysis id, never a path, so nothing a project holds reaches a
# filtergraph but numbers this module formats itself.
STABILIZE_VERSION = 'vidstab-3'
DETECT_OPTIONS = 'shakiness=6:accuracy=15:stepsize=6:mincontrast=0.2'
_vidstab = None


def vidstab_available():
    global _vidstab
    if _vidstab is None:
        import subprocess
        try:
            names = splice_platform.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            names = ''
        _vidstab = ' vidstabdetect ' in names and ' vidstabtransform ' in names
    return _vidstab


VIDSTAB_MISSING = ('This FFmpeg was built without libvidstab, so Splice cannot stabilize. '
                   'Install an FFmpeg built with --enable-libvidstab (most distribution and static builds include it).')


def stabilize_store():
    from premiere import MEDIA_ROOT
    return MEDIA_ROOT / '.effects' / 'stabilize'


def _analysis_dir(analysis):
    if not isinstance(analysis, str) or not ANALYSIS_ID.match(analysis):
        raise ValueError('Invalid stabilization analysis')
    return stabilize_store() / analysis


def _probe_frames(path, low, high):
    """Presentation times (seconds, file timestamps) of the video frames around source low..high,
    and the container start time. Source times count from the container start, as the export
    counts them (render_movie._probe, _input_time_shift) and as FFmpeg's input -ss does: on an
    MPEG-TS file whose sound starts first, the picture starts a little after source time 0."""
    import json
    probe = splice_platform.run(['ffprobe', '-v', 'error', '-show_entries', 'format=start_time', '-of', 'json', str(path)],
                           capture_output=True, text=True, timeout=60, check=True)
    start = json.loads(probe.stdout).get('format', {}).get('start_time')
    try:
        start = float(start) if start not in (None, 'N/A') else 0.0
    except ValueError:
        start = 0.0
    if not math.isfinite(start):
        start = 0.0
    # -read_intervals takes file timestamps, not source times.
    result = splice_platform.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                             '-read_intervals', f'{max(0.0, start+low-3):.6f}%{start+high+1:.6f}',
                             '-show_entries', 'frame=pts_time,best_effort_timestamp_time', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=600, check=True)
    data = json.loads(result.stdout)
    times = sorted({float(frame.get('pts_time', frame.get('best_effort_timestamp_time'))) for frame in data.get('frames', [])
                    if frame.get('pts_time', frame.get('best_effort_timestamp_time')) not in (None, 'N/A')})
    return times, start


def _settings_key(settings):
    return f"{int(round(settings['smoothing']))}-{int(round(settings['maxShift']))}-{settings['zoom']:.1f}-{int(settings['autoZoom'])}-{1 if settings.get('tripod') else 0}"


def _transform_settings(settings):
    out = {}
    for name, (low, high, default) in STABILIZE_PROPS.items():
        out[name] = _finite((settings or {}).get(name, default), 'stabilization '+name, low, high)
    if out['autoZoom'] != int(out['autoZoom']):
        raise ValueError('Invalid stabilization autoZoom')
    tripod = (settings or {}).get('tripod', False)
    if not isinstance(tripod, bool):
        raise ValueError('Invalid stabilization tripod')
    out['tripod'] = tripod
    return out


class Stabilizer:
    """Background vidstab analyses and monitor previews (server.py): one job per source range (or
    per analysis and setting), kept on disk. An analysis can be cancelled (the editor cancels it
    when no clip uses it any more: Reset, a deleted clip)."""
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.jobs = {}
        self.previews = {}

    def start(self, src, source_in, source_out):
        import hashlib, json, threading
        from premiere import _media_path
        if not vidstab_available():
            raise ValueError(VIDSTAB_MISSING)
        if not isinstance(src, str) or not src:
            raise ValueError('Invalid stabilization source')
        source_in = _finite(source_in, 'stabilization in point', 0, 48*3600)
        source_out = _finite(source_out, 'stabilization out point', 0, 48*3600)
        if source_out <= source_in or source_out-source_in > 3*3600:
            raise ValueError('Stabilize a range of up to three hours')
        path = _media_path(src)
        if _is_raw({'src': src}):
            raise ValueError(STABILIZE_RAW)
        stat = path.stat()
        analysis = hashlib.sha256(f'{STABILIZE_VERSION}|{path}|{stat.st_size}|{stat.st_mtime_ns}|{source_in:.6f}|{source_out:.6f}'.encode()).hexdigest()[:32]
        directory = _analysis_dir(analysis)
        # Decide under the lock; status() takes the lock itself (it is not re-entrant), so it is
        # asked only once the lock is released.
        with self.lock:
            job = self.jobs.get(analysis)
            known = (job and job['state'] in ('running', 'done')) or (directory/'analysis.json').exists()
            if not known:
                job = self.jobs[analysis] = {'state': 'running', 'progress': 0.0, 'analysis': analysis,
                                             '_cancel': threading.Event(), '_process': None}
                directory.mkdir(parents=True, exist_ok=True)
                job['_thread'] = threading.Thread(target=self._detect, args=(analysis, path, source_in, source_out, directory, job),
                                                  daemon=True)
                job['_thread'].start()
        return self.status(analysis)

    def cancel(self, analysis):
        """Stop a running analysis and remove what it wrote. It reports cancelled only once its
        FFmpeg has exited and its files are gone (the analysis thread removes them as it ends).
        A finished analysis is kept."""
        import subprocess
        if not isinstance(analysis, str) or not ANALYSIS_ID.match(analysis):
            raise ValueError('Invalid stabilization analysis id')
        with self.lock:
            job = self.jobs.get(analysis)
            if not job or job['state'] != 'running':
                return {'state': (job or {}).get('state', 'unknown'), 'analysis': analysis}
            job['_cancel'].set()
            process, thread = job.get('_process'), job.get('_thread')
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if thread is not None:
            thread.join(120)
        with self.lock:
            if self.jobs.get(analysis) is job:
                self.jobs[analysis] = {'state': 'cancelled', 'analysis': analysis, 'error': 'Analysis cancelled: analyze the clip again'}
            state = self.jobs[analysis]['state']
        return {'state': state, 'analysis': analysis}

    def _run(self, job, command, progress=None):
        """One vidstab pass, stoppable by cancel(): ``progress(line)`` reads FFmpeg's -progress lines."""
        import subprocess
        with self.lock:
            if job['_cancel'].is_set():
                raise InterruptedError('cancelled')
            process = job['_process'] = splice_platform.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                         stderr=subprocess.PIPE, text=True)
        errors = []
        import threading
        reader = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)
        reader.start()
        try:
            for line in process.stdout:
                if progress:
                    try:
                        progress(line.strip())
                    except ValueError:
                        continue
            code = process.wait()
            reader.join(5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
        if job['_cancel'].is_set():
            raise InterruptedError('cancelled')
        if code != 0:
            raise RuntimeError('Motion analysis failed: '+''.join(errors).strip()[-400:])

    def _detect(self, analysis, path, source_in, source_out, directory, job):
        import json, os, uuid
        trf = tripod = None
        try:
            times, start = _probe_frames(path, source_in+0, source_out+0)
            # Source times are measured from the container start, as the editor's are.
            low, high = source_in+start, source_out+start
            # From the frame on screen at the in point (it may start before it: an in point between
            # frames, or a picture that starts after the container) to the last one before the out.
            showing = [t for t in times if t <= low+1e-9]
            inside = [t for t in times if (showing[-1] if showing else low) <= t < high-1e-6]
            if len(inside) < 2:
                raise ValueError('The stabilized range holds fewer than two frames')
            first, last = inside[0], inside[-1]
            before = [t for t in times if t < first]
            after = [t for t in times if t > last]
            # Trim between frames, so the analysis and every transform read exactly these frames.
            trim_start = (before[-1]+first)/2 if before else first-1e-3
            trim_end = (last+after[0])/2 if after else last+1e-3
            meta = {'version': STABILIZE_VERSION, 'path': str(path), 'start': start, 'first': first, 'frames': len(inside),
                    'trim': [trim_start, trim_end], 'source': [source_in, source_out], 'offset': first-start}
            # This job's own files, moved into place only when both passes finish: a cancelled job
            # removes just its own, even when the clip is analyzed again meanwhile.
            token = uuid.uuid4().hex[:12]
            trf, tripod = directory/f'analysis-{token}.trf.part', directory/f'tripod-{token}.trf.part'
            # Both passes read exactly the analyzed frames: see _range_input.
            inputs, leading = _range_input(meta)
            command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-progress', 'pipe:1',
                       *inputs, '-map', '0:v:0',
                       '-vf', f"{leading}vidstabdetect=result={_vidstab_path(trf)}:{DETECT_OPTIONS}", '-f', 'null', '-']

            def progress(done, share=0.5, base=0.0):
                with self.lock:
                    if self.jobs.get(analysis) is job:
                        job['progress'] = min(0.99, max(0.0, base+share*done/len(inside)))
            self._run(job, command, _frames_progress(progress))
            if not trf.exists():
                raise RuntimeError('Motion analysis failed: no motion data was written')
            # Tripod mode measures every frame against the first: a second pass of its own.
            self._run(job, [*command[:command.index('-vf')+1],
                            f"{leading}vidstabdetect=result={_vidstab_path(tripod)}:{DETECT_OPTIONS}:tripod=1",
                            '-f', 'null', '-'], _frames_progress(lambda done: progress(done, 0.5, 0.5)))
            meta['shake'] = {'before': shake_metric(path, trim_start, trim_end, max(0.0, trim_start-start-RANGE_SEEK_MARGIN))}
            if job['_cancel'].is_set() or not tripod.exists():
                raise InterruptedError('cancelled') if job['_cancel'].is_set() else RuntimeError('Motion analysis failed')
            os.replace(trf, directory/'analysis.trf')
            os.replace(tripod, directory/'tripod.trf')
            (directory/'analysis.json').write_text(json.dumps(meta))
            with self.lock:
                if self.jobs.get(analysis) is job:
                    self.jobs[analysis] = {'state': 'done', 'analysis': analysis}
        except Exception as error:   # reported to the editor, which shows it on the clip
            with self.lock:
                # A cancelled job is settled by cancel(), after this thread has cleaned up.
                if self.jobs.get(analysis) is job and not job['_cancel'].is_set():
                    self.jobs[analysis] = {'state': 'failed', 'analysis': analysis, 'error': str(error)}
        finally:
            for name in (trf, tripod) if trf else ():
                name.unlink(missing_ok=True)

    def status(self, analysis):
        import json
        directory = _analysis_dir(analysis)
        with self.lock:
            job = {key: value for key, value in (self.jobs.get(analysis) or {}).items() if not key.startswith('_')}
        if job.get('state') in ('running', 'failed', 'cancelled'):
            return job
        meta_file = directory/'analysis.json'
        if not meta_file.exists():
            if job:
                return job
            raise KeyError('Unknown stabilization analysis: analyze the clip again')
        meta = json.loads(meta_file.read_text())
        return {'state': 'done', 'analysis': analysis, 'frames': meta['frames'], 'source': meta['source'],
                'shake': meta.get('shake'), 'offset': meta['offset']}

    def preview(self, analysis, settings):
        """A monitor proxy of the stabilized range (the same analysis and transform as the export),
        rendered in the background: {'state': 'running'} until it is ready, then {'state': 'done',
        'src', 'offset'}. The editor asks again until it is done."""
        import threading
        from premiere import MEDIA_ROOT
        settings = _transform_settings(settings)
        meta = analysis_meta(analysis)
        target = _analysis_dir(analysis)/f'preview-{_settings_key(settings)}.mp4'
        if target.exists():
            return {'state': 'done', 'src': '/media/'+str(target.relative_to(MEDIA_ROOT)), 'offset': meta['offset']}
        key = (analysis, _settings_key(settings))
        with self.lock:
            job = self.previews.get(key)
            if job and job['state'] == 'running':
                return {'state': 'running'}
            if job and job['state'] == 'failed':
                del self.previews[key]       # reported once; asking again retries
                return {'state': 'failed', 'error': job['error']}
            job = self.previews[key] = {'state': 'running'}

        def render():
            try:
                render_stabilized(analysis, settings, target, preview=True)
                with self.lock:
                    self.previews.pop(key, None)
            except Exception as error:   # reported to the editor on its next request
                with self.lock:
                    self.previews[key] = {'state': 'failed', 'error': str(error)}
        threading.Thread(target=render, daemon=True).start()
        return {'state': 'running'}


def _vidstab_path(path):
    from render_movie import _filter_path
    return _filter_path(path)


RANGE_SEEK_MARGIN = 2.0     # an MPEG-TS seek can land up to a GOP after the point asked for


def _range_input(meta):
    """FFmpeg input arguments and the leading filters that pass exactly the analyzed frames, in
    order. The input is seeked well before the range (input -ss counts from the container start,
    as meta['start'] does; timestamps stay the file's own), the trim cuts the range on the file's
    timestamps, and ``copy`` gives every frame its own buffer: vidstabtransform writes into the
    frame it receives, and when that is the decoder's buffer (once leading frames have been dropped
    by a trim or a seek, FFmpeg 8 hands it writable decoded frames) later frames decode from the
    written picture, so whole frames from other source times and double exposures came out."""
    trim_start, trim_end = meta['trim']
    arguments = ['-ss', f"{max(0.0, trim_start-meta['start']-RANGE_SEEK_MARGIN):.6f}", '-copyts',
                 '-protocol_whitelist', 'file', '-i', meta['path'], '-frames:v', str(int(meta['frames']))]
    return arguments, f'trim=start={trim_start:.9f}:end={trim_end:.9f},copy,'


def _frames_progress(callback):
    """A ``progress`` reader for _run: FFmpeg's ``frame=`` count (its output clock is not
    usable with -copyts)."""
    def read(line):
        if line.startswith('frame='):
            callback(int(line.split('=', 1)[1]))
    return read


def analysis_meta(analysis):
    import json
    meta_file = _analysis_dir(analysis)/'analysis.json'
    if not meta_file.exists():
        raise ValueError('This clip\'s stabilization analysis is missing: analyze the clip again')
    meta = json.loads(meta_file.read_text())
    if meta.get('version') != STABILIZE_VERSION:
        raise ValueError('This clip\'s stabilization analysis is outdated: analyze the clip again')
    return meta


def render_stabilized(analysis, settings, target, *, preview=False, cancelled=None, heartbeat=None, scratch=None):
    """Apply the analysis to exactly the analyzed frames. The export reads a lossless FFV1 file
    whose time 0 is the first analyzed frame; the monitor reads an H.264 proxy (with the sound)."""
    import os, subprocess
    from pathlib import Path
    from render_movie import _run_ffmpeg
    if not vidstab_available():
        raise ValueError(VIDSTAB_MISSING)
    meta = analysis_meta(analysis)
    directory = _analysis_dir(analysis)
    trf = directory/('tripod.trf' if settings.get('tripod') else 'analysis.trf')
    trim_start, trim_end = meta['trim']
    inputs, leading = _range_input(meta)
    picture = f"{leading}{stabilize_transform_args(settings, trf)},setpts=PTS-STARTPTS"
    # Each render writes its own partial file (two exports of one clip may run at once); the
    # finished file replaces the target atomically, and a cancelled or failed render removes it.
    import threading, uuid
    unique = f'{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}.part{Path(target).suffix}'
    partial = Path(scratch)/f'{Path(target).stem}.{unique}' if scratch else Path(f'{target}.{unique}')
    if preview:
        picture += ",scale='min(1280,iw)':-2:flags=bicubic,format=yuv420p"
        # The sound starts at the first analyzed frame, as the picture does.
        sound = ['-map', '0:a:0?', '-af', f"atrim=start={meta['first']:.9f}:end={trim_end:.9f},asetpts=PTS-STARTPTS",
                 '-c:a', 'aac', '-b:a', '160k']
        codec = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16', '-g', '12', '-movflags', '+faststart']
    else:
        sound = ['-an']
        # write_colr keeps the source's colour tags (MOV drops a matrix it cannot pair with
        # primaries and transfer); FFV1 stores limited range, which an untagged file reads as.
        codec = ['-c:v', 'ffv1', '-level', '3', '-g', '1', '-slicecrc', '0', '-movflags', '+write_colr']
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *inputs,
               '-map', '0:v:0', '-vf', picture, *codec, *sound,
               # Each stream starts at 0 (the MP4 muxer's edit lists; make_zero would shift both
               # streams by the B-frame delay instead).
               '-map_metadata', '-1', str(partial)]
    try:
        _run_ffmpeg(command, cancelled, heartbeat)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def stabilized_media(analysis, settings, cancelled=None, heartbeat=None, scratch=None):
    """The export's stabilized picture for an analysis (rendered once per setting, then kept).
    ``scratch``: the export's own work folder for the partial file."""
    settings = _transform_settings(settings)
    # MOV keeps the source's frame times exactly (Matroska rounds them to 1 ms, which moves an
    # in point just before a frame boundary onto the next frame).
    target = _analysis_dir(analysis)/f'export-{_settings_key(settings)}.mov'
    if not target.exists():
        render_stabilized(analysis, settings, target, cancelled=cancelled, heartbeat=heartbeat, scratch=scratch)
    return target, analysis_meta(analysis)


def shake_metric(path, start, end, seek=0.0, width=160):
    """Mean frame-to-frame global motion (pixels at ``width``): a block search on grey frames."""
    import array
    height = None
    raw = splice_platform.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{seek:.6f}', '-copyts', '-i', str(path), '-map', '0:v:0',
                          '-vf', f'trim=start={start:.9f}:end={end:.9f},scale={width}:-2,format=gray', '-f', 'rawvideo', '-'],
                         capture_output=True, timeout=1800, check=True).stdout
    probe = splice_platform.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{seek:.6f}', '-copyts', '-i', str(path), '-map', '0:v:0', '-frames:v', '1',
                            '-vf', f'trim=start={start:.9f},scale={width}:-2,format=gray', '-f', 'rawvideo', '-'],
                           capture_output=True, timeout=600, check=True).stdout
    height = len(probe)//width
    if not height:
        return 0.0
    frames = [raw[i:i+width*height] for i in range(0, len(raw)-width*height+1, width*height)]
    return mean_motion(frames, width, height)


def mean_motion(frames, width, height, radius=8, step=3):
    """Average |dx|+|dy| of the best integer shift between consecutive frames (sum of absolute
    differences over a centre window, every ``step`` pixels)."""
    if len(frames) < 2:
        return 0.0
    margin = radius+2
    xs = range(margin, width-margin, step)
    ys = range(margin, height-margin, step)
    total = 0.0
    for a, b in zip(frames, frames[1:]):
        best, best_cost = (0, 0), None
        for dy in range(-radius, radius+1):
            for dx in range(-radius, radius+1):
                cost = 0
                for y in ys:
                    row_a, row_b = y*width, (y+dy)*width+dx
                    for x in xs:
                        cost += abs(a[row_a+x]-b[row_b+x])
                if best_cost is None or cost < best_cost:
                    best, best_cost = (dx, dy), cost
        total += abs(best[0])+abs(best[1])
    return total/(len(frames)-1)


def needs_stabilization(project):
    """Whether any video clip of ``project`` names stabilization (live, bypassed or disabled)."""
    clips = project.get('clips') if isinstance(project, dict) else None
    return isinstance(clips, list) and any(isinstance(clip, dict) and clip.get('type') == 'video' and isinstance(clip.get('effects'), dict)
                                           and clip['effects'].get('stabilize') for clip in clips)


def apply_stabilization(project, cancelled=None, heartbeat=None, scratch=None):
    """Point every stabilized video clip's picture at its stabilized intermediate (a new media
    item whose time 0 is the first analyzed frame); the same clip's sound still reads the original
    media (``_soundMedia``/``_soundIn``, read by render_movie._audio_events). Returns the project
    (a copy when anything changed)."""
    import copy
    clips = project.get('clips')
    if not isinstance(clips, list) or not any(isinstance(clip, dict) and clip.get('type') == 'video' and clip.get('effects') for clip in clips):
        return project
    # Refused whether live, bypassed or disabled, as the editor refuses it at load (effects.js).
    items = {item.get('id'): item for item in project.get('media') or [] if isinstance(item, dict)}
    for clip in clips:
        if isinstance(clip, dict) and clip.get('type') == 'video' and isinstance(clip.get('effects'), dict) and clip['effects'].get('stabilize'):
            item = items.get(clip.get('mediaId')) or {}
            if item.get('nestedProject') or item.get('multicam') or item.get('nativeGenerator') is not None:
                raise ValueError(STABILIZE_SOURCE)
            if _is_raw(item):
                raise ValueError(STABILIZE_RAW)
    prepared = None
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict) or clip.get('type') != 'video' or clip.get('enabled') is False or clip.get('disabled'):
            continue
        live = effects_for(clip)
        stabilize = live and live.get('stabilize')
        if not stabilize:
            continue
        if prepared is None:
            prepared = copy.deepcopy(project)
            media_by_id = {item.get('id'): item for item in prepared.get('media') or [] if isinstance(item, dict)}
        target = prepared['clips'][index]
        item = media_by_id.get(target.get('mediaId'))
        if item is None or item.get('nestedProject') or item.get('multicam') or item.get('nativeGenerator') is not None:
            raise ValueError(STABILIZE_SOURCE)
        path, meta = stabilized_media(stabilize['analysis'], stabilize, cancelled, heartbeat, scratch=scratch)
        source_in = float(target.get('in', 0) or 0)
        span = source_delta(target, 0, float(target['duration']))
        low, high = meta['source']
        if source_in < low-0.02 or source_in+span > high+0.05:
            raise ValueError('A stabilized clip was trimmed past its analyzed range: analyze it again')
        from premiere import MEDIA_ROOT
        media_id = f"stabilized-{stabilize['analysis']}-{_settings_key(stabilize)}"
        if media_id not in media_by_id:
            stabilized = {key: value for key, value in item.items() if key not in ('audioStreams', 'audioStream', 'audioChannels')}
            stabilized.update(id=media_id, name=(item.get('name') or 'Clip')+' (stabilized)', src='/media/'+str(path.relative_to(MEDIA_ROOT)),
                              duration=meta['trim'][1]-meta['trim'][0], audioChannels=0)
            prepared['media'].append(stabilized)
            media_by_id[media_id] = stabilized
        # The clip keeps its own sound (same clip, so its crossfades and dips still find their
        # neighbours); only the picture reads the stabilized media.
        target['_soundMedia'], target['_soundIn'] = target.get('mediaId'), source_in
        target['mediaId'] = media_id
        target['in'] = max(0.0, source_in-meta['offset'])
        target['effects'] = {key: value for key, value in (target.get('effects') or {}).items() if key != 'stabilize'}
    return prepared or project
