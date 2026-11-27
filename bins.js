// Real Bin Management, Subclips, and Editorial Metadata (Scene, Shot, Take, Reel, Log Note, Color Labels).
window.SpliceBins = (() => {
  const uid = () => crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 12);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const COLOR_LABELS = [
    { key: '', name: 'None' },
    { key: 'blue', name: 'Blue' },
    { key: 'green', name: 'Green' },
    { key: 'yellow', name: 'Yellow' },
    { key: 'orange', name: 'Orange' },
    { key: 'red', name: 'Red' },
    { key: 'purple', name: 'Purple' },
    { key: 'teal', name: 'Teal' },
    { key: 'magenta', name: 'Magenta' },
  ];
  const VALID_COLORS = new Set(COLOR_LABELS.map(c => c.key));

  let treeExpanded = null; // null = auto (open when bins exist)

  function normalizeBinPath(raw) {
    return String(raw ?? '')
      .split('/')
      .map(part => part.trim())
      .filter(Boolean)
      .join('/');
  }

  function parentBinOf(path) {
    const norm = normalizeBinPath(path);
    const idx = norm.lastIndexOf('/');
    return idx > 0 ? norm.slice(0, idx) : '';
  }

  function allBins(project) {
    const set = new Set();
    const addWithAncestors = raw => {
      const norm = normalizeBinPath(raw);
      if (!norm) return;
      const parts = norm.split('/');
      let acc = '';
      for (const p of parts) {
        acc = acc ? acc + '/' + p : p;
        set.add(acc);
      }
    };
    for (const b of project?.bins || []) addWithAncestors(b);
    for (const m of project?.media || []) if (m.bin) addWithAncestors(m.bin);
    return [...set].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
  }

  function mediaMatchesBin(item, binFilter) {
    if (!binFilter) return true;
    const itemBin = normalizeBinPath(item.bin || '');
    if (binFilter === '@root') return !itemBin;
    const target = normalizeBinPath(binFilter);
    return itemBin === target || itemBin.startsWith(target + '/');
  }

  function createBin(rawPath) {
    const path = normalizeBinPath(rawPath);
    if (!path) throw new Error('Enter a bin name.');
    window.Editor.editProject(next => {
      const bins = new Set(allBins(next));
      const parts = path.split('/');
      let acc = '';
      for (const p of parts) {
        acc = acc ? acc + '/' + p : p;
        bins.add(acc);
      }
      next.bins = [...bins].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
    });
    treeExpanded = true;
    return path;
  }

  function renameBin(oldRaw, newRaw) {
    const oldPath = normalizeBinPath(oldRaw);
    const newPath = normalizeBinPath(newRaw);
    if (!oldPath) throw new Error('Choose a bin to rename.');
    if (!newPath) throw new Error('Enter a new bin name.');
    if (oldPath === newPath) return newPath;
    if (newPath.startsWith(oldPath + '/')) throw new Error('Cannot move a bin inside itself.');
    const remap = value => {
      const norm = normalizeBinPath(value);
      if (norm === oldPath) return newPath;
      if (norm.startsWith(oldPath + '/')) return newPath + norm.slice(oldPath.length);
      return norm;
    };
    window.Editor.editProject(next => {
      const updatedBins = new Set();
      for (const b of allBins(next)) {
        const mapped = remap(b);
        if (mapped) {
          const parts = mapped.split('/');
          let acc = '';
          for (const p of parts) { acc = acc ? acc + '/' + p : p; updatedBins.add(acc); }
        }
      }
      for (const m of next.media) {
        if (m.bin) m.bin = remap(m.bin);
      }
      next.bins = [...updatedBins].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
    });
    return newPath;
  }

  function deleteBin(rawPath) {
    const path = normalizeBinPath(rawPath);
    if (!path) return { moved: 0, targetBin: '' };
    const parent = parentBinOf(path);
    let moved = 0;
    window.Editor.editProject(next => {
      for (const m of next.media) {
        const norm = normalizeBinPath(m.bin || '');
        if (norm === path || norm.startsWith(path + '/')) {
          if (parent) m.bin = parent;
          else delete m.bin;
          moved++;
        }
      }
      const remaining = (next.bins || allBins(next))
        .map(normalizeBinPath)
        .filter(b => b && b !== path && !b.startsWith(path + '/'));
      if (parent && !remaining.includes(parent)) remaining.push(parent);
      next.bins = [...new Set(remaining)].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
    });
    return { moved, targetBin: parent };
  }

  function moveMediaToBin(mediaIds, rawBin) {
    const ids = new Set(Array.isArray(mediaIds) ? mediaIds : [mediaIds]);
    if (!ids.size) return 0;
    const target = normalizeBinPath(rawBin);
    let count = 0;
    window.Editor.editProject(next => {
      if (target) {
        const bins = new Set(allBins(next));
        const parts = target.split('/');
        let acc = '';
        for (const p of parts) { acc = acc ? acc + '/' + p : p; bins.add(acc); }
        next.bins = [...bins].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
      }
      for (const m of next.media) {
        if (!ids.has(m.id)) continue;
        if (target) m.bin = target;
        else delete m.bin;
        count++;
      }
    });
    return count;
  }

  function openCreateBinDialog(parentPath, onCreated, notify) {
    const existing = document.getElementById('bin-create-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'bin-create-dialog';
    dialog.className = 'studio-dialog';
    const prefix = normalizeBinPath(parentPath);
    dialog.innerHTML =
      `<h2>Create bin</h2>` +
      `<form>` +
      `<label>Bin path (use / for sub-bins)<input name="path" type="text" value="${esc(prefix ? prefix + '/' : '')}" placeholder="e.g. Scene 1/Cam A" required autocomplete="off"></label>` +
      `<p class="form-status" role="status"></p>` +
      `<footer><button type="button" data-close>Cancel</button><button class="primary" type="submit">Create bin</button></footer>` +
      `</form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form');
    const status = dialog.querySelector('.form-status');
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.onsubmit = e => {
      e.preventDefault();
      try {
        const created = createBin(form.elements.path.value);
        dialog.close();
        onCreated?.(created);
        notify?.(`Created bin "${created}"`);
      } catch (err) {
        status.textContent = err.message || 'Could not create bin.';
      }
    };
    dialog.showModal();
    form.elements.path.focus();
  }

  function openRenameBinDialog(oldPath, onRenamed, notify) {
    const existing = document.getElementById('bin-rename-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'bin-rename-dialog';
    dialog.className = 'studio-dialog';
    dialog.innerHTML =
      `<h2>Rename bin</h2>` +
      `<form>` +
      `<label>New bin path<input name="path" type="text" value="${esc(oldPath)}" required autocomplete="off"></label>` +
      `<p class="form-status" role="status"></p>` +
      `<footer><button type="button" data-close>Cancel</button><button class="primary" type="submit">Rename</button></footer>` +
      `</form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form');
    const status = dialog.querySelector('.form-status');
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.onsubmit = e => {
      e.preventDefault();
      try {
        const renamed = renameBin(oldPath, form.elements.path.value);
        dialog.close();
        onRenamed?.(oldPath, renamed);
        notify?.(`Renamed bin to "${renamed}"`);
      } catch (err) {
        status.textContent = err.message || 'Could not rename bin.';
      }
    };
    dialog.showModal();
    form.elements.path.select();
  }

  function openDeleteBinDialog(path, onDeleted, notify) {
    const project = window.Editor.get().project;
    const norm = normalizeBinPath(path);
    const affected = project.media.filter(m => mediaMatchesBin(m, norm));
    const parent = parentBinOf(norm);
    const destLabel = parent ? `parent bin "${parent}"` : 'Unfiled';
    const existing = document.getElementById('bin-delete-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'bin-delete-dialog';
    dialog.className = 'studio-dialog';
    dialog.innerHTML =
      `<h2>Delete bin</h2>` +
      `<p>${affected.length
        ? `Bin "${esc(norm)}" contains ${affected.length} item${affected.length === 1 ? '' : 's'}. Deleting this bin moves those items to ${esc(destLabel)} — no media will be deleted.`
        : `Delete empty bin "${esc(norm)}"?`}</p>` +
      `<footer><button type="button" data-close>Cancel</button><button type="button" class="primary" data-confirm>Delete bin</button></footer>`;
    document.body.append(dialog);
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    dialog.querySelector('[data-confirm]').onclick = () => {
      const res = deleteBin(norm);
      dialog.close();
      onDeleted?.(norm, res.targetBin);
      notify?.(res.moved ? `Deleted bin "${norm}" and moved ${res.moved} item(s) to ${destLabel}` : `Deleted empty bin "${norm}"`);
    };
    dialog.showModal();
  }

  function openMoveToBinDialog(mediaIds, notify) {
    const ids = Array.isArray(mediaIds) ? mediaIds : [mediaIds];
    if (!ids.length) {
      notify?.('Select one or more media items first.');
      return;
    }
    const project = window.Editor.get().project;
    const bins = allBins(project);
    const existing = document.getElementById('bin-move-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'bin-move-dialog';
    dialog.className = 'studio-dialog';
    const options = [`<option value="">Unfiled (root)</option>`, ...bins.map(b => `<option value="${esc(b)}">${esc(b)}</option>`)].join('');
    dialog.innerHTML =
      `<h2>Move ${ids.length} item${ids.length === 1 ? '' : 's'} to bin</h2>` +
      `<form>` +
      `<label>Existing bin<select name="existing">${options}</select></label>` +
      `<label>Or type new bin path<input name="custom" type="text" placeholder="e.g. Scene 2/B-Roll" autocomplete="off"></label>` +
      `<footer><button type="button" data-close>Cancel</button><button class="primary" type="submit">Move</button></footer>` +
      `</form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form');
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.onsubmit = e => {
      e.preventDefault();
      const target = normalizeBinPath(form.elements.custom.value || form.elements.existing.value);
      const moved = moveMediaToBin(ids, target);
      dialog.close();
      notify?.(`Moved ${moved} item${moved === 1 ? '' : 's'} to ${target || 'Unfiled'}`);
    };
    dialog.showModal();
  }

  // ---- Subclips ----

  function shiftTimecode(baseTc, offsetSec, fps) {
    const rate = Number(fps) > 0 ? Number(fps) : 25;
    const baseSec = window.SpliceMulticam?.parseTimecodeSeconds?.(baseTc, rate);
    const total = (baseSec !== null && baseSec !== undefined ? baseSec : 0) + Math.max(0, Number(offsetSec) || 0);
    return window.Editor.formatTimecode(total, rate);
  }

  function nextSubclipName(project, parentName) {
    const existing = new Set((project?.media || []).map(m => m.name));
    for (let i = 1; i < 999; i++) {
      const candidate = `${parentName} Subclip ${i}`;
      if (!existing.has(candidate)) return candidate;
    }
    return `${parentName} Subclip`;
  }

  function createSubclip({ parentMediaId, name, subclipIn, subclipOut, bin }) {
    const editor = window.Editor;
    const project = editor.get().project;
    const directParent = project.media.find(m => m.id === parentMediaId);
    if (!directParent) throw new Error('Parent media not found.');
    if (directParent.multicam) throw new Error('Subclips of multicam clips are not supported.');

    // Anchor subclip-of-subclip cleanly onto root media item with cumulative offset.
    const resolved = editor.resolveClipMedia({ mediaId: directParent.id, type: directParent.type }, project);
    const rootParent = resolved.item || directParent;
    const baseOffset = resolved.inShift || 0;

    const rawIn = Number(subclipIn);
    const rawOut = Number(subclipOut);
    if (!Number.isFinite(rawIn) || !Number.isFinite(rawOut) || rawIn < 0 || rawOut <= rawIn) {
      throw new Error('Subclip Out point must be after its In point.');
    }
    const maxLocal = directParent.duration || rootParent.duration || rawOut;
    const clampedIn = Math.max(0, Math.min(rawIn, maxLocal));
    const clampedOut = Math.max(clampedIn, Math.min(rawOut, maxLocal));
    const dur = clampedOut - clampedIn;
    if (dur < 1 / (project.fps || 25) - 1e-6) {
      throw new Error('Subclip must be at least one frame long.');
    }

    const absIn = baseOffset + clampedIn;
    const absOut = baseOffset + clampedOut;
    const fps = directParent.fps || rootParent.fps || project.fps || 25;
    const cleanBin = normalizeBinPath(bin !== undefined ? bin : directParent.bin || '');
    const subName = String(name || nextSubclipName(project, directParent.name)).trim() || nextSubclipName(project, directParent.name);
    const subId = uid();
    const subItem = {
      id: subId,
      name: subName,
      src: rootParent.src || '',
      ...(rootParent.originalSrc ? { originalSrc: rootParent.originalSrc } : {}),
      type: rootParent.type,
      duration: dur,
      fps,
      ...(rootParent.width ? { width: rootParent.width } : {}),
      ...(rootParent.height ? { height: rootParent.height } : {}),
      ...(rootParent.audioChannels !== undefined ? { audioChannels: rootParent.audioChannels } : {}),
      ...(rootParent.audioStreams !== undefined ? { audioStreams: rootParent.audioStreams } : {}),
      ...(rootParent.sourcePath ? { sourcePath: rootParent.sourcePath } : {}),
      ...(rootParent.reel ? { reel: rootParent.reel } : {}),
      ...(directParent.colorLabel ? { colorLabel: directParent.colorLabel } : {}),
      ...(cleanBin ? { bin: cleanBin } : {}),
      timecode: shiftTimecode(directParent.timecode || rootParent.timecode, clampedIn, fps),
      parentMediaId: rootParent.id,
      subclipIn: absIn,
      subclipOut: absOut,
      subclipLimits: true,
    };

    editor.editProject(next => {
      if (cleanBin) {
        const bins = new Set(allBins(next));
        const parts = cleanBin.split('/');
        let acc = '';
        for (const p of parts) { acc = acc ? acc + '/' + p : p; bins.add(acc); }
        next.bins = [...bins].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
      }
      next.media.push(subItem);
    });
    return subItem;
  }

  function openSubclipDialog(mediaId, range = {}, notify) {
    const project = window.Editor.get().project;
    const parent = project.media.find(m => m.id === mediaId);
    if (!parent) { notify?.('Media item not found.'); return; }
    if (parent.multicam) { notify?.('Subclips cannot be created from a multicam clip.'); return; }
    const fps = parent.fps || project.fps || 25;
    const inSec = Number.isFinite(range.in) ? Math.max(0, range.in) : (Number.isFinite(parent.sourceMarks?.in) ? parent.sourceMarks.in : 0);
    const outSec = Number.isFinite(range.out) ? Math.min(parent.duration || range.out, range.out) : (Number.isFinite(parent.sourceMarks?.out) ? parent.sourceMarks.out : (parent.duration || 0));
    if (outSec <= inSec + 1e-6) {
      notify?.('Mark a valid In/Out range before creating a subclip.');
      return;
    }

    const existing = document.getElementById('subclip-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'subclip-dialog';
    dialog.className = 'studio-dialog';
    const defaultName = nextSubclipName(project, parent.name);
    const bins = allBins(project);
    const binOptions = [
      `<option value="">Unfiled</option>`,
      ...bins.map(b => `<option value="${esc(b)}" ${b === normalizeBinPath(parent.bin || '') ? 'selected' : ''}>${esc(b)}</option>`),
    ].join('');
    dialog.innerHTML =
      `<h2>Make subclip</h2>` +
      `<form>` +
      `<label>Subclip name<input name="name" type="text" value="${esc(defaultName)}" required autocomplete="off"></label>` +
      `<p class="form-note">Range: ${esc(window.Editor.formatTimecode(inSec, fps))} – ${esc(window.Editor.formatTimecode(outSec, fps))} (${(outSec - inSec).toFixed(2)} s) · Restricted to subclip bounds</p>` +
      `<label>Bin<select name="bin">${binOptions}</select></label>` +
      `<p class="form-status" role="status"></p>` +
      `<footer><button type="button" data-close>Cancel</button><button class="primary" type="submit">Create subclip</button></footer>` +
      `</form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form');
    const status = dialog.querySelector('.form-status');
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.onsubmit = e => {
      e.preventDefault();
      try {
        const sub = createSubclip({
          parentMediaId: mediaId,
          name: form.elements.name.value,
          subclipIn: inSec,
          subclipOut: outSec,
          bin: form.elements.bin.value,
        });
        dialog.close();
        notify?.(`Created subclip "${sub.name}" (${sub.duration.toFixed(2)} s)`);
      } catch (err) {
        status.textContent = err.message || 'Could not create subclip.';
      }
    };
    dialog.showModal();
    form.elements.name.select();
  }

  function toggleSubclipLimits(mediaId) {
    let nextLimited = true;
    window.Editor.editProject(next => {
      const item = next.media.find(m => m.id === mediaId);
      if (!item || !item.parentMediaId) throw new Error('Selected item is not a subclip.');
      item.subclipLimits = item.subclipLimits === false;
      nextLimited = item.subclipLimits !== false;
    });
    return nextLimited;
  }

  function removeClipSubclipLimits(clipId) {
    window.Editor.editProject(next => {
      const clip = next.clips.find(c => c.id === clipId);
      if (!clip) return;
      const item = next.media.find(m => m.id === clip.mediaId);
      if (!item?.parentMediaId) throw new Error('Selected clip is not a subclip.');
      const resolved = window.Editor.resolveClipMedia(clip, next);
      if (!resolved.item) throw new Error('Parent media for subclip is missing.');
      const targets = [clip];
      if (clip.linkGroup) {
        for (const other of next.clips) {
          if (other.id !== clip.id && other.linkGroup === clip.linkGroup && other.mediaId === clip.mediaId) targets.push(other);
        }
      }
      for (const c of targets) {
        c.mediaId = resolved.item.id;
        c.in = (Number(c.in) || 0) + (resolved.inShift || 0);
      }
    });
  }

  function deleteMedia(mediaIds, notify) {
    const ids = new Set(Array.isArray(mediaIds) ? mediaIds : [mediaIds]);
    if (!ids.size) return;
    const project = window.Editor.get().project;
    // Find dependent subclips, multicam clips, and timeline clips.
    const depSubclips = project.media.filter(m => !ids.has(m.id) && m.parentMediaId && ids.has(m.parentMediaId));
    const allRemovedIds = new Set([...ids, ...depSubclips.map(s => s.id)]);
    const depMulticams = project.media.filter(m => !allRemovedIds.has(m.id) && m.multicam?.angles?.some(a => allRemovedIds.has(a.mediaId)));
    for (const mc of depMulticams) allRemovedIds.add(mc.id);
    const depClips = project.clips.filter(c => allRemovedIds.has(c.mediaId));

    const execute = () => {
      window.Editor.editProject(next => {
        next.clips = next.clips.filter(c => !allRemovedIds.has(c.mediaId));
        next.media = next.media.filter(m => !allRemovedIds.has(m.id));
      });
      notify?.(`Removed ${allRemovedIds.size} media item(s)` + (depClips.length ? ` and ${depClips.length} timeline clip(s)` : ''));
    };

    if (!depSubclips.length && !depMulticams.length && !depClips.length) {
      execute();
      return;
    }

    const parts = [];
    if (depSubclips.length) parts.push(`${depSubclips.length} subclip${depSubclips.length === 1 ? '' : 's'}`);
    if (depMulticams.length) parts.push(`${depMulticams.length} multicam clip${depMulticams.length === 1 ? '' : 's'}`);
    if (depClips.length) parts.push(`${depClips.length} timeline clip${depClips.length === 1 ? '' : 's'}`);

    const existing = document.getElementById('media-delete-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'media-delete-dialog';
    dialog.className = 'studio-dialog';
    dialog.innerHTML =
      `<h2>Delete referenced media?</h2>` +
      `<p>This media is referenced by ${esc(parts.join(', '))}. Deleting it will also remove those dependent items from the project (undoable with Ctrl+Z).</p>` +
      `<footer><button type="button" data-close>Cancel</button><button type="button" class="primary" data-confirm>Delete anyway</button></footer>`;
    document.body.append(dialog);
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    dialog.querySelector('[data-confirm]').onclick = () => {
      dialog.close();
      execute();
    };
    dialog.showModal();
  }

  // ---- Editorial Metadata & Color Labels ----

  function updateMediaMetadata(mediaId, patch) {
    window.Editor.editProject(next => {
      const item = next.media.find(m => m.id === mediaId);
      if (!item) return;
      for (const field of ['scene', 'shot', 'camera', 'take', 'reel', 'logNote']) {
        if (!(field in patch)) continue;
        const val = String(patch[field] ?? '').trim();
        if (val) item[field] = val;
        else delete item[field];
      }
      if ('bin' in patch) {
        const cleanBin = normalizeBinPath(patch.bin);
        if (cleanBin) {
          item.bin = cleanBin;
          const bins = new Set(allBins(next));
          const parts = cleanBin.split('/');
          let acc = '';
          for (const p of parts) { acc = acc ? acc + '/' + p : p; bins.add(acc); }
          next.bins = [...bins].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
        } else {
          delete item.bin;
        }
      }
      if ('colorLabel' in patch) {
        const key = String(patch.colorLabel || '').trim();
        if (key && VALID_COLORS.has(key)) item.colorLabel = key;
        else delete item.colorLabel;
      }
    });
  }

  function setClipColorLabel(clipIds, colorLabel) {
    const ids = new Set(Array.isArray(clipIds) ? clipIds : [clipIds]);
    if (!ids.size) return;
    const key = String(colorLabel || '').trim();
    if (key && !VALID_COLORS.has(key)) throw new Error('Invalid color label.');
    window.Editor.editProject(next => {
      for (const clip of next.clips) {
        if (!ids.has(clip.id)) continue;
        if (key) clip.colorLabel = key;
        else delete clip.colorLabel;
      }
    });
  }

  function openMetadataDialog(mediaId, notify) {
    const project = window.Editor.get().project;
    const item = project.media.find(m => m.id === mediaId);
    if (!item) return;
    const existing = document.getElementById('media-metadata-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'media-metadata-dialog';
    dialog.className = 'studio-dialog meta-dialog';
    let chosenColor = item.colorLabel || '';
    const swatches = COLOR_LABELS.map(c =>
      `<button type="button" class="label-swatch-btn${c.key === chosenColor ? ' active' : ''}" data-color="${esc(c.key)}" ${c.key ? `data-label-color="${esc(c.key)}"` : ''}><span class="label-dot"></span>${esc(c.name)}</button>`
    ).join('');
    dialog.innerHTML =
      `<h2>Media metadata · ${esc(item.name)}</h2>` +
      `<form>` +
      `<div class="mc-field-row">` +
      `<label>Scene<input name="scene" type="text" value="${esc(item.scene || '')}" placeholder="e.g. 12A"></label>` +
      `<label>Shot<input name="shot" type="text" value="${esc(item.shot || '')}" placeholder="e.g. CU"></label>` +
      `<label>Camera<input name="camera" type="text" value="${esc(item.camera || '')}" placeholder="e.g. A"></label>` +
      `</div>` +
      `<div class="mc-field-row">` +
      `<label>Take<input name="take" type="text" value="${esc(item.take || '')}" placeholder="e.g. 3"></label>` +
      `<label>Reel<input name="reel" type="text" value="${esc(item.reel || '')}" placeholder="e.g. A001"></label>` +
      `</div>` +
      `<label>Bin path<input name="bin" type="text" value="${esc(item.bin || '')}" placeholder="e.g. Scene 1/Cam A"></label>` +
      `<label>Log Note<input name="logNote" type="text" value="${esc(item.logNote || '')}" placeholder="Editorial notes…"></label>` +
      `<div><small>Color label</small><div class="label-swatch-row">${swatches}</div></div>` +
      `<footer><button type="button" data-close>Cancel</button><button class="primary" type="submit">Save metadata</button></footer>` +
      `</form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form');
    dialog.querySelectorAll('.label-swatch-btn').forEach(btn => {
      btn.onclick = () => {
        chosenColor = btn.dataset.color || '';
        dialog.querySelectorAll('.label-swatch-btn').forEach(b => b.classList.toggle('active', b === btn));
      };
    });
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.onsubmit = e => {
      e.preventDefault();
      updateMediaMetadata(mediaId, {
        scene: form.elements.scene.value,
        shot: form.elements.shot.value,
        camera: form.elements.camera.value,
        take: form.elements.take.value,
        reel: form.elements.reel.value,
        bin: form.elements.bin.value,
        logNote: form.elements.logNote.value,
        colorLabel: chosenColor,
      });
      dialog.close();
      notify?.(`Updated metadata for "${item.name}"`);
    };
    dialog.showModal();
  }

  function mountSourceMetadata(container, mediaId) {
    const project = window.Editor.get().project;
    const item = project.media.find(m => m.id === mediaId);
    if (!container || !item) return;
    const grid = document.createElement('div');
    grid.className = 'source-meta-grid';
    const colorOptions = COLOR_LABELS.map(c =>
      `<option value="${esc(c.key)}" ${c.key === (item.colorLabel || '') ? 'selected' : ''}>${esc(c.name)}</option>`
    ).join('');
    grid.innerHTML =
      `<label>Scene<input type="text" data-meta="scene" value="${esc(item.scene || '')}" placeholder="Scene"></label>` +
      `<label>Shot<input type="text" data-meta="shot" value="${esc(item.shot || '')}" placeholder="Shot"></label>` +
      `<label>Camera<input type="text" data-meta="camera" value="${esc(item.camera || '')}" placeholder="Camera"></label>` +
      `<label>Take<input type="text" data-meta="take" value="${esc(item.take || '')}" placeholder="Take"></label>` +
      `<label>Reel<input type="text" data-meta="reel" value="${esc(item.reel || '')}" placeholder="Reel"></label>` +
      `<label>Color label<select data-meta="colorLabel">${colorOptions}</select></label>` +
      `<label>Log Note<input type="text" data-meta="logNote" value="${esc(item.logNote || '')}" placeholder="Notes…"></label>`;
    grid.querySelectorAll('[data-meta]').forEach(el => {
      el.onchange = () => updateMediaMetadata(mediaId, { [el.dataset.meta]: el.value });
    });
    container.append(grid);
  }

  // ---- Bin Tree & Breadcrumb inside Media Pool ----

  function renderBinBrowser(container, state, { currentBin, onSelectBin, selectedMediaIds, notify }) {
    const project = state.project;
    const bins = allBins(project);
    const expanded = treeExpanded === null ? bins.length > 0 : treeExpanded;
    const selList = [...(selectedMediaIds || [])].filter(id => project.media.some(m => m.id === id));

    const wrap = document.createElement('div');
    wrap.className = 'bin-browser';
    wrap.id = 'bin-browser';

    const head = document.createElement('div');
    head.className = 'bin-browser-head';
    head.innerHTML =
      `<button type="button" class="bin-toggle-btn" id="bin-tree-toggle" aria-expanded="${expanded}">${expanded ? '▾' : '▸'} Bins (${bins.length})</button>` +
      `<div class="bin-action-bar">` +
      `<button type="button" class="bin-mini-btn" id="bin-create-btn" title="Create a new bin or sub-bin">+ Bin</button>` +
      `<button type="button" class="bin-mini-btn" id="bin-move-btn" title="Move selected media into a bin" ${selList.length ? '' : 'disabled'}>Move…</button>` +
      `<button type="button" class="bin-mini-btn" id="multicam-create-btn" title="Create multicam clip from 2–9 selected media items">Multicam…</button>` +
      `</div>`;
    wrap.append(head);

    // Breadcrumb when viewing a bin
    if (currentBin && currentBin !== '@root') {
      const crumbBar = document.createElement('div');
      crumbBar.className = 'bin-crumb-bar';
      const parts = normalizeBinPath(currentBin).split('/').filter(Boolean);
      const rootBtn = document.createElement('button');
      rootBtn.type = 'button';
      rootBtn.className = 'bin-crumb-btn';
      rootBtn.textContent = 'All bins';
      rootBtn.onclick = () => onSelectBin('');
      crumbBar.append(rootBtn);
      let acc = '';
      parts.forEach((part, idx) => {
        acc = acc ? acc + '/' + part : part;
        const sep = document.createElement('span');
        sep.textContent = '›';
        crumbBar.append(sep);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'bin-crumb-btn' + (idx === parts.length - 1 ? ' active' : '');
        btn.textContent = part;
        const target = acc;
        btn.onclick = () => onSelectBin(target);
        crumbBar.append(btn);
      });
      wrap.append(crumbBar);
    }

    const list = document.createElement('div');
    list.className = 'bin-tree-list';
    list.id = 'bin-tree-list';
    list.hidden = !expanded;

    const addNode = (label, value, depth, count, editable) => {
      const row = document.createElement('div');
      row.className = 'bin-node' + (currentBin === value ? ' active' : '');
      row.dataset.binPath = value;
      row.style.paddingLeft = `${4 + depth * 12}px`;

      const selectBtn = document.createElement('button');
      selectBtn.type = 'button';
      selectBtn.className = 'bin-node-select';
      selectBtn.textContent = (depth > 0 ? '└ ' : '') + label;
      selectBtn.onclick = () => onSelectBin(value);

      const countSpan = document.createElement('span');
      countSpan.className = 'bin-node-count';
      countSpan.textContent = String(count);

      row.append(selectBtn, countSpan);

      if (editable) {
        const ren = document.createElement('button');
        ren.type = 'button';
        ren.className = 'bin-node-op';
        ren.title = `Rename bin "${value}"`;
        ren.setAttribute('aria-label', `Rename bin ${value}`);
        ren.textContent = '✎';
        ren.onclick = e => {
          e.stopPropagation();
          openRenameBinDialog(value, (oldP, newP) => {
            if (currentBin === oldP || currentBin.startsWith(oldP + '/')) onSelectBin(newP);
          }, notify);
        };
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'bin-node-op';
        del.title = `Delete bin "${value}"`;
        del.setAttribute('aria-label', `Delete bin ${value}`);
        del.textContent = '×';
        del.onclick = e => {
          e.stopPropagation();
          openDeleteBinDialog(value, (delP, parentP) => {
            if (currentBin === delP || currentBin.startsWith(delP + '/')) onSelectBin(parentP);
          }, notify);
        };
        row.append(ren, del);
      }

      // Drag-and-drop media onto a bin node moves it into that bin.
      if (value !== '') {
        row.ondragover = e => {
          e.preventDefault();
          row.classList.add('drop-over');
        };
        row.ondragleave = () => row.classList.remove('drop-over');
        row.ondrop = e => {
          e.preventDefault();
          row.classList.remove('drop-over');
          const id = e.dataTransfer.getData('text/plain');
          if (!id) return;
          const liveSel = [...(selectedMediaIds || [])];
          const idsToMove = liveSel.includes(id) ? liveSel : [id];
          const dest = value === '@root' ? '' : value;
          const moved = moveMediaToBin(idsToMove, dest);
          notify?.(`Moved ${moved} item${moved === 1 ? '' : 's'} to ${dest || 'Unfiled'}`);
        };
      }
      list.append(row);
    };

    const unfiledCount = project.media.filter(m => !normalizeBinPath(m.bin || '')).length;
    addNode('All bins', '', 0, project.media.length, false);
    addNode('Unfiled', '@root', 0, unfiledCount, false);
    for (const binPath of bins) {
      const parts = binPath.split('/');
      const depth = parts.length - 1;
      const count = project.media.filter(m => mediaMatchesBin(m, binPath)).length;
      addNode(parts[parts.length - 1], binPath, depth, count, true);
    }

    wrap.append(list);
    container.prepend(wrap);

    wrap.querySelector('#bin-tree-toggle').onclick = () => {
      treeExpanded = list.hidden;
      list.hidden = !treeExpanded;
      const btn = wrap.querySelector('#bin-tree-toggle');
      btn.setAttribute('aria-expanded', String(treeExpanded));
      btn.textContent = `${treeExpanded ? '▾' : '▸'} Bins (${bins.length})`;
    };
    wrap.querySelector('#bin-create-btn').onclick = () => {
      const parentDefault = currentBin && currentBin !== '@root' ? currentBin : '';
      openCreateBinDialog(parentDefault, created => onSelectBin(created), notify);
    };
    wrap.querySelector('#bin-move-btn').onclick = () => {
      const curProj = window.Editor.get().project;
      const liveSel = [...(selectedMediaIds || [])].filter(id => curProj.media.some(m => m.id === id));
      openMoveToBinDialog(liveSel, notify);
    };
    wrap.querySelector('#multicam-create-btn').onclick = () => {
      const curProj = window.Editor.get().project;
      const liveSel = [...(selectedMediaIds || [])].filter(id => curProj.media.some(m => m.id === id));
      window.SpliceMulticam?.openCreateDialog(liveSel, notify);
    };
  }

  return {
    COLOR_LABELS,
    normalizeBinPath,
    parentBinOf,
    allBins,
    mediaMatchesBin,
    createBin,
    renameBin,
    deleteBin,
    moveMediaToBin,
    openCreateBinDialog,
    openRenameBinDialog,
    openDeleteBinDialog,
    openMoveToBinDialog,
    createSubclip,
    openSubclipDialog,
    toggleSubclipLimits,
    removeClipSubclipLimits,
    deleteMedia,
    updateMediaMetadata,
    setClipColorLabel,
    openMetadataDialog,
    mountSourceMetadata,
    renderBinBrowser,
  };
})();
