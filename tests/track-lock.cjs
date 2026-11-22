/* Browser regression for track locks, sync-lock and linked timeline edits. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-tracks-'));
  const server = spawn('python3', ['-u', 'server.py', '--port', '0'], {
    cwd: root, env: {...process.env, HOME: temp, XDG_DATA_HOME: temp}, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      server.stdout.once('data', data => resolve(data.toString().trim()));
      server.once('error', reject);
      server.once('exit', code => reject(Error('Server exited: ' + code)));
    });
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome',
      '/usr/bin/chromium', chromium.executablePath()].find(file => file && fs.existsSync(file));
    browser = await chromium.launch({executablePath, args: ['--no-sandbox']});
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.evaluate(() => SpliceSession.ready);
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
    await page.locator('[data-action=in]').click();
    await page.locator('[data-action=insert]').click();
    assert.match(await page.locator('.source-status').textContent(), /Unlock video track 2/);
    assert.ok((await page.locator('.source-track option').allTextContents()).includes('Video 4'));
    await page.locator('.source-close').click();
    await lock.click();
    assert.equal(await page.getByRole('button', {name: 'Lock Video 2'}).getAttribute('aria-pressed'), 'false');
    await page.evaluate(() => SpliceSource.open('video'));
    await page.locator('.source-track').selectOption('2');
    await page.locator('[data-action=insert]').click();
    await page.waitForFunction(() => Editor.get().project.clips.length === 4);
    assert.equal(await page.locator('.source-stage').count(), 0);
    await page.getByRole('button', {name: 'Lock Video 2'}).click();
    await page.waitForFunction(() => document.querySelector('#session-status')?.textContent === 'Draft saved locally');
    await page.reload();
    await page.evaluate(() => SpliceSession.ready);
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
          {id: 'head', mediaId: 'video', type: 'video', start: 8, duration: 8, in: 0,
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
        clips.find(c => c.id === 'head').in === 1 && clips.find(c => c.id === 'head').duration === 7 &&
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
    assert.deepEqual(errors, []);
    console.log('Track locks, linked edits, source insert guard and narrow UI passed; screenshot: ' + screenshot);
  } finally {
    await browser?.close();
    if (server.exitCode === null) {
      const stopped = new Promise(resolve => server.once('exit', resolve));
      server.kill('SIGTERM');
      await stopped;
    }
    fs.rmSync(temp, {recursive: true, force: true});
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
