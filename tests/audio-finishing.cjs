/* Audio finishing in the browser: the Audio workspace loudness meter (live, on the preview's
 * master bus), the inspector's Audio section (channels, sound transition, EQ, compressor),
 * track automation editing on the timeline, the export dialog's loudness/stems options and the
 * sync command. Also: EQ magnitude response of the preview's IIRFilterNodes against FFmpeg's
 * rendered impulse response, the dynamics worklet against FFmpeg acompressor/agate, and the
 * cost of a 12-track timeline with EQ and a compressor on every track.
 * Screenshots go to SPLICE_EVIDENCE_DIR (1440 × 900 and 1180 × 780). */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, ffmpeg} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const root = path.resolve(__dirname, '..');
const state = (page, fn, arg) => page.evaluate(fn, arg);
const waitFor = (page, fn, arg, timeout = 15000) => page.waitForFunction(fn, arg, {timeout});
async function shot(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  const size = page.viewportSize();
  await page.screenshot({path: path.join(evidence, `${name}-${size.width}x${size.height}.png`)});
}
const python = (code, input) => execFileSync('python3', ['-c', code], {cwd: root, input: JSON.stringify(input), encoding: 'utf8',
  maxBuffer: 1 << 26, env: {...process.env, PYTHONPATH: root}});

// A dialogue-like camera clip (picture and sound) and a music bed, 8 s each.
function makeMedia(directory) {
  const camera = path.join(directory, 'camera.mp4'), music = path.join(directory, 'music.wav');
  ffmpeg(['-f', 'lavfi', '-i', 'color=c=0x335544:s=160x90:r=25:d=8', '-f', 'lavfi', '-i',
    "anoisesrc=c=pink:a=0.5:r=48000:seed=4:d=8,bandpass=f=900:w=1400,volume=24dB,aeval='val(0)*pow(max(0,sin(2*PI*3.1*t)),2)':c=stereo",
    '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-shortest', camera]);
  ffmpeg(['-f', 'lavfi', '-i', "aevalsrc='0.25*sin(2*PI*220*t)+0.12*sin(2*PI*660*t)|0.25*sin(2*PI*330*t)+0.12*sin(2*PI*990*t)':s=48000:d=8",
    '-c:a', 'pcm_s16le', music]);
  return {camera, music};
}

async function project(page, media) {
  await state(page, () => SpliceSession.newProject({name: 'Audio finishing', width: 320, height: 180, fps: 25}));
  await page.locator('#media-input').setInputFiles([media.camera, media.music]);
  await waitFor(page, () => Editor.get().project.media.length === 2 && Editor.get().project.media.every(item => item.duration > 0), null, 30000);
  await state(page, () => Editor.editProject(project => {
    const camera = project.media.find(item => item.type === 'video'), music = project.media.find(item => item.type === 'audio');
    project.clips.push({id: 'v1', mediaId: camera.id, type: 'video', track: 1, start: 0, duration: 4, in: 0, linkGroup: 'take',
      embeddedAudio: false, color: {}, volume: 100, transition: 'none', title: ''});
    project.clips.push({id: 'v2', mediaId: camera.id, type: 'video', track: 1, start: 4, duration: 4, in: 2, linkGroup: 'take2',
      embeddedAudio: false, color: {}, volume: 100, transition: 'dissolve', transitionDuration: .5, transitionBefore: .25, title: ''});
    project.clips.push({id: 'd1', mediaId: camera.id, type: 'audio', track: -1, start: 0, duration: 4, in: 0, linkGroup: 'take',
      volume: 100, transition: 'none', title: ''});
    project.clips.push({id: 'd2', mediaId: camera.id, type: 'audio', track: -1, start: 4, duration: 4, in: 2, linkGroup: 'take2',
      volume: 100, transition: 'none', title: ''});
    project.clips.push({id: 'm1', mediaId: music.id, type: 'audio', track: -2, start: 0, duration: 8, in: 0, volume: 60,
      transition: 'none', title: ''});
    project.trackMix = {'-1': {gain: 100, pan: 0, mute: false, solo: false, stem: 'Dialogue'},
      '-2': {gain: 100, pan: 0, mute: false, solo: false, stem: 'Music'}};
  }));
  await page.evaluate(() => document.activeElement?.blur?.());
}

async function workspace(splice, media, viewport) {
  const {page, errors} = await openPage(splice, viewport);
  await project(page, media);
  await page.locator('[data-workspace=audio]').click();
  await page.locator('.audio-panel').waitFor();
  return {page, errors};
}

// ---- meter: plays the program through the live meter ----
async function meterWhilePlaying(splice, media, viewport) {
  const {page, errors} = await workspace(splice, media, viewport);
  await state(page, () => { Editor.seek(0); Editor.togglePlay(); });
  await waitFor(page, () => Number.isFinite(window.SpliceMixer.meter()?.shortTerm), null, 20000);
  await page.waitForTimeout(1500);
  const live = await state(page, () => ({snapshot: SpliceMixer.meter(), text: document.querySelector('[data-value=integrated]').textContent,
    stats: SpliceMixer.stats(), rate: Editor.get().playing}));
  await shot(page, 'meter-playing');
  await state(page, () => Editor.togglePlay());
  assert.ok(live.stats.worklet, 'the dynamics/meter worklet loaded');
  assert.equal(live.stats.sampleRate, 48000, 'the preview runs at the renderer rate');
  assert.ok(live.snapshot.momentary > -40 && live.snapshot.momentary < 0, `momentary ${live.snapshot.momentary}`);
  assert.match(live.text, /LUFS$/);
  // Nothing in the panel is clipped horizontally.
  const clipped = await state(page, () => [...document.querySelectorAll('.audio-panel *')].filter(node => {
    const box = node.getBoundingClientRect(), panel = document.querySelector('.audio-panel-content').getBoundingClientRect();
    return box.width && (box.right > panel.right + 1 || box.left < panel.left - 1);
  }).map(node => node.className || node.tagName).slice(0, 5));
  assert.deepEqual(clipped, [], 'controls overflow the Audio panel');
  assert.deepEqual(errors, []);
  await page.context().close();
  return live;
}

module.exports = {makeMedia, project, workspace, shot, state, waitFor, python};

async function main() {
  const splice = await startSplice('splice-audio-');
  try {
    const media = makeMedia(splice.temp);
    for (const viewport of process.env.AUDIO_CASES ? [] : [{width: 1440, height: 900}, {width: 1180, height: 780}]) {
      const live = await meterWhilePlaying(splice, media, viewport);
      console.log('meter', viewport.width, JSON.stringify(live.snapshot));
    }
    for (const test of require('./audio-finishing-cases.cjs').cases) {
      await test(splice, media);
    }
    console.log('audio finishing browser checks passed');
  } finally {
    await splice.stop();
  }
}
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
