// The Audio workspace: program loudness meter (BS.1770 / EBU R128, measured live on the
// preview's master bus), Analyze program (the exact numbers, measured by the renderer on the
// real mix), the master bus, per-track routing, stems and effects, automation lanes and
// dual-system sync.
(() => {
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const TARGETS = [[-14,'Streaming −14 LUFS'],[-16,'Podcast / mobile −16 LUFS'],[-23,'EBU R128 −23 LUFS'],[-24,'ATSC A/85 −24 LUFS'],
    [-27,'Cinema dialogue −27 LUFS']];
  const STEMS = ['Dialogue','Music','Effects'];
  const ROUTES = {front:'L/R', center:'Centre', lfe:'LFE', surround:'Ls/Rs'};
  let panel = null, editor = null, unsubscribe = null, target = -23, openFx = null, analysis = null, analyzing = null;
  const fmt = (value, unit) => Number.isFinite(value) ? `${value.toFixed(1).replace('-', '−')} ${unit}` : `— ${unit}`;
  // Loudness bars span −54..−0 LUFS (EBU +9 scale shown as absolute LUFS).
  const barHeight = value => Number.isFinite(value) ? Math.max(0, Math.min(100, (value+54)/54*100)) : 0;
  function build() {
    panel = document.createElement('aside');
    panel.className = 'audio-panel panel';
    panel.setAttribute('aria-label', 'Audio mixing and loudness');
    panel.innerHTML = `<div class="panel-top"><div class="panel-title">Audio</div></div><div class="audio-panel-content">
      <section class="loudness" aria-label="Program loudness">
        <div class="loudness-head"><h3>Program loudness</h3>
          <label class="loudness-target"><span class="visually-hidden">Loudness target</span><select data-target aria-label="Loudness target">${TARGETS.map(([value,label])=>`<option value="${value}" ${value===target?'selected':''}>${label}</option>`).join('')}</select></label></div>
        <div class="loudness-body">
          <div class="loudness-bars" aria-hidden="true">
            <div class="loudness-bar" data-bar="momentary"><span></span><b class="loudness-mark"></b><em>M</em></div>
            <div class="loudness-bar" data-bar="shortTerm"><span></span><b class="loudness-mark"></b><em>S</em></div>
          </div>
          <dl class="loudness-values">
            <div><dt>Momentary</dt><dd data-value="momentary">— LUFS</dd></div>
            <div><dt>Short-term</dt><dd data-value="shortTerm">— LUFS</dd></div>
            <div class="loudness-main"><dt>Integrated</dt><dd data-value="integrated">— LUFS</dd></div>
            <div><dt>Range</dt><dd data-value="range">— LU</dd></div>
            <div><dt title="Highest true peak since reset">Peak max</dt><dd data-value="truePeak">— dBTP</dd></div>
            <div><dt title="Loudest momentary value since reset">M max</dt><dd data-value="maxMomentary">— LUFS</dd></div>
          </dl>
        </div>
        <p class="loudness-note" data-note>Live meter on the preview's master bus while playing. Analyze program for the exact numbers.</p>
        <div class="loudness-actions"><button type="button" data-reset>Reset meter</button>
          <select data-analyze-range aria-label="Range to analyze"><option value="all">Entire sequence</option><option value="marks">Timeline In to Out</option></select>
          <button type="button" class="primary" data-analyze>Analyze program</button></div>
        <div class="loudness-analysis" data-analysis role="status" aria-live="polite"></div>
      </section>
      <section class="audio-section" aria-label="Master bus"><h3>Master bus</h3><p class="fx-note">EQ and compressor on the whole mix, before loudness normalization and the export limiter.</p><div class="audio-fx-mount" data-master></div></section>
      <section class="audio-section" aria-label="Audio tracks"><h3>Tracks</h3><p class="fx-note">Track volume and its automation feed the track's effects. Stems group tracks for the stems export.</p><div class="audio-tracks" data-tracks></div></section>
      <section class="audio-section" aria-label="Sync"><h3>Dual-system sound</h3><p class="fx-note">Sync the selected picture clips to separately recorded sound, by waveform or timecode.</p><button type="button" data-sync>Sync picture to sound…</button></section>
    </div>`;
    panel.querySelector('[data-target]').onchange = event => { target = Number(event.target.value); paintMeter(window.SpliceMixer?.meter()); };
    panel.querySelector('[data-reset]').onclick = () => { window.SpliceMixer?.resetMeter(); paintMeter(null); };
    panel.querySelector('[data-analyze]').onclick = analyze;
    panel.querySelector('[data-sync]').onclick = () => window.SpliceSync?.open();
    return panel;
  }
  function paintMeter(snapshot) {
    if (!panel) return;
    for (const key of ['momentary','shortTerm']) {
      const bar = panel.querySelector(`[data-bar="${key}"]`), value = snapshot?.[key];
      bar.firstElementChild.style.height = barHeight(value) + '%';
      bar.classList.toggle('over', Number.isFinite(value) && value > target+1);
      bar.querySelector('.loudness-mark').style.bottom = barHeight(target) + '%';
    }
    const units = {momentary:'LUFS', shortTerm:'LUFS', integrated:'LUFS', range:'LU', truePeak:'dBTP', maxMomentary:'LUFS'};
    for (const [key, unit] of Object.entries(units)) {
      const node = panel.querySelector(`[data-value="${key}"]`);
      node.textContent = fmt(snapshot?.[key], unit);
      if (key === 'integrated') node.dataset.state = !Number.isFinite(snapshot?.integrated) ? '' :
        Math.abs(snapshot.integrated-target) <= 1 ? 'on' : snapshot.integrated > target ? 'loud' : 'quiet';
      if (key === 'truePeak') node.dataset.state = Number.isFinite(snapshot?.truePeak) && snapshot.truePeak > -1 ? 'loud' : '';
    }
  }
  async function analyze() {
    if (analyzing) { analyzing.abort(); return; }
    const state = editor.get(), button = panel.querySelector('[data-analyze]'), out = panel.querySelector('[data-analysis]');
    const options = {codec: 'loudness'};
    if (panel.querySelector('[data-analyze-range]').value === 'marks') {
      const marks = state.project.marks || {};
      if (!Number.isFinite(marks.in) || !Number.isFinite(marks.out)) { out.textContent = 'Mark a timeline In and Out first (I and O).'; return; }
      options.range = {start: marks.in, end: marks.out};  // marks.out is the exclusive end
    }
    analyzing = new AbortController();
    button.textContent = 'Cancel analysis';
    out.textContent = 'Rendering the mix to measure it…';
    try {
      let report = null;
      await editor.exportVideo(options, job => {
        report = job.report?.audio || report;
        out.textContent = `Measuring the mix · ${Math.round(100*job.progress)}%`;
      }, analyzing.signal, {download: false});
      analysis = report;
      showAnalysis();
    } catch (error) {
      out.textContent = analyzing.signal.aborted ? 'Analysis cancelled.' : (error.message || 'Analysis failed.');
    } finally {
      analyzing = null; button.textContent = 'Analyze program';
    }
  }
  function showAnalysis() {
    const out = panel?.querySelector('[data-analysis]');
    if (!out || !analysis?.program) return;
    const p = analysis.program, value = (number, unit) => number === null || number === undefined ? 'silent' : `${String(number.toFixed(1)).replace('-', '−')} ${unit}`;
    const gain = Number.isFinite(p.integratedLufs) ? target-p.integratedLufs : null;
    out.innerHTML = `<h4>Analyzed mix · ${analysis.seconds.toFixed(2)} s from ${analysis.startSeconds.toFixed(2)} s</h4><dl>` +
      `<div><dt>Integrated</dt><dd>${value(p.integratedLufs,'LUFS')}</dd></div><div><dt>Loudness range</dt><dd>${value(p.loudnessRangeLu,'LU')}</dd></div>` +
      `<div><dt>True peak</dt><dd>${value(p.truePeakDbtp,'dBTP')}</dd></div><div><dt>Max momentary</dt><dd>${value(p.maxMomentaryLufs,'LUFS')}</dd></div>` +
      `<div><dt>Max short-term</dt><dd>${value(p.maxShortTermLufs,'LUFS')}</dd></div></dl>` +
      (gain === null ? '' : `<p>To reach ${target} LUFS: ${gain >= 0 ? '+' : '−'}${Math.abs(gain).toFixed(1)} dB` +
        (Number.isFinite(p.truePeakDbtp) && p.truePeakDbtp+gain > -1 ? ' (needs limiting to stay under −1 dBTP).' : ', linear gain fits under −1 dBTP.') + '</p>');
  }
  function audioTracks(project) {
    const tracks = new Set(project.clips.filter(clip => clip.type === 'audio').map(clip => clip.track));
    for (const key of Object.keys(project.trackMix || {})) if (Number(key) < 0) tracks.add(Number(key));
    return [...tracks].sort((a, b) => b-a);
  }
  function renderTracks(state) {
    const host = panel.querySelector('[data-tracks]');
    const rows = audioTracks(state.project).map(track => {
      const mix = state.project.trackMix?.[String(track)] || {};
      const stem = mix.stem || '';
      const fxCount = ['eq','comp','gate','denoise'].filter(kind => mix.fx?.[kind] && mix.fx[kind].enabled !== false).length;
      return `<div class="audio-track" data-track="${track}"><div class="audio-track-row"><strong>A${Math.abs(track)}</strong>` +
        `<label><span>Stem</span><input list="splice-stems" data-track-field="stem" maxlength="32" value="${esc(stem)}" placeholder="Other" aria-label="A${Math.abs(track)} stem"></label>` +
        `<label><span>Channels</span><select data-track-field="channels" aria-label="A${Math.abs(track)} channels"><option value="stereo">Stereo</option><option value="mono" ${mix.channels==='mono'?'selected':''}>Mono</option></select></label>` +
        `<label><span>5.1</span><select data-track-field="route" aria-label="A${Math.abs(track)} 5.1 placement">${Object.entries(ROUTES).map(([key,name])=>`<option value="${key}" ${(mix.route||'front')===key?'selected':''}>${name}</option>`).join('')}</select></label>` +
        `<button type="button" data-track-fx aria-expanded="${openFx===track}">Effects${fxCount ? ` · ${fxCount}` : ''}</button>` +
        `<button type="button" data-track-automation aria-pressed="${window.SpliceAutomation?.isShown(track) ? 'true' : 'false'}">Automation</button></div>` +
        (openFx === track ? '<div class="audio-fx-mount" data-track-mount></div>' : '') + '</div>';
    });
    host.innerHTML = (rows.join('') || '<p class="fx-note">No audio tracks yet.</p>') +
      `<datalist id="splice-stems">${STEMS.map(name => `<option value="${name}">`).join('')}</datalist>`;
    for (const row of host.querySelectorAll('.audio-track')) {
      const track = Number(row.dataset.track);
      for (const input of row.querySelectorAll('[data-track-field]')) input.onchange = () => {
        try { editor.updateTrackMix(track, {[input.dataset.trackField]: input.value.trim() || undefined}); }
        catch (error) { window.alert(error.message); }
      };
      row.querySelector('[data-track-fx]').onclick = () => { openFx = openFx === track ? null : track; renderTracks(editor.get()); };
      row.querySelector('[data-track-automation]').onclick = () => { window.SpliceAutomation?.toggle(track); renderTracks(editor.get()); };
      const mount = row.querySelector('[data-track-mount]');
      if (mount) window.SpliceAudioFX.render(mount, state.project.trackMix?.[String(track)]?.fx, {owner: 'track-' + track,
        onChange: fx => editor.updateTrackMix(track, {fx})});
    }
  }
  let renderedFor = null;
  function refresh(state) {
    if (!panel?.isConnected) return;
    if (renderedFor !== state.project) {
      renderedFor = state.project;
      // Do not rebuild while the user is typing in this panel.
      if (!panel.contains(document.activeElement) || document.activeElement.tagName === 'BUTTON') {
        window.SpliceAudioFX.render(panel.querySelector('[data-master]'), state.project.masterFx,
          {kinds: ['eq','comp'], owner: 'master', onChange: fx => editor.setMasterFx(fx)});
        renderTracks(state);
      } else renderedFor = null;
    }
    const master = panel.querySelector('[data-master]');
    window.SpliceAudioFX.paintReduction(master, 'master');
    const open = panel.querySelector('[data-track-mount]');
    if (open) window.SpliceAudioFX.paintReduction(open, 'track:' + open.closest('.audio-track').dataset.track);
  }
  function toggle(open) {
    const workspace = $('.workspace');
    editor = window.Editor;
    if (open && !panel?.isConnected) {
      workspace.prepend(panel || build());
      unsubscribe = window.SpliceMixer?.onMeter(paintMeter) || null;
      renderedFor = null;
      refresh(editor.get());
      paintMeter(window.SpliceMixer?.meter());
      showAnalysis();
    } else if (!open && panel?.isConnected) {
      panel.remove();
      unsubscribe?.(); unsubscribe = null;
    }
    workspace.classList.toggle('audio-workspace', !!open);
  }
  window.SpliceAudioPanel = {toggle, refresh, isOpen: () => !!panel?.isConnected};
})();
// The export dialog's Audio group: loudness normalization (off unless chosen), channel layout,
// stems, and the audio report every container now carries.
(() => {
  const TARGETS = [['off','Off · leave program loudness unchanged'],['-14','−14 LUFS · streaming'],['-16','−16 LUFS · podcast, mobile'],
    ['-23','−23 LUFS · EBU R128 broadcast'],['-24','−24 LUFS · ATSC A/85 broadcast'],['-27','−27 LUFS · cinema, dialogue-led'],['custom','Custom…']];
  function extend(group) {
    group.insertAdjacentHTML('beforeend',
      `<label>Loudness<select name="normalize" aria-describedby="export-normalize-help">${TARGETS.map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select></label>` +
      `<label hidden>Target (LUFS)<input name="normalizeTarget" type="number" min="-36" max="-8" step="0.1" value="-20"></label>` +
      `<label hidden>Method<select name="normalizeMode"><option value="linear">Gain only · no dynamics change</option><option value="limit">Gain, then true-peak limiting</option></select></label>` +
      `<p class="export-help" id="export-normalize-help">Measured in two passes (BS.1770). Gain only stops at the true-peak ceiling; limiting reaches the target and is reported.</p>` +
      `<label>Channels<select name="audioLayout"><option value="stereo">Stereo</option><option value="5.1">5.1 · L R C LFE Ls Rs</option><option value="5.1-downmix">5.1 mix delivered as a stereo downmix</option></select></label>` +
      `<label>Deliver<select name="audioDeliver" aria-describedby="export-stems-help"><option value="program">Program with the picture</option><option value="stems">Stems and mix as WAV files (ZIP, no picture)</option></select></label>` +
      `<label class="export-check" hidden>Per-track iso WAVs<input name="isoTracks" type="checkbox"></label>` +
      `<p class="export-help" id="export-stems-help" hidden>One WAV per stem (set stems in the Audio workspace), full length and sample-aligned, before the master compressor and limiter.</p>`);
    const form = group.closest('form');
    const sync = () => {
      const normalize = form.elements.normalize.value, stems = form.elements.audioDeliver.value === 'stems';
      form.elements.normalizeTarget.closest('label').hidden = normalize !== 'custom';
      form.elements.normalizeMode.closest('label').hidden = normalize === 'off';
      form.elements.isoTracks.closest('label').hidden = !stems;
      form.querySelector('#export-stems-help').hidden = !stems;
      const limiter = form.elements.limiter;
      form.elements.normalizeMode.setCustomValidity(normalize !== 'off' && form.elements.normalizeMode.value === 'limit' && limiter && !limiter.checked ?
        'Limiting needs the limiter on.' : '');
    };
    for (const name of ['normalize','normalizeMode','audioDeliver','limiter']) form.elements[name]?.addEventListener('change', sync);
    sync();
  }
  function apply(form, options) {
    const normalize = form.elements.normalize?.value;
    if (normalize && normalize !== 'off') {
      options.normalize = normalize === 'custom' ? form.elements.normalizeTarget.valueAsNumber : Number(normalize);
      options.normalizeMode = form.elements.normalizeMode.value;
    }
    if (form.elements.audioLayout && form.elements.audioLayout.value !== 'stereo') options.audioLayout = form.elements.audioLayout.value;
    if (form.elements.audioDeliver?.value === 'stems') {
      options.codec = 'stems';
      if (form.elements.isoTracks.checked) options.isoTracks = true;
      delete options.colorSpace; delete options.aacPeakSafe;
    }
    return options;
  }
  const db = value => value === null || value === undefined ? 'silent' : (value > 0 ? '+' : value < 0 ? '−' : '') + Math.abs(value).toFixed(1);
  function describe(audio) {
    if (!audio || typeof audio !== 'object' || !audio.output) return [];
    const lines = [], i = audio.input || {}, o = audio.output || {};
    lines.push(`${audio.container || 'Audio'} · ${audio.layout === '5.1' ? '5.1' : audio.layout === '5.1-downmix' ? 'stereo downmix of 5.1' : 'stereo'}: ` +
      `${db(o.integratedLufs)} LUFS integrated · ${db(o.truePeakDbtp)} dBTP true peak · ${o.loudnessRangeLu ?? '—'} LU range`);
    lines.push(`Mix before finishing: ${db(i.integratedLufs)} LUFS · ${db(i.truePeakDbtp)} dBTP · ${i.loudnessRangeLu ?? '—'} LU · gain applied ${db(audio.gainDb || 0)} dB`);
    if (audio.normalization) lines.push(`Normalization to ${audio.normalization.targetLufs} LUFS (${audio.normalization.mode === 'limit' ? 'gain and limiting' : 'gain only'}): ` +
      (audio.normalization.reached ? 'reached' : 'not reached') + (audio.normalization.note ? ` — ${audio.normalization.note}` : ''));
    if (audio.limiter) lines.push(audio.limiter.acted ? `True-peak limiter acted in ${audio.limiter.regions} place${audio.limiter.regions === 1 ? '' : 's'}, at most ${db(audio.limiter.maxReductionDb)} dB, ceiling ${db(audio.limiter.ceilingDbtp)} dBTP`
      : `True-peak limiter idle (ceiling ${db(audio.limiter.ceilingDbtp)} dBTP): the program passed bit for bit`);
    if (audio.aacPeakSafe && audio.aacGainDb) lines.push(`Lowered ${Math.abs(audio.aacGainDb).toFixed(1)} dB more to keep the decoded AAC under the ceiling`);
    if (audio.stems) lines.push(`Stems: ${audio.stems.map(stem => `${stem.name} (${stem.tracks.join(', ')})`).join(' · ')}` + (audio.stemsSumToMix ? ' — they sum to the mix' : ` — ${audio.stemsNote}`));
    return lines;
  }
  window.SpliceExportAudio = {extend, apply, describe};
})();
