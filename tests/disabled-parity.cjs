/* Preview / export parity for transitions next to a disabled clip.
 *
 * One rule everywhere (render_movie._disabled_stand_ins): a transition next to a disabled clip
 * still plays for the enabled clip, and the disabled side contributes nothing (transparent
 * picture, silence). V1 is green under every case; on V2 clip A (red) is followed by clip B
 * (blue) carrying a dissolve, dip to black or wipe, aligned before, centred on or after the
 * cut. With A or B disabled, the program monitor's picture is compared with the decoded
 * export at frames across each transition, and the gain the preview gives each sound clip in an
 * A1 crossfade with the same alignment is compared with the decoded export's sound level.
 *
 * The export goes through the real route (Editor.exportVideo → /api/exports → the renderer on
 * this tree). It runs when that renderer honours `enabled: false` (the probe in
 * tests/test_disabled_render.py). Otherwise it FAILS, unless SPLICE_PENDING_RENDERER_CHANGE
 * names the renderer change that adds it (impl/render-scale). */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, ffmpeg, colorAt} = require('./browser-harness.cjs');

const PENDING = 'impl/render-scale';
const FPS = 25;
const KINDS = ['dissolve', 'fade', 'wipe'];
const LEADS = [0, .4, .8];          // transitionBefore: after the cut, centred, before the cut
const LENGTH = .8;
const BLOCK = 5;                     // seconds per case on the timeline
const PICTURE_TOLERANCE = 20;        // RGB distance, mean over the frame
const GAIN_TOLERANCE = .12;

function rendererVerdict() {
  return execFileSync('python3', [path.join(__dirname, 'test_disabled_render.py')], {encoding: 'utf8'}).trim().split('\n').at(-1);
}

function makeMedia(directory) {
  const files = [];
  for (const color of ['red', 'blue', 'green']) {
    const file = path.join(directory, `${color}.mp4`);
    ffmpeg(['-y', '-f', 'lavfi', '-i', `color=c=${color}:s=160x90:r=${FPS}:d=10`, '-c:v', 'libx264', '-preset', 'ultrafast',
      '-g', '1', '-pix_fmt', 'yuv420p', file]);
    files.push(file);
  }
  for (const frequency of [1000, 500]) {
    const file = path.join(directory, `tone${frequency}.wav`);
    ffmpeg(['-y', '-f', 'lavfi', '-i', `sine=frequency=${frequency}:sample_rate=48000:d=10`, '-af', 'volume=0.5', file]);
    files.push(file);
  }
  return files;
}

function readPcm(file) {
  const raw = execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-i', file, '-vn', '-ac', '1', '-ar', '48000', '-f', 's16le', '-'],
    {maxBuffer: 64 << 20});
  return new Int16Array(raw.buffer, raw.byteOffset, Math.floor(raw.length / 2));
}

// Goertzel magnitude of one frequency around `seconds` (±20 ms) of 48 kHz mono PCM.
function toneLevel(pcm, seconds, frequency) {
  const from = Math.round((seconds - .02) * 48000), to = Math.round((seconds + .02) * 48000);
  const coefficient = 2 * Math.cos(2 * Math.PI * frequency / 48000);
  let previous = 0, before = 0;
  for (let index = from; index < to; index++) [previous, before] = [pcm[index] + coefficient * previous - before, previous];
  return Math.sqrt(Math.max(0, previous * previous + before * before - coefficient * previous * before)) / (to - from);
}

const cases = KINDS.flatMap(kind => LEADS.map(lead => ({kind, lead})));
function timeline(ids, off) {
  const picture = (id, mediaId, track, start, duration, extra = {}) => ({id, mediaId, type: 'video', track, start, duration, in: 1,
    color: {}, volume: 100, transition: 'none', title: '', embeddedAudio: false, ...extra});
  const sound = (id, mediaId, start, duration, extra = {}) => ({id, mediaId, type: 'audio', track: -1, start, duration, in: 1,
    volume: 100, transition: 'none', title: '', ...extra});
  const clips = [];
  cases.forEach(({kind, lead}, index) => {
    const at = index * BLOCK, transition = {transitionDuration: LENGTH, transitionBefore: lead};
    const disabled = side => off === side ? {enabled: false} : {};
    clips.push(picture(`low${index}`, ids.green, 1, at, BLOCK), picture(`a${index}`, ids.red, 2, at, 2, disabled('a')),
      picture(`b${index}`, ids.blue, 2, at + 2, 2, {transition: kind, ...transition, ...disabled('b')}),
      sound(`sa${index}`, ids.tone1000, at, 2, disabled('a')),
      sound(`sb${index}`, ids.tone500, at + 2, 2, {transition: 'dissolve', ...transition, ...disabled('b')}));
  });
  return clips;
}
// Frame-aligned times through each transition window, plus one frame either side of it.
function samples(index, lead) {
  const first = index * BLOCK + 2 - lead;
  return [first - 1 / FPS, ...[.1, .25, .5, .75, .9].map(part => first + part * LENGTH), first + LENGTH + 1 / FPS]
    .map(time => Math.round(time * FPS) / FPS);
}

