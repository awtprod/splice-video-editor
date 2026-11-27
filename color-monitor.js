/* Program monitor controls for grading: processing quality, grade bypass, before/after split,
 * monitoring views (luma, single channels, false colour, legal-range warning, zebra) and framing
 * guides. Everything here is a monitor setting: it is not saved in the project and not exported. */
(() => {
  const QUALITY_KEY = 'splice.monitorQuality';
  const QUALITIES = {auto:'Auto', full:'Full', half:'Half', quarter:'Quarter'};
  const VIEWS = {normal:'Normal', luma:'Luma only', red:'Red channel', green:'Green channel', blue:'Blue channel',
    falseColor:'False colour', legal:'Legal range warning', zebra:'Zebra (clipping)'};
  const GUIDES = {none:'No guides', safe:'Title/action safe', '2.39':'2.39:1', '1.85':'1.85:1', '4:3':'4:3', '1:1':'1:1', '9:16':'9:16'};
  const savedQuality = localStorage.getItem(QUALITY_KEY);
  let quality = Object.hasOwn(QUALITIES, savedQuality) ? savedQuality : 'auto';
  // Auto steps down a level during playback while frames take longer than the frame period.
  let autoLevel = 0, slowFrames = 0, fastFrames = 0;
  const editor = () => window.Editor;

  // The monitor's processing size for a sequence. Full is the sequence size (at most UHD), Half and
  // Quarter divide it; Auto is the shown size in device pixels, at least 960 wide, at most Full.
  function size(project, display = 0) {
    const max = window.SplicePicture?.maxFrame || {width:3840, height:2160};
    const aspect = project.width/project.height;
    let width = Math.min(project.width, max.width, max.height*aspect);
    if (quality === 'half') width /= 2;
    else if (quality === 'quarter') width /= 4;
    else if (quality === 'auto') width = Math.min(width, Math.max(960, display))/(2**autoLevel);
    width = Math.max(160, Math.round(width));
    return {width, height:Math.max(1, Math.min(2160, Math.round(width/aspect)))};
  }
  // Called once per painted frame while playing: adapt Auto to what the machine keeps up with.
  function measure(frameMs, fps, playing) {
    if (quality !== 'auto') return false;
    if (!playing) { if (autoLevel && ++fastFrames > 30) { autoLevel = 0; fastFrames = 0; return true; } return false; }
    const budget = 1000/fps*.8;
    if (frameMs > budget) { fastFrames = 0; if (++slowFrames >= 8 && autoLevel < 2) { autoLevel++; slowFrames = 0; return true; } }
    else { slowFrames = 0; if (autoLevel && frameMs < budget/3 && ++fastFrames >= 90) { autoLevel--; fastFrames = 0; return true; } }
    return false;
  }
  function changed() { document.dispatchEvent(new CustomEvent('splice-monitor-change')); }
  function setQuality(value) {
    if (!(value in QUALITIES)) throw new Error('Unknown monitor quality');
    quality = value; autoLevel = 0; localStorage.setItem(QUALITY_KEY, value); changed();
  }
  const options = (table, selected) => Object.entries(table).map(([key, title]) => `<option value="${key}" ${key === selected ? 'selected' : ''}>${title}</option>`).join('');
  function mount() {
    const actions = document.querySelector('.viewer-actions');
    if (!actions || actions.querySelector('.monitor-color-controls')) return;
    const group = document.createElement('span');
    group.className = 'monitor-color-controls';
    group.innerHTML =
      `<select class="monitor-select" data-monitor-quality aria-label="Monitor quality" title="Processing resolution of the program monitor">${options(QUALITIES, quality)}</select>` +
      `<select class="monitor-select" data-monitor-view aria-label="Monitor view" title="Monitoring view (not exported)">${options(VIEWS, 'normal')}</select>` +
      `<select class="monitor-select" data-monitor-guides aria-label="Monitor guides" title="Framing guides">${options(GUIDES, 'none')}</select>` +
      `<button type="button" class="quiet" data-monitor-bypass aria-pressed="false" title="Show every clip without its grade (monitor only)">Bypass</button>` +
      `<button type="button" class="quiet" data-monitor-split aria-pressed="false" title="Split the monitor: graded on the left, ungraded on the right">Split</button>`;
    actions.prepend(group);
    group.querySelector('[data-monitor-quality]').addEventListener('change', event => setQuality(event.target.value));
    group.querySelector('[data-monitor-view]').addEventListener('change', event => { editor().monitor.view = event.target.value; changed(); });
    group.querySelector('[data-monitor-guides]').addEventListener('change', event => guides(event.target.value));
    const bypass = group.querySelector('[data-monitor-bypass]'), split = group.querySelector('[data-monitor-split]');
    bypass.addEventListener('click', () => {
      const on = !editor().monitor.bypass;
      editor().monitor.bypass = on; bypass.setAttribute('aria-pressed', String(on));
      document.querySelector('#monitor-frame')?.classList.toggle('grade-bypassed', on);
      changed();
    });
    split.addEventListener('click', () => setSplit(editor().monitor.split == null ? .5 : null));
  }
  // Before/after split: graded left of the line, ungraded right; drag the handle to move it.
  function setSplit(value) {
    const frame = document.querySelector('#monitor-frame'), button = document.querySelector('[data-monitor-split]');
    editor().monitor.split = value == null ? null : Math.max(0, Math.min(1, value));
    button?.setAttribute('aria-pressed', String(value != null));
    let handle = frame?.querySelector('.monitor-split-handle');
    if (value == null) { handle?.remove(); changed(); return; }
    if (frame && !handle) {
      handle = document.createElement('div');
      handle.className = 'monitor-split-handle'; handle.setAttribute('role', 'slider'); handle.tabIndex = 0;
      handle.setAttribute('aria-label', 'Before/after split position'); handle.setAttribute('aria-valuemin', '0'); handle.setAttribute('aria-valuemax', '100');
      handle.innerHTML = '<span>Graded</span><span>Ungraded</span>';
      frame.append(handle);
      const move = event => {
        const box = frame.getBoundingClientRect();
        setSplit((event.clientX-box.left)/box.width);
      };
      handle.addEventListener('pointerdown', event => {
        handle.setPointerCapture(event.pointerId);
        const up = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', up); };
        handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
      });
      handle.addEventListener('keydown', event => {
        const step = event.shiftKey ? .1 : .01;
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault(); event.stopPropagation();
          setSplit(editor().monitor.split+(event.key === 'ArrowLeft' ? -step : step));
        }
      });
    }
    if (handle) { handle.style.left = `${editor().monitor.split*100}%`; handle.setAttribute('aria-valuenow', String(Math.round(editor().monitor.split*100))); }
    changed();
  }
  // Framing guides drawn over the monitor (not part of the picture, not read by the scopes).
  function guides(kind) {
    const frame = document.querySelector('#monitor-frame');
    frame?.querySelector('.monitor-guides')?.remove();
    frame?.classList.toggle('show-safe-area', kind === 'safe');
    if (!frame || kind === 'none' || kind === 'safe') return;
    const [a, b] = kind.split(':').map(Number), target = b ? a/b : a, project = editor().get().project;
    const aspect = project.width/project.height, overlay = document.createElement('div');
    overlay.className = 'monitor-guides'; overlay.setAttribute('aria-hidden', 'true');
    // The guide frames the target shape inside the picture: bars top and bottom, or left and right.
    const box = target >= aspect ? {width:100, height:aspect/target*100} : {width:target/aspect*100, height:100};
    overlay.innerHTML = `<div class="monitor-guide-box" style="width:${box.width}%;height:${box.height}%"><span>${GUIDES[kind]}</span></div>`;
    frame.append(overlay);
  }
  document.addEventListener('DOMContentLoaded', mount);
  if (document.readyState !== 'loading') setTimeout(mount);
  window.SpliceMonitor = {size, measure, setQuality, setSplit, quality:() => quality, autoLevel:() => autoLevel, qualities:Object.keys(QUALITIES), views:Object.keys(VIEWS), guides};
})();
