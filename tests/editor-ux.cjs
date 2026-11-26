/* Editor-facing follow-ups found by using Splice as a film editor: track targeting for source
 * edits, the program monitor at the sequence end, the sync menu, links through lift and extract,
 * start timecode and export ranges, ruler labels, timeline and panel layout at 1440 × 900,
 * 1180 × 780 and 1024 × 640, the large scope and the export dialog's audio fields.
 * SPLICE_ONLY=<name>[,<name>] runs chosen scenarios; SPLICE_EVIDENCE_DIR saves screenshots. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {root, startSplice, openPage} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const state = (page, fn, arg) => page.evaluate(fn, arg);
const waitFor = (page, fn, arg, timeout = 15000) => page.waitForFunction(fn, arg, {timeout});
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
async function capture(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  await page.screenshot({path: path.join(evidence, name)});
}
const sizeName = page => page.viewportSize().width + 'x' + page.viewportSize().height;

// A camera clip (picture with two channels of sound) and a music file, at 25 fps.
const camera = {id: 'cam', name: 'Interview_CamA_take3.mp4', src: 'assets/ridge.mp4', type: 'video', duration: 9, audioChannels: 2,
  sourcePath: '/media/projects/feature/Shoot_Day_04/CARD_A/CLIP/Interview_CamA_take3.mp4'};
const music = {id: 'mus', name: 'Score.wav', src: 'assets/score.wav', type: 'audio', duration: 32, audioChannels: 2};
const picture = (id, track, start, duration, extra = {}) => ({id, mediaId: 'cam', type: 'video', track, start, duration, in: 0,
  color: {}, volume: 100, transition: 'none', title: '', embeddedAudio: false, ...extra});
const sound = (id, track, start, duration, extra = {}) => ({id, mediaId: 'cam', type: 'audio', track, start, duration, in: 0,
  volume: 100, transition: 'none', title: '', ...extra});
// The dialogue pair every scene starts from: V1 and A1, linked.
const basePair = () => [picture('v1', 1, 0, 4, {linkGroup: 'base'}), sound('a1', -1, 0, 4, {linkGroup: 'base'})];
async function load(page, project = {}, name = 'ux.splice.json') {
  await state(page, async ([text, file]) => {
    await Editor.openProject(new File([text], file));
  }, [JSON.stringify({name: 'Editor UX', width: 1280, height: 720, fps: 25, media: [camera, music], clips: basePair(), ...project}), name]);
  await page.evaluate(() => document.activeElement?.blur?.());
  await settle(page);
}
const clipsOf = page => state(page, () => Editor.get().project.clips.map(c => ({id: c.id, type: c.type, track: c.track,
  start: Math.round(c.start * Editor.get().project.fps), duration: Math.round(c.duration * Editor.get().project.fps), link: c.linkGroup || null})));
async function openSource(page, id = 'cam') {
  await state(page, media => SpliceSource.open(media), id);
  await waitFor(page, () => document.querySelector('.source-stage video,.source-stage audio')?.readyState >= 1);
}
// Drop the source monitor's range on a timeline lane at a time, the way a drag from its picture
// ends. The drop point is taken from the playhead parked at that time. Returns the hint the
// timeline showed while the drag was over the lane.
async function dropOnLane(page, kind, track, seconds, insert = false) {
  await state(page, time => Editor.seek(time), seconds);
  await settle(page);
  return state(page, ([kind, track, insert]) => {
    const lane = document.querySelector(`.track-lane[data-kind="${kind}"][data-track="${track}"]`);
    const box = lane.getBoundingClientRect();
    const x = document.getElementById('playhead').getBoundingClientRect().left;
    const data = new DataTransfer();
    data.setData('application/x-splice-source', SpliceSource.mediaId());
    const options = {bubbles: true, cancelable: true, dataTransfer: data, clientX: x, clientY: box.top + box.height / 2, ctrlKey: insert};
    lane.dispatchEvent(new DragEvent('dragover', options));
    const hint = document.getElementById('timeline-status').textContent;
    lane.dispatchEvent(new DragEvent('drop', options));
    return hint;
  }, [kind, track, insert]);
}

// 1. Track targeting: picture to the track it is dropped on (or the chosen one), sound to the
// audio track with the same number, sound only on an audio track, locked tracks never edited.
async function targeting(page) {
  await load(page);
  await openSource(page);
  await state(page, () => Editor.setSourceSettings('cam', {sourceMarks: {in: 1, out: 2}}));
  // Dropped on V2 at 1 s: picture V2, sound A2. The V1/A1 dialogue is untouched and stays linked.
  const before = await clipsOf(page);
  const hint = await dropOnLane(page, 'video', 2, 1);
  assert.match(hint, /Picture → V2, sound → A2/);
  let clips = await clipsOf(page);
  const placed = clips.filter(c => !before.some(b => b.id === c.id));
  assert.deepEqual(placed.map(c => [c.type, c.track, c.start, c.duration]).sort(), [['audio', -2, 25, 25], ['video', 2, 25, 25]]);
  assert.equal(placed[0].link, placed[1].link);
  assert.ok(placed[0].link);
  assert.deepEqual(clips.filter(c => before.some(b => b.id === c.id)), before, 'the base pair on V1/A1 is unchanged');
  await state(page, () => Editor.undo());
  assert.deepEqual(await clipsOf(page), before, 'one undo removes picture and sound');
  // Dropped on A3: sound only.
  const soundHint = await dropOnLane(page, 'audio', -3, 2);
  assert.match(soundHint, /Sound only → A3/);
  clips = await clipsOf(page);
  assert.deepEqual(clips.filter(c => !before.some(b => b.id === c.id)).map(c => [c.type, c.track, c.start]), [['audio', -3, 50]]);
  await state(page, () => Editor.undo());
  // Buttons and keys use the source panel's pickers; the plan says where picture and sound land.
  await page.locator('.source-track').selectOption('3');
  await waitFor(page, () => /Picture → V3, sound → A3/.test(document.querySelector('.source-plan').textContent));
  await state(page, () => Editor.seek(3));
  await page.locator('.source-actions [data-action=overwrite]').click();
  clips = await clipsOf(page);
  assert.deepEqual(clips.filter(c => !before.some(b => b.id === c.id)).map(c => [c.type, c.track, c.start]).sort(), [['audio', -3, 75], ['video', 3, 75]]);
  await state(page, () => Editor.undo());
  // Another sound track can be chosen; "in picture clip" keeps the sound embedded.
  await page.locator('.source-audio-track').selectOption('-1');
  await waitFor(page, () => /Picture → V3, sound → A1/.test(document.querySelector('.source-plan').textContent));
  await page.locator('.source-audio-track').selectOption('');
  await waitFor(page, () => /sound in the picture clip/.test(document.querySelector('.source-plan').textContent));
  await page.locator('.source-audio-track').selectOption('follow');
  // A locked target is named in the plan and the edit is refused; nothing changes.
  await state(page, () => Editor.setTrackState(-3, 'audio', {locked: true}));
  await waitFor(page, () => /sound → A3 \(locked\)/.test(document.querySelector('.source-plan').textContent));
  const locked = await clipsOf(page);
  await page.locator('.source-actions [data-action=insert]').click();
  assert.match(await page.locator('.source-status').textContent(), /Unlock audio track 3/);
  assert.deepEqual(await clipsOf(page), locked);
  const lockedHint = await dropOnLane(page, 'video', 3, 6);
  assert.match(lockedHint, /A3 \(locked\)/);
  assert.deepEqual(await clipsOf(page), locked);
  await state(page, () => Editor.setTrackState(-3, 'audio', {locked: false}));
  // Insert onto V2 with sync lock: the base pair moves together and stays linked.
  await page.locator('.source-track').selectOption('2');
  await state(page, () => Editor.seek(1));
  await page.locator('.source-actions [data-action=insert]').click();
  clips = await clipsOf(page);
  const base = clips.filter(c => c.id === 'v1' || c.id === 'a1' || c.link === clips.find(x => x.id === 'v1')?.link);
  assert.ok(clips.filter(c => c.track === 2 || c.track === -2).length === 2, JSON.stringify(clips));
  assert.ok(base.every(c => c.link), 'the V1/A1 pieces stay linked after an insert: ' + JSON.stringify(clips));
  // A source with no sound refuses a drop on an audio track.
  await load(page, {media: [camera, {...camera, id: 'mute', name: 'Mute.mp4', audioChannels: 0}]});
  await openSource(page, 'mute');
  const muteHint = await dropOnLane(page, 'audio', -2, 1);
  assert.match(muteHint, /No sound/);
  assert.match(await page.locator('.source-status').textContent(), /has no sound/);
  await state(page, () => document.querySelector('.source-close').click());
  console.log('Source edits: picture to the drop or chosen video track, sound to the matching audio track, sound only on audio tracks, locked tracks refused.');
}

// 2. The program monitor shows black at or past the end of a sequence that has clips; the
// "Your canvas is ready" placeholder is only for an empty sequence.
async function monitorEnd(page) {
  await load(page);
  await state(page, () => Editor.seek(Editor.get().duration));
  await settle(page);
  const atEnd = await state(page, () => ({
    placeholder: getComputedStyle(document.getElementById('preview-empty')).display,
    pixel: [...document.getElementById('preview-canvas').getContext('2d').getImageData(480, 270, 1, 1).data].slice(0, 3)}));
  assert.deepEqual(atEnd, {placeholder: 'none', pixel: [0, 0, 0]});
  await load(page, {clips: []});
  assert.equal(await state(page, () => getComputedStyle(document.getElementById('preview-empty')).display), 'flex');
  console.log('Program monitor: black at the sequence end, placeholder only for an empty sequence.');
}

// 3. The first click on a sync badge after loading opens its menu, and it stays open; a real
// scroll of the timeline still closes it.
async function syncMenu(page) {
  await load(page, {clips: [picture('v', 1, 0, 4, {linkGroup: 'g'}), sound('a', -1, .4, 4, {in: .6, linkGroup: 'g'}), picture('w', 2, 5, 1)]});
  await page.locator('.sync-badge').first().click();
  await page.waitForTimeout(250);
  assert.equal(await page.locator('.sync-menu').count(), 1, 'the menu stays open after the first click');
  await state(page, () => { const scroll = document.getElementById('track-scroll'); scroll.scrollLeft += 200; });
  await page.waitForTimeout(150);
  assert.equal(await page.locator('.sync-menu').count(), 0, 'scrolling the timeline closes it');
  await page.locator('.sync-badge').first().click().catch(async () => {
    await state(page, () => { document.getElementById('track-scroll').scrollLeft = 0; });
    await page.locator('.sync-badge').first().click();
  });
  await page.waitForTimeout(250);
  assert.equal(await page.locator('.sync-menu').count(), 1);
  await page.keyboard.press('Escape');
  console.log('Sync menu opens on the first click and closes on a real scroll.');
}

// 4. Lift, extract, overwrite, split and ripple delete through split-edit linked pairs keep
// every piece that is still a valid pair linked.
async function links(page) {
  const result = await state(page, async ([media]) => {
    const f = 1 / 24, out = {};
    const pair = (trim) => ({name: 'P', width: 320, height: 180, fps: 24, markers: [], captions: [], media: [media],
      clips: [{id: 'v', type: 'video', track: 1, embeddedAudio: false, mediaId: 'cam', start: 0, duration: 2, in: 1, title: '', color: {}, volume: 100, transition: 'none', linkGroup: 'p'},
        {id: 'a', type: 'audio', track: -1, mediaId: 'cam', start: 0, duration: 2, in: 1, title: '', volume: 100, transition: 'none', linkGroup: 'p'}]});
    const shape = () => Editor.get().project.clips.map(c => [c.type[0], Math.round(c.start / f), Math.round(c.duration / f), c.linkGroup ? 'L' : '-'])
      .sort((x, y) => x[1] - y[1] || x[0].localeCompare(y[0]));
    const groups = () => new Set(Editor.get().project.clips.map(c => c.linkGroup).filter(Boolean)).size;
    const open = async (trim) => {
      await Editor.openProject(new File([JSON.stringify(pair(trim))], 'p.splice.json'));
      // A J-cut: the sound starts 3 frames after the picture.
      if (trim) Editor.withoutLinks(() => Editor.trimEdge('a', 'start', 3 * f));
    };
    for (const extract of [true, false]) {
      await open(true);
      Editor.setMark('in', 10 * f); Editor.setMark('out', 14 * f);
      Editor.liftRange({extract});
      out[extract ? 'extract' : 'lift'] = {clips: shape(), groups: groups()};
    }
    await open(true);
    Editor.setMark('in', 10 * f); Editor.setMark('out', 14 * f);
    Editor.placeSource('cam', {mode: 'overwrite', track: 1, audioTrack: -1, marks: {in: 5, out: 5 + 5 * f}});
    out.overwrite = {clips: shape(), groups: groups()};
    await open(true);
    Editor.seek(20 * f); Editor.select('v'); Editor.split();
    out.split = {clips: shape(), groups: groups()};
    await open(true);
    Editor.seek(20 * f); Editor.select('v'); Editor.split();
    const right = Editor.get().project.clips.find(c => c.type === 'video' && c.start > 0);
    Editor.select(Editor.get().project.clips.find(c => c.type === 'video' && c.start === 0).id);
    Editor.deleteSelected({ripple: true});
    out.rippleDelete = {clips: shape(), groups: groups(), rightKept: !!right};
    return out;
  }, [camera]);
  // Extract frames 10-14 (Out included): the head (J-cut kept) and the tail are each a linked pair.
  assert.deepEqual(result.extract, {clips: [['v', 0, 10, 'L'], ['a', 3, 7, 'L'], ['a', 10, 33, 'L'], ['v', 10, 33, 'L']], groups: 2}, JSON.stringify(result));
  assert.deepEqual(result.lift, {clips: [['v', 0, 10, 'L'], ['a', 3, 7, 'L'], ['a', 15, 33, 'L'], ['v', 15, 33, 'L']], groups: 2}, JSON.stringify(result));
  assert.equal(result.overwrite.groups, 3, JSON.stringify(result.overwrite));
  assert.ok(result.overwrite.clips.every(c => c[3] === 'L'), JSON.stringify(result.overwrite));
  assert.deepEqual(result.split, {clips: [['v', 0, 20, 'L'], ['a', 3, 17, 'L'], ['a', 20, 28, 'L'], ['v', 20, 28, 'L']], groups: 2}, JSON.stringify(result));
  assert.deepEqual(result.rippleDelete.clips, [['a', 0, 28, 'L'], ['v', 0, 28, 'L']], JSON.stringify(result));
  console.log('Lift, extract, overwrite, split and ripple delete keep valid pieces of split-edit pairs linked.');
}

// 5. Invalid stored start timecode opens with a compatibility line naming it; the video export
// dialog reads a lone In as In to the end and a lone Out as start to Out.
async function timecodeAndRange(page) {
  for (const [fps, text] of [[25, '-01:00:00:00'], [25, '25:00:00:00'], [29.97, '01:00:00:30'], [29.97, '00:01:00;00'], [24, 'garbage']]) {
    await load(page, {fps: fps === 29.97 ? 30000 / 1001 : fps, startTimecode: text});
    const report = await state(page, () => ({start: Editor.get().project.startTimecode, issues: SpliceDialogs.compatibility()}));
    assert.equal(report.start, 0);
    assert.ok(report.issues.some(issue => issue.includes('"' + text + '"')), JSON.stringify({text, report}));
    assert.match(await page.locator('#compatibility').textContent(), /\(\d+\)/);
  }
  await load(page, {clips: [picture('v', 1, 0, 8)]});
  const ranges = {};
  for (const [name, marks] of [['in', {in: 2}], ['out', {out: 3}], ['both', {in: 2, out: 3}], ['none', {}]]) {
    await state(page, m => {
      Editor.clearMarks();
      if ('in' in m) Editor.setMark('in', m.in);
      if ('out' in m) Editor.setMark('out', m.out - 1 / 25);
    }, marks);
    const opened = await state(page, () => {
      try { SpliceDialogs.exportVideo(); } catch (error) { return error.message; }
      return [...document.querySelectorAll('dialog')].map(d => d.className + d.open).join();
    });
    assert.match(opened, /studio-dialogtrue/, opened);
    ranges[name] = await state(page, () => {
      const form = document.querySelector('dialog[open] form');
      if (!form) throw Error('no export form: ' + [...document.querySelectorAll('dialog')].map(d => d.className + ':' + d.open + ':' + d.textContent.slice(0, 60)).join(' | '));
      const result = [form.elements.rangeMode.value, Number(form.elements.rangeStart.value), Number(form.elements.rangeEnd.value)];
      form.closest('dialog').close();
      return result;
    });
  }
  assert.deepEqual(ranges, {in: ['custom', 2, 8], out: ['custom', 0, 3], both: ['custom', 2, 3], none: ['all', ranges.none[1], 8]});
  console.log('Invalid start timecode is reported, and lone In or Out marks set the export range.');
}

// 6. Ruler labels at 29.97 and 59.94 fall on whole timecode seconds at every zoom level.
async function ruler(page) {
  for (const [fps, drop] of [[30000 / 1001, true], [60000 / 1001, true], [30000 / 1001, false], [24000 / 1001, false]]) {
    await load(page, {fps, dropFrame: drop, startTimecode: drop ? undefined : 0, clips: [picture('v', 1, 0, 700)]});
    for (const zoom of [0, 3, 6, 9, 12, 16, 24]) {
      await state(page, steps => {
        document.getElementById('zoom-fit').click();
        for (let i = 0; i < steps; i++) document.getElementById('zoom-in').click();
        document.getElementById('track-scroll').scrollLeft = 1000 + steps * 997;
      }, zoom);
      await settle(page);
      const labels = await state(page, () => [...document.querySelectorAll('.ruler-tick')].map(tick => tick.textContent));
      assert.ok(labels.length >= 3, `${labels.length} ticks at zoom ${zoom}`);
      const nominal = Math.round(fps), dropped = nominal / 15;
      // Second steps: frame field 00, or the first frame a drop-frame minute has. Frame steps: a
      // steady multiple of the step in the frame field.
      const fields = labels.map(label => label.split(/[:;]/).map(Number));
      const counts = fields.map(([hours, minutes, seconds, frames]) => ((hours * 60 + minutes) * 60 + seconds) * nominal + frames);
      const step = Math.min(...counts.slice(1).map((count, index) => count - counts[index]));
      const dropStart = ([, minutes, seconds, frames]) => drop && seconds === 0 && minutes % 10 !== 0 && frames === dropped;
      const frameTicks = step > 0 && fields.every(field => field[3] % step === 0 || dropStart(field));
      const whole = fields.every(field => field[3] === 0 || dropStart(field));
      assert.ok(whole || frameTicks, `fps ${fps} zoom ${zoom}: ${labels.join(' ')}`);
      if (zoom <= 9) assert.ok(whole, `fps ${fps} zoom ${zoom}: ${labels.join(' ')}`);
    }
  }
  console.log('Ruler ticks fall on whole timecode seconds (drop-frame aware) at every zoom.');
}

// Geometry helpers for the layout checks.
const layoutOf = page => state(page, () => {
  const rect = node => { const r = node.getBoundingClientRect(); return {left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height}; };
  const scroll = document.getElementById('track-scroll'), ruler = document.getElementById('time-ruler');
  const view = rect(scroll);
  const top = rect(ruler).bottom, bottom = view.top + scroll.clientHeight;
  const lanes = [...document.querySelectorAll('.track-lane')].map(lane => {
    const box = rect(lane);
    return {key: lane.dataset.kind + ':' + lane.dataset.track, clips: lane.querySelectorAll('.timeline-clip').length,
      visible: box.top >= top - 1 && box.bottom <= bottom + 1 && box.bottom <= innerHeight, height: box.height};
  });
  return {lanes, canvas: rect(document.getElementById('preview-canvas')), viewport: {width: innerWidth, height: innerHeight},
    toasts: [...document.querySelectorAll('.toast')].filter(t => getComputedStyle(t).display !== 'none' && t.textContent).map(rect),
    timeline: rect(document.querySelector('.timeline')), scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight};
});
const overlaps = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;

// 7. The tracks holding clips, and always V1 and A1, are visible without scrolling; an edit
// scrolls the track it changed into view; 1024 × 640 keeps a usable monitor and timeline.
async function timelineLayout(page) {
  const clips = [...basePair(), picture('v2', 2, 1, 2, {linkGroup: 'b'}), sound('a2', -2, 1, 2, {linkGroup: 'b'})];
  await load(page, {clips});
  for (const source of [false, true]) {
    if (source) await openSource(page);
    await settle(page);
    await page.waitForTimeout(100);
    const layout = await layoutOf(page);
    // From 1180 × 780 up every track holding clips is in view; at the 1024 × 640 minimum, V1 and A1.
    const roomy = layout.viewport.width >= 1180;
    const required = layout.lanes.filter(lane => (roomy && lane.clips) || lane.key === 'video:1' || lane.key === 'audio:-1');
    assert.ok(required.every(lane => lane.visible), JSON.stringify({source, lanes: layout.lanes}));
    assert.ok(layout.scrollWidth <= layout.viewport.width && layout.scrollHeight <= layout.viewport.height, JSON.stringify([layout.viewport, layout.scrollWidth, layout.scrollHeight]));
    assert.ok(layout.canvas.width >= 280 && layout.canvas.height >= 150, 'program monitor ' + JSON.stringify(layout.canvas));
    for (const toast of layout.toasts) assert.ok(!overlaps(toast, layout.timeline) || layout.lanes.every(l => !l.clips), 'a toast covers the timeline');
    await capture(page, `timeline-${sizeName(page)}-${source ? 'source' : 'plain'}.png`);
  }
  // An edit on a track that was scrolled away brings it into view.
  await state(page, () => { document.getElementById('track-scroll').scrollTop = 0; });
  await page.locator('.source-track').selectOption('4');
  await state(page, () => Editor.seek(6));
  await page.locator('.source-actions [data-action=overwrite]').click();
  await page.waitForTimeout(100);
  const after = await layoutOf(page);
  assert.ok(after.lanes.find(lane => lane.key === 'video:4').visible || after.lanes.find(lane => lane.key === 'audio:-4').visible, JSON.stringify(after.lanes));
  await state(page, () => document.querySelector('.source-close').click());
  console.log(`Timeline at ${sizeName(page)}: clip tracks and V1/A1 in view, edited tracks scrolled into view.`);
}

// 8. Linked-media paths stay inside their pool card, cut with an ellipsis, full path in the tooltip.
async function poolPaths(page) {
  await load(page, {media: [camera, {...camera, id: 'cam2', name: 'B002.mp4', sourcePath: '/media/projects/feature/Shoot_Day_04/CARD_B/CLIP/B002_with_a_long_name.mp4'}]});
  const cards = await state(page, () => [...document.querySelectorAll('.media-card')].map(card => {
    const box = card.getBoundingClientRect();
    const overflow = [...card.querySelectorAll('span')].some(span => {
      const r = span.getBoundingClientRect();
      return r.width && (r.right > box.right + 1 || r.left < box.left - 1);
    });
    const path = card.querySelector('.media-path');
    return {overflow, title: card.title + ' ' + path?.title, ellipsis: path && getComputedStyle(path).textOverflow, inside: path && path.scrollWidth > path.clientWidth};
  }));
  const grid = await state(page, () => {
    const boxes = [...document.querySelectorAll('.media-card')].map(card => card.getBoundingClientRect());
    return boxes.length < 2 || boxes[0].right <= boxes[1].left || boxes[0].bottom <= boxes[1].top;
  });
  assert.ok(grid);
  for (const card of cards) {
    assert.equal(card.overflow, false, JSON.stringify(card));
    assert.equal(card.ellipsis, 'ellipsis');
    assert.match(card.title, /\/media\/projects\/feature\/Shoot_Day_04\/CARD_/);
  }
  console.log('Pool cards keep their linked paths inside the card.');
}

// 9. The source panel's track pickers are reachable at every supported size.
async function sourcePickers(page) {
  await load(page);
  await openSource(page);
  const sizes = [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}];
  const original = page.viewportSize();
  for (const size of sizes) {
    await page.setViewportSize(size);
    await settle(page);
    const reach = await state(page, () => [...document.querySelectorAll('.source-track,.source-audio-track')].map(select => {
      const box = select.getBoundingClientRect();
      const hit = document.elementFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2);
      return {inside: box.top >= 0 && box.bottom <= innerHeight && box.left >= 0 && box.right <= innerWidth, hit: hit === select, width: box.width};
    }));
    assert.equal(reach.length, 2);
    assert.ok(reach.every(item => item.inside && item.hit && item.width >= 60), JSON.stringify({size, reach}));
    await capture(page, `source-pickers-${size.width}x${size.height}.png`);
  }
  await page.setViewportSize(original);
  await state(page, () => document.querySelector('.source-close').click());
  console.log('Source track pickers are in view at 1440, 1180 and 1024 px.');
}

// 10. The LUT name has a row of its own in the Color inspector.
async function lutName(page) {
  const lut = {size: 2, title: 'c102_Kodak_2383_D65_film_print_emulation_v3', domainMin: [0, 0, 0], domainMax: [1, 1, 1], data: [0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 1, 0, 1, 1, 1, 1, 1]};
  await load(page, {clips: [picture('v', 1, 0, 4, {color: {lut, lutIntensity: 100}})]});
  await state(page, () => Editor.select('v'));
  await waitFor(page, () => document.querySelector('.splice-lut-name'));
  const name = await state(page, () => {
    const node = document.querySelector('.splice-lut-name'), box = node.getBoundingClientRect();
    return {width: box.width, text: node.textContent, title: node.title, visible: box.width > 0 && box.height > 0};
  });
  assert.ok(name.width >= 150, JSON.stringify(name));
  assert.match(name.text, /c102_Kodak/);
  assert.equal(name.title, lut.title);
  console.log(`LUT name row ${Math.round(name.width)} px wide at ${sizeName(page)}.`);
}

// 11. A large scope docks beside the program monitor with all four scope types, and playback
// with it open stays free of long tasks.
async function scopeDock(page) {
  await load(page, {clips: [picture('v', 1, 0, 6)]});
  await page.locator('#scope-dock-toggle').click();
  await waitFor(page, () => document.querySelector('.scope-dock .splice-scope'));
  await page.waitForTimeout(300);
  const dock = await state(page, () => {
    const canvas = document.querySelector('.scope-dock .splice-scope'), box = canvas.getBoundingClientRect();
    const monitor = document.getElementById('preview-canvas').getBoundingClientRect();
    return {width: box.width, height: box.height, kinds: [...document.querySelectorAll('.scope-dock [data-scope]')].map(b => b.dataset.scope),
      painted: !!SpliceScopes.layoutOf(canvas), monitor: [monitor.width, monitor.height],
      overlap: box.left < monitor.right && monitor.left < box.right && box.top < monitor.bottom && monitor.top < box.bottom};
  });
  assert.ok(dock.width >= 480 && dock.height >= 200, JSON.stringify(dock));
  assert.deepEqual(dock.kinds, ['waveform', 'parade', 'vector', 'histogram']);
  assert.equal(dock.overlap, false);
  assert.ok(dock.monitor[0] >= 280, JSON.stringify(dock));
  for (const kind of dock.kinds) {
    await page.locator(`.scope-dock [data-scope=${kind}]`).click();
    await settle(page);
    assert.ok(await state(page, () => !!SpliceScopes.layoutOf(document.querySelector('.scope-dock .splice-scope'))));
  }
  const playback = await state(page, async () => {
    const tasks = [];
    const observer = new PerformanceObserver(list => tasks.push(...list.getEntries().map(entry => entry.duration)));
    observer.observe({type: 'longtask'});
    Editor.seek(0);
    Editor.togglePlay?.() ?? document.getElementById('play-toggle').click();
    await new Promise(resolve => setTimeout(resolve, 2000));
    if (Editor.get().playing) document.getElementById('play-toggle').click();
    observer.disconnect();
    return tasks;
  });
  assert.ok(playback.filter(ms => ms > 100).length === 0, 'long tasks during playback: ' + JSON.stringify(playback));
  await capture(page, `scope-dock-${sizeName(page)}.png`);
  await page.locator('.scope-dock-close').click();
  assert.equal(await page.locator('.scope-dock').count(), 0);
  console.log(`Large scope ${Math.round(dock.width)} × ${Math.round(dock.height)} px docked; playback long tasks ${playback.length}.`);
}

// 12. The export dialog offers the renderer's limiter and subtitle language options once the
// renderer accepts them (they come with another change; until then this check reports so).
async function exportAudioFields(page) {
  const renderer = ['export_jobs.py', 'render_movie.py'].map(file => fs.readFileSync(path.join(root, file), 'utf8')).join('\n');
  const supported = /limiterCeiling/.test(renderer) && /subtitleLanguage/.test(renderer);
  await load(page);
  await state(page, () => SpliceDialogs.exportVideo());
  const fields = await state(page, () => {
    const form = document.querySelector('dialog[open] form');
    const values = ['limiter', 'limiterCeiling', 'subtitleLanguage'].map(name => form.elements[name] ? String(form.elements[name].type === 'checkbox' ? form.elements[name].checked : form.elements[name].value) : null);
    form.closest('dialog').close();
    return values;
  });
  if (!supported) {
    assert.deepEqual(fields, [null, null, null], 'no fields for options the renderer does not accept yet');
    console.log('SKIPPED export limiter and subtitle language fields: the renderer on this tree does not accept them yet.');
    return;
  }
  assert.deepEqual(fields, ['true', '-1', 'und']);
  console.log('Export dialog offers limiter (on, −1 dBFS) and subtitle language (und).');
  if (/aacPeakSafe/.test(renderer)) {
    await state(page, () => SpliceDialogs.exportVideo());
    const peakSafe = await state(page, () => {
      const form = document.querySelector('dialog[open] form'), box = form.elements.aacPeakSafe;
      const value = box ? [box.type, box.checked, !box.closest('label').hidden] : null;
      form.closest('dialog').close();
      return value;
    });
    assert.deepEqual(peakSafe, ['checkbox', false, true], 'MP4 offers aacPeakSafe, off by default');
    console.log('Export dialog offers aacPeakSafe (off) for MP4.');
  }
}

const only = process.env.SPLICE_ONLY?.split(',');
async function main() {
  const splice = await startSplice('splice-ux-');
  try {
    const sections = [
      ['targeting', targeting],
      ['monitorEnd', monitorEnd],
      ['syncMenu', syncMenu],
      ['links', links, {once: true}],
      ['timecodeAndRange', timecodeAndRange, {once: true}],
      ['ruler', ruler],
      ['timelineLayout', timelineLayout, {small: true}],
      ['poolPaths', poolPaths],
      ['sourcePickers', sourcePickers, {once: true}],
      ['lutName', lutName],
      ['scopeDock', scopeDock],
      ['exportAudioFields', exportAudioFields, {once: true}],
    ];
    let pass = 1;
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
      const small = viewport.width < 1180;
      const {page, errors} = await openPage(splice, viewport);
      console.log(`Pass ${pass} at ${viewport.width} × ${viewport.height}`);
      for (const [name, run, options] of sections) {
        if (only && !only.includes(name)) continue;
        if (options?.once && pass > 1) continue;
        if (small && !options?.small) continue;
        await run(page);
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
