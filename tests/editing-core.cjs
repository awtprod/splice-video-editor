/* Browser regressions for split edits, three-point editing, trims, track output and timecode. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {root, startSplice, openPage, ffmpeg, makeCamera, nearestSegment, colorAt, readPcm, toneAt, segmentTone} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
let pass = 1;
async function capture(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  await page.screenshot({path: path.join(evidence, pass === 1 ? name : name.replace('.png', '-narrow.png'))});
}
const state = (page, fn, arg) => page.evaluate(fn, arg);
const waitFor = (page, fn, arg, timeout = 15000) => page.waitForFunction(fn, arg, {timeout});
// Clip nodes are rebuilt when the timeline re-renders after scrolling, so retry once a frame has passed.
async function clipBox(page, id, selector = '') {
  const target = page.locator(`[data-clip="${id}"] ${selector}`.trim());
  for (let attempt = 0; ; attempt++) {
    try {
      await target.scrollIntoViewIfNeeded({timeout: 2000});
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const box = await target.boundingBox();
      if (box) return box;
      if (attempt === 3) throw Error(`Clip ${id} ${selector} is not on screen`);
    } catch (error) {
      if (attempt === 3) throw error;
    }
  }
}

// Open the source monitor, mark a source range and place it with the given key.
async function sourceEdit(page, mediaId, {from, to, key}) {
  await state(page, id => SpliceSource.open(id), mediaId);
  await waitFor(page, () => document.querySelector('.source-stage video')?.readyState >= 2);
  for (const [edge, value] of [['in', from], ['out', to]]) {
    if (value === undefined) continue;
    await page.locator('.source-scrub').fill(String(value));
    await waitFor(page, target => Math.abs(document.querySelector('.source-stage video').currentTime - target) < .02, value);
    await page.locator(`[data-action=${edge}]`).click();
  }
  await page.keyboard.press(key);
  await waitFor(page, () => !document.querySelector('.source-dialog'));
}
// Drag a clip edge with modifiers held (Alt+Shift rolls only that side's cut).
async function dragEdge(page, id, edge, seconds, modifiers) {
  const pps = await state(page, clipId => {
    const node = document.querySelector(`[data-clip="${clipId}"]`);
    const clip = Editor.get().project.clips.find(c => c.id === clipId);
    return (node.getBoundingClientRect().width + 1) / clip.duration;
  }, id);
  const handle = await clipBox(page, id, edge === 'start' ? '.trim-left' : '.trim-right');
  const x = edge === 'start' ? handle.x + 1 : handle.x + handle.width - 1, y = handle.y + handle.height / 2;
  for (const key of modifiers) await page.keyboard.down(key);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + seconds * pps / 2, y, {steps: 4});
  await page.mouse.move(x + seconds * pps, y, {steps: 4});
  await page.mouse.up();
  for (const key of modifiers.reverse()) await page.keyboard.up(key);
}
const layout = clips => clips.map(c => [c.type, c.track, +c.start.toFixed(3), +c.duration.toFixed(3), +c.in.toFixed(3)])
  .sort((a, b) => a[0].localeCompare(b[0]) || a[2] - b[2]);
// Pair video and audio members of each link group by their timeline starts.
const linkPairs = clips => Object.values(Object.groupBy(clips.filter(c => c.linkGroup), c => c.linkGroup))
  .map(group => group.map(c => c.type + '@' + c.start.toFixed(2)).sort().join(' ')).sort();

async function splitEdits(splice, page, camera) {
  await state(page, () => SpliceSession.newProject({name: 'Split edits', width: 1280, height: 720, fps: 25}));
  await page.locator('#media-input').setInputFiles(camera);
  await waitFor(page, () => Editor.get().project.media[0]?.audioChannels > 0);
  const mediaId = await state(page, () => Editor.get().project.media[0].id);
  await page.keyboard.press('n'); // snapping off: drags land where they are dropped
  // Three-point edits build the cut: timeline In + source In/Out, then timeline In/Out + source In,
  // then a lone timeline Out back-times the source range.
  await state(page, () => Editor.seek(0));
  await page.keyboard.press('i');
  await sourceEdit(page, mediaId, {from: 0, to: 2, key: 'Period'});
  await state(page, () => { Editor.clearMarks(); Editor.seek(2); });
  await page.keyboard.press('i');
  // The playhead stops at the end of the sequence, so marks past it are set directly.
  await state(page, () => Editor.setMark('out', 4));
  await state(page, id => Editor.setSourceSettings(id, {sourceMarks: {}}), mediaId);
  await sourceEdit(page, mediaId, {from: 4, key: 'Period'});
  await state(page, () => { Editor.clearMarks(); Editor.setMark('out', 6); });
  await state(page, id => Editor.setSourceSettings(id, {sourceMarks: {}}), mediaId);
  await sourceEdit(page, mediaId, {from: 2, to: 4, key: 'Period'});
  await page.keyboard.press('Control+Shift+X');
  let clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(layout(clips), [
    ['audio', -1, 0, 2, 0], ['audio', -1, 2, 2, 4], ['audio', -1, 4, 2, 2],
    ['video', 1, 0, 2, 0], ['video', 1, 2, 2, 4], ['video', 1, 4, 2, 2]]);
  assert.deepEqual(linkPairs(clips), ['audio@0.00 video@0.00', 'audio@2.00 video@2.00', 'audio@4.00 video@4.00']);
  assert.ok(clips.filter(c => c.type === 'video').every(c => c.embeddedAudio === false));
  const built = clips;
  const id = (type, start) => built.find(c => c.type === type && Math.abs(c.start - start) < 1e-6).id;
  await page.keyboard.press('Equal');
  await page.keyboard.press('Equal');
  // L-cut: the first shot's sound runs 15 frames under the second picture.
  await dragEdge(page, id('audio', 0), 'end', .6, ['Alt', 'Shift']);
  // J-cut: the third shot's sound starts 15 frames before its picture.
  await dragEdge(page, id('audio', 4), 'start', -.6, ['Alt', 'Shift']);
  clips = await state(page, () => Editor.get().project.clips);
  const expected = [
    ['audio', -1, 0, 2.6, 0], ['audio', -1, 2.6, .8, 4.6], ['audio', -1, 3.4, 2.6, 1.4],
    ['video', 1, 0, 2, 0], ['video', 1, 2, 2, 4], ['video', 1, 4, 2, 2]];
  assert.deepEqual(layout(clips), expected);
  assert.deepEqual(linkPairs(clips), ['audio@0.00 video@0.00', 'audio@2.60 video@2.00', 'audio@3.40 video@4.00']);
  // Split edits keep sync, so no clip reports an offset; the preview plays the L-cut sound under shot two.
  assert.deepEqual(await state(page, () => Editor.get().project.clips.map(c => Editor.syncOffset(c)).filter(Boolean)), []);
  assert.deepEqual(await state(page, () => { Editor.seek(2.25); const s = Editor.get(); return [s.activeClip.in, s.activeAudio.in, s.activeAudio.start]; }), [4, 0, 0]);
  await page.keyboard.press('Backslash');
  await capture(page, 'split-edits-linked.png');
  // Undo and redo the J-cut as one step each.
  await page.keyboard.press('Control+z');
  assert.deepEqual(layout(await state(page, () => Editor.get().project.clips)).find(c => c[0] === 'audio' && c[2] === 2.6), ['audio', -1, 2.6, 1.4, 4.6]);
  await page.keyboard.press('Control+Shift+z');
  assert.deepEqual(layout(await state(page, () => Editor.get().project.clips)), expected);

  // Out of sync: with linked selection off, nudge one sound clip two frames and read the badge.
  await page.locator('#linked-toggle').click();
  await state(page, soundId => Editor.select(soundId), id('audio', 4));
  await page.keyboard.press('Alt+ArrowRight');
  await page.keyboard.press('Alt+ArrowRight');
  await page.locator('#linked-toggle').click();
  await waitFor(page, () => [...document.querySelectorAll('.sync-badge')].map(b => b.textContent).sort().join() === '+2,−2');
  await capture(page, 'split-edits-out-of-sync.png');
  await page.locator(`[data-clip="${id('audio', 4)}"] .sync-badge`).click();
  await page.getByRole('menuitem', {name: 'Move into sync'}).click();
  await waitFor(page, () => !document.querySelector('.sync-badge'));
  assert.deepEqual(layout(await state(page, () => Editor.get().project.clips)), expected);

  // Unlink and relink with Ctrl/Cmd+L.
  await state(page, ids => Editor.select(ids), id('video', 0));
  await page.keyboard.press('Control+l');
  clips = await state(page, () => Editor.get().project.clips);
  assert.equal(linkPairs(clips).length, 2);
  await capture(page, 'split-edits-unlinked.png');
  await page.keyboard.press('Control+z');
  assert.equal(linkPairs(await state(page, () => Editor.get().project.clips)).length, 3);

  // Working project and Premiere project both reopen with the split edits and links.
  const working = path.join(splice.temp, 'split-edits.splice.json');
  const [workingDownload] = await Promise.all([page.waitForEvent('download'), page.keyboard.press('Control+Shift+S')]);
  await workingDownload.saveAs(working);
  const premiere = path.join(splice.temp, 'split-edits.prproj');
  const [premiereDownload] = await Promise.all([page.waitForEvent('download', {timeout: 60000}), page.locator('#save-project').click()]);
  await premiereDownload.saveAs(premiere);
  for (const file of [working, premiere]) {
    await state(page, () => SpliceSession.newProject({name: 'Scratch', width: 1280, height: 720, fps: 25}));
    await page.locator('#project-input').setInputFiles(file);
    await waitFor(page, () => Editor.get().project.clips.length === 6 && !document.getElementById('project-loading'), undefined, 60000);
    clips = await state(page, () => Editor.get().project.clips);
    assert.deepEqual(layout(clips), expected, file);
    assert.deepEqual(linkPairs(clips), ['audio@0.00 video@0.00', 'audio@2.60 video@2.00', 'audio@3.40 video@4.00'], file);
  }

  // Export the reopened Premiere project and read picture and sound back against the timeline.
  await page.locator('#export-video').click();
  const download = page.waitForEvent('download', {timeout: 120000});
  await page.locator('.studio-dialog [type=submit]').click();
  await page.waitForFunction(() => !document.querySelector('.studio-dialog [type=submit]').disabled, null, {timeout: 120000});
  const status = await page.locator('.render-state').textContent();
  if (!status.startsWith('MP4 exported')) { download.catch(() => {}); throw Error('Export failed: ' + status); }
  const movie = path.join(splice.temp, 'split-edits.mp4');
  await (await download).saveAs(movie);
  await page.locator('.studio-dialog [data-cancel]').click();
  const pcm = readPcm(movie);
  const sourceAt = (type, time) => {
    const clip = clips.find(c => c.type === type && c.start <= time && time < c.start + c.duration);
    return clip.in + (time - clip.start);
  };
  const report = [];
  for (const time of [.5, 1.5, 2.25, 2.75, 3.25, 3.75, 4.5, 5.5]) {
    const picture = Math.floor(sourceAt('video', time)), sound = Math.floor(sourceAt('audio', time));
    const seenPicture = nearestSegment(colorAt(movie, time)), heard = toneAt(pcm, time);
    report.push({time, picture, seenPicture, sound: segmentTone(sound), heard: Math.round(heard)});
    assert.equal(seenPicture, picture, JSON.stringify(report));
    assert.ok(Math.abs(heard - segmentTone(sound)) < 30, JSON.stringify(report));
  }
  // The L-cut and J-cut windows really carry the other shot's sound.
  assert.deepEqual(report.filter(r => [2.25, 3.75].includes(r.time)).map(r => [r.seenPicture, r.sound]), [[4, 400], [5, 300]]);
  console.log('Split edits: three-point built, L-cut and J-cut rolled with Alt+Shift, sync badge repaired, ' +
    'reopened from working and Premiere saves, export matches the timeline: ' + JSON.stringify(report));
}

// A fresh project holding the camera source and one linked picture/sound pair.
async function linkedPairProject(page, camera, name) {
  await state(page, title => SpliceSession.newProject({name: title, width: 1280, height: 720, fps: 25}), name);
  await page.locator('#media-input').setInputFiles(camera);
  await waitFor(page, () => Editor.get().project.media[0]?.audioChannels > 0);
  await state(page, () => Editor.editProject(project => {
    const mediaId = project.media[0].id;
    for (const [id, type, track] of [['pair-v', 'video', 1], ['pair-a', 'audio', -1]])
      project.clips.push({id, mediaId, type, track, start: 0, duration: 2, in: 0, linkGroup: 'pair',
        embeddedAudio: type === 'video' ? false : undefined, color: {}, volume: 100, transition: 'none', title: ''});
  }));
  return state(page, () => Editor.get().project.media[0].id);
}
// The source monitor's Overwrite onto a track whose clips are linked to sound (failed before: the
// audio partner was orphaned and the edit aborted with "Linked audio and video must be edited together").
async function overwriteOntoLinked(page, camera) {
  const mediaId = await linkedPairProject(page, camera, 'Overwrite linked');
  await state(page, () => Editor.seek(.4));
  await state(page, id => SpliceSource.open(id), mediaId);
  await waitFor(page, () => document.querySelector('.source-stage video')?.readyState >= 2);
  await page.locator('.source-scrub').fill('5');
  await page.locator('[data-action=in]').click();
  await page.locator('.source-scrub').fill('5.8');
  await page.locator('[data-action=out]').click();
  await page.locator('[data-action=overwrite]').click();
  await waitFor(page, () => !document.querySelector('.source-dialog'));
  const clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(layout(clips), [['audio', -1, 0, .4, 0], ['audio', -1, .4, .8, 5], ['audio', -1, 1.2, .8, 1.2],
    ['video', 1, 0, .4, 0], ['video', 1, .4, .8, 5], ['video', 1, 1.2, .8, 1.2]]);
  assert.equal(linkPairs(clips).length, 3);
  console.log('Source monitor Overwrite onto linked picture and sound passed.');
}
async function threePointAndOverwrite(page, camera) {
  // Insert with a timeline In pushes everything after it; overwrite onto linked clips cuts them cleanly.
  const mediaId = await linkedPairProject(page, camera, 'Three-point');
  await state(page, () => Editor.seek(1));
  await page.keyboard.press('i');
  await state(page, id => Editor.setSourceSettings(id, {sourceMarks: {}}), mediaId);
  await sourceEdit(page, mediaId, {from: 3, to: 4, key: 'Comma'});
  let clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(layout(clips), [
    ['audio', -1, 0, 1, 0], ['audio', -1, 1, 1, 3], ['audio', -1, 2, 1, 1],
    ['video', 1, 0, 1, 0], ['video', 1, 1, 1, 3], ['video', 1, 2, 1, 1]]);
  assert.deepEqual(linkPairs(clips), ['audio@0.00 video@0.00', 'audio@1.00 video@1.00', 'audio@2.00 video@2.00']);
  // Overwrite one second across the cut at 1 s on a track whose clips are linked to sound.
  await state(page, () => { Editor.clearMarks(); Editor.seek(.4); });
  await page.keyboard.press('i');
  await state(page, () => Editor.seek(1.4));
  await page.keyboard.press('o');
  await state(page, id => Editor.setSourceSettings(id, {sourceMarks: {}}), mediaId);
  await sourceEdit(page, mediaId, {from: 5, key: 'Period'});
  clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(layout(clips), [
    ['audio', -1, 0, .4, 0], ['audio', -1, .4, 1, 5], ['audio', -1, 1.4, .6, 3.4], ['audio', -1, 2, 1, 1],
    ['video', 1, 0, .4, 0], ['video', 1, .4, 1, 5], ['video', 1, 1.4, .6, 3.4], ['video', 1, 2, 1, 1]]);
  assert.equal(linkPairs(clips).length, 4);
  await page.keyboard.press('Backslash');
  await capture(page, 'three-point-in-out.png');
  // Lift and extract the marked range.
  await page.keyboard.press('Quote');
  clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(layout(clips), [
    ['audio', -1, 0, .4, 0], ['audio', -1, .4, .6, 3.4], ['audio', -1, 1, 1, 1],
    ['video', 1, 0, .4, 0], ['video', 1, .4, .6, 3.4], ['video', 1, 1, 1, 1]]);
  // The export dialog defaults its custom range to the timeline In/Out.
  await state(page, () => { Editor.setMark('in', .4); Editor.setMark('out', 1.6); });
  await page.locator('#export-video').click();
  assert.deepEqual(await state(page, () => {
    const form = document.querySelector('.studio-dialog form');
    return [form.elements.rangeMode.value, form.elements.rangeStart.valueAsNumber, form.elements.rangeEnd.valueAsNumber];
  }), ['custom', .4, 1.6]);
  await page.locator('.studio-dialog [data-cancel]').click();
  console.log('Three-point insert with timeline In, overwrite across linked clips, extract and In/Out export range passed.');
}

async function trimsAndDelete(page) {
  await state(page, () => {
    Editor.newProject({name: 'Trims', width: 1280, height: 720, fps: 25});
    Editor.editProject(project => {
      project.media = [{id: 'long', name: 'Long', src: 'assets/ridge.mp4', type: 'video', duration: 30}];
      project.clips = ['a', 'b', 'c'].map((id, index) => ({id, mediaId: 'long', type: 'video', track: 1,
        start: index * 2, duration: 2, in: 5 + index * 5, color: {}, volume: 100, transition: 'none'}));
    });
    Editor.select('b');
  });
  // Slide: Alt+. moves the middle clip a frame right; its neighbours give and take the frame.
  await page.keyboard.press('Alt+Period');
  await page.keyboard.press('Alt+Period');
  let clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(layout(clips), [['video', 1, 0, 2.08, 5], ['video', 1, 2.08, 2, 10], ['video', 1, 4.08, 1.92, 15.08]]);
  // Alt+Shift+drag slides with the mouse.
  const box = await clipBox(page, 'b');
  await page.keyboard.down('Alt'); await page.keyboard.down('Shift');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 - box.width / 4, box.y + box.height / 2, {steps: 5});
  await page.mouse.up();
  await page.keyboard.up('Shift'); await page.keyboard.up('Alt');
  clips = await state(page, () => Editor.get().project.clips);
  assert.equal(clips[1].in, 10);
  assert.equal(clips[1].duration, 2);
  assert.ok(Math.abs(clips[1].start - 1.58) < .05, JSON.stringify(layout(clips)));
  assert.ok(Math.abs(clips[0].duration - clips[1].start) < 1e-6 && Math.abs(clips[2].start - clips[1].start - 2) < 1e-6);
  // Keyboard trim of a selected edit, nudge, edit-point navigation and ripple trim to playhead.
  await page.keyboard.press('Control+z');
  await page.keyboard.press('Control+z');
  await page.keyboard.press('Control+z');
  const right = await clipBox(page, 'b', '.trim-right');
  await page.mouse.click(right.x + right.width / 2, right.y + right.height / 2);
  await page.keyboard.press('Control+ArrowLeft');
  await page.keyboard.press('Control+Shift+ArrowLeft');
  clips = await state(page, () => Editor.get().project.clips);
  assert.equal(clips.find(c => c.id === 'b').duration, 1.76);
  await state(page, () => Editor.seek(0));
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  assert.equal(await state(page, () => Editor.get().time), 3.76);
  await page.keyboard.press('ArrowUp');
  assert.equal(await state(page, () => Editor.get().time), 2);
  await state(page, () => { Editor.select('a'); Editor.seek(1.6); });
  await page.keyboard.press('w');
  clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(layout(clips), [['video', 1, 0, 1.6, 5], ['video', 1, 1.6, 1.76, 10], ['video', 1, 3.6, 2, 15]]);
  // Backspace deletes, Shift+Backspace ripple deletes (Mac keyboards have no Delete key).
  await state(page, () => Editor.select('a'));
  await page.keyboard.press('Backspace');
  clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(clips.map(c => [c.id, c.start]), [['b', 1.6], ['c', 3.6]]);
  await state(page, () => Editor.select('b'));
  await page.keyboard.press('Shift+Backspace');
  clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(clips.map(c => [c.id, +c.start.toFixed(3)]), [['c', 1.84]]);
  // Add edit on all unlocked tracks.
  await state(page, () => {
    Editor.editProject(project => project.clips.push({id: 'under', mediaId: 'long', type: 'video', track: 2,
      start: 2, duration: 2, in: 0, color: {}, volume: 100, transition: 'none'}));
    Editor.setTrackState(2, 'video', {locked: true});
    Editor.seek(3);
  });
  await page.keyboard.press('Control+Shift+K');
  clips = await state(page, () => Editor.get().project.clips);
  assert.deepEqual(clips.filter(c => c.track === 1).map(c => [+c.start.toFixed(2), +c.duration.toFixed(2)]), [[1.84, 1.16], [3, .84]]);
  assert.equal(clips.filter(c => c.track === 2).length, 1);
  // Reset returns every adjustment to its default, speed included.
  await state(page, () => {
    Editor.setTrackState(2, 'video', {locked: false});
    Editor.updateClip('under', {color: {brightness: 140, contrast: 90, saturation: 20, exposure: 1, tint: 10},
      volume: 40, pan: 30, opacity: 50, transform: {scale: 150, x: 10, y: 0, rotation: 5}, fadeIn: .5,
      keyframes: {opacity: [{time: 0, value: 0}, {time: 1, value: 100}]}, transition: 'dissolve', transitionDuration: .5});
    Editor.setClipSpeed('under', 2);
    Editor.select('under');
  });
  await page.locator('#reset-clip').click();
  const reset = await state(page, () => Editor.get().project.clips.find(c => c.id === 'under'));
  assert.deepEqual([reset.color, reset.volume, reset.pan, reset.opacity, reset.transform, reset.keyframes, reset.fadeIn,
    reset.transition, reset.speed, reset.duration], [{brightness: 100, contrast: 100, saturation: 100}, 100,
    undefined, undefined, undefined, undefined, undefined, 'none', 1, 2]);
  console.log('Slide (keys and mouse), edit trim keys, edit-point navigation, Q/W ripple trim, Backspace, add edit and reset passed.');
}

// Average colour of the program monitor, painted on demand.
const programColor = page => page.evaluate(() => {
  const canvas = document.createElement('canvas');
  canvas.width = 64; canvas.height = 36;
  Editor.drawFrame(canvas);
  const pixels = canvas.getContext('2d').getImageData(0, 0, 64, 36).data;
  return [0, 1, 2].map(channel => {
    let total = 0;
    for (let index = channel; index < pixels.length; index += 4) total += pixels[index];
    return Math.round(total / (pixels.length / 4));
  });
});
async function hiddenTrack(splice, page) {
  const files = ['red', 'blue'].map(color => {
    const file = path.join(splice.temp, color + '.mp4');
    if (!fs.existsSync(file)) ffmpeg(['-f', 'lavfi', '-i', `color=c=${color}:s=160x90:r=25:d=4`, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', file]);
    return file;
  });
  await state(page, () => SpliceSession.newProject({name: 'Track output', width: 1280, height: 720, fps: 25}));
  await page.locator('#media-input').setInputFiles(files);
  await waitFor(page, () => Editor.get().project.media.length === 2);
  await state(page, () => {
    const id = name => Editor.get().project.media.find(item => item.name.startsWith(name)).id;
    const clip = (clipId, mediaId, track) => ({id: clipId, mediaId, type: 'video', track, start: 0, duration: 4, in: 0,
      color: {}, volume: 100, transition: 'none'});
    Editor.editProject(project => {
      project.clips = [clip('low', id('red'), 1), clip('high', id('blue'), 2),
        {id: 'text', type: 'title', mediaId: null, title: 'HIDDEN TITLE', track: 4, start: 0, duration: 4, in: 0,
          color: {}, volume: 100, transition: 'none'}];
    });
    Editor.seek(1);
  });
  const settled = id => waitFor(page, clipId => Editor.get().activeClip?.id === clipId &&
    Editor.get().video?.readyState >= 2 && !Editor.get().video.seeking, id);
  await settled('high');
  const before = await programColor(page);
  await page.getByRole('button', {name: 'Hide Video 2'}).click();
  await page.getByRole('button', {name: 'Hide Titles'}).click();
  await settled('low');
  const after = await programColor(page);
  assert.deepEqual(await state(page, () => Editor.get().project.trackState), {
    'video:2': {locked: false, syncLocked: true, hidden: true},
    'title:4': {locked: false, syncLocked: true, hidden: true}, 'video:4': {locked: false, syncLocked: true, hidden: true}});
  assert.ok(before[2] > 150 && before[0] < 60, 'blue V2 on top before hiding: ' + before);
  assert.ok(after[0] > 150 && after[2] < 60, 'red V1 after hiding V2: ' + after);
  await capture(page, 'track-output-hidden.png');
  await page.getByRole('button', {name: 'Show Video 2'}).click();
  await settled('high');
  console.log('Hidden video and title tracks leave the program monitor: ' + JSON.stringify({before, after}));
  // Media pool: search covers bin, path and camera metadata; the list view sorts by column.
  await state(page, () => Editor.editProject(project => {
    Object.assign(project.media.find(item => item.name.startsWith('red')), {bin: 'Day 2/B cam', reel: 'B004', timecode: '09:14:00:00', rate: '24000/1001'});
    Object.assign(project.media.find(item => item.name.startsWith('blue')), {sourcePath: '/shoot/A001C007.mov', reel: 'A001'});
  }));
  const names = () => page.locator('#library-body [data-media] .media-name, #library-body .media-row span:first-child').allTextContents();
  for (const [term, expected] of [['b cam', ['red.mp4']], ['a001c007', ['blue.mp4']], ['b004', ['red.mp4']]]) {
    await page.locator('#media-search').fill(term);
    assert.deepEqual(await names(), expected, term);
  }
  await page.locator('#media-search').fill('');
  await page.locator('#media-view').click();
  await page.getByRole('columnheader', {name: 'Reel'}).click();
  assert.deepEqual(await names(), ['blue.mp4', 'red.mp4']);
  await page.getByRole('columnheader', {name: /Reel/}).click();
  assert.deepEqual(await names(), ['red.mp4', 'blue.mp4']);
  assert.ok((await page.locator('.media-row').first().textContent()).includes('23.976'));
  await capture(page, 'media-pool-list.png');
  await page.locator('#media-view').click();
  console.log('Media search over bin, path and reel, and sortable list columns passed.');
}

async function premiereMutedTrack(page) {
  await page.locator('#project-input').setInputFiles(path.join(root, 'tests/fixtures/13_track_mute.prproj'));
  await waitFor(page, () => Editor.get().project.premiereOriginal && !document.getElementById('project-loading'), undefined, 60000);
  assert.deepEqual(await state(page, () => Editor.get().project.trackState['video:1']), {locked: false, syncLocked: true, hidden: true});
  assert.equal(await page.getByRole('button', {name: 'Show Video 1'}).getAttribute('aria-pressed'), 'true');
  await capture(page, 'track-header-eye.png');
  console.log('Premiere fixture 13_track_mute opens with Video 1 hidden.');
}

async function timecode(page) {
  await state(page, () => Editor.newProject({name: 'NTSC', width: 1280, height: 720, fps: 30000 / 1001}));
  assert.equal(await state(page, () => Editor.formatTimecode(3600, 30000 / 1001)), '01:00:00;00');
  assert.equal(await state(page, () => Editor.formatTimecode(3600, 60000 / 1001)), '01:00:00;00');
  assert.equal(await state(page, () => Editor.formatTimecode(3600, 25)), '01:00:00:00');
  await state(page, () => Editor.editProject(project => {
    project.media = [{id: 'long', name: 'Long', src: 'assets/ridge.mp4', type: 'video', duration: 7200}];
    project.clips = [{id: 'hour', mediaId: 'long', type: 'video', track: 1, start: 0, duration: 3700, in: 0, color: {}, volume: 100, transition: 'none'}];
  }));
  assert.equal(await page.locator('#viewer-time').textContent(), '01:00:00;00');
  await state(page, () => Editor.seek(3600));
  await waitFor(page, () => document.querySelector('#viewer-time').textContent === '02:00:00;00');
  // Type a timecode to move the playhead; relative entries move from the current frame.
  await page.locator('#viewer-time').click();
  await page.keyboard.type('01:10:00;02');
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#viewer-time').textContent(), '01:10:00;02');
  assert.equal(await state(page, () => Math.round(Editor.get().time * Editor.get().project.fps)), 17984);
  await page.locator('#viewer-time').click();
  await page.keyboard.type('+1:00');
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#viewer-time').textContent(), '01:10:01;02');
  // Existing projects without a start keep 00:00:00:00; the sequence setting changes it.
  await state(page, () => Editor.openProject(new File([JSON.stringify({name: 'Old', width: 1280, height: 720, fps: 25, media: [], clips: []})], 'old.splice.json')));
  assert.equal(await page.locator('#viewer-time').textContent(), '00:00:00:00');
  await page.locator('.file-menu summary').click();
  await page.locator('#sequence-settings').click();
  await page.locator('.studio-dialog [name=start]').fill('10:00:00:00');
  await page.locator('.studio-dialog .primary').click();
  assert.equal(await page.locator('#viewer-time').textContent(), '10:00:00:00');
  console.log('Drop-frame display, 01:00:00;00 start, timecode entry and start timecode setting passed.');
}

// SPLICE_ONLY=hidden,timecode runs chosen scenarios; each one stands alone.
const only = process.env.SPLICE_ONLY?.split(',');
async function main() {
  const splice = await startSplice('splice-editing-');
  try {
    const camera = path.join(splice.temp, 'camera.mp4');
    makeCamera(camera);
    const sections = [
      ['split', page => splitEdits(splice, page, camera)],
      ['threePoint', page => threePointAndOverwrite(page, camera)],
      ['overwrite', page => overwriteOntoLinked(page, camera)],
      ['trims', trimsAndDelete],
      ['hidden', page => hiddenTrack(splice, page)],
      ['premiere', premiereMutedTrack],
      ['timecode', timecode],
    ];
    // Every flow runs twice: a desktop window, then a smaller one.
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}]) {
      const {page, errors} = await openPage(splice, viewport);
      console.log(`Pass ${pass} at ${viewport.width} × ${viewport.height}`);
      for (const [name, run] of sections)
        if (!only || only.includes(name)) await run(page);
      assert.deepEqual(errors, []);
      await page.context().close();
      pass++;
    }
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
