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
    for (const item of project.media) best = Math.max(best, mediaDepth(item, seen));
    return best;
  }

  function mediaDepth(item, seen = new Set()) {
    if (!item?.nestedProject) return 0;
    return 1 + subtreeDepth(item.nestedProject, window.Editor.nestedAncestors(item, seen));
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

  // Track automation points between `from` and `from + span` on the child's clock, with the
  // curve's values at both ends (a segment cut by an end keeps its easing).
  function automationWindow(points, from, span) {
    const ordered = [...points].sort((a, b) => a.time - b.time);
    const at = t => window.SpliceAudioDSP.curveValue(ordered, t, ordered[0].value);
    const easeAt = t => !!ordered.filter(p => p.time <= t).pop()?.ease;
    const point = (time, value, ease) => ({time, value, ...(ease ? {ease:true} : {})});
    const inside = ordered.filter(p => p.time > from + 1e-9 && p.time < from + span - 1e-9)
      .map(p => point(p.time - from, p.value, p.ease));
    return [point(0, at(from), easeAt(from)), ...inside, point(span, at(from + span), false)];
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
    // Track automation is on the timeline clock: move it onto the child's clock with the clips.
    for (const mix of Object.values(childProject.trackMix || {}))
      for (const [key, points] of Object.entries(mix?.keyframes || {}))
        if (Array.isArray(points) && points.length) mix.keyframes[key] = automationWindow(points, minStart, span);
    if (project.trackState) childProject.trackState = structuredClone(project.trackState);

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
    const pictures = selected.filter(c => c.type !== 'audio');
    const visiblePictures = pictures.filter(c => !editor.isTrackHidden(c.track, c.type));
    const prefVideo = Math.min(...(visiblePictures.length ? visiblePictures : pictures).map(c => c.track));
    const prefAudio = hasAudio ? Math.max(...selected.filter(c => c.type === 'audio').map(c => c.track), -1) : null;
    const isLocked = (t, k) => editor.isTrackLocked?.(t, k);

    const unavailableVideo = t => isLocked(t, 'video') || (visiblePictures.length > 0 && editor.isTrackHidden(t, 'video'));
    const videoTrack = hasVideo ? freeTrack(remaining, 'video', Math.max(1, prefVideo), minStart, minStart + span, unavailableVideo) : null;
    // The child renders the source tracks' mix. Route that sound through a unity parent track.
    const solo = Object.values(project.trackMix || {}).some(m => m.solo);
    const unavailableAudio = t => {
      const mix = project.trackMix?.[t] || {};
      const hasFx = mix.fx && Object.values(mix.fx).some(stage => stage && stage.enabled !== false);
      const hasKeys = mix.keyframes && Object.values(mix.keyframes).some(points => Array.isArray(points) && points.length);
      if (isLocked(t, 'audio') || mix.mute || (mix.gain ?? 100) !== 100 || (mix.pan ?? 0) !== 0 ||
          mix.outputStages?.length || hasFx || hasKeys || mix.channels === 'mono' ||
          (mix.route && mix.route !== 'front')) return true;
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
      editor.swapSequence(child, {
        time: 0, selectedId: child.clips[0]?.id || null,
        beforeNotify(previous, revision) {
          stack.push({
            parentProject: previous.project, history: previous.history, future: previous.future,
            time: previous.time, selectedId: previous.selectedId, mediaId,
            seqId: item.nestedSequence, name: item.name, dirty: false, childRevision: revision,
          });
        },
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

  function pictureAdjustments(clip) {
    const names = [];
    for (const [key, value] of Object.entries(clip.transform || {}))
      if (value !== (key === 'scale' ? 100 : 0)) names.push(key);
    const color = clip.color || {};
    const gradeBypass = !!color.grade?.bypass;
    if (gradeBypass) {
      // A bypassed grade renders nothing but is still the editor's grade: carry it, never drop it.
      const {grade, ...rest} = color;
      if (Object.keys(grade).some(key => key !== 'bypass') || Object.entries(rest).some(([key, value]) =>
          !(key === 'lutIntensity' || key === 'lutName' || (key === 'lut' && !value)) &&
          value !== (['brightness','contrast','saturation'].includes(key) ? 100 : 0)))
        names.push('bypassed grade');
    } else {
      for (const [key, value] of Object.entries(color)) {
        if (key === 'lutIntensity' || key === 'lutName' || (key === 'lut' && !value)) continue;
        if (key === 'grade') {
          if (value && (window.SpliceGrade ? !window.SpliceGrade.prepare({grade: value}).identity : Object.keys(value).length))
            names.push('grade');
          continue;
        }
        if (value !== (['brightness','contrast','saturation'].includes(key) ? 100 : 0)) names.push(key);
      }
    }
    if ((clip.opacity ?? 100) !== 100) names.push('opacity');
    if (clip.transition && clip.transition !== 'none') names.push(clip.transition);
    if (clip.pictureDip) names.push('picture dip');
    for (const [key, points] of Object.entries(clip.keyframes || {}))
      if (key !== 'volume' && points.length) names.push('animated ' + key);
    // Clip effects, bypassed or switched off ones included (effects.js describe).
    names.push(...(window.SpliceEffects?.describe(clip.effects) || []));
    return names;
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
    // Outer audio finishing (clip effects and channel mapping, the parent track's effects,
    // automation, channel mode and surround routing) acts on the nested mix as a whole and has
    // no per-child equivalent on the unnested tracks. Keep it on the nested clip.
    // A bypassed (disabled) stage still holds the editor's settings: it stays on the nest too.
    const enabledFx = fx => !!fx && Object.values(fx).some(stage => stage &&
      (!Array.isArray(stage.bands) || stage.bands.length));
    const finishing = new Set();
    for (const c of outerClips) {
      if (c.type !== 'audio' && c.embeddedAudio === false) continue;
      const mix = project.trackMix?.[c.track] || {};
      if (enabledFx(c.audioFx)) finishing.add('clip audio effects');
      if (c.channelMap && c.channelMap !== 'stereo') finishing.add('clip channel mapping');
      if (c.type !== 'audio') continue;
      if (enabledFx(mix.fx)) finishing.add('track audio effects');
      if (mix.keyframes && Object.values(mix.keyframes).some(points => Array.isArray(points) && points.length))
        finishing.add('track automation');
      if (mix.channels === 'mono') finishing.add('mono track');
      if (mix.route && mix.route !== 'front') finishing.add(mix.route + ' track routing');
    }
    if (finishing.size)
      throw new Error('Cannot unnest losslessly: outer ' + [...finishing].join(', ') + ' must remain on the nested mix.');
    // A speed ramp or stabilization acts on the nest's timing or motion as a whole: no per-child copy keeps it.
    const whole = outerClips.flatMap(c => (window.SpliceEffects?.describe(c.effects, c.keyframes) || [])
      .filter(name => /^(speed ramp|stabilization)/.test(name)));
    if (whole.length)
      throw new Error('Cannot unnest losslessly: outer ' + [...new Set(whole)].join(', ') + ' must remain on the nested composite.');

    const outerIds = new Set(outerClips.map(c => c.id));
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
    const addedMixes = {};
    const pictureOuter = outerClips.find(c => c.type === 'video');
    const layers = [...new Set(child.clips.filter(c => c.type !== 'audio').map(c => c.track))].sort((a,b) => a-b);
    // Insert child layers at the nest's position. Keep an extra parent lane if its
    // visibility differs, so other edits on that lane keep their settings.
    const slot = pictureOuter?.track;
    const hiddenLayer = track => !!pictureOuter && (editor.isTrackHidden(slot, 'video') ||
      editor.isTrackHidden(track, 'video', child));
    const reuseSlot = pictureOuter && layers.length && hiddenLayer(layers[0]) === editor.isTrackHidden(slot, 'video');
    const inserted = pictureOuter ? Math.max(0, layers.length - (reuseSlot ? 1 : 0)) : 0;
    const parentLane = track => inserted && track > slot ? track + inserted : track;
    const childLane = track => slot + layers.indexOf(track) + (reuseSlot ? 0 : 1);
    const hiddenTracks = layers.filter(hiddenLayer).map(childLane);
    const shifted = workingClips.map(c => c.type === 'audio' ? c : {...c, track:parentLane(c.track)});
    if (shifted.some(c => c.track > 64) || (pictureOuter && slot + inserted > 64))
      throw new Error('Unnesting would exceed the 64 video track limit.');
    const childSolo = Object.values(child.trackMix || {}).some(m => m.solo);
    const parentSolo = Object.values(project.trackMix || {}).some(m => m.solo);
    function childMix(track, outerSound) {
      const mix = child.trackMix?.[track] || {};
      const outerMix = project.trackMix?.[outerSound.track] || {};
      const gain = (mix.gain ?? 100) * (outerMix.gain ?? 100) / 100;
      const stages = [...(mix.outputStages || [])];
      if (outerSound.volume != null && outerSound.volume !== 100) stages.push({gain:outerSound.volume, pan:0});
      // A second stereo pan cannot be represented by adding the pan angles. A gain
      // above the fader's 200% limit likewise needs its original second stage.
      if (outerMix.pan || gain > 200) stages.push({gain:gain > 200 ? outerMix.gain : 100, pan:outerMix.pan || 0});
      stages.push(...(outerMix.outputStages || []));
      // Child track automation runs on the child's clock: bring it onto the parent's.
      let keyframes = mix.keyframes;
      if (keyframes && Object.values(keyframes).some(points => Array.isArray(points) && points.length)) {
        if (outerSound.reverse || (Number(outerSound.speed) || 1) !== 1)
          throw new Error('Cannot unnest losslessly: child track automation on a retimed nested clip must remain on the nested composite.');
        const from = Number(outerSound.in) || 0;
        keyframes = Object.fromEntries(Object.entries(keyframes).filter(([, points]) => points?.length).map(([key, points]) =>
          [key, automationWindow(points, from, outerSound.duration).map(p => ({...p, time:snapToFps(p.time + outerSound.start, 48000)}))]));
      }
      // Solo is local to the child sequence. Carry its audible result onto the parent's buses.
      return {...mix, ...(keyframes ? {keyframes} : {}), gain:gain > 200 ? mix.gain : gain, ...(stages.length ? {outputStages:stages} : {}),
        mute:!!(mix.mute || (childSolo && !mix.solo) || outerMix.mute || (parentSolo && !outerMix.solo)), solo:!!outerMix.solo};
    }
    function placeSound(sound, preferred, clips, mix) {
      sound.track = editor.audioTrackForMix(sound, preferred, clips, mix, addedMixes);
      if (sound.track === null) throw new Error('Unnesting needs a free, unlocked audio track that preserves the child mix.');
    }

    function expandChild(cc, outer, picture, sound) {
      const speed = Number(outer.speed) || 1;
      const winStart = Number(outer.in) || 0, winEnd = winStart + outer.duration * speed;
      const ovStart = Math.max(cc.start, winStart);
      const ovEnd = Math.min(cc.start + cc.duration, winEnd);
      if (ovEnd <= ovStart + 1e-6) return;
      const cSpeed = Number(cc.speed) || 1;
      const trimHead = ovStart - cc.start;
      const newDuration = snapToFps((ovEnd - ovStart) / speed, fps);
      if (newDuration < 1 / fps - 1e-6) return;
      const offset = outer.reverse ? winEnd - ovEnd : ovStart - winStart;
      const newStart = snapToFps(outer.start + offset / speed, fps);
      const out = structuredClone(cc);
      out.id = uid();
      if (out.mediaId && mediaRemap.has(out.mediaId)) out.mediaId = mediaRemap.get(out.mediaId);
      out.start = newStart;
      out.duration = newDuration;
      if (!out.reverse) out.in = (Number(out.in) || 0) + trimHead * cSpeed;
      else out.in = (Number(out.in) || 0) + (cc.start + cc.duration - ovEnd) * cSpeed;
      if (outer.reverse || trimHead || ovEnd < cc.start + cc.duration || speed !== 1) {
        const tail = ovEnd - cc.start;
        const keys = cc.keyframes || {};
        const fade = cc.transition === 'fade' ? Math.min(cc.duration,
          (cc.transitionDuration ?? .5) - (cc.transitionBefore || 0)) : 0;
        if (cc.pictureDip) {
          out.pictureDip.origin += out.pictureDip.rate*(outer.reverse ? tail : trimHead);
          out.pictureDip.rate *= outer.reverse ? -speed : speed;
        } else if (fade > 0) {
          out.transition = 'none';
          delete out.transitionBefore;
          out.pictureDip = {duration:fade, origin:outer.reverse ? tail : trimHead,
            rate:outer.reverse ? -speed : speed};
        }
        if (cc.fadeIn || cc.fadeOut || cc.fadeClock) {
          const clock = cc.fadeClock || {origin:0, rate:1, duration:cc.duration};
          out.fadeClock = {duration:clock.duration,
            origin:clock.origin + clock.rate*(outer.reverse ? tail : trimHead),
            rate:clock.rate*(outer.reverse ? -speed : speed)};
        }
        if (Object.keys(keys).length) {
          out.keyframes = {};
          for (const [key,points] of Object.entries(keys)) {
            const step = outer.reverse ? (key === 'volume' ? 1/48000 : 1/fps) : 0;
            const curve = editor.clipKeyframeWindow({[key]:points}, trimHead-step, tail-step)[key];
            if (!curve) continue;
            if (outer.reverse) curve.reverse();
            out.keyframes[key] = curve.map(p => ({time:Math.max(0, (outer.reverse ? tail-trimHead-p.time : p.time)/speed), value:p.value}));
          }
        }
      }
      if (outer.reverse) out.reverse = !cc.reverse;
      if (speed !== 1) out.speed = cSpeed * speed;
      if (outer.enabled === false || outer.disabled === true) out.enabled = false;
      delete out.premiereIndex;
      delete out.premiereTrack;
      if (out.linkGroup) {
        if (!linkRemap.has(out.linkGroup)) linkRemap.set(out.linkGroup, uid());
        out.linkGroup = linkRemap.get(out.linkGroup);
      }
      const occupied = [...shifted, ...expanded];
      const soundTrack = sound && editor.separateMulticamSound(cc, child) ? -64 : cc.track;
      if (!picture) {
        out.type = 'audio';
        delete out.embeddedAudio;
        placeSound(out, cc.type === 'audio' ? cc.track : -1, occupied, childMix(soundTrack, sound));
      } else {
        out.track = childLane(cc.track);
        if (pictureAdjustments(outer).length) {
          for (const field of ['transform','color','opacity','effects'])
            if (outer[field] != null) out[field] = structuredClone(outer[field]);
          const keys = Object.fromEntries(Object.entries(outer.keyframes || {}).filter(([key]) => key !== 'volume'));
          const from = newStart - outer.start;
          if (Object.keys(keys).length) out.keyframes = {...out.keyframes,
            ...editor.clipKeyframeWindow(keys, from, from + newDuration)};
        }
        if (out.type === 'video' && !sound) out.embeddedAudio = false;
        if (sound) {
          const mix = childMix(soundTrack, sound);
          const bus = soundTrack === -64 ? -64 : out.track;
          if (bus > slot && bus <= slot + inserted && !addedMixes[bus]) addedMixes[bus] = mix;
          if (!editor.reserveTrackMix(bus, mix, occupied, addedMixes)) {
            // Keep the picture in place and give its sound a compatible audio lane.
            out.linkGroup ||= uid();
            const sound = {...out, type:'audio', id:uid()};
            placeSound(sound, -1, occupied, mix);
            out.embeddedAudio = false;
            expanded.push(out, sound);
            return;
          }
        }
      }
      expanded.push(out);
    }
    const childMedia = new Map(child.media.map(m => [m.id,m]));
    function expandWindow(picture, sound) {
      const adjustments = picture ? pictureAdjustments(picture) : [];
      if (adjustments.length) {
        const start = picture.in || 0, end = start + picture.duration * (picture.speed || 1);
        const pictures = child.clips.filter(c => c.type !== 'audio' && c.start < end && c.start+c.duration > start)
          .sort((a,b) => a.start-b.start);
        // Only distribute a composite effect over an uninterrupted run of neutral pictures.
        let cursor = start;
        const neutral = pictures.every(c => {
          const media = childMedia.get(c.mediaId);
          const contiguous = Math.abs(Math.max(start,c.start)-cursor) < 1e-6;
          cursor = Math.min(end,c.start+c.duration);
          return contiguous && c.type === 'video' && c.enabled !== false && !c.disabled &&
            !editor.isTrackHidden(c.track,'video',child) && !pictureAdjustments(c).length &&
            media && !media.hdr && !media.multicam && !media.nativeGenerator &&
            media.width * child.height === media.height * child.width;
        });
        if (!neutral || cursor < end-1e-6 || child.width !== project.width || child.height !== project.height ||
            picture.pictureDip || (picture.transition && picture.transition !== 'none'))
          throw new Error('Cannot unnest losslessly: outer ' + adjustments.join(', ') +
            ' must remain on the nested composite.');
      }
      for (const cc of child.clips || []) {
        const showPicture = picture && cc.type !== 'audio';
        const playSound = sound && (cc.type === 'audio' || (cc.type === 'video' && cc.embeddedAudio !== false &&
          editor.hasSound(childMedia.get(cc.mediaId), child)));
        if (showPicture || playSound) expandChild(cc, showPicture ? picture : sound, showPicture, playSound && sound);
      }
    }
    const sounds = new Set(outerClips.filter(c => c.type === 'audio'));
    for (const picture of outerClips.filter(c => c.type === 'video')) {
      // Keep the original embedded clips for coextensive V+A pairs. Independent cuts
      // (including a J- or L-cut) must use the window and clock of their own component.
      const paired = picture.embeddedAudio === false && [...sounds].find(c => c.start === picture.start &&
        c.duration === picture.duration && c.in === picture.in && (c.speed || 1) === (picture.speed || 1) &&
        !!c.reverse === !!picture.reverse && (c.enabled !== false) === (picture.enabled !== false));
      if (paired) sounds.delete(paired);
      expandWindow(picture, paired || (picture.embeddedAudio !== false && picture));
    }
    for (const sound of sounds) expandWindow(null, sound);

    const last = outerClips.reduce((a,b) => a.start+a.duration >= b.start+b.duration ? a : b);
    const end = last.start + last.duration;
    const delivered = [...shifted, ...expanded].reduce((out,c) => Math.max(out, c.start+c.duration), last.start);
    if (end - delivered > .5/fps) {
      // A disabled graphic needs no source footage, even for a completely empty child.
      expanded.push({id:uid(), type:'title', title:'', track:pictureOuter?.track || 1,
        start:delivered, duration:end-delivered, in:0, enabled:false});
    }

    editor.editProject(next => {
      if (inserted) {
        for (const c of next.clips) if (c.type !== 'audio') c.track = parentLane(c.track);
        for (const prop of ['trackState', 'trackMix']) {
          const entries = Object.entries(next[prop] || {}).map(([key,value]) => {
            const parts = key.split(':');
            const track = parentLane(Number(parts.pop()));
            if (track > 64) throw new Error('Unnesting would exceed the 64 video track limit.');
            return [[...parts,track].join(':'),value];
          });
          next[prop] = Object.fromEntries(entries);
        }
        if (next.trackCounts) next.trackCounts.video = Math.min(64, Math.max(slot + inserted,
          parentLane(next.trackCounts.video)));
      }
      for (const track of hiddenTracks) {
        next.trackState ||= {};
        const key = 'video:'+track;
        next.trackState[key] = {...next.trackState[key], hidden:true};
      }
      if (Object.keys(addedMixes).length) next.trackMix = {...next.trackMix, ...addedMixes};
      if (addedMedia.length) next.media.push(...addedMedia);
      next.clips = next.clips.filter(c => !outerIds.has(c.id));
      next.clips.push(...expanded);
    });
    editor.select(expanded[0]?.id || null);
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
