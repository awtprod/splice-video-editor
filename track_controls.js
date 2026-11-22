(() => {
  const key = lane => `${lane.kind}:${lane.track}`;
  function renderHeader(head, lane, project, editor) {
    const state = project.trackState?.[key(lane)] || (lane.kind === 'title' ? project.trackState?.['video:4'] : null) || {};
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
      node.textContent = text;
      node.title = label;
      node.setAttribute('aria-label', label);
      node.setAttribute('aria-pressed', String(active));
      node.classList.toggle('active', active);
      node.onclick = event => { event.stopPropagation(); action(); };
      controls.append(node);
    };
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
