/* What you see is what you export: the program monitor's pixels against decoded exports.
 *
 * One timeline holds every case, one second each: every primary control at several values,
 * wheels, curves, hue vs saturation, 33- and 65-point LUTs at full and partial intensity, short
 * and 4,096-entry 1D LUTs, an input LUT, a keyframed exposure ramp sampled at five points, an
 * adjustment clip over two tracks, each transition mid-way, opacity stacks, and untagged, 709,
 * 601, full-range, HLG and PQ sources. Sources are flat colour patches; each patch centre is
 * averaged over 6x6 pixels in the monitor (at Full quality, 1:1 with the sequence) and in the
 * export decoded by FFmpeg as a player would (its colour tags honoured).
 *
 * The monitor is read three ways: WebGL2, the Canvas 2D fallback, and WebGL2 after a forced
 * context loss and restore. Exports: ProRes 422 HQ (10-bit) and H.264 at CRF 14. Tolerances
 * (8-bit code values, max over patches and channels) are stated per case in TOLERANCE below.
 * A Markdown table of the measured differences is written to SPLICE_PROOF_DIR when it is set.
 */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {root, startSplice, openPage, ffmpeg} = require('./browser-harness.cjs');

const W = 640, H = 360, FPS = 25;
const PATCHES = [[230,40,30],[40,200,60],[40,60,220],[235,235,235],[200,140,110],[64,128,192],[20,20,20],[128,128,128]];
const centers = PATCHES.map((_, i) => [Math.floor(((i%4)+.5)*W/4), Math.floor((Math.floor(i/4)+.5)*H/2)]);
// Tolerances, 8-bit codes. SDR through ProRes: the codec and 4:2:2/4:2:0 chroma cost about one code.
// H.264 at CRF 14 adds up to two. HDR sources are previewed from the server's tone-mapped 8-bit
// proxy (H.264 CRF 19), which adds its own rounding. Transitions blend in Y'CbCr in the export.
const TOLERANCE = {prores:2, h264:4, hdr:5, hdrH264:6};

