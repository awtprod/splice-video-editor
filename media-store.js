// File inputs grant access to bytes, but their blob URLs expire with the page.
// Keep explicitly imported files on this workstation so saved cuts can reopen.
window.SpliceMedia = (() => {
  let database;
  const urls = new Map();
  const basename = (path) =>
    String(path || "")
      .split(/[\\/]/)
      .pop()
      .toLowerCase();
  const bundled = (src) =>
    /^(?:\.\/|\/)?assets\/(?:ridge|forest|valley|lake)\.(?:mp4|jpg)$|^(?:\.\/|\/)?assets\/score\.wav$/.test(
      src,
    );
  // Managed copies and proxies, or originals linked in place (desktop and
  // python3 server.py on this machine): the server resolves those through
  // its registry of paths the user picked.
  const localMedia = (src) => /^\/media\/(?:linked\/)?[a-f0-9]{32}\/[^/]+$/.test(src);
  const key = (item) =>
    /^m\d+$/.test(item.id) && item.path ? "path:" + item.path : item.id;
  const open = () => {
    if (!database)
      database = new Promise((resolve, reject) => {
        const request = indexedDB.open("splice-media", 1);
        request.onupgradeneeded = () => {
          const store = request.result.createObjectStore("files", {
            keyPath: "id",
          });
          store.createIndex("path", "path");
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => {
          database = null;
          reject(request.error);
        };
      });
    return database;
  };
  const read = (request) =>
    new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  function matches(item, file) {
    return (
      (item.offline || !item.src) &&
      [item.path, item.name].some(
        (key) => basename(key) === basename(file.name),
      )
    );
  }
  function matchFolder(items, files) {
    const parts = path => String(path||'').toLowerCase().split(/[\\/]/).filter(Boolean);
    const ordered = [];
    const consumed = new Set();
    for(const item of items.filter(m=>m.offline)){
      const source=parts(item.path||item.name);
      const options=files.filter(f=>basename(f.name)===source.at(-1)).map(file=>{
        const candidate=parts(file.webkitRelativePath||file.name);
        let count=0;
        while(count<Math.min(source.length,candidate.length) && source.at(-1-count)===candidate.at(-1-count))count++;
        return {file,count,depth:candidate.length};
      }).sort((a,b)=>b.count-a.count);
      if(!options.length)continue;
      if(options[0].count===options[1]?.count || consumed.has(options[0].file))
        throw Error('Ambiguous copies of '+item.name+'. Relink this source individually.');
      const {file,count,depth}=options[0];
      // A chosen card folder can share its name and filenames with another camera.
      consumed.add(file);ordered.push({item,file,matched:count,needsConfirmation:count<3||count<depth});
    }
    return ordered;
  }
  async function putRecords(records) {
    const db = await open();
    const transaction = db.transaction("files", "readwrite");
    const committed = new Promise((resolve, reject) => {
      transaction.oncomplete = resolve;
      transaction.onabort = () =>
        reject(
          transaction.error || new Error("Could not store this media file."),
        );
    });
    const store=transaction.objectStore("files");
    for(const record of records)store.put(record);
    await committed;
  }
  async function upload(file) {
    const response = await fetch('/api/media/upload?name='+encodeURIComponent(file.name), {
      method:'POST',body:file,signal:AbortSignal.timeout(60 * 60 * 1000)
    });
    const result = await response.json();
    if (!response.ok || !localMedia(result.src)) throw Error(result.error || 'Import failed.');
    return result.src;
  }
  // A picked original stays where it is. Picker entries look like Files to
  // the editor (name, webkitRelativePath) and carry the absolute path.
  let noticeShown = false;
  async function link(file) {
    if (file.spliceLinked) return file.spliceLinked;
    const response = await fetch('/api/media/link', {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({paths:[file.splicePath]}),signal:AbortSignal.timeout(120000)});
    const result = await response.json();
    const entry = result.files?.[0];
    if (result.notice && !noticeShown) { noticeShown = true; window.alert(result.notice); }
    if (!response.ok || !entry || entry.error || !localMedia(entry.src)) throw Error(entry?.error || result.error || 'Could not link '+file.name+'.');
    return entry;
  }
  async function exists(src) {
    if (!localMedia(src) && !bundled(src)) return false;
    return (await fetch(src,{method:'HEAD',cache:'no-store',signal:AbortSignal.timeout(15000)})).ok;
  }
  async function probe(src) {
    const response=await fetch('/api/media/info?src='+encodeURIComponent(src),{signal:AbortSignal.timeout(30000)});
    const data=await response.json();
    if(!response.ok) throw Error(data.error||'Could not inspect the original source');
    return data;
  }
  async function makeProxy(src, seekOptimized=false, audioStream=null, gradingData=false) {
    const response=await fetch('/api/media/proxy',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({src,seekOptimized,audioStream,gradingData}),signal:AbortSignal.timeout(3650000)});
    const data=await response.json();
    if(!response.ok || !localMedia(data.src)) throw Error(data.error||'Could not create edit proxy');
    return data.src;
  }
  const previewJobs = new Map();
  const previewKey = (item,stream,grading) => JSON.stringify([item.originalSrc||item.src,!!item.seekOptimized,stream,grading]);
  function hdrPreviewStatus(item, stream=item?.audioStream??null) {
    return item?.src ? previewJobs.get(previewKey(item,stream,true)) : undefined;
  }
  function preparePreview(item, stream, grading=false, retry=false) {
    if (!item.src || item.offline) return;
    const key=previewKey(item,stream,grading);
    let job=previewJobs.get(key);
    if (job && !(retry && job.error)) return job;
    job={pending:true};
    previewJobs.set(key,job);
    if (previewJobs.size>500) {
      const expired=[...previewJobs].find(([,entry])=>!entry.pending);
      if (expired) previewJobs.delete(expired[0]);
    }
    job.done=makeProxy(item.originalSrc||item.src,item.seekOptimized===true,stream,grading).then(src=>{
      job.src=src;
    }).catch(error=>{
      job.error=error.message;
    }).finally(()=>{
      job.pending=false;
      window.Editor?.refreshMedia?.();
    });
    return job;
  }
  async function importFile(file) {
    const linked=typeof file.splicePath==='string'?await link(file):null;
    const originalSrc=linked?linked.src:await upload(file);
    const info=await probe(originalSrc);
    const src=info.proxyRecommended?await makeProxy(originalSrc):originalSrc;
    return {...info,src,originalSrc,name:file.name,path:linked?linked.sourcePath:file.name,linked:!!linked,
            sourceFingerprint:linked?.fingerprint,offline:false,seekOptimized:false,audioStream:null};
  }
  const nestedRequests=new WeakMap();
  function prepareNested(item,retry=false) {
    if(!item?.nestedProject || item.src) return;
    if(nestedRequests.has(item) && !retry)return nestedRequests.get(item);
    const task=(async()=>{
      try {
        item.proxyState='preparing';
        const project=await forPremiere(item.nestedProject);
        const response=await fetch('/api/media/nested-preview',{method:'POST',headers:{'Content-Type':'application/json'},
                                 body:JSON.stringify({project}),signal:AbortSignal.timeout(3600000)});
        const body=await response.json();
        if(!response.ok||!localMedia(body.src))throw Error(body.error||'Nested preview failed');
        item.src=body.src;item.proxyState='ready';item.offline=false;
      }catch(e){item.proxyState='unavailable';item.proxyError=e.message;}
      window.Editor?.refreshMedia?.();
    })();
    nestedRequests.set(item,task);return task;
  }
  async function remember(item) {
    return rememberMany([item]);
  }
  function rememberMany(items) {
    return putRecords(items.map(item=>({id:key(item),path:item.path,serverSrc:item.originalSrc,previewSrc:item.src})));
  }
  async function restoreSource(item, original) {
    const metadata=await probe(original);
    const src=metadata.proxyRecommended||item.seekOptimized?await makeProxy(original,item.seekOptimized===true,item.audioStream??null):original;
    Object.assign(item,metadata,{src,originalSrc:original});
  }
  function needsSourceProbe(item) {
    return !Number.isInteger(item.audioStreams) || !Array.isArray(item.audioStreamDetails) || (item.audioStreams>1 && !Array.isArray(item.audioStreamChannels)) ||
      (item.hdr && item.hdrPreviewVersion!==3) || (item.still && item.stillPreviewVersion!==1);
  }
  async function restore(project) {
    const db = await open();
    for (const item of project.media) {
      if (Number.isInteger(item.nativeGenerator) || item.nativeUnsupported) {
        item.offline=false;
        continue;
      }
      if (Number.isInteger(item.nestedSequence)) {
        if(item.nestedProject) await restore(item.nestedProject);
        if(item.src && (!localMedia(item.src)||!await exists(item.src)))item.src='';
        item.offline=false;
        continue;
      }
      if ((bundled(item.src) || localMedia(item.src)) && await exists(item.src)) {
        const original=item.originalSrc||item.src;
        const bundledStill=bundled(item.src) && /\.(png|jpe?g|bmp)$/i.test(item.src);
        if ((bundledStill || localMedia(item.src) && (!item.originalSrc || needsSourceProbe(item))) &&
            (original===item.src || await exists(original))) await restoreSource(item,original);
        item.offline = false;
        continue;
      }
      let record = await read(
        db.transaction("files").objectStore("files").get(key(item)),
      );
      if (!record && item.path) {
        const byPath = db
          .transaction("files")
          .objectStore("files")
          .index("path");
        if ((await read(byPath.count(item.path))) === 1) {
          record = await read(
            db
              .transaction("files")
              .objectStore("files")
              .index("path")
              .get(item.path),
          );
        }
      }
      if (record?.serverSrc && await exists(record.serverSrc)) {
        item.originalSrc=record.serverSrc;
        item.src=record.previewSrc && await exists(record.previewSrc) ? record.previewSrc : record.serverSrc;
        if(item.src===record.serverSrc || needsSourceProbe(item))
          await restoreSource(item,record.serverSrc);
        item.storageKey=record.id; item.offline=false;
      } else if (record?.file) {
        if (!urls.has(record.id))
          urls.set(record.id, URL.createObjectURL(record.file));
        item.src = urls.get(record.id);
        item.storageKey = record.id;
        item.offline = false;
      } else {
        item.src = "";
        item.offline = true;
      }
    }
    return project;
  }
  async function forPremiere(project) {
    const copy = structuredClone(project);
    const db = await open();
    for (const item of copy.media) {
      delete item.thumb;
      if(Number.isInteger(item.nestedSequence)) {if(item.nestedProject) item.nestedProject=await forPremiere(item.nestedProject);continue;}
      if(localMedia(item.originalSrc) || bundled(item.originalSrc)) {item.src=item.originalSrc;continue;}
      if (!item.src || bundled(item.src) || localMedia(item.src)) continue;
      const record = await read(
        db
          .transaction("files")
          .objectStore("files")
          .get(item.storageKey || key(item)),
      );
      if (!record)
        throw new Error(
          "Relink " + item.name + " before saving a Premiere project.",
        );
      if (!record.serverSrc || !await exists(record.serverSrc)) {
        const response = await fetch(
          "/api/media/upload?name=" + encodeURIComponent(record.file.name),
          {
            method: "POST",
            body: record.file,
            signal: AbortSignal.timeout(300000),
          },
        );
        const result = await response.json();
        if (!response.ok || !localMedia(result.src))
          throw new Error(result.error || "Could not store " + item.name);
        record.serverSrc = result.src;
        await putRecords([record]);
      }
      item.src = record.serverSrc;
    }
    return copy;
  }
  return { importFile, remember, rememberMany, makeProxy, prepareNested, preparePreview, hdrPreviewStatus, restore, matches, matchFolder, forPremiere };
})();
