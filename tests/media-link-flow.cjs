/* Link media in place through the in-app picker: no copy, playback, reopen,
   offline after a move, relink through the picker, and the media cache view. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {startSplice} = require('./splice-server.cjs');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

const digest = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
function files(root) {
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root, {withFileTypes: true, recursive: true}).filter(e => e.isFile())
    .map(e => path.join(e.parentPath ?? e.path, e.name));
}

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-link-flow-'));
  const data = path.join(temp, 'data'), card = path.join(temp, 'Shoot Day 1', 'CARD_A');
  fs.mkdirSync(card, {recursive: true});
  const original = path.join(card, 'A001.mp4');
  execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=25:d=3', '-f', 'lavfi',
    '-i', 'sine=440:sample_rate=48000:d=3', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
    '-c:a', 'aac', '-movflags', '+faststart', original], {timeout: 60000});
  const originalHash = digest(original);
  const splice = await startSplice({root, env: {HOME: temp, XDG_DATA_HOME: temp, SPLICE_DATA_DIR: data}});
  let browser;
  try {
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome',
      chromium.executablePath()].find(p => p && fs.existsSync(p));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required']});
    const page = await (await browser.newContext({viewport: {width: 1440, height: 960}})).newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('dialog', d => d.accept());
    await splice.open(page);

    // Pick the file through the in-app browser: File → Link media in place.
    const pick = async (folder, button) => {
      await page.locator('.file-menu summary').click();
      await page.locator('#link-media').click();
      await page.locator('#link-location').fill(folder);
      await page.locator('#link-location').press('Enter');
      await page.locator('#link-entries label', {hasText: 'A001.mp4'}).locator('input').check()
        .catch(async error => { throw Error('Picker: ' + await page.locator('#link-message').textContent() + ' / ' + await page.locator('#link-entries').innerText() + ' ' + error.message); });
      await page.locator(button).click();
      await page.locator('dialog.splice-link').waitFor({state: 'detached', timeout: 60000});
    };
    await pick(card, '#link-import');
    const item = await page.evaluate(() => Editor.get().project.media.find(m => m.name === 'A001.mp4'));
    assert.ok(item, 'linked item is in the project');
    assert.equal(item.linked, true);
    assert.equal(item.path, fs.realpathSync(original));
    assert.equal(item.sourcePath, fs.realpathSync(original));
    assert.match(item.originalSrc, /^\/media\/linked\/[a-f0-9]{32}\/A001\.mp4$/);
    assert.equal(item.src, item.originalSrc, 'a browser-native original plays directly');
    const copies = files(path.join(data, 'media')).filter(file => fs.statSync(file).size === fs.statSync(original).size &&
                                                              digest(file) === originalHash);
    assert.deepEqual(copies, [], 'linking must not copy the original');

    // The original plays in a media element (cookie only) and on the timeline.
    const playable = await page.evaluate(src => new Promise(resolve => {
      const video = document.createElement('video');
      video.muted = true; video.src = src;
      video.onloadeddata = () => resolve({ok: true, width: video.videoWidth, duration: video.duration});
      video.onerror = () => resolve({ok: false});
    }), item.src);
    assert.deepEqual({ok: playable.ok, width: playable.width}, {ok: true, width: 320});
    await page.evaluate(id => Editor.addToTimeline(id), item.id);
    await page.waitForFunction(() => Editor.get().project.clips.some(c => Editor.get().project.media
      .find(m => m.id === c.mediaId)?.name === 'A001.mp4'));

    // Reopen from recovery: still linked and online.
    await page.waitForFunction(() => document.querySelector('#session-status').textContent === 'Draft saved locally');
    await page.reload();
    await page.evaluate(() => SpliceSession.ready);
    const reopened = await page.evaluate(() => Editor.get().project.media.find(m => m.name === 'A001.mp4'));
    assert.equal(reopened.offline, false);
    assert.equal(reopened.originalSrc, item.originalSrc);

    // Move the card: the source goes offline, then relinks through the picker.
    const moved = path.join(temp, 'Archive', 'CARD_A');
    fs.mkdirSync(path.dirname(moved), {recursive: true});
    fs.renameSync(card, moved);
    await page.reload();
    await page.evaluate(() => SpliceSession.ready);
    assert.equal(await page.evaluate(() => Editor.get().project.media.find(m => m.name === 'A001.mp4').offline), true);
    await pick(moved, '#link-relink');
    const relinked = await page.evaluate(() => Editor.get().project.media.find(m => m.name === 'A001.mp4'));
    assert.equal(relinked.offline, false);
    assert.equal(relinked.id, item.id);
    assert.equal(relinked.sourcePath, fs.realpathSync(path.join(moved, 'A001.mp4')));
    assert.notEqual(relinked.originalSrc, item.originalSrc);

    // Media cache view reports linked originals as not copied.
    await page.locator('.file-menu summary').click();
    await page.locator('#media-cache').click();
    await page.locator('dialog.splice-cache dd', {hasText: 'not copied'}).waitFor();
    const proof = process.env.SPLICE_PROOF_DIR;
    if (proof) { fs.mkdirSync(proof, {recursive: true}); await page.screenshot({path: path.join(proof, 'Splice-media-cache.png')}); }
    await page.locator('dialog.splice-cache button', {hasText: 'Close'}).click();
    assert.deepEqual(errors, []);
    console.log('Linked in place without a copy, played, reopened, went offline after a move, relinked through the picker.');
  } finally {
    await browser?.close();
    await splice.stop();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exit(1); });
