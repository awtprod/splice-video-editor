// Native Nested / Compound Sequences: create, step in/out with breadcrumbs, live edit propagation, unnest.
window.SpliceNested = (() => {
  const MAX_NEST_DEPTH = 4;
  const uid = () => crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 12);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Stack of parent sequence frames while stepped into a nested sequence.
  // Each entry: { parentProject, history, future, time, selectedId, mediaId, seqId, name, dirty, childRevision }
  let stack = [];
  let transitioning = false;

  const isNativeNested = item => !!(item && typeof item.nestedSequence === 'string' && item.nestedSequence && item.nestedProject);

  function subtreeDepth(project, seen = new Set()) {
    if (!project || !Array.isArray(project.media)) return 0;
    let best = 0;
    for (const item of project.media) {
      if (!item?.nestedProject) continue;
      const key = typeof item.nestedSequence === 'string' ? item.nestedSequence : 'p:' + item.nestedSequence;
      if (seen.has(key)) throw new Error('Circular nested sequence reference.');
      const next = new Set(seen);
      next.add(key);
      best = Math.max(best, 1 + subtreeDepth(item.nestedProject, next));
    }
    return best;
  }

  function mediaDepth(item, seen = new Set()) {
    if (!item?.nestedProject) return 0;
    const key = typeof item.nestedSequence === 'string' ? item.nestedSequence : 'p:' + item.nestedSequence;
    if (seen.has(key)) throw new Error('Circular nested sequence reference.');
    const next = new Set(seen);
    next.add(key);
    return 1 + subtreeDepth(item.nestedProject, next);
  }

  function nextSequenceName(project) {
    const existing = new Set((project?.media || []).map(m => m.name));
    for (let i = 1; i < 999; i++) {
      const candidate = `Nested Sequence ${i}`;
      if (!existing.has(candidate)) return candidate;
    }
    return `Nested Sequence ${Date.now()}`;
  }

  // Gather all media items referenced by `clips`, recursively following subclip parents and multicam angles.
  function collectReferencedMedia(project, clips) {
    const byId = new Map((project.media || []).map(m => [m.id, m]));
    const needed = new Set();
    const visit = id => {
      if (!id || needed.has(id)) return;
      const item = byId.get(id);
      if (!item) return;
      needed.add(id);
      if (item.parentMediaId) visit(item.parentMediaId);
      if (item.multicam?.angles) {
        for (const angle of item.multicam.angles) visit(angle.mediaId);
      }
    };
    for (const clip of clips) if (clip.mediaId) visit(clip.mediaId);
    return (project.media || []).filter(m => needed.has(m.id)).map(m => structuredClone(m));
  }

  function sequenceHasAudio(nestedProject) {
    const byId = new Map((nestedProject.media || []).map(m => [m.id, m]));
    for (const clip of nestedProject.clips || []) {
      if (clip.enabled === false) continue;
      if (clip.type === 'audio') return true;
      if (clip.type === 'video' && clip.embeddedAudio !== false) {
        const m = byId.get(clip.mediaId);
        if (m && !m.still && ((m.audioChannels || 0) > 0 || m.multicam)) return true;
      }
    }
    return false;
  }

  function computeProjectDuration(proj) {
    return Math.max(0, ...(proj.clips || []).map(c => (Number(c.start) || 0) + (Number(c.duration) || 0)));
  }

  function snapToFps(value, fps) {
    const f = Number(fps) > 0 ? Number(fps) : 25;
    return Math.round(value * f) / f;
  }

  function freeTrack(clips, kind, preferred, from, to, isLocked) {
    const step = kind === 'audio' ? -1 : 1;
    const candidates = [preferred];
    for (let i = 1; i <= 64; i++) {
      const t = i * step;
      if (t !== preferred) candidates.push(t);
    }
    for (const track of candidates) {
      if (isLocked && isLocked(track, kind)) continue;
      const busy = clips.some(c => {
        const cKind = c.type === 'audio' ? 'audio' : 'video';
        return cKind === kind && c.track === track && c.start < to - 1e-6 && c.start + c.duration > from + 1e-6;
      });
      if (!busy) return track;
    }
    return null;
  }

  function nestSelection(clipIds, options = {}) {
    const editor = window.Editor;
    const state = editor.get();
    const project = state.project;
    const idSet = new Set(Array.isArray(clipIds) ? clipIds : clipIds ? [clipIds] : []);
    if (!idSet.size && state.selectedId) idSet.add(state.selectedId);
    if (!idSet.size) throw new Error('Select one or more clips on the timeline to nest.');

    // Include linked partners when linked selection is active.
    if (state.linkedSelection) {
      const groups = new Set(project.clips.filter(c => idSet.has(c.id) && c.linkGroup).map(c => c.linkGroup));
      for (const clip of project.clips) if (clip.linkGroup && groups.has(clip.linkGroup)) idSet.add(clip.id);
    }
    const selected = project.clips.filter(c => idSet.has(c.id));
    if (!selected.length) throw new Error('Select one or more clips on the timeline to nest.');

    // Check locked tracks on selected clips.
    for (const clip of selected) {
      if (editor.isTrackLocked?.(clip.track, clip.type))
        throw new Error(`Unlock ${clip.type} track ${Math.abs(clip.track)} before nesting.`);
    }

    // Enforce max nesting depth <= 4 (accounting for current stack depth + any nested clips inside selection).
    const activeSeqIds = new Set(stack.map(f => f.seqId));
    let innerDepth = 0;
    for (const clip of selected) {
      const m = project.media.find(item => item.id === clip.mediaId);
      if (m?.nestedProject) {
        if (typeof m.nestedSequence === 'string' && activeSeqIds.has(m.nestedSequence))
          throw new Error('Cannot nest a sequence inside itself.');
        innerDepth = Math.max(innerDepth, mediaDepth(m, new Set(activeSeqIds)));
      }
    }
    if (stack.length + 1 + innerDepth > MAX_NEST_DEPTH) {
      throw new Error(`Maximum nesting depth is ${MAX_NEST_DEPTH} levels.`);
    }

    const name = String(options.name || nextSequenceName(project)).trim() || nextSequenceName(project);
    const fps = project.fps || 25;
    const minStart = Math.min(...selected.map(c => c.start));
    const maxEnd = Math.max(...selected.map(c => c.start + c.duration));
    const span = snapToFps(Math.max(1 / fps, maxEnd - minStart), fps);

    const childClips = selected.map(c => {
      const copy = structuredClone(c);
      copy.start = Math.max(0, snapToFps(copy.start - minStart, fps));
      delete copy.premiereIndex;
      delete copy.premiereTrack;
      return copy;
    });
    const childMedia = collectReferencedMedia(project, childClips);
    const childProject = {
      name,
      width: project.width,
      height: project.height,
      fps,
      startTimecode: project.startTimecode || 0,
      ...(project.dropFrame !== undefined ? { dropFrame: project.dropFrame } : {}),
      trackCounts: structuredClone(project.trackCounts || { video: 4, audio: 3 }),
      media: childMedia,
      clips: childClips,
      duration: span,
    };
    // A solo on a track outside the selection can silence the whole selection.
    if (project.trackMix) childProject.trackMix = structuredClone(project.trackMix);

    const hasVideo = selected.some(c => c.type === 'video' || c.type === 'title');
    const hasAudio = sequenceHasAudio(childProject);
    const seqId = 'nest-' + uid();
    const mediaId = uid();
    const nestedItem = {
      id: mediaId,
      name,
      src: '',
      type: hasVideo ? 'video' : 'audio',
      duration: span,
      fps,
      width: project.width,
      height: project.height,
      audioChannels: hasAudio ? 2 : 0,
      ...(options.bin ? { bin: options.bin } : {}),
      nestedSequence: seqId,
      nestedProject: childProject,
    };

    const remaining = project.clips.filter(c => !idSet.has(c.id));
    const prefVideo = hasVideo ? Math.min(...selected.filter(c => c.type !== 'audio').map(c => c.track)) : null;
    const prefAudio = hasAudio ? Math.max(...selected.filter(c => c.type === 'audio').map(c => c.track), -1) : null;
    const isLocked = (t, k) => editor.isTrackLocked?.(t, k);

    const videoTrack = hasVideo ? freeTrack(remaining, 'video', Math.max(1, prefVideo || 1), minStart, minStart + span, isLocked) : null;
    // The child renders the source tracks' mix. Route that sound through a unity parent track.
    const solo = Object.values(project.trackMix || {}).some(m => m.solo);
    const unavailableAudio = t => {
      const mix = project.trackMix?.[t] || {};
      if (isLocked(t, 'audio') || mix.mute || (mix.gain ?? 100) !== 100 || (mix.pan ?? 0) !== 0) return true;
      return solo && !mix.solo && project.clips.some(c => c.track === t);
    };
    const audioTrack = hasAudio ? freeTrack(remaining, 'audio', Math.min(-1, prefAudio || -1), minStart, minStart + span, unavailableAudio) : null;
    if (hasVideo && videoTrack === null) throw new Error('No free unlocked video track for the nested sequence.');
    if (hasAudio && audioTrack === null) throw new Error('The nested mix needs a free, unlocked audio track with unity gain and centered pan.');

    const createdClips = [];
    const linkGroup = hasVideo && hasAudio && audioTrack !== null ? uid() : undefined;
    if (hasVideo) {
      createdClips.push({
        id: uid(),
        mediaId,
        type: 'video',
        track: videoTrack,
        start: snapToFps(minStart, fps),
        duration: span,
        in: 0,
        title: '',
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: 'none',
        nestedSequence: seqId,
        ...(linkGroup ? { embeddedAudio: false, linkGroup } : hasAudio ? {} : { embeddedAudio: false }),
      });
    }
    if (hasAudio && audioTrack !== null) {
      createdClips.push({
        id: uid(),
        mediaId,
        type: 'audio',
        track: audioTrack,
        start: snapToFps(minStart, fps),
        duration: span,
        in: 0,
        title: '',
        volume: 100,
        transition: 'none',
        nestedSequence: seqId,
        ...(linkGroup ? { linkGroup } : {}),
      });
    }

    editor.editProject(next => {
      if (hasAudio && solo && !next.trackMix[audioTrack]?.solo)
        next.trackMix[audioTrack] = {...next.trackMix[audioTrack], solo:true};
      next.media.push(nestedItem);
      next.clips = next.clips.filter(c => !idSet.has(c.id));
      next.clips.push(...createdClips);
    });
    editor.select(createdClips[0].id);

    const liveItem = editor.get().project.media.find(m => m.id === mediaId);
    if (liveItem && window.SpliceMedia?.prepareNested) window.SpliceMedia.prepareNested(liveItem, true);
    return { mediaId, seqId, clipIds: createdClips.map(c => c.id), name };
  }

  function openNestDialog(clipIds, notify) {
    const editor = window.Editor;
    const state = editor.get();
    const ids = Array.isArray(clipIds) && clipIds.length ? clipIds : state.selectedId ? [state.selectedId] : [];
    if (!ids.length) {
      notify?.('Select one or more clips on the timeline to nest.');
      return;
    }
    const existing = document.getElementById('nest-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'nest-dialog';
    dialog.className = 'studio-dialog';
    const defaultName = nextSequenceName(state.project);
    dialog.innerHTML =
      `<h2>Nest selected clips</h2>` +
      `<form>` +
      `<label>Nested sequence name<input name="name" type="text" value="${esc(defaultName)}" required autocomplete="off" spellcheck="false"></label>` +
      `<p class="form-status" role="status"></p>` +
      `<footer><button type="button" data-close>Cancel</button><button class="primary" type="submit">Create nest</button></footer>` +
      `</form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form');
    const status = dialog.querySelector('.form-status');
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.onsubmit = e => {
      e.preventDefault();
      try {
        const res = nestSelection(ids, { name: form.elements.name.value });
        dialog.close();
        notify?.(`Created nested sequence "${res.name}"`);
      } catch (err) {
        status.textContent = err.message || 'Could not create nested sequence.';
      }
    };
    dialog.showModal();
    form.elements.name.select();
  }

  function applyChildToProject(parentProject, mediaId, childProject) {
    const next = structuredClone(parentProject);
    const item = next.media.find(m => m.id === mediaId);
    if (!item) return next;
    const cleanedChild = structuredClone(childProject);
    const childDur = Math.max(1 / (cleanedChild.fps || next.fps || 25), computeProjectDuration(cleanedChild));
    cleanedChild.duration = childDur;
    item.nestedProject = cleanedChild;
    const maxOuterEnd = Math.max(
      0,
      ...next.clips.filter(c => c.mediaId === mediaId).map(c => (Number(c.in) || 0) + (Number(c.duration) || 0) * (Number(c.speed) || 1)),
    );
    item.duration = Math.max(childDur, maxOuterEnd);
    item.audioChannels = sequenceHasAudio(cleanedChild) ? Math.max(2, item.audioChannels || 0) : 0;
    item.src = '';
    return next;
  }

  // Returns the root project with all currently open nested edits merged back up the stack.
  function rootProject() {
    const editor = window.Editor;
    if (!editor) return null;
    if (!stack.length) return editor.get().project;
    let merged = editor.get().project;
    for (let i = stack.length - 1; i >= 0; i--) {
      merged = applyChildToProject(stack[i].parentProject, stack[i].mediaId, merged);
    }
    return merged;
  }

  function renderBreadcrumb() {
    const bar = document.getElementById('nested-breadcrumb');
    if (!bar) return;
    if (!stack.length) {
      bar.hidden = true;
      bar.replaceChildren();
      return;
    }
    const editor = window.Editor;
    const currentName = editor?.get?.().project?.name || 'Nested Sequence';
    bar.hidden = false;
    const crumbs = [
      ...stack.map((frame, index) => ({ name: frame.parentProject.name || 'Main Sequence', depth: index })),
      { name: currentName, depth: stack.length },
    ];
    bar.innerHTML =
      `<button type="button" class="nested-back-btn" id="nested-back-btn" title="Return to parent sequence">← Back</button>` +
      `<nav class="nested-crumb-list" aria-label="Sequence hierarchy">` +
      crumbs.map((c, idx) => {
        const isLast = idx === crumbs.length - 1;
        return (idx > 0 ? '<span class="nested-crumb-sep" aria-hidden="true">›</span>' : '') +
          `<button type="button" class="nested-crumb${isLast ? ' current' : ''}" data-nest-depth="${c.depth}" ${isLast ? 'aria-current="page"' : ''}>${esc(c.name)}</button>`;
      }).join('') +
      `</nav>` +
      `<span class="nested-depth-pill">Depth ${stack.length}/${MAX_NEST_DEPTH}</span>`;
    bar.querySelector('#nested-back-btn').onclick = () => exit();
    bar.querySelectorAll('[data-nest-depth]').forEach(btn => {
      const d = Number(btn.dataset.nestDepth);
      if (d < stack.length) btn.onclick = () => goToDepth(d);
    });
  }

  async function enter(mediaId) {
    const editor = window.Editor;
    const state = editor.get();
    const item = state.project.media.find(m => m.id === mediaId);
    if (!isNativeNested(item)) throw new Error('This media item is not an editable nested sequence.');
    if (stack.length >= MAX_NEST_DEPTH) throw new Error(`Maximum nesting depth is ${MAX_NEST_DEPTH} levels.`);
    if (stack.some(f => f.seqId === item.nestedSequence)) throw new Error('Circular nested sequence reference.');

    const child = structuredClone(item.nestedProject);
    if (window.SpliceMedia?.restore) await window.SpliceMedia.restore(child);
    transitioning = true;
    try {
      const previous = editor.swapSequence(child, { time: 0, selectedId: child.clips[0]?.id || null });
      stack.push({
        parentProject: previous.project,
        history: previous.history,
        future: previous.future,
        time: previous.time,
        selectedId: previous.selectedId,
        mediaId,
        seqId: item.nestedSequence,
        name: item.name,
        dirty: false,
        childRevision: editor.get().revision,
      });
    } finally {
      transitioning = false;
    }
    renderBreadcrumb();
  }

  function exitOne() {
    if (!stack.length) return false;
    const editor = window.Editor;
    const frame = stack.pop();
    const childNow = structuredClone(editor.get().project);
    const wasDirty = frame.dirty || editor.get().revision !== frame.childRevision;

    transitioning = true;
    try {
      editor.swapSequence(frame.parentProject, {
        history: frame.history,
        future: frame.future,
        time: frame.time,
        selectedId: frame.selectedId,
      });
    } finally {
      transitioning = false;
    }

    if (wasDirty) {
      const cleanedChild = structuredClone(childNow);
      const childDur = Math.max(1 / (cleanedChild.fps || frame.parentProject.fps || 25), computeProjectDuration(cleanedChild));
      cleanedChild.duration = childDur;
      const childHasAudio = sequenceHasAudio(cleanedChild);
      editor.editProject(next => {
        const item = next.media.find(m => m.id === frame.mediaId);
        if (!item) return;
        item.nestedProject = cleanedChild;
        const maxOuterEnd = Math.max(
          0,
          ...next.clips.filter(c => c.mediaId === frame.mediaId).map(c => (Number(c.in) || 0) + (Number(c.duration) || 0) * (Number(c.speed) || 1)),
        );
        item.duration = Math.max(childDur, maxOuterEnd);
        item.audioChannels = childHasAudio ? Math.max(2, item.audioChannels || 0) : 0;
        item.src = '';
      });
      const updated = editor.get().project.media.find(m => m.id === frame.mediaId);
      if (updated) {
        window.SpliceMedia?.invalidateNested?.(updated);
        window.SpliceMedia?.prepareNested?.(updated, true);
      }
      if (stack.length) stack[stack.length - 1].dirty = true;
    }
    renderBreadcrumb();
    return true;
  }

  function exit() {
    return exitOne();
  }

  function goToDepth(targetDepth) {
    const target = Math.max(0, Math.min(stack.length, Number(targetDepth) || 0));
    while (stack.length > target) exitOne();
  }

  function reset() {
    stack = [];
    renderBreadcrumb();
  }

  function unnestClip(clipId) {
    const editor = window.Editor;
    const state = editor.get();
    const project = state.project;
    const primary = project.clips.find(c => c.id === (clipId || state.selectedId));
    if (!primary) throw new Error('Select a nested clip to unnest.');
    const item = project.media.find(m => m.id === primary.mediaId);
    if (!item?.nestedProject) throw new Error('Selected clip is not a nested sequence.');

    // Include linked partner if this nested clip is a V+A pair referencing the same nested media.
    const outerClips = [primary];
    if (primary.linkGroup) {
      for (const other of project.clips) {
        if (other.id !== primary.id && other.linkGroup === primary.linkGroup && other.mediaId === primary.mediaId)
          outerClips.push(other);
      }
    }
    for (const c of outerClips) {
      if (editor.isTrackLocked?.(c.track, c.type))
        throw new Error(`Unlock ${c.type} track ${Math.abs(c.track)} before unnesting.`);
    }

    const outerIds = new Set(outerClips.map(c => c.id));
    const speed = Number(primary.speed) || 1;
    const winStart = Number(primary.in) || 0;
    const winEnd = winStart + primary.duration * speed;
    const child = item.nestedProject;
    const fps = project.fps || 25;

    const existingMediaById = new Map(project.media.map(m => [m.id, m]));
    const mediaRemap = new Map();
    const addedMedia = [];
    for (const cm of child.media || []) {
      const existing = existingMediaById.get(cm.id);
      if (existing && existing.name === cm.name && existing.src === cm.src) {
        mediaRemap.set(cm.id, cm.id);
      } else if (!existing) {
        addedMedia.push(structuredClone(cm));
        mediaRemap.set(cm.id, cm.id);
      } else {
        const newId = uid();
        const copy = structuredClone(cm);
        copy.id = newId;
        addedMedia.push(copy);
        mediaRemap.set(cm.id, newId);
      }
    }
    // Remap parentMediaId and multicam angle mediaIds inside newly added media if remapped.
    for (const m of addedMedia) {
      if (m.parentMediaId && mediaRemap.has(m.parentMediaId)) m.parentMediaId = mediaRemap.get(m.parentMediaId);
      if (m.multicam?.angles) {
        for (const a of m.multicam.angles) if (mediaRemap.has(a.mediaId)) a.mediaId = mediaRemap.get(a.mediaId);
      }
    }

    const linkRemap = new Map();
    const expanded = [];
    const workingClips = project.clips.filter(c => !outerIds.has(c.id));
    const isLocked = (t, k) => editor.isTrackLocked?.(t, k);

    for (const cc of child.clips || []) {
      const ovStart = Math.max(cc.start, winStart);
      const ovEnd = Math.min(cc.start + cc.duration, winEnd);
      if (ovEnd <= ovStart + 1e-6) continue;
      const cSpeed = Number(cc.speed) || 1;
      const trimHead = ovStart - cc.start;
      const newDuration = snapToFps((ovEnd - ovStart) / speed, fps);
      if (newDuration < 1 / fps - 1e-6) continue;
      const offset = primary.reverse ? winEnd - ovEnd : ovStart - winStart;
      const newStart = snapToFps(primary.start + offset / speed, fps);
      const out = structuredClone(cc);
      out.id = uid();
      if (out.mediaId && mediaRemap.has(out.mediaId)) out.mediaId = mediaRemap.get(out.mediaId);
      out.start = newStart;
      out.duration = newDuration;
      if (!out.reverse) out.in = (Number(out.in) || 0) + trimHead * cSpeed;
      else out.in = (Number(out.in) || 0) + (cc.start + cc.duration - ovEnd) * cSpeed;
      if (primary.reverse) out.reverse = !cc.reverse;
      if (speed !== 1) out.speed = cSpeed * speed;
      if (primary.enabled === false) out.enabled = false;
      delete out.premiereIndex;
      delete out.premiereTrack;
      if (out.linkGroup) {
        if (!linkRemap.has(out.linkGroup)) linkRemap.set(out.linkGroup, uid());
        out.linkGroup = linkRemap.get(out.linkGroup);
      }
      const laneKind = out.type === 'audio' ? 'audio' : 'video';
      const chosenTrack = freeTrack([...workingClips, ...expanded], laneKind, out.track, out.start, out.start + out.duration, isLocked);
      if (chosenTrack === null) throw new Error(`No free unlocked ${laneKind} track to unnest into.`);
      out.track = chosenTrack;
      expanded.push(out);
    }

    if (!expanded.length) throw new Error('This nested clip has no content within its trimmed range.');

    editor.editProject(next => {
      if (addedMedia.length) next.media.push(...addedMedia);
      next.clips = next.clips.filter(c => !outerIds.has(c.id));
      next.clips.push(...expanded);
    });
    editor.select(expanded[0].id);
    return expanded;
  }

  // Subscribe to Editor so edits inside a nested sequence mark the top stack frame dirty and keep breadcrumb updated.
  function init() {
    if (!window.Editor) return;
    window.Editor.subscribe(state => {
      if (!transitioning && stack.length) {
        const top = stack[stack.length - 1];
        if (state.revision !== top.childRevision) top.dirty = true;
      }
      renderBreadcrumb();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();

  return {
    MAX_NEST_DEPTH,
    isNativeNested,
    nestSelection,
    openNestDialog,
    enter,
    exit,
    goToDepth,
    unnestClip,
    rootProject,
    reset,
    isNested: () => stack.length > 0,
    depth: () => stack.length,
    breadcrumbs: () => [
      ...stack.map((f, i) => ({ name: f.parentProject.name, depth: i })),
      ...(stack.length ? [{ name: window.Editor?.get?.().project?.name || '', depth: stack.length }] : []),
    ],
    renderBreadcrumb,
  };
})();
