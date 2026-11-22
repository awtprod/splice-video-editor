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
        track: 4,
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
  let playbackRate = 1;
  let lastPaintedFrame = -1;
  let video = null;
  let transitionVideo = null;
  let audio = null;
  const audioElements = new Map();
  const layerVideos = new Map();
  let activeVideoId = null;
  let transitionVideoId = null;
  let preloadClipId = null;
  let mutedTracks = { audio: false, video: false, title: false };
  let revision = 0;
  let gesture = false;
  let gestureBefore = null;
  let audioContext;
  const audioNodes = new WeakMap();
  function routeAudio(element) {
    if (!audioContext) audioContext = new AudioContext();
    let route = audioNodes.get(element);
    if (!route) {
      const source = audioContext.createMediaElementSource(element);
      const gain = audioContext.createGain();
      const pan = audioContext.createStereoPanner();
      const analyser=audioContext.createAnalyser();
      analyser.fftSize=256;
      source.connect(gain).connect(pan).connect(analyser).connect(audioContext.destination);
      route={source,gain,pan,analyser};
      audioNodes.set(element,route);
    }
    element.volume=1;
    return route;
  }
  function meters() {
    const result={};
    for (const clip of project.clips.filter(c=>c.type==='audio')) {
      const route=audioNodes.get(audioElements.get(clip.id));
      if (!route) continue;
      const samples=new Float32Array(route.analyser.fftSize);
      route.analyser.getFloatTimeDomainData(samples);
      const peak=Math.max(...samples.map(Math.abs));
      result[String(clip.track)]=Math.max(result[String(clip.track)]||0,peak);
    }
    return result;
  }
  function setAudio(element,clip) {
    if (!element || !clip) return;
    const route=routeAudio(element);
    const mix=trackSettings(clip.track);
    route.gain.gain.setTargetAtTime(audibility(clip),audioContext.currentTime,.012);
    route.pan.pan.setTargetAtTime(clamp(((Number(clip.pan)||0)+(Number(mix.pan)||0))/100,-1,1),audioContext.currentTime,.012);
  }
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
    if (gesture) return;
    revision += 1;
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
  const transitionLength = clip => clamp(Number(clip.transitionDuration || .5), 1 / project.fps, 2);
  const trackSettings = track => project.trackMix?.[String(track)] || {};
  const soloActive = () => Object.values(project.trackMix || {}).some(m => m.solo);
  const audibility = clip => {
    const mix = trackSettings(clip.track);
    if (mutedTracks.audio || mix.mute || (soloActive() && !mix.solo)) return 0;
    const local = time - clip.start;
    const fadeIn = Math.max(0,Number(clip.fadeIn||0));
    const fadeOut = Math.max(0,Number(clip.fadeOut||0));
    const fade = Math.min(1, fadeIn ? local/fadeIn : 1, fadeOut ? (clip.duration-local)/fadeOut : 1);
    return clamp((clip.volume??100)/100,0,2) * clamp((mix.gain??100)/100,0,2) * clamp(fade,0,1);
  };
  const filterFor = (clip) => {
    const c = clip.color || {};
    return `brightness(${Number(c.brightness ?? 100)}%) contrast(${Number(c.contrast ?? 100)}%) saturate(${Number(c.saturation ?? 100)}%)`;
  };
  const gradeGains = c => {
    const exposure=clamp(Number(c.exposure)||0,-4,4), temperature=clamp(Number(c.temperature)||0,-100,100)/100;
    const tint=clamp(Number(c.tint)||0,-100,100)/100, gain=2**exposure;
    return [gain*(1+.35*temperature-.1*tint),gain*(1+.2*tint),gain*(1-.35*temperature-.1*tint)];
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
    if (!clip || !["dissolve", "wipe"].includes(clip.transition)) return null;
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
    if (clip?.id === preloadClipId && clip.id !== activeVideoId) {
      const outgoing = video;
      video = transitionVideo;
      transitionVideo = outgoing;
      transitionVideoId = activeVideoId;
      activeVideoId = clip.id;
      preloadClipId = null;
    }
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
      if (playing && playbackRate > 0) video.play().catch(() => {});
      else video.pause();
    }
    const previous = clip && time < clip.start + transitionLength(clip) ? preceding(clip) : null;
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
      const target = (previous.in || 0) + previous.duration + time - clip.start;
      if (forceSeek || Math.abs(transitionVideo.currentTime - target) > .3) {
        try { transitionVideo.currentTime = target; } catch (_) { /* Metadata pending. */ }
      }
      if (playing && playbackRate > 0) transitionVideo.play().catch(() => {});
      else transitionVideo.pause();
    } else transitionVideo.pause();
    if (playing && playbackRate > 0 && clip && !previous) {
      const upcoming = project.clips.find(c => c.type === "video" && c.track === clip.track &&
        c.start >= time && c.start <= time + 1.5 && c.id !== clip.id);
      if (upcoming && upcoming.id !== preloadClipId) {
        const next = mediaOf(upcoming);
        if (next?.src) {
          transitionVideo.pause();
          transitionVideo.src = next.src;
          transitionVideo.load();
          transitionVideo.addEventListener("loadedmetadata", () => {
            if (preloadClipId === upcoming.id) transitionVideo.currentTime = upcoming.in || 0;
          }, { once: true });
          preloadClipId = upcoming.id;
          transitionVideoId = null;
        }
      }
    }
    const visible = project.clips.filter(c=>c.type==='video' && c.start<=time && time<c.start+c.duration);
    const currentVideoIds=new Set(visible.map(c=>c.id));
    for (const [id,element] of layerVideos) if (!currentVideoIds.has(id) || id===activeVideoId) {
      element.pause();element.removeAttribute('src');element.load();element.remove();layerVideos.delete(id);
    }
    for (const layer of visible) {
      if (layer.id===activeVideoId) continue;
      const item=mediaOf(layer);
      if (!item?.src) continue;
      let element=layerVideos.get(layer.id);
      if(!element) {
        element=document.createElement('video');element.muted=true;element.playsInline=true;
        element.preload='auto';element.style.display='none';element.src=item.src;document.body.append(element);layerVideos.set(layer.id,element);
      }
      const target=(layer.in||0)+time-layer.start;
      if(forceSeek||Math.abs(element.currentTime-target)>.3) try{element.currentTime=target}catch(_){}
      if(playing && playbackRate > 0) element.play().catch(()=>{}); else element.pause();
    }
    const currentIds = new Set(audioClips.map((c) => c.id));
    for (const [id, element] of audioElements)
      if (!currentIds.has(id)) {
        element.pause();
        const route=audioNodes.get(element);
        if (route) { route.source.disconnect(); route.gain.disconnect(); route.pan.disconnect(); route.analyser.disconnect(); audioNodes.delete(element); }
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
      if (playing && playbackRate > 0) element.play().catch(() => {});
      else element.pause();
      setAudio(element,audioClip);
    }
    audio = audioClips.length ? audioElements.get(audioClips[0].id) : null;
    if (clip) {
      setAudio(video,clip);
      if (clip.embeddedAudio === false) audioNodes.get(video).gain.gain.value=0;
    }
  }
  function animated(clip,key,base) {
    const points=(clip.keyframes?.[key]||[]).filter(p=>Number.isFinite(p.time)&&Number.isFinite(p.value)).sort((a,b)=>a.time-b.time);
    if (!points.length) return base;
    const local=time-clip.start;
    if (local<=points[0].time) return points[0].value;
    for (let i=1;i<points.length;i++) if(local<=points[i].time) {
      const a=points[i-1],b=points[i],u=(local-a.time)/(b.time-a.time);
      return a.value+(b.value-a.value)*u;
    }
    return points.at(-1).value;
  }
  const layerCanvases = new Map();
  function paintVideo(ctx,element,clip,canvas,alpha=1) {
    if (!element || element.readyState < 2) return;
    const sourceWidth=element.videoWidth||canvas.width;
    const sourceHeight=element.videoHeight||canvas.height;
    const fit=Math.min(canvas.width/sourceWidth,canvas.height/sourceHeight);
    const scale=clamp(animated(clip,"scale",Number(clip.transform?.scale??100))/100,.1,4);
    const x=clamp(animated(clip,"x",Number(clip.transform?.x??0)),-100,100)*canvas.width/100;
    const y=clamp(animated(clip,"y",Number(clip.transform?.y??0)),-100,100)*canvas.height/100;
    const rotation=clamp(animated(clip,"rotation",Number(clip.transform?.rotation??0)),-180,180)*Math.PI/180;
    const lut=clip.color?.lut;
    let target=ctx;
    const gains=gradeGains(clip.color||{});
    const graded=gains.some(g=>Math.abs(g-1)>.0001);
    if ((lut && window.SpliceLUT) || graded) {
      let layer=layerCanvases.get(clip.id);
      if (!layer) { layer=document.createElement('canvas'); layerCanvases.set(clip.id,layer); }
      if (layer.width!==canvas.width || layer.height!==canvas.height) {layer.width=canvas.width;layer.height=canvas.height;}
      target=layer.getContext('2d',{willReadFrequently:true});
      target.clearRect(0,0,layer.width,layer.height);
    }
    alpha *= clamp(animated(clip,"opacity",Number(clip.opacity??100))/100,0,1);
    target.save(); target.globalAlpha=target===ctx?alpha:1; target.filter=filterFor(clip);
    target.translate(canvas.width/2+x,canvas.height/2+y); target.rotate(rotation);
    target.drawImage(element,-sourceWidth*fit*scale/2,-sourceHeight*fit*scale/2,sourceWidth*fit*scale,sourceHeight*fit*scale);
    target.restore();
    if (target!==ctx) {
      const image=target.getImageData(0,0,canvas.width,canvas.height);
      if (graded) {
        const pixels=image.data;
        for(let i=0;i<pixels.length;i+=4) {
          pixels[i]=Math.min(255,pixels[i]*gains[0]);
          pixels[i+1]=Math.min(255,pixels[i+1]*gains[1]);
          pixels[i+2]=Math.min(255,pixels[i+2]*gains[2]);
        }
      }
      if (lut && window.SpliceLUT) window.SpliceLUT.applyImageData(image,lut,clip.color.lutIntensity??100);
      target.putImageData(image,0,0);
      ctx.save();ctx.globalAlpha=alpha;ctx.drawImage(target.canvas,0,0);ctx.restore();
    }
  }
  function drawFrame(canvas) {
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#080a0a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!mutedTracks.video) {
      const visible=project.clips.filter(c=>c.type==='video' && c.start<=time && time<c.start+c.duration)
        .sort((a,b)=>a.track-b.track || a.start-b.start);
      for (const clip of visible) {
        const element=clip.id===activeVideoId ? video : layerVideos.get(clip.id);
        const previous=clip.id===activeVideoId && time<clip.start+transitionLength(clip) ? preceding(clip) : null;
        if(previous && transitionVideo.readyState>=2) paintVideo(ctx,transitionVideo,previous,canvas);
        const alpha=(previous && clip.transition==='dissolve') || clip.transition==='fade' ?
          clamp((time-clip.start)/transitionLength(clip),0,1) : 1;
        if (previous && clip.transition==='wipe') {
          ctx.save();ctx.beginPath();ctx.rect(0,0,canvas.width*clamp((time-clip.start)/transitionLength(clip),0,1),canvas.height);ctx.clip();
        }
        paintVideo(ctx,element,clip,canvas,alpha);
        if (previous && clip.transition==='wipe') ctx.restore();
      }
      ctx.globalAlpha=1;ctx.filter='none';
    }
    const title = mutedTracks.title ? null : at("title", time);
    if (title?.title) {
      ctx.fillStyle = "#fff";
      const style=title.titleStyle||{};
      ctx.font = `bold ${Math.round(canvas.height * (Number(style.fontSize)||8)/100)}px "${style.fontFamily||'DM Sans'}"`;
      ctx.fillStyle=style.color||'#ffffff';
      ctx.textAlign = ['left','center','right'].includes(style.align) ? style.align : 'center';
      ctx.textBaseline = "middle";
      ctx.shadowColor = "#000";
      ctx.shadowBlur = canvas.height * 0.025;
      const tx=canvas.width * (Number(style.x??50)/100), ty=canvas.height * (Number(style.y??78)/100);
      const lines=title.title.split(/\r?\n/).slice(0,20);
      const lineHeight=Math.round(canvas.height * (Number(style.fontSize)||8)/100)*1.2;
      for (let i=0;i<lines.length;i++) {
        const lineY=ty+(i-(lines.length-1)/2)*lineHeight;
        if (Number(style.strokeWidth)>0) {
          ctx.lineWidth=Number(style.strokeWidth)*canvas.height/180;
          ctx.strokeStyle=style.strokeColor||'#000000';
          ctx.strokeText(lines[i],tx,lineY);
        }
        ctx.fillText(lines[i],tx,lineY);
      }
      ctx.shadowBlur = 0;
    }
  }
  function tick(timestamp) {
    if (playing) {
      time = clamp(time + Math.min((timestamp - lastTick) / 1000, 0.1) * playbackRate, 0, duration());
      if ((playbackRate > 0 && time >= duration()) || (playbackRate < 0 && time <= 0)) {
        playing = false;
        video?.pause();
        audio?.pause();
      }
      syncMedia(playbackRate < 0);
      const frame = Math.floor(time * project.fps);
      if (frame !== lastPaintedFrame) { lastPaintedFrame = frame; notify(); }
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
      revision,
      selectedId,
      selected: selected(),
      time,
      playing,
      mutedTracks,
      audioMeters: meters(),
      duration: duration(),
      video,
      audio,
      activeClip: at("video", time),
      activeTitle: at("title", time),
      activeAudio: at("audio", time),
    }),
    drawFrame,
    editProject(fn) {
      const previous=structuredClone(project);
      snapshot();
      try { fn(project); validateProject(project); }
      catch(error) { project=previous; history.pop(); revision += 1; throw error; }
      project.duration=duration(); time=clamp(time,0,project.duration); syncMedia(true); notify();
    },
    updateTrackMix(track, patch) {
      const key=String(track);
      const next={ gain:100, pan:0, mute:false, solo:false, ...project.trackMix?.[key], ...patch };
      if (JSON.stringify(next)===JSON.stringify(project.trackMix?.[key])) return;
      snapshot();
      project.trackMix ||= {};
      project.trackMix[key] = next;
      syncMedia(); notify();
    },
    beginGesture() { if (gesture) return; gestureBefore=structuredClone(project); gesture=true; },
    endGesture() {
      if (!gesture) return;
      gesture=false;
      if (JSON.stringify(gestureBefore)!==JSON.stringify(project)) {
        history.push(gestureBefore);if(history.length>50) history.shift();future=[];revision++;
      }
      gestureBefore=null;notify();
    },
    renameProject(name) { const next=String(name).trim(); if (!next || next===project.name) return; snapshot(); project.name=next; notify(); },
    setRate(rate) {
      const requested=Number(rate);
      if (!Number.isFinite(requested)) return;
      if (requested===0) { playing=false;syncMedia(true);notify();return; }
      playbackRate=clamp(requested,-4,4);
      if(time>=duration()&&playbackRate>0) time=0;
      if(time<=0&&playbackRate<0) time=duration();
      playing=true;
      for (const element of [video,transitionVideo,...audioElements.values(),...layerVideos.values()]) if(element) element.playbackRate=Math.max(.25,Math.abs(playbackRate));
      if(playbackRate>0) audioContext?.resume().catch(()=>{});
      syncMedia(true);notify();
    },
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
      if (playing && playbackRate < 0) playbackRate=1;
      if (playing && time >= duration()) time = 0;
      if (!playing) time = snap(time);
      if (playing) audioContext?.resume().catch(() => {});
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
    async relinkMediaItem(itemId, file) {
      const item = project.media.find(m => m.id === itemId);
      if (!item) throw new Error("Media item is unavailable.");
      const kind = file.type.startsWith("audio/") ? "audio" : file.type.startsWith("video/") ? "video" : "";
      if (kind !== item.type) throw new Error("Choose a " + item.type + " file.");
      const decoded = await fileToMedia(file);
      const needed = Math.max(0,...project.clips.filter(c=>c.mediaId===itemId).map(c=>c.in+c.duration));
      if (decoded.duration + .02 < needed) throw new Error(file.name + " is shorter than the source used on this timeline.");
      snapshot();
      item.duration=decoded.duration; item.src=decoded.src; item.offline=false;
      item.path=file.name;
      await SpliceMedia.remember(item,file);
      activeVideoId=null; syncMedia(true); notify();
    },
    async relinkMedia(files) {
      const matches = Array.from(files).filter((file) =>
        project.media.some((item) => SpliceMedia.matches(item, file)),
      );
      for (const file of matches) {
        const count=project.media.filter(item=>SpliceMedia.matches(item,file)).length;
        if(count!==1) throw new Error("Choose a file for each offline media item individually.");
      }
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
      const item = mediaOf(clip);
      const requestedIn = "in" in patch ? Number(patch.in) : clip.in;
      const requestedDuration = "duration" in patch ? Number(patch.duration) : clip.duration;
      if (!Number.isFinite(requestedIn) || requestedIn < 0 || !Number.isFinite(requestedDuration) || requestedDuration <= 0 ||
          (item?.duration && requestedIn + requestedDuration > item.duration + .01)) {
        throw new Error("Clip extends beyond available source footage.");
      }
      if (Object.entries(patch).every(([key,value]) => JSON.stringify(clip[key])===JSON.stringify(value))) return;
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
    trimEdge(id, edge, atTime, { ripple = false } = {}) {
      const clip = project.clips.find(c => c.id === id);
      if (!clip) return;
      const oldStart = clip.start, oldEnd = oldStart + clip.duration;
      const boundary = snap(atTime);
      const newStart = edge === "start" ? clamp(boundary, Math.max(0, oldStart - clip.in), oldEnd - 1/project.fps) : oldStart;
      const mediaItem = mediaOf(clip);
      const sourceEnd = mediaItem?.duration || Infinity;
      const newEnd = edge === "end" ? clamp(boundary, oldStart + 1/project.fps, oldStart + sourceEnd - clip.in) : oldEnd;
      const shift = (edge === "start" ? newStart - oldStart : newEnd - oldEnd);
      if (!shift) return;
      snapshot();
      if (edge === "start") { clip.start = newStart; clip.in += shift; clip.duration = oldEnd - newStart; }
      else clip.duration = newEnd - oldStart;
      if (ripple) for (const next of project.clips) if (next.id !== id && next.start >= oldEnd - .001) next.start = Math.max(0, snap(next.start + shift));
      project.duration = duration(); syncMedia(true); notify();
    },
    rollEdit(leftId, rightId, boundary) {
      const left = project.clips.find(c => c.id === leftId), right = project.clips.find(c => c.id === rightId);
      if (!left || !right || left.type !== right.type || left.track !== right.track || Math.abs(left.start + left.duration - right.start) > .05) return;
      const end = right.start + right.duration, source = mediaOf(left)?.duration || Infinity;
      const cut = clamp(snap(boundary), Math.max(left.start+1/project.fps,right.start-right.in), Math.min(end-1/project.fps,left.start+source-left.in));
      if (!Number.isFinite(cut) || cut === right.start) return;
      snapshot(); left.duration = cut-left.start; right.in += cut-right.start; right.duration = end-cut; right.start=cut;
      syncMedia(true); notify();
    },
    slipClip(id, delta) {
      const clip = project.clips.find(c => c.id === id), source = clip && mediaOf(clip)?.duration;
      if (!clip || !source) return;
      const next = clamp(snap(clip.in+delta),0,Math.max(0,source-clip.duration));
      if (next === clip.in) return;
      snapshot(); clip.in=next; syncMedia(true); notify();
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
        track: 4,
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
      revision += 1;
      syncMedia(true);
      notify();
    },
    redo() {
      if (!future.length) return;
      history.push(structuredClone(project));
      project = future.pop();
      revision += 1;
      syncMedia(true);
      notify();
    },
    newProject(settings = {}) {
      const name=String(settings.name||"Untitled project").trim();
      const width=Number(settings.width||1920), height=Number(settings.height||1080), fps=Number(settings.fps||25);
      if (!name || !Number.isInteger(width) || !Number.isInteger(height) || width<16 || height<16 || width>3840 || height>2160 ||
          !Number.isFinite(fps) || fps<1 || fps>60) throw new Error("Invalid sequence settings.");
      snapshot();
      project = {
        name,
        width,
        height,
        fps,
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
    async exportVideo(options = {}, onProgress = () => {}, signal) {
      if (!duration()) throw new Error("Add clips to the timeline before exporting.");
      if (signal?.aborted) throw signal.reason || new DOMException("Export cancelled", "AbortError");
      const prepared = window.SpliceMedia?.forPremiere
        ? await window.SpliceMedia.forPremiere(project)
        : structuredClone(project);
      if (signal?.aborted) throw signal.reason || new DOMException("Export cancelled", "AbortError");
      prepared.renderOptions = { mutedTracks };
      const response = await fetch("/api/exports", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: prepared, options }),
        signal: AbortSignal.timeout(120000),
      });
      if (!response.ok) throw new Error(await response.text());
      const { id } = await response.json();
      if (typeof id !== "string" || !id) throw new Error("Export server did not return a job ID.");
      const endpoint = "/api/exports/" + encodeURIComponent(id);
      let cancelSent = false;
      const cancelJob = async () => {
        if (cancelSent) return;
        cancelSent = true;
        const cancelled = await fetch(endpoint + "/cancel", { method: "POST", signal: AbortSignal.timeout(120000) });
        if (!cancelled.ok) throw new Error(await cancelled.text());
      };
      try {
        if (signal?.aborted) { await cancelJob(); throw signal.reason || new DOMException("Export cancelled", "AbortError"); }
        while (true) {
          await new Promise((resolve, reject) => {
            if (signal?.aborted) { reject(signal.reason || new DOMException("Export cancelled", "AbortError")); return; }
            const onAbort = () => { clearTimeout(timer); reject(signal.reason || new DOMException("Export cancelled", "AbortError")); };
            const timer = setTimeout(() => { signal?.removeEventListener("abort", onAbort); resolve(); }, 500);
            signal?.addEventListener("abort", onAbort, { once: true });
          });
          const status = await fetch(endpoint, { signal });
          if (!status.ok) throw new Error(await status.text());
          const job = await status.json();
          onProgress(job);
          if (job.state === "failed") throw new Error(job.error || "Export failed.");
          if (job.state === "cancelled") throw new Error("Export cancelled.");
          if (job.state === "complete") {
            const safe = (project.name || "Untitled").replace(/[^a-z0-9_-]+/gi, "-").replace(/^-+|-+$/g, "") || "Untitled";
            const downloadURL = endpoint + "/file?name=" + encodeURIComponent(safe + ".mp4");
            const anchor = document.createElement("a");
            anchor.href = downloadURL;
            anchor.download = safe + ".mp4";
            anchor.click();
            return { id, url: endpoint + "/preview", downloadURL };
          }
        }
      } catch (error) {
        if (signal?.aborted) await cancelJob();
        throw error;
      }
    },
  };
  window.Editor = Editor;
})();
