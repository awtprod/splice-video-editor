/* Clip enable / disable (Premiere's Clip > Enable, Shift+E): a clip with `enabled: false` keeps
 * every property but contributes no picture and no sound. Preview, the timeline's look, the
 * commands (keyboard, Edit menu, context menu), editing around disabled clips, persistence,
 * interchange round trips and the export request. Runs at 1440 × 900 and 1180 × 780.
 * SPLICE_ONLY=<name>[,<name>] runs chosen scenarios; SPLICE_EVIDENCE_DIR saves screenshots. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, makeCamera, ffmpeg} = require('./browser-harness.cjs');

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

// 2. Transitions next to a disabled clip follow the renderer's rule (render_movie._disabled_stand_ins):
// the transition still plays for the enabled clip and the disabled side is transparent and silent.
// Solid colours: V1 green under V2 red (a, 0-2 s) then blue (b, 2-4 s) with a centred 0.8 s
// transition. Also: a dissolve with no clip before it rises from what is underneath (not black), and
// a disabled title draws nothing.
async function solidMedia(page, splice) {
  const files = ['red', 'blue', 'lime'].map(color => {
    const file = path.join(splice.temp, `solid-${color}.mp4`);
    if (!fs.existsSync(file)) ffmpeg(['-y', '-f', 'lavfi', '-i', `color=c=${color}:s=160x90:r=25:d=8`, '-c:v', 'libx264',
      '-preset', 'ultrafast', '-g', '1', '-pix_fmt', 'yuv420p', file]);
    return file;
  });
  const tone = path.join(splice.temp, 'solid-tone.wav');
  if (!fs.existsSync(tone)) ffmpeg(['-y', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:d=8', tone]);
  await state(page, () => SpliceSession.newProject({name: 'Solid', width: 160, height: 90, fps: 25}));
  await page.locator('#media-input').setInputFiles([...files, tone]);
  await page.waitForFunction(() => Editor.get().project.media.length === 4);
  return state(page, () => Object.fromEntries(Editor.get().project.media.map(item => [item.name.replace(/\..*$/, '').replace('solid-', ''), item.id])));
}
async function setClips(page, clips) {
  await state(page, next => Editor.editProject(project => { project.clips = next; }), clips);
  await settle(page);
}
async function solidColor(page, time) {
  await state(page, t => Editor.seek(t), time);
  await page.waitForFunction(() => [...document.querySelectorAll('body > video')].filter(element => element.getAttribute('src'))
    .every(element => element.readyState >= 2 && !element.seeking));
  await page.waitForTimeout(150);
  return programColor(page, time);
}
const mix = (a, b, part) => a.map((value, index) => value * (1 - part) + b[index] * part);
const near = (actual, expected, label) =>
  assert.ok(distance(actual, expected) < 14, label + ': ' + JSON.stringify({actual, expected: expected.map(Math.round)}));
async function transitions(page, splice) {
  const ids = await solidMedia(page, splice);
  const clip = (id, mediaId, track, start, duration, extra) => picture(id, mediaId, track, start, duration, extra);
  const cut = (kind, off) => [clip('low', ids.lime, 1, 0, 5), clip('a', ids.red, 2, 0, 2, off === 'a' ? {enabled: false} : {}),
    clip('b', ids.blue, 2, 2, 2, {transition: kind, transitionDuration: .8, transitionBefore: .4, ...(off === 'b' ? {enabled: false} : {})})];
  await setClips(page, cut('dissolve'));
  const green = await solidColor(page, 4.5), red = await solidColor(page, 1), blue = await solidColor(page, 3.5);
  // Dissolve carried by a disabled clip: the clip before dissolves out to V1 (it does not cut).
  await setClips(page, cut('dissolve', 'b'));
  near(await solidColor(page, 1.8), mix(red, green, .25), 'disabled B: A dissolves out to V1 before the cut');
  near(await solidColor(page, 2.2), mix(red, green, .75), 'disabled B: A keeps dissolving out after the cut');
  // Dissolve after a disabled clip: B dissolves in from V1, not from black.
  await setClips(page, cut('dissolve', 'a'));
  near(await solidColor(page, 1.8), mix(green, blue, .25), 'disabled A: B dissolves in from V1');
  // Dip to black carried by a disabled clip: A still dips to black; after the cut V1 shows.
  await setClips(page, cut('fade', 'b'));
  near(await solidColor(page, 1.8), mix(red, [0, 0, 0], .5), 'disabled B: A dips to black');
  near(await solidColor(page, 2.2), green, 'disabled B: nothing after the dip');
  // Wipe carried by a disabled clip: A is wiped off to reveal V1.
  await setClips(page, cut('wipe', 'b'));
  near(await solidColor(page, 2), mix(red, green, .5), 'disabled B: A is wiped off to V1');
  // A dissolve with no clip before it (a gap) rises from what is underneath.
  await setClips(page, [clip('low', ids.lime, 1, 0, 5), clip('b', ids.blue, 2, 2, 2, {transition: 'dissolve', transitionDuration: .8})]);
  near(await solidColor(page, 2.4), mix(green, blue, .5), 'dissolve after a gap rises from V1');
  // Sound: A1 fades out through the crossfade of a disabled clip after it, against silence.
  await setClips(page, [sound('s1', ids.tone, -1, 0, 2), sound('s2', ids.tone, -1, 2, 2,
    {transition: 'dissolve', transitionDuration: .8, transitionBefore: .4, enabled: false})]);
  for (const [time, expected] of [[1.8, .75], [2.2, .25]]) {
    const gains = await state(page, t => { Editor.seek(t); return Editor.audioGains(); }, time);
    assert.deepEqual(Object.keys(gains), ['s1'], 'only the enabled clip sounds at ' + time);
    assert.ok(Math.abs(gains.s1 - expected) < .02, `s1 fades against silence at ${time}: ${JSON.stringify(gains)}`);
  }
  // A disabled title draws nothing.
  const title = {id: 't', mediaId: null, type: 'title', track: 4, start: 0, duration: 4, in: 0, title: 'DISABLED TITLE', color: {},
    volume: 100, transition: 'none'};
  const lit = () => state(page, () => {
    const canvas = document.getElementById('preview-canvas');
    Editor.drawFrame(canvas);
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let count = 0;
    for (let i = 0; i < data.length; i += 4) if (data[i] + data[i + 1] + data[i + 2] > 300) count++;
    return count;
  });
  await setClips(page, [title]);
  await state(page, () => Editor.seek(1));
  const shown = await lit();
  await setClips(page, [{...title, enabled: false}]);
  const hidden = await lit();
  assert.ok(shown > 100 && hidden === 0, 'a disabled title draws nothing: ' + JSON.stringify({shown, hidden}));
  console.log('Transitions: next to a disabled clip they still play against what is underneath (dissolve, dip, wipe, crossfade); disabled titles draw nothing.');
}

// 3. The timeline marks disabled clips (dimmed, hatched, OFF tag, tooltip) and they stay
// selectable, trimmable and movable.
async function timelineLook(page) {
  // Wide enough for the OFF tag beside the whole name (a short clip shows only its name; see followUps).
  await load(page, [picture('v', 'ridge', 1, 0, 6, {linkGroup: 'p'}), sound('a', 'score', -1, 0, 6, {linkGroup: 'p'}),
    picture('solo', 'valley', 2, 3.6, 6, {enabled: false})]);
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
// acts as one, led by the primary (or right-clicked) clip; one undo step; locked tracks refuse.
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
  // Mixed selection acts as one (Premiere's rule): the primary (first selected) clip leads. v1 is
  // disabled, so the whole selection becomes enabled ...
  await clickClip(page, 'v1');
  await clickClip(page, 'v2', {modifiers: ['Control']});
  await page.locator('.edit-menu summary').click();
  assert.match(await page.locator('.edit-menu [data-command=enable]').textContent(), /^Enable clips/);
  await page.keyboard.press('Escape');
  await page.keyboard.press('Shift+E');
  assert.deepEqual(await flags(page), {v1: true, a1: true, v2: true}, 'mixed selection led by a disabled clip: all enabled');
  assert.match(await toast(page), /^1 clip enabled$/, 'the toast counts clips that changed');
  await state(page, () => Editor.undo());
  assert.deepEqual(await flags(page), {v1: false, a1: true, v2: true}, 'one undo step');
  // ... led by an enabled clip, the whole selection becomes disabled. (Clicking a1 first clears
  // the multi-selection; a plain click on a member of it would keep it.)
  await clickClip(page, 'a1');
  await clickClip(page, 'v2');
  await clickClip(page, 'v1', {modifiers: ['Control']});
  await page.keyboard.press('Shift+E');
  assert.deepEqual(await flags(page), {v1: false, a1: true, v2: false}, 'mixed selection led by an enabled clip: all disabled');
  assert.match(await toast(page), /^1 clip disabled$/);
  await state(page, () => Editor.undo());
  // The context menu's lead is the clip under the pointer, and the entry says what it will do.
  await clickClip(page, 'a1');
  await clickClip(page, 'v2');
  await clickClip(page, 'v1', {modifiers: ['Control']});
  await capture(page, `enable-mixed-before-${sizeName(page)}.png`);
  await page.locator('.timeline-clip[data-clip="v1"]').click({button: 'right', position: {x: 12, y: 8}});
  const mixedEntry = page.locator('.sync-menu [role=menuitem]').first();
  assert.match(await mixedEntry.textContent(), /^Enable clips/);
  await mixedEntry.click();
  assert.deepEqual(await flags(page), {v1: true, a1: true, v2: true}, 'context menu on the disabled clip of a mixed selection: all enabled');
  await capture(page, `enable-mixed-after-${sizeName(page)}.png`);
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
  console.log('Commands: Shift+E, Edit menu and context menu toggle; linked pairs together; mixed selections act as one, led by the primary or right-clicked clip; one undo; locked tracks refuse.');
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

// 9. Follow-ups from the validation of this change: the selection highlight after reopening, the
// open report for a Premiere project, the context menu's keyboard, a short disabled clip's name,
// toast placement, enabling removes the key, tiny painted clips, and right-click never drags.
async function followUps(page) {
  const two = () => [picture('v', 'ridge', 1, 0, 2), picture('w', 'valley', 1, 2, 2)];
  // Reopening the same file clears the multi-selection highlight (same clip ids).
  await load(page, two());
  await clickClip(page, 'v');
  await clickClip(page, 'w', {modifiers: ['Control']});
  assert.equal(await page.locator('.timeline-clip.group-selected').count(), 2);
  await load(page, two());
  assert.equal(await page.locator('.timeline-clip.group-selected').count(), 0, 'no stale highlight after reopening');

  // Enabling removes the key (re-enabled clips look like never-disabled ones).
  await state(page, () => { Editor.toggleEnabled(['v']); Editor.toggleEnabled(['v']); });
  assert.equal(await state(page, () => 'enabled' in Editor.get().project.clips.find(c => c.id === 'v')), false);

  // Right-click and drag does not move a clip.
  const box = await page.locator('.timeline-clip[data-clip="w"]').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down({button: 'right'});
  await page.mouse.move(box.x + box.width / 2 + 120, box.y + box.height / 2, {steps: 6});
  await page.mouse.up({button: 'right'});
  await page.keyboard.press('Escape');
  assert.equal(await state(page, () => Editor.get().project.clips.find(c => c.id === 'w').start), 2, 'a right-button drag moves nothing');

  // Context menu keyboard: Up/Down (wrapping), Home/End, Enter/Space activate, Escape returns focus.
  const opened = await page.locator('.timeline-clip[data-clip="v"]');
  await opened.click({button: 'right', position: {x: 12, y: 8}});
  const focused = () => state(page, () => document.activeElement?.closest('.sync-menu') ? document.activeElement.textContent : null);
  assert.match(await focused(), /Disable clip/);
  await page.keyboard.press('ArrowDown');
  assert.match(await focused(), /Link/);
  await capture(page, `enable-context-menu-keyboard-${sizeName(page)}.png`);
  await page.keyboard.press('ArrowDown');
  assert.match(await focused(), /Disable clip/, 'Down wraps to the first entry');
  await page.keyboard.press('ArrowUp');
  assert.match(await focused(), /Link/, 'Up wraps to the last entry');
  await page.keyboard.press('Home');
  assert.match(await focused(), /Disable clip/);
  await page.keyboard.press('End');
  assert.match(await focused(), /Link/);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.sync-menu').count(), 0);
  assert.equal(await state(page, () => document.activeElement?.closest?.('.timeline-clip')?.dataset.clip ||
    (document.activeElement?.closest?.('#timeline-lanes,.track-lanes,.timeline') ? 'timeline' : document.activeElement?.tagName)), 'v',
  'Escape returns focus to the clip');
  await opened.click({button: 'right', position: {x: 12, y: 8}});
  await page.keyboard.press('Enter');
  assert.deepEqual(await flags(page), {v: false, w: true}, 'Enter activates the focused entry');
  await opened.click({button: 'right', position: {x: 12, y: 8}});
  await page.keyboard.press(' ');
  assert.deepEqual(await flags(page), {v: true, w: true}, 'Space activates the focused entry');
  assert.equal(await state(page, () => Editor.get().time), 0, 'Space in the menu does not start playback');

  // A short disabled clip shows its name; the OFF tag only when both fit.
  await load(page, [picture('short', 'ridge', 1, 0, 2, {enabled: false}), picture('long', 'valley', 1, 3, 6, {enabled: false})]);
  const tags = await state(page, () => Object.fromEntries([...document.querySelectorAll('.timeline-clip')].map(node => {
    const name = node.querySelector('.clip-name');
    return [node.dataset.clip, {off: !!node.querySelector('.clip-off'), dashed: getComputedStyle(node).borderStyle,
      nameFits: name.scrollWidth <= name.clientWidth + 1, text: name.textContent}];
  })));
  assert.deepEqual([tags.short.off, tags.short.dashed], [false, 'dashed'], 'short disabled clip: no tag, still dashed ' + JSON.stringify(tags));
  assert.match(tags.short.text, /Ridge/);
  assert.deepEqual([tags.long.off, tags.long.nameFits], [true, true], 'long disabled clip: tag and whole name ' + JSON.stringify(tags));
  await capture(page, `enable-short-disabled-${sizeName(page)}.png`);

  // Tiny clips painted on a lane canvas: a disabled one is drawn hollow and dim, not like an enabled one.
  // (The first clip is selected on open and stays a DOM node, so the far one comes first.)
  await load(page, [picture('far', 'ridge', 1, 1990, 8), picture('en', 'ridge', 1, 0, 8),
    picture('dis', 'valley', 1, 20, 8, {enabled: false})]);
  await page.locator('#zoom-fit').click();
  await settle(page);
  const runs = await state(page, () => {
    const canvas = document.querySelector('.track-lane[data-kind=video][data-track="1"] .lane-density');
    if (!canvas) return null;
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const columns = [];
    for (let x = 0; x < canvas.width; x++) {
      let sum = 0;
      for (let y = 0; y < canvas.height; y++) sum += data[(y * canvas.width + x) * 4 + 3];
      columns.push(sum);
    }
    const found = [];
    for (let x = 0; x < columns.length; x++) {
      if (!columns[x]) continue;
      const start = x;
      let total = 0;
      while (x < columns.length && columns[x]) total += columns[x++];
      found.push({start, width: x - start, mean: total / (x - start)});
    }
    return found;
  });
  assert.ok(runs && runs.length >= 2, 'tiny clips are painted: ' + JSON.stringify(runs));
  assert.ok(runs[1].mean < runs[0].mean * .5, 'a disabled painted clip is hollow and dim: ' + JSON.stringify(runs.slice(0, 2)));

  // Toasts: in the utility bar's empty middle, covering no control; a click dismisses one.
  const original = page.viewportSize();
  await page.setViewportSize({width: 1024, height: 640});
  await load(page, two());
  await state(page, () => SpliceSource.open('ridge'));
  await page.waitForFunction(() => document.querySelector('.workspace.source-open'));
  await page.locator('#scope-dock-toggle').click();
  const toastNode = page.locator('.toast');
  await toastNode.waitFor();
  assert.match(await toastNode.textContent(), /large scope/i);
  const covered = await state(page, () => {
    const toast = document.querySelector('.toast').getBoundingClientRect();
    const hit = element => {
      const box = element.getBoundingClientRect();
      return box.width && box.height && box.left < toast.right && box.right > toast.left && box.top < toast.bottom && box.bottom > toast.top;
    };
    return [...document.querySelectorAll('button, select, input, summary, [role=button], .timeline-clip, video')]
      .filter(element => element.getClientRects().length && !element.closest('.toast') && hit(element))
      .map(element => element.id || element.className || element.tagName);
  });
  assert.deepEqual(covered, [], 'the toast covers no control');
  await capture(page, `enable-toast-placement-1024x640.png`);
  await toastNode.click();
  assert.equal(await page.locator('.toast').count(), 0, 'a click dismisses the toast');
  await page.locator('.source-close').click();
  await page.setViewportSize(original);
  console.log('Follow-ups: selection cleared on reopen, key removed on enable, right-drag inert, menu keyboard, short-clip names, tiny clips, toast placement.');
}

// 10. Opening a Premiere project that holds disabled clips says how many.
async function premiereOpen(page) {
  await load(page, [picture('v', 'ridge', 1, 0, 2), picture('w', 'valley', 2, 0, 2, {enabled: false})]);
  const download = page.waitForEvent('download', {timeout: 60000});
  await state(page, () => SpliceSession.saveAs('prproj'));
  const file = await (await download).path();
  await state(page, () => {
    window.seenToasts = [];
    new MutationObserver(() => {
      const text = document.querySelector('.toast')?.textContent;
      if (text && window.seenToasts.at(-1) !== text) window.seenToasts.push(text);
    }).observe(document.body, {childList: true});
  });
  await page.locator('#project-input').setInputFiles({name: 'Disabled.prproj', mimeType: 'application/octet-stream', buffer: fs.readFileSync(file)});
  await page.waitForFunction(() => window.seenToasts.some(text => /^Project opened/.test(text)), null, {timeout: 60000});
  const last = await state(page, () => window.seenToasts.at(-1));
  assert.match(last, /^Project opened with 1 disabled clip \(marked OFF; Shift\+E enables\)\.$/, JSON.stringify(await state(page, () => window.seenToasts)));
  assert.equal(await page.locator('.timeline-clip.disabled').count(), 1);
  console.log('Premiere open: "' + last + '"');
}

const only = process.env.SPLICE_ONLY?.split(',');
async function main() {
  const splice = await startSplice('splice-enable-');
  try {
    const sections = [
      ['preview', preview], ['transitions', transitions], ['timelineLook', timelineLook], ['commands', commands],
      ['editing', editing], ['persistence', persistence, {once: true}], ['interchange', interchange, {once: true}],
      ['exportRequest', exportRequest, {once: true}], ['followUps', followUps], ['premiereOpen', premiereOpen, {once: true}],
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
