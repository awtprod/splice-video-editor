const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, makeCamera} = require('./browser-harness.cjs');

async function main() {
  const splice = await startSplice('splice-color-controls-');
  try {
    const {page, errors} = await openPage(splice);
    const camera = path.join(splice.temp, 'patches.mp4');
    makeCamera(camera);
    await page.locator('#media-input').setInputFiles(camera);
    await page.waitForFunction(() => Editor.get().project.media.some(m => m.name === 'patches.mp4'));
    await page.evaluate(() => {
      Editor.editProject(p => {
        const media = p.media.find(m => m.name === 'patches.mp4');
        p.clips = [0, 1].map(i => ({id:'test-'+i, mediaId:media.id, type:'video', track:1,
          start:i, duration:1, in:0, color:{}}));
      });
      Editor.select('test-0');
    });
    await page.locator('[data-workspace="color"]').click();
    const action = name => page.locator(`[data-grade-action="${name}"]`).click();
    const color = i => page.evaluate(i => Editor.get().project.clips[i].color, i);
    for (const [width, height] of [[1440, 900], [1180, 780]]) {
      await page.setViewportSize({width, height});
      const gain = page.locator('[data-wheel-number="gain"][data-channel="0"]');
      await gain.fill('1.25');
      await gain.press('Tab');
      assert.equal((await color(0)).grade.gain[0], 1.25);
      const originalKeys = await page.evaluate(() => {
        const keyframes = {opacity:[{time:0, value:75}]};
        for (const [key, [, high]] of Object.entries(SpliceGrade.animated)) keyframes[key] = [{time:0, value:high}];
        Editor.updateClip('test-0', {keyframes});
        return keyframes;
      });
      const keys = () => page.evaluate(() => Editor.get().project.clips[0].keyframes);
      await action('copy');
      await action('reset');
      assert.equal((await color(0)).grade, undefined);
      assert.deepEqual(await keys(), {opacity:originalKeys.opacity});
      await page.evaluate(() => Editor.undo());
      assert.deepEqual(await keys(), originalKeys);
      assert.equal((await color(0)).grade.gain[0], 1.25);
      await action('reset');
      await action('paste');
      assert.equal((await color(0)).grade.gain[0], 1.25);
      await action('source');
      assert.deepEqual(await color(1), await color(0));
      await page.locator('[data-grade-bypass]').check();
      assert.equal((await color(0)).grade.bypass, true);
      await page.locator('[data-grade-bypass]').uncheck();
      const pending = page.waitForEvent('download');
      await action('cube');
      const downloaded = await pending;
      assert.match(fs.readFileSync(await downloaded.path(), 'utf8'), /LUT_3D_SIZE 33/);
      await page.locator('[data-monitor-quality]').selectOption('half');
      assert.equal(await page.evaluate(() => SpliceMonitor.quality()), 'half');
      for (const view of await page.evaluate(() => SpliceMonitor.views)) {
        await page.locator('[data-monitor-view]').selectOption(view);
        assert.equal(await page.evaluate(() => Editor.monitor.view), view);
      }
      await page.locator('[data-monitor-view]').selectOption('normal');
      await page.locator('[data-monitor-guides]').selectOption('1.85');
      assert.equal(await page.locator('.monitor-guides').count(), 1);
      await page.locator('[data-monitor-bypass]').click();
      assert.equal(await page.evaluate(() => Editor.monitor.bypass), true);
      await page.locator('[data-monitor-bypass]').click();
      await page.locator('[data-monitor-split]').click();
      assert.equal(await page.evaluate(() => Editor.monitor.split), .5);
      await page.locator('[data-monitor-split]').click();
      await gain.scrollIntoViewIfNeeded();
      if (process.env.SPLICE_PROOF_DIR)
        await page.screenshot({path:path.join(process.env.SPLICE_PROOF_DIR, 'controls-'+width+'.png')});
      await action('reset');
    }
    // Locking a track disables editable Color controls while keeping read-only Copy and Export .cube usable.
    await page.evaluate(() => { Editor.setTrackState(1, 'video', {locked: true}); });
    await page.waitForFunction(() => document.querySelector('[data-grade-bypass]')?.disabled === true);
    assert.equal(await page.locator('[data-wheel-number="gain"][data-channel="0"]').isDisabled(), true);
    assert.equal(await page.locator('[data-grade-action="reset"]').isDisabled(), true);
    assert.equal(await page.locator('[data-grade-action="copy"]').isDisabled(), false);
    assert.equal(await page.locator('[data-grade-action="cube"]').isDisabled(), false);
    await page.locator('[data-wheel="gain"] .grade-wheel-pad').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal((await color(0)).grade, undefined, 'locked wheel pad ignores keyboard edits');
    await page.evaluate(() => { Editor.setTrackState(1, 'video', {locked: false}); });
    // Adjustment clips hide source-file controls (untagged matrix and input LUT).
    await page.locator('.edit-menu > summary').click();
    await page.locator('#new-adjustment-clip').click();
    await page.waitForFunction(() => Editor.get().selected && Editor.get().project.media.find(m => m.id === Editor.get().selected.mediaId)?.adjustment);
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    assert.equal(await page.locator('.grade-source').count(), 0, 'adjustment clip hides Source matrix/input LUT UI');
    assert.deepEqual(errors, []);
    console.log('Grade actions, cube download, monitor controls, locked-track and adjustment UI passed.');
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
