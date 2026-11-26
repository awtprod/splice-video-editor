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
    const node=dialog('New project');
    const form=document.createElement('form');form.method='dialog';
    form.innerHTML='<label>Project name<input name="name" value="Untitled" maxlength="120" required autofocus></label><label>Frame size<select name="size"><option value="1920x1080">1920 × 1080 · HD</option><option value="3840x2160">3840 × 2160 · UHD</option><option value="1280x720">1280 × 720</option><option value="1080x1920">1080 × 1920 · Portrait</option><option value="1080x1080">1080 × 1080 · Square</option></select></label><label>Frame rate<select name="rate"></select></label><footer><button type="button" data-close>Cancel</button><button class="primary">Create project</button></footer>';
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
    // An open export dialog comes to the front; a closed one is on its way out (its close
    // event removes it), so a new one is built.
    if(activeExport?.isConnected&&activeExport.open){activeExport.showModal();return;}
    const node=dialog('Export video');activeExport=node;node.dataset.dialog='export';
    const state=Editor.get(), form=document.createElement('form');
    form.innerHTML='<p class="export-spec"></p><label>Format<select name="codec"><option value="h264">H.264 / AAC · MP4</option><option value="prores">ProRes 422 HQ / PCM · MOV</option><option value="wav">Audio mix · WAV</option></select></label><label>Range<select name="rangeMode"><option value="all">Entire sequence</option><option value="custom">In to out (seconds)</option></select></label><label hidden>In (seconds)<input name="rangeStart" type="number" min="0" step="any"></label><label hidden>Out (seconds)<input name="rangeEnd" type="number" min="0" step="any"></label><label>Quality<select name="quality"><option value="16">Master · CRF 16</option><option value="20" selected>Delivery · CRF 20</option><option value="26">Review copy · CRF 26</option></select></label><fieldset class="export-audio"><legend>Audio</legend><label>Audio bitrate<select name="bitrate"><option>128</option><option selected>192</option><option>320</option></select></label><label>Limiter<input name="limiter" type="checkbox" checked></label><label>Ceiling (dBFS)<input name="limiterCeiling" type="number" min="-24" max="0" step="any" value="-1" aria-describedby="export-ceiling-help"></label><p class="export-help" id="export-ceiling-help">−24 to 0 dBFS. The limiter holds the mix at or below it before encoding.</p><label class="export-check">Keep decoded AAC under the ceiling (may lower the whole mix)<input name="aacPeakSafe" type="checkbox" aria-describedby="export-peak-safe-help"></label><p class="export-help" id="export-peak-safe-help">Lowers program level in 0.5 dB steps and reports the gain it applied.</p><label>Subtitle language<select name="subtitleLanguage" aria-describedby="export-language-help"></select></label><label hidden>Language code<input name="subtitleLanguageCode" type="text" maxlength="3" autocomplete="off" spellcheck="false" placeholder="e.g. nld" aria-describedby="export-language-help"></label><p class="export-help" id="export-language-help">ISO 639-2 code written into the subtitle track.</p></fieldset><progress max="1" value="0" hidden></progress><p role="status" class="render-state"></p><div class="export-report" hidden></div><footer><button type="button" data-cancel>Close</button><button type="submit" class="primary">Render MP4</button></footer>';
    const rangeFields=[form.elements.rangeStart,form.elements.rangeEnd];
    // Audio: the renderer's limiter, MP4 peak-safe gain and subtitle language options.
    const limiter=form.elements.limiter, ceiling=form.elements.limiterCeiling, peakSafe=form.elements.aacPeakSafe;
    const language=form.elements.subtitleLanguage, languageCode=form.elements.subtitleLanguageCode;
    const audioGroup=form.querySelector('.export-audio'), report=form.querySelector('.export-report');
    for(const [code,name] of [['und','Undetermined'],['eng','English'],['spa','Spanish'],['fra','French'],['deu','German'],['ita','Italian'],
      ['por','Portuguese'],['jpn','Japanese'],['kor','Korean'],['zho','Chinese'],['ara','Arabic'],['hin','Hindi']])language.append(new Option(code+' · '+name,code));
    language.append(new Option('Other code…','other'));
    const help=id=>form.querySelector('#'+id);
    const syncAudio=()=>{
      ceiling.disabled=!limiter.checked;
      languageCode.closest('label').hidden=language.value!=='other';
      const value=ceiling.valueAsNumber;
      ceiling.setCustomValidity(!limiter.checked||Number.isFinite(value)&&value>=-24&&value<=0?'':'Enter a ceiling from −24 to 0 dBFS.');
      const code=languageCode.value.trim();
      languageCode.setCustomValidity(languageCode.closest('label').hidden||language.closest('label').hidden||/^[a-z]{3}$/i.test(code)?'':
        'Enter a three-letter ISO 639-2 code, such as nld or swe.');
    };
    limiter.onchange=language.onchange=ceiling.oninput=languageCode.oninput=syncAudio;
    window.SpliceExportAudio?.extend(audioGroup);
    form.elements.codec.closest('label').insertAdjacentHTML('afterend',
      '<label hidden>Color space<select name="colorSpace"><option value="source">Preserve source color space</option><option value="rec709">Rec. 709 SDR</option><option value="pq">Rec. 2100 PQ HDR</option></select></label>');
    rangeFields[0].value=String(Math.min(state.time,Math.max(0,state.duration-1/state.project.fps)).toFixed(6));
    rangeFields[1].value=String((Math.ceil((state.duration-1e-9)*state.project.fps)/state.project.fps).toFixed(6));
    form.elements.rangeMode.onchange=()=>{
      const custom=form.elements.rangeMode.value==='custom';
      for(const field of rangeFields){field.closest('label').hidden=!custom;field.required=custom;}
    };
    // Timeline marks become the default custom export range: In to Out, a lone In runs to the
    // end of the sequence and a lone Out from its start.
    const marks=state.project.marks||{};
    const hasIn=Number.isFinite(marks.in), hasOut=Number.isFinite(marks.out);
    const sequenceEnd=Math.ceil((state.duration-1e-9)*state.project.fps)/state.project.fps;
    if(hasIn||hasOut){
      const start=hasIn?marks.in:0;
      const end=hasOut?Math.min(marks.out,state.duration):sequenceEnd;
      if(end>start){
        rangeFields[0].value=start.toFixed(6);
        rangeFields[1].value=end.toFixed(6);
        form.elements.rangeMode.value='custom';
        form.elements.rangeMode.onchange();
      }
    }
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
    form.querySelector('.export-spec').after(colorNotice);
    const captionsInRange=()=>{
      const custom=form.elements.rangeMode.value==='custom';
      const start=custom?rangeFields[0].valueAsNumber:0, end=custom?rangeFields[1].valueAsNumber:Infinity;
      return (state.project.captions||[]).some(c=>typeof c?.text==='string'&&Math.min(c.start+c.duration,end)>Math.max(c.start,Number.isFinite(start)?start:0));
    };
    let controller, renderWarnings=[], renderReport={};
    const syncFormat=()=>{
      const h264=form.elements.codec.value==='h264';
      const prores=form.elements.codec.value==='prores';
      const pq=prores&&form.elements.colorSpace.value==='pq';
      form.elements.quality.closest('label').hidden=!h264;
      form.elements.bitrate.closest('label').hidden=!h264;
      form.elements.colorSpace.closest('label').hidden=!prores;
      peakSafe.closest('label').hidden=help('export-peak-safe-help').hidden=!h264;
      // Only MP4 and MOV carry a subtitle stream, and only when captions fall inside the range.
      const subtitles=form.elements.codec.value!=='wav'&&captionsInRange();
      language.closest('label').hidden=help('export-language-help').hidden=!subtitles;
      if(!subtitles)languageCode.closest('label').hidden=true;
      syncAudio();
      colorNotice.hidden=!pq&&(!hdr||!(h264||prores&&form.elements.colorSpace.value==='rec709'));
      colorNotice.textContent=pq?'Maps SDR reference white to 203 nits and converts HDR footage to PQ.':
        'This export tone-maps HDR footage to Rec. 709.';
      render.textContent='Render '+({h264:'MP4',prores:'MOV',wav:'WAV'}[form.elements.codec.value]);
    };
    form.elements.codec.onchange=form.elements.colorSpace.onchange=syncFormat;
    const rangeChanged=form.elements.rangeMode.onchange;
    form.elements.rangeMode.onchange=()=>{rangeChanged();syncFormat();};
    for(const field of rangeFields)field.addEventListener('input',syncFormat);
    syncFormat();
    // A refused field says why in the status line too, not only in the browser's bubble.
    form.addEventListener('invalid',e=>{if(e.target===form.querySelector('input:invalid,select:invalid'))status.textContent=e.target.validationMessage;},true);
    const showReport=(audio,warnings)=>{
      report.replaceChildren();
      const dB=value=>(value>0?'+':value<0?'−':'')+Math.abs(value).toFixed(1);
      if(audio&&typeof audio==='object'){
        const parts=[];
        // A silent file has no peak (null); its loudness is only the meter's floor.
        for(const text of window.SpliceExportAudio?.describe(audio)||[]){const line=document.createElement('p');line.className='export-measured';line.textContent=text;report.append(line);}
        const silent=!Number.isFinite(audio.decodedTruePeakDb);
        parts.push(silent?'silent':'peak '+dB(audio.decodedTruePeakDb)+' dBTP');
        if(!silent&&Number.isFinite(audio.loudnessLufs))parts.push(dB(audio.loudnessLufs)+' LUFS');
        if(audio.aacPeakSafe)parts.push(audio.gainDb?'lowered '+Math.abs(audio.gainDb).toFixed(1)+' dB to fit the ceiling':'no gain needed');
        if(!audio.output){const line=document.createElement('p');line.className='export-measured';
          line.textContent='Decoded audio: '+parts.join(' · ');
          report.append(line);}
      }
      if(warnings.length){
        const list=document.createElement('ul');list.className='export-warnings';list.setAttribute('aria-label','Export warnings');
        for(const warning of warnings){const item=document.createElement('li');item.textContent=warning;list.append(item);}
        report.append(list);
      }
      report.hidden=!report.childElementCount;
    };
    cancel.onclick=()=>{if(controller){controller.abort();cancel.disabled=true;status.textContent='Cancelling…';}else node.close();};
    node.oncancel=e=>{if(controller){e.preventDefault();cancel.click();}};
    form.onsubmit=async e=>{
      e.preventDefault();if(controller)return;
      syncAudio();
      if(!form.checkValidity()){form.reportValidity();return;}
      controller=new AbortController();renderWarnings=[];renderReport={};showReport(null,[]);render.disabled=true;cancel.textContent='Cancel render';progress.hidden=false;
      const settings=[form.elements.codec,form.elements.colorSpace,form.elements.quality,form.elements.rangeMode,...rangeFields,audioGroup];
      for(const field of settings)field.disabled=true;
      status.textContent='Preparing media…';
      try {
        const opts={codec:form.elements.codec.value,crf:Number(form.elements.quality.value),audioBitrate:Number(form.elements.bitrate.value)};
        if(opts.codec==='prores')opts.colorSpace=form.elements.colorSpace.value;
        // Types as the renderer validates them: booleans, a number, a lowercase three-letter string.
        opts.limiter=limiter.checked;
        if(limiter.checked)opts.limiterCeiling=ceiling.valueAsNumber;
        if(opts.codec==='h264')opts.aacPeakSafe=peakSafe.checked;
        if(!language.closest('label').hidden)opts.subtitleLanguage=language.value==='other'?languageCode.value.trim().toLowerCase():language.value;
        if(form.elements.rangeMode.value==='custom')opts.range={start:rangeFields[0].valueAsNumber,end:rangeFields[1].valueAsNumber};
        window.SpliceExportAudio?.apply(form,opts);
        const result=await Editor.exportVideo(opts,job=>{
          renderWarnings=Array.isArray(job.warnings)?job.warnings:[]; renderReport=job.report||{}; progress.value=job.progress;status.textContent='Rendering · '+Math.round(100*job.progress)+'%';
        },controller.signal);
        progress.value=1;status.textContent=(opts.codec==='stems'?'Stems':{h264:'MP4',prores:'MOV',wav:'WAV'}[form.elements.codec.value])+' exported.'+(result?.url?' Review the render below.':'');
        showReport(renderReport.audio,renderWarnings);
        // The result is what the editor looks for next: bring it above the footer.
        (report.hidden?status:report).scrollIntoView({block:'nearest'});
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
        syncAudio();
      }
    };
    node.append(form);node.showModal();
  }
  function shortcuts() {
    const node=dialog('Keyboard and mouse');
    const list=document.createElement('dl');
    // Rendered from the timeline keymap so the reference always matches the bindings.
    for(const {keys,action} of window.SpliceKeymap||[]){
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=keys;dd.textContent=action;list.append(dt,dd);
    }
    const close=document.createElement('button');close.textContent='Close';close.onclick=()=>node.close();
    // The list scrolls on its own so the heading and Close stay in view; it opens at the top.
    node.classList.add('shortcut-reference');
    list.className='shortcut-list';
    node.append(list,close);node.showModal();
    close.focus({preventScroll:true});
    list.scrollTop=0;node.scrollTop=0;
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
