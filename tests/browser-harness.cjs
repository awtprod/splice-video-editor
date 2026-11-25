/* Shared setup for browser regressions: a private Splice server, Chromium and media helpers. */
const {spawn, execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

const root = path.resolve(__dirname, '..');

async function startSplice(prefix) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  const server = spawn('python3', ['-u', 'server.py', '--port', '0'], {
    cwd: root, env: {...process.env, HOME: temp, XDG_DATA_HOME: temp}, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '';
  server.stderr.on('data', chunk => { logs = (logs + chunk).slice(-4000); });
  const url = await new Promise((resolve, reject) => {
    server.stdout.once('data', data => resolve(data.toString().trim()));
    server.once('error', reject);
    server.once('exit', code => reject(Error('Server exited: ' + code + ' ' + logs)));
  });
  const executablePath = [process.env.SPLICE_CHROME, '/opt/google/chrome/chrome', '/usr/bin/chromium',
    '/usr/bin/google-chrome', chromium.executablePath()].find(file => file && fs.existsSync(file));
  if (!executablePath) throw Error('Install Chromium or set SPLICE_CHROME');
  const browser = await chromium.launch({executablePath,
    args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required', '--js-flags=--expose-gc']});
  async function stop() {
    await browser.close().catch(() => {});
    if (server.exitCode === null) {
      const stopped = new Promise(resolve => server.once('exit', resolve));
      server.kill('SIGTERM');
      await stopped;
    }
    fs.rmSync(temp, {recursive: true, force: true});
  }
  return {url, browser, temp, stop};
}

async function openPage(splice, viewport = {width: 1440, height: 900}) {
  const context = await splice.browser.newContext({viewport, acceptDownloads: true});
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  // Exercise the download path used by browsers without native file pickers.
  await page.addInitScript(() => { window.showSaveFilePicker = undefined; window.showOpenFilePicker = undefined; });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(splice.url);
  await page.evaluate(() => SpliceSession.ready);
  return {page, errors};
}

const ffmpeg = args => execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-threads', '1', ...args]);

// One second per segment: a distinct colour and a distinct tone (200, 300, ... 700 Hz).
const segmentColors = [[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 0], [255, 0, 255], [0, 255, 255]];
const segmentTone = index => 200 + 100 * index;
function makeCamera(file) {
  const names = ['red', 'lime', 'blue', 'yellow', 'magenta', 'cyan'];
  const inputs = names.flatMap(name => ['-f', 'lavfi', '-i', `color=c=${name}:s=160x90:r=25:d=1`]);
  ffmpeg([...inputs, '-f', 'lavfi', '-i', "aevalsrc='0.6*sin(2*PI*(200+100*floor(t))*t)':s=48000:d=6",
    '-filter_complex', names.map((_, index) => `[${index}:v]`).join('') + 'concat=n=6:v=1:a=0[v]',
    '-map', '[v]', '-map', '6:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-g', '1', '-c:a', 'aac', '-b:a', '160k', file]);
}
function nearestSegment(rgb) {
  let best = 0, distance = Infinity;
  segmentColors.forEach((color, index) => {
    const d = color.reduce((sum, value, channel) => sum + (value - rgb[channel]) ** 2, 0);
    if (d < distance) { distance = d; best = index; }
  });
  return best;
}
function colorAt(file, seconds) {
  const pixel = ffmpeg(['-ss', seconds.toFixed(3), '-i', file, '-frames:v', '1', '-vf', 'scale=1:1',
    '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']);
  return [...pixel.subarray(0, 3)];
}
function readPcm(file) {
  const raw = ffmpeg(['-i', file, '-vn', '-ac', '1', '-ar', '48000', '-f', 's16le', '-']);
  return new Int16Array(raw.buffer, raw.byteOffset, Math.floor(raw.length / 2));
}
// Dominant frequency in a 0.2 s window, from zero crossings of a pure tone.
function toneAt(pcm, seconds, rate = 48000) {
  const from = Math.round((seconds - .1) * rate), to = Math.round((seconds + .1) * rate);
  let crossings = 0;
  for (let index = from + 1; index < to; index++)
    if ((pcm[index - 1] < 0) !== (pcm[index] < 0)) crossings++;
  return crossings / 2 / ((to - from) / rate);
}

module.exports = {root, startSplice, openPage, ffmpeg, makeCamera, nearestSegment, colorAt, readPcm, toneAt, segmentTone};
