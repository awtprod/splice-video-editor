/* Browser flow: edit, export FCPXML, FCP 7 XML and EDL from File > Export timeline…, import them back. */
const assert = require('node:assert/strict');
const {startSplice} = require('./splice-server.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-interchange-'));
  const splice = await startSplice({root, env: {HOME: temp, XDG_DATA_HOME: temp, SPLICE_DATA_DIR: path.join(temp, 'data')}});
  let browser;
  try {
    const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome', chromium.executablePath()]
      .find(p => p && fs.existsSync(p));
    assert.ok(executablePath, 'Install Chromium or set SPLICE_CHROME');
    browser = await chromium.launch({executablePath, args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required']});
    const context = await browser.newContext({viewport: {width: 1440, height: 960}, acceptDownloads: true});
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    await page.addInitScript(() => { window.showSaveFilePicker = undefined; window.showOpenFilePicker = undefined; });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('dialog', d => d.accept());
    const fileMenu = async id => {
      if (!await page.locator(id).isVisible()) await page.locator('.file-menu summary').click();
      await page.locator(id).click();
    };
    await splice.open(page);
    await page.waitForFunction(() => Editor.get().video?.readyState >= 2);

    // A short edit on the starter project: a split, a marker, a gain change and a slower clip.
    await page.evaluate(() => {
      Editor.select('clip-1'); Editor.seek(12); Editor.split();
      Editor.updateClip('clip-score', {volume: 80});
      Editor.editProject(p => { p.markers = [{id: 'flow-marker', name: 'Flow marker', time: 20}]; });
    });
    const cutList = () => page.evaluate(() => {
      const {project} = Editor.get(), fps = project.fps, f = t => Math.floor(t * fps + .5);
      const name = id => project.media.find(m => m.id === id)?.name;
      return {
        clips: project.clips.map(c => [c.type, c.track, f(c.start), f(c.start + c.duration) - f(c.start), c.type === 'title' ? c.title : f(c.in),
          c.type === 'title' ? '' : name(c.mediaId), ['dissolve', 'wipe', 'fade'].includes(c.transition) ? f(c.transitionDuration) : 0])
          .sort((a, b) => JSON.stringify(a) < JSON.stringify(b) ? -1 : 1),
        markers: (project.markers || []).map(m => [m.name, f(m.time)]),
      };
    });
    const expected = await cutList();

    await fileMenu('#export-timeline');
    const exportDialog = page.locator('.interchange-dialog');
    await exportDialog.locator('[name=edl]').check();
    assert.ok(await exportDialog.locator('[data-edl-note]').isVisible(), 'EDL limits are shown next to the EDL option');
    await exportDialog.locator('[name=xmeml]').check();
    const downloads = [];
    page.on('download', d => downloads.push(d));
    await exportDialog.locator('[type=submit]').click();
    await exportDialog.locator('[role=status]').filter({hasText: /Exported 3 files/}).waitFor();
    for (let i = 0; i < 50 && downloads.length < 3; i++) await page.waitForTimeout(100);
    assert.equal(downloads.length, 3, 'FCPXML, FCP 7 XML and EDL downloads');
    const saved = {};
    for (const download of downloads) {
      const target = path.join(temp, download.suggestedFilename());
      await download.saveAs(target);
      saved[path.extname(target)] = target;
    }
    assert.match(fs.readFileSync(saved['.fcpxml'], 'utf8'), /<fcpxml version="1\.10">/);
    assert.match(fs.readFileSync(saved['.edl'], 'utf8'), /^FCM: NON-DROP FRAME$/m);
    assert.match(fs.readFileSync(saved['.xml'], 'utf8'), /<xmeml version="5">/);
    const reportText = await exportDialog.locator('.interchange-report pre').textContent();
    assert.match(reportText, /FCPXML 1\.10 \(DaVinci Resolve, Final Cut Pro\):/);
    assert.match(reportText, /Final Cut Pro 7 XML \(Premiere Pro, Resolve, Avid\):/);
    assert.match(reportText, /CONFORM WARNING/);
    await exportDialog.locator('[data-close]').click();

    const importFile = async (file, extra) => {
      await fileMenu('#import-timeline');
      const dialog = page.locator('.interchange-dialog');
      await dialog.locator('[name=file]').setInputFiles(file);
      if (extra) await extra(dialog);
      const before = await page.evaluate(() => Editor.get().revision);
      await dialog.locator('[type=submit]').click();
      await page.waitForFunction(r => Editor.get().revision !== r && !document.getElementById('project-loading'), before);
      await dialog.waitFor({state: 'detached'});
    };
    await importFile(saved['.fcpxml']);
    const imported = await cutList();
    assert.deepEqual(imported, expected);
    const state = await page.evaluate(() => ({
      warnings: Editor.get().project.compatibilityWarnings,
      offline: Editor.get().project.media.filter(m => m.offline).length,
      label: document.getElementById('compatibility').textContent,
    }));
    assert.match(state.warnings[0], /Imported from .*\.fcpxml \(FCPXML\)/);
    assert.equal(state.offline, 0, 'bundled sources link by path');
    assert.match(state.label, /Project compatibility \(\d+\)/);
    await page.waitForFunction(() => Editor.get().video?.readyState >= 2);
    console.log('FCPXML export and import through the File menu keep the cut list:', imported.clips.length, 'clips.');

    await importFile(saved['.xml']);
    const fromXml = await cutList();
    assert.deepEqual(fromXml.clips.filter(c => c[0] !== 'audio'), expected.clips.filter(c => c[0] !== 'audio'));
    assert.deepEqual(fromXml.markers, expected.markers);
    console.log('FCP 7 XML export and import keep the picture cut list and the marker.');

    // A rejected file leaves its error; choosing another file clears it.
    const junk = path.join(temp, 'junk.edl');
    fs.writeFileSync(junk, 'not an edit decision list\n');
    await fileMenu('#import-timeline');
    const importDialog = page.locator('.interchange-dialog');
    await importDialog.locator('[name=file]').setInputFiles(junk);
    await importDialog.locator('[type=submit]').click();
    await importDialog.locator('[role=status]').filter({hasText: /no events|could not|EDL/}).waitFor();
    await importDialog.locator('[name=file]').setInputFiles(saved['.fcpxml']);
    assert.equal(await importDialog.locator('[role=status]').textContent(), '', 'a new file clears the old error');
    await importDialog.locator('[data-close]').click();
    await importDialog.waitFor({state: 'detached'});

    await importFile(saved['.edl'], dialog => dialog.locator('[name=fps]').selectOption('25'));
    const fromEdl = await cutList();
    const pictures = expected.clips.filter(c => c[0] === 'video');
    assert.deepEqual(fromEdl.clips.filter(c => c[0] === 'video'), pictures);
    assert.deepEqual(fromEdl.markers, expected.markers);
    console.log('EDL import restores', pictures.length, 'picture events with dissolves and the locator.');
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await splice.stop();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
