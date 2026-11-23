const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const { existsSync, mkdirSync, mkdtempSync, readFileSync } = require('node:fs')
const { tmpdir } = require('node:os')
const path = require('node:path')
const { _electron: electron } = require('playwright-core')

const installed = path.join(process.env.LOCALAPPDATA, 'Programs', 'Splice', 'Application')
const ffprobe = path.join(installed, 'resources', 'ffmpeg', 'ffprobe.exe')
const proof = path.resolve('artifacts/installer-proof')
const workspace = mkdtempSync(path.join(tmpdir(), 'splice-installer-'))
const native = path.join(proof, 'Windows Installer.prproj')
const rendered = path.join(proof, 'Windows Installer.mp4')

async function windowForEditor() {
  // Launch with only system directories on PATH to exercise the bundled runtimes.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toLowerCase() !== 'path'))
  const app = await electron.launch({
    executablePath: path.join(installed, 'Splice.exe'),
    args: [`--user-data-dir=${path.join(workspace, 'profile')}`],
    env: { ...env, Path: path.join(process.env.SystemRoot, 'System32'), SPLICE_DATA_DIR: path.join(workspace, 'media') },
    timeout: 30000
  })
  const page = await app.firstWindow()
  page.setDefaultTimeout(20000)
  await page.waitForFunction(() => !!window.SpliceSession)
  await page.evaluate(() => SpliceSession.ready)
  await app.evaluate(({session}, directory) => {
    const { join } = process.getBuiltinModule('path')
    session.defaultSession.on('will-download', (event, item) => {
      const ext = item.getFilename().endsWith('.prproj') ? 'prproj' : 'mp4'
      item.setSavePath(join(directory, `Windows Installer.${ext}`))
    })
  }, proof)
  await page.evaluate(() => { window.showSaveFilePicker = undefined })
  return { app, page }
}

async function waitForFile(file) {
  for (let attempt = 0; attempt < 250; attempt++) {
    if (existsSync(file) && !existsSync(file + '.crdownload')) return
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error(`Download did not complete: ${file}`)
}

async function main() {
  mkdirSync(proof, { recursive: true })
  let { app, page } = await windowForEditor()
  try {
    await page.evaluate(() => SpliceSession.newProject({ name: 'Windows Installer', width: 640, height: 360, fps: 25 }))
    await page.locator('#media-input').setInputFiles(['assets/ridge.mp4', 'assets/score.wav'])
    await page.waitForFunction(() => Editor.get().project.media.length === 2)
    await page.evaluate(() => {
      for (const source of Editor.get().project.media) Editor.addToTimeline(source.id)
      for (const clip of Editor.get().project.clips) Editor.updateClip(clip.id, { duration: 7.04 })
      Editor.seek(0.4)
      Editor.addTitle()
      Editor.updateClip(Editor.get().selected.id, { title: 'Splice on Windows' })
    })
    await page.waitForFunction(() => Editor.get().video.readyState >= 2)
    await page.screenshot({ path: path.join(proof, 'Windows-editor.png') })
    await page.locator('#save-project').click()
    await waitForFile(native)
    assert.equal(readFileSync(native).readUInt16BE(), 0x1f8b)
    await page.waitForFunction(() => document.getElementById('save-status').hidden)
    await page.locator('#project-input').setInputFiles(native)
    await page.waitForFunction(() => Editor.get().project.premiereOriginal && !document.getElementById('project-loading'))
    assert.equal(await page.evaluate(() => Editor.get().project.clips.find(clip => clip.type === 'title').title), 'Splice on Windows')
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1120, 780))
    await page.locator('#export-video').click()
    await page.locator('.studio-dialog [type=submit]').click()
    await page.waitForFunction(() => document.querySelector('.studio-dialog [data-cancel]')?.textContent === 'Cancel render')
    await page.locator('.studio-dialog [data-cancel]').click()
    await page.waitForFunction(() => document.querySelector('.render-state')?.textContent === 'Render cancelled.')
    await page.locator('.studio-dialog [type=submit]').click()
    await page.waitForFunction(() => document.querySelector('.render-state')?.textContent.startsWith('MP4 exported'), null, { timeout: 90000 })
    await waitForFile(rendered)
    const { streams } = JSON.parse(execFileSync(ffprobe, [
      '-v', 'error', '-show_entries', 'stream=codec_name,nb_frames,width,height', '-of', 'json', rendered
    ], { encoding: 'utf8', timeout: 10000 }))
    assert.deepEqual(streams[0], { codec_name: 'h264', width: 640, height: 360, nb_frames: '176' })
    assert.equal(streams[1].codec_name, 'aac')
    await page.screenshot({ path: path.join(proof, 'Windows-export.png') })
    console.log(JSON.stringify({ native, rendered, streams, cancelledThenRetried: true }))
  } finally {
    await app.close()
  }

  ;({ app, page } = await windowForEditor())
  try {
    assert.equal(await page.evaluate(() => Editor.get().project.clips.find(clip => clip.type === 'title').title), 'Splice on Windows')
    console.log('The installed app restored its edit after restarting.')
  } finally {
    await app.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
