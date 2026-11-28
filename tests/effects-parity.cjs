/* Effects look the same in the monitor and in the export.
 *
 * One timeline holds every case, one second each (a speed ramp takes two): crops, masks of
 * each shape, a masked adjustment clip, chroma keys with spill, all ten blend modes, bezier
 * keyframes on opacity, scale, crop, masks and the grade, bypassed and disabled effects, and a
 * speed ramp whose source frames are read back by colour. Sources are flat colour patches
 * (and a source whose every frame has its own colour). At each sample point a 6x6 average is
 * read from the monitor (WebGL2 and Canvas 2D) and from the decoded ProRes 422 HQ, ProRes 4444
 * and H.264 exports. Points on hard mask or key edges (where a renderer may land a pixel either
 * side) are skipped: the JS maths says where they are. A Markdown table is written to
 * SPLICE_PROOF_DIR when set.
 */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {root, startSplice, openPage, ffmpeg} = require('./browser-harness.cjs');

const W = 640, H = 360, FPS = 25;
const PATCHES = [[230,40,30],[40,200,60],[40,60,220],[235,235,235],[200,140,110],[64,128,192],[20,20,20],[128,128,128]];
const POINTS = [];
for (let j = 0; j < 4; j++) for (let i = 0; i < 8; i++) POINTS.push([Math.floor((i+.5)*W/8), Math.floor((j+.5)*H/4)]);
const TOLERANCE = {prores:2, h264:4};

function source(file, paint, frames = 100) {
  const rgb = Buffer.alloc(W*H*3*frames);
  for (let f = 0; f < frames; f++) for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) rgb.set(paint(x, y, f), ((f*H+y)*W+x)*3);
  const raw = file+'.rgb';
  fs.writeFileSync(raw, rgb);
  ffmpeg(['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', `${W}x${H}`, '-r', String(FPS), '-i', raw,
    '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', String(frames/FPS),
    '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv',
    '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p', '-g', '1', '-c:a', 'aac', '-shortest', file]);
  fs.rmSync(raw);
}
const patch = order => (x, y) => PATCHES[order[Math.floor(y*2/H)*4+Math.floor(x*4/W)]];
const GREEN = [32, 192, 64];
const greenScreen = (x, y) => (x >= 240 && x < 400 && y >= 120 && y < 240) ? [192, 64, 96] :
  (x >= 70 && x < 170 && y >= 100 && y < 170) ? [110, 190, 120] : GREEN;
// Frame f has its own colour: the ramp case reads which source frame each output frame shows.
const frameColor = f => [20+2*f, 128, 235-2*f];

