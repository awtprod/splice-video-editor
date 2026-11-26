/* Start `python3 server.py` and open Splice the way a user does.
 *
 * The server prints a one-time launch link as its first stdout line; only
 * that link hands a browser the session cookie. Use:
 *
 *   const {startSplice} = require('./splice-server.cjs');
 *   const splice = await startSplice({root, env: {HOME: temp}});
 *   await splice.open(page);          // launch link, then waits for SpliceSession.ready
 *   await other.goto(splice.url);     // more tabs in the same browser context
 *   await splice.share(newContext);   // another context gets the same session
 *   await splice.stop();
 *
 * `splice.url` is the plain origin (http://127.0.0.1:<port>); `splice.process`
 * is the child process; `splice.logs()` returns the stderr tail. */
const {spawn} = require('node:child_process');
const path = require('node:path');

async function startSplice({root = path.resolve(__dirname, '..'), env = {}, port = 0} = {}) {
  const child = spawn('python3', ['-u', 'server.py', '--port', String(port)], {
    cwd: root, env: {...process.env, ...env}, stdio: ['ignore', 'pipe', 'pipe']});
  let logs = '';
  child.stderr.on('data', chunk => { logs = (logs + chunk).slice(-4000); });
  let launchUrl;
  try {
    launchUrl = await new Promise((resolve, reject) => {
      let buffer = '';
      const timer = setTimeout(() => reject(Error('No launch link from server.py ' + logs)), 20000);
      child.stdout.on('data', chunk => {
        buffer += chunk;
        const end = buffer.indexOf('\n');
        if (end >= 0) { clearTimeout(timer); resolve(buffer.slice(0, end).trim()); }
      });
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('exit', code => { clearTimeout(timer); reject(Error('Server exited ' + code + ' ' + logs)); });
    });
  } catch (error) {
    if (child.exitCode === null) child.kill('SIGKILL');
    throw error;
  }
  const url = new URL(launchUrl).origin;
  let session;
  return {
    process: child, url, launchUrl, logs: () => logs,
    async open(page) {
      if (session) await page.context().addCookies(session);
      await page.goto(session ? url : launchUrl);
      if (!session) {
        await page.waitForURL(url + '/');
        session = (await page.context().cookies(url)).filter(cookie => cookie.name.startsWith('splice_session_'));
      }
      await page.evaluate(() => SpliceSession.ready);
      return page;
    },
    async share(context) {
      if (!session) throw Error('Open Splice once before sharing its session');
      await context.addCookies(session);
      return context;
    },
    async stop() {
      if (child.exitCode !== null || child.signalCode) return;
      const ended = new Promise(resolve => child.once('exit', resolve));
      child.kill('SIGTERM');
      const timer = setTimeout(() => child.kill('SIGKILL'), 10000);
      await ended;
      clearTimeout(timer);
    },
  };
}

module.exports = {startSplice};
