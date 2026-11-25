/* Feature-length timelines: zoom range, virtualized rendering, per-edit cost and undo memory. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const clipCount = 2000, clipLength = 5.4; // 2,000 shots, three hours

// A three-hour, 2,000-clip sequence; optionally with a 40 MB Premiere original and five 33-point LUTs.
function feature({heavy}) {
  const lut = seed => ({size: 33, title: 'Grade ' + seed, domainMin: [0, 0, 0], domainMax: [1, 1, 1],
    data: Array.from({length: 33 ** 3 * 3}, (_, index) => ((index * 7919 + seed * 104729) % 1000) / 1000)});
  const project = {name: 'Feature', width: 1920, height: 1080, fps: 24, startTimecode: 86400,
    media: [{id: 'ridge', name: 'Ridge', src: 'assets/ridge.mp4', type: 'video', duration: 9}],
    clips: Array.from({length: clipCount}, (_, index) => ({id: 'shot-' + index, mediaId: 'ridge', type: 'video',
      track: 1 + (index % 2), start: index * clipLength, duration: clipLength, in: .5,
      color: {brightness: 100, contrast: 100, saturation: 100}, volume: 100, transition: 'none'}))};
  if (heavy) {
    project.premiereOriginal = 'QUJD'.repeat(10 * 1024 * 1024); // 40 MB of base64
    for (let seed = 0; seed < 5; seed++) project.clips[seed * 300].color.lut = lut(seed);
  }
  return project;
}
async function open(page, project) {
  await page.evaluate(async text => {
    await SpliceSession.newProject({name: 'Scratch', width: 1920, height: 1080, fps: 24});
    await Editor.openProject(new File([text], 'feature.splice.json'));
  }, JSON.stringify(project));
  await page.waitForFunction(() => Editor.get().project.clips.length === 2000);
}

async function zoomRange(page) {
  await open(page, feature({heavy: false}));
  const fit = await page.evaluate(() => {
    const started = performance.now();
    document.getElementById('zoom-fit').click();
    const elapsed = performance.now() - started;
    const scroll = document.getElementById('track-scroll');
    const ticks = [...document.querySelectorAll('.ruler-tick')].map(tick => tick.textContent);
    return {elapsed, surface: document.getElementById('timeline-surface').offsetWidth, viewport: scroll.clientWidth,
      nodes: document.querySelectorAll('.timeline-clip').length, density: document.querySelectorAll('.lane-density').length,
      label: document.getElementById('zoom-label').textContent, first: ticks[0], last: ticks.at(-1)};
  });
  // The whole feature fits in the viewport as painted lanes rather than thousands of nodes.
  assert.ok(fit.surface <= fit.viewport * 1.3, JSON.stringify(fit));
  assert.ok(fit.nodes <= 2 && fit.density >= 2, JSON.stringify(fit));
  assert.equal(fit.first, '01:00:00:00');
  assert.ok(fit.last >= '03:30:00:00', JSON.stringify(fit));
  assert.ok(fit.elapsed < 150, JSON.stringify(fit));
  if (evidence) await page.screenshot({path: path.join(evidence, 'feature-timeline-fit.png')});
  // Zoom in around the playhead down to frames: the playhead keeps its screen position.
  await page.evaluate(() => Editor.seek(5400));
  const anchor = await page.evaluate(() => {
    const scroll = document.getElementById('track-scroll');
    return document.getElementById('playhead').getBoundingClientRect().left - scroll.getBoundingClientRect().left;
  });
  let frames;
  for (let step = 0; step < 40; step++) {
    await page.keyboard.press('Equal');
    frames = await page.evaluate(() => {
      const scroll = document.getElementById('track-scroll');
      const head = document.getElementById('playhead').getBoundingClientRect().left - scroll.getBoundingClientRect().left;
      const [a, b] = [...document.querySelectorAll('.ruler-tick')].map(tick => parseFloat(tick.style.left));
      return {head, nodes: document.querySelectorAll('.timeline-clip').length, tickGap: b - a,
        label: document.getElementById('zoom-label').textContent, title: document.getElementById('zoom-label').title};
    });
    assert.ok(Math.abs(frames.head - anchor) < 2, JSON.stringify({anchor, frames}));
  }
  const pixelsPerFrame = parseFloat(frames.title.split('·')[1]);
  assert.ok(pixelsPerFrame >= 8, JSON.stringify(frames));
  assert.ok(frames.nodes <= 4, JSON.stringify(frames));
  // At the closest zoom the ruler labels individual frames.
  const labels = await page.evaluate(() => [...document.querySelectorAll('.ruler-tick')].slice(0, 2).map(tick => tick.textContent));
  assert.notEqual(labels[0].slice(-2), labels[1].slice(-2));
  // A middle zoom renders a bounded window of real clip nodes.
  for (let step = 0; step < 12; step++) await page.keyboard.press('Minus');
  const middle = await page.evaluate(() => document.querySelectorAll('.timeline-clip').length);
  assert.ok(middle > 4 && middle < 120, String(middle));
  console.log('Zoom: fit in ' + fit.elapsed.toFixed(1) + ' ms (' + fit.label + ' visible, ' + fit.nodes + ' clip nodes), ' +
    pixelsPerFrame + ' px per frame at the closest zoom, ' + middle + ' nodes in between.');
}

async function editCost(page) {
  await open(page, feature({heavy: true}));
  await page.waitForFunction(() => document.getElementById('session-status').textContent !== 'Saving draft…', null, {timeout: 60000});
  const cdp = await page.context().newCDPSession(page);
  const heap = async () => {
    await page.waitForFunction(() => document.getElementById('session-status').textContent !== 'Saving draft…', null, {timeout: 60000});
    await cdp.send('HeapProfiler.collectGarbage');
    await cdp.send('HeapProfiler.collectGarbage');
    return (await cdp.send('Runtime.getHeapUsage')).usedSize / 1048576;
  };
  // Simple edits through the public editor, each timed including every subscriber (timeline, inspector,
  // recovery). The recovery draft's own structuredClone is timed separately: it belongs to recovery.js.
  const run = count => page.evaluate(async total => {
    const times = [], drafts = [];
    const clone = window.structuredClone;
    let drafting = 0;
    window.structuredClone = value => {
      const started = performance.now();
      try { return clone(value); } finally { drafting += performance.now() - started; }
    };
    try {
      for (let index = 0; index < total; index++) {
        const id = 'shot-' + (index * 37 % 2000);
        drafting = 0;
        const started = performance.now();
        Editor.updateClip(id, {volume: 40 + (index % 50)});
        times.push(performance.now() - started);
        drafts.push(drafting);
        await new Promise(resolve => requestAnimationFrame(resolve));
      }
    } finally { window.structuredClone = clone; }
    return {times, drafts};
  }, count);
  await run(5);
  const before = await heap();
  const {times, drafts} = await run(40);
  const after = await heap();
  const middle = values => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  const editing = times.map((time, index) => time - drafts[index]);
  const median = middle(editing), p90 = [...editing].sort((a, b) => a - b)[Math.floor(editing.length * .9)];
  // Undo walks back through shared states and restores the heavy payloads intact.
  const undo = await page.evaluate(() => {
    const started = performance.now();
    for (let index = 0; index < 45; index++) Editor.undo();
    const elapsed = performance.now() - started;
    const project = Editor.get().project;
    return {elapsed, original: project.premiereOriginal.length, luts: project.clips.filter(c => c.color?.lut).length,
      volume: project.clips.find(c => c.id === 'shot-0').volume};
  });
  const report = {median: +median.toFixed(2), p90: +p90.toFixed(2), withRecoveryDraftMedian: +middle(times).toFixed(2),
    recoveryDraftMedian: +middle(drafts).toFixed(2), heapBeforeMB: +before.toFixed(1),
    heapAfterMB: +after.toFixed(1), growthMB: +(after - before).toFixed(1), undo45ms: +undo.elapsed.toFixed(1)};
  assert.ok(median < 16, JSON.stringify(report));
  assert.ok(after - before < 40, JSON.stringify(report));
  assert.deepEqual([undo.original, undo.luts, undo.volume], [40 * 1024 * 1024, 5, 100]);
  console.log('Per-edit cost on 2,000 clips with a 40 MB original and five LUTs: ' + JSON.stringify(report));
}

async function main() {
  const splice = await startSplice('splice-scale-');
  try {
    if (evidence) fs.mkdirSync(evidence, {recursive: true});
    const {page, errors} = await openPage(splice, {width: 1440, height: 900});
    // SPLICE_ONLY=zoom or SPLICE_ONLY=cost runs one measurement.
    const only = process.env.SPLICE_ONLY;
    if (!only || only === 'zoom') await zoomRange(page);
    if (!only || only === 'cost') await editCost(page);
    assert.deepEqual(errors, []);
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
