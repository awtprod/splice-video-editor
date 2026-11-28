// The desktop launcher (desktop/windows/main.cjs) in plain Node, in seconds: Electron is a
// stand-in that records what main.cjs asks of it, and the editor server is a fake that
// prints a chosen first line. Each scenario runs in its own process.
//
//   node tests/windows-launcher.cjs     runs every scenario; exit code 0 when all pass
//
// Covers: the launch link is loaded exactly as the server printed it (token included);
// a link for another origin, host, path or without a token is refused with a message and
// never loaded; a server that exits shows its own message; the backend is started with the
// launcher flags, the bundled FFmpeg folder first on PATH followed by the user's own entries
// (Windows quoting, blank entries, no PATH, and "Path" vs "PATH" with one key passed on) and
// SPLICE_FFMPEG_DIR; a RAW decoder on the user's PATH is found by raw_decode while ffmpeg stays
// the bundled one; the default profile folder; the permission handlers; navigation,
// window.open and <webview> are locked down.
const assert = require('node:assert/strict')
const { spawn, spawnSync } = require('node:child_process')
const { EventEmitter } = require('node:events')
const { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } = require('node:fs')
const { tmpdir } = require('node:os')
const path = require('node:path')

const ROOT = path.join(__dirname, '..')
const MAIN = path.join(ROOT, 'desktop', 'windows', 'main.cjs')
const NATIVE_WINDOWS = process.platform === 'win32'
const EXE = NATIVE_WINDOWS ? '.exe' : ''
// A user's PATH as Windows stores it: quoted folders (one holding a ';'), blank entries, spaces.
const WINDOWS_PATH = ';"C:\\Program Files\\RED\\REDline";;C:\\Tools\\braw ; "C:\\odd;name"\\bin;C:\\Windows\\System32;'
const WINDOWS_ENTRIES = ['C:\\Program Files\\RED\\REDline', 'C:\\Tools\\braw', '"C:\\odd;name\\bin"', 'C:\\Windows\\System32']
const PORT = 47911
const ORIGIN = `http://127.0.0.1:${PORT}`
const TOKEN = 'Fake-launch-token_0123456789abcdef'

const SCENARIOS = {
  'good link': { line: `${ORIGIN}/?launch=${TOKEN}`, loads: true },
  'other port': { line: `http://127.0.0.1:${PORT + 1}/?launch=${TOKEN}` },
  'other host': { line: `http://localhost:${PORT}/?launch=${TOKEN}` },
  'other scheme': { line: `https://127.0.0.1:${PORT}/?launch=${TOKEN}` },
  'other path': { line: `${ORIGIN}/elsewhere?launch=${TOKEN}` },
  'no token': { line: `${ORIGIN}/` },
  'empty token': { line: `${ORIGIN}/?launch=` },
  'not a link': { line: 'Splice is starting' },
  'server exits': { exit: 'Splice cannot start: its FFmpeg (x) is missing filters: zscale.' },
  'explicit profile': { line: `${ORIGIN}/?launch=${TOKEN}`, loads: true, userDataSwitch: true },
  // PATH handling. `paths` replaces every PATH-like variable before main.cjs runs; `windows`
  // runs main.cjs as on Windows (on any OS); `entries` is what must follow the bundled folder.
  'windows path': { line: `${ORIGIN}/?launch=${TOKEN}`, loads: true, windows: true,
    paths: { Path: WINDOWS_PATH }, entries: WINDOWS_ENTRIES },
  'windows PATH and Path': { line: `${ORIGIN}/?launch=${TOKEN}`, loads: true, windows: true,
    paths: NATIVE_WINDOWS ? { PATH: 'C:\\Tools\\braw' } : { PATH: 'C:\\Tools\\braw', Path: 'C:\\Tools\\braw' },
    entries: ['C:\\Tools\\braw'] },
  'windows no path': { line: `${ORIGIN}/?launch=${TOKEN}`, loads: true, windows: true, paths: {}, entries: [] },
  'no path': { line: `${ORIGIN}/?launch=${TOKEN}`, loads: true, paths: {}, entries: [] },
  // The real raw_decode, in the server's environment: a decoder stub on the user's PATH is
  // found, and ffmpeg is still the bundled one although the user's folder has an ffmpeg too.
  'decoder on user path': { line: `${ORIGIN}/?launch=${TOKEN}`, loads: true, decoders: true }
}

