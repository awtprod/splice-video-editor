/* The Export video dialog's Audio group: limiter, ceiling, MP4 peak-safe gain and subtitle
 * language. Checks the defaults, what is shown for each format, that invalid input is refused
 * before a job starts, that the request carries exactly the option names and types the renderer
 * accepts, and that a finished export shows the renderer's report and every warning.
 * SPLICE_EVIDENCE_DIR saves screenshots at 1440 × 900, 1180 × 780 and 1024 × 640. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, ffmpeg} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const state = (page, fn, arg) => page.evaluate(fn, arg);
const waitFor = (page, fn, arg, timeout = 15000) => page.waitForFunction(fn, arg, {timeout});
const dialog = '.export-dialog';
// The renderer's export options (render_movie._export_settings) and the type each must have.
const accepted = {codec: 'string', crf: 'number', audioBitrate: 'number', colorSpace: 'string', range: 'object',
  limiter: 'boolean', limiterCeiling: 'number', aacPeakSafe: 'boolean', subtitleLanguage: 'string'};

// One second of a 0 dBFS two-tone program under a small picture: AAC overshoots the ceiling on it.
function makeLoudClip(file) {
  ffmpeg(['-f', 'lavfi', '-i', 'color=c=gray:s=160x90:r=25:d=1',
    '-f', 'lavfi', '-i', "aevalsrc='0.5*sin(2*PI*997*t)+0.5*sin(2*PI*1499*t)':s=48000:c=stereo:d=1",
    '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-shortest', file]);
}

async function project(page, clip, {captions = true, sound = true} = {}) {
  await state(page, () => SpliceSession.newProject({name: 'Export audio', width: 320, height: 180, fps: 25}));
  await page.locator('#media-input').setInputFiles(clip);
  await waitFor(page, () => Editor.get().project.media[0]?.audioChannels > 0, null, 30000);
  await state(page, ([captions, sound]) => Editor.editProject(project => {
    const mediaId = project.media[0].id;
    for (const [id, type, track] of [['v', 'video', 1], ...(sound ? [['a', 'audio', -1]] : [])])
      project.clips.push({id, mediaId, type, track, start: 0, duration: 1, in: 0, linkGroup: sound ? 'pair' : undefined,
        embeddedAudio: type === 'video' ? false : undefined, color: {}, volume: 100, transition: 'none', title: ''});
    project.captions = captions ? [{id: 'cue', start: 0.2, duration: 0.6, text: 'Hello'}] : [];
  }), [captions, sound]);
  await page.evaluate(() => document.activeElement?.blur?.());
}

async function openDialog(page) {
  await page.locator('#export-video').click();
  await page.locator(dialog).waitFor();
}
const field = (page, name) => page.locator(`${dialog} [name=${name}]`);
const visible = (page, name) => state(page, name => !document.querySelector(`.export-dialog [name=${name}]`).closest('label').hidden, name);
const closeDialog = page => page.locator(`${dialog} [data-cancel]`).click();

// Watch the requests the page makes: the export POST bodies and the last status of each job.
function watchExports(page) {
  const seen = {posts: [], jobs: []};
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/exports') seen.posts.push(request.postDataJSON());
  });
  page.on('response', async response => {
    if (!/^\/api\/exports\/[^/]+$/.test(new URL(response.url()).pathname) || response.request().method() !== 'GET') return;
    try { seen.jobs.push(await response.json()); } catch {}
  });
  return seen;
}

function checkTypes(options) {
  for (const [name, value] of Object.entries(options)) {
    assert.ok(name in accepted, `the dialog sends only options the renderer reads, not ${name}`);
    assert.equal(typeof value, accepted[name], `${name} is sent as a ${accepted[name]}`);
  }
}

// Render, wait for the dialog to settle, and return the request's options, the final job status
// and the download.
async function render(page, seen) {
  const posts = seen.posts.length, polls = seen.jobs.length;
  const download = page.waitForEvent('download', {timeout: 120000});
  await page.locator(`${dialog} [type=submit]`).click();
  await page.waitForFunction(() => !document.querySelector('.export-dialog [type=submit]').disabled, null, {timeout: 120000});
  const status = await page.locator(`${dialog} .render-state`).textContent();
  if (!/ exported\./.test(status)) { download.catch(() => {}); throw Error('Export did not complete: ' + status); }
  assert.equal(seen.posts.length, posts + 1, 'one export request');
  const options = seen.posts.at(-1).options;
  checkTypes(options);
  // The page reads the final status before it settles; this listener's copy may land a moment later.
  const finished = () => seen.jobs.slice(polls).find(job => job.state === 'complete');
  for (let tries = 0; tries < 50 && !finished(); tries++) await new Promise(resolve => setTimeout(resolve, 100));
  const job = finished();
  assert.ok(job, 'the finished job status was read');
  return {options, job, download: await download};
}

async function reportShown(page) {
  return state(page, () => ({
    hidden: document.querySelector('.export-dialog .export-report').hidden,
    measured: document.querySelector('.export-dialog .export-measured')?.textContent ?? null,
    warnings: [...document.querySelectorAll('.export-dialog .export-warnings li')].map(item => item.textContent),
  }));
}
const signed = value => (value > 0 ? '+' : value < 0 ? '−' : '') + Math.abs(value).toFixed(1);

async function checkFits(page) {
  const fit = await state(page, () => {
    const box = document.querySelector('.export-dialog').getBoundingClientRect();
    const buttons = [...document.querySelectorAll('.export-dialog footer button')].map(button => {
      const rect = button.getBoundingClientRect();
      return document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) === button;
    });
    return {inside: box.top >= 0 && box.left >= 0 && box.bottom <= innerHeight && box.right <= innerWidth, buttons};
  });
  assert.ok(fit.inside, 'the dialog fits the window');
  assert.deepEqual(fit.buttons, [true, true], 'Close and Render stay visible');
}
async function capture(page, name) {
  await checkFits(page);
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  await page.screenshot({path: path.join(evidence, `${name}-${page.viewportSize().width}x${page.viewportSize().height}.png`)});
}

async function behaviour(splice, page, clip, seen) {
  await project(page, clip);
  await openDialog(page);
  // Defaults: limiter on at −1 dBFS, peak-safe gain off (MP4 only), subtitle language und.
  assert.equal(await field(page, 'limiter').isChecked(), true);
  assert.equal(await field(page, 'limiterCeiling').inputValue(), '-1');
  assert.deepEqual(await state(page, () => {
    const ceiling = document.querySelector('.export-dialog [name=limiterCeiling]');
    return [ceiling.min, ceiling.max];
  }), ['-24', '0']);
  assert.equal(await field(page, 'aacPeakSafe').isChecked(), false);
  assert.equal(await visible(page, 'aacPeakSafe'), true);
  assert.match(await page.locator('#export-peak-safe-help').textContent(), /0\.5 dB steps.*reports the gain/);
  assert.equal(await field(page, 'subtitleLanguage').inputValue(), 'und');
  assert.equal(await visible(page, 'subtitleLanguage'), true, 'captions make a subtitle stream');
  assert.deepEqual(await state(page, () => [...document.querySelector('.export-dialog [name=subtitleLanguage]').options].map(o => o.value)),
    ['und', 'eng', 'spa', 'fra', 'deu', 'ita', 'por', 'jpn', 'kor', 'zho', 'ara', 'hin', 'other']);
  assert.equal(await visible(page, 'subtitleLanguageCode'), false);

  // Every new control is labelled and reachable by Tab, in reading order.
  const order = await state(page, () => [...document.querySelectorAll('.export-dialog input,.export-dialog select')]
    .filter(el => !el.closest('[hidden]') && !el.disabled).map(el => [el.name, el.labels.length > 0 && el.labels[0].textContent.trim() !== '']));
  for (const [name, labelled] of order) assert.ok(labelled, `${name} is labelled`);
  await field(page, 'bitrate').focus();
  const tabbed = [];
  for (let step = 0; step < 4; step++) { await page.keyboard.press('Tab'); tabbed.push(await state(page, () => document.activeElement.name)); }
  assert.deepEqual(tabbed, ['limiter', 'limiterCeiling', 'aacPeakSafe', 'subtitleLanguage']);

  // Typing in the number and text fields stays in them: no timeline shortcut runs.
  const before = await state(page, () => JSON.stringify([Editor.get().time, Editor.get().project.marks, Editor.get().project.clips.length]));
  await field(page, 'limiterCeiling').fill('');
  await field(page, 'limiterCeiling').pressSequentially('-3');
  await field(page, 'subtitleLanguage').selectOption('other');
  assert.equal(await visible(page, 'subtitleLanguageCode'), true);
  await field(page, 'subtitleLanguageCode').pressSequentially('io ');
  assert.equal(await field(page, 'subtitleLanguageCode').inputValue(), 'io ');
  assert.equal(await state(page, () => JSON.stringify([Editor.get().time, Editor.get().project.marks, Editor.get().project.clips.length])), before);
  assert.equal(await state(page, () => SpliceKeys.keyBelongsToControl(document.querySelector('.export-dialog [name=limiterCeiling]'), {key: 'i'})), true);
  assert.equal(await state(page, () => SpliceKeys.keyBelongsToControl(document.querySelector('.export-dialog [name=subtitleLanguageCode]'), {key: 'i'})), true);

  // Invalid input is refused in the dialog with a message; no job starts.
  const posts = seen.posts.length;
  await page.locator(`${dialog} [type=submit]`).click();
  assert.match(await page.locator(`${dialog} .render-state`).textContent(), /three-letter ISO 639-2 code/);
  await field(page, 'subtitleLanguageCode').fill('nld');
  for (const value of ['-30', '0.5', '']) {
    await field(page, 'limiterCeiling').fill(value);
    await page.locator(`${dialog} [type=submit]`).click();
    assert.equal(await page.locator(`${dialog} .render-state`).textContent(), 'Enter a ceiling from −24 to 0 dBFS.', `ceiling ${JSON.stringify(value)} refused`);
  }
  assert.equal(seen.posts.length, posts, 'no export request for invalid input');

  // Limiter off: the ceiling is disabled (and its bad value no longer matters).
  await field(page, 'limiter').uncheck();
  assert.equal(await field(page, 'limiterCeiling').isDisabled(), true);
  await field(page, 'limiter').check();
  assert.equal(await field(page, 'limiterCeiling').isDisabled(), false);
  await field(page, 'limiterCeiling').fill('-1');

  // MP4 with the peak-safe gain on, Dutch subtitles: the report shows peak, loudness and gain.
  await field(page, 'aacPeakSafe').check();
  const safe = await render(page, seen);
  assert.deepEqual(safe.options, {codec: 'h264', crf: 20, audioBitrate: 192, limiter: true, limiterCeiling: -1, aacPeakSafe: true, subtitleLanguage: 'nld'});
  const audio = safe.job.report.audio;
  assert.ok(audio && audio.aacPeakSafe === true && Number.isFinite(audio.decodedTruePeakDb) && Number.isFinite(audio.loudnessLufs), JSON.stringify(safe.job.report));
  let shown = await reportShown(page);
  assert.equal(shown.hidden, false);
  assert.equal(shown.measured, 'Decoded audio: peak ' + signed(audio.decodedTruePeakDb) + ' dBTP · ' + signed(audio.loudnessLufs) + ' LUFS · ' +
    (audio.gainDb ? 'lowered ' + Math.abs(audio.gainDb).toFixed(1) + ' dB to fit the ceiling' : 'no gain needed'));
  assert.ok(audio.gainDb < 0, 'this loud program needs a gain to fit');
  assert.deepEqual(shown.warnings, safe.job.warnings);
  const output = path.join(splice.temp, 'peak-safe.mp4');
  await safe.download.saveAs(output);
  const streams = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', output])).streams;
  assert.equal(streams.find(s => s.codec_type === 'subtitle')?.tags?.language, 'nld');
  console.log('MP4 with aacPeakSafe: ' + shown.measured);

  // Default MP4: AAC overshoots the ceiling, and the renderer's warning is listed.
  await field(page, 'aacPeakSafe').uncheck();
  await field(page, 'subtitleLanguage').selectOption('und');
  const plain = await render(page, seen);
  assert.deepEqual(plain.options, {codec: 'h264', crf: 20, audioBitrate: 192, limiter: true, limiterCeiling: -1, aacPeakSafe: false, subtitleLanguage: 'und'});
  shown = await reportShown(page);
  assert.deepEqual(shown.warnings, plain.job.warnings);
  assert.ok(shown.warnings.some(text => /above the .* ceiling/.test(text)), 'the over-ceiling warning is shown: ' + JSON.stringify(plain.job));
  assert.doesNotMatch(shown.measured, /lowered|no gain/, 'no gain line without aacPeakSafe');
  console.log('Default MP4 warning shown: ' + shown.warnings[0].slice(0, 70) + '…');

  // Limiter off: no ceiling is sent, nothing is measured.
  await field(page, 'limiter').uncheck();
  const unlimited = await render(page, seen);
  assert.equal(unlimited.options.limiter, false);
  assert.equal('limiterCeiling' in unlimited.options, false);
  assert.equal((await reportShown(page)).measured, null);
  await field(page, 'limiter').check();

  // WAV: no AAC option, no subtitle language.
  await field(page, 'codec').selectOption('wav');
  assert.equal(await visible(page, 'aacPeakSafe'), false);
  assert.equal(await page.locator('#export-peak-safe-help').isVisible(), false);
  assert.equal(await visible(page, 'subtitleLanguage'), false);
  const wav = await render(page, seen);
  assert.deepEqual(wav.options, {codec: 'wav', crf: 20, audioBitrate: 192, limiter: true, limiterCeiling: -1});
  assert.ok(wav.download.suggestedFilename().endsWith('.wav'));
  await closeDialog(page);
  console.log('WAV export: no AAC option or subtitle language sent.');

  // A silent MP4 reports a null peak: the dialog says "silent".
  await project(page, clip, {captions: false, sound: false});
  await openDialog(page);
  assert.equal(await visible(page, 'subtitleLanguage'), false, 'no captions, no subtitle stream');
  const silent = await render(page, seen);
  assert.equal('subtitleLanguage' in silent.options, false);
  assert.equal(silent.job.report.audio?.decodedTruePeakDb, null);
  const silentLine = (await reportShown(page)).measured;
  assert.equal(silentLine, 'Decoded audio: silent');
  assert.doesNotMatch(silentLine, /null|Infinity|NaN/);
  await closeDialog(page);
  console.log('Silent MP4: ' + silentLine);
}

async function screenshots(page, clip, seen) {
  await project(page, clip);
  await openDialog(page);
  await capture(page, 'export-mp4');
  await field(page, 'codec').selectOption('wav');
  await capture(page, 'export-wav');
  await field(page, 'codec').selectOption('h264');
  await render(page, seen);
  assert.ok((await reportShown(page)).warnings.length, 'the finished default MP4 shows a warning');
  // The report sits under the status line; scroll it into view as an editor would.
  await page.locator(`${dialog} .export-report`).scrollIntoViewIfNeeded();
  await capture(page, 'export-finished-warning');
  await closeDialog(page);
}

async function main() {
  const splice = await startSplice('splice-xa-');
  try {
    const clip = path.join(splice.temp, 'loud.mov');
    makeLoudClip(clip);
    let first = true;
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
      const {page, errors} = await openPage(splice, viewport);
      const seen = watchExports(page);
      console.log(`Export dialog at ${viewport.width} × ${viewport.height}`);
      if (first) await behaviour(splice, page, clip, seen);
      await screenshots(page, clip, seen);
      assert.deepEqual(errors, []);
      await page.context().close();
      first = false;
    }
    console.log('Export audio dialog passed.');
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
