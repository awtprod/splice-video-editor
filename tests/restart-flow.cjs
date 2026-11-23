/* Crash mid-save and recover a disk-committed draft in a persistent browser profile. */
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright-core');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function bounded(promise, detail) {
  let timer;
  try {
    return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(detail)),8000);})]);
  } finally {clearTimeout(timer);}
}
const children=new Set();
function run(file,args,options){const child=spawn(file,args,options);children.add(child);child.once('exit',()=>children.delete(child));return child;}
async function stop(child){if(child.exitCode!==null||child.signalCode)return;const done=new Promise(r=>child.once('exit',r));child.kill('SIGKILL');await bounded(done,'Process failed to terminate');}
(async()=>{
 const executable=[process.env.SPLICE_CHROME,'/opt/google/chrome/chrome','/usr/bin/chromium',chromium.executablePath()]
   .find(candidate=>candidate&&fs.existsSync(candidate));
 assert.ok(executable,'Install Chromium or set SPLICE_CHROME');
 const profile=fs.mkdtempSync(path.join(os.tmpdir(),'splice-persistent-profile-'));
 const file=path.join(profile,'DevToolsActivePort');
 let p,url,server,chrome;
 async function startServer(port) {
   server=run('python3',['-u','server.py','--port',String(port)],{stdio:['ignore','pipe','ignore']});
   return bounded(new Promise((r,j)=>{server.once('error',j);server.stdout.once('data',d=>r(d.toString().trim()));}),'No server URL');
 }
 async function app(u) {
   fs.rmSync(file,{force:true});
   chrome=run(executable,[...(process.env.SPLICE_VISIBLE_BROWSER?[]:['--headless=new']),'--no-sandbox','--no-first-run','--disable-sync','--remote-debugging-port=0','--user-data-dir='+profile,'--app='+u],{stdio:'ignore'});
   for(let i=0;i<75&&!fs.existsSync(file);i++)await delay(100);
   const [port]=fs.readFileSync(file,'utf8').split('\n');
   const browser=await chromium.connectOverCDP('http://127.0.0.1:'+port,{timeout:8000});
   const context=browser.contexts()[0];
   const page=context.pages()[0]||await context.newPage();
   if(page.url()!==u+'/')await page.goto(u);
   await page.waitForFunction(()=>window.SpliceSession);
   await page.evaluate(()=>SpliceSession.ready);
   return page;
 }
 try {
  url=await startServer(0);p=await app(url);
  const name='Recovered after browser and server termination during save';
  await p.evaluate(x=>Editor.renameProject(x),name);
  await p.waitForFunction(()=>document.querySelector('#session-status').textContent==='Draft saved locally');
  let reached;
  const pending=new Promise(r=>{reached=r;});
  await p.evaluate(()=>{window.showSaveFilePicker=undefined;});
  await p.route('**/api/premiere/save',r=>{reached();const timer=setTimeout(()=>r.abort().catch(()=>{}),8000);p.once('close',()=>clearTimeout(timer));});
  await p.locator('#save-project').click();
  await bounded(pending,'Save request was not sent');
  await stop(chrome);
  await stop(server);
  assert.equal(await startServer(new URL(url).port),url);
  p=await app(url);
  const recovered=await p.evaluate(()=>Editor.get().project.name);
  assert.equal(recovered,name);
  if (process.env.SPLICE_PROOF_DIR) {
    fs.mkdirSync(process.env.SPLICE_PROOF_DIR,{recursive:true});
    await p.screenshot({path:path.join(process.env.SPLICE_PROOF_DIR,'Splice-full-restart-recovery.png')});
  }
  console.log(JSON.stringify({recovered,serverRestart:true,browserRestart:true,interruptedSaveRequest:true}));
 } finally {
  const stopped=await Promise.allSettled([...children].map(stop));
  for(const s of stopped)if(s.status==='rejected')console.error(s.reason);
  fs.rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:100});
 }
})().catch(e=>{console.error(e);process.exitCode=1});
