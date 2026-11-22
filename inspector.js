(() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const label = (title, key, value, min, max, step = 1, suffix = '', group = '') =>
    `<label class="splice-control"><span>${title}<output data-value="${key}">${value}${suffix}</output></span><input aria-label="${title}" type="range" data-field="${key}" data-group="${group}" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
  const field = (title, key, value, min, max, step = .04, group = '') =>
    `<label class="splice-number"><span>${title}</span><input aria-label="${title}" type="number" data-field="${key}" data-group="${group}" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
  const section = (title, body) => `<section class="splice-section"><h3>${title}</h3>${body}</section>`;
  const option = (key, title, selected) => `<option value="${key}" ${key === selected ? 'selected' : ''}>${title}</option>`;
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
    const maximumDuration = source?.duration ? Math.max(1/fps, source.duration - (clip.in || 0)) : 7200;
    let html = `<div class="splice-clip-heading"><small>${escape(clip.type)} · ${clip.track > 0 ? 'V' + clip.track : 'A' + Math.abs(clip.track)}</small><strong>${escape(title)}</strong></div>`;
    html += section('Timing',
      field('Timeline start', 'start', number(clip.start, 0).toFixed(2), 0, 7200, 1/fps) +
      (clip.type === 'title' ? '' : field('Source in', 'in', number(clip.in, 0).toFixed(2), 0, source?.duration || 7200, 1/fps)) +
      field('Duration', 'duration', number(clip.duration, 1).toFixed(2), 1/fps, maximumDuration, 1/fps));
    if (clip.type === 'video') {
      html += section('Transform',
        label('Scale', 'scale', number(transform.scale, 100), 10, 400, 1, '%', 'transform') +
        label('Horizontal', 'x', number(transform.x, 0), -100, 100, 1, '%', 'transform') +
        label('Vertical', 'y', number(transform.y, 0), -100, 100, 1, '%', 'transform') +
        label('Rotation', 'rotation', number(transform.rotation, 0), -180, 180, 1, '°', 'transform'));
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
        `<label class="splice-number"><span>Style</span><select aria-label="Transition style" data-field="transition">${option('none','Cut',clip.transition || 'none')}${option('dissolve','Cross dissolve',clip.transition || 'none')}${option('fade','Fade through black',clip.transition || 'none')}</select></label>` +
        field('Length', 'transitionDuration', number(clip.transitionDuration, .6).toFixed(2), .04, 2, .04));
    }
    if (clip.type === 'title') {
      html += section('Title',
        `<label class="splice-text"><span>Text</span><textarea aria-label="Title text" data-field="title" rows="2" maxlength="500">${escape(clip.title)}</textarea></label>` +
        `<label class="splice-number"><span>Typeface</span><select aria-label="Title typeface" data-field="fontFamily" data-group="titleStyle">${['DM Sans','IBM Plex Mono','DejaVu Sans'].map(name => option(name,name,text.fontFamily || 'DM Sans')).join('')}</select></label>` +
        label('Size', 'fontSize', number(text.fontSize, 8), 2, 20, .5, '%', 'titleStyle') +
        label('Horizontal', 'x', number(text.x, 50), 0, 100, 1, '%', 'titleStyle') +
        label('Vertical', 'y', number(text.y, 78), 0, 100, 1, '%', 'titleStyle') +
        `<label class="splice-number"><span>Alignment</span><select aria-label="Title alignment" data-field="align" data-group="titleStyle">${['left','center','right'].map(name => option(name,name,text.align || 'center')).join('')}</select></label>` +
        label('Outline', 'strokeWidth', number(text.strokeWidth, 0), 0, 8, .5, ' px', 'titleStyle') +
        `<label class="splice-number"><span>Outline color</span><input aria-label="Title outline color" type="color" data-field="strokeColor" data-group="titleStyle" value="${escape(text.strokeColor || '#000000')}"></label>` +
        `<label class="splice-number"><span>Text color</span><input aria-label="Title text color" type="color" data-field="color" data-group="titleStyle" value="${escape(text.color || '#ffffff')}"></label>`);
    }
    if (clip.type === 'audio' || clip.type === 'video' && clip.embeddedAudio !== false) {
      html += section('Audio',
        label('Gain', 'volume', number(clip.volume, 100), 0, 200, 1, '%') +
        label('Pan', 'pan', number(clip.pan, 0), -100, 100, 1, '', '') +
        field('Fade in', 'fadeIn', number(clip.fadeIn, 0).toFixed(2), 0, clip.duration, .04) +
        field('Fade out', 'fadeOut', number(clip.fadeOut, 0).toFixed(2), 0, clip.duration, .04));
    }
    if (clip.type === 'audio' || clip.type === 'video' && clip.embeddedAudio !== false) {
      const track = clip.type === 'audio' ? clip.track : 1;
      const mix = state.project.trackMix?.[track] || {};
      html += section('Track mixer',
        label('Track gain', 'gain', number(mix.gain, 100), 0, 200, 1, '%', 'trackMix') +
        label('Track pan', 'pan', number(mix.pan, 0), -100, 100, 1, '', 'trackMix') +
        `<div class="splice-toggles"><label><input type="checkbox" data-field="mute" data-group="trackMix" ${mix.mute ? 'checked' : ''}> Mute</label><label><input type="checkbox" data-field="solo" data-group="trackMix" ${mix.solo ? 'checked' : ''}> Solo</label></div>`);
    }
    element.innerHTML = html;
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
        if (group === 'trackMix') {
          const track = active.type === 'audio' ? active.track : 1;
          editor.updateTrackMix(track, {[key]: value});
        } else editor.updateClip(active.id, group ? {[group]: {...(active[group] || {}), [key]: value}} : {[key]: value});
      });
    }
    const picker = element.querySelector('.splice-lut-input');
    element.querySelector('[data-action="import-lut"]')?.addEventListener('click', () => picker.click());
    element.querySelector('[data-action="remove-lut"]')?.addEventListener('click', () => {
      editor.updateClip(clip.id, {color:{...color, lut:null}});
    });
    picker?.addEventListener('change', async () => {
      const file=picker.files?.[0];
      if (!file) return;
      try {
        if (file.size > 4_000_000) throw new Error('The LUT file is too large.');
        const lut=window.SpliceLUT.parseCube(await file.text(),file.name);
        editor.updateClip(clip.id,{color:{...color,lut,lutIntensity:100}});
      } catch(error) { window.alert(error.message || 'Cannot read this LUT.'); }
    });
    paintScope(element.querySelector('.splice-scope'));
  }
  function paintScope(canvas=document.querySelector('.splice-scope')) {
    if (!canvas) return;
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
  window.SpliceInspector = {render,paintScope};
})();
