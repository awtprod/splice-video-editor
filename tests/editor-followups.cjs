/* Follow-ups from the last validation of the editor: tracks holding clips are brought into view
 * even when they are far from V1/A1; a picture-only overwrite keeps the sound linked to the
 * picture on both sides of the cut; link bookkeeping for members outside an edit; truthful
 * source-monitor targets; drop-frame ruler ticks; compatibility notes shown on open; track heights
 * and scrolling an edited track into view. Runs at 1440 × 900, 1180 × 780 and 1024 × 640.
 * SPLICE_ONLY=<name>[,<name>] runs chosen scenarios; SPLICE_EVIDENCE_DIR saves screenshots. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const state = (page, fn, arg) => page.evaluate(fn, arg);
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
async function capture(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  await page.screenshot({path: path.join(evidence, name)});
}
const sizeName = page => page.viewportSize().width + 'x' + page.viewportSize().height;

const camera = {id: 'cam', name: 'Interview_CamA_take3.mp4', src: 'assets/ridge.mp4', type: 'video', duration: 9, audioChannels: 2};
const picture = (id, track, start, duration, extra = {}) => ({id, mediaId: 'cam', type: 'video', track, start, duration, in: 0,
  color: {}, volume: 100, transition: 'none', title: '', embeddedAudio: false, ...extra});
const sound = (id, track, start, duration, extra = {}) => ({id, mediaId: 'cam', type: 'audio', track, start, duration, in: 0,
  volume: 100, transition: 'none', title: '', ...extra});
const f = frames => frames / 25;
async function load(page, project = {}) {
  await state(page, async text => { await Editor.openProject(new File([text], 'followups.splice.json')); },
    JSON.stringify({name: 'Follow-ups', width: 1280, height: 720, fps: 25, media: [camera], clips: [], ...project}));
  await state(page, () => Editor.setLinkedSelection(true));
  await page.evaluate(() => document.activeElement?.blur?.());
  await settle(page);
}
const clips = page => state(page, () => Object.fromEntries(Editor.get().project.clips.map(c => [c.id, c])));
const lanes = page => state(page, () => {
  const scroll = document.getElementById('track-scroll');
  const top = document.getElementById('time-ruler').getBoundingClientRect().bottom;
  const bottom = scroll.getBoundingClientRect().top + scroll.clientHeight;
  return Object.fromEntries([...document.querySelectorAll('.track-lane')].map(lane => {
    const box = lane.getBoundingClientRect();
    return [lane.dataset.kind + ':' + lane.dataset.track, {height: Math.round(box.height),
      visible: box.top >= top - 1 && box.bottom <= bottom + 1 && box.bottom <= innerHeight}];
  }));
});
async function openSource(page) {
  await state(page, () => SpliceSource.open('cam'));
  await page.waitForFunction(() => document.querySelector('.source-stage video')?.readyState >= 1);
}

// 2. Clips only on V4 and A4: both are in view when the project opens, at every size.
async function farTracks(page) {
  await load(page, {clips: [picture('v4', 4, 0, 3, {linkGroup: 'p'}), sound('a4', -4, 0, 3, {linkGroup: 'p'})], trackCounts: {video: 6, audio: 6}});
  await page.waitForTimeout(100);
  const seen = await lanes(page);
  // At 1440 × 900 both fit; in smaller windows V4 to A4 is taller than the track area, so the
  // view shows a track that holds clips (before the fix it showed the empty V1/A1 boundary).
  const both = seen['video:4'].visible && seen['audio:-4'].visible;
  if (page.viewportSize().height >= 900) assert.ok(both, sizeName(page) + ' ' + JSON.stringify(seen));
  else assert.ok(seen['video:4'].visible || seen['audio:-4'].visible, sizeName(page) + ' ' + JSON.stringify(seen));
  await capture(page, `far-tracks-${sizeName(page)}.png`);
  // Many used tracks that cannot all fit: the view shows as many of them as it can, not empty V1/A1.
  const many = [];
  for (let n = 5; n <= 12; n++) many.push(picture('v' + n, n, 0, 2), sound('a' + n, -n, 0, 2));
  await load(page, {clips: many});
  await page.waitForTimeout(100);
  const crowded = await lanes(page);
  const shown = Object.entries(crowded).filter(([key, lane]) => lane.visible && /:-?(?:[5-9]|1[0-2])$/.test(key)).length;
  assert.ok(shown >= 3, 'tracks with clips in view: ' + shown + ' ' + JSON.stringify(crowded));
  console.log(`${sizeName(page)}: clips only on V4/A4: ${both ? 'both' : 'one of them'} in view; with 16 used tracks ${shown} are shown.`);
}

// 3 and 5. Link groups through edits.
async function links(page) {
  // Picture-only overwrite through a linked pair: the sound under the cut stays linked to the
  // picture on both sides, so moving the head pair moves the tail with it.
  await load(page, {clips: [picture('v', 1, 0, f(58), {linkGroup: 'p'}), sound('a', -1, 0, f(58), {linkGroup: 'p'})]});
  await state(page, ([from, length]) => Editor.placeSource('cam', {mode: 'overwrite', track: 1, audioTrack: null, marks: {in: 4, out: 4 + length}, at: from}), [f(20), f(12)]);
  let now = Object.values(await clips(page));
  const head = now.find(c => c.type === 'video' && c.start === 0), tail = now.find(c => c.type === 'video' && Math.round(c.start * 25) === 32);
  const middle = now.find(c => c.type === 'video' && Math.round(c.start * 25) === 20), audio = now.find(c => c.type === 'audio');
  assert.ok(head.linkGroup && head.linkGroup === tail.linkGroup && tail.linkGroup === audio.linkGroup, JSON.stringify(now));
  assert.equal(middle.linkGroup, undefined, 'the new picture is not part of the pair');
  // The validator's repro: moving the head pair takes the tail picture and the sound along.
  await state(page, ([id, by]) => Editor.moveClip(id, by, 1), [head.id, f(100)]);
  const shifted = await clips(page);
  assert.deepEqual([shifted[head.id].start, shifted[tail.id].start, shifted[audio.id].start].map(t => Math.round(t * 25)), [100, 132, 100],
    'the tail picture and the sound move with the head');
  assert.equal(Math.round(shifted[middle.id].start * 25), 20, 'the new picture stays');

  // Insert on V1 with A1 sync lock off: the tail picture moves away from the sound, so it is
  // unlinked (a lone piece is never kept linked); the head stays linked to the sound.
  await load(page, {clips: [picture('v', 1, 0, f(58), {linkGroup: 'p'}), sound('a', -1, 0, f(58), {linkGroup: 'p'})]});
  await state(page, () => Editor.setTrackState(-1, 'audio', {syncLocked: false}));
  await state(page, ([from]) => Editor.withoutLinks(() => Editor.placeSource('cam', {mode: 'insert', track: 1, audioTrack: null, marks: {in: 4, out: 4.48}, at: from})), [f(20)]);
  now = Object.values(await clips(page));
  const insertTail = now.find(c => c.type === 'video' && Math.round(c.start * 25) === 32);
  const insertHead = now.find(c => c.type === 'video' && c.start === 0);
  assert.equal(insertTail.linkGroup, undefined, 'a drifted lone piece is unlinked: ' + JSON.stringify(now));
  assert.ok(insertHead.linkGroup && insertHead.linkGroup === now.find(c => c.type === 'audio').linkGroup);

  // Three members, one of which the insert leaves behind (A2 not sync-locked, after the cut):
  // the moved pieces stay linked together and the drifted member is unlinked.
  await load(page, {clips: [picture('v', 1, 0, f(58), {linkGroup: 'p'}), sound('a', -1, 0, f(58), {linkGroup: 'p'}),
    sound('a2', -2, f(30), f(28), {linkGroup: 'p', in: f(30)})]});
  await state(page, () => Editor.setTrackState(-2, 'audio', {syncLocked: false}));
  await state(page, ([from]) => Editor.withoutLinks(() => Editor.placeSource('cam', {mode: 'insert', track: 1, audioTrack: -1, marks: {in: 4, out: 4.48}, at: from})), [f(20)]);
  now = await clips(page);
  const pieces = Object.values(now);
  const vTail = pieces.find(c => c.type === 'video' && Math.round(c.start * 25) === 32), aTail = pieces.find(c => c.track === -1 && Math.round(c.start * 25) === 32);
  assert.ok(vTail.linkGroup && vTail.linkGroup === aTail.linkGroup, 'moved pieces stay linked: ' + JSON.stringify(pieces));
  assert.equal(now.a2.linkGroup, undefined, 'the member left behind is unlinked: ' + JSON.stringify(now.a2));

  // A member outside a cleared range, after it, joins the pieces after the cut (overwrite);
  // a member after an insert point joins the moved pieces (insert).
  await load(page, {clips: [picture('v', 1, 0, f(40), {linkGroup: 'p'}), sound('a', -1, f(30), f(30), {linkGroup: 'p', in: f(30)})]});
  await state(page, ([from, length]) => Editor.placeSource('cam', {mode: 'overwrite', track: 1, audioTrack: null, marks: {in: 4, out: 4 + length}, at: from}), [f(10), f(15)]);
  now = Object.values(await clips(page));
  const after = now.find(c => c.type === 'video' && Math.round(c.start * 25) === 25), before = now.find(c => c.type === 'video' && c.start === 0);
  assert.ok(after.linkGroup && after.linkGroup === now.find(c => c.type === 'audio').linkGroup, 'overwrite: the later member joins the later piece ' + JSON.stringify(now));
  assert.equal(before.linkGroup, undefined);
  await load(page, {clips: [picture('v', 1, 0, f(40), {linkGroup: 'p'}), sound('a', -1, f(30), f(30), {linkGroup: 'p', in: f(30)})]});
  await state(page, ([from]) => Editor.placeSource('cam', {mode: 'insert', track: 1, audioTrack: null, marks: {in: 4, out: 4.6}, at: from}), [f(20)]);
  now = Object.values(await clips(page));
  const movedTail = now.find(c => c.type === 'video' && Math.round(c.start * 25) === 35);
  assert.ok(movedTail.linkGroup && movedTail.linkGroup === now.find(c => c.type === 'audio').linkGroup, 'insert: the later member joins the moved piece ' + JSON.stringify(now));
  console.log('Links: picture-only overwrite keeps both sides with the sound; drifted and lone pieces unlink; later members join the later side.');
}

// 3b. Linked operations on the pieces of a shot. Two picture-only overwrites cut a linked shot
// into three picture pieces that stay grouped with the one sound clip under them. Linked
// operations act on the members they concern: a trim moves only members whose same edge is at
// that time; delete and enable/disable take only members within the clip; a ripple delete cuts
// the spanning sound under the removed stretch so later pieces and sound move left together.
async function pieces(page) {
  const setup = async () => {
    await load(page, {clips: [picture('v', 1, 0, f(150), {linkGroup: 'p'}), sound('a', -1, 0, f(150), {linkGroup: 'p'})]});
    await state(page, ([x1, x2, length]) => {
      Editor.placeSource('cam', {mode: 'overwrite', track: 1, audioTrack: null, marks: {in: 4, out: 4 + length}, at: x1});
      Editor.placeSource('cam', {mode: 'overwrite', track: 1, audioTrack: null, marks: {in: 4, out: 4 + length}, at: x2});
    }, [f(25), f(75), f(24)]);
  };
  // "v1:0+25:G1" … one entry per clip, by lane and start; group letters by first appearance.
  const layout = () => state(page, () => {
    const names = new Map();
    const group = key => key ? (names.has(key) || names.set(key, 'G' + (names.size + 1)), names.get(key)) : '-';
    return Editor.get().project.clips.slice().sort((a, b) => b.track - a.track || a.start - b.start).map(c =>
      `${c.type[0]}${c.track}:${Math.round(c.start * 25)}+${Math.round(c.duration * 25)}:${c.linkGroup ? 'cam' : 'x'}:${group(c.linkGroup)}` +
      (Editor.syncOffset(c) ? ':off' : '') + (c.enabled === false ? ':OFF' : ''));
  });
  const piece = async n => state(page, n => Editor.get().project.clips.filter(c => c.type === 'video' && c.linkGroup)
    .sort((a, b) => a.start - b.start)[n], n);
  await setup();
  const base = ['v1:0+25:cam:G1', 'v1:25+24:x:-', 'v1:49+26:cam:G1', 'v1:75+24:x:-', 'v1:99+51:cam:G1', 'a-1:0+150:cam:G1'];
  assert.deepEqual(await layout(), base, 'three picture pieces grouped with the sound');
  // Trim the head piece's out point: only the head piece (no other member ends there).
  let head = await piece(0);
  await state(page, ([id, at]) => Editor.trimEdge(id, 'end', at), [head.id, f(20)]);
  assert.deepEqual(await layout(), ['v1:0+20:cam:G1', ...base.slice(1)], 'trim head out: only the head piece');
  // Trim the tail piece's in point: only the tail piece.
  await setup();
  const tail = await piece(2);
  await state(page, ([id, at]) => Editor.trimEdge(id, 'start', at), [tail.id, f(104)]);
  assert.deepEqual(await layout(), [...base.slice(0, 4), 'v1:104+46:cam:G1', base[5]], 'trim tail in: only the tail piece');
  // Trim the tail piece's out point: the sound ends there too, so both move.
  await state(page, ([id, at]) => Editor.trimEdge(id, 'end', at), [tail.id, f(140)]);
  assert.deepEqual(await layout(), [...base.slice(0, 4), 'v1:104+36:cam:G1', 'a-1:0+140:cam:G1'], 'trim tail out: the tail and the sound');
  // Roll the cut between the head piece and the first inserted clip: the sound does not move.
  await setup();
  head = await piece(0);
  const x1 = await state(page, () => Editor.get().project.clips.find(c => c.type === 'video' && !c.linkGroup && c.start < 1.5).id);
  await state(page, ([left, right, at]) => Editor.rollEdit(left, right, at), [head.id, x1, f(30)]);
  assert.deepEqual(await layout(), ['v1:0+30:cam:G1', 'v1:30+19:x:-', ...base.slice(2)], 'roll: only the pieces at the cut');
  // Delete the middle piece: only that piece; the sound and the other pieces stay linked.
  await setup();
  let middle = await piece(1);
  await state(page, id => Editor.deleteClips([id]), middle.id);
  assert.deepEqual(await layout(), [...base.slice(0, 2), ...base.slice(3)], 'delete the middle piece: only that piece');
  // Disable the middle piece: only that piece.
  await setup();
  middle = await piece(1);
  await state(page, id => Editor.toggleEnabled([id]), middle.id);
  assert.deepEqual(await layout(), [base[0], base[1], 'v1:49+26:cam:G1:OFF', ...base.slice(3)], 'disable the middle piece: only that piece');
  // Disabling the sound (it spans every piece) takes every piece.
  const audio = await state(page, () => Editor.get().project.clips.find(c => c.type === 'audio').id);
  await state(page, id => Editor.toggleEnabled([id]), audio);
  assert.equal((await layout()).filter(entry => entry.endsWith(':OFF')).length, 4, 'disable the sound: the shot');
  // Ripple delete the middle piece: the sound under it is cut out and the rest moves left in sync.
  await setup();
  middle = await piece(1);
  await state(page, id => Editor.deleteClips([id], {ripple: true}), middle.id);
  assert.deepEqual(await layout(), ['v1:0+25:cam:G1', 'v1:25+24:x:-', 'v1:49+24:x:-', 'v1:73+51:cam:G2', 'a-1:0+49:cam:G1', 'a-1:49+75:cam:G2'],
    'ripple delete the middle piece');
  // Ripple delete the first inserted clip: the sound under it is cut, later pieces and sound move left in sync.
  await setup();
  await state(page, () => Editor.deleteClips([Editor.get().project.clips.find(c => c.type === 'video' && !c.linkGroup && c.start < 1.5).id], {ripple: true}));
  assert.deepEqual(await layout(), ['v1:0+25:cam:G1', 'v1:25+26:cam:G2', 'v1:51+24:x:-', 'v1:75+51:cam:G2', 'a-1:0+25:cam:G1', 'a-1:25+101:cam:G2'],
    'ripple delete the inserted clip');
  // Ripple delete BOTH inserted clips at once: every remaining picture piece stays linked to its sound piece.
  await setup();
  await state(page, () => Editor.deleteClips(Editor.get().project.clips.filter(c => c.type === 'video' && !c.linkGroup).map(c => c.id), {ripple: true}));
  assert.deepEqual(await layout(), ['v1:0+25:cam:G1', 'v1:25+26:cam:G2', 'v1:51+51:cam:G3', 'a-1:0+25:cam:G1', 'a-1:25+26:cam:G2', 'a-1:51+51:cam:G3'],
    'ripple delete both inserted clips keeps every piece linked');
  // Deleting one of two grouped pieces leaves a lone clip: it is no longer linked.
  await load(page, {clips: [picture('p1', 1, 0, f(25), {linkGroup: 'q'}), picture('p2', 1, f(50), f(25), {linkGroup: 'q', in: f(50)})]});
  await state(page, () => Editor.deleteClips(['p1']));
  assert.deepEqual(Object.keys(await clips(page)), ['p2']);
  assert.equal((await clips(page)).p2.linkGroup, undefined, 'a lone piece is unlinked');
  // A plain linked pair with a J-cut (sound starts earlier) still trims together.
  await load(page, {clips: [picture('v', 1, f(10), f(40), {linkGroup: 'p', in: f(10)}), sound('a', -1, 0, f(50), {linkGroup: 'p'})]});
  await state(page, at => Editor.trimEdge('v', 'end', at), f(40));
  const pair = await clips(page);
  assert.deepEqual([pair.v.duration, pair.a.duration].map(t => Math.round(t * 25)), [30, 40], 'a J-cut pair trims together');
  console.log('Pieces of a shot: trims and rolls move members at the same edge, delete and disable take members within, ripple delete keeps sync.');
}

// 4. Source monitor targets, the drop-frame ruler and compatibility notes.
async function sourceTargets(page) {
  await load(page, {clips: [picture('v', 1, 0, 2)], trackCounts: {video: 4, audio: 4}});
  await openSource(page);
  // The plan line is redrawn on the next animation frame.
  const plan = async () => { await settle(page); return page.locator('.source-plan').textContent(); };
  // Picture None: sound "in the picture clip" is unavailable, and the plan says where sound goes.
  await page.locator('.source-audio-track').selectOption('');
  await page.locator('.source-track').selectOption('none');
  const embedded = await state(page, () => {
    const option = document.querySelector('.source-audio-track option[value=""]');
    return {disabled: option.disabled, sound: document.querySelector('.source-audio-track').value};
  });
  assert.deepEqual(embedded, {disabled: true, sound: 'follow'});
  assert.doesNotMatch(await plan(), /in the picture clip/);
  assert.match(await plan(), /Sound only → A1/);
  // "None" fits its picker.
  const fits = await state(page, () => {
    const select = document.querySelector('.source-track');
    const context = document.createElement('canvas').getContext('2d');
    context.font = getComputedStyle(select).font;
    return context.measureText(select.selectedOptions[0].textContent).width + 24 <= select.clientWidth;
  });
  assert.ok(fits, 'the Picture picker shows its whole choice');
  await page.locator('.source-track').selectOption('1');
  assert.equal(await state(page, () => document.querySelector('.source-audio-track option[value=""]').disabled), false);
  // A refused edit's message goes away when the target changes; a hidden target is named in the plan.
  await state(page, () => { Editor.setTrackState(2, 'video', {locked: true}); Editor.setTrackState(3, 'video', {hidden: true}); });
  await page.locator('.source-track').selectOption('2');
  await page.locator('.source-actions [data-action=overwrite]').click();
  assert.match(await page.locator('.source-status').textContent(), /Unlock video track 2/);
  await page.locator('.source-track').selectOption('3');
  assert.equal(await page.locator('.source-status').textContent(), '');
  assert.match(await plan(), /V3 \(hidden/);
  await capture(page, `source-hidden-target-${sizeName(page)}.png`);
  await page.locator('.source-close').click();

  // Drop-frame: at frame-level zoom the first real frame of a minute (;02) has a tick.
  await load(page, {fps: 30000 / 1001, clips: [picture('v', 1, 0, 9)]});
  const labels = await state(page, () => Editor.rulerTicks(59.5, 60.6, 5 * 1001 / 30000).map(tick => tick.label));
  assert.ok(labels.includes('00:01:00;02'), labels.join(' '));
  assert.ok(labels.includes('00:00:59;25') && labels.includes('00:01:00;05'), labels.join(' '));

  // Compatibility notes are counted beside the sequence name when a project opens.
  await load(page, {clips: [picture('v', 1, 0, 2)], compatibilityWarnings: ['Stored start timecode 25:00:00:00 is invalid; the sequence starts at 00:00:00:00.']});
  const badge = page.locator('#compat-badge');
  assert.ok(await badge.isVisible());
  assert.match(await badge.textContent(), /1 compatibility note/);
  assert.match(await state(page, () => document.querySelector('.toast')?.textContent || ''), /1 compatibility note/);
  await capture(page, `compatibility-badge-${sizeName(page)}.png`);
  await badge.click();
  assert.match(await page.locator('dialog[open]').textContent(), /25:00:00:00/);
  await page.locator('dialog[open] button', {hasText: 'Close'}).click();
  await load(page, {clips: [picture('v', 1, 0, 2)]});
  assert.equal(await badge.isVisible(), false);
  console.log('Source targets truthful, hidden target named, stale status cleared; ;02 ticked; compatibility count shown on open.');
}

// 5. Track heights and scrolling an edited track into view.
async function trackHeights(page) {
  await load(page, {clips: [picture('v2', 2, 0, 2), sound('a2', -2, 0, 2)], trackCounts: {video: 4, audio: 4}});
  const heights = await lanes(page);
  assert.ok(heights['video:2'].height >= 36 && heights['audio:-2'].height >= 36, 'tracks with clips are full height ' + JSON.stringify(heights));
  assert.ok(heights['video:1'].height >= 36 && heights['audio:-1'].height >= 36, 'V1 and A1 are full height ' + JSON.stringify(heights));
  assert.equal(heights['video:4'].height, 30, 'empty tracks are compact ' + JSON.stringify(heights));
  assert.equal(heights['audio:-3'].height, 30, 'empty tracks are compact ' + JSON.stringify(heights));
  // An edit on a track scrolled out of view brings it into view.
  const many = [];
  for (let n = 1; n <= 10; n++) many.push(picture('v' + n, n, 0, 2), sound('a' + n, -n, 0, 2));
  await load(page, {clips: many});
  for (const [scrollTop, kind, track] of [[0, 'audio', -10], [100000, 'video', 10]]) {
    await state(page, top => { document.getElementById('track-scroll').scrollTop = top; }, scrollTop);
    await settle(page);
    const key = kind + ':' + track;
    assert.equal((await lanes(page))[key].visible, false, key + ' starts out of view');
    await state(page, ([kind, track]) => Editor.updateClip(kind[0] + Math.abs(track), {start: .4}), [kind, track]);
    await settle(page);
    assert.ok((await lanes(page))[key].visible, key + ' is scrolled into view after the edit');
  }
  console.log('Tracks: used and V1/A1 tracks full height, empty ones compact; an edit scrolls its track into view.');
}

const only = process.env.SPLICE_ONLY?.split(',');
async function main() {
  const splice = await startSplice('splice-followups-');
  try {
    const sections = [
      ['farTracks', farTracks, {small: true}], ['links', links, {once: true}], ['pieces', pieces, {once: true}], ['sourceTargets', sourceTargets],
      ['trackHeights', trackHeights, {small: true}],
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
