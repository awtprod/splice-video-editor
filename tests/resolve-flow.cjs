/* Open synthetic DaVinci Resolve projects (.drp and disk library) through the shipped server and UI.
 *
 * Footage the project names is on this computer, in a folder on another filesystem than Splice's data
 * folder (/dev/shm when it is one, or SPLICE_TEST_MEDIA_DIR); it arrives linked in place. Set
 * SPLICE_TEST_DATA_DIR to choose where the data folder is made (default: the temp folder). */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {startSplice} = require('./splice-server.cjs');
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

function files(folder) {
  if (!fs.existsSync(folder)) return [];
  return fs.readdirSync(folder, {withFileTypes:true, recursive:true}).filter(e => e.isFile())
    .map(e => path.join(e.parentPath ?? e.path, e.name));
}

// A small grey picture of one frame, for comparing the export with its source.
function frame(file, seconds) {
  return execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-ss', String(seconds), '-i', file, '-frames:v', '1',
    '-vf', 'scale=32:18:flags=area,format=gray', '-f', 'rawvideo', '-'], {maxBuffer:1 << 20});
}
function stats(pixels) {
  const mean = pixels.reduce((a, b) => a + b, 0) / pixels.length;
  return {mean, deviation:Math.sqrt(pixels.reduce((a, b) => a + (b - mean) ** 2, 0) / pixels.length)};
}
function correlation(a, b) {
  const sa = stats(a), sb = stats(b);
  let sum = 0;
  for (let i = 0; i < a.length; i++) sum += (a[i] - sa.mean) * (b[i] - sb.mean);
  return sum / a.length / (sa.deviation * sb.deviation || 1);
}

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-resolve-'));
  const dataParent = process.env.SPLICE_TEST_DATA_DIR
    ? fs.mkdtempSync(path.join(process.env.SPLICE_TEST_DATA_DIR, 'splice-resolve-data-')) : temp;
  const data = path.join(dataParent, 'data');
  fs.mkdirSync(data, {recursive:true});
  const mediaParent = process.env.SPLICE_TEST_MEDIA_DIR ||
    (fs.existsSync('/dev/shm') && fs.statSync('/dev/shm').dev !== fs.statSync(data).dev ? '/dev/shm' : os.tmpdir());
  const media = fs.mkdtempSync(path.join(mediaParent, 'splice-resolve-media-'));
  const crossFilesystem = fs.statSync(media).dev !== fs.statSync(data).dev;
  console.log(`Footage folder is on ${crossFilesystem ? 'a different filesystem from' : 'the same filesystem as'} the data folder.`);
  // Footage on this computer (linked in place on open) and a Windows path that arrives offline.
  const footage = path.join(media, 'footage', 'Day 1');
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

  const footageHash = crypto.createHash('sha256').update(fs.readFileSync(path.join(footage, 'A001.mp4'))).digest('hex');
  let browser, splice;
  try {
    splice = await startSplice({root, env:{HOME:temp, XDG_DATA_HOME:temp, SPLICE_DATA_DIR:data}});
    const logs = () => splice.logs();
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome',
      chromium.executablePath()].find(p => p && fs.existsSync(p));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args:['--no-sandbox', '--autoplay-policy=no-user-gesture-required']});
    const context = await browser.newContext({viewport:{width:1440, height:960}, acceptDownloads:true});
    // The download path for saving the working project (no native file dialogs here).
    await context.addInitScript(() => { window.showSaveFilePicker = undefined; window.showOpenFilePicker = undefined; });
    const page = await context.newPage();
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
    // A timeline switch runs inside the workspace's busy() guard, which ignores a second change until it
    // finishes; the project is already showing before that, so wait for the guard too.
    const switched = name => waitState(name => Editor.get().project.name === name && document.querySelector('#save-status').hidden, name);
    await splice.open(page);

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
      const media = Object.fromEntries(p.media.map(m => [m.name, {offline:!!m.offline, path:m.path, linked:!!m.linked}]));
      return {size:[p.width, p.height, p.fps], clips:p.clips.length, media, markers:p.markers.map(m => m.name),
        linked:new Set(p.clips.filter(c => c.linkGroup).map(c => c.linkGroup)).size,
        dissolve:p.clips.filter(c => c.transition === 'dissolve').length,
        selector:[...document.querySelectorAll('#sequence-select option')].map(o => o.textContent),
        keptOutside:!JSON.stringify(p).includes('Sm2TiItem')};
    });
    assert.deepEqual(opened, {size:[1280, 720, 24], clips:4, linked:1, dissolve:1, markers:['Wide shot'],
      media:{'A001.mp4':{offline:false, path:path.join(footage, 'A001.mp4'), linked:true},
             'A002.wav':{offline:true, path:windowsAudio, linked:false}},
      selector:['Harbor cut', 'Harbor alt'], keptOutside:true});
    await page.locator('.timeline-clip').first().waitFor();
    // Linked in place: a registry link to the original, no copy or hard link in Splice's media folder.
    const footageSrc = await page.evaluate(() => Editor.get().project.media.find(m => m.name === 'A001.mp4').src);
    assert.match(footageSrc, /^\/media\/linked\/[a-f0-9]{32}\/A001\.mp4$/);
    const registry = JSON.parse(fs.readFileSync(path.join(data, 'linked-media.json'), 'utf8'));
    assert.deepEqual(Object.values(registry.links).map(e => e.path), [fs.realpathSync(path.join(footage, 'A001.mp4'))]);
    const footageInode = fs.statSync(path.join(footage, 'A001.mp4')).ino;
    assert.deepEqual(files(path.join(data, 'media')).filter(file => fs.statSync(file).ino === footageInode ||
      crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') === footageHash), [], 'the original must not be copied');

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
      console.error('Render state:', await page.locator('.render-state').allTextContents(), logs());
      throw error;
    });
    const status = await page.locator('.render-state').textContent();
    assert.ok(status.startsWith('MP4 exported'), status);
    await waitState(() => document.querySelector('.export-review')?.readyState >= 1);
    // The picture: half a second in, the export shows the linked footage (first clip, 1 s into the source).
    const exportSrc = await page.evaluate(() => document.querySelector('.export-review').src);
    const movie = path.join(temp, 'export.mp4');
    const fetched = await context.request.get(exportSrc);
    assert.equal(fetched.status(), 200);
    fs.writeFileSync(movie, await fetched.body());
    const exported = frame(movie, 0.5), original = frame(path.join(footage, 'A001.mp4'), 1.5);
    const look = {deviation:stats(exported).deviation, correlation:correlation(exported, original)};
    assert.ok(look.deviation > 8 && look.correlation > 0.5, JSON.stringify(look));
    fs.rmSync(movie);
    await page.keyboard.press('Escape');
    console.log(`Opened a .drp, relinked its Windows-path media, played and exported an MP4 (picture correlation ${look.correlation.toFixed(2)}).`);

    // Undo and redo an edit; the linked footage stays online at the same source.
    const target = await page.evaluate(() => Editor.get().project.clips.find(c => c.type === 'video').id);
    await page.evaluate(id => Editor.updateClip(id, {volume:21}), target);
    await click('#undo');
    await waitState(id => Editor.get().project.clips.find(c => c.id === id).volume !== 21, target);
    await click('#redo');
    await waitState(id => Editor.get().project.clips.find(c => c.id === id).volume === 21, target);
    assert.deepEqual(await page.evaluate(() => Editor.get().project.media.filter(m => m.name === 'A001.mp4').map(m => [m.offline, m.src])),
      [[false, footageSrc]]);

    // Save the working project and open it again: the footage is still linked, online, and plays.
    const [saved] = await Promise.all([page.waitForEvent('download'), fileMenu('#save-working')]);
    const savedPath = path.join(temp, 'Harbor cut.splice.json');
    await saved.saveAs(savedPath);
    assert.ok(fs.readFileSync(savedPath, 'utf8').includes(footageSrc));
    // The function File → Open calls with the chosen file; awaited, so playback starts after the open.
    await page.evaluate(([text, name]) => SpliceSession.openFile(new File([text], name, {type:'application/json'})),
      [fs.readFileSync(savedPath, 'utf8'), path.basename(savedPath)]);
    await waitState(src => { const m = Editor.get().project.media.find(m => m.name === 'A001.mp4');
      return m && !m.offline && (m.originalSrc || m.src) === src && m.duration > 8; }, footageSrc);
    assert.equal(await page.evaluate(id => Editor.get().project.clips.find(c => c.id === id)?.volume, target), 21);
    await page.evaluate(() => { Editor.seek(0); Editor.setRate(1); });
    await waitState(() => Editor.get().time > 0.4);
    await page.evaluate(() => Editor.setRate(0));
    console.log('Undo/redo, then saved and reopened the working project; the linked footage stayed online and played.');

    // Switch timelines with the sequence selector: each timeline keeps its own edits (and the relink).
    dialogs.length = 0;
    const edited = await page.evaluate(() => { const c = Editor.get().project.clips.find(c => c.type === 'video'); Editor.updateClip(c.id, {volume:37}); return c.resolveItemId; });
    await page.locator('#sequence-select').selectOption('1');
    await switched('Harbor alt');
    assert.deepEqual(await page.evaluate(() => { const c = Editor.get().project.clips[0]; return [Editor.get().project.fps, c.reverse]; }),
      [30000 / 1001, true]);
    await page.evaluate(() => Editor.updateClip(Editor.get().project.clips[0].id, {volume:55}));
    await page.locator('#sequence-select').selectOption('0');
    await switched('Harbor cut');
    await waitState(() => Editor.get().project.media.every(m => !m.offline));
    assert.equal(await page.evaluate(id => Editor.get().project.clips.find(c => c.resolveItemId === id).volume, edited), 37);
    await page.locator('#sequence-select').selectOption('1');
    await switched('Harbor alt');
    assert.equal(await page.evaluate(() => Editor.get().project.clips[0].volume), 55);
    await page.locator('#sequence-select').selectOption('0');
    await switched('Harbor cut');
    assert.deepEqual(dialogs, []);
    console.log('Switched timelines and back; each kept its edits without a prompt.');

    // Choose the library folder with the in-app folder browser: it lists the projects in it.
    await fileMenu('#open-resolve-library');
    const dialog = page.locator('.resolve-library');
    await dialog.locator('[name=path]').fill('');
    await dialog.getByRole('button', {name:'Browse…'}).click();
    const picker = page.locator('dialog.splice-folder-pick');
    await picker.locator('#link-location').fill(path.dirname(library));
    await picker.locator('#link-location').press('Enter');
    await picker.locator('button.link-folder', {hasText:path.basename(library)}).click();
    await page.waitForFunction(folder => document.querySelector('dialog.splice-folder-pick #link-location')?.value === folder, library);
    assert.equal(await picker.locator('input[type=checkbox]').count(), 0, 'the folder picker selects no files');
    await picker.getByRole('button', {name:'Choose this folder'}).click();
    await picker.waitFor({state:'detached'});
    await dialog.locator('tbody tr').first().waitFor();
    assert.equal(await dialog.locator('[name=path]').inputValue(), library);
    assert.equal(await dialog.locator('tbody td').first().textContent(), 'Harbor Lights');
    // Cancelling the picker keeps the typed path.
    await dialog.getByRole('button', {name:'Browse…'}).click();
    await picker.getByRole('button', {name:'Cancel'}).click();
    await picker.waitFor({state:'detached'});
    assert.equal(await dialog.locator('[name=path]').inputValue(), library);
    await dialog.getByRole('button', {name:'Close'}).click();
    console.log('Chose the library folder with the in-app folder browser.');

    // Open from a disk library by typing its folder (the fallback).
    await fileMenu('#open-resolve-library');
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
    await splice.open(second);
    await second.locator('#resolve-input').setInputFiles(drp);
    await second.waitForFunction(() => Editor.get().project.name === 'Harbor cut');
    await second.locator('.toast', {hasText:'saving back to Resolve will be unavailable'}).waitFor();
    assert.equal(await second.evaluate(() => Editor.get().project.resolveSource.originalKept), false);
    await limited.close();
    console.log('Opened a .drp where browser storage refused the original; the notice says write-back is unavailable.');
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await splice?.stop();
    fs.rmSync(temp, {recursive:true, force:true});
    fs.rmSync(media, {recursive:true, force:true});
    if (dataParent !== temp) fs.rmSync(dataParent, {recursive:true, force:true});
  }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