// ---------------------------------------------------------------- the fake editor server
function fakeServer() {
  const record = { args: process.argv.slice(3) }
  if (process.env.FAKE_DECODERS) {
    // What GET /api/raw/decoders returns (raw_decode.decoder_status()), plus the FFmpeg lookups.
    const code = 'import json, sys; sys.path.insert(0, sys.argv[1]); import raw_decode, splice_platform, delivery; ' +
      'print(json.dumps(dict(status=raw_decode.decoder_status(), ffmpeg=splice_platform.ffmpeg_tool("ffmpeg"), ' +
      'ffprobe=splice_platform.ffmpeg_tool("ffprobe"), identity=delivery._ffmpeg_identity()[0], ' +
      'command=splice_platform._command(["ffmpeg", "-version"]))))'
    const env = { ...process.env, SPLICE_DATA_DIR: process.env.FAKE_DATA }
    // No decoder settings from the runner's environment (SPLICE_RAW_DECODERS, SPLICE_BRAW_SDK, ...).
    for (const name of Object.keys(env)) if (/^SPLICE_/i.test(name) && !/^SPLICE_(FFMPEG|DATA)_DIR$/i.test(name)) delete env[name]
    const python = spawnSync(process.env.FAKE_PYTHON, ['-B', '-c', code, ROOT], { env, encoding: 'utf8', timeout: 60000 })
    record.decoders = python.status === 0 ? JSON.parse(python.stdout) : { error: python.stderr || String(python.error) }
  }
  writeFileSync(process.env.FAKE_RECORD, JSON.stringify(record))
  if (process.env.FAKE_EXIT) {
    process.stderr.write(process.env.FAKE_EXIT + '\n')
    process.exit(3)
  }
  process.stdout.write(process.env.FAKE_LINE + '\n')
  process.stdin.resume()
  process.stdin.on('end', () => process.exit(0)) // --exit-with-stdin
}

