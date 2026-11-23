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
  const auditionSpan = 4;
  const auditionBuffers = new Map();
  const auditions = new Map();
  function auditionBuffer(src, index, rate, reversed) {
    const key=JSON.stringify([src,index,rate,reversed]);
    let entry=auditionBuffers.get(key);
    if (entry?.retryAfter && performance.now()>=entry.retryAfter) {
      auditionBuffers.delete(key);
      entry=null;
    }
    if (entry) {
      auditionBuffers.delete(key);
      auditionBuffers.set(key,entry);
      return entry.buffer;
    }
    const controller=new AbortController();
    entry={controller};
    auditionBuffers.set(key,entry);
    if (auditionBuffers.size>24) {
      const oldest=auditionBuffers.keys().next().value;
      auditionBuffers.get(oldest).controller.abort();
      auditionBuffers.delete(oldest);
    }
    fetch('/api/media/audition?'+new URLSearchParams({src,index,rate,reverse:reversed?1:0}),{
      signal:AbortSignal.any([controller.signal,AbortSignal.timeout(10000)]),
    }).then(async response=>{
      if (!response.ok) throw Error(await response.text());
      entry.buffer=await audioContext.decodeAudioData(await response.arrayBuffer());
    }).catch(error=>{
      if (!controller.signal.aborted) {
        entry.retryAfter=performance.now()+5000;
        console.warn('Audio preview unavailable',error);
      }
    });
  }
  function stopAudition(state) {
    for(const voice of state.voices) { voice.stop();voice.disconnect(); }
    state.voices.clear();
  }
  function auditionAudio(element,clip,target,rate,forceSeek) {
    const item=mediaOf(clip);
    const src=element.getAttribute('src');
    if (!src || !item || item.audioChannels===0 || item.still || clip.embeddedAudio===false || target<0) return;
    const sourceSpan = auditionSpan*rate;
    const index=Math.floor(target/sourceSpan), step=clip.reverse?-1:1;
    const initial=auditionBuffer(src,index,rate,clip.reverse);
    const next=index+step;
    if (next>=0 && next*sourceSpan<item.duration && (clip.reverse?target-index*sourceSpan:(index+1)*sourceSpan-target)<rate*.75)
      auditionBuffer(src,next,rate,clip.reverse);
    if (!playing || playbackRate<0 || audioContext.state!=='running') return;
    let state=auditions.get(element);
    const now=audioContext.currentTime;
    if (forceSeek || state?.src!==src || state?.rate!==rate || state?.step!==step || state?.id!==clip.id ||
        (state.clock!==null && Math.abs(state.target+step*rate*(now-state.clock)-target)>rate*.15)) {
      if (state) stopAudition(state);
      state={src,id:clip.id,rate,step,clock:null,voices:new Set()};
      auditions.set(element,state);
    }
    state.active=true;
    if (!initial) return;
    let buffer, offset, start, chunk;
    if (state.clock===null || state.end<=now) {
      stopAudition(state);
      start=now;
      chunk=index;
      const inWindow=(target-index*sourceSpan)/rate;
      offset=clip.reverse ? auditionSpan-inWindow : inWindow;
      buffer=initial;
      state.clock=now;
      state.target=target;
    } else if (state.end-now<.12 && state.next>=0 && state.next*sourceSpan<item.duration) {
      chunk=state.next;
      buffer=auditionBuffer(src,chunk,rate,clip.reverse);
      start=state.end;
      offset=0;
    }
    if (!buffer || offset>=buffer.duration) return;
    const voice=audioContext.createBufferSource();
    voice.buffer=buffer;
    const end=start+auditionSpan-offset;
    const overlap=Math.max(0,buffer.duration-auditionSpan);
    const envelope=audioContext.createGain();
    if (overlap>0) {
      if (start>now) {
        envelope.gain.setValueAtTime(0,start);
        envelope.gain.linearRampToValueAtTime(1,start+overlap);
      }
      envelope.gain.setValueAtTime(1,end);
      envelope.gain.linearRampToValueAtTime(0,end+overlap);
    }
    voice.connect(envelope).connect(routeAudio(element).gain);
    voice.onended=()=>{voice.disconnect();envelope.disconnect();state.voices.delete(voice);};
    voice.start(start,offset);
    state.voices.add(voice);
    state.next=chunk+step;
    state.end=end;
  }
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
  const clipPlaybackRate = clip => clamp((Number(clip.speed)||1)*Math.abs(playbackRate),.0625,16);
  function syncClipPlayback(element, clip, target, forceSeek, audible=true) {
    const rate=clipPlaybackRate(clip);
    const item=mediaOf(clip), stream=clipAudioStream(clip,item);
    if (item?.still) {
      element.pause();
      element.muted=true;
      return;
    }
    if (stream!==(item?.audioStream??null) && element.getAttribute('src')===item?.src) audible=false;
    element.playbackRate=rate;
    setMediaPlaying(element,playing && playbackRate>0 && !clip.reverse && rate>=.25);
    element.muted=!audible || clip.reverse || rate<.25;
    if (!audible) return;
    setAudio(element,clip);
    if (clip.reverse || rate<.25) auditionAudio(element,clip,target,rate,forceSeek);
  }
  const seekTolerance = clip => Math.max(1/project.fps,Math.min(1,Number(clip.speed)||1)*.3);
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
  const titleTrack = () => Math.min(64,Math.max(4,...project.clips.filter(c=>c.type==='video').map(c=>c.track+1)));
  function sourceSelection(item, marks=item.sourceMarks, stream=item.sourceAudioStream) {
    const usedLength=project.clips.find(c=>c.mediaId===item.id)?.duration;
    const available=item.duration||usedLength||5;
    const selection={in:0,duration:available};
    if(Number.isFinite(marks?.in) && Number.isFinite(marks?.out)) {
      const from=clamp(marks.in,0,available);
      const to=clamp(marks.out,from,available);
      const frames=Math.min(Math.round((to-from)*project.fps),Math.floor((available-from)*project.fps+1e-6));
      if(frames<1) throw Error('Mark a range at least one sequence frame long.');
      Object.assign(selection,{in:from,duration:frames/project.fps});
    }
    if(item.audioStreams>1) {
      const valid=stream===null||Number.isInteger(stream)&&stream>=0&&stream<item.audioStreams;
      selection.audioStream=valid?stream:item.audioStream??null;
    }
    return selection;
  }
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
  const sameLaneOverlap = (left, right) => (left.type === 'audio') === (right.type === 'audio') && left.track === right.track &&
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
  const requestedTransitionLength = clip => clamp(Number(clip.transitionDuration || .5), 1/project.fps, 7200);
  const transitionBefore = clip => clamp(Number(clip.transitionBefore)||0,0,requestedTransitionLength(clip));
  const transitionLength = clip => Math.min(requestedTransitionLength(clip),clip.duration+transitionBefore(clip));
  const sourceTime = (clip, atTime) => {
    if (mediaOf(clip)?.still) return 0;
    const speed=clamp(Number(clip.speed)||1,.1,16), local=atTime-clip.start;
    return Number(clip.in||0)+(clip.reverse ? clip.duration-local : local)*speed-(clip.reverse?1e-6:0);
  };
  const trackSettings = track => project.trackMix?.[String(track)] || {};
  const soloActive = () => Object.values(project.trackMix || {}).some(m => m.solo);
  let transitionFollowers=new Map();
  let videoDipFollowers=new Map();
  const followingTransition = clip => transitionFollowers.get(clip.id);
  function indexTransitions() {
    const cuts=new Map();transitionFollowers=new Map();videoDipFollowers=new Map();
    for(const clip of project.clips) if(['dissolve','wipe'].includes(clip.transition) || (clip.type==='video' && clip.transition==='fade'))
      cuts.set(`${clip.type}:${clip.track}:${Math.round(clip.start*project.fps)}`,clip);
    for(const clip of project.clips) {
      const next=cuts.get(`${clip.type}:${clip.track}:${Math.round((clip.start+clip.duration)*project.fps)}`);
      if(next && next.id!==clip.id) (next.transition==='fade'?videoDipFollowers:transitionFollowers).set(clip.id,next);
    }
  }
  const audioTransitionGain = (clip, progress) => {
    const value=clamp(progress,0,1);
    return clip.transitionMatchName==='Constant Power' ? Math.sin(value*Math.PI/2) : value;
  };
  const audibility = clip => {
    const mix = trackSettings(clip.track);
    if (mutedTracks.audio || mix.mute || (soloActive() && !mix.solo)) return 0;
    const local = time - clip.start;
    const fadeIn = clamp(Number(clip.fadeIn)||0,0,clip.duration);
    const fadeOut = clamp(Number(clip.fadeOut)||0,0,clip.duration);
    let fade = Math.min(1, fadeIn ? local/fadeIn : 1, fadeOut ? (clip.duration-local)/fadeOut : 1);
    if(['dissolve','wipe','fade'].includes(clip.transition)) fade*=audioTransitionGain(clip,(time-clip.start+transitionBefore(clip))/transitionLength(clip));
    const next=followingTransition(clip);
    if(next) fade*=audioTransitionGain(next,1-(time-next.start+transitionBefore(next))/transitionLength(next));
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
  const decode709 = value => Math.sign(value)*Math.abs(value)**2.4;
  const encode709 = value => Math.sign(value)*Math.abs(value)**(1/2.4);
  const decodePixels709 = Array.from({length:256},(_,level)=>decode709(level/255));
  const decodePixelsPQ = Array.from({length:256},(_,level)=>{
    const value=(level/255)**(32/2523);
    return (10000/203)*(Math.max(0,value-3424/4096)/(2413/128-(2392/128)*value))**(16384/2610);
  });
  function hasHdrGrade(color={}) {
    return ['brightness','contrast','saturation'].some(key=>(color[key]??100)!==100) ||
      ['exposure','temperature','tint'].some(key=>Number(color[key])||0);
  }
  const clipAudioStream=(clip,item)=>'audioStream' in clip?clip.audioStream:item?.audioStream??null;
  function programSource(clip) {
    const item=clip && mediaOf(clip);
    if (!item?.src) return;
    const stream=clipAudioStream(clip,item);
    if (item.hdr && hasHdrGrade(clip.color||{})) {
      const graded=window.SpliceMedia?.preparePreview(item,stream,true)?.src;
      if (graded) return graded;
    }
    if (stream!==(item.audioStream??null))
      return window.SpliceMedia?.preparePreview(item,stream)?.src||item.src;
    return item.src;
  }
  const hdrShoulder=.78, hdrPeak=1000/203;
  // Match the HDR preview proxy's Mobius shoulder. Undo it before grading.
  const shoulderA=-(hdrShoulder**2)*(hdrPeak-1)/(hdrShoulder**2-2*hdrShoulder+hdrPeak);
  const shoulderB=(hdrShoulder**2-2*hdrShoulder*hdrPeak+hdrPeak)/(hdrPeak-1);
  const shoulderScale=(shoulderB+hdrShoulder)**2/(shoulderB-shoulderA);
  function gradeHdrPreview(pixels, color, gains, gradingData) {
    const brightness=(color.brightness??100)/100, contrast=(color.contrast??100)/100, saturation=(color.saturation??100)/100;
    const displayGrade=brightness!==1 || contrast!==1 || saturation!==1;
    const decode=gradingData?decodePixelsPQ:decodePixels709;
    for(let i=0;i<pixels.length;i+=4) {
      if (!pixels[i+3]) continue;
      let r=decode[pixels[i]],g=decode[pixels[i+1]],b=decode[pixels[i+2]];
      const before=Math.max(r,g,b);
      if (gradingData) {
        // Unpack the proxy's linear BT.2020 primaries into the working gamut.
        const red=1.66049*r-.58764*g-.07285*b, green=-.12455*r+1.13290*g-.00835*b;
        b=-.01815*r-.10058*g+1.11873*b;r=red;g=green;
      } else if (before>hdrShoulder) {
        const inverse=(before*shoulderB-shoulderScale*shoulderA)/(shoulderScale-before);
        const ratio=Math.min(hdrPeak,inverse)/before;
        r*=ratio;g*=ratio;b*=ratio;
      }
      if (displayGrade) {
        r=(encode709(r)*brightness-.5)*contrast+.5;
        g=(encode709(g)*brightness-.5)*contrast+.5;
        b=(encode709(b)*brightness-.5)*contrast+.5;
        const luma=(.213*r+.715*g+.072*b)*(1-saturation);
        r=decode709(r*saturation+luma);
        g=decode709(g*saturation+luma);
        b=decode709(b*saturation+luma);
      }
      r*=gains[0];g*=gains[1];b*=gains[2];
      const after=Math.max(r,g,b);
      const ratio=after<=hdrShoulder ? 1 : shoulderScale*(after+shoulderA)/(after+shoulderB)/after;
      pixels[i]=255*encode709(r*ratio);
      pixels[i+1]=255*encode709(g*ratio);
      pixels[i+2]=255*encode709(b*ratio);
    }
  }
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
            Math.round((c.start+c.duration)*source.fps)===Math.round(clip.start*source.fps),
        )
        .sort((a, b) => b.start - a.start)[0] || null
    );
  }
  function transitionTail(clip, position) {
    if (position>=clip.start && position<clip.start+transitionLength(clip)-transitionBefore(clip))
      return preceding(clip);
  }
  function transitionShortages(source) {
    const media = new Map(source.media.map(item=>[item.id,item]));
    const boundary = (clip, point) => `${clip.type}:${clip.track}:${Math.round(point*source.fps)}`;
    const ends = new Map(source.clips.filter(clip=>clip.type!=='title' && media.get(clip.mediaId)?.nativeGenerator!==1414680150)
      .map(clip=>[boundary(clip,clip.start+clip.duration),clip]));
    const shortages = new Map();
    for (const clip of source.clips) {
      if (!['dissolve','wipe','fade'].includes(clip.transition)) continue;
      const before=transitionBefore(clip);
      const after=requestedTransitionLength(clip)-before;
      const prior=ends.get(boundary(clip,clip.start));
      const priorMedia=prior && media.get(prior.mediaId);
      const currentMedia=media.get(clip.mediaId);
      const speed=Number(clip.speed)||1;
      const inSpare=clip.reverse ? (currentMedia?.duration-clip.in)/speed-clip.duration : clip.in/speed;
      const priorSpeed=Number(prior?.speed)||1;
      const outSpare=prior?.reverse ? prior.in/priorSpeed : (priorMedia?.duration-prior?.in)/priorSpeed-prior?.duration;
      const needsOut=clip.transition!=='fade' && prior && prior!==clip && !priorMedia?.offline && !priorMedia?.still;
      const incoming=before>0 && !currentMedia?.offline && !currentMedia?.still && Number.isFinite(inSpare) ?
        Math.max(0,before-Math.min(inSpare,clip.start)) : 0;
      const outgoing=needsOut && Number.isFinite(outSpare) ? Math.max(0,after-outSpare) : 0;
      if (incoming>1e-6 || outgoing>1e-6) shortages.set(clip.id,{incoming,outgoing,before,after});
    }
    return shortages;
  }
  function assertTransitionHandles(before, after) {
    const shortages = transitionShortages(after);
    if (!shortages.size) return;
    const original = transitionShortages(before);
    for (const [id, limits] of shortages) {
      const prior=original.get(id);
      if (limits.incoming>(prior?.incoming||0)+1e-6)
        throw Error('This transition needs '+limits.before.toFixed(2)+' s of unused incoming footage.');
      if (limits.outgoing>(prior?.outgoing||0)+1e-6)
        throw Error('This transition needs '+limits.after.toFixed(2)+' s of unused outgoing footage.');
    }
  }
  const presentedSeekTimes = new WeakMap();
  function seekVideo(element, clip, target, forceSeek) {
    if (element.currentTime===0 && mediaOf(clip)?.still) return;
    const slow=clipPlaybackRate(clip)<.25;
    const framewise=playing && (playbackRate<0 || clip.reverse || slow);
    if (!forceSeek && framewise) {
      if (element.seeking || element.readyState<2) return;
      if (presentedSeekTimes.get(element)!==element.currentTime) {
        // Give the monitor one paint of the decoded frame before requesting another.
        presentedSeekTimes.set(element,element.currentTime);
        return;
      }
    }
    if (!forceSeek && framewise && slow) {
      const fps=mediaOf(clip)?.fps||project.fps;
      if (Math.floor(element.currentTime*fps+1e-6)===Math.floor(target*fps+1e-6)) return;
    } else if (!forceSeek && Math.abs(element.currentTime-target)<=(framewise?.5/project.fps:seekTolerance(clip))) return;
    try { element.currentTime = Math.max(0,target); }
    catch (error) {
      if (error.name!=='InvalidStateError') throw error;
    }
  }
  function syncMedia(forceSeek = false) {
    ensureElements();
    for(const state of auditions.values()) state.active=false;
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
    const source = programSource(clip);
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
      (video.getAttribute("src") || "") !== (source || "")
    ) {
      activeVideoId = clip?.id;
      video.pause();
      video.removeAttribute("src");
      video.load();
      if (source) {
        video.src = source;
        video.load();
      }
    }
    if (clip && mediaItem) {
      const target = sourceTime(clip,time);
      seekVideo(video,clip,target,forceSeek);
      syncClipPlayback(video,clip,target,forceSeek);
    }
    const previous = clip ? transitionTail(clip,time) : null;
    const priorSource = programSource(previous);
    if (
      (previous?.id || null) !== transitionVideoId ||
      (priorSource && transitionVideo.getAttribute('src') !== priorSource)
    ) {
      transitionVideoId = previous?.id;
      transitionVideo.removeAttribute("src");
      transitionVideo.load();
      if (priorSource) {
        transitionVideo.src = priorSource;
        transitionVideo.load();
      }
    }
    if (previous) {
      const target = sourceTime(previous,previous.start+previous.duration+time-clip.start);
      seekVideo(transitionVideo,previous,target,forceSeek);
      syncClipPlayback(transitionVideo,previous,target,forceSeek);
    } else transitionVideo.pause();
    if (playing && playbackRate > 0 && clip && !previous) {
      const upcoming = project.clips
        .filter(c=>c.type==='video' && c.start>=time && c.start<=time+1.5 &&
          (c.track>clip.track || c.start>=clip.start+clip.duration-1e-6))
        .sort((a,b)=>a.start-b.start || b.track-a.track)
        .find(c=>at('video',c.start)?.id===c.id);
      if (upcoming && upcoming.id !== preloadClipId) {
        const nextSource = programSource(upcoming);
        if (nextSource) {
          transitionVideo.pause();
          transitionVideo.muted=true;
          transitionVideo.src = nextSource;
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
        seekVideo(transitionVideo,upcoming,target,forceSeek);
        syncClipPlayback(transitionVideo,upcoming,target,forceSeek,transitionBefore(upcoming)>0);
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
      const layerSource=programSource(layer);
      if (!layerSource) continue;
      let element=layerVideos.get(layer.id);
      if(!element) {
        element=document.createElement('video');element.muted=true;element.playsInline=true;
        element.preload='auto';element.style.display='none';element.src=layerSource;document.body.append(element);layerVideos.set(layer.id,element);
      }
      if (element.getAttribute('src') !== layerSource) element.src = layerSource;
      const preload = preloading.has(layer.id);
      const target=sourceTime(layer,preload?layer.start-transitionBefore(layer):time);
      seekVideo(element,layer,target,forceSeek);
      if (preload) {
        element.muted=true;
        setMediaPlaying(element,false);
      } else syncClipPlayback(element,layer,target,forceSeek);
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
      const source=programSource(audioClip);
      if (!source) continue;
      let element = audioElements.get(audioClip.id);
      if (!element) {
        element = document.createElement("audio");
        element.src = source;
        element.preload = "auto";
        element.style.display = "none";
        document.body.append(element);
        audioElements.set(audioClip.id, element);
      }
      if (element.getAttribute("src") !== source) element.src = source;
      const target = sourceTime(audioClip,time);
      if (forceSeek || Math.abs(element.currentTime - target) > seekTolerance(audioClip))
        try {
          element.currentTime = target;
        } catch (_) {
          /* Metadata may still be loading. */
        }
      syncClipPlayback(element,audioClip,target,forceSeek);
    }
    audio = audioClips.length ? audioElements.get(audioClips[0].id) : null;
    for(const [element,state] of auditions) if (!state.active) {
      stopAudition(state);
      auditions.delete(element);
    }
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
  const dissolveCanvas=document.createElement('canvas');
  const blackGenerator=document.createElement('canvas');blackGenerator.width=16;blackGenerator.height=9;
  blackGenerator.getContext('2d').fillRect(0,0,16,9);
  function paintVideo(ctx,element,clip,canvas,alpha=1,brightness=1) {
    if (!element || (element instanceof HTMLVideoElement && element.readyState < 2)) return;
    const color=clip.color||{}, lut=color.lutIntensity===0?null:color.lut;
    const gains=gradeGains(color);
    const graded=gains.some(g=>Math.abs(g-1)>.0001);
    const item=mediaOf(clip);
    const gradingSource=item?.hdr && window.SpliceMedia?.hdrPreviewStatus(item,clipAudioStream(clip,item))?.src;
    const gradingData=!!gradingSource && element.getAttribute('src')===gradingSource;
    const hdrGrade=item?.hdr && (gradingData || hasHdrGrade(color));
    const previewScale=(lut || hdrGrade) && playing ? .5 : 1;
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
    if ((lut && window.SpliceLUT) || graded || hdrGrade) {
      let layer=layerCanvases.get(clip.id);
      if (!layer) { layer=document.createElement('canvas'); layerCanvases.set(clip.id,layer); }
      if (layer.width!==drawWidth || layer.height!==drawHeight) {layer.width=drawWidth;layer.height=drawHeight;}
      target=layer.getContext('2d',{willReadFrequently:true});
      target.clearRect(0,0,layer.width,layer.height);
    }
    alpha *= clamp(animated(clip,"opacity",Number(clip.opacity??100))/100,0,1);
    target.save(); target.globalAlpha=target===ctx?alpha:1; target.filter=hdrGrade?'none':filterFor(clip);
    if (target===ctx && brightness<1) target.filter+=` brightness(${brightness})`;
    target.translate(drawWidth/2+x,drawHeight/2+y); target.rotate(rotation);
    target.drawImage(element,-sourceWidth*fit*scale/2,-sourceHeight*fit*scale/2,sourceWidth*fit*scale,sourceHeight*fit*scale);
    target.restore();
    if (target!==ctx) {
      const image=target.getImageData(0,0,drawWidth,drawHeight);
      if (hdrGrade) gradeHdrPreview(image.data,color,gains,gradingData);
      else if (graded) {
        const pixels=image.data;
        for(let i=0;i<pixels.length;i+=4) {
          pixels[i]=Math.min(255,pixels[i]*gains[0]);
          pixels[i+1]=Math.min(255,pixels[i+1]*gains[1]);
          pixels[i+2]=Math.min(255,pixels[i+2]*gains[2]);
        }
      }
      if (lut && window.SpliceLUT) window.SpliceLUT.applyImageData(image,lut,clip.color.lutIntensity??100);
      target.putImageData(image,0,0);
      ctx.save();ctx.globalAlpha=alpha;
      if (brightness<1) ctx.filter=`brightness(${brightness})`;
      ctx.drawImage(target.canvas,0,0,canvas.width,canvas.height);ctx.restore();
    }
  }
  function videoFor(clip) {
    if (mediaOf(clip)?.nativeGenerator===1112293707) return blackGenerator;
    if (clip.id===activeVideoId) return video;
    if (clip.id===preloadClipId || clip.id===transitionVideoId) return transitionVideo;
    return layerVideos.get(clip.id);
  }
  const previewProjects = new WeakMap();
  function paintTitle(ctx,canvas,title) {
    ctx.save();ctx.globalAlpha=clamp(animated(title,'opacity',Number(title.opacity??100))/100,0,1);
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
    ctx.restore();
  }
  function drawFrame(canvas) {
    const active = at('video', time);
    if (!mutedTracks.video && active && mediaOf(active)?.src &&
        video?.readyState < 2 && previewProjects.get(canvas) === project) return;
    previewProjects.set(canvas, project);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const visible=project.clips.filter(clip=>clip.type==='video' ? !mutedTracks.video &&
      clip.start-transitionBefore(clip)<=time && time<clip.start+clip.duration :
      clip.type==='title' && !mutedTracks.title && clip.title && clip.start<=time && time<clip.start+clip.duration);
    visible.sort((a,b)=>(a.track??4)-(b.track??4) || Number(a.type==='title')-Number(b.type==='title') || a.start-b.start);
    const dissolvePairs=new Map(), outgoingIds=new Set();
    for (const clip of visible) {
      if (clip.type!=='video' || clip.transition!=='dissolve' || time>=clip.start+transitionLength(clip)-transitionBefore(clip)) continue;
      const previous=preceding(clip);
      if (previous) {dissolvePairs.set(clip.id,previous);outgoingIds.add(previous.id);}
    }
    for (const clip of visible) {
      if (clip.type==='title') {
        paintTitle(ctx,canvas,clip);
        continue;
      }
      if (outgoingIds.has(clip.id) || mediaOf(clip)?.nativeGenerator===1414680150) continue;
      if (clip.transition==='fade' && time<clip.start) continue;
      const element=videoFor(clip);
      const progress=clamp((time-(clip.start-transitionBefore(clip)))/transitionLength(clip),0,1);
      const previous=dissolvePairs.get(clip.id);
      if (previous) {
        if (dissolveCanvas.width!==canvas.width || dissolveCanvas.height!==canvas.height) {
          dissolveCanvas.width=canvas.width;dissolveCanvas.height=canvas.height;
        }
        const mix=dissolveCanvas.getContext('2d');
        mix.clearRect(0,0,canvas.width,canvas.height);
        paintVideo(mix,videoFor(previous),previous,canvas,1-progress);
        mix.save();mix.globalCompositeOperation='lighter';
        paintVideo(mix,element,clip,canvas,progress);mix.restore();
        ctx.drawImage(dissolveCanvas,0,0);
        continue;
      }
      let brightness=1;
      const head=transitionLength(clip)-transitionBefore(clip);
      if (clip.transition==='fade' && head>0) brightness=clamp((time-clip.start)/head,0,1);
      const nextDip=videoDipFollowers.get(clip.id);
      if (nextDip && transitionBefore(nextDip)>0) brightness*=clamp((nextDip.start-time)/transitionBefore(nextDip),0,1);
      if (clip.transition==='wipe') {
        const tail=transitionTail(clip,time);
        if (tail) paintVideo(ctx,videoFor(tail),tail,canvas);
        ctx.save();ctx.beginPath();ctx.rect(0,0,canvas.width*progress,canvas.height);ctx.clip();
      }
      paintVideo(ctx,element,clip,canvas,clip.transition==='dissolve'?progress:1,brightness);
      if (clip.transition==='wipe') ctx.restore();
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
      syncMedia(!playing);
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
    if (needsAudio && item.audioStreams>1 && replacement.audioStreams<item.audioStreams)
      throw Error(replacement.name+' has fewer audio streams than the original source.');
    const end=Math.max(0,...clips.map(c=>{
      const next=followingTransition(c);
      const handle=c.reverse?transitionBefore(c):next?transitionLength(next)-transitionBefore(next):0;
      return c.in+(c.duration+handle)*(Number(c.speed)||1);
    }));
    if(!replacement.still && replacement.duration+1e-6<end) throw Error(replacement.name+' is shorter than the source used on this timeline.');
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
  function validateProject(value, depth = 0) {
    if (depth >= 4) throw new Error("This project nests sequences more than four levels deep.");
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
    if (value.trackMix != null && (typeof value.trackMix !== "object" ||
        Array.isArray(value.trackMix) || Object.values(value.trackMix).some(mix => !mix || typeof mix !== "object" || Array.isArray(mix))))
      throw new Error("This project file has invalid audio mixer settings.");
    if (value.markers != null && (!Array.isArray(value.markers) ||
        value.markers.some(marker => !marker || typeof marker.name !== "string" || !Number.isFinite(marker.time))))
      throw new Error("This project file has invalid markers.");
    if (value.captions != null && (!Array.isArray(value.captions) ||
        value.captions.some(cue => !cue || typeof cue.text !== "string" || !Number.isFinite(cue.start) || !Number.isFinite(cue.duration))))
      throw new Error("This project file has invalid captions.");
    for (const field of ["premiereSequences", "compatibilityWarnings"]) {
      const entries = value[field];
      if (entries != null && (!Array.isArray(entries) || entries.some(entry => typeof entry !== "string")))
        throw new Error("This project file has invalid Premiere metadata.");
    }
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
      if (Number.isInteger(item.nestedSequence) && item.nestedProject != null)
        validateProject(item.nestedProject, depth + 1);
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
      if (clip.type === "title" && clip.title != null && typeof clip.title !== "string")
        throw new Error("A title in this project has invalid text.");
      if (clip.keyframes != null && (typeof clip.keyframes !== "object" || Array.isArray(clip.keyframes) ||
          Object.values(clip.keyframes).some(points => !Array.isArray(points) ||
            points.some(point => !point || !Number.isFinite(point.time) || !Number.isFinite(point.value)))))
        throw new Error("This project file has invalid keyframes.");
      if (clip.color?.lut) window.SpliceLUT.validate(clip.color.lut,clip.color.lutIntensity);
      clipIds.add(clip.id);
    }
  }
  const Editor = {
    formatTimecode(seconds, fps = 30) {
      const frames = Math.max(0, Math.round((seconds || 0) * fps));
      const nominal = Math.round(fps);
      const whole = Math.floor(frames / nominal);
      return [Math.floor(whole/3600),Math.floor(whole/60)%60,whole%60,frames%nominal]
        .map(value=>String(value).padStart(2,'0')).join(':');
    },
    get: () => ({
      project,
      revision,
      selectedId,
      selected: selected(),
      time,
      playing,
      playbackRate,
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
      assertTransitionHandles(project,next);
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
      if(playbackRate>0) audioContext?.resume().catch(()=>{});
      syncMedia(true);notify();
    },
    shuttle(direction) {
      const rate=playing && Math.sign(playbackRate)===direction ? Math.min(4,Math.abs(playbackRate)*2) : 1;
      Editor.setRate(direction*rate);
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
    setSourceSettings(id, settings) {
      const item=project.media.find(entry=>entry.id===id);
      if (!item) return;
      Object.assign(item,settings);
      revision++;
      notify();
    },
    sourceSelection,
    titleTrack,
    async prepareSourceProxy(id) {
      const sequence=project, media=sequence.media.find(item=>item.id===id);
      if(!media || media.offline || !media.src)throw Error('Relink the source before building its seek proxy.');
      const originalSrc=media.originalSrc||media.src;
      const src=await SpliceMedia.makeProxy(originalSrc,true,media.audioStream??null);
      if(sequence!==project)throw Error('The project changed while the proxy was building. Choose the source again.');
      const update={...media,originalSrc,src,seekOptimized:true};
      await SpliceMedia.remember(update);
      if(sequence!==project)throw Error('The project changed while the proxy was building. Choose the source again.');
      Editor.editProject(next=>Object.assign(next.media.find(item=>item.id===id),update));
      return update;
    },
    async addMedia(files) {
      const imports=Array.from(files);
      if (!imports.length) return;
      const sequence=project;
      const mapped=new Set();
      const choices=imports.map(file=>{
        const candidates=sequence.media.filter(item=>SpliceMedia.matches(item,file));
        if (candidates.length>1) throw Error('Multiple offline sources match '+file.name+'. Relink them individually.');
        const offline=candidates[0];
        if (offline && mapped.has(offline.id)) throw Error('Ambiguous copies of '+file.name+'. Relink this source individually.');
        if (offline) mapped.add(offline.id);
        return {file,offline};
      });
      const rows=[];
      for (const {file,offline} of choices) {
        const decoded=await fileToMedia(file);
        if(offline) checkReplacement(offline,decoded);
        const item=offline ? {...offline,...decoded,id:offline.id,path:offline.path} : decoded;
        rows.push(item);
      }
      if(project!==sequence) throw Error('The active sequence changed during import. Import again in the intended sequence.');
      await SpliceMedia.rememberMany(rows);
      if(project!==sequence) throw Error('The active sequence changed during import. Import again in the intended sequence.');
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
      const unverified=matches.filter(match=>match.needsConfirmation);
      if(unverified.length){
        const examples=unverified.slice(0,5).map(({item,file})=>`${item.path||item.name} → ${file.webkitRelativePath||file.name}`);
        const remaining=unverified.length-examples.length;
        if(remaining) examples.push(`and ${remaining} more`);
        const reason=unverified.every(match=>match.matched===1)?'Only the filenames match':'The selected folders do not identify the original camera or path';
        if(!window.confirm(`Review these relinks. ${reason}:\n\n${examples.join('\n')}\n\nUse these files?`))return false;
      }
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
    addToTimeline(id, placement) {
      const item = project.media.find((m) => m.id === id);
      if (!item) return;
      const start = placement?.start!==undefined ? snap(placement.start) : (
        item.type === "audio"
          ? time
          : Math.max(
              0,
              ...project.clips
                .filter((c) => c.type === "video")
                .map((c) => c.start + c.duration),
            ));
      const clip = {
        id: uid(),
        mediaId: id,
        start,
        ...sourceSelection(item),
        track: placement?.track ?? (item.type === "audio" ? -1 : 1),
        type: item.type,
        title: "",
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: "none",
      };
      Editor.editProject(next=>next.clips.push(clip));
      selectedId = clip.id;
      notify();
    },
    updateClip(id, patch) {
      const clip = project.clips.find((c) => c.id === id);
      if (!clip) return;
      if ('track' in patch && (!Number.isInteger(patch.track) || Math.abs(patch.track)>64 ||
          (clip.type==='audio' ? patch.track>=0 : patch.track<=0))) throw Error('Choose a valid timeline track.');
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
            (!source?.still && source?.duration && Number(candidate.in)+Number(candidate.duration)*Number(candidate.speed ?? 1)>source.duration+.01))
          throw new Error("Clip extends beyond available source footage.");
      }
      Editor.editProject(next=>{
        for(const change of changes){
          const target=next.clips.find(c=>c.id===change.id), original={...target};
          Object.assign(target,change.patch);
          if (change.patch.transition==='none') {
            delete target.transitionBefore;
            delete target.transitionMatchName;
          }
          for(const key of ["start","duration"])if(key in change.patch)target[key]=snap(target[key]);
          if ('duration' in change.patch && !('keyframes' in change.patch)) {
            if ('speed' in change.patch && original.keyframes) {
              const ratio=target.duration/original.duration;
              target.keyframes=Object.fromEntries(Object.entries(original.keyframes).map(([key,points])=>
                [key,points.map(point=>({...point,time:point.time*ratio}))]));
            } else resizeKeyframes(target,original,0);
          }
        }
      });
    },
    setClipSpeed(id, speed) {
      const clip=project.clips.find(c=>c.id===id);
      if(!clip)return;
      if(clip.nativeTimeRemap)throw Error('Edit this Premiere speed curve in Premiere.');
      if(!Number.isFinite(speed) || speed<.1 || speed>8)throw Error('Choose a clip speed between 10% and 800%.');
      const oldSpeed=Number(clip.speed)||1;
      if(Math.abs(speed-oldSpeed)<1e-6)return;
      const sourceLength=clip.duration*oldSpeed;
      const frames=Math.floor(sourceLength*project.fps/speed+1e-6);
      if(frames<1)throw Error('This speed would make the clip shorter than a timeline frame.');
      Editor.updateClip(id,{speed,duration:frames/project.fps});
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
      const earliest=mediaItem?.still ? 0 : clip.reverse ? oldStart-(sourceEnd-clip.in-clip.duration*speed)/speed : oldStart-clip.in/speed;
      const latest=mediaItem?.still ? Infinity : clip.reverse ? oldEnd+clip.in/speed : oldStart+(sourceEnd-clip.in)/speed;
      const newStart = edge === "start" ? clamp(boundary, Math.max(0, earliest), oldEnd - 1/project.fps) : oldStart;
      const newEnd = edge === "end" ? clamp(boundary, oldStart + 1/project.fps, latest) : oldEnd;
      const shift = (edge === "start" ? newStart - oldStart : newEnd - oldEnd);
      if (!shift) return;
      const memberIds=new Set([clip.id,...linkedClips(clip).map(c=>c.id)]);
      const rippleShift=edge==="start"?-shift:shift;
      for(const member of project.clips.filter(c=>memberIds.has(c.id))){
        const nextIn=member.in+(edge==="start"&&!member.reverse?shift*(Number(member.speed)||1):edge==="end"&&member.reverse?-shift*(Number(member.speed)||1):0);
        const nextDuration=member.duration+(edge==="start"?-shift:shift);
        const source=mediaOf(member);
        if(nextDuration<1/project.fps-.001 ||
           (!source?.still && (nextIn<-.001 || (source?.duration && nextIn+nextDuration*(Number(member.speed)||1)>source.duration+.01))))
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
      const head = clip => mediaOf(clip)?.still ? Infinity : clip.in / (Number(clip.speed) || 1);
      const tail = clip => mediaOf(clip)?.still ? Infinity : ((mediaOf(clip)?.duration || Infinity) - clip.in) / (Number(clip.speed) || 1) - clip.duration;
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
      const clip = project.clips.find(c => c.id === id), source = clip && mediaOf(clip);
      if (!clip || !source?.duration || source.still) return;
      ensureLinkedEditable(clip);
      const members=[clip,...linkedClips(clip)];
      const bounds=members.map(member=>{
        if(mediaOf(member)?.still) return [-Infinity,Infinity];
        const following=followingTransition(member);
        const before=transitionBefore(member);
        const after=following ? requestedTransitionLength(following)-transitionBefore(following) : 0;
        const speed=Number(member.speed)||1;
        const sourceHead=(member.reverse?after:before)*speed;
        const sourceTail=(member.duration+(member.reverse?before:after))*speed;
        return [sourceHead-member.in,(mediaOf(member)?.duration||0)-member.in-sourceTail];
      });
      const lower=Math.max(...bounds.map(([start])=>start));
      const upper=Math.min(...bounds.map(([,end])=>end));
      if (lower>upper) throw new Error('Not enough source footage to slip the clip and keep its transitions.');
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
    moveClips(placements) {
      const byId=new Map(placements.map(item=>[item.id,item]));
      Editor.editProject(next=>{
        for(const clip of next.clips){
          const placement=byId.get(clip.id);
          if(!placement)continue;
          if(clip.type==='audio'?placement.track>=0:placement.track<1)
            throw Error('There is no room to move the selection and keep its track spacing.');
          clip.start=snap(placement.start);
          clip.track=placement.track;
        }
      });
    },
    addTitle() {
      const track=titleTrack();
      if (trackLocked("title", track)) throw new Error("Unlock the title track before editing.");
      const clip = {
        id: uid(),
        mediaId: null,
        start: time,
        duration: 4,
        in: 0,
        track,
        type: "title",
        title: "Your title",
        color: { brightness: 100, contrast: 100, saturation: 100 },
        volume: 100,
        transition: "none",
      };
      Editor.editProject(next=>next.clips.push(clip));
      selectedId = clip.id;
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
          signal: AbortSignal.timeout(120000),
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
