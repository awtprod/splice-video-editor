(() => {
  const editor = window.Editor;
  const $ = (id) => document.getElementById(id);
  const fmt = editor.formatTimecode;
  // Sequence positions read in sequence timecode (start offset, drop-frame where it applies).
  const tc = (seconds) => editor.sequenceTimecode(seconds);
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
  // Timeline scale in pixels per second; continuous from whole-timeline fit to many pixels per frame.
  let pps = 33;
  let lastProject = "";
  let lastSelection = null;
  let lastMedia = "";
  let toastTimer;
  const px = (seconds) => seconds * pps;
  const waveformCache = new Map();
  async function waveform(src, stream = null) {
    const key = JSON.stringify([src, stream]);
    if (!waveformCache.has(key)) {
      const query = new URLSearchParams({src});
      if (Number.isInteger(stream)) query.set("stream", stream);
      waveformCache.set(
        key,
        (async () => {
          const response=await fetch('/api/media/waveform?'+query,{signal:AbortSignal.timeout(215000)});
          if(!response.ok) return null;
          return (await response.json()).peaks;
        })().catch(() => null),
      );
    }
    return waveformCache.get(key);
  }
  // `width` is the clip's on-screen width, known from the zoom; measuring the canvas here would
  // force a layout for every audio clip after each re-render.
  async function paintWave(canvas, media, clip, width) {
    if (!media?.src) return;
    let src=media.src, chosen=null;
    const stream='audioStream' in clip?clip.audioStream:media.audioStream??null;
    if(Number.isInteger(stream)) {
      // The server reads the chosen stream straight from the source file.
      src=media.originalSrc||media.src;
      chosen=stream;
    } else if(stream!==(media.audioStream??null)) {
      const preview=SpliceMedia.preparePreview(media,stream);
      await preview?.done;
      src=preview?.src;
    }
    if (!src || !canvas.isConnected) return;
    const samples = await waveform(src, chosen);
    if (!samples?.length || !canvas.isConnected) return;
    canvas.width = Math.min(8192, Math.round(Math.max(1, width) * Math.min(2, window.devicePixelRatio || 1)));
    canvas.height = 92;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#a3eab9";
    // Every bar goes into one path and is filled once.
    const bars = new Path2D();
    const duration = media.duration || clip.duration;
    const samplesPerSecond = samples.length / duration;
    // The clip plays clip.duration * speed seconds of source, backwards when reversed.
    const sourceSpan = clip.duration * (Number(clip.speed) || 1);
    const columnSpan = (sourceSpan / canvas.width) * 4 * samplesPerSecond;
    for (let x = 1; x < canvas.width; x += 4) {
      const offset = (clip.reverse ? 1 - x / canvas.width : x / canvas.width) * sourceSpan;
      const first = Math.max(0, Math.floor(((clip.in || 0) + offset) * samplesPerSecond - (clip.reverse ? columnSpan : 0)));
      const last = Math.min(samples.length, first + Math.max(1, Math.ceil(columnSpan)));
      let peak = 0;
      for (
        let i = first;
        i < last;
        i += Math.max(1, Math.floor((last - first) / 20))
      )
        peak = Math.max(peak, Math.abs(samples[i]));
      const h = Math.max(2, peak * canvas.height * 0.82);
      bars.rect(x, (canvas.height - h) / 2, 2, h);
    }
    ctx.fill(bars);
  }
  const typeOf = (clip) => clip.type;
  const notify = (message) => {
    document.querySelector(".toast")?.remove();
    const node = document.createElement("div");
    node.className = "toast";
    node.setAttribute("role", "status");
    node.textContent = message;
    document.body.append(node);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.remove(), 3500);
  };
  const safeEdit = action => { try { return action(); } catch (error) { notify(error.message); return null; } };
  let pending = false;
  let relinkTarget = null;
  const thumbPending = new Set();
  // Generated thumbnails live here, keyed by source, instead of being written onto media
  // objects that earlier undo states share.
  const generatedThumbs = new Map();
  const thumbOf = (media) => media?.thumb || generatedThumbs.get(media?.src) || "";
  function makeThumb(media) {
    if (thumbOf(media) || !media.src || media.type !== "video" || thumbPending.has(media.id) || thumbPending.size>=3) return;
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
      generatedThumbs.set(media.src, canvas.toDataURL("image/jpeg", .75));
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
  function mediaLabel(media) {
    if(Number.isInteger(media.nestedSequence))return media.proxyState==='preparing'?'Preparing nested preview':media.proxyState==='unavailable'?'Nested preview unavailable':'Nested sequence';
    if(Number.isInteger(media.nativeGenerator))return [1112293707,1414680150].includes(media.nativeGenerator)?'Premiere generator':'Native generator · Premiere preview';
    if(media.nativeUnsupported)return 'Native source · Premiere preview';
    if(media.offline || !media.src)return 'Offline · Relink media';
    return (media.type==='audio'?'Audio':'Video')+' · '+Math.round(media.duration||0)+'s'+(media.originalSrc && media.src!==media.originalSrc?' · Proxy':'');
  }
  let mediaView = "grid";
  let mediaSort = {key: "name", direction: 1};
  // Optional source metadata (camera rate, timecode, reel...) appears only when the media carries it.
  const mediaRate = (media) => {
    const [numerator, denominator] = String(media.rate || "").split("/").map(Number);
    const rate = denominator ? numerator / denominator : numerator || media.fps;
    return Number.isFinite(rate) && rate > 0 ? Number(rate.toFixed(3)) + (media.vfr ? " VFR" : "") : "";
  };
  const mediaTimecode = (media) => typeof media.timecode === "string" ? media.timecode : "";
  const mediaAudio = (media) => !Number.isInteger(media.audioStreams) ? "" : media.audioStreams === 0 ? "No audio" :
    media.audioStreams + (media.audioStreams === 1 ? " audio stream" : " audio streams");
  const mediaColumns = [
    {key: "name", label: "Name", value: (media) => media.name},
    {key: "duration", label: "Duration", value: (media) => media.duration || 0, show: (media, project) => fmt(media.duration, project.fps)},
    {key: "rate", label: "Rate", value: mediaRate},
    {key: "timecode", label: "Start TC", value: mediaTimecode},
    {key: "reel", label: "Reel", value: (media) => media.reel || ""},
    {key: "audio", label: "Audio", value: (media) => media.audioStreams ?? -1, show: mediaAudio},
    {key: "bin", label: "Bin", value: (media) => media.bin || ""},
  ];
  function mediaMatches(media, project) {
    const term = $("media-search").value.trim().toLowerCase();
    const filter = $("media-filter").value, bin = $("bin-filter").value;
    if (bin && (bin === "@root" ? media.bin : media.bin !== bin)) return false;
    if (filter !== "all" && filter !== media.type && !(filter === "offline" && media.offline)) return false;
    const text = [media.name, media.bin, media.path, media.sourcePath, media.reel, mediaTimecode(media)].filter(Boolean).join(" ");
    return !term || text.toLowerCase().includes(term);
  }
  function sortedMedia(items, project) {
    const column = mediaColumns.find((entry) => entry.key === mediaSort.key) || mediaColumns[0];
    const compare = (a, b) => {
      const left = column.value(a, project), right = column.value(b, project);
      return typeof left === "number" && typeof right === "number" ? left - right :
        String(left).localeCompare(String(right), undefined, {numeric: true, sensitivity: "base"});
    };
    return [...items].sort((a, b) => compare(a, b) * mediaSort.direction);
  }
  function mediaDetails(media, withPath = true) {
    return [mediaRate(media) && mediaRate(media) + " fps", mediaTimecode(media) && "TC " + mediaTimecode(media),
      media.reel && "Reel " + media.reel, mediaAudio(media), withPath && media.sourcePath].filter(Boolean).join(" · ");
  }
  // A card's linked-media path is one line, cut short with an ellipsis; the tooltip has it in full.
  function mediaCard(media, project) {
    const audio = media.type === "audio";
    const picture = audio ? "♫" : thumbOf(media) ? `<img src="${esc(thumbOf(media))}" alt="">` : "▶";
    const details = mediaDetails(media, false);
    const tooltip = [media.name, mediaDetails(media)].filter(Boolean).join("\n");
    const extra = details ? `<span class="media-meta media-extra">${esc(details)}</span>` : "";
    const path = media.sourcePath ? `<span class="media-meta media-path" title="${esc(media.sourcePath)}">${esc(media.sourcePath)}</span>` : "";
    return `<button class="media-card" draggable="true" data-media="${esc(media.id)}" title="${esc(tooltip)}"><div class="media-thumb ${audio ? "audio-thumb" : ""}">${picture}<span class="media-icon">${audio ? "♫" : "▶"}</span><span class="media-duration">${fmt(media.duration, project.fps).slice(3, 8)}</span></div><span class="media-name">${esc(media.name)}</span><span class="media-meta">${esc(mediaLabel(media))}</span>${extra}${path}</button>`;
  }
  function mediaTable(items, project) {
    const arrow = (key) => mediaSort.key === key ? (mediaSort.direction > 0 ? " ▲" : " ▼") : "";
    const head = mediaColumns.map((column) => `<button type="button" role="columnheader" data-sort="${column.key}" aria-sort="${mediaSort.key === column.key ? (mediaSort.direction > 0 ? "ascending" : "descending") : "none"}">${column.label}${arrow(column.key)}</button>`).join("");
    const rows = items.map((media) => `<button class="media-row" role="row" draggable="true" data-media="${esc(media.id)}" title="${esc(media.sourcePath || media.path || media.name)}">` +
      mediaColumns.map((column) => `<span role="cell">${esc(column.show ? column.show(media, project) : column.value(media, project))}</span>`).join("") + "</button>").join("");
    return `<div class="media-table" role="table" aria-label="Media list"><div class="media-table-head" role="row">${head}</div>${rows}</div>`;
  }
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
      const shown = sortedMedia(project.media.filter(media => mediaMatches(media, project)), project);
      const offline = project.media.some(m=>m.offline) ?
        '<button class="asset-add" id="relink-assets">↻ &nbsp; Relink offline media</button><button class="asset-add" id="relink-folder">Relink from folder…</button>' : "";
      const items = mediaView === "list" ? mediaTable(shown, project) :
        '<div class="media-grid">' + shown.map(media => mediaCard(media, project)).join("") + '</div>';
      body.innerHTML = '<div class="library-section">Imported media</div>' + offline + items +
        '<button class="asset-add" id="asset-add">＋ &nbsp; Import your own footage or audio</button>';
      body.querySelectorAll("[data-sort]").forEach(button => {
        button.onclick = () => {
          mediaSort = {key: button.dataset.sort, direction: mediaSort.key === button.dataset.sort ? -mediaSort.direction : 1};
          lastMedia = "";
          renderLibrary(editor.get());
        };
      });
      body.querySelectorAll("[data-media]").forEach((el) => {
        if (project.media.find(m => m.id === el.dataset.media)?.offline) {
          el.onclick = () => { relinkTarget = el.dataset.media; $("specific-relink-input").click(); };
        }
        if(!el.onclick) el.onclick=()=>SpliceSource.open(el.dataset.media);
        const media=project.media.find(m=>m.id===el.dataset.media);
        // The tooltip says what a click does, then the card's details (its full path included).
        let action=media?.offline?"Relink this source":"Open source monitor";
        if(Number.isInteger(media?.nestedSequence)){el.onclick=()=>busy(()=>SpliceSession.sequence(media.nestedSequence));action="Open nested sequence";}
        el.title=action+(el.title?"\n"+el.title:"");
        el.ondragstart = (e) =>
          e.dataTransfer.setData("text/plain", el.dataset.media);
      });
      $("asset-add").onclick = () => $("media-input").click();
      if ($("relink-folder")) $("relink-folder").onclick=()=>$("folder-relink-input").click();
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
  const withLinked = clips => {
    const all=editor.get().project.clips;
    const ids=new Set(clips.map(clip=>clip.id));
    if(editor.get().linkedSelection)for(const clip of clips)if(clip.linkGroup)
      for(const member of all.filter(item=>item.linkGroup===clip.linkGroup))ids.add(member.id);
    return all.filter(clip=>ids.has(clip.id));
  };
  let snapEnabled = true;
  let rippleEnabled = false;
  function toggleRipple() {
    rippleEnabled=!rippleEnabled;
    $("ripple-toggle").classList.toggle("active",rippleEnabled);
    $("delete-selected").title=rippleEnabled?"Ripple delete selected clip":"Delete selected clip";
  }
  function snapTime(value, ignoredId) {
    const time = Math.max(0, value);
    if (!snapEnabled) return time;
    const state = editor.get();
    const edges = [0, state.time, ...state.project.clips.filter(c=>c.id!==ignoredId).flatMap(c=>[c.start,c.start+c.duration])];
    let nearest=time, distance=8/pps;
    for (const edge of edges) {
      if (Math.abs(edge-time) < distance) {distance=Math.abs(edge-time);nearest=edge;}
    }
    return nearest;
  }
  function positionTime(event, element) {
    return Math.max(0, (event.clientX-element.getBoundingClientRect().left)/pps);
  }
  let viewStart=0, viewEnd=Infinity, renderedWidth=0;
  let selectedEdit=null;
  // Timeline viewport size and scroll, cached from observers and scroll events so a re-render
  // after every edit never has to force a synchronous layout to read them.
  const viewport={width:0,height:0,left:0,top:0};
  const timelineWidth=()=>Math.max(300,viewport.width||$("track-scroll").clientWidth);
  function scrollTimelineTo(left) {
    const scroll=$("track-scroll");
    scroll.scrollLeft=Math.max(0,left);
    viewport.left=scroll.scrollLeft;
  }
  // Ruler spacing: the smallest step that leaves room for a full timecode label.
  // Frame steps divide the timecode rate, so every second's ticks are evenly spaced.
  function rulerStep(fps) {
    const nominal=Math.round(fps);
    const frameSteps=[];
    for(let frames=1;frames<nominal;frames++)if(nominal%frames===0)frameSteps.push(frames/fps);
    const steps=[...frameSteps,1,2,5,10,15,30,60,120,300,600,900,1800,3600,7200];
    return steps.find(step=>px(step)>=96) || steps.at(-1);
  }
  const surfaceWidth = (duration, screen) => Math.max(screen, px(duration) + Math.max(120, screen * .25));
  function zoomLimits(state = editor.get()) {
    const screen=timelineWidth();
    const span=Math.max(1,state.duration);
    const fit=(screen-24)/span;
    // Up to forty pixels per frame, within the widest element a browser lays out reliably.
    const finest=Math.max(fit,Math.min(state.project.fps*40,15e6/(span+60)));
    return {fit,min:Math.min(fit,20),max:finest};
  }
  function describeSpan(seconds) {
    if (seconds < 60) return seconds.toFixed(seconds < 10 ? 1 : 0) + " s";
    const minutes=Math.floor(seconds/60);
    if (minutes < 60) return minutes + " min";
    return Math.floor(minutes/60) + " h " + String(minutes%60).padStart(2,"0") + " min";
  }
  // Change scale while keeping one timeline position under the same screen pixel.
  function setZoom(next, anchor) {
    const state=editor.get(), scroll=$("track-scroll");
    const {min,max}=zoomLimits(state);
    const visibleHead=state.time*pps>=scroll.scrollLeft && state.time*pps<=scroll.scrollLeft+scroll.clientWidth;
    const time=anchor?.time ?? (visibleHead ? state.time : (scroll.scrollLeft+scroll.clientWidth/2)/pps);
    const screenX=anchor?.x ?? (time*pps-scroll.scrollLeft);
    pps=clamp(next,min,max);
    $("timeline-surface").style.width=surfaceWidth(state.duration,timelineWidth())+"px";
    scrollTimelineTo(time*pps-screenX);
    lastProject="";
    refresh(editor.get());
  }
  function zoomToFit() {
    pps=zoomLimits().fit;
    scrollTimelineTo(0);
    lastProject="";
    refresh(editor.get());
  }
  const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
  function laneList(project) {
    const highestVideo = Math.max(1, ...project.clips.filter(c=>c.type==="video").map(c=>c.track));
    const lowestAudio = Math.min(-1, ...project.clips.filter(c=>c.type==="audio").map(c=>c.track));
    const defaultTitle=editor.titleTrack();
    const titleTracks=new Set([defaultTitle,...project.clips.filter(c=>c.type==='title').map(c=>c.track)]);
    const pictureLanes=[...Array.from(titleTracks,track=>({kind:'title',track,name:track===defaultTitle?'Titles':'Titles · V'+track})),
      ...Array.from({length:Math.min(64,Math.max(2,project.trackCounts?.video||3,highestVideo+1))},(_,n)=>({kind:'video',track:n+1,name:'Video '+(n+1)}))];
    pictureLanes.sort((a,b)=>b.track-a.track);
    return [...pictureLanes,
      ...Array.from({length:Math.min(64,Math.max(2,project.trackCounts?.audio||3,-lowestAudio+1))},(_,n)=>({kind:'audio',track:-n-1,name:'Audio '+(n+1)}))];
  }
  // Clips too narrow to read are painted on one canvas per lane instead of becoming DOM nodes.
  const tinyClipWidth=8;
  // Below this width a clip is too narrow for a readable waveform; it gets none.
  const waveformMinWidth=16;
  function paintDensity(surface, clips, kind) {
    if (!clips.length) return;
    const canvas=document.createElement("canvas");
    canvas.className="lane-density";
    const left=Math.max(0,viewStart), width=Math.max(1,Math.min(16000,Math.round(viewEnd-left)));
    canvas.width=width;canvas.height=40;
    canvas.style.left=left+"px";canvas.style.width=width+"px";
    const ctx=canvas.getContext("2d");
    ctx.fillStyle=kind==="audio"?"#4c8a62":kind==="title"?"#8c6fb8":"#5c7fa8";
    for(const clip of clips){
      const x=px(clip.start)-left;
      ctx.fillRect(Math.floor(x),0,Math.max(1,Math.ceil(px(clip.duration))),40);
    }
    surface.append(canvas);
  }
  const linkIcon='<svg class="link-mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M6.5 9.5l3-3M5 7.5L3.6 8.9a2.5 2.5 0 0 0 3.5 3.5L8.5 11M11 8.5l1.4-1.4a2.5 2.5 0 0 0-3.5-3.5L7.5 5"/></svg>';
  function clipNode(clip, lane, project, selectedId, group) {
    const media=mediaFor(project,clip),node=document.createElement("button");
    const offset=clip.linkGroup?editor.syncOffset(clip):null;
    const embedded=editor.hasEmbeddedAudio(clip);
    node.className="timeline-clip "+lane.kind+(clip.id===selectedId?" selected":"")+(group.has(clip.id)?" group-selected":"")+
      (clip.linkGroup?" linked":"")+(offset?" out-of-sync":"")+(embedded?" has-audio":"")+
      (selectedEdit?.id===clip.id?" edit-"+selectedEdit.edge:"");
    node.style.left=px(clip.start)+"px";node.style.width=Math.max(tinyClipWidth,px(clip.duration)-1)+"px";
    node.dataset.clip=clip.id;
    const name=clip.title||media?.name||"Title";
    const state=[clip.linkGroup?"linked":"",embedded?"with camera sound":"",offset?`${Math.abs(offset)} frames ${offset>0?"late":"early"} against its linked partner`:""].filter(Boolean).join(", ");
    node.title=name+" · "+tc(clip.start)+(state?" · "+state:"");
    node.innerHTML='<span class="clip-overlay"></span>'+(lane.kind==="audio"?'<canvas class="clip-wave"></canvas>':embedded?'<canvas class="clip-wave camera-wave"></canvas>':"")+
      '<span class="clip-name">'+(clip.linkGroup?linkIcon:"")+esc(name)+'</span>'+
      (offset?`<span class="sync-badge" role="button" tabindex="-1" data-sync="${esc(clip.id)}" title="Out of sync by ${Math.abs(offset)} frames. Click to move or slip into sync.">${offset>0?"+":"−"}${Math.abs(offset)}</span>`:"")+
      '<span class="trim-handle trim-left" title="Trim in · Alt+drag trims only this side of a linked clip"></span><span class="trim-handle trim-right" title="Trim out · Alt+drag trims only this side of a linked clip"></span>';
    if (thumbOf(media)) {
      // Set the thumbnail as a property: generated thumbnails are long data URLs, costly to escape and parse as markup.
      const image=document.createElement("img");
      image.className="clip-image";image.alt="";image.src=thumbOf(media);
      node.prepend(image);
    }
    if (Number.isInteger(clip.nestedSequence)) {
      node.title += ' · Double-click to open nested sequence';
      node.ondblclick=()=>busy(()=>SpliceSession.sequence(clip.nestedSequence));
    }
    const waveWidth=Math.max(tinyClipWidth,px(clip.duration)-1)-6;
    if((lane.kind==="audio"||embedded)&&waveWidth>=waveformMinWidth)
      requestAnimationFrame(()=>paintWave(node.querySelector("canvas"),media,clip,waveWidth));
    return node;
  }
  function renderMarks(project, height) {
    const surface=$("timeline-surface");
    surface.querySelector(".mark-range")?.remove();
    const marks=project.marks||{};
    if(!Number.isFinite(marks.in)&&!Number.isFinite(marks.out))return;
    const range=document.createElement("div");range.className="mark-range";
    const from=Number.isFinite(marks.in)?marks.in:0, to=Number.isFinite(marks.out)?marks.out:Math.max(from,editor.get().duration);
    range.classList.toggle("open-in",!Number.isFinite(marks.in));
    range.classList.toggle("open-out",!Number.isFinite(marks.out));
    range.style.left=px(from)+"px";range.style.width=Math.max(2,px(to-from))+"px";range.style.height=height+"px";
    range.title="Timeline "+(Number.isFinite(marks.in)?"In "+tc(marks.in):"")+(Number.isFinite(marks.out)?" Out "+tc(editor.markOutFrame(marks.out)):"")+
      (Number.isFinite(marks.in)&&Number.isFinite(marks.out)?" · Duration "+fmt(marks.out-marks.in,project.fps):"");
    surface.append(range);
  }
  // Track heights. Empty tracks are drawn short; tracks that hold clips, and V1 and A1 where
  // edits land, share the rest of the timeline's height (from 36 to 64 px each), so they all
  // fit without scrolling whenever they can.
  const rulerHeight=31, compactLane=30;
  const laneKey=lane=>lane.kind+":"+lane.track;
  const clipLaneKey=clip=>(clip.type==="title"?"title":clip.type)+":"+clip.track;
  function laneHeights(visibleLanes, used) {
    const full=visibleLanes.map(lane=>used.has(laneKey(lane))||lane.kind!=="title"&&Math.abs(lane.track)===1);
    const fullCount=full.filter(Boolean).length;
    const room=(viewport.height||240)-rulerHeight-(full.length-fullCount)*compactLane;
    const tallest=window.innerHeight<=850?50:64;
    const each=clamp(Math.floor(room/Math.max(1,fullCount)),36,tallest);
    return full.map(isFull=>isFull?each:compactLane);
  }
  // Vertical position of the track area: centred on the V1/A1 boundary when a project opens,
  // and moved only as far as needed to show the tracks an edit changed.
  let layout={opened:-1,lanes:[],heights:[],counts:new Map()};
  function laneCounts(project) {
    const counts=new Map();
    for(const clip of project.clips){
      const key=clipLaneKey(clip);
      const [count,sum]=counts.get(key)||[0,0];
      counts.set(key,[count+1,sum+clip.start*3+clip.duration]);
    }
    return counts;
  }
  function laneSpan(keys) {
    let top=rulerHeight, from=Infinity, to=-Infinity;
    layout.lanes.forEach((lane,index)=>{
      const height=layout.heights[index];
      if(keys.has(laneKey(lane))){from=Math.min(from,top);to=Math.max(to,top+height);}
      top+=height;
    });
    return Number.isFinite(from)?{from,to}:null;
  }
  function placeTracks(scrollTo) {
    const scroll=$("track-scroll");
    const visible=Math.max(1,viewport.height-rulerHeight);
    const total=layout.heights.reduce((sum,height)=>sum+height,0);
    const top=clamp(Math.round(scrollTo),0,Math.max(0,total-visible));
    if(Math.abs(top-scroll.scrollTop)<1)return;
    scroll.scrollTop=top;
    viewport.top=top;
  }
  // Show every track that holds clips (and V1 and A1) when they fit, centred; otherwise centre
  // the V1/A1 boundary, as other editors keep the picture/sound divider in view.
  function centreTracks() {
    const visible=viewport.height-rulerHeight;
    const required=new Set(["video:1","audio:-1",...layout.counts.keys()]);
    const span=laneSpan(required);
    if(span&&span.to-span.from<=visible){
      placeTracks((span.from+span.to)/2-rulerHeight-visible/2);
      return;
    }
    const firstAudio=layout.lanes.findIndex(lane=>lane.kind==="audio");
    const divider=layout.heights.slice(0,Math.max(0,firstAudio)).reduce((sum,height)=>sum+height,0);
    placeTracks(divider-visible/2);
  }
  function revealLanes(keys) {
    const span=laneSpan(keys);
    if(!span)return;
    const visible=viewport.height-rulerHeight;
    // Lane span in scroll coordinates: the sticky ruler covers the first 31 px of the view.
    const from=span.from-rulerHeight, to=span.to-rulerHeight;
    if(from>=viewport.top&&to<=viewport.top+visible)return;
    placeTracks(to-from>visible||from<viewport.top?from:to-visible);
  }
  function renderTimeline(state) {
    const {project,duration,selectedId}=state;
    const visibleLanes=laneList(project);
    const counts=laneCounts(project);
    const heights=laneHeights(visibleLanes,counts);
    const changed=new Set();
    for(const [key,value] of counts){
      const before=layout.counts.get(key);
      if(!before||before[0]!==value[0]||Math.abs(before[1]-value[1])>1e-6)changed.add(key);
    }
    const opened=state.opened!==layout.opened;
    layout={opened:state.opened,lanes:visibleLanes,heights,counts};
    // Read every scroll metric before touching the DOM, so the rebuild never forces a synchronous layout.
    const screen=timelineWidth();
    const {min,max}=zoomLimits(state);
    pps=clamp(pps,min,max);
    const width=surfaceWidth(duration,screen);
    renderedWidth=width;
    viewStart=Math.max(0,viewport.left-screen);viewEnd=Math.min(width,viewport.left+2*screen);
    const visible=(start,end)=>px(start)<=viewEnd && px(end)>=viewStart;
    $("timeline-surface").style.width=width+"px";
    $("playhead").style.left=px(state.time)+"px";
    const height=rulerHeight+heights.reduce((sum,lane)=>sum+lane,0);
    $("timeline-surface").style.height=height+"px";
    $("zoom-label").textContent=describeSpan(screen/pps);
    $("zoom-label").title="Visible span · "+(pps*1/project.fps).toFixed(2)+" px per frame";
    const ruler=$("time-ruler");ruler.innerHTML="";
    // Ticks fall on whole timecode seconds (drop-frame aware), so labels read 01:00:20:00
    // rather than 01:00:19:29 at 29.97 and 59.94 fps.
    const step=rulerStep(project.fps);
    for(const {time,label} of editor.rulerTicks(viewStart/pps,viewEnd/pps,step)){
      const tick=document.createElement("span");
      tick.className="ruler-tick";
      tick.style.left=px(time)+"px";
      tick.textContent=label;
      ruler.append(tick);
    }
    for(const marker of project.markers||[]){
      if(!visible(marker.time,marker.time))continue;
      const flag=document.createElement('button');flag.className='edit-marker';flag.style.left=px(marker.time)+'px';
      flag.title=marker.name+' · '+tc(marker.time);flag.setAttribute('aria-label',flag.title);
      flag.onpointerdown=e=>{e.stopPropagation();editor.seek(marker.time);};flag.ondblclick=()=>editMarker(marker);ruler.append(flag);
    }
    renderMarks(project,height);
    const byLane=new Map();
    for(const clip of project.clips){
      if(!visible(clip.start,clip.start+clip.duration))continue;
      const key=(clip.type==="title"?"title":clip.type)+":"+clip.track;
      if(!byLane.has(key))byLane.set(key,[]);
      byLane.get(key).push(clip);
    }
    $("track-heads").querySelectorAll(".track-head").forEach(node=>node.remove());
    const lanes=$("track-lanes");lanes.replaceChildren();
    visibleLanes.forEach((lane,index)=>{
      const head=document.createElement("div");head.className="track-head";
      const laneHeight=heights[index]+"px";
      const compact=heights[index]===compactLane;
      head.style.height=laneHeight;
      head.classList.toggle("is-compact",compact);
      window.SpliceTracks.renderHeader(head,lane,project,editor);
      $("track-heads").append(head);
      const surface=document.createElement("div");surface.className="track-lane "+lane.kind+"-lane";
      surface.style.height=laneHeight;
      surface.classList.toggle("is-compact",compact);
      surface.dataset.track=lane.track;surface.dataset.kind=lane.kind;
      surface.classList.toggle('is-locked', editor.isTrackLocked(lane.track,lane.kind));
      surface.classList.toggle('is-hidden', lane.kind!=="audio" && editor.isTrackHidden(lane.track,lane.kind));
      lanes.append(surface);
      const clips=byLane.get(lane.kind+":"+lane.track)||[];
      const tiny=[];
      for(const clip of clips){
        if(px(clip.duration)<tinyClipWidth && clip.id!==selectedId){tiny.push(clip);continue;}
        surface.append(clipNode(clip,lane,project,selectedId,group));
      }
      paintDensity(surface,tiny,lane.kind);
      if(lane.kind==="audio")window.SpliceAutomation?.renderLane(surface,lane,project,editor,{px,seconds:duration});
    });
    // Rebuilt headers can lose their scroll offset; level them with the lanes at the next frame
    // rather than forcing a synchronous layout of the rebuilt timeline here. A newly opened
    // project centres V1/A1; an edit brings the tracks it changed into view.
    requestAnimationFrame(()=>{
      if(opened)centreTracks();
      else if(changed.size)revealLanes(changed);
      $("track-heads").scrollTop=$("track-scroll").scrollTop;
    });
  }
  function renderPreview(state) {
    const { project, time, duration, playing } = state;
    SpliceCaptions.paint(state);
    const fps = project.fps || 30;
    if (!$("viewer-time").querySelector("input")) $("viewer-time").textContent = tc(time);
    $("header-time").textContent = tc(time);
    $("viewer-duration").textContent = `/ ${fmt(duration, fps)}`;
    if (playing !== lastPlay) $("play-toggle").innerHTML = playing
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>'
      : svg(icons["play-toggle"]);
    $("playhead").style.left = `${px(time)}px`;
    // The placeholder is for an empty sequence only. Gaps, and the playhead parked at or past
    // the end of the sequence, show black, as the program monitor in other editors does.
    $("preview-empty").style.display = project.clips.length ? "none" : "flex";
  }
  let lastPaint = "", lastScope = -Infinity, monitorState;
  for (const event of ['loadeddata','seeked','canplay','emptied'])
    document.addEventListener(event,()=>{lastPaint="";},true);
  function paintPreview(now) {
    const canvas = $("preview-canvas"), state = monitorState;
    if (!state) { requestAnimationFrame(paintPreview); return; }
    const video = state.video;
    const frame=Math.floor(state.time*state.project.fps);
    const paintKey = [state.revision, frame, state.selectedId, video?.currentSrc,video?.readyState,
      state.playing?'rolling':video?.currentTime, video?.seeking,canvas.width,canvas.height].join(':');
    if (paintKey !== lastPaint) {
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
  let lastPlay, lastFrame, lastOverallKey, lastAspect;
  function refresh(state) {
    monitorState = state;
    const picture=state.activeClip;
    const hdrStatus=window.SpliceMedia?.hdrPreviewStatus(mediaFor(state.project,picture||{}),
      picture && 'audioStream' in picture ? picture.audioStream : undefined);
    const previewStatus=$('hdr-preview-status');
    previewStatus.hidden=!(hdrStatus?.pending || hdrStatus?.error);
    previewStatus.textContent=hdrStatus?.pending ? 'Preparing HDR preview…' : hdrStatus?.error ? 'HDR preview proxy unavailable' : '';
    previewStatus.title=hdrStatus?.error||'';
    const fraction=Math.round(state.time*state.project.fps);
    if(fraction!==lastFrame || state.playing!==lastPlay || state.revision!==lastOverallKey){
      renderPreview(state);
      lastFrame=fraction;lastPlay=state.playing;lastOverallKey=state.revision;
    }
    const signature = JSON.stringify({
      revision: state.revision,
      name: state.project.name,
      pps,
      selectedId: state.selectedId,
      linkedSelection: state.linkedSelection,
    });
    const projectChanged = signature !== lastProject;
    if (projectChanged && !gesture) {
      renderTimeline(state);
      lastProject = signature;
    }
    window.SpliceAudioPanel?.refresh(state);
    const mediaSignature = [state.revision, tab, $("bin-filter").value, $("media-search").value, $("media-filter").value,
      mediaView, mediaSort.key, mediaSort.direction].join("|");
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
    const sequences=state.project.resolveTimelines||state.project.premiereSequences||[], selector=$('sequence-select');
    selector.hidden=sequences.length<2;
    if(!selector.hidden){
      selector.replaceChildren(...sequences.map((name,i)=>new Option(name,String(i))));
      selector.value=String(state.project.resolveTimelines?state.project.resolveTimeline:state.project.premiereSequence);
    }
    $('size-label').textContent=state.project.width+' × '+state.project.height;
    const aspect=state.project.width/state.project.height;
    const canvas=$('preview-canvas'), height=Math.min(2048,Math.max(1,Math.round(960/aspect)));
    if(canvas.width!==960 || canvas.height!==height){canvas.width=960; canvas.height=height; lastPaint='';}
    // Re-measure only when the frame shape changes; the ResizeObserver covers window resizes.
    // Measuring on every edit forces a synchronous layout of the whole page.
    if(aspect!==lastAspect){lastAspect=aspect;sizeMonitor();}
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
        // The Audio workspace swaps the media pool for the mixer and loudness meter.
        window.SpliceAudioPanel?.toggle(button.dataset.workspace === "audio");
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
  $("folder-relink-input").onchange=e=>{
    const files=Array.from(e.target.files);e.target.value="";
    if(files.length)busy(()=>editor.relinkFolder(files),"Matching sources relinked");
  };
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
  $("open-project").onclick = () => {
    if (!gesture) busy(() => window.SpliceSession?.open?.() ?? $("project-input").click());
  };
  $("project-input").onchange = (e) => {
    const file = e.target.files[0];
    if (file) busy(() => window.SpliceSession?.openFile?.(file) ?? editor.openProject(file), "Project opened");
    e.target.value = "";
  };
  $("save-project").onclick = () =>
    busy(() => window.SpliceSession?.save?.("prproj") ?? editor.saveProject("prproj"), "Project saved");
  $("export-video").onclick=()=>SpliceDialogs.exportVideo();
  // A large scope docked beside the program monitor, for grading. It takes the media pool's
  // column while open; the inspector's small scope steps aside so only one scope is painted.
  function toggleScopeDock(open=!document.querySelector(".scope-dock")) {
    const workspace=document.querySelector(".workspace"), toggle=$("scope-dock-toggle");
    document.querySelector(".scope-dock")?.remove();
    workspace.classList.toggle("scope-docked",open);
    toggle.setAttribute("aria-pressed",String(open));
    toggle.classList.toggle("active",open);
    if(open){
      const dock=document.createElement("section");
      dock.className="scope-dock panel";
      dock.setAttribute("aria-label","Video scope");
      dock.innerHTML='<div class="panel-top"><div class="panel-title">Scopes <span class="monitor-subtitle">/ graded program frame</span></div>'+
        '<button type="button" class="icon-button scope-dock-close" aria-label="Close the large scope" title="Close the large scope">×</button></div>'+
        '<div class="scope-dock-body">'+SpliceScopes.markup()+'</div>';
      workspace.prepend(dock);
      SpliceScopes.bind(dock);
      dock.querySelector(".scope-dock-close").onclick=()=>toggleScopeDock(false);
    }
    requestAnimationFrame(()=>window.SpliceInspector?.paintScope?.(undefined,true));
  }
  $("scope-dock-toggle").onclick=()=>toggleScopeDock();
  document.addEventListener("click",event=>{
    if(event.target.closest?.("[data-scope-dock]"))toggleScopeDock(true);
  });
  $("new-project").onclick=()=>SpliceDialogs.newSequence();
  $("shortcuts").onclick=()=>SpliceDialogs.shortcuts();
  $("save-working").onclick=()=>busy(()=>SpliceSession.saveAs("splice"));
  document.querySelector(".file-menu").addEventListener("click",e=>{if(e.target.closest("button"))document.querySelector(".file-menu").open=false;});
  // The File and Edit menus close on Escape (focus returns to their title) and on a click elsewhere.
  const menus=[...document.querySelectorAll(".file-menu,.edit-menu")];
  document.addEventListener("keydown",e=>{
    const open=menus.find(menu=>menu.open);
    if(e.key!=="Escape"||!open)return;
    e.preventDefault();
    e.stopPropagation();
    open.open=false;
    open.querySelector("summary").focus();
  },true);
  document.addEventListener("pointerdown",e=>{
    for(const menu of menus)if(menu.open&&!menu.contains(e.target))menu.open=false;
  },true);
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
  $("delete-selected").onclick = () => safeEdit(() => editor.deleteSelected({ripple:rippleEnabled}));
  $("undo").onclick = () => editor.undo();
  $("redo").onclick = () => editor.redo();
  $("reset-clip").onclick = () => {
    const c = editor.get().selected;
    if (c) safeEdit(() => editor.resetClip(c.id));
  };
  $("zoom-out").onclick = () => setZoom(pps / 1.5);
  $("zoom-in").onclick = () => setZoom(pps * 1.5);
  $("zoom-fit").onclick = zoomToFit;
  // Ctrl/Cmd or Alt + wheel zooms around the pointer.
  $("track-scroll").addEventListener("wheel", e => {
    if (!e.ctrlKey && !e.metaKey && !e.altKey) return;
    e.preventDefault();
    const scroll=$("track-scroll"), x=e.clientX-scroll.getBoundingClientRect().left;
    setZoom(pps*Math.exp(-e.deltaY/300),{time:(scroll.scrollLeft+x)/pps,x});
  },{passive:false});
  $("linked-toggle").onclick = () => editor.setLinkedSelection(!editor.get().linkedSelection);
  $("media-view").onclick = () => {
    mediaView = mediaView === "grid" ? "list" : "grid";
    $("media-view").textContent = mediaView === "grid" ? "List" : "Grid";
    $("media-view").setAttribute("aria-pressed", String(mediaView === "list"));
    lastMedia = "";
    renderLibrary(editor.get());
  };
  editor.subscribe(state => {
    $("linked-toggle").classList.toggle("active", state.linkedSelection);
    $("linked-toggle").setAttribute("aria-pressed", String(state.linkedSelection));
    const marks = state.project.marks || {};
    // The Out label names the last frame the range includes.
    const label = [Number.isFinite(marks.in) ? "In " + tc(marks.in) : "", Number.isFinite(marks.out) ? "Out " + tc(editor.markOutFrame(marks.out)) : ""].filter(Boolean).join(" · ");
    $("mark-status").textContent = label;
    $("mark-status").hidden = !label;
  });
  let scrubbing=false;
  $("time-ruler").addEventListener("pointerdown",e=>{scrubbing=true;$("time-ruler").setPointerCapture(e.pointerId);editor.seek(positionTime(e,$("time-ruler")));});
  $("time-ruler").addEventListener("pointermove",e=>{if(scrubbing)editor.seek(positionTime(e,$("time-ruler")));});
  $("time-ruler").addEventListener("pointerup",()=>scrubbing=false);
  const lanes=$("track-lanes");
  let scrollFrame=0;
  new ResizeObserver(entries=>{
    const width=Math.round(entries[0].contentRect.width), height=Math.round(entries[0].contentRect.height);
    if(width===viewport.width&&height===viewport.height)return;
    const newHeight=height!==viewport.height;
    viewport.width=width;viewport.height=height;
    // A new timeline height (the first measurement, a resize, the source panel opening) lays
    // the tracks out again around the ones in use.
    if(newHeight)layout.opened=-1;
    lastProject="";refresh(editor.get());
  }).observe($("track-scroll"));
  $("track-scroll").addEventListener("scroll",()=>{
    const surface=$("track-scroll");$("track-heads").scrollTop=surface.scrollTop;
    viewport.left=surface.scrollLeft;
    viewport.top=surface.scrollTop;
    if(scrollFrame||gesture)return;
    scrollFrame=requestAnimationFrame(()=>{
      scrollFrame=0;
      // Cached sizes only: reading scrollWidth or clientWidth here would force a layout.
      const screen=timelineWidth();
      if((viewport.left<viewStart+screen/4 && viewStart>0) ||
         (viewport.left+1.25*screen>viewEnd && viewEnd<renderedWidth))
        renderTimeline(editor.get());
    });
  });
  // Out-of-sync badge: offer Premiere's "Move into sync" and "Slip into sync".
  // One menu at a time; closing it always removes its outside-click listener.
  let closeSyncMenu=null;
  function syncMenu(id, box) {
    closeSyncMenu?.();
    const menu=document.createElement("div");menu.className="sync-menu";menu.setAttribute("role","menu");
    menu.style.left=clamp(box.left,8,window.innerWidth-190)+"px";
    // Below the badge when there is room, otherwise above it.
    const below=box.bottom+4, height=84;
    menu.style.top=(below+height<=window.innerHeight-8?below:Math.max(8,box.top-height-4))+"px";
    const dismiss=event=>{if(!menu.contains(event.target))close();};
    // Escape closes it (focus returns to the timeline); a wheel turn or a resize closes it too.
    const onKey=event=>{if(event.key==="Escape"){event.preventDefault();event.stopPropagation();close();}};
    // A scroll closes it only when it moved the badge away from the menu. Re-rendering the
    // timeline (selecting the clip does) fires scroll events on its containers without moving
    // anything, and must not close the menu it was opened with.
    let scrollCheck=0;
    const onScroll=()=>{
      if(scrollCheck)return;
      scrollCheck=requestAnimationFrame(()=>{
        scrollCheck=0;
        const badge=lanes.querySelector('.sync-badge[data-sync="'+CSS.escape(id)+'"]');
        const now=badge?.getBoundingClientRect();
        if(!now||Math.abs(now.left-box.left)>1||Math.abs(now.top-box.top)>1)close();
      });
    };
    const close=()=>{
      menu.remove();
      cancelAnimationFrame(scrollCheck);
      scrollCheck=0;
      window.removeEventListener("pointerdown",dismiss,true);
      window.removeEventListener("keydown",onKey,true);
      window.removeEventListener("scroll",onScroll,true);
      window.removeEventListener("wheel",close,true);
      window.removeEventListener("resize",close);
      if(closeSyncMenu===close)closeSyncMenu=null;
      if(document.activeElement===document.body||!document.activeElement)lanes.focus?.({preventScroll:true});
    };
    closeSyncMenu=close;
    for(const [how,label] of [["move","Move into sync"],["slip","Slip into sync"]]){
      const item=document.createElement("button");item.type="button";item.setAttribute("role","menuitem");item.textContent=label;
      item.onclick=()=>{close();safeEdit(()=>editor.bringIntoSync(id,how));};
      menu.append(item);
    }
    window.addEventListener("pointerdown",dismiss,true);
    window.addEventListener("keydown",onKey,true);
    window.addEventListener("scroll",onScroll,true);
    window.addEventListener("wheel",close,true);
    window.addEventListener("resize",close);
    document.body.append(menu);
    menu.querySelector("button").focus();
  }
  lanes.addEventListener("pointerdown",e=>{
    const badge=e.target.closest(".sync-badge");
    if(badge){
      e.stopPropagation();e.preventDefault();
      // Measure the badge before selecting: selection re-renders the timeline and replaces it.
      const box=badge.getBoundingClientRect(), id=badge.dataset.sync;
      editor.select(id);
      syncMenu(id,box);
      return;
    }
    const density=e.target.closest(".lane-density");
    if(density){
      // Zoomed far out, a click on the painted lane selects the clip under the pointer.
      const lane=density.parentElement, at=positionTime(e,lane);
      const hit=editor.get().project.clips.find(c=>c.type===lane.dataset.kind&&c.track===Number(lane.dataset.track)&&c.start<=at&&at<c.start+c.duration);
      if(hit){editor.select(hit.id);lastProject="";refresh(editor.get());}
      return;
    }
    const node=e.target.closest(".timeline-clip");if(!node)return;
    const clip=editor.get().project.clips.find(c=>c.id===node.dataset.clip);if(!clip)return;
    if(e.ctrlKey||e.metaKey){
      if(!group.size&&editor.get().selectedId)group.add(editor.get().selectedId);
      if(group.has(clip.id))group.delete(clip.id);else group.add(clip.id);
      lastProject="";refresh(editor.get());e.preventDefault();return;
    }
    const edge=e.target.closest(".trim-handle");
    selectedEdit=edge?{id:clip.id,edge:edge.classList.contains("trim-left")?"start":"end"}:null;
    if (!group.has(clip.id) || edge || e.altKey) group.clear();
    const originals=withLinked(editor.get().project.clips.filter(c=>group.has(c.id)||c.id===clip.id))
      .map(c=>({id:c.id,start:c.start,track:c.track,type:c.type}));
    if ([clip,...originals].some(item=>editor.isTrackLocked(item.track,item.type))) {
      notify('Unlock the track before editing.'); return;
    }
    const mode=edge?(edge.classList.contains("trim-left")?"left":"right"):e.altKey&&e.shiftKey?"slide":e.altKey?"slip":"move";
    gesture={id:clip.id,originals,original:structuredClone(clip),x:e.clientX,mode,track:clip.track,
      // Alt on an edge trims only this side of a linked pair: a J- or L-cut.
      single:!!edge&&e.altKey,detach:!!edge&&e.altKey&&editor.hasEmbeddedAudio(clip)};
    editor.beginGesture?.();
    node.setPointerCapture(e.pointerId);
    editor.select(clip.id);
    node.classList.add("selected");
    e.preventDefault();
  });
  function trimDuringGesture(clip, boundary, roll) {
    const edgeName=gesture.mode==="left"?"start":"end";
    if(roll){
      // Find the neighbour once: after the first roll step the cut no longer sits at the original edge.
      const start=gesture.original.start,end=start+gesture.original.duration;
      gesture.neighbour??=editor.get().project.clips.find(c=>c.id!==clip.id&&c.track===clip.track&&c.type===clip.type&&
        (gesture.mode==="left"?Math.abs(c.start+c.duration-start)<.05:Math.abs(c.start-end)<.05))?.id||null;
      const neighbour=editor.get().project.clips.find(c=>c.id===gesture.neighbour);
      if(neighbour){
        const roll=()=>gesture.mode==="left"?editor.rollEdit(neighbour.id,clip.id,boundary):editor.rollEdit(clip.id,neighbour.id,boundary);
        safeEdit(()=>gesture.single?editor.withoutLinks(roll):roll());
        return;
      }
    }
    const trim=()=>editor.trimEdge(clip.id,edgeName,boundary,{ripple:rippleEnabled});
    safeEdit(()=>gesture.single?editor.withoutLinks(trim):trim());
  }
  window.addEventListener("pointermove",e=>{
    if(!gesture)return;
    let clip=editor.get().project.clips.find(c=>c.id===gesture.id);if(!clip)return;
    const delta=(e.clientX-gesture.x)/pps;
    const start=gesture.original.start,end=start+gesture.original.duration;
    if(gesture.mode==="move"){
      const lane=document.elementFromPoint(e.clientX,e.clientY)?.closest(".track-lane");
      const track=lane && lane.dataset.kind===clip.type?Number(lane.dataset.track):gesture.track;
      const members=gesture.originals;
      const offset=Math.max(snapTime(start+delta,clip.id)-start,-Math.min(...members.map(c=>c.start)));
      gesture.placements=members.map(original=>{
        const dest=original.type!==clip.type||clip.type==='title'?original.track:original.track+track-gesture.track;
        return {...original,start:original.start+offset,track:dest};
      });
      for(const placement of gesture.placements){
        const node=lanes.querySelector('[data-clip="'+CSS.escape(placement.id)+'"]');
        const target=lanes.querySelector('[data-kind="'+placement.type+'"][data-track="'+placement.track+'"]');
        if(node){node.style.left=px(placement.start)+'px';if(target&&node.parentElement!==target)target.append(node);}
      }
      return;
    }
    if(gesture.mode==="slip"){ safeEdit(()=>editor.slipClip?.(clip.id,delta-(gesture.lastDelta||0))); gesture.lastDelta=delta; return; }
    if(gesture.mode==="slide"){ safeEdit(()=>editor.slideClip(clip.id,start+delta-clip.start)); }
    else {
      if(gesture.detach){
        gesture.detach=false;
        if(!safeEdit(()=>editor.detachAudio(clip.id))){gesture.single=false;}
        clip=editor.get().project.clips.find(c=>c.id===gesture.id);
      }
      trimDuringGesture(clip,snapTime((gesture.mode==="left"?start:end)+delta,clip.id),e.shiftKey);
    }
    const current=editor.get().project.clips.find(c=>c.id===clip.id);
    const node=lanes.querySelector('[data-clip="'+CSS.escape(clip.id)+'"]');
    if(node&&current){node.style.left=px(current.start)+"px";node.style.width=Math.max(tinyClipWidth,px(current.duration)-1)+"px";}
  });
  window.addEventListener("pointerup",()=>{
    if(!gesture)return;
    if(gesture.placements)safeEdit(()=>editor.moveClips(gesture.placements));
    editor.endGesture?.();gesture=null;lastProject="";refresh(editor.get());
  });
  window.addEventListener("pointercancel",()=>{editor.endGesture?.();gesture=null;lastProject="";refresh(editor.get());});
  lanes.addEventListener("click",e=>{if(e.target.classList.contains("track-lane")){selectedEdit=null;editor.seek(positionTime(e,e.target));}});
  // Dragging from the source monitor: say where picture and sound will land before the drop.
  let dropHint=null;
  function showDropHint(lane) {
    const kind=lane.dataset.kind==="title"?"video":lane.dataset.kind;
    const text=SpliceSource.mediaType()==="audio"&&kind!=="audio"?"Drop sound on an audio track":
      SpliceSource.describeDrop({kind,track:Number(lane.dataset.track)});
    if(dropHint?.lane===lane&&dropHint.text===text)return;
    dropHint?.lane.classList.remove("drop-target");
    lane.classList.add("drop-target");
    dropHint={lane,text,status:dropHint?.status??$("timeline-status").textContent};
    $("timeline-status").textContent=text;
  }
  function clearDropHint() {
    if(!dropHint)return;
    dropHint.lane.classList.remove("drop-target");
    $("timeline-status").textContent=dropHint.status;
    dropHint=null;
  }
  lanes.addEventListener("dragover",e=>{
    const lane=e.target.closest(".track-lane");
    if(!lane)return;
    e.preventDefault();
    if(e.dataTransfer.types.includes("application/x-splice-source")&&window.SpliceSource?.isOpen?.())showDropHint(lane);
  });
  lanes.addEventListener("dragleave",e=>{if(!lanes.contains(e.relatedTarget))clearDropHint();});
  lanes.addEventListener("drop",e=>{
    const lane=e.target.closest(".track-lane");if(!lane)return;e.preventDefault();
    clearDropHint();
    const sourceId=e.dataTransfer.getData("application/x-splice-source");
    if(sourceId&&window.SpliceSource?.mediaId?.()===sourceId){
      // From the source monitor: its marked range, overwritten (Ctrl/⌘: inserted) where it was dropped.
      // A picture drop lands on that video track, its sound on the targeted audio track; a drop
      // on an audio track places the sound only.
      clearDropHint();
      const kind=lane.dataset.kind==="title"?"video":lane.dataset.kind;
      if(SpliceSource.mediaType()==="audio"&&kind!=="audio")return notify("Drop sound on an audio track.");
      SpliceSource.place(e.ctrlKey||e.metaKey?"insert":"overwrite",{kind,track:Number(lane.dataset.track),at:snapTime(positionTime(e,lane))});
      return;
    }
    const id=e.dataTransfer.getData("text/plain");
    const item=editor.get().project.media.find(m=>m.id===id);
    if(!item||item.type!==(lane.dataset.kind==="title"?"video":lane.dataset.kind))return notify("Choose a "+item?.type+" lane.");
    if (editor.isTrackLocked(Number(lane.dataset.track),lane.dataset.kind)) return notify('Unlock the track before editing.');
    const start=positionTime(e,lane);
    safeEdit(()=>editor.addToTimeline(id,{start:snapTime(start),track:Number(lane.dataset.track)}));
  });
  function toggleSnap(){snapEnabled=!snapEnabled;$("snap-toggle").classList.toggle("active",snapEnabled);}
  $("snap-toggle").onclick=toggleSnap;
  $("ripple-toggle").onclick=toggleRipple;
  $("compatibility").onclick=()=>SpliceDialogs.projectReport();
  $("versions").onclick=()=>busy(()=>SpliceSession.history());
  $("save-as").onclick=()=>busy(()=>SpliceSession.saveAs("prproj"));
  $("sequence-settings").onclick=()=>sequenceSettings();
  $("sequence-select").onchange=e=>{
    const project=editor.get().project;
    // Another task holds the workspace: say so and show the open timeline again, instead of a choice that is ignored.
    if(pending){e.target.value=String(project.resolveTimelines?project.resolveTimeline:project.premiereSequence);notify("Wait for the current task to finish, then choose the timeline again.");return;}
    busy(()=>project.resolveTimelines?SpliceResolve.timeline(Number(e.target.value)):SpliceSession.sequence(Number(e.target.value)));
  };
  $("fullscreen-monitor").onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else $("monitor-frame").requestFullscreen?.();
  };
  for (const button of document.querySelectorAll(".edit-menu [data-command]"))
    button.onclick = () => {
      document.querySelector(".edit-menu").open = false;
      commands[button.dataset.command]?.();
    };
  // Click the program timecode to type a position: 01:00:10:00, 1001000, or +15 / -1:00 to move relative.
  $("viewer-time").onclick = () => {
    const display=$("viewer-time");
    if (display.querySelector("input")) return;
    const field=document.createElement("input");
    field.className="timecode-entry";field.value=tc(editor.get().time);field.setAttribute("aria-label","Go to timecode");
    display.replaceChildren(field);field.select();
    let finished=false;
    const finish=(apply)=>{
      if(finished)return;
      finished=true;
      if(apply)safeEdit(()=>editor.seekTimecode(field.value));
      display.textContent=tc(editor.get().time);
    };
    field.onkeydown=event=>{
      event.stopPropagation();
      if(event.key==="Enter"){event.preventDefault();finish(true);}
      if(event.key==="Escape"){event.preventDefault();finish(false);}
    };
    field.onblur=()=>finish(false);
  };
  function sequenceSettings() {
    const project=editor.get().project;
    const node=document.createElement("dialog");node.className="studio-dialog";
    node.innerHTML='<h2>Sequence settings</h2><form><label>Start timecode<input name="start" autocomplete="off" spellcheck="false" required></label><label class="drop-frame-row">Drop-frame timecode<input name="drop" type="checkbox"></label><p class="form-status" role="status"></p><footer><button type="button" data-close>Cancel</button><button class="primary">Apply</button></footer></form>';
    const form=node.querySelector("form");
    const dropRate=editor.isDropFrameRate(project.fps);
    form.elements.drop.checked=dropRate&&project.dropFrame!==false;
    form.elements.drop.closest("label").hidden=!dropRate;
    form.elements.start.value=tc(0);
    node.querySelector("[data-close]").onclick=()=>node.close();
    node.addEventListener("close",()=>node.remove(),{once:true});
    form.onsubmit=e=>{
      e.preventDefault();
      try {
        editor.setTimecodeSettings({startTimecode:form.elements.start.value,dropFrame:form.elements.drop.checked});
        node.close();
      } catch(error) { node.querySelector(".form-status").textContent=error.message; }
    };
    document.body.append(node);node.showModal();form.elements.start.select();
  }
  function editPoint(direction) {
    const state=editor.get(), points=editor.editPoints();
    const target=direction>0 ? points.find(point=>point>state.time+1e-6) : points.findLast(point=>point<state.time-1e-6);
    if(target!==undefined)editor.seek(target);
  }
  const selection=()=>{
    const state=editor.get();
    return state.project.clips.filter(c=>group.size?group.has(c.id):c.id===state.selectedId);
  };
  // The clip a playhead command acts on: the selected clip if it spans the playhead, else the top clip there.
  function clipAtPlayhead() {
    const state=editor.get(), t=state.time;
    const spans=c=>c.start<=t+1e-6&&t<c.start+c.duration-1e-6;
    if(state.selected&&spans(state.selected))return state.selected;
    return state.activeClip||state.activeAudio||state.project.clips.filter(c=>c.type==="title"&&spans(c))[0]||null;
  }
  function rippleTrimToPlayhead(edge) {
    const clip=clipAtPlayhead();
    if(!clip)return notify("Park the playhead inside a clip to ripple trim it.");
    const start=clip.start;
    // The playhead frame goes with the end trim, so on the first frame nothing would remain.
    if(edge==="end"&&Math.round(editor.get().time*editor.get().project.fps)<=Math.round(start*editor.get().project.fps))
      return notify("The playhead is on the clip's first frame. Move it into the clip to ripple trim its end.");
    if(safeEdit(()=>{editor.trimEdge(clip.id,edge,editor.get().time,{ripple:true});return true;})&&edge==="start")editor.seek(start);
  }
  function trimSelectedEdit(frames) {
    const state=editor.get(), clip=state.project.clips.find(c=>c.id===selectedEdit?.id);
    if(!clip)return notify("Click a clip edge to select an edit, then trim it with Ctrl/Cmd + arrows.");
    const step=frames/state.project.fps;
    const boundary=selectedEdit.edge==="start"?clip.start+step:clip.start+clip.duration+step;
    safeEdit(()=>editor.trimEdge(clip.id,selectedEdit.edge,boundary,{ripple:rippleEnabled}));
  }
  function nudge(frames) {
    const chosen=withLinked(selection());
    if(!chosen.length)return;
    const step=frames/editor.get().project.fps;
    if(chosen.some(c=>c.start+step<-1e-6))return notify("The selection is already at the start of the sequence.");
    safeEdit(()=>editor.moveClips(chosen.map(c=>({id:c.id,start:c.start+step,track:c.track,type:c.type}))));
  }
  function selectOnTrack(forward) {
    const state=editor.get(), anchor=state.selected||clipAtPlayhead();
    if(!anchor)return notify("Select a clip to choose its track.");
    const chosen=state.project.clips.filter(c=>c.type===anchor.type&&c.track===anchor.track&&
      (forward?c.start+c.duration>state.time+1e-6:c.start<state.time-1e-6));
    group.clear();
    for(const clip of chosen)group.add(clip.id);
    lastProject="";refresh(state);
    notify(chosen.length+" clip"+(chosen.length===1?"":"s")+" selected "+(forward?"forward":"backward")+" on "+(anchor.type==="audio"?"Audio ":"Video ")+Math.abs(anchor.track));
  }
  function deleteSelection(ripple) {
    if(group.size){safeEdit(()=>editor.deleteClips([...group],{ripple}));group.clear();return;}
    safeEdit(()=>editor.deleteSelected({ripple}));
  }
  function toggleLink() {
    const ids=selection().map(c=>c.id);
    if(!ids.length)return notify("Select clips to link or unlink.");
    const result=safeEdit(()=>editor.toggleLink(ids));
    if(result)notify({linked:"Clips linked",unlinked:"Clips unlinked",separated:"Camera sound separated onto its own audio clip"}[result]);
  }
  function fromSource(mode) {
    if(!window.SpliceSource?.isOpen?.())return notify("Open a clip in the source monitor first.");
    SpliceSource.place(mode);
  }
  function pasteClips() {
    const t=editor.get().time, first=Math.min(...clipboard.map(c=>c.start));
    safeEdit(()=>editor.editProject(p=>{
      const links=new Map();
      for(const original of clipboard){
        if(original.mediaId&&!p.media.some(m=>m.id===original.mediaId))throw Error("Paste into a project containing the source media.");
        const c=structuredClone(original);c.start=t+c.start-first;c.id=crypto.randomUUID();delete c.premiereIndex;delete c.premiereTrack;
        if(c.linkGroup){if(!links.has(c.linkGroup))links.set(c.linkGroup,crypto.randomUUID());c.linkGroup=links.get(c.linkGroup);}
        p.clips.push(c);
      }
    }));
  }
  const commands={
    link:toggleLink,
    lift:()=>safeEdit(()=>editor.liftRange()),
    extract:()=>safeEdit(()=>editor.liftRange({extract:true})),
    addEditAll:()=>safeEdit(()=>editor.addEditAll()),
    markClip:()=>safeEdit(()=>editor.markClip()),
    clearMarks:()=>editor.clearMarks(),
    rippleStart:()=>rippleTrimToPlayhead("start"),
    rippleEnd:()=>rippleTrimToPlayhead("end"),
    syncSound:()=>window.SpliceSync?.open(),
  };
  const mod=e=>e.ctrlKey||e.metaKey;
  // A binding matches e.code or the (lower-cased) key, with exactly these modifiers held.
  const press=(keys,{ctrl=false,shift=false,alt=false}={})=>e=>
    [].concat(keys).some(key=>key===e.code||key===(e.key.length===1?e.key.toLowerCase():e.key))&&
    mod(e)===ctrl&&e.shiftKey===shift&&e.altKey===alt;
  const forward=e=>e.key==="ArrowRight"||e.key==="ArrowDown"||e.code==="Period"||e.code==="BracketRight";
  const step=frames=>editor.seek(editor.get().time+frames/editor.get().project.fps);
  const arrows=["ArrowLeft","ArrowRight"];
  // The single keymap: the keyboard handler and the Keyboard and mouse reference both read it.
  const keymap=[
    {keys:"Space",action:"Play / pause",test:press("Space"),run:()=>editor.togglePlay()},
    {keys:"J · K · L",action:"Play reverse · stop · forward (repeat to speed up)",test:press(["j","k","l"]),
      run:e=>e.key.toLowerCase()==="k"?editor.setRate(0):editor.shuttle(e.key.toLowerCase()==="l"?1:-1)},
    {keys:"← · →",action:"Step one frame",test:press(arrows),run:e=>step(forward(e)?1:-1)},
    {keys:"Shift + ← · →",action:"Step five frames",test:press(arrows,{shift:true}),run:e=>step(forward(e)?5:-5)},
    {keys:"↑ · ↓",action:"Go to previous · next edit point",test:press(["ArrowUp","ArrowDown"]),run:e=>editPoint(forward(e)?1:-1)},
    {keys:"I · O",action:"Mark timeline In · Out on the frame under the playhead (both frames are included)",test:press(["i","o"]),run:e=>safeEdit(()=>editor.setMark(e.key.toLowerCase()==="i"?"in":"out"))},
    {keys:"Shift + I · O",action:"Go to the timeline In frame · Out frame",test:press(["i","o"],{shift:true}),run:e=>{
      const out=e.key.toLowerCase()==="o", mark=editor.get().project.marks?.[out?"out":"in"];
      if(Number.isFinite(mark))editor.seek(out?editor.markOutFrame(mark):mark);
    }},
    {keys:"X",action:"Mark In and Out around the selected clip",test:press("x"),run:commands.markClip},
    {keys:"Ctrl/Cmd + Shift + X",action:"Clear timeline In and Out",test:press("x",{ctrl:true,shift:true}),run:commands.clearMarks},
    {keys:"; · '",action:"Lift · extract the In/Out range on unlocked tracks",test:press(["Semicolon","Quote"]),
      run:e=>e.code==="Quote"?commands.extract():commands.lift()},
    {keys:", · .",action:"Insert · overwrite from the source monitor (three-point edit)",test:press(["Comma","Period"]),
      run:e=>fromSource(e.code==="Period"?"overwrite":"insert")},
    {keys:"S · Ctrl/Cmd + K",action:"Add edit to the selected clip at the playhead",test:e=>press("s")(e)||press("k",{ctrl:true})(e),run:()=>safeEdit(()=>editor.split())},
    {keys:"Ctrl/Cmd + Shift + K",action:"Add edit on all unlocked tracks",test:press("k",{ctrl:true,shift:true}),run:commands.addEditAll},
    {keys:"Q · W",action:"Ripple trim clip start · end to the playhead",test:press(["q","w"]),run:e=>rippleTrimToPlayhead(e.key.toLowerCase()==="q"?"start":"end")},
    {keys:"Ctrl/Cmd + ← · →",action:"Trim the selected edit one frame (click an edge to select it)",test:press(arrows,{ctrl:true}),run:e=>trimSelectedEdit(forward(e)?1:-1)},
    {keys:"Ctrl/Cmd + Shift + ← · →",action:"Trim the selected edit five frames",test:press(arrows,{ctrl:true,shift:true}),run:e=>trimSelectedEdit(forward(e)?5:-5)},
    {keys:"Alt + ← · →",action:"Nudge selected clips one frame",test:press(arrows,{alt:true}),run:e=>nudge(forward(e)?1:-1)},
    {keys:"Alt + Shift + ← · →",action:"Nudge selected clips five frames",test:press(arrows,{alt:true,shift:true}),run:e=>nudge(forward(e)?5:-5)},
    {keys:"Ctrl/Cmd + Alt + ← · →",action:"Slip the selected clip one frame",test:press(arrows,{ctrl:true,alt:true}),
      run:e=>{const clip=editor.get().selected;if(clip)safeEdit(()=>editor.slipClip(clip.id,(forward(e)?1:-1)*(Number(clip.speed)||1)/editor.get().project.fps));}},
    {keys:"Alt + , · .",action:"Slide the selected clip one frame",test:press(["Comma","Period"],{alt:true}),
      run:e=>{const clip=editor.get().selected;if(clip)safeEdit(()=>editor.slideClip(clip.id,(forward(e)?1:-1)/editor.get().project.fps));}},
    {keys:"A · Shift + A",action:"Select forward · backward on the selected clip's track",test:e=>press("a")(e)||press("a",{shift:true})(e),run:e=>selectOnTrack(!e.shiftKey)},
    {keys:"Delete · Backspace",action:"Delete the selection (ripple when Ripple is on)",test:press(["Delete","Backspace"]),run:()=>deleteSelection(rippleEnabled)},
    {keys:"Shift + Delete · Backspace",action:"Ripple delete the selection",test:press(["Delete","Backspace"],{shift:true}),run:()=>deleteSelection(true)},
    {keys:"Ctrl/Cmd + L",action:"Link · unlink clips (separates a camera clip's sound)",test:press("l",{ctrl:true}),run:toggleLink},
    {keys:"Ctrl/Cmd + Shift + L",action:"Toggle linked selection",test:press("l",{ctrl:true,shift:true}),run:()=>$("linked-toggle").click()},
    {keys:"= · − · \\",action:"Zoom in · out · fit the whole timeline",test:press(["Equal","Minus","Backslash","NumpadAdd","NumpadSubtract"]),
      run:e=>e.code==="Backslash"?zoomToFit():setZoom(/Equal|Add/.test(e.code)?pps*1.5:pps/1.5)},
    {keys:"M · [ · ]",action:"Add marker · previous · next marker",test:press(["m","BracketLeft","BracketRight"]),run:e=>{
      if(e.key.toLowerCase()==="m")return editMarker();
      const s=editor.get(), times=(s.project.markers||[]).map(m=>m.time);
      editor.seek(forward(e)?Math.min(s.duration,...times.filter(x=>x>s.time+.001)):Math.max(0,...times.filter(x=>x<s.time-.001)));
    }},
    {keys:"N · R",action:"Toggle snapping · ripple mode",test:press(["n","r"]),run:e=>e.key.toLowerCase()==="n"?toggleSnap():toggleRipple()},
    {keys:"Ctrl/Cmd + C · V",action:"Copy · paste clips at the playhead",test:e=>press(["c","v"],{ctrl:true})(e),run:e=>{
      if(e.key.toLowerCase()==="c")clipboard=structuredClone(withLinked(selection()));
      else if(clipboard.length)pasteClips();
    }},
    {keys:"Ctrl/Cmd + Z · Shift + Z",action:"Undo · redo",test:e=>press("z",{ctrl:true})(e)||press("z",{ctrl:true,shift:true})(e),run:e=>e.shiftKey?editor.redo():editor.undo()},
    {keys:"Ctrl/Cmd + S · Shift + S",action:"Save Premiere project · save working project",anywhere:true,
      test:e=>press("s",{ctrl:true})(e)||press("s",{ctrl:true,shift:true})(e),
      run:e=>e.shiftKey?busy(() => window.SpliceSession?.save?.("splice") ?? editor.saveProject("splice"), "Splice project saved"):$("save-project").click()},
    {keys:"Ctrl/Cmd + O",action:"Open project",anywhere:true,test:press("o",{ctrl:true}),run:()=>$("open-project").click()},
    {keys:"Drag clip edge",action:"Trim (both sides of a linked clip)"},
    {keys:"Alt + drag clip edge",action:"Trim only this side of a linked clip: J-cut or L-cut"},
    {keys:"Shift + drag clip edge",action:"Roll the cut between adjacent clips"},
    {keys:"Alt + Shift + drag clip edge",action:"Roll only this side's cut: J-cut or L-cut against the picture"},
    {keys:"Alt + drag clip",action:"Slip the source under the edit"},
    {keys:"Alt + Shift + drag clip",action:"Slide the clip between its neighbours"},
    {keys:"Ctrl/Cmd + click",action:"Select multiple clips"},
    {keys:"Ctrl/Cmd or Alt + scroll",action:"Zoom the timeline around the pointer"},
    {keys:"Click the program timecode",action:"Type a timecode to move the playhead (+/− for relative)"},
    {keys:"Click the sync number",action:"Move or slip an out-of-sync clip back into sync"},
  ];
  window.SpliceKeymap=keymap.map(({keys,action})=>({keys,action}));
  // Only controls that take typed text (or list type-ahead) keep keys from the timeline. A slider
  // keeps its arrow keys and a checkbox or button its Space and Enter; every other shortcut works.
  const textEntry=new Set(["text","search","email","number","password","tel","url","date","time","datetime-local","month","week"]);
  function keyBelongsToControl(element, e) {
    if (!element || element===document.body) return false;
    if (element.isContentEditable || element.tagName==="TEXTAREA" || element.tagName==="SELECT") return true;
    if (element.tagName!=="INPUT") return false;
    const type=(element.getAttribute("type")||"text").toLowerCase();
    if (textEntry.has(type)) return true;
    if (type==="range") return /^(Arrow|Home$|End$|Page)/.test(e.key);
    return e.key===" " || e.key==="Enter";
  }
  window.SpliceKeys={keyBelongsToControl};
  window.SpliceWorkspace={refresh:()=>renderTimeline(editor.get()),selection:()=>selection().map(clip=>clip.id)};
  document.addEventListener("keydown", (e) => {
    if (document.querySelector("dialog:modal") || e.target?.closest?.(".source-dialog, .sync-menu")) return;
    const typing=keyBelongsToControl(document.activeElement,e);
    const binding=keymap.find(row=>row.test&&(row.anywhere||!typing)&&row.test(e));
    if(!binding)return;
    e.preventDefault();
    binding.run(e);
  });
})();
