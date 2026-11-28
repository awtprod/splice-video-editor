/* Camera RAW through multicam, nested sequences, subclips and bins, in the browser.
 *
 * A 12-frame CinemaDNG sequence and a plain H.264 camera are linked in place.
 * - Multicam: with the RAW camera as the active angle, the Camera RAW Inspector appears on the
 *   multicam clip, Exposure +1 / 7200 K re-decode the angle in the Program Monitor, and the ProRes
 *   and H.264 exports both match the monitor (the H.264 conform leaves the BT.709 intermediate alone).
 * - Nested: the RAW clip nested into a sequence previews (server-rendered nest) and exports with its
 *   settings; stepping in shows the Inspector on the inner clip; Unnest returns its rawSettings intact.
 * - Subclip: a subclip of the RAW media keeps the RAW facts (format, camera, rawEstimated), its clip
 *   keeps rawSettings and previews from the original; the media list shows and searches the RAW
 *   format and camera model.
 * Screenshots of the RAW Inspector beside the monitor header and Multicam toggle at 1440x900,
 * 1180x780 and 1024x640 go to SPLICE_PROOF_DIR when it is set. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage} = require('./browser-harness.cjs');

const root = path.resolve(__dirname, '..');
const WIDTH = 128, HEIGHT = 96, FPS = 24;
const PARITY_LIMIT = 2, H264_LIMIT = 3, NEST_LIMIT = 3;
const proofDir = process.env.SPLICE_PROOF_DIR || '';
const SETTLE = {timeout: 90000};

function fixtures(directory) {
  const script = [
    'import json, sys',
    'from pathlib import Path',
    'from tests import raw_fixtures',
    'base = Path(sys.argv[1])',
    `first = raw_fixtures.write_cinemadng_sequence(base / 'C001_CARD', prefix='C001_', frames=12, fps=${FPS},`
      + ` bit_depth=12, width=${WIDTH}, height=${HEIGHT}, with_wav=True, reel='C001', timecode=(1, 0, 0, 0))`,
    "print(json.dumps({'card': str(first.parent)}))",
  ].join('\n');
  const result = JSON.parse(execFileSync('python3', ['-c', script, directory], {cwd: root, encoding: 'utf8'}).trim());
  result.camB = path.join(directory, 'CAMB');
  fs.mkdirSync(result.camB, {recursive: true});
  execFileSync('ffmpeg', ['-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i', `color=c=0x204060:s=${WIDTH}x${HEIGHT}:r=${FPS}:d=0.5`,
    '-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000:duration=0.5', '-shortest', '-c:v', 'libx264',
    '-pix_fmt', 'yuv420p', '-c:a', 'aac', path.join(result.camB, 'CAMB_0001.mp4')]);
  return result;
}

function patchBox(width, height, col, row) {
  const cw = Math.floor(width / 3), ch = Math.floor(height / 2);
  return {x0: col * cw + Math.floor(cw / 4), x1: (col + 1) * cw - Math.floor(cw / 4),
          y0: row * ch + Math.max(6, Math.floor(ch / 4)), y1: (row + 1) * ch - Math.floor(ch / 4)};
}

function exportedPatch(file, frame, col, row) {
  const raw = execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-i', file,
    '-vf', `select=eq(n\\,${frame}),setparams=range=tv,zscale,format=gbrp`,
    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']);
  const {x0, x1, y0, y1} = patchBox(WIDTH, HEIGHT, col, row);
  const sum = [0, 0, 0];
  let count = 0;
  for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
    for (let c = 0; c < 3; c++) sum[c] += raw[(y * WIDTH + x) * 3 + c];
    count++;
  }
  return sum.map(value => value / count);
}

async function monitorPatch(page, col, row) {
  await page.waitForFunction(() => [...document.querySelectorAll('body > video')].filter(el => el.getAttribute('src'))
    .every(el => el.readyState >= 2 && !el.seeking));
  await page.waitForTimeout(150);
  return page.evaluate(box => {
    const canvas = document.getElementById('preview-canvas');
    Editor.drawFrame(canvas);
    const scaleX = canvas.width / box.width, scaleY = canvas.height / box.height;
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const sum = [0, 0, 0];
    let count = 0;
    for (let y = Math.ceil(box.y0 * scaleY); y < Math.floor(box.y1 * scaleY); y++) {
      for (let x = Math.ceil(box.x0 * scaleX); x < Math.floor(box.x1 * scaleX); x++) {
        for (let c = 0; c < 3; c++) sum[c] += data[(y * canvas.width + x) * 4 + c];
        count++;
      }
    }
    return sum.map(value => value / count);
  }, {...patchBox(WIDTH, HEIGHT, col, row), width: WIDTH, height: HEIGHT});
}

async function allPatches(page) {
  const out = [];
  for (let row = 0; row < 2; row++) for (let col = 0; col < 3; col++) out.push(await monitorPatch(page, col, row));
  return out;
}
const fileAllPatches = (file, frame) => [0, 1].flatMap(row => [0, 1, 2].map(col => exportedPatch(file, frame, col, row)));
const worstDelta = (a, b) => Math.max(...a.flatMap((patch, i) => patch.map((v, c) => Math.abs(v - b[i][c]))));

// The clip's RAW re-decode (of the media it actually plays: an angle, a subclip's original) is on screen.
async function decoded(page, clipId) {
  await page.waitForFunction(id => {
    const state = Editor.get();
    const clip = state.project.clips.find(c => c.id === id);
    const source = clip && Editor.resolveClipMedia(clip).item;
    const job = source && SpliceMedia.rawPreviewStatus(source, clip.rawSettings);
    if (!job || job.pending) return false;
    if (job.error) throw new Error(job.error);
    return [...document.querySelectorAll('body > video')].some(el => el.getAttribute('src') === job.src && el.readyState >= 2);
  }, clipId, SETTLE);
}

async function setRange(page, field, value) {
  await page.locator(`[data-raw-field="${field}"]`).fill(String(value));
  await page.locator(`[data-raw-field="${field}"]`).dispatchEvent('change');
}

async function linkInPlace(page, folder, name) {
  await page.locator('.file-menu summary').click();
  await page.locator('#link-media').click();
  await page.locator('#link-location').fill(folder);
  await page.locator('#link-location').press('Enter');
  const row = page.locator('#link-entries label', {hasText: name});
  await row.waitFor();
  await row.locator('input').check();
  await page.locator('#link-import').click();
  await page.locator('dialog.splice-link').waitFor({state: 'detached', timeout: 120000});
}

async function exportFile(page, splice, options, name) {
  const job = await page.evaluate(options => Editor.exportVideo(options), options);
  const file = path.join(splice.temp, name);
  fs.writeFileSync(file, await (await page.request.get(new URL(job.downloadURL, splice.url).href)).body());
  return file;
}

const within = (rect, viewport) => rect.width > 0 && rect.left >= -1 && rect.top >= -1
  && rect.right <= viewport.width + 1 && rect.bottom <= viewport.height + 1;

async function main() {
  const splice = await startSplice('splice-raw-editorial-');
  if (proofDir) fs.mkdirSync(proofDir, {recursive: true});
  const report = {};
  try {
    const media = fixtures(splice.temp);
    const {page, errors} = await openPage(splice, {width: 1440, height: 900});
    page.setDefaultTimeout(30000);
    await page.evaluate(({w, h, fps}) => SpliceSession.newProject({name: 'RAW editorial', width: w, height: h, fps}),
      {w: WIDTH, h: HEIGHT, fps: FPS});
    await linkInPlace(page, media.card, 'C001_000001.dng');
    await linkInPlace(page, media.camB, 'CAMB_0001.mp4');
    const raw = await page.waitForFunction(() => Editor.get().project.media.find(m => m.rawFormat === 'cinemadng')).then(h => h.jsonValue());
    const camB = await page.waitForFunction(() => Editor.get().project.media.find(m => m.name === 'CAMB_0001.mp4')).then(h => h.jsonValue());

    // Reference: the RAW clip cut straight into the timeline, Exposure +1 and 7200 K, full debayer.
    const settings = {decodeUsing: 'clip', iso: 800, exposure: 1, kelvin: 7200, tint: 0, colorSpace: 'rec709',
                      gamma: 'rec709', highlightRecovery: false, debayer: 'full'};
    await page.evaluate(({id, settings}) => {
      Editor.editProject(project => {
        project.clips = [{id: 'plain', mediaId: id, type: 'video', track: 1, start: 0, duration: 0.5, in: 0,
                          color: {}, volume: 100, transition: 'none', title: '', rawSettings: settings}];
      });
      Editor.select('plain');
      Editor.seek(5 / 24);
    }, {id: raw.id, settings});
    await decoded(page, 'plain');
    const reference = await allPatches(page);

    // ---- Multicam: the RAW camera as an angle --------------------------------------------------
    await page.evaluate(({camB, raw}) => {
      Editor.editProject(project => { project.clips = []; });
      const mc = SpliceMulticam.createMulticam({name: 'MC RAW', angleMediaIds: [camB, raw], audioMode: 'follow'});
      Editor.addToTimeline(mc.id);
      const clip = Editor.get().project.clips.find(c => c.type === 'video');
      Editor.select(clip.id);
      Editor.seek(5 / 24);
    }, {camB: camB.id, raw: raw.id});
    assert.equal(await page.locator('[data-raw-panel]').count(), 0, 'camera B is not RAW: no RAW Inspector');
    await page.locator('[data-action="mc-angle"]').selectOption('1');
    await page.waitForSelector('[data-raw-panel]');
    assert.match(await page.locator('[data-raw-format-badge]').textContent(), /^CinemaDNG 12-bit$/);
    const activeId = await page.evaluate(() => Editor.get().selected.id);
    await page.locator('[data-raw-field="debayer"]').selectOption('full');
    await decoded(page, activeId);
    const mcBefore = await monitorPatch(page, 0, 0);
    await setRange(page, 'exposure', 1);
    await setRange(page, 'kelvin', 7200);
    await decoded(page, activeId);
    const mcState = await page.evaluate(id => {
      const state = Editor.get(), clip = state.project.clips.find(c => c.id === id);
      return {settings: clip.rawSettings, angle: clip.multicamAngle, media: state.project.media.find(m => m.id === clip.mediaId).multicam ? 'multicam' : 'other',
              rawMediaTouched: 'rawSettings' in state.project.media.find(m => m.rawFormat === 'cinemadng')};
    }, activeId);
    assert.deepEqual([mcState.media, mcState.angle], ['multicam', 1], 'settings live on the multicam clip');
    assert.deepEqual([mcState.settings.decodeUsing, mcState.settings.exposure, mcState.settings.kelvin, mcState.settings.debayer],
      ['clip', 1, 7200, 'full']);
    assert.equal(mcState.rawMediaTouched, false);
    const mcPatches = await allPatches(page);
    assert.ok(mcPatches[0][1] > mcBefore[1] + 25, `+1 stop brightens the angle: ${mcBefore} -> ${mcPatches[0]}`);
    const mcVsPlain = worstDelta(mcPatches, reference);
    assert.ok(mcVsPlain <= 2, `multicam angle monitor vs plain RAW clip: ${mcVsPlain}`);

    const mcProres = await exportFile(page, splice, {codec: 'prores', profile: 'hq', limiter: false}, 'mc.mov');
    const mcH264 = await exportFile(page, splice, {codec: 'h264', limiter: false}, 'mc.mp4');
    const proresPatches = fileAllPatches(mcProres, 5), h264Patches = fileAllPatches(mcH264, 5);
    report.multicam = {monitorVsPlain: mcVsPlain, monitorVsProres: worstDelta(mcPatches, proresPatches),
                       h264VsProres: worstDelta(h264Patches, proresPatches)};
    assert.ok(report.multicam.monitorVsProres <= PARITY_LIMIT, `multicam monitor vs ProRes: ${JSON.stringify(report.multicam)}`);
    assert.ok(report.multicam.h264VsProres <= H264_LIMIT, `multicam H.264 vs ProRes (double conform?): ${JSON.stringify(report.multicam)}`);

    // Screenshots: RAW Inspector beside the monitor header and Multicam toggle.
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
      await page.setViewportSize(viewport);
      await page.waitForTimeout(200);
      await page.locator('[data-raw-panel]').scrollIntoViewIfNeeded();
      const boxes = await page.evaluate(() => {
        const box = el => el ? el.getBoundingClientRect().toJSON() : null;
        const panel = document.querySelector('[data-raw-panel]');
        return {panel: box(panel), overflow: panel.scrollWidth - panel.clientWidth,
                toggle: box(document.getElementById('multicam-toggle'))};
      });
      assert.ok(boxes.panel.left >= -1 && boxes.panel.right <= viewport.width + 1 && boxes.overflow <= 1,
        `RAW panel fits at ${viewport.width}x${viewport.height}: ${JSON.stringify(boxes)}`);
      assert.ok(within(boxes.toggle, viewport), `Multicam toggle visible at ${viewport.width}x${viewport.height}: ${JSON.stringify(boxes)}`);
      const overlap = (a, b) => !(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top);
      assert.ok(!overlap(boxes.toggle, boxes.panel), `Multicam toggle clear of the RAW panel at ${viewport.width}x${viewport.height}`);
      if (proofDir) await page.screenshot({path: path.join(proofDir, `raw-multicam-inspector-${viewport.width}x${viewport.height}.png`)});
    }
    await page.setViewportSize({width: 1440, height: 900});

    // ---- Nested sequence holding the RAW clip ----------------------------------------------------
    const nest = await page.evaluate(({id, settings}) => {
      Editor.editProject(project => {
        project.clips = [{id: 'inner', mediaId: id, type: 'video', track: 1, start: 0, duration: 0.5, in: 0,
                          color: {}, volume: 100, transition: 'none', title: '', rawSettings: settings}];
      });
      return SpliceNested.nestSelection(['inner'], {name: 'RAW Nest'});
    }, {id: raw.id, settings});
    const nestClip = nest.clipIds[0];
    await page.evaluate(id => { Editor.select(id); Editor.seek(5 / 24); }, nestClip);
    await page.waitForFunction(() => {
      const state = Editor.get();
      const item = state.project.media.find(m => m.nestedProject);
      if (item?.proxyState === 'unavailable') throw new Error(item.proxyError);
      return item?.src && [...document.querySelectorAll('body > video')].some(el => el.getAttribute('src') === item.src && el.readyState >= 2);
    }, null, SETTLE);
    const nestPatches = await allPatches(page);
    const nestVsPlain = worstDelta(nestPatches, reference);
    assert.ok(nestVsPlain <= NEST_LIMIT, `nested RAW preview vs plain RAW clip: ${nestVsPlain}`);
    const nestProres = await exportFile(page, splice, {codec: 'prores', profile: 'hq', limiter: false}, 'nest.mov');
    const nestH264 = await exportFile(page, splice, {codec: 'h264', limiter: false}, 'nest.mp4');
    report.nested = {previewVsPlain: nestVsPlain, proresVsPlain: worstDelta(fileAllPatches(nestProres, 5), reference),
                     h264VsProres: worstDelta(fileAllPatches(nestH264, 5), fileAllPatches(nestProres, 5))};
    assert.ok(report.nested.proresVsPlain <= PARITY_LIMIT + 1, `nested ProRes vs plain RAW monitor: ${JSON.stringify(report.nested)}`);
    assert.ok(report.nested.h264VsProres <= H264_LIMIT, `nested H.264 vs ProRes: ${JSON.stringify(report.nested)}`);
    // Inside the nest, the inner clip shows the RAW Inspector with its settings.
    const nestedMediaId = await page.evaluate(() => Editor.get().project.media.find(m => m.nestedProject).id);
    await page.evaluate(async id => { await SpliceNested.enter(id); Editor.select(Editor.get().project.clips.find(c => c.type === 'video').id); }, nestedMediaId);
    await page.waitForSelector('[data-raw-panel]');
    assert.equal(await page.locator('[data-raw-field="kelvin"]').inputValue(), '7200');
    await page.evaluate(() => SpliceNested.exit());
    await page.waitForFunction(() => !!Editor.get().project.media.find(m => m.nestedProject));
    // Unnest returns the inner clip with its rawSettings intact.
    const unnested = await page.evaluate(() => {
      const outer = Editor.get().project.clips.find(c => Editor.get().project.media.find(m => m.id === c.mediaId)?.nestedProject && c.type === 'video');
      SpliceNested.unnestClip(outer.id);
      const state = Editor.get();
      return state.project.clips.filter(c => c.type === 'video').map(c => ({raw: state.project.media.find(m => m.id === c.mediaId)?.raw === true,
                                                                            settings: c.rawSettings}));
    });
    assert.equal(unnested.length, 1);
    assert.equal(unnested[0].raw, true);
    assert.deepEqual(unnested[0].settings, settings, 'Unnest carries the RAW clip settings intact');

    // ---- Subclip of the RAW media ---------------------------------------------------------------
    const sub = await page.evaluate(({id, settings}) => {
      const item = SpliceBins.createSubclip({parentMediaId: id, name: 'C001 hero', subclipIn: 3 / 24, subclipOut: 9 / 24});
      Editor.editProject(project => {
        project.clips = [{id: 'subclip', mediaId: item.id, type: 'video', track: 1, start: 0, duration: 6 / 24, in: 0,
                          color: {}, volume: 100, transition: 'none', title: '', rawSettings: settings}];
      });
      Editor.select('subclip');
      Editor.seek(2 / 24);
      return Editor.get().project.media.find(m => m.id === item.id);
    }, {id: raw.id, settings});
    for (const key of ['raw', 'rawFormat', 'rawFormatLabel', 'rawEstimated', 'rawMetadata', 'rawDefaults', 'rawDecoderAvailable'])
      assert.deepEqual(sub[key], raw[key], `subclip keeps ${key}`);
    assert.equal(sub.reel, 'C001');
    await page.waitForSelector('[data-raw-panel]');
    assert.equal(await page.locator('[data-raw-field="kelvin"]').inputValue(), '7200', 'the subclip clip keeps rawSettings');
    await decoded(page, 'subclip');
    const subPatches = await allPatches(page);
    report.subclip = {monitorVsPlain: worstDelta(subPatches, reference)};
    assert.ok(report.subclip.monitorVsPlain <= 2, `subclip preview vs plain: ${JSON.stringify(report.subclip)}`);
    // A save round trip keeps both.
    const download = page.waitForEvent('download');
    await page.evaluate(() => Editor.saveProject('splice'));
    const saved = JSON.parse(fs.readFileSync(await (await download).path(), 'utf8'));
    const savedSub = saved.media.find(m => m.id === sub.id);
    assert.deepEqual(savedSub.rawEstimated, raw.rawEstimated);
    assert.deepEqual(saved.clips.find(c => c.id === 'subclip').rawSettings, settings);

    // Bins and metadata search: the RAW format and camera model are shown and searchable.
    const camera = raw.rawMetadata?.camera;
    assert.ok(camera, 'fixture records a camera model');
    for (const term of ['CinemaDNG', camera]) {
      await page.locator('#media-search').fill(term);
      await page.waitForTimeout(250);
      const rows = await page.locator('#library-body .media-card').allInnerTexts();
      assert.equal(rows.length, 2, `search "${term}" finds the RAW media and its subclip: ${rows}`);
      assert.ok(rows.every(text => text.includes('CinemaDNG')), `rows show the RAW format: ${rows}`);
    }
    await page.locator('#media-search').fill('CAMB');
    await page.waitForTimeout(250);
    assert.equal(await page.locator('#library-body .media-card').count(), 1);
    await page.locator('#media-search').fill('');

    assert.deepEqual(errors, []);
    if (proofDir) fs.writeFileSync(path.join(proofDir, 'raw-editorial-parity.json'), JSON.stringify(report, null, 2));
    console.log(`Camera RAW editorial: multicam angle, nested sequence and subclip develop with their rawSettings `
      + `(monitor/export deltas ${JSON.stringify(report)}); Unnest keeps settings; bins search RAW format and camera.`);
  } finally {
    await splice.stop();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
