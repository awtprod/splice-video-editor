// Multicam Editing: create (Start / Source Timecode / Marker sync), live 2x2 / 3x3 monitor & switching, flatten.
window.SpliceMulticam = (() => {
  const uid = () => crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 12);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  let viewEnabled = false;
  let activeGridMediaId = null;
  let tileVideos = []; // [{ index, tile, video, angle, resolvedItem, src }]
  let lastSyncStamp = 0;

  function isMulticamItem(item) {
    return !!(item && item.multicam && Array.isArray(item.multicam.angles) && item.multicam.angles.length > 0);
  }

  function parseTimecodeSeconds(tc, fps) {
    const rate = Number(fps) > 0 ? Number(fps) : 25;
    if (typeof tc === 'number' && Number.isFinite(tc) && tc >= 0) return tc / rate;
    if (typeof tc !== 'string') return null;
    const trimmed = tc.trim();
    if (!trimmed) return null;
    const match = trimmed.match(/^(\d{1,2})[:;](\d{2})[:;](\d{2})[:;](\d{2})$/);
    if (!match) return null;
    const [, hh, mm, ss, ff] = match.map(Number);
    const nom = Math.round(rate);
    if (mm >= 60 || ss >= 60 || ff >= nom) return null;
    const drop = trimmed.includes(';') && (Math.abs(rate - 29.97) < 0.05 || Math.abs(rate - 59.94) < 0.05);
    const dropPerMin = Math.abs(rate - 59.94) < 0.05 ? 4 : 2;
    const totalMin = hh * 60 + mm;
    let frames = ((hh * 3600) + (mm * 60) + ss) * nom + ff;
    if (drop) frames -= dropPerMin * (totalMin - Math.floor(totalMin / 10));
    return Math.max(0, frames) / rate;
  }

  function findFirstMarkerSeconds(item, project) {
    if (Array.isArray(item.markers) && item.markers.length) {
      const valid = item.markers
        .filter(m => m && Number.isFinite(Number(m.time)) && Number(m.time) >= 0)
        .sort((a, b) => Number(a.time) - Number(b.time));
      if (valid.length) return Number(valid[0].time);
    }
    // Fallback: check timeline markers falling inside a clip using this media item.
    const markers = (project?.markers || [])
      .filter(m => m && Number.isFinite(Number(m.time)))
      .sort((a, b) => Number(a.time) - Number(b.time));
    if (!markers.length) return null;
    for (const clip of project?.clips || []) {
      if (clip.mediaId !== item.id) continue;
      for (const m of markers) {
        if (m.time >= clip.start - 1e-6 && m.time <= clip.start + clip.duration + 1e-6) {
          const speed = Number(clip.speed) || 1;
          return Math.max(0, (Number(clip.in) || 0) + (m.time - clip.start) * speed);
        }
      }
    }
    return null;
  }

  function computeAngles(items, syncMethod, project) {
    if (!Array.isArray(items) || items.length < 2 || items.length > 9) {
      return { ok: false, error: 'Select between 2 and 9 media items to create a multicam clip.', angles: [] };
    }
    for (const item of items) {
      if (!item) return { ok: false, error: 'One of the selected media items is missing.', angles: [] };
      if (isMulticamItem(item)) return { ok: false, error: `"${item.name}" is already a multicam clip.` , angles: [] };
    }
    const fps = project?.fps || 25;
    if (syncMethod === 'timecode') {
      const parsed = [];
      for (const item of items) {
        const raw = item.timecode ?? item.sourceStartTimecode;
        const sec = parseTimecodeSeconds(raw, item.fps || fps);
        if (sec === null) {
          return {
            ok: false,
            error: `"${item.name}" has no valid source timecode. Set Start TC or choose Start / Marker sync.`,
            angles: [],
          };
        }
        parsed.push({ item, tcSec: sec, tcLabel: typeof raw === 'string' ? raw : window.Editor.formatTimecode(sec, item.fps || fps) });
      }
      const minTc = Math.min(...parsed.map(p => p.tcSec));
      const angles = parsed.map(p => {
        const offset = Math.round((p.tcSec - minTc) * fps) / fps;
        const dur = Number(p.item.duration) || 5;
        return {
          mediaId: p.item.id,
          name: p.item.name,
          offset,
          in: 0,
          duration: dur,
          syncLabel: p.tcLabel,
        };
      });
      return { ok: true, angles };
    }
    if (syncMethod === 'marker') {
      const parsed = [];
      for (const item of items) {
        const mSec = findFirstMarkerSeconds(item, project);
        if (mSec === null) {
          return {
            ok: false,
            error: `"${item.name}" has no marker to sync on. Add a marker or choose Start / Timecode sync.`,
            angles: [],
          };
        }
        parsed.push({ item, markerSec: mSec });
      }
      const maxMarker = Math.max(...parsed.map(p => p.markerSec));
      const angles = parsed.map(p => {
        const offset = Math.round((maxMarker - p.markerSec) * fps) / fps;
        const dur = Number(p.item.duration) || 5;
        return {
          mediaId: p.item.id,
          name: p.item.name,
          offset,
          in: 0,
          duration: dur,
          syncLabel: `Marker @ ${window.Editor.formatTimecode(p.markerSec, p.item.fps || fps)}`,
        };
      });
      return { ok: true, angles };
    }
    // Default: 'start' (In points / Start, 0-aligned)
    const angles = items.map(item => {
      const inPt = Number.isFinite(item.sourceMarks?.in) ? clamp(item.sourceMarks.in, 0, Math.max(0, (item.duration || 0) - 1 / fps)) : 0;
      const outPt = Number.isFinite(item.sourceMarks?.out) ? clamp(item.sourceMarks.out, inPt + 1 / fps, item.duration || inPt + 1) : (item.duration || 5);
      const dur = Math.max(1 / fps, outPt - inPt);
      return {
        mediaId: item.id,
        name: item.name,
        offset: 0,
        in: inPt,
        duration: dur,
        syncLabel: inPt > 0 ? `In ${window.Editor.formatTimecode(inPt, item.fps || fps)}` : 'Start (00:00:00:00)',
      };
    });
    return { ok: true, angles };
  }

  function nextMulticamName(project) {
    const existing = new Set((project?.media || []).map(m => m.name));
    for (let i = 1; i < 999; i++) {
      const candidate = `Multicam Clip ${i}`;
      if (!existing.has(candidate)) return candidate;
    }
    return `Multicam Clip ${Date.now()}`;
  }

  function createMulticam({ name, angleMediaIds, syncMethod = 'start', audioMode = 'camera1', audioAngle = 0, bin = '' }) {
    const editor = window.Editor;
    const project = editor.get().project;
    const byId = new Map(project.media.map(m => [m.id, m]));
    const items = (angleMediaIds || []).map(id => byId.get(id)).filter(Boolean);
    const computed = computeAngles(items, syncMethod, project);
    if (!computed.ok) throw new Error(computed.error);
    if (!['camera1', 'follow', 'all', 'fixed'].includes(audioMode)) throw new Error('Invalid multicam audio mode.');
    const safeAudioAngle = Number.isInteger(audioAngle) && audioAngle >= 0 && audioAngle < computed.angles.length ? audioAngle : 0;

    const totalDuration = Math.max(...computed.angles.map(a => a.offset + a.duration));
    const firstVideo = items.find(m => m.type === 'video') || items[0];
    const hasAudio = items.some(m => m.type === 'audio' || (!m.still && (m.audioChannels || 0) > 0));
    const mcId = uid();
    const mcName = String(name || nextMulticamName(project)).trim() || nextMulticamName(project);
    const mcItem = {
      id: mcId,
      name: mcName,
      src: firstVideo?.src || '',
      type: firstVideo.type === 'audio' ? 'audio' : 'video',
      duration: totalDuration,
      fps: firstVideo.fps || project.fps || 25,
      width: firstVideo.width || project.width || 1920,
      height: firstVideo.height || project.height || 1080,
      audioChannels: hasAudio ? 2 : 0,
      ...(bin ? { bin } : {}),
      multicam: {
        syncMethod,
        audioMode,
        ...(audioMode === 'fixed' ? { audioAngle: safeAudioAngle } : {}),
        angles: computed.angles.map(a => ({
          mediaId: a.mediaId,
          name: a.name,
          offset: a.offset,
          ...(a.in > 0 ? { in: a.in } : {}),
          duration: a.duration,
        })),
      },
    };
    editor.editProject(next => {
      next.media.push(mcItem);
    });
    return mcItem;
  }

  function openCreateDialog(mediaIds, notify) {
    const editor = window.Editor;
    const project = editor.get().project;
    let ids = Array.isArray(mediaIds) ? [...new Set(mediaIds)] : [];
    // If fewer than 2 pool items passed, try selected timeline clips or all non-multicam media when 2-9 exist.
    if (ids.length < 2) {
      const eligible = project.media.filter(m => !isMulticamItem(m));
      if (eligible.length >= 2 && eligible.length <= 9) ids = eligible.map(m => m.id);
    }
    const items = ids.map(id => project.media.find(m => m.id === id)).filter(Boolean);
    if (items.length < 2 || items.length > 9) {
      notify?.('Select between 2 and 9 media items (Ctrl/Cmd+click in Media Pool) to create a multicam clip.');
      return;
    }

    const existing = document.getElementById('multicam-create-dialog');
    if (existing) existing.remove();
    const dialog = document.createElement('dialog');
    dialog.id = 'multicam-create-dialog';
    dialog.className = 'studio-dialog mc-dialog';
    const defaultName = nextMulticamName(project);
    const angleOptions = items.map((m, i) => `<option value="${i}">Angle ${i + 1}: ${esc(m.name)}</option>`).join('');
    dialog.innerHTML =
      `<h2>Create multicam clip</h2>` +
      `<form>` +
      `<label>Multicam clip name<input name="name" type="text" value="${esc(defaultName)}" required autocomplete="off"></label>` +
      `<div class="mc-field-row">` +
      `<label>Sync method<select name="syncMethod"><option value="start">In points / Start</option><option value="timecode">Source Timecode</option><option value="marker">Marker</option></select></label>` +
      `<label>Audio mode<select name="audioMode"><option value="camera1">Camera 1</option><option value="follow">Follow active angle</option><option value="all">All angles</option><option value="fixed">Specific angle</option></select></label>` +
      `</div>` +
      `<label class="mc-fixed-row" hidden>Audio angle<select name="audioAngle">${angleOptions}</select></label>` +
      `<div class="mc-preview-wrap"></div>` +
      `<p class="form-status" role="status"></p>` +
      `<footer><button type="button" data-close>Cancel</button><button class="primary" type="submit">Create multicam</button></footer>` +
      `</form>`;
    document.body.append(dialog);

    const form = dialog.querySelector('form');
    const previewWrap = dialog.querySelector('.mc-preview-wrap');
    const fixedRow = dialog.querySelector('.mc-fixed-row');
    const status = dialog.querySelector('.form-status');
    const submitBtn = dialog.querySelector('button[type="submit"]');

    function updatePreview() {
      const syncMethod = form.elements.syncMethod.value;
      const audioMode = form.elements.audioMode.value;
      fixedRow.hidden = audioMode !== 'fixed';
      const result = computeAngles(items, syncMethod, project);
      if (!result.ok) {
        previewWrap.innerHTML = `<p class="mc-offset-warn">${esc(result.error)}</p>`;
        submitBtn.disabled = true;
        return;
      }
      submitBtn.disabled = false;
      const rows = result.angles.map((a, idx) =>
        `<tr><td>${idx + 1}</td><td>${esc(a.name)}</td><td>${esc(a.syncLabel)}</td><td>+${a.offset.toFixed(2)}s</td><td>${a.duration.toFixed(2)}s</td></tr>`
      ).join('');
      previewWrap.innerHTML =
        `<table class="mc-angle-table"><thead><tr><th>#</th><th>Angle</th><th>Sync point</th><th>Offset</th><th>Duration</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    form.elements.syncMethod.onchange = updatePreview;
    form.elements.audioMode.onchange = updatePreview;
    dialog.querySelector('[data-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    form.onsubmit = e => {
      e.preventDefault();
      status.textContent = '';
      try {
        const mc = createMulticam({
          name: form.elements.name.value,
          angleMediaIds: items.map(i => i.id),
          syncMethod: form.elements.syncMethod.value,
          audioMode: form.elements.audioMode.value,
          audioAngle: Number(form.elements.audioAngle.value) || 0,
        });
        dialog.close();
        notify?.(`Created multicam clip "${mc.name}" (${mc.multicam.angles.length} angles)`);
      } catch (err) {
        status.textContent = err.message || 'Could not create multicam clip.';
      }
    };
    updatePreview();
    dialog.showModal();
    form.elements.name.select();
  }

  // Locate the active multicam video clip under the playhead (or selected if playhead sits inside it).
  function activeMulticamClip(state = window.Editor?.get?.()) {
    if (!state?.project) return null;
    const { project, time, selected } = state;
    const isMcVideo = c => c && c.type === 'video' && c.enabled !== false && isMulticamItem(project.media.find(m => m.id === c.mediaId));
    if (isMcVideo(selected) && selected.start <= time + 1e-6 && time < selected.start + selected.duration - 1e-6) {
      return selected;
    }
    const candidates = project.clips
      .filter(c => isMcVideo(c) && c.start <= time + 1e-6 && time < c.start + c.duration - 1e-6)
      .sort((a, b) => b.track - a.track);
    return candidates[0] || null;
  }

  function cleanupTileVideos() {
    for (const entry of tileVideos) {
      if (entry.video) {
        entry.video.pause();
        entry.video.removeAttribute('src');
        entry.video.load();
        entry.video.remove();
      }
    }
    tileVideos = [];
    activeGridMediaId = null;
  }

  function ensureGrid(state) {
    const grid = document.getElementById('multicam-grid');
    const toggle = document.getElementById('multicam-toggle');
    if (toggle) {
      toggle.classList.toggle('active', viewEnabled);
      toggle.setAttribute('aria-pressed', String(viewEnabled));
    }
    if (!grid) return;
    if (!viewEnabled) {
      if (!grid.hidden || tileVideos.length) {
        cleanupTileVideos();
        grid.replaceChildren();
        grid.hidden = true;
      }
      return;
    }
    grid.hidden = false;
    const clip = activeMulticamClip(state);
    const mcMedia = clip ? state.project.media.find(m => m.id === clip.mediaId) : null;
    if (!clip || !isMulticamItem(mcMedia)) {
      cleanupTileVideos();
      grid.className = 'multicam-grid';
      grid.innerHTML =
        '<div class="multicam-empty-msg"><strong>Multicam View</strong><span>Park the playhead on a multicam clip to view and switch angles (keys 1–9).</span></div>';
      return;
    }

    const angles = mcMedia.multicam.angles;
    const gridClass = angles.length <= 4 ? 'multicam-grid grid-2x2' : 'multicam-grid grid-3x3';
    if (grid.className !== gridClass) grid.className = gridClass;

    if (activeGridMediaId !== mcMedia.id || tileVideos.length !== angles.length) {
      cleanupTileVideos();
      grid.replaceChildren();
      activeGridMediaId = mcMedia.id;
      angles.forEach((angle, idx) => {
        const angleMedia = state.project.media.find(m => m.id === angle.mediaId);
        const resolved = angleMedia ? window.Editor.resolveClipMedia({ mediaId: angleMedia.id, type: angleMedia.type }, state.project) : { item: null, inShift: 0 };
        const tile = document.createElement('button');
        tile.type = 'button';
        tile.className = 'multicam-tile';
        tile.dataset.angle = String(idx);
        tile.title = `Angle ${idx + 1}: ${angle.name || angleMedia?.name || 'Angle'} (click or press ${idx + 1} to cut; Shift+click to switch without cutting)`;
        const vid = document.createElement('video');
        vid.muted = true;
        vid.playsInline = true;
        vid.preload = 'auto';
        const src = resolved.item?.src || '';
        if (src) vid.src = src;
        const badge = document.createElement('span');
        badge.className = 'multicam-badge';
        badge.textContent = `${idx + 1} · ${angle.name || angleMedia?.name || 'Camera ' + (idx + 1)}`;
        tile.append(vid, badge);
        tile.onclick = e => {
          e.stopPropagation();
          try {
            switchAngle(idx, { cut: !e.shiftKey });
          } catch (err) {
            window.SpliceNotify?.(err.message);
          }
        };
        grid.append(tile);
        tileVideos.push({ index: idx, tile, video: vid, angle, resolvedItem: resolved.item, inShift: resolved.inShift || 0, src });
      });
    }

    const activeIdx = Number.isInteger(clip.multicamAngle) ? clip.multicamAngle : 0;
    const speed = clamp(Number(clip.speed) || 1, 0.1, 16);
    const local = state.time - clip.start;
    const mcTime = (Number(clip.in) || 0) + (clip.reverse ? clip.duration - local : local) * speed;
    const now = performance.now();
    const forceSeek = !state.playing || now - lastSyncStamp > 250;
    if (forceSeek) lastSyncStamp = now;

    for (const entry of tileVideos) {
      const isAct = entry.index === activeIdx;
      entry.tile.classList.toggle('active', isAct);
      entry.tile.classList.toggle('recording', isAct && !!state.playing);
      entry.tile.setAttribute('aria-pressed', String(isAct));

      const offset = Number(entry.angle.offset) || 0;
      const angIn = Number(entry.angle.in) || 0;
      const angDur = Number(entry.angle.duration) || entry.resolvedItem?.duration || Infinity;
      const inRange = mcTime >= offset - 1e-6 && mcTime < offset + angDur - 1e-6;
      entry.tile.classList.toggle('out-of-range', !inRange);
      const targetTime = Math.max(0, mcTime - offset + angIn + entry.inShift);
      const vid = entry.video;
      if (!entry.src || !inRange) {
        if (!vid.paused) vid.pause();
        continue;
      }
      if (vid.readyState >= 1) {
        const tol = state.playing ? 0.25 : 0.02;
        if (forceSeek && Math.abs(vid.currentTime - targetTime) > tol) {
          try { vid.currentTime = targetTime; } catch (_) {}
        }
      }
      if (state.playing && state.playbackRate > 0 && !clip.reverse) {
        vid.playbackRate = clamp(speed * state.playbackRate, 0.25, 4);
        if (vid.paused) vid.play().catch(() => {});
      } else if (!vid.paused) {
        vid.pause();
      }
    }
  }

  function setMulticamView(enabled) {
    viewEnabled = !!enabled;
    ensureGrid(window.Editor?.get?.());
    return viewEnabled;
  }

  function toggleMulticamView() {
    return setMulticamView(!viewEnabled);
  }

  function switchAngle(angleIndex, { cut = true } = {}) {
    const editor = window.Editor;
    const state = editor.get();
    const project = state.project;
    const clip = activeMulticamClip(state);
    if (!clip) throw new Error('Park the playhead on a multicam clip to switch angles.');
    const mcMedia = project.media.find(m => m.id === clip.mediaId);
    if (!isMulticamItem(mcMedia)) throw new Error('Active clip is not a multicam clip.');
    const angles = mcMedia.multicam.angles;
    if (!Number.isInteger(angleIndex) || angleIndex < 0 || angleIndex >= angles.length) {
      throw new Error(`Angle ${angleIndex + 1} does not exist (this multicam has ${angles.length} angles).`);
    }
    if (editor.isTrackLocked?.(clip.track, clip.type)) {
      throw new Error(`Unlock video track ${clip.track} before switching angles.`);
    }

    const fps = project.fps || 25;
    const frame = 1 / fps;
    const snapped = Math.round(state.time * fps) / fps;
    const audioMode = mcMedia.multicam.audioMode || 'camera1';
    const strictlyInside = snapped >= clip.start + frame - 1e-6 && snapped <= clip.start + clip.duration - frame + 1e-6;
    const shouldCut = cut && strictlyInside;

    const linkedAudio = clip.linkGroup
      ? project.clips.find(c => c.id !== clip.id && c.linkGroup === clip.linkGroup && c.type === 'audio' && c.mediaId === clip.mediaId)
      : null;
    if (linkedAudio && editor.isTrackLocked?.(linkedAudio.track, 'audio')) {
      throw new Error(`Unlock audio track ${Math.abs(linkedAudio.track)} before switching angles.`);
    }

    let nextSelectedId = clip.id;
    editor.editProject(next => {
      const target = next.clips.find(c => c.id === clip.id);
      if (!target) return;
      const partner = linkedAudio ? next.clips.find(c => c.id === linkedAudio.id) : null;
      if (!shouldCut) {
        target.multicamAngle = angleIndex;
        if (partner && audioMode === 'follow') partner.multicamAngle = angleIndex;
        return;
      }
      const speed = Number(target.speed) || 1;
      const leftDur = Math.round((snapped - target.start) * fps) / fps;
      const rightDur = Math.round((target.start + target.duration - snapped) * fps) / fps;
      if (leftDur < frame - 1e-6 || rightDur < frame - 1e-6) {
        target.multicamAngle = angleIndex;
        if (partner && audioMode === 'follow') partner.multicamAngle = angleIndex;
        return;
      }
      const rightVideo = structuredClone(target);
      rightVideo.id = uid();
      rightVideo.start = snapped;
      rightVideo.duration = rightDur;
      if (!target.reverse) rightVideo.in = (Number(target.in) || 0) + leftDur * speed;
      else target.in = (Number(target.in) || 0) + rightDur * speed;
      rightVideo.multicamAngle = angleIndex;
      rightVideo.transition = 'none';
      delete rightVideo.transitionBefore;
      delete rightVideo.transitionDuration;
      delete rightVideo.transitionMatchName;
      delete rightVideo.premiereIndex;
      target.duration = leftDur;

      if (partner && partner.start < snapped - frame * 0.5 && partner.start + partner.duration > snapped + frame * 0.5) {
        const leftGroup = uid(), rightGroup = uid();
        const pSpeed = Number(partner.speed) || 1;
        const pLeftDur = Math.round((snapped - partner.start) * fps) / fps;
        const pRightDur = Math.round((partner.start + partner.duration - snapped) * fps) / fps;
        const rightAudio = structuredClone(partner);
        rightAudio.id = uid();
        rightAudio.start = snapped;
        rightAudio.duration = pRightDur;
        if (!partner.reverse) rightAudio.in = (Number(partner.in) || 0) + pLeftDur * pSpeed;
        else partner.in = (Number(partner.in) || 0) + pRightDur * pSpeed;
        if (audioMode === 'follow') rightAudio.multicamAngle = angleIndex;
        rightAudio.transition = 'none';
        delete rightAudio.transitionBefore;
        delete rightAudio.transitionDuration;
        delete rightAudio.transitionMatchName;
        delete rightAudio.premiereIndex;
        partner.duration = pLeftDur;
        target.linkGroup = leftGroup;
        partner.linkGroup = leftGroup;
        rightVideo.linkGroup = rightGroup;
        rightAudio.linkGroup = rightGroup;
        next.clips.push(rightVideo, rightAudio);
      } else {
        delete rightVideo.linkGroup;
        next.clips.push(rightVideo);
      }
      nextSelectedId = rightVideo.id;
    });
    editor.select(nextSelectedId);
    ensureGrid(editor.get());
    return { clipId: nextSelectedId, angleIndex, cut: shouldCut };
  }

  function sameTrackMix(a = {}, b = {}) {
    return (a.gain ?? 100) === (b.gain ?? 100) && (a.pan ?? 0) === (b.pan ?? 0) &&
      !!a.mute === !!b.mute && !!a.solo === !!b.solo;
  }

  function flattenClips(clipIds) {
    const editor = window.Editor;
    const state = editor.get();
    const project = state.project;
    const idSet = new Set(Array.isArray(clipIds) ? clipIds : clipIds ? [clipIds] : []);
    if (!idSet.size && state.selectedId) idSet.add(state.selectedId);
    if (!idSet.size) throw new Error('Select one or more multicam clips to flatten.');

    // Include linked partners referencing the same multicam item.
    const selectedGroups = new Set(project.clips.filter(c => idSet.has(c.id) && c.linkGroup).map(c => c.linkGroup));
    for (const c of project.clips) if (c.linkGroup && selectedGroups.has(c.linkGroup)) idSet.add(c.id);

    const targets = project.clips.filter(c => idSet.has(c.id) && isMulticamItem(project.media.find(m => m.id === c.mediaId)));
    if (!targets.length) throw new Error('Select one or more multicam clips to flatten.');
    for (const c of targets) {
      if (editor.isTrackLocked?.(c.track, c.type))
        throw new Error(`Unlock ${c.type} track ${Math.abs(c.track)} before flattening.`);
    }

    const fps = project.fps || 25;
    const mediaById = new Map(project.media.map(m => [m.id, m]));
    const targetIds = new Set(targets.map(c => c.id));

    const makeAngleClip = (base, angle, kind, track, embeddedAudio) => {
      const angMedia = mediaById.get(angle.mediaId);
      if (!angMedia) throw new Error('Multicam angle references missing media.');
      const offset = Number(angle.offset) || 0;
      const angIn = Number(angle.in) || 0;
      const angDur = Number(angle.duration) || angMedia.duration || Infinity;
      const speed = Number(base.speed) || 1;
      const mcIn = Number(base.in) || 0;
      const mcSpan = base.duration * speed;
      const ovStart = Math.max(mcIn, offset);
      const ovEnd = Math.min(mcIn + mcSpan, offset + angDur);
      if (ovEnd <= ovStart + 1e-6) return null;
      const out = structuredClone(base);
      out.id = uid();
      out.type = kind;
      out.track = track;
      out.mediaId = angle.mediaId;
      out.start = Math.round((base.reverse ? base.start + (mcIn + mcSpan - ovEnd) / speed : base.start + (ovStart - mcIn) / speed) * fps) / fps;
      out.duration = Math.round(((ovEnd - ovStart) / speed) * fps) / fps;
      if (out.duration < 1 / fps - 1e-6) return null;
      out.in = angIn + (ovStart - offset);
      delete out.multicamAngle;
      delete out.multicamAudioAngle;
      delete out.premiereIndex;
      delete out.premiereTrack;
      if (embeddedAudio !== undefined) out.embeddedAudio = embeddedAudio;
      return out;
    };

    const produced = [];
    const audioLanes = project.clips.filter(c => c.type === 'audio');
    const addedMixes = {};
    const addAngleAudio = (base, angle, sourceTrack, preferred) => {
      const media = mediaById.get(angle.mediaId);
      if (!media || (media.type !== 'audio' && (media.still || !media.audioChannels))) return;
      const sound = makeAngleClip(base, angle, 'audio', preferred);
      if (!sound) return;
      const mix = project.trackMix?.[sourceTrack];
      // A lane with other clips must retain its mix even outside this edit.
      // A64 also carries embedded multicam voices with no timeline audio clip.
      const candidates = [preferred, ...Array.from({length:64}, (_, i) => -1-i)];
      for (const track of candidates) {
        if (editor.isTrackLocked(track, 'audio')) continue;
        const lane = audioLanes.filter(c => c.track === track);
        if (lane.some(c => c !== base && c.start < sound.start + sound.duration - 1e-6 &&
            c.start + c.duration > sound.start + 1e-6)) continue;
        const destinationMix = addedMixes[track] ?? project.trackMix?.[track];
        if (!sameTrackMix(mix, destinationMix)) {
          if (lane.length || destinationMix || track === -64) continue;
          addedMixes[track] = structuredClone(mix);
        }
        sound.track = track;
        delete sound.linkGroup;
        audioLanes.push(sound);
        produced.push(sound);
        return;
      }
      throw new Error('Flattening needs a free, unlocked audio track that preserves the camera mix.');
    };
    const linkRemap = new Map();
    for (const clip of targets) {
      const item = mediaById.get(clip.mediaId);
      const mc = item.multicam;
      const angles = mc.angles;
      const audioMode = mc.audioMode || 'camera1';
      const allAngles = audioMode === 'all' && !Number.isInteger(clip.multicamAudioAngle);
      const vIdx = clamp(Number.isInteger(clip.multicamAngle) ? clip.multicamAngle : 0, 0, angles.length - 1);
      const aIdx = clamp(
        Number.isInteger(clip.multicamAudioAngle)
          ? clip.multicamAudioAngle
          : audioMode === 'camera1' ? 0 : audioMode === 'fixed' ? (mc.audioAngle || 0) : vIdx,
        0,
        angles.length - 1,
      );

      if (clip.type === 'audio') {
        if (allAngles) {
          for (const angle of angles) addAngleAudio(clip, angle, clip.track, clip.track);
        } else {
          const ac = makeAngleClip(clip, angles[aIdx], 'audio', clip.track);
          if (ac) {
            if (clip.linkGroup && aIdx === vIdx && audioMode !== 'all') {
              if (!linkRemap.has(clip.linkGroup)) linkRemap.set(clip.linkGroup, uid());
              ac.linkGroup = linkRemap.get(clip.linkGroup);
            } else delete ac.linkGroup;
            produced.push(ac);
          }
        }
        continue;
      }

      // Video clip
      const hasEmbedded = clip.embeddedAudio !== false;
      const sameAudio = aIdx === vIdx && audioMode !== 'all';
      const vc = makeAngleClip(clip, angles[vIdx], 'video', clip.track, hasEmbedded ? sameAudio : false);
      if (vc) {
        if (clip.linkGroup && aIdx === vIdx && audioMode !== 'all') {
          if (!linkRemap.has(clip.linkGroup)) linkRemap.set(clip.linkGroup, uid());
          vc.linkGroup = linkRemap.get(clip.linkGroup);
        } else delete vc.linkGroup;
        produced.push(vc);
      }
      if (hasEmbedded && !sameAudio) {
        for (const angle of (allAngles ? angles : [angles[aIdx]])) addAngleAudio(clip, angle, -64, -1);
      }
    }

    if (!produced.length) throw new Error('No angle footage overlapped the selected multicam clip(s).');

    editor.editProject(next => {
      if (Object.keys(addedMixes).length) next.trackMix = {...next.trackMix, ...addedMixes};
      next.clips = next.clips.filter(c => !targetIds.has(c.id));
      next.clips.push(...produced);
    });
    editor.select(produced[0].id);
    ensureGrid(editor.get());
    return produced;
  }

  function init() {
    if (!window.Editor) return;
    window.Editor.subscribe(state => {
      ensureGrid(state);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();

  return {
    isMulticamItem,
    parseTimecodeSeconds,
    computeAngles,
    createMulticam,
    openCreateDialog,
    activeMulticamClip,
    switchAngle,
    flattenClips,
    setMulticamView,
    toggleMulticamView,
    isViewEnabled: () => viewEnabled,
    cleanup: cleanupTileVideos,
  };
})();
