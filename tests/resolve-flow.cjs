/* Open synthetic DaVinci Resolve projects (.drp and disk library) through the shipped server and UI. */
const assert = require('node:assert/strict');
const {spawn, execFileSync} = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

function treeState(folder) {
  const out = {};
  const walk = dir => {
    for (const entry of fs.readdirSync(dir, {withFileTypes:true}).sort((a, b) => a.name.localeCompare(b.name))) {
      const full = path.join(dir, entry.name), rel = path.relative(folder, full);
      if (entry.isDirectory()) { out[rel] = 'dir'; walk(full); }
      else out[rel] = crypto.createHash('sha256').update(fs.readFileSync(full)).digest('hex');
    }
  };
  walk(folder);
  return out;
}

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-resolve-'));
  // Footage on this computer (linked on open) and a Windows path that arrives offline.
  const footage = path.join(temp, 'footage', 'Day 1');
  fs.mkdirSync(footage, {recursive:true});
  // Camera footage with picture and sound (the linked audio clip plays from it).
  execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-threads', '1', '-i', path.join(root, 'assets/ridge.mp4'),
    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=9', '-t', '9', '-vf', 'scale=320:180',
    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
    path.join(footage, 'A001.mp4')]);
  const relink = path.join(temp, 'relink', 'Shoot', 'Day 1');
  fs.mkdirSync(relink, {recursive:true});
  fs.copyFileSync(path.join(root, 'assets/score.wav'), path.join(relink, 'A002.wav'));
  const windowsAudio = 'E:\\Shoot\\Day 1\\A002.wav';
  const drp = path.join(temp, 'Harbor Lights.drp');
  const fixtures = path.join(root, 'tests/resolve_fixtures.py');
  execFileSync('python3', [fixtures, 'drp', drp, path.join(footage, 'A001.mp4'), windowsAudio, '--no-generator']);
  const library = path.join(temp, 'Resolve library');
  execFileSync('python3', [fixtures, 'library', library, path.join(footage, 'A001.mp4'), windowsAudio, '--no-generator']);
  const libraryBefore = treeState(library);

  const server = spawn('python3', ['-u', 'server.py', '--port', '0'], {
    cwd:root, env:{...process.env, HOME:temp, XDG_DATA_HOME:temp}, stdio:['ignore', 'pipe', 'pipe']
  });
  let browser, logs = '';
  server.stderr.on('data', chunk => { logs = (logs + chunk).slice(-4000); });
  try {
    const url = await new Promise((resolve, reject) => {
      server.stdout.once('data', d => resolve(d.toString().trim()));
      server.once('error', reject);
      server.once('exit', code => reject(Error('Server exited ' + code + ' ' + logs)));
    });
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome',
      chromium.executablePath()].find(p => p && fs.existsSync(p));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args:['--no-sandbox', '--autoplay-policy=no-user-gesture-required']});
    const page = await (await browser.newContext({viewport:{width:1440, height:960}})).newPage();
    page.setDefaultTimeout(20000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const dialogs = [];
    page.on('dialog', d => { dialogs.push(d.message()); d.accept(); });
    page.on('response', async r => {
      if (r.status() >= 400 && r.url().includes('/api/') && !r.url().includes('/api/resolve/open'))
        console.error(r.status(), r.url(), (await r.text().catch(() => '')).slice(0, 300));
      if (r.request().method() === 'GET' && /\/api\/exports\/[a-f0-9]{32}$/.test(r.url())) {
        const job = await r.json().catch(() => ({}));
        if (job.state === 'failed') console.error('Render failed:', job.error);
      }
    });
    const click = selector => page.locator(selector).first().click();
    const fileMenu = async id => { if (!await page.locator(id).isVisible()) await click('.file-menu summary'); await click(id); };
    const waitState = (fn, arg) => page.waitForFunction(fn, arg, {timeout:20000});
    await page.goto(url); await page.evaluate(() => SpliceSession.ready);

    // A damaged .drp is refused and the open edit stays as it was.
    const before = await page.evaluate(() => JSON.stringify(Editor.get().project));
    await page.locator('#resolve-input').setInputFiles({name:'Broken.drp', mimeType:'application/octet-stream', buffer:Buffer.from('PK\x03\x04 not a project')});
    await page.locator('.toast', {hasText:'not a DaVinci Resolve project'}).waitFor();
    assert.equal(await page.evaluate(() => JSON.stringify(Editor.get().project)), before);

    // Open the .drp: timeline, linked and offline media, link groups, markers, transitions.
    await page.locator('#resolve-input').setInputFiles(drp);
    await waitState(() => Editor.get().project.name === 'Harbor cut' && Editor.get().project.media.every(m => m.offline || m.duration > 8));
    const opened = await page.evaluate(() => {
      const p = Editor.get().project;
      const media = Object.fromEntries(p.media.map(m => [m.name, {offline:!!m.offline, path:m.path}]));
      return {size:[p.width, p.height, p.fps], clips:p.clips.length, media, markers:p.markers.map(m => m.name),
        linked:new Set(p.clips.filter(c => c.linkGroup).map(c => c.linkGroup)).size,
        dissolve:p.clips.filter(c => c.transition === 'dissolve').length,
        selector:[...document.querySelectorAll('#sequence-select option')].map(o => o.textContent),
        keptOutside:!JSON.stringify(p).includes('Sm2TiItem')};
    });
    assert.deepEqual(opened, {size:[1280, 720, 24], clips:4, linked:1, dissolve:1, markers:['Wide shot'],
      media:{'A001.mp4':{offline:false, path:path.join(footage, 'A001.mp4')}, 'A002.wav':{offline:true, path:windowsAudio}},
      selector:['Harbor cut', 'Harbor alt'], keptOutside:true});
    await page.locator('.timeline-clip').first().waitFor();

    // Compatibility warnings name the timeline, track and clip.
    await fileMenu('#compatibility');
    const report = await page.locator('.studio-dialog li').allTextContents();
    assert.ok(report.some(t => t.startsWith('Harbor cut · Picture · A001.mp4') && t.includes('Color page grade')), report.join('\n'));
    assert.ok(report.some(t => t.includes('Fusion composition')), report.join('\n'));
    assert.ok(report.some(t => t.includes('offline on this computer')), report.join('\n'));
    await page.getByRole('button', {name:'Close'}).click();

    // Relink the Windows path from a folder: matched by directory suffix and file name.
    await page.locator('#folder-relink-input').setInputFiles(path.join(temp, 'relink'));
    await waitState(() => Editor.get().project.media.every(m => !m.offline));

    // Play.
    await page.evaluate(() => { Editor.seek(0); Editor.setRate(1); });
    await waitState(() => Editor.get().time > 0.4);
    await page.evaluate(() => Editor.setRate(0));

    // Export a short MP4.
    await click('#export-video');
    await click('.studio-dialog [type=submit]');
    await page.waitForFunction(() => /exported|failed|not supported|error|cancel/i.test(document.querySelector('.render-state')?.textContent || ''),
      null, {timeout:180000}).catch(async error => {
      console.error('Render state:', await page.locator('.render-state').allTextContents(), logs);
      throw error;
    });
    const status = await page.locator('.render-state').textContent();
    assert.ok(status.startsWith('MP4 exported'), status);
    await waitState(() => document.querySelector('.export-review')?.readyState >= 1);
    await page.keyboard.press('Escape');
    console.log('Opened a .drp, relinked its Windows-path media, played and exported an MP4.');

    // Switch timelines with the sequence selector: each timeline keeps its own edits (and the relink).
    dialogs.length = 0;
    const edited = await page.evaluate(() => { const c = Editor.get().project.clips.find(c => c.type === 'video'); Editor.updateClip(c.id, {volume:37}); return c.resolveItemId; });
    await page.locator('#sequence-select').selectOption('1');
    await waitState(() => Editor.get().project.name === 'Harbor alt');
    assert.deepEqual(await page.evaluate(() => { const c = Editor.get().project.clips[0]; return [Editor.get().project.fps, c.reverse]; }),
      [30000 / 1001, true]);
    await page.evaluate(() => Editor.updateClip(Editor.get().project.clips[0].id, {volume:55}));
    await page.locator('#sequence-select').selectOption('0');
    await waitState(() => Editor.get().project.name === 'Harbor cut' && Editor.get().project.media.every(m => !m.offline));
    assert.equal(await page.evaluate(id => Editor.get().project.clips.find(c => c.resolveItemId === id).volume, edited), 37);
    await page.locator('#sequence-select').selectOption('1');
    await waitState(() => Editor.get().project.name === 'Harbor alt');
    assert.equal(await page.evaluate(() => Editor.get().project.clips[0].volume), 55);
    await page.locator('#sequence-select').selectOption('0');
    await waitState(() => Editor.get().project.name === 'Harbor cut');
    assert.deepEqual(dialogs, []);
    console.log('Switched timelines and back; each kept its edits without a prompt.');

    // Open from a disk library by typing its folder.
    await fileMenu('#open-resolve-library');
    const dialog = page.locator('.resolve-library');
    await dialog.locator('[name=path]').fill(library);
    await dialog.getByRole('button', {name:'List projects'}).click();
    await dialog.locator('tbody tr').first().waitFor();
    assert.deepEqual(await dialog.locator('tbody td').allTextContents().then(t => [t[0], t[2]]), ['Harbor Lights', '2']);
    await dialog.getByRole('button', {name:'Open'}).click();
    await waitState(() => Editor.get().project.resolveSource?.format === 'library' && Editor.get().project.name === 'Harbor cut');
    assert.equal(await page.evaluate(() => Editor.get().project.clips.length), 4);
    assert.deepEqual(treeState(library), libraryBefore);
    console.log('Listed and opened a Resolve disk library project; the library is unchanged.');

    // Resolve reorders the timelines and saves. The selector still lists "Harbor alt" second;
    // it opens by its id (by position it would now be "Harbor cut"), with a note about the save.
    execFileSync('python3', [fixtures, 'library', library, path.join(footage, 'A001.mp4'), windowsAudio, '--no-generator', '--reverse']);
    await page.locator('#sequence-select').selectOption('1');
    await waitState(() => Editor.get().project.name === 'Harbor alt' && Editor.get().project.resolveSource?.format === 'library');
    await page.locator('.toast', {hasText:'saved this project again'}).waitFor();
    console.log('A library timeline opened by its id after Resolve reordered the timelines.');
    assert.deepEqual(errors, []);

    // A browser that cannot store the original still opens the .drp, with a visible notice.
    const limited = await browser.newContext({viewport:{width:1440, height:960}});
    await limited.addInitScript(() => {
      const open = indexedDB.open.bind(indexedDB);
      indexedDB.open = (name, version) => {
        if (name !== 'splice-resolve') return open(name, version);
        const req = {};
        setTimeout(() => { Object.defineProperty(req, 'error', {value:new DOMException('Quota exceeded', 'QuotaExceededError')}); req.onerror?.(); });
        return req;
      };
    });
    const second = await limited.newPage();
    second.setDefaultTimeout(20000);
    second.on('pageerror', e => errors.push(e.message));
    await second.goto(url); await second.evaluate(() => SpliceSession.ready);
    await second.locator('#resolve-input').setInputFiles(drp);
    await second.waitForFunction(() => Editor.get().project.name === 'Harbor cut');
    await second.locator('.toast', {hasText:'saving back to Resolve will be unavailable'}).waitFor();
    assert.equal(await second.evaluate(() => Editor.get().project.resolveSource.originalKept), false);
    await limited.close();
    console.log('Opened a .drp where browser storage refused the original; the notice says write-back is unavailable.');
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    if (server.exitCode === null) { const end = new Promise(r => server.once('exit', r)); server.kill('SIGTERM'); await end; }
    fs.rmSync(temp, {recursive:true, force:true});
  }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
