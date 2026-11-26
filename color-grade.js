/* The primary grade of a clip, as one pure function in one working space, realised from tables.
 *
 * Working space: Rec. 709 video-encoded R'G'B' (the BT.1886 display signal), full-range 0..1, after
 * the source's own matrix and range have been undone (see `inputMatrix`). Grading and compositing
 * both happen in this space, as the renderer has always composited, so existing projects keep
 * their look.
 *
 * The grade is a fixed chain of stages. Each stage is either a per-channel curve (sampled into a
 * 4,096-entry table and read with linear interpolation, as FFmpeg's lut1d reads it), a 3x3 matrix,
 * or a 3D lattice (read with trilinear interpolation, as FFmpeg's lut3d reads it):
 *
 *   pre    1D  brightness, contrast about the pivot, each clamped to 0..1      (legacy controls)
 *   sat    3x3 saturation about Rec. 709 luma (.213, .715, .072)               (legacy control)
 *   post   1D  exposure/white-balance gains (clamped), then offset, lift/gamma/gain wheels and the
 *              master and R, G, B curves                                         (new controls)
 *   hueSat 3D  33-point lattice of saturation as a function of hue               (new, optional)
 *   look   the clip's creative LUT (its own lattice, or its full 1D curve) at its intensity
 *
 * The browser builds these tables here; color_grade.py builds the same tables in Python for the
 * export. Both sides then apply the same tables with the same interpolation, so the picture in the
 * monitor and the exported picture differ only by the codec. tests/test_color_grade.py compares the
 * tables of both builders and the pixels FFmpeg produces from them with the pixels the monitor
 * computes.
 */
