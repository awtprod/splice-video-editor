/* Delivery in the browser: presets, formats this machine cannot encode, conversions announced
 * before rendering, the render queue (add, batch, reorder, hold, cancel, retry, reveal), and the
 * queue surviving a page reload and a server killed mid-render, with no process left behind.
 * Runs at 1440 × 900 and 1180 × 780; checks the Export dialog fits 1024 × 640 with Render in reach.
 * SPLICE_EVIDENCE_DIR saves screenshots. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');
const {startSplice} = require('./splice-server.cjs');

const root = path.resolve(__dirname, '..');
const evidence = process.env.SPLICE_EVIDENCE_DIR;
const ffmpeg = args => execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-y', ...args]);
const dialog = '.studio-dialog[data-dialog=export]';
const queueDialog = '.studio-dialog[data-dialog=queue]';
const state = (page, fn, arg) => page.evaluate(fn, arg);
const waitFor = (page, fn, arg, timeout = 30000) => page.waitForFunction(fn, arg, {timeout, polling: 100});

async function shot(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  const {width, height} = page.viewportSize();
  await page.screenshot({path: path.join(evidence, `${name}-${width}x${height}.png`)});
}

async function browserFor(splice) {
  const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome',
    chromium.executablePath()].find(file => file && fs.existsSync(file));
  assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
  return chromium.launch({executablePath, args: ['--no-sandbox']});
}

async function newPage(browser, splice, viewport, answers) {
  const context = await browser.newContext({viewport, acceptDownloads: true});
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  // prompt() and confirm() answers, in order; confirm defaults to yes.
  page.on('dialog', d => d.accept(d.type() === 'prompt' ? answers.shift() ?? '' : undefined));
  await splice.open(page);
  return {page, errors};
}

async function project(page, clip, seconds = 4) {
  await state(page, () => SpliceSession.newProject({name: 'Queue film', width: 640, height: 360, fps: 25}));
  await page.locator('#media-input').setInputFiles(clip);
  await waitFor(page, () => Editor.get().project.media[0]?.audioChannels > 0);
  await state(page, seconds => Editor.editProject(project => {
    const mediaId = project.media[0].id;
    for (let start = 0; start < seconds; start += 4)
      for (const [type, track] of [['video', 1], ['audio', -1]])
        project.clips.push({id: type + start, mediaId, type, track, start, duration: 4, in: 0, linkGroup: 'pair' + start,
          embeddedAudio: type === 'video' ? false : undefined, color: {}, volume: 100, transition: 'none', title: ''});
    project.startTimecode = 90000;
  }), seconds);
  await page.evaluate(() => document.activeElement?.blur?.());
}

async function openExport(page) {
  await page.locator('#export-video').click();
  await page.locator(dialog).waitFor();
  // The machine's formats arrive with the capability check.
  await waitFor(page, () => document.querySelector('.studio-dialog[data-dialog=export] select[name=preset]').options.length > 5, null, 120000);
}

async function checkFits(page) {
  const fit = await state(page, () => {
    const box = document.querySelector('.studio-dialog[data-dialog=export]').getBoundingClientRect();
    const render = document.querySelector('.studio-dialog[data-dialog=export] footer [type=submit]').getBoundingClientRect();
    const hit = document.elementFromPoint(render.left + render.width / 2, render.top + render.height / 2);
    return {inside: box.top >= 0 && box.bottom <= innerHeight && box.right <= innerWidth, render: Boolean(hit?.closest('[type=submit]'))};
  });
  assert.ok(fit.inside, 'the Export dialog fits the window');
  assert.ok(fit.render, 'Render stays reachable');
}

async function queueRows(page) {
  return state(page, () => [...document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-job')].map(row =>
    ({id: row.dataset.id, name: row.querySelector('strong').textContent, state: row.querySelector('.queue-state').textContent})));
}

async function act(page, name, action) {
  await page.locator(`${queueDialog} .queue-job`, {hasText: name}).locator(`[data-action=${action}]`).first().click();
}

async function exportOptions(page) {
  return state(page, () => {
    const form = document.querySelector('.studio-dialog[data-dialog=export] form');
    return [...form.querySelectorAll('select')].filter(s => !s.closest('label')?.hidden).map(s => s.name);
  });
}

async function main() {
  const temp = fs.mkdtempSync(path.join(process.env.SPLICE_TEMP_DIR || os.tmpdir(), 'splice-dq-'));
  const out = path.join(temp, 'Exports');
  fs.mkdirSync(out);
  const scratch = path.join(temp, 'scratch');
  const env = {HOME: temp, XDG_DATA_HOME: temp, SPLICE_NO_REVEAL: '1', SPLICE_EXPORT_DIR: out, SPLICE_TEMP_DIR: scratch};
  let splice = await startSplice({root, env});
  const browser = await browserFor(splice);
  try {
    const clip = path.join(temp, 'clip.mp4');
    ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=25:d=4', '-f', 'lavfi', '-i', 'sine=f=440:d=4', '-shortest', '-c:v', 'libx264',
      '-pix_fmt', 'yuv420p', '-c:a', 'aac', clip]);
    let {page, errors} = await newPage(browser, splice, {width: 1440, height: 900}, ['Client review']);
    await project(page, clip);
    await page.route('**/api/delivery/capabilities', async route => {
      const response = await route.fetch();
      const capabilities = await response.json();
      const key = Object.keys(capabilities.machine.variants).find(key => key.startsWith('h264|high422|') && key.endsWith('|mp4|'));
      assert.ok(key);
      capabilities.machine.variants[key] = 'profile probe failed';
      const hdr = capabilities.presets.find(item => item.id === 'youtube-uhd-hdr').options.hdr;
      Object.assign(hdr, {lightLevels: 'manual', maxCll: 600, maxFall: 200});
      await route.fulfill({response, json: capabilities});
    });
    await openExport(page);
    await checkFits(page);
    // The original default request is unchanged by the new fields.
    assert.equal(await page.locator(`${dialog} select[name=codec]`).inputValue(), 'h264');
    assert.deepEqual(await state(page, () => {
      const form = document.querySelector('.studio-dialog[data-dialog=export] form');
      return [...form.querySelectorAll('.export-format select')].filter(s => s.offsetParent !== null).map(s => s.name);
    }), ['codec', 'profile', 'container', 'encoder', 'rateControl', 'quality', 'speed', 'audioCodec']);
    // Hardware encoders this machine cannot use are offered disabled with the reason.
    const encoders = await state(page, () => [...document.querySelector('.studio-dialog[data-dialog=export] select[name=encoder]').options]
      .map(o => ({value: o.value, disabled: o.disabled, text: o.textContent})));
    assert.ok(encoders.some(o => o.value === 'software' && !o.disabled));
    for (const option of encoders.filter(o => o.value !== 'software'))
      assert.ok(!option.disabled || /unavailable: \S/.test(option.text), 'a disabled encoder says why: ' + option.text);
    console.log('Encoders: ' + encoders.map(o => o.value + (o.disabled ? ' (unavailable)' : '')).join(', '));
    await shot(page, 'delivery-export-default');

    const unsupported = page.locator(`${dialog} [name=profile] option[value=high422]`);
    assert.ok(await unsupported.isDisabled());
    assert.match(await unsupported.textContent(), /profile probe failed/);
    await page.locator(`${dialog} select[name=preset]`).selectOption('youtube-uhd-hdr');
    assert.equal(await page.locator(`${dialog} [name=hdrMaxCll]`).inputValue(), '600');
    assert.equal(await page.locator(`${dialog} [name=hdrMaxFall]`).inputValue(), '200');
    await page.locator(`${dialog} [name=codec]`).selectOption('wav');
    for (const format of ['pcm16', 'pcm32f']) {
      const check = page.waitForRequest(req => req.url().endsWith('/api/delivery/check') &&
        req.postDataJSON().options.audioCodec === format);
      await page.locator(`${dialog} [name=audioCodec]`).selectOption(format);
      assert.equal((await check).postDataJSON().options.codec, 'wav');
    }

    // A preset fills the fields and the size conversion is announced before rendering.
    await page.locator(`${dialog} select[name=preset]`).selectOption('proxy-prores');
    assert.match(await page.locator(`${dialog} .export-purpose`).textContent(), /half size with timecode and clip-name burn-ins/);
    assert.equal(await page.locator(`${dialog} select[name=codec]`).inputValue(), 'prores');
    assert.equal(await page.locator(`${dialog} select[name=profile]`).inputValue(), 'proxy');
    await waitFor(page, () => /Frame size changes from 640 × 360 to 320 × 180/.test(document.querySelector('.studio-dialog[data-dialog=export] .export-notices')?.textContent || ''));
    await page.locator(`${dialog} .export-burnins summary`).click();
    assert.equal(await page.locator(`${dialog} [name=burn-timecode]`).isChecked(), true);
    await shot(page, 'delivery-export-proxy-preset');
    // An unsupported combination is refused before a job starts.
    await page.locator(`${dialog} select[name=codec]`).selectOption('dnxhr');
    await page.locator(`${dialog} .export-output summary`).click();
    await page.locator(`${dialog} select[name=outputSize]`).selectOption('custom');
    await page.locator(`${dialog} [name=outputWidth]`).fill('200');
    await page.locator(`${dialog} [name=outputHeight]`).fill('100');
    await waitFor(page, () => /DNxHR needs a frame of at least 256 × 120/.test(document.querySelector('.studio-dialog[data-dialog=export] .export-notices')?.textContent || ''));
    await page.locator(`${dialog} .export-add-queue`).click();
    await waitFor(page, () => /DNxHR needs/.test(document.querySelector('.studio-dialog[data-dialog=export] .render-state').textContent));
    await shot(page, 'delivery-export-refused');
    await page.locator(`${dialog} select[name=outputSize]`).selectOption('');
    // Saving the current settings as a preset (named through prompt()).
    await page.locator(`${dialog} select[name=preset]`).selectOption('youtube-1080');
    await page.locator(`${dialog} select[name=preset]`).selectOption('');
    await page.locator(`${dialog} select[name=codec]`).selectOption('h264');
    await page.locator(`${dialog} .export-preset-row button`, {hasText: 'Save preset'}).click();
    await waitFor(page, () => [...document.querySelector('.studio-dialog[data-dialog=export] select[name=preset]').options].some(o => o.textContent === 'Client review'));

    // Queue: pause it, add two jobs and a batch of two presets, reorder, hold and cancel.
    await page.locator(`${dialog} .export-open-queue`).click();
    await page.locator(`${queueDialog} .queue-pause`).click();
    await waitFor(page, () => document.querySelector('.studio-dialog[data-dialog=queue] .queue-pause').textContent === 'Resume queue');
    await page.locator(`${queueDialog} footer button`).click();
    await page.locator(`${dialog} [name=outputName]`).fill('Film web');
    await page.locator(`${dialog} .export-add-queue`).click();
    await page.locator(queueDialog).waitFor();
    await page.locator(`${queueDialog} footer button`).click();
    await page.locator(`${dialog} select[name=preset]`).selectOption('broadcast-prores');
    await page.locator(`${dialog} [name=outputName]`).fill('Film master');
    await page.locator(`${dialog} .export-add-queue`).click();
    await page.locator(queueDialog).waitFor();
    await page.locator(`${queueDialog} footer button`).click();
    await page.locator(`${dialog} [name=codec]`).selectOption('h264');
    await page.locator(`${dialog} [name=speed]`).selectOption('fast');
    await page.locator(`${dialog} [name=outputName]`).fill('Film');
    await page.locator(`${dialog} .export-batch`).click();
    for (const name of ['Archive master (ProRes 4444 XQ)', 'Web AV1 (small file)'])
      await page.locator('.studio-dialog[data-dialog=batch] label', {hasText: name}).locator('input').check();
    await shot(page, 'delivery-batch');
    await page.locator('.studio-dialog[data-dialog=batch] button.primary').click();
    await page.locator(queueDialog).waitFor();
    await waitFor(page, () => document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-job').length === 4);
    let rows = await queueRows(page);
    // A batch queues in the preset list's order.
    assert.deepEqual(rows.map(r => r.name), ['Film web', 'Film master', 'Film - Web AV1 (small file)', 'Film - Archive master (ProRes 4444 XQ)']);
    assert.ok(rows.every(r => r.state === 'Waiting'));
    await act(page, 'Film master', 'up');
    await waitFor(page, () => document.querySelector('.studio-dialog[data-dialog=queue] .queue-job strong').textContent === 'Film master');
    await act(page, 'Web AV1', 'hold');
    await act(page, 'Archive master', 'cancel');
    await waitFor(page, () => [...document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-state')].map(n => n.textContent).join() === 'Waiting,Waiting,Held,Cancelled');
    await shot(page, 'delivery-queue-waiting');

    // Reload: the queue is the server's, so it is all still there.
    await page.reload();
    await page.evaluate(() => SpliceSession.ready);
    await page.evaluate(() => SpliceDelivery.queuePanel());
    await waitFor(page, () => document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-job').length === 4);
    rows = await queueRows(page);
    assert.deepEqual(rows.map(r => [r.name, r.state]), [['Film master', 'Waiting'], ['Film web', 'Waiting'],
      ['Film - Web AV1 (small file)', 'Held'], ['Film - Archive master (ProRes 4444 XQ)', 'Cancelled']]);
    console.log('Queue kept its order, held and cancelled jobs across a page reload.');

    // Run: the two waiting jobs render one after another; retry the cancelled one, release the held one.
    await page.locator(`${queueDialog} .queue-pause`).click();
    await waitFor(page, () => [...document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-state')].slice(0, 2).every(n => n.textContent === 'Done'), null, 180000);
    await act(page, 'Archive master', 'retry');
    await act(page, 'Web AV1', 'release');
    await waitFor(page, () => [...document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-state')].every(n => n.textContent === 'Done'), null, 240000);
    await shot(page, 'delivery-queue-done');
    const files = fs.readdirSync(out).sort();
    assert.deepEqual(files, ['Film - Archive master (ProRes 4444 XQ).mov', 'Film - Web AV1 (small file).mp4', 'Film master.mov', 'Film web.mp4']);
    const master = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', path.join(out, 'Film master.mov')])).streams;
    assert.ok(master.some(s => s.codec_name === 'prores' && s.profile === 'HQ'));
    assert.ok(master.some(s => s.codec_name === 'pcm_s24le'));
    assert.equal(master.find(s => s.codec_type === 'data')?.tags?.timecode, '01:00:00:00');
    await act(page, 'Film master', 'reveal');
    await waitFor(page, () => /Film master\.mov/.test(document.querySelector('.studio-dialog[data-dialog=queue] .render-state').textContent));
    const report = await page.locator(`${queueDialog} .queue-job`, {hasText: 'Film web'}).textContent();
    assert.match(report, /Decoded audio peak .* dBTP/);
    assert.match(report, /Timecode starts 01:00:00:00/);
    console.log('Queue rendered ' + files.join(', '));
    assert.deepEqual(errors, []);
    await page.context().close();

    // Server killed mid-render: the job becomes interrupted, waiting jobs stay, nothing is left running.
    ({page, errors} = await newPage(browser, splice, {width: 1180, height: 780}, []));
    await project(page, clip, 120);
    await openExport(page);
    await checkFits(page);
    await shot(page, 'delivery-export-default');
    await page.locator(`${dialog} select[name=preset]`).selectOption('archive');
    await page.locator(`${dialog} .export-output summary`).click();
    await page.locator(`${dialog} [name=outputName]`).fill('Long master');
    await page.locator(`${dialog} .export-add-queue`).click();
    await page.locator(queueDialog).waitFor();
    await page.locator(`${queueDialog} footer button`).click();
    await page.locator(`${dialog} select[name=preset]`).selectOption('youtube-1080');
    await page.locator(`${dialog} [name=outputName]`).fill('Waiting job');
    await page.locator(`${dialog} .export-add-queue`).click();
    await waitFor(page, () => document.querySelector('.studio-dialog[data-dialog=queue] .queue-job.state-rendering progress')?.value > 0.02, null, 120000);
    await shot(page, 'delivery-queue-rendering');
    const port = new URL(splice.url).port;
    splice.process.kill('SIGKILL');
    await new Promise(resolve => splice.process.once('exit', resolve));
    // The export worker notices its server is gone and takes its FFmpeg with it.
    const leftovers = () => {
      try { return execFileSync('pgrep', ['-f', temp]).toString().trim(); } catch { return ''; }
    };
    for (let i = 0; i < 100 && leftovers(); i++) await new Promise(r => setTimeout(r, 100));
    assert.equal(leftovers(), '', 'no worker or FFmpeg outlives the killed server');
    splice = await startSplice({root, env, port});
    await splice.open(page);
    await page.evaluate(() => SpliceDelivery.queuePanel());
    await waitFor(page, () => document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-job').length === 6);
    await waitFor(page, () => [...document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-job')].some(row => /Interrupted/.test(row.textContent)));
    await shot(page, 'delivery-queue-interrupted');
    const interrupted = await page.locator(`${queueDialog} .queue-job`, {hasText: 'Long master'}).textContent();
    assert.match(interrupted, /Interrupted.*Splice stopped while this job was rendering/);
    const leftoverScratch = fs.existsSync(scratch) ? fs.readdirSync(scratch).filter(name => name.startsWith('splice-')) : [];
    assert.deepEqual(leftoverScratch, [], 'the restarted server removed the killed job’s scratch');
    // The waiting job runs after the restart; the interrupted one renders again on retry (then cancel it).
    await waitFor(page, () => [...document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-job')].some(row => /Waiting job/.test(row.textContent) && /Done/.test(row.textContent)), null, 180000);
    await act(page, 'Long master', 'retry');
    await waitFor(page, () => document.querySelector('.studio-dialog[data-dialog=queue] .queue-job.state-rendering'), null, 60000);
    await act(page, 'Long master', 'cancel');
    await waitFor(page, () => [...document.querySelectorAll('.studio-dialog[data-dialog=queue] .queue-job')].some(row => /Long master/.test(row.textContent) && /Cancelled/.test(row.textContent)), null, 60000);
    assert.ok(!fs.existsSync(path.join(out, 'Long master.mov')), 'nothing half-written in the output folder');
    console.log('Server kill: the job was interrupted, the waiting job ran after restart, retry and cancel work.');
    assert.deepEqual(errors, []);
    await page.context().close();

    // The Export dialog at the smallest supported window.
    ({page, errors} = await newPage(browser, splice, {width: 1024, height: 640}, []));
    await project(page, clip);
    await openExport(page);
    await checkFits(page);
    await shot(page, 'delivery-export-small');
    await page.locator(`${dialog} .export-open-queue`).click();
    await page.locator(queueDialog).waitFor();
    await shot(page, 'delivery-queue-small');
    assert.deepEqual(errors, []);
    console.log('Delivery queue passed.');
  } finally {
    await browser.close().catch(() => {});
    await splice.stop();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
