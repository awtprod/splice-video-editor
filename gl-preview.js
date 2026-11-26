/* The program monitor's picture pipeline: decode -> input transform -> grade -> composite -> display.
 *
 * engine.js describes a frame as a display list (`plan`, see framePlan there); this module draws it,
 * on WebGL2 when it is available and on a Canvas 2D fallback otherwise (or after the WebGL context is
 * lost, until it is restored). Both renderers use the same grade tables as the export (color-grade.js,
 * color_grade.py) and the same compositing math as the renderer:
 *
 *  - decode: the exact Y'CbCr planes of the decoded frame (WebCodecs VideoFrame.copyTo), converted
 *    with the matrix and range of the source under the shared rule (SpliceGrade.matrixName). Where a
 *    browser cannot hand out the planes, the browser's own RGB conversion is used and the monitor says so.
 *  - grade: 1D tables read with linear interpolation (FFmpeg lut1d), 3D lattices read with trilinear
 *    interpolation (FFmpeg lut3d), the saturation matrix, all in float, clamped to 0..1.
 *  - composite: straight-alpha "over" in the working space (FFmpeg overlay), dissolves as
 *    A*(1-p)+B*p of the two composites over the same lower layers (FFmpeg blend), dips as a gain on
 *    the graded picture (FFmpeg fade), wipes as a hard edge.
 *  - output: the Rec. 709 R'G'B' code values are written to the sRGB canvas unchanged, which is how a
 *    player shows the exported file on the same display.
 */
