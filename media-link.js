// Link media in place: browse this computer through the Splice server and
// reference originals where they are, with no copy. A browser page cannot
// hand the server a file path, so the server lists folders and the user picks.
// Also the media cache view: managed copies, proxies, and deleting unused ones.
window.SpliceLink = (() => {
  const GB = 1024 ** 3;
  const size = bytes => bytes >= GB ? (bytes / GB).toFixed(2) + ' GB' : bytes >= 1024 ** 2 ?
    (bytes / 1024 ** 2).toFixed(1) + ' MB' : Math.ceil(bytes / 1024) + ' KB';
  async function api(url, options = {}) {
    const response = await fetch(url, {signal: AbortSignal.timeout(options.timeout || 120000), ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.error || 'Request failed (' + response.status + ')');
    return data;
  }
  const post = (url, body, timeout) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify(body), timeout});
  function element(tag, props = {}, ...children) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  }
  function modal(title, className) {
    const dialog = element('dialog', {className: 'studio-dialog ' + className});
    dialog.setAttribute('aria-label', title);
    dialog.append(element('h2', {textContent: title}));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    return dialog;
  }
  const status = message => { const node = document.getElementById('timeline-status'); if (node) node.textContent = message; };
  // Picked entries behave like File objects for Editor.addMedia/relink*.
  const asFile = (entry, linked) => ({name: entry.name, splicePath: entry.sourcePath || entry.path,
                                      webkitRelativePath: entry.relativePath || '', spliceLinked: linked ? entry : undefined});

  function openPicker() {
    const dialog = modal('Link media in place', 'splice-link');
    const location = element('input', {type: 'text', id: 'link-location', spellcheck: false,
                                       placeholder: 'Folder path', ariaLabel: 'Folder path'});
    const go = element('button', {type: 'button', textContent: 'Go', id: 'link-go'});
    const up = element('button', {type: 'button', textContent: '↑', title: 'Parent folder', id: 'link-up'});
    const places = element('nav', {className: 'link-places', ariaLabel: 'Places'});
    const list = element('ul', {className: 'link-entries', id: 'link-entries'});
    const message = element('p', {className: 'link-message', id: 'link-message', role: 'status'});
    const note = element('p', {className: 'link-note', textContent:
      'Linked files stay where they are and are not copied. Keep the drive connected while you edit; ' +
      'if a file moves, Splice marks it offline so you can relink it here.'});
    const button = (id, text, primary) => element('button', {type: 'button', id, textContent: text, className: primary ? 'primary' : ''});
    const importSelected = button('link-import', 'Link selected files', true);
    const importFolder = button('link-import-folder', 'Link whole folder');
    const relinkSelected = button('link-relink', 'Relink offline from selection');
    const relinkFolder = button('link-relink-folder', 'Relink offline from folder');
    const cancel = button('link-cancel', 'Cancel');
    dialog.append(element('div', {className: 'link-bar'}, up, location, go),
                  element('div', {className: 'link-body'}, places, list), message, note,
                  element('footer', {}, cancel, relinkFolder, relinkSelected, importFolder, importSelected));
    let current = null, shown = 0;
    const selected = () => [...list.querySelectorAll('input:checked')].map(box => box.closest('li').entry);
    function refresh() {
      const files = selected().length;
      importSelected.disabled = relinkSelected.disabled = !files;
      importFolder.disabled = relinkFolder.disabled = !current;
      const offline = window.Editor?.get().project.media.some(item => item.offline);
      relinkSelected.hidden = relinkFolder.hidden = !offline;
    }
    async function show(path) {
      const request = ++shown;
      message.textContent = 'Loading…';
      try {
        const data = await api('/api/files?path=' + encodeURIComponent(path || ''));
        if (request !== shown) return;  // A newer folder was chosen meanwhile.
        current = data.path;
        if (data.path) location.value = data.path;  // Keep a path the user is still typing.
        up.disabled = !data.parent; up.dataset.path = data.parent || '';
        places.replaceChildren(...data.places.map(place => element('button', {type: 'button', textContent: place.name,
          title: place.path, onclick: () => show(place.path)})));
        list.replaceChildren(...data.entries.map(entry => {
          const row = element('li', {className: 'link-' + entry.kind});
          row.entry = entry;
          if (entry.folder) {
            row.append(element('button', {type: 'button', className: 'link-folder', textContent: '▸ ' + entry.name,
                                          onclick: () => show(entry.path)}));
          } else {
            const box = element('input', {type: 'checkbox', disabled: entry.kind === 'raw', onchange: refresh});
            row.append(element('label', {title: entry.kind === 'raw' ? 'Camera RAW needs a transcode from the vendor tool' : entry.path},
              box, element('span', {textContent: entry.name}), element('small', {textContent: size(entry.size)})));
          }
          return row;
        }));
        message.textContent = data.path ? (data.truncated ? 'Showing the first 5,000 items.' : '') : 'Choose a place or type a folder path.';
      } catch (error) { if (request === shown) message.textContent = error.message; }
      refresh();
    }
    async function run(label, action) {
      message.textContent = label;
      dialog.querySelectorAll('footer button').forEach(b => { b.disabled = true; });
      try {
        const result = await action();
        if (result !== false) { dialog.close(); status('Media linked in place'); }
      } catch (error) { message.textContent = error.message; refresh(); cancel.disabled = false; }
    }
    async function linkFolder() {
      const {files} = await post('/api/media/link', {folder: current}, 600000);
      const failed = files.filter(file => file.error);
      const linked = files.filter(file => !file.error);
      if (!linked.length) throw Error(failed[0]?.error || 'The folder contains no media files.');
      if (failed.length) window.alert(failed.length + ' file(s) were skipped:\n' + failed.slice(0, 8).map(f => f.name + ': ' + f.error).join('\n'));
      return linked.map(entry => asFile(entry, true));
    }
    location.onkeydown = event => { if (event.key === 'Enter') show(location.value.trim()); };
    go.onclick = () => show(location.value.trim());
    up.onclick = () => show(up.dataset.path);
    cancel.onclick = () => dialog.close();
    importSelected.onclick = () => run('Linking…', () => Editor.addMedia(selected().map(entry => asFile(entry))));
    relinkSelected.onclick = () => run('Relinking…', () => Editor.relinkMedia(selected().map(entry => asFile(entry))));
    importFolder.onclick = () => run('Linking folder…', async () => Editor.addMedia(await linkFolder()));
    relinkFolder.onclick = () => run('Relinking from folder…', async () => Editor.relinkFolder(await linkFolder()));
    dialog.showModal();
    show(null);
    return dialog;
  }

  async function openCache() {
    const dialog = modal('Media cache', 'splice-cache');
    const body = element('div', {className: 'cache-body', textContent: 'Measuring…'});
    const message = element('p', {className: 'link-message', id: 'cache-message', role: 'status'});
    const prune = element('button', {type: 'button', id: 'cache-prune', textContent: 'Delete unused copies and proxies'});
    const close = element('button', {type: 'button', textContent: 'Close', className: 'primary', onclick: () => dialog.close()});
    dialog.append(body, message, element('p', {className: 'link-note', textContent:
      'Unused means no project in Recent projects & recovery refers to it. Proxies are rebuilt on demand. ' +
      'A project saved elsewhere that used a deleted imported copy will ask you to relink that source.'}),
      element('footer', {}, prune, close));
    dialog.showModal();
    async function render() {
      const report = await api('/api/media/cache');
      const rows = report.entries.slice(0, 40).map(entry => element('tr', {},
        element('td', {textContent: entry.kind === 'proxy' ? 'Proxy' : 'Imported copy'}),
        element('td', {textContent: entry.files.join(', ')}), element('td', {textContent: size(entry.bytes)})));
      body.replaceChildren(
        element('dl', {},
          element('dt', {textContent: 'Imported copies'}), element('dd', {textContent: size(report.importBytes), id: 'cache-imports'}),
          element('dt', {textContent: 'Proxies'}), element('dd', {textContent: size(report.proxyBytes), id: 'cache-proxies'}),
          element('dt', {textContent: 'Linked originals'}), element('dd', {textContent: report.linkedFiles + ' files, ' + size(report.linkedBytes) + ' (not copied)'}),
          element('dt', {textContent: 'Free space'}), element('dd', {textContent: size(report.freeBytes)})),
        element('table', {}, ...rows));
      prune.disabled = !report.entries.length;
    }
    prune.onclick = async () => {
      if (!window.confirm('Delete imported copies and proxies that no recent project uses?')) return;
      prune.disabled = true;
      try {
        const referenced = await SpliceSession.mediaReferences();
        const result = await post('/api/media/cache/prune', {referenced});
        message.textContent = `Deleted ${result.deleted.length} item(s), freed ${size(result.freedBytes)}.` +
          (result.failed.length ? ` ${result.failed.length} could not be deleted.` : '');
        await render();
      } catch (error) { message.textContent = error.message; prune.disabled = false; }
    };
    render().catch(error => { body.textContent = error.message; });
    return dialog;
  }

  document.getElementById('link-media')?.addEventListener('click', openPicker);
  document.getElementById('media-cache')?.addEventListener('click', openCache);
  return {openPicker, openCache};
})();
