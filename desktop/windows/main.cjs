const { app, BrowserWindow, dialog } = require('electron')
const { spawn } = require('node:child_process')
const { join } = require('node:path')

// The editor keeps drafts in the page's IndexedDB, which belongs to one origin,
// so the port stays fixed across launches. SPLICE_PORT exists for test runs.
const port = Number.parseInt(process.env.SPLICE_PORT || '47834', 10)
const origin = `http://127.0.0.1:${port}`
const windows = process.platform === 'win32'
let editor
let backend
let shuttingDown = false

// Chromium's profile and caches are per machine, so they live under
// %LOCALAPPDATA%\Splice (next to the media cache), not in the roaming profile
// Electron would pick by default. An explicit --user-data-dir still wins.
if (!app.commandLine.hasSwitch('user-data-dir')) {
  const local = process.env.LOCALAPPDATA || join(app.getPath('home'), 'AppData', 'Local')
  app.setPath('userData', join(process.env.SPLICE_DATA_DIR || join(local, 'Splice'), 'desktop'))
}

function startBackend() {
  const root = process.resourcesPath
  const source = join(root, 'splice')
  // Only the bundled FFmpeg is on PATH, so a different FFmpeg installed on the
  // machine can never be picked up by the editor.
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => name.toLowerCase() !== 'path'))
  env[windows ? 'Path' : 'PATH'] = join(root, 'ffmpeg')
  env.PYTHONDONTWRITEBYTECODE = '1'
  env.PYTHONUTF8 = '1'
  const child = spawn(join(root, 'python', windows ? 'python.exe' : 'python3'), [
    '-B', '-u', join(source, 'server.py'), '--port', String(port), '--exit-with-stdin', '--require-ffmpeg-features'
  ], {
    cwd: source,
    env,
    // Gives Python a hidden console that FFmpeg and render workers inherit, so
    // no console window opens for any child process.
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe']
  })
  backend = child
  let stderr = ''
  let ready = false
  child.stdin.on('error', error => console.error(error.message))
  child.stderr.on('data', chunk => {
    stderr = (stderr + chunk).slice(-1800)
    if (process.env.SPLICE_TEST_TRACE) console.error(chunk.toString().trimEnd())
  })
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('The editor server did not start within 40 seconds.')), 40000)
    let startup = ''
    child.stdout.on('data', chunk => {
      if (ready) return
      startup += chunk
      if (!startup.includes('\n')) return
      clearTimeout(timeout)
      const address = startup.split('\n', 1)[0].trim()
      startup = ''
      // The first line is the server's one-time launch link, which sets the
      // session cookie for this window.
      let link
      try { link = new URL(address) } catch { link = null }
      if (!link || link.origin !== origin || link.pathname !== '/' || !link.searchParams.get('launch')) {
        reject(new Error('The editor server returned an unexpected startup address.'))
        return
      }
      ready = true
      resolve(link.href)
    })
    child.once('error', error => {
      backend = null
      clearTimeout(timeout)
      reject(error)
    })
    child.once('exit', (code, signal) => {
      backend = null
      clearTimeout(timeout)
      if (!ready) {
        reject(new Error(stderr.trim() || `The editor server exited (${code ?? signal}).`))
      } else if (!shuttingDown) {
        dialog.showErrorBox('Splice stopped', stderr.trim() || 'The editor server closed unexpectedly.')
        app.quit()
      }
    })
  })
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (!editor) return
    if (editor.isMinimized()) editor.restore()
    editor.focus()
  })
  app.on('window-all-closed', () => app.quit())
  app.on('before-quit', event => {
    if (!backend) return
    event.preventDefault()
    if (shuttingDown) return
    shuttingDown = true
    const child = backend
    const deadline = setTimeout(() => child.kill(), 8000)
    child.once('close', () => {
      clearTimeout(deadline)
      app.quit()
    })
    child.stdin.end()
  })
  app.whenReady().then(async () => {
    const launch = await startBackend()
    editor = new BrowserWindow({
      width: 1480,
      height: 960,
      title: 'Splice',
      backgroundColor: '#101112',
      webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true }
    })
    editor.on('closed', () => { editor = null })
    editor.removeMenu()
    editor.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    for (const event of ['will-navigate', 'will-redirect']) {
      editor.webContents.on(event, (navigation, address) => {
        if (new URL(address).origin !== origin) navigation.preventDefault()
      })
    }
    await editor.loadURL(launch)
  }).catch(error => {
    dialog.showErrorBox('Splice could not start', error.message)
    app.quit()
  })
}
