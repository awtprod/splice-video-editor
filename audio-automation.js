// Track automation lanes: volume (%) and pan keyframes on audio tracks, drawn over the lane.
// Click the lane to add a point, drag a point to move it (time and value), double-click it to
// type a value, Delete removes it, E switches its segment between linear and eased. The
// curve is evaluated the same way in the preview (engine.js) and the export (render_movie).
(() => {
  const shown = new Map();            // track -> 'volume' | 'pan'
  const SPEC = {volume: {low: 0, high: 200, unit: '%', step: 1, label: 'Volume'}, pan: {low: -100, high: 100, unit: '', step: 1, label: 'Pan'}};
  const SVG = 'http://www.w3.org/2000/svg';
  const dB = value => value > 0 ? 20*Math.log10(value/100) : -Infinity;
  const describe = (key, value) => key === 'volume' ? `${Math.round(value)}% (${Number.isFinite(dB(value)) ? dB(value).toFixed(1) : '−∞'} dB)` :
    value === 0 ? 'centre' : `${Math.abs(Math.round(value))} ${value < 0 ? 'left' : 'right'}`;
  function points(project, track, key) {
    return [...(project.trackMix?.[String(track)]?.keyframes?.[key] || [])].sort((a, b) => a.time - b.time);
  }
  function renderLane(surface, lane, project, editor, {px, seconds}) {
    const key = shown.get(lane.track);
    if (!key) return;
    const spec = SPEC[key], height = surface.clientHeight || parseFloat(surface.style.height) || 60;
    const fps = project.fps || 25;
    const svg = document.createElementNS(SVG, 'svg');
    svg.classList.add('automation-lane');
    svg.setAttribute('aria-label', `A${Math.abs(lane.track)} ${spec.label.toLowerCase()} automation. Click to add a point.`);
    const y = value => 6 + (1 - (value - spec.low)/(spec.high - spec.low))*(height - 12);
    const valueAt = offsetY => Math.round(Math.max(spec.low, Math.min(spec.high, spec.low + (1 - (offsetY - 6)/(height - 12))*(spec.high - spec.low))));
    const list = points(project, lane.track, key);
    const base = key === 'volume' ? Number(project.trackMix?.[String(lane.track)]?.gain ?? 100) : Number(project.trackMix?.[String(lane.track)]?.pan ?? 0);
    const commit = next => {
      try { editor.setTrackAutomation(lane.track, key, next); } catch (error) { window.alert(error.message); }
    };
    // The curve, sampled across the surface: eased segments are drawn as they sound.
    const width = parseFloat(surface.parentElement?.style.width || surface.closest('.timeline-surface')?.style.width) || px(seconds);
    const reference = document.createElementNS(SVG, 'line');
    reference.setAttribute('class', 'automation-reference');
    reference.setAttribute('x1', 0); reference.setAttribute('x2', width);
    reference.setAttribute('y1', y(key === 'volume' ? 100 : 0)); reference.setAttribute('y2', y(key === 'volume' ? 100 : 0));
    svg.append(reference);
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('class', 'automation-curve');
    const curve = window.SpliceAudioDSP.curveValue;
    let d = '';
    if (list.length) {
      const first = list[0], last = list.at(-1);
      d = `M0 ${y(first.value)} L${px(first.time)} ${y(first.value)}`;
      for (let index = 0; index + 1 < list.length; index++) {
        const a = list[index], b = list[index + 1], steps = a.ease ? 24 : 1;
        for (let k = 1; k <= steps; k++) {
          const t = a.time + (b.time - a.time)*k/steps;
          d += ` L${px(t)} ${y(curve(list, t, base))}`;
        }
      }
      d += ` L${width} ${y(last.value)}`;
    } else d = `M0 ${y(base)} L${width} ${y(base)}`;
    path.setAttribute('d', d);
    svg.append(path);
    list.forEach((point, index) => {
      const dot = document.createElementNS(SVG, 'circle');
      dot.setAttribute('class', 'automation-point' + (point.ease ? ' eased' : ''));
      dot.setAttribute('cx', px(point.time)); dot.setAttribute('cy', y(point.value)); dot.setAttribute('r', 5);
      dot.setAttribute('tabindex', '0');
      dot.setAttribute('role', 'slider');
      dot.setAttribute('aria-label', `${spec.label} point at ${point.time.toFixed(2)} s`);
      dot.setAttribute('aria-valuemin', spec.low); dot.setAttribute('aria-valuemax', spec.high);
      dot.setAttribute('aria-valuenow', point.value); dot.setAttribute('aria-valuetext', describe(key, point.value) + (point.ease ? ', eased' : ''));
      const title = document.createElementNS(SVG, 'title');
      title.textContent = `${point.time.toFixed(2)} s · ${describe(key, point.value)}${point.ease ? ' · eased' : ''}. Drag to move, double-click to type, Delete to remove, E to ease.`;
      dot.append(title);
      const replace = changes => commit(list.map((other, at) => at === index ? {...other, ...changes} : other));
      dot.onpointerdown = event => {
        event.stopPropagation(); event.preventDefault();
        dot.setPointerCapture(event.pointerId);
        dot.focus({preventScroll: true});
        const box = svg.getBoundingClientRect(), low = index ? list[index - 1].time + 1/fps : 0, high = index + 1 < list.length ? list[index + 1].time - 1/fps : Infinity;
        let moved = null;
        dot.onpointermove = move => {
          const time = Math.max(low, Math.min(high, Math.round((point.time + (move.clientX - event.clientX)/px(1))*fps)/fps));
          const value = valueAt(move.clientY - box.top);
          moved = {time, value};
          dot.setAttribute('cx', px(time)); dot.setAttribute('cy', y(value));
          title.textContent = `${time.toFixed(2)} s · ${describe(key, value)}`;
        };
        dot.onpointerup = () => { dot.onpointermove = dot.onpointerup = null; if (moved) { refocus = {track: lane.track, time: moved.time}; replace(moved); } };
      };
      dot.ondblclick = event => { event.stopPropagation(); entry(svg, dot, point, spec, value => replace({value})); };
      dot.onkeydown = event => {
        const handled = ['Delete','Backspace','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Enter','e','E'].includes(event.key);
        if (!handled) return;
        event.preventDefault(); event.stopPropagation();
        // Set before the edit: the lane is redrawn synchronously when the project changes.
        refocus = {track: lane.track, time: point.time};
        const step = event.shiftKey ? 10 : spec.step;
        if (event.key === 'Delete' || event.key === 'Backspace') commit(list.filter((_, at) => at !== index));
        else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') replace({value: Math.max(spec.low, Math.min(spec.high, point.value + (event.key === 'ArrowUp' ? step : -step)))});
        else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          const time = Math.round((point.time + (event.key === 'ArrowRight' ? 1 : -1)*(event.shiftKey ? 5 : 1)/fps)*fps)/fps;
          if (time >= 0 && !list.some((other, at) => at !== index && Math.abs(other.time - time) < .5/fps)) { refocus.time = time; replace({time}); }
        } else if (event.key === 'Enter') entry(svg, dot, point, spec, value => replace({value}));
        else replace({ease: !point.ease});
      };
      svg.append(dot);
    });
    svg.onpointerdown = event => {
      if (event.target !== svg && !event.target.classList.contains('automation-curve') && event.target !== reference) return;
      event.stopPropagation(); event.preventDefault();
      const box = svg.getBoundingClientRect();
      const time = Math.max(0, Math.round((event.clientX - box.left)/px(1)*fps)/fps);
      if (list.some(point => Math.abs(point.time - time) < .5/fps)) return;
      const value = list.length ? Math.round(curve(list, time, base)) : valueAt(event.clientY - box.top);
      refocus = {track: lane.track, time};
      commit([...list, {time, value}]);
    };
    // A label and the property switch, at the lane's left edge.
    const label = document.createElement('div');
    label.className = 'automation-tag';
    label.innerHTML = `<select aria-label="A${Math.abs(lane.track)} automation property"><option value="volume">Volume</option><option value="pan" ${key==='pan'?'selected':''}>Pan</option></select>`;
    label.querySelector('select').onchange = event => { shown.set(lane.track, event.target.value); window.SpliceWorkspace?.refresh?.(); };
    label.onpointerdown = event => event.stopPropagation();
    surface.classList.add('has-automation');
    surface.append(svg, label);
    if (refocus && refocus.track === lane.track) {
      const target = [...svg.querySelectorAll('.automation-point')].find((_, at) => Math.abs(list[at].time - refocus.time) < .5/fps) ||
        svg.querySelector('.automation-point');
      refocus = null;
      requestAnimationFrame(() => target?.focus({preventScroll: true}));
    }
  }
  let refocus = null;
  function entry(svg, dot, point, spec, done) {
    const box = dot.getBoundingClientRect();
    const input = document.createElement('input');
    input.type = 'number'; input.min = spec.low; input.max = spec.high; input.step = spec.step; input.value = point.value;
    input.className = 'automation-entry';
    input.setAttribute('aria-label', `${spec.label} value${spec.unit ? ' (' + spec.unit + ')' : ''}`);
    input.style.left = Math.min(window.innerWidth - 90, box.left) + 'px'; input.style.top = Math.max(4, box.top - 34) + 'px';
    let finished = false;
    const finish = save => {
      if (finished) return;   // removing the field blurs it, which would finish it again
      finished = true;
      const value = Number(input.value);
      input.remove();
      if (save && Number.isFinite(value) && value >= spec.low && value <= spec.high) done(value);
    };
    input.onkeydown = event => { if (event.key === 'Enter') finish(true); else if (event.key === 'Escape') finish(false); event.stopPropagation(); };
    input.onblur = () => finish(true);
    document.body.append(input);
    input.focus(); input.select();
  }
  window.SpliceAutomation = {
    renderLane,
    isShown: track => shown.has(track),
    toggle(track, key = 'volume') {
      if (shown.has(track)) shown.delete(track); else shown.set(track, key);
      window.SpliceWorkspace?.refresh?.();
    },
  };
})();
