// Sync picture to sound: the selected picture clips are matched with separately recorded sound
// (one chosen file, or every sound file in a bin) by waveform or timecode on the server
// (audio_sync.py), then linked as picture/sound pairs. Low-confidence or drifting matches are
// listed and left alone unless the user applies them.
(() => {
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function open() {
    const editor = window.Editor, state = editor.get(), project = state.project;
    const chosen = new Set(window.SpliceWorkspace?.selection?.() || [state.selectedId]);
    const pictures = project.clips.filter(clip => chosen.has(clip.id) && clip.type === 'video');
    const sounds = project.media.filter(item => item.type === 'audio');
    const bins = [...new Set(sounds.map(item => item.bin).filter(Boolean))];
    const dialog = document.createElement('dialog');
    dialog.className = 'audio-sync-dialog';
    dialog.setAttribute('aria-labelledby', 'audio-sync-title');
    dialog.innerHTML = `<form method="dialog"><h2 id="audio-sync-title">Sync picture to sound</h2>
      <p>${pictures.length ? `${pictures.length} picture clip${pictures.length === 1 ? '' : 's'} selected.` : 'Select one or more picture clips on the timeline first.'}</p>
      <label>Sound<select name="sound"><option value="*">Find the matching take among all sound files</option>
        ${bins.map(bin => `<option value="bin:${esc(bin)}">Find the matching take in bin ${esc(bin)}</option>`).join('')}
        ${sounds.map(item => `<option value="${esc(item.id)}">${esc(item.name || item.id)}</option>`).join('')}</select></label>
      <label>Method<select name="method"><option value="auto">Timecode when both files have it, else waveform</option><option value="waveform">Waveform</option><option value="timecode">Timecode</option></select></label>
      <label>Search range<span class="fx-input"><input name="search" type="number" min="1" max="600" step="1" value="60" aria-label="Search range (seconds)"><span class="fx-unit">± s</span></span></label>
      <label>Camera sound<select name="scratch"><option value="mute">Turn off the camera's scratch sound</option><option value="keep">Keep the camera sound playing</option></select></label>
      <div class="audio-sync-results" role="status" aria-live="polite"></div>
      <footer><button type="button" data-close>Close</button><button type="button" data-apply hidden>Apply low-confidence matches too</button><button type="submit" class="primary" ${pictures.length && sounds.length ? '' : 'disabled'}>Sync</button></footer></form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form'), out = dialog.querySelector('.audio-sync-results'), applyAll = dialog.querySelector('[data-apply]');
    const close = () => { dialog.close(); dialog.remove(); };
    dialog.querySelector('[data-close]').onclick = close;
    dialog.addEventListener('cancel', () => dialog.remove(), {once: true});
    let pending = [];
    form.onsubmit = async event => {
      event.preventDefault();
      pending = [];
      applyAll.hidden = true;
      const choice = form.elements.sound.value;
      const candidates = choice === '*' ? sounds : choice.startsWith('bin:') ? sounds.filter(item => item.bin === choice.slice(4)) :
        sounds.filter(item => item.id === choice);
      const pairs = [];
      for (const picture of pictures) for (const sound of candidates) pairs.push({picture, sound});
      if (pairs.length > 40) { out.textContent = `That is ${pairs.length} comparisons; choose a bin or one sound file (at most 40).`; return; }
      const media = id => project.media.find(item => item.id === id);
      out.textContent = `Comparing ${pairs.length} picture and sound pair${pairs.length === 1 ? '' : 's'}…`;
      form.querySelector('[type=submit]').disabled = true;
      try {
        const response = await fetch('/api/audio/sync', {method: 'POST', headers: {'Content-Type': 'application/json'},
          signal: AbortSignal.timeout(600000), body: JSON.stringify({method: form.elements.method.value, fps: project.fps,
            searchSeconds: Number(form.elements.search.value),
            pairs: pairs.map(({picture, sound}) => {
              const item = media(picture.mediaId);
              return {picture: item.originalSrc || item.src, sound: sound.originalSrc || sound.src,
                pictureStream: editor.clipAudioStream(picture, item) ?? 0, soundStream: sound.audioStream ?? 0,
                pictureSeconds: Math.min(180, Math.max(1, (Number(picture.in) || 0) + picture.duration*(Number(picture.speed) || 1)))};
            })})});
        if (!response.ok) throw Error(await response.text());
        const {results} = await response.json();
        // Best match per picture clip.
        const best = new Map();
        results.forEach((result, index) => {
          const pair = pairs[index], offset = result.offsetSeconds;
          if (Number.isFinite(offset) && !editor.syncSoundPlacement(pair.picture, pair.sound, offset, project.fps)) return;
          const current = best.get(pair.picture.id);
          const score = (result.ok ? 10 : 0) + (result.confidence || 0);
          if (!current || score > current.score) best.set(pair.picture.id, {...pair, result, score});
        });
        const good = [...best.values()].filter(entry => entry.result.ok), weak = [...best.values()].filter(entry => !entry.result.ok);
        const line = ({picture, sound, result}) => `<li data-state="${result.ok ? 'ok' : 'weak'}"><strong>${esc(picture.title || media(picture.mediaId)?.name || 'Clip')}</strong> ↔ ${esc(sound.name || sound.id)}: ` +
          (Number.isFinite(result.offsetSeconds) ? `${result.offsetSeconds.toFixed(4)} s · ${result.offsetFrames} frames · ${result.offsetSamples} samples at 48 kHz` : 'no offset') +
          ` · ${result.method}${result.method === 'waveform' ? `, confidence ${result.confidence}` : ''}${result.note ? ` — ${esc(result.note)}` : ''}</li>`;
        const unmatched = pictures.filter(picture => !best.has(picture.id)).map(picture =>
          `<li>${esc(picture.title || media(picture.mediaId)?.name || 'Clip')}: no overlapping sound.</li>`);
        out.innerHTML = `<ul>${[...good, ...weak].map(line).concat(unmatched).join('')}</ul>`;
        const scratch = form.elements.scratch.value;
        if (good.length) editor.syncSound(good.map(({picture, sound, result}) => ({pictureId: picture.id, mediaId: sound.id, offset: result.offsetSeconds})), {scratch});
        out.insertAdjacentHTML('beforeend', `<p>${good.length} synced and linked.${weak.length ? ` ${weak.length} not synced (low confidence or drift).` : ''}</p>`);
        pending = weak.filter(entry => Number.isFinite(entry.result.offsetSeconds));
        applyAll.hidden = !pending.length;
        applyAll.onclick = () => {
          try { editor.syncSound(pending.map(({picture, sound, result}) => ({pictureId: picture.id, mediaId: sound.id, offset: result.offsetSeconds})), {scratch}); }
          catch (error) { out.insertAdjacentHTML('beforeend', `<p>${esc(error.message)}</p>`); return; }
          out.insertAdjacentHTML('beforeend', `<p>${pending.length} low-confidence match${pending.length === 1 ? '' : 'es'} applied.</p>`);
          applyAll.hidden = true; pending = [];
        };
      } catch (error) {
        out.textContent = error.message || 'Sync failed.';
      } finally {
        form.querySelector('[type=submit]').disabled = false;
      }
    };
    dialog.showModal();
  }
  window.SpliceSync = {open};
})();
