const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const os=require('node:os');

module.exports=async function recoveryFlow(page,context,url,root) {
  const load=async project=>{
    await page.locator('#project-input').setInputFiles({name:'offline.splice',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(project))});
    await page.waitForFunction(name=>Editor.get().project.name===name,project.name);
  };
  const real=path.join(root,'assets/ridge.mp4');
  const source={id:'first',name:'ridge.mp4',path:'/camera-a/ridge.mp4',type:'video',src:'',offline:true,duration:70};
  const base={name:'Relink check',width:1280,height:720,fps:25,media:[source],clips:[
    {id:'shot',start:0,duration:1,in:60,track:1,type:'video',mediaId:'first'}]};
  await load(base);
  await page.locator('#relink-input').setInputFiles(real);
  await page.waitForFunction(()=>document.querySelector('.toast')?.textContent.includes('shorter'));
  assert.ok(await page.evaluate(()=>Editor.get().project.media[0].offline));
  await load({...base,name:'Retimed relink',clips:[{...base.clips[0],in:0,duration:5,speed:2}]});
  await page.locator('[data-library=media]').click();
  const [retimedChooser]=await Promise.all([page.waitForEvent('filechooser'),page.locator('[data-media=first]').click()]);
  await retimedChooser.setFiles({name:'retimed-short-ridge.mp4',mimeType:'video/mp4',buffer:fs.readFileSync(real)});
  await page.waitForFunction(()=>document.querySelector('.toast')?.textContent.includes('retimed-short-ridge.mp4 is shorter'));
  assert.ok(await page.evaluate(()=>Editor.get().project.media[0].offline));
  await load({...base,name:'Camera relink with sound',media:[{...source,audioChannels:1}],
    clips:[{...base.clips[0],in:0,embeddedAudio:true}]});
  const [silentChooser]=await Promise.all([page.waitForEvent('filechooser'),page.locator('[data-media=first]').click()]);
  await silentChooser.setFiles(real);
  await page.waitForFunction(()=>document.querySelector('.toast')?.textContent.includes('has no audio stream'));
  assert.ok(await page.evaluate(()=>Editor.get().project.media[0].offline));
  const duplicate={...source,id:'second',path:'/camera-b/ridge.mp4'};
  await load({...base,name:'Camera A and B',media:[source,duplicate],clips:[{...base.clips[0],in:1}]});
  await page.locator('#relink-input').setInputFiles(real);
  await page.waitForFunction(()=>document.querySelector('.toast')?.textContent.includes('Multiple offline sources'));
  await page.locator('[data-library=media]').click();
  const [chooser]=await Promise.all([page.waitForEvent('filechooser'),page.locator('[data-media=first]').click()]);
  await chooser.setFiles(real);
  await page.waitForFunction(()=>!Editor.get().project.media[0].offline);
  assert.equal(await page.evaluate(()=>Editor.get().project.media[1].offline),true);
  const tree=fs.mkdtempSync(path.join(os.tmpdir(),'splice-relink-'));
  try {
    for(const [sub,asset] of [['camera-a','ridge'],['camera-b','valley']]){
      fs.mkdirSync(path.join(tree,sub));fs.copyFileSync(path.join(root,'assets',asset+'.mp4'),path.join(tree,sub,'ridge.mp4'));
    }
    await load({...base,name:'Folder matching',media:[{...source,path:'/previous/location/camera-a/ridge.mp4'}, {...duplicate,path:'/previous/location/camera-b/ridge.mp4'}],clips:[{...base.clips[0],in:0}]});
    await page.locator('#folder-relink-input').setInputFiles(tree);
    await page.waitForFunction(()=>Editor.get().project.media.every(m=>!m.offline));
    const media=await page.evaluate(()=>Editor.get().project.media);
    assert.notEqual(media[0].originalSrc,media[1].originalSrc);
    assert.ok(media[0].path.includes('camera-a'));assert.ok(media[1].path.includes('camera-b'));
    // Start the concurrent-window exercise from the folder-relinked project.
    await page.evaluate(()=>Editor.renameProject('Camera A and B'));
  } finally {fs.rmSync(tree,{recursive:true,force:true});}
  await page.waitForFunction(()=>document.querySelector('#session-status').textContent==='Draft saved locally');
  const other=await context.newPage();
  try {
    await other.goto(url);await other.evaluate(()=>SpliceSession.ready);
    assert.equal(await other.evaluate(()=>Editor.get().project.name),'Camera A and B');
    const rename=async(tab,name)=>{
      tab.once('dialog',d=>d.accept(name));
      await tab.locator('#project-name').click();
      await tab.waitForFunction(n=>Editor.get().project.name===n,name);
      await tab.waitForFunction(()=>document.querySelector('#session-status').textContent==='Draft saved locally');
    };
    // The caller normally accepts browser prompts. Use a separate page without that handler
    // to test the native rename and keep the first tab on its own divergent edit.
    await rename(other,'Window B');
    await page.locator('[data-library=titles]').click();await page.locator('#add-title').click();
    await page.waitForFunction(()=>document.querySelector('#session-status').textContent==='Draft saved locally');
    if(!await page.locator('#versions').isVisible())await page.locator('.file-menu summary').click();
    await page.locator('#versions').click();
    const buttons=page.locator('.splice-session-dialog button');
    await buttons.first().waitFor();
    const rows=await buttons.allTextContents();
    assert.ok(rows.some(x=>x.startsWith('Window B ·')));
    assert.ok(rows.some(x=>x.startsWith('Camera A and B ·')));
    await buttons.filter({hasText:'Window B ·'}).first().click();
    await page.waitForFunction(()=>Editor.get().project.name==='Window B');
    assert.equal(await page.evaluate(()=>Editor.get().project.clips.some(c=>c.type==='title')),false);
    await other.bringToFront();
    await rename(other,'Crash recovery evidence');
    const session=await context.newCDPSession(other);
    await session.send('Inspector.enable');
    const crashed=other.waitForEvent('crash',{timeout:12000});
    session.send('Page.crash').catch(e=>{if(!e.message.includes('closed'))console.error(e.message);});
    await crashed;
    const recovered=await context.newPage();
    try {
      await recovered.goto(url);await recovered.evaluate(()=>SpliceSession.ready);
      assert.equal(await recovered.evaluate(()=>Editor.get().project.name),'Crash recovery evidence');
      assert.equal(await recovered.evaluate(()=>Editor.get().project.media[0].offline),false);
    } finally {await recovered.close();}
    console.log('Short-file and duplicate-name relink rejected. Individual relink, divergent-window sessions and Chromium renderer-crash recovery preserved edits.');
  } finally {await other.close();}
};
