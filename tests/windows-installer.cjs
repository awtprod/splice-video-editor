const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } = require('node:fs')
const { tmpdir } = require('node:os')
const path = require('node:path')
const { _electron: electron } = require('playwright-core')

// Checks the installed Windows app. SPLICE_INSTALLED_APP points it at another
// staged copy (a Linux Electron build of the same shell, for local runs), and
// SPLICE_EXECUTABLE at the Electron binary to drive (CI: a copy of Splice.exe
// whose inspector fuse is back on, since Playwright needs it).
//
//   node tests/windows-installer.cjs               project round trip, exports, draft restore
//   node tests/windows-installer.cjs --user-path   a RAW decoder on the user's PATH is found, and
//                                                  the bundled FFmpeg is still the one used
const windows = process.platform === 'win32'
const installed = process.env.SPLICE_INSTALLED_APP ||
  path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Splice', 'Application')
const executable = process.env.SPLICE_EXECUTABLE || path.join(installed, windows ? 'Splice.exe' : 'Splice')
const ffprobe = path.join(installed, 'resources', 'ffmpeg', windows ? 'ffprobe.exe' : 'ffprobe')
const proof = path.resolve(process.env.SPLICE_INSTALLER_PROOF || 'artifacts/installer-proof')
const workspace = mkdtempSync(path.join(tmpdir(), 'splice-installer-'))
const native = path.join(proof, 'Windows Installer.prproj')
const rendered = path.join(proof, 'Windows Installer.mp4')

