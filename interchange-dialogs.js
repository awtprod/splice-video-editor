// File > Export timeline… and File > Import timeline…: FCPXML, CMX3600 EDL and OpenTimelineIO.
window.SpliceInterchange = (() => {
  const RATES = ['24000/1001', '24', '25', '30000/1001', '30', '50', '60000/1001', '60'];
  const DROP_RATES = [30000 / 1001, 60000 / 1001];
  const rateLabel = r => { const [n, d] = r.split('/').map(Number); return Number((n / (d || 1)).toFixed(3)) + ' fps'; };
  const nearestRate = fps => RATES.reduce((best, r) => {
    const [n, d] = r.split('/').map(Number), [bn, bd] = best.split('/').map(Number);
    return Math.abs(n / (d || 1) - fps) < Math.abs(bn / (bd || 1) - fps) ? r : best;
  }, '25');
  function dialog(heading) {
    const node = document.createElement('dialog');
    node.className = 'studio-dialog interchange-dialog';
    const title = document.createElement('h2'); title.textContent = heading; node.append(title);
    document.body.append(node);
    node.addEventListener('close', () => node.remove(), { once: true });
    return node;
  }
  function save(blob, name) {
    const url = URL.createObjectURL(blob);
    Object.assign(document.createElement('a'), { href: url, download: name }).click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  async function post(route, payload) {
    const response = await fetch('/api/interchange/' + route, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload), signal: AbortSignal.timeout(300000),
    });
    const body = await response.json().catch(() => ({ error: 'The server returned an unreadable reply.' }));
    if (!response.ok) throw Error(body.error || 'Timeline interchange failed.');
    return body;
  }

  function exportTimeline() {
    const state = Editor.get(), project = state.project, node = dialog('Export timeline');
    const form = document.createElement('form');
    const drop = DROP_RATES.some(r => Math.abs(project.fps - r) < 1e-3);
    form.innerHTML = `<p class="export-spec"></p>
      <fieldset class="interchange-formats"><legend>Formats</legend>
        <label><input type="checkbox" name="fcpxml" checked> FCPXML 1.10 · Resolve, Final Cut Pro</label>
        <label><input type="checkbox" name="edl"> CMX3600 EDL · one per picture track</label>
        <label class="interchange-sub"><input type="checkbox" name="audioEdl"> Sound EDLs as well</label>
        <label><input type="checkbox" name="otio"> OpenTimelineIO (.otio)</label>
      </fieldset>
      <label>Range<select name="rangeMode"><option value="all">Entire sequence</option><option value="custom">In to out (seconds)</option></select></label>
      <label hidden>In (seconds)<input name="rangeStart" type="number" min="0" step="any"></label>
      <label hidden>Out (seconds)<input name="rangeEnd" type="number" min="0" step="any"></label>
      <label>Start timecode<input name="startTimecode" value="01:00:00:00" pattern="\\d{2}:\\d{2}:\\d{2}[:;]\\d{2}" required></label>
      <label${drop ? '' : ' hidden'}>Timecode<select name="dropFrame"><option value="1">Drop frame</option><option value="0">Non-drop frame</option></select></label>
      <label>Unreadable source timecode<select name="unknownTimecode"><option value="refuse">Stop and list the sources</option><option value="zero">Write 00:00:00:00 and flag each clip</option></select></label>
      <label class="interchange-check"><input type="checkbox" name="package"> Turnover package (.zip with a report of what each format cannot carry)</label>
      <p role="status" class="render-state"></p>
      <details class="interchange-report" hidden><summary>Export report</summary><pre></pre><button type="button" data-report>Save report</button></details>
      <footer><button type="button" data-close>Close</button><button type="submit" class="primary">Export</button></footer>`;
    form.querySelector('.export-spec').textContent = `${project.name} · ${project.width} × ${project.height} · ${Number(project.fps.toFixed(3))} fps`;
    const fields = form.elements, status = form.querySelector('[role=status]');
    const range = [fields.rangeStart, fields.rangeEnd];
    range[0].value = '0';
    range[1].value = String(Number(state.duration.toFixed(6)));
    fields.rangeMode.onchange = () => {
      for (const field of range) { field.closest('label').hidden = fields.rangeMode.value !== 'custom'; field.required = !field.closest('label').hidden; }
    };
    const syncSound = () => { fields.audioEdl.closest('label').hidden = !fields.edl.checked; };
    fields.edl.onchange = syncSound; syncSound();
    form.querySelector('[data-close]').onclick = () => node.close();
    let report = '';
    form.querySelector('[data-report]').onclick = () => save(new Blob([report], { type: 'text/plain' }), 'timeline-report.txt');
    form.onsubmit = async event => {
      event.preventDefault();
      const formats = ['fcpxml', 'edl', 'otio'].filter(name => fields[name].checked);
      if (!formats.length) { status.textContent = 'Choose at least one format.'; return; }
      const submit = form.querySelector('[type=submit]'); submit.disabled = true;
      status.textContent = 'Reading source timecode…';
      try {
        const prepared = window.SpliceMedia?.forPremiere ? await SpliceMedia.forPremiere(Editor.get().project) : structuredClone(Editor.get().project);
        delete prepared.premiereOriginal; delete prepared.premiereManaged;
        const options = {
          formats, audioEdl: fields.audioEdl.checked, package: fields.package.checked,
          startTimecode: fields.startTimecode.value.trim(), dropFrame: drop && fields.dropFrame.value === '1',
          unknownTimecode: fields.unknownTimecode.value,
        };
        if (fields.rangeMode.value === 'custom') options.range = { start: range[0].valueAsNumber, end: range[1].valueAsNumber };
        const result = await post('export', { project: prepared, options });
        report = result.report;
        if (result.package) {
          const bytes = Uint8Array.from(atob(result.package.base64), c => c.charCodeAt(0));
          save(new Blob([bytes], { type: 'application/zip' }), result.package.name);
        } else {
          for (const file of result.files) save(new Blob([file.content], { type: 'text/plain' }), file.name);
        }
        const details = form.querySelector('.interchange-report');
        details.hidden = false; details.querySelector('pre').textContent = report;
        status.textContent = `Exported ${result.files.length} file${result.files.length === 1 ? '' : 's'}` +
          (result.issues ? ` · ${result.issues} item${result.issues === 1 ? '' : 's'} in the report could not be carried exactly.` : '.');
      } catch (error) {
        status.textContent = error.message;
      } finally { submit.disabled = false; }
    };
    node.append(form); node.showModal();
  }

  function importTimeline() {
    const project = Editor.get().project, node = dialog('Import timeline');
    const form = document.createElement('form');
    form.innerHTML = `<p>Opens an FCPXML, CMX3600 EDL or OpenTimelineIO file as a new project. Media found in Splice plays at once; other sources arrive offline for relinking. Anything not imported is listed under Project compatibility.</p>
      <label>Timeline file<input type="file" name="file" accept=".fcpxml,.xml,.edl,.otio" required></label>
      <label hidden>EDL frame rate<select name="fps"></select></label>
      <p role="status" class="render-state"></p>
      <footer><button type="button" data-close>Cancel</button><button type="submit" class="primary">Import</button></footer>`;
    const fields = form.elements, status = form.querySelector('[role=status]');
    for (const rate of RATES) fields.fps.append(new Option(rateLabel(rate), rate));
    fields.fps.value = nearestRate(project.fps);
    fields.file.onchange = () => { fields.fps.closest('label').hidden = !/\.edl$/i.test(fields.file.files[0]?.name || ''); };
    form.querySelector('[data-close]').onclick = () => node.close();
    form.onsubmit = async event => {
      event.preventDefault();
      const file = fields.file.files[0];
      if (!file) return;
      if (file.size > 30 * 1024 * 1024) { status.textContent = 'The timeline file is larger than 30 MB.'; return; }
      const submit = form.querySelector('[type=submit]'); submit.disabled = true; status.textContent = 'Reading timeline…';
      try {
        const result = await post('import', { filename: file.name, text: await file.text(),
          options: { fps: fields.fps.value, width: project.width, height: project.height } });
        const name = (result.project.name || 'Imported').replace(/[^a-z0-9_-]+/gi, '-') + '.splice.json';
        const opened = new File([JSON.stringify(result.project)], name, { type: 'application/json' });
        await (window.SpliceSession?.openFile ? SpliceSession.openFile(opened) : Editor.openProject(opened));
        node.close();
      } catch (error) {
        status.textContent = error.message; submit.disabled = false;
      }
    };
    node.append(form); node.showModal();
  }

  document.getElementById('export-timeline')?.addEventListener('click', exportTimeline);
  document.getElementById('import-timeline')?.addEventListener('click', importTimeline);
  return { exportTimeline, importTimeline };
})();
