/* The Effects section of the inspector: speed ramps (presets, knots, ramp sound), keyframe easing
 * with a curve editor, crop, masks (with handles on the program monitor), chroma key (swatch or
 * monitor eyedropper, alpha matte view), blend mode and stabilization.
 *
 * Every change is one undoable edit of clip.effects or clip.keyframes (a drag is one undo step:
 * Editor.beginGesture/endGesture). Mask handles are DOM drawn over the monitor: they are never
 * part of a picture, so they can never reach an export.
 */
(() => {
  const E = window.SpliceEffects;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const notify = message => (window.SpliceNotify || window.alert)(message);
  const open = new Set(['speed']);
  let easingKey = null, easingIndex = 0, editMask = 0, picking = false;
  const INTERP_NAMES = {linear:'Linear', hold:'Hold', easeIn:'Ease in', easeOut:'Ease out', easeInOut:'Ease in and out', bezier:'Custom bezier'};
  const MASK_NAMES = {x:'Centre X', y:'Centre Y', width:'Width', height:'Height', rotation:'Rotation', feather:'Feather', expansion:'Expansion', opacity:'Opacity'};
  const CROP_NAMES = {left:'Left', right:'Right', top:'Top', bottom:'Bottom', feather:'Edge feather'};
  const KEY_NAMES = {similarity:'Similarity', smoothness:'Smoothness', spill:'Spill suppression', choke:'Choke', feather:'Matte feather'};
  const PROPERTY_NAMES = {opacity:'Opacity', x:'Horizontal', y:'Vertical', scale:'Scale', rotation:'Rotation', volume:'Audio gain', speed:'Speed',
    exposure:'Exposure', temperature:'Temperature', tint:'Tint', brightness:'Brightness', contrast:'Contrast', saturation:'Saturation'};
  const propertyName = key => PROPERTY_NAMES[key] || key.replace(/^crop\./, 'Crop ').replace(/^mask\.(\d+)\./, (_, n) => `Mask ${Number(n)+1} `);

  const mediaOf = (state, clip) => state.project.media.find(item => item.id === clip.mediaId);
  const local = (state, clip) => clamp(state.time-clip.start, 0, clip.duration);
  // A property animated by keyframes: the slider edits the key at the playhead (adding one there).
  function valueAt(clip, key, base, time) {
    const points = clip.keyframes?.[key];
    return points?.length ? E.curveValue(points, time, base) : base;
  }
  function setKey(clip, key, time, value, fps) {
    const points = (clip.keyframes?.[key] || []).filter(point => Math.abs(point.time-time) >= .5/fps);
    const near = (clip.keyframes?.[key] || []).find(point => Math.abs(point.time-time) < .5/fps);
    points.push({...(near || {}), time:near ? near.time : Math.round(time*fps)/fps, value});
    points.sort((a, b) => a.time-b.time);
    return {...(clip.keyframes || {}), [key]:points};
  }
  const slider = (label, path, value, low, high, step, suffix, keyed, keyName) =>
    `<label class="splice-control fx-control"><span>${label}<output data-fx-out="${path}">${Math.round(value*100)/100}${suffix}</output>` +
    (keyName ? `<button type="button" class="fx-key ${keyed ? 'on' : ''}" data-fx-key="${keyName}" data-fx-base="${path}" title="${keyed ? 'Add or update a keyframe at the playhead' : 'Animate: add a keyframe at the playhead'}" aria-label="Keyframe ${esc(label)}">◆</button>` +
      (keyed ? `<button type="button" class="fx-key-clear" data-fx-unkey="${keyName}" title="Remove the animation" aria-label="Stop animating ${esc(label)}">✕</button>` : '') : '') +
    `</span><input type="range" aria-label="${esc(label)}" data-fx="${path}" ${keyName ? `data-fx-keyname="${keyName}"` : ''} min="${low}" max="${high}" step="${step}" value="${value}"></label>`;
  // A disclosure group: a button and a body that is hidden (not merely collapsed) while closed.
  const details = (id, title, body, extra = '') => `<div class="fx-group" data-fx-group="${id}" ${open.has(id) ? 'data-open' : ''}>` +
    `<button type="button" class="fx-summary" data-fx-summary aria-expanded="${open.has(id)}" aria-controls="fx-body-${id}">${title}${extra}</button>` +
    `<div class="fx-body" id="fx-body-${id}" ${open.has(id) ? '' : 'hidden'}>${body}</div></div>`;

  // ---------------------------------------------------------------- markup
  function speedMarkup(clip, source, state) {
    if (clip.type === 'title' || source?.still || source?.nativeGenerator || clip.nativeTimeRemap) return '';
    if (window.SpliceMulticam?.isMulticamItem?.(source)) return details('speed', 'Speed ramp', '<p class="splice-handle">Flatten the multicam clip to ramp its speed.</p>');
    const ramp = E.rampPoints(clip), fps = state.project.fps || 25, t = local(state, clip);
    let body = `<div class="fx-presets">${Object.entries(E.PRESETS).map(([id, preset]) => `<button type="button" data-fx-preset="${id}">${esc(preset.name)}</button>`).join('')}</div>`;
    if (clip.reverse) body = '<p class="splice-handle">Speed ramps play forward. Turn off Reverse to ramp this clip.</p>';
    else if (ramp) {
      body += speedCurve(clip, ramp, t) +
        `<div class="fx-knots">${ramp.map((point, index) => `<div class="fx-knot"><button type="button" data-fx-seek="${point.time}" title="Go to this speed keyframe">${point.time.toFixed(2)}s</button>` +
          `<input type="number" aria-label="Speed at ${point.time.toFixed(2)} seconds (%)" data-fx-knot="${index}" min="0" max="1600" step="1" value="${Math.round(point.value*100)/100}">` +
          `<select aria-label="Interpolation after ${point.time.toFixed(2)} seconds" data-fx-knot-interp="${index}">${Object.entries(INTERP_NAMES).map(([key, name]) => `<option value="${key}" ${(point.interp || (point.ease ? 'easeInOut' : 'linear')) === key ? 'selected' : ''}>${name}</option>`).join('')}</select>` +
          `<button type="button" data-fx-knot-delete="${index}" aria-label="Delete speed keyframe at ${point.time.toFixed(2)} seconds" ${ramp.length < 3 ? 'disabled' : ''}>✕</button></div>`).join('')}</div>` +
        `<div class="fx-row"><button type="button" data-fx-knot-add>Add speed key at playhead</button><button type="button" data-fx-ramp-remove>Remove ramp</button></div>` +
        `<p class="splice-handle">Speed now: ${Math.round(E.rampSpeed(ramp, t)*100)}% · plays ${(E.rampSource(ramp, clip.duration)).toFixed(2)} s of source in ${clip.duration.toFixed(2)} s.</p>`;
    } else body += '<p class="splice-handle">Choose a preset, or add a speed key at the playhead to start a ramp. The clip keeps its source In and Out.</p><div class="fx-row"><button type="button" data-fx-knot-add>Add speed key at playhead</button></div>';
    const audio = clip.effects?.speedAudio || 'pitch';
    if (!clip.reverse) body += `<label class="splice-number"><span>Ramp sound</span><select aria-label="Sound of the speed ramp" data-fx-speed-audio>${[['pitch', 'Keep pitch'], ['varispeed', 'Varispeed (pitch follows speed)'], ['mute', 'Mute']].map(([key, name]) => `<option value="${key}" ${audio === key ? 'selected' : ''}>${name}</option>`).join('')}</select></label>`;
    void fps;
    return details('speed', 'Speed ramp', body, ramp ? ' <span class="fx-badge">on</span>' : '');
  }
  function speedCurve(clip, ramp, t) {
    const width = 236, height = 64, top = Math.max(100, ...ramp.map(p => p.value))*1.1;
    const span = Math.max(clip.duration, ramp[ramp.length-1].time);
    const path = [];
    for (let i = 0; i <= 96; i++) { const time = span*i/96; path.push(`${(i/96*width).toFixed(1)},${(height-E.curveValue(ramp, time, 100)/top*height).toFixed(1)}`); }
    const knots = ramp.map(point => `<circle cx="${(point.time/span*width).toFixed(1)}" cy="${(height-point.value/top*height).toFixed(1)}" r="3.5"/>`).join('');
    return `<svg class="fx-speed-curve" viewBox="0 0 ${width} ${height}" role="img" aria-label="Speed over the clip"><line class="fx-unity" x1="0" x2="${width}" y1="${(height-100/top*height).toFixed(1)}" y2="${(height-100/top*height).toFixed(1)}"/>` +
      `<polyline points="${path.join(' ')}"/>${knots}<line class="fx-playhead" x1="${(t/span*width).toFixed(1)}" x2="${(t/span*width).toFixed(1)}" y1="0" y2="${height}"/></svg>`;
  }
  function easingMarkup(clip) {
    const keys = Object.entries(clip.keyframes || {}).filter(([, points]) => points?.length > 1).map(([key]) => key);
    if (!keys.length) return details('easing', 'Keyframe easing', '<p class="splice-handle">Animate a property (its ◆ button, or the Keyframes section) to shape its curve here.</p>');
    if (!keys.includes(easingKey)) easingKey = keys[0];
    const points = E.orderedPoints(clip.keyframes[easingKey]);
    easingIndex = clamp(easingIndex, 0, Math.max(0, points.length-2));
    const a = points[easingIndex], b = points[easingIndex+1];
    const kind = E.segmentKind(a, b), interp = a.interp || (a.ease ? 'easeInOut' : 'linear');
    const body = `<label class="splice-number"><span>Property</span><select aria-label="Property to ease" data-fx-ease-key>${keys.map(key => `<option value="${esc(key)}" ${key === easingKey ? 'selected' : ''}>${esc(propertyName(key))}</option>`).join('')}</select></label>` +
      `<div class="fx-segments" role="list">${points.slice(0, -1).map((point, index) => `<button type="button" role="listitem" data-fx-ease-segment="${index}" aria-pressed="${index === easingIndex}">${point.time.toFixed(2)}–${points[index+1].time.toFixed(2)}s</button>`).join('')}</div>` +
      `<label class="splice-number"><span>Easing</span><select aria-label="Easing of the selected segment" data-fx-ease>${Object.entries(INTERP_NAMES).map(([key, name]) => `<option value="${key}" ${interp === key ? 'selected' : ''}>${name}</option>`).join('')}</select></label>` +
      curveEditor(kind, interp === 'bezier');
    return details('easing', 'Keyframe easing', body);
  }
  function curveEditor(kind, editable) {
    const size = 132, pad = 14, x = value => pad+value*(size-2*pad), y = value => size-pad-value*(size-2*pad);
    const curve = [];
    for (let i = 0; i <= 48; i++) curve.push(`${x(i/48).toFixed(1)},${y(E.segmentFraction(kind, i/48)).toFixed(1)}`);
    const [name, x1, y1, x2, y2] = kind;
    const handles = name === 'bezier' || name === 'linear' ? `<line class="fx-arm" x1="${x(0)}" y1="${y(0)}" x2="${x(x1)}" y2="${y(y1)}"/><line class="fx-arm" x1="${x(1)}" y1="${y(1)}" x2="${x(x2)}" y2="${y(y2)}"/>` +
      `<circle class="fx-handle ${editable ? 'live' : ''}" data-fx-handle="out" cx="${x(x1)}" cy="${y(y1)}" r="6"/><circle class="fx-handle ${editable ? 'live' : ''}" data-fx-handle="in" cx="${x(x2)}" cy="${y(y2)}" r="6"/>` : '';
    return `<div class="fx-curve-editor"><svg viewBox="0 0 ${size} ${size}" data-fx-curve tabindex="0" role="img" aria-label="Easing curve: time across, value up${editable ? '. Drag the handles to shape it.' : ''}">` +
      `<rect class="fx-frame" x="${pad}" y="${pad}" width="${size-2*pad}" height="${size-2*pad}"/><polyline points="${curve.join(' ')}"/>${handles}</svg>` +
      (editable ? `<div class="fx-handle-numbers">${[['out', 0, x1], ['out', 1, y1], ['in', 0, x2], ['in', 1, y2]].map(([handle, axis, value]) =>
        `<label><span>${handle === 'out' ? 'Out' : 'In'} ${axis ? 'y' : 'x'}</span><input type="number" data-fx-handle-number="${handle}" data-axis="${axis}" min="${axis ? -2 : 0}" max="${axis ? 3 : 1}" step=".01" value="${Math.round(value*100)/100}" aria-label="${handle === 'out' ? 'Outgoing' : 'Incoming'} handle ${axis ? 'value' : 'time'}"></label>`).join('')}</div>` :
        '<p class="splice-handle">Choose Custom bezier to drag the handles.</p>') + '</div>';
  }
  function cropMarkup(clip, t) {
    const crop = clip.effects?.crop;
    const body = `<label class="splice-safe-toggle"><input type="checkbox" data-fx-toggle="crop" ${crop && crop.enabled !== false ? 'checked' : ''}> Crop</label>` +
      Object.entries(E.CROP_PROPS).map(([name, [low, high, base]]) => {
        const key = 'crop.'+name, keyed = !!clip.keyframes?.[key]?.length;
        return slider(CROP_NAMES[name], key, valueAt(clip, key, crop?.[name] ?? base, t), low, name === 'feather' ? 50 : high, 1, '%', keyed, key);
      }).join('');
    return details('crop', 'Crop', body, crop ? ' <span class="fx-badge">on</span>' : '');
  }
  function maskMarkup(clip, t) {
    const masks = clip.effects?.masks || [];
    editMask = clamp(editMask, 0, Math.max(0, masks.length-1));
    let body = `<div class="fx-row">${E.MASK_SHAPES.map(shape => `<button type="button" data-fx-mask-add="${shape}">+ ${shape[0].toUpperCase()+shape.slice(1)}</button>`).join('')}</div>`;
    if (masks.length >= E.MAX_MASKS) body = `<p class="splice-handle">A clip has at most ${E.MAX_MASKS} masks.</p>`;
    body += masks.map((mask, index) => {
      const head = `<div class="fx-mask-head"><button type="button" class="fx-mask-pick" data-fx-mask-edit="${index}" aria-pressed="${index === editMask}" title="Show this mask's handles on the monitor">Mask ${index+1} · ${mask.shape}</button>` +
        `<label><input type="checkbox" data-fx-mask-flag="enabled" data-mask="${index}" ${mask.enabled !== false ? 'checked' : ''}> On</label>` +
        `<label><input type="checkbox" data-fx-mask-flag="invert" data-mask="${index}" ${mask.invert ? 'checked' : ''}> Invert</label>` +
        `<button type="button" data-fx-mask-delete="${index}" aria-label="Delete mask ${index+1}">✕</button></div>`;
      if (index !== editMask) return `<div class="fx-mask">${head}</div>`;
      return `<div class="fx-mask editing">${head}${Object.entries(E.MASK_PROPS).map(([name, [low, high, base]]) => {
        const key = `mask.${index}.${name}`, keyed = !!clip.keyframes?.[key]?.length;
        const range = name === 'x' || name === 'y' ? [-20, 120] : name === 'width' || name === 'height' ? [0, 200] : name === 'feather' ? [0, 50] : name === 'expansion' ? [-50, 50] : [low, high];
        return slider(MASK_NAMES[name], key, valueAt(clip, key, mask[name] ?? base, t), range[0], range[1], name === 'rotation' ? 1 : .5, name === 'rotation' ? '°' : '%', keyed, key);
      }).join('')}</div>`;
    }).join('');
    if (masks.length) body += '<p class="splice-handle">Drag the handles on the monitor: the centre moves, corners size, the stem rotates, polygon points move. On an adjustment clip the masks limit its grade (a power window).</p>';
    return details('masks', 'Masks', body, masks.length ? ` <span class="fx-badge">${masks.length}</span>` : '');
  }
  function keyMarkup(clip, state) {
    const key = clip.effects?.key;
    const alpha = state.monitorAlpha === clip.id;
    const body = `<label class="splice-safe-toggle"><input type="checkbox" data-fx-toggle="key" ${key && key.enabled !== false ? 'checked' : ''}> Chroma key</label>` +
      `<div class="fx-row"><label class="fx-swatch"><span>Key colour</span><input type="color" aria-label="Key colour" data-fx-key-color value="${esc(key?.color || '#00ff00')}"></label>` +
      `<button type="button" data-fx-eyedropper aria-pressed="${picking}" title="Click the monitor to sample the key colour from the clip">${picking ? 'Click the monitor…' : 'Eyedropper'}</button></div>` +
      Object.entries(E.KEY_PROPS).map(([name, [low, high, base]]) => slider(KEY_NAMES[name], 'key.'+name, key?.[name] ?? base, low, high, 1, '%', false, null)).join('') +
      `<label class="splice-safe-toggle"><input type="checkbox" data-fx-key-flag="invert" ${key?.invert ? 'checked' : ''}> Invert matte</label>` +
      `<label class="splice-safe-toggle"><input type="checkbox" data-fx-alpha-view ${alpha ? 'checked' : ''}> View alpha matte (monitor only)</label>`;
    return details('key', 'Chroma key', body, key ? ' <span class="fx-badge">on</span>' : '');
  }
  function blendMarkup(clip) {
    const blend = clip.effects?.blend || 'normal';
    return details('blend', 'Blend mode', `<label class="splice-number"><span>Mode</span><select aria-label="Blend mode" data-fx-blend>${E.BLEND_MODES.map(mode => `<option value="${mode}" ${mode === blend ? 'selected' : ''}>${E.BLEND_NAMES[mode]}</option>`).join('')}</select></label>` +
      '<p class="splice-handle">Blends with the layers beneath, after this clip\'s grade, opacity, masks and key.</p>', blend !== 'normal' ? ` <span class="fx-badge">${E.BLEND_NAMES[blend]}</span>` : '');
  }
  function stabilizeMarkup(clip, source) {
    if (source?.still || source?.nativeGenerator || window.SpliceNested?.isNativeNested?.(source) || window.SpliceMulticam?.isMulticamItem?.(source))
      return details('stabilize', 'Stabilization', '<p class="splice-handle">Stabilize the clips inside a nested or multicam clip, or a video clip.</p>');
    if (source?.raw === true) return details('stabilize', 'Stabilization', `<p class="splice-handle">${esc(E.MESSAGES.stabilizeRaw)}</p>`);
    const s = clip.effects?.stabilize, status = jobs.get(s?.analysis) || null;
    const capability = capabilities.vidstab;
    let body = capability === false ? `<p class="splice-compat" role="alert">${esc(capabilities.message || 'This FFmpeg was built without libvidstab, so Splice cannot stabilize. Install an FFmpeg with vidstab (--enable-libvidstab).')}</p>` : '';
    body += `<div class="fx-row"><button type="button" data-fx-analyze ${capability === false || status?.state === 'running' ? 'disabled' : ''}>${s?.analysis ? 'Analyze again' : 'Analyze'}</button>` +
      (s ? `<label><input type="checkbox" data-fx-toggle="stabilize" ${s.enabled !== false ? 'checked' : ''}> On</label><button type="button" data-fx-stabilize-reset>Reset</button>` : '') + '</div>';
    if (status?.state === 'running') body += `<p class="splice-handle" role="status">Analyzing motion… ${Math.round((status.progress || 0)*100)}%</p>`;
    else if (status?.state === 'failed' || status?.state === 'cancelled') body += `<p class="splice-compat" role="alert">${esc(status.error)}</p>`;
    else if (s?.analysis && status?.state === 'done') body += `<p class="splice-handle" role="status">Analyzed ${status.frames} frames${Number.isFinite(status.shake?.before) ? ` · measured shake ${status.shake.before.toFixed(1)} px/frame` : ''}.</p>`;
    if (s) body += Object.entries(E.STABILIZE_PROPS).filter(([name]) => name !== 'autoZoom').map(([name, [low, high, base]]) =>
      slider({smoothing:'Smoothing (frames)', maxShift:'Max shift (px, -1 any)', zoom:'Zoom'}[name], 'stabilize.'+name, s[name] ?? base, low, high, 1, name === 'zoom' ? '%' : '', false, null)).join('') +
      `<label class="splice-number"><span>Auto zoom</span><select aria-label="Stabilization auto zoom" data-fx-stabilize-zoom>${[['0', 'Off'], ['1', 'Fill (static)'], ['2', 'Adaptive']].map(([value, name]) => `<option value="${value}" ${String(s.autoZoom ?? 1) === value ? 'selected' : ''}>${name}</option>`).join('')}</select></label>` +
      `<label class="splice-safe-toggle"><input type="checkbox" data-fx-stabilize-flag="tripod" ${s.tripod ? 'checked' : ''}> Tripod (lock to the first frame)</label>`;
    return details('stabilize', 'Stabilization', body, s?.analysis ? ' <span class="fx-badge">on</span>' : '');
  }
  function markup(clip, source, state) {
    if (clip.type === 'title') return '';
    const t = local(state, clip), effects = clip.effects;
    const adjustment = source?.adjustment;
    let html = `<div class="fx-tools" data-fx-tools><div class="fx-row fx-top"><label class="splice-safe-toggle"><input type="checkbox" data-fx-bypass ${effects?.bypass ? 'checked' : ''}> Bypass effects</label>` +
      `<button type="button" data-fx-reset title="Remove every effect from this clip">Reset effects</button></div>`;
    if (clip.type === 'video' && !adjustment) html += speedMarkup(clip, source, state);
    else if (clip.type === 'audio') html += speedMarkup(clip, source, state);
    html += easingMarkup(clip);
    if (clip.type === 'video') {
      html += cropMarkup(clip, t) + maskMarkup(clip, t);
      if (!adjustment) html += keyMarkup(clip, {monitorAlpha:window.Editor?.monitor?.alphaView}) + blendMarkup(clip) + stabilizeMarkup(clip, source);
    }
    return html + '</div>';
  }

  // ---------------------------------------------------------------- editing
  function update(editor, clip, patch) {
    try { editor.updateClip(clip.id, patch); return true; }
    catch (error) { notify(error.message); return false; }
  }
  const effectsOf = clip => structuredClone(clip.effects || {});
  function writePath(editor, clip, path, value, fps, time) {
    const [group, name, prop] = path.split('.');
    const keyName = group === 'mask' ? path : group === 'crop' ? path : null;
    if (keyName && clip.keyframes?.[keyName]?.length) return update(editor, clip, {keyframes:setKey(clip, keyName, time, value, fps)});
    const effects = effectsOf(clip);
    if (group === 'crop') effects.crop = {left:0, right:0, top:0, bottom:0, feather:0, ...(effects.crop || {}), [name]:value};
    else if (group === 'mask') effects.masks[Number(name)] = {...effects.masks[Number(name)], [prop]:value};
    else if (group === 'key') effects.key = {color:'#00ff00', ...(effects.key || {}), [name]:value};
    else if (group === 'stabilize') effects.stabilize = {...(effects.stabilize || {}), [name]:value};
    return update(editor, clip, {effects});
  }
  function rampChange(editor, clip, keys, state) {
    // Keep the clip's source span: the duration follows the curve (whole frames).
    const fps = state.project.fps || 25, span = E.sourceDelta(clip, 0, clip.duration);
    const members = [clip, ...state.project.clips.filter(other => other.id !== clip.id && clip.linkGroup && other.linkGroup === clip.linkGroup)];
    try {
      editor.editProject(next => {
        for (const member of members) {
          const target = next.clips.find(other => other.id === member.id);
          if (!keys) {
            const average = E.sourceDelta(target, 0, target.duration)/target.duration;
            const {speed, ...rest} = target.keyframes || {};
            target.keyframes = rest;
            target.speed = clamp(Math.round(average*1e4)/1e4, .1, 16);
            continue;
          }
          if (target.reverse) throw new Error('Speed ramps play forward. Turn off Reverse to ramp this clip.');
          const length = E.rampDuration(keys, span);
          if (length == null || length < 1/fps) throw new Error('This ramp never plays the clip\'s source to its Out point.');
          target.keyframes = {...(target.keyframes || {}), speed:keys.map(point => ({...point}))};
          target.duration = Math.max(1/fps, Math.round(length*fps)/fps);
        }
      });
      return true;
    } catch (error) { notify(error.message); return false; }
  }
  // The ramp's sound mode, like the ramp itself (rampChange), applies to the whole link group:
  // the linked sound clip is the one that plays.
  function speedAudioChange(editor, clip, mode) {
    const members = [clip, ...editor.get().project.clips.filter(other => other.id !== clip.id && clip.linkGroup && other.linkGroup === clip.linkGroup)];
    try {
      editor.editProject(next => {
        for (const member of members) {
          const target = next.clips.find(other => other.id === member.id);
          target.effects = {...(target.effects || {}), speedAudio:mode};
        }
      });
      return true;
    } catch (error) { notify(error.message); return false; }
  }
  function bind(element, clip, editor, rerender) {
    const box = element.querySelector('[data-fx-tools]');
    if (!box) return;
    const state = editor.get(), fps = state.project.fps || 25, time = () => local(editor.get(), current());
    const current = () => editor.get().project.clips.find(c => c.id === clip.id) || clip;
    const locked = !!editor.isTrackLocked?.(clip.track, clip.type);
    if (locked) for (const input of box.querySelectorAll('input,select,button')) input.disabled = true;
    // Remember open groups at the click (a toggle event can arrive after the inspector re-renders).
    for (const group of box.querySelectorAll('[data-fx-group]')) group.querySelector('[data-fx-summary]').addEventListener('click', event => {
      const id = group.dataset.fxGroup, body = group.querySelector('.fx-body'), opening = body.hidden;
      if (opening) open.add(id); else open.delete(id);
      body.hidden = !opening; event.currentTarget.setAttribute('aria-expanded', String(opening)); group.toggleAttribute('data-open', opening);
      if (id === 'masks') requestAnimationFrame(() => overlay.refresh());
    });
    box.querySelector('[data-fx-bypass]')?.addEventListener('change', event => update(editor, current(), {effects:{...effectsOf(current()), bypass:event.target.checked}}));
    box.querySelector('[data-fx-reset]')?.addEventListener('click', () => {
      const keys = Object.fromEntries(Object.entries(current().keyframes || {}).filter(([key]) => !E.isEffectKey(key) || key === 'speed'));
      update(editor, current(), {effects:undefined, keyframes:keys}); rerender();
    });
    // Sliders: a drag is one undo step.
    for (const input of box.querySelectorAll('input[data-fx]')) {
      input.addEventListener('pointerdown', () => editor.beginGesture());
      input.addEventListener('input', () => {
        const out = box.querySelector(`[data-fx-out="${CSS.escape(input.dataset.fx)}"]`);
        if (out) out.textContent = input.value+(out.textContent.match(/[%°]$/)?.[0] || '');
        writePath(editor, current(), input.dataset.fx, Number(input.value), fps, time());
      });
      input.addEventListener('change', () => { editor.endGesture(); rerender(); });
    }
    for (const button of box.querySelectorAll('[data-fx-key]')) button.addEventListener('click', () => {
      const key = button.dataset.fxKey, live = current(), input = box.querySelector(`input[data-fx="${CSS.escape(button.dataset.fxBase)}"]`);
      if (update(editor, live, {keyframes:setKey(live, key, time(), Number(input.value), fps)})) rerender();
    });
    for (const button of box.querySelectorAll('[data-fx-unkey]')) button.addEventListener('click', () => {
      const live = current(), key = button.dataset.fxUnkey, value = valueAt(live, key, 0, time());
      const {[key]:gone, ...keys} = live.keyframes || {};
      const effects = effectsOf(live), [group, index, prop] = key.split('.');
      if (group === 'crop') effects.crop = {...(effects.crop || {}), [index]:value};
      if (group === 'mask') effects.masks[Number(index)][prop] = value;
      update(editor, live, {keyframes:keys, effects}); rerender();
    });
    for (const toggle of box.querySelectorAll('[data-fx-toggle]')) toggle.addEventListener('change', () => {
      const name = toggle.dataset.fxToggle, effects = effectsOf(current());
      if (!effects[name] && toggle.checked) effects[name] = name === 'crop' ? {left:0, right:0, top:0, bottom:0, feather:0} : name === 'key' ? {color:'#00ff00'} : {};
      if (effects[name]) effects[name].enabled = toggle.checked;
      update(editor, current(), {effects}); rerender();
    });
    // ---- speed ramp
    box.querySelector('[data-fx-speed-audio]')?.addEventListener('change', event => speedAudioChange(editor, current(), event.target.value));
    for (const button of box.querySelectorAll('[data-fx-preset]')) button.addEventListener('click', () => {
      const live = current(), span = E.sourceDelta(live, 0, live.duration);
      try { if (rampChange(editor, live, E.presetRamp(button.dataset.fxPreset, span).keys, editor.get())) rerender(); }
      catch (error) { notify(error.message); }
    });
    box.querySelector('[data-fx-knot-add]')?.addEventListener('click', () => {
      const live = current(), ramp = E.rampPoints(live), t = Math.round(time()*fps)/fps;
      const speed = ramp ? E.rampSpeed(ramp, t)*100 : (Number(live.speed) || 1)*100;
      const keys = ramp ? [...ramp.filter(point => Math.abs(point.time-t) >= .5/fps), {time:t, value:Math.round(speed)}].sort((a, b) => a.time-b.time) :
        [{time:0, value:Math.round(speed)}, ...(t > .5/fps && t < live.duration-.5/fps ? [{time:t, value:Math.round(speed)}] : []), {time:live.duration, value:Math.round(speed)}];
      if (rampChange(editor, live, keys, editor.get())) rerender();
    });
    box.querySelector('[data-fx-ramp-remove]')?.addEventListener('click', () => { if (rampChange(editor, current(), null, editor.get())) rerender(); });
    for (const input of box.querySelectorAll('[data-fx-knot]')) input.addEventListener('change', () => {
      const ramp = E.rampPoints(current()).map(point => ({...point})), value = Number(input.value);
      if (!Number.isFinite(value) || value < 0 || value > 1600) { notify('Choose a speed from 0% (a freeze) to 1600%.'); return rerender(); }
      ramp[Number(input.dataset.fxKnot)].value = value;
      rampChange(editor, current(), ramp, editor.get()); rerender();
    });
    for (const select of box.querySelectorAll('[data-fx-knot-interp]')) select.addEventListener('change', () => {
      const ramp = E.rampPoints(current()).map(point => ({...point})), point = ramp[Number(select.dataset.fxKnotInterp)];
      delete point.ease; point.interp = select.value;
      rampChange(editor, current(), ramp, editor.get()); rerender();
    });
    for (const button of box.querySelectorAll('[data-fx-knot-delete]')) button.addEventListener('click', () => {
      const ramp = E.rampPoints(current()).filter((_, index) => index !== Number(button.dataset.fxKnotDelete));
      rampChange(editor, current(), ramp, editor.get()); rerender();
    });
    for (const button of box.querySelectorAll('[data-fx-seek]')) button.addEventListener('click', () => editor.seek(current().start+Number(button.dataset.fxSeek)));
    // ---- easing
    box.querySelector('[data-fx-ease-key]')?.addEventListener('change', event => { easingKey = event.target.value; easingIndex = 0; rerender(); });
    for (const button of box.querySelectorAll('[data-fx-ease-segment]')) button.addEventListener('click', () => { easingIndex = Number(button.dataset.fxEaseSegment); rerender(); });
    const setSegment = (mutate, gesture = false) => {
      const live = current(), points = [...(live.keyframes?.[easingKey] || [])].sort((a, b) => a.time-b.time).map(point => ({...point}));
      if (points.length < 2) return;
      mutate(points[easingIndex], points[easingIndex+1]);
      if (easingKey === 'speed') return rampChange(editor, live, points, editor.get());
      return update(editor, live, {keyframes:{...live.keyframes, [easingKey]:points}}) && !gesture;
    };
    box.querySelector('[data-fx-ease]')?.addEventListener('change', event => {
      setSegment((a, b) => {
        delete a.ease; a.interp = event.target.value;
        if (event.target.value === 'bezier') {
          const preset = E.PRESET_HANDLES.easeInOut;
          a.outHandle ||= [preset[0], preset[1]]; b.inHandle ||= [preset[2], preset[3]];
        }
      });
      rerender();
    });
    for (const input of box.querySelectorAll('[data-fx-handle-number]')) input.addEventListener('change', () => {
      const value = Number(input.value), axis = Number(input.dataset.axis);
      setSegment((a, b) => {
        const target = input.dataset.fxHandleNumber === 'out' ? (a.outHandle ||= [1/3, 1/3]) : (b.inHandle ||= [2/3, 2/3]);
        target[axis] = axis ? clamp(value, -2, 3) : clamp(value, 0, 1);
      });
      rerender();
    });
    const curve = box.querySelector('[data-fx-curve]');
    for (const handle of box.querySelectorAll('.fx-handle.live')) handle.addEventListener('pointerdown', event => {
      event.preventDefault();
      const which = handle.dataset.fxHandle, rect = curve.getBoundingClientRect(), size = 132, pad = 14;
      editor.beginGesture();
      // Focus inside the inspector keeps it from re-rendering under the drag (as the colour wheels do).
      curve.focus({preventScroll:true});
      const move = moveEvent => {
        const u = clamp(((moveEvent.clientX-rect.left)/rect.width*size-pad)/(size-2*pad), 0, 1);
        const v = clamp((size-pad-(moveEvent.clientY-rect.top)/rect.height*size)/(size-2*pad), -2, 3);
        setSegment((a, b) => { if (which === 'out') a.outHandle = [Math.round(u*1000)/1000, Math.round(v*1000)/1000]; else b.inHandle = [Math.round(u*1000)/1000, Math.round(v*1000)/1000]; }, true);
        handle.setAttribute('cx', pad+u*(size-2*pad)); handle.setAttribute('cy', size-pad-v*(size-2*pad));
      };
      const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up); editor.endGesture(); rerender(); };
      window.addEventListener('pointermove', move); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
    });
    // ---- masks
    for (const button of box.querySelectorAll('[data-fx-mask-add]')) button.addEventListener('click', () => {
      const effects = effectsOf(current()), shape = button.dataset.fxMaskAdd;
      effects.masks = [...(effects.masks || []), {shape, x:50, y:50, width:shape === 'polygon' ? 100 : 50, height:shape === 'polygon' ? 100 : 50, rotation:0, feather:2, expansion:0, opacity:100,
        ...(shape === 'polygon' ? {points:[[-25, -25], [25, -25], [30, 20], [-20, 25]]} : {})}];
      editMask = effects.masks.length-1;
      if (update(editor, current(), {effects})) { open.add('masks'); rerender(); }
    });
    for (const button of box.querySelectorAll('[data-fx-mask-edit]')) button.addEventListener('click', () => { editMask = Number(button.dataset.fxMaskEdit); rerender(); overlay.refresh(); });
    for (const box2 of box.querySelectorAll('[data-fx-mask-flag]')) box2.addEventListener('change', () => {
      const effects = effectsOf(current()); effects.masks[Number(box2.dataset.mask)][box2.dataset.fxMaskFlag] = box2.checked;
      update(editor, current(), {effects});
    });
    for (const button of box.querySelectorAll('[data-fx-mask-delete]')) button.addEventListener('click', () => {
      const live = current(), index = Number(button.dataset.fxMaskDelete), effects = effectsOf(live);
      effects.masks.splice(index, 1);
      // Animated properties of later masks move down one slot.
      const keys = {};
      for (const [key, points] of Object.entries(live.keyframes || {})) {
        const match = key.match(/^mask\.(\d+)\.(.+)$/);
        if (!match) keys[key] = points;
        else if (Number(match[1]) < index) keys[key] = points;
        else if (Number(match[1]) > index) keys[`mask.${Number(match[1])-1}.${match[2]}`] = points;
      }
      if (!effects.masks.length) delete effects.masks;
      update(editor, live, {effects, keyframes:keys}); rerender();
    });
    // ---- chroma key
    box.querySelector('[data-fx-key-color]')?.addEventListener('change', event => {
      const effects = effectsOf(current()); effects.key = {...(effects.key || {}), color:event.target.value};
      update(editor, current(), {effects});
    });
    box.querySelector('[data-fx-key-flag]')?.addEventListener('change', event => {
      const effects = effectsOf(current()); effects.key = {color:'#00ff00', ...(effects.key || {}), invert:event.target.checked};
      update(editor, current(), {effects});
    });
    box.querySelector('[data-fx-alpha-view]')?.addEventListener('change', event => {
      editor.monitor.alphaView = event.target.checked ? clip.id : null; editor.refreshMedia();
    });
    box.querySelector('[data-fx-eyedropper]')?.addEventListener('click', () => { picking = !picking; overlay.refresh(); rerender(); });
    box.querySelector('[data-fx-blend]')?.addEventListener('change', event => update(editor, current(), {effects:{...effectsOf(current()), blend:event.target.value}}));
    // ---- stabilization
    box.querySelector('[data-fx-analyze]')?.addEventListener('click', () => analyze(editor, current(), rerender));
    box.querySelector('[data-fx-stabilize-reset]')?.addEventListener('click', () => {
      const effects = effectsOf(current()), id = effects.stabilize?.analysis;
      delete effects.stabilize;
      if (update(editor, current(), {effects})) release(editor, id);
      rerender();
    });
    box.querySelector('[data-fx-stabilize-zoom]')?.addEventListener('change', event => writePath(editor, current(), 'stabilize.autoZoom', Number(event.target.value), fps, time()));
    box.querySelector('[data-fx-stabilize-flag]')?.addEventListener('change', event => {
      const effects = effectsOf(current()); effects.stabilize = {...(effects.stabilize || {}), tripod:event.target.checked};
      update(editor, current(), {effects});
    });
    if (!capabilities.checked) checkCapabilities().then(rerender);
    overlay.refresh();
  }

  // ---------------------------------------------------------------- stabilization jobs
  const jobs = new Map(), capabilities = {checked:false, vidstab:null, message:''};
  const api = (path, options = {}) => fetch(path, options).then(async response => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    return body;
  });
  async function checkCapabilities() {
    capabilities.checked = true;
    try { Object.assign(capabilities, await api('/api/effects/capabilities')); } catch (error) { capabilities.vidstab = null; }
  }
  async function analyze(editor, clip, rerender) {
    const state = editor.get(), item = mediaOf(state, clip);
    const sourceIn = Number(clip.in) || 0, sourceOut = sourceIn+E.sourceDelta(clip, 0, clip.duration);
    try {
      const status = await api('/api/effects/stabilize', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({src:item.src, in:sourceIn, out:sourceOut})});
      jobs.set(status.analysis, status);
      const effects = structuredClone(clip.effects || {});
      effects.stabilize = {smoothing:15, maxShift:-1, zoom:0, autoZoom:1, tripod:false, ...(effects.stabilize || {}), analysis:status.analysis, source:[sourceIn, sourceOut], enabled:true};
      const previous = clip.effects?.stabilize?.analysis;
      editor.updateClip(clip.id, {effects});
      if (previous !== status.analysis) release(editor, previous);
      rerender();
      poll(status.analysis, editor, rerender);
    } catch (error) { notify(error.message); }
  }
  // An analysis no clip uses any more (Reset, a deleted clip, a new analysis) is cancelled on the
  // server, so its two vidstab passes stop.
  const inUse = (state, id) => (state?.project?.clips || []).some(clip => clip.effects?.stabilize?.analysis === id);
  function release(editor, id) {
    if (!id || inUse(editor.get(), id) || jobs.get(id)?.state !== 'running') return;
    jobs.set(id, {state:'cancelled', analysis:id, error:'Analysis cancelled: analyze the clip again'});
    api('/api/effects/stabilize/cancel', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id})}).catch(() => {});
  }
  function poll(id, editor, rerender) {
    const step = async () => {
      if (!inUse(editor.get(), id)) { release(editor, id); rerender(); return; }
      let status;
      try { status = await api('/api/effects/stabilize?id='+encodeURIComponent(id)); }
      catch (error) { jobs.set(id, {state:'failed', error:error.message}); rerender(); return; }
      jobs.set(id, status);
      if (status.state === 'running') setTimeout(step, 500);
      else if (status.state === 'done') editor.refreshMedia();
      rerender();
    };
    step();
  }
  // The monitor shows a stabilized clip from its preview (made from the same analysis as the export).
  function stabilizedPreview(clip) {
    const s = clip?.effects?.stabilize;
    if (!s?.analysis || s.enabled === false || clip.effects?.bypass) return null;
    const status = jobs.get(s.analysis);
    if (!status && !pending.has(s.analysis)) {
      // A project reopened later: ask the server for the analysis it keeps.
      pending.add(s.analysis);
      api('/api/effects/stabilize?id='+encodeURIComponent(s.analysis)).then(result => { jobs.set(s.analysis, result); window.Editor?.refreshMedia(); }).catch(() => {});
    }
    if (status?.state !== 'done') return null;
    const key = [s.smoothing, s.maxShift, s.zoom, s.autoZoom, s.tripod ? 1 : 0].join(',');
    const preview = status.previews?.[key];
    if (!preview) {
      if (!pending.has(s.analysis+key)) {
        pending.add(s.analysis+key);
        // The server renders the preview as a job: ask until it is done (never blocking a request).
        const request = () => api('/api/effects/stabilize/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:s.analysis, settings:{smoothing:s.smoothing, maxShift:s.maxShift, zoom:s.zoom, autoZoom:s.autoZoom, tripod:!!s.tripod}})})
          .then(result => {
            if (result.state === 'running') { setTimeout(request, 400); return; }
            pending.delete(s.analysis+key);
            if (result.state === 'failed') { notify(result.error || 'The stabilized preview failed.'); return; }
            (status.previews ||= {})[key] = {src:result.src, offset:result.offset};
            window.Editor?.refreshMedia();
          })
          .catch(error => { pending.delete(s.analysis+key); notify(error.message); });
        request();
      }
      return null;
    }
    return preview;      // {src, offset}: the preview's time 0 is source time `offset`
  }
  const pending = new Set();

  // ---------------------------------------------------------------- monitor overlay (mask handles, eyedropper)
  const overlay = (() => {
    let svg = null;
    function geometry(state, clip, canvas) {
      const item = mediaOf(state, clip), rect = canvas.getBoundingClientRect(), frame = canvas.parentElement.getBoundingClientRect();
      const W = rect.width, H = rect.height;
      if (item?.adjustment) return {toScreen:(u, v) => [rect.left-frame.left+u*W, rect.top-frame.top+v*H], aspect:W/H, w:W, h:H, angle:0};
      const sw = item?.width || state.project.width, sh = item?.height || state.project.height, fit = Math.min(W/sw, H/sh);
      const t = state.time-clip.start, k = clip.keyframes || {}, value = (key, base) => k[key]?.length ? E.curveValue(k[key], t, base) : base;
      const scale = value('scale', clip.transform?.scale ?? 100)/100, angle = value('rotation', clip.transform?.rotation ?? 0)*Math.PI/180;
      const cx = rect.left-frame.left+W/2+value('x', clip.transform?.x ?? 0)*W/100, cy = rect.top-frame.top+H/2+value('y', clip.transform?.y ?? 0)*H/100;
      const w = sw*fit*scale, h = sh*fit*scale, c = Math.cos(angle), s = Math.sin(angle);
      return {toScreen:(u, v) => { const dx = (u-.5)*w, dy = (v-.5)*h; return [cx+c*dx-s*dy, cy+s*dx+c*dy]; },
        toUv:(x, y) => { const dx = x-cx, dy = y-cy; return [(c*dx+s*dy)/w+.5, (-s*dx+c*dy)/h+.5]; }, aspect:sw/sh, w, h, angle};
    }
    function refresh() {
      const state = window.Editor?.get(), canvas = document.getElementById('preview-canvas'), frame = document.getElementById('monitor-frame');
      if (!state || !canvas || !frame) return;
      if (!svg) {
        svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.classList.add('fx-overlay'); svg.setAttribute('aria-hidden', 'true');
        frame.append(svg);
        canvas.addEventListener('click', pick, true);
      }
      const clip = state.selected, mask = clip?.effects?.masks?.[editMask];
      frame.classList.toggle('fx-picking', picking);
      const inside = clip && state.time >= clip.start && state.time < clip.start+clip.duration;
      if (!clip || clip.type !== 'video' || !mask || !inside || !open.has('masks')) { svg.innerHTML = ''; svg.style.display = 'none'; return; }
      svg.style.display = '';
      const g = geometry(state, clip, canvas), t = state.time-clip.start;
      const values = {};
      for (const name of Object.keys(E.MASK_PROPS)) values[name] = valueAt(clip, `mask.${editMask}.${name}`, mask[name] ?? E.MASK_PROPS[name][2], t);
      const m = E.maskParams(mask, values);
      // The outline in picture coordinates, mapped through the clip's transform.
      const ca = Math.cos(m.angle), sa = Math.sin(m.angle);
      const local = (px, py) => { const x = m.x+(ca*px*g.aspect-sa*py)/g.aspect, y = m.y+(sa*px*g.aspect+ca*py); return g.toScreen(x, y); };
      let outline = [];
      if (m.shape === 'polygon') outline = m.points.map(([x, y]) => local(x*m.w, y*m.h));
      else if (m.shape === 'ellipse') for (let i = 0; i < 64; i++) { const a = i/64*2*Math.PI; outline.push(local(Math.cos(a)*m.w/2, Math.sin(a)*m.h/2)); }
      else outline = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => local(sx*m.w/2, sy*m.h/2));
      const centre = g.toScreen(m.x, m.y), stem = local(0, -m.h/2-.08);
      const handles = m.shape === 'polygon' ? outline.map((p, index) => `<circle class="fx-mh" data-mh="point" data-index="${index}" cx="${p[0]}" cy="${p[1]}" r="6"/>`) :
        [[1, 1], [-1, -1], [1, -1], [-1, 1]].map(([sx, sy]) => { const p = local(sx*m.w/2, sy*m.h/2); return `<rect class="fx-mh" data-mh="size" data-sx="${sx}" data-sy="${sy}" x="${p[0]-5}" y="${p[1]-5}" width="10" height="10"/>`; });
      svg.innerHTML = `<polygon class="fx-outline" points="${outline.map(p => p.join(',')).join(' ')}"/>` +
        `<line class="fx-stem" x1="${local(0, -m.h/2)[0]}" y1="${local(0, -m.h/2)[1]}" x2="${stem[0]}" y2="${stem[1]}"/>` +
        `<circle class="fx-mh rotate" data-mh="rotate" cx="${stem[0]}" cy="${stem[1]}" r="6"/><circle class="fx-mh centre" data-mh="move" cx="${centre[0]}" cy="${centre[1]}" r="7"/>${handles.join('')}`;
      for (const handle of svg.querySelectorAll('.fx-mh')) handle.addEventListener('pointerdown', event => drag(event, handle, clip, g));
    }
    function drag(event, handle, clip, g) {
      event.preventDefault(); event.stopPropagation();
      const editor = window.Editor, frame = document.getElementById('monitor-frame').getBoundingClientRect();
      const fps = editor.get().project.fps || 25, t = editor.get().time-clip.start, index = editMask;
      const start = current => editor.get().project.clips.find(c => c.id === clip.id) || current;
      const base = start(clip).effects.masks[index];
      const read = name => valueAt(start(clip), `mask.${index}.${name}`, base[name] ?? E.MASK_PROPS[name][2], t);
      const x0 = read('x'), y0 = read('y'), rot0 = read('rotation');
      editor.beginGesture();
      // The overlay redraws as the mask moves: follow the pointer on the window, not the handle.
      const write = (values, extra) => {
        let live = start(clip);
        const effects = structuredClone(live.effects);
        let keyframes = live.keyframes || {};
        for (const [name, value] of Object.entries(values)) {
          const key = `mask.${index}.${name}`;
          if (keyframes[key]?.length) keyframes = setKey({keyframes}, key, t, value, fps);
          else effects.masks[index][name] = value;
        }
        if (extra) extra(effects.masks[index]);
        try { editor.updateClip(clip.id, {effects, keyframes}); } catch (error) { notify(error.message); }
        refresh();
      };
      const toUv = (x, y) => g.toUv ? g.toUv(x, y) : [(x-(frame.left-frame.left))/g.w, y/g.h];
      const move = moveEvent => {
        const [u, v] = toUv(moveEvent.clientX-frame.left, moveEvent.clientY-frame.top);
        const kind = handle.dataset.mh;
        if (kind === 'move') write({x:clamp(Math.round(u*1000)/10, -100, 200), y:clamp(Math.round(v*1000)/10, -100, 200)});
        else if (kind === 'rotate') {
          const dx = (u-x0/100)*g.aspect, dy = v-y0/100;
          write({rotation:clamp(Math.round((Math.atan2(dy, dx)*180/Math.PI+90)*10)/10, -360, 360)});
        } else {
          const angle = rot0*Math.PI/180, dx = (u-x0/100)*g.aspect, dy = v-y0/100;
          const qx = (Math.cos(angle)*dx+Math.sin(angle)*dy)/g.aspect, qy = -Math.sin(angle)*dx+Math.cos(angle)*dy;
          if (kind === 'size') write({width:clamp(Math.round(Math.abs(qx)*2000)/10, 0, 400), height:clamp(Math.round(Math.abs(qy)*2000)/10, 0, 400)});
          else write({}, mask => {
            const point = Number(handle.dataset.index), sx = (mask.width ?? 100)/100 || 1, sy = (mask.height ?? 100)/100 || 1;
            mask.points[point] = [clamp(Math.round(qx/sx*1000)/10, -200, 200), clamp(Math.round(qy/sy*1000)/10, -200, 200)];
          });
        }
      };
      const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up); editor.endGesture(); };
      window.addEventListener('pointermove', move); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
    }
    // Eyedropper: the clip's own picture under the click, decoded exactly as the monitor and the
    // export decode it (the clip drawn alone, without its grade or effects), not the keyed composite.
    function pick(event) {
      if (!picking) return;
      event.preventDefault(); event.stopPropagation();
      picking = false;
      const editor = window.Editor, state = editor.get(), clip = state.selected, canvas = event.currentTarget;
      if (!clip) return refresh();
      const plan = editor.framePlan(canvas), op = plan.ops.flatMap(o => [o, o.outgoing, o.incoming]).find(o => o?.clip?.id === clip.id);
      const rect = canvas.getBoundingClientRect();
      const x = Math.floor((event.clientX-rect.left)*canvas.width/rect.width), y = Math.floor((event.clientY-rect.top)*canvas.height/rect.height);
      if (!op || op.type !== 'video') { notify('Move the playhead over the selected clip to sample its colour.'); return refresh(); }
      const probe = document.createElement('canvas');
      probe.width = canvas.width; probe.height = canvas.height;
      window.SplicePicture.render(probe, {width:canvas.width, height:canvas.height, view:'normal',
        ops:[{...op, fx:null, grade:null, alpha:1, brightness:1, wipe:null, wipeOut:null}]});
      const data = probe.getContext('2d').getImageData(Math.max(0, x-1), Math.max(0, y-1), 3, 3).data, sum = [0, 0, 0, 0];
      for (let i = 0; i < data.length; i += 4) for (let c = 0; c < 4; c++) sum[c] += data[i+c];
      if (sum[3] < 9*128) { notify('Click inside the selected clip to sample its colour.'); return refresh(); }
      const color = '#'+sum.slice(0, 3).map(value => Math.round(value/9).toString(16).padStart(2, '0')).join('');
      const effects = structuredClone(clip.effects || {});
      effects.key = {similarity:20, smoothness:10, spill:50, ...(effects.key || {}), color, enabled:true};
      try { editor.updateClip(clip.id, {effects}); } catch (error) { notify(error.message); }
      refresh();
    }
    return {refresh, get picking() { return picking; }};
  })();

  // ---------------------------------------------------------------- timeline knots
  // Speed keyframes show as ticks along the bottom of a ramped clip on the timeline.
  function paintKnots() {
    const state = window.Editor?.get();
    if (!state) return;
    for (const node of document.querySelectorAll('.timeline-clip[data-clip]')) {
      const clip = state.project.clips.find(c => c.id === node.dataset.clip), ramp = clip && E.rampPoints(clip);
      let strip = node.querySelector('.fx-knot-strip');
      if (!ramp) { strip?.remove(); continue; }
      const key = JSON.stringify([ramp, clip.duration]);
      if (strip?.dataset.key === key) continue;
      if (!strip) { strip = document.createElement('span'); strip.className = 'fx-knot-strip'; strip.setAttribute('aria-hidden', 'true'); node.append(strip); }
      strip.dataset.key = key;
      strip.innerHTML = ramp.filter(point => point.time <= clip.duration+1e-6).map(point =>
        `<i style="left:${(point.time/clip.duration*100).toFixed(2)}%" title="${Math.round(point.value)}%"></i>`).join('');
    }
  }
  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; paintKnots(); overlay.refresh(); });
  }
  function init() {
    if (!window.Editor) return;
    window.Editor.subscribe(schedule);
    window.addEventListener('resize', schedule);
    schedule();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true});
  else init();

  window.SpliceEffectsUI = {markup, bind, overlay, stabilizedPreview, jobs, capabilities, rampChange, speedAudioChange, analyze};
})();
