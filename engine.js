(() => {
  const assetNames = ["ridge", "valley", "forest", "lake"];
  const labels = ["The Ridge", "Into the Valley", "Forest Light", "The Lake"];
  const media = assetNames.map((name, index) => ({
    id: name,
    name: labels[index],
    src: `assets/${name}.mp4`,
    thumb: `assets/${name}.jpg`,
    duration: 9,
    type: "video",
  }));
  media.push({
    id: "score",
    name: "Ambient score",
    src: "assets/score.wav",
    duration: 32,
    type: "audio",
  });
  const starter = () => ({
    name: "Alpine Passage",
    width: 1920,
    height: 1080,
    fps: 25,
    media: structuredClone(media),
    clips: [
      ...assetNames.map((name, index) => ({
        id: `clip-${index}`,
        mediaId: name,
        start: index * 8,
        duration: 8,
        in: 0.5,
        track: 1,
        type: "video",
        title: "",
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: index ? "dissolve" : "none",
      })),
      {
        id: "opening-title",
        mediaId: null,
        start: 0.8,
        duration: 3,
        in: 0,
        track: 2,
        type: "title",
        title: "ALPINE PASSAGE",
        color: {},
        volume: 100,
        transition: "none",
      },
      {
        id: "clip-score",
        mediaId: "score",
        start: 0,
        duration: 32,
        in: 0,
        track: -1,
        type: "audio",
        title: "",
        volume: 55,
        transition: "none",
      },
    ],
    duration: 32,
  });
  let project = starter();
  let selectedId = "clip-0";
  let time = 0;
  let playing = false;
  let lastTick = 0;
  let video = null;
  let transitionVideo = null;
  let audio = null;
  const audioElements = new Map();
  let activeVideoId = null;
  let transitionVideoId = null;
  let mutedTracks = { audio: false, video: false, title: false };
  let history = [];
  let future = [];
  const subscribers = new Set();
  const uid = () => crypto.randomUUID();
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const snap = (value) => Math.round(Number(value) * project.fps) / project.fps;
  const selected = () => project.clips.find((c) => c.id === selectedId);
  const duration = () =>
    Math.max(0, ...project.clips.map((c) => c.start + c.duration));
  const notify = () => subscribers.forEach((fn) => fn(Editor.get()));
  const snapshot = () => {
    history.push(structuredClone(project));
    if (history.length > 50) history.shift();
    future = [];
  };
  const mediaOf = (clip) => project.media.find((m) => m.id === clip.mediaId);
  const at = (kind, t) =>
    project.clips
      .filter(
        (c) => c.type === kind && c.start <= t && t < c.start + c.duration,
      )
      .sort((a, b) => b.track - a.track)[0];
  const filterFor = (clip) => {
    const c = clip.color || {};
    return `brightness(${Number(c.brightness ?? 100)}%) contrast(${Number(c.contrast ?? 100)}%) saturate(${Number(c.saturation ?? 100)}%)`;
  };
  function ensureElements() {
    if (!video) {
      video = document.createElement("video");
      video.playsInline = true;
      video.preload = "auto";
      video.style.display = "none";
      document.body.append(video);
    }
    if (!transitionVideo) {
      transitionVideo = document.createElement("video");
      transitionVideo.muted = true;
      transitionVideo.preload = "auto";
      transitionVideo.style.display = "none";
      document.body.append(transitionVideo);
    }
  }
  function preceding(clip) {
    if (!clip || clip.transition !== "dissolve") return null;
    return (
      project.clips
        .filter(
          (c) =>
            c.type === "video" &&
            c.id !== clip.id &&
            c.start < clip.start &&
            c.start + c.duration >= clip.start - 0.05,
        )
        .sort((a, b) => b.start - a.start)[0] || null
    );
  }
  function syncMedia(forceSeek = false) {
    ensureElements();
    const clip = at("video", time);
    const audioClips = project.clips.filter(
      (c) =>
        c.type === "audio" && c.start <= time && time < c.start + c.duration,
    );
    const mediaItem = clip && mediaOf(clip);
    if (
      clip?.id !== activeVideoId ||
      (video.getAttribute("src") || "") !== (mediaItem?.src || "")
    ) {
      activeVideoId = clip?.id;
      video.pause();
      video.removeAttribute("src");
      video.load();
      if (mediaItem?.src) {
        video.src = mediaItem.src;
        video.load();
      }
    }
    if (clip && mediaItem) {
      const target = (clip.in || 0) + time - clip.start;
      if (forceSeek || Math.abs(video.currentTime - target) > 0.3) {
        try {
          video.currentTime = target;
        } catch (_) {
          /* Metadata may still be loading. */
        }
      }
      video.style.filter = filterFor(clip);
      if (playing) video.play().catch(() => {});
      else video.pause();
    }
    const previous = clip && time < clip.start + 0.6 ? preceding(clip) : null;
    const previousMedia = previous && mediaOf(previous);
    if (
      previous?.id !== transitionVideoId ||
      (previousMedia?.src &&
        transitionVideo.src !== new URL(previousMedia.src, location.href).href)
    ) {
      transitionVideoId = previous?.id;
      transitionVideo.removeAttribute("src");
      transitionVideo.load();
      if (previousMedia?.src) {
        transitionVideo.src = previousMedia.src;
        transitionVideo.load();
      }
    }
    if (previous) {
      const target =
        (previous.in || 0) + previous.duration - 0.6 + (time - clip.start);
      if (Math.abs(transitionVideo.currentTime - target) > 1 / project.fps)
        try {
          transitionVideo.currentTime = target;
        } catch (_) {
          /* Metadata pending. */
        }
    }
    const currentIds = new Set(audioClips.map((c) => c.id));
    for (const [id, element] of audioElements)
      if (!currentIds.has(id)) {
        element.pause();
        element.removeAttribute("src");
        element.load();
        element.remove();
        audioElements.delete(id);
      }
    for (const audioClip of audioClips) {
      const item = mediaOf(audioClip);
      if (!item?.src) continue;
      let element = audioElements.get(audioClip.id);
      if (!element) {
        element = document.createElement("audio");
        element.src = item.src;
        element.preload = "auto";
        element.style.display = "none";
        document.body.append(element);
        audioElements.set(audioClip.id, element);
      }
      if (element.getAttribute("src") !== item.src) element.src = item.src;
      const target = (audioClip.in || 0) + time - audioClip.start;
      if (forceSeek || Math.abs(element.currentTime - target) > 0.3)
        try {
          element.currentTime = target;
        } catch (_) {
          /* Metadata may still be loading. */
        }
      element.volume = mutedTracks.audio
        ? 0
        : clamp((audioClip.volume ?? 100) / 100, 0, 1);
      if (playing) element.play().catch(() => {});
      else element.pause();
    }
    audio = audioClips.length ? audioElements.get(audioClips[0].id) : null;
    video.volume =
      mutedTracks.audio || clip?.embeddedAudio === false
        ? 0
        : clamp((clip?.volume ?? 100) / 100, 0, 1);
  }
  function drawFrame(canvas) {
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#080a0a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const clip = at("video", time);
    if (!mutedTracks.video && clip) {
      const previous = time < clip.start + 0.6 ? preceding(clip) : null;
      if (previous && transitionVideo.readyState >= 2) {
        ctx.filter = filterFor(previous);
        ctx.drawImage(transitionVideo, 0, 0, canvas.width, canvas.height);
      }
      if (video?.readyState >= 2) {
        ctx.globalAlpha = previous ? clamp((time - clip.start) / 0.6, 0, 1) : 1;
        ctx.filter = filterFor(clip);
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      }
      ctx.globalAlpha = 1;
      ctx.filter = "none";
    }
    const title = mutedTracks.title ? null : at("title", time);
    if (title?.title) {
      ctx.fillStyle = "#fff";
      ctx.font = `bold ${Math.round(canvas.height * 0.08)}px sans-serif`;
      ctx.textAlign = "center";
      ctx.shadowColor = "#000";
      ctx.shadowBlur = canvas.height * 0.025;
      ctx.fillText(title.title, canvas.width / 2, canvas.height * 0.78);
      ctx.shadowBlur = 0;
    }
  }
  function tick(timestamp) {
    if (playing) {
      time = Math.min(
        duration(),
        time + Math.min((timestamp - lastTick) / 1000, 0.1),
      );
      if (time >= duration()) {
        playing = false;
        video?.pause();
        audio?.pause();
      }
      syncMedia();
      notify();
    }
    lastTick = timestamp;
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  async function fileToMedia(file) {
    const src = URL.createObjectURL(file);
    const isAudio = file.type.startsWith("audio/");
    const element = document.createElement(isAudio ? "audio" : "video");
    element.preload = "metadata";
    element.src = src;
    const length = await new Promise((resolve, reject) => {
      element.onloadedmetadata = () => resolve(element.duration);
      element.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    });
    if (!Number.isFinite(length) || length <= 0)
      throw new Error(`Could not determine the duration of ${file.name}.`);
    return {
      id: uid(),
      name: file.name,
      src,
      duration: length,
      type: isAudio ? "audio" : "video",
    };
  }
  function download(blob, name) {
    const url = URL.createObjectURL(blob);
    const anchor = Object.assign(document.createElement("a"), {
      href: url,
      download: name,
    });
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  function validateProject(value) {
    if (
      !value ||
      typeof value !== "object" ||
      typeof value.name !== "string" ||
      !Number.isFinite(value.width) ||
      value.width <= 0 ||
      !Number.isFinite(value.height) ||
      value.height <= 0 ||
      !Number.isFinite(value.fps) ||
      value.fps <= 0 ||
      !Array.isArray(value.media) ||
      !Array.isArray(value.clips)
    )
      throw new Error(
        "This project file has invalid settings or timeline data.",
      );
    const mediaIds = new Set();
    for (const item of value.media) {
      if (
        !item ||
        typeof item.id !== "string" ||
        typeof item.name !== "string" ||
        typeof item.src !== "string" ||
        !["video", "audio"].includes(item.type) ||
        !Number.isFinite(item.duration) ||
        item.duration < 0 ||
        mediaIds.has(item.id)
      )
        throw new Error("This project file has invalid media.");
      mediaIds.add(item.id);
    }
    const clipIds = new Set();
    for (const clip of value.clips) {
      if (
        !clip ||
        typeof clip.id !== "string" ||
        clipIds.has(clip.id) ||
        !["video", "audio", "title"].includes(clip.type) ||
        (clip.type !== "title" && !mediaIds.has(clip.mediaId)) ||
        !Number.isFinite(clip.start) ||
        clip.start < 0 ||
        !Number.isFinite(clip.duration) ||
        clip.duration <= 0 ||
        !Number.isFinite(clip.in) ||
        clip.in < 0 ||
        !Number.isFinite(clip.track)
      )
        throw new Error("This project file has an invalid clip.");
      clipIds.add(clip.id);
    }
  }
  const Editor = {
    get: () => ({
      project,
      selectedId,
      selected: selected(),
      time,
      playing,
      mutedTracks,
      duration: duration(),
      video,
      audio,
      activeClip: at("video", time),
      activeTitle: at("title", time),
      activeAudio: at("audio", time),
    }),
    drawFrame,
    setTrackMuted(track, muted) {
      if (track in mutedTracks) {
        mutedTracks[track] = !!muted;
        syncMedia();
        notify();
      }
    },
    subscribe(fn) {
      subscribers.add(fn);
      fn(Editor.get());
      return () => subscribers.delete(fn);
    },
    seek(t) {
      time = clamp(snap(t) || 0, 0, duration());
      syncMedia(true);
      notify();
    },
    togglePlay() {
      playing = !playing;
      if (playing && time >= duration()) time = 0;
      if (!playing) time = snap(time);
      syncMedia(true);
      notify();
    },
    select(id) {
      selectedId = id;
      notify();
    },
    async addMedia(files) {
      const accepted = Array.from(files).filter(
        (f) => f.type.startsWith("video/") || f.type.startsWith("audio/"),
      );
      if (!accepted.length)
        throw new Error(
          "Choose a video or audio file supported by your browser.",
        );
      snapshot();
      for (const file of accepted) {
        const offline = project.media.find((item) =>
          window.SpliceMedia?.matches(item, file),
        );
        const decoded = await fileToMedia(file);
        const item = offline || decoded;
        if (offline) {
          item.duration = decoded.duration;
          item.src = decoded.src;
        }
        if (window.SpliceMedia) await window.SpliceMedia.remember(item, file);
        if (!offline) project.media.push(item);
        item.offline = false;
      }
      activeVideoId = null;
      syncMedia(true);
      notify();
    },
    async relinkMedia(files) {
      const matches = Array.from(files).filter((file) =>
        project.media.some((item) => SpliceMedia.matches(item, file)),
      );
      if (!matches.length)
        throw new Error(
          "None of the selected files match the offline media names.",
        );
      return Editor.addMedia(matches);
    },
    addToTimeline(id) {
      const item = project.media.find((m) => m.id === id);
      if (!item) return;
      snapshot();
      const start =
        item.type === "audio"
          ? time
          : Math.max(
              0,
              ...project.clips
                .filter((c) => c.type === "video")
                .map((c) => c.start + c.duration),
            );
      const knownLength = project.clips.find((c) => c.mediaId === id)?.duration;
      const clip = {
        id: uid(),
        mediaId: id,
        start,
        duration: item.duration || knownLength || 5,
        in: 0,
        track: item.type === "audio" ? -1 : 1,
        type: item.type,
        title: "",
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: "none",
      };
      project.clips.push(clip);
      selectedId = clip.id;
      project.duration = duration();
      notify();
    },
    updateClip(id, patch) {
      const clip = project.clips.find((c) => c.id === id);
      if (!clip) return;
      snapshot();
      Object.assign(clip, patch);
      for (const key of ["start", "duration", "in"])
        if (key in patch) clip[key] = snap(clip[key]);
      clip.start = Math.max(0, Number(clip.start) || 0);
      clip.duration = Math.max(1 / project.fps, Number(clip.duration) || 1);
      project.duration = duration();
      syncMedia(true);
      notify();
    },
    split() {
      const clip = selected() || at("video", time);
      const cut = snap(time);
      if (!clip || cut <= clip.start || cut >= clip.start + clip.duration)
        return;
      snapshot();
      const length = cut - clip.start;
      const right = {
        ...structuredClone(clip),
        id: uid(),
        start: cut,
        in: (clip.in || 0) + length,
        duration: clip.duration - length,
      };
      delete right.premiereIndex;
      project.clips.push(right);
      clip.duration = length;
      time = cut;
      notify();
    },
    deleteSelected() {
      const index = project.clips.findIndex((c) => c.id === selectedId);
      if (index < 0) return;
      snapshot();
      project.clips.splice(index, 1);
      selectedId = project.clips[0]?.id || null;
      project.duration = duration();
      syncMedia(true);
      notify();
    },
    moveClip(id, start, track) {
      Editor.updateClip(id, { start, track });
    },
    addTitle() {
      snapshot();
      const clip = {
        id: uid(),
        mediaId: null,
        start: time,
        duration: 4,
        in: 0,
        track: 2,
        type: "title",
        title: "Your title",
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: "none",
      };
      project.clips.push(clip);
      selectedId = clip.id;
      project.duration = duration();
      notify();
    },
    addTransition() {
      const clip = selected();
      if (clip && clip.type === "video")
        Editor.updateClip(clip.id, {
          transition: clip.transition === "dissolve" ? "none" : "dissolve",
        });
    },
    undo() {
      if (!history.length) return;
      future.push(structuredClone(project));
      project = history.pop();
      syncMedia(true);
      notify();
    },
    redo() {
      if (!future.length) return;
      history.push(structuredClone(project));
      project = future.pop();
      syncMedia(true);
      notify();
    },
    newProject() {
      snapshot();
      project = {
        name: "Untitled project",
        width: 1920,
        height: 1080,
        fps: 25,
        media: [],
        clips: [],
        duration: 0,
      };
      selectedId = null;
      time = 0;
      playing = false;
      mutedTracks = { audio: false, video: false, title: false };
      syncMedia(true);
      notify();
    },
    async openProject(file) {
      const name = file.name.toLowerCase();
      let imported;
      if (name.endsWith(".prproj")) {
        const form = new FormData();
        form.append("file", file);
        const response = await fetch("/api/premiere/open", {
          method: "POST",
          body: form,
        });
        if (!response.ok) throw new Error(await response.text());
        imported = (await response.json()).project;
      } else imported = JSON.parse(await file.text());
      validateProject(imported);
      if (window.SpliceMedia) await window.SpliceMedia.restore(imported);
      snapshot();
      project = imported;
      selectedId = project.clips[0]?.id || null;
      time = 0;
      playing = false;
      mutedTracks = { audio: false, video: false, title: false };
      syncMedia(true);
      notify();
    },
    async saveProject(format = "prproj") {
      const safe =
        project.name.replace(/[^a-z0-9_-]+/gi, "-").replace(/^-|-$/g, "") ||
        "project";
      if (format === "prproj") {
        const prepared = window.SpliceMedia?.forPremiere
          ? await window.SpliceMedia.forPremiere(project)
          : project;
        const response = await fetch("/api/premiere/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project: prepared }),
          signal: AbortSignal.timeout(120000),
        });
        if (!response.ok) throw new Error(await response.text());
        download(await response.blob(), `${safe}.prproj`);
      } else
        download(
          new Blob([JSON.stringify(project, null, 2)], {
            type: "application/json",
          }),
          `${safe}.splice.json`,
        );
    },
    async exportVideo() {
      if (!duration())
        throw new Error("Add clips to the timeline before exporting.");
      const prepared = window.SpliceMedia?.forPremiere
        ? await window.SpliceMedia.forPremiere(project)
        : structuredClone(project);
      prepared.renderOptions = { mutedTracks };
      const response = await fetch("/api/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: prepared }),
        signal: AbortSignal.timeout(3600000),
      });
      if (!response.ok) throw new Error(await response.text());
      download(
        await response.blob(),
        `${project.name.replace(/[^a-z0-9_-]+/gi, "-")}.mp4`,
      );
    },
  };
  window.Editor = Editor;
})();
