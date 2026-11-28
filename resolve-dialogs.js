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
    return node;
  }
  async function load(project, extra = '') {
    const safe = (project.name || 'Resolve timeline').replace(/[\\/:*?"<>|]/g, '-');
    await SpliceSession.openFile(new File([JSON.stringify(project)], safe + '.splice.json', {type:'application/json'}));
    const issues = project.compatibilityWarnings?.length || 0;
    // What came in disabled, hidden, muted or locked, and where linked media is: from the importer.
    const summary = (project.resolveSource?.summary || []).filter(line => typeof line === 'string').slice(0, 4);
    const parts = [extra, ...summary, issues ? 'Opened from Resolve. ' + issues + ' item' + (issues === 1 ? '' : 's') + ' listed in File → Resolve compatibility details.' : ''];
    const notice = parts.filter(Boolean).join(' ');
    if (notice) {
      const node = toast(notice, extra || summary.length || issues ? 12 : 6);
      if (issues) {
        const more = document.createElement('button');
        more.type = 'button'; more.textContent = 'Details…'; more.dataset.compatibility = '';
        more.onclick = () => { node.remove(); showCompatibility(project); };
        node.append(' ', more);
      }
    }
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
  // The one place that asks for a folder: Splice's in-app folder browser (media-link.js). The typed
  // path in the dialog stays as the fallback; a cancelled pick keeps it.
  async function pickFolder(initial = '') {
    if (!window.SpliceLink?.pickFolder) return null;
    return window.SpliceLink.pickFolder({title:'Choose the Resolve library folder', initial});
  }
  // A grouped line's places (clips): the first SHOWN, and a button that shows the rest.
  const SHOWN = 20;
  function placesList(places, summaryText) {
    const details = document.createElement('details'), summary = document.createElement('summary');
    summary.textContent = summaryText;
    const list = document.createElement('ol');
    const add = items => { for (const place of items) { const li = document.createElement('li'); li.textContent = place; list.append(li); } };
    add(places.slice(0, SHOWN));
    details.append(summary, list);
    if (places.length > SHOWN) {
      const more = document.createElement('button');
      more.type = 'button'; more.dataset.showAll = ''; more.textContent = 'Show all ' + places.length;
      more.onclick = () => { add(places.slice(SHOWN)); more.remove(); };
      details.append(more);
    }
    return details;
  }
  // File → Resolve compatibility details: every grouped line of the open timeline's import, each
  // with the clips it stands for on demand.
  function showCompatibility(project = Editor.get().project) {
    const warnings = (project?.compatibilityWarnings || []).filter(line => typeof line === 'string');
    const details = project?.compatibilityDetails && typeof project.compatibilityDetails === 'object' ? project.compatibilityDetails : {};
    if (!project?.resolveSource) { toast('This timeline was not opened from Resolve.'); return null; }
    const node = document.createElement('dialog');
    node.className = 'studio-dialog resolve-library resolve-compatibility';
    node.innerHTML = '<h2>Resolve compatibility details</h2><p class="resolve-note">What Splice did not import from the ' +
      'Resolve project, one line per kind. Open a line to see the clips it stands for.</p><div class="resolve-report"><ul></ul></div>' +
      '<footer><button type="button" data-close>Close</button></footer>';
    const list = node.querySelector('ul');
    if (!warnings.length) list.innerHTML = '<li>Nothing: everything in this timeline was imported.</li>';
    for (const line of warnings) {
      const item = document.createElement('li'), text = document.createElement('span');
      text.textContent = line; item.append(text);
      const places = Array.isArray(details[line]) ? details[line].filter(place => typeof place === 'string') : [];
      if (places.length) item.append(placesList(places, places.length + ' clip' + (places.length === 1 ? '' : 's')));
      list.append(item);
    }
    document.body.append(node);
    node.addEventListener('close', () => node.remove(), {once:true});
    node.querySelector('[data-close]').onclick = () => node.close();
    node.showModal();
    return node;
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
    node.querySelector('[data-browse]').onclick = async () => {
      const picked = await pickFolder(field.value);
      if (picked) { field.value = picked; form.requestSubmit(); }
    };
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
  // While a switch is under way the selector shows the timeline that is open (it never shows one
  // that is not) and is disabled, with a notice; editing the open timeline goes on and is kept.
  let switching = null;
  function showOpen() {
    const selector = $('sequence-select'), open = Editor.get().project;
    if (selector && Array.isArray(open.resolveTimelines)) selector.value = String(open.resolveTimeline);
  }
  async function timeline(index) {
    const current = Editor.get().project, selector = $('sequence-select');
    showOpen();
    if (switching) { toast('“' + switching + '” is still opening; choose again when it is open.'); return; }
    const label = current.resolveTimelines?.[index] || 'That timeline';
    switching = label;
    if (selector) { selector.disabled = true; selector.setAttribute('aria-busy', 'true'); }
    const note = toast('Opening “' + label + '”… You can keep editing “' + current.name + '”; your edits are kept.', 300);
    try { await switchTimeline(current, index); }
    finally {
      switching = null;
      note.remove();
      if (selector) { selector.disabled = false; selector.removeAttribute('aria-busy'); }
      showOpen();
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
    // The open timeline may have been edited while the other one was read: keep it as it is now.
    const open = Editor.get().project;
    if (open.resolveSource?.openId !== source.openId || open.resolveSource?.timelineId !== source.timelineId)
      throw Error('Another project was opened while “' + label + '” was loading, so the timeline was not switched.');
    if (kept) {
      try { await keepWorking(open); }
      catch (error) {
        console.warn('Resolve working copy not kept:', error);
        if (!confirm('This browser cannot keep the latest edits of “' + open.name + '”. Switch anyway and lose them?'))
          throw Error('Timeline not switched.');
      }
    }
    await load(project, extra);
  }
  // ---------------------------------------------------------------- save to a Resolve library
  // File → Save to Resolve library…: the open timeline (and any other timeline of the same Resolve
  // project edited in this session) written as a NEW project in a disk library. The server checks
  // and reports first; nothing is written until the report is confirmed. The seed is the Resolve
  // project the timeline came from (its original, kept since it was opened), or a template
  // project the user picks in the library.
  const SAVE_KEYS = {library:'splice.resolveLibrary', map:'splice.resolvePathMap'};
  function parsePathMap(text) {
    const rules = [];
    for (const [index, raw] of String(text || '').split(/\r?\n/).entries()) {
      const line = raw.trim();
      if (!line || line.startsWith('#')) continue;
      const parts = line.split('=>');
      if (parts.length !== 2 || !parts[0].trim() || !parts[1].trim())
        throw Error('Path mapping line ' + (index + 1) + ': write it as "from folder => to folder".');
      rules.push({from:parts[0].trim(), to:parts[1].trim()});
    }
    return rules;
  }
  async function editedTimelines(current) {
    // The open timeline first, then other timelines of the same open that have a working copy.
    const source = current.resolveSource, out = [{project:current, label:current.name, open:true}];
    if (!source?.openId) return out;
    const ids = current.resolveTimelineIds || [], labels = current.resolveTimelines || [];
    for (const [index, id] of ids.entries()) {
      if (id === source.timelineId) continue;
      const project = await working(source.openId, id).catch(() => undefined);
      if (project) out.push({project, label:labels[index] || project.name, open:false});
    }
    return out;
  }
  async function ensureSeed(source) {
    if (source.format !== 'drp') return;
    const blob = await recall(source.sha256).catch(() => undefined);
    if (!blob) throw Error('Open the .drp file again: this browser no longer holds it, and it is the seed of the save.');
    const {sha256} = await post('/api/resolve/seed', await blob.arrayBuffer());
    if (sha256 !== source.sha256) throw Error('The kept .drp does not match the project that was opened.');
  }
  function reportTable(result) {
    const wrap = document.createElement('div');
    wrap.className = 'resolve-report';
    const groups = [['written', 'Written'], ['opt-in', 'Needs your permission'], ['not written', 'Not written']];
    for (const [status, title] of groups) {
      const lines = (result.lines || []).filter(line => line.status === status);
      if (!lines.length) continue;
      const heading = document.createElement('h3'); heading.textContent = title; heading.dataset.status = status;
      const list = document.createElement('ul');
      for (const line of lines) {
        const item = document.createElement('li'); item.dataset.status = status;
        const what = document.createElement('span'); what.textContent = line.what + (line.count > 1 ? ' — ' + line.count : '');
        item.append(what);
        if (line.all?.length)
          item.append(placesList(line.all, line.examples.join(' · ') + (line.all.length > line.examples.length ? ' …' : '')));
        list.append(item);
      }
      wrap.append(heading, list);
    }
    return wrap;
  }
  async function saveToLibrary() {
    const state = Editor.get(), current = window.SpliceNested?.rootProject?.() || state.project, source = current.resolveSource;
    const timelines = await editedTimelines(current);
    const node = document.createElement('dialog');
    node.className = 'studio-dialog resolve-library resolve-save';
    node.innerHTML = '<h2>Save to Resolve library</h2><form>' +
      '<div class="resolve-path"><label>Library folder<input name="library" required autocomplete="off" spellcheck="false" ' +
      'placeholder="Folder that holds “Resolve Projects”"></label><button type="button" data-browse>Browse…</button>' +
      '<button type="button" data-list>Read library</button></div>' +
      '<label data-user hidden>Resolve user<select name="user"></select></label>' +
      '<label>New project name<input name="name" required maxlength="200" autocomplete="off"></label>' +
      '<fieldset data-seed><legend>Made from</legend></fieldset>' +
      '<fieldset data-timelines><legend>Timelines</legend></fieldset>' +
      '<details><summary>Path mapping (optional)</summary><p class="resolve-note">Where the Resolve computer sees your media, ' +
      'one “from folder => to folder” per line. Where several rules match a path, the longest “from” folder wins, '
      + 'whatever the order (<code>/mnt/media</code> before <code>/mnt</code>).</p><textarea name="pathMap" rows="3" spellcheck="false" ' +
      'placeholder="/mnt/shoot => E:\\Shoot"></textarea></details>' +
      '<label class="resolve-check"><input type="checkbox" name="unverified"> Allow media kinds Resolve has not been ' +
      'verified to take (they are listed in the report)</label>' +
      '<label class="resolve-check"><input type="checkbox" name="unproven"> Allow changes whose bytes are not proven ' +
      '(track lock/mute, position X, a seed made from a .drp)</label>' +
      '<p role="status" class="resolve-status"></p><div data-report></div>' +
      '<footer><button type="button" data-close>Close</button><button type="button" data-check>Check</button>' +
      '<button type="button" class="primary" data-save disabled>Save new project</button></footer></form>';
    document.body.append(node);
    node.addEventListener('close', () => node.remove(), {once:true});
    const form = node.querySelector('form'), status = node.querySelector('[role=status]');
    const fields = form.elements, reportNode = node.querySelector('[data-report]');
    const saveButton = node.querySelector('[data-save]'), seedBox = node.querySelector('[data-seed]');
    fields.library.value = localStorage.getItem(SAVE_KEYS.library) || '';
    fields.pathMap.value = localStorage.getItem(SAVE_KEYS.map) || '';
    fields.name.value = ((source?.project || current.name || 'Project') + ' (Splice)').slice(0, 200);
    if (source?.sha256) {
      const note = document.createElement('p');
      note.className = 'resolve-note';
      note.textContent = 'The Resolve project this timeline came from (' + (source.project || source.name || 'original') +
        '): everything Splice did not change is carried over from it unchanged.';
      seedBox.append(note);
    } else {
      seedBox.innerHTML = '<p class="resolve-note">This timeline did not come from Resolve. Pick a project in the library ' +
        'as the template: Resolve settings, color management and bins come from it, and its one timeline holds your cut.</p>' +
        '<label>Template project<select name="template"><option value="">Read the library first</option></select></label>' +
        '<label class="resolve-check"><input type="checkbox" name="keepMedia"> Keep the template’s media in the new project</label>';
    }
    const timelineBox = node.querySelector('[data-timelines]');
    for (const [index, entry] of timelines.entries()) {
      const label = document.createElement('label'), box = document.createElement('input');
      label.className = 'resolve-check'; box.type = 'checkbox'; box.name = 'timeline'; box.value = String(index);
      box.checked = entry.open;
      label.append(box, ' ' + entry.label + (entry.open ? ' (open)' : ' (edited earlier)'));
      timelineBox.append(label);
    }
    if (!source?.sha256) timelineBox.hidden = true;
    let checked = null, busy = false;
    const stale = () => { checked = null; saveButton.disabled = true; };
    form.addEventListener('input', stale);
    form.addEventListener('change', stale);
    node.querySelector('[data-close]').onclick = () => node.close();
    async function readLibrary() {
      status.textContent = 'Reading the library…';
      const {projects, users} = await post('/api/resolve/library/list', {path:fields.library.value}, true);
      localStorage.setItem(SAVE_KEYS.library, fields.library.value);
      const userBox = node.querySelector('[data-user]');
      fields.user.replaceChildren(...(users || []).map(user => new Option(user, user)));
      userBox.hidden = (users || []).length < 2;
      if (fields.template) {
        fields.template.replaceChildren(new Option('Choose a project…', ''), ...projects.filter(p => !p.error)
          .map(p => new Option(p.name + (p.timelines != null ? ' · ' + p.timelines + ' timeline(s)' : ''), p.path)));
      }
      status.textContent = projects.length + ' project(s) in the library.';
    }
    node.querySelector('[data-browse]').onclick = async () => {
      const picked = await pickFolder(fields.library.value);
      if (picked) { fields.library.value = picked; stale(); await readLibrary().catch(error => { status.textContent = error.message; }); }
    };
    node.querySelector('[data-list]').onclick = () => readLibrary().catch(error => { status.textContent = error.message; });
    function request(confirm) {
      const chosen = [...form.querySelectorAll('input[name=timeline]:checked')].map(box => timelines[Number(box.value)].project);
      const payload = {library:fields.library.value.trim(), name:fields.name.value.trim(), user:fields.user.value || null,
        timelines:source?.sha256 ? chosen : [current], pathMap:parsePathMap(fields.pathMap.value),
        allow:{unverifiedMedia:fields.unverified.checked, unproven:fields.unproven.checked}, confirm};
      if (source?.sha256) payload.seed = {kind:source.format === 'drp' ? 'drp' : 'library', sha256:source.sha256,
        path:source.path, project:source.project};
      else {
        if (!fields.template.value) throw Error('Choose the template project.');
        payload.seed = {kind:'template', path:fields.template.value};
        payload.keepTemplateMedia = fields.keepMedia.checked;
      }
      if (!payload.timelines.length) throw Error('Choose at least one timeline.');
      return payload;
    }
    async function send(confirm) {
      if (busy) return;
      busy = true; saveButton.disabled = true;
      status.textContent = confirm ? 'Writing the new project…' : 'Checking what Resolve can take…';
      try {
        const payload = request(confirm);
        localStorage.setItem(SAVE_KEYS.map, fields.pathMap.value);
        if (source?.sha256) await ensureSeed(source);
        const result = await post('/api/resolve/library/save', payload, true);
        reportNode.replaceChildren(reportTable(result));
        if (confirm) {
          status.textContent = 'Saved “' + result.project.name + '” in the library. Open it in Resolve’s Project Manager.';
          const reopen = document.createElement('button');
          reopen.type = 'button'; reopen.textContent = 'Open the saved project in Splice'; reopen.dataset.reopen = '';
          reopen.onclick = async () => {
            const {project} = await post('/api/resolve/library/open', {path:result.project.path, timeline:0}, true);
            node.close(); await load(fresh(project));
          };
          reportNode.prepend(reopen);
          checked = null;
        } else {
          checked = JSON.stringify(payload);
          status.textContent = 'Checked. Review the report, then save. Nothing has been written yet.';
          saveButton.disabled = false;
        }
      } catch (error) {
        status.textContent = error.message;
      } finally { busy = false; if (checked) saveButton.disabled = false; }
    }
    node.querySelector('[data-check]').onclick = () => send(false);
    saveButton.onclick = () => {
      if (!checked) return;
      try { if (JSON.stringify(request(false)) !== checked) { stale(); status.textContent = 'The choices changed: check again.'; return; } }
      catch (error) { status.textContent = error.message; return; }
      send(true);
    };
    node.showModal();
    if (fields.library.value) readLibrary().catch(error => { status.textContent = error.message; });
  }
  const input = $('resolve-input');
  const run = action => action().catch(error => toast(error.message || String(error)));
  $('open-resolve').onclick = () => { input.value = ''; input.click(); };
  input.onchange = () => { const file = input.files[0]; if (file) run(() => openDrp(file)); };
  $('open-resolve-library').onclick = () => openLibrary();
  $('save-resolve-library').onclick = () => run(saveToLibrary);
  $('resolve-compatibility').onclick = () => showCompatibility();
  return {openDrp, openLibrary, timeline, pickFolder, saveToLibrary, parsePathMap, showCompatibility, placesList};
})();
