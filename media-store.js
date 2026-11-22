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
    /^(?:\.\/|\/)?assets\/(?:ridge|forest|valley|lake)\.mp4$|^(?:\.\/|\/)?assets\/score\.wav$/.test(
      src,
    );
  const localMedia = (src) => /^\/media\/[a-f0-9]{32}\/[^/]+$/.test(src);
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
  async function put(record) {
    const db = await open();
    const transaction = db.transaction("files", "readwrite");
    const committed = new Promise((resolve, reject) => {
      transaction.oncomplete = resolve;
      transaction.onabort = () =>
        reject(
          transaction.error || new Error("Could not store this media file."),
        );
    });
    transaction.objectStore("files").put(record);
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
  async function exists(src) {
    if (!localMedia(src) && !bundled(src)) return false;
    return (await fetch(src,{method:'HEAD',cache:'no-store',signal:AbortSignal.timeout(15000)})).ok;
  }
  async function remember(item, file) {
    item.path ||= file.name;
    const serverSrc = await upload(file);
    await put({id:key(item),path:item.path,serverSrc});
    if (item.src?.startsWith('blob:')) URL.revokeObjectURL(item.src);
    item.src=serverSrc;
    item.offline=false;
    return item;
  }
  async function restore(project) {
    const db = await open();
    for (const item of project.media) {
      if ((bundled(item.src) || localMedia(item.src)) && await exists(item.src)) {
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
        item.src=record.serverSrc; item.storageKey=record.id; item.offline=false;
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
        await put(record);
      }
      item.src = record.serverSrc;
    }
    return copy;
  }
  return { remember, restore, matches, forPremiere };
})();
