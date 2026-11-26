/* Media cache across two Splice servers sharing one data folder (the desktop
   app and `python3 server.py`): the bulk action deletes only unused proxies,
   never an imported copy another window's project uses; imported copies go
   only when selected and confirmed. Also the desktop launcher's local
   redirect file and a foreign cookie on 127.0.0.1. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');
const {startSplice} = require('./splice-server.cjs');

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-cache-flow-'));
  const env = {HOME: temp, XDG_DATA_HOME: temp, SPLICE_DATA_DIR: path.join(temp, 'data')};
  const servers = [];
  let browser;
  try {
    const desktop = await startSplice({root, env});
    servers.push(desktop);
    const other = await startSplice({root, env});
    servers.push(other);
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome',
      chromium.executablePath()].find(p => p && fs.existsSync(p));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args: ['--no-sandbox']});

    // Window A opens through the desktop launcher's local redirect file, so
    // the launch link never appears on the browser command line.
    const launcher = execFileSync('python3', ['-c',
      'import sys, pathlib, splice_app; print(splice_app.write_launcher(pathlib.Path(sys.argv[1]), sys.argv[2]).as_uri())',
      temp, desktop.launchUrl], {cwd: root, encoding: 'utf8'}).trim();
    assert.ok(launcher.startsWith('file://') && !launcher.includes('launch='));
    const contextA = await browser.newContext();
    // Another local app on 127.0.0.1 left a JSON-valued cookie (shared by every port).
    await contextA.addCookies([{name: 'prefs', value: '{"theme":"dark"}', domain: '127.0.0.1', path: '/'}]);
    const a = await contextA.newPage();
    a.setDefaultTimeout(60000);
    a.on('dialog', d => d.accept());
    await a.goto(launcher);
    await a.waitForURL(desktop.url + '/');
    await a.evaluate(() => SpliceSession.ready);
    fs.rmSync(new URL(launcher));  // splice_app.py deletes it when the link is used (Python test).
    const src = await a.evaluate(async () => {
      const blob = await (await fetch('/assets/ridge.mp4')).blob();
      await Editor.addMedia([new File([blob], 'interview.mp4', {type: 'video/mp4'})]);
      return Editor.get().project.media.find(m => m.name === 'interview.mp4').originalSrc;
    });
    await a.waitForFunction(() => document.querySelector('#session-status').textContent === 'Draft saved locally');
    assert.ok((await a.evaluate(() => SpliceSession.mediaReferences())).includes(src));
    const media = path.join(env.SPLICE_DATA_DIR, 'media');
    const imported = path.join(media, src.split('/')[2]);
    // An old unused proxy, and the 10-minute guard passed for both.
    const proxy = path.join(media, 'c'.repeat(32));
    fs.mkdirSync(proxy);
    fs.writeFileSync(path.join(proxy, 'edit.mp4'), Buffer.alloc(2048));
    fs.writeFileSync(path.join(proxy, '.splice-derived'), '');
    const old = Date.now() / 1000 - 3600;
    for (const dir of [imported, proxy]) fs.utimesSync(dir, old, old);

    // Window B (the other server, its own browser storage) cleans up.
    const b = await (await browser.newContext()).newPage();
    b.setDefaultTimeout(60000);
    const prompts = [];
    b.on('dialog', d => { prompts.push(d.message()); d.accept(); });
    await other.open(b);
    await b.locator('.file-menu summary').click();
    await b.locator('#media-cache').click();
    const row = b.locator('#cache-import-list tr.cache-import', {hasText: 'interview.mp4'});
    await row.waitFor();
    assert.match(await row.innerText(), /Not used by a project here/);
    await b.locator('#cache-prune:not([disabled])').click();
    await b.locator('#cache-message').filter({hasText: 'Deleted'}).waitFor();
    assert.match(await b.locator('#cache-message').textContent(), /Deleted 1 item\(s\).*Imported copies were kept/);
    assert.equal(fs.existsSync(proxy), false, 'the unused proxy is deleted');
    assert.equal(fs.existsSync(imported), true, 'the imported copy is kept');
    assert.equal(await a.evaluate(async s => (await fetch(s, {method: 'HEAD'})).status, src), 200);
    assert.deepEqual(prompts, [], 'deleting proxies needs no confirmation');

    // Deleting the import needs an explicit selection and confirmation.
    await row.locator('input.cache-select').check();
    await b.locator('#cache-delete:not([disabled])').click();
    await row.waitFor({state: 'detached'});
    assert.match(await b.locator('#cache-message').textContent(), /^Deleted 1 item\(s\), freed 1\.\d MB\.$/);
    assert.equal(prompts.length, 1);
    assert.match(prompts[0], /cannot be rebuilt/);
    assert.match(prompts[0], /other Splice windows, the desktop app or saved project files/);
    assert.equal(fs.existsSync(imported), false);
    assert.equal(await b.locator('#cache-import-list tr.cache-import', {hasText: 'interview.mp4'}).count(), 0, await b.locator('.cache-body').innerText());
    await b.locator('dialog.splice-cache button', {hasText: 'Close'}).click();
    console.log('Media cache kept another window\'s imported copy, deleted only the unused proxy, and deleted the copy once selected and confirmed.');
  } finally {
    await browser?.close();
    for (const server of servers) await server.stop();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