// ---------------------------------------------------------------- one scenario, with main.cjs
async function scenario(name) {
  const spec = SCENARIOS[name]
  const work = mkdtempSync(path.join(tmpdir(), 'splice-launcher-'))
  const record = path.join(work, 'server.json')
  const seen = { errors: [], loads: [], windows: [], userData: null, serverExited: false, quit: false }
  let resolveDone
  const done = new Promise(resolve => { resolveDone = resolve })

  const handlers = {}
  const session = {
    defaultSession: {
      setPermissionRequestHandler: handler => { handlers.request = handler },
      setPermissionCheckHandler: handler => { handlers.check = handler }
    }
  }
  class Contents extends EventEmitter {
    setWindowOpenHandler(handler) { this.openHandler = handler }
    getURL() { return this.url || '' }
  }
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super()
      this.options = options
      this.webContents = new Contents()
      seen.windows.push(this)
      app.emit('web-contents-created', {}, this.webContents)
    }
    removeMenu() {}
    isMinimized() { return false }
    async loadURL(address) {
      seen.loads.push(address)
      this.webContents.url = address
      setImmediate(() => resolveDone())
    }
  }
  const app = new EventEmitter()
  app.commandLine = { hasSwitch: name => spec.userDataSwitch && name === 'user-data-dir' }
  app.getPath = () => path.join(work, 'home')
  app.setPath = (key, value) => { if (key === 'userData') seen.userData = value }
  app.requestSingleInstanceLock = () => true
  app.whenReady = () => Promise.resolve()
  app.quit = () => {
    let prevented = false
    app.emit('before-quit', { preventDefault: () => { prevented = true } })
    if (!prevented) { seen.quit = true; resolveDone() }
  }
  const dialog = { showErrorBox: (title, message) => { seen.errors.push([title, message]); setImmediate(() => resolveDone()) } }
  const electron = { app, BrowserWindow, dialog, session }

  const Module = require('node:module')
  const load = Module._load
  Module._load = function (request, ...rest) {
    return request === 'electron' ? electron : load.call(this, request, ...rest)
  }
  const childProcess = require('node:child_process')
  childProcess.spawn = (command, args, options) => {
    seen.command = command
    seen.env = options.env // as main.cjs passes it (Node on Windows would quietly drop a duplicate PATH key)
    const child = spawn(process.execPath, [__filename, '--fake-server', ...args], {
      ...options,
      env: { ...options.env, FAKE_LINE: spec.line || '', FAKE_EXIT: spec.exit || '', FAKE_RECORD: record,
        FAKE_DECODERS: spec.decoders ? '1' : '', FAKE_DATA: path.join(work, 'data'), FAKE_PYTHON: python }
    })
    child.once('exit', () => { seen.serverExited = true })
    return child
  }
  process.resourcesPath = path.join(work, 'resources')
  mkdirSync(path.join(process.resourcesPath, 'splice'), { recursive: true }) // the server's working directory
  process.env.LOCALAPPDATA = path.join(work, 'Local')
  delete process.env.SPLICE_DATA_DIR
  process.env.SPLICE_PORT = String(PORT)
  const bundled = path.join(process.resourcesPath, 'ffmpeg')
  mkdirSync(bundled)
  for (const name of ['ffmpeg', 'ffprobe']) writeFileSync(path.join(bundled, name + EXE), '')
  // Python for raw_decode, by its full path: found on the runner's PATH before it is replaced.
  const python = process.env.SPLICE_TEST_PYTHON || (process.env.PATH || process.env.Path || '').split(path.delimiter)
    .map(folder => path.join(folder.replaceAll('"', ''), NATIVE_WINDOWS ? 'python.exe' : 'python3'))
    .find(file => { try { return statSync(file).isFile() } catch { return false } }) || ''
  // The user's PATH: a fixed one, or theirs with a folder holding a RAW decoder and an ffmpeg.
  let userEntries = spec.entries
  const user = path.join(work, 'user bin')
  if (spec.decoders) {
    mkdirSync(user)
    for (const name of ['braw-decode', 'ffmpeg', 'ffprobe']) {
      const file = path.join(user, name + (NATIVE_WINDOWS ? '.cmd' : ''))
      writeFileSync(file, NATIVE_WINDOWS ? '@exit /b 9\r\n' : '#!/bin/sh\nexit 9\n', { mode: 0o755 })
    }
    // The runner's own folders (so Python is found), without blank or quoted ones (covered above).
    const own = (process.env.PATH || '').split(path.delimiter).map(entry => entry.trim())
    userEntries = [user, ...own.filter(entry => entry && !entry.includes('"'))]
  }
  const paths = spec.paths || (spec.decoders ? { PATH: userEntries.join(path.delimiter) }
    : { PATH: NATIVE_WINDOWS ? 'C:\\RAW\\bin;C:\\Windows\\System32' : '/opt/raw/bin:/usr/bin:/bin' })
  if (!spec.paths && !spec.decoders) userEntries = paths.PATH.split(path.delimiter)
  for (const name of Object.keys(process.env)) if (name.toLowerCase() === 'path') delete process.env[name]
  Object.assign(process.env, paths)
  const windows = spec.windows || NATIVE_WINDOWS
  if (windows) Object.defineProperty(process, 'platform', { value: 'win32' })
  require(MAIN)

  await Promise.race([done, new Promise((_, reject) => setTimeout(() => reject(new Error(`${name}: main.cjs did nothing within 10 s`)), 10000))])
  const server = JSON.parse(readFileSync(record, 'utf8'))
  const resources = process.resourcesPath

  // Started as the launcher must start it: the bundled FFmpeg folder first on PATH, then the
  // user's own entries, under exactly one PATH key ("Path" on Windows).
  assert.equal(seen.command, path.join(resources, 'python', windows ? 'python.exe' : 'python3'))
  assert.deepEqual(server.args, ['-B', '-u', path.join(resources, 'splice', 'server.py'), '--port', String(PORT),
    '--exit-with-stdin', '--require-ffmpeg-features'])
  const key = windows ? 'Path' : 'PATH'
  assert.deepEqual(Object.keys(seen.env).filter(name => name.toLowerCase() === 'path'), [key], 'one PATH variable')
  const separator = windows ? ';' : path.delimiter
  assert.equal(seen.env[key], [bundled, ...userEntries].join(separator), 'PATH: the bundled FFmpeg, then the user\'s folders')
  assert.equal(seen.env.SPLICE_FFMPEG_DIR, bundled)
  if (spec.decoders) {
    const found = server.decoders
    assert.ok(found.status, `raw_decode did not run: ${found.error}`)
    const braw = found.status.decoders.find(decoder => decoder.id === 'braw')
    const same = (a, b) => NATIVE_WINDOWS ? a.toLowerCase() === b.toLowerCase() : a === b
    assert.equal(braw.available, true, JSON.stringify(braw))
    assert.ok(same(realpathSync.native(braw.path), realpathSync.native(path.join(user, 'braw-decode' + (NATIVE_WINDOWS ? '.cmd' : '')))),
      `braw-decode on the user's PATH must be found, got ${braw.path}`)
    assert.equal(found.ffmpeg, path.join(bundled, 'ffmpeg' + EXE), 'ffmpeg must be the bundled one')
    assert.equal(found.ffprobe, path.join(bundled, 'ffprobe' + EXE), 'ffprobe must be the bundled one')
    assert.equal(found.identity, path.join(bundled, 'ffmpeg' + EXE), 'delivery must probe the bundled ffmpeg')
    assert.deepEqual(found.command, [path.join(bundled, 'ffmpeg' + EXE), '-version'], 'a bare "ffmpeg" must run the bundled one')
  }
  // Chromium's profile: %LOCALAPPDATA%\Splice\desktop unless --user-data-dir was given.
  assert.equal(seen.userData, spec.userDataSwitch ? null : path.join(process.env.LOCALAPPDATA, 'Splice', 'desktop'))

  if (spec.loads) {
    assert.deepEqual(seen.errors, [])
    assert.deepEqual(seen.loads, [spec.line], 'the window must open the launch link exactly as the server printed it')
    const [editor] = seen.windows
    assert.deepEqual(editor.options.webPreferences, { nodeIntegration: false, contextIsolation: true, sandbox: true })
    assert.deepEqual(editor.webContents.openHandler({ url: `${ORIGIN}/x` }), { action: 'deny' })
    for (const [address, allowed] of [[`${ORIGIN}/other`, true], ['http://127.0.0.1:1/', false], ['https://example.com/', false]]) {
      for (const event of ['will-navigate', 'will-redirect']) {
        let blocked = false
        editor.webContents.emit(event, { preventDefault: () => { blocked = true } }, address)
        assert.equal(blocked, !allowed, `${event} ${address}`)
      }
    }
    let webview = false
    editor.webContents.emit('will-attach-webview', { preventDefault: () => { webview = true } })
    assert.ok(webview, '<webview> must be refused')
    // Permissions: only what the editor uses, only for its own origin.
    const request = (permission, requestingUrl) => new Promise(resolve =>
      handlers.request(editor.webContents, permission, resolve, { requestingUrl }))
    for (const permission of ['fullscreen', 'notifications', 'fileSystem']) {
      assert.equal(await request(permission, `${ORIGIN}/`), true, permission)
      assert.equal(await request(permission, 'https://example.com/'), false, `${permission} for another origin`)
      assert.equal(handlers.check(editor.webContents, permission, ORIGIN), true, `check ${permission}`)
      assert.equal(handlers.check(editor.webContents, permission, 'http://127.0.0.1:1'), false, `check ${permission} elsewhere`)
    }
    for (const permission of ['media', 'clipboard-read', 'clipboard-sanitized-write', 'geolocation', 'midiSysex',
      'display-capture', 'hid', 'serial', 'usb', 'pointerLock', 'keyboardLock', 'openExternal', 'window-management',
      'idle-detection', 'storage-access', 'unknown']) {
      assert.equal(await request(permission, `${ORIGIN}/`), false, permission)
      assert.equal(handlers.check(editor.webContents, permission, ORIGIN), false, `check ${permission}`)
    }
    // Quitting closes the server's input pipe, which stops it, then the app quits.
    app.quit()
    for (let i = 0; i < 100 && !(seen.quit && seen.serverExited); i++) await new Promise(resolve => setTimeout(resolve, 50))
    assert.ok(seen.serverExited, 'quitting must stop the editor server')
    assert.ok(seen.quit, 'the app must quit once the server stopped')
  } else {
    assert.deepEqual(seen.loads, [], `${name}: nothing may be loaded`)
    assert.equal(seen.errors.length, 1, JSON.stringify(seen.errors))
    const [title, message] = seen.errors[0]
    assert.equal(title, 'Splice could not start')
    assert.equal(message, spec.exit || 'The editor server returned an unexpected startup address.')
    for (let i = 0; i < 100 && !seen.serverExited; i++) await new Promise(resolve => setTimeout(resolve, 50))
    assert.ok(seen.serverExited, 'the editor server must be stopped after a refused start')
  }
  rmSync(work, { recursive: true, force: true })
}

// ---------------------------------------------------------------- driver
if (process.argv[2] === '--fake-server') {
  fakeServer()
} else if (process.argv[2] === '--scenario') {
  scenario(process.argv[3]).then(() => process.exit(0), error => { console.error(error.stack || error); process.exit(1) })
} else {
  let failed = 0
  for (const name of Object.keys(SCENARIOS)) {
    const run = spawnSync(process.execPath, [__filename, '--scenario', name], { encoding: 'utf8', timeout: 30000 })
    const ok = run.status === 0
    if (!ok) failed++
    console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}`)
    if (!ok) console.log((run.stderr || run.error?.message || '').trim().split('\n').map(line => '     ' + line).join('\n'))
  }
  console.log(failed ? `${failed} launcher scenario(s) failed` : `all ${Object.keys(SCENARIOS).length} launcher scenarios passed`)
  process.exitCode = failed ? 1 : 0
}
