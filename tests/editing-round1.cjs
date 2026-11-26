/* Regressions for the first review round of the editing core (PR #7): one scenario per finding,
 * plus checks for behaviour that earlier tests left unguarded. SPLICE_ONLY=<name>[,<name>] runs
 * chosen scenarios; SPLICE_EVIDENCE_DIR saves screenshots. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {root, startSplice, openPage, ffmpeg, makeCamera, nearestSegment, colorAt, readPcm, toneAt} = require('./browser-harness.cjs');

// The renderer change that leaves hidden picture tracks out of the export is in PR #6
// (impl/render-scale). Until it is on dev, the picture half of the hidden-track export check is
// an expected failure. Set this to '' once PR #6 has merged.
const HIDDEN_EXPORT_EXPECTED_FAILURE = 'needs the renderer change in PR #6 (impl/render-scale)';

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const frame = 1 / 25;
const state = (page, fn, arg) => page.evaluate(fn, arg);
const waitFor = (page, fn, arg, timeout = 15000) => page.waitForFunction(fn, arg, {timeout});
const blur = page => page.evaluate(() => document.activeElement?.blur?.());
async function capture(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  await page.screenshot({path: path.join(evidence, name)});
}
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

// A working project on bundled media, opened fresh (no undo history).
const clip = (id, start, duration, extra = {}) => ({id, mediaId: 'long', type: 'video', track: 1, start, duration, in: 5,
  color: {}, volume: 100, transition: 'none', title: '', ...extra});
async function load(page, project) {
  await state(page, async text => {
    await Editor.openProject(new File([text], 'round1.splice.json'));
  }, JSON.stringify({name: 'Round 1', width: 1280, height: 720, fps: 25,
    media: [{id: 'long', name: 'Long take', src: 'assets/ridge.mp4', type: 'video', duration: 30}], ...project}));
  await blur(page);
}
const clipsOf = page => state(page, () => Editor.get().project.clips);
const byId = (clips, id) => clips.find(c => c.id === id);
const frames = seconds => Math.round(seconds * 25);
const attempt = (page, fn, arg) => page.evaluate(([source, value]) => {
  try { return {ok: (0, eval)(source)(value)}; } catch (error) { return {error: error.message}; }
}, [String(fn), arg]);

// 1. Hidden picture tracks: preview and export agree, and the hidden track's camera sound plays in both.
async function hiddenExport(splice, page) {
  const red = path.join(splice.temp, 'silent-red.mp4');
  if (!fs.existsSync(red)) ffmpeg(['-f', 'lavfi', '-i', 'color=c=red:s=160x90:r=25:d=3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', red]);
  const camera = path.join(splice.temp, 'camera.mp4');
  await state(page, () => SpliceSession.newProject({name: 'Hidden export', width: 320, height: 180, fps: 25}));
  await page.locator('#media-input').setInputFiles([red, camera]);
  await waitFor(page, () => Editor.get().project.media.length === 2 && Editor.get().project.media.some(m => m.audioChannels > 0));
  await state(page, () => {
    const media = Editor.get().project.media;
    const silent = media.find(m => m.name.startsWith('silent')).id, cam = media.find(m => m.name.startsWith('camera')).id;
    Editor.editProject(project => {
      project.clips = [
        {id: 'v1', mediaId: silent, type: 'video', track: 1, start: 0, duration: 2, in: 0, color: {}, volume: 100, transition: 'none'},
        // Camera clip with its sound embedded, from source second 1 (lime and blue, 300 and 400 Hz).
        {id: 'v2', mediaId: cam, type: 'video', track: 2, start: 0, duration: 2, in: 1, color: {}, volume: 100, transition: 'none'}];
    });
    Editor.setTrackState(2, 'video', {hidden: true});
    Editor.seek(1);
  });
  // Preview: V1's picture, and the hidden V2 clip's camera sound is playing (its element is not muted).
  const preview = await waitFor(page, () => {
    const camera = Editor.get().project.media.find(m => m.name.startsWith('camera'));
    const element = [...document.querySelectorAll('video')].find(v => v.getAttribute('src') === camera.src);
    return Editor.get().activeClip?.id === 'v1' && element && !element.muted && element.readyState >= 2 && {active: 'v1', muted: element.muted};
  }).then(handle => handle.jsonValue());
  assert.deepEqual(preview, {active: 'v1', muted: false});
  // Export: the request carries trackState as the renderer contract reads it.
  await state(page, () => {
    window.exportBodies = [];
    const send = window.fetch;
    window.fetch = (url, options) => {
      if (String(url) === '/api/exports') window.exportBodies.push(JSON.parse(options.body));
      return send(url, options);
    };
  });
  await page.locator('#export-video').click();
  const download = page.waitForEvent('download', {timeout: 120000});
  await page.locator('.studio-dialog [type=submit]').click();
  await page.waitForFunction(() => !document.querySelector('.studio-dialog [type=submit]').disabled, null, {timeout: 120000});
  const status = await page.locator('.render-state').textContent();
  if (!status.startsWith('MP4 exported')) { download.catch(() => {}); throw Error('Export failed: ' + status); }
  const movie = path.join(splice.temp, 'hidden-export.mp4');
  await (await download).saveAs(movie);
  await page.locator('.studio-dialog [data-cancel]').click();
  const sent = await state(page, () => window.exportBodies[0].project.trackState);
  assert.deepEqual(sent, {'video:2': {locked: false, syncLocked: true, hidden: true}});
  const seen = [.5, 1.5].map(time => nearestSegment(colorAt(movie, time)));
  const heard = [.5, 1.5].map(time => Math.round(toneAt(readPcm(movie), time)));
  // Hiding is a picture toggle: the hidden track's camera sound stays in the export
  // (source seconds 1.5 and 2.5: 300 Hz and 400 Hz).
  assert.ok(Math.abs(heard[0] - 300) < 30 && Math.abs(heard[1] - 400) < 30, 'hidden track sound in export: ' + heard);
  const pictureOk = seen.every(segment => segment === 0);
  if (pictureOk && HIDDEN_EXPORT_EXPECTED_FAILURE)
    assert.fail('The hidden-track export now leaves V2 out: remove HIDDEN_EXPORT_EXPECTED_FAILURE (PR #6 has landed).');
  if (!pictureOk && !HIDDEN_EXPORT_EXPECTED_FAILURE)
    assert.fail('Export shows the hidden V2 picture: ' + JSON.stringify({seen, heard}));
  console.log(pictureOk ? 'Hidden V2: export shows V1 and keeps V2\'s camera sound: ' + JSON.stringify({seen, heard}) :
    'EXPECTED FAILURE (' + HIDDEN_EXPORT_EXPECTED_FAILURE + '): export still shows the hidden V2 picture ' +
    JSON.stringify({seen, heard}) + '; preview and the export request are correct and V2\'s camera sound is in the file.');
}

// 2. A Premiere sequence opens at its own start timecode (Adobe fixture: 00:00:00;29, drop-frame).
async function premiereTimecode(page) {
  await page.locator('#project-input').setInputFiles(path.join(root, 'tests/fixtures/13_track_mute.prproj'));
  await waitFor(page, () => Editor.get().project.premiereOriginal && !document.getElementById('project-loading'), undefined, 60000);
  assert.deepEqual(await state(page, () => [Editor.get().project.startTimecode, Editor.get().project.dropFrame]), [29, true]);
  await waitFor(page, () => document.querySelector('#viewer-time').textContent === '00:00:00;29');
  console.log('Premiere sequence start timecode and drop-frame setting open with the project.');
}

// 3. In and Out both include the frame they are set on.
async function outInclusive(page) {
  await load(page, {clips: [clip('a', 0, 8), clip('b', 8, 2, {in: 0})]});
  await state(page, () => Editor.seek(50 / 25));
  await page.keyboard.press('i');
  await state(page, () => Editor.seek(80 / 25));
  await page.keyboard.press('o');
  const marks = await state(page, () => Editor.get().project.marks);
  assert.deepEqual([frames(marks.in), frames(marks.out)], [50, 81], 'In 50 / Out 80 is 31 frames: the range ends after frame 80');
  assert.equal(await page.locator('#mark-status').textContent(), 'In 00:00:02:00 · Out 00:00:03:05');
  await state(page, () => Editor.seek(0));
  await page.keyboard.press('Shift+o');
  assert.equal(await state(page, () => Math.round(Editor.get().time * 25)), 80, 'Shift+O goes to the Out frame');
  // Source marks from the source monitor: Mark In on frame 10, Mark Out on frame 40 is 31 frames.
  await state(page, () => SpliceSource.open('long'));
  await waitFor(page, () => document.querySelector('.source-stage video')?.readyState >= 2);
  for (const [edge, at] of [['in', 10], ['out', 40]]) {
    await page.locator('.source-scrub').fill(String(at / 25));
    await waitFor(page, target => Math.abs(document.querySelector('.source-stage video').currentTime - target) < .02, at / 25);
    await page.locator(`[data-action=${edge}]`).click();
  }
  assert.deepEqual(await state(page, () => Editor.get().project.media[0].sourceMarks).then(m => [frames(m.in), frames(m.out)]), [10, 41]);
  await waitFor(page, () => document.querySelector('.source-range').textContent === 'In 00:00:00:10 · Out 00:00:01:15 · 00:00:01:06');
  await page.keyboard.press('Period');
  await waitFor(page, () => document.querySelector('.source-status').textContent.startsWith('Overwrote'));
  await page.locator('.source-close').click();
  let placed = (await clipsOf(page)).find(c => c.start > 1.99 && c.start < 2.01);
  assert.deepEqual([frames(placed.start), frames(placed.duration), frames(placed.in), placed.speed ?? 1], [50, 31, 10, 1]);
  // Three- and four-point arithmetic against the same 31-frame timeline range.
  const place = sourceMarks => state(page, marks => {
    const result = Editor.placeSource('long', {mode: 'overwrite', track: 2, marks});
    const clip = Editor.get().project.clips.find(c => c.id === result.clips[0]);
    Editor.undo();
    return {method: result.method, start: Math.round(clip.start * 25), length: Math.round(clip.duration * 25),
      in: Math.round(clip.in * 25 * 1000) / 1000, speed: clip.speed ?? 1};
  }, sourceMarks);
  // Back-timed from a lone source Out on frame 100: the source In is frame 70.
  assert.deepEqual(await place({out: 101 / 25}), {method: 'back-timed from source Out', start: 50, length: 31, in: 70, speed: 1});
  // Four marks: 62 source frames into 31 timeline frames plays at 200%.
  assert.deepEqual(await place({in: 10 / 25, out: 72 / 25}), {method: 'fit to fill', start: 50, length: 31, in: 10, speed: 2});
  // One frame longer than the range is enough to fit to fill; the same length is a plain edit.
  const longer = await place({in: 10 / 25, out: 42 / 25});
  assert.deepEqual([longer.method, longer.length, Math.abs(longer.speed - 32 / 31) < 1e-9], ['fit to fill', 31, true]);
  assert.deepEqual(await place({in: 10 / 25, out: 41 / 25}), {method: 'timeline In/Out', start: 50, length: 31, in: 10, speed: 1});
  // A lone timeline Out on frame 80 back-times the edit so it ends after frame 80.
  const backTimed = await state(page, () => {
    Editor.setMark('in', null);
    const result = Editor.placeSource('long', {mode: 'overwrite', track: 2, marks: {in: 0, out: 20 / 25}});
    const clip = Editor.get().project.clips.find(c => c.id === result.clips[0]);
    Editor.undo(); Editor.undo();
    return [Math.round(clip.start * 25), Math.round((clip.start + clip.duration) * 25)];
  });
  assert.deepEqual(backTimed, [61, 81]);
  // Lift and extract remove frames 50 to 80, 31 frames.
  await page.keyboard.press('Control+z');
  await page.keyboard.press('Semicolon');
  let clips = await clipsOf(page);
  assert.deepEqual(clips.filter(c => c.track === 1).map(c => [frames(c.start), frames(c.duration)]).sort((x, y) => x[0] - y[0]), [[0, 50], [81, 119], [200, 50]]);
  await page.keyboard.press('Control+z');
  await page.keyboard.press('Quote');
  clips = await clipsOf(page);
  assert.deepEqual(clips.filter(c => c.track === 1).map(c => [frames(c.start), frames(c.duration)]).sort((x, y) => x[0] - y[0]), [[0, 50], [50, 119], [169, 50]]);
  await page.keyboard.press('Control+z');
  // The export range defaults to the In/Out range, Out frame included.
  await page.locator('#export-video').click();
  assert.deepEqual(await state(page, () => {
    const form = document.querySelector('.studio-dialog form');
    return [form.elements.rangeStart.valueAsNumber, form.elements.rangeEnd.valueAsNumber];
  }), [2, 3.24]);
  await page.locator('.studio-dialog [data-cancel]').click();
  // Marks saved by the earlier build stored the same [in, out) range: they open unchanged.
  await load(page, {clips: [clip('a', 0, 8)], marks: {in: 2, out: 3.2}});
  assert.equal(await page.locator('#mark-status').textContent(), 'In 00:00:02:00 · Out 00:00:03:04');
  // The keyboard reference says so.
  assert.ok((await state(page, () => SpliceKeymap.find(row => row.keys === 'I · O').action)).includes('both frames are included'));
  console.log('Out marks include their frame: In 50 / Out 80 is 31 frames for marks, placement, back-timing, fit to fill, lift, extract and export.');
}

// 4. The sync menu opens at the badge, and its outside-click listener never outlives it.
async function syncMenu(page) {
  await load(page, {clips: [clip('pic', 0, 2, {in: 1, linkGroup: 'g', embeddedAudio: false}),
    clip('snd', .08, 2, {type: 'audio', track: -1, in: 1, linkGroup: 'g', color: undefined})]});
  await state(page, () => {
    window.pointerListeners = 0;
    const add = window.addEventListener, remove = window.removeEventListener;
    const capture = options => options === true || options?.capture === true;
    // Playwright adds capture listeners of its own while it clicks; count Splice's only.
    const fromSplice = () => /\/workspace\.js:/.test(new Error().stack);
    window.addEventListener = function (type, listener, options) {
      if (type === 'pointerdown' && capture(options) && fromSplice()) window.pointerListeners++;
      return add.call(this, type, listener, options);
    };
    window.removeEventListener = function (type, listener, options) {
      if (type === 'pointerdown' && capture(options) && fromSplice()) window.pointerListeners--;
      return remove.call(this, type, listener, options);
    };
  });
  const badge = page.locator('[data-clip="snd"] .sync-badge');
  for (let round = 0; round < 2; round++) {
    await badge.click();
    await page.locator('.sync-menu').waitFor();
    const [menu, mark] = await state(page, () => [document.querySelector('.sync-menu'), document.querySelector('[data-clip="snd"] .sync-badge')]
      .map(node => node && JSON.parse(JSON.stringify(node.getBoundingClientRect()))));
    assert.ok(menu && mark && Math.abs(menu.x - mark.x) < 12 && Math.abs(menu.y - mark.y) < 100, JSON.stringify({menu, mark}));
  }
  assert.equal(await state(page, () => document.querySelectorAll('.sync-menu').length), 1);
  await page.keyboard.press('Escape');
  assert.equal(await state(page, () => window.pointerListeners), 0, 'every menu removed its listener');
  await badge.click();
  await capture(page, 'sync-menu-' + page.viewportSize().width + '.png');
  await page.getByRole('menuitem', {name: 'Move into sync'}).click();
  await waitFor(page, () => !document.querySelector('.sync-badge'));
  assert.deepEqual(await state(page, () => [window.pointerListeners, document.querySelectorAll('.sync-menu').length]), [0, 0]);
  console.log('Sync menu opens beside its badge and leaves no listener behind.');
}

// 5. The source monitor is a panel: buttons in view, track headers uncovered, open across edits.
async function sourcePanel(page) {
  await load(page, {clips: [clip('a', 0, 4)]});
  await state(page, () => SpliceSource.open('long'));
  await waitFor(page, () => document.querySelector('.source-stage video')?.readyState >= 2);
  const original = page.viewportSize();
  for (const size of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
    await page.setViewportSize(size);
    await settle(page);
    const layout = await state(page, () => {
      const rect = node => node.getBoundingClientRect();
      const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
      const panel = rect(document.querySelector('.source-dialog'));
      const hidden = [...document.querySelectorAll('.source-actions button')].filter(button => {
        const box = rect(button), x = (box.left + box.right) / 2, y = (box.top + box.bottom) / 2;
        return box.top < 0 || box.bottom > innerHeight || box.left < 0 || box.right > innerWidth || document.elementFromPoint(x, y) !== button;
      }).map(button => button.textContent);
      const canvas = rect(document.getElementById('preview-canvas'));
      return {hidden, coversHeads: overlaps(panel, rect(document.getElementById('track-heads'))),
        coversTimeline: overlaps(panel, rect(document.querySelector('.timeline'))),
        programVisible: canvas.width > 120 && document.elementFromPoint((canvas.left + canvas.right) / 2, (canvas.top + canvas.bottom) / 2)?.id === 'preview-canvas',
        stage: rect(document.querySelector('.source-stage')).height};
    });
    assert.deepEqual([layout.hidden, layout.coversHeads, layout.coversTimeline, layout.programVisible], [[], false, false, true], JSON.stringify({size, layout}));
    assert.ok(layout.stage >= 70, JSON.stringify({size, layout}));
    await capture(page, `source-monitor-${size.width}.png`);
  }
  await page.setViewportSize(original);
  await settle(page);
  // Mark and insert twice at the playhead without reopening the monitor; its keys work while it has focus.
  await state(page, () => Editor.seek(2));
  for (const [from, to] of [[1, 2], [3, 4]]) {
    await page.locator('.source-scrub').fill(String(from));
    await waitFor(page, t => Math.abs(document.querySelector('.source-stage video').currentTime - t) < .02, from);
    await page.keyboard.press('i');
    await page.locator('.source-scrub').fill(String(to - frame));
    await waitFor(page, t => Math.abs(document.querySelector('.source-stage video').currentTime - t) < .02, to - frame);
    await page.keyboard.press('o');
    await page.keyboard.press('Comma');
    await waitFor(page, n => Editor.get().project.clips.length === n, from === 1 ? 3 : 4);
    assert.equal(await page.locator('.source-dialog').count(), 1, 'the monitor stays open after an edit');
    assert.match(await page.locator('.source-status').textContent(), /^Inserted 00:00:01:00 at /);
  }
  await page.locator('.source-close').click();
  assert.equal(await state(page, () => document.querySelector('.workspace').classList.contains('source-open')), false);
  console.log('Source monitor panel keeps its buttons in view at 1440, 1180 and 1024 px, leaves the timeline and program monitor uncovered, and stays open for consecutive edits.');
}

// 6. Recovery stores LUT tables once, by content, and every recovery path puts them back.
async function recoveryPayloads(page) {
  const lut = (size, seed) => ({size, title: 'Look ' + seed, domainMin: [0, 0, 0], domainMax: [1, 1, 1],
    data: Array.from({length: size ** 3 * 3}, (_, i) => ((i * 7 + seed) % 100) / 100)});
  const table = lut(5, 1);
  await state(page, () => SpliceSession.newProject({name: 'LUT recovery', width: 1280, height: 720, fps: 25}));
  await state(page, look => Editor.editProject(project => {
    project.media = [{id: 'long', name: 'Long take', src: 'assets/ridge.mp4', type: 'video', duration: 30}];
    project.clips = [{id: 'graded', mediaId: 'long', type: 'video', track: 1, start: 0, duration: 2, in: 0,
      color: {lut: look, lutIntensity: 80}, volume: 100, transition: 'none'}];
  }), table);
  await waitFor(page, () => document.querySelector('#session-status').textContent === 'Draft saved locally');
  const db = () => state(page, async () => {
    const database = await new Promise(ok => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); });
    const all = store => new Promise(ok => { const r = database.transaction(store).objectStore(store).getAll(); r.onsuccess = () => ok(r.result); });
    const keys = store => new Promise(ok => { const r = database.transaction(store).objectStore(store).getAllKeys(); r.onsuccess = () => ok(r.result); });
    const sessions = await all('sessions'), originals = await keys('originals'), last = await new Promise(ok => {
      const r = database.transaction('metadata').objectStore('metadata').get('lastSession'); r.onsuccess = () => ok(r.result); });
    const row = sessions.find(s => s.id === last);
    database.close();
    return {lut: row.data.clips[0].color.lut, payloadKeys: row.payloadKeys, originals};
  });
  const stored = await db();
  assert.match(stored.lut.payload, /^lut:/, 'the draft refers to the table');
  assert.deepEqual(stored.payloadKeys, [stored.lut.payload]);
  assert.ok(stored.originals.includes(stored.lut.payload));
  // Reload: the recovered project has the table back.
  await page.reload();
  await state(page, () => SpliceSession.ready);
  assert.deepEqual(await state(page, () => Editor.get().project.clips[0].color.lut), table);
  // Version restore: opening the list checkpoints the draft (intensity 80) as a version; after a
  // change to 30, restoring that older version brings back its table.
  const versions = async () => {
    if (!await page.locator('#versions').isVisible()) await page.locator('.file-menu summary').click();
    await page.locator('#versions').click();
    await page.locator('.splice-session-dialog').waitFor();
  };
  await versions();
  await page.locator('.splice-session-dialog button', {hasText: 'Cancel'}).click();
  await state(page, () => Editor.updateClip('graded', {color: {...Editor.get().project.clips[0].color, lutIntensity: 30}}));
  await waitFor(page, () => document.querySelector('#session-status').textContent === 'Draft saved locally');
  await versions();
  await page.locator('.splice-session-dialog button', {hasText: 'Restore version'}).last().click();
  await waitFor(page, () => document.querySelector('#session-status').textContent === 'Version restored');
  assert.deepEqual(await state(page, () => [Editor.get().project.clips[0].color.lutIntensity, Editor.get().project.clips[0].color.lut]), [80, table]);
  // Pruning keeps tables that a kept draft or version uses and removes the rest.
  await state(page, async () => {
    const database = await new Promise(ok => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); });
    const tx = database.transaction('originals', 'readwrite');
    tx.objectStore('originals').put({size: 2, data: []}, 'lut:orphan');
    await new Promise(ok => { tx.oncomplete = ok; });
    database.close();
  });
  await state(page, () => SpliceSession.prune());
  const pruned = await db();
  assert.ok(pruned.originals.includes(pruned.lut.payload) && !pruned.originals.includes('lut:orphan'), JSON.stringify(pruned.originals));
  // A draft written before payloads existed carries its table inline and still restores.
  const old = lut(3, 7);
  await state(page, async look => {
    const database = await new Promise(ok => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); });
    const tx = database.transaction(['sessions', 'metadata'], 'readwrite');
    tx.objectStore('sessions').put({id: 'old-draft', name: 'Old draft', stamp: Date.now(), data: {name: 'Old draft', width: 1280, height: 720, fps: 25,
      media: [{id: 'long', name: 'Long take', src: 'assets/ridge.mp4', type: 'video', duration: 30}],
      clips: [{id: 'old', mediaId: 'long', type: 'video', track: 1, start: 0, duration: 1, in: 0, color: {lut: look}, volume: 100, transition: 'none'}]}});
    tx.objectStore('metadata').put('old-draft', 'lastSession');
    await new Promise(ok => { tx.oncomplete = ok; });
    database.close();
  }, old);
  await page.reload();
  await state(page, () => SpliceSession.ready);
  assert.deepEqual(await state(page, () => [Editor.get().project.name, Editor.get().project.clips[0].color.lut]), ['Old draft', old]);
  // The working-project file carries the table itself.
  const [download] = await Promise.all([page.waitForEvent('download'), state(page, () => Editor.saveProject('splice'))]);
  const saved = JSON.parse(fs.readFileSync(await download.path(), 'utf8'));
  assert.deepEqual(saved.clips[0].color.lut, old);
  console.log('Recovery keeps LUT tables out of band by content and restores them on reload, version restore and old drafts; pruning keeps only used tables.');
}

// 7. Each user action is one undo step or no change.
async function atomicActions(page) {
  await load(page, {clips: [clip('a', 0, 2, {speed: 2, volume: 40, color: {brightness: 130}}), clip('b', 2, 2, {in: 0})]});
  const before = await state(page, () => ({clips: JSON.stringify(Editor.get().project.clips), revision: Editor.get().revision}));
  const reset = await attempt(page, () => Editor.resetClip('a'));
  assert.match(reset.error, /nothing was reset/);
  assert.deepEqual(await state(page, () => ({clips: JSON.stringify(Editor.get().project.clips), revision: Editor.get().revision})), before);
  // With room to return to 100%, Reset is one undo step.
  await state(page, () => Editor.deleteClips(['b']));
  const kept = await state(page, () => JSON.stringify(Editor.get().project.clips));
  await state(page, () => Editor.resetClip('a'));
  assert.deepEqual(await state(page, () => { const a = Editor.get().project.clips[0]; return [a.speed, a.volume, a.duration]; }), [1, 100, 4]);
  await state(page, () => Editor.undo());
  assert.equal(await state(page, () => JSON.stringify(Editor.get().project.clips)), kept);
  // Slip into sync that runs out of source changes nothing.
  // The sound starts six frames early against its picture but has one frame of source before it.
  await load(page, {clips: [clip('pic', .24, 2, {in: 0, linkGroup: 'g', embeddedAudio: false}),
    clip('snd', 0, 2, {type: 'audio', track: -1, in: frame, linkGroup: 'g'})]});
  const linked = await state(page, () => ({clips: JSON.stringify(Editor.get().project.clips), revision: Editor.get().revision}));
  const slip = await attempt(page, () => Editor.bringIntoSync('snd', 'slip'));
  assert.match(slip.error, /Not enough source media/);
  assert.deepEqual(await state(page, () => ({clips: JSON.stringify(Editor.get().project.clips), revision: Editor.get().revision})), linked);
  console.log('Reset and Slip into sync are all-or-nothing, one undo step each.');
}

// 8. Undo states are never changed in place.
async function sharedState(page, camera) {
  await load(page, {clips: [clip('a', 0, 2)]});
  await state(page, () => Editor.updateClip('a', {volume: 50}));
  const afterEdit = await state(page, () => JSON.stringify(Editor.get().project));
  await state(page, () => Editor.setSourceSettings('long', {sourceMarks: {in: 1, out: 2}}));
  await state(page, () => Editor.undo());
  assert.equal(await state(page, () => JSON.stringify(Editor.get().project)), afterEdit, 'undo returns to the state after the edit, untouched');
  await state(page, () => Editor.undo());
  assert.deepEqual(await state(page, () => [Editor.get().project.clips[0].volume, Editor.get().project.media[0].sourceMarks]), [100, undefined]);
  await state(page, () => { Editor.redo(); Editor.redo(); });
  assert.deepEqual(await state(page, () => Editor.get().project.media[0].sourceMarks), {in: 1, out: 2});
  // Generated pool thumbnails are kept by the pool, not written onto shared media objects.
  await state(page, () => SpliceSession.newProject({name: 'Thumbs', width: 1280, height: 720, fps: 25}));
  await page.locator('#media-input').setInputFiles(camera);
  await waitFor(page, () => document.querySelector('#library-body .media-thumb img'));
  assert.equal(await state(page, () => 'thumb' in Editor.get().project.media[0]), false);
  console.log('Source settings undo as their own step and thumbnails never touch undo states.');
}

// 9. Timecode entry.
async function timecodeEntry(page) {
  await state(page, () => Editor.newProject({name: 'NTSC', width: 1280, height: 720, fps: 30000 / 1001}));
  await state(page, () => Editor.editProject(project => {
    project.media = [{id: 'long', name: 'Long', src: 'assets/ridge.mp4', type: 'video', duration: 7200}];
    project.clips = [{id: 'hour', mediaId: 'long', type: 'video', track: 1, start: 0, duration: 3700, in: 0, color: {}, volume: 100, transition: 'none'}];
  }));
  const fps = 30000 / 1001;
  const parse = (text, base) => attempt(page, ([value, at]) => Editor.sequenceTimecode(Editor.parseSequenceTimecode(value, at)), [text, base]);
  const base = (5 * 30 + 12) / fps; // 01:00:05;12
  assert.deepEqual(await parse('1000', base), {ok: '01:00:10;00'}, 'digits fill from the right; the hour comes from the playhead');
  assert.deepEqual(await parse('2.', base), {ok: '01:00:02;00'});
  assert.deepEqual(await parse('-2.', base), {ok: '01:00:03;12'});
  assert.deepEqual(await parse('+1.', base), {ok: '01:00:06;12'});
  assert.match((await parse('1..', base)).error, /does not exist in drop-frame timecode; that minute starts at 01:01:00;02/);
  assert.deepEqual(await parse('1.02', base), {ok: '01:01:00;02'});
  assert.match((await parse('01:01:00;00', base)).error, /does not exist in drop-frame/);
  assert.match((await parse('01:01:00;01', base)).error, /does not exist in drop-frame/);
  assert.deepEqual(await parse('01:10:00;00', base), {ok: '01:10:00;00'});
  for (const text of ['abc', '', '  ', '1:2:3:4:5', '123456789', '01:00:1x:00'])
    assert.match((await parse(text, base)).error, /Type a timecode|at most eight digits/, JSON.stringify(text));
  assert.match((await parse('00:59:59;00', base)).error, /before the sequence start/);
  // Typed in the program timecode on a new sequence that starts at 01:00:00;00.
  await page.locator('#viewer-time').click();
  await page.keyboard.type('1000');
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#viewer-time').textContent(), '01:00:10;00');
  // Every label goes back to its own frame: entry and display round to whole frames the same way.
  const mismatches = await state(page, rate => {
    const bad = [];
    for (let minute = 0; minute < 61; minute++) for (const offset of [-2, -1, 0, 1, 2, 3, 900]) {
      const n = Math.max(0, Math.round(minute * 60 * rate) + offset);
      const label = Editor.sequenceTimecode(n / rate);
      Editor.seekTimecode(label);
      if (Math.round(Editor.get().time * rate) !== n || Editor.sequenceTimecode(Editor.get().time) !== label) bad.push([n, label]);
    }
    return bad;
  }, fps);
  assert.deepEqual(mismatches, []);
  // At 25 fps the same entries have no drop-frame gaps.
  await state(page, () => Editor.newProject({name: 'PAL', width: 1280, height: 720, fps: 25}));
  assert.deepEqual(await attempt(page, () => Editor.sequenceTimecode(Editor.parseSequenceTimecode('1..', 0))), {ok: '01:01:00:00'});
  console.log('Timecode entry: "." is 00, partial entries fill from the right, drop-frame gaps and junk are refused, labels round-trip.');
}

// 10. A title lane and the video lane of its number are one Premiere track.
async function titleTrackState(page) {
  await load(page, {clips: [clip('a', 0, 2), {id: 't', type: 'title', mediaId: null, title: 'Lower third', track: 2,
    start: 0, duration: 2, in: 0, color: {}, volume: 100, transition: 'none'}]});
  await page.getByRole('button', {name: 'Hide Titles · V2'}).click();
  assert.deepEqual(await state(page, () => Editor.get().project.trackState), {'video:2': {locked: false, syncLocked: true, hidden: true}});
  const [download] = await Promise.all([page.waitForEvent('download', {timeout: 60000}), state(page, () => Editor.saveProject('prproj'))]);
  await page.locator('#project-input').setInputFiles(await download.path().then(file => {
    const copy = file + '.prproj';
    fs.copyFileSync(file, copy);
    return copy;
  }));
  await waitFor(page, () => Editor.get().project.premiereOriginal && !document.getElementById('project-loading'), undefined, 60000);
  assert.deepEqual(await state(page, () => [Editor.isTrackHidden(2, 'title'), Editor.isTrackHidden(2, 'video')]), [true, true]);
  assert.equal(await page.getByRole('button', {name: 'Show Titles · V2'}).getAttribute('aria-pressed'), 'true');
  // A working project from the earlier build with different states on the two lanes opens merged and saves.
  await load(page, {clips: [clip('a', 0, 2), {id: 't', type: 'title', mediaId: null, title: 'Lower third', track: 2,
    start: 0, duration: 2, in: 0, color: {}, volume: 100, transition: 'none'}],
  trackState: {'video:2': {locked: false, syncLocked: true, hidden: true}, 'title:2': {locked: true, syncLocked: true}}});
  assert.deepEqual(await state(page, () => Editor.get().project.trackState), {'video:2': {locked: true, syncLocked: true, hidden: true}});
  const [saved] = await Promise.all([page.waitForEvent('download', {timeout: 60000}), state(page, () => Editor.saveProject('prproj'))]);
  assert.ok(fs.statSync(await saved.path()).size > 0);
  console.log('Title lanes share their video track\'s state, save as one Premiere track and reopen the same.');
}

// 11. Lift around transitions, W on a first frame, and which controls keep keys from the timeline.
async function liftAndKeys(page) {
  // b's dissolve starts at the cut (nothing before it) in `afterCut`, and is centred on it in `centred`.
  const afterCut = {clips: [clip('a', 0, 2), clip('b', 2, 2, {in: 15, transition: 'dissolve', transitionDuration: .4})]};
  const centred = {clips: [clip('a', 0, 2), clip('b', 2, 2, {in: 15, transition: 'dissolve', transitionDuration: .4, transitionBefore: .2})]};
  await load(page, afterCut);
  // Lifting a range that ends at the cut leaves a gap before b: its dissolve goes too.
  await state(page, () => { Editor.setMark('in', 1); Editor.setMark('out', 2 - 1 / 25); Editor.liftRange(); });
  let clips = await clipsOf(page);
  assert.deepEqual([byId(clips, 'a').duration, byId(clips, 'b').start, byId(clips, 'b').transition], [1, 2, 'none']);
  // Lifting across the transition removes it with the material around the cut.
  await load(page, centred);
  // Frames 47 to 52 (In 1.88 s, Out on 2.08 s) straddle the cut at frame 50 and the dissolve around it.
  const across = await attempt(page, () => { Editor.setMark('in', 47 / 25); Editor.setMark('out', 52 / 25); Editor.liftRange(); });
  assert.equal(across.error, undefined);
  clips = await clipsOf(page);
  assert.deepEqual(clips.map(c => [c.id, frames(c.start), frames(c.duration), c.transition]), [['a', 0, 47, 'none'], ['b', 53, 47, 'none']]);
  // An overwrite that ends at the cut keeps the dissolve: new material sits before it.
  await load(page, afterCut);
  await state(page, () => { Editor.setMark('in', 1); Editor.setMark('out', 2 - 1 / 25); Editor.placeSource('long', {mode: 'overwrite', track: 1, marks: {in: 10, out: 11}}); });
  assert.equal(byId(await clipsOf(page), 'b').transition, 'dissolve');
  // W with the playhead on b's first frame trims nothing.
  await load(page, {clips: [clip('a', 0, 2), clip('b', 2, 2, {in: 15})]});
  await state(page, () => { Editor.select('b'); Editor.seek(2); });
  await page.keyboard.press('w');
  assert.equal(byId(await clipsOf(page), 'b').duration, 2);
  assert.match(await page.locator('.toast').textContent(), /first frame/);
  // After using a slider or checkbox, shortcuts still work; the slider keeps its arrow keys.
  await state(page, () => Editor.select('a'));
  const slider = page.locator('#inspector-content input[type=range]').first();
  await slider.focus();
  await state(page, () => Editor.seek(1));
  await page.keyboard.press('i');
  assert.equal(await state(page, () => Editor.get().project.marks?.in), 1);
  await page.keyboard.press('ArrowRight');
  assert.equal(await state(page, () => Editor.get().time), 1, 'arrows move the slider, not the playhead');
  const checkbox = page.locator('#inspector-content input[type=checkbox]').first();
  if (await checkbox.count()) {
    await checkbox.focus();
    await state(page, () => Editor.seek(1.2));
    await page.keyboard.press('i');
    assert.equal(await state(page, () => Editor.get().project.marks?.in), 1.2);
  }
  // Backspace and Delete in a text field edit the text, never the timeline.
  await page.locator('#media-search').fill('abc');
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  assert.equal((await clipsOf(page)).length, 2);
  assert.equal(await page.locator('#media-search').inputValue(), 'ab');
  await page.locator('#media-search').fill('');
  // With a modal dialog open the timeline keys do nothing.
  await blur(page);
  await page.locator('#shortcuts').evaluate(button => button.click());
  await page.locator('.shortcut-reference').waitFor();
  await page.keyboard.press('Delete');
  assert.equal((await clipsOf(page)).length, 2);
  await page.locator('.shortcut-reference > button').click();
  console.log('Lift drops transitions it cuts through, W on a first frame refuses, and only text fields keep keys from the timeline.');
}

// 12. Titles on the frame grid; ripple delete of a selection that is not aligned.
async function titlesAndRipple(page) {
  for (const rate of [24000 / 1001, 30000 / 1001]) {
    const title = await state(page, fps => {
      Editor.newProject({name: 'Titles', width: 1280, height: 720, fps});
      Editor.seek(0);
      Editor.addTitle();
      const clip = Editor.get().selected;
      return [clip.start * fps, clip.duration * fps];
    }, rate);
    assert.ok(title.every(value => Math.abs(value - Math.round(value)) < 1e-9), JSON.stringify(title));
  }
  await load(page, {clips: ['a', 'b', 'c', 'd'].map((id, index) => clip(id, index * 2, 2))});
  await state(page, () => Editor.deleteClips(['a', 'c'], {ripple: true}));
  assert.deepEqual((await clipsOf(page)).map(c => [c.id, c.start]).sort(), [['b', 0], ['d', 2]]);
  console.log('Titles last whole frames at 23.976 and 29.97; ripple delete closes each gap of a scattered selection.');
}

// 13. Preview and waveforms read the clip's chosen audio stream.
async function audioStreams(splice, page) {
  const file = path.join(splice.temp, 'two-streams.mp4');
  if (!fs.existsSync(file)) ffmpeg(['-f', 'lavfi', '-i', 'color=c=gray:s=160x90:r=25:d=2', '-f', 'lavfi', '-i', 'sine=f=300:d=2',
    '-f', 'lavfi', '-i', 'sine=f=600:d=2', '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', file]);
  const requests = [];
  const listener = request => { if (/\/api\/media\/(waveform|audition)\?/.test(request.url())) requests.push(request.url()); };
  page.on('request', listener);
  try {
    await state(page, () => SpliceSession.newProject({name: 'Streams', width: 1280, height: 720, fps: 25}));
    await page.locator('#media-input').setInputFiles(file);
    await waitFor(page, () => Editor.get().project.media[0]?.audioStreams === 2, undefined, 60000);
    const original = await state(page, () => {
      const item = Editor.get().project.media[0];
      Editor.editProject(project => {
        project.clips = [{id: 'sound', mediaId: item.id, type: 'audio', track: -1, start: 0, duration: 2, in: 0, audioStream: 1,
          volume: 100, transition: 'none', reverse: true}];
      });
      return item.originalSrc || item.src;
    });
    await waitFor(page, () => true);
    const waveform = () => requests.find(url => url.includes('/api/media/waveform') && new URL(url).searchParams.get('stream') === '1');
    for (let tries = 0; tries < 50 && !waveform(); tries++) await page.waitForTimeout(100);
    assert.ok(waveform(), 'waveform request with stream=1: ' + requests.join(' '));
    assert.equal(new URL(waveform()).searchParams.get('src'), original);
    // Reversed playback is auditioned from the server, from the same stream.
    await state(page, () => { Editor.seek(1.5); Editor.togglePlay(); });
    const audition = () => requests.find(url => url.includes('/api/media/audition') && new URL(url).searchParams.get('stream') === '1');
    for (let tries = 0; tries < 50 && !audition(); tries++) await page.waitForTimeout(100);
    await state(page, () => { if (Editor.get().playing) Editor.togglePlay(); });
    assert.ok(audition(), 'audition request with stream=1: ' + requests.join(' '));
    assert.equal(new URL(audition()).searchParams.get('src'), original);
  } finally {
    page.off('request', listener);
  }
  console.log('Waveforms and audio preview request the clip\'s chosen stream from the source.');
}

// Layout: keyboard reference, media pool tools and list, sync badge against the clip name.
async function layoutChecks(page, camera) {
  await state(page, () => SpliceSession.newProject({name: 'Layout', width: 1280, height: 720, fps: 25}));
  await page.locator('#media-input').setInputFiles(camera);
  await waitFor(page, () => Editor.get().project.media[0]?.audioChannels > 0);
  await state(page, () => Editor.editProject(project => Object.assign(project.media[0],
    {bin: 'Day 2/A cam', reel: 'A001', timecode: '09:14:00:00', rate: '25/1'})));
  const width = page.viewportSize().width;
  // Pool tools: the search field has room for its placeholder; the list shows every column.
  const pool = await state(page, () => ({search: document.getElementById('media-search').getBoundingClientRect().width,
    filter: document.getElementById('media-filter').getBoundingClientRect().width}));
  assert.ok(pool.search >= 150 && pool.filter >= 80, JSON.stringify(pool));
  await page.locator('#media-view').click();
  const columns = await state(page, () => {
    const panel = document.querySelector('.library').getBoundingClientRect();
    return [...document.querySelectorAll('.media-table-head button')].filter(button => {
      const box = button.getBoundingClientRect();
      return box.left < panel.left || box.right > panel.right + .5 || box.width < 20;
    }).map(button => button.textContent);
  });
  assert.deepEqual(columns, [], 'every list column header is inside the pool');
  await capture(page, `media-pool-list-${width}.png`);
  await page.locator('#media-view').click();
  // Keyboard reference opens at the top with its heading in view.
  await page.locator('#shortcuts').evaluate(button => button.click());
  await page.locator('.shortcut-reference').waitFor();
  const reference = await state(page, () => {
    const dialog = document.querySelector('.shortcut-reference'), heading = dialog.querySelector('h2').getBoundingClientRect();
    const box = dialog.getBoundingClientRect(), close = dialog.querySelector(':scope > button').getBoundingClientRect();
    return {headingVisible: heading.top >= box.top && heading.bottom <= box.bottom, top: dialog.querySelector('.shortcut-list').scrollTop,
      closeVisible: close.bottom <= innerHeight && close.top >= box.top};
  });
  assert.deepEqual(reference, {headingVisible: true, top: 0, closeVisible: true});
  await capture(page, `keyboard-reference-${width}.png`);
  await page.locator('.shortcut-reference > button').click();
  // The sync badge leaves the clip name readable.
  const mediaId = await state(page, () => Editor.get().project.media[0].id);
  await state(page, id => Editor.editProject(project => {
    project.clips = [
      {id: 'pic', mediaId: id, type: 'video', track: 1, start: 0, duration: 3, in: 1, linkGroup: 'g', embeddedAudio: false, color: {}, volume: 100, transition: 'none'},
      {id: 'snd', mediaId: id, type: 'audio', track: -1, start: .08, duration: 3, in: 1, linkGroup: 'g', volume: 100, transition: 'none'}];
  }), mediaId);
  const clash = await state(page, () => [...document.querySelectorAll('.timeline-clip.out-of-sync')].map(node => {
    const name = node.querySelector('.clip-name'), badge = node.querySelector('.sync-badge').getBoundingClientRect();
    const box = name.getBoundingClientRect(), textRight = box.right - parseFloat(getComputedStyle(name).paddingRight);
    return textRight <= badge.left + .5 || box.bottom <= badge.top || box.top >= badge.bottom;
  }));
  assert.deepEqual(clash, [true, true]);
  console.log('Keyboard reference opens at the top, pool search and list columns fit, sync badge clears the clip name.');
}

// Behaviour earlier tests did not pin down.
async function unguarded(page) {
  // Separating a camera clip's sound takes it out of the picture clip: it must not play twice.
  await state(page, () => SpliceSession.newProject({name: 'Guards', width: 1280, height: 720, fps: 25}));
  await state(page, () => Editor.editProject(project => {
    project.media = [{id: 'cam', name: 'Camera', src: 'assets/ridge.mp4', type: 'video', duration: 30, audioChannels: 2}];
    project.clips = [{id: 'pic', mediaId: 'cam', type: 'video', track: 1, start: 0, duration: 2, in: 0, color: {}, volume: 100, transition: 'none'}];
  }));
  assert.equal(await state(page, () => Editor.toggleLink(['pic'])), 'separated');
  assert.deepEqual(await state(page, () => {
    const clips = Editor.get().project.clips, picture = clips.find(c => c.id === 'pic');
    return [picture.embeddedAudio, Editor.hasEmbeddedAudio(picture), clips.filter(c => c.type === 'audio').length];
  }), [false, false, 1]);
  // Overwrite through a linked pair whose members differ in length unlinks the whole group.
  await load(page, {clips: [clip('v', 0, 2, {in: 0, linkGroup: 'g', embeddedAudio: false}),
    clip('s', 0, 2.4, {type: 'audio', track: -1, in: 0, linkGroup: 'g'})]});
  await state(page, () => { Editor.setMark('in', .8); Editor.setMark('out', 1.16);
    Editor.placeSource('long', {mode: 'overwrite', track: 1, audioTrack: null, marks: {in: 10, out: 11}}); });
  assert.deepEqual((await clipsOf(page)).filter(c => c.mediaId === 'long' && Math.abs(c.in - 10) > .01 && c.linkGroup).map(c => c.id), []);
  // Split: a linked member wholly after the cut joins the right-hand piece; a lone left piece is unlinked.
  await load(page, {clips: [clip('v', 0, 4, {in: 0, linkGroup: 'g', embeddedAudio: false}),
    clip('s', 2.6, 1.4, {type: 'audio', track: -1, in: 2.6, linkGroup: 'g'})]});
  await state(page, () => { Editor.select('v'); Editor.seek(2); Editor.split(); });
  let clips = await clipsOf(page);
  const left = byId(clips, 'v'), right = clips.find(c => c.type === 'video' && c.id !== 'v');
  assert.deepEqual([left.linkGroup, typeof right.linkGroup, byId(clips, 's').linkGroup === right.linkGroup], [undefined, 'string', true]);
  // Insert opens space on sync-locked tracks only.
  await load(page, {clips: [clip('a', 0, 4), clip('m1', 3, 2, {type: 'audio', track: -2, in: 0}), clip('m2', 3, 2, {type: 'audio', track: -3, in: 0})],
    trackState: {'audio:-3': {locked: false, syncLocked: false}}});
  await state(page, () => { Editor.setMark('in', 2); Editor.placeSource('long', {mode: 'insert', track: 1, marks: {in: 10, out: 11}}); });
  clips = await clipsOf(page);
  assert.deepEqual([byId(clips, 'm1').start, byId(clips, 'm2').start], [4, 3]);
  // Slide never takes the last frame of a neighbour.
  await load(page, {clips: [clip('a', 0, 2), clip('b', 2, 2, {in: 10}), clip('c', 4, 2, {in: 15})]});
  await state(page, () => Editor.slideClip('b', 5));
  clips = await clipsOf(page);
  assert.deepEqual([frames(byId(clips, 'b').start), frames(byId(clips, 'c').duration), frames(byId(clips, 'c').start)], [99, 1, 149]);
  console.log('Separated sound, overwrite unlinking, split regrouping, insert sync lock and slide limits hold.');
}

const only = process.env.SPLICE_ONLY?.split(',');
async function main() {
  const splice = await startSplice('splice-round1-');
  try {
    const camera = path.join(splice.temp, 'camera.mp4');
    makeCamera(camera);
    const sections = [
      ['hiddenExport', page => hiddenExport(splice, page), {once: true}],
      ['premiereTimecode', premiereTimecode],
      ['outInclusive', outInclusive],
      ['syncMenu', syncMenu],
      ['sourcePanel', sourcePanel],
      ['recovery', recoveryPayloads],
      ['atomic', atomicActions],
      ['sharedState', page => sharedState(page, camera)],
      ['timecodeEntry', timecodeEntry],
      ['titleTrack', titleTrackState],
      ['liftAndKeys', liftAndKeys],
      ['titlesAndRipple', titlesAndRipple],
      ['audioStreams', page => audioStreams(splice, page), {once: true}],
      ['layout', page => layoutChecks(page, camera)],
      ['unguarded', unguarded],
    ];
    let pass = 1;
    // Every flow runs twice, at a desktop size and a smaller one (the export and stream checks once).
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}]) {
      const {page, errors} = await openPage(splice, viewport);
      console.log(`Pass ${pass} at ${viewport.width} × ${viewport.height}`);
      for (const [name, run, options] of sections)
        if ((!only || only.includes(name)) && !(options?.once && pass > 1)) await run(page);
      assert.deepEqual(errors, []);
      await page.context().close();
      pass++;
    }
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
