/* Clip effects for the program monitor: keyframe easing, speed ramps, crop, masks, chroma key and
 * blend modes. effects_engine.py has the same maths for the export: each function here has a
 * twin there (snake_case) with the same operations in the same order, so they agree to rounding.
 * See effects_engine.py for the data model. gl-preview.js draws with SpliceEffects.GLSL and
 * bindUniforms; the Canvas fallback calls canvasPass and blendPixels.
 */
(() => {
  const INTERPOLATIONS = ['linear', 'hold', 'easeIn', 'easeOut', 'easeInOut', 'bezier'];
  const PRESET_HANDLES = {easeIn:[.42, 0, 1, 1], easeOut:[0, 0, .58, 1], easeInOut:[.42, 0, .58, 1]};
  const SOLVE_STEPS = 40, HANDLE_Y = [-2, 3];
  const BLEND_MODES = ['normal', 'multiply', 'screen', 'overlay', 'add', 'darken', 'lighten', 'softLight', 'hardLight', 'difference'];
  const BLEND_NAMES = {normal:'Normal', multiply:'Multiply', screen:'Screen', overlay:'Overlay', add:'Add', darken:'Darken',
    lighten:'Lighten', softLight:'Soft light', hardLight:'Hard light', difference:'Difference'};
  const MASK_SHAPES = ['rectangle', 'ellipse', 'polygon'];
  const MAX_MASKS = 8, MAX_POLYGON_POINTS = 16;
  const SPEED_AUDIO = ['pitch', 'varispeed', 'mute'];
  const SPEED_RANGE = [0, 1600];
  const MASK_PROPS = {x:[-100, 200, 50], y:[-100, 200, 50], width:[0, 400, 50], height:[0, 400, 50], rotation:[-360, 360, 0],
    feather:[0, 100, 0], expansion:[-100, 100, 0], opacity:[0, 100, 100]};
  const CROP_PROPS = {left:[0, 100, 0], right:[0, 100, 0], top:[0, 100, 0], bottom:[0, 100, 0], feather:[0, 100, 0]};
  const KEY_PROPS = {similarity:[0, 100, 20], smoothness:[0, 100, 10], spill:[0, 100, 50], choke:[0, 100, 0], feather:[0, 100, 0]};
  const STABILIZE_PROPS = {smoothing:[1, 120, 15], maxShift:[-1, 500, -1], zoom:[-50, 100, 0], autoZoom:[0, 2, 1]};
  const KEY_BOUNDS = {speed:SPEED_RANGE};
  for (const [name, [low, high]] of Object.entries(CROP_PROPS)) KEY_BOUNDS['crop.'+name] = [low, high];
  for (let index = 0; index < MAX_MASKS; index++) for (const [name, [low, high]] of Object.entries(MASK_PROPS)) KEY_BOUNDS[`mask.${index}.${name}`] = [low, high];
  const fail = message => { throw new Error(message); };
  const finite = (value, label, low, high) => {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < low || value > high) fail('Invalid '+label);
    return value;
  };
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

  // ---------------------------------------------------------------- keyframe curves
  function validatePointShape(point, label) {
    if (point.interp != null && !INTERPOLATIONS.includes(point.interp)) fail(`Invalid ${label} keyframe interpolation`);
    for (const name of ['inHandle', 'outHandle']) {
      const handle = point[name];
      if (handle == null) continue;
      if (!Array.isArray(handle) || handle.length !== 2) fail(`Invalid ${label} keyframe ${name}`);
      finite(handle[0], `${label} keyframe ${name}`, 0, 1);
      finite(handle[1], `${label} keyframe ${name}`, ...HANDLE_Y);
    }
    if ('ease' in point && typeof point.ease !== 'boolean') fail(`Invalid ${label} keyframe: ease must be true or false`);
  }
  const eased = points => points.slice(0, -1).some(point => point.ease === true || (point.interp != null && point.interp !== 'linear'));
  function segmentKind(a, b) {
    const interp = a.interp;
    if (interp === 'hold') return ['hold', 0, 0, 1, 1];
    if (PRESET_HANDLES[interp]) return ['bezier', ...PRESET_HANDLES[interp]];
    if (interp === 'bezier') {
      const [x1, y1] = a.outHandle || [1/3, 1/3], [x2, y2] = b.inHandle || [2/3, 2/3];
      return ['bezier', x1, y1, x2, y2];
    }
    if (interp == null && a.ease === true) return ['smooth', 0, 0, 1, 1];
    return ['linear', 0, 0, 1, 1];
  }
  function coefficients(p1, p2) { const c = 3*p1, b = 3*(p2-p1)-c; return [1-c-b, b, c]; }
  function bezierComponent(p1, p2, s) { const [a, b, c] = coefficients(p1, p2); return ((a*s+b)*s+c)*s; }
  function bezierSolve(x1, x2, u) {
    if (u <= 0) return 0;
    if (u >= 1) return 1;
    let low = 0, high = 1;
    for (let step = 0; step < SOLVE_STEPS; step++) {
      const middle = (low+high)/2;
      if (bezierComponent(x1, x2, middle) < u) low = middle; else high = middle;
    }
    return (low+high)/2;
  }
  function segmentFraction(kind, u) {
    const [name, x1, y1, x2, y2] = kind;
    if (name === 'hold') return u >= 1 ? 1 : 0;
    if (name === 'smooth') return u*u*(3-2*u);
    if (name === 'bezier') return bezierComponent(y1, y2, bezierSolve(x1, x2, u));
    return u;
  }
  const orderedPoints = points => (points || []).filter(p => p && Number.isFinite(p.time) && Number.isFinite(p.value)).sort((a, b) => a.time-b.time);
  // Sorted input (as the engine keeps it) skips the copy.
  function curveValue(points, local, base) {
    const ordered = isSorted(points) ? points : orderedPoints(points);
    if (!ordered.length) return base;
    if (local <= ordered[0].time) return ordered[0].value;
    for (let i = 1; i < ordered.length; i++) if (local <= ordered[i].time) {
      const a = ordered[i-1], b = ordered[i];
      if (local >= b.time && a.interp === 'hold') return b.value;
      const u = (local-a.time)/(b.time-a.time);
      return a.value+(b.value-a.value)*segmentFraction(segmentKind(a, b), u);
    }
    return ordered[ordered.length-1].value;
  }
  function isSorted(points) {
    if (!Array.isArray(points)) return false;
    for (let i = 1; i < points.length; i++) if (!(points[i].time > points[i-1].time)) return false;
    return points.every(p => Number.isFinite(p?.time) && Number.isFinite(p.value));
  }
  function splitSegment(kind, u) {
    const [, x1, y1, x2, y2] = kind, s = bezierSolve(x1, x2, u);
    const cut = (p1, p2) => {
      const a = [0, p1, p2, 1];
      const l1 = a[0]+(a[1]-a[0])*s, m = a[1]+(a[2]-a[1])*s, r2 = a[2]+(a[3]-a[2])*s;
      const l2 = l1+(m-l1)*s, r1 = m+(r2-m)*s, mid = l2+(r1-l2)*s;
      return [[l1, l2, mid], [r1, r2, mid]];
    };
    const [[lx1, lx2, xm], [rx1, rx2]] = cut(x1, x2), [[ly1, ly2, ym], [ry1, ry2]] = cut(y1, y2);
    const norm = (value, low, high) => Math.abs(high-low) > 1e-12 ? (value-low)/(high-low) : value;
    const left = [norm(lx1, 0, xm), Math.abs(ym) > 1e-12 ? norm(ly1, 0, ym) : 1/3, norm(lx2, 0, xm), Math.abs(ym) > 1e-12 ? norm(ly2, 0, ym) : 2/3];
    const right = [norm(rx1, xm, 1), Math.abs(1-ym) > 1e-12 ? norm(ry1, ym, 1) : 1/3, norm(rx2, xm, 1), Math.abs(1-ym) > 1e-12 ? norm(ry2, ym, 1) : 2/3];
    const cx = value => clamp(value, 0, 1), cy = value => clamp(value, ...HANDLE_Y);
    return [[cx(left[0]), cy(left[1]), cx(left[2]), cy(left[3])], [cx(right[0]), cy(right[1]), cx(right[2]), cy(right[3])], ym];
  }
  // Absolute control points of a segment, de Casteljau in 2D, and the keys that draw them: a piece
  // whose handles cannot be written in the segment's unit square (a curve that overshoots its
  // ends) is split again at its value extremum, so a cut never bends a curve.
  function controlPoints(a, b, kind) {
    const [, x1, y1, x2, y2] = kind[0] === 'smooth' ? ['bezier', 1/3, 0, 2/3, 1] : kind;
    const dt = b.time-a.time, dv = b.value-a.value;
    return [[a.time, a.value], [a.time+x1*dt, a.value+y1*dv], [a.time+x2*dt, a.value+y2*dv], [b.time, b.value]];
  }
  function casteljau(P, s) {
    const lerp = (p, q) => [p[0]+(q[0]-p[0])*s, p[1]+(q[1]-p[1])*s];
    const a = lerp(P[0], P[1]), b = lerp(P[1], P[2]), c = lerp(P[2], P[3]), d = lerp(a, b), e = lerp(b, c), m = lerp(d, e);
    return [[P[0], a, d, m], [m, e, c, P[3]]];
  }
  function curveParam(P, time) {
    const dt = P[3][0]-P[0][0];
    if (dt <= 0) return 0;
    const x1 = (P[1][0]-P[0][0])/dt, x2 = (P[2][0]-P[0][0])/dt;
    return bezierSolve(x1, x2, (time-P[0][0])/dt);
  }
  function handlesOf(P) {
    const dt = P[3][0]-P[0][0], dv = P[3][1]-P[0][1], spread = Math.max(...P.map(p => Math.abs(p[1]-P[0][1])));
    const x1 = clamp((P[1][0]-P[0][0])/dt, 0, 1), x2 = clamp((P[2][0]-P[0][0])/dt, 0, 1);
    if (Math.abs(dv) <= 1e-12) return spread <= 1e-9 ? [x1, 1/3, x2, 2/3] : null;
    const y1 = (P[1][1]-P[0][1])/dv, y2 = (P[2][1]-P[0][1])/dv;
    return y1 >= HANDLE_Y[0] && y1 <= HANDLE_Y[1] && y2 >= HANDLE_Y[0] && y2 <= HANDLE_Y[1] ? [x1, y1, x2, y2] : null;
  }
  function valueExtremum(P) {
    // y(s) = y0 + ((A s + B) s + C) s: the first root of its derivative inside (0, 1), else the middle.
    const p1 = P[1][1]-P[0][1], p2 = P[2][1]-P[0][1], p3 = P[3][1]-P[0][1];
    const A = p3-3*p2+3*p1, B = 3*p2-6*p1, C = 3*p1;
    let roots = [];
    if (Math.abs(A) < 1e-12) { if (Math.abs(B) > 1e-12) roots = [-C/(2*B)]; }
    else { const disc = 4*B*B-12*A*C; if (disc >= 0) roots = [(-2*B-Math.sqrt(disc))/(6*A), (-2*B+Math.sqrt(disc))/(6*A)]; }
    return roots.filter(r => r > 1e-6 && r < 1-1e-6).sort((x, y) => x-y)[0] ?? .5;
  }
  function pieces(P, depth = 0) {
    const handles = handlesOf(P);
    if (handles || depth >= 4) return [{P, handles:handles || [1/3, 1/3, 2/3, 2/3]}];
    const [left, right] = casteljau(P, valueExtremum(P));
    return [...pieces(left, depth+1), ...pieces(right, depth+1)];
  }
  // The keys that shape from..to, re-based to start at 0. A segment cut by the window keeps its
  // exact shape: eased segments become bezier segments with the halves' handles.
  function windowPoints(points, from, to) {
    const ordered = orderedPoints(points);
    if (!ordered.length) return [];
    const plain = point => { const out = {...point}; for (const key of ['interp', 'ease', 'inHandle', 'outHandle']) if (out[key] == null) delete out[key]; return out; };
    const out = [];
    const push = point => {
      const last = out[out.length-1];
      if (last && Math.abs(last.time-point.time) < 1e-9) { Object.assign(last, {...point, inHandle:last.inHandle ?? point.inHandle}); return; }
      out.push(point);
    };
    const curved = kind => kind[0] === 'bezier' || kind[0] === 'smooth';
    // Before the first key and after the last the curve is flat.
    if (from < ordered[0].time) push({time:0, value:ordered[0].value});
    for (let i = 0; i < ordered.length; i++) {
      const a = ordered[i], b = ordered[i+1];
      if (!b) { if (a.time >= from-1e-9 && a.time <= to+1e-9) push(plain({...a, time:a.time-from})); break; }
      if (b.time <= from || a.time >= to) continue;
      const kind = segmentKind(a, b), lo = Math.max(from, a.time), hi = Math.min(to, b.time);
      if (!curved(kind)) {
        const start = {time:lo-from, value:curveValue(ordered, lo, 0)};
        if (lo === a.time) Object.assign(start, plain({...a, time:a.time-from}));
        else if (kind[0] === 'hold') start.interp = 'hold';
        push(start);
        continue;
      }
      let P = controlPoints(a, b, kind);
      if (hi < b.time) P = casteljau(P, curveParam(P, hi))[0];
      if (lo > a.time) P = casteljau(P, curveParam(P, lo))[1];
      for (const piece of pieces(P)) {
        const [x1, y1, x2, y2] = piece.handles;
        const head = {time:piece.P[0][0]-from, value:piece.P[0][1], interp:'bezier', outHandle:[x1, y1]};
        if (piece.P[0][0] === a.time && a.inHandle) head.inHandle = a.inHandle;
        push(head);
        out.push({time:piece.P[3][0]-from, value:piece.P[3][1], inHandle:[x2, y2], _end:true});
      }
      // The piece end is the next key (or the window end): drop the placeholder unless it is the end.
      if (out[out.length-1]._end && hi < to) { const end = out.pop(); delete end._end; out.push(end); }
    }
    // The window end, held flat after the last key.
    const last = out[out.length-1];
    if (!last || last.time < to-from-1e-9) push({time:to-from, value:curveValue(ordered, to, 0)});
    for (const point of out) delete point._end;
    // Close the window exactly at 0 and to-from.
    if (out[0].time > 1e-9) out.unshift({time:0, value:curveValue(ordered, from, 0)});
    for (const point of out) {
      if (Math.abs(point.time) < 1e-9) point.time = 0;
      if (Math.abs(point.time-(to-from)) < 1e-9) point.time = to-from;
    }
    return out.map(plain);
  }

  // ---------------------------------------------------------------- speed ramps
  const rampPoints = clip => { const points = clip?.keyframes?.speed; return points?.length ? orderedPoints(points) : null; };
  function segmentIntegral(kind, v0, dv, u) {
    const [name, x1, y1, x2, y2] = kind;
    if (name === 'hold') return v0*u;
    if (name === 'linear') return v0*u+dv*u*u/2;
    if (name === 'smooth') return v0*u+dv*(u**3-u**4/2);
    const s = bezierSolve(x1, x2, u);
    const [ax, bx, cx] = coefficients(x1, x2), [ay, by, cy] = coefficients(y1, y2);
    const p5 = 3*ax*ay, p4 = 3*ax*by+2*bx*ay, p3 = 3*ax*cy+2*bx*by+cx*ay, p2 = 2*bx*cy+cx*by, p1 = cx*cy;
    const j = ((((p5/6*s+p4/5)*s+p3/4)*s+p2/3)*s+p1/2)*s*s;
    return v0*u+dv*j;
  }
  function offsetAt(points, local) {
    const first = points[0];
    if (local <= first.time) return first.value/100*(local-first.time);
    let total = 0;
    for (let i = 1; i < points.length; i++) {
      const a = points[i-1], b = points[i], span = b.time-a.time, kind = segmentKind(a, b);
      if (local <= b.time) return total+span*segmentIntegral(kind, a.value/100, (b.value-a.value)/100, (local-a.time)/span);
      total += span*segmentIntegral(kind, a.value/100, (b.value-a.value)/100, 1);
    }
    return total+points[points.length-1].value/100*(local-points[points.length-1].time);
  }
  const rampSource = (points, local) => offsetAt(points, local)-offsetAt(points, 0);
  const rampSpeed = (points, local) => Math.max(0, curveValue(points, local, 100))/100;
  function rampDuration(points, span, limit = 7200) {
    if (span <= 0) return 0;
    if (rampSource(points, limit) < span) return null;
    let low = 0, high = limit;
    for (let step = 0; step < 80; step++) { const middle = (low+high)/2; if (rampSource(points, middle) < span) low = middle; else high = middle; }
    return high;
  }
  // Source seconds between clip-local times a and b, for any clip (ramped or constant speed).
  function sourceDelta(clip, a, b) {
    const points = rampPoints(clip);
    if (points) return rampSource(points, b)-rampSource(points, a);
    return (b-a)*(Number(clip.speed) || 1);
  }
  // After every edit: a ramped clip's nominal speed is its average (edits use it for the Out point).
  function normalizeRamps(project) {
    for (const clip of project.clips || []) {
      if (!clip.keyframes?.speed?.length || !(clip.duration > 0)) continue;
      const ramp = rampPoints(clip);
      const average = Math.min(16, Math.max(.1, Math.round(rampSource(ramp, clip.duration)/clip.duration*1e6)/1e6));
      if (clip.speed !== average) clip.speed = average;
    }
  }
  // Speed ramp presets: keys over the clip's current length that keep its source span when
  // the duration is solved (applyRamp).
  const PRESETS = {
    smoothSlowMo:{name:'Smooth Slow-Mo', keys:[[0, 100, 'easeInOut'], [.3, 25, 'hold'], [.7, 25, 'easeInOut'], [1, 100]]},
    fastToSlow:{name:'Fast-to-Slow', keys:[[0, 400, 'easeInOut'], [.5, 50], [1, 50]]},
    speedUp:{name:'Speed Up', keys:[[0, 100, 'easeIn'], [.5, 300], [1, 300]]},
    freeze:{name:'Freeze Middle', keys:[[0, 100, 'hold'], [.4, 0, 'hold'], [.6, 100], [1, 100]]},
  };
  // Keys for a preset whose ramp plays the clip's source span exactly: the times are solved so
  // the integral equals the span (scale of the time axis), returned with the new duration.
  function presetRamp(presetId, span) {
    const preset = PRESETS[presetId];
    if (!preset) fail('Unknown speed preset');
    const make = length => preset.keys.map(([at, value, interp]) => ({time:at*length, value, ...(interp ? {interp} : {})}));
    // rampSource is linear in the time scale for a fixed shape: one evaluation gives the length.
    const unit = rampSource(make(1), 1);
    if (!(unit > 0)) fail('This ramp never plays the source.');
    const length = span/unit;
    return {keys:make(length), duration:length};
  }

  // ---------------------------------------------------------------- validation
  function validateEffects(effects) {
    if (effects == null) return;
    if (typeof effects !== 'object' || Array.isArray(effects)) fail('Invalid clip effects');
    const allowed = new Set(['crop', 'masks', 'key', 'blend', 'stabilize', 'speedAudio', 'bypass']);
    const unknown = Object.keys(effects).filter(key => !allowed.has(key));
    if (unknown.length) fail('Unknown clip effect: '+unknown.join(', '));
    const flag = (section, name, label) => { if (name in section && typeof section[name] !== 'boolean') fail(`Invalid ${label} ${name}`); };
    flag(effects, 'bypass', 'effects');
    const section = (value, label) => { if (value != null && (typeof value !== 'object' || Array.isArray(value))) fail('Invalid '+label); return value; };
    const crop = section(effects.crop, 'crop');
    if (crop) {
      for (const [name, [low, high]] of Object.entries(CROP_PROPS)) if (name in crop) finite(crop[name], 'crop '+name, low, high);
      flag(crop, 'enabled', 'crop');
      if ((crop.left || 0)+(crop.right || 0) > 100 || (crop.top || 0)+(crop.bottom || 0) > 100) fail('The crop removes more than the whole picture');
    }
    if (effects.masks != null) {
      if (!Array.isArray(effects.masks) || effects.masks.length > MAX_MASKS) fail(`A clip has at most ${MAX_MASKS} masks`);
      effects.masks.forEach((mask, number) => {
        if (!mask || typeof mask !== 'object' || !MASK_SHAPES.includes(mask.shape)) fail(`Invalid mask ${number+1}`);
        for (const [name, [low, high]] of Object.entries(MASK_PROPS)) if (name in mask) finite(mask[name], `mask ${number+1} ${name}`, low, high);
        flag(mask, 'invert', 'mask'); flag(mask, 'enabled', 'mask');
        if (mask.shape === 'polygon') {
          if (!Array.isArray(mask.points) || mask.points.length < 3 || mask.points.length > MAX_POLYGON_POINTS) fail(`A polygon mask has 3 to ${MAX_POLYGON_POINTS} points`);
          for (const point of mask.points) {
            if (!Array.isArray(point) || point.length !== 2) fail(`Invalid mask ${number+1} point`);
            finite(point[0], 'mask point', -200, 200); finite(point[1], 'mask point', -200, 200);
          }
        }
      });
    }
    const key = section(effects.key, 'chroma key');
    if (key) {
      if ('color' in key && (typeof key.color !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(key.color))) fail('Invalid key color');
      for (const [name, [low, high]] of Object.entries(KEY_PROPS)) if (name in key) finite(key[name], 'key '+name, low, high);
      flag(key, 'invert', 'key'); flag(key, 'enabled', 'key');
    }
    if (effects.blend != null && !BLEND_MODES.includes(effects.blend)) fail('Invalid blend mode');
    const stabilize = section(effects.stabilize, 'stabilization');
    if (stabilize) {
      if (stabilize.analysis != null && (typeof stabilize.analysis !== 'string' || !/^[0-9a-f]{32}$/.test(stabilize.analysis))) fail('Invalid stabilization analysis');
      for (const [name, [low, high]] of Object.entries(STABILIZE_PROPS)) if (name in stabilize) finite(stabilize[name], 'stabilization '+name, low, high);
      if ('autoZoom' in stabilize && !Number.isInteger(stabilize.autoZoom)) fail('Invalid stabilization autoZoom');
      flag(stabilize, 'tripod', 'stabilization'); flag(stabilize, 'enabled', 'stabilization');
      if (stabilize.source != null && (!Array.isArray(stabilize.source) || stabilize.source.length !== 2 ||
          !stabilize.source.every(v => Number.isFinite(v) && v >= 0) || stabilize.source[1] <= stabilize.source[0])) fail('Invalid stabilization source range');
    }
    if (effects.speedAudio != null && !SPEED_AUDIO.includes(effects.speedAudio)) fail('Invalid ramp audio mode');
  }
  // Grade keyframes mix two gradings between key times: the mix eases as the key at the segment's
  // start says (the first grade property, in color-grade.js order, with a key there).
  const GRADE_KEYS = ['exposure', 'temperature', 'tint', 'brightness', 'contrast', 'saturation'];
  function gradeSegmentKind(clip, start) {
    for (const key of GRADE_KEYS) {
      const points = orderedPoints(clip.keyframes?.[key]), index = points.findIndex(point => point.time === start);
      if (index >= 0 && index < points.length-1) return segmentKind(points[index], points[index+1]);
    }
    return null;
  }
  // Names of a clip's configured effects (bypassed or switched off ones too): effects_engine.describe.
  function describe(effects, keyframes = null) {
    const names = [];
    if (keyframes?.speed?.length) names.push('speed ramp');
    if (!effects) return names;
    if (effects.crop) names.push('crop');
    if (effects.masks?.length) names.push('masks');
    if (effects.key) names.push('chroma key');
    if ((effects.blend || 'normal') !== 'normal') names.push(effects.blend+' blend mode');
    if (effects.stabilize) names.push('stabilization');
    return effects.bypass && names.length ? names.map(name => name+' (bypassed)') : names;
  }
  // Keyframes of effect properties (speed, crop.*, mask.N.*): bounds and interpolation fields.
  function validateKeyframes(keyframes) {
    for (const [key, points] of Object.entries(keyframes || {})) {
      for (const point of points || []) validatePointShape(point, key);
      const bounds = KEY_BOUNDS[key];
      if (!bounds) continue;
      if (points.length > 100) fail(`A clip has more than 100 ${key} keyframes, the export limit`);
      for (const point of points) { finite(point.time, key+' keyframe time', 0, 48*3600); finite(point.value, key+' keyframe value', ...bounds); }
    }
  }
  const isEffectKey = key => key in KEY_BOUNDS;

  // ---------------------------------------------------------------- per-pixel maths
  function box(qx, qy, hx, hy) {
    const dx = Math.abs(qx)-hx, dy = Math.abs(qy)-hy;
    return Math.hypot(Math.max(dx, 0), Math.max(dy, 0))+Math.min(Math.max(dx, dy), 0);
  }
  function segmentDistance(px, py, ax, ay, bx, by) {
    const ex = bx-ax, ey = by-ay, wx = px-ax, wy = py-ay, length = ex*ex+ey*ey;
    const h = length > 0 ? Math.min(1, Math.max(0, (wx*ex+wy*ey)/length)) : 0;
    return Math.hypot(wx-ex*h, wy-ey*h);
  }
  function maskParams(mask, values = {}) {
    const get = name => name in values ? values[name] : (mask[name] ?? MASK_PROPS[name][2]);
    return {shape:mask.shape, x:get('x')/100, y:get('y')/100, w:Math.max(get('width'), 0)/100, h:Math.max(get('height'), 0)/100,
      angle:get('rotation')*Math.PI/180, feather:get('feather')/100, expansion:get('expansion')/100, opacity:get('opacity')/100,
      invert:!!mask.invert, points:(mask.points || []).map(([x, y]) => [x/100, y/100])};
  }
  function maskCoverage(m, u, v, aspect) {
    const px = (u-m.x)*aspect, py = v-m.y, c = Math.cos(m.angle), s = Math.sin(m.angle);
    const qx = c*px+s*py, qy = -s*px+c*py;
    let d;
    if (m.shape === 'rectangle') d = box(qx, qy, m.w/2*aspect, m.h/2);
    else if (m.shape === 'ellipse') {
      const hx = Math.max(m.w/2*aspect, 1e-6), hy = Math.max(m.h/2, 1e-6);
      d = (Math.hypot(qx/hx, qy/hy)-1)*Math.min(hx, hy);
    } else {
      const corners = m.points.map(([x, y]) => [x*m.w*aspect, y*m.h]);
      d = 1e9; let inside = false;
      for (let i = 0; i < corners.length; i++) {
        const [ax, ay] = corners[i], [bx, by] = corners[(i+1) % corners.length];
        d = Math.min(d, segmentDistance(qx, qy, ax, ay, bx, by));
        if ((ay > qy) !== (by > qy) && qx < (bx-ax)*(qy-ay)/(by-ay)+ax) inside = !inside;
      }
      if (inside) d = -d;
    }
    d -= m.expansion;
    let coverage = m.feather > 0 ? Math.min(1, Math.max(0, .5-d/m.feather)) : (d <= 0 ? 1 : 0);
    if (m.invert) coverage = 1-coverage;
    return coverage*m.opacity;
  }
  function masksAlpha(params, u, v, aspect) {
    if (!params.length) return 1;
    let keep = 1;
    for (const m of params) keep *= 1-maskCoverage(m, u, v, aspect);
    return 1-keep;
  }
  function cropParams(crop, values = {}) {
    const out = {};
    for (const name of Object.keys(CROP_PROPS)) out[name] = (name in values ? values[name] : (crop[name] ?? 0))/100;
    return out;
  }
  function cropAlpha(c, u, v, aspect) {
    const d = Math.min((u-c.left)*aspect, (1-c.right-u)*aspect, v-c.top, 1-c.bottom-v);
    return c.feather > 0 ? Math.min(1, Math.max(0, d/c.feather)) : (d >= 0 ? 1 : 0);
  }
  const KR_KB = {bt709:[.2126, .0722], bt601:[.299, .114], bt2020:[.2627, .0593]};
  function keyChroma(color, matrix) {
    const [kr, kb] = KR_KB[matrix] || KR_KB.bt709;
    const [r, g, b] = [1, 3, 5].map(i => parseInt(color.slice(i, i+2), 16)/255);
    const y = kr*r+(1-kr-kb)*g+kb*b;
    return [(128+224*(b-y)/(2*(1-kb)))/255, (128+224*(r-y)/(2*(1-kr)))/255];
  }
  const NEUTRAL = 128/255;
  function keyParams(key, matrix) {
    const [cb, cr] = keyChroma(key.color || '#00ff00', matrix), dx = cb-NEUTRAL, dy = cr-NEUTRAL, length = Math.hypot(dx, dy);
    const get = name => key[name] ?? KEY_PROPS[name][2];
    return {cb, cr, dx:length > 1e-9 ? dx/length : 0, dy:length > 1e-9 ? dy/length : 0,
      similarity:get('similarity')/100*.4, smoothness:get('smoothness')/100*.4, spill:get('spill')/100,
      choke:get('choke')/100*.9, feather:get('feather')/100, invert:!!key.invert, matrix};
  }
  function keyAlpha(k, cb, cr) {
    const d = Math.hypot(cb-k.cb, cr-k.cr);
    let a = k.smoothness > 0 ? Math.min(1, Math.max(0, (d-k.similarity)/k.smoothness)) : (d > k.similarity ? 1 : 0);
    a = Math.min(1, Math.max(0, (a-k.choke)/(1-k.choke)));
    a = a+k.feather*(a*a*(3-2*a)-a);
    return k.invert ? 1-a : a;
  }
  function despill(k, cb, cr, out) {
    let x = cb-NEUTRAL, y = cr-NEUTRAL;
    const p = x*k.dx+y*k.dy;
    if (p > 0) { x -= k.dx*p*k.spill; y -= k.dy*p*k.spill; }
    out[0] = NEUTRAL+x; out[1] = NEUTRAL+y;
    return out;
  }
  const softD = b => b <= .25 ? ((16*b-12)*b+4)*b : Math.sqrt(b);
  function blendChannel(mode, b, s) {
    switch (mode) {
      case 'multiply': return b*s;
      case 'screen': return b+s-b*s;
      case 'overlay': return b <= .5 ? 2*s*b : 1-2*(1-s)*(1-b);
      case 'add': return Math.min(1, b+s);
      case 'darken': return Math.min(b, s);
      case 'lighten': return Math.max(b, s);
      case 'hardLight': return s <= .5 ? 2*s*b : 1-2*(1-s)*(1-b);
      case 'softLight': return s <= .5 ? b-(1-2*s)*b*(1-b) : b+(2*s-1)*(softD(b)-b);
      case 'difference': return Math.abs(b-s);
      default: return s;
    }
  }

  // ---------------------------------------------------------------- per-frame state for the monitor
  // What renders: bypassed and disabled effects are kept in the project and skipped here.
  function live(effects, keyframes = null) {
    if (!effects || effects.bypass) return null;
    const cropKeyed = keyframes && ['left', 'right', 'top', 'bottom'].some(k => keyframes['crop.'+k]?.length);
    const crop = effects.crop && effects.crop.enabled !== false && (cropKeyed || ['left', 'right', 'top', 'bottom'].some(k => effects.crop[k])) ? effects.crop : null;
    const masks = (effects.masks || []).map((mask, index) => [index, mask]).filter(([, mask]) => mask.enabled !== false);
    const key = effects.key && effects.key.enabled !== false ? effects.key : null;
    const blend = effects.blend && effects.blend !== 'normal' ? effects.blend : null;
    if (!crop && !masks.length && !key && !blend) return null;
    return {crop, masks, key, blend};
  }
  const animatedValues = (clip, prefix, names, local) => {
    const values = {};
    for (const name of names) {
      const points = clip.keyframes?.[prefix+name];
      if (points?.length) values[name] = curveValue(points, local, 0);
    }
    return values;
  };
  // The prepared effects of one layer at clip-local time `local` (null when nothing applies).
  // `matrix` is the source's Y'CbCr matrix name (the key colour is converted with it).
  function layerFx(clip, local, matrix, alphaView = false) {
    const fx = live(clip.effects, clip.keyframes);
    if (!fx && !alphaView) return null;
    const out = {blend:fx?.blend || null, alphaView};
    if (fx?.crop) out.crop = cropParams(fx.crop, animatedValues(clip, 'crop.', Object.keys(CROP_PROPS), local));
    out.masks = (fx?.masks || []).map(([index, mask]) => maskParams(mask, animatedValues(clip, `mask.${index}.`, Object.keys(MASK_PROPS), local)));
    if (fx?.key) out.key = keyParams(fx.key, matrix === 'bt601' ? 'bt601' : matrix === 'bt2020' ? 'bt2020' : 'bt709');
    out.alpha = !!(out.crop || out.masks.length || out.key);
    return out;
  }

  // ---------------------------------------------------------------- WebGL2
  // Functions for the layer shader (gl-preview.js FRAGMENT), compiled when FX is defined.
  const GLSL = `
uniform int u_fxMasks; uniform vec4 u_fxA[${MAX_MASKS}]; uniform vec4 u_fxB[${MAX_MASKS}]; uniform ivec4 u_fxC[${MAX_MASKS}];
uniform vec2 u_fxPoly[${MAX_MASKS*MAX_POLYGON_POINTS}];
uniform vec4 u_fxCrop; uniform float u_fxCropFeather; uniform int u_fxFlags; uniform float u_fxAspect;
uniform vec4 u_fxKey; uniform vec4 u_fxKey2; uniform vec2 u_fxKey3; uniform vec2 u_fxKrKb;
float fxBox(vec2 q, vec2 h) { vec2 d = abs(q)-h; return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0); }
float fxSeg(vec2 p, vec2 a, vec2 b) { vec2 e = b-a, w = p-a; float l = dot(e, e); float h = l > 0.0 ? clamp(dot(w, e)/l, 0.0, 1.0) : 0.0; return length(w-e*h); }
float fxMask(int i, vec2 uv) {
  vec4 A = u_fxA[i], B = u_fxB[i]; ivec4 C = u_fxC[i];
  vec2 p = vec2((uv.x-A.x)*u_fxAspect, uv.y-A.y); float c = cos(B.x), s = sin(B.x);
  vec2 q = vec2(c*p.x+s*p.y, -s*p.x+c*p.y);
  float d;
  if (C.x == 0) d = fxBox(q, vec2(A.z/2.0*u_fxAspect, A.w/2.0));
  else if (C.x == 1) { vec2 h = max(vec2(A.z/2.0*u_fxAspect, A.w/2.0), vec2(1e-6)); d = (length(q/h)-1.0)*min(h.x, h.y); }
  else {
    d = 1e9; bool inside = false;
    for (int k = 0; k < C.w; k++) {
      vec2 a = u_fxPoly[C.z+k]*vec2(A.z*u_fxAspect, A.w), b = u_fxPoly[C.z+(k+1 == C.w ? 0 : k+1)]*vec2(A.z*u_fxAspect, A.w);
      d = min(d, fxSeg(q, a, b));
      if ((a.y > q.y) != (b.y > q.y) && q.x < (b.x-a.x)*(q.y-a.y)/(b.y-a.y)+a.x) inside = !inside;
    }
    if (inside) d = -d;
  }
  d -= B.z;
  float coverage = B.y > 0.0 ? clamp(0.5-d/B.y, 0.0, 1.0) : (d <= 0.0 ? 1.0 : 0.0);
  if (C.y == 1) coverage = 1.0-coverage;
  return coverage*B.w;
}
float fxGeometry(vec2 uv) {
  float alpha = 1.0;
  if ((u_fxFlags & 1) != 0) {
    float d = min(min((uv.x-u_fxCrop.x)*u_fxAspect, (1.0-u_fxCrop.y-uv.x)*u_fxAspect), min(uv.y-u_fxCrop.z, 1.0-u_fxCrop.w-uv.y));
    alpha *= u_fxCropFeather > 0.0 ? clamp(d/u_fxCropFeather, 0.0, 1.0) : (d >= 0.0 ? 1.0 : 0.0);
  }
  if (u_fxMasks > 0) { float keep = 1.0; for (int i = 0; i < u_fxMasks; i++) keep *= 1.0-fxMask(i, uv); alpha *= 1.0-keep; }
  return alpha;
}
float fxKeyAlpha(vec2 cbcr) {
  float d = length(cbcr-u_fxKey.xy);
  float a = u_fxKey2.y > 0.0 ? clamp((d-u_fxKey2.x)/u_fxKey2.y, 0.0, 1.0) : (d > u_fxKey2.x ? 1.0 : 0.0);
  a = clamp((a-u_fxKey2.z)/(1.0-u_fxKey2.z), 0.0, 1.0);
  a = a+u_fxKey2.w*(a*a*(3.0-2.0*a)-a);
  return u_fxKey3.y > 0.5 ? 1.0-a : a;
}
vec2 fxDespill(vec2 cbcr) {
  vec2 x = cbcr-vec2(${NEUTRAL}); float p = dot(x, u_fxKey.zw);
  if (p > 0.0) x -= u_fxKey.zw*p*u_fxKey3.x;
  return vec2(${NEUTRAL})+x;
}
// R'G'B' (a browser-decoded or still source) to and from Cb, Cr codes / 255, video range.
vec2 fxChroma(vec3 rgb) {
  float y = u_fxKrKb.x*rgb.r+(1.0-u_fxKrKb.x-u_fxKrKb.y)*rgb.g+u_fxKrKb.y*rgb.b;
  return vec2((128.0+224.0*(rgb.b-y)/(2.0*(1.0-u_fxKrKb.y)))/255.0, (128.0+224.0*(rgb.r-y)/(2.0*(1.0-u_fxKrKb.x)))/255.0);
}
vec3 fxRechroma(vec3 rgb, vec2 from, vec2 to) {
  vec2 d = (to-from)*255.0/224.0; float kr = u_fxKrKb.x, kb = u_fxKrKb.y, kg = 1.0-kr-kb;
  float dr = d.y*2.0*(1.0-kr), db = d.x*2.0*(1.0-kb);
  return rgb + vec3(dr, -(kr*dr+kb*db)/kg, db);
}`;
  const BLEND_GLSL = `
float fxSoftD(float b) { return b <= 0.25 ? ((16.0*b-12.0)*b+4.0)*b : sqrt(b); }
float fxBlend(int mode, float b, float s) {
  if (mode == 1) return b*s;
  if (mode == 2) return b+s-b*s;
  if (mode == 3) return b <= 0.5 ? 2.0*s*b : 1.0-2.0*(1.0-s)*(1.0-b);
  if (mode == 4) return min(1.0, b+s);
  if (mode == 5) return min(b, s);
  if (mode == 6) return max(b, s);
  if (mode == 7) return s <= 0.5 ? b-(1.0-2.0*s)*b*(1.0-b) : b+(2.0*s-1.0)*(fxSoftD(b)-b);
  if (mode == 8) return s <= 0.5 ? 2.0*s*b : 1.0-2.0*(1.0-s)*(1.0-b);
  if (mode == 9) return abs(b-s);
  return s;
}`;
  const BLEND_INDEX = {normal:0, multiply:1, screen:2, overlay:3, add:4, darken:5, lighten:6, softLight:7, hardLight:8, difference:9};
  function bindUniforms(gl, u, fx, aspect) {
    let flags = 0;
    if (fx?.crop) { flags |= 1; gl.uniform4f(u.u_fxCrop, fx.crop.left, fx.crop.right, fx.crop.top, fx.crop.bottom); gl.uniform1f(u.u_fxCropFeather, fx.crop.feather); }
    if (fx?.key) flags |= 2;
    if (fx?.alphaView) flags |= 4;
    gl.uniform1i(u.u_fxFlags, flags); gl.uniform1f(u.u_fxAspect, aspect);
    const masks = fx?.masks || [];
    gl.uniform1i(u.u_fxMasks, masks.length);
    if (masks.length) {
      const A = new Float32Array(MAX_MASKS*4), B = new Float32Array(MAX_MASKS*4), C = new Int32Array(MAX_MASKS*4), P = new Float32Array(MAX_MASKS*MAX_POLYGON_POINTS*2);
      masks.forEach((m, i) => {
        A.set([m.x, m.y, m.w, m.h], i*4); B.set([m.angle, m.feather, m.expansion, m.opacity], i*4);
        C.set([MASK_SHAPES.indexOf(m.shape), m.invert ? 1 : 0, i*MAX_POLYGON_POINTS, m.points.length], i*4);
        m.points.forEach(([x, y], k) => P.set([x, y], (i*MAX_POLYGON_POINTS+k)*2));
      });
      gl.uniform4fv(u['u_fxA[0]'], A); gl.uniform4fv(u['u_fxB[0]'], B); gl.uniform4iv(u['u_fxC[0]'], C);
      if (u['u_fxPoly[0]']) gl.uniform2fv(u['u_fxPoly[0]'], P);
    }
    const k = fx?.key;
    if (k) {
      gl.uniform4f(u.u_fxKey, k.cb, k.cr, k.dx, k.dy); gl.uniform4f(u.u_fxKey2, k.similarity, k.smoothness, k.choke, k.feather);
      gl.uniform2f(u.u_fxKey3, k.spill, k.invert ? 1 : 0);
      const [kr, kb] = KR_KB[k.matrix] || KR_KB.bt709; gl.uniform2f(u.u_fxKrKb, kr, kb);
    }
  }

  // ---------------------------------------------------------------- Canvas 2D
  // Effects on a layer already drawn (geometry applied) into `image` (canvas pixels): each pixel
  // is mapped back to the clip's picture (u, v) as the GL fragment's v_uv is. `yuv`: the pixels
  // hold Y'CbCr codes (decode with `decode` afterwards unless a grade does it).
  function canvasPass(image, op, width, height, yuv, decode) {
    const fx = op.fx, d = image.data, g = op.geometry;
    if (!fx) return;
    let toUv = null, aspect = width/height;
    if (g) {
      const fit = Math.min(width/g.sourceWidth, height/g.sourceHeight);
      const w = g.sourceWidth*fit*g.scale, h = g.sourceHeight*fit*g.scale, cx = width/2+g.x*width/100, cy = height/2+g.y*height/100;
      const c = Math.cos(g.rotation), s = Math.sin(g.rotation);
      aspect = g.sourceWidth/g.sourceHeight;
      toUv = (x, y, out) => { const dx = x-cx, dy = y-cy; out[0] = (c*dx+s*dy)/w+.5; out[1] = (-s*dx+c*dy)/h+.5; };
    } else toUv = (x, y, out) => { out[0] = x/width; out[1] = y/height; };
    const uv = [0, 0], spilled = [0, 0], rgb = [0, 0, 0];
    const k = fx.key, [kr, kb] = k ? (KR_KB[k.matrix] || KR_KB.bt709) : [0, 0];
    for (let y = 0, i = 0; y < height; y++) for (let x = 0; x < width; x++, i += 4) {
      if (!d[i+3]) continue;
      toUv(x+.5, y+.5, uv);
      let alpha = 1;
      if (fx.crop) alpha *= cropAlpha(fx.crop, uv[0], uv[1], aspect);
      if (fx.masks.length) alpha *= masksAlpha(fx.masks, uv[0], uv[1], aspect);
      if (k) {
        let cb, cr;
        if (yuv) { cb = d[i+1]/255; cr = d[i+2]/255; }
        else {
          const r = d[i]/255, gg = d[i+1]/255, b = d[i+2]/255, yy = kr*r+(1-kr-kb)*gg+kb*b;
          cb = (128+224*(b-yy)/(2*(1-kb)))/255; cr = (128+224*(r-yy)/(2*(1-kr)))/255;
        }
        alpha *= keyAlpha(k, cb, cr);
        despill(k, cb, cr, spilled);
        if (yuv) { d[i+1] = Math.round(spilled[0]*255); d[i+2] = Math.round(spilled[1]*255); }
        else {
          const ddb = (spilled[0]-cb)*255/224*2*(1-kb), ddr = (spilled[1]-cr)*255/224*2*(1-kr), kg = 1-kr-kb;
          d[i] = Math.round(clamp(d[i]/255+ddr, 0, 1)*255); d[i+1] = Math.round(clamp(d[i+1]/255-(kr*ddr+kb*ddb)/kg, 0, 1)*255);
          d[i+2] = Math.round(clamp(d[i+2]/255+ddb, 0, 1)*255);
        }
      }
      d[i+3] = Math.round(d[i+3]*alpha);
      if (fx.alphaView) { const level = d[i+3]; d[i] = d[i+1] = d[i+2] = level; d[i+3] = 255; }
    }
    if (yuv && decode && !fx.alphaView) {
      for (let i = 0; i < d.length; i += 4) if (d[i+3]) { decode(d[i], d[i+1], d[i+2], rgb); for (let c = 0; c < 3; c++) d[i+c] = Math.round(clamp(rgb[c], 0, 1)*255); }
    }
  }
  // Composite `layer` (straight alpha, at opacity `alpha`) over `base` with a blend mode.
  function blendPixels(base, layer, mode, alpha) {
    const b = base.data, s = layer.data;
    for (let i = 0; i < b.length; i += 4) {
      const as = s[i+3]/255*alpha;
      if (!as) continue;
      const ab = b[i+3]/255;
      for (let c = 0; c < 3; c++) {
        const cb = b[i+c]/255, cs = s[i+c]/255;
        const mixed = cs+(blendChannel(mode, cb, cs)-cs)*ab;
        b[i+c] = Math.round((cb+(mixed-cb)*as)*255);
      }
      b[i+3] = Math.round((as+ab*(1-as))*255);
    }
  }

  window.SpliceEffects = {INTERPOLATIONS, BLEND_MODES, BLEND_NAMES, BLEND_INDEX, MASK_SHAPES, MAX_MASKS, MAX_POLYGON_POINTS, SPEED_AUDIO,
    MASK_PROPS, CROP_PROPS, KEY_PROPS, STABILIZE_PROPS, KEY_BOUNDS, PRESETS, PRESET_HANDLES, GLSL, BLEND_GLSL,
    validatePointShape, eased, segmentKind, bezierComponent, bezierSolve, segmentFraction, curveValue, splitSegment, windowPoints,
    orderedPoints, normalizeRamps, describe, gradeSegmentKind, rampPoints, rampSource, rampSpeed, rampDuration, sourceDelta, presetRamp, validateEffects, validateKeyframes, isEffectKey,
    maskParams, maskCoverage, masksAlpha, cropParams, cropAlpha, keyChroma, keyParams, keyAlpha, despill, blendChannel,
    live, layerFx, bindUniforms, canvasPass, blendPixels, segmentIntegral, offsetAt};
})();
