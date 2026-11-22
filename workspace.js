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
          const response=await fetch('/api/media/waveform?src='+encodeURIComponent(src),{signal:AbortSignal.timeout(215000)});
          if(!response.ok) return null;
          return (await response.json()).peaks;
        })().catch(() => null),
      );
    return waveformCache.get(src);
  }
  async function paintWave(canvas, media, clip) {
    if (!media?.src) return;
    const samples = await waveform(media.src);
    if (!samples?.length || !canvas.isConnected) return;
    const width = Math.max(1, Math.round(canvas.getBoundingClientRect().width));
    canvas.width = Math.min(8192,width * 2);
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
  const safeEdit = action => { try { return action(); } catch (error) { notify(error.message); return null; } };
  let pending = false;
  let relinkTarget = null;
  const thumbPending = new Set();
  function makeThumb(media) {
    if (media.thumb || !media.src || media.type !== "video" || thumbPending.has(media.id) || thumbPending.size>=3) return;
    thumbPending.add(media.id);
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "metadata";
    video.src = media.src;
    video.onloadedmetadata = () => video.currentTime = Math.min(1, video.duration / 4);
    video.onseeked = () => {
      const canvas = document.createElement("canvas");
      canvas.width = 320;
      canvas.height = 180;
      canvas.getContext("2d").drawImage(video, 0, 0, 320, 180);
      media.thumb = canvas.toDataURL("image/jpeg", .75);
      thumbPending.delete(media.id);
      video.removeAttribute("src");
      video.load();
      lastMedia="";lastProject="";refresh(editor.get());
    };
    video.onerror = () => thumbPending.delete(media.id);
  }
  let gesture;
  const busy = async (action, done) => {
    if (pending) return;
    pending = true;
    $("save-status").hidden=false; $("save-status").textContent = "Working…";
    try {
      const result = await action();
      $("save-status").textContent = "Ready"; $("save-status").hidden=true;
      if (done && result !== false) notify(done);
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
    project.media.forEach(makeThumb);
    $("asset-count").textContent = project.media.length;
    $("asset-footer").textContent = `${project.media.length} assets in project`;
    const body = $("library-body");
    document.querySelector('.pool-tools').hidden=tab!=='media';
    if(tab==='captions'){SpliceCaptions.render(body,state,notify);return;}
    if (tab === "media") {
      const previousBin=$('bin-filter').value;
      $('bin-filter').replaceChildren(new Option('All bins',''),new Option('Unfiled','@root'),
        ...[...new Set(project.media.map(m=>m.bin).filter(Boolean))].sort().map(f=>new Option(f,f)));
      if ([...$('bin-filter').options].some(o=>o.value===previousBin)) $('bin-filter').value=previousBin;
      const bin=$('bin-filter').value;
      body.innerHTML = `<div class="library-section">Imported media</div>${project.media.some((m) => m.offline || !m.src) ? '<button class="asset-add" id="relink-assets">↻ &nbsp; Relink offline media</button>' : ""}<div class="media-grid">${project.media.filter(media => { const term=$("media-search").value.trim().toLowerCase(); const filter=$("media-filter").value;return (!bin || (bin==="@root" ? !media.bin : media.bin===bin)) && (!term || (media.name+" "+(media.path||"")).toLowerCase().includes(term)) && (filter==="all"||filter===media.type||(filter==="offline"&&(media.offline||!media.src))); }).map((media) => `<button class="media-card" draggable="true" data-media="${esc(media.id)}" title="Double click to add to timeline"><div class="media-thumb ${media.type === "audio" ? "audio-thumb" : ""}">${media.type === "audio" ? "♫" : media.thumb ? `<img src="${esc(media.thumb)}" alt="">` : "▶"}<span class="media-icon">${media.type === "audio" ? "♫" : "▶"}</span><span class="media-duration">${fmt(media.duration, project.fps).slice(3, 8)}</span></div><span class="media-name">${esc(media.name)}</span><span class="media-meta">${media.offline || !media.src ? "Offline · Relink media" : `${media.type === "audio" ? "Audio" : "Video"} · ${Math.round(media.duration || 0)}s`}</span></button>`).join("")}</div><button class="asset-add" id="asset-add">＋ &nbsp; Import your own footage or audio</button>`;
      body.querySelectorAll("[data-media]").forEach((el) => {
        if (project.media.find(m => m.id === el.dataset.media)?.offline) {
          el.onclick = () => { relinkTarget = el.dataset.media; $("specific-relink-input").click(); };
        }
        if(!el.onclick) el.onclick=()=>SpliceSource.open(el.dataset.media);
        const media=project.media.find(m=>m.id===el.dataset.media);
        if(Number.isInteger(media?.nestedSequence)){el.onclick=()=>busy(()=>SpliceSession.sequence(media.nestedSequence));el.title="Open nested sequence";}
        else el.title=media?.offline?"Relink this source":"Open source monitor";
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
    if ($("add-title")) $("add-title").onclick = () => safeEdit(() => editor.addTitle());
  }
  const group = new Set();
  let clipboard=[];
  let snapEnabled = true;
  let rippleEnabled = false;
  function snapTime(value, ignoredId) {
    const time = Math.max(0, value);
    if (!snapEnabled) return time;
    const state = editor.get();
    const edges = [0, state.time, ...state.project.clips.filter(c=>c.id!==ignoredId).flatMap(c=>[c.start,c.start+c.duration])];
    let nearest=time, distance=8/(60*zoom);
    for (const edge of edges) {
      if (Math.abs(edge-time) < distance) {distance=Math.abs(edge-time);nearest=edge;}
    }
    return nearest;
  }
  function positionTime(event, element) {
    return Math.max(0, (event.clientX-element.getBoundingClientRect().left)/(60*zoom));
  }
  let viewStart=0, viewEnd=Infinity;
  function renderTimeline(state) {
    const {project,duration,selectedId}=state;
    const highestVideo = Math.max(1, ...project.clips.filter(c=>c.type==="video").map(c=>c.track));
    const lowestAudio = Math.min(-1, ...project.clips.filter(c=>c.type==="audio").map(c=>c.track));
    const visibleLanes=[{kind:'title',track:4,name:'Titles'},
      ...Array.from({length:Math.min(64,Math.max(2,project.trackCounts?.video||3,highestVideo+1))},(_,n)=>({kind:'video',track:n+1,name:'Video '+(n+1)})).reverse(),
      ...Array.from({length:Math.min(64,Math.max(2,project.trackCounts?.audio||3,-lowestAudio+1))},(_,n)=>({kind:'audio',track:-n-1,name:'Audio '+(n+1)}))];
    const scroll=$("track-scroll"), screen=Math.max(300,scroll.clientWidth);
    const width=Math.max(screen,px(Math.max(38,duration+4)));
    viewStart=Math.max(0,scroll.scrollLeft-screen);viewEnd=Math.min(width,scroll.scrollLeft+2*screen);
    const visible=(start,end)=>px(start)<=viewEnd && px(end)>=viewStart;
    $("timeline-surface").style.width=width+"px";
    $("timeline-surface").style.height=(31+visibleLanes.length*64)+"px";
    const ruler=$("time-ruler");ruler.innerHTML="";
    const step=zoom<.7?5:zoom>1.5?1:2;
    ruler.style.backgroundSize=px(step)+"px 100%";
    for(let t=Math.floor(viewStart/px(step))*step;t<=Math.ceil(viewEnd/(60*zoom));t+=step){
      const tick=document.createElement("span");tick.className="ruler-tick";
      tick.style.left=px(t)+5+"px";tick.textContent=fmt(t,project.fps).slice(3,8);ruler.append(tick);
    }
    for(const marker of project.markers||[]){
      if(!visible(marker.time,marker.time))continue;
      const flag=document.createElement('button');flag.className='edit-marker';flag.style.left=px(marker.time)+'px';
      flag.title=marker.name+' · '+fmt(marker.time,project.fps);flag.setAttribute('aria-label',flag.title);
      flag.onpointerdown=e=>{e.stopPropagation();editor.seek(marker.time);};flag.ondblclick=()=>editMarker(marker);ruler.append(flag);
    }
    $("track-heads").querySelectorAll(".track-head").forEach(node=>node.remove());
    const lanes=$("track-lanes");lanes.replaceChildren();
    for(const lane of visibleLanes){
      const head=document.createElement("div");head.className="track-head";
      window.SpliceTracks.renderHeader(head,lane,project,editor);
      $("track-heads").append(head);
      const surface=document.createElement("div");surface.className="track-lane "+lane.kind+"-lane";
      surface.dataset.track=lane.track;surface.dataset.kind=lane.kind;
      surface.classList.toggle('is-locked', editor.isTrackLocked(lane.track,lane.kind));
      lanes.append(surface);
      for(const clip of project.clips.filter(c=>(c.track===lane.track || lane.kind==="title") && c.type===lane.kind && visible(c.start,c.start+c.duration))){
        const media=mediaFor(project,clip),node=document.createElement("button");
        node.className="timeline-clip "+lane.kind+(clip.id===selectedId?" selected":"")+(group.has(clip.id)?" group-selected":"");
        node.style.left=px(clip.start)+"px";node.style.width=Math.max(22,px(clip.duration)-2)+"px";
        node.dataset.clip=clip.id;node.title=(clip.title||media?.name||"Title")+" · "+fmt(clip.start,project.fps);
        node.innerHTML=(media?.thumb?'<img class="clip-image" src="'+esc(media.thumb)+'" alt="">':"")+
          '<span class="clip-overlay"></span>'+(lane.kind==="audio"?'<canvas class="clip-wave"></canvas>':"")+
          '<span class="clip-name">'+esc(clip.title||media?.name||"Title")+'</span>'+
          '<span class="trim-handle trim-left" title="Trim in"></span><span class="trim-handle trim-right" title="Trim out"></span>';
        if (Number.isInteger(clip.nestedSequence)) {
          node.title += ' · Double-click to open nested sequence';
          node.ondblclick=()=>busy(()=>SpliceSession.sequence(clip.nestedSequence));
        }
        surface.append(node);
        if(lane.kind==="audio")paintWave(node.querySelector("canvas"),media,clip);
      }
    }
  }
  function renderPreview(state) {
    const { project, time, duration, playing, activeClip, activeTitle } = state;
    SpliceCaptions.paint(state);
    const fps = project.fps || 30;
    $("viewer-time").textContent = fmt(time, fps);
    $("header-time").textContent = fmt(time, fps);
    $("viewer-duration").textContent = `/ ${fmt(duration, fps)}`;
    if (playing !== lastPlay) $("play-toggle").innerHTML = playing
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>'
      : svg(icons["play-toggle"]);
    $("playhead").style.left = `${px(time)}px`;
    $("preview-empty").style.display =
      activeClip || activeTitle ? "none" : "flex";
  }
  let lastPaint = "", lastScope = -Infinity, monitorState;
  for (const event of ['loadeddata','seeked','canplay','emptied'])
    document.addEventListener(event,()=>{lastPaint="";},true);
  function paintPreview(now) {
    const canvas = $("preview-canvas"), state = monitorState;
    if (!state) { requestAnimationFrame(paintPreview); return; }
    const video = state.video;
    const paintKey = [state.revision, state.time, state.selectedId, video?.currentSrc,
      video?.readyState, video?.currentTime, video?.seeking, canvas.width, canvas.height].join(":");
    if (state.playing || paintKey !== lastPaint) {
      editor.drawFrame(canvas);
      lastPaint = paintKey;
    }
    if (now-lastScope >= 100) {
      window.SpliceInspector?.paintScope?.();
      window.SpliceInspector?.paintMeters?.(state);
      lastScope=now;
    }
    requestAnimationFrame(paintPreview);
  }
  requestAnimationFrame(paintPreview);
  let lastPlay, lastFrame, lastOverallKey;
  function refresh(state) {
    monitorState = state;
    const fraction=Math.round(state.time*state.project.fps);
    if(fraction!==lastFrame || state.playing!==lastPlay || state.revision!==lastOverallKey){
      renderPreview(state);
      lastFrame=fraction;lastPlay=state.playing;lastOverallKey=state.revision;
    }
    const signature = JSON.stringify({
      revision: state.revision,
      name: state.project.name,
      zoom,
      selectedId: state.selectedId,
    });
    const projectChanged = signature !== lastProject;
    if (projectChanged && !gesture) {
      renderTimeline(state);
      lastProject = signature;
    }
    document
      .querySelectorAll(".track-mute")
      .forEach((b) =>
        b.classList.toggle("off", !!state.mutedTracks?.[b.dataset.track]),
      );
    const mediaSignature = state.revision + tab + $("bin-filter").value + $("media-search").value + $("media-filter").value;
    if (mediaSignature !== lastMedia) {
      renderLibrary(state);
      lastMedia = mediaSignature;
    }
    if (
      state.selectedId !== lastSelection ||
      (projectChanged &&
        !document.activeElement?.closest?.("#inspector-content"))
    ) {
      window.SpliceInspector?.render(state, $("inspector-content"), editor);
      lastSelection = state.selectedId;
    }
    if (!projectChanged) return;
    const issueCount=SpliceDialogs.compatibility(state.project).length;
    $('compatibility').textContent=issueCount?'Project compatibility ('+issueCount+')…':'Project compatibility…';
    const sequences=state.project.premiereSequences||[], selector=$('sequence-select');
    selector.hidden=sequences.length<2;
    if(!selector.hidden){
      selector.replaceChildren(...sequences.map((name,i)=>new Option(name,String(i))));
      selector.value=String(state.project.premiereSequence);
    }
    $('size-label').textContent=state.project.width+' × '+state.project.height;
    const aspect=state.project.width/state.project.height;
    const canvas=$('preview-canvas'), height=Math.min(2048,Math.max(1,Math.round(960/aspect)));
    if(canvas.width!==960 || canvas.height!==height){canvas.width=960; canvas.height=height; lastPaint='';}
    sizeMonitor();
    $("project-name").innerHTML =
      `${esc(state.project.name)} <span class="chevron">⌄</span>`;
    document.querySelector(".sequence-name").textContent =
      `${state.project.name} — Main Edit`;
    document.querySelector(".timeline-project").textContent =
      `${state.project.name} — Main Edit`;
    const sequenceNumber=String((state.project.premiereSequence||0)+1).padStart(2,'0');
    document.querySelector('.sequence-label').textContent='SEQUENCE '+sequenceNumber;
    $("fps-label").textContent = `${Number((state.project.fps || 30).toFixed(3))} FPS`;
    $("timeline-status").textContent =
      `${state.project.clips.length} clips · ${fmt(state.duration, state.project.fps)}`;
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
    const ratio = editor.get().project.width/editor.get().project.height;
    const width = Math.max(
      1,
      Math.min(area.clientWidth - 24, (area.clientHeight - 24) * ratio),
    );
    frame.style.width = `${width}px`;
    frame.style.height = `${width / ratio}px`;
  }
  new ResizeObserver(sizeMonitor).observe(
    document.querySelector(".monitor-area"),
  );
  editor.subscribe(refresh);
  $("specific-relink-input").onchange = e => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file && relinkTarget) busy(() => editor.relinkMediaItem(relinkTarget, file), "Media relinked");
    relinkTarget = null;
  };
  $("media-search").addEventListener("input",()=>refresh(editor.get()));
  $("bin-filter").addEventListener("change",()=>refresh(editor.get()));
  $("media-filter").addEventListener("change",()=>refresh(editor.get()));
  window.addEventListener("resize",()=>{lastProject="";refresh(editor.get());});
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
  $("open-project").onclick = () => window.SpliceSession?.open?.() ?? $("project-input").click();
  $("project-input").onchange = (e) => {
    const file = e.target.files[0];
    if (file) busy(() => window.SpliceSession?.openFile?.(file) ?? editor.openProject(file), "Project opened");
    e.target.value = "";
  };
  $("save-project").onclick = () =>
    busy(() => window.SpliceSession?.save?.("prproj") ?? editor.saveProject("prproj"), "Project saved");
  $("export-video").onclick=()=>SpliceDialogs.exportVideo();
  $("new-project").onclick=()=>SpliceDialogs.newSequence();
  $("shortcuts").onclick=()=>SpliceDialogs.shortcuts();
  $("save-working").onclick=()=>busy(()=>SpliceSession.saveAs("splice"));
  document.querySelector(".file-menu").addEventListener("click",e=>{if(e.target.closest("button"))document.querySelector(".file-menu").open=false;});
  $("add-marker").onclick=()=>editMarker();
  function editMarker(existing){
    const name=prompt('Marker name (leave blank to remove)',existing?.name||'Marker');
    if(name===null)return;
    editor.editProject(p=>{
      p.markers ||= [];
      if(existing) p.markers = p.markers.filter(m=>m.id!==existing.id);
      if(name.trim())p.markers.push({...existing,id:existing?.id||crypto.randomUUID(),name:name.trim(),time:existing?.time??editor.get().time});
    });
  }
  $("project-name").onclick = () => {
    const name = prompt("Project name", editor.get().project.name);
    if (name?.trim()) {
      editor.renameProject?.(name.trim());
    }
  };
  $("play-toggle").onclick = () => editor.togglePlay();
  $("jump-start").onclick = () => editor.seek(0);
  $("jump-end").onclick = () => editor.seek(editor.get().duration);
  $("step-back").onclick = () =>
    editor.seek(editor.get().time - 1 / editor.get().project.fps);
  $("step-forward").onclick = () =>
    editor.seek(editor.get().time + 1 / editor.get().project.fps);
  $("split").onclick = () => safeEdit(() => editor.split());
  $("delete-selected").onclick = () => safeEdit(() => editor.deleteSelected());
  $("undo").onclick = () => editor.undo();
  $("redo").onclick = () => editor.redo();
  $("reset-clip").onclick = () => {
    const c = editor.get().selected;
    if (c)
      safeEdit(() => editor.updateClip(c.id, {
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: "none",
      }));
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
  let scrubbing=false;
  $("time-ruler").addEventListener("pointerdown",e=>{scrubbing=true;$("time-ruler").setPointerCapture(e.pointerId);editor.seek(positionTime(e,$("time-ruler")));});
  $("time-ruler").addEventListener("pointermove",e=>{if(scrubbing)editor.seek(positionTime(e,$("time-ruler")));});
  $("time-ruler").addEventListener("pointerup",()=>scrubbing=false);
  const lanes=$("track-lanes");
  let scrollFrame=0;
  $("track-scroll").addEventListener("scroll",()=>{
    const surface=$("track-scroll");$("track-heads").scrollTop=surface.scrollTop;
    if(scrollFrame||gesture)return;
    scrollFrame=requestAnimationFrame(()=>{
      scrollFrame=0;
      if((surface.scrollLeft<viewStart+surface.clientWidth/4 && viewStart>0) ||
         (surface.scrollLeft+1.25*surface.clientWidth>viewEnd && viewEnd<surface.scrollWidth))
        renderTimeline(editor.get());
    });
  });
  lanes.addEventListener("pointerdown",e=>{
    const node=e.target.closest(".timeline-clip");if(!node)return;
    const clip=editor.get().project.clips.find(c=>c.id===node.dataset.clip);if(!clip)return;
    if(e.ctrlKey||e.metaKey){
      if(!group.size&&editor.get().selectedId)group.add(editor.get().selectedId);
      if(group.has(clip.id))group.delete(clip.id);else group.add(clip.id);
      lastProject="";refresh(editor.get());e.preventDefault();return;
    }
    const edge=e.target.closest(".trim-handle");
    if (!group.has(clip.id) || edge || e.altKey) group.clear();
    const originals=editor.get().project.clips.filter(c=>group.has(c.id)).map(c=>({id:c.id,start:c.start,track:c.track,type:c.type}));
    if ([clip,...originals].some(item=>editor.isTrackLocked(item.track,item.type))) {
      notify('Unlock the track before editing.'); return;
    }
    gesture={id:clip.id,originals,original:structuredClone(clip),x:e.clientX,
      mode:edge?.classList.contains("trim-left")?"left":edge?"right":e.altKey?"slip":"move",
      track:clip.track};
    editor.beginGesture?.();
    node.setPointerCapture(e.pointerId);
    editor.select(clip.id);
    node.classList.add("selected");
    e.preventDefault();
  });
  window.addEventListener("pointermove",e=>{
    if(!gesture)return;
    const clip=editor.get().project.clips.find(c=>c.id===gesture.id);if(!clip)return;
    const delta=(e.clientX-gesture.x)/(60*zoom);
    const start=gesture.original.start,end=start+gesture.original.duration;
    if(gesture.mode==="move"){
      const lane=document.elementFromPoint(e.clientX,e.clientY)?.closest(".track-lane");
      const track=lane && lane.dataset.kind===clip.type?Number(lane.dataset.track):gesture.track;
      const members=gesture.originals.length?gesture.originals:[gesture.original];
      const offset=Math.max(snapTime(start+delta,clip.id)-start,-Math.min(...members.map(c=>c.start)));
      for(const original of members){
        const dest=original.type!==clip.type||clip.type==='title'?original.track:
            original.type==='audio'?Math.min(-1,original.track+track-gesture.track):Math.max(1,original.track+track-gesture.track);
        if (editor.isTrackLocked(dest,original.type)) continue;
        safeEdit(()=>editor.moveClip(original.id,original.start+offset,dest));
        const actual=editor.get().project.clips.find(c=>c.id===original.id);
        const node=lanes.querySelector('[data-clip="'+CSS.escape(original.id)+'"]');
        const target=lanes.querySelector('[data-kind="'+original.type+'"][data-track="'+dest+'"]');
        if(node){node.style.left=px(actual.start)+'px';if(target&&node.parentElement!==target)target.append(node);}
      }
    } else if(gesture.mode==="slip") { safeEdit(()=>editor.slipClip?.(clip.id,delta-(gesture.lastDelta||0))); gesture.lastDelta=delta; }
    else {
      const boundary=snapTime((gesture.mode==="left"?start:end)+delta,clip.id);
      if(e.shiftKey){
        const neighbour=editor.get().project.clips.find(c=>c.id!==clip.id&&c.track===clip.track&&
          (gesture.mode==="left"?Math.abs(c.start+c.duration-start)<.05:Math.abs(c.start-end)<.05));
        if(neighbour){if(gesture.mode==="left")safeEdit(()=>editor.rollEdit?.(neighbour.id,clip.id,boundary));
          else safeEdit(()=>editor.rollEdit?.(clip.id,neighbour.id,boundary));}
        else safeEdit(()=>editor.trimEdge?.(clip.id,gesture.mode==="left"?"start":"end",boundary,{ripple:rippleEnabled}));
      }else safeEdit(()=>editor.trimEdge?.(clip.id,gesture.mode==="left"?"start":"end",boundary,{ripple:rippleEnabled}));
      const node=lanes.querySelector('[data-clip="'+clip.id+'"]');
      if(node){node.style.left=px(clip.start)+"px";node.style.width=Math.max(22,px(clip.duration)-2)+"px";}
    }
  });
  window.addEventListener("pointerup",()=>{if(gesture){editor.endGesture?.();gesture=null;lastProject="";refresh(editor.get());}});
  window.addEventListener("pointercancel",()=>{editor.endGesture?.();gesture=null;lastProject="";refresh(editor.get());});
  lanes.addEventListener("click",e=>{if(e.target.classList.contains("track-lane"))editor.seek(positionTime(e,e.target));});
  lanes.addEventListener("dragover",e=>{if(e.target.closest(".track-lane"))e.preventDefault();});
  lanes.addEventListener("drop",e=>{
    const lane=e.target.closest(".track-lane");if(!lane)return;e.preventDefault();
    const id=e.dataTransfer.getData("text/plain");
    const item=editor.get().project.media.find(m=>m.id===id);
    if(!item||item.type!==lane.dataset.kind)return notify("Choose a "+lane.dataset.kind+" lane.");
    if (editor.isTrackLocked(Number(lane.dataset.track),lane.dataset.kind)) return notify('Unlock the track before editing.');
    safeEdit(()=>{editor.seek(snapTime(positionTime(e,lane)));editor.addToTimeline(id);
      editor.moveClip(editor.get().selectedId,editor.get().time,Number(lane.dataset.track));});
  });
  $("snap-toggle").onclick=()=>{snapEnabled=!snapEnabled;$("snap-toggle").classList.toggle("active",snapEnabled)};
  $("ripple-toggle").onclick=()=>{rippleEnabled=!rippleEnabled;$("ripple-toggle").classList.toggle("active",rippleEnabled)};
  $("compatibility").onclick=()=>SpliceDialogs.projectReport();
  $("versions").onclick=()=>busy(()=>SpliceSession.history());
  $("save-as").onclick=()=>busy(()=>SpliceSession.saveAs("prproj"));
  $("sequence-select").onchange=e=>busy(()=>SpliceSession.sequence(Number(e.target.value)));
  $("fullscreen-monitor").onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else $("monitor-frame").requestFullscreen?.();
  };
  document.addEventListener("keydown", (e) => {
    if (document.querySelector('dialog[open]')) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (e.shiftKey)
        busy(() => window.SpliceSession?.save?.("splice") ?? editor.saveProject("splice"), "Splice project saved");
      else $("save-project").click();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "o") {
      e.preventDefault();
      $("open-project").click();
      return;
    }
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
    const selected = editor.get().project.clips.filter(c=>group.size?group.has(c.id):c.id===editor.get().selectedId);
    const key=e.key.toLowerCase();
    if((e.metaKey||e.ctrlKey)&&key==="c"){e.preventDefault();clipboard=structuredClone(selected);return;}
    if((e.metaKey||e.ctrlKey)&&key==="v"&&clipboard.length){e.preventDefault();const t=editor.get().time, first=Math.min(...clipboard.map(c=>c.start));safeEdit(()=>editor.editProject(p=>{
      for(const original of clipboard){if(original.mediaId&&!p.media.some(m=>m.id===original.mediaId))throw Error("Paste into a project containing the source media.");
        const c=structuredClone(original);c.start=t+c.start-first;c.id=crypto.randomUUID();delete c.premiereIndex;delete c.premiereTrack;p.clips.push(c);}
    }));return;}
    if(e.key==="Delete"&&group.size){safeEdit(()=>editor.editProject(p=>{p.clips=p.clips.filter(c=>!group.has(c.id))}));group.clear();return;}
    if(key==="m"&&!e.ctrlKey&&!e.metaKey){e.preventDefault();editMarker();return;}
    if(e.key==="["||e.key==="]"){e.preventDefault();const s=editor.get();const times=(s.project.markers||[]).map(m=>m.time);editor.seek(e.key==="]"?Math.min(s.duration,...times.filter(x=>x>s.time+.001)):Math.max(0,...times.filter(x=>x<s.time-.001)));return;}
    if(e.key==="ArrowLeft"||e.key==="ArrowRight"){e.preventDefault();editor.seek(editor.get().time+(e.key==="ArrowRight"?1:-1)/editor.get().project.fps);return;}
    if (e.key.toLowerCase()==="j"){e.preventDefault();editor.setRate?.(-1);return;}
    if (e.key.toLowerCase()==="k"){e.preventDefault();editor.setRate?.(0);return;}
    if (e.key.toLowerCase()==="l"){e.preventDefault();editor.setRate?.(1);return;}
    if (e.key.toLowerCase()==="n"){e.preventDefault();snapEnabled=!snapEnabled;$("snap-toggle").classList.toggle("active",snapEnabled);return;}
    if (e.key.toLowerCase()==="r"){e.preventDefault();rippleEnabled=!rippleEnabled;$("ripple-toggle").classList.toggle("active",rippleEnabled);return;}
    if (e.code === "Space") {
      e.preventDefault();
      editor.togglePlay();
    }
    if (e.key.toLowerCase() === "s" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      safeEdit(()=>editor.split());
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      e.shiftKey ? editor.redo() : editor.undo();
    }
    if (e.key === "Delete") safeEdit(()=>editor.deleteSelected());
  });
})();