function patchSource(file, {matrix = 'bt709', range = 'tv', tags = true, transfer = null, color = null, yuv = null} = {}) {
  const rgb = Buffer.alloc(W*H*3);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const c = color || PATCHES[Math.floor(y*2/H)*4+Math.floor(x*4/W)];
    rgb.set(c, (y*W+x)*3);
  }
  const raw = file+'.rgb';
  fs.writeFileSync(raw, rgb);
  let graph, codec = ['-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p'];
  if (transfer) {
    // HDR: the patches as Rec. 709 SDR graphics at 203 nits (BT.2408), in BT.2020 with HLG or PQ.
    const t = transfer === 'hlg' ? 'arib-std-b67' : 'smpte2084';
    graph = `format=gbrpf32le,setparams=color_primaries=bt709:color_trc=bt709:colorspace=gbr:range=pc,zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt2020:t=${t}:m=2020_ncl:r=limited:npl=203,format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=${t}:colorspace=bt2020nc:range=tv`;
    codec = ['-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p10le'];
  } else {
    graph = `scale=out_color_matrix=${matrix}:out_range=${range},format=yuv420p,` + (tags ?
      `setparams=colorspace=${matrix === 'bt601' ? 'smpte170m' : 'bt709'}:color_primaries=${matrix === 'bt601' ? 'smpte170m' : 'bt709'}:color_trc=${matrix === 'bt601' ? 'smpte170m' : 'bt709'}:range=${range}` :
      'setparams=colorspace=unknown:color_primaries=unknown:color_trc=unknown:range=tv');
  }
  if (yuv) graph += `,lutyuv=y=${yuv[0]}:u=${yuv[1]}:v=${yuv[2]}`;
  ffmpeg(['-y', '-stream_loop', '-1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', `${W}x${H}`, '-r', String(FPS), '-i', raw,
    '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '4', '-vf', graph, ...codec, '-g', '1', '-c:a', 'aac', '-shortest', file]);
  fs.rmSync(raw);
}
function cube3D(size, fn, header = '') {
  const rows = [`LUT_3D_SIZE ${size}`+header];
  for (let b = 0; b < size; b++) for (let g = 0; g < size; g++) for (let r = 0; r < size; r++)
    rows.push(fn(r/(size-1), g/(size-1), b/(size-1)).map(v => v.toFixed(6)).join(' '));
  return rows.join('\n')+'\n';
}
function cube1D(size, fn, header = '') {
  const rows = [`LUT_1D_SIZE ${size}`+header];
  for (let i = 0; i < size; i++) rows.push(fn(i/(size-1)).map(v => v.toFixed(6)).join(' '));
  return rows.join('\n')+'\n';
}
const warm = (r, g, b) => [Math.min(1, r**.8*1.05+.1*g), g**1.2*.9+.05*b, Math.max(0, b*.8+.15*r*g)];
const film = t => [t**.9, Math.min(1, .03+t**1.1), .05+.85*t**1.3];
const logToVideo = t => [Math.min(1, Math.max(0, (t-.1)*1.35)), Math.min(1, Math.max(0, (t-.1)*1.35)), Math.min(1, Math.max(0, (t-.1)*1.35))];

async function main() {
  const splice = await startSplice('splice-parity-');
  const proof = process.env.SPLICE_PROOF_DIR;
  if (proof) fs.mkdirSync(proof, {recursive:true});
  const files = {};
  try {
    const sources = {tag709:{}, untagged:{matrix:'bt601', tags:false}, override709:{tags:false}, tag601:{matrix:'bt601'}, full709:{range:'pc'},
      hlg:{transfer:'hlg'}, pq:{transfer:'pq'}, flat:{color:[100,180,220]}, headroom:{color:[128,128,128]}, wideRgb:{yuv:[235,16,240]}};
    for (const [name, spec] of Object.entries(sources)) { files[name] = path.join(splice.temp, name+'.mp4'); patchSource(files[name], spec); }
    const {page, errors} = await openPage(splice, {width:1440, height:900});
    page.setDefaultTimeout(60000);
    await page.evaluate(({W, H, FPS}) => SpliceSession.newProject({name:'Parity', width:W, height:H, fps:FPS}), {W, H, FPS});
    await page.locator('#media-input').setInputFiles(Object.values(files));
    await page.waitForFunction(count => Editor.get().project.media.length === count && Editor.get().project.media.every(item => item.src), Object.keys(files).length);
    await page.evaluate(() => SpliceMonitor.setQuality('full'));
    const extendedDomain = '\nDOMAIN_MIN -1 0.25 0\nDOMAIN_MAX 1 0.75 2';
    const luts = await page.evaluate(texts => Object.fromEntries(Object.entries(texts).map(([name, text]) => [name, SpliceLUT.parseCube(text, name)])), {
      lut33:cube3D(33, warm), lut65:cube3D(65, warm), short1D:cube1D(17, film), long1D:cube1D(4096, film),
      extended1D:cube1D(2, t => [t,t,t], extendedDomain), extended3D:cube3D(2, (r,g,b) => [r,g,b], extendedDomain),
      domain1D:cube1D(1024, film, '\nDOMAIN_MIN 0 0.05 0\nDOMAIN_MAX 1 0.95 0.9'), input:cube1D(256, logToVideo)});
    // ---- The cases. Each: clips (relative to its second), sample times (relative), tolerance kind.
    const cases = [];
    const one = (name, color, source = 'tag709', extra = {}) => cases.push({name, source, clips:[{track:1, color, ...extra}], times:[.48]});
    one('identity', {});
    one('decoded headroom before desaturation', {saturation:0}, 'wideRgb');
    const fastRamp = {exposure:[{time:0,value:-1}, {time:.04,value:1}]};
    for (const mode of ['forward', 'reverse', 'adjustment']) {
      const graded = {track:1, start:.02, color:{}, keyframes:fastRamp, reverse:mode === 'reverse'};
      const clips = mode === 'adjustment' ? [{track:1, color:{}}, {...graded, track:2, adjustment:true}] : [graded];
      cases.push({name:'fractional start '+mode, source:'tag709', clips, times:[.04,.08]});
    }
    for (const v of [50, 150]) one(`brightness ${v}`, {brightness:v});
    for (const v of [50, 160]) one(`contrast ${v}`, {contrast:v});
    one('contrast 160 pivot 0.3', {contrast:160, grade:{pivot:.3}});
    for (const v of [0, 60, 200]) one(`saturation ${v}`, {saturation:v});
    for (const v of [-1, .7]) one(`exposure ${v}`, {exposure:v});
    for (const v of [-60, 60]) one(`temperature ${v}`, {temperature:v});
    for (const v of [-60, 60]) one(`tint ${v}`, {tint:v});
    one('lift', {grade:{lift:[.06, -.02, -.04, .03]}});
    one('gamma', {grade:{gamma:[-.2, .1, .3, .15]}});
    one('gain', {grade:{gain:[1.15, .95, .9, 1.05]}});
    one('offset', {grade:{offset:[-.03, .02, .05, -.02]}});
    one('wheels together', {exposure:.3, saturation:120, grade:{lift:[.02, 0, -.02, 0], gamma:[0, .05, -.05, .1], gain:[1.1, 1, .92, 1], offset:[0, 0, .02, 0]}});
    one('master curve', {grade:{curves:{master:[[0, 0], [.25, .18], [.75, .84], [1, 1]]}}});
    one('RGB curves', {grade:{curves:{red:[[0, .03], [.5, .56], [1, .97]], green:[[0, 0], [.6, .55], [1, 1]], blue:[[0, .06], [1, .9]]}}});
    one('hue vs saturation', {grade:{hueSat:[[0, 1.4], [.33, .6], [.66, 1.2]]}});
    cases.push(
      {name:'first frame with hue curve and creative 3D LUT', source:'tag709', coldLuts:true, times:[.48],
        clips:[{track:1, color:{grade:{hueSat:[[0, 1.4], [.33, .6], [.66, 1.2]]}, lut:luts.lut33}}]},
      {name:'first frame with input and creative 1D LUTs', source:'flat', coldLuts:true, times:[.48],
        clips:[{track:1, color:{lut:luts.short1D}}]});
    for (const [lut, intensity] of [['lut33', 100], ['lut33', 40], ['lut65', 100], ['lut65', 40], ['short1D', 100], ['long1D', 100], ['long1D', 55], ['domain1D', 100], ['domain1D', 60]])
      one(`${lut} at ${intensity}%`, {lut:luts[lut], lutIntensity:intensity});
    one('LUT after wheels and curves', {grade:{gain:[1.05, 1, .95, 1], curves:{master:[[0, 0], [.5, .46], [1, 1]]}}, lut:luts.lut65, lutIntensity:80});
    one('bypassed clip grade', {exposure:1, grade:{bypass:true, gain:[2, 1, 1, 1]}});
    for (const source of ['untagged', 'override709', 'tag601', 'full709']) {
      one(`${source} source`, {}, source);
      one(`${source} source graded`, {saturation:140, grade:{gamma:[0, 0, .1, .1]}}, source);
    }
    for (const source of ['hlg', 'pq']) {
      one(`${source} source`, {}, source, {kind:'hdr'});
      one(`${source} source with wheels`, {grade:{gain:[1.1, 1, .9, 1]}}, source, {kind:'hdr'});
      one(`${source} source bypassed`, {exposure:1, contrast:140, grade:{bypass:true, gain:[1.2, 1, .8, 1]}}, source, {kind:'hdr'});
      cases.push({name:`${source} source animated exposure`, source, length:1, clips:[{track:1, color:{exposure:.5},
        keyframes:{exposure:[{time:0, value:-1}, {time:.48, value:0}, {time:.96, value:1}]}}], times:[0, .48, .96]});
    }
    cases.push({name:'input LUT on the source', source:'flat', inputLut:true, clips:[{track:1, color:{saturation:120}}], times:[.48]});
    one('extended input LUT before desaturation', {saturation:0}, 'headroom');
    for (const [name, lut] of [['1D', luts.short1D], ['3D', luts.lut33]])
      one('extended input with partial '+name+' look', {lut, lutIntensity:50}, 'headroom');
    for (const lut of ['extended1D', 'extended3D']) for (const intensity of [75, 100])
      one(`${lut} domain at ${intensity}%`, {lut:luts[lut], lutIntensity:intensity}, 'headroom');
    for (const edit of ['uncut', 'split', 'trimmed']) {
      cases.push({name:edit+' grade ramp', edit, source:'tag709', length:2, times:[.48, 1, 1.48],
        clips:[{track:1, color:{}, keyframes:{exposure:[{time:0,value:-1}, {time:2,value:1}],
          saturation:[{time:0,value:60}, {time:1.64,value:115}]}}]});
    }
    // Keyframed exposure ramp over two seconds: -1 EV to +1 EV, sampled at five points.
    cases.push({name:'exposure ramp keyframes', source:'tag709', length:2, clips:[{track:1, color:{}, keyframes:{exposure:[{time:0, value:-1}, {time:1, value:.2}, {time:2, value:1}]}}],
      times:[0, .48, 1, 1.48, 1.96]});
    // Opacity stack: V2 at 60 % and V3 at 30 % over V1.
    cases.push({name:'opacity stack', source:'tag709', clips:[{track:1, color:{}}, {track:2, source:'flat', opacity:60, color:{}}, {track:3, source:'tag601', opacity:30, color:{contrast:130}}], times:[.48]});
    // Adjustment clip over two tracks, at 70 % opacity.
    cases.push({name:'adjustment clip over two tracks', source:'tag709', clips:[{track:1, color:{}}, {track:2, source:'flat', opacity:50, color:{}},
      {track:3, adjustment:true, opacity:70, color:{saturation:40, grade:{gain:[1.2, 1, .8, 1], curves:{master:[[0, .05], [1, .95]]}}}}], times:[.48]});
    cases.push({name:'stacked adjustment clips', source:'tag709', clips:[{track:1, color:{}}, {track:2, adjustment:true, color:{exposure:.5}}, {track:3, adjustment:true, opacity:50, color:{saturation:0}}], times:[.48]});
    // Transitions: second clip on V1 starts at .5 with a 0.6 s transition centred on the cut; sampled mid-way.
    for (const transition of ['dissolve', 'fade', 'wipe'])
      cases.push({name:`${transition} mid-way`, source:'tag709', transition, clips:[], times:[.48]});
    cases.push({name:'dissolve with trimmed grade ramp', source:'tag709', transition:'dissolve', clips:[],
      tailKeys:{exposure:[{time:-.24,value:-1}, {time:1.76,value:1}]}, times:[.60,.68]});
    cases.push({name:'fractional outgoing grade', source:'tag709', transition:'dissolve', clips:[], tailStart:.02,
      tailKeys:{exposure:[{time:.48,value:-1}, {time:.56,value:1}]}, times:[.52,.56]});
    if (process.env.SPLICE_PARITY_ONLY) { const keep = process.env.SPLICE_PARITY_ONLY.split(','); cases.splice(0, cases.length, ...cases.filter(item => keep.includes(item.name))); }
    // ---- Build the timeline.
    const timeline = [];
    let at = 0;
    for (const item of cases) {
      item.start = at;
      const length = item.length || 1;
      if (item.transition) {
        const lead = item.tailStart || 0;
        timeline.push({key:item.name+'a', mediaKey:'tag709', track:1, start:at+lead, duration:.5-lead, in:.5, color:{brightness:80}, keyframes:item.tailKeys});
        timeline.push({key:item.name+'b', mediaKey:'flat', track:1, start:at+.5, duration:.5, in:.5, color:{grade:{gain:[1.1, 1, .9, 1]}},
          transition:item.transition, transitionDuration:.6, transitionBefore:.3});
      } else for (const clip of item.clips) timeline.push({key:item.name+clip.track, mediaKey:clip.adjustment ? null : clip.source || item.source, adjustment:clip.adjustment,
        track:clip.track, start:at+(clip.start || 0), duration:length-(clip.start || 0), in:.2, reverse:clip.reverse, color:clip.color, opacity:clip.opacity, keyframes:clip.keyframes});
      at += length;
    }
    await page.evaluate(({timeline, inputLut}) => {
      Editor.editProject(project => {
        project.clips = timeline.map((clip, index) => {
          let mediaId = clip.mediaKey && project.media.find(item => item.name === clip.mediaKey+'.mp4').id;
          if (clip.adjustment) {
            mediaId = 'adjust-'+index;
            project.media.push({id:mediaId, type:'video', name:'Adjustment clip', src:'', duration:48*3600, nativeGenerator:1414680150, adjustment:true});
          }
          const out = {id:'clip-'+index, type:'video', mediaId, track:clip.track, start:clip.start, duration:clip.duration, in:clip.in,
            embeddedAudio:false, volume:100, transition:clip.transition || 'none', color:clip.color || {}};
          if (clip.opacity != null) out.opacity = clip.opacity;
          if (clip.reverse) out.reverse = true;
          if (clip.keyframes) out.keyframes = clip.keyframes;
          if (clip.transition) { out.transitionDuration = clip.transitionDuration; out.transitionBefore = clip.transitionBefore; }
          return out;
        });
        project.media.find(item => item.name === 'override709.mp4').colorMatrix = 'bt709';
        project.media.find(item => item.name === 'flat.mp4').inputLut = inputLut;
        project.media.find(item => item.name === 'headroom.mp4').inputLut =
          SpliceLUT.parseCube('LUT_3D_SIZE 2\n' + '-0.5 0.5 1.5\n'.repeat(8), 'Headroom');
      });
    }, {timeline, inputLut:luts.input});
    for (const {edit, start} of cases.filter(c => c.edit && c.edit !== 'uncut')) {
      await page.evaluate(({edit, start}) => {
        const id = Editor.get().project.clips.find(c => c.start === start).id;
        if (edit === 'split') {
          Editor.select(id);
          Editor.seek(start+1);
          Editor.split();
        } else {
          Editor.trimEdge(id, 'start', start+.24);
          Editor.trimEdge(id, 'end', start+1.76);
        }
      }, {edit, start});
    }
    // ---- Read the monitor at every sample point.
    const sampleTimes = cases.flatMap(item => item.times.map(t => ({item, time:item.start+t, frame:Math.round((item.start+t)*FPS)})));
    async function readMonitor() {
      const out = [], costs = [];
      for (const sample of sampleTimes) {
        const previous = await page.evaluate(time => {
          const before = SplicePicture.stats.frames;
          Editor.seek(time);
          return before;
        }, sample.frame/FPS+1e-4);
        // A source change can leave the old canvas visible until loadeddata. Its
        // stale count is also zero. Wait for a new paint and the active decoder.
        const layers = sample.item.transition ? 1 : sample.item.clips.filter(clip => !clip.adjustment).length;
        await page.waitForFunction(({before, layers}) => {
          const {project, activeClip, video} = Editor.get();
          const source = project.media.find(m => m.id === activeClip?.mediaId);
          const ready = !source?.src || (video.readyState >= 2 && !video.seeking && video.currentSrc === video.src);
          const canvas = document.querySelector('#preview-canvas');
          const videos = Editor.framePlan(canvas).ops.flatMap(op => [op, op.outgoing, op.incoming])
            .filter(op => op?.element instanceof HTMLVideoElement);
          if (videos.length < layers || videos.some(op => op.element.readyState < 2 || op.element.seeking)) return false;
          const stats = SplicePicture.stats;
          return ready && stats.frames > before && stats.exactDecode && stats.stale === 0 &&
            document.querySelector('#preview-canvas').width === 640;
        }, {before:previous, layers});
        // Sample after the workspace has presented the newly ready composition.
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await page.waitForFunction(() => SplicePicture.stats.exactDecode && SplicePicture.stats.stale === 0);
        costs.push(await page.evaluate(() => SplicePicture.stats.frameMs));
        out.push(await page.evaluate(({centers, coldLuts}) => {
          const canvas = document.querySelector('#preview-canvas');
          if (coldLuts && SplicePicture.stats.renderer === 'webgl2') {
            const plan = Editor.framePlan(canvas);
            SplicePicture.reset();
            SplicePicture.render(canvas, plan);
          }
          const ctx = canvas.getContext('2d');
          return centers.map(([x, y]) => {
            const d = ctx.getImageData(x-3, y-3, 6, 6).data, sum = [0, 0, 0];
            for (let i = 0; i < d.length; i += 4) for (let c = 0; c < 3; c++) sum[c] += d[i+c];
            return sum.map(v => v/36);
          });
        }, {centers, coldLuts:sample.item.coldLuts}));
      }
      costs.sort((a,b) => a-b);
      console.log(await page.evaluate(() => SplicePicture.stats.renderer), 'median monitor draw (ms at 640x360):', costs[Math.floor(costs.length/2)]);
      return out;
    }
    const gl = await readMonitor();
    assert.equal(await page.evaluate(() => SplicePicture.stats.renderer), 'webgl2', 'the monitor drew with WebGL2');
    assert.equal(await page.evaluate(() => SplicePicture.stats.exactDecode), true, 'the monitor read the exact decoded planes');
    await page.evaluate(() => SplicePicture.setCanvasOnly(true));
    const canvas = await readMonitor();
    assert.equal(await page.evaluate(() => SplicePicture.stats.renderer), 'canvas');
    await page.evaluate(() => SplicePicture.setCanvasOnly(false));
    // Context loss: the monitor keeps drawing (Canvas) while the context is lost and returns to WebGL2.
    const lost = await page.evaluate(async () => {
      const extension = SplicePicture.loseContext();
      await new Promise(resolve => setTimeout(resolve, 50));
      Editor.seek(Editor.get().time+1/25); await new Promise(resolve => setTimeout(resolve, 300));
      const during = SplicePicture.stats.renderer;
      extension.restoreContext();
      await new Promise(resolve => setTimeout(resolve, 300));
      Editor.seek(Editor.get().time-1/25); await new Promise(resolve => setTimeout(resolve, 300));
      return {during, after:SplicePicture.stats.renderer, lost:SplicePicture.stats.lost, restored:SplicePicture.stats.restored};
    });
    assert.deepEqual(lost, {during:'canvas', after:'webgl2', lost:1, restored:1}, JSON.stringify(lost));
    const restored = await readMonitor();
    // ---- Export the same project with the real renderer.
    const project = await page.evaluate(async () => {
      const prepared = await SpliceMedia.forPremiere(Editor.get().project);
      delete prepared.premiereOriginal; delete prepared.premiereManaged;
      prepared.renderOptions = {mutedTracks:{}};
      return prepared;
    });
    // ProRes: one master cannot mix HDR and SDR sources unless delivered as Rec. 709, which also
    // converts SD primaries; so SDR cases and HDR cases go to two ProRes files. H.264 takes all.
    const hdrMedia = new Set(project.media.filter(item => item.name === 'hlg.mp4' || item.name === 'pq.mp4').map(item => item.id));
    const render = (name, clips, options) => {
      const projectFile = path.join(splice.temp, name+'.json'), movie = path.join(splice.temp, name+(options.codec === 'prores' ? '.mov' : '.mp4'));
      fs.writeFileSync(projectFile, JSON.stringify({...project, clips}));
      execFileSync('python3', ['-c', `import json,sys; sys.path.insert(0, ${JSON.stringify(root)}); from render_movie import render_to_file; render_to_file(json.load(open(sys.argv[1])), sys.argv[2], options=json.loads(sys.argv[3]))`,
        projectFile, movie, JSON.stringify({crf:14, limiter:false, ...options})], {cwd:root, env:{...process.env, TMPDIR:splice.temp, HOME:splice.temp, XDG_DATA_HOME:splice.temp}, stdio:['ignore', 'inherit', 'inherit'], timeout:900000});
      return movie;
    };
    const decode = (movie, samples = sampleTimes) => {
      const select = samples.map(sample => `eq(n\\,${sample.frame})`).join('+');
      // Decoded as a player does: exact conversion (zscale to planar RGB; packed rgb24 would fall back to swscale), video range. ProRes carries no range
      // flag, and FFmpeg's default 10-bit to RGB path is off by up to two codes.
      const raw = execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-i', movie, '-vf', `select='${select}',setparams=range=tv,zscale,format=gbrp`, '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], {maxBuffer:samples.length*W*H*3+65536, timeout:180000});
      assert.equal(raw.length, samples.length*W*H*3, `${movie}: every sample frame decoded`);
      return samples.map((_, index) => centers.map(([x, y]) => {
        const sum = [0, 0, 0];
        for (let yy = y-3; yy < y+3; yy++) for (let xx = x-3; xx < x+3; xx++) for (let c = 0; c < 3; c++) sum[c] += raw[index*W*H*3+(yy*W+xx)*3+c];
        return sum.map(v => v/36);
      }));
    };
    const isHdr = sample => sample.item.source === 'hlg' || sample.item.source === 'pq';
    const sdrSamples = sampleTimes.filter(sample => !isHdr(sample)), hdrSamples = sampleTimes.filter(isHdr);
    const sdrMovie = decode(render('parity-sdr', project.clips.filter(clip => !hdrMedia.has(clip.mediaId)), {codec:'prores'}), sdrSamples);
    const hdrMovie = hdrSamples.length ? decode(render('parity-hdr', project.clips.filter(clip => hdrMedia.has(clip.mediaId)), {codec:'prores', colorSpace:'rec709'}), hdrSamples) : [];
    const exports = {prores:sampleTimes.map(sample => isHdr(sample) ? hdrMovie[hdrSamples.indexOf(sample)] : sdrMovie[sdrSamples.indexOf(sample)]),
      h264:decode(render('parity-h264', project.clips, {codec:'h264'}))};
    // ---- Compare.
    const worst = (a, b) => Math.max(...a.flatMap((patch, i) => patch.map((v, c) => Math.abs(v-b[i][c]))));
    const rows = [], failures = [];
    const uncut = sampleTimes.findIndex(sample => sample.item.edit === 'uncut');
    sampleTimes.forEach((sample, index) => {
      const hdr = isHdr(sample);
      const row = {case:sample.item.name+(sample.item.times.length > 1 ? ` @${(sample.time-sample.item.start).toFixed(2)}s` : ''),
        prores:worst(gl[index], exports.prores[index]), h264:worst(gl[index], exports.h264[index]),
        canvas:worst(canvas[index], exports.prores[index]), restored:worst(restored[index], exports.prores[index]),
        moved:worst(gl[index], PATCHES)};
      const limits = {prores:hdr ? TOLERANCE.hdr : TOLERANCE.prores, h264:hdr ? TOLERANCE.hdrH264 : TOLERANCE.h264};
      row.limit = `${limits.prores}/${limits.h264}`;
      rows.push(row);
      if (uncut >= 0 && ['split','trimmed'].includes(sample.item.edit)) {
        const original = uncut+sample.item.times.findIndex(t => Math.abs(sample.time-sample.item.start-t)<1e-6);
        for (const [path, pictures] of Object.entries({webgl:gl, canvas, prores:exports.prores})) {
          const error = worst(pictures[index], pictures[original]);
          if (error > 1) failures.push(`${row.case}: ${path} differs from the uncut frame by ${error.toFixed(2)} codes`);
        }
      }
      for (const key of ['prores', 'canvas', 'restored']) if (row[key] > limits.prores) failures.push(`${row.case}: monitor (${key === 'prores' ? 'WebGL2' : key}) vs ProRes ${row[key].toFixed(2)} > ${limits.prores}`);
      if (row.h264 > limits.h264) failures.push(`${row.case}: monitor vs H.264 ${row.h264.toFixed(2)} > ${limits.h264}`);
    });
    if (process.env.SPLICE_PARITY_KEEP) fs.cpSync(splice.temp, process.env.SPLICE_PARITY_KEEP, {recursive:true});
    if (process.env.SPLICE_PARITY_DEBUG) for (const name of process.env.SPLICE_PARITY_DEBUG.split(',')) {
      const index = sampleTimes.findIndex(sample => sample.item.name === name);
      console.log(name, JSON.stringify({monitor:gl[index].map(p => p.map(Math.round)), prores:exports.prores[index].map(p => p.map(Math.round)), h264:exports.h264[index].map(p => p.map(Math.round))}));
    }
    // Grades must actually change the picture (no tautology): most cases move some patch by > 8 codes.
    const changed = rows.filter(row => row.moved > 8).length;
    const table = ['| case | WebGL2 vs ProRes | WebGL2 vs H.264 | Canvas vs ProRes | after context loss vs ProRes | limit ProRes/H.264 |', '|---|---|---|---|---|---|',
      ...rows.map(row => `| ${row.case} | ${row.prores.toFixed(2)} | ${row.h264.toFixed(2)} | ${row.canvas.toFixed(2)} | ${row.restored.toFixed(2)} | ${row.limit} |`)].join('\n');
    if (proof) fs.writeFileSync(path.join(proof, 'parity-table.md'), table+'\n');
    console.log(table);
    if (!process.env.SPLICE_PARITY_ONLY) {
      assert.ok(changed >= rows.length*.7, `the cases change the picture (${changed} of ${rows.length})`);
      for (const source of ['hlg', 'pq']) {
        const idxOf = name => sampleTimes.findIndex(s => s.item.name === name);
        const u = idxOf(`${source} source`), b = idxOf(`${source} source bypassed`);
        assert.ok(worst(exports.prores[b], exports.prores[u]) <= 1, `${source} bypassed ProRes matches tone-mapped ungraded`);
        assert.ok(worst(gl[b], gl[u]) <= 2, `${source} bypassed WebGL2 matches tone-mapped ungraded`);
        const a0 = sampleTimes.findIndex(s => s.item.name === `${source} source animated exposure` && Math.abs(s.time-s.item.start) < .01);
        const a2 = sampleTimes.findIndex(s => s.item.name === `${source} source animated exposure` && Math.abs(s.time-s.item.start-.96) < .01);
        assert.ok(worst(exports.prores[a0], exports.prores[a2]) > 40, `${source} animated exposure moves across keyframes in ProRes`);
        assert.ok(worst(gl[a0], gl[a2]) > 40, `${source} animated exposure moves across keyframes in WebGL2`);
      }
    }
    assert.deepEqual(failures, [], failures.join('\n'));
    if (!process.env.SPLICE_PARITY_ONLY) for (const source of ['hlg', 'pq']) {
      const index = sampleTimes.findIndex(s => s.item.name === source+' source');
      await page.evaluate(async time => {
        Editor.seek(time);
        const {activeClip,project} = Editor.get(), item = project.media.find(m => m.id===activeClip.mediaId);
        await SpliceMedia.preparePreview(item, item.audioStream ?? null, true).done;
        Editor.updateClip(activeClip.id,{color:{exposure:1}});
      }, sampleTimes[index].time);
      for (const mode of ['bypass', 'split']) {
        const toggle = page.locator('[data-monitor-'+mode+']');
        await toggle.click();
        await page.waitForFunction(({centers, reference, split}) => {
          const canvas = document.querySelector('#preview-canvas'), ctx = canvas.getContext('2d');
          const delta = centers.map(([x,y], i) => {
            const rgb = ctx.getImageData(x,y,1,1).data;
            return Math.max(...reference[i].map((v,c) => Math.abs(rgb[c]-v)));
          });
          const right = delta.filter((v,i) => centers[i][0] > canvas.width/2);
          const left = delta.filter((v,i) => centers[i][0] < canvas.width/2);
          return split ? Math.max(...right)<=4 && Math.max(...left)>20 : Math.max(...delta)<=4;
        }, {centers, reference:gl[index], split:mode==='split'});
        await toggle.click();
      }
    }
    assert.deepEqual(errors, []);
    console.log(`Monitor matches the export: ${rows.length} samples, WebGL2, Canvas and after context loss, ProRes and H.264.`);
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exit(1); });
