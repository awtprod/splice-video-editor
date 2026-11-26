/* Save to a Resolve library through the shipped server and UI: open a synthetic .drp (a Resolve
 * 21.1-shaped project), edit it, save it as a new project in a synthetic disk library, read the
 * report, confirm, reopen the saved project from the library and compare it with the edit. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {startSplice} = require('./splice-server.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-rsave-'));
  const data = path.join(temp, 'data');
  fs.mkdirSync(data, {recursive:true});
  const library = path.join(temp, 'library'), drp = path.join(temp, 'Seed project.drp');
  const made = JSON.parse(execFileSync('python3', [path.join(root, 'tests/resolve_fixtures.py'), 'seed', library, temp, drp]).toString());
  const projects = path.join(library, 'Resolve Projects', 'Users', 'guest', 'Projects');
  const before = fs.readdirSync(projects).sort();
  let browser, splice;
  try {
    splice = await startSplice({root, env:{HOME:temp, XDG_DATA_HOME:temp, SPLICE_DATA_DIR:data}});
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome',
      chromium.executablePath()].find(p => p && fs.existsSync(p));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args:['--no-sandbox']});
    const context = await browser.newContext({viewport:{width:1440, height:960}});
    await context.addInitScript(() => { window.showSaveFilePicker = undefined; window.showOpenFilePicker = undefined; });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('dialog', d => d.accept());
    const waitState = (fn, arg) => page.waitForFunction(fn, arg, {timeout:30000});
    const fileMenu = async id => { if (!await page.locator(id).isVisible()) await page.locator('.file-menu summary').first().click(); await page.locator(id).click(); };
    await splice.open(page);

    // Open the .drp: three linked picture/sound pairs at 29.97 fps, the files linked in place.
    await page.locator('#resolve-input').setInputFiles(drp);
    await waitState(() => Editor.get().project.name === 'Cut 1' && Editor.get().project.media.length === 2 &&
      Editor.get().project.media.every(m => !m.offline));
    // Edit: trim the first pair's tail by 2 frames, disable the second picture, set the last sound to 50 %.
    const edited = await page.evaluate(() => {
      const p = Editor.get().project, frame = 1 / p.fps;
      const byKind = kind => p.clips.filter(c => c.type === kind).sort((a, b) => a.start - b.start);
      const [v, a] = [byKind('video'), byKind('audio')];
      Editor.updateClip(v[0].id, {duration:v[0].duration - 2 * frame});
      Editor.updateClip(a[0].id, {duration:a[0].duration - 2 * frame});
      Editor.updateClip(v[1].id, {enabled:false});
      Editor.updateClip(a[2].id, {volume:50});
      return true;
    });
    assert.ok(edited);
    const shape = () => page.evaluate(() => {
      const p = Editor.get().project;
      return p.clips.map(c => [c.type, c.track, Math.round(c.start * p.fps), Math.round(c.duration * p.fps),
        Math.round(c.in * p.fps), c.enabled !== false, c.type === 'audio' ? Math.round(c.volume) : null]).sort();
    });
    const wanted = await shape();

    // File → Save to Resolve library…: the library, a new name, the .drp seed needs the opt-in.
    await fileMenu('#save-resolve-library');
    const dialog = page.locator('dialog.resolve-save');
    await dialog.waitFor();
    await dialog.locator('input[name=library]').fill(library);
    await dialog.locator('[data-list]').click();
    await dialog.locator('[role=status]', {hasText:'project(s) in the library'}).waitFor();
    await dialog.locator('input[name=name]').fill('Seed project');
    await dialog.locator('[data-check]').click();
    await dialog.locator('[role=status]', {hasText:'already has a project named'}).waitFor();
    assert.deepEqual(fs.readdirSync(projects).sort(), before, 'a refused name leaves the library as it was');
    await dialog.locator('input[name=name]').fill('Cut from Splice');
    await dialog.locator('[data-check]').click();
    await dialog.locator('[role=status]', {hasText:'Allow unproven changes'}).waitFor();
    await dialog.locator('input[name=unproven]').check();
    await dialog.locator('[data-check]').click();
    await dialog.locator('[role=status]', {hasText:'Checked.'}).waitFor().catch(async error => {
      throw Error('Check did not finish: ' + await dialog.locator('[role=status]').textContent() + ' ' + splice.logs());
    });
    assert.deepEqual(fs.readdirSync(projects).sort(), before, 'checking writes nothing into the library');
    const report = await dialog.locator('.resolve-report').innerText();
    for (const line of ['Clips disabled or enabled', 'Clip volume changes', 'Seed made from the .drp', 'Everything else in the seed'])
      assert.ok(report.includes(line), report);
    assert.ok(await dialog.locator('[data-save]').isEnabled());
    await dialog.locator('[data-save]').click();
    await dialog.locator('[role=status]', {hasText:'Saved “Cut from Splice”'}).waitFor();
    assert.deepEqual(fs.readdirSync(projects).sort(), [...before, 'Cut from Splice'].sort());
    assert.ok(!fs.existsSync(path.join(library, 'Resolve Projects', '.splice-staging')), 'no staging folder is left');
    assert.deepEqual(fs.readdirSync(path.join(projects, 'Cut from Splice')), ['Project.db']);

    // Reopen the saved project from the library: the same timeline as the edit.
    await dialog.locator('[data-reopen]').click();
    await waitState(() => Editor.get().project.resolveSource?.format === 'library' &&
      Editor.get().project.resolveSource.path.includes('Cut from Splice'));
    assert.deepEqual(await shape(), wanted);
    assert.deepEqual(errors, []);
    console.log(`Saved an edited .drp timeline as a new library project and reopened it (${wanted.length} clips equal).`);
  } finally {
    await browser?.close();
    await splice?.stop();
    fs.rmSync(temp, {recursive:true, force:true});
  }
}

main().catch(error => { console.error(error); process.exit(1); });
