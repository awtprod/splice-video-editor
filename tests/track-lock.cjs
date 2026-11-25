/* Browser regression for track locks, sync-lock and linked timeline edits. */
const assert = require('node:assert/strict');
const {startSplice} = require('./splice-server.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-tracks-'));
  const splice = await startSplice({root, env: {HOME: temp, XDG_DATA_HOME: temp}});
  let browser;
  try {
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome',
      '/usr/bin/chromium', chromium.executablePath()].find(file => file && fs.existsSync(file));
    browser = await chromium.launch({executablePath, args: ['--no-sandbox']});
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await splice.open(page);
    const result = await page.evaluate(() => {
      Editor.newProject({name: 'Track check', width: 1280, height: 720, fps: 25});
      Editor.editProject(project => {
        project.media = [
          {id: 'video', name: 'Ridge', src: 'assets/ridge.mp4', type: 'video', duration: 9},
          {id: 'audio', name: 'Score', src: 'assets/score.wav', type: 'audio', duration: 32},
        ];
        project.clips = [
          {id: 'v', mediaId: 'video', type: 'video', start: 0, duration: 4, in: 0,
            track: 1, linkGroup: 'pair', color: {}, volume: 100, transition: 'none'},
          {id: 'a', mediaId: 'audio', type: 'audio', start: 0, duration: 4, in: 0,
            track: -1, linkGroup: 'pair', color: {}, volume: 100, transition: 'none'},
          {id: 'later', mediaId: 'video', type: 'video', start: 5, duration: 2, in: 0,
            track: 3, color: {}, volume: 100, transition: 'none'},
        ];
      });
      const before = JSON.stringify(Editor.get().project.clips);
      Editor.setTrackState(-1, 'audio', {locked: true});
      let rejectedMove = false, rejectedTransaction = false;
      try { Editor.moveClip('v', 1, 2); } catch (error) { rejectedMove = /Unlock/.test(error.message); }
      try { Editor.editProject(project => {project.clips[0].start = 1;}); }
      catch (error) { rejectedTransaction = /Linked|Unlock/.test(error.message); }
      const unchanged = before === JSON.stringify(Editor.get().project.clips);
      Editor.setTrackState(-1, 'audio', {locked: false});
      Editor.moveClip('v', 1, 2);
      Editor.trimEdge('v', 'end', 3);
      Editor.slipClip('v', .5);
      const pair = Editor.get().project.clips.filter(clip => clip.linkGroup === 'pair');
      const aligned = pair.length === 2 && pair.every(clip =>
        clip.start === 1 && clip.duration === 2 && Math.abs(clip.in - .5) < .03);
      Editor.select('v'); Editor.seek(2); Editor.split();
      const splitTogether = Editor.get().project.clips.filter(clip => clip.linkGroup).length === 4;
      Editor.deleteSelected();
      const deletedTogether = Editor.get().project.clips.filter(clip => clip.linkGroup).length === 2;
      Editor.setTrackState(2, 'video', {locked: true});
      return {rejectedMove, rejectedTransaction, unchanged, aligned, splitTogether, deletedTogether};
    });
    assert.deepEqual(result, {rejectedMove: true, rejectedTransaction: true, unchanged: true,
      aligned: true, splitTogether: true, deletedTogether: true});
    const lock = page.getByRole('button', {name: 'Unlock Video 2'});
    assert.equal(await lock.getAttribute('aria-pressed'), 'true');
    await page.evaluate(() => SpliceSource.open('video'));
    await page.locator('.source-track').selectOption('2');
    await page.locator('.source-scrub').evaluate(input=>{
      input.value=.4;
      input.dispatchEvent(new Event('input'));
    });
    await page.waitForFunction(()=>Math.abs(document.querySelector('.source-stage video').currentTime-.4)<.02);
    await page.locator('[data-action=in]').click();
    await page.locator('[data-action=insert]').click();
    assert.match(await page.locator('.source-status').textContent(), /Unlock video track 2/);
    assert.ok((await page.locator('.source-track option').allTextContents()).includes('Video 4'));
    await page.locator('.source-close').click();
    await lock.click();
    assert.equal(await page.getByRole('button', {name: 'Lock Video 2'}).getAttribute('aria-pressed'), 'false');
    await page.evaluate(() => SpliceSource.open('video'));
    await page.waitForFunction(()=>document.querySelector('.source-range').textContent.startsWith('00:00:00:10'));
    await page.locator('.source-track').selectOption('2');
    await page.locator('[data-action=insert]').click();
    await page.waitForFunction(() => Editor.get().project.clips.length === 4);
    assert.equal(await page.locator('.source-stage').count(), 0);
    await page.getByRole('button', {name: 'Lock Video 2'}).click();
    await page.waitForFunction(() => document.querySelector('#session-status')?.textContent === 'Draft saved locally');
    await page.reload();
    await page.evaluate(() => SpliceSession.ready);
    assert.equal(await page.evaluate(()=>Editor.get().project.media.find(item=>item.id==='video').sourceMarks.in),.4);
    assert.equal(await page.getByRole('button', {name: 'Unlock Video 2'}).getAttribute('aria-pressed'), 'true');
    await page.setViewportSize({width: 1024, height: 760});
    const screenshot = path.join(root, 'artifacts', 'track-controls.png');
    await page.screenshot({path: screenshot});
    await page.evaluate(() => {
      Editor.newProject({name: 'Linked insert', fps: 25});
      Editor.editProject(project => {
        project.media = [
          {id: 'video', name: 'Ridge', src: 'assets/ridge.mp4', type: 'video', duration: 9},
          {id: 'audio', name: 'Score', src: 'assets/score.wav', type: 'audio', duration: 32},
        ];
        project.clips = [
          {id: 'v', mediaId: 'video', type: 'video', start: 0, duration: 4, in: 0,
            track: 1, linkGroup: 'pair', color: {}, volume: 100, transition: 'none'},
          {id: 'a', mediaId: 'audio', type: 'audio', start: 0, duration: 4, in: 0,
            track: -1, linkGroup: 'pair', color: {}, volume: 100, transition: 'none'},
        ];
      });
      Editor.seek(2); SpliceSource.open('video');
    });
    await page.locator('.source-track').selectOption('3');
    await page.locator('[data-action=insert]').click();
    await page.waitForFunction(() => Editor.get().project.clips.length === 5);
    const groups = await page.evaluate(() => {
      const links = Editor.get().project.clips.filter(clip => clip.linkGroup);
      return Object.values(Object.groupBy(links, clip => clip.linkGroup)).map(items => items.length).sort();
    });
    assert.deepEqual(groups, [2, 2]);
    const ripple = await page.evaluate(() => {
      Editor.newProject({name: 'Ripple check', fps: 25});
      Editor.editProject(project => {
        project.media = [
          {id: 'video', name: 'Long source', src: 'assets/ridge.mp4', type: 'video', duration: 30},
          {id: 'audio', name: 'Long sound', src: 'assets/score.wav', type: 'audio', duration: 32},
        ];
        project.clips = [
          {id: 'head', mediaId: 'video', type: 'video', start: 8, duration: 8, in: .5,
            track: 1, color: {}, volume: 100, transition: 'none'},
          {id: 'tail', mediaId: 'video', type: 'video', start: 16, duration: 4, in: 0,
            track: 1, color: {}, volume: 100, transition: 'none'},
          {id: 'sound', mediaId: 'audio', type: 'audio', start: 16, duration: 4, in: 0,
            track: -1, color: {}, volume: 100, transition: 'none'},
        ];
      });
      Editor.trimEdge('head', 'start', 9, {ripple: true});
      let clips = Editor.get().project.clips;
      const trimmed = clips.find(c => c.id === 'head').start === 8 &&
        clips.find(c => c.id === 'head').in === 1.5 && clips.find(c => c.id === 'head').duration === 7 &&
        clips.find(c => c.id === 'tail').start === 15 && clips.find(c => c.id === 'sound').start === 15;
      Editor.deleteClips(['head'], {ripple: true});
      clips = Editor.get().project.clips;
      const deleted = clips.find(c => c.id === 'tail').start === 8 && clips.find(c => c.id === 'sound').start === 8;
      Editor.editProject(project => project.clips.push({id: 'blocker', mediaId: 'video', type: 'video',
        start: 12, duration: 3, in: 0, track: 1, color: {}, volume: 100, transition: 'none'}));
      const before = JSON.stringify(Editor.get().project.clips);
      let rejectedMove = false;
      try {Editor.moveClip('tail', 9, 1);} catch (error) {rejectedMove = /already has a clip/.test(error.message);}
      return {trimmed, deleted, rejectedMove, unchanged: before === JSON.stringify(Editor.get().project.clips)};
    });
    assert.deepEqual(ripple, {trimmed: true, deleted: true, rejectedMove: true, unchanged: true});
    const rolled = await page.evaluate(() => {
      Editor.editProject(project => {
        project.clips = [];
        for (const [group, start] of [['left', 0], ['right', 3]]) {
          for (const [type, track] of [['video', 1], ['audio', -1]]) {
            project.clips.push({id: `${group}-${type}`, mediaId: type, type, track,
              linkGroup: group, start, duration: 3, in: .5});
          }
        }
      });
      Editor.rollEdit('left-video', 'right-video', 4);
      return Editor.get().project.clips.map(({start,duration,in:sourceIn}) => [start,duration,sourceIn]);
    });
    assert.deepEqual(rolled, [[0,4,.5],[0,4,.5],[4,2,1.5],[4,2,1.5]]);
    assert.deepEqual(errors, []);
    console.log('Track locks, linked edits, source insert guard and narrow UI passed; screenshot: ' + screenshot);
  } finally {
    await browser?.close();
    await splice.stop();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
