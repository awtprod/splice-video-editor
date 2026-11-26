/* Docked panels at the supported window sizes: every combination of the source monitor, the
 * large scope dock and the inspector at 1440 × 900, 1180 × 780 and 1024 × 640. Each control in
 * a panel must sit inside its panel and the window and be the element a click there reaches;
 * the program monitor keeps a real minimum size. When the source monitor and the docked scope
 * cannot share the window, the scope dock closes and says why.
 * SPLICE_EVIDENCE_DIR saves a screenshot of every combination. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage} = require('./browser-harness.cjs');

const evidence = process.env.SPLICE_EVIDENCE_DIR;
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const camera = {id: 'cam', name: 'Interview_CamA_take3.mp4', src: 'assets/ridge.mp4', type: 'video', duration: 9, audioChannels: 2};
const project = {name: 'Panels', width: 1280, height: 720, fps: 25, media: [camera],
  clips: [{id: 'v', mediaId: 'cam', type: 'video', track: 1, start: 0, duration: 6, in: 0, color: {}, volume: 100, transition: 'none', title: ''}]};
// The program monitor's picture never gets smaller than this (16:9, at the 1024 × 640 minimum).
const MIN_PROGRAM = {width: 300, height: 150};

// Every control of the visible panels, checked in place: inside its panel, inside the window and
// hit by elementFromPoint at its centre. Controls in a panel's scrolling body are scrolled into
// view first (that is how a user reaches them).
function measure() {
  const panels = [...document.querySelectorAll('.workspace > .panel, .workspace > .source-dialog')]
    .filter(panel => getComputedStyle(panel).display !== 'none');
  const problems = [];
  const inside = (box, outer) => box.left >= outer.left - 1 && box.right <= outer.right + 1 && box.top >= outer.top - 1 && box.bottom <= outer.bottom + 1;
  for (const panel of panels) {
    const name = panel.classList.contains('source-dialog') ? 'source' : panel.classList.contains('scope-dock') ? 'scope' :
      panel.classList.contains('viewer') ? 'program' : panel.classList.contains('inspector') ? 'inspector' : 'library';
    for (const control of panel.querySelectorAll('button, select, input:not([type=hidden])')) {
      if (!control.getClientRects().length || getComputedStyle(control).visibility === 'hidden') continue;
      const scroller = control.closest('.library-body, .inspector-content, .source-controls');
      if (scroller && panel.contains(scroller)) control.scrollIntoView({block: 'nearest', inline: 'nearest'});
      const box = control.getBoundingClientRect(), outer = panel.getBoundingClientRect();
      const label = name + ' ' + (control.title || control.getAttribute('aria-label') || control.textContent.trim() || control.className).slice(0, 40);
      if (!inside(box, outer)) { problems.push(label + ' outside its panel'); continue; }
      if (box.left < 0 || box.top < 0 || box.right > innerWidth + 1 || box.bottom > innerHeight + 1) { problems.push(label + ' outside the window'); continue; }
      const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      if (!hit || !(hit === control || control.contains(hit) || hit.closest('label')?.contains(control))) problems.push(label + ' covered by ' + (hit?.className || hit?.tagName));
    }
  }
  const canvas = document.getElementById('preview-canvas').getBoundingClientRect();
  return {problems, canvas: {width: Math.round(canvas.width), height: Math.round(canvas.height)},
    panels: panels.map(panel => panel.className.split(' ')[0]), scope: !!document.querySelector('.scope-dock'),
    source: !!document.querySelector('.source-dialog[open]'), status: document.querySelector('.toast')?.textContent || '',
    page: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    scopeBox: (box => box && [Math.round(box.width), Math.round(box.height)])(document.querySelector('.scope-dock .splice-scope')?.getBoundingClientRect()),
    inspector: getComputedStyle(document.querySelector('.inspector')).display !== 'none'};
}

async function openCombination(page, {source, scope}) {
  await page.evaluate(async text => { await Editor.openProject(new File([text], 'panels.splice.json')); }, JSON.stringify(project));
  if (await page.locator('.scope-dock').count()) await page.locator('.scope-dock-close').click();
  if (await page.locator('.source-dialog[open]').count()) await page.locator('.source-close').click();
  if (scope) await page.locator('#scope-dock-toggle').click();
  if (source) {
    await page.evaluate(() => SpliceSource.open('cam'));
    await page.waitForFunction(() => document.querySelector('.source-stage video')?.readyState >= 1);
  }
  await settle(page);
  await page.waitForTimeout(150);
}

async function main() {
  const splice = await startSplice('splice-panels-');
  try {
    for (const viewport of [{width: 1440, height: 900}, {width: 1180, height: 780}, {width: 1024, height: 640}]) {
      const {page, errors} = await openPage(splice, viewport);
      const size = viewport.width + 'x' + viewport.height;
      for (const combination of [{}, {source: true}, {scope: true}, {source: true, scope: true}, {source: true, scopeAfter: true}]) {
        await openCombination(page, combination);
        if (combination.scopeAfter) {
          await page.locator('#scope-dock-toggle').click();
          await settle(page);
          await page.waitForTimeout(150);
        }
        const layout = await page.evaluate(measure);
        const name = [combination.source ? 'source' : '', combination.scope || combination.scopeAfter ? 'scope' : '',
          layout.inspector ? 'inspector' : ''].filter(Boolean).join('+') || 'program';
        const detail = size + ' ' + name + ' ' + JSON.stringify(layout);
        assert.deepEqual(layout.problems, [], detail);
        assert.ok(layout.canvas.width >= MIN_PROGRAM.width && layout.canvas.height >= MIN_PROGRAM.height, 'program monitor ' + detail);
        assert.ok(layout.page[0] <= viewport.width && layout.page[1] <= viewport.height, 'page overflow ' + detail);
        const both = combination.source && (combination.scope || combination.scopeAfter);
        // Both fit from 1100 px wide; below that the dock gives way, with a reason.
        if (both) assert.equal(layout.scope, viewport.width >= 1100, detail);
        if (both && !layout.scope) assert.match(layout.status, /scope/i, 'the scope dock says why it is closed: ' + detail);
        if (combination.scope && !combination.source) assert.ok(layout.scope, detail);
        if (layout.scope) assert.ok(layout.scopeBox[0] >= 300 && layout.scopeBox[1] >= 150, 'docked scope size ' + detail);
        if (combination.source) assert.ok(layout.source, detail);
        // The source monitor's edit buttons work where they are.
        if (layout.source) {
          const before = await page.evaluate(() => Editor.get().project.clips.length);
          await page.locator('.source-actions [data-action=append]').click({timeout: 3000});
          assert.ok(await page.evaluate(() => Editor.get().project.clips.length) > before, 'Append from the source panel ' + detail);
        }
        if (evidence) {
          fs.mkdirSync(evidence, {recursive: true});
          await page.screenshot({path: path.join(evidence, `panels-${size}-${name}${combination.scopeAfter ? '-scope-opened-last' : ''}.png`)});
        }
        console.log(`${size} ${name}${combination.scopeAfter ? ' (scope opened last)' : ''}: program ${layout.canvas.width} × ${layout.canvas.height}, scope ${layout.scope ? 'docked' : 'closed'}, controls reachable.`);
      }
      // A window shrunk below the room both need closes the docked scope, with a reason.
      if (viewport.width >= 1180) {
        await openCombination(page, {source: true, scope: true});
        if (await page.locator('.scope-dock').count()) {
          await page.setViewportSize({width: 1024, height: 640});
          await settle(page);
          await page.waitForTimeout(150);
          const shrunk = await page.evaluate(measure);
          assert.deepEqual(shrunk.problems, [], 'after shrinking ' + JSON.stringify(shrunk));
          assert.equal(shrunk.scope, false, 'shrinking the window closes the docked scope');
          assert.match(shrunk.status, /scope/i);
          await page.setViewportSize(viewport);
        }
      }
      assert.deepEqual(errors, []);
      await page.context().close();
    }
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
