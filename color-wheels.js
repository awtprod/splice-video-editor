/* The grading controls of the inspector's Color section: lift/gamma/gain/offset wheels with luma
 * sliders and per-channel numbers, contrast pivot, master and R/G/B curves, hue vs saturation,
 * clip bypass, copy/paste/apply-to-source/reset of a grade, export of the grade as a .cube file,
 * and the source's input interpretation (untagged matrix, technical input LUT).
 *
 * Values are written to clip.color.grade (validated by color-grade.js and color_grade.py). A drag
 * is one undo step (Editor.beginGesture/endGesture). Keys a control uses never reach the timeline.
 */
(() => {
  const G = window.SpliceGrade;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const WHEEL_NAMES = {lift:'Lift', gamma:'Gamma', gain:'Gain', offset:'Offset'};
  // Full scale of a wheel's puck, as a Cb/Cr offset (the vectorscope's axes, so the puck moves the
  // way the trace moves on the scope).
  const WHEEL_RANGE = .3;
  const CURVE_TABS = {master:'Master', red:'Red', green:'Green', blue:'Blue', hueSat:'Hue vs sat'};
  let curveTab = 'master', selectedPoint = 0, clipboard = null;
  const defaults = name => { const d = G.wheels[name][2]; return [d, d, d, d]; };
  const round = value => Math.round(value*10000)/10000;

  // Channel values -> puck (Cb, Cr) and back, around the wheel's neutral value.
  function puckOf(name, values) {
    const d = G.wheels[name][2], r = values[0]-d, g = values[1]-d, b = values[2]-d, y = .2126*r+.7152*g+.0722*b;
    return {cb:(b-y)/1.8556, cr:(r-y)/1.5748};
  }
  function channelsOf(name, cb, cr, master) {
    const d = G.wheels[name][2], [low, high] = G.wheels[name];
    const clampTo = value => Math.max(low, Math.min(high, value));
    return [clampTo(d+1.5748*cr), clampTo(d-.1873*cb-.4681*cr), clampTo(d+1.8556*cb), master];
  }
  function markup(clip, source) {
    const grade = clip.color?.grade || {};
    const wheel = name => {
      const values = grade[name] || defaults(name), [low, high] = G.wheels[name];
      const step = name === 'gain' ? .01 : .005;
      return `<div class="grade-wheel" data-wheel="${name}">` +
        `<div class="grade-wheel-head"><span>${WHEEL_NAMES[name]}</span><button type="button" class="grade-mini" data-wheel-reset="${name}" title="Reset ${WHEEL_NAMES[name]}" aria-label="Reset ${WHEEL_NAMES[name]}">↺</button></div>` +
        `<canvas class="grade-wheel-pad" width="148" height="148" tabindex="0" role="slider" aria-label="${WHEEL_NAMES[name]} colour balance" aria-valuetext="${values.slice(0,3).map(value => value.toFixed(3)).join(', ')}" title="Drag to balance colour. Arrow keys nudge (Shift: more). Double-click resets."></canvas>` +
        `<input type="range" class="grade-wheel-master" data-wheel-master="${name}" aria-label="${WHEEL_NAMES[name]} luma" min="${low}" max="${high}" step="${step}" value="${values[3]}">` +
        `<div class="grade-wheel-numbers">${['R','G','B','Y'].map((label, channel) =>
          `<label><span>${label}</span><input type="number" data-wheel-number="${name}" data-channel="${channel}" aria-label="${WHEEL_NAMES[name]} ${label}" min="${low}" max="${high}" step="${step}" value="${round(values[channel])}"></label>`).join('')}</div></div>`;
    };
    const curvePoints = curveTab === 'hueSat' ? grade.hueSat || [] : grade.curves?.[curveTab] || [[0, 0], [1, 1]];
    const point = curvePoints[Math.min(selectedPoint, curvePoints.length-1)];
    const yMax = curveTab === 'hueSat' ? 2 : 1;
    const matrixTag = source?.colorSpace;
    const untagged = source && source.type === 'video' && !source.still && !['bt709','smpte170m','bt470bg','bt2020nc','bt2020c','fcc','smpte240m'].includes(matrixTag);
    return `<div class="grade-tools" data-grade-tools>` +
      `<div class="grade-actions">` +
        `<label class="splice-safe-toggle"><input type="checkbox" data-grade-bypass aria-label="Bypass this clip's grade" ${grade.bypass ? 'checked' : ''}> Bypass grade</label>` +
        `<button type="button" data-grade-action="copy" title="Copy this clip's grade">Copy</button>` +
        `<button type="button" data-grade-action="paste" ${clipboard ? '' : 'disabled'} title="Paste the copied grade onto this clip">Paste</button>` +
        `<button type="button" data-grade-action="source" title="Give every clip of this source this grade">Apply to source</button>` +
        `<button type="button" data-grade-action="reset" title="Reset every colour control of this clip">Reset</button>` +
        `<button type="button" data-grade-action="cube" title="Save this clip's whole grade as a 33-point .cube LUT">Export .cube</button>` +
      `</div>` +
      `<label class="splice-control"><span>Contrast pivot<output data-grade-out="pivot">${(grade.pivot ?? .5).toFixed(2)}</output></span><input type="range" data-grade-pivot aria-label="Contrast pivot" min=".05" max=".95" step=".01" value="${grade.pivot ?? .5}"></label>` +
      `<div class="grade-wheels">${Object.keys(WHEEL_NAMES).map(wheel).join('')}</div>` +
      `<div class="grade-curves">` +
        `<div class="grade-curve-tabs" role="tablist" aria-label="Curve">${Object.entries(CURVE_TABS).map(([key, title]) => `<button type="button" role="tab" data-curve-tab="${key}" aria-selected="${key === curveTab}">${title}</button>`).join('')}</div>` +
        `<canvas class="grade-curve" width="560" height="300" tabindex="0" role="application" aria-label="${CURVE_TABS[curveTab]} curve: click to add a point, drag to move, Delete removes the selected point" title="Click to add a point. Drag to move. Double-click or Delete removes a point."></canvas>` +
        `<div class="grade-curve-point">` +
          `<label><span>${curveTab === 'hueSat' ? 'Hue' : 'In'}</span><input type="number" data-curve-x aria-label="Selected point input" min="0" max="${curveTab === 'hueSat' ? .999 : 1}" step=".001" value="${point ? round(point[0]) : 0}" ${point ? '' : 'disabled'}></label>` +
          `<label><span>${curveTab === 'hueSat' ? 'Sat' : 'Out'}</span><input type="number" data-curve-y aria-label="Selected point output" min="0" max="${yMax}" step=".001" value="${point ? round(point[1]) : 0}" ${point ? '' : 'disabled'}></label>` +
          `<button type="button" data-curve-reset title="Reset this curve">Reset curve</button>` +
        `</div>` +
      `</div>` +
      (source && source.type === 'video' ? `<div class="grade-source"><p class="grade-source-title"><small>Source</small> ${esc(source.name)}</p>` +
        (untagged ? `<label class="splice-number"><span>Untagged matrix</span><select data-source-matrix aria-label="Read this untagged source as">` +
          `<option value="" ${!source.colorMatrix ? 'selected' : ''}>Rec. 601 (default)</option><option value="bt709" ${source.colorMatrix === 'bt709' ? 'selected' : ''}>Rec. 709</option><option value="bt601" ${source.colorMatrix === 'bt601' ? 'selected' : ''}>Rec. 601</option></select></label>` : '') +
        `<p class="splice-lut-name" title="${esc(source.inputLut?.title || 'No input LUT')}"><small>Input LUT</small> ${esc(source.inputLut?.title || 'None')}</p>` +
        `<div class="splice-lut-row"><button type="button" data-action="import-input-lut" title="A technical LUT (camera log to Rec. 709) for every clip of this source, applied before the grade">${source.inputLut ? 'Replace input LUT' : 'Import input LUT'}</button>${source.inputLut ? '<button type="button" data-action="remove-input-lut">Remove</button>' : ''}</div>` +
        `<input type="file" class="splice-input-lut-file" accept=".cube" hidden></div>` : '') +
      `</div>`;
  }

  function drawWheel(canvas, name, values) {
    const ctx = canvas.getContext('2d'), size = canvas.width, c = size/2, r = c-6;
    ctx.clearRect(0, 0, size, size);
    // Hue ring at the angle each colour takes on the vectorscope (Cb right, Cr up).
    for (let a = 0; a < 360; a += 3) {
      const t = a*Math.PI/180, cb = Math.cos(t), cr = Math.sin(t);
      const rgb = [.5+.6*1.5748*cr, .5-.6*(.1873*cb+.4681*cr), .5+.6*1.8556*cb].map(v => Math.round(Math.max(0, Math.min(1, v))*255));
      ctx.strokeStyle = `rgb(${rgb})`; ctx.lineWidth = 5;
      ctx.beginPath(); ctx.arc(c, c, r, -t-.03, -t+.06); ctx.stroke();
    }
    ctx.strokeStyle = 'rgba(220,230,220,.25)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(c-r, c); ctx.lineTo(c+r, c); ctx.moveTo(c, c-r); ctx.lineTo(c, c+r); ctx.stroke();
    ctx.beginPath(); ctx.arc(c, c, r/2, 0, Math.PI*2); ctx.stroke();
    const {cb, cr} = puckOf(name, values), x = c+cb/WHEEL_RANGE*(r-8), y = c-cr/WHEEL_RANGE*(r-8);
    ctx.fillStyle = '#e8efe6'; ctx.strokeStyle = '#101410'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(Math.max(8, Math.min(size-8, x)), Math.max(8, Math.min(size-8, y)), 6, 0, Math.PI*2); ctx.fill(); ctx.stroke();
  }
  function drawCurve(canvas, grade) {
    const ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height, pad = 10;
    const hue = curveTab === 'hueSat', yMax = hue ? 2 : 1;
    const X = x => pad+x*(w-2*pad), Y = y => h-pad-y/yMax*(h-2*pad);
    ctx.fillStyle = '#0d110d'; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = 'rgba(200,215,200,.14)'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) { ctx.beginPath(); ctx.moveTo(X(i/4), Y(0)); ctx.lineTo(X(i/4), Y(yMax)); ctx.moveTo(X(0), Y(i/4*yMax)); ctx.lineTo(X(1), Y(i/4*yMax)); ctx.stroke(); }
    const colour = {master:'#e8efe6', red:'#ff6a5c', green:'#62d36a', blue:'#6a9cff', hueSat:'#e0a44a'}[curveTab];
    const points = hue ? grade.hueSat || [] : grade.curves?.[curveTab] || [[0, 0], [1, 1]];
    ctx.strokeStyle = colour; ctx.lineWidth = 2; ctx.beginPath();
    if (hue) {
      // The same cyclic interpolation the grade applies.
      const fn = G.hueSatFunction(points) || (() => 1);
      for (let i = 0; i <= 200; i++) {
        const t = i/200, value = fn(t);
        if (i) ctx.lineTo(X(t), Y(value)); else ctx.moveTo(X(t), Y(value));
      }
    } else {
      const fn = G.curveFunction(points) || (v => v);
      for (let i = 0; i <= 200; i++) { const t = i/200; if (i) ctx.lineTo(X(t), Y(fn(t))); else ctx.moveTo(X(t), Y(fn(t))); }
    }
    ctx.stroke();
    points.forEach((point, index) => {
      ctx.fillStyle = index === selectedPoint ? colour : '#101410'; ctx.strokeStyle = colour; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(X(point[0]), Y(point[1]), 6, 0, Math.PI*2); ctx.fill(); ctx.stroke();
    });
    return {X, Y, pad, yMax};
  }
  // ---- Editing
  function bind(element, clip, editor, rerender) {
    const root = element.querySelector('[data-grade-tools]');
    if (!root) return;
    const current = () => editor.get().project.clips.find(item => item.id === clip.id) || clip;
    const gradeOf = () => ({...(current().color?.grade || {})});
    const write = (grade, keep = false) => {
      const active = current(), clean = {...grade};
      for (const key of Object.keys(clean)) if (clean[key] === undefined) delete clean[key];
      G.validateGrade(clean);
      editor.updateClip(active.id, {color:{...(active.color || {}), grade:Object.keys(clean).length ? clean : undefined}});
      if (!keep) rerender();
    };
    const guarded = action => (...args) => { try { return action(...args); } catch (error) { window.alert(error.message || 'This grade cannot be applied.'); rerender(); } };
    // Keys handled here stay here: number fields already keep them; the pads say so explicitly.
    for (const pad of root.querySelectorAll('canvas[tabindex]')) pad.addEventListener('keydown', event => {
      if (/^(Arrow|Delete$|Backspace$|Home$|End$)/.test(event.key)) event.stopPropagation();
    });
    // Wheels
    for (const box of root.querySelectorAll('[data-wheel]')) {
      const name = box.dataset.wheel, pad = box.querySelector('.grade-wheel-pad');
      const values = () => gradeOf()[name] || defaults(name);
      drawWheel(pad, name, values());
      const setPuck = (cb, cr, keep) => {
        const v = values(), grade = gradeOf();
        grade[name] = channelsOf(name, cb, cr, v[3]).map(round);
        write(grade, keep);
        drawWheel(pad, name, grade[name]);
        box.querySelectorAll('[data-wheel-number]').forEach((input, channel) => { input.value = String(round(grade[name][channel])); });
      };
      const fromPointer = event => {
        const rect = pad.getBoundingClientRect(), half = rect.width/2, r = half-6-8*rect.width/pad.width;
        let cb = (event.clientX-rect.left-half)/r*WHEEL_RANGE, cr = -(event.clientY-rect.top-half)/r*WHEEL_RANGE;
        const m = Math.hypot(cb, cr); if (m > WHEEL_RANGE) { cb *= WHEEL_RANGE/m; cr *= WHEEL_RANGE/m; }
        return {cb, cr};
      };
      pad.addEventListener('pointerdown', guarded(event => {
        pad.setPointerCapture(event.pointerId); pad.focus();
        editor.beginGesture();
        const start = fromPointer(event), origin = puckOf(name, values()), fine = event.shiftKey;
        const move = guarded(moveEvent => {
          const at = fromPointer(moveEvent), scale = fine || moveEvent.shiftKey ? .25 : 1;
          setPuck(origin.cb+(at.cb-start.cb)*scale, origin.cr+(at.cr-start.cr)*scale, true);
        });
        const up = () => { pad.removeEventListener('pointermove', move); pad.removeEventListener('pointerup', up); pad.removeEventListener('pointercancel', up); editor.endGesture(); rerender(); };
        pad.addEventListener('pointermove', move); pad.addEventListener('pointerup', up); pad.addEventListener('pointercancel', up);
      }));
      pad.addEventListener('dblclick', guarded(() => { const grade = gradeOf(); delete grade[name]; write(grade); }));
      pad.addEventListener('keydown', guarded(event => {
        const step = (event.shiftKey ? .02 : .002), p = puckOf(name, values());
        const moves = {ArrowLeft:[-step, 0], ArrowRight:[step, 0], ArrowUp:[0, step], ArrowDown:[0, -step]}[event.key];
        if (!moves) return;
        event.preventDefault();
        setPuck(p.cb+moves[0], p.cr+moves[1], false);
      }));
      box.querySelector('[data-wheel-master]').addEventListener('input', guarded(event => {
        const grade = gradeOf(), v = [...values()]; v[3] = Number(event.target.value); grade[name] = v; write(grade, true);
        box.querySelector(`[data-wheel-number][data-channel="3"]`).value = String(v[3]);
      }));
      box.querySelector('[data-wheel-master]').addEventListener('pointerdown', () => editor.beginGesture());
      box.querySelector('[data-wheel-master]').addEventListener('change', () => { editor.endGesture(); rerender(); });
      for (const input of box.querySelectorAll('[data-wheel-number]')) input.addEventListener('change', guarded(() => {
        const value = Number(input.value), [low, high] = G.wheels[name];
        if (!Number.isFinite(value) || value < low || value > high) { input.setCustomValidity(`Enter ${low} to ${high}.`); input.reportValidity(); return; }
        input.setCustomValidity('');
        const grade = gradeOf(), v = [...values()]; v[Number(input.dataset.channel)] = value; grade[name] = v; write(grade);
      }));
      box.querySelector('[data-wheel-reset]').addEventListener('click', guarded(() => { const grade = gradeOf(); delete grade[name]; write(grade); }));
    }
    // Pivot
    const pivot = root.querySelector('[data-grade-pivot]');
    pivot.addEventListener('pointerdown', () => editor.beginGesture());
    pivot.addEventListener('input', guarded(() => { const grade = gradeOf(); grade.pivot = Number(pivot.value); write(grade, true); root.querySelector('[data-grade-out="pivot"]').textContent = Number(pivot.value).toFixed(2); }));
    pivot.addEventListener('change', () => { editor.endGesture(); rerender(); });
    // Curves
    const canvas = root.querySelector('.grade-curve');
    const pointsOf = grade => curveTab === 'hueSat' ? [...(grade.hueSat || [])].map(p => [...p]) : [...(grade.curves?.[curveTab] || [[0, 0], [1, 1]])].map(p => [...p]);
    const store = (grade, points, keep) => {
      if (curveTab === 'hueSat') grade.hueSat = points.length ? points : undefined;
      else grade.curves = {...(grade.curves || {}), [curveTab]:points};
      write(grade, keep);
    };
    let geometry = drawCurve(canvas, gradeOf());
    const toValue = event => {
      const rect = canvas.getBoundingClientRect(), sx = canvas.width/rect.width, sy = canvas.height/rect.height;
      const x = ((event.clientX-rect.left)*sx-geometry.pad)/(canvas.width-2*geometry.pad), y = (canvas.height-geometry.pad-(event.clientY-rect.top)*sy)/(canvas.height-2*geometry.pad)*geometry.yMax;
      return [Math.max(0, Math.min(curveTab === 'hueSat' ? .999 : 1, x)), Math.max(0, Math.min(geometry.yMax, y))];
    };
    const nearest = (points, at) => {
      const rect = canvas.getBoundingClientRect();
      let best = -1, distance = 12*canvas.width/rect.width;
      points.forEach((point, index) => {
        const d = Math.hypot(geometry.X(point[0])-geometry.X(at[0]), geometry.Y(point[1])-geometry.Y(at[1]));
        if (d < distance) { distance = d; best = index; }
      });
      return best;
    };
    // Moving a point keeps the points in order: it stops just short of its neighbours.
    const place = (points, index, [x, y]) => {
      const gap = .01, low = index ? points[index-1][0]+gap : 0, high = index < points.length-1 ? points[index+1][0]-gap : curveTab === 'hueSat' ? .999 : 1;
      points[index] = [round(Math.max(low, Math.min(high, x))), round(y)];
    };
    canvas.addEventListener('pointerdown', guarded(event => {
      const grade = gradeOf(), points = pointsOf(grade), at = toValue(event);
      let index = nearest(points, at);
      if (index < 0) {
        if (points.length >= G.limits.points) return;
        if (points.some(point => Math.abs(point[0]-at[0]) < .01)) return;
        points.push(at.map(round)); points.sort((a, b) => a[0]-b[0]); index = points.findIndex(point => point[0] === round(at[0]));
      }
      selectedPoint = index;
      canvas.setPointerCapture(event.pointerId); canvas.focus();
      editor.beginGesture();
      store(grade, points, true); geometry = drawCurve(canvas, gradeOf());
      const move = guarded(moveEvent => {
        const next = gradeOf(), list = pointsOf(next);
        place(list, selectedPoint, toValue(moveEvent));
        store(next, list, true); geometry = drawCurve(canvas, gradeOf());
      });
      const up = () => { canvas.removeEventListener('pointermove', move); canvas.removeEventListener('pointerup', up); canvas.removeEventListener('pointercancel', up); editor.endGesture(); rerender(); };
      canvas.addEventListener('pointermove', move); canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
    }));
    const removeSelected = guarded(() => {
      const grade = gradeOf(), points = pointsOf(grade);
      if (curveTab !== 'hueSat' && points.length <= 2) return;
      points.splice(selectedPoint, 1); selectedPoint = Math.max(0, selectedPoint-1);
      store(grade, points);
    });
    canvas.addEventListener('dblclick', removeSelected);
    canvas.addEventListener('keydown', guarded(event => {
      const grade = gradeOf(), points = pointsOf(grade);
      if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); removeSelected(); return; }
      if (event.key === 'Home' || event.key === 'End') { event.preventDefault(); selectedPoint = event.key === 'Home' ? 0 : points.length-1; rerender(); return; }
      const step = event.shiftKey ? .05 : .005, moves = {ArrowLeft:[-step, 0], ArrowRight:[step, 0], ArrowUp:[0, step], ArrowDown:[0, -step]}[event.key];
      if (!moves || !points[selectedPoint]) return;
      event.preventDefault();
      place(points, selectedPoint, [points[selectedPoint][0]+moves[0], Math.max(0, Math.min(geometry.yMax, points[selectedPoint][1]+moves[1]))]);
      store(grade, points);
    }));
    for (const tab of root.querySelectorAll('[data-curve-tab]')) tab.addEventListener('click', () => { curveTab = tab.dataset.curveTab; selectedPoint = 0; rerender(); });
    for (const [selector, axis] of [['[data-curve-x]', 0], ['[data-curve-y]', 1]]) root.querySelector(selector).addEventListener('change', guarded(event => {
      const grade = gradeOf(), points = pointsOf(grade), value = Number(event.target.value);
      if (!points[selectedPoint] || !Number.isFinite(value)) return;
      const next = [...points[selectedPoint]]; next[axis] = value;
      place(points, selectedPoint, next); store(grade, points);
    }));
    root.querySelector('[data-curve-reset]').addEventListener('click', guarded(() => {
      const grade = gradeOf();
      if (curveTab === 'hueSat') delete grade.hueSat;
      else { const curves = {...(grade.curves || {})}; delete curves[curveTab]; grade.curves = Object.keys(curves).length ? curves : undefined; }
      selectedPoint = 0; write(grade);
    }));
    // Clip-level actions
    root.querySelector('[data-grade-bypass]').addEventListener('change', guarded(event => { const grade = gradeOf(); grade.bypass = event.target.checked || undefined; write(grade); }));
    const colorKeys = ['brightness', 'contrast', 'saturation', 'exposure', 'temperature', 'tint', 'lut', 'lutIntensity', 'grade'];
    const actions = {
      copy() { const color = current().color || {}; clipboard = Object.fromEntries(colorKeys.filter(key => color[key] !== undefined).map(key => [key, color[key]])); rerender(); },
      paste() { if (clipboard) editor.updateClip(current().id, {color:{...structuredCloneGrade(clipboard)}}); rerender(); },
      source() {
        const active = current(), color = active.color || {};
        const targets = editor.get().project.clips.filter(item => item.type === 'video' && item.mediaId === active.mediaId && item.id !== active.id);
        if (!targets.length) return window.alert('No other clip uses this source.');
        editor.editProject(project => { for (const target of project.clips) if (targets.some(item => item.id === target.id)) target.color = structuredCloneGrade(color); });
        rerender();
      },
      reset() { editor.updateClip(current().id, {color:{brightness:100, contrast:100, saturation:100}}); rerender(); },
      cube() {
        const active = current(), text = G.cubeText(active.color || {}, 33, `${active.title || 'Splice'} grade`);
        const link = document.createElement('a'), url = URL.createObjectURL(new Blob([text], {type:'text/plain'}));
        link.href = url; link.download = `${(active.title || 'clip').replace(/[^\w.-]+/g, '_').slice(0, 60) || 'clip'}-grade.cube`;
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      },
    };
    for (const button of root.querySelectorAll('[data-grade-action]')) button.addEventListener('click', guarded(() => actions[button.dataset.gradeAction]()));
    // Source settings
    const updateMedia = patch => editor.editProject(project => { const item = project.media.find(entry => entry.id === current().mediaId); for (const [key, value] of Object.entries(patch)) { if (value === undefined) delete item[key]; else item[key] = value; } });
    root.querySelector('[data-source-matrix]')?.addEventListener('change', guarded(event => { updateMedia({colorMatrix:event.target.value || undefined}); rerender(); }));
    const file = root.querySelector('.splice-input-lut-file');
    root.querySelector('[data-action="import-input-lut"]')?.addEventListener('click', () => file.click());
    root.querySelector('[data-action="remove-input-lut"]')?.addEventListener('click', guarded(() => { updateMedia({inputLut:undefined}); rerender(); }));
    file?.addEventListener('change', guarded(async () => {
      const chosen = file.files?.[0];
      if (!chosen) return;
      try {
        if (chosen.size > window.SpliceLUT.limits.chars) throw new Error('The LUT file is too large.');
        const lut = window.SpliceLUT.parseCube(await chosen.text(), chosen.name);
        updateMedia({inputLut:lut}); rerender();
      } catch (error) { window.alert(error.message || 'Cannot read this LUT.'); }
    }));
  }
  // A colour state copied between clips: the LUT table is shared (never mutated), the rest copied.
  function structuredCloneGrade(color) {
    const {lut, ...rest} = color;
    return {...structuredClone(rest), ...(lut !== undefined ? {lut} : {})};
  }
  // An adjustment clip: a transparent generator (so other tools and older Splice builds treat it as
  // empty picture) marked `adjustment`, whose clip grade applies to the composite beneath it.
  function addAdjustmentClip(editor = window.Editor, length = 5) {
    const state = editor.get(), at = state.time, id = 'adjustment-'+Math.random().toString(36).slice(2, 10);
    const busy = state.project.clips.filter(clip => clip.type !== 'audio' && clip.start < at+length && at < clip.start+clip.duration);
    const track = Math.max(0, ...busy.map(clip => Number(clip.track) || 0))+1;
    if (track > 64) throw new Error('No free video track above this point.');
    editor.editProject(project => {
      project.media.push({id:id+'-media', type:'video', name:'Adjustment clip', src:'', duration:48*3600, nativeGenerator:1414680150, adjustment:true});
      project.clips.push({id, type:'video', mediaId:id+'-media', track, start:at, duration:length, in:0, title:'Adjustment',
        embeddedAudio:false, volume:100, transition:'none', color:{brightness:100, contrast:100, saturation:100}});
    });
    editor.select?.(id);
    return id;
  }
  // The Color page gives the inspector the width the wheels and curves need.
  document.addEventListener('click', event => {
    const tab = event.target.closest?.('[data-workspace]');
    if (tab) document.querySelector('.workspace')?.classList.toggle('color-page', tab.dataset.workspace === 'color');
  });
  function mountMenu() {
    const button = document.getElementById('new-adjustment-clip');
    if (!button || button.dataset.bound) return;
    button.dataset.bound = '1';
    button.addEventListener('click', () => {
      document.querySelector('.edit-menu')?.removeAttribute('open');
      try { addAdjustmentClip(); } catch (error) { window.alert(error.message || 'Cannot add an adjustment clip here.'); }
    });
  }
  document.addEventListener('DOMContentLoaded', mountMenu);
  if (document.readyState !== 'loading') setTimeout(mountMenu);
  window.SpliceColorUI = {markup, bind, puckOf, channelsOf, addAdjustmentClip, clipboard:() => clipboard};
})();
