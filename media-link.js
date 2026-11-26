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

  // The folder browser both dialogs share: a path bar, places, and one directory's entries.
  // With `selectFiles`, media files get checkboxes; otherwise only folders can be opened.
  function folderBrowser({selectFiles, onChange}) {
    const location = element('input', {type: 'text', id: 'link-location', spellcheck: false,
                                       placeholder: 'Folder path', ariaLabel: 'Folder path'});
    const go = element('button', {type: 'button', textContent: 'Go', id: 'link-go'});
    const up = element('button', {type: 'button', textContent: '↑', title: 'Parent folder', id: 'link-up'});
    const places = element('nav', {className: 'link-places', ariaLabel: 'Places'});
    const list = element('ul', {className: 'link-entries', id: 'link-entries'});
    const message = element('p', {className: 'link-message', id: 'link-message', role: 'status'});
    const state = {current: null};
    let shown = 0;
    async function show(path) {
      const request = ++shown;
      message.textContent = 'Loading…';
      try {
        const data = await api('/api/files?path=' + encodeURIComponent(path || ''));
        if (request !== shown) return;  // A newer folder was chosen meanwhile.
        state.current = data.path;
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
          } else if (selectFiles) {
            const box = element('input', {type: 'checkbox', disabled: entry.kind === 'raw', onchange: onChange});
            row.append(element('label', {title: entry.kind === 'raw' ? 'Camera RAW needs a transcode from the vendor tool' : entry.path},
              box, element('span', {textContent: entry.name}), element('small', {textContent: size(entry.size)})));
          } else {
            row.append(element('label', {title: entry.path}, element('span', {textContent: entry.name}),
                               element('small', {textContent: size(entry.size)})));
          }
          return row;
        }));
        message.textContent = data.path ? (data.truncated ? 'Showing the first 5,000 items.' : '') : 'Choose a place or type a folder path.';
        onChange();
        return true;
      } catch (error) { if (request === shown) message.textContent = error.message; }
      onChange();
      return false;
    }
    location.onkeydown = event => { if (event.key === 'Enter') show(location.value.trim()); };
    go.onclick = () => show(location.value.trim());
    up.onclick = () => show(up.dataset.path);
    return {state, show, list, message,
            nodes: [element('div', {className: 'link-bar'}, up, location, go),
                    element('div', {className: 'link-body'}, places, list), message]};
  }

  function openPicker() {
    const dialog = modal('Link media in place', 'splice-link');
    const note = element('p', {className: 'link-note', textContent:
      'Linked files stay where they are and are not copied. Keep the drive connected while you edit; ' +
      'if a file moves, Splice marks it offline so you can relink it here.'});
    const button = (id, text, primary) => element('button', {type: 'button', id, textContent: text, className: primary ? 'primary' : ''});
    const importSelected = button('link-import', 'Link selected files', true);
    const importFolder = button('link-import-folder', 'Link whole folder');
    const relinkSelected = button('link-relink', 'Relink offline from selection');
    const relinkFolder = button('link-relink-folder', 'Relink offline from folder');
    const cancel = button('link-cancel', 'Cancel');
    const browser = folderBrowser({selectFiles: true, onChange: () => refresh()});
    const {state, list, message} = browser;
    dialog.append(...browser.nodes, note,
                  element('footer', {}, cancel, relinkFolder, relinkSelected, importFolder, importSelected));
    const selected = () => [...list.querySelectorAll('input:checked')].map(box => box.closest('li').entry);
    function refresh() {
      const files = selected().length;
      importSelected.disabled = relinkSelected.disabled = !files;
      importFolder.disabled = relinkFolder.disabled = !state.current;
      const offline = window.Editor?.get().project.media.some(item => item.offline);
      relinkSelected.hidden = relinkFolder.hidden = !offline;
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
      const {files, notice} = await post('/api/media/link', {folder: state.current}, 600000);
      if (notice) window.alert(notice);
      const failed = files.filter(file => file.error);
      const linked = files.filter(file => !file.error);
      if (!linked.length) throw Error(failed[0]?.error || 'The folder contains no media files.');
      if (failed.length) window.alert(failed.length + ' file(s) were skipped:\n' + failed.slice(0, 8).map(f => f.name + ': ' + f.error).join('\n'));
      return linked.map(entry => asFile(entry, true));
    }
    cancel.onclick = () => dialog.close();
    importSelected.onclick = () => run('Linking…', () => Editor.addMedia(selected().map(entry => asFile(entry))));
    relinkSelected.onclick = () => run('Relinking…', () => Editor.relinkMedia(selected().map(entry => asFile(entry))));
    importFolder.onclick = () => run('Linking folder…', async () => Editor.addMedia(await linkFolder()));
    relinkFolder.onclick = () => run('Relinking from folder…', async () => Editor.relinkFolder(await linkFolder()));
    dialog.showModal();
    browser.show(null);
    return dialog;
  }

  // Choose a folder on this computer (nothing is linked). Resolves to its absolute path, or
  // null when cancelled. Opens over any dialog already showing.
  function pickFolder({title = 'Choose a folder', initial = ''} = {}) {
    return new Promise(resolve => {
      const dialog = modal(title, 'splice-link splice-folder-pick');
      const cancel = element('button', {type: 'button', id: 'pick-cancel', textContent: 'Cancel'});
      const choose = element('button', {type: 'button', id: 'pick-choose', textContent: 'Choose this folder', className: 'primary'});
      const browser = folderBrowser({selectFiles: false, onChange: () => { choose.disabled = !browser.state.current; }});
      dialog.append(...browser.nodes, element('footer', {}, cancel, choose));
      let chosen = null;
      dialog.addEventListener('close', () => resolve(chosen));
      cancel.onclick = () => dialog.close();
      choose.onclick = () => { chosen = browser.state.current; dialog.close(); };
      choose.disabled = true;
      dialog.showModal();
      // A remembered folder that is gone opens the list of places instead.
      browser.show(initial || null).then(ok => { if (!ok && initial) browser.show(null); });
    });
  }

  // Media cache. Proxies and previews are rebuilt on demand, so unused ones
  // can go in bulk. Imported copies are originals the user handed to Splice,
  // possibly the only copy, and another Splice window, the desktop app or a
  // project file saved to disk may use one this browser does not know about:
  // they are deleted only when the user selects them, after a confirmation.
  async function openCache() {
    const dialog = modal('Media cache', 'splice-cache');
    const body = element('div', {className: 'cache-body', textContent: 'Measuring…'});
    const message = element('p', {className: 'link-message', id: 'cache-message', role: 'status'});
    const prune = element('button', {type: 'button', id: 'cache-prune', textContent: 'Delete unused proxies'});
    const remove = element('button', {type: 'button', id: 'cache-delete', textContent: 'Delete selected copies…', disabled: true});
    const forget = element('button', {type: 'button', id: 'cache-forget', textContent: 'Forget unused links…'});
    const close = element('button', {type: 'button', textContent: 'Close', className: 'primary', onclick: () => dialog.close()});
    dialog.append(body, message, element('p', {className: 'link-note', textContent:
      '"Used" means a project in this browser\'s Recent projects & recovery refers to it. Other Splice windows, the ' +
      'desktop app and project files saved to disk are not known here. Proxies are rebuilt on demand.'}),
      element('footer', {}, forget, remove, prune, close));
    dialog.showModal();
    let report, used = new Set();
    const date = seconds => seconds ? new Date(seconds * 1000).toLocaleDateString() : '—';
    const checked = () => [...body.querySelectorAll('input.cache-select:checked')].map(box => box.value);
    const refresh = () => { remove.disabled = !checked().length; };
    async function render() {
      const [next, references] = await Promise.all([api('/api/media/cache'), SpliceSession.mediaReferences()]);
      report = next;
      used = new Set(references.map(src => (src.match(/^\/media\/(?:linked\/)?([a-f0-9]{32})\//) || [])[1]).filter(Boolean));
      const imports = report.entries.filter(entry => entry.kind === 'import');
      const rows = imports.map(entry => element('tr', {className: 'cache-import'},
        element('td', {}, element('input', {type: 'checkbox', className: 'cache-select', value: entry.id, onchange: refresh,
                                            ariaLabel: 'Select ' + entry.files.join(', ')})),
        element('td', {textContent: entry.files.join(', ') || entry.id}), element('td', {textContent: size(entry.bytes)}),
        element('td', {textContent: date(entry.lastUsed)}),
        element('td', {className: used.has(entry.id) ? 'cache-used' : 'cache-unused',
                       textContent: used.has(entry.id) ? 'Used here' : 'Not used by a project here'})));
      const linkedUnused = report.links.filter(link => !used.has(link.id)).length;
      body.replaceChildren(
        element('dl', {},
          element('dt', {textContent: 'Imported copies'}), element('dd', {textContent: size(report.importBytes), id: 'cache-imports'}),
          element('dt', {textContent: 'Proxies'}), element('dd', {textContent: size(report.proxyBytes), id: 'cache-proxies'}),
          element('dt', {textContent: 'Linked originals'}), element('dd', {id: 'cache-links', textContent: report.linkedFiles +
            ' files, ' + size(report.linkedBytes) + ' (not copied)' + (report.linkedFiles >= report.maxLinks ? ' — list full' : '')}),
          element('dt', {textContent: 'Free space'}), element('dd', {textContent: size(report.freeBytes)})),
        ...(report.notice ? [element('p', {className: 'link-message', id: 'cache-notice', textContent: report.notice})] : []),
        ...(report.unclassified ? [element('p', {className: 'link-note', id: 'cache-unclassified', textContent:
          `${report.unclassified} folder(s) from an earlier Splice version are listed as imported copies until Splice ` +
          'has checked them; reopen Media cache to continue. The bulk delete never removes them meanwhile.'})] : []),
        element('h3', {textContent: 'Imported copies'}),
        imports.length ? element('table', {id: 'cache-import-list'},
          element('thead', {}, element('tr', {}, ...['', 'File', 'Size', 'Last used', 'Projects'].map(text => element('th', {textContent: text})))),
          element('tbody', {}, ...rows)) : element('p', {textContent: 'None.'}));
      prune.disabled = !report.entries.some(entry => entry.kind === 'proxy');
      forget.disabled = !report.linkedFiles;
      forget.title = linkedUnused + ' of the newest ' + report.links.length + ' links are not used here';
      refresh();
    }
    const done = (verb, result) => `${verb} ${result.deleted.length} item(s), freed ${size(result.freedBytes)}.` +
      (result.failed.length ? ` ${result.failed.length} could not be deleted.` : '');
    async function act(button, action) {
      button.disabled = true;
      try { message.textContent = await action(); await render(); }
      catch (error) { message.textContent = error.message; button.disabled = false; }
    }
    prune.onclick = () => act(prune, async () => {
      const referenced = await SpliceSession.mediaReferences();
      return done('Deleted', await post('/api/media/cache/prune', {referenced})) + ' Imported copies were kept.';
    });
    remove.onclick = () => {
      const ids = checked();
      const chosen = report.entries.filter(entry => ids.includes(entry.id));
      const inUse = chosen.filter(entry => used.has(entry.id)).length;
      if (!window.confirm(`Delete ${chosen.length} imported cop${chosen.length === 1 ? 'y' : 'ies'} (` +
          size(chosen.reduce((sum, entry) => sum + entry.bytes, 0)) + ')? These are the files you imported into Splice. ' +
          'They cannot be rebuilt: if you no longer have the original elsewhere, it is gone. Projects in other Splice ' +
          'windows, the desktop app or saved project files that use them will need the source relinked.' +
          (inUse ? ` ${inUse} of them ${inUse === 1 ? 'is' : 'are'} used by a project here.` : ''))) return;
      act(remove, async () => done('Deleted', await post('/api/media/cache/delete', {ids, confirm: true})));
    };
    forget.onclick = () => {
      if (!window.confirm('Forget linked originals no project in this browser uses? The files stay where they are. ' +
          'A project elsewhere that uses one shows it offline until you relink it.')) return;
      act(forget, async () => {
        const referenced = await SpliceSession.mediaReferences();
        const result = await post('/api/media/links/forget', {referenced});
        return `Forgot ${result.forgotten} link(s). No files were deleted.`;
      });
    };
    render().catch(error => { body.textContent = error.message; });
    return dialog;
  }

  document.getElementById('link-media')?.addEventListener('click', openPicker);
  document.getElementById('media-cache')?.addEventListener('click', openCache);
  return {openPicker, openCache, pickFolder};
})();
