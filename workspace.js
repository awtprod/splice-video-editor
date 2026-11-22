(() => {
  const editor = window.Editor;
  const $ = (id) => document.getElementById(id);
  const fmt = (seconds, fps = 30) => {
    const frames = Math.max(0, Math.round((seconds || 0) * fps));
    const nominal = Math.round(fps);
    const f = frames % nominal;
    const whole = Math.floor(frames / nominal);
    return [
      Math.floor(whole / 3600),
      Math.floor(whole / 60) % 60,
      whole % 60,
      f,
    ]
      .map((n) => String(n).padStart(2, "0"))
      .join(":");
  };
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  let tab = "media";
  let zoom = 0.55;
  let lastProject = "";
  let lastSelection = null;
  let lastMedia = "";
  let toastTimer;
  const px = (seconds) => seconds * 60 * zoom;
  const waveformCache = new Map();
  async function waveform(src) {
    if (!waveformCache.has(src))
      waveformCache.set(
        src,
        (async () => {
          const buffer = await (await fetch(src)).arrayBuffer();
          const context = new (
            window.AudioContext || window.webkitAudioContext
          )();
          try {
            const decoded = await context.decodeAudioData(buffer);
            return decoded.getChannelData(0).slice();
          } finally {
            await context.close();
          }
        })().catch(() => null),
      );
    return waveformCache.get(src);
  }
  async function paintWave(canvas, media, clip) {
    if (!media?.src) return;
    const samples = await waveform(media.src);
    if (!samples || !canvas.isConnected) return;
    const width = Math.max(1, Math.round(canvas.getBoundingClientRect().width));
    canvas.width = width * 2;
    canvas.height = 92;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#a3eab9";
    const duration = media.duration || clip.duration;
    const samplesPerSecond = samples.length / duration;
    for (let x = 1; x < canvas.width; x += 4) {
      const first = Math.max(
        0,
        Math.floor(
          ((clip.in || 0) + (x / canvas.width) * clip.duration) *
            samplesPerSecond,
        ),
      );
      const last = Math.min(
        samples.length,
        first +
          Math.max(
            1,
            Math.ceil((clip.duration / canvas.width) * samplesPerSecond * 4),
          ),
      );
      let peak = 0;
      for (
        let i = first;
        i < last;
        i += Math.max(1, Math.floor((last - first) / 20))
      )
        peak = Math.max(peak, Math.abs(samples[i]));
      const h = Math.max(2, peak * canvas.height * 0.82);
      ctx.fillRect(x, (canvas.height - h) / 2, 2, h);
    }
  }
  const typeOf = (clip) => clip.type;
  const notify = (message) => {
    document.querySelector(".toast")?.remove();
    const node = document.createElement("div");
    node.className = "toast";
    node.textContent = message;
    document.body.append(node);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.remove(), 3500);
  };
  let pending = false;
  const busy = async (action, done) => {
    if (pending) return;
    pending = true;
    $("save-status").textContent = "Working…";
    try {
      await action();
      $("save-status").textContent = "Ready";
      if (done) notify(done);
    } catch (error) {
      $("save-status").textContent = "Error";
      notify(error.message || String(error));
    } finally {
      pending = false;
    }
  };
  const mediaFor = (project, clip) =>
    project.media.find((m) => m.id === clip.mediaId);
  function renderLibrary(state) {
    const { project } = state;
    $("asset-count").textContent = project.media.length;
    $("asset-footer").textContent = `${project.media.length} assets in project`;
    const body = $("library-body");
    if (tab === "media") {
      body.innerHTML = `<div class="library-section">Imported media</div>${project.media.some((m) => m.offline || !m.src) ? '<button class="asset-add" id="relink-assets">↻ &nbsp; Relink offline media</button>' : ""}<div class="media-grid">${project.media.map((media) => `<button class="media-card" draggable="true" data-media="${esc(media.id)}" title="Double click to add to timeline"><div class="media-thumb ${media.type === "audio" ? "audio-thumb" : ""}">${media.type === "audio" ? "♫" : media.thumb ? `<img src="${esc(media.thumb)}" alt="">` : "▶"}<span class="media-icon">${media.type === "audio" ? "♫" : "▶"}</span><span class="media-duration">${fmt(media.duration, project.fps).slice(3, 8)}</span></div><span class="media-name">${esc(media.name)}</span><span class="media-meta">${media.offline || !media.src ? "Offline · Relink media" : `${media.type === "audio" ? "Audio" : "Video"} · ${Math.round(media.duration || 0)}s`}</span></button>`).join("")}</div><button class="asset-add" id="asset-add">＋ &nbsp; Import your own footage or audio</button>`;
      body.querySelectorAll("[data-media]").forEach((el) => {
        el.ondblclick = () => editor.addToTimeline(el.dataset.media);
        el.ondragstart = (e) =>
          e.dataTransfer.setData("text/plain", el.dataset.media);
      });
      $("asset-add").onclick = () => $("media-input").click();
      if ($("relink-assets"))
        $("relink-assets").onclick = () => $("relink-input").click();
    } else if (tab === "effects")
      body.innerHTML =
        '<div class="library-section">Video effects</div><button class="library-action" id="add-transition"><span>◩</span><div>Cross dissolve<small>Blend into the selected clip</small></div></button><div class="library-section" style="margin-top:22px">Color controls</div><p class="library-help">Select a video clip and use the Inspector to adjust brightness, contrast, and saturation.</p>';
    else
      body.innerHTML =
        '<div class="library-section">Text & graphics</div><button class="library-action" id="add-title"><span>T</span><div>Simple title<small>Place a title at the playhead</small></div></button>';
    if ($("add-transition"))
      $("add-transition").onclick = () => {
        if (!editor.get().selected || typeOf(editor.get().selected) !== "video")
          return notify("Select a video clip first.");
        editor.addTransition();
      };
    if ($("add-title")) $("add-title").onclick = () => editor.addTitle();
  }
  function renderInspector(state) {
    const { selected, project } = state;
    const panel = $("inspector-content");
    if (!selected) {
      panel.innerHTML =
        '<div class="inspector-empty"><div><strong>No clip selected</strong><br>Select a clip on the timeline to edit its settings.</div></div>';
      return;
    }
    const media = mediaFor(project, selected);
    const kind = typeOf(selected);
    const color = selected.color || {};
    panel.innerHTML = `<div class="inspector-section"><div class="inspect-kicker">${kind.toUpperCase()} CLIP</div><div class="inspect-title">${esc(selected.title || media?.name || "Title")}</div><div class="inspect-subtitle">${fmt(selected.start, project.fps)} &nbsp; · &nbsp; ${fmt(selected.duration, project.fps)} duration</div></div>
      ${kind === "title" ? `<div class="inspector-section"><div class="inspector-heading">Text</div><label class="inspect-row">Title <input id="setting-title" type="text" value="${esc(selected.title)}" maxlength="100"></label></div>` : ""}
      <div class="inspector-section"><div class="inspector-heading">Timing</div><label class="inspect-row">Start (sec) <input id="setting-start" type="number" min="0" step="0.1" value="${Number(selected.start).toFixed(1)}"></label><label class="inspect-row">Source in (sec) <input id="setting-in" type="number" min="0" step="0.1" value="${Number(selected.in || 0).toFixed(1)}"></label><label class="inspect-row">Duration (sec) <input id="setting-duration" type="number" min="0.1" step="0.1" value="${Number(selected.duration).toFixed(1)}"></label></div>
      ${kind === "video" ? `<div class="inspector-section"><div class="inspector-heading">Color <small>Basic correction</small></div>${["brightness", "contrast", "saturation"].map((key) => `<label class="inspect-row">${key[0].toUpperCase() + key.slice(1)}</label><div class="inspect-slider"><input data-color="${key}" type="range" min="0" max="200" value="${Number(color[key] ?? 100)}"><span id="value-${key}">${Number(color[key] ?? 100)}%</span></div>`).join("")}</div><div class="inspector-section"><div class="inspector-heading">Transition</div><label class="inspect-row">Incoming <select id="setting-transition"><option value="none" ${selected.transition !== "dissolve" ? "selected" : ""}>None</option><option value="dissolve" ${selected.transition === "dissolve" ? "selected" : ""}>Cross dissolve</option></select></label></div>` : ""}
      ${kind === "audio" ? `<div class="inspector-section"><div class="inspector-heading">Audio mixing</div><label class="inspect-row">Volume</label><div class="inspect-slider"><input id="setting-volume" type="range" min="0" max="100" value="${Number(selected.volume ?? 100)}"><span id="value-volume">${Number(selected.volume ?? 100)}%</span></div></div>` : ""}`;
    panel.querySelectorAll("input,select").forEach((input) => {
      input.addEventListener(
        input.type === "range" ? "change" : "change",
        () => {
          const current = editor.get().selected;
          if (!current) return;
          if (input.id === "setting-title")
            editor.updateClip(current.id, { title: input.value });
          if (input.id === "setting-start")
            editor.updateClip(current.id, { start: Number(input.value) });
          if (input.id === "setting-in")
            editor.updateClip(current.id, { in: Number(input.value) });
          if (input.id === "setting-duration")
            editor.updateClip(current.id, { duration: Number(input.value) });
          if (input.id === "setting-transition")
            editor.updateClip(current.id, { transition: input.value });
          if (input.id === "setting-volume")
            editor.updateClip(current.id, { volume: Number(input.value) });
          if (input.dataset.color)
            editor.updateClip(current.id, {
              color: {
                ...(current.color || {}),
                [input.dataset.color]: Number(input.value),
              },
            });
        },
      );
      if (input.type === "range")
        input.addEventListener("input", () => {
          const span = $(
            input.dataset.color
              ? `value-${input.dataset.color}`
              : "value-volume",
          );
          if (span) span.textContent = `${input.value}%`;
        });
    });
  }
  function renderTimeline(state) {
    const { project, duration, selectedId } = state;
    const width = Math.max(
      $("track-scroll").clientWidth,
      px(Math.max(38, duration + 4)),
    );
    $("timeline-surface").style.width = `${width}px`;
    const ruler = $("time-ruler");
    ruler.innerHTML = "";
    const step = 5;
    ruler.style.backgroundSize = `${px(step)}px 100%`;
    for (let t = 0; t <= Math.ceil(width / (60 * zoom)); t += step) {
      const tick = document.createElement("span");
      tick.className = "ruler-tick";
      tick.style.left = `${px(t) + 5}px`;
      tick.textContent = fmt(t, project.fps).slice(3, 8);
      ruler.append(tick);
    }
    for (const kind of ["title", "video", "audio"]) {
      const lane = $(`${kind}-lane`);
      lane.innerHTML = "";
      project.clips
        .filter((c) => typeOf(c) === kind)
        .forEach((clip) => {
          const media = mediaFor(project, clip);
          const node = document.createElement("button");
          node.className = `timeline-clip ${kind}${clip.id === selectedId ? " selected" : ""}`;
          node.style.left = `${px(clip.start)}px`;
          node.style.width = `${Math.max(22, px(clip.duration) - 2)}px`;
          node.dataset.clip = clip.id;
          node.draggable = true;
          node.innerHTML = `${media?.thumb ? `<img class="clip-image" src="${esc(media.thumb)}" alt="">` : ""}<span class="clip-overlay"></span>${kind === "audio" ? '<canvas class="clip-wave"></canvas>' : ""}<span class="clip-name">${esc(clip.title || media?.name || "Title")}${clip.transition === "dissolve" ? " · Fade" : ""}</span>`;
          node.onclick = (e) => {
            e.stopPropagation();
            editor.select(clip.id);
          };
          node.ondragstart = (e) => {
            e.dataTransfer.setData("application/splice-clip", clip.id);
          };
          lane.append(node);
          if (kind === "audio")
            paintWave(node.querySelector("canvas"), media, clip);
        });
    }
  }
  function renderPreview(state) {
    const { project, time, duration, playing, activeClip, activeTitle } = state;
    const fps = project.fps || 30;
    $("viewer-time").textContent = fmt(time, fps);
    $("header-time").textContent = fmt(time, fps);
    $("viewer-duration").textContent = `/ ${fmt(duration, fps)}`;
    $("play-toggle").innerHTML = playing
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>'
      : svg(icons["play-toggle"]);
    $("playhead").style.left = `${px(time)}px`;
    $("preview-empty").style.display =
      activeClip || activeTitle ? "none" : "flex";
  }
  const posterCache = new Map();
  function paintPreview() {
    const canvas = $("preview-canvas");
    if (editor.drawFrame) editor.drawFrame(canvas);
    const state = editor.get();
    const media = state.activeClip && mediaFor(state.project, state.activeClip);
    if (media?.thumb && (!state.video || state.video.readyState < 2)) {
      let poster = posterCache.get(media.thumb);
      if (!poster) {
        poster = new Image();
        poster.src = media.thumb;
        posterCache.set(media.thumb, poster);
      }
      if (poster.complete && poster.naturalWidth) {
        const ctx = canvas.getContext("2d");
        ctx.drawImage(poster, 0, 0, canvas.width, canvas.height);
      }
    }
    requestAnimationFrame(paintPreview);
  }
  requestAnimationFrame(paintPreview);
  function refresh(state) {
    const signature = JSON.stringify({
      media: state.project.media,
      clips: state.project.clips,
      name: state.project.name,
      zoom,
      selectedId: state.selectedId,
    });
    const projectChanged = signature !== lastProject;
    if (projectChanged) {
      renderTimeline(state);
      lastProject = signature;
    }
    document
      .querySelectorAll(".track-mute")
      .forEach((b) =>
        b.classList.toggle("off", !!state.mutedTracks?.[b.dataset.track]),
      );
    const mediaSignature = JSON.stringify(state.project.media) + tab;
    if (mediaSignature !== lastMedia) {
      renderLibrary(state);
      lastMedia = mediaSignature;
    }
    if (
      state.selectedId !== lastSelection ||
      (projectChanged &&
        !document.activeElement?.closest?.("#inspector-content"))
    ) {
      renderInspector(state);
      lastSelection = state.selectedId;
    }
    $("project-name").innerHTML =
      `${esc(state.project.name)} <span class="chevron">⌄</span>`;
    document.querySelector(".sequence-name").textContent =
      `${state.project.name} — Main Edit`;
    document.querySelector(".timeline-project").textContent =
      `${state.project.name} — Main Edit`;
    $("fps-label").textContent = `${Number((state.project.fps || 30).toFixed(3))} FPS`;
    $("timeline-status").textContent =
      `${state.project.clips.length} clips · ${fmt(state.duration, state.project.fps)}`;
    renderPreview(state);
  }
  const icons = {
    "library-import": "M12 5v14M5 12h14",
    "fullscreen-monitor": "M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5",
    "jump-start": "M4 5v14M18 5L8 12l10 7V5Z",
    "step-back": "M17 5L7 12l10 7V5Z",
    "play-toggle": "M8 5l11 7-11 7V5Z",
    "step-forward": "M7 5l10 7-10 7V5Z",
    "jump-end": "M20 5v14M6 5l10 7-10 7V5Z",
    undo: "M9 14L4 9l5-5M4 9h10a6 6 0 0 1 0 12",
    redo: "M15 14l5-5-5-5M20 9H10a6 6 0 0 0 0 12",
    "delete-selected": "M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15M10 10v7M14 10v7",
    "reset-clip": "M3 12a9 9 0 1 0 3-7M3 4v5h5",
    "open-project": "M12 17V4M7 9l5-5 5 5M4 17v3h16v-3",
    "save-project": "M5 3h12l3 3v15H4V3h1zm2 0v6h10V3M7 21v-9h10v9",
  };
  const svg = (path) =>
    `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`;
  for (const [id, path] of Object.entries(icons)) {
    const button = $(id);
    if (!button) continue;
    const label = button.querySelector("span");
    button.innerHTML =
      svg(path) + (label ? ` <span>${label.textContent}</span>` : "");
  }
  $("play-toggle").dataset.icon = "play";
  function sizeMonitor() {
    const area = document.querySelector(".monitor-area");
    const frame = $("monitor-frame");
    const width = Math.max(
      1,
      Math.min(area.clientWidth - 24, ((area.clientHeight - 24) * 16) / 9),
    );
    frame.style.width = `${width}px`;
    frame.style.height = `${(width * 9) / 16}px`;
  }
  new ResizeObserver(sizeMonitor).observe(
    document.querySelector(".monitor-area"),
  );
  editor.subscribe(refresh);
  sizeMonitor();
  editor.seek(0);
  document.querySelectorAll("[data-library]").forEach(
    (button) =>
      (button.onclick = () => {
        tab = button.dataset.library;
        document
          .querySelectorAll("[data-library]")
          .forEach((b) => b.classList.toggle("active", b === button));
        lastMedia = "";
        renderLibrary(editor.get());
      }),
  );
  document.querySelectorAll("[data-workspace]").forEach(
    (button) =>
      (button.onclick = () => {
        document
          .querySelectorAll("[data-workspace]")
          .forEach((b) => b.classList.toggle("active", b === button));
        if (
          button.dataset.workspace === "color" ||
          button.dataset.workspace === "audio"
        ) {
          const kind = button.dataset.workspace === "audio" ? "audio" : "video";
          const target = editor
            .get()
            .project.clips.find((c) => typeOf(c) === kind);
          if (target) editor.select(target.id);
          else notify(`Add a ${kind} clip to adjust it.`);
        }
      }),
  );
  $("import-media").onclick = $("library-import").onclick = () =>
    $("media-input").click();
  $("relink-input").onchange = (e) => {
    const files = Array.from(e.target.files);
    if (files.length) busy(() => editor.relinkMedia(files), "Media relinked");
    e.target.value = "";
  };
  $("media-input").onchange = (e) => {
    const files = Array.from(e.target.files);
    e.target.value = "";
    if (files.length) busy(() => editor.addMedia(files), "Media imported");
  };
  $("open-project").onclick = () => $("project-input").click();
  $("project-input").onchange = (e) => {
    const file = e.target.files[0];
    if (file) busy(() => editor.openProject(file), "Project opened");
    e.target.value = "";
  };
  $("save-project").onclick = () =>
    busy(() => editor.saveProject("prproj"), "Premiere project saved");
  $("export-video").onclick = () =>
    busy(() => editor.exportVideo(), "Video export complete");
  $("new-project").onclick = () => {
    const current = editor.get().project;
    if (
      current.clips.length &&
      !confirm(
        "Create a new project? Save your current edit first if you need it.",
      )
    )
      return;
    editor.newProject();
    notify("New project created");
  };
  $("project-name").onclick = () => {
    const name = prompt("Project name", editor.get().project.name);
    if (name?.trim()) {
      editor.get().project.name = name.trim();
      refresh(editor.get());
    }
  };
  $("play-toggle").onclick = () => editor.togglePlay();
  $("jump-start").onclick = () => editor.seek(0);
  $("jump-end").onclick = () => editor.seek(editor.get().duration);
  $("step-back").onclick = () =>
    editor.seek(editor.get().time - 1 / editor.get().project.fps);
  $("step-forward").onclick = () =>
    editor.seek(editor.get().time + 1 / editor.get().project.fps);
  $("split").onclick = () => editor.split();
  $("delete-selected").onclick = () => editor.deleteSelected();
  $("undo").onclick = () => editor.undo();
  $("redo").onclick = () => editor.redo();
  $("reset-clip").onclick = () => {
    const c = editor.get().selected;
    if (c)
      editor.updateClip(c.id, {
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: "none",
      });
  };
  $("zoom-out").onclick = () => {
    zoom = Math.max(0.4, zoom / 1.25);
    $("zoom-label").textContent = `${Math.round(zoom * 100)}%`;
    lastProject = "";
    refresh(editor.get());
  };
  $("zoom-in").onclick = () => {
    zoom = Math.min(3, zoom * 1.25);
    $("zoom-label").textContent = `${Math.round(zoom * 100)}%`;
    lastProject = "";
    refresh(editor.get());
  };
  $("time-ruler").onclick = (e) =>
    editor.seek(
      (e.clientX - $("time-ruler").getBoundingClientRect().left) / (60 * zoom),
    );
  document.querySelectorAll(".track-lane").forEach((lane) => {
    lane.ondragover = (e) => e.preventDefault();
    lane.ondrop = (e) => {
      e.preventDefault();
      const id = e.dataTransfer.getData("application/splice-clip");
      const media = e.dataTransfer.getData("text/plain");
      const dragged = id
        ? editor.get().project.clips.find((c) => c.id === id)
        : editor.get().project.media.find((m) => m.id === media);
      if (!dragged || dragged.type !== lane.dataset.track)
        return notify(
          "Drop this item onto its " +
            (dragged?.type || "matching") +
            " track.",
        );
      const seconds = Math.max(
        0,
        (e.clientX - lane.getBoundingClientRect().left) / (60 * zoom),
      );
      if (id) editor.moveClip(id, seconds, dragged.track);
      else if (media) {
        editor.seek(seconds);
        editor.addToTimeline(media);
        const c = editor.get().selected;
        if (c)
          editor.moveClip(
            c.id,
            seconds,
            lane.dataset.track === "audio" ? -1 : 1,
          );
      }
    };
    lane.onclick = (e) => {
      if (e.target === lane)
        editor.seek(
          (e.clientX - lane.getBoundingClientRect().left) / (60 * zoom),
        );
    };
  });
  document.querySelectorAll(".track-mute").forEach(
    (button) =>
      (button.onclick = () => {
        button.classList.toggle("off");
        const off = button.classList.contains("off");
        editor.setTrackMuted?.(button.dataset.track, off);
        notify(`${button.dataset.track} track ${off ? "muted" : "enabled"}`);
      }),
  );
  $("fullscreen-monitor").onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else $("monitor-frame").requestFullscreen?.();
  };
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (e.shiftKey)
        busy(() => editor.saveProject("splice"), "Splice project saved");
      else $("save-project").click();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "o") {
      e.preventDefault();
      $("open-project").click();
      return;
    }
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
    if (e.code === "Space") {
      e.preventDefault();
      editor.togglePlay();
    }
    if (e.key.toLowerCase() === "s" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      editor.split();
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      e.shiftKey ? editor.redo() : editor.undo();
    }
    if (e.key === "Delete" || e.key === "Backspace") editor.deleteSelected();
  });
})();
