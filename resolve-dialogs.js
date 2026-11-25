// Open DaVinci Resolve projects: a .drp export, or a project in a Resolve disk library.
// The .drp's bytes stay outside the editing model (which is cloned for every undo step) in
// their own IndexedDB store, keyed by the file's SHA-256 that the project carries.
window.SpliceResolve = (() => {
  const $ = id => document.getElementById(id);
  const KEEP = 8;
  let database;
  const request = req => new Promise((ok, fail) => { req.onsuccess = () => ok(req.result); req.onerror = () => fail(req.error); });
  function open() {
    database ||= new Promise((ok, fail) => {
      const req = indexedDB.open('splice-resolve', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('originals', {keyPath:'key'});
      req.onsuccess = () => ok(req.result);
      req.onerror = () => { database = null; fail(req.error); };
    });
    return database;
  }
  async function remember(key, name, blob) {
    const db = await open();
    const tx = db.transaction('originals', 'readwrite'), store = tx.objectStore('originals');
    const done = new Promise((ok, fail) => { tx.oncomplete = ok; tx.onabort = () => fail(tx.error || Error('Could not keep the Resolve project.')); });
    store.put({key, name, blob, stamp:Date.now()});
    const rows = await request(store.getAll());
    rows.sort((a, b) => b.stamp - a.stamp).slice(KEEP).forEach(row => store.delete(row.key));
    await done;
  }
  async function recall(key) {
    return (await request((await open()).transaction('originals').objectStore('originals').get(key)))?.blob;
  }
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
  function toast(message) {
    document.querySelector('.toast')?.remove();
    const node = document.createElement('div');
    node.className = 'toast'; node.setAttribute('role', 'alert'); node.textContent = message;
    document.body.append(node); setTimeout(() => node.remove(), 6000);
  }
  async function load(project) {
    const safe = (project.name || 'Resolve timeline').replace(/[\\/:*?"<>|]/g, '-');
    await SpliceSession.openFile(new File([JSON.stringify(project)], safe + '.splice.json', {type:'application/json'}));
    const issues = project.compatibilityWarnings?.length || 0;
    if (issues) toast('Opened from Resolve. ' + issues + ' item' + (issues === 1 ? '' : 's') + ' listed in File → Project compatibility.');
  }
  async function openDrp(file) {
    if (!/\.drp$/i.test(file.name)) throw Error('Choose a DaVinci Resolve project (.drp) file.');
    const buffer = await file.arrayBuffer();
    const key = await digest(buffer);
    const {project} = await post('/api/resolve/open?timeline=0', buffer);
    if (project.resolveSource?.sha256 !== key) throw Error('The project changed while it was read. Open it again.');
    project.resolveSource.name = file.name;
    await remember(key, file.name, new Blob([buffer]));
    await load(project);
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
              await load(project);
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
  async function timeline(index) {
    const source = Editor.get().project.resolveSource;
    if (!source) throw Error('This project did not come from Resolve.');
    let project;
    if (source.format === 'drp') {
      const blob = await recall(source.sha256);
      if (!blob) throw Error('Open the .drp file again to switch timelines.');
      project = (await post('/api/resolve/open?timeline=' + index, await blob.arrayBuffer())).project;
      project.resolveSource.name = source.name;
    } else {
      project = (await post('/api/resolve/library/open', {path:source.path, timeline:index}, true)).project;
    }
    await load(project);
  }
  const input = $('resolve-input');
  const run = action => action().catch(error => toast(error.message || String(error)));
  $('open-resolve').onclick = () => { input.value = ''; input.click(); };
  input.onchange = () => { const file = input.files[0]; if (file) run(() => openDrp(file)); };
  $('open-resolve-library').onclick = () => openLibrary();
  return {openDrp, openLibrary, timeline, pickFolder};
})();
