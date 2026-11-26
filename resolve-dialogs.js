// Open DaVinci Resolve projects: a .drp export, or a project in a Resolve disk library.
// The .drp's bytes stay outside the editing model (which is cloned for every undo step) in
// their own IndexedDB store, keyed by the file's SHA-256 that the project carries.
// Each timeline of an opened project has its own working copy: switching timelines keeps the
// timeline being left (in the "working" store, keyed by the open's id and the Resolve timeline
// id) and comes back to it with its edits, rather than reading it from Resolve again.
window.SpliceResolve = (() => {
  const $ = id => document.getElementById(id);
  const KEEP = 8, KEEP_WORKING = 200;
  let database;
  const request = req => new Promise((ok, fail) => { req.onsuccess = () => ok(req.result); req.onerror = () => fail(req.error); });
  function open() {
    database ||= new Promise((ok, fail) => {
      const req = indexedDB.open('splice-resolve', 2);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('originals')) db.createObjectStore('originals', {keyPath:'key'});
        if (!db.objectStoreNames.contains('working')) db.createObjectStore('working', {keyPath:'key'});
      };
      req.onsuccess = () => { req.result.onversionchange = () => { req.result.close(); database = null; }; ok(req.result); };
      req.onerror = () => { database = null; fail(req.error); };
      req.onblocked = () => { database = null; fail(Error('Close other Splice windows so Resolve storage can be upgraded.')); };
    });
    return database;
  }
  async function put(storeName, row, keep) {
    const db = await open();
    const tx = db.transaction(storeName, 'readwrite'), store = tx.objectStore(storeName);
    const done = new Promise((ok, fail) => { tx.oncomplete = ok; tx.onabort = () => fail(tx.error || Error('Browser storage refused the Resolve project.')); });
    store.put({...row, stamp:Date.now()});
    const rows = await request(store.getAll());
    rows.sort((a, b) => b.stamp - a.stamp).slice(keep).forEach(old => store.delete(old.key));
    await done;
  }
  async function get(storeName, key) {
    return request((await open()).transaction(storeName).objectStore(storeName).get(key));
  }
  const remember = (key, name, blob) => put('originals', {key, name, blob}, KEEP);
  const recall = async key => (await get('originals', key))?.blob;
  const workingKey = (openId, timelineId) => openId + '|' + timelineId;
  const keepWorking = project => put('working', {key:workingKey(project.resolveSource.openId, project.resolveSource.timelineId),
    project:structuredClone(project)}, KEEP_WORKING);
  const working = async (openId, timelineId) => (await get('working', workingKey(openId, timelineId)))?.project;
  async function digest(buffer) {
    return [...new Uint8Array(await crypto.subtle.digest('SHA-256', buffer))].map(b => b.toString(16).padStart(2, '0')).join('');
  }
  async function post(url, body, json) {
    const response = await fetch(url, {method:'POST', body:json ? JSON.stringify(body) : body,
      headers:{'Content-Type':json ? 'application/json' : 'application/octet-stream'}, signal:AbortSignal.timeout(300000)});
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw Error(data.error || 'Splice could not open the Resolve project.');
    return data;
  }
  function toast(message, seconds = 6) {
    document.querySelector('.toast')?.remove();
    const node = document.createElement('div');
    node.className = 'toast'; node.setAttribute('role', 'alert'); node.textContent = message;
    document.body.append(node); setTimeout(() => node.remove(), seconds * 1000);
  }
  async function load(project, extra = '') {
    const safe = (project.name || 'Resolve timeline').replace(/[\\/:*?"<>|]/g, '-');
    await SpliceSession.openFile(new File([JSON.stringify(project)], safe + '.splice.json', {type:'application/json'}));
    const issues = project.compatibilityWarnings?.length || 0;
    const parts = [extra, issues ? 'Opened from Resolve. ' + issues + ' item' + (issues === 1 ? '' : 's') + ' listed in File → Project compatibility.' : ''];
    const notice = parts.filter(Boolean).join(' ');
    if (notice) toast(notice, extra ? 12 : 6);
  }
  // A fresh open of a Resolve project: every timeline's working copy belongs to this open.
  function fresh(project) {
    project.resolveSource.openId = crypto.randomUUID();
    return project;
  }
  const UNKEPT = 'Opened, but this browser could not keep a copy of the .drp, so saving back to Resolve will be ' +
    'unavailable and switching timelines needs the file opened again.';
  async function openDrp(file) {
    if (!/\.drp$/i.test(file.name)) throw Error('Choose a DaVinci Resolve project (.drp) file.');
    const buffer = await file.arrayBuffer();
    const key = await digest(buffer);
    const {project} = await post('/api/resolve/open?timeline=0', buffer);
    if (project.resolveSource?.sha256 !== key) throw Error('The project changed while it was read. Open it again.');
    project.resolveSource.name = file.name;
    let extra = '';
    try { await remember(key, file.name, new Blob([buffer])); project.resolveSource.originalKept = true; }
    catch (error) {
      console.warn('Resolve original not kept:', error);
      project.resolveSource.originalKept = false;
      extra = UNKEPT;
    }
    await load(fresh(project), extra);
  }
  // The one place that asks for a folder path: swap in the server-side picker when it exists.
  async function pickFolder(initial = '') {
    if (window.SplicePicker?.pickFolder) return window.SplicePicker.pickFolder({title:'Resolve library folder', initial});
    return initial;
  }
  const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : 'Unknown';
  function openLibrary() {
    const node = document.createElement('dialog');
    node.className = 'studio-dialog resolve-library';
    node.innerHTML = '<h2>Open from Resolve library</h2><form class="resolve-path"><label>Library folder<input name="path" required ' +
      'placeholder="Folder that holds “Resolve Projects”" autocomplete="off" spellcheck="false"></label>' +
      '<button type="button" data-browse>Browse…</button><button class="primary">List projects</button></form>' +
      '<p class="resolve-note">Splice reads the library without changing it. Network (PostgreSQL) libraries are not supported.</p>' +
      '<p role="status" class="resolve-status"></p><table class="resolve-projects" hidden><thead><tr><th>Project</th><th>Last saved</th>' +
      '<th>Timelines</th><th></th></tr></thead><tbody></tbody></table><footer><button type="button" data-close>Close</button></footer>';
    document.body.append(node);
    node.addEventListener('close', () => node.remove(), {once:true});
    const form = node.querySelector('form'), status = node.querySelector('[role=status]'), table = node.querySelector('table');
    const field = form.elements.path;
    field.value = localStorage.getItem('splice.resolveLibrary') || '';
    node.querySelector('[data-close]').onclick = () => node.close();
    node.querySelector('[data-browse]').onclick = async () => { field.value = await pickFolder(field.value) || field.value; };
    let busy = false;
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      busy = true; status.textContent = 'Reading the library…'; table.hidden = true;
      try {
        const {projects, truncated} = await post('/api/resolve/library/list', {path:field.value}, true);
        localStorage.setItem('splice.resolveLibrary', field.value);
        const body = table.querySelector('tbody');
        body.replaceChildren(...projects.map(entry => {
          const row = document.createElement('tr');
          for (const text of [entry.name, when(entry.saved), entry.timelines ?? '—']) {
            const cell = document.createElement('td'); cell.textContent = String(text); row.append(cell);
          }
          const cell = document.createElement('td'), button = document.createElement('button');
          button.type = 'button'; button.textContent = entry.error ? 'Unavailable' : 'Open';
          button.disabled = !!entry.error; button.title = entry.error || 'Open ' + entry.name;
          button.onclick = async () => {
            if (busy) return;
            busy = true; status.textContent = 'Opening ' + entry.name + '…';
            try {
              const {project} = await post('/api/resolve/library/open', {path:entry.path, timeline:0}, true);
              node.close();
              await load(fresh(project));
            } catch (error) { status.textContent = error.message; }
            finally { busy = false; }
          };
          cell.append(button); row.append(cell); return row;
        }));
        table.hidden = false;
        status.textContent = projects.length + ' project' + (projects.length === 1 ? '' : 's') + (truncated ? ' (the list is cut short)' : '') + '.';
      } catch (error) { status.textContent = error.message; }
      finally { busy = false; }
    };
    node.showModal();
    if (field.value) form.requestSubmit(); else field.focus();
  }
  // Switch to the timeline at ``index`` of the selector. The timeline being left is kept with its
  // edits; the one chosen comes back from its working copy, or is read from Resolve by its id
  // (never by position, which changes when Resolve adds or reorders timelines).
  async function timeline(index) {
    const current = Editor.get().project;
    try { await switchTimeline(current, index); }
    catch (error) {
      // Leave the selector on the timeline that is still open.
      if (Editor.get().project === current && $('sequence-select')) $('sequence-select').value = String(current.resolveTimeline);
      throw error;
    }
  }
  async function switchTimeline(current, index) {
    const source = current.resolveSource;
    if (!source) throw Error('This project did not come from Resolve.');
    const ids = current.resolveTimelineIds || [], labels = current.resolveTimelines || [];
    const id = ids[index], label = labels[index] || 'That timeline';
    if (id === undefined) throw Error('That timeline is not in this project.');
    if (id === source.timelineId) return;
    source.openId ||= crypto.randomUUID();
    let kept = true;
    try { await keepWorking(current); }
    catch (error) {
      console.warn('Resolve working copy not kept:', error);
      kept = false;
      if (!confirm('This browser cannot keep the edits of “' + current.name + '” while you switch timelines. ' +
                   'Switch anyway and lose them?')) throw Error('Timeline not switched.');
    }
    let project = kept ? await working(source.openId, id).catch(() => undefined) : undefined;
    let extra = '';
    if (!project) {
      try {
        if (source.format === 'drp') {
          const blob = await recall(source.sha256).catch(() => undefined);
          if (!blob) throw Error('Open the .drp file again to switch timelines.');
          project = (await post('/api/resolve/open?timelineId=' + encodeURIComponent(id), await blob.arrayBuffer())).project;
          project.resolveSource.name = source.name;
          project.resolveSource.originalKept = true;
        } else {
          project = (await post('/api/resolve/library/open', {path:source.path, timelineId:id}, true)).project;
          if (project.resolveSource.sha256 !== source.sha256)
            extra = 'Resolve saved this project again after it was opened; “' + project.name + '” is read from the newer save.';
        }
      } catch (error) {
        throw /no longer in this Resolve project/.test(error.message)
          ? Error('“' + label + '” is no longer in this Resolve project; it may have been deleted since the project was opened.')
          : error;
      }
      project.resolveSource.openId = source.openId;
    }
    await load(project, extra);
  }
  const input = $('resolve-input');
  const run = action => action().catch(error => toast(error.message || String(error)));
  $('open-resolve').onclick = () => { input.value = ''; input.click(); };
  input.onchange = () => { const file = input.files[0]; if (file) run(() => openDrp(file)); };
  $('open-resolve-library').onclick = () => openLibrary();
  return {openDrp, openLibrary, timeline, pickFolder};
})();
