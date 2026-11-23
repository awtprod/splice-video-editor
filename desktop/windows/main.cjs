const { app, BrowserWindow, dialog } = require('electron')
const { spawn } = require('node:child_process')
const { join } = require('node:path')

const origin = 'http://127.0.0.1:47834'
let editor
let backend
let shuttingDown = false

function startBackend() {
  const root = process.resourcesPath
  const source = join(root, 'splice')
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => name.toLowerCase() !== 'path'))
  env.Path = join(root, 'ffmpeg')
  const child = spawn(join(root, 'python', 'python.exe'), [
    '-B', '-u', join(source, 'server.py'), '--port', '47834', '--exit-with-stdin'
  ], {
    cwd: source,
    env,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe']
  })
  backend = child
  let stderr = ''
  let ready = false
  child.stdin.on('error', error => console.error(error.message))
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-1800) })
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('The editor server did not start within 15 seconds.')), 15000)
    let startup = ''
    child.stdout.on('data', chunk => {
      if (ready) return
      startup += chunk
      if (!startup.includes('\n')) return
      clearTimeout(timeout)
      const address = startup.trim()
      startup = ''
      if (address !== origin) {
        reject(new Error('The editor server returned an unexpected startup address.'))
        return
      }
      ready = true
      resolve()
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
    await startBackend()
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
    await editor.loadURL(origin)
  }).catch(error => {
    dialog.showErrorBox('Splice could not start', error.message)
    app.quit()
  })
}
