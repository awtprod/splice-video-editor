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
    const req = indexedDB.open('splice-sessions', 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      db.createObjectStore('sessions', {keyPath:'id'});
      db.createObjectStore('versions', {keyPath:'id'}).createIndex('session','session');
      db.createObjectStore('originals');
      db.createObjectStore('handles');
    };
    req.onsuccess = () => ok(req.result);
    req.onerror = () => fail(req.error);
  });
  const get = async (store, id) => request((await dbReady).transaction(store).objectStore(store).get(id));
  const tab = uuid();
  let id = uuid(), revision, touched = false, persisting = false, suppress = false;
  let fileHandle, savedFormat, original, originalKey, lastRecord, pending;
  let chain = Promise.resolve();
  function status(message) {
    const node = document.getElementById('session-status');
    if (node) node.textContent = message;
  }
  const signal = message => { status(message); console.warn(message); };
  function persist(checkpoint = false) {
    if (suppress) return chain;
    status('Saving draft…');
    const source = editor.get().project;
    if (source.premiereOriginal !== original) {
      original = source.premiereOriginal;
      originalKey = original ? uuid() : null;
    }
    const {premiereOriginal, ...remainder} = source;
    pending = {id, data:structuredClone(remainder), original:premiereOriginal, originalKey,
               touched, checkpoint:checkpoint || !!pending?.checkpoint, handle:fileHandle, format:savedFormat};
    if (persisting) return chain;
    persisting = true;
    chain = (async () => {
      const db = await dbReady;
      while (pending) {
        const payload = pending;
        pending = undefined;
        const tx = db.transaction(['sessions','versions','originals','handles'],'readwrite');
        const done = complete(tx);
        const sessions = tx.objectStore('sessions');
        const current = await request(sessions.get(payload.id));
        if (current && current.writer !== tab && current.stamp !== lastRecord?.stamp) {
          // Another window edited this recovery slot. Preserve both edits.
          id = payload.id = uuid();
          fileHandle = payload.handle = null;
          if (pending) { pending.id = id; pending.handle = null; }
        }
        const now = Date.now(), versions = tx.objectStore('versions');
        if (current && current.id === payload.id &&
            (payload.checkpoint || now - (current.versionAt || 0) >= 30000)) {
          versions.put({id:uuid(),session:payload.id,name:current.name,stamp:current.stamp,
                        data:current.data,originalKey:current.originalKey});
        }
        const row = {id:payload.id,name:payload.data.name,stamp:now,writer:tab,data:payload.data,
                     originalKey:payload.originalKey,touched:payload.touched,format:payload.format,
                     versionAt:current && !payload.checkpoint && now-current.versionAt<30000 ? current.versionAt : now};
        sessions.put(row);
        if (payload.originalKey && payload.originalKey !== current?.originalKey)
          tx.objectStore('originals').put(payload.original,payload.originalKey);
        if (payload.handle) tx.objectStore('handles').put(payload.handle,payload.id);
        else tx.objectStore('handles').delete(payload.id);
        const older = await request(versions.index('session').getAll(payload.id));
        older.sort((a,b)=>b.stamp-a.stamp).slice(12).forEach(v=>versions.delete(v.id));
        await done;
        lastRecord = row;
        localStorage.setItem('splice.lastSession',row.id);
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
  async function restore(row, historical = false) {
    const project = {...row.data};
    if (row.originalKey) project.premiereOriginal = await get('originals',row.originalKey);
    await activate(()=>editor.openProject(new File([JSON.stringify(project)],'session.splice.json')));
    id = row.session || row.id;
    touched = historical || row.touched;
    fileHandle = await get('handles', id);
    savedFormat = row.format || (await get('sessions',id))?.format;
    original = project.premiereOriginal;
    originalKey = row.originalKey;
    lastRecord = await get('sessions',id);
    if (historical) await persist(true);
    status(historical ? 'Version restored' : 'Session restored');
  }
  const ready = (async () => {
    try {
      const last = localStorage.getItem('splice.lastSession');
      if (last) {
        const row = await get('sessions',last);
        if (row) await restore(row);
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
  async function leave() {
    await ready;
    if (persisting) await chain;
    if (touched) await persist(true);
  }
  async function start(action, handle, format) {
    await leave();
    await activate(action);
    id = uuid(); lastRecord = undefined; original = undefined; originalKey = null;
    fileHandle = handle; savedFormat = format; touched = false;
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
    await start(()=>editor.openProject(file),handle,/\.prproj$/i.test(file.name)?'prproj':'splice');
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
    const suffix = format==='prproj'?'.prproj':'.splice.json';
    const filename = (editor.get().project.name.replace(/[\\/:*?"<>|]/g,'-')||'Untitled')+suffix;
    try {
      if (!handle && window.showSaveFilePicker)
        handle=await showSaveFilePicker({suggestedName:filename,types:pickerTypes(format),id:'splice-project'});
      if (handle && await handle.requestPermission({mode:'readwrite'}) !== 'granted')
        throw Error('Write permission was declined. Use Save as to choose another file.');
    } catch(e) { if(e.name==='AbortError') return false; throw e; }
    await ready;
    const savedRevision = editor.get().revision;
    const project = structuredClone(editor.get().project);
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
      const stream = await handle.createWritable();
      try { await stream.write(blob); await stream.close(); }
      catch(e) { await stream.abort().catch(()=>{}); throw e; }
    } else {
      const anchor=document.createElement('a'),url=URL.createObjectURL(blob);
      anchor.download=filename; anchor.href=url; anchor.click();
      setTimeout(()=>URL.revokeObjectURL(url),60000);
    }
    fileHandle=handle; savedFormat=format;
    touched = savedRevision!==editor.get().revision;
    await persist(true);
  }
  async function history() {
    await leave();
    const db=await dbReady;
    const sessions=await request(db.transaction('sessions').objectStore('sessions').getAll());
    const versions=await request(db.transaction('versions').objectStore('versions').index('session').getAll(id));
    const rows=[
      ...sessions.sort((a,b)=>b.stamp-a.stamp).map(v=>({label:v.name+' · '+new Date(v.stamp).toLocaleString(),value:v})),
      ...versions.sort((a,b)=>b.stamp-a.stamp).map(v=>({label:'Restore version · '+new Date(v.stamp).toLocaleString(),value:v}))
    ];
    const choice=await choose('Recent projects and versions',rows);
    if(choice) await restore(choice,!!choice.session);
  }
  async function sequence(requestedIndex) {
    await leave();
    const p=editor.get().project;
    const index=requestedIndex??await choose('Edit sequence',(p.premiereSequences || []).map((label,value)=>({label,value})));
    if(index===undefined || index===p.premiereSequence) return;
    const response=await fetch('/api/premiere/sequence',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:await SpliceMedia.forPremiere(p),index}),signal:AbortSignal.timeout(120000)});
    if(!response.ok) throw Error((await response.json()).error);
    const {project}=await response.json();
    await activate(()=>editor.openProject(new File([JSON.stringify(project)],'sequence.splice.json')));
    touched=true; await persist(true);
  }
  window.addEventListener('beforeunload',e=>{if(persisting){e.preventDefault();e.returnValue='';}});
  return {ready,open,openFile,save,saveAs:format=>save(format,true),history,sequence,
          newProject:settings=>start(()=>editor.newProject(settings),null,null)};
})();
