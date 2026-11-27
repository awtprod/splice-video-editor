/* Camera RAW in the browser, end to end, on synthetic media only.
 *
 * A 12-bit CinemaDNG sequence (12 frames, colour patches, timecode, reel, sidecar WAV) is linked
 * through File > Link media in place, where the sequence is listed once. In the Camera RAW
 * Inspector, Preview Debayer Full, Exposure +1 and White Balance 7200 K re-decode the clip; the
 * Program Monitor is then compared patch by patch with the decoded 10-bit ProRes 422 HQ export of
 * the same frame (at most 2 RGB codes apart). Undo/redo, save/reopen, Reset to Camera Metadata
 * and a disabled RAW clip are checked, a .braw without a decoder shows the RAW Decoders prompt,
 * and the RAW Decoders dialog is laid out and photographed at 1440x900, 1180x780 and 1024x640.
 * Screenshots and the parity table go to SPLICE_PROOF_DIR when it is set. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage} = require('./browser-harness.cjs');

const root = path.resolve(__dirname, '..');
const WIDTH = 128, HEIGHT = 96, FPS = 24;
const PARITY_LIMIT = 2;
const proofDir = process.env.SPLICE_PROOF_DIR || '';

function fixtures(directory) {
  const script = [
    'import json, sys',
    'from pathlib import Path',
    'from tests import raw_fixtures',
    'base = Path(sys.argv[1])',
    `first = raw_fixtures.write_cinemadng_sequence(base / 'A001_CARD', prefix='A001_', frames=12, fps=${FPS},`
      + ` bit_depth=12, width=${WIDTH}, height=${HEIGHT}, with_wav=True, reel='A001', timecode=(1, 0, 0, 0))`,
    "braw = raw_fixtures.write_synthetic_braw(base / 'B001_CARD' / 'B001_C001.braw', width=160, height=90, fps=24,"
      + " duration=0.5, reel='B001', timecode='02:00:00:00', with_audio=True, with_sidecar=True)",
    // No .sidecar: ISO and white balance are not in the file, so they are placeholders.
    "bare = raw_fixtures.write_synthetic_braw(base / 'B002_CARD' / 'B002_C001.braw', width=160, height=90, fps=24,"
      + " duration=0.5, reel='B002', timecode='03:00:00:00', with_audio=True, with_sidecar=False)",
    "print(json.dumps({'first': str(first), 'card': str(first.parent), 'brawCard': str(braw.parent),"
      + " 'bareCard': str(bare.parent)}))",
  ].join('\n');
  return JSON.parse(execFileSync('python3', ['-c', script, directory], {cwd: root, encoding: 'utf8'}).trim());
}

// Patch (col 0..2, row 0..1) interior, away from the frame-marker rows at the top.
function patchBox(width, height, col, row) {
  const cw = Math.floor(width / 3), ch = Math.floor(height / 2);
  return {x0: col * cw + Math.floor(cw / 4), x1: (col + 1) * cw - Math.floor(cw / 4),
          y0: row * ch + Math.max(6, Math.floor(ch / 4)), y1: (row + 1) * ch - Math.floor(ch / 4)};
}

// Decoded as a player does (as color-parity.cjs): exact BT.709 video-range conversion through
// zscale; FFmpeg's default packed-RGB conversion would use another matrix.
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
  await page.waitForTimeout(120);
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

// The clip's current RAW re-decode has finished and its proxy is what the monitor shows.
async function decoded(page, clipId) {
  await page.waitForFunction(id => {
    const state = Editor.get();
    const clip = state.project.clips.find(c => c.id === id);
    const source = clip && state.project.media.find(m => m.id === clip.mediaId);
    const job = source && SpliceMedia.rawPreviewStatus(source, clip.rawSettings);
    if (!job || job.pending) return false;
    if (job.error) throw new Error(job.error);
    return [...document.querySelectorAll('body > video')].some(el => el.getAttribute('src') === job.src && el.readyState >= 2);
  }, clipId, {timeout: 60000});
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
  const listed = await page.locator('#link-entries label').allInnerTexts();
  await row.locator('input').check();
  await page.locator('#link-import').click();
  await page.locator('dialog.splice-link').waitFor({state: 'detached', timeout: 120000});
  return listed;
}

function within(rect, viewport) {
  return rect.left >= -1 && rect.top >= -1 && rect.right <= viewport.width + 1 && rect.bottom <= viewport.height + 1;
}

async function main() {
  const splice = await startSplice('splice-camera-raw-');
  if (proofDir) fs.mkdirSync(proofDir, {recursive: true});
  try {
    const media = fixtures(splice.temp);
    const {page, errors} = await openPage(splice, {width: 1440, height: 900});
    page.setDefaultTimeout(30000);
    await page.evaluate(({w, h, fps}) => SpliceSession.newProject({name: 'Camera RAW', width: w, height: h, fps}),
      {w: WIDTH, h: HEIGHT, fps: FPS});

    // The picker lists the 12-frame sequence once, by its first frame.
    const listed = await linkInPlace(page, media.card, 'A001_000001.dng');
    const dngRows = listed.filter(text => /\.dng/i.test(text));
    assert.equal(dngRows.length, 1, `one row for the sequence: ${listed}`);
    assert.match(dngRows[0], /12 frames/);
    const item = await page.waitForFunction(() => Editor.get().project.media.find(m => m.rawFormat === 'cinemadng')).then(h => h.jsonValue());
    assert.equal(item.raw, true);
    assert.equal(item.linked, true);
    assert.equal(item.timecode, '01:00:00:00');
    assert.equal(item.reel, 'A001');
    assert.equal(item.audioStreams, 1, 'the sidecar WAV is the clip sound');
    assert.ok(Math.abs(item.duration - 12 / FPS) < 1e-6);

    await page.evaluate(id => {
      Editor.editProject(project => {
        project.clips = [{id: 'raw-1', mediaId: id, type: 'video', track: 1, start: 0, duration: 12 / 24, in: 0,
                          color: {}, volume: 100, transition: 'none', title: ''}];
      });
      Editor.select('raw-1');
      Editor.seek(5 / 24);
    }, item.id);

    // The Camera RAW Inspector appears on its own for a RAW clip.
    await page.waitForSelector('[data-raw-panel]');
    assert.match(await page.locator('[data-raw-format-badge]').textContent(), /^CinemaDNG 12-bit$/);
    assert.match(await page.locator('.raw-badge-decoder').textContent(), /Built-in CinemaDNG/);
    const meta = await page.locator('.raw-camera-meta').first().textContent();
    for (const part of [/Reel A001/, /TC 01:00:00:00/, /12 frames/]) assert.match(meta, part);
    assert.equal(await page.locator('[data-raw-estimated]').count(), 0, 'every field came from the DNG tags');

    await page.locator('[data-raw-field="debayer"]').selectOption('full');
    await decoded(page, 'raw-1');
    const before = await monitorPatch(page, 0, 0);

    await setRange(page, 'exposure', 1);
    await setRange(page, 'kelvin', 7200);
    await decoded(page, 'raw-1');
    const settings = await page.evaluate(() => Editor.get().project.clips[0].rawSettings);
    assert.deepEqual([settings.decodeUsing, settings.exposure, settings.kelvin, settings.debayer], ['clip', 1, 7200, 'full']);
    const after = await monitorPatch(page, 0, 0);
    assert.ok(after[1] > before[1] + 25, `+1 stop brightens grey: ${before} -> ${after}`);
    assert.ok(after[0] / after[2] > before[0] / before[2] * 1.05, `5600 K -> 7200 K warms grey: ${before} -> ${after}`);
    if (proofDir) await page.locator('#preview-canvas').screenshot({path: path.join(proofDir, 'monitor-exposure+1-7200K.png')});

    // The decoded ProRes export of frame 5 matches the monitor, patch by patch.
    const job = await page.evaluate(() => Editor.exportVideo({codec: 'prores', profile: 'hq', limiter: false}));
    const exported = path.join(splice.temp, 'raw-export.mov');
    fs.writeFileSync(exported, await (await page.request.get(new URL(job.downloadURL, splice.url).href)).body());
    const parity = [];
    for (let row = 0; row < 2; row++) for (let col = 0; col < 3; col++) {
      const monitor = await monitorPatch(page, col, row);
      const file = exportedPatch(exported, 5, col, row);
      const delta = Math.max(...monitor.map((value, c) => Math.abs(value - file[c])));
      parity.push({patch: `${col},${row}`, monitor: monitor.map(v => +v.toFixed(2)), export: file.map(v => +v.toFixed(2)),
                   delta: +delta.toFixed(2)});
    }
    if (proofDir) fs.writeFileSync(path.join(proofDir, 'monitor-vs-prores.json'), JSON.stringify(parity, null, 2));
    const worst = Math.max(...parity.map(entry => entry.delta));
    assert.ok(worst <= PARITY_LIMIT, `monitor vs ProRes export: ${JSON.stringify(parity)}`);

    // Shown in the Color workspace too.
    await page.locator('[data-workspace="color"]').click();
    await page.waitForSelector('[data-raw-panel]');
    if (proofDir) await page.screenshot({path: path.join(proofDir, 'color-workspace-raw-inspector.png')});
    await page.locator('[data-workspace="edit"]').click();
    await page.waitForSelector('[data-raw-panel]');

    // Undo and redo step through the RAW changes.
    await page.evaluate(() => Editor.undo());
    assert.equal(await page.evaluate(() => Editor.get().project.clips[0].rawSettings.kelvin) === 7200, false);
    assert.equal(await page.evaluate(() => Editor.get().project.clips[0].rawSettings.exposure), 1);
    await page.evaluate(() => Editor.redo());
    assert.equal(await page.evaluate(() => Editor.get().project.clips[0].rawSettings.kelvin), 7200);

    // Save and reopen.
    const download = page.waitForEvent('download');
    await page.evaluate(() => Editor.saveProject('splice'));
    const json = fs.readFileSync(await (await download).path(), 'utf8');
    await page.evaluate(async text => {
      await Editor.openProject(new File([text], 'camera-raw.splice.json'));
      Editor.select('raw-1');
      Editor.seek(5 / 24);
    }, json);
    await page.waitForSelector('[data-raw-panel]');
    const reopened = await page.evaluate(() => Editor.get().project.clips[0].rawSettings);
    assert.deepEqual([reopened.decodeUsing, reopened.exposure, reopened.kelvin], ['clip', 1, 7200]);
    await decoded(page, 'raw-1');
    const reopenedGrey = await monitorPatch(page, 0, 0);
    assert.ok(Math.max(...reopenedGrey.map((v, c) => Math.abs(v - after[c]))) <= 1, 'reopened clip decodes the same');

    // A disabled RAW clip shows nothing; enabling it brings the picture back.
    await page.evaluate(() => { Editor.updateClip('raw-1', {enabled: false}); Editor.seek(5 / 24); });
    assert.deepEqual((await monitorPatch(page, 1, 1)).map(Math.round), [0, 0, 0]);
    await page.evaluate(() => { Editor.updateClip('raw-1', {enabled: true}); Editor.select('raw-1'); });

    // Reset to Camera Metadata, and undo it.
    await page.waitForSelector('[data-raw-panel]');
    await page.locator('[data-raw-action="reset"]').click();
    const reset = await page.evaluate(() => Editor.get().project.clips[0].rawSettings);
    assert.deepEqual([reset.decodeUsing, reset.exposure], ['camera', 0]);
    await page.evaluate(() => Editor.undo());
    assert.equal(await page.evaluate(() => Editor.get().project.clips[0].rawSettings.decodeUsing), 'clip');

    // A Blackmagic RAW clip with no decoder installed: sound, timecode and reel import; the
    // Inspector says how to add a decoder or attach a proxy.
    await linkInPlace(page, media.brawCard, 'B001_C001.braw');
    const braw = await page.waitForFunction(() => Editor.get().project.media.find(m => m.rawFormat === 'braw')).then(h => h.jsonValue());
    assert.equal(braw.rawDecoderAvailable, false);
    assert.equal(braw.timecode, '02:00:00:00');
    assert.equal(braw.audioStreams, 1);
    await page.evaluate(id => {
      Editor.editProject(project => project.clips.push({id: 'braw-1', mediaId: id, type: 'video', track: 1, start: 1,
        duration: 0.5, in: 0, color: {}, volume: 100, transition: 'none', title: ''}));
      Editor.select('braw-1');
    }, braw.id);
    const banner = await page.locator('.raw-status-banner').textContent();
    assert.match(banner, /Blackmagic RAW/);
    assert.match(banner, /RAW Decoders/);
    assert.equal(await page.locator('[data-raw-action="attach-proxy"]').count(), 1);
    if (proofDir) {
      await page.locator('.raw-status-banner').scrollIntoViewIfNeeded();
      await page.screenshot({path: path.join(proofDir, 'braw-missing-decoder.png')});
    }

    // Look metadata the file does not record is marked "estimated" on its control and named in
    // the "Not in the file" line; a decoder that cannot take a setting has that control off.
    await linkInPlace(page, media.bareCard, 'B002_C001.braw');
    const bare = await page.waitForFunction(() => Editor.get().project.media.find(m => m.reel === 'B002')).then(h => h.jsonValue());
    assert.deepEqual(bare.rawEstimated.filter(key => ['iso', 'kelvin', 'tint'].includes(key)), ['iso', 'kelvin', 'tint']);
    await page.evaluate(id => {
      Editor.editProject(project => project.clips.push({id: 'bare-1', mediaId: id, type: 'video', track: 1, start: 2,
        duration: 0.5, in: 0, color: {}, volume: 100, transition: 'none', title: '', rawSettings: {decodeUsing: 'camera'}}));
      Editor.select('bare-1');
    }, bare.id);
    await page.waitForSelector('[data-raw-panel] [data-raw-est="iso"]');
    assert.deepEqual(await page.locator('[data-raw-est]').evaluateAll(nodes => nodes.map(n => n.dataset.rawEst)), ['iso', 'kelvin', 'tint']);
    assert.match(await page.locator('[data-raw-estimated]').textContent(), /ISO, white balance, tint/);
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
      await page.setViewportSize(viewport);
      await page.waitForTimeout(150);
      const marker = page.locator('[data-raw-est="kelvin"]');
      await marker.scrollIntoViewIfNeeded();
      const boxes = await page.locator('[data-raw-panel]').evaluate(panel => {
        const box = el => el.getBoundingClientRect().toJSON();
        return {panel: box(panel), overflow: panel.scrollWidth - panel.clientWidth,
                markers: [...panel.querySelectorAll('[data-raw-est]')].map(box),
                labels: [...panel.querySelectorAll('[data-raw-est]')].map(el => {
                  const row = el.closest('label').getBoundingClientRect();
                  return el.getBoundingClientRect().right <= row.right + 1;
                })};
      });
      assert.ok(boxes.overflow <= 1, `estimated Inspector overflows at ${viewport.width}x${viewport.height}`);
      assert.ok(boxes.markers.every(m => m.width > 0 && m.left >= boxes.panel.left - 1 && m.right <= boxes.panel.right + 1),
        `estimated markers inside the panel at ${viewport.width}x${viewport.height}: ${JSON.stringify(boxes)}`);
      assert.ok(boxes.labels.every(Boolean), 'markers stay inside their control rows');
      if (proofDir) {
        await page.locator('[data-raw-estimated]').evaluate(el => el.scrollIntoView({block: 'start'}));
        await page.screenshot({path: path.join(proofDir, `raw-inspector-estimated-note-${viewport.width}x${viewport.height}.png`)});
        await page.locator('[data-raw-est="kelvin"]').evaluate(el => el.scrollIntoView({block: 'center'}));
        await page.screenshot({path: path.join(proofDir, `raw-inspector-estimated-${viewport.width}x${viewport.height}.png`)});
      }
    }
    await page.setViewportSize({width: 1440, height: 900});
    // Switching to Clip makes the values the editor's own: no longer flagged on the controls.
    await page.locator('[data-raw-field="decodeUsing"]').selectOption('clip');
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'bare-1').rawSettings.decodeUsing === 'clip');
    assert.equal(await page.locator('[data-raw-est]').count(), 0);
    assert.equal(await page.locator('[data-raw-estimated]').count(), 1, 'the "Not in the file" line stays');
    await page.evaluate(() => Editor.undo());
    // A decoder without Kelvin/tint options (dcraw_emu takes the camera's white balance only).
    await page.evaluate(id => Editor.editProject(project => {
      project.media.find(m => m.id === id).rawCapabilities = {tool: 'dcraw_emu',
        settings: ['iso', 'exposure', 'colorSpace', 'gamma', 'highlightRecovery', 'debayer'],
        colorSpaces: ['linear', 'rec2020', 'rec709'], gammas: ['linear', 'rec709'], pairs: null};
    }), bare.id);
    await page.evaluate(() => Editor.select('bare-1'));
    await page.waitForSelector('[data-raw-unsupported-note]');
    assert.equal(await page.locator('[data-raw-field="kelvin"]').isDisabled(), true);
    assert.equal(await page.locator('[data-raw-field="tint"]').isDisabled(), true);
    assert.equal(await page.locator('[data-raw-field="iso"]').isDisabled(), false);
    assert.match(await page.locator('[data-raw-unsupported-note]').textContent(), /dcraw_emu cannot take white balance, tint/);
    const offGammas = await page.locator('[data-raw-field="gamma"] option[disabled]').evaluateAll(nodes => nodes.map(n => n.value));
    assert.deepEqual(offGammas.sort(), ['bmd_extended_video'], 'gammas dcraw_emu cannot render are off (the current one stays selectable)');
    if (proofDir) {
      await page.locator('[data-raw-field="kelvin"]').evaluate(el => el.scrollIntoView({block: 'center'}));
      await page.screenshot({path: path.join(proofDir, 'raw-inspector-decoder-limits.png')});
    }

    // RAW Decoders dialog from the File menu at three window sizes; the Inspector fits as well.
    await page.evaluate(() => Editor.select('raw-1'));
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
      await page.setViewportSize(viewport);
      await page.waitForTimeout(150);
      const panel = page.locator('[data-raw-panel]');
      await panel.scrollIntoViewIfNeeded();
      const panelBox = await panel.evaluate(el => {
        const r = el.getBoundingClientRect();
        return {left: r.left, right: r.right, overflow: el.scrollWidth - el.clientWidth};
      });
      assert.ok(panelBox.left >= -1 && panelBox.right <= viewport.width + 1 && panelBox.overflow <= 1,
        `Inspector RAW panel at ${viewport.width}x${viewport.height}: ${JSON.stringify(panelBox)}`);
      if (proofDir) await page.screenshot({path: path.join(proofDir, `raw-inspector-${viewport.width}x${viewport.height}.png`)});

      await page.locator('.file-menu summary').click();
      await page.locator('#raw-decoders').click();
      await page.waitForSelector('.splice-raw-dialog[open] [data-decoder-id="braw"]');
      const dialog = page.locator('.splice-raw-dialog');
      assert.equal(await dialog.locator('[data-decoder-id]').count(), 9);
      assert.equal((await dialog.locator('[data-decoder-id="cinemadng"] .raw-decoder-state').textContent()).trim(), 'Available');
      assert.equal((await dialog.locator('[data-decoder-id="braw"] .raw-decoder-state').textContent()).trim(), 'Not installed');
      const rect = await dialog.evaluate(el => el.getBoundingClientRect().toJSON());
      assert.ok(within(rect, viewport), `dialog inside ${viewport.width}x${viewport.height}: ${JSON.stringify(rect)}`);
      for (const action of ['save', 'close']) {
        const button = await dialog.locator(`[data-raw-dialog-action="${action}"]`).evaluate(el => el.getBoundingClientRect().toJSON());
        assert.ok(within(button, viewport), `${action} button reachable at ${viewport.width}x${viewport.height}`);
      }
      if (proofDir) await page.screenshot({path: path.join(proofDir, `raw-decoders-${viewport.width}x${viewport.height}.png`)});

      // A path that is not an executable is refused with a message, and nothing is saved.
      if (viewport.width === 1024) {
        await dialog.locator('[data-decoder-path="braw"]').fill('/etc/hostname');
        await dialog.locator('[data-raw-dialog-action="save"]').click();
        await page.waitForFunction(() => document.querySelector('[data-raw-dialog-message]')?.classList.contains('error'));
        assert.match(await dialog.locator('[data-raw-dialog-message]').textContent(), /execute permission|regular file/);
        if (proofDir) await page.screenshot({path: path.join(proofDir, 'raw-decoders-rejected-path.png')});
      }
      await dialog.locator('[data-raw-dialog-action="close"]').click();
      await page.waitForFunction(() => !document.querySelector('.splice-raw-dialog'));
    }

    assert.deepEqual(errors, []);
    console.log(`Camera RAW: CinemaDNG sequence linked, re-decoded from the Inspector, monitor within ${worst.toFixed(2)} `
      + 'RGB codes of the ProRes export; undo/redo, reopen, disabled clip, missing-decoder BRAW and the RAW Decoders dialog at 3 sizes.');
  } finally {
    await splice.stop();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
