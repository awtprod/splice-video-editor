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
    form.innerHTML='<p class="export-spec"></p><label>Format<select name="codec"><option value="h264">H.264 / AAC · MP4</option><option value="prores">ProRes 422 HQ / PCM · MOV</option><option value="wav">Audio mix · WAV</option></select></label><label>Range<select name="rangeMode"><option value="all">Entire sequence</option><option value="custom">Custom range</option></select></label><label hidden>Start (seconds)<input name="rangeStart" type="number" min="0" step="any"></label><label hidden>End (seconds)<input name="rangeEnd" type="number" min="0" step="any"></label><label>Quality<select name="quality"><option value="16">Master · CRF 16</option><option value="20" selected>Delivery · CRF 20</option><option value="26">Review copy · CRF 26</option></select></label><label>Audio bitrate<select name="bitrate"><option>128</option><option selected>192</option><option>320</option></select></label><progress max="1" value="0" hidden></progress><p role="status" class="render-state"></p><footer><button type="button" data-cancel>Close</button><button type="submit" class="primary">Render MP4</button></footer>';
    const rangeFields=[form.elements.rangeStart,form.elements.rangeEnd];
    form.elements.codec.closest('label').insertAdjacentHTML('afterend',
      '<label hidden>Color space<select name="colorSpace"><option value="source">Preserve source color space</option><option value="rec709">Rec. 709 SDR</option></select></label>');
    rangeFields[0].value=String(Math.min(state.time,Math.max(0,state.duration-1/state.project.fps)).toFixed(6));
    rangeFields[1].value=String((Math.ceil((state.duration-1e-9)*state.project.fps)/state.project.fps).toFixed(6));
    form.elements.rangeMode.onchange=()=>{
      const custom=form.elements.rangeMode.value==='custom';
      for(const field of rangeFields){field.closest('label').hidden=!custom;field.required=custom;}
    };
    form.querySelector('.export-spec').textContent=state.project.name+' · '+state.project.width+' × '+state.project.height+' · '+Number(state.project.fps.toFixed(3))+' fps';
    const nativeBlocks=state.project.clips.filter(c=>c.transition==='native'||c.nativeTimeRemap||c.nativeUnsupported);
    if(nativeBlocks.length) {
      const preview=document.createElement('p');preview.className='render-blocked';
      preview.textContent=nativeBlocks.map(c=>{
        const m=state.project.media.find(item=>item.id===c.mediaId);
        const problem=c.transition==='native'? c.transitionMatchName||'native transition' : c.nativeTimeRemap?'time-remapping curve':c.nativeUnsupported;
        return (m?.name||c.title||'Clip')+' at '+c.start.toFixed(2)+' s uses '+problem;
      }).join('. ')+'. Replace the unsupported item to render this sequence.';
      form.querySelector('.export-spec').after(preview);
    }
    if (state.project.clips.some(c => c.nativeVolumeOutsideTrim)) {
      const notice = document.createElement('p');
      notice.className = 'render-blocked';
      notice.textContent = "This render uses static gain for clips with Premiere volume keys outside their source trim.";
      form.querySelector('.export-spec').after(notice);
    }
    const render=form.querySelector('[type=submit]'), cancel=form.querySelector('[data-cancel]'), progress=form.querySelector('progress'), status=form.querySelector('[role=status]');
    const colorNotice=document.createElement('p');
    const hdrSources=new Set(state.project.media.filter(m=>m.hdr).map(m=>m.id));
    const hdr=state.project.clips.some(c=>c.type==='video' && hdrSources.has(c.mediaId));
    colorNotice.textContent='This export tone-maps HDR footage to Rec. 709.';
    form.querySelector('.export-spec').after(colorNotice);
    let controller, renderWarnings=[];
    const syncFormat=()=>{
      const h264=form.elements.codec.value==='h264';
      const prores=form.elements.codec.value==='prores';
      form.elements.quality.closest('label').hidden=!h264;
      form.elements.bitrate.closest('label').hidden=!h264;
      form.elements.colorSpace.closest('label').hidden=!prores;
      colorNotice.hidden=!hdr||!(h264||prores&&form.elements.colorSpace.value==='rec709');
      render.textContent='Render '+({h264:'MP4',prores:'MOV',wav:'WAV'}[form.elements.codec.value]);
    };
    form.elements.codec.onchange=form.elements.colorSpace.onchange=syncFormat;syncFormat();
    cancel.onclick=()=>{if(controller){controller.abort();cancel.disabled=true;status.textContent='Cancelling…';}else node.close();};
    node.oncancel=e=>{if(controller){e.preventDefault();cancel.click();}};
    form.onsubmit=async e=>{
      e.preventDefault();if(controller)return;
      controller=new AbortController();renderWarnings=[];render.disabled=true;cancel.textContent='Cancel render';progress.hidden=false;
      const settings=[form.elements.codec,form.elements.colorSpace,form.elements.quality,form.elements.bitrate,form.elements.rangeMode,...rangeFields];
      for(const field of settings)field.disabled=true;
      status.textContent='Preparing media…';
      try {
        const opts={codec:form.elements.codec.value,crf:Number(form.elements.quality.value),audioBitrate:Number(form.elements.bitrate.value)};
        if(opts.codec==='prores')opts.colorSpace=form.elements.colorSpace.value;
        if(form.elements.rangeMode.value==='custom')opts.range={start:rangeFields[0].valueAsNumber,end:rangeFields[1].valueAsNumber};
        const result=await Editor.exportVideo(opts,job=>{
          renderWarnings=job.warnings||[]; progress.value=job.progress;status.textContent='Rendering · '+Math.round(100*job.progress)+'%';
        },controller.signal);
        progress.value=1;status.textContent=({h264:'MP4',prores:'MOV',wav:'WAV'}[form.elements.codec.value])+' exported.'+(result?.url?' Review the render below.':'')+(renderWarnings.length?' '+renderWarnings.join(' '):'');
        form.querySelector('.export-review')?.remove();
        if(result?.url){
          let movie=document.createElement(form.elements.codec.value==='wav'?'audio':'video');
          {movie.controls=true;movie.className='export-review';movie.setAttribute('aria-label','Rendered sequence');form.querySelector('footer').before(movie);}
          movie.src=result.url;
        }
      } catch(err) {
        status.textContent=controller.signal.aborted?'Render cancelled.':err.message;
      } finally {
        controller=null;cancel.disabled=false;render.disabled=false;cancel.textContent='Close';
        for(const field of settings)field.disabled=false;
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
  function compatibility(project=Editor.get().project) {
    const issues=[];
    for(const clip of project.clips){
      const name=clip.title||project.media.find(m=>m.id===clip.mediaId)?.name||'Clip';
      if(clip.nativeTimeRemap) issues.push(name+' has native time remapping. Its speed curve is preserved in the Premiere project.');
      if(clip.transition==='native') issues.push(name+' uses a native Premiere transition. Its transition is preserved in the original project.');
      if(clip.color?.lut) issues.push(name+' has an applied LUT. A working project or rendered video retains this grade.');
      if(clip.nativeUnsupported) issues.push(name+' · '+String(clip.nativeUnsupported));
    }
    for(const issue of project.compatibilityWarnings||[])issues.push(String(issue));
    return [...new Set(issues)];
  }
  function projectReport(){
    const node=dialog('Project compatibility');
    const list=document.createElement('ul'), issues=compatibility();
    for(const text of issues){const li=document.createElement('li');li.textContent=text;list.append(li);}
    if(issues.length)node.append(list);else{const p=document.createElement('p');p.textContent='No flagged items in this sequence.';node.append(p);}
    const close=document.createElement('button');close.textContent='Close';close.onclick=()=>node.close();node.append(close);node.showModal();
  }
  return {newSequence,exportVideo,shortcuts,compatibility,projectReport};
})();
