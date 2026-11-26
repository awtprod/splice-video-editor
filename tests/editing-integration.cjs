/* The editing core (PR #7) merged with dev's scopes, Resolve reader and timeline interchange: one
 * scenario per integration finding, plus checks mutation testing showed were unguarded.
 * SPLICE_ONLY=<name>[,<name>] runs chosen scenarios; SPLICE_EVIDENCE_DIR saves screenshots. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {root, startSplice, openPage, makeCamera} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const state = (page, fn, arg) => page.evaluate(fn, arg);
const waitFor = (page, fn, arg, timeout = 20000) => page.waitForFunction(fn, arg, {timeout});
const status = page => state(page, () => document.querySelector('#session-status').textContent);
const savedDraft = page => waitFor(page, () => document.querySelector('#session-status').textContent === 'Draft saved locally', undefined, 60000);
async function capture(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, {recursive: true});
  await page.screenshot({path: path.join(evidence, name)});
}
const db = (page, fn, arg) => page.evaluate(async ([source, value]) => {
  const handle = await new Promise((ok, fail) => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); r.onerror = () => fail(r.error); });
  try { return await (0, eval)(source)(handle, value); } finally { handle.close(); }
}, [String(fn), arg]);
const all = (handle, store) => new Promise(ok => { const r = handle.transaction(store).objectStore(store).getAll(); r.onsuccess = () => ok(r.result); });
const keys = (handle, store) => new Promise(ok => { const r = handle.transaction(store).objectStore(store).getAllKeys(); r.onsuccess = () => ok(r.result); });

// 1. One start timecode representation: frames. Text starts from earlier builds open; Resolve
// timelines keep their own start and drop-frame setting.
async function startTimecode(splice, page, drp) {
  const open = (project) => state(page, text => SpliceSession.openFile(new File([text], 'old.splice.json')), JSON.stringify({
    name: 'Old', width: 320, height: 180, fps: 25, media: [], clips: [], ...project}));
  await open({startTimecode: '01:00:00:00'});
  assert.deepEqual(await state(page, () => [Editor.get().project.startTimecode, Editor.get().project.dropFrame]), [90000, undefined]);
  await waitFor(page, () => document.querySelector('#viewer-time').textContent === '01:00:00:00');
  await open({fps: 30000 / 1001, startTimecode: '01:00:00;00'});
  assert.deepEqual(await state(page, () => [Editor.get().project.startTimecode, Editor.get().project.dropFrame]), [107892, true]);
  await waitFor(page, () => document.querySelector('#viewer-time').textContent === '01:00:00;00');
  await open({fps: 30000 / 1001, startTimecode: '00:59:59:00'});
  assert.deepEqual(await state(page, () => [Editor.get().project.startTimecode, Editor.get().project.dropFrame]), [107970, false]);
  // A recovery draft written by an earlier build holds the text form: the page reopens it.
  await state(page, () => Editor.renameProject('Old draft'));
  await savedDraft(page);
  await db(page, async (handle) => {
    const tx = handle.transaction('sessions', 'readwrite'), store = tx.objectStore('sessions');
    const rows = await new Promise(ok => { const r = store.getAll(); r.onsuccess = () => ok(r.result); });
    for (const row of rows) if (row.name === 'Old draft') { row.data.startTimecode = '00:59:59:00'; delete row.data.dropFrame; store.put(row); }
    await new Promise(ok => { tx.oncomplete = ok; });
  });
  await page.reload();
  await state(page, () => SpliceSession.ready);
  assert.deepEqual(await state(page, () => [Editor.get().project.name, Editor.get().project.startTimecode, Editor.get().project.dropFrame]), ['Old draft', 107970, false]);
  // A Resolve project opens (it was refused as "invalid start timecode"), at its own start.
  await page.locator('#resolve-input').setInputFiles(drp);
  await waitFor(page, () => Editor.get().project.name === 'Harbor cut');
  assert.deepEqual(await state(page, () => [Editor.get().project.startTimecode, Editor.get().project.fps]), [86400, 24]);
  await waitFor(page, () => document.querySelector('#viewer-time').textContent === '01:00:00:00');
  await capture(page, 'resolve-open-' + page.viewportSize().width + '.png');
  // The 29.97 timeline in the same project does not say drop-frame: it shows non-drop, as Resolve does.
  await state(page, () => SpliceResolve.timeline(1));
  await waitFor(page, () => Editor.get().project.name === 'Harbor alt');
  assert.deepEqual(await state(page, () => [Editor.get().project.startTimecode, Editor.get().project.dropFrame]), [0, false]);
  await waitFor(page, () => document.querySelector('#viewer-time').textContent === '00:00:00:00');
  // The export dialog starts from the sequence's own start timecode and drop-frame setting.
  await state(page, () => Editor.setTimecodeSettings({startTimecode: '01:00:00;00', dropFrame: true}));
  await page.locator('.file-menu summary').click();
  await page.locator('#export-timeline').click();
  const fields = await state(page, () => { const f = document.querySelector('dialog form').elements; return [f.startTimecode.value, f.dropFrame.value, f.rangeMode.value]; });
  assert.deepEqual(fields, ['01:00:00;00', '1', 'all']);
  await page.keyboard.press('Escape');
  await state(page, () => document.querySelector('dialog[open]')?.close());
  console.log('Start timecode: text forms open as frames (project, 29.97 drop/non-drop, old draft); Resolve opens at 01:00:00:00 and its 29.97 timeline shows non-drop; the export dialog follows the sequence.');
}

// 2. Recovery keys a LUT on everything that defines it, keeps version tables and drops orphans.
async function lutRecovery(page) {
  const cube = (title, n, odd) => { const rows = ['TITLE "' + title + '"', 'LUT_1D_SIZE ' + n];
    for (let i = 0; i < n; i++) { const t = i / (n - 1), v = i % 2 && odd ? Math.min(1, t + .2) : t; rows.push([v, v, v].map(x => x.toFixed(6)).join(' ')); }
    return rows.join('\n'); };
  // Field by field: changing any defining field changes the key; an equal table shares it.
  const sensitivity = await state(page, ([a, b]) => {
    const base = SpliceLUT.parseCube(a, 'Show look'), key = SpliceSession.lutKey;
    const variants = {
      same: structuredClone(base), title: {...structuredClone(base), title: 'Other'},
      data: (() => { const v = structuredClone(base); v.data[7] += 1e-3; return v; })(),
      domainMin: {...structuredClone(base), domainMin: [-.1, 0, 0]}, domainMax: {...structuredClone(base), domainMax: [1, 1, 1.5]},
      curveData: SpliceLUT.parseCube(b, 'Show look'),
      curveSize: (() => { const v = structuredClone(base); v.curve = {size: v.curve.size - 1, data: v.curve.data.slice(3)}; return v; })(),
      noCurve: (() => { const v = structuredClone(base); delete v.curve; return v; })(),
    };
    return Object.fromEntries(Object.entries(variants).map(([name, lut]) => [name, key(lut) === key(base)]));
  }, [cube('Show look', 129, false), cube('Show look', 129, true)]);
  assert.deepEqual(sensitivity, {same: true, title: false, data: false, domainMin: false, domainMax: false, curveData: false, curveSize: false, noCurve: false});
  // Two 1D LUTs, same title and 65-point lattice, different curves, survive a reload apart.
  await state(page, () => SpliceSession.newProject({name: 'LUT keys', width: 320, height: 180, fps: 25}));
  await state(page, ([a, b]) => Editor.editProject(project => {
    project.media = [{id: 'r', name: 'Ridge', src: 'assets/ridge.mp4', type: 'video', duration: 9}];
    project.clips = ['D', 'E'].map((id, i) => ({id, mediaId: 'r', type: 'video', track: 1, start: i * 2, duration: 2, in: 0, volume: 100, transition: 'none',
      color: {brightness: 100, contrast: 100, saturation: 100, lut: SpliceLUT.parseCube(i ? b : a, 'Show look')}}));
  }), [cube('Show look', 129, false), cube('Show look', 129, true)]);
  const curves = () => state(page, () => Editor.get().project.clips.map(c => c.color.lut.curve.data.slice(0, 6).map(v => +v.toFixed(4))));
  const before = await curves();
  assert.notDeepEqual(before[0], before[1]);
  assert.equal(await state(page, () => Editor.get().project.clips[0].color.lut.data.length), await state(page, () => Editor.get().project.clips[1].color.lut.data.length));
  await savedDraft(page);
  await page.reload();
  await state(page, () => SpliceSession.ready);
  assert.deepEqual(await curves(), before, 'each clip keeps its own 1D curve after recovery');
  // A table only an older version uses is kept by pruning (versions list their keys); an orphan goes.
  const lutKeyOf = () => state(page, () => SpliceSession.lutKey(Editor.get().project.clips[0].color.lut));
  const oldKey = await lutKeyOf();
  await state(page, () => { const real = Date.now.bind(Date); Date.now = () => real() + 31000; });
  await state(page, b => Editor.updateClip('D', {color: {...Editor.get().project.clips[0].color, lut: SpliceLUT.parseCube(b, 'Replacement')}}),
    cube('Replacement', 33, true));
  await waitFor(page, key => SpliceSession.lutKey(Editor.get().project.clips[0].color.lut) !== key, oldKey);
  await savedDraft(page);
  await db(page, async handle => {
    const tx = handle.transaction('originals', 'readwrite');
    tx.objectStore('originals').put({size: 2, data: new Array(24).fill(0)}, 'lut:orphan');
    await new Promise(ok => { tx.oncomplete = ok; });
  });
  const removed = await state(page, () => SpliceSession.prune());
  const stored = await db(page, async (handle, key) => {
    const versions = await new Promise(ok => { const r = handle.transaction('versions').objectStore('versions').getAll(); r.onsuccess = () => ok(r.result); });
    const originals = await new Promise(ok => { const r = handle.transaction('originals').objectStore('originals').getAllKeys(); r.onsuccess = () => ok(r.result); });
    return {versionLists: versions.some(v => (v.payloadKeys || []).includes(key)), kept: originals.includes(key), orphan: originals.includes('lut:orphan')};
  }, oldKey);
  assert.ok(removed >= 1);
  assert.deepEqual(stored, {versionLists: true, kept: true, orphan: false});
  console.log('Recovery LUT keys cover title, table, domain and 1D curve; version tables kept, orphans pruned.');
}

// 8. Browser storage full: the user is told once, nothing is thrown at the page.
async function quota(splice, page, errors) {
  await state(page, () => SpliceSession.newProject({name: 'Quota', width: 320, height: 180, fps: 25}));
  await savedDraft(page).catch(() => {});
  const cdp = await page.context().newCDPSession(page);
  const usage = await state(page, async () => (await navigator.storage.estimate()).usage);
  await cdp.send('Storage.overrideQuotaForOrigin', {origin: splice.url, quotaSize: usage + 64 * 1024});
  await state(page, () => Editor.editProject(project => {
    project.media = [{id: 'r', name: 'Ridge', src: 'assets/ridge.mp4', type: 'video', duration: 9}];
    const big = SpliceLUT.parseCube(['LUT_3D_SIZE 33', ...Array.from({length: 33 ** 3}, (_, i) => [i % 33, (i / 33 | 0) % 33, i / 1089 | 0].map(v => (v / 32).toFixed(6)).join(' '))].join('\n'), 'Big');
    project.clips = [{id: 'q', mediaId: 'r', type: 'video', track: 1, start: 0, duration: 1, in: 0, volume: 100, transition: 'none', color: {lut: big}}];
  }));
  await waitFor(page, () => /Recovery unavailable/.test(document.querySelector('#session-status').textContent), undefined, 60000);
  await page.waitForTimeout(300);
  await cdp.send('Storage.overrideQuotaForOrigin', {origin: splice.url});
  assert.deepEqual(errors.filter(message => /transaction/i.test(message)), [], 'no uncaught transaction error');
  await state(page, () => Editor.renameProject('Quota again'));
  await savedDraft(page);
  console.log('Storage quota exceeded: "' + 'Recovery unavailable' + '" shown, no uncaught error; saving resumes when space returns.');
}

// 3. Edits on the merged tree are not slowed by forced scope repaints.
async function scopesNotForced(page) {
  await state(page, () => SpliceSession.newProject({name: 'Scopes', width: 320, height: 180, fps: 25}));
  await state(page, () => Editor.editProject(project => {
    project.media = [{id: 'r', name: 'Ridge', src: 'assets/ridge.mp4', type: 'video', duration: 9}];
    project.clips = [{id: 'a', mediaId: 'r', type: 'video', track: 1, start: 0, duration: 4, in: 0, volume: 100, transition: 'none', color: {brightness: 100, contrast: 100, saturation: 100}}];
  }));
  await state(page, () => Editor.select('a'));
  await waitFor(page, () => document.querySelector('.splice-scope') && Editor.get().video?.readyState >= 2);
  await page.waitForTimeout(400);
  const result = await state(page, async () => {
    const canvas = document.querySelector('.splice-scope');
    let paints = 0;
    const original = SpliceScopes.paint;
    SpliceScopes.paint = (c, force) => { if (force) paints++; return original(c, force); };
    const started = performance.now();
    for (let i = 0; i < 20; i++) Editor.updateClip('a', {volume: 50 + i});
    const ms = performance.now() - started;
    await new Promise(ok => requestAnimationFrame(() => requestAnimationFrame(ok)));
    SpliceScopes.paint = original;
    return {forced: paints, sameCanvas: document.querySelector('.splice-scope') === canvas, ms};
  });
  assert.equal(result.forced, 0, 'inspector re-renders do not force a scope repaint');
  assert.ok(result.sameCanvas, 'the scope canvas (and its picture) survives re-renders');
  console.log('Scopes: 20 edits, 0 forced repaints, canvas kept, ' + result.ms.toFixed(1) + ' ms.');
}

// 5. The docked source monitor: room for the picture, edits build the scene in order, drag to a track.
async function sourceMonitor(splice, page, camera) {
  const {width, height} = page.viewportSize();
  await state(page, () => SpliceSession.newProject({name: 'Scene', width: 1280, height: 720, fps: 25}));
  await page.locator('#media-input').setInputFiles(camera);
  await waitFor(page, () => Editor.get().project.media[0]?.audioChannels > 0);
  const id = await state(page, () => Editor.get().project.media[0].id);
  await state(page, mediaId => SpliceSource.open(mediaId), id);
  await waitFor(page, () => document.querySelector('.source-stage video')?.readyState >= 2);
  const box = () => state(page, () => {
    const r = n => { const b = document.querySelector(n).getBoundingClientRect(); return [Math.round(b.top), Math.round(b.height)]; };
    return {video: r('.source-stage video'), panel: r('.source-dialog'), head: r('.source-head')};
  });
  const opened = await box();
  const minimum = width >= 1180 && height >= 780 ? 160 : 110;
  assert.ok(opened.video[1] >= minimum, `picture ${opened.video[1]} px tall at ${width}x${height}`);
  assert.ok(opened.video[1] >= opened.panel[1] / 3, `picture ${opened.video[1]} px of a ${opened.panel[1]} px panel`);
  await capture(page, `source-open-${width}.png`);
  const cycle = async (from, to, button = 'insert') => {
    for (const [edge, at] of [['in', from], ['out', to]]) {
      await page.locator('.source-scrub').fill(String(at));
      await waitFor(page, target => Math.abs(document.querySelector('.source-stage video').currentTime - target) < .03, at);
      await page.locator(`.source-dialog [data-action=${edge}]`).click();
    }
    await page.locator(`.source-dialog [data-action=${button}]`).click();
    await waitFor(page, () => /^(Inserted|Overwrote)/.test(document.querySelector('.source-status').textContent));
  };
  // Three mark-and-insert cycles: shots 1 (red), 3 (blue) and 5 (magenta) in that order.
  await cycle(0, .96); await cycle(2, 2.96); await cycle(4, 4.96);
  const after = await box();
  assert.deepEqual(after, opened, 'no layout jump after inserts');
  const order = await state(page, () => Editor.get().project.clips.filter(c => c.type === 'video').sort((a, b) => a.start - b.start).map(c => [+c.start.toFixed(2), +c.in.toFixed(2)]));
  assert.deepEqual(order, [[0, 0], [1, 2], [2, 4]], 'the scene builds in order');
  assert.equal(await state(page, () => Math.round(Editor.get().time * 25)), 75, 'the playhead parks after the last placed clip');
  await capture(page, `source-after-inserts-${width}.png`);
  // With a timeline In the edit goes there; the In/Out marks are cleared afterwards (Premiere).
  await state(page, () => Editor.setMark('in', 1));
  await cycle(5, 5.48, 'overwrite');
  assert.deepEqual(await state(page, () => [Editor.get().project.marks ?? null, Math.round(Editor.get().time * 25)]), [null, 38]);
  assert.ok(await state(page, () => Editor.get().project.clips.some(c => c.type === 'video' && Math.abs(c.start - 1) < 1e-6 && Math.abs(c.in - 5) < 1e-6)));
  // Drag from the source picture onto Video 2 at about 1 s: overwrite there with the source marks.
  const lane = page.locator('.track-lane[data-kind="video"][data-track="2"]');
  await lane.scrollIntoViewIfNeeded();
  const laneBox = await lane.boundingBox();
  const at = await state(page, () => Editor.get().time);
  await page.locator('.source-stage').dragTo(lane, {targetPosition: {x: 40, y: laneBox.height / 2}});
  const dropped = await state(page, () => Editor.get().project.clips.filter(c => c.track === 2));
  assert.equal(dropped.length, 1, 'drag from the source picture places a clip on Video 2');
  assert.ok(Math.abs(dropped[0].in - 5) < 1e-6 && Math.abs(dropped[0].duration - .48) < 1e-6);
  assert.ok(await state(page, () => Editor.get().project.clips.some(c => c.type === 'audio' && c.linkGroup && c.linkGroup === Editor.get().project.clips.find(v => v.track === 2).linkGroup)));
  void at;
  await page.locator('.source-close').click();
  console.log(`Source monitor at ${width}x${height}: picture ${opened.video[1]} of ${opened.panel[1]} px, no jump, three inserts in order, timeline In edit clears the marks, drag to a track overwrites there.`);
}

// 7. Linked pairs from the Resolve and interchange importers behave like native pairs.
async function importedPairs(splice, page, drp) {
  const check = async label => {
    const pair = await state(page, () => {
      const clips = Editor.get().project.clips;
      const video = clips.find(c => c.type === 'video' && c.linkGroup && clips.some(a => a.type === 'audio' && a.linkGroup === c.linkGroup && a.mediaId === c.mediaId));
      return video && {video: video.id, audio: clips.find(a => a.type === 'audio' && a.linkGroup === video.linkGroup).id, embedded: video.embeddedAudio};
    });
    assert.ok(pair, label + ': a linked picture/sound pair');
    assert.equal(pair.embedded, false, label + ': the picture carries no sound of its own');
    const initial = await state(page, () => JSON.stringify(Editor.get().project.clips));
    const result = await state(page, ({video, audio}) => {
      const get = id => Editor.get().project.clips.find(c => c.id === id);
      const fps = Editor.get().project.fps, out = {};
      const offsets = () => Editor.get().project.clips.filter(c => c.linkGroup).map(c => Editor.syncOffset(c)).filter(v => v);
      // Linked move: the sound follows.
      const s0 = get(audio).start;
      Editor.moveClip(video, get(video).start + 10 / fps, get(video).track);
      out.follows = Math.round((get(audio).start - s0) * fps);
      Editor.undo();
      // One-sided trim of the sound's end (link suspended): the picture stays, still in sync.
      const vEnd = get(video).start + get(video).duration;
      Editor.withoutLinks(() => Editor.trimEdge(audio, 'end', get(audio).start + get(audio).duration - 4 / fps));
      out.trimOneSided = [Math.round((get(video).start + get(video).duration - vEnd) * fps), Editor.syncOffset(get(video))];
      // One-sided move: the sync badge counts the frames, and "move into sync" repairs it.
      Editor.withoutLinks(() => Editor.moveClip(audio, get(audio).start + 3 / fps, get(audio).track));
      out.badge = Editor.syncOffset(get(video));
      Editor.bringIntoSync(audio, 'move');
      out.repaired = Editor.syncOffset(get(video));
      // Unlink and relink.
      out.unlink = Editor.toggleLink([video]);
      out.unlinked = [get(video).linkGroup ?? null, get(audio).linkGroup ?? null];
      out.relink = Editor.toggleLink([video, audio]);
      out.relinked = !!get(video).linkGroup && get(video).linkGroup === get(audio).linkGroup;
      // Overwrite across the pair, then lift and extract a range through it, then slide.
      const start = get(video).start, media = get(video).mediaId;
      Editor.placeSource(media, {mode: 'overwrite', track: get(video).track, audioTrack: get(audio).track, marks: {in: 0, out: 4 / fps}, at: start + 4 / fps});
      out.afterOverwrite = offsets();
      Editor.setMark('in', start + 2 / fps); Editor.setMark('out', start + 5 / fps);
      Editor.liftRange({extract: true});
      out.afterExtract = offsets();
      const piece = Editor.get().project.clips.find(c => c.type === 'video' && c.linkGroup && c.start > start);
      if (piece) { try { Editor.slideClip(piece.id, 1 / fps); } catch (error) { out.slideRefused = error.message; } }
      out.afterSlide = offsets();
      return out;
    }, pair);
    assert.equal(result.follows, 10, label + ': linked sound follows a move');
    assert.deepEqual(result.trimOneSided, [0, null], label + ': one-sided trim leaves the picture and sync alone');
    assert.equal(result.badge, -3, label + ': sync badge counts the slipped frames');
    assert.equal(result.repaired, null, label + ': move into sync repairs it');
    assert.deepEqual([result.unlink, result.unlinked, result.relink, result.relinked], ['unlinked', [null, null], 'linked', true], label + ': unlink and relink');
    assert.deepEqual([result.afterOverwrite, result.afterExtract, result.afterSlide], [[], [], []], label + ': overwrite, extract and slide keep every pair in sync');
    // Undo walks every step back to the imported timeline.
    for (let i = 0; i < 60 && await state(page, text => JSON.stringify(Editor.get().project.clips) !== text, initial); i++) await state(page, () => Editor.undo());
    assert.equal(await state(page, () => JSON.stringify(Editor.get().project.clips)), initial, label + ': undo returns to the imported timeline');
    console.log(label + ': linked pair moves together, one-sided trim, sync badge and repair, unlink/relink, overwrite, extract, slide and undo behave as native pairs.');
  };
  await page.locator('#resolve-input').setInputFiles(drp);
  await waitFor(page, () => Editor.get().project.name === 'Harbor cut' && Editor.get().project.media.some(m => !m.offline));
  await check('Resolve');
  // A native linked pair through FCPXML and Final Cut 7 XML and back in.
  for (const format of ['fcpxml', 'xmeml']) {
    await state(page, () => SpliceSession.newProject({name: 'Pair', width: 320, height: 180, fps: 25}));
    await page.locator('#media-input').setInputFiles(path.join(splice.temp, 'camera.mp4'));
    await waitFor(page, () => Editor.get().project.media[0]?.audioChannels > 0);
    await state(page, () => Editor.placeSource(Editor.get().project.media[0].id, {mode: 'overwrite', track: 1, audioTrack: -1, marks: {in: 0, out: 4}, at: 0}));
    await state(page, async format => {
      const post = async (route, body) => { const r = await fetch('/api/interchange/' + route, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); const j = await r.json(); if (!r.ok) throw Error(j.error); return j; };
      const exported = await post('export', {project: await SpliceMedia.forPremiere(Editor.get().project), options: {formats: [format]}});
      const file = exported.files[0];
      const imported = await post('import', {filename: file.name, text: file.content, options: {linkMedia: true}});
      await SpliceSession.openFile(new File([JSON.stringify(imported.project)], 'imported.splice.json'));
    }, format);
    await waitFor(page, () => Editor.get().project.media.some(m => !m.offline));
    await check(format === 'fcpxml' ? 'FCPXML' : 'Final Cut 7 XML');
  }
}

// 8 and mutation gaps: sync menu closing, Out at the end, the keyboard guard on drop-downs,
// and where extract stops shifting.
async function smaller(page) {
  const clip = (id, start, duration, extra = {}) => ({id, mediaId: 'long', type: 'video', track: 1, start, duration, in: 5, color: {}, volume: 100, transition: 'none', title: '', ...extra});
  const load = project => state(page, text => Editor.openProject(new File([text], 'small.splice.json')), JSON.stringify({name: 'Small', width: 1280, height: 720, fps: 25,
    media: [{id: 'long', name: 'Long take', src: 'assets/ridge.mp4', type: 'video', duration: 30}], ...project}));
  // Out with the playhead parked at the sequence end marks the last frame.
  await load({clips: [clip('a', 0, 4)]});
  await state(page, () => { Editor.seek(Editor.get().project.duration); Editor.setMark('out'); });
  assert.equal(await state(page, () => Math.round(Editor.get().project.marks.out * 25)), 100);
  // Sync menu: Escape closes it, and so does scrolling.
  await load({clips: [clip('v', 0, 4, {in: 2, linkGroup: 'g', embeddedAudio: false}), clip('s', 0, 4, {type: 'audio', track: -1, in: 2.2, linkGroup: 'g'})]});
  for (const how of ['Escape', 'scroll']) {
    await page.locator('.sync-badge').first().click();
    await page.locator('.sync-menu').waitFor();
    if (how === 'Escape') await page.keyboard.press('Escape');
    else await page.mouse.wheel(0, 120);
    await page.locator('.sync-menu').waitFor({state: 'detached'});
  }
  // A drop-down keeps its keys: I and arrow keys do not reach the timeline.
  await state(page, () => Editor.clearMarks());
  const select = page.locator('select').first();
  await select.focus();
  await page.keyboard.press('i');
  await page.keyboard.press('o');
  assert.equal(await state(page, () => Editor.get().project.marks ?? null), null, 'keys typed in a drop-down stay there');
  await state(page, () => document.activeElement.blur());
  // Extract shifts clips that start exactly at the Out boundary and later; earlier ones stay.
  await load({clips: [clip('a', 0, 2), clip('b', 2, 1, {in: 10}), clip('c', 3, 2, {in: 12}), clip('d', 6, 1, {in: 20})]});
  await state(page, () => { Editor.setMark('in', 1); Editor.setMark('out', 2.96); Editor.liftRange({extract: true}); });
  const starts = await state(page, () => Object.fromEntries(Editor.get().project.clips.map(c => [c.id, +c.start.toFixed(2)])));
  assert.deepEqual(starts, {a: 0, c: 1, d: 4}, 'extract removes 1-3 s and shifts the clips at and after 3 s by 2 s');
  console.log('Out at the sequence end, sync menu Escape/scroll close, drop-down key guard and extract boundary hold.');
}

const only = process.env.SPLICE_ONLY?.split(',');
async function main() {
  const splice = await startSplice('splice-integ-');
  try {
    const camera = path.join(splice.temp, 'camera.mp4');
    makeCamera(camera);
    const footage = path.join(splice.temp, 'footage');
    fs.mkdirSync(footage);
    fs.copyFileSync(camera, path.join(footage, 'A001.mp4'));
    execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=4', path.join(footage, 'A002.wav')]);
    const drp = path.join(splice.temp, 'Harbor.drp');
    execFileSync('python3', [path.join(root, 'tests/resolve_fixtures.py'), 'drp', drp, path.join(footage, 'A001.mp4'), path.join(footage, 'A002.wav'), '--no-generator']);
    const sections = [
      ['startTimecode', page => startTimecode(splice, page, drp)],
      ['lutRecovery', lutRecovery, {once: true}],
      ['scopes', scopesNotForced],
      ['sourceMonitor', page => sourceMonitor(splice, page, camera)],
      ['importedPairs', page => importedPairs(splice, page, drp), {once: true}],
      ['smaller', smaller],
      ['quota', (page, errors) => quota(splice, page, errors), {once: true}],
    ];
    let pass = 1;
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
      const {page, errors} = await openPage(splice, viewport);
      console.log(`Pass ${pass} at ${viewport.width} × ${viewport.height}`);
      for (const [name, run, options] of sections)
        if ((!only || only.includes(name)) && !(options?.once && pass > 1)) await run(page, errors);
      assert.deepEqual(errors, []);
      await page.context().close();
      pass++;
    }
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
