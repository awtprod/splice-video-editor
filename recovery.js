// A saved interchange file and a recoverable editing session have independent lifetimes.
window.SpliceSession = (() => {
  const editor = window.Editor;
  const uuid = () => crypto.randomUUID();
  const request = req => new Promise((ok, fail) => {
    req.onsuccess = () => ok(req.result);
    req.onerror = () => fail(req.error);
  });
  const complete = tx => new Promise((ok, fail) => {
    tx.oncomplete = ok;
    tx.onabort = () => fail(tx.error || Error('Session transaction interrupted'));
  });
  const dbReady = new Promise((ok, fail) => {
    const req = indexedDB.open('splice-sessions', 2);
    req.onupgradeneeded = event => {
      const db = req.result;
      if (event.oldVersion === 0) {
        db.createObjectStore('sessions', {keyPath:'id'});
        db.createObjectStore('versions', {keyPath:'id'}).createIndex('session','session');
        db.createObjectStore('originals');
        db.createObjectStore('handles');
      }
      db.createObjectStore('metadata');
    };
    req.onsuccess = () => {
      req.result.onversionchange = () => req.result.close();
      ok(req.result);
    };
    req.onblocked = () => fail(Error('Close other Splice windows so recovery can be upgraded.'));
    req.onerror = () => fail(req.error);
  });
  const get = async (store, id) => request((await dbReady).transaction(store).objectStore(store).get(id));
  // Retention: "Recent projects & recovery" lists, and the database keeps,
  // the 30 most recently edited projects plus any project edited in the last
  // 30 days (at most 200), and always the open one. A removed project takes
  // its versions, file handle and stored Premiere originals with it; an
  // original no remaining project or version refers to is removed too.
  const KEEP_RECENT = 30, KEEP_DAYS = 30, KEEP_MAX = 200;
  const tab = uuid();
  let id = uuid(), revision, touched = false, persisting = false, suppress = false;
  let fileHandle, diskSignature, savedFormat, original, originalKey, lastRecord, pending;
  const signature=file=>[file.lastModified,file.size].join(':');
  let chain = Promise.resolve();
  // Large immutable payloads (LUT tables) are stored once in the 'originals' store under a
  // content key ("lut:v2:<hash>"; drafts from earlier builds keep their older "lut:<size>:" keys), the way the Premiere original is, and drafts refer to them as
  // {payload: key}. Undo states share LUT objects and never change them in place, so a key is
  // computed once per table. Rows list their keys in `payloadKeys` so pruning can keep them.
  const payloadKeys = new WeakMap();
  const storedPayloads = new Set();
  // The key covers everything that defines the LUT: every field, nested ones included (the 3D
  // table, its size and domain, a 1D LUT's full `curve` with its size and samples, the title),
  // each hashed with its name and type, so two LUTs differing in any of them get different keys.
  function contentKey(lut) {
    let key = payloadKeys.get(lut);
    if (key) return key;
    const view = new DataView(new ArrayBuffer(8));
    let low = 0x811c9dc5, high = 0x01000193 ^ (lut.data?.length || 0), count = 0;
    const mix = value => {
      view.setFloat64(0, value);
      low = Math.imul(low ^ view.getUint32(0), 0x01000193);
      high = Math.imul(high ^ view.getUint32(4), 0x5bd1e995) ^ (low >>> 13);
      count++;
    };
    const text = value => { mix(value.length); for (let index = 0; index < value.length; index++) mix(value.charCodeAt(index)); };
    const walk = value => {
      if (typeof value === 'number') { mix(1); mix(value); }
      else if (typeof value === 'string') { mix(2); text(value); }
      else if (typeof value === 'boolean') { mix(3); mix(value ? 1 : 0); }
      else if (Array.isArray(value)) { mix(4); mix(value.length); for (const entry of value) walk(entry); }
      else if (value && typeof value === 'object') {
        const keys = Object.keys(value).filter(name => value[name] !== undefined).sort();
        mix(5); mix(keys.length);
        for (const name of keys) { text(name); walk(value[name]); }
      } else mix(6);
    };
    walk(lut);
    key = 'lut:v2:' + lut.size + ':' + (lut.data?.length || 0) + ':' + (lut.curve ? lut.curve.size + ':' : '') +
      count.toString(36) + ':' + (low >>> 0).toString(16) + (high >>> 0).toString(16);
    payloadKeys.set(lut, key);
    return key;
  }
  // The project as it goes into a draft: no Premiere original, LUT tables replaced by keys.
  // Nothing is copied except the clips that carry a LUT (and the arrays holding them).
  function draftOf(source, payloads) {
    let clips = source.clips;
    if (clips.some(clip => clip.color?.lut)) {
      clips = clips.map(clip => {
        const lut = clip.color?.lut;
        if (!lut || typeof lut !== 'object' || !Array.isArray(lut.data)) return clip;
        const key = contentKey(lut);
        payloads.set(key, lut);
        return {...clip, color: {...clip.color, lut: {payload: key}}};
      });
    }
    let media = source.media;
    if (media?.some(item => item.nestedProject || item.inputLut)) {
      media = media.map(item => {
        let next = item.nestedProject ? {...item, nestedProject: draftOf(item.nestedProject, payloads)} : item;
        // A source's input LUT is stored once, like clip LUTs.
        if (item.inputLut && typeof item.inputLut === 'object' && Array.isArray(item.inputLut.data)) {
          const key = contentKey(item.inputLut);
          payloads.set(key, item.inputLut);
          next = {...next, inputLut: {payload: key}};
        }
        return next;
      });
    }
    const {premiereOriginal, ...rest} = source;
    return {...rest, clips, media};
  }
  // Put LUT tables back into a draft read from the database. Drafts saved before payloads
  // existed carry their tables inline and pass through unchanged.
  async function withPayloads(data) {
    const keys = new Set();
    const collect = project => {
      for (const clip of project?.clips || []) if (typeof clip.color?.lut?.payload === 'string') keys.add(clip.color.lut.payload);
      for (const item of project?.media || []) {
        if (item.nestedProject) collect(item.nestedProject);
        if (typeof item.inputLut?.payload === 'string') keys.add(item.inputLut.payload);
      }
    };
    collect(data);
    if (!keys.size) return data;
    const tables = new Map();
    for (const key of keys) {
      const lut = await get('originals', key);
      if (!lut) throw Error('A LUT used by this draft is missing from recovery storage.');
      payloadKeys.set(lut, key);
      storedPayloads.add(key);
      tables.set(key, lut);
    }
    const restore = project => ({...project,
      clips: (project.clips || []).map(clip => typeof clip.color?.lut?.payload === 'string' ?
        {...clip, color: {...clip.color, lut: tables.get(clip.color.lut.payload)}} : clip),
      media: (project.media || []).map(item => {
        let next = item.nestedProject ? {...item, nestedProject: restore(item.nestedProject)} : item;
        if (typeof item.inputLut?.payload === 'string') next = {...next, inputLut: tables.get(item.inputLut.payload)};
        return next;
      })});
    return restore(data);
  }
  function status(message) {
    const node = document.getElementById('session-status');
    if (node) node.textContent = message;
  }
  const signal = message => { status(message); console.warn(message); };
  const rootProject = () => window.SpliceNested?.rootProject?.() || editor.get().project;
  function persist(checkpoint = false) {
    if (suppress) return chain;
    status('Saving draft…');
    const source = rootProject();
    if (source.premiereOriginal !== original) {
      original = source.premiereOriginal;
      originalKey = original ? uuid() : null;
    }
    // Undo states are never changed in place, so the draft can refer to the live project; the
    // database makes its own copy when the row is written.
    const payloads = new Map();
    pending = {id, data:draftOf(source, payloads), payloads, original:source.premiereOriginal, originalKey,
               touched, checkpoint:checkpoint || !!pending?.checkpoint, handle:fileHandle, diskSignature, format:savedFormat};
    if (persisting) return chain;
    persisting = true;
    chain = (async () => {
      const db = await dbReady;
      while (pending) {
        const payload = pending;
        pending = undefined;
        const tx = db.transaction(['sessions','versions','originals','handles','metadata'],'readwrite',{durability:'strict'});
        const done = complete(tx);
        // When a request in the transaction fails first (quota exceeded), the error is thrown
        // from that request and reported once below; the transaction's own abort is the same
        // failure and must not surface again as an unhandled rejection.
        done.catch(() => {});
        const sessions = tx.objectStore('sessions');
        const current = await request(sessions.get(payload.id));
        if (current && current.writer !== tab && (current.token || current.stamp) !== (lastRecord?.token || lastRecord?.stamp)) {
          // Another window edited this recovery slot. Preserve both edits.
          id = payload.id = uuid();
          fileHandle = payload.handle = null; diskSignature = payload.diskSignature = undefined;
          if (pending) { pending.id = id; pending.handle = null; }
        }
        const now = Date.now(), versions = tx.objectStore('versions');
        if (current && current.id === payload.id &&
            (payload.checkpoint || now - (current.versionAt || 0) >= 30000)) {
          versions.put({id:uuid(),session:payload.id,name:current.name,stamp:current.stamp,
                        data:current.data,originalKey:current.originalKey,payloadKeys:current.payloadKeys});
        }
        const row = {id:payload.id,name:payload.data.name,stamp:now,writer:tab,token:uuid(),data:payload.data,
                     originalKey:payload.originalKey,payloadKeys:[...payload.payloads.keys()],
                     touched:payload.touched,format:payload.format,diskSignature:payload.diskSignature,
                     versionAt:current && !payload.checkpoint && now-current.versionAt<30000 ? current.versionAt : now};
        sessions.put(row);
        tx.objectStore('metadata').put(row.id,'lastSession');
        if (payload.originalKey && payload.originalKey !== current?.originalKey)
          tx.objectStore('originals').put(payload.original,payload.originalKey);
        // Each table is written once per page; pruning may have removed it since, so a table
        // this row needs that the store lacks is written again.
        const written = [];
        for (const [key, lut] of payload.payloads) {
          if (storedPayloads.has(key) && await request(tx.objectStore('originals').count(key))) continue;
          tx.objectStore('originals').put(lut, key);
          written.push(key);
        }
        if (payload.handle) tx.objectStore('handles').put(payload.handle,payload.id);
        else tx.objectStore('handles').delete(payload.id);
        const older = await request(versions.index('session').getAll(payload.id));
        older.sort((a,b)=>b.stamp-a.stamp).slice(12).forEach(v=>versions.delete(v.id));
        await done;
        for (const key of written) storedPayloads.add(key);
        lastRecord = row;
      }
      status(touched ? 'Draft saved locally' : 'Saved');
    })().catch(err=>{signal('Recovery unavailable: '+err.message); throw err;})
      .finally(()=>{persisting=false;});
    return chain;
  }
  async function activate(action) {
    await chain.catch(()=>{});
    suppress = true;
    try { await action(); revision = editor.get().revision; }
    finally { suppress = false; }
  }
  async function switchProject(message, action) {
    if (document.getElementById('project-loading')) throw Error('Wait for the project to finish opening.');
    const dialog = document.createElement('dialog');
    dialog.id = 'project-loading';
    dialog.className = 'studio-dialog';
    dialog.setAttribute('aria-labelledby','project-loading-title');
    dialog.innerHTML='<h2 id="project-loading-title"></h2><progress aria-label="Loading project"></progress>';
    dialog.querySelector('h2').textContent=message;
    dialog.oncancel=event=>event.preventDefault();
    document.body.append(dialog);
    const save=document.getElementById('save-project');
    save.disabled=true;
    try {
      dialog.showModal();
      editor.setRate(0);
      return await action();
    } finally {
      dialog.close();
      dialog.remove();
      save.disabled=false;
    }
  }
  async function restore(row, historical = false) {
    const project = {...await withPayloads(row.data)};
    if (row.originalKey) project.premiereOriginal = await get('originals',row.originalKey);
    await activate(()=>editor.openProject(new File([JSON.stringify(project)],'session.splice.json')));
    id = row.session || row.id;
    touched = historical || row.touched;
    fileHandle = await get('handles', id);
    diskSignature=(await get('sessions',id))?.diskSignature;
    savedFormat = row.format || (await get('sessions',id))?.format;
    original = project.premiereOriginal;
    originalKey = row.originalKey;
    lastRecord = await get('sessions',id);
    if (historical) await persist(true);
    status(historical ? 'Version restored' : 'Session restored');
  }
  const ready = (async () => {
    try {
      const last = await get('metadata','lastSession') || localStorage.getItem('splice.lastSession');
      if (last) {
        const row = await get('sessions',last);
        if (row) await switchProject('Restoring project…',()=>restore(row));
      }
    } catch(e) { signal('Could not restore the last session: '+e.message); }
    revision = editor.get().revision;
    editor.subscribe(state=>{
      if (suppress || revision === state.revision) return;
      revision = state.revision;
      touched = true;
      persist().catch(()=>{});
    });
  })().catch(e=>signal('Could not restore the last session: '+e.message));
  ready.then(()=>prune()).catch(e=>signal('Could not tidy recovery data: '+e.message));
  async function leave() {
    await ready;
    if (persisting) await chain;
    if (touched) await persist(true);
  }
  async function start(action, handle, format, fingerprint) {
    await leave();
    await activate(action);
    id = uuid(); lastRecord = undefined; original = undefined; originalKey = null;
    fileHandle = handle; diskSignature=fingerprint; savedFormat = format; touched = false;
    await persist();
  }
  function choose(title, rows) {
    const dlg = document.createElement('dialog');
    dlg.className = 'splice-session-dialog';
    const heading = document.createElement('h2');
    heading.textContent = title; dlg.append(heading);
    return new Promise(resolve=>{
      let value;
      for (const row of rows) {
        const button = document.createElement('button');
        button.textContent = row.label; button.type = 'button';
        button.onclick = ()=>{value=row.value;dlg.close();};
        dlg.append(button);
      }
      const close = document.createElement('button'); close.textContent='Cancel';
      close.onclick=()=>dlg.close(); dlg.append(close);
      dlg.onclose=()=>{dlg.remove();resolve(value);};
      document.body.append(dlg); dlg.showModal();
    });
  }
  async function openFile(file,handle=null) {
    await ready;
    await switchProject('Opening project…',()=>start(()=>editor.openProject(file),handle,
      /\.prproj$/i.test(file.name)?'prproj':'splice',handle?signature(file):undefined));
  }
  function pickerTypes(format) {
    const entries = {
      prproj:{description:'Premiere project',accept:{'application/octet-stream':['.prproj']}},
      splice:{description:'Splice working project',accept:{'application/json':['.json','.splice']}}
    };
    return format ? [entries[format]] : Object.values(entries);
  }
  async function open() {
    if (!window.showOpenFilePicker) { document.getElementById('project-input').click(); return; }
    try {
      const [handle] = await showOpenFilePicker({types:pickerTypes(),id:'splice-project'});
      await openFile(await handle.getFile(),handle);
    } catch (e) { if(e.name!=='AbortError') throw e; }
  }
  async function save(format='prproj', forceAs=false) {
    // Invoke the native picker in the click's activation window.
    let handle = !forceAs && savedFormat===format ? fileHandle : null;
    const reusing=!!handle;
    const suffix = format==='prproj'?'.prproj':'.splice.json';
    const filename = (rootProject().name.replace(/[\\/:*?"<>|]/g,'-')||'Untitled')+suffix;
    try {
      if (!handle && window.showSaveFilePicker)
        handle=await showSaveFilePicker({suggestedName:filename,types:pickerTypes(format),id:'splice-project'});
      if (handle && await handle.requestPermission({mode:'readwrite'}) !== 'granted')
        throw Error('Write permission was declined. Use Save as to choose another file.');
    } catch(e) { if(e.name==='AbortError') return false; throw e; }
    await ready;
    if(persisting) await chain;
    if(reusing && handle!==fileHandle)throw Error('Another editing window was recovered. Use Save as to choose a separate project file.');
    const savedRevision = editor.get().revision;
    const project = structuredClone(rootProject());
    let blob;
    if (format==='prproj') {
      const prepared = await SpliceMedia.forPremiere(project);
      const response = await fetch('/api/premiere/save',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({project:prepared}),signal:AbortSignal.timeout(120000)});
      if (!response.ok) throw Error((await response.json()).error || 'Premiere save failed');
      blob = await response.blob();
    } else {
      blob = new Blob([JSON.stringify(project)],{type:'application/json'});
    }
    if (handle) {
      if(reusing && diskSignature && signature(await handle.getFile())!==diskSignature)
        throw Error('The file on disk changed after it was opened. Use Save as to keep both versions.');
      const stream = await handle.createWritable();
      try { await stream.write(blob); await stream.close(); }
      catch(e) { await stream.abort().catch(()=>{}); throw e; }
      diskSignature=signature(await handle.getFile());
    } else {
      const anchor=document.createElement('a'),url=URL.createObjectURL(blob);
      anchor.download=filename; anchor.href=url; anchor.click();
      setTimeout(()=>URL.revokeObjectURL(url),60000);
    }
    fileHandle=handle; savedFormat=format;
    touched = savedRevision!==editor.get().revision;
    await persist(true);
  }
  // While "Recent projects & recovery" is open in any tab, nothing listed
  // there may disappear: the dialog holds a shared lock and pruning runs only
  // when it can take the lock exclusively at once (otherwise it skips; the
  // next start or list does it).
  const LIST_LOCK = 'splice-recovery-list';
  let listing = 0;
  function whileListed(work) {
    listing++;
    const run = async () => { try { return await work(); } finally { listing--; } };
    return navigator.locks ? navigator.locks.request(LIST_LOCK, {mode:'shared'}, run) : run();
  }
  function prune() {
    if (listing) return Promise.resolve(0);
    if (!navigator.locks) return pruneNow();
    return navigator.locks.request(LIST_LOCK, {ifAvailable:true}, lock => lock ? pruneNow() : 0);
  }
  async function pruneNow() {
    const db=await dbReady;
    const tx=db.transaction(['sessions','versions','originals','handles','metadata'],'readwrite');
    const done=complete(tx);
    const last=await request(tx.objectStore('metadata').get('lastSession'));
    const sessions=(await request(tx.objectStore("sessions").getAll())).sort((a,b)=>(b.stamp||0)-(a.stamp||0));
    const cutoff=Date.now()-KEEP_DAYS*864e5;
    const kept=new Set(sessions.filter((row,index)=>index<KEEP_RECENT || index<KEEP_MAX && row.stamp>=cutoff ||
                                                   row.id===id || row.id===last).map(row=>row.id));
    const originals=new Set(sessions.filter(row=>kept.has(row.id)).flatMap(row=>[row.originalKey,...(row.payloadKeys||[])]).filter(Boolean));
    let removed=0;
    for(const row of sessions) if(!kept.has(row.id)){tx.objectStore('sessions').delete(row.id);tx.objectStore('handles').delete(row.id);removed++;}
    await new Promise((ok,fail)=>{
      const cursor=tx.objectStore('versions').openCursor();
      cursor.onerror=()=>fail(cursor.error);
      cursor.onsuccess=()=>{
        const entry=cursor.result;
        if(!entry)return ok();
        if(!kept.has(entry.value.session))entry.delete();
        else for(const key of [entry.value.originalKey,...(entry.value.payloadKeys||[])])if(key)originals.add(key);
        entry.continue();
      };
    });
    for(const key of await request(tx.objectStore('originals').getAllKeys()))
      if(!originals.has(key)){tx.objectStore('originals').delete(key);storedPayloads.delete(key);removed++;}
    await done;
    return removed;
  }
  // Every managed or linked media source a retained project or version in
  // this browser still uses. The media cache marks these "used here"; it
  // cannot see other browsers, the desktop app or project files on disk.
  async function mediaReferences() {
    await ready;
    const db=await dbReady, found=new Set();
    const scan=value=>{for(const match of JSON.stringify(value).matchAll(/\/media\/(?:linked\/)?[a-f0-9]{32}\/[^"\\]*/g))found.add(match[0]);};
    scan(rootProject());
    for(const store of ['sessions','versions'])
      for(const row of await request(db.transaction(store).objectStore(store).getAll()))scan(row.data);
    return [...found];
  }
  async function history() {
    await leave();
    await prune();
    const choice=await whileListed(async()=>{
      const db=await dbReady;
      const sessions=await request(db.transaction('sessions').objectStore('sessions').getAll());
      const versions=await request(db.transaction('versions').objectStore('versions').index('session').getAll(id));
      const rows=[
        ...sessions.sort((a,b)=>b.stamp-a.stamp).map(v=>({label:v.name+' · '+new Date(v.stamp).toLocaleString(),value:v})),
        ...versions.sort((a,b)=>b.stamp-a.stamp).map(v=>({label:'Restore version · '+new Date(v.stamp).toLocaleString(),value:v}))
      ];
      return choose('Recent projects and versions',rows);
    });
    if(choice) {
      await switchProject('Restoring project…',async()=>{
        await restore(choice,!!choice.session);
        if (!choice.session) await persist();
      });
    }
  }
  async function sequence(requestedIndex) {
    await leave();
    const p=editor.get().project;
    const index=requestedIndex??await choose('Edit sequence',(p.premiereSequences || []).map((label,value)=>({label,value})));
    if(index===undefined || index===p.premiereSequence) return;
    await switchProject('Opening sequence…',async()=>{
      const response=await fetch('/api/premiere/sequence',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({project:await SpliceMedia.forPremiere(p),index}),signal:AbortSignal.timeout(120000)});
      if(!response.ok) throw Error((await response.json()).error);
      const {project}=await response.json();
      await activate(()=>editor.openProject(new File([JSON.stringify(project)],'sequence.splice.json')));
      touched=true; await persist(true);
    });
  }
  window.addEventListener('beforeunload',e=>{if(persisting){e.preventDefault();e.returnValue='';}});
  return {ready,open,openFile,save,saveAs:format=>save(format,true),history,sequence,prune,mediaReferences,lutKey:contentKey,
          newProject:settings=>start(()=>editor.newProject(settings),null,null)};
})();
