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
        transitionDuration: .48,
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
      route={source,gain,pan,analyser,samples:new Float32Array(analyser.fftSize)};
      audioNodes.set(element,route);
    }
    element.volume=1;
    return route;
  }
  let meterSampleAt=-Infinity, meterValues={};
  function meters() {
    const now=performance.now();
    if(now-meterSampleAt<100)return meterValues;
    meterSampleAt=now; meterValues={};
    const active = new Map([...audioElements,...layerVideos]);
    if(activeVideoId && video)active.set(activeVideoId,video);
    for(const clip of project.clips) {
      const route=audioNodes.get(active.get(clip.id));
      if(!route)continue;
      route.analyser.getFloatTimeDomainData(route.samples);
      let peak=0;
      for(const sample of route.samples)peak=Math.max(peak,Math.abs(sample));
      meterValues[clip.track]=Math.max(meterValues[clip.track]||0,peak);
    }
    return meterValues;
  }
  function disconnectAudio(element) {
    const route=audioNodes.get(element);
    if(!route)return;
    route.source.disconnect();route.gain.disconnect();route.pan.disconnect();route.analyser.disconnect();
    audioNodes.delete(element);
  }
  function setAudio(element,clip) {
    if (!element || !clip) return;
    const route=routeAudio(element);
    const mix=trackSettings(clip.track);
    const gain=clip.type==='video' && clip.embeddedAudio===false ? 0 : audibility(clip);
    const pan=clamp(((Number(clip.pan)||0)+(Number(mix.pan)||0))/100,-1,1);
    if (gain!==route.gainLevel) {
      route.gain.gain.setTargetAtTime(gain,audioContext.currentTime,.012);
      route.gainLevel=gain;
    }
    if (pan!==route.panLevel) {
      route.pan.pan.setTargetAtTime(pan,audioContext.currentTime,.012);
      route.panLevel=pan;
    }
  }
  function setMediaPlaying(element, shouldPlay) {
    if (shouldPlay && element.paused) element.play().catch(() => {});
    else if (!shouldPlay && !element.paused) element.pause();
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
  const trackKey = (kind, track) => `${kind}:${track}`;
  const trackState = (kind, track, source = project) =>
    source.trackState?.[trackKey(kind, track)] ||
    (kind === "title" ? source.trackState?.[trackKey("video", track)] : null) || {};
  const trackLocked = (kind, track, source = project) => !!trackState(kind, track, source).locked;
  const syncLocked = (kind, track, source = project) => trackState(kind, track, source).syncLocked !== false;
  const ensureEditable = (clip, destination = clip.track) => {
    if (trackLocked(clip.type, clip.track) || trackLocked(clip.type, destination))
      throw new Error(`Unlock ${clip.type} track ${Math.abs(clip.track)} before editing.`);
  };
  const linkedClips = (clip) => clip.linkGroup
    ? project.clips.filter((other) => other.id !== clip.id && other.linkGroup === clip.linkGroup) : [];
  const ensureLinkedEditable = (clip) => {
    ensureEditable(clip);
    for (const other of linkedClips(clip)) ensureEditable(other);
  };
  const assertLinksStayAligned = (before, after) => {
    const current = new Map(after.clips.map((clip) => [clip.id, clip]));
    const groups = new Map();
    for (const clip of before.clips) if (clip.linkGroup) {
      if (!groups.has(clip.linkGroup)) groups.set(clip.linkGroup, []);
      groups.get(clip.linkGroup).push(clip);
    }
    for (const [key, members] of groups) {
      const afterMembers = after.clips.filter((clip) => clip.linkGroup === key);
      if (afterMembers.length && afterMembers.length !== members.length)
        throw new Error("Linked audio and video must be edited together.");
      if (!afterMembers.length) continue;
      const changes = members.map((clip) => {
        const next = current.get(clip.id);
        if (!next) throw new Error("Linked audio and video must be edited together.");
        return [next.start - clip.start, next.duration - clip.duration, next.in - clip.in];
      });
      for (const values of changes.slice(1))
        if (values.some((value, index) => Math.abs(value - changes[0][index]) > .001))
          throw new Error("Linked audio and video must stay aligned.");
    }
  };
  const sameLaneOverlap = (left, right) => left.type === right.type && left.track === right.track &&
    left.start < right.start + right.duration - .001 && right.start < left.start + left.duration - .001;
  const assertNoNewOverlaps = (before, after) => {
    const original = new Map(before.clips.map(clip => [clip.id, clip]));
    for (const clip of after.clips) {
      const prior = original.get(clip.id);
      if (prior && prior.start === clip.start && prior.duration === clip.duration && prior.track === clip.track && prior.type === clip.type) continue;
      for (const other of after.clips) {
        if (other.id === clip.id || !sameLaneOverlap(clip, other)) continue;
        const oldOther = original.get(other.id);
        if (!prior || !oldOther || !sameLaneOverlap(prior, oldOther))
          throw new Error(`Move blocked: ${clip.type} track ${Math.abs(clip.track)} already has a clip there.`);
      }
    }
  };
  const assertLockedTracksUnchanged = (before, after) => {
    const oldClips = new Map(before.clips.map((clip) => [clip.id, clip]));
    const newClips = new Map(after.clips.map((clip) => [clip.id, clip]));
    for (const clip of before.clips)
      if (trackLocked(clip.type, clip.track, before) && JSON.stringify(clip) !== JSON.stringify(newClips.get(clip.id)))
        throw new Error(`Unlock ${clip.type} track ${Math.abs(clip.track)} before editing.`);
    for (const clip of after.clips)
      if (trackLocked(clip.type, clip.track, after) && JSON.stringify(clip) !== JSON.stringify(oldClips.get(clip.id)))
        throw new Error(`Unlock ${clip.type} track ${Math.abs(clip.track)} before editing.`);
  };
  const at = (kind, t) =>
    project.clips
      .filter(
        (c) => c.type === kind && c.start <= t && t < c.start + c.duration,
      )
      .sort((a, b) => b.track - a.track)[0];
  const transitionLength = clip => clamp(Number(clip.transitionDuration || .5), 1 / project.fps, 2);
  const transitionBefore = clip => clamp(Number(clip.transitionBefore)||0,0,transitionLength(clip));
  const sourceTime = (clip, atTime) => {
    const speed=clamp(Number(clip.speed)||1,.1,16), local=atTime-clip.start;
    return Number(clip.in||0)+(clip.reverse ? clip.duration-local : local)*speed;
  };
  const trackSettings = track => project.trackMix?.[String(track)] || {};
  const soloActive = () => Object.values(project.trackMix || {}).some(m => m.solo);
  let transitionFollowers=new Map();
  const followingTransition = clip => transitionFollowers.get(clip.id);
  function indexTransitions() {
    const cuts=new Map();transitionFollowers=new Map();
    for(const clip of project.clips) if(['dissolve','wipe'].includes(clip.transition))
      cuts.set(`${clip.type}:${clip.track}:${Math.round(clip.start*project.fps)}`,clip);
    for(const clip of project.clips) {
      const next=cuts.get(`${clip.type}:${clip.track}:${Math.round((clip.start+clip.duration)*project.fps)}`);
      if(next && next.id!==clip.id) transitionFollowers.set(clip.id,next);
    }
  }
  const audibility = clip => {
    const mix = trackSettings(clip.track);
    if (mutedTracks.audio || mix.mute || (soloActive() && !mix.solo)) return 0;
    const local = time - clip.start;
    const fadeIn = clamp(Number(clip.fadeIn)||0,0,clip.duration);
    const fadeOut = clamp(Number(clip.fadeOut)||0,0,clip.duration);
    let fade = Math.min(1, fadeIn ? local/fadeIn : 1, fadeOut ? (clip.duration-local)/fadeOut : 1);
    if(['dissolve','wipe','fade'].includes(clip.transition)) fade*=clamp((time-clip.start+transitionBefore(clip))/transitionLength(clip),0,1);
    const next=followingTransition(clip);
    if(next) fade*=clamp(1-(time-next.start+transitionBefore(next))/transitionLength(next),0,1);
    return clamp(animated(clip,'volume',Number(clip.volume??100))/100,0,2) * clamp((mix.gain??100)/100,0,2) * clamp(fade,0,1);
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
  function preceding(clip, source = project) {
    if (!clip || !["dissolve", "wipe"].includes(clip.transition)) return null;
    return (
      source.clips
        .filter(
          (c) =>
            c.type === "video" &&
            c.id !== clip.id &&
            source.media.find(m=>m.id===c.mediaId)?.nativeGenerator !== 1414680150 &&
            c.track === clip.track &&
            c.start < clip.start &&
            c.start + c.duration >= clip.start - 0.05,
        )
        .sort((a, b) => b.start - a.start)[0] || null
    );
  }
  function transitionTail(clip, position) {
    if (position>=clip.start && position<clip.start+transitionLength(clip)-transitionBefore(clip))
      return preceding(clip);
  }
  function validateNewTransition(clip, source) {
    if (!['dissolve','wipe'].includes(clip.transition)) return;
    const prior = preceding(clip, source);
    if (!prior) return;
    const media = source.media.find(m=>m.id===prior.mediaId);
    if (!media || media.offline) return;
    const speed = Number(prior.speed)||1;
    const available = prior.reverse ? prior.in/speed : (media.duration-prior.in-prior.duration*speed)/speed;
    const needed = Math.min(transitionLength(clip)-transitionBefore(clip), clip.duration);
    if (available+1e-6<needed)
      throw Error('This transition needs '+needed.toFixed(2)+' s of unused outgoing footage.');
  }
  function syncMedia(forceSeek = false) {
    ensureElements();
    indexTransitions();
    if (window.SpliceMedia?.prepareNested) for (const clip of project.clips) {
      if (clip.type==='title' || clip.start-transitionBefore(clip)>time+1.5 || clip.start+clip.duration<time) continue;
      const item=mediaOf(clip);
      if (item?.nestedProject && !item.src) window.SpliceMedia.prepareNested(item);
    }
    const clip = at("video", time);
    const audioClips = project.clips.filter(c=>c.type==='audio' && c.start-transitionBefore(c)<=time &&
      time<c.start+c.duration+(followingTransition(c)?transitionLength(followingTransition(c))-transitionBefore(followingTransition(c)):0));
    const mediaItem = clip && mediaOf(clip);
    if (clip && clip.id !== activeVideoId) {
      const preparedLayer=layerVideos.get(clip.id);
      const prepared=clip.id===preloadClipId?transitionVideo:preparedLayer;
      if(prepared){
        const outgoing=video, outgoingId=activeVideoId;
        const staysVisible=project.clips.some(c=>c.id===outgoingId && c.start<=time && time<c.start+c.duration);
        video=prepared;
        activeVideoId=clip.id;
        if(preparedLayer)layerVideos.delete(clip.id);
        if(staysVisible){
          layerVideos.set(outgoingId,outgoing);
          if (!preparedLayer) {
            transitionVideo = null;
            transitionVideoId = null;
            preloadClipId = null;
            ensureElements();
          }
        }else{
          if(preparedLayer){
            transitionVideo.pause();disconnectAudio(transitionVideo);
            transitionVideo.removeAttribute('src');transitionVideo.load();transitionVideo.remove();
          }
          transitionVideo=outgoing;transitionVideoId=outgoingId;preloadClipId=null;
        }
      }
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
      video.muted=false;
      const target = sourceTime(clip,time);
      if (forceSeek || Math.abs(video.currentTime - target) > (clip.reverse ? .5/project.fps : .3)) {
        try {
          video.currentTime = target;
        } catch (_) {
          /* Metadata may still be loading. */
        }
      }
      video.playbackRate=clamp((Number(clip.speed)||1)*Math.max(.25,Math.abs(playbackRate)),.25,16);
      setMediaPlaying(video,playing && playbackRate>0 && !clip.reverse);
    }
    const previous = clip ? transitionTail(clip,time) : null;
    const previousMedia = previous && mediaOf(previous);
    if (
      (previous?.id || null) !== transitionVideoId ||
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
      const target = sourceTime(previous,previous.start+previous.duration+time-clip.start);
      if (forceSeek || Math.abs(transitionVideo.currentTime - target) > (previous.reverse ? .5/project.fps : .3)) {
        try { transitionVideo.currentTime = target; } catch (_) { /* Metadata pending. */ }
      }
      transitionVideo.playbackRate=clamp((Number(previous.speed)||1)*Math.max(.25,Math.abs(playbackRate)),.25,16);
      setMediaPlaying(transitionVideo,playing && playbackRate>0 && !previous.reverse);
      transitionVideo.muted=false;
      setAudio(transitionVideo,previous);
    } else transitionVideo.pause();
    if (playing && playbackRate > 0 && clip && !previous) {
      const upcoming = project.clips
        .filter(c=>c.type==='video' && c.start>=time && c.start<=time+1.5 &&
          (c.track>clip.track || c.start>=clip.start+clip.duration-1e-6))
        .sort((a,b)=>a.start-b.start || b.track-a.track)
        .find(c=>at('video',c.start)?.id===c.id);
      if (upcoming && upcoming.id !== preloadClipId) {
        const next = mediaOf(upcoming);
        if (next?.src) {
          transitionVideo.pause();
          transitionVideo.muted=true;
          transitionVideo.src = next.src;
          transitionVideo.load();
          const decoder = transitionVideo;
          decoder.addEventListener("loadedmetadata", () => {
            if (decoder === transitionVideo && preloadClipId === upcoming.id)
              decoder.currentTime = sourceTime(upcoming,upcoming.start-transitionBefore(upcoming));
          }, { once: true });
          preloadClipId = upcoming.id;
          transitionVideoId = null;
        }
      }
      if(upcoming && preloadClipId===upcoming.id && time>=upcoming.start-transitionBefore(upcoming)) {
        const target=sourceTime(upcoming,time);
        if(forceSeek||Math.abs(transitionVideo.currentTime-target)>.3) try{transitionVideo.currentTime=target}catch(_){}
        setMediaPlaying(transitionVideo,!upcoming.reverse);
        if(transitionBefore(upcoming)>0) {
          transitionVideo.muted=false;
          setAudio(transitionVideo,upcoming);
        }
      }
    }
    const visible = project.clips.filter(c=>c.type==='video' && c.start-transitionBefore(c)<=time && time<c.start+c.duration);
    const layers = new Map(visible.map(layer=>[layer.id,layer]));
    for (const layer of visible) {
      const tail = transitionTail(layer,time);
      if (tail && tail.id!==previous?.id) layers.set(tail.id,tail);
    }
    const preloading = new Set();
    if (playing && playbackRate>0) {
      const waiting = new Map();
      for (const next of project.clips) {
        if (next.type!=='video' || next.id===preloadClipId || layers.has(next.id)) continue;
        const first = next.start-transitionBefore(next);
        if (first<=time || first>time+.75 || !preceding(next)) continue;
        const prior = waiting.get(next.track);
        if (!prior || first<prior.start-transitionBefore(prior)) waiting.set(next.track,next);
      }
      for (const next of waiting.values()) {
        layers.set(next.id,next);
        preloading.add(next.id);
      }
    }
    const currentVideoIds=new Set(layers.keys());
    for (const [id,element] of layerVideos) if (!currentVideoIds.has(id) || id===activeVideoId || id===preloadClipId) {
      element.pause();disconnectAudio(element);element.removeAttribute('src');element.load();element.remove();layerVideos.delete(id);
    }
    for (const layer of layers.values()) {
      if (layer.id===activeVideoId || layer.id===preloadClipId) continue;
      const item=mediaOf(layer);
      if (!item?.src) continue;
      let element=layerVideos.get(layer.id);
      if(!element) {
        element=document.createElement('video');element.muted=true;element.playsInline=true;
        element.preload='auto';element.style.display='none';element.src=item.src;document.body.append(element);layerVideos.set(layer.id,element);
      }
      if (element.getAttribute('src') !== item.src) element.src = item.src;
      const preload = preloading.has(layer.id);
      const target=sourceTime(layer,preload?layer.start-transitionBefore(layer):time);
      if (forceSeek || Math.abs(element.currentTime-target) > (layer.reverse ? .5/project.fps : .3))
        element.currentTime = target;
      element.playbackRate=clamp((Number(layer.speed)||1)*Math.max(.25,Math.abs(playbackRate)),.25,16);
      setMediaPlaying(element,!preload && playing && playbackRate>0 && !layer.reverse);
      element.muted=preload;
      if (!preload) setAudio(element,layer);
    }
    const currentIds = new Set(audioClips.map((c) => c.id));
    for (const [id, element] of audioElements)
      if (!currentIds.has(id)) {
        element.pause();
        disconnectAudio(element);
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
      const target = sourceTime(audioClip,time);
      if (forceSeek || Math.abs(element.currentTime - target) > 0.3)
        try {
          element.currentTime = target;
        } catch (_) {
          /* Metadata may still be loading. */
        }
      element.playbackRate=clamp((Number(audioClip.speed)||1)*Math.max(.25,Math.abs(playbackRate)),.25,16);
      setMediaPlaying(element,playing && playbackRate>0 && !audioClip.reverse);
      setAudio(element,audioClip);
    }
    audio = audioClips.length ? audioElements.get(audioClips[0].id) : null;
    if (clip) setAudio(video,clip);
  }
  function curveValue(points,local,base) {
    if (!points.length) return base;
    if (local<=points[0].time) return points[0].value;
    for (let i=1;i<points.length;i++) if(local<=points[i].time) {
      const a=points[i-1],b=points[i],u=(local-a.time)/(b.time-a.time);
      return a.value+(b.value-a.value)*u;
    }
    return points.at(-1).value;
  }
  function animated(clip,key,base) {
    const points=(clip.keyframes?.[key]||[]).filter(p=>Number.isFinite(p.time)&&Number.isFinite(p.value)).sort((a,b)=>a.time-b.time);
    return curveValue(points,time-clip.start,base);
  }
  function clipKeyframeWindow(keys,from,to) {
    const cropped={};
    for (const [key,points] of Object.entries(keys||{})) {
      if (!points.length) continue;
      const ordered=[...points].sort((a,b)=>a.time-b.time);
      cropped[key]=[
        {time:0,value:curveValue(ordered,from,0)},
        ...ordered.filter(point=>point.time>from+1e-6 && point.time<to-1e-6)
          .map(point=>({time:point.time-from,value:point.value})),
        {time:to-from,value:curveValue(ordered,to,0)},
      ];
    }
    return cropped;
  }
  function resizeKeyframes(clip, original, shift) {
    if (!original?.keyframes) return;
    clip.keyframes = Math.abs(shift)<1e-6 && Math.abs(original.duration-clip.duration)<1e-6 ?
      structuredClone(original.keyframes) : clipKeyframeWindow(original.keyframes,shift,shift+clip.duration);
  }
  function splitClipAt(clip,cut) {
    const offset=cut-clip.start, duration=clip.duration;
    if (offset<=1e-6 || offset>=duration-1e-6) return null;
    if (['dissolve','wipe','fade'].includes(clip.transition) && offset<transitionLength(clip)-transitionBefore(clip)-1e-6)
      throw new Error('Move the cut outside the transition before splitting this clip.');
    if (clip.transition==='native') throw new Error('Replace the imported transition before splitting this clip.');
    const following = followingTransition(clip);
    if (following && cut>following.start-transitionBefore(following)+1e-6)
      throw new Error('Move the cut outside the start of the following transition.');
    const speed=Number(clip.speed)||1;
    const right={...structuredClone(clip),id:uid(),start:cut,duration:duration-offset,
      in:clip.reverse?clip.in:clip.in+offset*speed,transition:'none'};
    for (const key of ['premiereTrack','premiereIndex','transitionBefore','transitionDuration','transitionMatchName']) delete right[key];
    const keys=structuredClone(clip.keyframes||{});
    const fadeIn=clamp(Number(clip.fadeIn)||0,0,duration), fadeOut=clamp(Number(clip.fadeOut)||0,0,duration);
    if (offset<fadeIn || offset>duration-fadeOut) {
      if (keys.volume?.length) throw new Error('Move the cut outside the combined fade and gain automation.');
      const gain=clip.volume??100;
      const times=[0, duration, Math.min(fadeIn,duration), Math.max(0,duration-fadeOut)];
      if (fadeIn && fadeOut && fadeIn+fadeOut>duration) times.push(duration*fadeIn/(fadeIn+fadeOut));
      keys.volume=[...new Set(times)].sort((a,b)=>a-b).map(t=>({time:t,
        value:gain*Math.min(1,fadeIn?t/fadeIn:1,fadeOut?(duration-t)/fadeOut:1)}));
      clip.fadeIn=0;
      right.fadeOut=0;
    }
    clip.fadeOut=0;
    right.fadeIn=0;
    if (Object.values(keys).some(points=>points.length)) {
      clip.keyframes=clipKeyframeWindow(keys,0,offset);
      right.keyframes=clipKeyframeWindow(keys,offset,duration);
    }
    clip.duration=offset;
    if(clip.reverse) clip.in+=(duration-offset)*speed;
    return right;
  }
  const layerCanvases = new Map();
  const blackGenerator=document.createElement('canvas');blackGenerator.width=16;blackGenerator.height=9;
  blackGenerator.getContext('2d').fillRect(0,0,16,9);
  function paintVideo(ctx,element,clip,canvas,alpha=1) {
    if (!element || (element instanceof HTMLVideoElement && element.readyState < 2)) return;
    const lut=clip.color?.lut;
    const previewScale=lut && playing ? .5 : 1;
    const drawWidth=Math.max(1,Math.round(canvas.width*previewScale));
    const drawHeight=Math.max(1,Math.round(canvas.height*previewScale));
    const sourceWidth=element.videoWidth||element.width||canvas.width;
    const sourceHeight=element.videoHeight||element.height||canvas.height;
    const fit=Math.min(drawWidth/sourceWidth,drawHeight/sourceHeight);
    const scale=clamp(animated(clip,"scale",Number(clip.transform?.scale??100))/100,.1,4);
    const x=clamp(animated(clip,"x",Number(clip.transform?.x??0)),-100,100)*drawWidth/100;
    const y=clamp(animated(clip,"y",Number(clip.transform?.y??0)),-100,100)*drawHeight/100;
    const rotation=clamp(animated(clip,"rotation",Number(clip.transform?.rotation??0)),-180,180)*Math.PI/180;
    let target=ctx;
    const gains=gradeGains(clip.color||{});
    const graded=gains.some(g=>Math.abs(g-1)>.0001);
    if ((lut && window.SpliceLUT) || graded) {
      let layer=layerCanvases.get(clip.id);
      if (!layer) { layer=document.createElement('canvas'); layerCanvases.set(clip.id,layer); }
      if (layer.width!==drawWidth || layer.height!==drawHeight) {layer.width=drawWidth;layer.height=drawHeight;}
      target=layer.getContext('2d',{willReadFrequently:true});
      target.clearRect(0,0,layer.width,layer.height);
    }
    alpha *= clamp(animated(clip,"opacity",Number(clip.opacity??100))/100,0,1);
    target.save(); target.globalAlpha=target===ctx?alpha:1; target.filter=filterFor(clip);
    target.translate(drawWidth/2+x,drawHeight/2+y); target.rotate(rotation);
    target.drawImage(element,-sourceWidth*fit*scale/2,-sourceHeight*fit*scale/2,sourceWidth*fit*scale,sourceHeight*fit*scale);
    target.restore();
    if (target!==ctx) {
      const image=target.getImageData(0,0,drawWidth,drawHeight);
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
      ctx.save();ctx.globalAlpha=alpha;ctx.drawImage(target.canvas,0,0,canvas.width,canvas.height);ctx.restore();
    }
  }
  const previewProjects = new WeakMap();
  function drawFrame(canvas) {
    const active = at('video', time);
    if (!mutedTracks.video && active && mediaOf(active)?.src &&
        video?.readyState < 2 && previewProjects.get(canvas) === project) return;
    previewProjects.set(canvas, project);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#080a0a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!mutedTracks.video) {
      const visible=project.clips.filter(c=>c.type==='video' && c.start-transitionBefore(c)<=time && time<c.start+c.duration)
        .sort((a,b)=>a.track-b.track || a.start-b.start);
      for (const clip of visible) {
        const generator=mediaOf(clip)?.nativeGenerator;
        if(generator===1414680150) continue;
        const element=generator===1112293707 ? blackGenerator : clip.id===activeVideoId ? video : clip.id===preloadClipId ? transitionVideo : layerVideos.get(clip.id);
        const previous=transitionTail(clip,time);
        if(previous) {
          const priorSource=mediaOf(previous)?.nativeGenerator===1112293707 ? blackGenerator :
            clip.id===activeVideoId ? transitionVideo : layerVideos.get(previous.id);
          paintVideo(ctx,priorSource,previous,canvas);
        }
        const progress=clamp((time-(clip.start-transitionBefore(clip)))/transitionLength(clip),0,1);
        const alpha=(clip.transition==='dissolve' || clip.transition==='fade') ? progress : 1;
        if (clip.transition==='fade' && progress<1) { ctx.fillStyle='#000';ctx.fillRect(0,0,canvas.width,canvas.height); }
        if (clip.transition==='wipe') {
          ctx.save();ctx.beginPath();ctx.rect(0,0,canvas.width*progress,canvas.height);ctx.clip();
        }
        paintVideo(ctx,element,clip,canvas,alpha);
        if (clip.transition==='wipe') ctx.restore();
      }
      ctx.globalAlpha=1;ctx.filter='none';
    }
    const titles = mutedTracks.title ? [] : project.clips.filter(c=>c.type==='title'&&c.title&&c.start<=time&&time<c.start+c.duration)
      .sort((a,b)=>a.track-b.track||a.start-b.start);
    for (const title of titles) {
      ctx.fillStyle = "#fff";
      const style=title.titleStyle||{};
      ctx.font = `${Math.round(canvas.height * (Number(style.fontSize)||8)/100)}px "${style.fontFamily||'DM Sans'}"`;
      ctx.fillStyle=style.color||'#ffffff';
      ctx.textAlign = ['left','center','right'].includes(style.align) ? style.align : 'center';
      ctx.textBaseline = "alphabetic";
      ctx.shadowColor = "#000";
      ctx.shadowBlur = 0;
      ctx.shadowOffsetX=3;ctx.shadowOffsetY=3;
      const tx=canvas.width * (Number(style.x??50)/100), ty=canvas.height * (Number(style.y??78)/100);
      const lines=title.title.split(/\r?\n/).slice(0,20);
      const lineHeight=Math.round(canvas.height * (Number(style.fontSize)||8)/100)*1.2;
      for (let i=0;i<lines.length;i++) {
        const metrics=ctx.measureText(lines[i]);
        const lineY=ty+(i-(lines.length-1)/2)*lineHeight+
          (metrics.actualBoundingBoxAscent-metrics.actualBoundingBoxDescent)/2;
        if (Number(style.strokeWidth)>0) {
          ctx.lineWidth=Number(style.strokeWidth)*canvas.height/90;
          ctx.strokeStyle=style.strokeColor||'#000000';
          ctx.strokeText(lines[i],tx,lineY);
        }
        ctx.fillText(lines[i],tx,lineY);
      }
      ctx.shadowBlur = 0;
      ctx.shadowOffsetX=0;ctx.shadowOffsetY=0;
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
      syncMedia(playbackRate < 0 || project.clips.some(c=>c.reverse && c.start<=time && time<c.start+c.duration));
      const frame = Math.floor(time * project.fps);
      if (frame !== lastPaintedFrame) { lastPaintedFrame = frame; notify(); }
    }
    lastTick = timestamp;
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  async function fileToMedia(file) {
    return {id:uid(), ...await SpliceMedia.importFile(file)};
  }
  function checkReplacement(item,replacement) {
    if(replacement.type!==item.type) throw Error('Choose a '+item.type+' file for '+item.name+'.');
    const clips=project.clips.filter(c=>c.mediaId===item.id);
    const needsAudio = item.type==='audio' || clips.some(c=>c.embeddedAudio===true ||
      (item.audioChannels>0 && c.embeddedAudio!==false));
    if (needsAudio && !(replacement.audioChannels>0))
      throw Error(replacement.name+' has no audio stream. Choose a replacement that includes the original sound.');
    const end=Math.max(0,...clips.map(c=>{
      const next=followingTransition(c);
      const handle=c.reverse?transitionBefore(c):next?transitionLength(next)-transitionBefore(next):0;
      return c.in+(c.duration+handle)*(Number(c.speed)||1);
    }));
    if(replacement.duration+1e-6<end) throw Error(replacement.name+' is shorter than the source used on this timeline.');
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
    if (value.trackState !== undefined) {
      if (!value.trackState || typeof value.trackState !== "object" || Array.isArray(value.trackState))
        throw new Error("This project file has invalid track controls.");
      for (const [key, state] of Object.entries(value.trackState))
        if (!/^(?:video:[1-9]\d*|audio:-[1-9]\d*|title:[1-9]\d*)$/.test(key) ||
            !state || typeof state !== "object" || Array.isArray(state) ||
            (state.locked !== undefined && typeof state.locked !== "boolean") ||
            (state.syncLocked !== undefined && typeof state.syncLocked !== "boolean"))
          throw new Error("This project file has invalid track controls.");
    }
    if (value.trackCounts !== undefined && (!value.trackCounts ||
        !Number.isInteger(value.trackCounts.video) || !Number.isInteger(value.trackCounts.audio) ||
        value.trackCounts.video < 1 || value.trackCounts.video > 64 ||
        value.trackCounts.audio < 1 || value.trackCounts.audio > 64))
      throw new Error("This project file has invalid track counts.");
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
        || (clip.linkGroup !== undefined && (typeof clip.linkGroup !== "string" || clip.linkGroup.length > 128))
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
    refreshMedia() { syncMedia(true);notify(); },
    editProject(fn) {
      const next=structuredClone(project);
      fn(next); validateProject(next); assertLockedTracksUnchanged(project,next);
      assertLinksStayAligned(project,next); assertNoNewOverlaps(project,next);
      if (JSON.stringify(project)===JSON.stringify(next)) return;
      snapshot(); project=next;
      project.duration=duration(); time=clamp(time,0,project.duration); syncMedia(true); notify();
    },
    isTrackLocked(track, kind) { return trackLocked(kind || (track < 0 ? "audio" : "video"), track); },
    isTrackSyncLocked(track, kind) { return syncLocked(kind || (track < 0 ? "audio" : "video"), track); },
    setTrackState(track, kind, patch) {
      const key=trackKey(kind, track);
      const current={locked:false,syncLocked:true,...project.trackState?.[key]};
      const next={...current,...patch};
      if (next.locked===current.locked && next.syncLocked===current.syncLocked) return;
      if (typeof next.locked!=="boolean" || typeof next.syncLocked!=="boolean") throw new Error("Invalid track state.");
      snapshot(); project.trackState ||= {}; project.trackState[key]=next;
      if(track===4 && (kind==="title" || kind==="video"))
        project.trackState[trackKey(kind==="title"?"video":"title",4)]={...next};
      notify();
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
      const previousTime = time;
      playing = !playing;
      if (playing && playbackRate < 0) playbackRate=1;
      if (playing && time >= duration()) time = 0;
      if (!playing) time = snap(time);
      if (playing) audioContext?.resume().catch(() => {});
      syncMedia(!playing || time !== previousTime);
      notify();
    },
    select(id) {
      selectedId = id;
      notify();
    },
    async addMedia(files) {
      const imports=Array.from(files);
      if (!imports.length) return;
      const sequence=project;
      const rows=[];
      for (const file of imports) {
        const candidates=project.media.filter(item=>SpliceMedia.matches(item,file));
        if (candidates.length>1) throw Error('Multiple offline sources match '+file.name+'. Relink them individually.');
        const decoded=await fileToMedia(file), offline=candidates[0];
        if(offline) checkReplacement(offline,decoded);
        const item=offline ? {...offline,...decoded,id:offline.id,path:offline.path} : decoded;
        rows.push(item);
      }
      if(project!==sequence) throw Error('The active sequence changed during import. Import again in the intended sequence.');
      await SpliceMedia.rememberMany(rows);
      snapshot();
      for(const row of rows){
        const existing=project.media.find(m=>m.id===row.id);
        if(existing) Object.assign(existing,row); else project.media.push(row);
      }
      activeVideoId=null;syncMedia(true);notify();
    },
    async relinkMediaItem(itemId,file) {
      const original=project.media.find(m=>m.id===itemId);
      if(!original) throw Error('Media item is unavailable.');
      const decoded=await fileToMedia(file);
      checkReplacement(original,decoded);
      const item={...original,...decoded,id:itemId,path:original.path||file.name};
      await SpliceMedia.remember(item);
      if(!project.media.includes(original)) throw Error('The active media changed during relink.');
      snapshot();Object.assign(original,item);activeVideoId=null;syncMedia(true);notify();
    },
    async relinkFolder(files) {
      const sequence=project;
      const matches=SpliceMedia.matchFolder(sequence.media,Array.from(files));
      if(!matches.length)throw Error('The folder contains no matching offline media.');
      const rows=[];
      for(const {item,file} of matches){
        const decoded=await fileToMedia(file);checkReplacement(item,decoded);
        rows.push({item,updated:{...item,...decoded,id:item.id,path:item.path||file.name}});
      }
      if(sequence!==project)throw Error('The sequence changed during relinking. Choose the folder again.');
      await SpliceMedia.rememberMany(rows.map(row=>row.updated));
      snapshot();rows.forEach(row=>Object.assign(row.item,row.updated));activeVideoId=null;syncMedia(true);notify();
    },
    async relinkMedia(files) {
      const matched=Array.from(files).filter(f=>project.media.some(m=>SpliceMedia.matches(m,f)));
      if(!matched.length) throw Error('No selected files match the offline sources.');
      await Editor.addMedia(matched);
    },
    addToTimeline(id) {
      const item = project.media.find((m) => m.id === id);
      if (!item) return;
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
      ensureEditable(clip);
      snapshot();
      project.clips.push(clip);
      selectedId = clip.id;
      project.duration = duration();
      notify();
    },
    updateClip(id, patch) {
      const clip = project.clips.find((c) => c.id === id);
      if (!clip) return;
      ensureEditable(clip, "track" in patch ? Number(patch.track) : clip.track);
      if (Object.entries(patch).every(([key,value]) => JSON.stringify(clip[key])===JSON.stringify(value))) return;
      const linkedFields=["start","duration","in","speed","reverse"];
      const partners=linkedFields.some(key=>key in patch) ? linkedClips(clip) : [];
      const changes=[{id,patch}];
      for (const partner of partners) {
        ensureEditable(partner);
        const linkedPatch={};
        for (const key of ["start","duration","in"])
          if (key in patch) linkedPatch[key]=partner[key]+Number(patch[key])-Number(clip[key]);
        for (const key of ["speed","reverse"])
          if (key in patch) linkedPatch[key]=patch[key];
        changes.push({id:partner.id,patch:linkedPatch});
      }
      for (const change of changes) {
        const target=project.clips.find(c=>c.id===change.id);
        const candidate={...target,...change.patch};
        const source=mediaOf(target);
        if (!Number.isFinite(Number(candidate.start)) || Number(candidate.start)<0 ||
            !Number.isFinite(Number(candidate.in)) || Number(candidate.in)<0 ||
            !Number.isFinite(Number(candidate.duration)) || Number(candidate.duration)<=0 ||
            !Number.isFinite(Number(candidate.speed ?? 1)) || Number(candidate.speed ?? 1)<.1 || Number(candidate.speed ?? 1)>16 ||
            (source?.duration && Number(candidate.in)+Number(candidate.duration)*Number(candidate.speed ?? 1)>source.duration+.01))
          throw new Error("Clip extends beyond available source footage.");
      }
      Editor.editProject(next=>{
        for(const change of changes){
          const target=next.clips.find(c=>c.id===change.id), original={...target};
          Object.assign(target,change.patch);
          for(const key of ["start","duration"])if(key in change.patch)target[key]=snap(target[key]);
          if ('duration' in change.patch && !('keyframes' in change.patch)) resizeKeyframes(target,original,0);
          if (['transition','transitionDuration','transitionBefore'].some(key=>key in change.patch)) validateNewTransition(target,next);
        }
      });
    },
    trimEdge(id, edge, atTime, { ripple = false } = {}) {
      const clip = project.clips.find(c => c.id === id);
      if (!clip) return;
      ensureLinkedEditable(clip);
      const oldStart = clip.start, oldEnd = oldStart + clip.duration;
      const boundary = snap(atTime);
      const speed=Number(clip.speed)||1;
      const mediaItem = mediaOf(clip);
      const sourceEnd = mediaItem?.duration || Infinity;
      const earliest=clip.reverse ? oldStart-(sourceEnd-clip.in-clip.duration*speed)/speed : oldStart-clip.in/speed;
      const latest=clip.reverse ? oldEnd+clip.in/speed : oldStart+(sourceEnd-clip.in)/speed;
      const newStart = edge === "start" ? clamp(boundary, Math.max(0, earliest), oldEnd - 1/project.fps) : oldStart;
      const newEnd = edge === "end" ? clamp(boundary, oldStart + 1/project.fps, latest) : oldEnd;
      const shift = (edge === "start" ? newStart - oldStart : newEnd - oldEnd);
      if (!shift) return;
      const memberIds=new Set([clip.id,...linkedClips(clip).map(c=>c.id)]);
      const rippleShift=edge==="start"?-shift:shift;
      for(const member of project.clips.filter(c=>memberIds.has(c.id))){
        const nextIn=member.in+(edge==="start"&&!member.reverse?shift*(Number(member.speed)||1):edge==="end"&&member.reverse?-shift*(Number(member.speed)||1):0);
        const nextDuration=member.duration+(edge==="start"?-shift:shift);
        const available=mediaOf(member)?.duration;
        if(nextDuration<1/project.fps-.001 || nextIn<-.001 || (available && nextIn+nextDuration*(Number(member.speed)||1)>available+.01))
          throw new Error("Linked clip cannot be trimmed beyond its source.");
      }
      Editor.editProject(next=>{
        for(const member of next.clips.filter(c=>memberIds.has(c.id))){
          const original=(gestureBefore||project).clips.find(c=>c.id===member.id);
          if(edge==="start"){if(!ripple)member.start=snap(member.start+shift);if(!member.reverse)member.in=Math.max(0,member.in+shift*(Number(member.speed)||1));member.duration=snap(member.duration-shift);}
          else{member.duration=snap(member.duration+shift);if(member.reverse)member.in=Math.max(0,member.in-shift*(Number(member.speed)||1));}
          const headShift = ripple ? original.duration-member.duration : member.start-original.start;
          resizeKeyframes(member,original,edge==='start'?headShift:0);
        }
        if(ripple)for(const following of next.clips)
          if(!memberIds.has(following.id)&&following.start>=oldEnd-.001&&!trackLocked(following.type,following.track)&&syncLocked(following.type,following.track))
            following.start=Math.max(0,snap(following.start+rippleShift));
      });
    },
    rollEdit(leftId, rightId, boundary) {
      const left = project.clips.find(c => c.id === leftId), right = project.clips.find(c => c.id === rightId);
      if (!left || !right || left.type !== right.type || left.track !== right.track || Math.abs(left.start + left.duration - right.start) > .05) return;
      const outgoing = [left, ...linkedClips(left)], incoming = [right, ...linkedClips(right)];
      const leftIds = new Set(outgoing.map(clip => clip.id)), rightIds = new Set(incoming.map(clip => clip.id));
      if (incoming.some(clip => leftIds.has(clip.id))) return;
      for (const clip of [...outgoing,...incoming]) ensureEditable(clip);
      const frame = 1/project.fps;
      const head = clip => clip.in / (Number(clip.speed) || 1);
      const tail = clip => ((mediaOf(clip)?.duration || Infinity) - clip.in) / (Number(clip.speed) || 1) - clip.duration;
      const earliest = Math.max(...outgoing.map(clip => frame-clip.duration),
        ...incoming.map(clip => Math.max(-clip.start, -(clip.reverse ? tail(clip) : head(clip)))));
      const latest = Math.min(...outgoing.map(clip => clip.reverse ? head(clip) : tail(clip)),
        ...incoming.map(clip => clip.duration-frame));
      const lower = Math.ceil((earliest-1e-7)*project.fps)*frame;
      const upper = Math.floor((latest+1e-7)*project.fps)*frame;
      if (lower > upper) return;
      const shift = clamp(snap(boundary-right.start),lower,upper);
      if (!Number.isFinite(shift) || Math.abs(shift)<1e-7) return;
      Editor.editProject(next => {
        for (const clip of next.clips) {
          const speed = Number(clip.speed) || 1;
          if (leftIds.has(clip.id)) {
            const original=(gestureBefore||project).clips.find(c=>c.id===clip.id);
            clip.duration += shift;
            if (clip.reverse) clip.in = Math.max(0,clip.in-shift*speed);
            resizeKeyframes(clip,original,0);
          } else if (rightIds.has(clip.id)) {
            const original=(gestureBefore||project).clips.find(c=>c.id===clip.id);
            clip.start += shift;
            clip.duration -= shift;
            if (!clip.reverse) clip.in = Math.max(0,clip.in+shift*speed);
            resizeKeyframes(clip,original,clip.start-original.start);
          }
        }
      });
    },
    slipClip(id, delta) {
      const clip = project.clips.find(c => c.id === id), source = clip && mediaOf(clip)?.duration;
      if (!clip || !source) return;
      ensureLinkedEditable(clip);
      const members=[clip,...linkedClips(clip)];
      const lower=Math.max(...members.map(member=>-member.in));
      const upper=Math.min(...members.map(member=>(mediaOf(member)?.duration||0)-member.in-member.duration*(Number(member.speed)||1)));
      const shift=clamp(snap(delta),lower,upper);
      if (!shift) return;
      Editor.editProject(next=>{for(const member of next.clips.filter(c=>members.some(m=>m.id===c.id)))member.in=Math.max(0,member.in+shift);});
    },
    splitClipAt,
    split() {
      const clip = selected() || at("video", time);
      const cut = snap(time);
      if (!clip || cut <= clip.start || cut >= clip.start + clip.duration)
        return;
      ensureLinkedEditable(clip);
      const members=[clip,...linkedClips(clip)];
      const rightGroup=clip.linkGroup?uid():null;
      Editor.editProject(next=>{
        for(const member of next.clips.filter(c=>members.some(m=>m.id===c.id))){
          const right=splitClipAt(member,cut);
          if (!right) throw new Error("Linked clip does not cross the playhead.");
          if(rightGroup)right.linkGroup=rightGroup;
          next.clips.push(right);
        }
      });
      time = cut;
    },
    deleteClips(ids, {ripple=false} = {}) {
      const selected=new Set(ids);
      const members=project.clips.filter(clip=>selected.has(clip.id));
      for(const clip of [...members])for(const linked of linkedClips(clip))selected.add(linked.id);
      const removing=project.clips.filter(clip=>selected.has(clip.id));
      if(!removing.length)return;
      for(const clip of removing)ensureEditable(clip);
      const begin=Math.min(...removing.map(clip=>clip.start));
      const end=Math.max(...removing.map(clip=>clip.start+clip.duration));
      if(ripple && removing.some(clip=>Math.abs(clip.start-begin)>.001 || Math.abs(clip.start+clip.duration-end)>.001))
        throw new Error("Ripple delete one aligned edit group at a time.");
      Editor.editProject(next=>{
        next.clips=next.clips.filter(clip=>!selected.has(clip.id));
        if(ripple)for(const clip of next.clips)
          if(clip.start>=end-.001&&!trackLocked(clip.type,clip.track)&&syncLocked(clip.type,clip.track))
            clip.start=snap(clip.start-(end-begin));
      });
      selectedId = project.clips[0]?.id || null;
      notify();
    },
    deleteSelected(options) {
      if (selectedId) Editor.deleteClips([selectedId], options);
    },
    moveClip(id, start, track) {
      Editor.updateClip(id, { start, track });
    },
    addTitle() {
      if (trackLocked("title", 4)) throw new Error("Unlock the title track before editing.");
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
      time=clamp(time,0,duration());
      if (!project.clips.some(c=>c.id===selectedId)) selectedId=project.clips[0]?.id||null;
      syncMedia(true);
      notify();
    },
    redo() {
      if (!future.length) return;
      history.push(structuredClone(project));
      project = future.pop();
      revision += 1;
      time=clamp(time,0,duration());
      if (!project.clips.some(c=>c.id===selectedId)) selectedId=project.clips[0]?.id||null;
      syncMedia(true);
      notify();
    },
    newProject(settings = {}) {
      const name=String(settings.name||"Untitled project").trim();
      const width=Number(settings.width||1920), height=Number(settings.height||1080), fps=Number(settings.fps||25);
      if (!name || !Number.isInteger(width) || !Number.isInteger(height) || width<16 || height<16 || width>3840 || height>2160 ||
          !Number.isFinite(fps) || fps<1 || fps>60) throw new Error("Invalid sequence settings.");
      history=[];future=[];gesture=false;gestureBefore=null;revision++;
      project = {
        name,
        width,
        height,
        fps,
        trackCounts: {video: 4, audio: 3},
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
      history=[];future=[];gesture=false;gestureBefore=null;revision++;
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
      delete prepared.premiereOriginal; delete prepared.premiereManaged;
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
            const extension = ["mp4","mov","wav"].includes(job.extension) ? job.extension : "mp4";
            const downloadURL = endpoint + "/file?name=" + encodeURIComponent(safe + "." + extension);
            const anchor = document.createElement("a");
            anchor.href = downloadURL;
            anchor.download = safe + "." + extension;
            anchor.click();
            return { id, url: job.previewable === false ? null : endpoint + "/preview", downloadURL,
              extension, mime: job.mime, previewable: job.previewable !== false };
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
