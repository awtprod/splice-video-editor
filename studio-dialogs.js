window.SpliceDialogs = (() => {
  function dialog(heading) {
    const node = document.createElement('dialog');
    node.className='studio-dialog';
    const title=document.createElement('h2');title.textContent=heading;node.append(title);
    document.body.append(node);
    node.addEventListener('close',()=>node.remove(),{once:true});
    return node;
  }
  async function newSequence() {
    const node=dialog('New sequence');
    const form=document.createElement('form');form.method='dialog';
    form.innerHTML='<label>Project name<input name="name" value="Untitled" maxlength="120" required autofocus></label><label>Frame size<select name="size"><option value="1920x1080">1920 × 1080 · HD</option><option value="3840x2160">3840 × 2160 · UHD</option><option value="1280x720">1280 × 720</option><option value="1080x1920">1080 × 1920 · Portrait</option><option value="1080x1080">1080 × 1080 · Square</option></select></label><label>Frame rate<select name="rate"></select></label><footer><button type="button" data-close>Cancel</button><button class="primary">Create sequence</button></footer>';
    const rate=form.elements.rate;
    for(const x of ['24000/1001','24','25','30000/1001','30','50','60000/1001','60']){
      const [n,d]=x.split('/').map(Number);rate.append(new Option(String(Number((n/(d||1)).toFixed(3)))+' fps',x));
    }
    rate.value='25';
    form.querySelector('[data-close]').onclick=()=>node.close();
    form.onsubmit=async e=>{
      e.preventDefault();const [width,height]=form.elements.size.value.split('x').map(Number);
      const [n,d]=rate.value.split('/').map(Number);
      await SpliceSession.newProject({name:form.elements.name.value.trim()||'Untitled',width,height,fps:n/(d||1)});
      node.close();
    };
    node.append(form);node.showModal();form.elements.name.select();
  }
  let activeExport=null;
  function exportVideo() {
    if(activeExport?.isConnected){activeExport.showModal();return;}
    const node=dialog('Export video');activeExport=node;
    const state=Editor.get(), form=document.createElement('form');
    form.innerHTML='<p class="export-spec"></p><label>Quality<select name="quality"><option value="16">Master · CRF 16</option><option value="20" selected>Delivery · CRF 20</option><option value="26">Review copy · CRF 26</option></select></label><label>Audio bitrate<select name="bitrate"><option>128</option><option selected>192</option><option>320</option></select></label><progress max="1" value="0" hidden></progress><p role="status" class="render-state"></p><footer><button type="button" data-cancel>Close</button><button type="submit" class="primary">Render MP4</button></footer>';
    form.querySelector('.export-spec').textContent=state.project.name+' · '+state.project.width+' × '+state.project.height+' · '+Number(state.project.fps.toFixed(3))+' fps · H.264 / AAC';
    const render=form.querySelector('[type=submit]'), cancel=form.querySelector('[data-cancel]'), progress=form.querySelector('progress'), status=form.querySelector('[role=status]');
    let controller;
    cancel.onclick=()=>{if(controller){controller.abort();cancel.disabled=true;status.textContent='Cancelling…';}else node.close();};
    node.oncancel=e=>{if(controller){e.preventDefault();cancel.click();}};
    form.onsubmit=async e=>{
      e.preventDefault();if(controller)return;
      controller=new AbortController();render.disabled=true;cancel.textContent='Cancel render';progress.hidden=false;
      form.elements.quality.disabled=form.elements.bitrate.disabled=true;status.textContent='Preparing media…';
      try {
        const result=await Editor.exportVideo({crf:Number(form.elements.quality.value),audioBitrate:Number(form.elements.bitrate.value)},job=>{
          progress.value=job.progress;status.textContent='Rendering · '+Math.round(100*job.progress)+'%';
        },controller.signal);
        progress.value=1;status.textContent='MP4 exported. Review the rendered sequence below.';
        if(result?.url){
          let movie=form.querySelector('video');
          if(!movie){movie=document.createElement('video');movie.controls=true;movie.className='export-review';movie.setAttribute('aria-label','Rendered sequence');form.querySelector('footer').before(movie);}
          movie.src=result.url;
        }
      } catch(err) {
        status.textContent=controller.signal.aborted?'Render cancelled.':err.message;
      } finally {
        controller=null;cancel.disabled=false;render.disabled=false;cancel.textContent='Close';
        form.elements.quality.disabled=form.elements.bitrate.disabled=false;
      }
    };
    node.append(form);node.showModal();
  }
  function shortcuts() {
    const node=dialog('Keyboard and mouse');
    const list=document.createElement('dl');
    for(const [keys,action] of [
        ['Space · J K L','Play / pause · reverse, stop, forward'],['← →','Step one frame'],
        ['S · Delete','Split at playhead · delete selection'],['Ctrl/Cmd + click','Select multiple clips'],
        ['Ctrl/Cmd + C / V','Copy / paste clips at playhead'],['N · R','Toggle snapping · ripple trim'],
        ['Drag clip edge','Trim source range'],['Shift + drag edge','Roll the cut between adjacent clips'],
        ['Alt + drag clip','Slip source under the edit'],['M · [ / ]','Add marker · previous / next marker'],
        ['Ctrl/Cmd + Z · Shift + Z','Undo · redo'],['Ctrl/Cmd + S · Shift + S','Save Premiere · save working project']]){
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=keys;dd.textContent=action;list.append(dt,dd);
    }
    const close=document.createElement('button');close.textContent='Close';close.onclick=()=>node.close();
    node.append(list,close);node.showModal();
  }
  return {newSequence,exportVideo,shortcuts};
})();