async function windowForEditor(userFolders = []) {
  // Launch with only system directories on PATH (and the given folders before them) to
  // exercise the bundled runtimes. On Windows the folders are quoted and followed by a blank
  // entry, as users' PATHs often are.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toLowerCase() !== 'path'))
  const systemPath = windows
    ? { Path: [...userFolders.map(folder => `"${folder}"`), '', path.join(process.env.SystemRoot, 'System32')].join(';') }
    : { PATH: [...userFolders, '/usr/bin', '/bin'].join(':') }
  const app = await electron.launch({
    executablePath: executable,
    args: [`--user-data-dir=${path.join(workspace, 'profile')}`, ...(windows ? [] : ['--no-sandbox'])],
    env: { ...env, ...systemPath, SPLICE_DATA_DIR: path.join(workspace, 'media'), SPLICE_TEST_TRACE: '1' },
    timeout: 30000
  })
  // Anything failing before the window is handed back closes the app here, so the
  // run ends with the error instead of waiting on an Electron left open.
  let page
  try {
    app.on('console', message => { if (message.type() === 'error') console.error(message.text()) })
    page = await app.firstWindow()
    page.setDefaultTimeout(20000)
    // The server refuses a window that did not arrive through the one-time launch link,
    // so a launcher that loses the token fails here at once, not on a timeout.
    const refusal = 'Open Splice from its launch link'
    await page.waitForFunction(text => !!window.SpliceSession || document.body?.textContent.includes(text), refusal)
    const refused = await page.evaluate(text => !window.SpliceSession && document.body.textContent.includes(text), refusal)
    assert.ok(!refused, `The editor server refused the launcher's window (${page.url()}): the launch link did not reach it.`)
    await page.evaluate(() => SpliceSession.ready)
    page.on('pageerror', error => console.error(error))
    await app.evaluate(({session}, directory) => {
      const { join } = process.getBuiltinModule('path')
      session.defaultSession.on('will-download', (event, item) => {
        const ext = item.getFilename().endsWith('.prproj') ? 'prproj' : 'mp4'
        item.setSavePath(join(directory, `Windows Installer.${ext}`))
      })
    }, proof)
    await page.evaluate(() => { window.showSaveFilePicker = undefined })
  } catch (error) {
    await app.close().catch(() => {})
    throw error
  }
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
    console.log('Starting the first export after Premiere reopen.')
    await page.locator('.studio-dialog [type=submit]').click()
    await page.waitForFunction(() => !document.querySelector('.studio-dialog [type=submit]').disabled,
      null, { timeout: 25000 })
    const firstStatus = await page.locator('.render-state').textContent()
    assert.ok(firstStatus.startsWith('MP4 exported'), firstStatus)
    await waitForFile(rendered)
    require('node:fs').unlinkSync(rendered)
    console.log('First export passed. Checking cancel and retry.')
    await page.locator('.studio-dialog [type=submit]').click()
    await page.waitForFunction(() => document.querySelector('.studio-dialog [data-cancel]')?.textContent === 'Cancel render')
    await page.locator('.studio-dialog [data-cancel]').click()
    await page.waitForFunction(() => document.querySelector('.render-state')?.textContent === 'Render cancelled.')
    await page.locator('.studio-dialog [type=submit]').click()
    await page.waitForFunction(() => {
      const render = document.querySelector('.studio-dialog [type=submit]')
      return render && !render.disabled
    }, null, { timeout: 45000 })
    const status = await page.locator('.render-state').textContent()
    assert.ok(status.startsWith('MP4 exported'), status)
    await waitForFile(rendered)
    const { streams } = JSON.parse(execFileSync(ffprobe, [
      '-v', 'error', '-show_entries', 'stream=codec_name,nb_frames,width,height', '-of', 'json', rendered
    ], { encoding: 'utf8', timeout: 10000 }))
    assert.deepEqual(streams[0], { codec_name: 'h264', width: 640, height: 360, nb_frames: '176' })
    assert.equal(streams[1].codec_name, 'aac')
    await page.screenshot({ path: path.join(proof, 'Windows-export.png') })
    console.log(JSON.stringify({ native, rendered, streams, cancelledThenRetried: true }))
  } catch (error) {
    await page.screenshot({ path: path.join(proof, 'Windows-error.png') }).catch(() => {})
    console.error(await page.locator('.render-state').allTextContents())
    try {
      const powershell = path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
      console.error(execFileSync(powershell, [
        '-NoProfile', '-NonInteractive', '-Command',
        `Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe','ffmpeg.exe','ffprobe.exe' } | Select-Object Name,ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress`
      ], { timeout: 8000, encoding: 'utf8' }))
    } catch (diagnosticError) {
      console.error(diagnosticError.message)
    }
    throw error
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

// A decoder the user put on PATH is found, as GET /api/raw/decoders reports it, while every
// FFmpeg lookup and run still uses the bundled FFmpeg, although the same folder has an ffmpeg.
async function decodersOnUserPath() {
  mkdirSync(proof, { recursive: true })
  const folder = path.join(workspace, 'user tools')
  const ran = path.join(workspace, 'user-ffmpeg-ran.txt')
  mkdirSync(folder)
  const stub = name => path.join(folder, name + (windows ? '.cmd' : ''))
  for (const name of ['braw-decode', 'ffmpeg', 'ffprobe']) {
    const record = name === 'braw-decode' ? '' : windows ? `echo ${name}>>"${ran}"\r\n` : `echo ${name} >> '${ran}'\n`
    writeFileSync(stub(name), windows ? `@echo off\r\n${record}exit /b 9\r\n` : `#!/bin/sh\n${record}exit 9\n`, { mode: 0o755 })
  }
  const bundled = name => path.join(installed, 'resources', 'ffmpeg', name + (windows ? '.exe' : ''))
  const same = (a, b) => windows ? path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase() : path.resolve(a) === path.resolve(b)
  const { app, page } = await windowForEditor([folder])
  try {
    const { decoders } = await page.evaluate(() => fetch('/api/raw/decoders').then(response => response.json()))
    const braw = decoders.find(decoder => decoder.id === 'braw')
    assert.equal(braw.available, true, `braw-decode on the user's PATH was not found: ${JSON.stringify(braw)}`)
    assert.ok(same(realpathSync.native(braw.path), realpathSync.native(stub('braw-decode'))), braw.path)
    const prores = decoders.find(decoder => decoder.id === 'prores_raw')
    if (windows) assert.equal(prores.available, true, 'the bundled FFmpeg 9.1 has prores_raw')
    if (prores.available) assert.ok(same(prores.path, bundled('ffmpeg')), `prores_raw uses ${prores.path}`)
    // The export dialog's capabilities: found, and test encodes run, with the bundled FFmpeg.
    const { machine } = await page.evaluate(() => fetch('/api/delivery/capabilities').then(response => response.json()))
    assert.ok(same(machine.ffmpeg, bundled('ffmpeg')), `delivery uses ${machine.ffmpeg}`)
    assert.ok(!existsSync(ran), `the user's ffmpeg ran: ${existsSync(ran) && readFileSync(ran, 'utf8')}`)
    const report = { userPath: folder, braw: braw.path, proresRaw: prores.path, deliveryFFmpeg: machine.ffmpeg,
      userFFmpegRan: false }
    writeFileSync(path.join(proof, 'user-path.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(report))
  } finally {
    await app.close()
  }
}

;(process.argv[2] === '--user-path' ? decodersOnUserPath : main)()
  .catch(error => { console.error(error); process.exitCode = 1 })