async function previewColor(page, time) {
  await page.evaluate(t => Editor.seek(t), time);
  await page.waitForFunction(() => [...document.querySelectorAll('body > video')].filter(element => element.getAttribute('src'))
    .every(element => element.readyState >= 2 && !element.seeking));
  await page.waitForTimeout(120);
  return page.evaluate(() => {
    const canvas = document.getElementById('preview-canvas');
    Editor.drawFrame(canvas);
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const sum = [0, 0, 0];
    let count = 0;
    for (let i = 0; i < data.length; i += 4 * 13, count++) for (let c = 0; c < 3; c++) sum[c] += data[i + c];
    return sum.map(value => Math.round(value / count));
  });
}

async function main() {
  const verdict = rendererVerdict();
  if (verdict !== 'honors') {
    if (process.env.SPLICE_PENDING_RENDERER_CHANGE === PENDING) {
      console.log(`Disabled-clip parity: skipped, the renderer on this tree renders disabled clips (${PENDING} pending).`);
      return;
    }
    throw new Error('The renderer on this tree renders clips with `enabled: false`, so preview and export cannot agree. ' +
      `Set SPLICE_PENDING_RENDERER_CHANGE=${PENDING} to allow this while that renderer change is pending.`);
  }
  const splice = await startSplice('splice-parity-');
  try {
    const {page, errors} = await openPage(splice, {width: 1440, height: 900});
    page.setDefaultTimeout(60000);
    await page.evaluate(() => SpliceSession.newProject({name: 'Parity', width: 160, height: 90, fps: 25}));
    await page.locator('#media-input').setInputFiles(makeMedia(splice.temp));
    await page.waitForFunction(() => Editor.get().project.media.length === 5);
    const ids = await page.evaluate(() => Object.fromEntries(Editor.get().project.media.map(item => [item.name.replace(/\..*$/, ''), item.id])));
    const failures = [];
    for (const off of ['a', 'b']) {
      const clips = timeline(ids, off);
      await page.evaluate(next => Editor.editProject(project => { project.clips = next; }), clips);
      const job = await page.evaluate(() => Editor.exportVideo({}));
      const file = path.join(splice.temp, `parity-${off}.mp4`);
      fs.writeFileSync(file, await (await page.request.get(new URL(job.downloadURL, splice.url).href)).body());
      const pcm = readPcm(file);
      // Both tones have the same level in their files: the enabled one, where it plays alone at
      // full gain, is the reference for a gain of 1.
      const reference = off === 'a' ? toneLevel(pcm, 3.5, 500) : toneLevel(pcm, 1, 1000);
      assert.ok(reference > 100, 'the enabled tone is heard: ' + reference);
      for (const [index, {kind, lead}] of cases.entries()) {
        for (const time of samples(index, lead)) {
          const preview = await previewColor(page, time);
          // -ss picks the first frame at or after the time: aim a quarter frame early to get this frame.
          const exported = colorAt(file, time - .25 / FPS);
          const distance = Math.hypot(...preview.map((value, channel) => value - exported[channel]));
          const row = {off, kind, lead, time, preview, exported, distance: Math.round(distance)};
          if (distance > PICTURE_TOLERANCE) failures.push(row);
          if (process.env.SPLICE_PARITY_LOG) console.log(JSON.stringify(row));
          const gains = await page.evaluate(() => Editor.audioGains());
          for (const [id, frequency] of [[`sa${index}`, 1000], [`sb${index}`, 500]]) {
            const heard = Math.min(1.2, toneLevel(pcm, time, frequency) / reference);
            const gain = gains[id] ?? 0;
            const sound = {off, lead, time, clip: id, previewGain: +gain.toFixed(3), exportLevel: +heard.toFixed(3)};
            if (Math.abs(heard - gain) > GAIN_TOLERANCE) failures.push(sound);
            if (process.env.SPLICE_PARITY_LOG) console.log(JSON.stringify(sound));
          }
        }
      }
    }
    assert.deepEqual(failures, [], 'preview and export disagree next to a disabled clip');
    assert.deepEqual(errors, []);
    console.log(`Disabled-clip parity: ${cases.length} picture transitions × A or B disabled and their A1 crossfades match the export.`);
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
