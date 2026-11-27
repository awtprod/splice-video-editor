/* End-to-end browser verification for Native Nested Sequences, Multicam Editing, Subclips,
 * Real Bin Management, and Editorial Metadata & Color Labels.
 * Runs across 1440x900, 1180x780, and 1024x640 viewports.
 * Set SPLICE_EVIDENCE_DIR to save visual inspection screenshots. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, ffmpeg, makeCamera, nearestSegment, colorAt, readPcm, toneAt} = require('./browser-harness.cjs');

const evidenceDir = process.env.SPLICE_EVIDENCE_DIR || '';
const settle = page => page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
const blur = page => page.evaluate(() => document.activeElement?.blur?.());

async function capture(page, filename) {
  if (!evidenceDir) return;
  fs.mkdirSync(evidenceDir, {recursive: true});
  await page.screenshot({path: path.join(evidenceDir, filename)});
}

function makeSolidClip(file, color, freq, duration = 4) {
  ffmpeg([
    '-f', 'lavfi', '-i', `color=c=${color}:s=320x180:r=25:d=${duration}`,
    '-f', 'lavfi', '-i', `sine=frequency=${freq}:sample_rate=48000:duration=${duration}`,
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-g', '1',
    '-c:a', 'aac', '-b:a', '128k',
    file,
  ]);
}

async function programColor(page, time) {
  await page.evaluate(t => Editor.seek(t), time);
  await page.waitForFunction(() => {
    const s = Editor.get();
    if (!s.activeClip) return true;
    const v = s.video;
    return v && v.readyState >= 2 && !v.seeking;
  }, {timeout: 15000});
  await page.waitForTimeout(220);
  await settle(page);
  return page.evaluate(() => {
    const canvas = document.getElementById('preview-canvas');
    Editor.drawFrame(canvas);
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const sum = [0, 0, 0];
    let n = 0;
    for (let i = 0; i < data.length; i += 4 * 97) {
      sum[0] += data[i];
      sum[1] += data[i + 1];
      sum[2] += data[i + 2];
      n++;
    }
    return sum.map(v => Math.round(v / Math.max(1, n)));
  });
}

function dominantColor(rgb) {
  const [r, g, b] = rgb;
  if (r > 180 && g < 80 && b < 80) return 'red';
  if (g > 180 && r < 80 && b < 80) return 'green';
  if (b > 180 && r < 80 && g < 80) return 'blue';
  if (r > 180 && g > 180 && b < 80) return 'yellow';
  return `rgb(${r},${g},${b})`;
}

async function freshProjectWithMedia(page, files, name = 'Test Project') {
  await page.evaluate(async projName => {
    await SpliceSession.newProject({name: projName, width: 320, height: 180, fps: 25});
  }, name);
  await page.locator('#media-input').setInputFiles(files);
  await page.waitForFunction(expected => Editor.get().project.media.length >= expected, files.length, {timeout: 20000});
  await settle(page);
}

async function waitForNestedPreview(page, mediaId) {
  await page.waitForFunction(id => {
    const m = Editor.get().project.media.find(item => item.id === id);
    return !!(m && m.src && m.src.startsWith('/media/') && m.proxyState === 'ready');
  }, mediaId, {timeout: 30000});
}

// ---------------------------------------------------------------------------
// 1. Native Nested Sequences
// ---------------------------------------------------------------------------
async function testNestedSequences(page, files, directory) {
  await freshProjectWithMedia(page, [files.red, files.green, files.blue], 'Nest Main');
  // Place Red (0..2s) and Green (2..4s) on V1.
  const clipIds = await page.evaluate(() => {
    const p = Editor.get().project;
    const mRed = p.media.find(m => m.name.includes('CamA_Red'));
    const mGreen = p.media.find(m => m.name.includes('CamB_Green'));
    Editor.editProject(next => {
      next.clips = [
        {id: 'c_red', mediaId: mRed.id, type: 'video', track: 1, start: 0, duration: 2, in: 0, color: {}, volume: 100, transition: 'none', embeddedAudio: true},
        {id: 'c_green', mediaId: mGreen.id, type: 'video', track: 1, start: 2, duration: 2, in: 0, color: {}, volume: 100, transition: 'none', embeddedAudio: true},
      ];
    });
    Editor.select('c_red');
    return ['c_red', 'c_green'];
  });
  await settle(page);

  // Select both clips via Ctrl+click on second clip, then Edit -> Nest…
  await page.locator('.timeline-clip[data-clip="c_green"]').click({modifiers: ['Control'], position: {x: 12, y: 10}});
  await settle(page);
  await page.locator('.edit-menu summary').click();
  await page.locator('.edit-menu [data-command="nest"]').click();
  await page.locator('#nest-dialog').waitFor({state: 'visible'});
  await page.locator('#nest-dialog input[name="name"]').fill('Intro Nest');
  await page.locator('#nest-dialog button[type="submit"]').click();
  await page.locator('#nest-dialog').waitFor({state: 'detached'});
  await settle(page);

  const nestedInfo = await page.evaluate(() => {
    const p = Editor.get().project;
    const item = p.media.find(m => m.nestedProject && m.name === 'Intro Nest');
    const clip = p.clips.find(c => c.mediaId === item?.id && c.type === 'video');
    return {mediaId: item?.id, clipId: clip?.id, clipCount: p.clips.length, innerCount: item?.nestedProject?.clips?.length};
  });
  assert.ok(nestedInfo.mediaId, 'nested sequence media item created');
  assert.ok(nestedInfo.clipId, 'nested clip placed on timeline');
  assert.equal(nestedInfo.innerCount, 2, 'nested sequence holds both inner clips');

  await waitForNestedPreview(page, nestedInfo.mediaId);
  const beforeEditColor = await programColor(page, 0.5);
  assert.equal(dominantColor(beforeEditColor), 'red', `before inner edit, 0.5s is red (got ${beforeEditColor})`);

  // Step into the nested sequence via double-click on timeline clip.
  await page.locator(`.timeline-clip[data-clip="${nestedInfo.clipId}"]`).dblclick({position: {x: 16, y: 10}});
  await page.locator('#nested-breadcrumb').waitFor({state: 'visible'});
  await settle(page);
  const crumbText = await page.locator('#nested-breadcrumb').textContent();
  assert.match(crumbText, /Nest Main/, 'breadcrumb shows parent sequence');
  assert.match(crumbText, /Intro Nest/, 'breadcrumb shows nested sequence');
  assert.match(crumbText, /Depth 1\/4/, 'breadcrumb shows Depth 1/4');
  await capture(page, 'nested-breadcrumb-1440x900.png');
  await page.waitForFunction(() => document.getElementById('session-status')?.textContent.includes('Draft saved'));
  await page.reload();
  await page.evaluate(() => SpliceSession.ready);
  assert.equal(await page.evaluate(() => Editor.get().project.name), 'Nest Main');
  await page.evaluate(id => SpliceNested.enter(id), nestedInfo.mediaId);

  // Edit inside the nested sequence: swap first inner clip (Red) to Blue!
  const blueMediaId = await page.evaluate(() => {
    const root = SpliceNested.rootProject();
    return root.media.find(m => m.name.includes('CamC_Blue')).id;
  });
  await page.evaluate(blueId => {
    const root = SpliceNested.rootProject();
    const blueItem = structuredClone(root.media.find(m => m.id === blueId));
    Editor.editProject(next => {
      if (!next.media.some(m => m.id === blueId)) next.media.push(blueItem);
      const first = next.clips.find(c => c.start === 0 && c.type === 'video');
      first.mediaId = blueId;
    });
  }, blueMediaId);
  await settle(page);

  // Verify depth 2 nesting inside the nest works cleanly, then undo it so depth 1 edit stays clean.
  await page.evaluate(() => {
    const first = Editor.get().project.clips[0];
    SpliceNested.nestSelection([first.id], {name: 'Inner Level 2'});
  });
  const level2MediaId = await page.evaluate(() => Editor.get().project.media.find(m => m.name === 'Inner Level 2')?.id);
  assert.ok(level2MediaId, 'depth-2 nested sequence created inside depth-1 nest');
  await page.evaluate(id => SpliceNested.enter(id), level2MediaId);
  await settle(page);
  assert.match(await page.locator('#nested-breadcrumb').textContent(), /Depth 2\/4/, 'depth 2 breadcrumb shown');
  await page.locator('#nested-back-btn').click();
  await settle(page);
  assert.match(await page.locator('#nested-breadcrumb').textContent(), /Depth 1\/4/, 'returned to depth 1');
  // Undo the inner level-2 nest so our depth-1 blue+green edit is crisp for unnest testing.
  await page.evaluate(() => Editor.undo());
  await settle(page);

  // Return to parent sequence via breadcrumb Back button; parent preview must regenerate & show BLUE at 0.5s!
  const oldSrc = await page.evaluate(id => SpliceNested.rootProject().media.find(m => m.id === id)?.src || '', nestedInfo.mediaId);
  await page.locator('#nested-back-btn').click();
  await page.locator('#nested-breadcrumb').waitFor({state: 'hidden'});
  await page.waitForFunction(({id, prev}) => {
    const m = Editor.get().project.media.find(item => item.id === id);
    return !!(m && m.src && m.src.startsWith('/media/') && m.src !== prev && m.proxyState === 'ready');
  }, {id: nestedInfo.mediaId, prev: oldSrc}, {timeout: 30000});
  await settle(page);

  const afterEditColor = await programColor(page, 0.5);
  assert.equal(dominantColor(afterEditColor), 'blue', `parent preview updated after inner edit to blue (got ${afterEditColor})`);
  const afterEditGreen = await programColor(page, 2.5);
  assert.equal(dominantColor(afterEditGreen), 'green', `parent preview 2.5s remains green (got ${afterEditGreen})`);

  // Verify persistence across reload (recovery) and save/reopen (.splice.json).
  await page.waitForFunction(() => document.getElementById('session-status')?.textContent?.includes('Draft saved'), {timeout: 15000});
  const savedJson = await page.evaluate(() => JSON.stringify(SpliceNested.rootProject()));
  await page.reload();
  await page.waitForFunction(() => Editor.get().project.media.some(m => m.name === 'Intro Nest' && m.nestedProject), {timeout: 15000});
  await settle(page);
  const reloadedNested = await page.evaluate(() => {
    const p = Editor.get().project;
    const item = p.media.find(m => m.name === 'Intro Nest');
    const clip = p.clips.find(c => c.mediaId === item?.id);
    return {hasNested: !!item?.nestedProject, clipId: clip?.id, innerClips: item?.nestedProject?.clips?.length};
  });
  assert.equal(reloadedNested.hasNested, true, 'nested sequence survives page reload recovery');
  assert.equal(reloadedNested.innerClips, 2, 'inner clips survive recovery');

  // Reopen via explicit working project file (save/reopen).
  await page.evaluate(async json => {
    await SpliceSession.openFile(new File([json], 'nest-reopen.splice.json', {type: 'application/json'}));
  }, savedJson);
  await page.waitForFunction(() => Editor.get().project.media.some(m => m.name === 'Intro Nest' && m.nestedProject), {timeout: 15000});
  await settle(page);

  // Unnest in place via right-click context menu -> verify 2 plain clips (Blue + Green), then Undo / Redo!
  const reopenedClipId = await page.evaluate(() => {
    const p = Editor.get().project;
    const item = p.media.find(m => m.name === 'Intro Nest');
    return p.clips.find(c => c.mediaId === item.id).id;
  });
  await page.locator(`.timeline-clip[data-clip="${reopenedClipId}"]`).click({button: 'right', position: {x: 14, y: 10}});
  await page.locator('.sync-menu button', {hasText: 'Unnest / Decompose in place'}).click();
  await settle(page);

  const afterUnnest = await page.evaluate(() => Editor.get().project.clips.map(c => ({start: c.start, dur: c.duration, media: Editor.get().project.media.find(m => m.id === c.mediaId)?.name})));
  assert.equal(afterUnnest.length, 2, 'unnest expands to 2 clips');
  assert.ok(afterUnnest.some(c => c.media.includes('CamC_Blue') && Math.abs(c.start) < 0.05), 'first unnested clip is Blue');
  assert.ok(afterUnnest.some(c => c.media.includes('CamB_Green') && Math.abs(c.start - 2) < 0.05), 'second unnested clip is Green');

  // Undo restores nested clip pair; Redo re-unnests back to plain clips.
  await blur(page);
  await page.keyboard.press('Control+z');
  await settle(page);
  assert.ok(await page.evaluate(() => Editor.get().project.clips.every(c => c.nestedSequence != null)), 'Ctrl+Z undoes unnest back to nested clips');
  await page.keyboard.press('Control+Shift+Z');
  await settle(page);
  assert.ok(await page.evaluate(() => Editor.get().project.clips.length === 2 && Editor.get().project.clips.every(c => c.nestedSequence == null)), 'Ctrl+Shift+Z redoes unnest back to plain clips');

  const reversed = await page.evaluate(clipId => {
    Editor.undo();
    Editor.editProject(p => {
      const outer = p.clips.find(c => c.type === 'video');
      for (const clip of p.clips) Object.assign(clip, {reverse:true, in:.4, duration:3});
      const child = p.media.find(m => m.id === outer.mediaId).nestedProject;
      Object.assign(child.clips.find(c => c.start === 2), {reverse:true, fadeOut:2});
      Object.assign(child.clips.find(c => c.start === 0), {transition:'fade', transitionDuration:.8, fadeIn:1});
    });
    return SpliceNested.unnestClip(clipId).sort((a,b) => a.start-b.start)
      .map(c => [c.start, c.duration, c.reverse, Number(c.in.toFixed(2)), c.pictureDip || null]);
  }, reopenedClipId);
  assert.deepEqual(reversed, [[0, 1.4, false, .6, null], [1.4, 1.6, true, .4, {origin:2, rate:-1, duration:.8}]]);
  const whole = await decoded('reversed-flat');
  await page.evaluate(() => {
    for (const [key, cut] of [['fadeClock', .8], ['pictureDip', 2.6]]) {
      Editor.select(Editor.get().project.clips.find(c => c[key] && c.start <= cut && cut < c.start + c.duration).id);
      Editor.seek(cut);
      Editor.split();
    }
  });
  assert.deepEqual(await decoded('reversed-split'), whole);
  await page.evaluate(() => { Editor.undo(); Editor.undo(); });
  const edits = await page.evaluate(() => {
    const results = [];
    for (const [key, cut] of [['fadeClock', .8], ['pictureDip', 2.6]]) {
      const clip = Editor.get().project.clips.find(c => c[key] && c.start <= cut && cut < c.start + c.duration);
      Editor.select(clip.id);
      Editor.seek(cut);
      Editor.split();
      const right = Editor.get().project.clips.find(c => c.start === cut);
      Editor.trimEdge(right.id, 'start', cut + .08);
      Editor.trimEdge(right.id, 'end', right.start + right.duration - .04);
      const trimmed = Editor.get().project.clips.find(c => c.id === right.id);
      results.push([trimmed[key].origin, trimmed.fadeOut || 0, trimmed.keyframes?.volume]);
      Editor.undo();
      Editor.undo();
      Editor.undo();
    }
    return results;
  });
  assert.ok(Math.abs(edits[0][0] - .52) < 1e-6 && edits[0][1] === 2 && !edits[0][2]);
  assert.ok(Math.abs(edits[1][0] - .72) < 1e-6);

  const clocksAfterEdit = await page.evaluate(() => {
    const id = Editor.get().project.clips.find(c => c.pictureDip && c.fadeClock).id;
    return [{fadeOut:.24}, {transition:'none'}].map(patch => {
      Editor.updateClip(id, patch);
      const edited = Editor.get().project.clips.find(c => c.id === id);
      const clocks = [!!edited.pictureDip, !!edited.fadeClock];
      Editor.undo();
      return clocks;
    });
  });
  assert.deepEqual(clocksAfterEdit, [[true, false], [false, true]]);
  const resetTime = 2.92;
  const faded = await programColor(page, resetTime);
  await page.evaluate(() => {
    const clip = Editor.get().project.clips.find(c => c.pictureDip && c.fadeClock);
    Editor.resetClip(clip.id);
    const reset = Editor.get().project.clips.find(c => c.id === clip.id);
    if ('pictureDip' in reset || 'fadeClock' in reset) throw Error('Reset retained a nested fade clock');
  });
  const restored = await programColor(page, resetTime);
  assert.equal(dominantColor(restored), 'blue');
  assert.ok(faded[2] < restored[2] * .8, 'reset removes the reversed picture dip');
  await decoded('reversed-reset', ['wav']);
  const level = (file, time) => {
    const samples = readPcm(path.join(directory, file)).subarray(Math.round(time*48000), Math.round((time+.04)*48000));
    return Math.sqrt(samples.reduce((sum, v) => sum + v*v, 0) / samples.length);
  };
  const full = level('reversed-reset.wav', 2);
  assert.ok(full > 100 && Math.abs(level('reversed-reset.wav', resetTime) / full - 1) < .05);
  assert.ok(level('reversed-flat.wav', resetTime) < full * .5, 'reset restores full camera sound');
  await page.evaluate(() => Editor.undo());

  await page.evaluate(() => {
    Editor.undo();
    Editor.editProject(p => {
      const outer = p.clips.find(c => c.type === 'video');
      for (const c of p.clips) Object.assign(c, {in:0, duration:4, reverse:false});
      const child = p.media.find(m => m.id === outer.mediaId).nestedProject;
      child.clips = child.clips.filter(c => c.start === 0);
    });
  });
  async function decoded(label, codecs = ['prores', 'wav']) {
    const hashes = [];
    for (const codec of codecs) {
      const job = await page.evaluate(codec => Editor.exportVideo({codec, limiter:false}), codec);
      const response = await page.request.get(new URL(job.downloadURL, page.url()).href);
      assert.equal(response.status(), 200);
      const file = path.join(directory, label + (codec === 'wav' ? '.wav' : '.mov'));
      fs.writeFileSync(file, await response.body());
      hashes.push(ffmpeg(['-i', file, ...(codec === 'wav' ? ['-c:a','pcm_s24le'] : ['-an']),
        '-f','hash','-hash','sha256','-']).toString());
    }
    return hashes;
  }
  await page.evaluate(() => Editor.editProject(p => {
    const outer = p.clips.find(c => c.type === 'video');
    const child = p.media.find(m => m.id === outer.mediaId).nestedProject;
    for (const c of p.clips) c.duration = 2;
    Object.assign(outer, {transform:{scale:50}});
    const c = child.clips.find(c => c.type === 'video');
    Object.assign(c, {transition:'none', transform:{scale:80}});
  }));
  const beforeRefusal = await page.evaluate(() => JSON.stringify(Editor.get().project));
  await assert.rejects(page.evaluate(id => SpliceNested.unnestClip(id), reopenedClipId), /unnest losslessly.*scale/i);
  assert.equal(await page.evaluate(() => JSON.stringify(Editor.get().project)), beforeRefusal);
  await page.evaluate(() => Editor.editProject(p => {
    const child = p.media.find(m => m.id === p.clips[0].mediaId).nestedProject;
    delete child.clips.find(c => c.type === 'video').transform;
  }));
  const scaled = await decoded('scaled-nest', ['prores']);
  await page.evaluate(id => SpliceNested.unnestClip(id), reopenedClipId);
  assert.deepEqual(await decoded('scaled-flat', ['prores']), scaled, 'decoded 50% scale survives unnest');
  const frame = ffmpeg(['-ss','0.5','-i',path.join(directory,'scaled-flat.mov'),'-frames:v','1',
    '-pix_fmt','rgb24','-f','rawvideo','-']);
  let bluePixels = 0;
  for (let i = 2; i < frame.length; i += 3) if (frame[i] > 200) bluePixels++;
  assert.equal(bluePixels, 160 * 90, '50% scale fills one quarter of the 320×180 frame');
  const curves = await page.evaluate(id => {
    Editor.undo();
    Editor.editProject(p => {
      const outer = p.clips.find(c => c.type === 'video');
      outer.color = {saturation:75};
      outer.keyframes = {opacity:[{time:0,value:100},{time:2,value:40}]};
      const child = p.media.find(m => m.id === outer.mediaId).nestedProject;
      const first = child.clips.find(c => c.type === 'video');
      first.duration = 1;
      child.clips.push({...first, id:'second-picture', start:1});
    });
    return SpliceNested.unnestClip(id).filter(c => c.type === 'video')
      .map(c => [c.color.saturation, c.keyframes.opacity.map(p => [p.time,p.value])]);
  }, reopenedClipId);
  assert.deepEqual(curves, [[75,[[0,100],[1,70]]],[75,[[0,70],[1,40]]]]);
  await page.evaluate(() => { Editor.undo(); Editor.undo(); Editor.undo(); Editor.undo(); });
  const padded = await decoded('padded-nest');
  await page.evaluate(id => SpliceNested.unnestClip(id), reopenedClipId);
  assert.deepEqual(await decoded('padded-flat'), padded);
  await page.evaluate(() => { Editor.undo(); Editor.undo(); });
  await page.evaluate(async id => {
    await SpliceNested.enter(id);
    Editor.editProject(p => { p.clips = []; });
    SpliceNested.exit();
  }, nestedInfo.mediaId);
  await waitForNestedPreview(page, nestedInfo.mediaId);
  const proxyDuration = await page.evaluate(async id => {
    const media = Editor.get().project.media.find(m => m.id === id);
    const decoder = document.createElement('video');
    try {
      const ready = new Promise((ok, fail) => { decoder.onloadedmetadata = ok; decoder.onerror = fail; });
      decoder.src = media.src;
      await ready;
      return decoder.duration;
    } finally { decoder.removeAttribute('src'); decoder.load(); }
  }, nestedInfo.mediaId);
  assert.ok(proxyDuration >= 3.4, 'empty child proxy covers the full trimmed parent source range');

  console.log('Nested sequences: nest, step-in, depth-2, edit, parent preview refresh, recovery/reopen, unnest, undo/redo passed.');
}

// ---------------------------------------------------------------------------
// 2. Multicam Editing (Start & Timecode sync, live switching, flatten)
// ---------------------------------------------------------------------------
async function testMulticam(page, files) {
  await freshProjectWithMedia(page, [files.red, files.green, files.blue], 'Multicam Edit');

  // Multi-select all 3 cameras in Media Pool using Ctrl+click.
  const cards = page.locator('#library-body .media-card');
  assert.equal(await cards.count(), 3, '3 camera cards in Media Pool');
  await cards.nth(0).click({modifiers: ['Control']});
  await cards.nth(1).click({modifiers: ['Control']});
  await cards.nth(2).click({modifiers: ['Control']});
  await settle(page);

  // Open Create Multicam dialog via Media Pool toolbar button.
  await page.locator('#multicam-create-btn').click();
  await page.locator('#multicam-create-dialog').waitFor({state: 'visible'});

  // Verify invalid Timecode sync input is refused cleanly when clips lack timecode.
  await page.locator('#multicam-create-dialog select[name="syncMethod"]').selectOption('timecode');
  await settle(page);
  assert.ok(await page.locator('#multicam-create-dialog .mc-offset-warn').isVisible(), 'missing timecode warning shown');
  assert.equal(await page.locator('#multicam-create-dialog button[type="submit"]').isDisabled(), true, 'submit disabled for invalid timecode');

  // Switch back to Start sync -> verify preview table has 3 angles -> create!
  await page.locator('#multicam-create-dialog select[name="syncMethod"]').selectOption('start');
  await page.locator('#multicam-create-dialog select[name="audioMode"]').selectOption('follow');
  await page.locator('#multicam-create-dialog input[name="name"]').fill('MC Start');
  await settle(page);
  assert.equal(await page.locator('#multicam-create-dialog .mc-angle-table tbody tr').count(), 3, '3 angle rows previewed');
  await page.locator('#multicam-create-dialog button[type="submit"]').click();
  await page.locator('#multicam-create-dialog').waitFor({state: 'detached'});
  await settle(page);

  // Also test Create Multicam by Source Timecode after assigning valid timecodes.
  await page.evaluate(() => {
    Editor.editProject(next => {
      const a = next.media.find(m => m.name.includes('CamA_Red'));
      const b = next.media.find(m => m.name.includes('CamB_Green'));
      const c = next.media.find(m => m.name.includes('CamC_Blue'));
      a.timecode = '01:00:00:00';
      b.timecode = '01:00:01:00';
      c.timecode = '01:00:02:00';
    });
  });
  await settle(page);
  const mcTcIds = await page.evaluate(() =>
    ['CamA_Red', 'CamB_Green', 'CamC_Blue'].map(tag => Editor.get().project.media.find(m => m.name.includes(tag)).id)
  );
  await page.evaluate(ids => SpliceMulticam.openCreateDialog(ids, SpliceNotify), mcTcIds);
  await page.locator('#multicam-create-dialog').waitFor({state: 'visible'});
  await page.locator('#multicam-create-dialog select[name="syncMethod"]').selectOption('timecode');
  await page.locator('#multicam-create-dialog input[name="name"]').fill('MC Timecode');
  await settle(page);
  const tcRowsText = await page.locator('#multicam-create-dialog .mc-angle-table').textContent();
  assert.match(tcRowsText, /\+0\.00s/, 'CamA offset +0.00s shown');
  assert.match(tcRowsText, /\+1\.00s/, 'CamB offset +1.00s shown');
  assert.match(tcRowsText, /\+2\.00s/, 'CamC offset +2.00s shown');
  await page.locator('#multicam-create-dialog button[type="submit"]').click();
  await page.locator('#multicam-create-dialog').waitFor({state: 'detached'});
  await settle(page);

  // Verify MC Timecode stored exact offsets.
  const tcOffsets = await page.evaluate(() => {
    const mc = Editor.get().project.media.find(m => m.name === 'MC Timecode');
    return mc.multicam.angles.map(a => a.offset);
  });
  assert.deepEqual(tcOffsets, [0, 1, 2], 'timecode-synced multicam stores exact angle offsets');

  // Edit "MC Start" onto the timeline (0..4s) and turn on the Multicam Monitor!
  await page.evaluate(() => {
    const mc = Editor.get().project.media.find(m => m.name === 'MC Start');
    Editor.editProject(next => {
      next.clips = [{
        id: 'mc_clip_1', mediaId: mc.id, type: 'video', track: 1,
        start: 0, duration: 4, in: 0, color: {}, volume: 100, transition: 'none',
        embeddedAudio: true, multicamAngle: 0,
      }];
    });
    Editor.select('mc_clip_1');
    Editor.seek(0);
  });
  await settle(page);

  await page.locator('#multicam-toggle').click();
  await page.locator('#multicam-grid').waitFor({state: 'visible'});
  await settle(page);
  assert.equal(await page.locator('#multicam-grid .multicam-tile').count(), 3, '3 angle tiles in multicam monitor');
  assert.ok(await page.locator('#multicam-grid .multicam-tile[data-angle="0"]').evaluate(el => el.classList.contains('active')), 'Angle 1 active initially');

  // At t=0 (start of clip), pressing key '2' switches Angle 1 -> Angle 2 WITHOUT cutting!
  await blur(page);
  await page.keyboard.press('Digit2');
  await settle(page);
  assert.equal(await page.evaluate(() => Editor.get().project.clips.length), 1, 'switching at clip start does not add cut');
  assert.equal(await page.evaluate(() => Editor.get().project.clips[0].multicamAngle), 1, 'switched to Angle 2');

  // Seek to t=1.0s and press key '1' -> splits at 1.0s, right segment becomes Angle 1 (Red)!
  await page.evaluate(() => Editor.seek(1.0));
  await settle(page);
  await blur(page);
  await page.keyboard.press('Digit1');
  await settle(page);
  assert.equal(await page.evaluate(() => Editor.get().project.clips.length), 2, 'pressing 1 inside clip adds cut');

  // Seek to t=2.0s and click Angle 3 tile -> splits at 2.0s, right segment becomes Angle 3 (Blue)!
  await page.evaluate(() => Editor.seek(2.0));
  await settle(page);
  await page.locator('#multicam-grid .multicam-tile[data-angle="2"]').click();
  await settle(page);
  assert.equal(await page.evaluate(() => Editor.get().project.clips.length), 3, 'clicking angle tile cuts to Angle 3');
  await capture(page, 'multicam-grid-1440x900.png');

  // Seek to t=2.5s and press Shift+2 -> switches active segment (2..4s) to Angle 2 WITHOUT adding a 4th cut, then Undo restores Angle 3!
  await page.evaluate(() => Editor.seek(2.5));
  await settle(page);
  await blur(page);
  await page.keyboard.press('Shift+Digit2');
  await settle(page);
  assert.equal(await page.evaluate(() => Editor.get().project.clips.length), 3, 'Shift+2 switches without adding cut');
  assert.equal(await page.evaluate(() => Editor.get().project.clips.find(c => c.start >= 1.9).multicamAngle), 1, 'segment switched to Angle 2');
  await page.keyboard.press('Control+z');
  await settle(page);
  assert.equal(await page.evaluate(() => Editor.get().project.clips.find(c => c.start >= 1.9).multicamAngle), 2, 'Ctrl+Z undid Shift+2 switch');

  // Close Multicam Monitor and verify exact preview frames on the Program canvas (Green -> Red -> Blue)!
  await page.locator('#multicam-toggle').click();
  await page.locator('#multicam-grid').waitFor({state: 'hidden'});
  await settle(page);

  assert.equal(dominantColor(await programColor(page, 0.5)), 'green', '0.5s preview is Angle 2 (green)');
  assert.equal(dominantColor(await programColor(page, 1.5)), 'red', '1.5s preview is Angle 1 (red)');
  assert.equal(dominantColor(await programColor(page, 2.5)), 'blue', '2.5s preview is Angle 3 (blue)');

  // Select all 3 multicam segments and Flatten multicam via Edit menu -> verify plain clips match & Undo/Redo!
  const allMcClips = await page.evaluate(() => Editor.get().project.clips.map(c => c.id));
  await page.locator(`.timeline-clip[data-clip="${allMcClips[0]}"]`).click({position: {x: 10, y: 10}});
  for (const id of allMcClips.slice(1)) {
    await page.locator(`.timeline-clip[data-clip="${id}"]`).click({modifiers: ['Control'], position: {x: 10, y: 10}});
  }
  await settle(page);
  await page.locator('.edit-menu summary').click();
  await page.locator('.edit-menu [data-command="flattenMulticam"]').click();
  await settle(page);

  const flattened = await page.evaluate(() => Editor.get().project.clips
    .slice().sort((a, b) => a.start - b.start)
    .map(c => ({
      start: c.start, duration: c.duration, in: c.in,
      mediaName: Editor.get().project.media.find(m => m.id === c.mediaId)?.name,
      hasAngleProp: 'multicamAngle' in c,
    })));
  assert.equal(flattened.length, 3, 'flattened into 3 plain clips');
  assert.ok(flattened.every(c => !c.hasAngleProp), 'flattened clips no longer carry multicamAngle');
  assert.ok(flattened[0].mediaName.includes('CamB_Green') && Math.abs(flattened[0].in - 0) < 0.05, 'segment 1 flattened to CamB_Green @ in=0');
  assert.ok(flattened[1].mediaName.includes('CamA_Red') && Math.abs(flattened[1].in - 1) < 0.05, 'segment 2 flattened to CamA_Red @ in=1');
  assert.ok(flattened[2].mediaName.includes('CamC_Blue') && Math.abs(flattened[2].in - 2) < 0.05, 'segment 3 flattened to CamC_Blue @ in=2');

  // Verify flattened preview matches un-flattened preview pixel-for-pixel!
  assert.equal(dominantColor(await programColor(page, 0.5)), 'green', 'flattened 0.5s still green');
  assert.equal(dominantColor(await programColor(page, 1.5)), 'red', 'flattened 1.5s still red');
  assert.equal(dominantColor(await programColor(page, 2.5)), 'blue', 'flattened 2.5s still blue');

  // Undo restores multicam clips; Redo re-flattens.
  await blur(page);
  await page.keyboard.press('Control+z');
  await settle(page);
  assert.ok(await page.evaluate(() => Editor.get().project.clips.every(c => Number.isInteger(c.multicamAngle))), 'Ctrl+Z restored multicam clips');
  await page.keyboard.press('Control+Shift+Z');
  await settle(page);
  assert.ok(await page.evaluate(() => Editor.get().project.clips.every(c => !('multicamAngle' in c))), 'Ctrl+Shift+Z re-flattened multicam clips');

  console.log('Multicam: Start & Timecode sync, invalid refusal, 2x2 monitor, live keys/clicks/Shift+key, preview pixels, flatten, undo/redo passed.');
}

// ---------------------------------------------------------------------------
// 3. Subclips (Source Monitor -> Media Pool -> Timeline -> Persistence)
// ---------------------------------------------------------------------------
async function testRecorderFirst(page, files) {
  const recorder = path.join(path.dirname(files.red), 'recorder.wav');
  ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=900:duration=4', recorder]);
  await freshProjectWithMedia(page, [recorder, files.green], 'Recorder and camera');
  const selection = await page.evaluate(() => {
    const items = Editor.get().project.media;
    const recorder = items.find(m => m.type === 'audio'), camera = items.find(m => m.type === 'video');
    const mc = SpliceMulticam.createMulticam({angleMediaIds:[recorder.id, camera.id], audioMode:'follow'});
    Editor.addToTimeline(mc.id);
    return Editor.get().project.clips.map(c => c.multicamAngle);
  });
  assert.deepEqual(selection, [1, 1]);
  await page.evaluate(() => {
    Editor.seek(.5);
    SpliceMulticam.setMulticamView(true);
  });
  assert.ok(await page.locator('.multicam-tile[data-angle="0"]').isDisabled());
  await assert.rejects(page.evaluate(() => SpliceMulticam.switchAngle(0)), /video angle/);
  await blur(page);
  await page.keyboard.press('1');
  assert.equal(await page.evaluate(() => Editor.get().project.clips.length), 2);
  assert.equal(await page.locator('[data-action="mc-angle"]').inputValue(), '1');
  assert.equal(await page.locator('[data-action="mc-angle"] option[value="0"]').count(), 0);
  await page.evaluate(() => Editor.select(Editor.get().project.clips.find(c => c.type === 'audio').id));
  await page.locator('[data-action="mc-angle"]').selectOption('0');
  const routing = await page.evaluate(() => Editor.get().project.clips.map(c => Editor.resolveClipMedia(c).item.type));
  assert.deepEqual(routing, ['video', 'audio']);
  await capture(page, 'recorder-first-grid.png');
  await page.evaluate(() => SpliceMulticam.setMulticamView(false));
  assert.equal(dominantColor(await programColor(page, .5)), 'green');
  for (const override of [null, 0]) {
    const result = await page.evaluate(override => {
      Editor.editProject(p => {
        p.clips = p.clips.filter(c => c.type === 'video');
        p.clips[0].embeddedAudio = true;
        delete p.clips[0].linkGroup;
        if (override !== null) p.clips[0].multicamAudioAngle = override;
      });
      const id = Editor.detachAudio(Editor.get().project.clips[0].id);
      const clip = Editor.get().project.clips.find(c => c.id === id);
      return [clip.multicamAngle, clip.multicamAudioAngle ?? null, Editor.resolveClipMedia(clip).item.type];
    }, override);
    assert.deepEqual(result, [1, override, override === null ? 'video' : 'audio']);
  }
}

async function testMulticamSource(page, files) {
  await freshProjectWithMedia(page, [files.multi, files.red], 'Camera source clock');
  await page.evaluate(() => {
    const cameras = Editor.get().project.media;
    const first = cameras.find(m => m.name.includes('MultiSeg'));
    Editor.setSourceSettings(first.id, {sourceMarks:{in:3,out:4}});
    const mc = SpliceMulticam.createMulticam({angleMediaIds:[first.id, cameras.find(m => m.id !== first.id).id]});
    SpliceSource.open(mc.id);
  });
  const video = page.locator('.source-stage video');
  await page.waitForFunction(() => {
    const v = document.querySelector('.source-stage video');
    return v.readyState >= 2 && !v.seeking && Math.abs(v.currentTime-3) < .001;
  });
  const preview = await video.evaluate(v => {
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.drawImage(v,0,0,1,1);
    return [...ctx.getImageData(0,0,1,1).data].slice(0,3);
  });
  assert.equal(dominantColor(preview), 'yellow');
  for (const action of ['in','out','insert']) await page.locator('.source-dialog [data-action="'+action+'"]').click();
  assert.equal(await page.evaluate(() => Editor.get().project.clips.find(c => c.type === 'video').in), 0);
  assert.equal(dominantColor(await programColor(page,0)), dominantColor(preview));
  await capture(page, 'multicam-source-in.png');
  await page.locator('.source-scrub').evaluate(input => {
    input.value = '2';
    input.dispatchEvent(new Event('input'));
  });
  assert.equal(await video.evaluate(v => getComputedStyle(v).visibility), 'hidden');
  await page.locator('.source-close').click();
}

async function testSubclips(page, files) {
  await freshProjectWithMedia(page, [files.multi], 'Subclip Test');
  await page.evaluate(() => SpliceBins.createBin('Selects'));
  await settle(page);

  // Open MultiSeg.mp4 (0-1s red, 1-2s lime, 2-3s blue, 3-4s yellow, 4-5s magenta, 5-6s cyan) in Source Monitor.
  await page.locator('#library-body .media-card').first().click();
  await page.locator('.source-dialog').waitFor({state: 'visible'});
  await page.waitForFunction(() => {
    const v = document.querySelector('.source-dialog video');
    return v && v.readyState >= 1 && v.duration > 5;
  }, {timeout: 15000});

  // Set In = 2.0s (Blue) and Out = 4.0s (end of Yellow, 2.0s duration).
  await page.evaluate(() => {
    const v = document.querySelector('.source-dialog video');
    v.currentTime = 2.0;
  });
  await page.waitForTimeout(150);
  await page.locator('.source-dialog [data-action="in"]').click();
  await page.evaluate(() => {
    const v = document.querySelector('.source-dialog video');
    v.currentTime = 3.96;
  });
  await page.waitForTimeout(150);
  await page.locator('.source-dialog [data-action="out"]').click();
  await settle(page);

  // Trigger Make Subclip via Ctrl+U!
  await page.keyboard.press('Control+u');
  await page.locator('#subclip-dialog').waitFor({state: 'visible'});
  await page.locator('#subclip-dialog input[name="name"]').fill('BlueToYellow Sub');
  await page.locator('#subclip-dialog select[name="bin"]').selectOption('Selects');
  await page.locator('#subclip-dialog button[type="submit"]').click();
  await page.locator('#subclip-dialog').waitFor({state: 'detached'});
  await settle(page);

  const subInfo = await page.evaluate(() => {
    const p = Editor.get().project;
    const sub = p.media.find(m => m.name === 'BlueToYellow Sub');
    return sub ? {id: sub.id, parentMediaId: sub.parentMediaId, subclipIn: sub.subclipIn, subclipOut: sub.subclipOut, duration: sub.duration, bin: sub.bin} : null;
  });
  assert.ok(subInfo && subInfo.parentMediaId, 'subclip created with parentMediaId');
  assert.ok(Math.abs(subInfo.subclipIn - 2.0) < 0.1, `subclipIn ~ 2.0s (got ${subInfo.subclipIn})`);
  assert.ok(Math.abs(subInfo.duration - 2.0) < 0.1, `subclip duration ~ 2.0s (got ${subInfo.duration})`);
  assert.equal(subInfo.bin, 'Selects', 'subclip placed in Selects bin');

  // Close parent in Source Monitor, open the subclip in Source Monitor, inspect badge, and Overwrite onto timeline!
  await page.locator('.source-dialog .source-close').click();
  await page.locator(`.media-card[data-media="${subInfo.id}"]`).click();
  await page.locator('.source-dialog').waitFor({state: 'visible'});
  await settle(page);
  assert.ok(await page.locator('.source-subclip-badge').isVisible(), 'subclip bounds badge shown in Source Monitor');
  await capture(page, 'subclip-source-1440x900.png');

  await page.locator('.source-dialog [data-action="overwrite"]').click();
  await settle(page);
  await page.locator('.source-dialog .source-close').click();

  // Verify subclip on timeline shows BLUE at 0.5s (parent 2.5s) and YELLOW at 1.5s (parent 3.5s)!
  const c0 = await programColor(page, 0.5);
  const c1 = await programColor(page, 1.5);
  assert.equal(nearestSegment(c0), 2, `subclip 0.5s is segment 2 blue (got ${c0})`);
  assert.equal(nearestSegment(c1), 3, `subclip 1.5s is segment 3 yellow (got ${c1})`);

  // Verify hard subclip bounds reject extending duration beyond subclip length!
  const clipId = await page.evaluate(id => Editor.get().project.clips.find(c => c.mediaId === id && c.type === 'video')?.id, subInfo.id);
  assert.ok(clipId, 'subclip video clip on timeline');
  const clampedDur = await page.evaluate(id => {
    try { Editor.trimEdge(id, 'end', 5.0); } catch (_) {}
    return Editor.get().project.clips.find(c => c.id === id).duration;
  }, clipId);
  assert.ok(clampedDur <= subInfo.duration + 0.05, `subclip trim respects hard bounds (${clampedDur} <= ${subInfo.duration})`);

  // Reload page and confirm subclip & timeline clip survive recovery!
  await page.waitForFunction(() => document.getElementById('session-status')?.textContent?.includes('Draft saved'), {timeout: 15000});
  await page.reload();
  await page.waitForFunction(() => Editor.get().project.media.some(m => m.name === 'BlueToYellow Sub' && m.parentMediaId), {timeout: 15000});
  await settle(page);

  // Remove subclip limits via Inspector button -> clip now references parent media with shifted `in`!
  const reloadedClipId = await page.evaluate(() => {
    const p = Editor.get().project;
    const sub = p.media.find(m => m.name === 'BlueToYellow Sub');
    return p.clips.find(c => c.mediaId === sub.id && c.type === 'video').id;
  });
  await page.locator(`.timeline-clip[data-clip="${reloadedClipId}"]`).click({position: {x: 12, y: 10}});
  await settle(page);
  await page.locator('#inspector-content [data-action="remove-subclip-limits"]').click();
  await settle(page);
  const unlinkedClip = await page.evaluate(id => Editor.get().project.clips.find(c => c.id === id), reloadedClipId);
  assert.equal(unlinkedClip.mediaId, subInfo.parentMediaId, 'removing subclip limits points clip to parent media');
  assert.ok(Math.abs(unlinkedClip.in - subInfo.subclipIn) < 0.1, 'removing subclip limits shifts clip.in by subclipIn');

  console.log('Subclips: Ctrl+U creation, bounds badge, timeline offset pixels, hard trim bounds, reload persistence, remove limits passed.');
}

// ---------------------------------------------------------------------------
// 4. Real Bin Management & Editorial Metadata / Color Labels
// ---------------------------------------------------------------------------
async function testBinsAndMetadata(page, files) {
  await freshProjectWithMedia(page, [files.red, files.green, files.blue], 'Bins & Meta');

  // Create bin "Scene 1" and nested sub-bin "Scene 1/Cam A".
  await page.locator('#bin-create-btn').click();
  await page.locator('#bin-create-dialog').waitFor({state: 'visible'});
  await page.locator('#bin-create-dialog input[name="path"]').fill('Scene 1');
  await page.locator('#bin-create-dialog button[type="submit"]').click();
  await page.locator('#bin-create-dialog').waitFor({state: 'detached'});
  await settle(page);

  await page.locator('#bin-create-btn').click();
  await page.locator('#bin-create-dialog').waitFor({state: 'visible'});
  await page.locator('#bin-create-dialog input[name="path"]').fill('Scene 1/Cam A');
  await page.locator('#bin-create-dialog button[type="submit"]').click();
  await page.locator('#bin-create-dialog').waitFor({state: 'detached'});
  await settle(page);

  // Verify breadcrumb shows All bins › Scene 1 › Cam A.
  const crumb = await page.locator('.bin-crumb-bar').textContent();
  assert.match(crumb, /Scene 1/, 'breadcrumb includes parent bin');
  assert.match(crumb, /Cam A/, 'breadcrumb includes child bin');

  // Switch back to All bins, select CamA_Red, move it into "Scene 1/Cam A" via Move… dialog.
  await page.locator('.bin-crumb-btn', {hasText: 'All bins'}).click();
  await settle(page);
  const redId = await page.evaluate(() => Editor.get().project.media.find(m => m.name.includes('CamA_Red')).id);
  const greenId = await page.evaluate(() => Editor.get().project.media.find(m => m.name.includes('CamB_Green')).id);
  await page.locator(`.media-card[data-media="${redId}"]`).click({modifiers: ['Control']});
  await settle(page);
  await page.locator('#bin-move-btn').click();
  await page.locator('#bin-move-dialog').waitFor({state: 'visible'});
  await page.locator('#bin-move-dialog select[name="existing"]').selectOption('Scene 1/Cam A');
  await page.locator('#bin-move-dialog button[type="submit"]').click();
  await page.locator('#bin-move-dialog').waitFor({state: 'detached'});
  await settle(page);
  assert.equal(await page.evaluate(id => Editor.get().project.media.find(m => m.id === id).bin, redId), 'Scene 1/Cam A', 'Move dialog moved item into nested bin');

  // Drag CamB_Green onto "Scene 1" bin node via synthetic DataTransfer drop event.
  await page.evaluate(id => {
    const node = document.querySelector('.bin-node[data-bin-path="Scene 1"]');
    const dt = new DataTransfer();
    dt.setData('text/plain', id);
    node.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt}));
  }, greenId);
  await settle(page);
  assert.equal(await page.evaluate(id => Editor.get().project.media.find(m => m.id === id).bin, greenId), 'Scene 1', 'Drag-and-drop moved item into Scene 1');

  // Rename "Scene 1" -> "Act 1" and confirm nested sub-bin & media items update!
  await page.locator('.bin-node[data-bin-path="Scene 1"] button[aria-label="Rename bin Scene 1"]').click();
  await page.locator('#bin-rename-dialog').waitFor({state: 'visible'});
  await page.locator('#bin-rename-dialog input[name="path"]').fill('Act 1');
  await page.locator('#bin-rename-dialog button[type="submit"]').click();
  await page.locator('#bin-rename-dialog').waitFor({state: 'detached'});
  await settle(page);
  assert.equal(await page.evaluate(id => Editor.get().project.media.find(m => m.id === id).bin, redId), 'Act 1/Cam A', 'rename bin updated nested item path');
  assert.equal(await page.evaluate(id => Editor.get().project.media.find(m => m.id === id).bin, greenId), 'Act 1', 'rename bin updated direct item path');

  // Delete "Act 1/Cam A" -> confirm dialog -> CamA_Red moves safely to "Act 1" (never deleted!).
  await page.locator('.bin-node[data-bin-path="Act 1/Cam A"] button[aria-label="Delete bin Act 1/Cam A"]').click();
  await page.locator('#bin-delete-dialog').waitFor({state: 'visible'});
  await page.locator('#bin-delete-dialog [data-confirm]').click();
  await page.locator('#bin-delete-dialog').waitFor({state: 'detached'});
  await settle(page);
  assert.equal(await page.evaluate(() => Editor.get().project.media.length), 3, 'deleting bin never deletes media');
  assert.equal(await page.evaluate(id => Editor.get().project.media.find(m => m.id === id).bin, redId), 'Act 1', 'deleted sub-bin item moved to parent bin');

  // Set editorial metadata & color labels on CamA_Red (via right-click dialog) and CamB_Green (via Source Monitor).
  await page.locator(`.media-card[data-media="${redId}"]`).click({button: 'right'});
  await page.locator('.sync-menu button', {hasText: 'Metadata & color label'}).click();
  await page.locator('#media-metadata-dialog').waitFor({state: 'visible'});
  await page.locator('#media-metadata-dialog input[name="scene"]').fill('12A');
  await page.locator('#media-metadata-dialog input[name="shot"]').fill('CU');
  await page.locator('#media-metadata-dialog input[name="camera"]').fill('Camera C146');
  await page.locator('#media-metadata-dialog input[name="take"]').fill('3');
  await page.locator('#media-metadata-dialog input[name="reel"]').fill('A001');
  await page.locator('#media-metadata-dialog input[name="logNote"]').fill('Hero smile');
  await page.locator('#media-metadata-dialog .label-swatch-btn[data-color="orange"]').click();
  await page.locator('#media-metadata-dialog button[type="submit"]').click();
  await page.locator('#media-metadata-dialog').waitFor({state: 'detached'});
  await settle(page);

  await page.locator(`.media-card[data-media="${greenId}"]`).click();
  await page.locator('.source-dialog').waitFor({state: 'visible'});
  await page.locator('.source-meta-grid [data-meta="scene"]').fill('04B');
  await page.locator('.source-meta-grid [data-meta="scene"]').dispatchEvent('change');
  await page.locator('.source-meta-grid [data-meta="colorLabel"]').selectOption('green');
  await page.locator('.source-dialog .source-close').click();
  await settle(page);

  // Place CamA_Red on timeline and confirm color label stripe appears on timeline clip, then override via Inspector!
  await page.evaluate(id => {
    Editor.editProject(next => {
      next.clips = [{id: 'labeled_clip', mediaId: id, type: 'video', track: 1, start: 0, duration: 2, in: 0, color: {}, volume: 100, transition: 'none'}];
    });
    Editor.select('labeled_clip');
  }, redId);
  await settle(page);
  assert.equal(await page.locator('.timeline-clip[data-clip="labeled_clip"]').getAttribute('data-label-color'), 'orange', 'clip inherits media color label');
  await page.locator('#inspector-content select[data-field="colorLabel"]').selectOption('purple');
  await settle(page);
  assert.equal(await page.locator('.timeline-clip[data-clip="labeled_clip"]').getAttribute('data-label-color'), 'purple', 'Inspector updates clip color label');

  // Switch Media Pool to List view -> verify columns, search by Log Note, and sort by Scene!
  await page.locator('#media-view').click();
  await settle(page);
  assert.ok(await page.locator('.media-table-head [data-sort="scene"]').isVisible(), 'Scene column present');
  assert.ok(await page.locator('.media-table-head [data-sort="logNote"]').isVisible(), 'Log Note column present');
  assert.ok(await page.locator('.media-table-head [data-sort="colorLabel"]').isVisible(), 'Label column present');
  await capture(page, 'bins-metadata-list-1440x900.png');

  // Sort by Scene ascending -> 04B comes before 12A (non-empty values order predictably).
  await page.locator('.media-table-head [data-sort="scene"]').click();
  await settle(page);
  await page.locator('#media-search').fill('C146');
  await settle(page);
  assert.equal(await page.locator('#library-body .media-row').count(), 1);
  assert.match(await page.locator('#library-body .media-row').textContent(), /CamA_Red/);
  await page.locator('#media-search').fill('Hero smile');
  await settle(page);
  assert.equal(await page.locator('#library-body .media-row').count(), 1, 'search by Log Note filters to 1 matching row');
  assert.match(await page.locator('#library-body .media-row').first().textContent(), /CamA_Red/, 'matched row is CamA_Red');
  await page.locator('#media-search').fill('');
  await settle(page);

  console.log('Bins & Metadata: nested bins, move dialog, drag-drop, rename, safe delete, metadata/color labels, list sort & search passed.');
}

// ---------------------------------------------------------------------------
// 5. Responsive Viewport Checks (1180x780 & 1024x640)
// ---------------------------------------------------------------------------
async function testViewportLayout(splice, viewport, files) {
  const label = `${viewport.width}x${viewport.height}`;
  const {page, errors} = await openPage(splice, viewport);
  try {
    await freshProjectWithMedia(page, [files.red, files.green, files.blue], `Layout ${label}`);
    // Create a nested bin + multicam clip, put multicam on timeline, turn on Multicam Monitor!
    await page.evaluate(() => {
      SpliceBins.createBin('Scene 1/Cam A');
      const items = Editor.get().project.media;
      SpliceBins.moveMediaToBin([items[0].id], 'Scene 1/Cam A');
      SpliceBins.updateMediaMetadata(items[0].id, {scene: '1', shot: 'WS', take: '1', colorLabel: 'blue'});
      const mc = SpliceMulticam.createMulticam({name: 'MC Responsive', angleMediaIds: items.map(m => m.id), syncMethod: 'start'});
      Editor.editProject(next => {
        next.clips = [{id: 'mc_r', mediaId: mc.id, type: 'video', track: 1, start: 0, duration: 3, in: 0, color: {}, volume: 100, transition: 'none', multicamAngle: 0}];
      });
      Editor.select('mc_r');
      Editor.seek(0.5);
    });
    await settle(page);

    // Toggle Multicam Monitor and verify zero horizontal clipping of viewer controls and bin browser!
    await page.locator('#multicam-toggle').click();
    await page.locator('#multicam-grid').waitFor({state: 'visible'});
    await settle(page);

    const metrics = await page.evaluate(() => {
      const vw = window.innerWidth, vh = window.innerHeight;
      const check = selector => {
        const el = document.querySelector(selector);
        if (!el) return {missing: true};
        const r = el.getBoundingClientRect();
        return {w: Math.round(r.width), h: Math.round(r.height), inside: r.left >= -1 && r.right <= vw + 1 && r.top >= -1 && r.bottom <= vh + 1};
      };
      return {
        multicamBtn: check('#multicam-toggle'),
        scopeBtn: check('#scope-dock-toggle'),
        grid: check('#multicam-grid'),
        binBrowser: check('#bin-browser'),
        tiles: document.querySelectorAll('#multicam-grid .multicam-tile').length,
      };
    });
    assert.equal(metrics.tiles, 3, `${label}: 3 multicam tiles visible`);
    assert.ok(metrics.multicamBtn.inside && metrics.multicamBtn.w > 20, `${label}: Multicam button fits inside viewport (${JSON.stringify(metrics.multicamBtn)})`);
    assert.ok(metrics.scopeBtn.inside && metrics.scopeBtn.w > 20, `${label}: Scopes button fits inside viewport (${JSON.stringify(metrics.scopeBtn)})`);
    assert.ok(metrics.grid.inside && metrics.grid.w >= 260 && metrics.grid.h >= 140, `${label}: Multicam grid usable (${JSON.stringify(metrics.grid)})`);
    assert.ok(metrics.binBrowser.inside, `${label}: Bin browser stays inside Media Pool (${JSON.stringify(metrics.binBrowser)})`);

    // Live angle switch works at this viewport too!
    await page.locator('#multicam-grid .multicam-tile[data-angle="1"]').click();
    await settle(page);
    assert.equal(await page.evaluate(() => Editor.get().project.clips.length), 2, `${label}: clicking multicam tile cuts angle`);

    await capture(page, `multicam-grid-${label}.png`);

    await page.evaluate(() => SpliceBins.createSubclip({name:'Badge geometry',
      parentMediaId:Editor.get().project.media[0].id, subclipIn:0, subclipOut:1}));
    await page.locator('#media-search').fill('Badge geometry');
    const badge = page.locator('.media-badge-pill.subclip');
    await badge.waitFor({state:'visible'});
    assert.ok(await badge.evaluate(el => {
      const a = el.getBoundingClientRect(), b = el.parentElement.querySelector('.media-icon').getBoundingClientRect();
      return a.left >= b.right || a.right <= b.left || a.top >= b.bottom || a.bottom <= b.top;
    }), label + ': SUB badge clears the media icon');

    if (viewport.width === 1024) {
      await page.locator('#media-view').click();
      await settle(page);
      await capture(page, `bins-metadata-${label}.png`);
    }

    assert.deepEqual(errors, [], `${label}: zero page errors`);
    console.log(`Viewport ${label}: Multicam grid (${metrics.grid.w}x${metrics.grid.h}), bin browser, and live cut verified.`);
  } finally {
    await page.context().close();
  }
}

// Runs in the page, through the same WAV export used by the editor.
async function stereoWavLevels() {
  const job = await Editor.exportVideo({codec:'wav', limiter:false}, undefined, AbortSignal.timeout(60000));
  const bytes = await (await fetch(job.downloadURL)).arrayBuffer();
  const pcm = await new OfflineAudioContext(2, 1, 48000).decodeAudioData(bytes);
  return [0,1].flatMap(ch => [.25,1.25].map(t => {
    const samples = pcm.getChannelData(ch).subarray(t * 48000, (t + .25) * 48000);
    return Math.sqrt(samples.reduce((sum,v) => sum + v*v, 0) / samples.length);
  }));
}

async function testAudioParity(page, files) {
  // Attach before the editor creates its first video-element route.
  await page.addInitScript(() => {
    const connect = AudioNode.prototype.connect;
    let spectrum;
    AudioNode.prototype.connect = function (node, ...args) {
      if (node === this.context.destination) {
        spectrum ||= this.context.createAnalyser();
        spectrum.fftSize = 4096;
        spectrum.smoothingTimeConstant = 0;
        connect.call(this, spectrum);
      }
      return connect.call(this, node, ...args);
    };
    window.previewTones = () => {
      if (!spectrum) return [];
      const bins = new Float32Array(spectrum.frequencyBinCount);
      spectrum.getFloatFrequencyData(bins);
      return [300,500,700].filter(hz => bins[Math.round(hz*spectrum.fftSize/spectrum.context.sampleRate)] > -46);
    };
    window.restoreAudioProbe = () => { AudioNode.prototype.connect = connect; };
  });
  await page.reload();
  await page.evaluate(() => SpliceSession.ready);
  await freshProjectWithMedia(page, [files.red, files.green, files.blue], 'Audio parity');
  await page.evaluate(() => {
    const ids = [...Editor.get().project.media].sort((a,b) => a.name.localeCompare(b.name)).map(m => m.id);
    const mc = SpliceMulticam.createMulticam({name:'All cameras', angleMediaIds:ids, audioMode:'all'});
    Editor.editProject(p => {
      Object.assign(p.media.find(m => m.id === mc.id).multicam.angles[2], {offset:.4, duration:1.6});
      p.clips = [{id:'sound', mediaId:mc.id, type:'audio', track:-1, start:0, duration:3, in:0}];
    });
    Editor.seek(.25);
    Editor.togglePlay();
  });
  try {
    await page.waitForFunction(() => previewTones().length === 3);
    await page.evaluate(() => Editor.updateClip('sound', {multicamAudioAngle:1}));
    await page.waitForFunction(() => String(previewTones()) === '500');
    await page.evaluate(() => {
      if (Editor.get().playing) Editor.togglePlay();
      Editor.editProject(p => {
        Object.assign(p.clips[0], {type:'video', track:1, embeddedAudio:true});
        delete p.clips[0].multicamAudioAngle;
      });
      Editor.seek(.25);
      Editor.togglePlay();
    });
    await page.waitForFunction(() => previewTones().length === 3 && Editor.get().video.muted);
    await page.evaluate(() => Editor.seek(2.2));
    await page.waitForFunction(() => String(previewTones()) === '300,500');
    for (const [mode, override, hz] of [['camera1', null, 300], ['fixed', null, 700], ['follow', 0, 300], ['follow', null, 500]]) {
      await page.evaluate(({mode, override}) => {
        if (Editor.get().playing) Editor.togglePlay();
        Editor.editProject(p => {
          const clip = p.clips[0], mc = p.media.find(m => m.id === clip.mediaId).multicam;
          Object.assign(mc, {audioMode:mode, audioAngle:2});
          clip.multicamAngle = 1;
          if (override === null) delete clip.multicamAudioAngle;
          else clip.multicamAudioAngle = override;
        });
      }, {mode, override});
      assert.equal(dominantColor(await programColor(page,.8)), 'green', 'camera 2 stays on screen');
      const url = await page.evaluate(async () => (await Editor.exportVideo({codec:'wav'})).downloadURL);
      const file = path.join(path.dirname(files.red), 'embedded.wav');
      fs.writeFileSync(file, await (await page.request.get(new URL(url,page.url()).href)).body());
      const exported = toneAt(readPcm(file), .8);
      assert.ok(Math.abs(exported - hz) <= 5, mode + ' export');
      await page.evaluate(() => Editor.togglePlay());
      await page.waitForFunction(() => previewTones().length > 0 && Editor.get().time > 1.05);
      const tones = await page.evaluate(() => previewTones());
      console.log('Embedded multicam:', {mode, override, tones, exported});
      assert.deepEqual(tones, [hz], mode + ' preview');
      if (mode === 'camera1') await capture(page, 'embedded-picture2-audio1.png');
    }
  } finally {
    await page.evaluate(() => { if (Editor.get().playing) Editor.togglePlay(); restoreAudioProbe(); });
  }
  // Compare the stereo WAV both before and after nesting part of a mixed track.
  await page.evaluate(() => Editor.editProject(p => {
    const mediaId = p.media.find(m => m.name.includes('CamA_Red')).id;
    p.clips = [0,1].map(start => ({id:'a'+start, type:'audio', track:-1, mediaId, start, duration:1, in:0}));
    p.trackMix = {'-1':{gain:50, pan:35}};
  }));
  const before = await page.evaluate(stereoWavLevels);
  await page.evaluate(() => SpliceNested.nestSelection(['a0']));
  const after = await page.evaluate(stereoWavLevels);
  console.log('WAV channel RMS before and after nesting:', {before, after});
  before.forEach((v,i) => assert.ok(v > .005 && Math.abs(after[i]/v - 1) < .03));
}

async function testUnnestMix(page, files) {
  await freshProjectWithMedia(page, [files.red, files.green], 'Unnest mix');
  for (const scenario of ['zero volume','parent gain','parent pan','boost','gain and pan','muted','child solo','parent solo','relocated','embedded']) {
    const nest = await page.evaluate(scenario => {
      const [camera, other] = [...Editor.get().project.media].filter(m => !m.nestedProject)
        .sort((a,b) => a.name.localeCompare(b.name));
      const embedded = scenario === 'embedded';
      Editor.editProject(p => {
        p.trackMix = {};
        if (scenario === 'parent solo') p.trackMix[-1] = {solo:true};
        if (scenario === 'relocated') p.trackMix[-1] = {gain:45, pan:55};
        p.clips = [
          {id:'inside', type:embedded ? 'video' : 'audio', mediaId:camera.id, track:embedded ? 1 : -1,
            start:0, duration:1, in:0},
          {id:'neighbor', type:embedded ? 'video' : 'audio', mediaId:other.id, track:embedded ? 1 : -1,
            start:1, duration:1, in:0},
        ];
        if (scenario === 'child solo') p.clips.push({id:'second', type:'audio', mediaId:other.id,
          track:-2, start:0, duration:1, in:0});
      });
      const nest = SpliceNested.nestSelection(scenario === 'child solo' ? ['inside','second'] : ['inside']);
      if (scenario === 'relocated') Editor.editProject(p => {
        p.clips.push({id:'busy', type:'audio', mediaId:other.id, track:-1, start:0, duration:1, in:0});
      });
      return nest;
    }, scenario);
    await page.evaluate(async ({nest, scenario}) => {
      await SpliceNested.enter(nest.mediaId);
      Editor.updateTrackMix(scenario === 'embedded' ? 1 : -1,
        {gain:scenario === 'boost' ? 160 : 45, pan:55, mute:scenario === 'muted', solo:scenario === 'child solo'});
      if (scenario === 'parent solo') Editor.updateTrackMix(-2, {solo:true});
      SpliceNested.exit();
      if (scenario === 'zero volume') Editor.editProject(p => p.clips.find(c => c.mediaId === nest.mediaId).volume = 0);
      if (['parent gain','parent pan','boost'].includes(scenario)) {
        const outer = Editor.get().project.clips.find(c => c.mediaId === nest.mediaId);
        Editor.updateTrackMix(outer.track, {gain:scenario === 'boost' ? 180 : 50, pan:scenario === 'parent pan' ? -40 : 0});
      }
    }, {nest, scenario});
    const before = await page.evaluate(stereoWavLevels);
    await page.evaluate(id => SpliceNested.unnestClip(id), nest.clipIds[0]);
    const after = await page.evaluate(stereoWavLevels);
    console.log('Unnest stereo RMS:', {scenario, before, after});
    if (scenario === 'relocated') await capture(page, 'unnested-audio-tracks.png');
    assert.ok(before[1] > .005 && before[3] > .005, 'parent neighbor is audible');
    assert.ok(scenario === 'zero volume' || scenario === 'muted' || scenario === 'parent solo' ? before[0] < .001 : before[0] > .001);
    before.forEach((v,i) => assert.ok(Math.abs(v - after[i]) < Math.max(.0003, v*.03), scenario));
  }
}

async function testCameraEffectTiming(page, files) {
  await freshProjectWithMedia(page, [files.red, files.green], 'Camera effect timing');
  const mc = await page.evaluate(() => SpliceMulticam.createMulticam({name:'Two cameras', audioMode:'follow',
    angleMediaIds:Editor.get().project.media.slice().sort((a,b) => a.name.localeCompare(b.name)).map(m => m.id)}).id);
  for (const scenario of ['cut', 'late', 'flattened', 'fade', 'dissolve', 'wipe', 'fade flat', 'dissolve flat', 'wipe flat']) {
    const transition = ['fade','wipe','dissolve'].find(kind => scenario.startsWith(kind));
    await page.evaluate(({mc, scenario, transition}) => {
      const ramp = [{time:0,value:0}, {time:4,value:100}];
      Editor.editProject(p => {
        const late = scenario !== 'cut';
        Object.assign(p.media.find(m => m.id === mc).multicam.angles[1], {offset:late ? 2 : 0, duration:late ? 2 : 4});
        const base = {mediaId:mc, start:0, in:0, duration:4, linkGroup:'camera', multicamAngle:late ? 1 : 0,
          ...(transition ? {transition, transitionDuration:4} : {})};
        p.clips = [{...base, id:'v', type:'video', track:1, embeddedAudio:false, keyframes:{opacity:ramp}},
          {...base, id:'a', type:'audio', track:-1, fadeIn:4, keyframes:{volume:ramp}}];
      });
      Editor.select('v');
      Editor.seek(2);
      if (scenario === 'cut') SpliceMulticam.switchAngle(1);
      if (scenario === 'flattened' || scenario.endsWith(' flat')) SpliceMulticam.flattenClips(['v']);
    }, {mc, scenario, transition});
    const preview = await programColor(page, 2);
    const gains = await page.evaluate(() => Object.values(Editor.audioGains()));
    assert.deepEqual(gains, [transition ? .125 : .25], scenario + ' preview gain');
    assert.ok(Math.abs(preview[1] - (transition ? 64 : 127)) < 10, scenario + ' preview ' + preview);
    if (!transition) await capture(page, 'camera-effect-' + scenario + '.png');
    const url = await page.evaluate(async () =>
      (await Editor.exportVideo({codec:'h264', limiter:false}, undefined, AbortSignal.timeout(60000))).downloadURL);
    const response = await page.request.get(new URL(url, page.url()).href);
    assert.ok(response.ok());
    const file = path.join(path.dirname(files.red), scenario + '.mp4');
    fs.writeFileSync(file, await response.body());
    const rgb = colorAt(file, 2);
    const pcm = readPcm(file);
    const rms = t => {
      const samples = pcm.slice(Math.round(t*48000), Math.round((t+.04)*48000));
      return Math.sqrt(samples.reduce((s,v) => s + v*v, 0)/samples.length);
    };
    const ratio = rms(2.04)/rms(3.84);
    assert.ok(Math.abs(rgb[1] - preview[1]) < 10, scenario + ' export ' + rgb);
    assert.ok(Math.abs(ratio - (2.06/3.86)**(transition ? 3 : 2)) < .025, scenario + ' audio ratio ' + ratio);
    if (scenario === 'cut') assert.ok(Math.abs(rms(1.94)/rms(2.04) - (1.96/2.06)**2) < .04);
    console.log('Camera effect timing:', {scenario, preview, gains, rgb, ratio});
  }
}

async function testFlattenRouting(page, files) {
  await freshProjectWithMedia(page, [files.red, files.green, files.blue], 'Flatten routing');
  const {mcId, cameras} = await page.evaluate(() => {
    const cameras = [...Editor.get().project.media].sort((a,b) => a.name.localeCompare(b.name)).map(m => m.id);
    const mc = SpliceMulticam.createMulticam({name:'Cameras', angleMediaIds:cameras.slice(0,2), audioMode:'all'});
    return {mcId:mc.id, cameras};
  });
  for (const mode of ['mixed','muted','override','embedded mix','embedded override']) {
    const embedded = mode.startsWith('embedded');
    const override = mode.endsWith('override');
    await page.evaluate(({embedded, override, mode, mcId, cameras}) => Editor.editProject(p => {
      p.trackMix = {
        [embedded ? -64 : -1]:{gain:45, pan:60, mute:mode === 'muted', solo:true},
        '-2':{gain:75, pan:-35, solo:true},
      };
      p.clips = [
        {id:'mc', type:embedded ? 'video' : 'audio', mediaId:mcId, track:embedded ? 1 : -1,
          start:0, duration:1, in:0, ...(override ? {multicamAudioAngle:1} : {})},
        {id:'other', type:'audio', mediaId:cameras[2], track:-2, start:1, duration:1, in:0},
      ];
    }), {embedded, override, mode, mcId, cameras});
    const before = await page.evaluate(stereoWavLevels);
    const angles = await page.evaluate(cameras => SpliceMulticam.flattenClips(['mc'])
      .filter(c => c.type === 'audio').map(c => cameras.indexOf(c.mediaId)), cameras);
    const after = await page.evaluate(stereoWavLevels);
    console.log('Flattened stereo WAV:', {mode, angles, before, after});
    assert.deepEqual(angles, override ? [1] : [0,1], mode);
    assert.ok(before[1] > .005 && before[3] > .005, 'unrelated clip is audible');
    assert.ok(mode === 'muted' ? before[0] === 0 : before[0] > .001, mode);
    before.forEach((v,i) => assert.ok(Math.abs(after[i] - v) < .0001, mode + ' changes stereo output'));
  }
  const tail = await page.evaluate(mcId => {
    Editor.editProject(p => {
      p.media.find(m => m.id === mcId).multicam.angles[0].duration = .8;
      p.clips = [{id:'mc', type:'video', track:1, mediaId:mcId, start:0, duration:2,
        in:0, multicamAngle:0, multicamAudioAngle:0}];
    });
    return SpliceMulticam.flattenClips(['mc']).map(c => [c.start, c.duration, c.enabled !== false]);
  }, mcId);
  assert.deepEqual(tail, [[0,.8,true],[0,.8,true],[.8,1.2,false]]);
  const exhausted = await page.evaluate(mcId => {
    Editor.editProject(p => {
      p.clips = [{id:'mc', type:'audio', mediaId:mcId, track:-1, start:0, duration:1, in:0}];
      p.trackState = {};
      for (let i = 2; i <= 64; i++) p.trackState['audio:' + -i] = {locked:true};
    });
    const before = JSON.stringify(Editor.get().project);
    let message;
    try { SpliceMulticam.flattenClips(['mc']); } catch (error) { message = error.message; }
    return {message, intact:before === JSON.stringify(Editor.get().project)};
  }, mcId);
  assert.match(exhausted.message, /free, unlocked audio track/);
  assert.ok(exhausted.intact, 'a full timeline retains every angle and mixer setting');
}

async function testNestVisibility(page, files) {
  await freshProjectWithMedia(page, [files.red, files.green], 'Nest track visibility');
  await page.evaluate(() => {
    const media = [...Editor.get().project.media].sort((a,b) => a.name.localeCompare(b.name));
    Editor.editProject(p => {
      p.clips = media.map((m,i) => ({id:'v'+i, type:'video', mediaId:m.id, track:i+1,
        start:i, duration:1, in:0, embeddedAudio:false}));
    });
    Editor.setTrackState(1, 'video', {hidden:true});
  });
  const before = [await programColor(page,.5), await programColor(page,1.5)];
  assert.ok(Math.max(...before[0]) < 25);
  assert.equal(dominantColor(before[1]), 'green');
  const nest = await page.evaluate(() => SpliceNested.nestSelection(['v0','v1']));
  await waitForNestedPreview(page, nest.mediaId);
  const after = [await programColor(page,.5), await programColor(page,1.5)];
  console.log('Program RGB before and after nesting hidden V1 and visible V2:', {before, after});
  assert.ok(Math.max(...after[0]) < 25);
  assert.equal(dominantColor(after[1]), 'green');
  await capture(page, 'nested-visible-v2.png');
  const url = await page.evaluate(async () =>
    (await Editor.exportVideo({codec:'h264'}, undefined, AbortSignal.timeout(60000))).downloadURL);
  const response = await page.request.get(new URL(url, page.url()).href);
  assert.ok(response.ok());
  const file = path.join(path.dirname(files.red), 'visible-v2.mp4');
  fs.writeFileSync(file, await response.body());
  const colors = [.5, 1.5].map(t => colorAt(file, t));
  console.log('Nested H.264 export RGB:', colors);
  assert.ok(Math.max(...colors[0]) < 25);
  assert.equal(dominantColor(colors[1]), 'green');

  // Swap the output lanes in the parent while the child keeps V1 hidden and V2 visible.
  await page.evaluate(() => {
    Editor.setTrackState(1, 'video', {hidden:false});
    Editor.setTrackState(2, 'video', {hidden:true});
    Editor.editProject(p => {
      const nest = p.clips[0];
      nest.track = 3;
      p.clips.push({...nest, id:'neighbor', mediaId:p.media.find(m => m.name.includes('CamA_Red')).id,
        track:1, start:2, duration:1, embeddedAudio:false, nestedSequence:undefined});
      p.clips.push({...p.clips.at(-1), id:'under', track:1, start:1, duration:1},
        {...p.clips.at(-1), id:'over', track:4, start:1.8, duration:.2});
    });
  });
  await page.evaluate(id => SpliceNested.unnestClip(id), nest.clipIds[0]);
  const visible = [];
  for (const t of [.5,1.5,2.5]) visible.push(await programColor(page,t));
  console.log('Program RGB after unnesting onto different parent lanes:', visible);
  assert.ok(Math.max(...visible[0]) < 25);
  assert.equal(dominantColor(visible[1]), 'green');
  assert.equal(dominantColor(visible[2]), 'red');
  assert.equal(dominantColor(await programColor(page,1.9)), 'red', 'upper parent layer stays above child');
  const layers = await page.evaluate(() => Editor.get().project.clips.map(c => [c.id,c.track]));
  assert.ok(layers.find(([id]) => id === 'over')[1] > layers.find(([id]) => id === 'under')[1]);
  await capture(page, 'unnest-visible-lanes.png');
}

async function testUnnestComponents(page, files) {
  await freshProjectWithMedia(page,[files.red,files.green],'Unnest one component');
  for (const component of ['audio','video']) {
    const outer = await page.evaluate(component => {
      Editor.editProject(p => {
        const camera = p.media.find(m => m.name.includes('CamA_Red'));
        p.clips = [{id:'cam', type:'video', track:1, mediaId:camera.id, start:0, duration:1, in:0, embeddedAudio:true},
          {id:'title', type:'title', track:2, start:0, duration:1, in:0, title:'Child'},
          {id:'mic', type:'audio', track:-1, mediaId:camera.id, start:0, duration:1, in:0}];
      });
      const nest = SpliceNested.nestSelection(['cam','title','mic']);
      Editor.editProject(p => { p.clips = p.clips.filter(c => c.type === component); delete p.clips[0].linkGroup; });
      return Editor.get().project.clips.find(c => c.mediaId === nest.mediaId).id;
    }, component);
    const result = await page.evaluate(id => SpliceNested.unnestClip(id).map(c => [c.type,c.embeddedAudio]), outer);
    console.log('Remaining nest component', component, result);
    if (component === 'audio') assert.deepEqual(result.map(c => c[0]), ['audio','audio']);
    else assert.deepEqual(result, [['video',false],['title',undefined]]);
  }
}

async function main() {
  const splice = await startSplice('splice-mn-');
  try {
    const files = {
      red: path.join(splice.temp, 'CamA_Red.mp4'),
      green: path.join(splice.temp, 'CamB_Green.mp4'),
      blue: path.join(splice.temp, 'CamC_Blue.mp4'),
      multi: path.join(splice.temp, 'MultiSeg.mp4'),
    };
    makeSolidClip(files.red, 'red', 300, 4);
    makeSolidClip(files.green, 'lime', 500, 4);
    makeSolidClip(files.blue, 'blue', 700, 4);
    makeCamera(files.multi);

    // Full end-to-end functional suite at 1440x900
    const {page, errors} = await openPage(splice, {width: 1440, height: 900});
    try {
      for (const kind of ['cycle','depth']) {
        await assert.rejects(page.evaluate(kind => Editor.editProject(p => {
          const child = () => ({name:'child', width:160, height:90, fps:25, media:[], clips:[]});
          let parent = p;
          for (let level = 0; level < 5; level++) {
            const next = kind === 'cycle' && level === 1 ? p : child();
            parent.media.push({id:'level'+level, name:'child', src:'', type:'video', duration:1,
              ...(level % 2 ? {nestedSequence:false} : {}), nestedProject:next});
            if (next === p) break;
            parent = next;
          }
        }), kind), kind === 'cycle' ? /Circular/ : /four levels/);
      }
      await testCameraEffectTiming(page, files);
      await testNestedSequences(page, files, splice.temp);
      await testMulticam(page, files);
      await testAudioParity(page, files);
      await testUnnestMix(page, files);
      await testFlattenRouting(page, files);
      await testNestVisibility(page, files);
      await testUnnestComponents(page, files);
      await testRecorderFirst(page, files);
      await testMulticamSource(page, files);
      await testSubclips(page, files);
      await testBinsAndMetadata(page, files);
      assert.deepEqual(errors, [], '1440x900: zero page errors');
    } finally {
      await page.context().close();
    }

    // Responsive verification at 1180x780 and 1024x640
    await testViewportLayout(splice, {width: 1180, height: 780}, files);
    await testViewportLayout(splice, {width: 1024, height: 640}, files);

    console.log('ALL multicam-nested browser tests PASSED across 1440x900, 1180x780, and 1024x640.');
  } finally {
    await splice.stop();
  }
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