async function main() {
  const splice = await startSplice('splice-effects-parity-');
  const proof = process.env.SPLICE_PROOF_DIR;
  if (proof) fs.mkdirSync(proof, {recursive:true});
  try {
    const files = {bg:patch([0,1,2,3,4,5,6,7]), rev:patch([7,6,5,4,3,2,1,0]), green:greenScreen, frames:(x, y, f) => frameColor(f)};
    const paths = {};
    for (const [name, paint] of Object.entries(files)) { paths[name] = path.join(splice.temp, name+'.mp4'); source(paths[name], paint); }
    const {page, errors} = await openPage(splice, {width:1440, height:900});
    page.setDefaultTimeout(60000);
    await page.evaluate(({W, H, FPS}) => SpliceSession.newProject({name:'Effects parity', width:W, height:H, fps:FPS}), {W, H, FPS});
    await page.locator('#media-input').setInputFiles(Object.values(paths));
    await page.waitForFunction(count => Editor.get().project.media.length === count && Editor.get().project.media.every(item => item.src), Object.keys(paths).length);
    await page.evaluate(() => SpliceMonitor.setQuality('full'));

    // ---- Cases: clips (V1 is the patch background unless `alone`), sample times within the case.
    const cases = [];
    const over = (name, top, extra = {}) => cases.push({name, clips:[{track:1, source:'bg'}, {track:2, source:'rev', ...top}], times:[.48], ...extra});
    over('crop left/right/top/bottom', {effects:{crop:{left:20, right:10, top:15, bottom:25}}});
    over('crop feathered', {effects:{crop:{left:15, right:15, top:10, bottom:10, feather:12}}});
    over('crop animated (easeInOut)', {effects:{crop:{left:0, right:0, top:0, bottom:0}},
      keyframes:{'crop.left':[{time:0, value:0, interp:'easeInOut'}, {time:1, value:60}]}}, {times:[.3, .7]});
    over('rectangle mask, rotated, feathered', {effects:{masks:[{shape:'rectangle', x:50, y:50, width:50, height:60, rotation:25, feather:6}]}});
    over('ellipse mask, inverted, 70% opacity', {effects:{masks:[{shape:'ellipse', x:45, y:55, width:50, height:70, invert:true, opacity:70}]}});
    over('polygon mask with expansion', {effects:{masks:[{shape:'polygon', x:50, y:50, width:100, height:100, expansion:4,
      points:[[-30, -35], [35, -20], [20, 35], [-25, 30]]}]}});
    over('two masks (union)', {effects:{masks:[{shape:'ellipse', x:30, y:40, width:30, height:40}, {shape:'rectangle', x:72, y:62, width:30, height:40, feather:4}]}});
    over('mask animated (bezier handles)', {effects:{masks:[{shape:'ellipse', x:30, y:50, width:30, height:50}]},
      keyframes:{'mask.0.x':[{time:0, value:30, interp:'bezier', outHandle:[.1, .9]}, {time:1, value:70, inHandle:[.6, 1]}]}}, {times:[.25, .6]});
    cases.push({name:'masked adjustment clip (power window)', times:[.48], clips:[{track:1, source:'bg'},
      {track:2, adjustment:true, color:{saturation:0, exposure:.5}, effects:{masks:[{shape:'ellipse', x:50, y:50, width:55, height:70, feather:5}]}}]});
    cases.push({name:'adjustment clip cropped', times:[.48], clips:[{track:1, source:'bg'},
      {track:2, adjustment:true, color:{grade:{gain:[1.3, 1, .7, 1]}}, effects:{crop:{left:30, right:20, top:0, bottom:40}}}]});
    const key = (name, settings) => cases.push({name, times:[.48], clips:[{track:1, source:'bg'}, {track:2, source:'green', effects:{key:{color:'#20c040', ...settings}}}]});
    key('chroma key', {similarity:12, smoothness:8, spill:0});
    key('chroma key with spill suppression', {similarity:6, smoothness:30, spill:85});
    key('chroma key, choke and feather', {similarity:10, smoothness:25, spill:40, choke:30, feather:60});
    key('chroma key inverted', {similarity:12, smoothness:8, invert:true});
    cases.push({name:'chroma key graded and masked', times:[.48], clips:[{track:1, source:'bg'}, {track:2, source:'green', color:{saturation:150, exposure:.3},
      effects:{key:{color:'#20c040', similarity:12, smoothness:10, spill:60}, masks:[{shape:'rectangle', x:55, y:50, width:70, height:80}]}}]});
    for (const mode of ['multiply', 'screen', 'overlay', 'add', 'darken', 'lighten', 'softLight', 'hardLight', 'difference'])
      over(`blend ${mode}`, {effects:{blend:mode}});
    // An opaque full-frame layer without live effects covers the frame: the monitor skips what is beneath.
    over('blend normal (explicit)', {effects:{blend:'normal'}}, {layers:1});
    over('blend multiply at 60% with a mask', {opacity:60, effects:{blend:'multiply', masks:[{shape:'ellipse', x:50, y:50, width:70, height:80, feather:8}]}});
    over('blend screen over a grade', {color:{saturation:140, grade:{gain:[1.1, 1, .9, 1]}}, effects:{blend:'screen'}});
    over('blend overlay under an adjustment clip', {effects:{blend:'overlay'}}, {});
    cases[cases.length-1].clips.push({track:3, adjustment:true, color:{contrast:130}});
    over('opacity easeInOut', {keyframes:{opacity:[{time:0, value:0, interp:'easeInOut'}, {time:1, value:100}]}}, {times:[.25, .5]});
    // A flat layer: inside a scaled patch grid FFmpeg's bicubic and the monitor's bilinear differ at patch edges.
    over('scale and position, bezier', {source:'frames', keyframes:{scale:[{time:0, value:50, interp:'bezier', outHandle:[.3, 0]}, {time:1, value:100, inHandle:[.7, 1]}],
      x:[{time:0, value:-20, interp:'easeOut'}, {time:1, value:20}]}}, {times:[.4]});
    over('grade keyframes easeIn', {color:{}, keyframes:{exposure:[{time:0, value:-1, interp:'easeIn'}, {time:1, value:1}]}}, {times:[.3, .7], layers:1});
    over('hold keyframes', {keyframes:{opacity:[{time:0, value:30, interp:'hold'}, {time:.5, value:90}]}}, {times:[.3, .7]});
    over('bypassed effects', {effects:{bypass:true, crop:{left:40, right:40, top:0, bottom:0}, blend:'difference'}}, {layers:1});
    over('disabled clip with effects', {enabled:false, effects:{masks:[{shape:'ellipse', x:50, y:50, width:50, height:50}], blend:'screen'}});
    cases.push({name:'speed ramp (ease, hold, bezier, freeze)', length:2, alone:true, times:[.12, .52, .92, 1.32, 1.72, 1.96],
      clips:[{track:1, source:'frames', in:.4, effects:{speedAudio:'varispeed'}, keyframes:{speed:[{time:0, value:100, interp:'easeInOut'}, {time:.4, value:30, interp:'hold'},
        {time:.8, value:30, interp:'bezier', outHandle:[.2, .8]}, {time:1.2, value:250, inHandle:[.5, 0]}, {time:1.5, value:0, interp:'hold'}, {time:1.7, value:100}]}}]});
    if (process.env.SPLICE_PARITY_ONLY) { const keep = process.env.SPLICE_PARITY_ONLY.split('|'); cases.splice(0, cases.length, ...cases.filter(item => keep.includes(item.name))); }

    // ---- Build the timeline.
    const timeline = [];
    let at = 0;
    for (const item of cases) {
      item.start = at;
      const length = item.length || 1;
      for (const clip of item.clips) timeline.push({...clip, start:at, duration:length, in:clip.in ?? .2});
      at += length;
    }
    await page.evaluate(({timeline}) => {
      Editor.editProject(project => {
        project.clips = timeline.map((clip, index) => {
          let mediaId = clip.source && project.media.find(item => item.name === clip.source+'.mp4').id;
          if (clip.adjustment) {
            mediaId = 'adjust-'+index;
            project.media.push({id:mediaId, type:'video', name:'Adjustment clip', src:'', duration:48*3600, nativeGenerator:1414680150, adjustment:true});
          }
          const out = {id:'clip-'+index, type:'video', mediaId, track:clip.track, start:clip.start, duration:clip.duration, in:clip.in,
            embeddedAudio:false, volume:100, transition:'none', color:clip.color || {}};
          for (const key of ['opacity', 'keyframes', 'effects', 'enabled']) if (clip[key] != null) out[key] = clip[key];
          return out;
        });
      });
    }, {timeline});
    // Ramped clip: its duration follows from the curve (the source plays to the out point).
    const sampleTimes = cases.flatMap(item => item.times.map(t => ({item, time:item.start+t, frame:Math.round((item.start+t)*FPS)})));
    // Points on hard edges of a mask, crop or key are not compared (a renderer may land a pixel either side).
    const usable = await page.evaluate(({samples, POINTS, W, H, GREEN}) => {
      const E = SpliceEffects, out = [];
      for (const sample of samples) {
        const clips = Editor.get().project.clips.filter(c => c.start <= sample.time && sample.time < c.start+c.duration && (c.effects || c.keyframes));
        out.push(POINTS.map(([x, y]) => clips.every(clip => {
          const local = sample.time-clip.start;
          // Points within 8 px of a scaled or moved layer's edge are not compared either.
          const k = clip.keyframes || {}, value = (key, base) => k[key]?.length ? E.curveValue(k[key], local, base) : base;
          const scale = value('scale', clip.transform?.scale ?? 100)/100, cx = W/2+value('x', 0)*W/100, cy = H/2+value('y', 0)*H/100;
          if (scale !== 1 || cx !== W/2 || cy !== H/2) {
            const left = cx-W*scale/2, right = cx+W*scale/2, top = cy-H*scale/2, bottom = cy+H*scale/2;
            if ([left, right].some(edge => Math.abs(x-edge) < 8) && y > top-8 && y < bottom+8) return false;
            if ([top, bottom].some(edge => Math.abs(y-edge) < 8) && x > left-8 && x < right+8) return false;
          }
          const fx = E.layerFx(clip, local, 'bt709');
          if (!fx) return true;
          const alphaAt = (px, py) => {
            let a = 1;
            if (fx.crop) a *= E.cropAlpha(fx.crop, (px+.5)/W, (py+.5)/H, W/H);
            if (fx.masks.length) a *= E.masksAlpha(fx.masks, (px+.5)/W, (py+.5)/H, W/H);
            return a;
          };
          const centre = alphaAt(x, y);
          for (const dx of [-5, 0, 5]) for (const dy of [-5, 0, 5]) if (Math.abs(alphaAt(x+dx, y+dy)-centre) > .12) return false;
          return true;
        })));
      }
      return out;
    }, {samples:sampleTimes.map(s => ({time:s.frame/FPS+1e-4})), POINTS, W, H, GREEN});

    async function readMonitor() {
      const out = [];
      for (const sample of sampleTimes) {
        const previous = await page.evaluate(time => { const before = SplicePicture.stats.frames; Editor.seek(time); return before; }, sample.frame/FPS+1e-4);
        const layers = sample.item.layers ?? sample.item.clips.filter(clip => !clip.adjustment && clip.enabled !== false).length;
        await page.waitForFunction(({before, layers}) => {
          const canvas = document.querySelector('#preview-canvas');
          const videos = Editor.framePlan(canvas).ops.flatMap(op => [op, op.outgoing, op.incoming]).filter(op => op?.element instanceof HTMLVideoElement);
          if (videos.length < layers || videos.some(op => op.element.readyState < 2 || op.element.seeking)) return false;
          const stats = SplicePicture.stats;
          return stats.frames > before && stats.exactDecode && stats.stale === 0 && canvas.width === 640;
        }, {before:previous, layers}).catch(error => { throw new Error(`${sample.item.name} @${sample.time}: ${error.message} ${errors.join(' | ')}`); });
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await page.waitForFunction(() => SplicePicture.stats.exactDecode && SplicePicture.stats.stale === 0);
        out.push(await page.evaluate(POINTS => {
          const ctx = document.querySelector('#preview-canvas').getContext('2d');
          return POINTS.map(([x, y]) => {
            const d = ctx.getImageData(x-3, y-3, 6, 6).data, sum = [0, 0, 0];
            for (let i = 0; i < d.length; i += 4) for (let c = 0; c < 3; c++) sum[c] += d[i+c];
            return sum.map(v => v/36);
          });
        }, POINTS));
      }
      return out;
    }
    const gl = await readMonitor();
    assert.equal(await page.evaluate(() => SplicePicture.stats.renderer), 'webgl2', 'the monitor drew with WebGL2');
    const glMs = await page.evaluate(() => SplicePicture.stats.frameMs);
    await page.evaluate(() => SplicePicture.setCanvasOnly(true));
    const canvas = await readMonitor();
    assert.equal(await page.evaluate(() => SplicePicture.stats.renderer), 'canvas');
    await page.evaluate(() => SplicePicture.setCanvasOnly(false));

    // ---- Export with the real renderer.
    const project = await page.evaluate(async () => {
      const prepared = await SpliceMedia.forPremiere(Editor.get().project);
      delete prepared.premiereOriginal; delete prepared.premiereManaged;
      prepared.renderOptions = {mutedTracks:{}};
      return prepared;
    });
    const render = (name, options) => {
      const projectFile = path.join(splice.temp, name+'.json'), movie = path.join(splice.temp, name+(options.codec === 'prores' ? '.mov' : '.mp4'));
      fs.writeFileSync(projectFile, JSON.stringify(project));
      const started = Date.now();
      execFileSync('python3', ['-c', `import json,sys; from pathlib import Path; sys.path.insert(0, ${JSON.stringify(root)}); from render_movie import render_to_file; render_to_file(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2], options=json.loads(sys.argv[3]))`,
        projectFile, movie, JSON.stringify({crf:14, limiter:false, ...options})], {cwd:root, env:{...process.env, TMPDIR:splice.temp, HOME:splice.temp, XDG_DATA_HOME:splice.temp}, stdio:['ignore', 'inherit', 'inherit'], timeout:900000});
      console.log(`${name}: rendered ${at.toFixed(0)} s of timeline in ${((Date.now()-started)/1000).toFixed(1)} s`);
      return movie;
    };
    const decode = movie => {
      const select = sampleTimes.map(sample => `eq(n\\,${sample.frame})`).join('+');
      const raw = execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-i', movie, '-vf', `select='${select}',setparams=range=tv,zscale,format=gbrp`, '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], {maxBuffer:sampleTimes.length*W*H*3+65536, timeout:180000});
      assert.equal(raw.length, sampleTimes.length*W*H*3, `${movie}: every sample frame decoded`);
      return sampleTimes.map((_, index) => POINTS.map(([x, y]) => {
        const sum = [0, 0, 0];
        for (let yy = y-3; yy < y+3; yy++) for (let xx = x-3; xx < x+3; xx++) for (let c = 0; c < 3; c++) sum[c] += raw[index*W*H*3+(yy*W+xx)*3+c];
        return sum.map(v => v/36);
      }));
    };
    const exports = {prores:decode(render('effects-prores', {codec:'prores'})), prores4444:decode(render('effects-4444', {codec:'prores', profile:'4444', alpha:true})),
      h264:decode(render('effects-h264', {codec:'h264'}))};

    // ---- Compare.
    const worst = (a, b, keep) => Math.max(0, ...a.flatMap((point, i) => keep[i] ? point.map((v, c) => Math.abs(v-b[i][c])) : []));
    const rows = [], failures = [];
    sampleTimes.forEach((sample, index) => {
      const keep = usable[index];
      const row = {case:sample.item.name+(sample.item.times.length > 1 ? ` @${(sample.time-sample.item.start).toFixed(2)}s` : ''),
        points:keep.filter(Boolean).length,
        prores:worst(gl[index], exports.prores[index], keep), prores4444:worst(gl[index], exports.prores4444[index], keep),
        h264:worst(gl[index], exports.h264[index], keep), canvas:worst(canvas[index], exports.prores[index], keep),
        canvasGl:worst(canvas[index], gl[index], keep)};
      rows.push(row);
      for (const key of ['prores', 'prores4444', 'canvas']) if (row[key] > TOLERANCE.prores) failures.push(`${row.case}: ${key === 'canvas' ? 'Canvas' : 'WebGL2'} vs ${key === 'prores4444' ? 'ProRes 4444' : 'ProRes 422 HQ'} ${row[key].toFixed(2)} > ${TOLERANCE.prores}`);
      if (row.h264 > TOLERANCE.h264) failures.push(`${row.case}: WebGL2 vs H.264 ${row.h264.toFixed(2)} > ${TOLERANCE.h264}`);
      if (row.points < 8) failures.push(`${row.case}: only ${row.points} comparable points`);
    });
    // Effects must change the picture (no tautology): compare with the plain background patches.
    const background = POINTS.map(([x, y]) => PATCHES[Math.floor(y*2/H)*4+Math.floor(x*4/W)]);
    const moved = sampleTimes.map((sample, index) => worst(exports.prores[index], background, usable[index]));
    const identical = name => moved[sampleTimes.findIndex(s => s.item.name === name)];
    if (!process.env.SPLICE_PARITY_ONLY) assert.ok(identical('disabled clip with effects') <= 2, 'a disabled clip shows nothing: only the background');
    const changed = moved.filter(value => value > 8).length;
    // The ramp: each output frame shows the source frame the curve says (colour encodes the frame).
    const ramp = cases.find(item => item.name.startsWith('speed ramp'));
    if (ramp) {
      const frames = await page.evaluate(({times, start, id}) => {
        const clip = Editor.get().project.clips.find(c => c.start === start);
        return times.map(t => Math.floor((clip.in+SpliceEffects.rampSource(SpliceEffects.rampPoints(clip), Math.round((start+t)*25)/25-start)+1e-6)*25));
      }, {times:ramp.times, start:ramp.start});
      ramp.times.forEach((t, k) => {
        const index = sampleTimes.findIndex(s => s.item === ramp && Math.abs(s.time-ramp.start-t) < 1e-9);
        for (const [name, pictures] of Object.entries({webgl:gl, canvas, prores:exports.prores, h264:exports.h264})) {
          const rgb = pictures[index][9];
          let shown = 0;
          for (let f = 1; f < 100; f++) if (frameColor(f).reduce((d, v, c) => d+(v-rgb[c])**2, 0) < frameColor(shown).reduce((d, v, c) => d+(v-rgb[c])**2, 0)) shown = f;
          if (shown !== frames[k]) failures.push(`speed ramp @${t}: ${name} shows source frame ${shown}, the curve gives ${frames[k]}`);
        }
      });
    }
    if (process.env.SPLICE_PARITY_KEEP) fs.cpSync(splice.temp, process.env.SPLICE_PARITY_KEEP, {recursive:true});
    const table = ['| case | points | WebGL2 vs ProRes 422 HQ | WebGL2 vs ProRes 4444 | WebGL2 vs H.264 | Canvas vs ProRes 422 HQ | Canvas vs WebGL2 |', '|---|---|---|---|---|---|---|',
      ...rows.map(row => `| ${row.case} | ${row.points} | ${row.prores.toFixed(2)} | ${row.prores4444.toFixed(2)} | ${row.h264.toFixed(2)} | ${row.canvas.toFixed(2)} | ${row.canvasGl.toFixed(2)} |`)].join('\n');
    if (proof) fs.writeFileSync(path.join(proof, 'parity-table.md'), `Max difference in 8-bit RGB codes over the comparable 6x6 sample points (limit ProRes ${TOLERANCE.prores}, H.264 ${TOLERANCE.h264}).\n\n${table}\n`);
    console.log(table);
    console.log('WebGL2 frame time at 640x360 (last sample):', glMs.toFixed(1), 'ms');
    if (process.env.SPLICE_PARITY_DEBUG) for (const name of process.env.SPLICE_PARITY_DEBUG.split('|')) {
      const index = sampleTimes.findIndex(sample => sample.item.name === name);
      console.log(name, JSON.stringify({keep:usable[index], gl:gl[index].map(p => p.map(Math.round)), canvas:canvas[index].map(p => p.map(Math.round)), prores:exports.prores[index].map(p => p.map(Math.round))}));
    }
    if (!process.env.SPLICE_PARITY_ONLY) assert.ok(changed >= rows.length*.7, `the cases change the picture (${changed} of ${rows.length})`);
    assert.deepEqual(failures, [], failures.join('\n'));
    assert.deepEqual(errors, []);
    console.log(`Effects match the export: ${rows.length} samples, WebGL2 and Canvas against ProRes 422 HQ, ProRes 4444 and H.264.`);
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exit(1); });
