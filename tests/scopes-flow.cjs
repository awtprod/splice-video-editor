/* Browser regression for the video scopes: geometry is asserted from the scope canvas pixels. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {startSplice} = require('./splice-server.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');
const vm = require('node:vm');

// ---- Pure checks under Node: scopes.js draws into a recording 2D context. Every expectation
// below is computed here from the Rec. 709 / broadcast definitions, not read from scopes.js.
function recorder(log) {
  const ctx = {font:'10px monospace', calls:log};
  for (const name of ['fillRect','strokeRect','beginPath','moveTo','lineTo','stroke','arc','fillText','strokeText','save','restore','drawImage','putImageData','setLineDash'])
    ctx[name] = (...args) => { log.push({name, args, strokeStyle:ctx.strokeStyle, fillStyle:ctx.fillStyle, dash:ctx.dash, align:ctx.textAlign, font:ctx.font}); if (name === 'setLineDash') ctx.dash = args[0].length > 0; };
  // Monospace text: 0.6 em per character.
  ctx.measureText = value => ({width:String(value).length*parseFloat(ctx.font)*.6});
  return ctx;
}
function loadScopes() {
  const traces = [];
  const sandbox = {window:{}, document:{createElement:() => ({width:0, height:0, getContext:() => { const log = []; traces.push(log); return recorder(log); }})},
    ImageData:class { constructor(data, width, height) { Object.assign(this, {data, width, height}); } }};
  sandbox.window.document = sandbox.document;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'scopes.js'), 'utf8'), sandbox);
  return {S:sandbox.window.SpliceScopes, traces};
}
const frameOf = (width, height, pixel) => {
  const data = new Uint8ClampedArray(width*height*4);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) data.set([...pixel(x, y), 255], (y*width+x)*4);
  return {data, width, height};
};
// Draw a scope and return its calls, layout and trace image.
function draw(kind, width, height, frame) {
  const {S, traces} = loadScopes(), log = [];
  const layout = S.render(recorder(log), kind, width, height, 1, frame);
  const put = traces.flat().filter(call => call.name === 'putImageData').at(-1);
  return {S, log, layout, trace:put?.args[0]};
}
function unitChecks() {
  const {S} = loadScopes();
  const exactLuma = (r, g, b) => 2126*r+7152*g+722*b;           // luma x 10000
  // Every 8-bit colour lands in the nearest luma code: at most 0.5 code from the exact value.
  let worst = 0;
  for (let r = 0; r < 256; r++) for (let g = 0; g < 256; g++) for (let b = 0; b < 256; b++)
    worst = Math.max(worst, Math.abs(S.lumaCode(r, g, b)*10000-exactLuma(r, g, b)));
  assert.ok(worst <= 5000, `luma is at most 0.5 code off (worst ${worst/10000})`);
  // Waveform and histogram both bin luma: pure red 54, green 182, blue 18 (Rec. 709 x 255, rounded).
  // One red, two green and three blue pixels, so each count also says which primary it came from.
  const primaries = frameOf(6, 1, x => [[255,0,0],[0,255,0],[0,255,0],[0,0,255],[0,0,255],[0,0,255]][x]);
  const wave = S.analyze(primaries.data, 6, 1, 'waveform', 6), hist = S.analyze(primaries.data, 6, 1, 'histogram');
  [[0,54],[1,182],[2,182],[3,18],[4,18],[5,18]].forEach(([column, code]) => assert.equal(wave.luma[column*256+code], 1, `waveform luma of picture column ${column} is ${code}`));
  assert.deepEqual([54,182,18].map(code => hist.histogram[3][code]), [1,2,3], 'histogram luma: red 54, green 182, blue 18');
  // Histogram channels count their own channel.
  assert.deepEqual([0,1,2].map(channel => [hist.histogram[channel][255], hist.histogram[channel][0]]), [[1,5],[2,4],[3,3]], 'histogram R, G, B');
  // Waveform columns: 960 picture columns into 480 bins, two per bin, in order.
  const ramp = frameOf(960, 1, x => [x%256, x%256, x%256]), columns = S.analyze(ramp.data, 960, 1, 'waveform');
  for (let x = 0; x < 960; x++) assert.ok(columns.luma[(x >> 1)*256+x%256] >= 1, `picture column ${x} lands in bin ${x >> 1}`);
  // Sampling: at most 960 columns and 960x540 pixels for any shape; 16:9 at 1080p gives 960x540.
  assert.deepEqual({...S.sampleSize(1920, 1080)}, {width:960, height:540});
  assert.deepEqual({...S.sampleSize(960, 540)}, {width:960, height:540});
  assert.deepEqual({...S.sampleSize(960, 1707)}, {width:540, height:960});
  assert.deepEqual({...S.sampleSize(1080, 1080)}, {width:720, height:720});
  assert.deepEqual({...S.sampleSize(320, 240)}, {width:320, height:240});
  for (const [w, h] of [[1080,1920],[1440,1080],[4096,2160],[720,1280],[2000,300]]) {
    const size = S.sampleSize(w, h);
    assert.ok(size.width <= 960 && size.width*size.height <= 960*540 && (size.width === 960 || size.width*size.height > 960*540*.99) && Math.abs(size.width/size.height-w/h) < .02*w/h+1/size.height, `${w}x${h} -> ${JSON.stringify(size)}`);
  }

  // Level axis: 255 at the top of the plot, 0 at the bottom; legal-range lines at 16 and 235.
  const gray = frameOf(64, 36, () => [200,200,200]);
  const waveform = draw('waveform', 400, 240, gray), plot = waveform.layout.plot;
  const levelY = level => plot.y+(255-level)/255*(plot.h-1);
  const label = (log, value) => log.filter(call => call.name === 'fillText' && call.args[0] === value);
  assert.ok(Math.abs(label(waveform.log, '100')[0].args[2]-levelY(255)) < .01, '100 % is at the top');
  assert.ok(Math.abs(label(waveform.log, '0')[0].args[2]-levelY(0)) < .01, '0 % is at the bottom');
  // Strokes: each line is beginPath/moveTo/lineTo/stroke; collect [x0,y0,x1,y1,style,dashed].
  const lines = log => {
    const out = [];
    for (let i = 0; i+1 < log.length; i++) if (log[i].name === 'moveTo' && log[i+1].name === 'lineTo') out.push([...log[i].args, ...log[i+1].args, log[i+1].strokeStyle, log[i+1].dash]);
    return out;
  };
  const amber = lines(waveform.log).filter(line => line[5] && line[4].startsWith('rgba(224,164,74'));
  assert.deepEqual(amber.map(line => line[1]).sort((a, b) => a-b), [Math.round(levelY(235))+.5, Math.round(levelY(16))+.5], 'legal-range lines at 235 and 16');
  for (const code of ['255 1023','235 940','16 64','0 0']) assert.equal(label(waveform.log, code).length, 1, `code label ${code}`);
  // The trace of a flat 200 gray sits on the 200 row of the plot, across the plot.
  const rowOf = y => { let n = 0; for (let x = 0; x < plot.w; x++) if (waveform.trace.data[(y*plot.w+x)*4+3]) n++; return n; };
  const row = Math.round((255-200)/255*(plot.h-1));
  assert.ok(rowOf(row) > plot.w*.9 && rowOf(Math.round((255-55)/255*(plot.h-1))) === 0, 'waveform trace at level 200');

  // Parade: three panels side by side covering the plot, widths within a pixel.
  const parade = draw('parade', 400, 240, gray).layout;
  assert.equal(parade.panels[0].x, 0);
  for (let i = 0; i < 3; i++) assert.equal(parade.panels[i].x+parade.panels[i].w, i < 2 ? parade.panels[i+1].x : parade.plot.w, `panel ${i} meets the next`);
  assert.ok(Math.max(...parade.panels.map(p => p.w))-Math.min(...parade.panels.map(p => p.w)) <= 1, 'panels share the width');

  // Vectorscope: 75 % bar targets and the skin-tone line, from Rec. 709 Cb/Cr over a +-0.55 range.
  const RANGE = .55, bars = {R:[1,0,0],Mg:[1,0,1],B:[0,0,1],Cy:[0,1,1],G:[0,1,0],Yl:[1,1,0]};
  const vector = draw('vector', 300, 300, gray), vp = vector.layout.plot;
  const at = (cb, cr) => [vp.x+(cb+RANGE)/(2*RANGE)*vp.w, vp.y+(1-(cr+RANGE)/(2*RANGE))*vp.h];
  const boxes = vector.log.filter(call => call.name === 'strokeRect' && call.strokeStyle === 'rgba(224,230,224,.85)').map(call => [call.args[0]+call.args[2]/2, call.args[1]+call.args[3]/2]);
  assert.equal(boxes.length, 6);
  for (const [name, [r, g, b]] of Object.entries(bars)) {
    const y = .75*(.2126*r+.7152*g+.0722*b), [x0, y0] = at((.75*b-y)/1.8556, (.75*r-y)/1.5748);
    assert.ok(boxes.some(([x, yy]) => Math.hypot(x-x0, yy-y0) <= 1.5), `${name} target at its 75 % position`);
  }
  const skin = lines(vector.log).find(line => line[4].startsWith('rgba(224,164,74'));
  const [cx, cy] = at(0, 0);
  assert.ok(Math.hypot(skin[0]-cx, skin[1]-cy) < .01, 'skin line starts at the centre');
  assert.ok(Math.abs(Math.atan2(cy-skin[3], skin[2]-cx)*180/Math.PI-123) < .2, 'skin line at 123 degrees');
  assert.ok(Math.abs(Math.hypot(skin[2]-cx, skin[3]-cy)-.5/(2*RANGE)*vp.w) < .5, 'skin line reaches the 50 % ring');

  // Histogram: tick lines on the centre column of their level.
  const histogram = draw('histogram', 400, 240, gray), hp = histogram.layout.plot;
  const ticks = lines(histogram.log).filter(line => line[4] === 'rgba(160,185,160,.26)').map(line => line[0]);
  assert.deepEqual(ticks, [0,64,128,192,255].map(level => hp.x+Math.min(hp.w-1, Math.floor((level+.5)*hp.w/256))+.5), 'histogram ticks');
  assert.equal(label(histogram.log, '1023').length, 1, '10-bit full scale is 1023');

  // Narrow scopes (156 CSS px): no two labels of a histogram row touch, and no vectorscope label
  // covers a trace from 75 % and 100 % bars.
  const narrowHistogram = draw('histogram', 156, 200, gray);
  const rows = new Map();
  for (const call of narrowHistogram.log.filter(call => call.name === 'fillText' && call.args[2] > narrowHistogram.layout.plot.y+narrowHistogram.layout.plot.h)) {
    const width = String(call.args[0]).length*parseFloat(call.font)*.6, x = call.args[1];
    const span = call.align === 'left' ? [x, x+width] : call.align === 'right' ? [x-width, x] : [x-width/2, x+width/2];
    const list = rows.get(call.args[2]) ?? []; list.push(span); rows.set(call.args[2], list);
  }
  assert.equal(rows.size, 3, 'three label rows');
  for (const spans of rows.values()) {
    spans.sort((a, b) => a[0]-b[0]);
    assert.ok(spans.length >= 3, 'ends and middle stay labelled');
    for (let i = 1; i < spans.length; i++) assert.ok(spans[i][0]-spans[i-1][1] >= 3, `histogram labels keep apart: ${JSON.stringify(spans)}`);
  }
  const saturated = frameOf(12, 2, (x, y) => Object.values(bars)[x%6].map(value => Math.round(value*(x < 6 ? 191 : 255))));
  for (const size of [156, 300]) {
    const scope = draw('vector', size, size, saturated), sp = scope.layout.plot, font = scope.layout.font;
    for (const call of scope.log.filter(call => call.name === 'fillText' && Object.keys(bars).includes(call.args[0]))) {
      const half = String(call.args[0]).length*font*.3, [x, y] = [call.args[1]-sp.x, call.args[2]-sp.y];
      let covered = 0;
      for (let yy = Math.floor(y-font/2); yy <= Math.ceil(y+font/2); yy++) for (let xx = Math.floor(x-half); xx <= Math.ceil(x+half); xx++)
        if (xx >= 0 && yy >= 0 && xx < sp.w && yy < sp.h && scope.trace.data[(yy*sp.w+xx)*4+3]) covered++;
      assert.equal(covered, 0, `${size} px vectorscope: label ${call.args[0]} sits on a trace`);
    }
  }
  console.log(`Scope maths: luma within 0.5 code for all 2^24 colours, bins, axes, legal lines, targets, skin line, ticks and narrow labels.`);
}

// Test pictures: left red / right blue, a gray ramp, 75 % colour bars, a flat dark gray.
const FRAMES = {
  redblue:['if(lt(X,480),255,0)','0','if(lt(X,480),0,255)'],
  ramp:['X*255/959','X*255/959','X*255/959'],
  bars:['if(eq(mod(floor(floor(X/120)/2),2),0),191,0)','if(lt(floor(X/120),4),191,0)','if(eq(mod(floor(X/120),2),0),191,0)'],
  gray:['64','64','64'],
};
const BAR_COLORS = {R:[1,0,0],Mg:[1,0,1],B:[0,0,1],Cy:[0,1,1],G:[0,1,0],Yl:[1,1,0]};

// Pixel classes. Graticule lines and labels are gray, amber or white-ish, so these tell traces apart.
const trace = ([r,g,b]) => g-r >= 14 && g-b >= 14;
const strongTrace = ([r,g,b]) => g-r >= 40 && g-b >= 40;
const redTrace = ([r,g,b]) => r-g >= 14 && r-b >= 14 && Math.abs(g-b) <= 8;
const greenTrace = ([r,g,b]) => g-r >= 14 && g-b >= 14;
const blueTrace = ([r,g,b]) => b-g >= 14 && b-r >= 14;
const fill = ([r,g,b]) => Math.max(r,g,b) >= 100 && !(r > g+30 && g > b+40);

async function main() {
  unitChecks();
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-scopes-'));
  const proof = process.env.SPLICE_PROOF_DIR;
  if (proof) fs.mkdirSync(proof, {recursive:true});
  const splice = await startSplice({root, env:{HOME:temp, XDG_DATA_HOME:temp}});
  let browser;
  try {
    const executablePath = [process.env.SPLICE_CHROME,'/opt/google/chrome/chrome','/usr/bin/chromium','/usr/bin/google-chrome',chromium.executablePath()].find(file => file && fs.existsSync(file));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args:['--no-sandbox','--autoplay-policy=no-user-gesture-required']});
    const page = await browser.newPage({viewport:{width:1440,height:960}});
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await splice.open(page);

    for (const [name, expressions] of Object.entries(FRAMES))
      execFileSync('ffmpeg',['-nostdin','-v','error','-y','-f','lavfi','-i',
        'color=c=black:s=960x540,format=rgb24,geq='+['r','g','b'].map((channel, index) => `${channel}='${expressions[index]}'`).join(':'),
        '-frames:v','1',path.join(temp,name+'.png')]);
    await page.evaluate(() => SpliceSession.newProject({name:'Scopes',width:1280,height:720,fps:25}));
    await page.locator('#media-input').setInputFiles(Object.keys(FRAMES).map(name => path.join(temp,name+'.png')));
    await page.waitForFunction(count => Editor.get().project.media.length === count, Object.keys(FRAMES).length);
    const ids = {};
    await page.evaluate(names => {
      Editor.editProject(project => {
        project.clips = names.map((name, index) => ({id:'frame-'+name, mediaId:project.media.find(item => item.name === name+'.png').id,
          type:'video', track:1, start:index*3, duration:3, in:0, color:{}}));
      });
    }, Object.keys(FRAMES));
    Object.keys(FRAMES).forEach((name, index) => { ids[name] = {id:'frame-'+name, time:index*3+1}; });

    // Show a frame in the program monitor, select its clip and wait for the monitor to carry the picture.
    async function show(name, expectPixel) {
      await page.evaluate(({id, time}) => { Editor.select(id); Editor.seek(time); }, ids[name]);
      await page.waitForFunction(({expectPixel}) => {
        const canvas = document.querySelector('#preview-canvas'), data = canvas.getContext('2d').getImageData(10, canvas.height/2|0, 1, 1).data;
        return document.querySelector('.splice-scope') && Math.abs(data[0]-expectPixel[0]) <= 2 && Math.abs(data[1]-expectPixel[1]) <= 2 && Math.abs(data[2]-expectPixel[2]) <= 2;
      }, {expectPixel});
    }
    // Pick a scope (when the build has the picker) and read the scope canvas back.
    async function scope(kind) {
      if (kind && await page.locator('[data-scope]').count()) await page.locator(`[data-scope="${kind}"]`).click();
      await page.evaluate(() => window.SpliceScopes?.paint(document.querySelector('.splice-scope'), true));
      return page.evaluate(() => {
        const canvas = document.querySelector('.splice-scope'), ctx = canvas.getContext('2d');
        return {width:canvas.width, height:canvas.height, layout:window.SpliceScopes?.layoutOf(canvas) ?? null, data:Array.from(ctx.getImageData(0,0,canvas.width,canvas.height).data)};
      });
    }
    const pixelAt = (image, x, y) => { const i = (Math.round(y)*image.width+Math.round(x))*4; return image.data.slice(i, i+3); };
    // True when a pixel of the class lies in the box (plot-relative fractions, inclusive).
    const present = (image, plot, test, [x0, x1, y0, y1]) => {
      let count = 0;
      for (let y = Math.round(plot.y+y0*(plot.h-1)); y <= Math.round(plot.y+y1*(plot.h-1)); y++)
        for (let x = Math.round(x0); x <= Math.round(x1); x++) if (test(pixelAt(image, x, y))) count++;
      return count;
    };

    // ---- RGB parade: horizontal picture position on x within each channel panel, level on y.
    await show('redblue', [255,0,0]);
    const parade = await scope('parade');
    // The previous implementation drew a fixed 240x210 canvas with three 80 px panels over y 2-70.
    const legacy = !parade.layout;
    const plot = legacy ? {x:0, y:2, w:240, h:69} : parade.layout.plot;
    const panels = legacy ? [0,1,2].map(index => ({x:index*80, w:80})) : parade.layout.panels;
    const inPanel = (index, from, to) => [plot.x+panels[index].x+from*panels[index].w, plot.x+panels[index].x+to*panels[index].w];
    const band = {top:[0,.06], middle:[.2,.8], bottom:[.94,1]};
    const coverage = (index, test, half, rows) => {
      // Fraction of the half-panel's pixel columns holding a trace pixel inside the row band.
      const [x0, x1] = half === 'left' ? inPanel(index, .04, .46) : inPanel(index, .54, .96);
      let hit = 0, total = 0;
      for (let x = Math.round(x0); x <= Math.round(x1); x++, total++)
        if (present(parade, plot, test, [x, x, rows[0], rows[1]])) hit++;
      return hit/total;
    };
    // Red is full level on the left half and zero on the right half: top-left and bottom-right only.
    assert.ok(coverage(0, redTrace, 'left', band.top) > .9, 'red: left half should trace at 100 %');
    assert.equal(coverage(0, redTrace, 'right', band.top), 0, 'red: right half must not reach 100 %');
    assert.ok(coverage(0, redTrace, 'right', band.bottom) > .9, 'red: right half should trace at 0 %');
    assert.equal(coverage(0, redTrace, 'left', band.bottom), 0, 'red: left half must not sit at 0 %');
    assert.equal(present(parade, plot, redTrace, [inPanel(0,.02,.98)[0], inPanel(0,.02,.98)[1], ...band.middle]), 0, 'red: nothing between 20 % and 80 %');
    // Green is zero everywhere; blue mirrors red.
    assert.ok(coverage(1, greenTrace, 'left', band.bottom) > .9 && coverage(1, greenTrace, 'right', band.bottom) > .9, 'green: whole panel at 0 %');
    assert.equal(coverage(1, greenTrace, 'left', band.top)+coverage(1, greenTrace, 'right', band.top), 0, 'green: never at 100 %');
    assert.ok(coverage(2, blueTrace, 'right', band.top) > .9 && coverage(2, blueTrace, 'left', band.bottom) > .9, 'blue: left 0 %, right 100 %');
    assert.equal(coverage(2, blueTrace, 'left', band.top)+coverage(2, blueTrace, 'right', band.bottom), 0, 'blue: opposite halves stay empty');
    console.log('RGB parade: red, green and blue trace by picture position and level.');
    if (legacy) throw new Error('Build has no scope picker; the remaining checks need it.');
    // The scope reads the whole 16:9 monitor picture (up to 960 columns), not a thumbnail.
    const sampled = await page.evaluate(() => { const c = document.querySelector('#preview-canvas'); return {preview:[c.width, c.height], sample:SpliceScopes.sampleOf(document.querySelector('.splice-scope'))}; });
    assert.ok(sampled.preview[0] >= 640, JSON.stringify(sampled));
    assert.deepEqual([sampled.sample.width, sampled.sample.height], sampled.preview[0] <= 960 ? sampled.preview : [960, Math.round(sampled.preview[1]*960/sampled.preview[0])], JSON.stringify(sampled));

    const shot = async name => { if (proof) await page.locator('.splice-scopes').screenshot({path:path.join(proof,name)}); };

    // ---- Luma waveform: a left-to-right ramp is a diagonal.
    await show('ramp', [2,2,2]);
    const waveform = await scope('waveform');
    const wave = waveform.layout.plot;
    // Count trace pixels around the point (column fraction, level fraction) of the waveform.
    const around = (column, level) => {
      const x = wave.x+column*(wave.w-1), y = wave.y+(1-level)*(wave.h-1), spread = .025*wave.w+2;
      let hit = 0;
      for (let dy = -spread; dy <= spread; dy++) for (let dx = -spread; dx <= spread; dx++) if (trace(pixelAt(waveform, x+dx, y+dy))) hit++;
      return hit;
    };
    for (const fraction of [.05,.2,.35,.5,.65,.8,.95]) assert.ok(around(fraction, fraction) > 0, `ramp: expected a trace at ${fraction}`);
    for (const fraction of [.1,.25,.8]) assert.equal(around(fraction, 1-fraction), 0, `ramp: the mirrored point at ${fraction} must be empty`);
    await shot('ramp-waveform.png');
    console.log('Luma waveform: gray ramp is a diagonal.');

    // ---- Vectorscope: 75 % bars land on the 75 % colour targets.
    await show('bars', [191,191,191]);
    const vector = await scope('vector');
    const circle = vector.layout.plot, range = await page.evaluate(() => SpliceScopes.range);
    const place = ([r, g, b], gain = .75) => {
      const [R, G, B] = [r*gain, g*gain, b*gain], y = .2126*R+.7152*G+.0722*B;
      const cb = (B-y)/1.8556, cr = (R-y)/1.5748;
      return [circle.x+(cb+range)/(2*range)*circle.w, circle.y+(1-(cr+range)/(2*range))*circle.h];
    };
    const radius = .03*circle.w+2;
    const near = (center, test) => {
      let hit = 0;
      for (let y = Math.floor(center[1]-radius); y <= Math.ceil(center[1]+radius); y++)
        for (let x = Math.floor(center[0]-radius); x <= Math.ceil(center[0]+radius); x++)
          if (Math.hypot(x-center[0], y-center[1]) <= radius && test(pixelAt(vector, x, y))) hit++;
      return hit;
    };
    const centers = [...Object.values(BAR_COLORS).map(color => place(color)), place([0,0,0])];
    for (const [name, color] of Object.entries(BAR_COLORS)) assert.ok(near(place(color), strongTrace) >= 4, `vectorscope: ${name} bar must sit on its 75 % target`);
    assert.ok(near(place([0,0,0]), strongTrace) >= 4, 'vectorscope: white, gray and black bars sit at the centre');
    let stray = 0;
    for (let y = circle.y; y < circle.y+circle.h; y++) for (let x = circle.x; x < circle.x+circle.w; x++)
      if (strongTrace(pixelAt(vector, x, y)) && !centers.some(center => Math.hypot(x-center[0], y-center[1]) <= radius)) stray++;
    assert.equal(stray, 0, 'vectorscope: nothing but the bar colours and the centre');
    for (const kind of ['waveform','parade','vector','histogram']) { await scope(kind); await shot(`bars-${kind}.png`); }
    await scope('vector');
    if (proof) await page.screenshot({path:path.join(proof,'workspace-bars-vectorscope.png')});
    console.log('Vectorscope: all six 75 % bars sit on their targets.');

    // ---- Histogram: peaks where the code values are.
    const heightAt = (image, plotRect, level) => {
      const x = plotRect.x+Math.min(plotRect.w-1, Math.floor((level+.5)*plotRect.w/256));
      let top = null;
      for (let y = plotRect.y; y < plotRect.y+plotRect.h; y++) if (fill(pixelAt(image, x, y)) && top === null) top = y;
      return top === null ? 0 : (plotRect.y+plotRect.h-top)/plotRect.h;
    };
    // The monitor picture comes through an 8-bit video proxy, which moves code values by one or two and leaves gaps in a ramp, so peaks are read within a few levels.
    const peakNear = (image, plotRect, level, spread = 2) => Math.max(...Array.from({length:2*spread+1}, (_, index) => index-spread).map(offset => heightAt(image, plotRect, Math.min(255, Math.max(0, level+offset)))));
    await show('redblue', [255,0,0]);
    await scope('histogram');
    await page.locator('[data-scope-luma]').uncheck();
    const histogram = await scope('histogram'), hp = histogram.layout.plot;
    assert.ok(peakNear(histogram, hp, 0) > .95, 'histogram: green, red and blue pile up at 0');
    assert.ok(peakNear(histogram, hp, 255) > .95, 'histogram: red and blue pile up at 255 (the end bins are drawn clipped at the top)');
    for (const level of [20, 40, 100, 128, 160, 200, 230]) assert.equal(heightAt(histogram, hp, level), 0, `histogram: nothing at ${level}`);
    await show('bars', [191,191,191]);
    const barHistogram = await scope('histogram');
    assert.ok(peakNear(barHistogram, hp, 0) > .4 && peakNear(barHistogram, hp, 191) > .4, 'histogram: bars peak at 0 and 191');
    for (const level of [20, 60, 100, 150, 175, 215, 250]) assert.equal(heightAt(barHistogram, hp, level), 0, `histogram: bars leave ${level} empty`);
    await show('ramp', [2,2,2]);
    const rampHistogram = await scope('histogram');
    for (const level of [10, 60, 120, 180, 240]) assert.ok(peakNear(rampHistogram, hp, level, 5) > .5, `histogram: ramp is level at ${level}`);
    console.log('Histogram: peaks sit at 0 / 255, 0 / 191, and the ramp is flat.');

    // ---- Scopes show the graded picture: exposure, then a 65-point LUT.
    await show('gray', [64,64,64]);
    const levelOf = async () => {
      const image = await scope('waveform'), p = image.layout.plot;
      const found = [];
      for (let y = p.y; y < p.y+p.h; y++) if (strongTrace(pixelAt(image, p.x+p.w/2, y))) found.push(y);
      return found.length ? 255*(1-((found.reduce((a, b) => a+b)/found.length)-p.y)/(p.h-1)) : null;
    };
    assert.ok(Math.abs(await levelOf()-64) < 6, 'ungraded gray sits at 64');
    await page.evaluate(() => Editor.updateClip('frame-gray', {color:{exposure:1}}));
    await page.waitForFunction(() => document.querySelector('#preview-canvas').getContext('2d').getImageData(10,200,1,1).data[0] > 120);
    assert.ok(Math.abs(await levelOf()-128) < 6, '+1 EV doubles the level on the scope');
    await page.evaluate(() => {
      const size = 65, rows = ['LUT_3D_SIZE 65'];
      for (let b = 0; b < size; b++) for (let g = 0; g < size; g++) for (let r = 0; r < size; r++) rows.push([1-r/64, 1-g/64, 1-b/64].join(' '));
      Editor.updateClip('frame-gray', {color:{exposure:0, lut:SpliceLUT.parseCube(rows.join('\n'), 'invert65.cube'), lutIntensity:100}});
    });
    await page.waitForFunction(() => document.querySelector('#preview-canvas').getContext('2d').getImageData(10,200,1,1).data[0] > 180);
    assert.ok(Math.abs(await levelOf()-191) < 6, 'an inverting 65-point LUT moves 64 to 191 on the scope');
    console.log('Scopes follow the graded picture (exposure and a 65-point LUT).');
    // A long 1D LUT is applied at full length in preview and export (lut1d): no approximation note.
    const note = async entries => {
      await page.evaluate(count => {
        const rows = ['LUT_1D_SIZE '+count];
        for (let i = 0; i < count; i++) { const y = (i/(count-1))**(1/2.4); rows.push(`${y} ${y} ${y}`); }
        Editor.updateClip('frame-gray', {color:{lut:SpliceLUT.parseCube(rows.join('\n'), 'toe.cube'), lutIntensity:100}});
        Editor.select(null); Editor.select('frame-gray');
      }, entries);
      return page.evaluate(() => document.querySelector('.splice-lut-note')?.textContent ?? '');
    };
    assert.equal(await note(1024), '', 'a 1024-entry 1D LUT is applied exactly and has no note');
    assert.equal(await note(65), '', 'a 65-entry 1D LUT is exact and has no note');
    console.log('1D LUTs of 1024 and 65 entries are applied exactly; no approximation note.');
    await page.evaluate(() => Editor.updateClip('frame-gray', {color:{lut:null}}));

    // ---- Playback, pausing and scrubbing keep the scopes current; cost per repaint is measured.
    await page.evaluate(() => SpliceSession.newProject({name:'Sample',width:1280,height:720,fps:25}));
    await page.locator('#media-input').setInputFiles(path.join(root,'assets/ridge.mp4'));
    await page.waitForFunction(() => Editor.get().project.media.length === 1);
    await page.evaluate(() => {
      const media = Editor.get().project.media[0];
      Editor.editProject(project => { project.clips = [{id:'ridge', mediaId:media.id, type:'video', track:1, start:0, duration:Math.min(8, media.duration), in:0, color:{}}]; });
      Editor.select('ridge'); Editor.seek(.5);
    });
    await page.waitForFunction(() => Editor.get().video.readyState >= 2 && !Editor.get().video.seeking && document.querySelector('.splice-scope'));
    const signature = async () => {
      await page.evaluate(() => SpliceScopes.paint(document.querySelector('.splice-scope'), true));
      return page.evaluate(() => { const c = document.querySelector('.splice-scope'); let h = 0; const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data; for (let i = 0; i < d.length; i += 7) h = (h*31+d[i])|0; return h; });
    };
    for (const kind of ['waveform','parade','vector','histogram']) {
      await page.locator(`[data-scope="${kind}"]`).click();
      await page.evaluate(() => Editor.seek(.5));
      await page.waitForFunction(() => !Editor.get().video.seeking); await page.waitForTimeout(250);
      const before = await signature();
      assert.equal(await signature(), before, `${kind}: paused scope is stable`);
      await page.evaluate(() => Editor.seek(5));
      await page.waitForFunction(() => !Editor.get().video.seeking); await page.waitForTimeout(250);
      assert.notEqual(await signature(), before, `${kind}: scrubbing to another frame changes the scope`);
      await shot(`${kind}-sample.png`);
    }
    // Unforced paints, as the monitor loop issues them, must track playback on their own.
    await page.evaluate(() => { Editor.seek(0); Editor.togglePlay(); });
    const seen = new Set();
    for (let i = 0; i < 12; i++) {
      await page.waitForTimeout(200);
      seen.add(await page.evaluate(() => { const c = document.querySelector('.splice-scope'); let h = 0; const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data; for (let i = 0; i < d.length; i += 7) h = (h*31+d[i])|0; return h; }));
    }
    await page.evaluate(() => Editor.get().playing && Editor.togglePlay());
    assert.ok(seen.size >= 3, `scopes follow playback by themselves (${seen.size} distinct images)`);
    const cost = await page.evaluate(() => {
      const canvas = document.querySelector('.splice-scope'), out = {};
      for (const kind of SpliceScopes.kinds) {
        document.querySelector(`[data-scope="${kind}"]`).click();
        const times = [];
        for (let i = 0; i < 12; i++) { const start = performance.now(); SpliceScopes.paint(canvas, true); times.push(performance.now()-start); }
        times.sort((a, b) => a-b); out[kind] = +times[6].toFixed(1);
      }
      return out;
    });
    console.log('Scope repaint cost (ms, median, 960x540 frame):', JSON.stringify(cost));
    for (const [kind, milliseconds] of Object.entries(cost)) assert.ok(milliseconds < 60, `${kind} repaint took ${milliseconds} ms`);

    // ---- Resizing: the height slider and a narrow window.
    await page.locator('[data-scope="parade"]').click();
    await page.locator('[data-scope-height]').fill('120'); await page.locator('[data-scope-height]').dispatchEvent('input');
    const small = await page.evaluate(() => document.querySelector('.splice-scope').offsetHeight);
    await page.locator('[data-scope-height]').fill('400'); await page.locator('[data-scope-height]').dispatchEvent('input');
    const large = await page.evaluate(() => document.querySelector('.splice-scope').offsetHeight);
    assert.ok(small === 120 && large === 400, `scope heights ${small}, ${large}`);
    await page.setViewportSize({width:900, height:1000});
    await page.waitForTimeout(300);
    await page.evaluate(() => SpliceScopes.paint(document.querySelector('.splice-scope'), true));
    const narrow = await page.evaluate(() => { const c = document.querySelector('.splice-scope'); return {css:c.clientWidth, bitmap:c.width, plot:SpliceScopes.layoutOf(c).plot}; });
    assert.ok(narrow.bitmap >= 160 && narrow.plot.w >= 30 && narrow.plot.x+narrow.plot.w < narrow.bitmap, JSON.stringify(narrow));
    if (proof) {
      await page.locator('[data-scope-height]').fill('200'); await page.locator('[data-scope-height]').dispatchEvent('input');
      for (const kind of ['parade','vector']) { await scope(kind); await shot(`narrow-900px-${kind}.png`); }
      await page.screenshot({path:path.join(proof,'workspace-narrow-900px.png')});
    }
    if (proof) for (const kind of ['waveform','histogram']) { await scope(kind); await shot(`narrow-900px-${kind}.png`); }
    console.log('Scopes resize with the height control and a narrow window.');

    // ---- Vertical, square and 4:3 sequences: the sample stays within 960x540 pixels.
    await page.setViewportSize({width:1440, height:960});
    for (const [label, width, height] of [['9x16',1080,1920],['1x1',1080,1080],['4x3',1440,1080]]) {
      await page.evaluate(({width, height}) => SpliceSession.newProject({name:'Shape', width, height, fps:25}), {width, height});
      await page.locator('#media-input').setInputFiles(path.join(root,'assets/ridge.mp4'));
      await page.waitForFunction(() => Editor.get().project.media.length === 1);
      await page.evaluate(() => {
        const media = Editor.get().project.media[0];
        Editor.editProject(project => { project.clips = [{id:'shape', mediaId:media.id, type:'video', track:1, start:0, duration:Math.min(8, media.duration), in:0, color:{}}]; });
        Editor.select('shape'); Editor.seek(.5);
      });
      await page.waitForFunction(() => Editor.get().video.readyState >= 2 && !Editor.get().video.seeking && document.querySelector('.splice-scope'));
      await page.waitForTimeout(250);
      const shape = await page.evaluate(() => {
        const canvas = document.querySelector('.splice-scope'), preview = document.querySelector('#preview-canvas'), out = {preview:[preview.width, preview.height], cost:{}};
        for (const kind of SpliceScopes.kinds) {
          document.querySelector(`[data-scope="${kind}"]`).click();
          const times = [];
          for (let i = 0; i < 12; i++) { const start = performance.now(); SpliceScopes.paint(canvas, true); times.push(performance.now()-start); }
          times.sort((a, b) => a-b); out.cost[kind] = +times[6].toFixed(1);
        }
        out.sample = SpliceScopes.sampleOf(canvas);
        return out;
      });
      const pixels = shape.sample.width*shape.sample.height, aspect = shape.sample.width/shape.sample.height;
      assert.ok(pixels <= 960*540 && pixels > 960*540*.98, `${label}: ${JSON.stringify(shape)}`);
      assert.ok(Math.abs(aspect-shape.preview[0]/shape.preview[1]) < .01, `${label}: sample keeps the picture shape ${JSON.stringify(shape)}`);
      for (const [kind, milliseconds] of Object.entries(shape.cost)) assert.ok(milliseconds < 60, `${label} ${kind} repaint took ${milliseconds} ms`);
      console.log(`${label} sequence: monitor ${shape.preview.join('x')}, scopes sample ${shape.sample.width}x${shape.sample.height}, repaint ms ${JSON.stringify(shape.cost)}`);
      if (proof && label === '9x16') for (const kind of ['waveform','parade','vector','histogram']) { await scope(kind); await shot(`vertical-9x16-${kind}.png`); }
    }
    if (proof) await page.screenshot({path:path.join(proof,'workspace-4x3.png')});
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await splice.stop();
    fs.rmSync(temp, {recursive:true, force:true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
