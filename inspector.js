(() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const label = (title, key, value, min, max, step = 1, suffix = '', group = '') =>
    `<label class="splice-control"><span>${title}<output data-value="${key}">${value}${suffix}</output></span><input aria-label="${title}" type="range" data-field="${key}" data-group="${group}" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
  const field = (title, key, value, min, max, step = .04, group = '') =>
    `<label class="splice-number"><span>${title}</span><input aria-label="${title}" type="number" data-field="${key}" data-group="${group}" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
  const section = (title, body) => `<section class="splice-section"><h3>${title}</h3>${body}</section>`;
  const option = (key, title, selected) => `<option value="${escape(key)}" ${key === selected ? 'selected' : ''}>${escape(title)}</option>`;
  let lastScopePaint = 0;
  let automationProperty = 'opacity';
  let showSafeArea = false;
  const automation = {opacity:['Opacity',0,100,1],x:['Horizontal',-100,100,1],y:['Vertical',-100,100,1],scale:['Scale',10,400,1],rotation:['Rotation',-180,180,1],volume:['Audio gain',0,200,1]};
  function atKeyframe(points,time,base) {
    if(!points.length)return base;
    if(time<=points[0].time)return points[0].value;
    for(let i=1;i<points.length;i++)if(time<=points[i].time){
      const previous=points[i-1],next=points[i];
      const ratio=(time-previous.time)/(next.time-previous.time);
      return Math.round((previous.value+(next.value-previous.value)*ratio)*100)/100;
    }
    return points.at(-1).value;
  }
  function render(state, element, editor) {
    const clip = state.selected;
    if (!clip) {
      element.innerHTML = '<div class="inspector-empty">Select a clip to edit its settings.</div>';
      return;
    }
    const source = state.project.media.find(item => item.id === clip.mediaId);
    const title = clip.title || source?.name || 'Title';
    const color = clip.color || {};
    const transform = clip.transform || {};
    const text = clip.titleStyle || {};
    const fps = state.project.fps || 25;
    const locked=!!editor.isTrackLocked?.(clip.track,clip.type);
    const maximumDuration = !source?.still && source?.duration ? Math.max(1/fps, (source.duration - (clip.in || 0))/number(clip.speed,1)) : 7200;
    let html = `<div class="splice-clip-heading"><small>${escape(clip.type)} · ${clip.track > 0 ? 'V' + clip.track : 'A' + Math.abs(clip.track)}</small><strong>${escape(title)}</strong></div>`;
    if(locked) html += '<p class="splice-compat">This track is locked. Unlock it in the timeline to edit this clip.</p>';
    html += section('Timing',
      field('Timeline start', 'start', number(clip.start, 0).toFixed(2), 0, 7200, 1/fps) +
      (clip.type==='title' ? field('Video track','track',clip.track,1,64,1) : '') +
      (clip.type === 'title' || source?.still ? '' : field('Source in', 'in', number(clip.in, 0).toFixed(2), 0, source?.duration || 7200, 1/fps)) +
      field('Duration', 'duration', number(clip.duration, 1).toFixed(2), 1/fps, maximumDuration, 1/fps));
    if (clip.type!=='title' && !source?.still && !clip.nativeTimeRemap) html += section('Speed',
      field('Speed (%)','speedPercent',Math.round(number(clip.speed,1)*10000)/100,10,800,.01) +
      `<label class="splice-safe-toggle"><input aria-label="Reverse clip" type="checkbox" data-field="reverse" ${clip.reverse?'checked':''}> Reverse clip</label>`);
    if (clip.type === 'video') {
      const previous = state.project.clips.find(item => item.type==='video' && item.track===clip.track && item.id!==clip.id &&
        Math.round((item.start+item.duration)*fps)===Math.round(clip.start*fps));
      const previousMedia = previous && state.project.media.find(item=>item.id===previous.mediaId);
      const previousSpeed = number(previous?.speed,1);
      const handle = previous && previousMedia ? Math.max(0,previous.reverse ? previous.in/previousSpeed :
        (previousMedia.duration-previous.in)/previousSpeed-previous.duration) : 0;
      html += section('Transform',
        label('Scale', 'scale', number(transform.scale, 100), 10, 400, 1, '%', 'transform') +
        label('Horizontal', 'x', number(transform.x, 0), -100, 100, 1, '%', 'transform') +
        label('Vertical', 'y', number(transform.y, 0), -100, 100, 1, '%', 'transform') +
        label('Rotation', 'rotation', number(transform.rotation, 0), -180, 180, 1, '°', 'transform') +
        label('Opacity', 'opacity', number(clip.opacity, 100), 0, 100, 1, '%') +
        (['scale','x','y','rotation','opacity'].some(key=>clip.keyframes?.[key]?.length) ? '<p class="splice-handle">Animated properties are controlled by their keyframes below.</p>' : ''));
      html += section('Color',
        label('Brightness', 'brightness', number(color.brightness, 100), 0, 200, 1, '%', 'color') +
        label('Contrast', 'contrast', number(color.contrast, 100), 0, 200, 1, '%', 'color') +
        label('Saturation', 'saturation', number(color.saturation, 100), 0, 200, 1, '%', 'color') +
        label('Exposure', 'exposure', number(color.exposure, 0), -4, 4, .1, ' EV', 'color') +
        label('Temperature', 'temperature', number(color.temperature, 0), -100, 100, 1, '', 'color') +
        label('Tint', 'tint', number(color.tint, 0), -100, 100, 1, '', 'color') +
        `<div class="splice-lut-row"><button type="button" data-action="import-lut">Import .cube LUT</button><span>${escape(color.lut?.title || 'No LUT')}</span>${color.lut ? '<button type="button" data-action="remove-lut">Remove</button>' : ''}</div>` +
        (color.lut ? label('LUT intensity', 'lutIntensity', number(color.lutIntensity, 100), 0, 100, 1, '%', 'color') : '') +
        '<input type="file" class="splice-lut-input" accept=".cube" hidden>' +
        '<canvas class="splice-scope" width="240" height="210" aria-label="RGB parade, luma waveform, and vectorscope from graded frame"></canvas>');
      html += section('Incoming transition',
        `<label class="splice-number"><span>Style</span><select aria-label="Transition style" data-field="transition">${clip.transition==='native'?option('native','Imported: '+(clip.transitionMatchName||'Premiere effect'),'native'):''}${option('none','Cut',clip.transition || 'none')}${option('dissolve','Cross dissolve',clip.transition || 'none')}${option('fade','Fade through black',clip.transition || 'none')}${option('wipe','Wipe left to right',clip.transition || 'none')}</select></label>` +
        field('Length', 'transitionDuration', number(clip.transitionDuration, .5).toFixed(2), 1/fps, 7200, 1/fps) +
        (['dissolve','wipe'].includes(clip.transition) && !previousMedia?.still ? `<p class="splice-handle ${handle+1e-6 < number(clip.transitionDuration,.5)-number(clip.transitionBefore,0) ? 'splice-handle-warn' : ''}">Outgoing source handle: ${handle.toFixed(2)} s. ${previous ? '' : 'Place a video clip directly before this one.'}</p>` : '') +
        (clip.transition!=='none' && clip.transition!=='native' && number(clip.transitionDuration,.5)>clip.duration+number(clip.transitionBefore,0)+1e-6 ?
          `<p class="splice-handle">The available shot limits playback to ${(clip.duration+number(clip.transitionBefore,0)).toFixed(2)} s.</p>` : ''));
    }
    if (clip.type === 'title') {
      const fonts=['DM Sans','IBM Plex Mono','DejaVu Sans'];
      if(text.fontFamily && !fonts.includes(text.fontFamily))fonts.unshift(text.fontFamily);
      html += section('Title',
        `<label class="splice-text"><span>Text</span><textarea aria-label="Title text" data-field="title" rows="2" maxlength="500">${escape(clip.title)}</textarea></label>` +
        `<label class="splice-number"><span>Typeface</span><select aria-label="Title typeface" data-field="fontFamily" data-group="titleStyle">${fonts.map(name => option(name,name,text.fontFamily || 'DM Sans')).join('')}</select></label>` +
        label('Size', 'fontSize', number(text.fontSize, 8), 2, 100, .5, '%', 'titleStyle') +
        label('Opacity', 'opacity', number(clip.opacity, 100), 0, 100, 1, '%') +
        label('Horizontal', 'x', number(text.x, 50), 0, 100, 1, '%', 'titleStyle') +
        label('Vertical', 'y', number(text.y, 78), 0, 100, 1, '%', 'titleStyle') +
        `<label class="splice-number"><span>Alignment</span><select aria-label="Title alignment" data-field="align" data-group="titleStyle">${['left','center','right'].map(name => option(name,name,text.align || 'center')).join('')}</select></label>` +
        label('Outline', 'strokeWidth', number(text.strokeWidth, 0), 0, 8, .5, ' px', 'titleStyle') +
        `<label class="splice-number"><span>Outline color</span><input aria-label="Title outline color" type="color" data-field="strokeColor" data-group="titleStyle" value="${escape(text.strokeColor || '#000000')}"></label>` +
        `<label class="splice-number"><span>Text color</span><input aria-label="Title text color" type="color" data-field="color" data-group="titleStyle" value="${escape(text.color || '#ffffff')}"></label>` +
        `<label class="splice-safe-toggle"><input type="checkbox" data-safe-area ${showSafeArea ? 'checked' : ''}> Show title and action safe areas</label>`);
    }
    if (clip.type === 'audio' || clip.type === 'video' && clip.embeddedAudio !== false) {
      const stream='audioStream' in clip?clip.audioStream:source?.audioStream;
      html += section('Audio',
        (source?.audioStreams>1 ? `<p class="splice-handle">${stream==null?`${source.audioStreams} source streams in stereo`:`Source stream ${stream+1}`}</p>` : '') +
        label('Gain', 'volume', number(clip.volume, 100), 0, 200, 1, '%') +
        (clip.keyframes?.volume?.length ? '<p class="splice-handle">Gain keyframes control the level. Edit them below.</p>' : '') +
        label('Pan', 'pan', number(clip.pan, 0), -100, 100, 1, '', '') +
        field('Fade in', 'fadeIn', number(clip.fadeIn, 0).toFixed(2), 0, clip.duration, .04) +
        field('Fade out', 'fadeOut', number(clip.fadeOut, 0).toFixed(2), 0, clip.duration, .04));
    }
    if (clip.type === 'audio') {
      html += section('Incoming transition',
        `<label class="splice-number"><span>Style</span><select aria-label="Audio transition style" data-field="transition">${clip.transition==='native'?option('native','Imported: '+(clip.transitionMatchName||'Premiere effect'),'native'):''}${option('none','Cut',clip.transition || 'none')}${option('dissolve','Crossfade',clip.transition || 'none')}</select></label>` +
        field('Length','transitionDuration',number(clip.transitionDuration,.5).toFixed(2),1/fps,7200,1/fps));
    }
    if (['video','audio','title'].includes(clip.type)) {
      const available=clip.type==='audio' ? ['volume'] : clip.type==='title' ? ['opacity'] :
        ['opacity','x','y','scale','rotation',...(clip.embeddedAudio===false?[]:['volume'])];
      if(!available.includes(automationProperty))automationProperty=available[0];
      const spec=automation[automationProperty], local=Math.max(0,Math.min(clip.duration,number(state.time,clip.start)-clip.start));
      const points=(clip.keyframes?.[automationProperty] || []).slice().sort((a,b)=>a.time-b.time);
      const base=automationProperty==='opacity' ? number(clip.opacity,100) : automationProperty==='volume' ? number(clip.volume,100) : number(transform[automationProperty],automationProperty==='scale'?100:0);
      const current=atKeyframe(points,local,base);
      html += section('Keyframes',
        `<label class="splice-number"><span>Property</span><select aria-label="Keyframe property" data-key-property>${available.map(key=>option(key,automation[key][0],automationProperty)).join('')}</select></label>` +
        `<div class="splice-key-edit"><label><span class="splice-key-time">Value at ${local.toFixed(2)} s</span><input aria-label="Keyframe value" type="number" class="splice-key-value" min="${spec[1]}" max="${spec[2]}" step="${spec[3]}" value="${current}"></label><button type="button" data-key-add>Add / update</button></div>` +
        (points.length ? `<div class="splice-key-list">${points.map(point=>`<button type="button" data-key-time="${point.time}" title="Go to keyframe at ${point.time.toFixed(2)} seconds">${point.time.toFixed(2)}s · ${point.value}</button>`).join('')}</div><button type="button" class="splice-key-delete" data-key-delete>Delete keyframe at playhead</button>` : '<p class="splice-handle">No keyframes. Add one at the playhead to animate this property.</p>'));
    }
    if (clip.type === 'audio' || clip.type === 'video' && clip.embeddedAudio !== false) {
      const track = clip.track;
      const mix = state.project.trackMix?.[track] || {};
      html += section('Track mixer',
        label('Track gain', 'gain', number(mix.gain, 100), 0, 200, 1, '%', 'trackMix') +
        label('Track pan', 'pan', number(mix.pan, 0), -100, 100, 1, '', 'trackMix') +
        `<div class="splice-meter" data-track="${track}" aria-label="Track peak meter"><span></span></div>` +
        `<div class="splice-toggles"><label><input type="checkbox" data-field="mute" data-group="trackMix" ${mix.mute ? 'checked' : ''}> Mute</label><label><input type="checkbox" data-field="solo" data-group="trackMix" ${mix.solo ? 'checked' : ''}> Solo</label></div>`);
    }
    const nativeLimits=[];
    if (color.lut) nativeLimits.push('3D LUT');
    if (['x','y'].some(key=>Array.isArray(clip.keyframes?.[key])&&clip.keyframes[key].length)) nativeLimits.push('position keyframes');
    if (clip.keyframes?.volume?.length && (number(clip.fadeIn,0)||number(clip.fadeOut,0))) nativeLimits.push('combined gain keyframes and fades');
    if (number(clip.pan,0)) nativeLimits.push('clip audio pan');
    const videoMix = clip.type === 'video' && state.project.trackMix?.[clip.track];
    if (videoMix?.pan || videoMix?.solo) nativeLimits.push('embedded video-track pan or solo');
    if (nativeLimits.length) html += section('Premiere compatibility',
      `<p class="splice-compat">${escape(nativeLimits.join(', '))} will not save to Premiere. Save a Splice working copy to keep this edit.</p>`);
    if(clip.transition==='native') html += section('Render compatibility',
      `<p class="splice-compat">This imported Premiere transition (${escape(clip.transitionMatchName||'unknown effect')}) stays in the native project. MP4 export cannot render it yet. Choose a supported transition to export video.</p>`);
    element.innerHTML = html;
    for(const key of ['scale','x','y','rotation','opacity','volume'])
      if(clip.keyframes?.[key]?.length) element.querySelector(`[data-field="${key}"]:not([data-group="trackMix"])`)?.setAttribute('disabled','');
    if(clip.transition==='native') element.querySelector('[data-field="transitionDuration"]')?.setAttribute('disabled','');
    if(locked) for(const input of element.querySelectorAll('[data-field]:not([data-group="trackMix"]),[data-key-add],[data-key-delete],[data-key-property],.splice-key-value,[data-action="import-lut"],[data-action="remove-lut"]')) input.disabled=true;
    document.querySelector('#monitor-frame')?.classList.toggle('show-safe-area', showSafeArea);
    element.querySelector('[data-safe-area]')?.addEventListener('change',event=>{
      showSafeArea=event.target.checked;
      document.querySelector('#monitor-frame')?.classList.toggle('show-safe-area',showSafeArea);
    });
    for (const input of element.querySelectorAll('[data-field]')) {
      input.addEventListener('input', () => {
        const output = input.closest("label")?.querySelector("output");
        if (output) output.textContent = input.value + (output.textContent.endsWith('%') ? '%' : output.textContent.endsWith('°') ? '°' : '');
      });
      input.addEventListener('change', () => {
        const active = editor.get().selected;
        if (!active || active.id !== clip.id) return;
        const key = input.dataset.field;
        const raw = input.value;
        const value = input.type === 'checkbox' ? input.checked : input.type === 'number' || input.type === 'range' ? Number(raw) : raw;
        if (input.type === 'number' && (!Number.isFinite(value) || value < Number(input.min) || value > Number(input.max))) {
          input.value = String(active[key] ?? '');
          input.setCustomValidity('Choose a value within the allowed range.');
          input.reportValidity();
          return;
        }
        input.setCustomValidity('');
        const group = input.dataset.group;
        try {
          if (group === 'trackMix') {
            editor.updateTrackMix(active.track, {[key]: value});
          } else if (key==='speedPercent') {
            editor.setClipSpeed(active.id,value/100);
            render(editor.get(),element,editor);
          } else {
            editor.updateClip(active.id, group ? {[group]: {...(active[group] || {}), [key]: value}} : {[key]: value});
            if(['align','track','pan','fadeIn','fadeOut','transition','transitionDuration'].includes(key)) render(editor.get(),element,editor);
          }
        } catch(error) {
          window.alert(error.message || 'This clip cannot be changed.');
          render(editor.get(),element,editor);
        }
      });
    }
    const picker = element.querySelector('.splice-lut-input');
    element.querySelector('[data-action="import-lut"]')?.addEventListener('click', () => picker.click());
    element.querySelector('[data-action="remove-lut"]')?.addEventListener('click', () => {
      editor.updateClip(clip.id, {color:{...color, lut:null}});
      render(editor.get(),element,editor);
    });
    picker?.addEventListener('change', async () => {
      const file=picker.files?.[0];
      if (!file) return;
      try {
        if (file.size > 4_000_000) throw new Error('The LUT file is too large.');
        const lut=window.SpliceLUT.parseCube(await file.text(),file.name);
        editor.updateClip(clip.id,{color:{...color,lut,lutIntensity:100}});
        render(editor.get(),element,editor);
      } catch(error) { window.alert(error.message || 'Cannot read this LUT.'); }
    });
    element.querySelector('[data-key-property]')?.addEventListener('change', event=>{
      automationProperty=event.target.value;
      render(editor.get(),element,editor);
    });
    element.querySelector('[data-key-add]')?.addEventListener('click',()=>{
      const active=editor.get().selected;
      if (!active || active.id!==clip.id) return;
      const value=Number(element.querySelector('.splice-key-value')?.value),spec=automation[automationProperty];
      if (!Number.isFinite(value)||value<spec[1]||value>spec[2]) return window.alert(`Enter ${spec[0]} between ${spec[1]} and ${spec[2]}.`);
      const time=Math.round(Math.max(0,Math.min(active.duration,editor.get().time-active.start))*fps)/fps;
      const points=(active.keyframes?.[automationProperty]||[]).filter(point=>Math.abs(point.time-time)>=.5/fps);
      points.push({time,value});points.sort((a,b)=>a.time-b.time);
      editor.updateClip(active.id,{keyframes:{...(active.keyframes||{}),[automationProperty]:points}});
      render(editor.get(),element,editor);
    });
    element.querySelector('[data-key-delete]')?.addEventListener('click',()=>{
      const active=editor.get().selected;
      if (!active || active.id!==clip.id) return;
      const time=editor.get().time-active.start;
      const points=(active.keyframes?.[automationProperty]||[]).filter(point=>Math.abs(point.time-time)>=.5/fps);
      editor.updateClip(active.id,{keyframes:{...(active.keyframes||{}),[automationProperty]:points}});
      render(editor.get(),element,editor);
    });
    for(const button of element.querySelectorAll('[data-key-time]')) button.addEventListener('click',()=>{
      editor.seek(clip.start+Number(button.dataset.keyTime));
      render(editor.get(),element,editor);
    });
    paintScope(element.querySelector('.splice-scope'));
    paintMeters(state);
  }
  function paintMeters(state=window.Editor?.get()) {
    for (const meter of document.querySelectorAll('.splice-meter')) {
      const peak=Number(state?.audioMeters?.[meter.dataset.track] || 0);
      meter.firstElementChild.style.width=`${Math.min(100,Math.max(0,peak*100))}%`;
      meter.setAttribute('aria-valuenow',String(Math.round(peak*100)));
    }
    const input=document.querySelector('.splice-key-value'),clip=state?.selected;
    if(input && clip && document.activeElement!==input){
      const local=Math.max(0,Math.min(clip.duration,number(state.time,clip.start)-clip.start));
      const points=(clip.keyframes?.[automationProperty]||[]).slice().sort((a,b)=>a.time-b.time);
      const base=automationProperty==='opacity'?number(clip.opacity,100):automationProperty==='volume'?number(clip.volume,100):number(clip.transform?.[automationProperty],automationProperty==='scale'?100:0);
      input.value=String(atKeyframe(points,local,base));
      const label=document.querySelector('.splice-key-time');
      if(label)label.textContent=`Value at ${local.toFixed(2)} s`;
    }
  }
  function paintScope(canvas=document.querySelector('.splice-scope')) {
    if (!canvas) return;
    const now=performance.now();
    if (now-lastScopePaint < 100) return;
    lastScopePaint=now;
    const video=document.querySelector('#preview-canvas');
    if (!video || !video.width || !video.height) return;
    const small = document.createElement('canvas');
    small.width = 96; small.height = 54;
    const context = small.getContext('2d', {willReadFrequently:true});
    try { context.drawImage(video, 0, 0, 96, 54); } catch { return; }
    const bytes = context.getImageData(0,0,96,54).data;
    const ctx = canvas.getContext('2d');
    const w=canvas.width,h=canvas.height;
    ctx.fillStyle='#101610'; ctx.fillRect(0,0,w,h);
    ctx.strokeStyle='#344135'; ctx.lineWidth=1;
    for(const y of [0,70,140,210]) {ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}
    for(let p=0;p<bytes.length;p+=4){
      const pixel=p/4,x=pixel%96;
      for(let channel=0;channel<3;channel++){
        ctx.fillStyle=['rgba(255,92,92,.12)','rgba(107,241,125,.12)','rgba(105,157,255,.12)'][channel];
        ctx.fillRect(channel*80+bytes[p+channel]/255*79,70-bytes[p+channel]/255*68,1,1);
      }
      const lum=(.2126*bytes[p]+.7152*bytes[p+1]+.0722*bytes[p+2])/255;
      ctx.fillStyle='rgba(244,244,220,.18)'; ctx.fillRect(x*2.5,140-lum*68,1,1);
      const u=(-.14713*bytes[p]-.28886*bytes[p+1]+.436*bytes[p+2])/255;
      const v=(.615*bytes[p]-.51499*bytes[p+1]-.10001*bytes[p+2])/255;
      ctx.fillStyle='rgba(190,242,110,.23)';ctx.fillRect(120+u*112,175-v*50,1,1);
    }
    ctx.fillStyle='#b9c9b5';ctx.font='10px monospace';
    ctx.fillText('RGB PARADE',5,10);ctx.fillText('LUMA WAVEFORM',5,80);ctx.fillText('VECTORSCOPE',5,150);
  }
  window.SpliceInspector = {render,paintScope,paintMeters};
})();