(() => {
  const TABLE = 4096;              // entries of each 1D table
  const HUE_SAT_SIZE = 33;         // lattice points per axis of the hue-vs-saturation table
  const MAX_POINTS = 16;           // points per curve
  const MAX_KEY_TIMES = 16;        // distinct grade keyframe times per clip
  const LUMA = [.213, .715, .072]; // the legacy saturation matrix's weights (render_movie.py)
  const WHEELS = {lift:[-1, 1, 0], gamma:[-1, 1, 0], gain:[0, 4, 1], offset:[-1, 1, 0]};
  const CURVES = ['master', 'red', 'green', 'blue'];
  // Scalar controls a grade keyframe can animate, with their limits (keyframes live in clip.keyframes).
  const ANIMATED = {exposure:[-4, 4, 0], temperature:[-100, 100, 0], tint:[-100, 100, 0],
    brightness:[0, 200, 100], contrast:[0, 200, 100], saturation:[0, 200, 100]};
  const clamp01 = value => value < 0 ? 0 : value > 1 ? 1 : value;
  const finite = (value, low, high) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high;

  function fail(message) { throw new Error('Invalid clip grade: ' + message); }
  function checkPoints(points, name, yHigh) {
    if (!Array.isArray(points) || points.length > MAX_POINTS) fail(`${name} has more than ${MAX_POINTS} points`);
    let last = -1;
    for (const point of points) {
      if (!Array.isArray(point) || point.length !== 2 || !finite(point[0], 0, 1) || !finite(point[1], 0, yHigh)) fail(`${name} point`);
      if (point[0] <= last) fail(`${name} points must be in increasing order`);
      last = point[0];
    }
  }
  // Validates a stored grade (clip.color.grade). Returns it unchanged when valid; throws otherwise.
  function validateGrade(grade) {
    if (grade === undefined || grade === null) return grade;
    if (typeof grade !== 'object' || Array.isArray(grade)) fail('not an object');
    for (const [name, [low, high]] of Object.entries(WHEELS)) {
      if (grade[name] === undefined) continue;
      if (!Array.isArray(grade[name]) || grade[name].length !== 4 || !grade[name].every(value => finite(value, low, high))) fail(name);
    }
    if (grade.pivot !== undefined && !finite(grade.pivot, .05, .95)) fail('pivot');
    if (grade.bypass !== undefined && typeof grade.bypass !== 'boolean') fail('bypass');
    if (grade.curves !== undefined) {
      if (!grade.curves || typeof grade.curves !== 'object' || Array.isArray(grade.curves)) fail('curves');
      for (const key of Object.keys(grade.curves)) {
        if (!CURVES.includes(key)) fail('curve name');
        checkPoints(grade.curves[key], key + ' curve', 1);
        if (grade.curves[key].length === 1) fail(key + ' curve needs two points');
      }
    }
    if (grade.hueSat !== undefined) checkPoints(grade.hueSat, 'hue vs saturation', 2);
    for (const point of grade.hueSat || []) if (point[0] >= 1) fail('hue vs saturation hue');
    return grade;
  }
  const wheel = (grade, name) => grade?.[name] || [WHEELS[name][2], WHEELS[name][2], WHEELS[name][2], WHEELS[name][2]];
  const identityCurve = points => !points || !points.length || (points.length === 2 && points[0][0] === 0 && points[0][1] === 0 && points[1][0] === 1 && points[1][1] === 1);
  // Whether the new (wheels, curves, pivot, hue/saturation) part of a grade does anything.
  function gradeActive(grade) {
    if (!grade) return false;
    for (const name of Object.keys(WHEELS)) if (wheel(grade, name).some(value => value !== WHEELS[name][2])) return true;
    if ((grade.pivot ?? .5) !== .5) return true;
    if (CURVES.some(key => !identityCurve(grade.curves?.[key]))) return true;
    return !!grade.hueSat?.length && grade.hueSat.some(point => point[1] !== 1);
  }
  // Monotone cubic (Fritsch-Carlson) through the points: never overshoots between two points, so
  // a curve that rises (or falls) between neighbours keeps doing so. Flat beyond the end points.
  function curveFunction(points) {
    if (identityCurve(points)) return null;
    const x = points.map(point => point[0]), y = points.map(point => point[1]), n = x.length;
    const delta = [], slope = new Array(n).fill(0);
    for (let i = 0; i < n-1; i++) delta.push((y[i+1]-y[i])/(x[i+1]-x[i]));
    slope[0] = delta[0]; slope[n-1] = delta[n-2];
    for (let i = 1; i < n-1; i++) slope[i] = delta[i-1]*delta[i] <= 0 ? 0 : (delta[i-1]+delta[i])/2;
    for (let i = 0; i < n-1; i++) {
      if (delta[i] === 0) { slope[i] = 0; slope[i+1] = 0; continue; }
      const a = slope[i]/delta[i], b = slope[i+1]/delta[i], h = a*a+b*b;
      if (h > 9) { const t = 3/Math.sqrt(h); slope[i] = t*a*delta[i]; slope[i+1] = t*b*delta[i]; }
    }
    return value => {
      if (value <= x[0]) return y[0];
      if (value >= x[n-1]) return y[n-1];
      let i = 0;
      while (value > x[i+1]) i++;
      const h = x[i+1]-x[i], t = (value-x[i])/h, t2 = t*t, t3 = t2*t;
      return (2*t3-3*t2+1)*y[i]+(t3-2*t2+t)*h*slope[i]+(-2*t3+3*t2)*y[i+1]+(t3-t2)*h*slope[i+1];
    };
  }
  // Exposure and white balance gains, as the export has always computed them.
  function gains(color) {
    const exposure = Math.max(-4, Math.min(4, Number(color.exposure) || 0));
    const temperature = Math.max(-100, Math.min(100, Number(color.temperature) || 0))/100;
    const tint = Math.max(-100, Math.min(100, Number(color.tint) || 0))/100, gain = 2**exposure;
    return [gain*(1+.35*temperature-.1*tint), gain*(1+.2*tint), gain*(1-.35*temperature-.1*tint)];
  }
  const legacy = color => ({
    brightness:Math.max(0, Math.min(3, Number(color.brightness ?? 100)/100)),
    contrast:Math.max(0, Math.min(3, Number(color.contrast ?? 100)/100)),
    saturation:Math.max(0, Math.min(3, Number(color.saturation ?? 100)/100)),
  });
  function hueOf(r, g, b) {
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max-min;
    if (d <= 0) return 0;
    let h = max === r ? (g-b)/d : max === g ? 2+(b-r)/d : 4+(r-g)/d;
    h /= 6;
    return h < 0 ? h+1 : h;
  }
  function hueSatFunction(points) {
    if (!points?.length || points.every(point => point[1] === 1)) return null;
    if (points.length === 1) return () => points[0][1];
    return hue => {
      for (let i = 0; i < points.length; i++) {
        const a = points[i], b = points[(i+1) % points.length];
        const span = i === points.length-1 ? b[0]+1-a[0] : b[0]-a[0];
        let offset = hue-a[0];
        if (offset < 0) offset += 1;
        if (offset <= span) return a[1]+(b[1]-a[1])*offset/span;
      }
      return points[0][1];
    };
  }
  // The stages of a grade at one set of control values. Pure: same input, same tables.
  function stages(color = {}) {
    const grade = color.grade && !color.grade.bypass ? color.grade : null;
    const {brightness, contrast, saturation} = legacy(color);
    const pivot = grade?.pivot ?? .5, gain = gains(color);
    const lift = wheel(grade, 'lift'), gamma = wheel(grade, 'gamma'), gainWheel = wheel(grade, 'gain'), offset = wheel(grade, 'offset');
    const master = curveFunction(grade?.curves?.master), channels = ['red', 'green', 'blue'].map(key => curveFunction(grade?.curves?.[key]));
    const active = gradeActive(grade);
    const pre = (brightness === 1 && contrast === 1) ? null : value => clamp01((clamp01(value*brightness)-pivot)*contrast+pivot);
    const post = [0, 1, 2].map(channel => {
      const g = gain[channel];
      if (!active && g === 1) return null;
      const L = lift[channel]+lift[3], G = 2**-(gamma[channel]+gamma[3]), K = gainWheel[channel]*gainWheel[3], O = offset[channel]+offset[3];
      const curve = channels[channel];
      return value => {
        let v = clamp01(value*g);
        if (!active) return v;
        v = K*((v+O)+L*(1-(v+O)));
        v = v > 0 ? v**G : v;
        v = clamp01(v);
        if (master) v = master(v);
        if (curve) v = curve(v);
        return clamp01(v);
      };
    });
    const matrix = saturation === 1 ? null : [0, 1, 2].map(row => [0, 1, 2].map(column => saturation*(row === column)+(1-saturation)*LUMA[column]));
    return {pre, matrix, post, hueSat:hueSatFunction(grade?.hueSat)};
  }
  function table(fn) {
    const out = new Float32Array(TABLE);
    for (let i = 0; i < TABLE; i++) out[i] = fn(i/(TABLE-1));
    return out;
  }
  function hueSatLattice(fn) {
    const n = HUE_SAT_SIZE, out = new Float32Array(n*n*n*3);
    let i = 0;
    for (let b = 0; b < n; b++) for (let g = 0; g < n; g++) for (let r = 0; r < n; r++) {
      const R = r/(n-1), G = g/(n-1), B = b/(n-1), y = .2126*R+.7152*G+.0722*B, s = fn(hueOf(R, G, B));
      out[i++] = clamp01(y+(R-y)*s); out[i++] = clamp01(y+(G-y)*s); out[i++] = clamp01(y+(B-y)*s);
    }
    return out;
  }
  // Tables for one set of control values. Cached by the values that shape them.
  const cache = new Map(), CACHE_LIMIT = 64;
  function signature(color = {}) {
    const grade = color.grade && !color.grade.bypass ? color.grade : null;
    return JSON.stringify([color.brightness ?? 100, color.contrast ?? 100, color.saturation ?? 100, color.exposure || 0,
      color.temperature || 0, color.tint || 0, grade && gradeActive(grade) ? grade : null]);
  }
  function tables(color = {}) {
    const key = signature(color);
    let result = cache.get(key);
    if (result) { cache.delete(key); cache.set(key, result); return result; }
    const s = stages(color);
    const flat = s.post.every(fn => !fn);
    result = {key, pre:s.pre ? table(s.pre) : null, matrix:s.matrix, post:flat ? null : s.post.map(fn => table(fn || (v => clamp01(v)))),
      hueSat:s.hueSat ? hueSatLattice(s.hueSat) : null};
    result.identity = !result.pre && !result.matrix && !result.post && !result.hueSat;
    cache.set(key, result);
    while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value);
    return result;
  }
  // ---- Evaluation from the tables (the CPU monitor and the .cube export use these) ----
  function lookup1D(values, value) {
    const position = clamp01(value)*(TABLE-1), low = Math.floor(position), high = Math.min(TABLE-1, low+1);
    return values[low]+(position-low)*(values[high]-values[low]);
  }
  function trilinear(data, size, r, g, b, out) {
    const pr = clamp01(r)*(size-1), pg = clamp01(g)*(size-1), pb = clamp01(b)*(size-1);
    const r0 = Math.floor(pr), g0 = Math.floor(pg), b0 = Math.floor(pb);
    const r1 = Math.min(size-1, r0+1), g1 = Math.min(size-1, g0+1), b1 = Math.min(size-1, b0+1);
    const fr = pr-r0, fg = pg-g0, fb = pb-b0, plane = size*size;
    for (let c = 0; c < 3; c++) {
      const at = (x, y, z) => data[3*(x+size*y+plane*z)+c];
      const c00 = at(r0,g0,b0)+fr*(at(r1,g0,b0)-at(r0,g0,b0)), c10 = at(r0,g1,b0)+fr*(at(r1,g1,b0)-at(r0,g1,b0));
      const c01 = at(r0,g0,b1)+fr*(at(r1,g0,b1)-at(r0,g0,b1)), c11 = at(r0,g1,b1)+fr*(at(r1,g1,b1)-at(r0,g1,b1));
      const c0 = c00+fg*(c10-c00), c1 = c01+fg*(c11-c01);
      out[c] = c0+fb*(c1-c0);
    }
    return out;
  }
  function curve1D(curve, t, channel) {
    const position = clamp01(t)*(curve.size-1), low = Math.floor(position), high = Math.min(curve.size-1, low+1);
    return curve.data[low*3+channel]+(position-low)*(curve.data[high*3+channel]-curve.data[low*3+channel]);
  }
  // A creative or technical LUT at an intensity, on one float pixel (in place). 1D files use their full
  // curve (FFmpeg lut1d), 3D files their lattice (FFmpeg lut3d); the domain maps onto 0..1 first.
  function applyLook(lut, intensity, rgb, scratch) {
    const mix = Math.max(0, Math.min(1, intensity/100));
    if (!lut || !mix) return rgb;
    const min = lut.domainMin || [0, 0, 0], max = lut.domainMax || [1, 1, 1];
    const n0 = (rgb[0]-min[0])/(max[0]-min[0]), n1 = (rgb[1]-min[1])/(max[1]-min[1]), n2 = (rgb[2]-min[2])/(max[2]-min[2]);
    if (lut.curve) { scratch[0] = curve1D(lut.curve, n0, 0); scratch[1] = curve1D(lut.curve, n1, 1); scratch[2] = curve1D(lut.curve, n2, 2); }
    else trilinear(lut.data, lut.size, n0, n1, n2, scratch);
    for (let c = 0; c < 3; c++) rgb[c] = rgb[c]*(1-mix)+scratch[c]*mix;
    return rgb;
  }
  // The whole chain on one float pixel (in place), given tables and the look/input LUTs.
  function evaluate(prepared, rgb, scratch = [0, 0, 0]) {
    const {tables:t, look, lookIntensity, input} = prepared;
    if (input) applyLook(input, 100, rgb, scratch);
    if (t.pre) for (let c = 0; c < 3; c++) rgb[c] = lookup1D(t.pre, rgb[c]);
    if (t.matrix) {
      const m = t.matrix, r = rgb[0], g = rgb[1], b = rgb[2];
      rgb[0] = m[0][0]*r+m[0][1]*g+m[0][2]*b; rgb[1] = m[1][0]*r+m[1][1]*g+m[1][2]*b; rgb[2] = m[2][0]*r+m[2][1]*g+m[2][2]*b;
    }
    if (t.post) for (let c = 0; c < 3; c++) rgb[c] = lookup1D(t.post[c], rgb[c]);
    if (t.hueSat) { trilinear(t.hueSat, HUE_SAT_SIZE, rgb[0], rgb[1], rgb[2], scratch); rgb[0] = scratch[0]; rgb[1] = scratch[1]; rgb[2] = scratch[2]; }
    if (look) applyLook(look, lookIntensity, rgb, scratch);
    rgb[0] = clamp01(rgb[0]); rgb[1] = clamp01(rgb[1]); rgb[2] = clamp01(rgb[2]);
    return rgb;
  }
  // Everything the monitor needs to grade one clip at one moment.
  function prepare(color = {}, inputLut = null) {
    const bypass = !!color.grade?.bypass;
    const look = !bypass && color.lut && (color.lutIntensity ?? 100) > 0 ? color.lut : null;
    const t = bypass ? tables({}) : tables(color);
    return {tables:t, look, lookIntensity:look ? Number(color.lutIntensity ?? 100) : 0, input:inputLut || null,
      identity:t.identity && !look && !inputLut};
  }
  // ---- Grade keyframes ----
  // Times (clip-local seconds) at which any animated grade control has a keyframe.
  function keyTimes(clip) {
    const times = new Set();
    for (const key of Object.keys(ANIMATED)) for (const point of clip.keyframes?.[key] || []) if (Number.isFinite(point.time) && Number.isFinite(point.value)) times.add(point.time);
    return [...times].sort((a, b) => a-b).slice(0, MAX_KEY_TIMES);
  }
  function valueAt(points, local, base) {
    const ordered = points.filter(point => Number.isFinite(point.time) && Number.isFinite(point.value)).sort((a, b) => a.time-b.time);
    if (!ordered.length) return base;
    if (local <= ordered[0].time) return ordered[0].value;
    for (let i = 1; i < ordered.length; i++) if (local <= ordered[i].time) {
      const a = ordered[i-1], b = ordered[i];
      return a.value+(b.value-a.value)*(local-a.time)/(b.time-a.time);
    }
    return ordered.at(-1).value;
  }
  // The control values at one keyframe time.
  function colorAt(clip, local) {
    const color = {...(clip.color || {})};
    for (const [key, [low, high, base]] of Object.entries(ANIMATED)) {
      const points = clip.keyframes?.[key];
      if (points?.length) color[key] = Math.max(low, Math.min(high, valueAt(points, local, Number(color[key] ?? base))));
    }
    return color;
  }
  // Which two keyframe gradings mix at clip-local time `local`, and how: the output of the grade at
  // the keyframe before and after is mixed linearly over time (the export does the same with blend).
  function keySegment(clip, local) {
    const times = keyTimes(clip);
    if (!times.length) return null;
    if (local <= times[0]) return {a:times[0], b:times[0], weight:0};
    for (let i = 1; i < times.length; i++) if (local < times[i]) return {a:times[i-1], b:times[i], weight:(local-times[i-1])/(times[i]-times[i-1])};
    return {a:times.at(-1), b:times.at(-1), weight:0};
  }
  // ---- A clip's grade as a .cube file (for a colorist's tool) ----
  function cubeText(color = {}, size = 33, title = 'Splice grade') {
    if (!Number.isInteger(size) || size < 2 || size > 65) throw new Error('Use 2-65 points for an exported grade.');
    const prepared = prepare(color), rows = [`TITLE "${String(title).replace(/["\r\n]/g, '').slice(0, 120)}"`, `LUT_3D_SIZE ${size}`, 'DOMAIN_MIN 0 0 0', 'DOMAIN_MAX 1 1 1'];
    const rgb = [0, 0, 0], scratch = [0, 0, 0];
    for (let b = 0; b < size; b++) for (let g = 0; g < size; g++) for (let r = 0; r < size; r++) {
      rgb[0] = r/(size-1); rgb[1] = g/(size-1); rgb[2] = b/(size-1);
      evaluate(prepared, rgb, scratch);
      rows.push(rgb.map(value => value.toFixed(6)).join(' '));
    }
    return rows.join('\n')+'\n';
  }
  // ---- Input interpretation ----
  // Y'CbCr -> R'G'B' for a source, from its tags. An untagged source uses its per-source choice
  // (media.colorMatrix) or else Rec. 601, which FFmpeg, and so every Splice export, has always
  // assumed for untagged video; limited range unless the stream says full. color_grade.source_matrix
  // is the export's copy of this rule.
  const KR_KB = {bt709:[.2126, .0722], bt601:[.299, .114], bt2020:[.2627, .0593]};
  function matrixName(tag, override) {
    if (tag === 'bt709') return 'bt709';
    if (['smpte170m', 'bt470bg', 'bt601', 'fcc', 'smpte240m'].includes(tag)) return 'bt601';
    if (['bt2020nc', 'bt2020c', 'bt2020-ncl'].includes(tag)) return 'bt2020';
    return override === 'bt709' || override === 'bt601' ? override : 'bt601';
  }
  // Coefficients for R'G'B' = M * [Y - y0, Cb - c0, Cr - c0] with values normalised to 0..1 of full scale.
  function inputMatrix(name, fullRange, bits = 8) {
    const [kr, kb] = KR_KB[name] || KR_KB.bt709, kg = 1-kr-kb, scale = 2**bits-1, unit = 2**(bits-8);
    const ys = fullRange ? 1 : scale/(219*unit), cs = fullRange ? 1 : scale/(224*unit);
    return {
      offset:[fullRange ? 0 : 16*unit/scale, 128*unit/scale],
      rows:[[ys, 0, 2*(1-kr)*cs], [ys, -2*(1-kb)*kb/kg*cs, -2*(1-kr)*kr/kg*cs], [ys, 2*(1-kb)*cs, 0]],
    };
  }
  window.SpliceGrade = {validateGrade, gradeActive, tables, prepare, evaluate, stages, gains, signature, curveFunction, hueOf,
    keyTimes, colorAt, keySegment, cubeText, matrixName, inputMatrix, lookup1D, trilinear, applyLook,
    limits:{table:TABLE, hueSat:HUE_SAT_SIZE, points:MAX_POINTS, keyTimes:MAX_KEY_TIMES}, animated:ANIMATED, wheels:WHEELS, curves:CURVES};
})();
