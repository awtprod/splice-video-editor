/* Clip enable / disable (Premiere's Clip > Enable, Shift+E): a clip with `enabled: false` keeps
 * every property but contributes no picture and no sound. Preview, the timeline's look, the
 * commands (keyboard, Edit menu, context menu), editing around disabled clips, persistence,
 * interchange round trips and the export request. Runs at 1440 × 900 and 1180 × 780.
 * SPLICE_ONLY=<name>[,<name>] runs chosen scenarios; SPLICE_EVIDENCE_DIR saves screenshots. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, makeCamera} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const state = (page, fn, arg) => page.evaluate(fn, arg);
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const blur = page => page.evaluate(() => document.activeElement?.blur?.());
async function capture(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  await page.screenshot({path: path.join(evidence, name)});
}
const sizeName = page => page.viewportSize().width + 'x' + page.viewportSize().height;

const media = [
  {id: 'ridge', name: 'Ridge_A001.mp4', src: 'assets/ridge.mp4', type: 'video', duration: 9},
  {id: 'valley', name: 'Valley_B002.mp4', src: 'assets/valley.mp4', type: 'video', duration: 9},
  {id: 'score', name: 'Score.wav', src: 'assets/score.wav', type: 'audio', duration: 30, audioChannels: 2},
];
const picture = (id, mediaId, track, start, duration, extra = {}) => ({id, mediaId, type: 'video', track, start, duration, in: 1,
  color: {}, volume: 100, transition: 'none', title: '', embeddedAudio: false, ...extra});
const sound = (id, mediaId, track, start, duration, extra = {}) => ({id, mediaId, type: 'audio', track, start, duration, in: 1,
  volume: 100, transition: 'none', title: '', ...extra});
async function load(page, clips, extra = {}) {
  await state(page, async text => { await Editor.openProject(new File([text], 'enable.splice.json')); },
    JSON.stringify({name: 'Enable', width: 320, height: 180, fps: 25, media, clips, ...extra}));
  await state(page, () => Editor.setLinkedSelection(true));
  await blur(page);
  await settle(page);
}
const flags = page => state(page, () => Object.fromEntries(Editor.get().project.clips.map(c => [c.id, c.enabled !== false])));
const clipsOf = page => state(page, () => Editor.get().project.clips.map(c => ({...c})));
const toast = page => state(page, () => document.querySelector('.toast')?.textContent || '');
async function clickClip(page, id, options = {}) {
  await page.locator(`.timeline-clip[data-clip="${id}"]`).click({position: {x: 12, y: 8}, ...options});
}
// Mean RGB of the program monitor once the frame at `time` is painted.
async function programColor(page, time) {
  await state(page, t => Editor.seek(t), time);
  await page.waitForFunction(() => {
    const active = Editor.get().activeClip;
    return !active || Editor.get().video?.readyState >= 2;
  });
  await page.waitForTimeout(250);
  await settle(page);
  return state(page, () => {
    const canvas = document.getElementById('preview-canvas');
    Editor.drawFrame(canvas);
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const sum = [0, 0, 0];
    for (let i = 0; i < data.length; i += 4 * 97) for (let c = 0; c < 3; c++) sum[c] += data[i + c];
    const count = Math.ceil(data.length / (4 * 97));
    return sum.map(value => Math.round(value / count));
  });
}
const distance = (a, b) => Math.hypot(...a.map((value, index) => value - b[index]));
const luma = rgb => .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];

// 1. Preview: a disabled clip on V2 shows the V1 clip under it; disabled sound and a disabled
// camera clip's own sound get no media element (so they cannot play).
async function preview(page, splice) {
  await load(page, [picture('low', 'ridge', 1, 0, 4)]);
  const lowOnly = await programColor(page, 1);
  await load(page, [picture('low', 'ridge', 1, 0, 4), picture('top', 'valley', 2, 0, 4)]);
  const topShown = await programColor(page, 1);
  assert.ok(distance(lowOnly, topShown) > 20, 'the two sources look different: ' + JSON.stringify([lowOnly, topShown]));
  await state(page, () => Editor.toggleEnabled(['top']));
  const disabled = await programColor(page, 1);
  assert.ok(distance(disabled, lowOnly) < 8, 'disabled V2 shows V1: ' + JSON.stringify({lowOnly, topShown, disabled}));
  assert.equal(await state(page, () => Editor.get().activeClip?.id), 'low');
  // Stacked disabled clips over nothing: black.
  await state(page, () => Editor.toggleEnabled(['low']));
  assert.ok(luma(await programColor(page, 1)) < 4, 'all disabled: black');
  await capture(page, `enable-stack-${sizeName(page)}.png`);

  // Sound: the disabled score clip and the disabled camera clip have no element; enabled, they do.
  const camera = path.join(splice.temp, 'camera.mp4');
  if (!fs.existsSync(camera)) makeCamera(camera);
  await state(page, () => SpliceSession.newProject({name: 'Sound', width: 320, height: 180, fps: 25}));
  await page.locator('#media-input').setInputFiles([camera, path.join(__dirname, '..', 'assets', 'score.wav')]);
  await page.waitForFunction(() => Editor.get().project.media.length === 2 && Editor.get().project.media.some(m => m.audioChannels > 0 && m.type === 'video'));
  const elements = () => state(page, () => {
    const {project} = Editor.get();
    const cam = project.media.find(m => m.type === 'video'), music = project.media.find(m => m.type === 'audio');
    const found = src => [...document.querySelectorAll('body > video, body > audio')].filter(e => e.getAttribute('src') === src && !e.muted).length;
    return {camera: found(cam.src), music: found(music.src), activeAudio: Editor.get().activeAudio?.id || null};
  });
  await state(page, () => {
    const {project} = Editor.get();
    const cam = project.media.find(m => m.type === 'video').id, music = project.media.find(m => m.type === 'audio').id;
    Editor.editProject(next => {
      next.clips = [
        {id: 'cam', mediaId: cam, type: 'video', track: 2, start: 0, duration: 3, in: 0, color: {}, volume: 100, transition: 'none', enabled: false},
        {id: 'music', mediaId: music, type: 'audio', track: -1, start: 0, duration: 3, in: 0, volume: 100, transition: 'none', enabled: false}];
    });
    Editor.seek(1);
  });
  await page.waitForTimeout(300);
  assert.deepEqual(await elements(), {camera: 0, music: 0, activeAudio: null}, 'disabled clips have no audible element');
  await state(page, () => { Editor.toggleEnabled(['cam', 'music']); Editor.seek(1.2); });
  await page.waitForFunction(() => Editor.get().activeAudio?.id === 'music');
  const enabled = await elements();
  assert.deepEqual([enabled.camera > 0, enabled.music > 0], [true, true], 'enabled again, both play: ' + JSON.stringify(enabled));
  console.log('Preview: disabled V2 shows V1, disabled clips are black and silent, re-enabled clips play.');
}

// 2. Transitions next to a disabled clip. The rule: a transition belongs to the clip it starts;
// a disabled clip takes its transition with it (the clip before simply ends with a cut); a
// transition after a disabled clip still plays, against what is underneath (black here).
async function transitions(page) {
  const cut = () => [picture('a', 'ridge', 1, 0, 2), picture('b', 'valley', 1, 2, 2, {transition: 'dissolve', transitionDuration: .8})];
  await load(page, cut());
  const mixed = await programColor(page, 2.4);
  await state(page, () => Editor.toggleEnabled(['a']));
  const fromBlack = await programColor(page, 2.4);
  const bAlone = await programColor(page, 3.5);
  assert.ok(luma(fromBlack) < luma(bAlone) * .75, 'dissolve after a disabled clip rises from black: ' + JSON.stringify({mixed, fromBlack, bAlone}));
  await load(page, cut());
  await state(page, () => Editor.toggleEnabled(['b']));
  const before = await programColor(page, 1.5);
  const after = await programColor(page, 2.4);
  assert.ok(luma(before) > 10 && luma(after) < 4, 'a disabled clip takes its dissolve: the clip before cuts to black ' + JSON.stringify({before, after}));
  console.log('Transitions: after a disabled clip the dissolve rises from black; a disabled clip takes its own dissolve.');
}

// 3. The timeline marks disabled clips (dimmed, hatched, OFF tag, tooltip) and they stay
// selectable, trimmable and movable.
async function timelineLook(page) {
  await load(page, [picture('v', 'ridge', 1, 0, 3, {linkGroup: 'p'}), sound('a', 'score', -1, 0, 3, {linkGroup: 'p'}),
    picture('solo', 'valley', 2, 3.6, 2, {enabled: false})]);
  await clickClip(page, 'v');
  await page.keyboard.press('Shift+E');
  await settle(page);
  const look = await state(page, () => [...document.querySelectorAll('.timeline-clip')].map(node => ({
    id: node.dataset.clip, disabled: node.classList.contains('disabled'), off: node.querySelector('.clip-off')?.textContent || '',
    title: node.title, dashed: getComputedStyle(node).borderStyle})));
  for (const id of ['v', 'a', 'solo']) {
    const node = look.find(entry => entry.id === id);
    assert.deepEqual([node.disabled, node.off, node.dashed], [true, 'OFF', 'dashed'], JSON.stringify(node));
    assert.match(node.title, /disabled/);
  }
  assert.match(await toast(page), /2 clips disabled/);
  await capture(page, `enable-pair-and-single-${sizeName(page)}.png`);
  // Still selectable, trimmable and movable.
  await clickClip(page, 'solo');
  assert.equal(await state(page, () => Editor.get().selectedId), 'solo');
  await state(page, () => { Editor.trimEdge('solo', 'end', 5); Editor.moveClip('solo', 4, 2); });
  const solo = (await clipsOf(page)).find(c => c.id === 'solo');
  assert.deepEqual([solo.start, Math.round(solo.duration * 25), solo.enabled], [4, 35, false]);
  console.log('Timeline: disabled clips are dashed, hatched and tagged OFF, and still select, trim and move.');
}

// 4. Commands: Shift+E, the Edit menu and the context menu; linked selection; a mixed selection
// flips each clip; one undo step; locked tracks refuse.
async function commands(page) {
  const clips = () => [picture('v1', 'ridge', 1, 0, 2, {linkGroup: 'p'}), sound('a1', 'score', -1, 0, 2, {linkGroup: 'p'}),
    picture('v2', 'valley', 1, 2, 2)];
  await load(page, clips());
  await clickClip(page, 'v1');
  await page.keyboard.press('Shift+E');
  assert.deepEqual(await flags(page), {v1: false, a1: false, v2: true}, 'linked selection on: the pair toggles together');
  await state(page, () => Editor.undo());
  assert.deepEqual(await flags(page), {v1: true, a1: true, v2: true}, 'one undo step restores both');
  // Linked selection off: only the clicked clip.
  await page.keyboard.press('Control+Shift+L');
  await clickClip(page, 'v1');
  await page.keyboard.press('Shift+E');
  assert.deepEqual(await flags(page), {v1: false, a1: true, v2: true}, 'linked selection off: only the selected clip');
  // Mixed selection: each clip flips (Premiere's rule).
  await clickClip(page, 'v1');
  await clickClip(page, 'v2', {modifiers: ['Control']});
  await page.keyboard.press('Shift+E');
  assert.deepEqual(await flags(page), {v1: true, a1: true, v2: false}, 'mixed selection: each flips');
  assert.match(await toast(page), /1 clip disabled, 1 clip enabled/);
  await page.keyboard.press('Control+Shift+L');
  // Edit menu entry.
  await load(page, clips());
  await clickClip(page, 'v2');
  await page.locator('.edit-menu summary').click();
  await capture(page, `enable-edit-menu-${sizeName(page)}.png`);
  await page.locator('.edit-menu [data-command=enable]').click();
  assert.deepEqual(await flags(page), {v1: true, a1: true, v2: false}, 'Edit > Enable / disable clip');
  assert.equal(await state(page, () => document.querySelector('.edit-menu').open), false);
  // Context menu on a clip outside the selection selects it, then disables it and its partner.
  await page.locator('.timeline-clip[data-clip="a1"]').click({button: 'right', position: {x: 12, y: 8}});
  const menu = page.locator('.sync-menu [role=menuitem]').first();
  assert.match(await menu.textContent(), /Disable clip/);
  await capture(page, `enable-context-menu-${sizeName(page)}.png`);
  await menu.click();
  assert.deepEqual(await flags(page), {v1: false, a1: false, v2: false}, 'context menu');
  assert.equal(await page.locator('.sync-menu').count(), 0);
  await page.locator('.timeline-clip[data-clip="v1"]').click({button: 'right', position: {x: 12, y: 8}});
  assert.match(await page.locator('.sync-menu [role=menuitem]').first().textContent(), /Enable clip/);
  await page.keyboard.press('Escape');
  // Locked track: refused with a message, nothing changes.
  await load(page, clips());
  await state(page, () => Editor.setTrackState(-1, 'audio', {locked: true}));
  await clickClip(page, 'v1');
  await page.keyboard.press('Shift+E');
  assert.deepEqual(await flags(page), {v1: true, a1: true, v2: true});
  assert.match(await toast(page), /Unlock audio track 1/);
  // Nothing selected: a message.
  await state(page, () => Editor.select(null));
  await page.keyboard.press('Shift+E');
  assert.match(await toast(page), /Select clips to enable or disable/);
  // The keyboard reference lists it (it is read from the single keymap).
  const row = await state(page, () => SpliceKeymap.find(entry => entry.keys === 'Shift + E')?.action);
  assert.match(row, /Enable · disable/);
  await page.locator('.file-menu summary').click();
  await page.locator('#shortcuts').click();
  const reference = page.locator('dialog[open] dt', {hasText: /^Shift \+ E$/});
  await reference.scrollIntoViewIfNeeded();
  assert.ok(await reference.isVisible());
  await capture(page, `enable-keyboard-reference-${sizeName(page)}.png`);
  await page.locator('dialog[open] button', {hasText: 'Close'}).click();
  console.log('Commands: Shift+E, Edit menu and context menu toggle; linked pairs together; mixed selections flip each; one undo; locked tracks refuse.');
}

// 5. Editing around disabled clips keeps the flag on every piece and treats the clip like any other.
async function editing(page) {
  const pair = () => [picture('v', 'ridge', 1, 0, 4, {linkGroup: 'p', enabled: false}), sound('a', 'ridge', -1, 0, 4, {linkGroup: 'p', enabled: false}),
    picture('next', 'valley', 1, 4, 2)];
  // Split: both pieces stay disabled and linked.
  await load(page, pair());
  await clickClip(page, 'v');
  await state(page, () => Editor.seek(1));
  await page.keyboard.press('s');
  let clips = await clipsOf(page);
  assert.equal(clips.length, 5);
  assert.ok(clips.filter(c => c.id !== 'next').every(c => c.enabled === false), 'split pieces stay disabled');
  // Copy / paste keeps the flag.
  await clickClip(page, 'next');
  await page.keyboard.press('Control+z');
  await clickClip(page, 'v');
  await page.keyboard.press('Control+c');
  await state(page, () => Editor.seek(6));
  await page.keyboard.press('Control+v');
  clips = await clipsOf(page);
  const pasted = clips.filter(c => c.start >= 6 - 1e-6);
  assert.deepEqual(pasted.map(c => [c.type, c.enabled]).sort(), [['audio', false], ['video', false]]);
  assert.equal(pasted[0].linkGroup, pasted[1].linkGroup);
  // Reset clip does not re-enable.
  await state(page, () => { Editor.updateClip('next', {enabled: false, opacity: 40}); Editor.resetClip('next'); });
  const reset = (await clipsOf(page)).find(c => c.id === 'next');
  assert.deepEqual([reset.enabled, reset.opacity], [false, undefined]);
  // Select forward on the track includes disabled clips.
  await clickClip(page, 'v');
  await state(page, () => Editor.seek(0));
  await page.keyboard.press('a');
  assert.match(await toast(page), /3 clips selected forward on Video 1/);
  // Trims: ripple, roll, slip and slide on disabled clips keep the flag.
  await load(page, [...pair(), picture('last', 'ridge', 1, 6, 2, {enabled: false})]);
  await state(page, () => {
    Editor.rollEdit('v', 'next', 3.6);
    Editor.slipClip('v', .2);
    Editor.slideClip('last', -.2);
    Editor.trimEdge('last', 'end', 7.4, {ripple: true});
  });
  clips = await clipsOf(page);
  assert.deepEqual(clips.map(c => [c.id, c.enabled !== false]).sort(), [['a', false], ['last', false], ['next', true], ['v', false]]);
  // Sync badge on a drifted disabled pair; snapping and edit points see disabled clips.
  await state(page, () => Editor.withoutLinks(() => Editor.updateClip('a', {start: .4})));
  await settle(page);
  assert.equal(await page.locator('.timeline-clip.disabled .sync-badge').count(), 2, 'sync badges on a drifted disabled pair');
  assert.ok(await state(page, () => Editor.editPoints().some(point => Math.abs(point - 7.4) < 1e-6)));
  // Three-point overwrite over a disabled clip: the remaining pieces stay disabled, the new clip is enabled.
  await load(page, pair());
  await state(page, () => Editor.placeSource('valley', {mode: 'overwrite', track: 1, marks: {in: 0, out: 1}, at: 1}));
  clips = await clipsOf(page);
  const onV1 = clips.filter(c => c.track === 1).sort((x, y) => x.start - y.start).map(c => [c.start, c.duration, c.enabled !== false]);
  assert.deepEqual(onV1, [[0, 1, false], [1, 1, true], [2, 2, false], [4, 2, true]]);
  // Lift and extract over disabled clips.
  await load(page, pair());
  await state(page, () => { Editor.setMark('in', 1); Editor.setMark('out', 1.96); Editor.liftRange({extract: true}); });
  clips = await clipsOf(page);
  assert.ok(clips.filter(c => c.mediaId !== 'valley').every(c => c.enabled === false), JSON.stringify(clips));
  assert.equal(clips.filter(c => c.mediaId !== 'valley').length, 4);
  console.log('Editing: split, paste, reset, select-forward, roll/slip/slide/ripple, overwrite and extract keep disabled clips disabled.');
}

// 6. Persistence and compatibility: the working project, recovery after a reload, and projects
// that already carry `enabled: false` (from the FCPXML, FCP 7 XML and OTIO importers).
async function persistence(page) {
  await load(page, [picture('v', 'ridge', 1, 0, 2), picture('w', 'valley', 2, 0, 2, {enabled: false})]);
  assert.match(await toast(page), /Opened with 1 disabled clip/);
  assert.equal(await page.locator('.timeline-clip.disabled').count(), 1);
  // Working project file: save and reopen.
  const download = page.waitForEvent('download');
  await state(page, () => SpliceSession.saveAs('splice'));
  const file = await (await download).path();
  const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(saved.clips.find(c => c.id === 'w').enabled, false);
  assert.equal('enabled' in saved.clips.find(c => c.id === 'v'), false, 'enabled clips carry no flag');
  await page.locator('#project-input').setInputFiles(file);
  await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'w')?.enabled === false);
  // Recovery: once the file has finished opening (its own recovery slot is written), change the
  // flag, let the draft save, reload.
  await page.waitForFunction(() => !document.getElementById('project-loading') && document.getElementById('session-status').textContent === 'Saved');
  await state(page, () => Editor.toggleEnabled(['v']));
  await page.waitForFunction(() => document.getElementById('session-status').textContent === 'Draft saved locally');
  await page.reload();
  await state(page, () => SpliceSession.ready);
  await page.waitForFunction(() => Editor.get().project.clips.some(c => c.id === 'w'));
  assert.deepEqual(await flags(page), {v: false, w: false}, 'recovery restores the flags');
  console.log('Persistence: working project file and recovery keep disabled clips; opening one says how many are disabled.');
}

// 7. Interchange: FCPXML, FCP 7 XML and OTIO written from Splice keep the flag and read it back.
async function interchange(page, splice) {
  await load(page, [picture('v', 'ridge', 1, 0, 2), picture('alt', 'valley', 2, 0, 2, {enabled: false}), picture('w', 'valley', 1, 2, 2),
    sound('mus', 'score', -1, 0, 4, {enabled: false})]);
  const fileMenu = async id => {
    if (!await page.locator(id).isVisible()) await page.locator('.file-menu summary').click();
    await page.locator(id).click();
  };
  await fileMenu('#export-timeline');
  const dialog = page.locator('.interchange-dialog');
  await dialog.locator('[name=xmeml]').check();
  await dialog.locator('[name=otio]').check();
  const downloads = [];
  const onDownload = item => downloads.push(item);
  page.on('download', onDownload);
  await dialog.locator('[type=submit]').click();
  await dialog.locator('[role=status]').filter({hasText: /Exported 3 files/}).waitFor();
  for (let i = 0; i < 50 && downloads.length < 3; i++) await page.waitForTimeout(100);
  page.off('download', onDownload);
  await dialog.locator('[data-close]').click();
  const files = {};
  for (const item of downloads) {
    const target = path.join(splice.temp, sizeName(page) + '-' + item.suggestedFilename());
    await item.saveAs(target);
    files[path.extname(target)] = target;
  }
  assert.deepEqual(Object.keys(files).sort(), ['.fcpxml', '.otio', '.xml']);
  for (const [extension, file] of Object.entries(files)) {
    await fileMenu('#import-timeline');
    const importer = page.locator('.interchange-dialog');
    await importer.locator('[name=file]').setInputFiles(file);
    const before = await state(page, () => Editor.get().revision);
    await importer.locator('[type=submit]').click();
    await page.waitForFunction(r => Editor.get().revision !== r && !document.getElementById('project-loading'), before);
    await importer.waitFor({state: 'detached'});
    const imported = await state(page, () => Editor.get().project.clips.map(c => [c.type, c.track, Math.round(c.start * 25), c.enabled !== false])
      .sort((a, b) => JSON.stringify(a) < JSON.stringify(b) ? -1 : 1));
    assert.deepEqual(imported, [['audio', -1, 0, false], ['video', 1, 0, true], ['video', 1, 50, true], ['video', 2, 0, false]], extension);
    await settle(page);
    assert.equal(await page.locator('.timeline-clip.disabled').count(), 2, extension + ' shows the imported disabled clips as disabled');
  }
  console.log('Interchange: FCPXML, FCP 7 XML and OTIO round trips keep the disabled V2 and A1 clips disabled.');
}

// 8. Export: the request carries the flag unchanged (the renderer decides; see tests/test_disabled_render.py).
async function exportRequest(page) {
  await load(page, [picture('v', 'ridge', 1, 0, 1), picture('w', 'valley', 2, 0, 1, {enabled: false})]);
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
  await page.waitForFunction(() => window.exportBodies.length && !document.querySelector('.studio-dialog [type=submit]').disabled, null, {timeout: 120000});
  await download.catch(() => {});
  await page.locator('.studio-dialog [data-cancel]').click();
  const sent = await state(page, () => window.exportBodies[0].project.clips.map(c => [c.id, c.enabled]));
  assert.deepEqual(sent.sort(), [['v', undefined], ['w', false]]);
  console.log('Export: the render request carries enabled: false unchanged.');
}

const only = process.env.SPLICE_ONLY?.split(',');
async function main() {
  const splice = await startSplice('splice-enable-');
  try {
    const sections = [
      ['preview', preview], ['transitions', transitions], ['timelineLook', timelineLook], ['commands', commands],
      ['editing', editing], ['persistence', persistence, {once: true}], ['interchange', interchange, {once: true}],
      ['exportRequest', exportRequest, {once: true}],
    ];
    let pass = 1;
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}]) {
      const {page, errors} = await openPage(splice, viewport);
      console.log(`Pass ${pass} at ${viewport.width} × ${viewport.height}`);
      for (const [name, run, options] of sections) {
        if (only && !only.includes(name)) continue;
        if (options?.once && pass > 1) continue;
        await run(page, splice);
      }
      assert.deepEqual(errors, []);
      await page.context().close();
      pass++;
    }
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
