/* Regression through the shipped Linux HTTP, browser and FFmpeg entry points. */
const assert = require('node:assert/strict');
const {spawn, execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'splice-flow-'));
  const server = spawn('python3', ['-u','server.py','--port','0'], {
    cwd:root, env:{...process.env, HOME:temp, XDG_DATA_HOME:temp}, stdio:['ignore','pipe','pipe']
  });
  let browser, logs='';
  server.stderr.on('data',chunk=>{logs=(logs+chunk).slice(-4000);});
  try {
    const url=await new Promise((resolve,reject)=>{
      server.stdout.once('data',d=>resolve(d.toString().trim()));
      server.once('error',reject);
      server.once('exit',code=>reject(Error('Server exited '+code+' '+logs)));
    });
    const executablePath=[process.env.SPLICE_CHROME,'/opt/google/chrome/chrome','/usr/bin/chromium','/usr/bin/google-chrome',chromium.executablePath()].find(p=>p&&fs.existsSync(p));
    assert.ok(executablePath,'Install Chromium or set SPLICE_CHROME');
    browser=await chromium.launch({executablePath,args:['--no-sandbox','--autoplay-policy=no-user-gesture-required']});
    const context=await browser.newContext({viewport:{width:1440,height:960},acceptDownloads:true});
    const page=await context.newPage();
    page.setDefaultTimeout(12000);
    // Exercise the shipped download path for browsers without native filesystem dialogs.
    await page.addInitScript(()=>{window.showSaveFilePicker=undefined;window.showOpenFilePicker=undefined;});
    page.on('response',async r=>{if(r.status()>=400&&r.url().includes('/api/'))console.error(r.status(),r.url(),(await r.text().catch(()=>'' )).slice(0,200));});
    page.on('response',async r=>{
      if(r.request().method()==='GET' && /\/api\/exports\/[a-f0-9]{32}$/.test(r.url())){
        const job=await r.json().catch(()=>({}));
        if(job.state==='failed')console.error('Render failed:',job.error);
      }
    });
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('dialog',d=>d.accept());
    const click=selector=>page.locator(selector).first().click();
    const change=async(selector,value)=>{
      const field=page.locator(selector).first();await field.fill(String(value));await field.dispatchEvent('change');
    };
    const fileMenu=async(id)=>{if(!await page.locator(id).isVisible())await click('.file-menu summary');await click(id);};
    const waitState=async(fn,arg)=>page.waitForFunction(fn,arg,{timeout:12000});
    await page.goto(url);await page.evaluate(()=>SpliceSession.ready);
    await waitState(()=>Editor.get().video.readyState>=2);
    const screenshots=process.env.SPLICE_PROOF_DIR;
    if(screenshots){fs.mkdirSync(screenshots,{recursive:true});await page.screenshot({path:path.join(screenshots,'Splice-workspace.png')});}
    const cameraOriginal=path.join(temp,'camera-original.mov');
    execFileSync('ffmpeg',['-nostdin','-v','error','-threads','1','-i',path.join(root,'assets/ridge.mp4'),
      '-t','4','-vf','scale=320:180','-c:v','prores_ks','-profile:v','2','-threads','1','-an',cameraOriginal]);
    for(let pass=1;pass<=2;pass++) {
      const name='Studio flow '+pass;
      await fileMenu('#new-project');
      const dlg=page.getByRole('dialog');
      await dlg.locator('[name=name]').fill(name);
      await dlg.locator('[name=size]').selectOption('1280x720');
      await dlg.getByRole('button',{name:'Create sequence'}).click();
      await waitState(n=>Editor.get().project.name===n,name);
      await click('[data-library=media]');
      await page.locator('#media-input').setInputFiles(pass===1?path.join(root,'assets/ridge.mp4'):cameraOriginal);
      await waitState(()=>Editor.get().project.media.length===1);
      if(pass===2) {
        const imported=await page.evaluate(()=>Editor.get().project.media[0]);
        assert.equal(imported.videoCodec,'prores');assert.ok(imported.src.endsWith('.mp4'));assert.ok(imported.originalSrc.endsWith('.mov'));
      }
      await click('[data-media]');await waitState(()=>document.querySelector('.source-stage video')?.readyState>=2);
      if(screenshots&&pass===1)await page.screenshot({path:path.join(screenshots,'Splice-source.png')});
      await change('.source-scrub',1);await click('[data-action=in]');
      await change('.source-scrub',3);await click('[data-action=out]');
      await change('.source-bin input','Documentary / A camera');
      await page.locator('.source-track').selectOption('2');
      await click('[data-action=insert]');
      await waitState(()=>Editor.get().project.clips.some(c=>c.track===2&&c.in===1&&c.duration===2));
      await click('.timeline-clip.video');
      await change('[data-field=saturation]',75);
      const edge=await page.locator('.timeline-clip.video .trim-right').boundingBox();
      await page.mouse.move(edge.x+edge.width/2,edge.y+edge.height/2);await page.mouse.down();
      await page.mouse.move(edge.x+edge.width/2-22,edge.y+edge.height/2,{steps:5});await page.mouse.up();
      await waitState(()=>Editor.get().project.clips[0].duration<2);
      await click('#undo');await waitState(()=>Editor.get().project.clips[0].duration===2);
      await click('[data-library=titles]');await click('#add-title');
      await change('[data-field=title]','Opening '+pass);
      await waitState(t=>Editor.get().project.clips.some(c=>c.title===t),'Opening '+pass);
      await waitState(()=>document.querySelector('#session-status').textContent==='Draft saved locally');
      await page.reload();await page.evaluate(()=>SpliceSession.ready);
      await waitState(t=>Editor.get().project.clips.some(c=>c.title===t),'Opening '+pass);
      const exportPath=path.join(temp,'studio-'+pass+'.prproj');
      const [native]=await Promise.all([page.waitForEvent('download'),click('#save-project')]);await native.saveAs(exportPath);
      assert.equal(fs.readFileSync(exportPath).readUInt16BE(),0x1f8b);
      await page.locator('#project-input').setInputFiles(exportPath);
      await waitState(()=>Editor.get().project.premiereOriginal);
      assert.deepEqual(await page.evaluate(()=>{const p=Editor.get().project,c=p.clips.find(v=>v.type==='video');return [c.track,c.in,c.duration,c.color.saturation,p.media[0].bin,p.clips.find(v=>v.type==='title').title];}),[2,1,2,75,'Documentary/A camera','Opening '+pass]);
      if(pass===2) {
        const reopened=await page.evaluate(()=>Editor.get().project.media[0]);
        assert.ok(reopened.path.endsWith('.mov'));assert.ok(reopened.src.endsWith('.mp4'));
      }
      if(pass===1) {
        await click('#export-video'); await click('.studio-dialog [type=submit]');
        await waitState(()=>document.querySelector('.studio-dialog [data-cancel]')?.textContent==='Cancel render');
        await click('.studio-dialog [data-cancel]');
        await waitState(()=>document.querySelector('.render-state')?.textContent==='Render cancelled.');
        await click('.studio-dialog [data-cancel]');
      }
      await click('#export-video');
      const download=page.waitForEvent('download',{timeout:90000});
      await click('.studio-dialog [type=submit]');
      await page.waitForFunction(()=>!document.querySelector('.studio-dialog [type=submit]').disabled,null,{timeout:90000});
      const status=await page.locator('.render-state').textContent();
      if (!status.startsWith('MP4 exported')) {
        download.catch(()=>{});
        throw Error('Export did not complete: '+status);
      }
      const movie=await download;
      const moviePath=path.join(temp,'proof.mp4');await movie.saveAs(moviePath);
      const streams=JSON.parse(execFileSync('ffprobe',['-v','error','-show_streams','-of','json',moviePath])).streams;
      assert.ok(streams.some(s=>s.codec_name==='h264'&&s.width===1280&&s.height===720&&Number(s.nb_frames)>=50));
      assert.ok(streams.some(s=>s.codec_name==='aac'));
      await waitState(()=>document.querySelector('.export-review')?.readyState>=2);
      await click('.studio-dialog [data-cancel]');
      if(pass===2) {
        for(const [codec,extension,expected] of [['prores','mov','prores'],['wav','wav','pcm_s24le']]) {
          await click('#export-video');await page.locator('select[name=codec]').selectOption(codec);
          const [delivery]=await Promise.all([page.waitForEvent('download',{timeout:60000}),click('.studio-dialog [type=submit]')]);
          assert.ok(delivery.suggestedFilename().endsWith('.'+extension));
          const output=path.join(temp,'mix.'+extension);await delivery.saveAs(output);
          const verified=JSON.parse(execFileSync('ffprobe',['-v','error','-show_streams','-of','json',output])).streams;
          assert.ok(verified.some(s=>s.codec_name===expected));
          if(codec==='prores')assert.ok(verified.some(s=>s.codec_name==='pcm_s24le'));
          await waitState(()=>document.querySelector('.studio-dialog [data-cancel]').textContent==='Close');
          await click('.studio-dialog [data-cancel]');
        }
        console.log('Downloaded ProRes HQ with PCM audio and a separate 24-bit WAV through the export dialog.');
      }
      await page.setViewportSize({width:pass===1?1180:1440,height:pass===1?800:960});
      assert.ok(await page.locator('#export-video').isVisible());
      console.log('Linux edit, recovery, Premiere save/reopen and H.264/AAC render passed, run '+pass);
    }
    await require('./recovery-flow.cjs')(page,page.context(),url,root);
    assert.deepEqual(errors,[]);
  } finally {
    await browser?.close();
    if(server.exitCode===null){const end=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await end;}
    fs.rmSync(temp,{recursive:true,force:true});
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
