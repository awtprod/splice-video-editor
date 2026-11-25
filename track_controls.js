(() => {
  const key = lane => `${lane.kind}:${lane.track}`;
  function eyeIcon(closed) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.classList.add('track-eye');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', closed ? 'M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6ZM4 4l16 16' : 'M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6Zm9-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z');
    svg.append(path);
    return svg;
  }
  function renderHeader(head, lane, project, editor) {
    const state = project.trackState?.[key(lane)] || (lane.kind === 'title' ? project.trackState?.['video:'+lane.track] : null) || {};
    const mix = project.trackMix?.[String(lane.track)] || {};
    const locked = !!state.locked;
    const synced = state.syncLocked !== false;
    head.classList.toggle('is-locked', locked);
    const icon = document.createElement('span');
    icon.className = 'track-icon';
    icon.textContent = lane.kind === 'title' ? 'T' : lane.kind === 'audio' ? '♫' : '▣';
    const name = document.createElement('span');
    name.className = 'track-name';
    name.textContent = lane.name;
    const controls = document.createElement('span');
    controls.className = 'track-controls';
    const button = (text, label, active, action) => {
      const node = document.createElement('button');
      node.type = 'button';
      if (text instanceof Node) node.append(text);
      else node.textContent = text;
      node.title = label;
      node.setAttribute('aria-label', label);
      node.setAttribute('aria-pressed', String(active));
      node.classList.toggle('active', active);
      node.onclick = event => { event.stopPropagation(); action(); };
      controls.append(node);
    };
    if (lane.kind !== 'audio') {
      // Track output toggle: a hidden picture track is left out of the program monitor and the render.
      const hidden = state.hidden === true;
      button(eyeIcon(hidden), `${hidden ? 'Show' : 'Hide'} ${lane.name}`, hidden,
        () => editor.setTrackState(lane.track, lane.kind, {hidden: !hidden}));
      head.classList.toggle('is-hidden', hidden);
    }
    button('L', `${locked ? 'Unlock' : 'Lock'} ${lane.name}`, locked,
      () => editor.setTrackState(lane.track, lane.kind, {locked: !locked}));
    button('↔', `${synced ? 'Disable' : 'Enable'} ripple sync for ${lane.name}`, synced,
      () => editor.setTrackState(lane.track, lane.kind, {syncLocked: !synced}));
    if (lane.kind === 'audio') {
      button('M', `${mix.mute ? 'Unmute' : 'Mute'} ${lane.name}`, !!mix.mute,
        () => editor.updateTrackMix(lane.track, {mute: !mix.mute}));
      button('S', `${mix.solo ? 'Clear solo on' : 'Solo'} ${lane.name}`, !!mix.solo,
        () => editor.updateTrackMix(lane.track, {solo: !mix.solo}));
    }
    head.replaceChildren(icon, name, controls);
  }
  window.SpliceTracks = {renderHeader};
})();
