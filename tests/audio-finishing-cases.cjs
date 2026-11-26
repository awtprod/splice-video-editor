/* Cases for tests/audio-finishing.cjs (one function each, run at 1440 × 900 and 1180 × 780 where
 * layout matters). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const {openPage, ffmpeg} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const root = path.resolve(__dirname, '..');
const lazy = () => require('./audio-finishing.cjs');
const VIEWPORTS = [{width: 1440, height: 900}, {width: 1180, height: 780}];
const report = {};
function record(key, value) {
  report[key] = value;
  if (evidence) fs.writeFileSync(path.join(evidence, 'browser-measurements.json'), JSON.stringify(report, null, 2));
}

// Typing into a number field must never reach the timeline's shortcuts.
async function typeSafely(page, locator, text) {
  const before = await page.evaluate(() => JSON.stringify({clips: Editor.get().project.clips.length, marks: Editor.get().project.marks || null,
    time: Editor.get().time, playing: Editor.get().playing}));
  await locator.click();
  await locator.press('Control+a');
  for (const key of ['s', 'i', 'o', 'x', 'k', 'Backspace', 'Delete', 'ArrowLeft']) await locator.press(key);
  await locator.pressSequentially(text);
  const after = await page.evaluate(() => JSON.stringify({clips: Editor.get().project.clips.length, marks: Editor.get().project.marks || null,
    time: Editor.get().time, playing: Editor.get().playing}));
  assert.equal(after, before, 'keys typed in a number field reached the timeline');
  await locator.press('Enter');
  await locator.press('Tab');
}

async function busEffects(splice, media) {
  const {workspace} = lazy();
  const {page} = await workspace(splice, media, VIEWPORTS[0]);
  const track = page.locator('.audio-track[data-track="-1"]');
  await track.locator('[data-track-fx]').click();
  for (const master of [false, true]) {
    const mount = master ? page.locator('[data-master]') : track.locator('[data-track-mount]');
    await mount.locator('[data-toggle=comp]').check();
    for (const [key, value] of [['threshold', '-36'], ['ratio', '6']]) {
      const input = mount.locator('input[data-kind=comp][data-key=' + key + ']');
      await input.focus();
      await input.fill(value);
      await input.dispatchEvent('change');  // Commit while focused, as with the number field's stepper.
    }
    const comp = await page.evaluate(master => {
      const p = Editor.get().project;
      return (master ? p.masterFx : p.trackMix['-1'].fx).comp;
    }, master);
    assert.ok(comp.enabled);
    assert.equal(comp.threshold, -36);
    assert.equal(comp.ratio, 6);
  }
  await page.context().close();
}

async function inspectorAudio(splice, media) {
  const {project, shot, state, waitFor} = lazy();
  for (const viewport of VIEWPORTS) {
    const {page, errors} = await openPage(splice, viewport);
    await project(page, media);
    await state(page, () => Editor.select('d1'));
    const panel = page.locator('#inspector-content');
    await panel.locator('[data-field=channelMap]').selectOption('mono');
    await waitFor(page, () => Editor.get().project.clips.find(clip => clip.id === 'd1').channelMap === 'mono');
    await panel.locator('[data-field=audioTransition]').selectOption('dip');
    await waitFor(page, () => Editor.get().project.clips.find(clip => clip.id === 'd1').audioTransition === 'dip');
    // The linked sound of v2 follows its picture's dissolve: the inspector says so.
    await state(page, () => Editor.select('d2'));
    assert.match(await page.locator('#inspector-content [data-field=audioTransition] option[value=auto]').textContent(), /Follow picture \(crossfade\)/);
    await state(page, () => Editor.select('d1'));
    await panel.locator('[data-toggle=eq]').check();
    await panel.locator('[data-preset]').selectOption('dialogue');
    await waitFor(page, () => Editor.get().project.clips.find(clip => clip.id === 'd1').audioFx?.eq?.bands?.length === 3);
    await typeSafely(page, panel.locator('[data-band="0"] [data-band-field=freq]'), '100');
    await waitFor(page, () => Editor.get().project.clips.find(clip => clip.id === 'd1').audioFx.eq.bands[0].freq === 100);
    await panel.locator('[data-toggle=comp]').check();
    await typeSafely(page, panel.locator('input[data-kind=comp][data-key=threshold]'), '-40');
    await waitFor(page, () => Editor.get().project.clips.find(clip => clip.id === 'd1').audioFx.comp.threshold === -40);
    // An out-of-range value is refused with a message, not stored.
    const ratio = panel.locator('input[data-kind=comp][data-key=ratio]');
    await ratio.fill('50'); await ratio.press('Enter');
    assert.equal(await state(page, () => Editor.get().project.clips.find(clip => clip.id === 'd1').audioFx.comp.ratio), 3);
    assert.ok(await ratio.evaluate(input => !input.validity.valid));
    // Play: the compressor's gain-reduction meter moves (−40 dBFS threshold on dialogue).
    await state(page, () => { Editor.seek(.5); Editor.togglePlay(); });
    await waitFor(page, () => (SpliceMixer.reduction('clip:d1')?.comp ?? 0) < -1, null, 15000);
    await page.locator('.fx-block[data-block=comp]').scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const meter = await page.locator('[data-gr=comp] output').textContent();
    assert.match(meter, /^−\d+\.\d dB$/);
    await shot(page, 'inspector-compressor');
    await page.locator('.fx-block[data-block=eq]').scrollIntoViewIfNeeded();
    await shot(page, 'inspector-eq');
    await state(page, () => Editor.togglePlay());
    // Units are written next to every number field.
    const unlabelled = await page.locator('.audio-fx-mount input[type=number]').evaluateAll(inputs =>
      inputs.filter(input => !/\((dBFS|dB|ms|Hz|:1)\)|Q$/.test(input.getAttribute('aria-label') || '')).map(input => input.outerHTML));
    assert.deepEqual(unlabelled, []);
    // Keyboard: Tab reaches the effect controls.
    await page.locator('[data-toggle=eq]').focus();
    let reached = false;
    for (let step = 0; step < 12 && !reached; step++) {
      await page.keyboard.press('Tab');
      reached = await state(page, () => !!document.activeElement?.closest('.fx-band'));
    }
    assert.ok(reached, 'Tab does not reach the EQ band fields');
    assert.deepEqual(errors, []);
    await page.context().close();
  }
}

async function automation(splice, media) {
  const {project, shot, state, waitFor} = lazy();
  for (const viewport of VIEWPORTS) {
    const {page, errors} = await openPage(splice, viewport);
    await project(page, media);
    const header = page.locator('.track-head').filter({hasText: 'Audio 1'}).locator('button[aria-label^="Show automation"]');
    await header.click();
    const lane = page.locator('.track-lane[data-kind=audio][data-track="-1"] .automation-lane');
    await lane.waitFor();
    const box = await lane.boundingBox();
    const pps = await state(page, () => (document.querySelector('.timeline-clip[data-clip=d1]').getBoundingClientRect().width+1)/4);
    // Add two points, at 1 s and 3 s.
    await page.mouse.click(box.x + pps*1, box.y + box.height*.6);
    await waitFor(page, () => Editor.get().project.trackMix['-1'].keyframes?.volume?.length === 1);
    await page.mouse.click(box.x + pps*3, box.y + box.height*.6);
    await waitFor(page, () => Editor.get().project.trackMix['-1'].keyframes?.volume?.length === 2);
    const points = await state(page, () => Editor.get().project.trackMix['-1'].keyframes.volume);
    assert.deepEqual(points.map(point => point.time), [1, 3]);
    // Drag the second point down to about half height (≈ 100 %) and a quarter second later.
    const dot = page.locator('.track-lane[data-track="-1"] .automation-point').nth(1);
    const at = await dot.boundingBox();
    await page.mouse.move(at.x + at.width/2, at.y + at.height/2);
    await page.mouse.down();
    await page.mouse.move(at.x + at.width/2 + pps*.25, box.y + box.height*.75, {steps: 6});
    await page.mouse.up();
    await waitFor(page, () => Math.abs(Editor.get().project.trackMix['-1'].keyframes.volume[1].time - 3.24) < .05);
    // The lane is redrawn after each edit and the edited point keeps the focus.
    const focused = () => waitFor(page, () => document.activeElement?.classList?.contains('automation-point'));
    await focused();
    const dragged = await state(page, () => Editor.get().project.trackMix['-1'].keyframes.volume[1]);
    assert.ok(dragged.value < 100, `dragged value ${dragged.value}`);
    // Keyboard: focus, Up raises by 1 (Shift by 10), E eases, Enter types a value, Delete removes.
    const first = page.locator('.track-lane[data-track="-1"] .automation-point').first();
    await first.focus();
    const start = await state(page, () => Editor.get().project.trackMix['-1'].keyframes.volume[0].value);
    await page.keyboard.press('ArrowUp');
    await waitFor(page, value => Editor.get().project.trackMix['-1'].keyframes.volume[0].value === value + 1, start);
    await focused();
    await page.keyboard.press('e');
    await waitFor(page, () => Editor.get().project.trackMix['-1'].keyframes.volume[0].ease === true);
    await focused();
    await page.keyboard.press('Enter');
    const entry = page.locator('.automation-entry');
    await entry.waitFor();
    await entry.fill('50');
    await entry.press('Enter');
    await waitFor(page, () => Editor.get().project.trackMix['-1'].keyframes.volume[0].value === 50);
    await shot(page, 'automation-lane');
    // The curve is what playback hears: at 2 s, between the eased point (50 % at 1 s) and the dragged one.
    const expected = await state(page, () => {
      const [a, b] = Editor.get().project.trackMix['-1'].keyframes.volume, u = (2 - a.time)/(b.time - a.time);
      return a.value + (b.value - a.value)*u*u*(3 - 2*u);
    });
    const heard = await state(page, () => { Editor.seek(2); return SpliceAudioDSP.curveValue(Editor.get().project.trackMix['-1'].keyframes.volume, 2, 100); });
    assert.ok(Math.abs(heard - expected) < 1e-9);
    const clips = await state(page, () => Editor.get().project.clips.length);
    await page.locator('.track-lane[data-track="-1"] .automation-point').first().focus();
    await page.keyboard.press('Delete');
    await waitFor(page, () => Editor.get().project.trackMix['-1'].keyframes.volume.length === 1);
    assert.equal(await state(page, () => Editor.get().project.clips.length), clips, 'Delete on a point deleted clips');
    // Undo restores the point (each edit is one step).
    await state(page, () => document.activeElement?.blur());
    await page.keyboard.press('Control+z');
    await waitFor(page, () => Editor.get().project.trackMix['-1'].keyframes.volume.length === 2);
    // Pan automation on the same lane.
    await page.locator('.track-lane[data-track="-1"] .automation-tag select').selectOption('pan');
    await page.locator('.track-lane[data-track="-1"] .automation-lane').click({position: {x: pps*2, y: 4}});
    await waitFor(page, () => Editor.get().project.trackMix['-1'].keyframes.pan?.length === 1);
    assert.ok(await state(page, () => Editor.get().project.trackMix['-1'].keyframes.pan[0].value) > 80);
    assert.deepEqual(errors, []);
    await page.context().close();
  }
}

async function exportDialog(splice, media) {
  const {project, shot, state, waitFor} = lazy();
  const dialog = '.studio-dialog[data-dialog=export]';
  for (const viewport of VIEWPORTS) {
    const {page, errors} = await openPage(splice, viewport);
    await project(page, media);
    const bodies = [];
    page.on('request', request => { if (request.url().endsWith('/api/exports') && request.method() === 'POST') bodies.push(JSON.parse(request.postData()).options); });
    await page.locator('#export-video').click();
    await page.locator(dialog).waitFor();
    const field = name => page.locator(`${dialog} [name=${name}]`);
    assert.equal(await field('normalize').inputValue(), 'off', 'normalization must be off by default');
    assert.equal(await field('audioDeliver').inputValue(), 'program');
    assert.ok(await field('normalizeMode').evaluate(node => node.closest('label').hidden));
    await field('codec').selectOption('wav');
    await field('normalize').selectOption('-23');
    assert.ok(!(await field('normalizeMode').evaluate(node => node.closest('label').hidden)));
    await page.locator(`${dialog} .export-audio`).scrollIntoViewIfNeeded();
    await shot(page, 'export-audio-group');
    await page.locator(`${dialog} button[type=submit]`).click();
    await waitFor(page, d => /exported/.test(document.querySelector(d + ' .render-state').textContent), dialog, 120000);
    const lines = await page.locator(`${dialog} .export-measured`).allTextContents();
    assert.ok(lines.some(line => /Normalization to -23 LUFS \(gain only\): reached/.test(line)), lines.join('\n'));
    assert.ok(lines.some(line => /^WAV · stereo: −23\.\d LUFS integrated/.test(line)), lines.join('\n'));
    await page.locator(`${dialog} .export-report`).scrollIntoViewIfNeeded();
    await shot(page, 'export-report-normalized');
    assert.deepEqual(bodies.at(-1), {codec: 'wav', crf: 20, audioBitrate: 192, limiter: true, limiterCeiling: -1,
      normalize: -23, normalizeMode: 'linear'});
    // Stems: a ZIP of WAVs, one per stem, plus the mix; the report names the stems.
    await field('normalize').selectOption('off');
    await field('audioDeliver').selectOption('stems');
    await field('isoTracks').check();
    await page.locator(`${dialog} button[type=submit]`).click();
    await waitFor(page, d => /Stems exported/.test(document.querySelector(d + ' .render-state').textContent), dialog, 180000);
    assert.equal(bodies.at(-1).codec, 'stems');
    assert.equal(bodies.at(-1).isoTracks, true);
    const stems = await page.locator(`${dialog} .export-measured`).allTextContents();
    assert.ok(stems.some(line => /Stems: Dialogue \(A1\) · Music \(A2\) — they sum to the mix/.test(line)), stems.join('\n'));
    assert.deepEqual(errors, []);
    await page.context().close();
  }
}

// A take: a recorder WAV of syllable-like bursts (tests/audio_fixtures.py) and a camera clip whose
// scratch sound starts `offset` seconds into it (quieter, filtered, hiss, AAC).
function makeTake(directory, offset) {
  const recorder = path.join(directory, `recorder-${offset}.wav`), camera = path.join(directory, `camera-${offset}.mp4`);
  const fixtures = path.join(root, 'tests', 'audio_fixtures.py');
  execFileSync('python3', [fixtures, 'speech', recorder, '40', '7']);
  execFileSync('python3', [fixtures, 'take', recorder, camera, String(offset), '20']);
  return {recorder, camera};
}

async function syncCommand(splice) {
  const {shot, state, waitFor} = lazy();
  const offset = 3.48;
  const take = makeTake(splice.temp, offset);
  for (const viewport of VIEWPORTS) {
    const {page, errors} = await openPage(splice, viewport);
    await state(page, () => SpliceSession.newProject({name: 'Sync', width: 320, height: 180, fps: 25}));
    await page.locator('#media-input').setInputFiles([take.camera, take.recorder]);
    await waitFor(page, () => Editor.get().project.media.length === 2 && Editor.get().project.media.every(item => item.duration > 0), null, 30000);
    await state(page, () => Editor.editProject(project => {
      const camera = project.media.find(item => item.type === 'video');
      project.clips.push({id: 'pic', mediaId: camera.id, type: 'video', track: 1, start: 2, duration: 12, in: 1, color: {}, volume: 100,
        transition: 'none', title: '', embeddedAudio:false, linkGroup:'camera-pair'});
      project.clips.push({id:'scratch', mediaId:camera.id, type:'audio', track:-1, start:2, duration:12, in:1, linkGroup:'camera-pair'});
    }));
    await state(page, () => Editor.select('pic'));
    await page.locator('.edit-menu summary').click();
    await page.locator('.edit-menu [data-command=syncSound]').click();
    const dialog = page.locator('.audio-sync-dialog');
    await dialog.waitFor();
    const keep = viewport.width === 1180;
    await dialog.locator('[name=scratch]').selectOption(keep ? 'keep' : 'mute');
    await dialog.locator('[name=method]').selectOption('waveform');
    await dialog.locator('button[type=submit]').click();
    await waitFor(page, () => /synced and linked/.test(document.querySelector('.audio-sync-dialog .audio-sync-results')?.textContent || ''), null, 120000);
    await shot(page, 'sync-dialog');
    const result = await state(page, () => {
      const project = Editor.get().project, picture = project.clips.find(clip => clip.id === 'pic');
      const sound = project.clips.find(clip => clip.type === 'audio' && clip.id !== 'scratch');
      return {scratch:project.clips.find(c => c.id === 'scratch'), picture, sound, text: document.querySelector('.audio-sync-results').textContent};
    });
    assert.ok(result.sound, result.text);
    assert.equal(result.scratch.enabled !== false, keep);
    assert.equal(result.scratch.linkGroup, result.picture.linkGroup);
    assert.equal(result.sound.linkGroup, result.picture.linkGroup);
    assert.equal(result.picture.embeddedAudio, false, 'the scratch track is turned off');
    const error = (result.sound.in - (result.picture.in + offset))*48000;
    record('syncErrorSamples', error);
    assert.ok(Math.abs(error) <= 48, `sync off by ${error.toFixed(1)} samples`);
    assert.equal(result.sound.start, result.picture.start);
    await dialog.locator('[data-close]').click();
    await page.evaluate(() => Editor.undo());
    const restored = await page.evaluate(() => Editor.get().project.clips);
    assert.deepEqual(restored.map(c => c.id), ['pic', 'scratch']);
    assert.ok(restored.every(c => c.linkGroup === 'camera-pair' && c.enabled !== false));
    assert.deepEqual(errors, []);
    await page.context().close();
  }
}

// The preview's IIRFilterNodes (the browser's own filter code, fed audio-dsp.js coefficients)
// against FFmpeg's biquad chain (fed audio_dsp.py coefficients), measured on FFmpeg's impulse response.
async function eqParity(splice) {
  const {state, python} = lazy();
  const {page, errors} = await openPage(splice);
  const settings = [
    {eq: {bands: [{type: 'highpass', freq: 80, q: .7071}, {type: 'peak', freq: 250, q: 1.2, gain: -3}, {type: 'peak', freq: 3200, q: 2, gain: 4.5},
      {type: 'highshelf', freq: 9000, q: .7, gain: -6}, {type: 'lowshelf', freq: 120, q: .8, gain: 3}, {type: 'lowpass', freq: 16000, q: .9}]}},
    {eq: {bands: [{type: 'peak', freq: 6500, q: 3, gain: -12}, {type: 'peak', freq: 40, q: 6, gain: 12}, {type: 'peak', freq: 18000, q: 1, gain: 9}]}},
  ];
  const frequencies = Array.from({length: 121}, (_, index) => 20*10**(3*index/120));
  let worst = 0;
  for (const fx of settings) {
    const preview = await state(page, ([fx, frequencies]) => {
      const context = new OfflineAudioContext(2, 128, 48000);
      const total = new Float32Array(frequencies.length).fill(0);
      const hz = Float32Array.from(frequencies), mag = new Float32Array(frequencies.length), phase = new Float32Array(frequencies.length);
      for (const [b0, b1, b2, a1, a2] of SpliceAudioDSP.eqSections(fx.eq, 48000)) {
        context.createIIRFilter([b0, b1, b2], [1, a1, a2]).getFrequencyResponse(hz, mag, phase);
        for (let index = 0; index < mag.length; index++) total[index] += 20*Math.log10(mag[index]);
      }
      return [...total];
    }, [fx, frequencies]);
    const exported = JSON.parse(python(`
import json, sys, math, subprocess, array, audio_dsp
request = json.load(sys.stdin)
chain = ','.join(audio_dsp.ffmpeg_filters(audio_dsp.normalize(request['fx'])))
length = 1 << 17
raw = subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i',f"aevalsrc='if(eq(n,0),1,0)':s=48000:d={length/48000}",
  '-af', chain, '-f','f64le','-c:a','pcm_f64le','-'])
h = array.array('d', raw)[:length]
out = []
for f in request['frequencies']:
    w = 2*math.pi*f/48000
    re = sum(v*math.cos(w*n) for n, v in enumerate(h) if abs(v) > 1e-13)
    im = sum(v*math.sin(w*n) for n, v in enumerate(h) if abs(v) > 1e-13)
    out.append(20*math.log10(math.hypot(re, im)))
print(json.dumps(out))`, {fx, frequencies}));
    preview.forEach((value, index) => { worst = Math.max(worst, Math.abs(value - exported[index])); });
  }
  record('eqParityMaxDb', worst);
  assert.ok(worst <= .1, `EQ preview and export differ by ${worst.toFixed(4)} dB`);
  assert.deepEqual(errors, []);
  await page.context().close();
}

// The dynamics worklet in a real (offline) Web Audio graph against FFmpeg acompressor / agate.
async function dynamicsParity(splice) {
  const {state, python} = lazy();
  const {page, errors} = await openPage(splice);
  const effects = {
    comp: {threshold: -24, ratio: 4, attack: 5, release: 80, knee: 6, makeup: 3},
    gate: {threshold: -30, ratio: 6, attack: 1, release: 60, range: -30},
    denoise: {amount: 20, floor: -35},
  };
  const signals = JSON.parse(python(`
import json, sys, subprocess, array, base64, audio_dsp
request = json.load(sys.stdin)
source = "aevalsrc='(0.6*gt(mod(t,1),0.5)+0.03)*sin(2*PI*440*t)|(0.6*gt(mod(t,1),0.5)+0.03)*sin(2*PI*440*t+1)':s=48000:d=4"
def pcm(af=None):
    return subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i',source,*(['-af',af] if af else []),'-f','f32le','-c:a','pcm_f32le','-'])
out = {'input': base64.b64encode(pcm()).decode()}
for kind, settings in request.items():
    fx = audio_dsp.normalize({kind: settings})
    out[kind] = base64.b64encode(pcm(audio_dsp.ffmpeg_filters(fx)[0])).decode()
print(json.dumps(out))`, effects));
  const results = {};
  for (const kind of Object.keys(effects)) {
    results[kind] = await state(page, async ([signals, kind, settings]) => {
      const decode = text => { const bytes = Uint8Array.from(atob(text), c => c.charCodeAt(0)); return new Float32Array(bytes.buffer); };
      const input = decode(signals.input), expected = decode(signals[kind]), frames = input.length/2;
      const context = new OfflineAudioContext(2, frames, 48000);
      await context.audioWorklet.addModule('audio-dsp.js');
      const buffer = context.createBuffer(2, frames, 48000);
      for (let channel = 0; channel < 2; channel++) buffer.copyToChannel(input.filter((_, index) => index % 2 === channel), channel);
      const source = context.createBufferSource(); source.buffer = buffer;
      const node = new AudioWorkletNode(context, 'splice-dynamics', {outputChannelCount: [2], processorOptions: settings});
      source.connect(node).connect(context.destination); source.start();
      const rendered = await context.startRendering();
      let maxAbs = 0, maxDb = 0;
      for (let channel = 0; channel < 2; channel++) {
        const data = rendered.getChannelData(channel);
        for (let index = 0; index < frames; index++) {
          const want = expected[2*index + channel], got = data[index];
          maxAbs = Math.max(maxAbs, Math.abs(got - want));
          if (Math.abs(want) > 1e-3) maxDb = Math.max(maxDb, Math.abs(20*Math.log10(Math.abs(got/want))));
        }
      }
      return {maxAbs, maxDb};
    }, [signals, kind, {[kind]: effects[kind]}]);
  }
  record('dynamicsParity', results);
  for (const [kind, {maxDb}] of Object.entries(results)) assert.ok(maxDb < .01, `${kind} preview differs from export by ${maxDb} dB`);
  assert.deepEqual(errors, []);
  await page.context().close();
}

// A mono signal stays mono through clip dynamics until it reaches the stereo panner.
async function monoDynamics(splice) {
  const {page} = await openPage(splice);
  await page.goto(new URL('audio-mixer.js', page.url()).href);
  await page.addScriptTag({url:'audio-dsp.js'});
  await page.addScriptTag({url:'audio-mixer.js'});
  const levels = await page.evaluate(async () => {
    const ctx = new OfflineAudioContext(2, 2048, 48000);
    await SpliceMixer.attach(ctx);
    const route = {source:ctx.createGain(), input:ctx.createGain(), gain:ctx.createGain(), pan:ctx.createStereoPanner(), analyser:ctx.createAnalyser()};
    route.source.connect(route.input);
    route.gain.connect(route.pan).connect(route.analyser);
    const clip = {id:'mono', track:-1, channelMap:'right', audioFx:{comp:{ratio:1, makeup:0}}};
    const project = {clips:[clip]};
    SpliceMixer.configure(project);
    SpliceMixer.routeClip(route, clip, project, 1);
    const source = ctx.createConstantSource();
    source.offset.value = .1;
    source.connect(route.source);
    source.start();
    const result = await ctx.startRendering();
    return [0,1].map(channel => result.getChannelData(channel)[1500]);
  });
  for (const level of levels) assert.ok(Math.abs(level - .1*Math.SQRT1_2) < .0001, String(levels));
  await page.context().close();
}

async function auditionEffects(splice, media) {
  const {project, waitFor} = lazy();
  for (const settings of [{reverse:true}, {speed:.125}]) {
    const {page} = await openPage(splice);
    await project(page, media);
    await page.evaluate(settings => {
      Editor.editProject(p => {
        const music = p.media.find(item => item.type === 'audio');
        p.clips = [{id:'audition', mediaId:music.id, type:'audio', track:-1, start:0, duration:6, in:0,
          channelMap:'right', audioFx:{comp:{threshold:-50, ratio:10}}, ...settings}];
      });
      Editor.seek(.2);
      Editor.togglePlay();
    }, settings);
    await waitFor(page, () => (SpliceMixer.reduction('clip:audition')?.comp ?? 0) < -6, null, 15000);
    await page.context().close();
  }
}

// 12 audio tracks, EQ (four bands) and a compressor on every track: live playback long tasks,
// and the same graph rendered offline to measure how far ahead of real time it runs.
async function performance12(splice, media) {
  const {project, state, waitFor} = lazy();
  const {page, errors} = await openPage(splice);
  await project(page, media);
  const fx = {eq: {bands: [{type: 'highpass', freq: 80, q: .7}, {type: 'peak', freq: 300, q: 1, gain: -3}, {type: 'peak', freq: 3000, q: 1.5, gain: 3},
    {type: 'highshelf', freq: 10000, q: .7, gain: -2}]}, comp: {threshold: -24, ratio: 3, attack: 10, release: 120, knee: 6, makeup: 2}};
  await state(page, fx => Editor.editProject(project => {
    const music = project.media.find(item => item.type === 'audio');
    project.clips = [];
    project.trackMix = {};
    for (let track = 1; track <= 12; track++) {
      project.clips.push({id: 'p' + track, mediaId: music.id, type: 'audio', track: -track, start: 0, duration: 8, in: 0, volume: 20,
        transition: 'none', title: '', audioFx: {eq: fx.eq}});
      project.trackMix[String(-track)] = {gain: 100, pan: 0, mute: false, solo: false, fx};
    }
  }), fx);
  const live = await state(page, async () => {
    const tasks = [];
    const observer = new PerformanceObserver(list => tasks.push(...list.getEntries().map(entry => entry.duration)));
    observer.observe({type: 'longtask', buffered: false});
    Editor.seek(0); Editor.togglePlay();
    const began = performance.now();
    await new Promise(resolve => setTimeout(resolve, 6000));
    Editor.togglePlay();
    observer.disconnect();
    return {seconds: (performance.now() - began)/1000, longTasks: tasks.length, longestMs: Math.max(0, ...tasks), stats: SpliceMixer.stats()};
  });
  const offline = await state(page, async fx => {
    const seconds = 20, context = new OfflineAudioContext(2, 48000*seconds, 48000);
    await context.audioWorklet.addModule('audio-dsp.js');
    const noise = context.createBuffer(2, 48000*seconds, 48000);
    for (let channel = 0; channel < 2; channel++) { const data = noise.getChannelData(channel); for (let i = 0; i < data.length; i++) data[i] = (Math.random() - .5)*.3; }
    for (let track = 0; track < 12; track++) {
      const source = context.createBufferSource(); source.buffer = noise;
      let node = source;
      for (const round of [0, 1]) {  // clip EQ, then track EQ and compressor
        for (const [b0, b1, b2, a1, a2] of SpliceAudioDSP.eqSections(fx.eq, 48000)) node = node.connect(context.createIIRFilter([b0, b1, b2], [1, a1, a2]));
        if (round) { const dyn = new AudioWorkletNode(context, 'splice-dynamics', {outputChannelCount: [2], processorOptions: {comp: fx.comp}}); node = node.connect(dyn); }
      }
      node.connect(context.destination); source.start();
    }
    const began = performance.now();
    await context.startRendering();
    return {seconds, renderSeconds: (performance.now() - began)/1000};
  }, fx);
  offline.realtimeFactor = offline.seconds/offline.renderSeconds;
  record('performance12Tracks', {live, offline});
  console.log('12-track playback', JSON.stringify(live), 'offline', JSON.stringify(offline));
  assert.equal(live.stats.tracks, 12);
  assert.ok(live.stats.iir >= 12*4, 'every track runs its EQ');
  assert.ok(offline.realtimeFactor > 3, `the 12-track graph renders only ${offline.realtimeFactor.toFixed(1)}x real time`);
  assert.ok(live.longestMs < 250, `a ${live.longestMs} ms long task during playback`);
  assert.deepEqual(errors, []);
  await page.context().close();
}

exports.cases = (process.env.AUDIO_CASES ? process.env.AUDIO_CASES.split(',') : null) ?
  [eqParity, dynamicsParity, monoDynamics, auditionEffects, busEffects, inspectorAudio, automation, exportDialog, syncCommand, performance12].filter(fn => process.env.AUDIO_CASES.split(',').includes(fn.name)) :
  [eqParity, dynamicsParity, monoDynamics, auditionEffects, busEffects, inspectorAudio, automation, exportDialog, syncCommand, performance12];
