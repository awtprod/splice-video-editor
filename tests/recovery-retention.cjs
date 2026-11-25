/* Recovery retention (recovery.js prune) and the media sources a browser's
   projects use (mediaReferences). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');
const {startSplice} = require('./splice-server.cjs');

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-retention-'));
  const splice = await startSplice({root, env: {HOME: temp, XDG_DATA_HOME: temp, SPLICE_DATA_DIR: path.join(temp, 'data')}});
  let browser;
  try {
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome',
      chromium.executablePath()].find(p => p && fs.existsSync(p));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args: ['--no-sandbox']});
    const page = await (await browser.newContext()).newPage();
    page.setDefaultTimeout(30000);
    await splice.open(page);
    await page.evaluate(() => Editor.renameProject('Open project'));
    await page.waitForFunction(() => document.querySelector('#session-status').textContent === 'Draft saved locally');

    const seed = () => page.evaluate(async () => {
      const db = await new Promise((ok, fail) => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); r.onerror = () => fail(r.error); });
      const hour = 3600e3, day = 24 * hour, now = Date.now();
      const tx = db.transaction(['sessions', 'versions', 'originals', 'handles'], 'readwrite');
      const media = (id, src) => ({project: {media: [{id, src, originalSrc: src}]}});
      // 210 projects edited in the last 9 days, 40 edited 40-79 days ago, one with no date.
      for (let i = 0; i < 210; i++)
        tx.objectStore('sessions').put({id: 'recent' + i, name: 'Recent ' + i, stamp: now - (i + 1) * hour,
          data: media('m', `/media/${'a'.repeat(31)}${i % 10}/r${i}.mp4`), originalKey: i === 205 ? 'origRecent205' : undefined});
      for (let i = 0; i < 40; i++)
        tx.objectStore('sessions').put({id: 'old' + i, name: 'Old ' + i, stamp: now - (40 + i) * day,
          data: media('m', i === 3 ? `/media/linked/${'b'.repeat(32)}/old3.mov` : `/media/${'c'.repeat(32)}/old${i}.mp4`),
          originalKey: i === 0 ? 'origOld0' : undefined});
      tx.objectStore('sessions').put({id: 'noStamp', name: 'No date', data: {}});
      tx.objectStore('handles').put({}, 'old5');
      tx.objectStore('versions').put({id: 'v-old5', session: 'old5', stamp: now, originalKey: 'origV5', data: media('m', `/media/${'d'.repeat(32)}/v.mp4`)});
      tx.objectStore('versions').put({id: 'v-recent3', session: 'recent3', stamp: now, originalKey: 'origV3', data: media('m', `/media/linked/${'e'.repeat(32)}/v3.mov`)});
      for (const key of ['origRecent205', 'origOld0', 'origV5', 'origV3', 'orphan']) tx.objectStore('originals').put(new Uint8Array(4), key);
      await new Promise((ok, fail) => { tx.oncomplete = ok; tx.onerror = () => fail(tx.error); });
    });
    const read = () => page.evaluate(async () => {
      const db = await new Promise(ok => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); });
      const keys = store => new Promise(ok => { const r = db.transaction(store).objectStore(store).getAllKeys(); r.onsuccess = () => ok(r.result); });
      return {sessions: await keys('sessions'), versions: await keys('versions'), originals: await keys('originals'), handles: await keys('handles')};
    });

    // While "Recent projects & recovery" is open (in this or another tab),
    // pruning is held off, so nothing it lists can disappear.
    if (!await page.locator('#versions').isVisible()) await page.locator('.file-menu summary').click();
    await page.locator('#versions').click();
    const list = page.locator('.splice-session-dialog');
    await list.locator('button', {hasText: 'Open project ·'}).waitFor();
    await seed();
    assert.equal(await page.evaluate(() => SpliceSession.prune()), 0);
    assert.equal(await page.evaluate(() => new Promise(ok => {
      // Another tab of the same browser is held off too.
      navigator.locks.request('splice-recovery-list', {ifAvailable: true}, lock => ok(lock === null));
    })), true);
    assert.ok((await read()).sessions.includes('old39'));
    await list.locator('button', {hasText: 'Cancel'}).click();
    await list.waitFor({state: 'detached'});

    const open = await page.evaluate(async () => {
      const db = await new Promise(ok => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); });
      return new Promise(ok => { const r = db.transaction('metadata').objectStore('metadata').get('lastSession'); r.onsuccess = () => ok(r.result); });
    });
    assert.match(String(open), /\w{8}/);
    const removed = await page.evaluate(() => SpliceSession.prune());
    const after = await read();
    const recent = after.sessions.filter(k => k.startsWith('recent'));
    const old = after.sessions.filter(k => k.startsWith('old'));
    // All 210 recent projects are within 30 days: the 200 newest stay, the
    // open project (edited just now) among them. The 40 old ones are outside
    // 30 days and beyond the 30 most recent.
    assert.equal(recent.length, 199);
    assert.ok(recent.includes('recent0') && recent.includes('recent198') && !recent.includes('recent199'));
    assert.deepEqual(old, []);
    assert.equal(after.sessions.includes('noStamp'), false);
    const others = after.sessions.filter(k => !k.startsWith('recent'));
    assert.equal(others.length, 1, 'only the open project besides the recent ones: ' + others);
    assert.equal(others[0], open, 'the open project (its last session) is kept');
    // Removed projects take their versions, file handles and stored originals with them.
    assert.deepEqual(after.versions.filter(k => k.startsWith('v-')), ['v-recent3']);
    assert.deepEqual(after.handles, []);
    assert.deepEqual(after.originals.sort(), ['origV3']);
    assert.equal(removed, 11 + 40 + 1 + 4, 'sessions plus stored originals removed');

    // With fewer projects, the 30 most recent stay whatever their age.
    await page.evaluate(async () => {
      const db = await new Promise(ok => { const r = indexedDB.open('splice-sessions'); r.onsuccess = () => ok(r.result); });
      const tx = db.transaction('sessions', 'readwrite');
      for (let i = 0; i < 200; i++) tx.objectStore('sessions').delete('recent' + i);
      const now = Date.now();
      for (let i = 0; i < 35; i++) tx.objectStore('sessions').put({id: 'aged' + i, name: 'Aged ' + i, stamp: now - (60 + i) * 864e5, data: {}});
      await new Promise(ok => { tx.oncomplete = ok; });
    });
    await page.evaluate(() => SpliceSession.prune());
    const aged = (await read()).sessions.filter(k => k.startsWith('aged'));
    assert.equal(aged.length, 29, 'the open project is one of the 30 most recent');
    assert.ok(aged.includes('aged0') && aged.includes('aged28') && !aged.includes('aged29'));

    // mediaReferences lists managed and linked sources of what is kept.
    await seed();
    const references = await page.evaluate(() => SpliceSession.mediaReferences());
    assert.ok(references.includes(`/media/${'a'.repeat(31)}0/r0.mp4`));
    assert.ok(references.includes(`/media/linked/${'b'.repeat(32)}/old3.mov`));
    assert.ok(references.includes(`/media/linked/${'e'.repeat(32)}/v3.mov`));
    await page.evaluate(() => SpliceSession.prune());
    const kept = await page.evaluate(() => SpliceSession.mediaReferences());
    assert.equal(kept.includes(`/media/linked/${'b'.repeat(32)}/old3.mov`), false, 'a removed project no longer counts');
    assert.equal(kept.includes(`/media/${'d'.repeat(32)}/v.mp4`), false, 'nor its versions');
    assert.ok(kept.includes(`/media/linked/${'e'.repeat(32)}/v3.mov`), 'a kept project\'s version counts');
    console.log('Recovery keeps the 30 most recent and up to 200 from the last 30 days plus the open project, removes the rest with their versions and originals, and never while the list is open.');
  } finally {
    await browser?.close();
    await splice.stop();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