(() => {
  const G = window.SpliceGrade;
  const TABLE = G.limits.table, HS = G.limits.hueSat;
  const MAX_FRAME = {width:3840, height:2160};
  // GPU memory the LUT and table textures may hold before the least recently used are released.
  const TEXTURE_BUDGET = 192*1024*1024;
  // stale: layers of the last frame drawn without the exact planes of the frame on screen (a copy
  // was still in flight); 0 means the monitor shows exactly the current frames.
  const stats = {renderer:'none', frameMs:0, exactDecode:true, stale:0, textureBytes:0, textures:0, lost:0, restored:0, frames:0};
  let forceCanvas = false, onReady = () => {};

  // ---------------------------------------------------------------- decoded planes (shared)
  // Latest Y'CbCr planes per media element. VideoFrame.copyTo is asynchronous: a paint uses the last
  // planes that arrived and a fresh copy asks for a repaint when it lands.
  const planes = new WeakMap();
  const PLANAR = {I420:[1, 2, 2], I420A:[1, 2, 2], NV12:[1, 2, 2]};
  function frameKey(element) { return `${element.currentSrc || element.src}|${element.currentTime}|${element.videoWidth}x${element.videoHeight}`; }
  let retry = 0;
  // Ask for a repaint soon when the planes on hand are not the frame on screen yet.
  function repaintSoon() {
    if (retry) return;
    retry = setTimeout(() => { retry = 0; onReady(); }, 16);
  }
  function requestPlanes(element) {
    if (!(element instanceof HTMLVideoElement) || element.readyState < 2 || typeof VideoFrame !== 'function') return null;
    let entry = planes.get(element);
    if (!entry) { entry = {key:'', pending:null, frame:null, failed:false, spare:null}; planes.set(element, entry); }
    const key = frameKey(element), source = element.currentSrc || element.src;
    // Planes of another file are never shown: the browser's RGB stands in until the copy lands.
    const usable = entry.frame && entry.frame.source === source ? entry.frame : null;
    if (entry.key === key || entry.failed) return usable;
    if (!entry.pending) {
      let frame;
      try { frame = new VideoFrame(element); }
      catch (error) {
        if (error.name !== 'InvalidStateError') { entry.failed = true; console.warn('Splice monitor: exact decode unavailable', error); }
        repaintSoon();
        return usable;
      }
      const format = frame.format, rect = frame.visibleRect;
      if (!PLANAR[format]) { frame.close(); entry.failed = true; return null; }
      entry.pending = key;
      const size = frame.allocationSize({rect});
      const buffer = entry.spare?.byteLength === size ? entry.spare : new Uint8Array(size);
      entry.spare = null;
      frame.copyTo(buffer, {rect}).then(layout => {
        entry.spare = entry.frame?.data || null;
        entry.frame = {key, source, format, width:rect.width, height:rect.height, layout, data:buffer, colorSpace:frame.colorSpace?.toJSON?.() || {}};
        entry.key = key;
        onReady();
      }, error => { entry.failed = true; console.warn('Splice monitor: exact decode unavailable', error); onReady(); })
        .finally(() => { frame.close(); if (entry.pending === key) entry.pending = null; });
    }
    return usable;
  }
  // R'G'B' (0..1) of planes at integer source position, for the Canvas fallback.
  function planeConverter(frame, input) {
    const {data, layout, width} = frame, m = G.inputMatrix(input.matrix, frame.colorSpace?.fullRange ?? input.fullRange);
    const [y0, c0] = m.offset, rows = m.rows, nv12 = frame.format === 'NV12';
    return (x, y, out) => {
      const Y = data[layout[0].offset+y*layout[0].stride+x]/255, cx = x >> 1, cy = y >> 1;
      const U = (nv12 ? data[layout[1].offset+cy*layout[1].stride+2*cx] : data[layout[1].offset+cy*layout[1].stride+cx])/255-c0;
      const V = (nv12 ? data[layout[1].offset+cy*layout[1].stride+2*cx+1] : data[layout[2].offset+cy*layout[2].stride+cx])/255-c0;
      const L = Y-y0;
      out[0] = rows[0][0]*L+rows[0][2]*V; out[1] = rows[1][0]*L+rows[1][1]*U+rows[1][2]*V; out[2] = rows[2][0]*L+rows[2][1]*U;
      out[3] = frame.format === 'I420A' ? data[layout[3].offset+y*layout[3].stride+x] : 255;
      return out;
    };
  }

  // ---------------------------------------------------------------- WebGL2 renderer
  const VERTEX = `#version 300 es
in vec2 a_corner;
uniform vec2 u_canvas;
uniform vec2 u_center;
uniform vec2 u_size;
uniform float u_angle;
out vec2 v_uv;
void main() {
  vec2 local = a_corner*u_size;
  float c = cos(u_angle), s = sin(u_angle);
  vec2 pixel = u_center + vec2(c*local.x - s*local.y, s*local.x + c*local.y);
  gl_Position = vec4(pixel.x/u_canvas.x*2.0-1.0, 1.0-pixel.y/u_canvas.y*2.0, 0.0, 1.0);
  v_uv = a_corner + 0.5;
}`;
  // Compiled once per feature set (SOURCE, SETS, HUE, LOOK, INPUT): a plain layer runs a short
  // program; uniform branches in one large shader cost a software rasteriser several times more.
  const FRAGMENT = defines => `#version 300 es
${defines}
precision highp float; precision highp int; precision highp sampler3D;
in vec2 v_uv;
out vec4 outColor;
uniform sampler2D u_y, u_u, u_v, u_planeAlpha;
uniform mat3 u_yuv;              // columns: Y, Cb, Cr contributions
uniform vec2 u_yuvOffset;
uniform float u_keyWeight;
uniform sampler2D u_tab0, u_tab1; // 4096 x 2: row 0 pre, row 1 post (RGB)
uniform ivec2 u_flags;           // per set: 1 pre, 2 matrix, 4 post
uniform mat3 u_mat0, u_mat1;
uniform sampler3D u_hs;
// LOOK: 0 none, 1 lattice, 2 1D curve. SOURCE: 0 RGB(A), 1 I420, 2 NV12, 3 I420 + alpha.
uniform sampler3D u_look3; uniform sampler2D u_look1;
uniform int u_lookSize; uniform int u_lookWidth; uniform vec3 u_lookMin, u_lookMax; uniform float u_lookMix;
// INPUT: the source's technical LUT, encoded as LOOK, at 100 %.
uniform sampler3D u_in3; uniform sampler2D u_in1;
uniform int u_inSize; uniform int u_inWidth; uniform vec3 u_inMin, u_inMax;
uniform float u_alpha, u_gain;
uniform vec4 u_wipe;             // x: wipe edge in pixels (or -1), y: canvas height
float tab(sampler2D t, int row, float x, int channel) {
  float p = clamp(x, 0.0, 1.0)*${TABLE - 1}.0; int i0 = int(p); int i1 = min(i0+1, ${TABLE - 1});
  float a = texelFetch(t, ivec2(i0, row), 0)[channel], b = texelFetch(t, ivec2(i1, row), 0)[channel];
  return a + (p-float(i0))*(b-a);
}
vec3 lattice(sampler3D t, int size, vec3 x) {
  vec3 p = clamp(x, 0.0, 1.0)*float(size-1); ivec3 i0 = ivec3(p); ivec3 i1 = min(i0+1, ivec3(size-1)); vec3 f = p-vec3(i0);
  vec3 c000 = texelFetch(t, i0, 0).rgb, c100 = texelFetch(t, ivec3(i1.x,i0.y,i0.z), 0).rgb;
  vec3 c010 = texelFetch(t, ivec3(i0.x,i1.y,i0.z), 0).rgb, c110 = texelFetch(t, ivec3(i1.x,i1.y,i0.z), 0).rgb;
  vec3 c001 = texelFetch(t, ivec3(i0.x,i0.y,i1.z), 0).rgb, c101 = texelFetch(t, ivec3(i1.x,i0.y,i1.z), 0).rgb;
  vec3 c011 = texelFetch(t, ivec3(i0.x,i1.y,i1.z), 0).rgb, c111 = texelFetch(t, i1, 0).rgb;
  vec3 c00 = c000+f.x*(c100-c000), c10 = c010+f.x*(c110-c010), c01 = c001+f.x*(c101-c001), c11 = c011+f.x*(c111-c011);
  vec3 c0 = c00+f.y*(c10-c00), c1 = c01+f.y*(c11-c01);
  return c0+f.z*(c1-c0);
}
float curve(sampler2D t, int size, int width, float x, int channel) {
  float p = clamp(x, 0.0, 1.0)*float(size-1); int i0 = int(p); int i1 = min(i0+1, size-1);
  float a = texelFetch(t, ivec2(i0 % width, i0 / width), 0)[channel], b = texelFetch(t, ivec2(i1 % width, i1 / width), 0)[channel];
  return a + (p-float(i0))*(b-a);
}
vec3 look(int kind, sampler3D t3, sampler2D t1, int size, int width, vec3 lo, vec3 hi, float mixing, vec3 x) {
  vec3 n = (x-lo)/(hi-lo), l;
  if (kind == 1) l = lattice(t3, size, n);
  else l = vec3(curve(t1, size, width, n.r, 0), curve(t1, size, width, n.g, 1), curve(t1, size, width, n.b, 2));
  return x*(1.0-mixing) + l*mixing;
}
vec3 primaries(vec3 x, sampler2D t, int flags, mat3 m) {
  if ((flags & 1) != 0) x = vec3(tab(t, 0, x.r, 0), tab(t, 0, x.g, 0), tab(t, 0, x.b, 0));
  if ((flags & 2) != 0) x = m*x;
  if ((flags & 4) != 0) x = vec3(tab(t, 1, x.r, 0), tab(t, 1, x.g, 1), tab(t, 1, x.b, 2));
  return x;
}
vec3 grade(vec3 x, sampler2D t, int flags, mat3 m) {
  x = primaries(x, t, flags, m);
#if HUE == 1
  x = lattice(u_hs, ${HS}, x);
#endif
#if LOOK != 0
  x = look(LOOK, u_look3, u_look1, u_lookSize, u_lookWidth, u_lookMin, u_lookMax, u_lookMix, x);
#endif
  return clamp(x, 0.0, 1.0);
}
void main() {
  vec4 source;
#if SOURCE == 0
  source = texture(u_y, v_uv);
#else
  float Y = texture(u_y, v_uv).r - u_yuvOffset.x;
#if SOURCE == 2
  vec2 C = texture(u_u, v_uv).rg;
#else
  vec2 C = vec2(texture(u_u, v_uv).r, texture(u_v, v_uv).r);
#endif
  C -= u_yuvOffset.y;
#if SOURCE == 3
  source = vec4(u_yuv*vec3(Y, C), texture(u_planeAlpha, v_uv).r);
#else
  source = vec4(u_yuv*vec3(Y, C), 1.0);
#endif
#endif
  vec3 x = source.rgb;
#if INPUT != 0
  x = clamp(look(INPUT, u_in3, u_in1, u_inSize, u_inWidth, u_inMin, u_inMax, 1.0, x), 0.0, 1.0);
#endif
#if SETS == 1
  x = grade(x, u_tab0, u_flags.x, u_mat0);
#elif SETS == 2
  vec3 a = grade(x, u_tab0, u_flags.x, u_mat0), b = grade(x, u_tab1, u_flags.y, u_mat1); x = a + (b-a)*u_keyWeight;
#else
  x = clamp(x, 0.0, 1.0);
#endif
  x *= u_gain;
  if (u_wipe.x >= 0.0 && gl_FragCoord.x >= u_wipe.x) discard;
  if (u_wipe.z >= 0.0 && gl_FragCoord.x < u_wipe.z) discard;
  outColor = vec4(x, source.a*u_alpha);
}`;
  // Full-frame passes: copy, mix of two composites, monitoring views.
  const PASS_VERTEX = `#version 300 es
in vec2 a_corner; out vec2 v_uv;
void main() { v_uv = a_corner + 0.5; gl_Position = vec4(a_corner*2.0, 0.0, 1.0); }`;
  const PASS_FRAGMENT = defines => `#version 300 es
${defines}
precision highp float;
in vec2 v_uv; out vec4 outColor;
uniform sampler2D u_a, u_b;
uniform float u_mix;
uniform vec4 u_split;            // x: split position 0..1 (or -1), y: 1 to flip vertically
uniform sampler2D u_before;
float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
vec3 falseColor(float y) {
  float code = y*255.0;
  if (code < 16.0) return vec3(0.35, 0.0, 0.6);
  if (code < 40.0) return vec3(0.0, 0.25, 0.9);
  if (code > 250.0) return vec3(1.0, 0.0, 0.0);
  if (code > 235.0) return vec3(1.0, 0.85, 0.0);
  if (abs(code-118.0) < 6.0) return vec3(0.1, 0.75, 0.2);
  if (abs(code-170.0) < 6.0) return vec3(1.0, 0.55, 0.7);
  return vec3(y);
}
void main() {
  vec2 uv = u_split.y > 0.5 ? vec2(v_uv.x, 1.0-v_uv.y) : v_uv;
  vec4 a = texture(u_a, uv), b = texture(u_b, uv);
  vec4 c = a + (b-a)*u_mix;
#if VIEW == 0 && SPLIT == 0
  outColor = c;
  return;
#endif
  if (u_split.x >= 0.0 && uv.x > u_split.x) c = texture(u_before, uv);
  float y = luma(c.rgb);
  if (VIEW == 1) c.rgb = vec3(y);
  else if (VIEW == 2) c.rgb = vec3(c.r);
  else if (VIEW == 3) c.rgb = vec3(c.g);
  else if (VIEW == 4) c.rgb = vec3(c.b);
  else if (VIEW == 5) c.rgb = falseColor(y);
  else if (VIEW == 6) { if (y*255.0 > 235.0 || y*255.0 < 16.0) c.rgb = mod(floor(gl_FragCoord.x/4.0)+floor(gl_FragCoord.y/4.0), 2.0) < 1.0 ? vec3(1.0, 0.0, 1.0) : c.rgb; }
  else if (VIEW == 7) { if (max(c.r, max(c.g, c.b)) >= 0.98) c.rgb = mod(floor((gl_FragCoord.x+gl_FragCoord.y)/6.0), 2.0) < 1.0 ? vec3(0.0) : vec3(1.0); }
  outColor = c;
}`;
  const VIEW = {normal:0, luma:1, red:2, green:3, blue:4, falseColor:5, legal:6, zebra:7};

  class GLRenderer {
    constructor() {
      this.canvas = document.createElement('canvas');
      this.lost = false;
      this.onLost = event => { event.preventDefault(); this.lost = true; stats.lost++; this.released(); };
      this.onRestored = () => { this.lost = false; stats.restored++; try { this.setup(); } catch (error) { console.error(error); this.failed = true; } onReady(); };
      this.canvas.addEventListener('webglcontextlost', this.onLost);
      this.canvas.addEventListener('webglcontextrestored', this.onRestored);
      const gl = this.canvas.getContext('webgl2', {alpha:false, antialias:false, depth:false, stencil:false, premultipliedAlpha:false, preserveDrawingBuffer:true});
      if (!gl) throw new Error('WebGL2 is unavailable');
      this.gl = gl;
      try { this.setup(); } catch (error) { this.destroy(); throw error; }
    }
    // One linked program per feature set, kept for the life of the context.
    program(kind, features) {
      const key = kind+JSON.stringify(features);
      let program = this.variants.get(key);
      if (!program) {
        const defines = Object.entries(features).map(([name, value]) => `#define ${name} ${value}`).join('\n');
        program = kind === 'layer' ? this.link(VERTEX, FRAGMENT(defines)) : this.link(PASS_VERTEX, PASS_FRAGMENT(defines));
        this.variants.set(key, program); this.programs.push(program.program);
      }
      return program;
    }
    released() {
      this.variants = new Map();
      this.textures = new Map(); this.planeTextures = new WeakMap(); this.targets = []; this.programs = null; this.frameTextures = new Map();
      stats.textureBytes = 0; stats.textures = 0;
    }
    setup() {
      const gl = this.gl;
      this.released();
      const compile = (type, source) => {
        const shader = gl.createShader(type); gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS) && !gl.isContextLost()) {
          const message = gl.getShaderInfoLog(shader);
          gl.deleteShader(shader);
          throw new Error(message);
        }
        return shader;
      };
      const link = (vertex, fragment) => {
        const program = gl.createProgram(), shaders = [];
        try {
          shaders.push(compile(gl.VERTEX_SHADER, vertex));
          shaders.push(compile(gl.FRAGMENT_SHADER, fragment));
          for (const shader of shaders) gl.attachShader(program, shader);
          gl.bindAttribLocation(program, 0, 'a_corner'); gl.linkProgram(program);
          if (!gl.getProgramParameter(program, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error(gl.getProgramInfoLog(program));
        } catch (error) {
          gl.deleteProgram(program);
          throw error;
        } finally {
          for (const shader of shaders) gl.deleteShader(shader);
        }
        const uniforms = {};
        const count = gl.getProgramParameter(program, gl.ACTIVE_UNIFORMS) || 0;
        for (let i = 0; i < count; i++) { const info = gl.getActiveUniform(program, i); uniforms[info.name] = gl.getUniformLocation(program, info.name); }
        return {program, uniforms};
      };
      this.link = link;
      this.variants = new Map();
      this.programs = [];
      this.quad = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-.5,-.5, .5,-.5, -.5,.5, .5,.5]), gl.STATIC_DRAW);
      this.vao = gl.createVertexArray(); gl.bindVertexArray(this.vao);
      gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      this.dummy2D = this.texture2D(gl.RGBA32F, 1, 1, gl.RGBA, gl.FLOAT, new Float32Array(4), 'dummy2d', true);
      this.dummy3D = gl.createTexture(); gl.bindTexture(gl.TEXTURE_3D, this.dummy3D);
      gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGB32F, 1, 1, 1, 0, gl.RGB, gl.FLOAT, new Float32Array(3));
      this.nearest(gl.TEXTURE_3D);
    }
    nearest(target) {
      const gl = this.gl;
      gl.texParameteri(target, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(target, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(target, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(target, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      if (target === gl.TEXTURE_3D) gl.texParameteri(target, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    }
    texture2D(internal, width, height, format, type, data, key, pinned = false) {
      const gl = this.gl, texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, internal, width, height, 0, format, type, data);
      this.nearest(gl.TEXTURE_2D);
      if (!pinned) this.remember(key, texture, (data?.byteLength) || width*height*16);
      return texture;
    }
    // Least-recently-used table and LUT textures, bounded by TEXTURE_BUDGET.
    remember(key, texture, bytes) {
      this.textures.set(key, {texture, bytes});
      stats.textureBytes += bytes; stats.textures = this.textures.size;
      for (const [old, entry] of this.textures) {
        if (stats.textureBytes <= TEXTURE_BUDGET || old === key) break;
        this.gl.deleteTexture(entry.texture); this.textures.delete(old); stats.textureBytes -= entry.bytes;
      }
      stats.textures = this.textures.size;
    }
    cached(key) {
      const entry = this.textures.get(key);
      if (!entry) return null;
      this.textures.delete(key); this.textures.set(key, entry);
      return entry.texture;
    }
    tableTexture(tables) {
      const key = 'tab|'+tables.key;
      let texture = this.cached(key);
      if (texture) return texture;
      const data = new Float32Array(TABLE*2*4);
      for (let i = 0; i < TABLE; i++) {
        const pre = tables.pre ? tables.pre[i] : i/(TABLE-1);
        data[i*4] = pre; data[i*4+1] = pre; data[i*4+2] = pre;
        for (let c = 0; c < 3; c++) data[(TABLE+i)*4+c] = tables.post ? tables.post[c][i] : i/(TABLE-1);
      }
      return this.texture2D(this.gl.RGBA32F, TABLE, 2, this.gl.RGBA, this.gl.FLOAT, data, key);
    }
    latticeTexture(key, size, data) {
      let texture = this.cached(key);
      if (texture) return texture;
      const gl = this.gl;
      texture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_3D, texture);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGB32F, size, size, size, 0, gl.RGB, gl.FLOAT, data instanceof Float32Array ? data : Float32Array.from(data));
      this.nearest(gl.TEXTURE_3D);
      this.remember(key, texture, size**3*12);
      return texture;
    }
    curveTexture(key, curve) {
      let texture = this.cached(key);
      if (texture) return {texture, width:Math.min(curve.size, 4096)};
      const width = Math.min(curve.size, 4096), rows = Math.ceil(curve.size/width), data = new Float32Array(width*rows*4);
      for (let i = 0; i < curve.size; i++) for (let c = 0; c < 3; c++) data[i*4+c] = curve.data[i*3+c];
      texture = this.texture2D(this.gl.RGBA32F, width, rows, this.gl.RGBA, this.gl.FLOAT, data, key);
      return {texture, width};
    }
    lutBinding(lut) {
      if (!lut) return null;
      const id = lutId(lut);
      if (lut.curve) { const {texture, width} = this.curveTexture('curve|'+id, lut.curve); return {kind:2, texture, width, size:lut.curve.size, lut}; }
      return {kind:1, texture:this.latticeTexture('lattice|'+id, lut.size, lut.data), size:lut.size, width:1, lut};
    }
    target(index, width, height) {
      const gl = this.gl;
      let target = this.targets[index];
      if (!target || target.width !== width || target.height !== height) {
        if (target) { gl.deleteTexture(target.texture); gl.deleteFramebuffer(target.framebuffer); }
        const texture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        this.nearest(gl.TEXTURE_2D);
        const framebuffer = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
        target = this.targets[index] = {texture, framebuffer, width, height};
      }
      return target;
    }
    // Source textures of one layer: exact planes when available, else the browser's RGB.
    sourceTextures(op) {
      const gl = this.gl, element = op.element;
      let state = this.planeTextures.get(element);
      if (!state) { state = {y:gl.createTexture(), u:gl.createTexture(), v:gl.createTexture(), a:gl.createTexture(), key:''}; this.planeTextures.set(element, state); this.frameTextures.set(state, element); }
      const frame = op.exact === false ? null : requestPlanes(element);
      if (op.exact !== false && element instanceof HTMLVideoElement && frame?.key !== frameKey(element)) stats.stale++;
      const linear = texture => { gl.bindTexture(gl.TEXTURE_2D, texture); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE); };
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      if (frame) {
        if (state.key !== frame.key) {
          const {data, layout, width, height, format} = frame, cw = Math.ceil(width/2), ch = Math.ceil(height/2);
          // Luma is filtered; chroma is replicated over its 2x2 block, as the browser's own decode
          // does (FFmpeg interpolates chroma: sharp colour edges can differ, flat areas do not).
          const upload = (texture, plane, w, h, internal, fmt, channels) => {
            linear(texture);
            if (plane === 1 || plane === 2) { gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST); }
            gl.pixelStorei(gl.UNPACK_ROW_LENGTH, layout[plane].stride/channels);
            gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, fmt, gl.UNSIGNED_BYTE, data.subarray(layout[plane].offset));
          };
          upload(state.y, 0, width, height, gl.R8, gl.RED, 1);
          if (format === 'NV12') upload(state.u, 1, cw, ch, gl.RG8, gl.RG, 2);
          else { upload(state.u, 1, cw, ch, gl.R8, gl.RED, 1); upload(state.v, 2, cw, ch, gl.R8, gl.RED, 1); }
          if (format === 'I420A') upload(state.a, 3, width, height, gl.R8, gl.RED, 1);
          gl.pixelStorei(gl.UNPACK_ROW_LENGTH, 0);
          state.key = frame.key;
        }
        return {kind:frame.format === 'NV12' ? 2 : 1, state, frame};
      }
      // Browser RGB: stills drawn on a canvas, generators, or a browser without exact planes.
      linear(state.y);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, element);
      state.key = '';
      if (element instanceof HTMLVideoElement) stats.exactDecode = false;
      return {kind:0, state};
    }
    bindGrade(u, grade) {
      const gl = this.gl;
      const flags = prepared => (prepared.tables.pre ? 1 : 0) | (prepared.tables.matrix ? 2 : 0) | (prepared.tables.post ? 4 : 0);
      const matrix = prepared => new Float32Array(prepared.tables.matrix ? [0,1,2].flatMap(c => [0,1,2].map(r => prepared.tables.matrix[r][c])) : [1,0,0,0,1,0,0,0,1]);
      const a = grade?.a, b = grade?.b || grade?.a;
      gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, a ? this.tableTexture(a.tables) : this.dummy2D); gl.uniform1i(u.u_tab0, 3);
      gl.activeTexture(gl.TEXTURE4); gl.bindTexture(gl.TEXTURE_2D, b ? this.tableTexture(b.tables) : this.dummy2D); gl.uniform1i(u.u_tab1, 4);
      gl.uniform2i(u.u_flags, a ? flags(a) : 0, b ? flags(b) : 0);
      gl.uniformMatrix3fv(u.u_mat0, false, a ? matrix(a) : matrix({tables:{}}));
      gl.uniformMatrix3fv(u.u_mat1, false, b ? matrix(b) : matrix({tables:{}}));
      gl.uniform1f(u.u_keyWeight, grade?.weight || 0);
      const hs = a?.tables.hueSat;
      gl.activeTexture(gl.TEXTURE5); gl.bindTexture(gl.TEXTURE_3D, hs ? this.latticeTexture('hs|'+a.tables.key, HS, hs) : this.dummy3D); gl.uniform1i(u.u_hs, 5);
      this.bindLook(u, 'look', a?.look ? this.lutBinding(a.look) : null, a?.lookIntensity ?? 0, 6);
      this.bindLook(u, 'in', grade?.input ? this.lutBinding(grade.input) : null, 100, 8);
    }
    bindLook(u, name, binding, intensity, unit) {
      const gl = this.gl;
      gl.activeTexture(gl.TEXTURE0+unit); gl.bindTexture(gl.TEXTURE_3D, binding?.kind === 1 ? binding.texture : this.dummy3D); gl.uniform1i(u[`u_${name}3`], unit);
      gl.activeTexture(gl.TEXTURE0+unit+1); gl.bindTexture(gl.TEXTURE_2D, binding?.kind === 2 ? binding.texture : this.dummy2D); gl.uniform1i(u[`u_${name}1`], unit+1);
      gl.uniform1i(u[`u_${name}Size`], binding?.size || 2); gl.uniform1i(u[`u_${name}Width`], binding?.width || 1);
      gl.uniform3fv(u[`u_${name}Min`], binding?.lut.domainMin || [0,0,0]); gl.uniform3fv(u[`u_${name}Max`], binding?.lut.domainMax || [1,1,1]);
      if (u[`u_${name}Mix`]) gl.uniform1f(u[`u_${name}Mix`], Math.max(0, Math.min(1, intensity/100)));
    }
    features(sourceKind, grade) {
      const a = grade?.a;
      const lookKind = lut => !lut ? 0 : lut.curve ? 2 : 1;
      return {SOURCE:sourceKind, SETS:grade ? (grade.b ? 2 : 1) : 0, HUE:a?.tables.hueSat ? 1 : 0,
        LOOK:lookKind(a?.look), INPUT:lookKind(grade?.input)};
    }
    drawLayer(op, width, height) {
      const gl = this.gl;
      const source = this.sourceTextures(op);
      const {program, uniforms:u} = this.program('layer', this.features(source.kind === 1 && source.frame.format === 'I420A' ? 3 : source.kind, op.grade));
      gl.useProgram(program);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, source.state.y); gl.uniform1i(u.u_y, 0);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, source.state.u); gl.uniform1i(u.u_u, 1);
      gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, source.state.v); gl.uniform1i(u.u_v, 2);
      gl.activeTexture(gl.TEXTURE10); gl.bindTexture(gl.TEXTURE_2D, source.state.a); gl.uniform1i(u.u_planeAlpha, 10);
      if (source.kind) {
        const m = G.inputMatrix(op.input.matrix, source.frame.colorSpace?.fullRange ?? op.input.fullRange);
        gl.uniformMatrix3fv(u.u_yuv, false, new Float32Array([m.rows[0][0], m.rows[1][0], m.rows[2][0], m.rows[0][1], m.rows[1][1], m.rows[2][1], m.rows[0][2], m.rows[1][2], m.rows[2][2]]));
        gl.uniform2f(u.u_yuvOffset, m.offset[0], m.offset[1]);
      }
      this.bindGrade(u, op.grade);
      const g = op.geometry, fit = Math.min(width/g.sourceWidth, height/g.sourceHeight);
      gl.uniform2f(u.u_canvas, width, height);
      gl.uniform2f(u.u_center, width/2+g.x*width/100, height/2+g.y*height/100);
      gl.uniform2f(u.u_size, g.sourceWidth*fit*g.scale, g.sourceHeight*fit*g.scale);
      gl.uniform1f(u.u_angle, g.rotation);
      gl.uniform1f(u.u_alpha, op.alpha); gl.uniform1f(u.u_gain, op.brightness ?? 1);
      gl.uniform4f(u.u_wipe, op.wipe == null ? -1 : op.wipe*width, height, op.wipeOut == null ? -1 : op.wipeOut*width, 0);
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.disable(gl.BLEND);
    }
    // Copy `from` into `to` (or mix two composites, or apply the monitoring view on the way out).
    passTo(framebuffer, a, b, mix, view = 0, flip = false, before = null, split = -1) {
      const gl = this.gl, {program, uniforms:u} = this.program('pass', {VIEW:view, SPLIT:before ? 1 : 0});
      gl.useProgram(program);
      gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, a); gl.uniform1i(u.u_a, 0);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, b || a); gl.uniform1i(u.u_b, 1);
      gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, before || a); gl.uniform1i(u.u_before, 2);
      gl.uniform1f(u.u_mix, mix); gl.uniform4f(u.u_split, before ? split : -1, flip ? 1 : 0, 0, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
    // An adjustment clip: grade what lies beneath, mixed in by the clip's opacity.
    adjust(op, acc, scratch, width, height) {
      const gl = this.gl;
      this.passTo(scratch.framebuffer, acc.texture, null, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, acc.framebuffer);
      gl.viewport(0, 0, width, height);
      const {program, uniforms:u} = this.program('layer', this.features(0, op.grade));
      gl.useProgram(program);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, scratch.texture); gl.uniform1i(u.u_y, 0);
      this.bindGrade(u, op.grade);
      gl.uniform2f(u.u_canvas, width, height); gl.uniform2f(u.u_center, width/2, height/2); gl.uniform2f(u.u_size, width, -height);
      gl.uniform1f(u.u_angle, 0); gl.uniform1f(u.u_alpha, op.alpha); gl.uniform1f(u.u_gain, 1); gl.uniform4f(u.u_wipe, -1, height, -1, 0);
      gl.enable(gl.BLEND); gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4); gl.disable(gl.BLEND);
    }
    titleTexture(op, width, height) {
      let canvas = this.titleCanvas;
      if (!canvas) canvas = this.titleCanvas = document.createElement('canvas');
      if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, width, height);
      op.paint(ctx, canvas);
      return canvas;
    }
    // Draw ops into the framebuffer `acc` (composites over what it holds).
    compose(ops, acc, depth, width, height) {
      const gl = this.gl;
      for (const op of ops) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, acc.framebuffer); gl.viewport(0, 0, width, height);
        if (op.type === 'video') this.drawLayer(op, width, height);
        else if (op.type === 'title') {
          const canvas = this.titleTexture(op, width, height);
          this.drawLayer({element:canvas, exact:false, alpha:1, geometry:{sourceWidth:width, sourceHeight:height, x:0, y:0, scale:1, rotation:0}}, width, height);
        } else if (op.type === 'adjust') this.adjust(op, acc, this.target(depth+1, width, height), width, height);
        else if (op.type === 'dissolve') {
          // A = beneath + outgoing, B = beneath + incoming; acc = A*(1-p) + B*p.
          const a = this.target(depth+1, width, height), b = this.target(depth+2, width, height);
          this.passTo(a.framebuffer, acc.texture, null, 0); this.passTo(b.framebuffer, acc.texture, null, 0);
          gl.viewport(0, 0, width, height);
          if (op.outgoing) this.compose([op.outgoing], a, depth+3, width, height);
          if (op.incoming) this.compose([op.incoming], b, depth+3, width, height);
          gl.viewport(0, 0, width, height);
          this.passTo(acc.framebuffer, a.texture, b.texture, op.progress);
        }
      }
    }
    render(plan) {
      const gl = this.gl, {width, height} = plan;
      if (this.canvas.width !== width || this.canvas.height !== height) { this.canvas.width = width; this.canvas.height = height; }
      const acc = this.target(0, width, height);
      gl.bindFramebuffer(gl.FRAMEBUFFER, acc.framebuffer); gl.viewport(0, 0, width, height);
      gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      this.compose(plan.ops, acc, 0, width, height);
      let before = null;
      if (plan.split != null && plan.beforeOps) {
        before = this.target(7, width, height);
        gl.bindFramebuffer(gl.FRAMEBUFFER, before.framebuffer); gl.viewport(0, 0, width, height);
        gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
        this.compose(plan.beforeOps, before, 8, width, height);
      }
      gl.viewport(0, 0, width, height);
      this.passTo(null, acc.texture, null, 0, VIEW[plan.view] || 0, false, before?.texture, plan.split ?? -1);
      return this.canvas;
    }
    readPixels(x, y, w, h) {
      const gl = this.gl, out = new Uint8Array(w*h*4);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.targets[0].framebuffer);
      gl.readPixels(x, y, w, h, gl.RGBA, gl.UNSIGNED_BYTE, out);
      return out;
    }
    destroy() {
      const gl = this.gl;
      this.canvas.removeEventListener('webglcontextlost', this.onLost);
      this.canvas.removeEventListener('webglcontextrestored', this.onRestored);
      if (!gl.isContextLost()) {
        for (const entry of this.textures.values()) gl.deleteTexture(entry.texture);
        for (const target of this.targets) if (target) { gl.deleteTexture(target.texture); gl.deleteFramebuffer(target.framebuffer); }
        for (const state of this.frameTextures.keys()) { gl.deleteTexture(state.y); gl.deleteTexture(state.u); gl.deleteTexture(state.v); gl.deleteTexture(state.a); }
        for (const program of this.programs || []) gl.deleteProgram(program);
        gl.deleteBuffer(this.quad); gl.deleteVertexArray(this.vao); gl.deleteTexture(this.dummy2D); gl.deleteTexture(this.dummy3D);
      }
      this.released();
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    }
  }
  // Release the textures of media elements the engine no longer shows.
  function forget(elements) {
    if (!gl || gl.lost) return;
    for (const [state, element] of gl.frameTextures) if (!elements.has(element)) {
      gl.gl.deleteTexture(state.y); gl.gl.deleteTexture(state.u); gl.gl.deleteTexture(state.v); gl.gl.deleteTexture(state.a);
      gl.frameTextures.delete(state); gl.planeTextures.delete(element);
    }
  }
  const lutIds = new WeakMap(); let nextLut = 1;
  function lutId(lut) {
    const key = lut.curve ? lut.curve.data : lut.data;
    if (!lutIds.has(key)) lutIds.set(key, nextLut++);
    return lutIds.get(key);
  }

  // ---------------------------------------------------------------- Canvas 2D fallback
  // Same plan, same tables, on the CPU. Pixels are graded at 8-bit inputs through a cache of the
  // whole chain per colour, so repeated colours cost one lookup.
  class CanvasRenderer {
    constructor() { this.canvas = document.createElement('canvas'); this.layers = []; this.sources = new WeakMap(); }
    scratch(index, width, height) {
      let canvas = this.layers[index];
      if (!canvas) canvas = this.layers[index] = document.createElement('canvas');
      if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
      return canvas;
    }
    // The source picture as R'G'B': decoded from the exact planes when possible.
    sourceCanvas(op) {
      const element = op.element;
      const frame = op.exact === false ? null : requestPlanes(element);
      if (op.exact !== false && element instanceof HTMLVideoElement && frame?.key !== frameKey(element)) stats.stale++;
      if (!frame) { if (element instanceof HTMLVideoElement) stats.exactDecode = false; return element; }
      let entry = this.sources.get(element);
      if (!entry) { entry = {canvas:document.createElement('canvas'), key:''}; this.sources.set(element, entry); }
      if (entry.key !== frame.key + op.input.matrix) {
        const {width, height} = frame, canvas = entry.canvas;
        if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
        const ctx = canvas.getContext('2d', {willReadFrequently:true}), image = ctx.createImageData(width, height), out = image.data;
        const convert = planeConverter(frame, op.input), rgb = [0, 0, 0, 255];
        for (let y = 0, i = 0; y < height; y++) for (let x = 0; x < width; x++, i += 4) {
          convert(x, y, rgb);
          out[i] = Math.round(Math.max(0, Math.min(1, rgb[0]))*255); out[i+1] = Math.round(Math.max(0, Math.min(1, rgb[1]))*255); out[i+2] = Math.round(Math.max(0, Math.min(1, rgb[2]))*255); out[i+3] = rgb[3];
        }
        ctx.putImageData(image, 0, 0);
        entry.key = frame.key + op.input.matrix;
      }
      return entry.canvas;
    }
    gradePixels(image, grade) {
      if (!grade) return;
      const data = image.data, cache = new Map(), x = [0, 0, 0], y = [0, 0, 0], scratch = [0, 0, 0];
      const a = grade.a, b = grade.b, w = grade.weight || 0, input = grade.input;
      const withInput = prepared => input ? {...prepared, input} : prepared;
      const A = withInput(a), B = b && withInput(b);
      for (let i = 0; i < data.length; i += 4) {
        if (!data[i+3]) continue;
        const key = data[i] << 16 | data[i+1] << 8 | data[i+2];
        let packed = cache.get(key);
        if (packed === undefined) {
          x[0] = data[i]/255; x[1] = data[i+1]/255; x[2] = data[i+2]/255;
          y[0] = x[0]; y[1] = x[1]; y[2] = x[2];
          G.evaluate(A, x, scratch);
          if (B) { G.evaluate(B, y, scratch); for (let c = 0; c < 3; c++) x[c] += (y[c]-x[c])*w; }
          packed = Math.round(x[0]*255) | Math.round(x[1]*255) << 8 | Math.round(x[2]*255) << 16;
          if (cache.size < 1 << 20) cache.set(key, packed);
        }
        data[i] = packed & 255; data[i+1] = packed >> 8 & 255; data[i+2] = packed >> 16;
      }
    }
    drawLayer(ctx, op, width, height) {
      const source = op.type === 'title' ? null : this.sourceCanvas(op);
      const layer = this.scratch(4, width, height), lctx = layer.getContext('2d', {willReadFrequently:true});
      lctx.clearRect(0, 0, width, height);
      if (op.type === 'title') op.paint(lctx, layer);
      else {
        const g = op.geometry, fit = Math.min(width/g.sourceWidth, height/g.sourceHeight);
        lctx.save(); lctx.translate(width/2+g.x*width/100, height/2+g.y*height/100); lctx.rotate(g.rotation);
        lctx.drawImage(source, -g.sourceWidth*fit*g.scale/2, -g.sourceHeight*fit*g.scale/2, g.sourceWidth*fit*g.scale, g.sourceHeight*fit*g.scale);
        lctx.restore();
        if (op.grade || op.brightness < 1) {
          const image = lctx.getImageData(0, 0, width, height);
          this.gradePixels(image, op.grade);
          if (op.brightness < 1) for (let i = 0; i < image.data.length; i += 4) { image.data[i] *= op.brightness; image.data[i+1] *= op.brightness; image.data[i+2] *= op.brightness; }
          lctx.putImageData(image, 0, 0);
        }
      }
      ctx.save(); ctx.globalAlpha = op.alpha ?? 1;
      if (op.wipe != null) { ctx.beginPath(); ctx.rect(0, 0, width*op.wipe, height); ctx.clip(); }
      if (op.wipeOut != null) { ctx.beginPath(); ctx.rect(width*op.wipeOut, 0, width*(1-op.wipeOut), height); ctx.clip(); }
      ctx.drawImage(layer, 0, 0); ctx.restore();
    }
    compose(ctx, ops, depth, width, height) {
      for (const op of ops) {
        if (op.type === 'video' || op.type === 'title') this.drawLayer(ctx, op, width, height);
        else if (op.type === 'adjust') {
          const image = ctx.getImageData(0, 0, width, height), original = new Uint8ClampedArray(image.data);
          this.gradePixels(image, op.grade);
          const a = op.alpha;
          for (let i = 0; i < original.length; i += 4) for (let c = 0; c < 3; c++) image.data[i+c] = original[i+c]+(image.data[i+c]-original[i+c])*a;
          ctx.putImageData(image, 0, 0);
        } else if (op.type === 'dissolve') {
          const A = this.scratch(5+depth*2, width, height), B = this.scratch(6+depth*2, width, height);
          const actx = A.getContext('2d', {willReadFrequently:true}), bctx = B.getContext('2d', {willReadFrequently:true});
          actx.globalAlpha = 1; bctx.globalAlpha = 1;
          actx.drawImage(ctx.canvas, 0, 0); bctx.drawImage(ctx.canvas, 0, 0);
          if (op.outgoing) this.compose(actx, [op.outgoing], depth+1, width, height);
          if (op.incoming) this.compose(bctx, [op.incoming], depth+1, width, height);
          // A*(1-p)+B*p: draw A, then B at opacity p.
          ctx.save(); ctx.globalAlpha = 1; ctx.drawImage(A, 0, 0); ctx.globalAlpha = op.progress; ctx.drawImage(B, 0, 0); ctx.restore();
        }
      }
    }
    render(plan) {
      const {width, height} = plan;
      if (this.canvas.width !== width || this.canvas.height !== height) { this.canvas.width = width; this.canvas.height = height; }
      const ctx = this.canvas.getContext('2d', {willReadFrequently:true});
      ctx.globalAlpha = 1; ctx.fillStyle = '#000'; ctx.fillRect(0, 0, width, height);
      this.compose(ctx, plan.ops, 0, width, height);
      if (plan.split != null && plan.beforeOps) {
        const before = this.scratch(20, width, height), bctx = before.getContext('2d', {willReadFrequently:true});
        bctx.fillStyle = '#000'; bctx.fillRect(0, 0, width, height);
        this.compose(bctx, plan.beforeOps, 1, width, height);
        const edge = Math.round(width*plan.split);
        ctx.drawImage(before, edge, 0, width-edge, height, edge, 0, width-edge, height);
      }
      if (plan.view && plan.view !== 'normal') applyView(ctx, width, height, plan.view);
      return this.canvas;
    }
    destroy() { this.layers = []; }
  }
  // Monitoring views on the CPU (the GPU pass shader has the same rules).
  function applyView(ctx, width, height, view) {
    const image = ctx.getImageData(0, 0, width, height), d = image.data;
    for (let i = 0, p = 0; i < d.length; i += 4, p++) {
      const y = .2126*d[i]+.7152*d[i+1]+.0722*d[i+2], x = p % width, row = (p/width) | 0;
      let r = d[i], g = d[i+1], b = d[i+2];
      if (view === 'luma') r = g = b = y;
      else if (view === 'red') g = b = r;
      else if (view === 'green') r = b = g;
      else if (view === 'blue') r = g = b;
      else if (view === 'falseColor') [r, g, b] = y < 16 ? [89, 0, 153] : y < 40 ? [0, 64, 230] : y > 250 ? [255, 0, 0] : y > 235 ? [255, 217, 0] : Math.abs(y-118) < 6 ? [26, 191, 51] : Math.abs(y-170) < 6 ? [255, 140, 179] : [y, y, y];
      else if (view === 'legal' && (y > 235 || y < 16) && ((Math.floor(x/4)+Math.floor((height-1-row)/4)) % 2) < 1) [r, g, b] = [255, 0, 255];
      else if (view === 'zebra' && Math.max(r, g, b) >= 250 && Math.floor((x+height-1-row)/6) % 2 < 1) r = g = b = 0;
      d[i] = r; d[i+1] = g; d[i+2] = b;
    }
    ctx.putImageData(image, 0, 0);
  }

  // ---------------------------------------------------------------- choosing a renderer
  let gl = null, canvas2d = null, glError = '';
  function renderer() {
    if (!forceCanvas && !glError) {
      if (!gl) { try { gl = new GLRenderer(); } catch (error) { glError = error.message; gl = null; } }
      if (gl && !gl.lost && !gl.failed) return gl;
    }
    return canvas2d || (canvas2d = new CanvasRenderer());
  }
  // Draw `plan` into the visible 2D canvas `target`.
  function render(target, plan) {
    const started = performance.now();
    stats.exactDecode = true; stats.stale = 0;
    let chosen = renderer(), picture;
    try { picture = chosen.render(plan); }
    catch (error) {
      if (chosen !== gl) throw error;
      console.error('Splice monitor: WebGL failed, using the Canvas renderer', error);
      gl.failed = true; glError = error.message;
      chosen = renderer(); picture = chosen.render(plan);
    }
    const ctx = target.getContext('2d');
    ctx.save(); ctx.globalAlpha = 1; ctx.imageSmoothingEnabled = false; ctx.globalCompositeOperation = 'copy';
    ctx.drawImage(picture, 0, 0, target.width, target.height);
    ctx.restore();
    stats.renderer = chosen === gl ? 'webgl2' : 'canvas';
    stats.frameMs = performance.now()-started; stats.frames++;
  }
  // Grade timing alone (for the performance report): a synthetic frame through the GL grade.
  function setCanvasOnly(value) { forceCanvas = !!value; }
  function loseContext() { const extension = gl?.gl.getExtension('WEBGL_lose_context'); extension?.loseContext(); return extension; }
  function reset() { clearTimeout(retry); retry = 0; gl?.destroy(); gl = null; glError = ''; canvas2d?.destroy(); canvas2d = null; }
  window.SplicePicture = {render, stats, forget, setCanvasOnly, loseContext, reset, maxFrame:MAX_FRAME, views:Object.keys(VIEW),
    onReady:callback => { onReady = callback; }, glError:() => glError, renderer:() => (renderer() === gl ? 'webgl2' : 'canvas'),
    readGL:(x, y, w, h) => gl?.readPixels(x, y, w, h), context:() => gl?.gl || null};
})();
