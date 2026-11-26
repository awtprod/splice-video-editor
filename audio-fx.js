// Audio effect controls (clip, track and master): parametric EQ with its response curve,
// compressor, gate and de-noise. Values are committed on `change`, so typing a number is one
// undo step and never reaches the timeline's shortcuts (number fields keep their keys).
(() => {
  const dsp = window.SpliceAudioDSP;
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const TYPE_NAMES = {highpass:'High-pass', lowpass:'Low-pass', lowshelf:'Low shelf', highshelf:'High shelf', peak:'Bell'};
  // Presets for common dialogue jobs. They replace the EQ bands.
  const PRESETS = {
    dialogue: {label:'Dialogue clean-up', bands:[{type:'highpass',freq:80,q:.7071,gain:0},{type:'peak',freq:250,q:1.2,gain:-2},{type:'peak',freq:4000,q:1,gain:2}]},
    deess: {label:'De-ess band cut', bands:[{type:'peak',freq:6500,q:3,gain:-5}]},
    duck: {label:'Music under dialogue', bands:[{type:'peak',freq:2500,q:1.2,gain:-4},{type:'highpass',freq:40,q:.7071,gain:0}]},
  };
  const FIELDS = {
    comp: [['threshold','Threshold','dBFS',.5],['ratio','Ratio',':1',.1],['attack','Attack','ms',.1],['release','Release','ms',1],
      ['knee','Knee','dB',.5],['makeup','Make-up','dB',.5]],
    gate: [['threshold','Threshold','dBFS',.5],['range','Range','dB',1],['ratio','Ratio',':1',.5],['attack','Attack','ms',.1],['release','Release','ms',1]],
    denoise: [['amount','Reduction','dB',.5],['floor','Noise floor','dBFS',1]],
  };
  const TITLES = {comp:'Compressor', gate:'Gate / expander', denoise:'De-noise'};
  function defaults(kind) {
    const values = {enabled:true};
    for (const [key,[, , fallback]] of Object.entries(dsp.BOUNDS[kind])) values[key] = fallback;
    return values;
  }
  function number(kind, key, label, unit, step, value, owner) {
    const [low, high] = dsp.BOUNDS[kind][key];
    const id = `fx-${owner}-${kind}-${key}`.replace(/[^\w-]/g, '_');
    return `<label class="fx-field" for="${id}"><span>${label}</span><span class="fx-input"><input id="${id}" type="number" data-kind="${kind}" data-key="${key}" min="${low}" max="${+high.toFixed(2)}" step="${step}" value="${value}" aria-label="${label} (${unit})"><span class="fx-unit">${unit}</span></span></label>`;
  }
  function render(container, fx, options = {}) {
    const {kinds = ['eq','comp','gate','denoise'], owner = 'clip', onChange, disabled = false} = options;
    fx = fx || {};
    const eq = fx.eq || {enabled:true, bands:[]};
    let html = '';
    if (kinds.includes('eq')) {
      const bands = eq.bands || [];
      html += `<fieldset class="fx-block" data-block="eq"><legend><label><input type="checkbox" data-toggle="eq" ${fx.eq && eq.enabled !== false ? 'checked' : ''}> Parametric EQ</label></legend>` +
        `<canvas class="fx-curve" width="280" height="84" role="img" aria-label="EQ response, 20 Hz to 20 kHz, ±24 dB"></canvas>` +
        bands.map((band, index) => `<div class="fx-band" data-band="${index}">` +
          `<select data-band-field="type" aria-label="Band ${index+1} type">${Object.entries(TYPE_NAMES).map(([key,name])=>`<option value="${key}" ${band.type===key?'selected':''}>${name}</option>`).join('')}</select>` +
          `<label><span class="fx-input"><input type="number" data-band-field="freq" min="20" max="20000" step="1" value="${band.freq}" aria-label="Band ${index+1} frequency (Hz)"><span class="fx-unit">Hz</span></span></label>` +
          `<label><span class="fx-input"><input type="number" data-band-field="gain" min="-24" max="24" step=".5" value="${band.gain ?? 0}" aria-label="Band ${index+1} gain (dB)" ${band.type==='highpass'||band.type==='lowpass'?'disabled':''}><span class="fx-unit">dB</span></span></label>` +
          `<label><span class="fx-input"><input type="number" data-band-field="q" min="0.1" max="18" step=".05" value="${band.q ?? .7071}" aria-label="Band ${index+1} Q"><span class="fx-unit">Q</span></span></label>` +
          `<button type="button" data-band-remove aria-label="Remove band ${index+1}" title="Remove band">×</button></div>`).join('') +
        `<div class="fx-row"><button type="button" data-band-add ${bands.length >= 8 ? 'disabled' : ''}>Add band</button>` +
        `<select data-preset aria-label="EQ preset"><option value="">Preset…</option>${Object.entries(PRESETS).map(([key,preset])=>`<option value="${key}">${preset.label}</option>`).join('')}</select></div></fieldset>`;
    }
    for (const kind of ['gate','comp','denoise']) {
      if (!kinds.includes(kind)) continue;
      const values = {...defaults(kind), ...(fx[kind] || {})};
      html += `<fieldset class="fx-block" data-block="${kind}"><legend><label><input type="checkbox" data-toggle="${kind}" ${fx[kind] && values.enabled !== false ? 'checked' : ''}> ${TITLES[kind]}</label></legend>` +
        `<div class="fx-grid">${FIELDS[kind].map(([key,label,unit,step]) => number(kind, key, label, unit, step, values[key], owner)).join('')}</div>` +
        (kind === 'denoise' ? '<p class="fx-note">Set the noise floor to the level of steady background hiss.</p>' :
          `<div class="fx-gr" data-gr="${kind}" role="meter" aria-label="${TITLES[kind]} gain reduction" aria-valuemin="0" aria-valuemax="24" aria-valuenow="0"><span></span><output>0.0 dB</output></div>`) +
        '</fieldset>';
    }
    container.innerHTML = html;
    if (disabled) for (const input of container.querySelectorAll('input,select,button')) input.disabled = true;
    drawCurve(container.querySelector('.fx-curve'), fx.eq);
    const commit = next => { try { onChange?.(next); } catch (error) { window.alert(error.message || 'These settings are not valid.'); render(container, fx, options); } };
    const current = () => structuredClone(fx);
    container.querySelectorAll('[data-toggle]').forEach(box => box.onchange = () => {
      const next = current(), kind = box.dataset.toggle;
      if (kind === 'eq') next.eq = {...(next.eq || {bands: []}), enabled: box.checked};
      else next[kind] = {...defaults(kind), ...(next[kind] || {}), enabled: box.checked};
      commit(next);
    });
    container.querySelectorAll('input[data-kind]').forEach(input => input.onchange = () => {
      const value = Number(input.value), next = current(), kind = input.dataset.kind;
      if (!Number.isFinite(value) || value < Number(input.min) || value > Number(input.max)) {
        input.setCustomValidity(`Use ${input.min} to ${input.max}.`); input.reportValidity(); return;
      }
      input.setCustomValidity('');
      next[kind] = {...defaults(kind), ...(next[kind] || {}), [input.dataset.key]: value};
      commit(next);
    });
    container.querySelectorAll('.fx-band').forEach(row => {
      const index = Number(row.dataset.band);
      row.querySelectorAll('[data-band-field]').forEach(input => input.onchange = () => {
        const next = current(), band = next.eq.bands[index], key = input.dataset.bandField;
        if (key === 'type') band.type = input.value;
        else {
          const value = Number(input.value);
          if (!Number.isFinite(value) || value < Number(input.min) || value > Number(input.max)) {
            input.setCustomValidity(`Use ${input.min} to ${input.max}.`); input.reportValidity(); return;
          }
          band[key] = value;
        }
        commit(next);
      });
      row.querySelector('[data-band-remove]').onclick = () => { const next = current(); next.eq.bands.splice(index, 1); commit(next); };
    });
    const add = container.querySelector('[data-band-add]');
    if (add) add.onclick = () => {
      const next = current();
      next.eq = {enabled: true, ...(next.eq || {}), bands: [...(next.eq?.bands || []), {type:'peak', freq:1000, q:1, gain:0}]};
      commit(next);
    };
    const preset = container.querySelector('[data-preset]');
    if (preset) preset.onchange = () => {
      if (!PRESETS[preset.value]) return;
      const next = current();
      next.eq = {enabled: true, bands: structuredClone(PRESETS[preset.value].bands)};
      commit(next);
    };
  }
  // The EQ's magnitude response from the same coefficients the preview and the export use.
  function drawCurve(canvas, eq) {
    if (!canvas) return;
    const g = canvas.getContext('2d'), {width, height} = canvas;
    g.clearRect(0, 0, width, height);
    const style = getComputedStyle(canvas);
    g.strokeStyle = style.getPropertyValue('--fx-grid') || 'rgba(255,255,255,.12)';
    g.fillStyle = style.getPropertyValue('--fx-label') || 'rgba(255,255,255,.55)';
    g.font = '9px DM Sans, sans-serif';
    const x = f => Math.log10(f/20)/3*width, y = db => height/2 - db/24*(height/2-4);
    for (const f of [50,100,200,500,1000,2000,5000,10000]) { g.beginPath(); g.moveTo(x(f),0); g.lineTo(x(f),height); g.stroke(); }
    for (const db of [-12,0,12]) { g.beginPath(); g.moveTo(0,y(db)); g.lineTo(width,y(db)); g.stroke(); g.fillText(`${db>0?'+':''}${db}`,2,y(db)-2); }
    for (const [f,label] of [[100,'100'],[1000,'1k'],[10000,'10k']]) g.fillText(label, x(f)+2, height-3);
    const sections = eq && eq.enabled !== false ? dsp.eqSections(eq, 48000) : [];
    g.strokeStyle = style.getPropertyValue('--fx-curve') || '#7dd3a7'; g.lineWidth = 2; g.beginPath();
    for (let px = 0; px <= width; px++) {
      const f = 20*10**(3*px/width), db = Math.max(-24, Math.min(24, dsp.magnitudeDb(sections, f, 48000)));
      px ? g.lineTo(px, y(db)) : g.moveTo(px, y(db));
    }
    g.stroke(); g.lineWidth = 1;
  }
  // Gain-reduction meters for whatever effect panel is open (owner: clip:<id>, track:<n>, master).
  function paintReduction(container, owner) {
    const reduction = window.SpliceMixer?.reduction(owner) || {};
    for (const meter of container.querySelectorAll('[data-gr]')) {
      const db = Math.max(0, -(reduction[meter.dataset.gr] ?? 0));
      meter.firstElementChild.style.width = Math.min(100, db/24*100) + '%';
      meter.querySelector('output').textContent = `−${db.toFixed(1)} dB`;
      meter.setAttribute('aria-valuenow', db.toFixed(1));
    }
  }
  window.SpliceAudioFX = {render, paintReduction, PRESETS, esc};
})();
