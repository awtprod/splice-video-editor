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
  const comparisonVideos = new Map();
  let activeVideoId = null;
  let transitionVideoId = null;
  let preloadClipId = null;
  let mutedTracks = { audio: false, video: false, title: false };
  let revision = 0;
  let opened = 0;
  let gesture = false;
  let gestureBefore = null;
  // The state an atomic action started from; it must never be changed in place.
  let transactionBase = null;
  // Linked selection: when off, linked clips move and trim on their own (Premiere's toggle).
  let linkedSelection = true;
  let linkSuspended = 0;
  const linkingActive = () => linkedSelection && !linkSuspended;
  let audioContext;
  const audioNodes = new WeakMap();
  const auditionSpan = 4;
  const auditionBuffers = new Map();
  const auditions = new Map();
  function auditionBuffer(src, index, rate, reversed, stream=null) {
    const key=JSON.stringify([src,index,rate,reversed,stream]);
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
    const query=new URLSearchParams({src,index,rate,reverse:reversed?1:0});
    if (Number.isInteger(stream)) query.set('stream',stream);
    fetch('/api/media/audition?'+query,{
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
    // A chosen source stream is read from the original file; otherwise from what is playing.
    const stream=clipAudioStream(clip,item);
    const chosen=Number.isInteger(stream) ? stream : null;
    const src=chosen===null ? element.getAttribute('src') : item?.originalSrc||item?.src;
    if (!src || !item || item.audioChannels===0 || item.still || clip.embeddedAudio===false || target<0) return;
    const sourceSpan = auditionSpan*rate;
    const index=Math.floor(target/sourceSpan), step=clip.reverse?-1:1;
    const initial=auditionBuffer(src,index,rate,clip.reverse,chosen);
    const next=index+step;
    if (next>=0 && next*sourceSpan<item.duration && (clip.reverse?target-index*sourceSpan:(index+1)*sourceSpan-target)<rate*.75)
      auditionBuffer(src,next,rate,clip.reverse,chosen);
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
      buffer=auditionBuffer(src,chunk,rate,clip.reverse,chosen);
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
  // Per-project caches for what playback asks every frame. A project object changes only
  // through snapshot() (which clears them) or is replaced by a new one.
  let cachedFor = null, cachedDuration = 0;
  const duration = () => {
    if (cachedFor !== project) {
      let end = 0;
      for (const clip of project.clips) end = Math.max(end, clip.start + clip.duration);
      cachedFor = project;
      cachedDuration = end;
      indexedFor = null;
    }
    return cachedDuration;
  };
  let indexedFor = null;
  const notify = () => subscribers.forEach((fn) => fn(Editor.get()));
  // Undo states share large immutable payloads instead of copying them per edit:
  // LUT tables are replaced, never mutated, and strings (the embedded Premiere
  // original) are immutable. Everything else is copied so drafts never alias.
  function cloneState(value, key) {
    if (value === null || typeof value !== "object") return value;
    if (key === "lut" || key === "inputLut") return value;
    if (Array.isArray(value)) return value.map((item) => cloneState(item));
    const copy = {};
    for (const name of Object.keys(value)) copy[name] = cloneState(value[name], name);
    return copy;
  }
  function sameValue(left, right) {
    if (left === right) return true;
    if (!left || !right || typeof left !== "object" || typeof right !== "object")
      return Number.isNaN(left) && Number.isNaN(right);
    if (Array.isArray(left) !== Array.isArray(right)) return false;
    if (Array.isArray(left))
      return left.length === right.length && left.every((item, index) => sameValue(item, right[index]));
    let present = 0;
    for (const key of Object.keys(left)) {
      if (left[key] === undefined) continue;
      if (!sameValue(left[key], right[key])) return false;
      present++;
    }
    for (const key of Object.keys(right)) if (right[key] !== undefined) present--;
    return present === 0;
  }
  // After an edit, point unchanged clips and media back at the previous state's
  // objects so fifty undo steps of a 2,000-clip timeline hold one copy of each.
  function shareUnchanged(before, after) {
    for (const field of ["clips", "media"]) {
      const previous = new Map((before[field] || []).map((item) => [item.id, item]));
      const items = after[field] || [];
      for (let index = 0; index < items.length; index++) {
        const prior = previous.get(items[index]?.id);
        if (prior && sameValue(prior, items[index])) items[index] = prior;
      }
    }
  }
  function remember(previous) {
    history.push(previous);
    if (history.length > 50) history.shift();
    future = [];
    revision += 1;
  }
  // In-place edits: retire the current state to history and continue on a copy.
  const snapshot = () => {
    cachedFor = null;
    if (gesture) {
      if (project === gestureBefore || project === transactionBase) project = cloneState(project);
      return;
    }
    remember(project);
    project = cloneState(project);
  };
  // One user action that takes several edits: it becomes one undo step, or, if any part
  // fails, the project returns to exactly where it was and the error is raised.
  function atomically(action) {
    if (transactionBase) return action();
    const before = project, inGesture = gesture;
    transactionBase = before;
    if (!inGesture) { gesture = true; gestureBefore = before; }
    let failed = false;
    try {
      return action();
    } catch (error) {
      failed = true;
      throw error;
    } finally {
      transactionBase = null;
      if (!inGesture) { gesture = false; gestureBefore = null; }
      if (failed || sameValue(before, project)) project = before;
      else {
        if (!inGesture) remember(before);
        // Only the new state is written to; `before` may be an undo state.
        project.duration = duration();
        time = clamp(time, 0, project.duration);
      }
      syncMedia(true);
      notify();
    }
  }
  const mediaOf = (clip) => project.media.find((m) => m.id === clip.mediaId);
  const titleTrack = () => Math.min(64,Math.max(4,...project.clips.filter(c=>c.type==='video').map(c=>c.track+1)));
  function sourceSelection(item, marks=item.sourceMarks, stream=item.sourceAudioStream) {
    const usedLength=project.clips.find(c=>c.mediaId===item.id)?.duration;
    const available=item.duration||usedLength||5;
    const selection={in:0,duration:available};
    // Either mark may be missing: an In alone runs to the end, an Out alone from the start.
    if(Number.isFinite(marks?.in) || Number.isFinite(marks?.out)) {
      const from=Number.isFinite(marks.in) ? clamp(marks.in,0,available) : 0;
      const to=Number.isFinite(marks.out) ? clamp(marks.out,from,available) : available;
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
  const trackHidden = (kind, track, source = project) => trackState(kind, track, source).hidden === true;
  const groupMembers = (clip, source = project) => clip.linkGroup
    ? source.clips.filter((other) => other.id !== clip.id && other.linkGroup === clip.linkGroup) : [];
  const linkedClips = (clip) => linkingActive() ? groupMembers(clip) : [];
  // A link group with two or more members on one lane holds the pieces of one shot that
  // picture-only edits cut apart, kept in sync with the sound that spans them. Linked operations
  // then act on the members they concern rather than on the whole group:
  // - an edit at one edge (trim, roll) also moves only members whose same edge is at that time;
  // - an operation on a whole clip (delete, enable/disable, slide) also takes only the members
  //   that lie within it (the sound spanning every piece takes every piece with it);
  // - a ripple delete cuts a linked clip that spans the removed stretch, so the members after it
  //   move left together (see deleteClips).
  // Groups with at most one member per lane (a linked pair, J- and L-cuts included) act whole.
  const piecesGroup = (members) => {
    const lanes = new Set();
    for (const member of members) {
      const lane = (member.type === "audio" ? "audio:" : "video:") + member.track;
      if (lanes.has(lane)) return true;
      lanes.add(lane);
    }
    return false;
  };
  const partnersAtEdge = (clip, edge) => {
    const members = linkedClips(clip);
    if (!members.length || !piecesGroup([clip, ...members])) return members;
    const edgeOf = (member) => edge === "start" ? member.start : member.start + member.duration;
    return members.filter((member) => Math.abs(edgeOf(member) - edgeOf(clip)) < .5 / project.fps);
  };
  const partnersWithin = (clip) => {
    const members = linkedClips(clip);
    if (!members.length || !piecesGroup([clip, ...members])) return members;
    const end = clip.start + clip.duration;
    return members.filter((member) => member.start >= clip.start - 1e-6 && member.start + member.duration <= end + 1e-6);
  };
  // A link group reduced to one clip (linked selection off) is no longer a link.
  function dropLoneLinks(source) {
    const counts = new Map();
    for (const clip of source.clips) if (clip.linkGroup) counts.set(clip.linkGroup, (counts.get(clip.linkGroup) || 0) + 1);
    for (const clip of source.clips) if (clip.linkGroup && counts.get(clip.linkGroup) < 2) delete clip.linkGroup;
  }
  const hasEmbeddedAudio = (clip, source = project) => {
    if (clip.type !== "video" || clip.embeddedAudio === false) return false;
    const item = source.media.find((entry) => entry.id === clip.mediaId);
    return !!item && !item.still && item.audioChannels > 0;
  };
  const frameSeconds = () => 1 / project.fps;
  // Timeline seconds before the clip's first frame and after its last that the source can still supply.
  const sourceHead = (clip) => mediaOf(clip)?.still ? Infinity : clip.in / (Number(clip.speed) || 1);
  const sourceTail = (clip) => mediaOf(clip)?.still ? Infinity :
    ((mediaOf(clip)?.duration || Infinity) - clip.in) / (Number(clip.speed) || 1) - clip.duration;
  const roomBefore = (clip) => clip.reverse ? sourceTail(clip) : sourceHead(clip);
  const roomAfter = (clip) => clip.reverse ? sourceHead(clip) : sourceTail(clip);
  // Frames by which a linked clip's picture or sound has drifted from its partner.
  function syncOffset(clip, source = project) {
    const partner = groupMembers(clip, source).find((other) => other.mediaId === clip.mediaId && other.type !== clip.type);
    if (!partner || clip.reverse || partner.reverse) return null;
    const speed = Number(clip.speed) || 1, partnerSpeed = Number(partner.speed) || 1;
    if (Math.abs(speed - partnerSpeed) > 1e-6) return null;
    const origin = clip.start - clip.in / speed, partnerOrigin = partner.start - partner.in / partnerSpeed;
    return {partner, frames: Math.round((origin - partnerOrigin) * source.fps)};
  }
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
      if (afterMembers.length && piecesGroup(members)) {
        // Pieces of a shot: a member may be deleted or edited alone, but every member still in the
        // group keeps its picture and sound in sync with the others (equal sync-anchor shifts).
        if (members.some((clip) => current.has(clip.id) && current.get(clip.id).linkGroup !== key))
          throw new Error("Linked audio and video must be edited together.");
        const shifts = afterMembers.flatMap((clip) => {
          const prior = members.find((member) => member.id === clip.id);
          return prior ? [syncAnchor(clip) - syncAnchor(prior)] : [];
        });
        if (shifts.some((shift) => Math.abs(shift - shifts[0]) > .001))
          throw new Error("Linked audio and video must stay aligned.");
        continue;
      }
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
    const unchanged = (clip, prior) => prior && prior.start === clip.start && prior.duration === clip.duration &&
      prior.track === clip.track && prior.type === clip.type;
    const lanes = new Map();
    for (const clip of after.clips) {
      const key = (clip.type === 'audio' ? 'a' : 'v') + clip.track;
      if (!lanes.has(key)) lanes.set(key, []);
      lanes.get(key).push(clip);
    }
    // Sweep each lane in start order, comparing a clip only with clips still running.
    for (const clips of lanes.values()) {
      clips.sort((a, b) => a.start - b.start);
      let running = [];
      for (const clip of clips) {
        running = running.filter(other => other.start + other.duration - .001 > clip.start);
        for (const other of running) {
          if (!sameLaneOverlap(clip, other)) continue;
          const prior = original.get(clip.id), oldOther = original.get(other.id);
          if (unchanged(clip, prior) && unchanged(other, oldOther)) continue;
          if (!prior || !oldOther || !sameLaneOverlap(prior, oldOther)) {
            const moved = unchanged(clip, prior) ? other : clip;
            throw new Error(`Move blocked: ${moved.type} track ${Math.abs(moved.track)} already has a clip there.`);
          }
        }
        running.push(clip);
      }
    }
  };
  const assertLockedTracksUnchanged = (before, after) => {
    const oldClips = new Map(before.clips.map((clip) => [clip.id, clip]));
    const newClips = new Map(after.clips.map((clip) => [clip.id, clip]));
    for (const clip of before.clips)
      if (trackLocked(clip.type, clip.track, before) && !sameValue(clip, newClips.get(clip.id)))
        throw new Error(`Unlock ${clip.type} track ${Math.abs(clip.track)} before editing.`);
    for (const clip of after.clips)
      if (trackLocked(clip.type, clip.track, after) && !sameValue(clip, oldClips.get(clip.id)))
        throw new Error(`Unlock ${clip.type} track ${Math.abs(clip.track)} before editing.`);
  };
  // A disabled clip (`enabled === false`) keeps every property but contributes no picture and
  // no sound, its own embedded camera sound included; absent or true means enabled.
  // Transitions next to a disabled clip follow the renderer (render_movie._disabled_stand_ins):
  // the transition still plays for the enabled clip and the disabled side is transparent and
  // silent. A transition carried by a disabled clip dissolves / wipes / dips the clip before it
  // out to whatever is underneath (black if nothing); a transition on the clip after a disabled
  // clip brings that clip in from what is underneath; an audio crossfade fades the enabled side
  // against silence.
  const isEnabled = (clip) => clip?.enabled !== false;
  // A disabled picture clip that carries a transition: drawn as nothing, but its transition
  // still takes the clip before it out.
  const standIn = (clip) => clip.type === "video" && !isEnabled(clip) && ["dissolve", "wipe", "fade"].includes(clip.transition);
  // The topmost enabled clip of a kind at a time. Hidden picture tracks never reach the program
  // monitor; their clips still get media elements (see syncMedia's layers), so embedded camera sound plays.
  const at = (kind, t) =>
    project.clips
      .filter(
        (c) => c.type === kind && isEnabled(c) && c.start <= t && t < c.start + c.duration &&
          (kind === "audio" || !trackHidden(kind, c.track)),
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
    duration();
    if (indexedFor===project) return;
    indexedFor=project;
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
  // Enabled sound clips heard now, including a clip fading out through the crossfade of the clip
  // after it (which may be disabled: the fade then runs against silence).
  const soundingAudioClips = () => project.clips.filter(c=>c.type==='audio' && isEnabled(c) && c.start-transitionBefore(c)<=time &&
    time<c.start+c.duration+(followingTransition(c)?transitionLength(followingTransition(c))-transitionBefore(followingTransition(c)):0));
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
  // Exposure and white balance gains (one formula, color-grade.js).
  const gradeGains = c => window.SpliceGrade.gains(c);
  const decode709 = value => Math.sign(value)*Math.abs(value)**2.4;
  const encode709 = value => Math.sign(value)*Math.abs(value)**(1/2.4);
  const decodePixels709 = Array.from({length:256},(_,level)=>decode709(level/255));
  // Inverse of the signed linear Rec.709 grading-proxy packing in media_proxy.py.
  const decodeGradePixels = Array.from({length:256},(_,level)=>{
    if (level<32) return -Math.expm1((32-level)/3.45)/256;
    if (level>206) return Math.exp((level-206)/11);
    return ((level-32)/174)**2.4;
  });
  const primaryColorKeys = ['brightness','contrast','saturation','exposure','temperature','tint'];
  function hdrStaticColor(clip) {
    const color = clip?.color || {};
    if (color.grade?.bypass) return {};
    const keys = clip?.keyframes || {};
    const out = {};
    for (const key of primaryColorKeys) {
      if (keys[key]?.length) continue;
      if (key in color) out[key] = color[key];
    }
    return out;
  }
  function hasHdrGrade(clipOrColor={}) {
    const color = 'color' in clipOrColor || 'keyframes' in clipOrColor ? hdrStaticColor(clipOrColor) : clipOrColor;
    if (color.grade?.bypass) return false;
    return ['brightness','contrast','saturation'].some(key=>(color[key]??100)!==100) ||
      ['exposure','temperature','tint'].some(key=>Number(color[key])||0);
  }
  const clipAudioStream=(clip,item)=>'audioStream' in clip?clip.audioStream:item?.audioStream??null;
  function programSource(clip) {
    const item=clip && mediaOf(clip);
    if (!item?.src) return;
    const stream=clipAudioStream(clip,item);
    if (item.hdr && hasHdrGrade(clip)) {
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
    const decode=gradingData?decodeGradePixels:decodePixels709;
    for(let i=0;i<pixels.length;i+=4) {
      if (!pixels[i+3]) continue;
      let r=decode[pixels[i]],g=decode[pixels[i+1]],b=decode[pixels[i+2]];
      const before=Math.max(r,g,b);
      if (!gradingData && before>hdrShoulder) {
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
            isEnabled(c) &&
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
    const transitions = source.clips.filter(clip => ['dissolve','wipe','fade'].includes(clip.transition));
    if (!transitions.length) return new Map();
    const media = new Map(source.media.map(item=>[item.id,item]));
    const boundary = (clip, point) => `${clip.type}:${clip.track}:${Math.round(point*source.fps)}`;
    const ends = new Map(source.clips.filter(clip=>clip.type!=='title' && media.get(clip.mediaId)?.nativeGenerator!==1414680150)
      .map(clip=>[boundary(clip,clip.start+clip.duration),clip]));
    const shortages = new Map();
    for (const clip of transitions) {
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
  function layerVideo(cache, id, source) {
    let element=cache.get(id);
    if (!element) {
      element=document.createElement('video');element.muted=true;element.playsInline=true;
      element.preload='auto';element.style.display='none';document.body.append(element);cache.set(id,element);
    }
    if (element.getAttribute('src')!==source) element.src=source;
    return element;
  }
  function releaseLayerVideo(cache,id,element) {
    element.pause();disconnectAudio(element);element.removeAttribute('src');element.load();element.remove();cache.delete(id);
  }
  function syncMedia(forceSeek = false) {
    ensureElements();
    for(const state of auditions.values()) state.active=false;
    indexTransitions();
    if (window.SpliceMedia?.prepareNested) for (const clip of project.clips) {
      if (clip.type==='title' || !isEnabled(clip) || clip.start-transitionBefore(clip)>time+1.5 || clip.start+clip.duration<time) continue;
      const item=mediaOf(clip);
      if (item?.nestedProject && !item.src) window.SpliceMedia.prepareNested(item);
    }
    const clip = at("video", time);
    // The clip before a disabled clip's transition keeps playing out through that transition.
    const standInTails = new Map();
    for (const c of project.clips) {
      if (!standIn(c) || trackHidden("video", c.track)) continue;
      const tail = transitionTail(c, time);
      if (tail) standInTails.set(tail.id, tail);
    }
    const audioClips = soundingAudioClips();
    const mediaItem = clip && mediaOf(clip);
    const source = programSource(clip);
    if (clip && clip.id !== activeVideoId) {
      const preparedLayer=layerVideos.get(clip.id);
      const prepared=clip.id===preloadClipId?transitionVideo:preparedLayer;
      if(prepared){
        const outgoing=video, outgoingId=activeVideoId;
        const staysVisible=standInTails.has(outgoingId) ||
          project.clips.some(c=>c.id===outgoingId && isEnabled(c) && c.start<=time && time<c.start+c.duration);
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
        .filter(c=>c.type==='video' && isEnabled(c) && c.start>=time && c.start<=time+1.5 &&
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
    const visible = project.clips.filter(c=>c.type==='video' && isEnabled(c) && c.start-transitionBefore(c)<=time && time<c.start+c.duration);
    const layers = new Map(visible.map(layer=>[layer.id,layer]));
    for (const layer of visible) {
      const tail = transitionTail(layer,time);
      if (tail && tail.id!==previous?.id) layers.set(tail.id,tail);
    }
    for (const tail of standInTails.values()) if (tail.id!==previous?.id) layers.set(tail.id,tail);
    const preloading = new Set();
    if (playing && playbackRate>0) {
      const waiting = new Map();
      for (const next of project.clips) {
        if (next.type!=='video' || !isEnabled(next) || next.id===preloadClipId || layers.has(next.id)) continue;
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
      releaseLayerVideo(layerVideos,id,element);
    }
    for (const layer of layers.values()) {
      if (layer.id===activeVideoId || layer.id===preloadClipId) continue;
      const layerSource=programSource(layer);
      if (!layerSource) continue;
      const element=layerVideo(layerVideos,layer.id,layerSource);
      const preload = preloading.has(layer.id);
      const target=sourceTime(layer,preload?layer.start-transitionBefore(layer):time);
      seekVideo(element,layer,target,forceSeek);
      if (preload) {
        element.muted=true;
        setMediaPlaying(element,false);
      } else syncClipPlayback(element,layer,target,forceSeek);
    }
    // Bypass and split show the ordinary tone-mapped proxy. Decoding the 8-bit
    // grading proxy back to a neutral image can change saturated colours.
    const references=new Set();
    if ((monitor.bypass || monitor.split!=null) && !mutedTracks.video) {
      if (previous) layers.set(previous.id,previous);
      for (const layer of layers.values()) {
        const item=mediaOf(layer);
        if (preloading.has(layer.id) || trackHidden('video',layer.track) || !item?.hdr || !hasHdrGrade(layer)) continue;
        const main=videoFor(layer), gradingSource=window.SpliceMedia?.hdrPreviewStatus(item,clipAudioStream(layer,item))?.src;
        if (!gradingSource || main?.getAttribute('src')!==gradingSource) continue;
        const fresh=comparisonVideos.get(layer.id)?.getAttribute('src')!==item.src;
        const ref=layerVideo(comparisonVideos,layer.id,item.src), target=main.currentTime;
        const skew=Math.abs(ref.currentTime-target)>1/(item.fps || project.fps);
        seekVideo(ref,layer,target,forceSeek || fresh || (skew && !ref.seeking));
        syncClipPlayback(ref,layer,target,forceSeek,false);
        references.add(layer.id);
      }
    }
    for (const [id,element] of comparisonVideos) if (!references.has(id)) releaseLayerVideo(comparisonVideos,id,element);
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
      if (primaryColorKeys.includes(key)) {
        // The monitor blends graded pictures. Keep their original endpoints, including
        // the ones outside a trimmed clip, and rebase them onto its new local clock.
        cropped[key]=ordered.map(point=>({...point,time:point.time-from}));
        continue;
      }
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
  // `source` is the project being edited, which may already differ from the current one
  // (a lift drops the transitions it cuts through before cutting the clips around them).
  function splitClipAt(clip,cut,source=project) {
    const offset=cut-clip.start, duration=clip.duration;
    if (offset<=1e-6 || offset>=duration-1e-6) return null;
    if (['dissolve','wipe','fade'].includes(clip.transition) && offset<transitionLength(clip)-transitionBefore(clip)-1e-6)
      throw new Error('Move the cut outside the transition before splitting this clip.');
    if (clip.transition==='native') throw new Error('Replace the imported transition before splitting this clip.');
    const indexed = followingTransition(clip);
    const following = indexed && source.clips.find(other=>other.id===indexed.id && ['dissolve','wipe'].includes(other.transition));
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
  // An HDR clip graded with static brightness/contrast/exposure controls is graded in linear light
  // on the CPU (as the export grades it before the tone map) at the monitor's size; the result is a
  // layer the picture pipeline composites like any other. When bypassed while showing HDR grading
  // data, it still runs the neutral Rec.709 Mobius tone map so bypass preserves tone mapping.
  function hdrLayer(element,clip,canvas,bypass=false) {
    const color=bypass ? {} : hdrStaticColor(clip), gains=bypass ? [1,1,1] : gradeGains(color), item=mediaOf(clip);
    const gradingSource=window.SpliceMedia?.hdrPreviewStatus(item,clipAudioStream(clip,item))?.src;
    const gradingData=!!gradingSource && element.getAttribute('src')===gradingSource;
    const sourceWidth=element.videoWidth||canvas.width, sourceHeight=element.videoHeight||canvas.height;
    const fit=Math.min(1,canvas.width/sourceWidth,canvas.height/sourceHeight);
    const width=Math.max(1,Math.round(sourceWidth*fit)), height=Math.max(1,Math.round(sourceHeight*fit));
    const key=bypass ? `${clip.id}:bypass` : clip.id;
    let layer=layerCanvases.get(key);
    if (!layer) { layer=document.createElement('canvas'); layerCanvases.set(key,layer); }
    if (layer.width!==width || layer.height!==height) {layer.width=width;layer.height=height;}
    const target=layer.getContext('2d',{willReadFrequently:true});
    target.drawImage(element,0,0,width,height);
    const image=target.getImageData(0,0,width,height);
    gradeHdrPreview(image.data,color,gains,gradingData);
    target.putImageData(image,0,0);
    return layer;
  }
  // Monitor-only switches (not saved, not exported): grade bypass, before/after split, views.
  const monitor={bypass:false,split:null,view:'normal'};
  let comparing=false;
  document.addEventListener('splice-monitor-change',()=>{
    const active=monitor.bypass || monitor.split!=null;
    if (active!==comparing) { comparing=active;syncMedia(false); }
  });
  // The grade of one clip at the playhead, for the picture pipeline: one prepared grade, or the two
  // keyframe gradings and their mix (color-grade.js keySegment), plus the source's input LUT.
  function gradeOf(clip,bypass) {
    const item=mediaOf(clip), input=item?.inputLut||null, Grade=window.SpliceGrade;
    if (!Grade) return null;
    const clipBypass=bypass || !!clip.color?.grade?.bypass;
    if (clipBypass) return input?{a:Grade.prepare({}),input}:null;
    let color=clip.color||{};
    // HDR static primaries run in linear light (hdrLayer); animated primaries and wheels/curves/LUTs run here.
    if (item?.hdr) color=Object.fromEntries(Object.entries(color).filter(([key])=>!primaryColorKeys.includes(key)));
    const segment=Grade.keySegment(clip,time-clip.start);
    const subject=segment?{...clip,color}:null;
    const a=Grade.prepare(segment?Grade.colorAt(subject,segment.a):color);
    const b=segment && segment.b!==segment.a ? Grade.prepare(Grade.colorAt(subject,segment.b)) : null;
    if (a.identity && !b && !input) return null;
    return {a,b,weight:segment?.weight||0,input};
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
  // Whether a video layer paints every pixel of the frame with full opacity right now.
  // A clip inside its own dissolve does not, with or without a clip before it; nor does a clip
  // that a transition is taking out (it may be leaving against a disabled, transparent clip).
  function coversFrame(clip, canvas, outgoingIds) {
    if (clip.type!=='video' || !isEnabled(clip) || clip.transition==='wipe' || clip.transition==='fade' || outgoingIds.has(clip.id)) return false;
    if (clip.transition==='dissolve' && time<clip.start+transitionLength(clip)-transitionBefore(clip)) return false;
    if (time<clip.start || videoDipFollowers.get(clip.id)) return false;
    const item=mediaOf(clip);
    if (!item || item.nativeGenerator===1414680150) return false;
    const element=videoFor(clip);
    if (!(element instanceof HTMLVideoElement) || element.readyState<2 || !element.videoWidth || !element.videoHeight) return false;
    if (Math.abs(element.videoWidth/element.videoHeight-canvas.width/canvas.height)>.01) return false;
    const opacity=animated(clip,"opacity",Number(clip.opacity??100));
    const scale=animated(clip,"scale",Number(clip.transform?.scale??100));
    const x=animated(clip,"x",Number(clip.transform?.x??0)), y=animated(clip,"y",Number(clip.transform?.y??0));
    const rotation=animated(clip,"rotation",Number(clip.transform?.rotation??0));
    return opacity>=100 && scale>=100 && !x && !y && !rotation;
  }
  // The frame at the playhead as a display list for the picture pipeline (gl-preview.js).
  function framePlan(canvas,bypass=monitor.bypass) {
    const plan={width:canvas.width,height:canvas.height,ops:[],view:monitor.view};
    const visible=project.clips.filter(clip=>clip.type!=='audio' && (isEnabled(clip) || standIn(clip)) && !trackHidden(clip.type,clip.track) && (clip.type==='video' ? !mutedTracks.video &&
      clip.start-transitionBefore(clip)<=time && time<clip.start+clip.duration :
      !mutedTracks.title && clip.title && clip.start<=time && time<clip.start+clip.duration));
    visible.sort((a,b)=>(a.track??4)-(b.track??4) || Number(a.type==='title')-Number(b.type==='title') || a.start-b.start);
    const dissolvePairs=new Map(), wipeOuts=new Map(), outgoingIds=new Set();
    for (const clip of visible) {
      if (clip.type!=='video' || time>=clip.start+transitionLength(clip)-transitionBefore(clip)) continue;
      const previous=preceding(clip);
      if (!previous) continue;
      if (clip.transition==='dissolve') {dissolvePairs.set(clip.id,previous);outgoingIds.add(previous.id);}
      // A disabled clip wiping in over the clip before it: that clip is wiped off to what is underneath.
      else if (clip.transition==='wipe' && !isEnabled(clip)) {wipeOuts.set(clip.id,previous);outgoingIds.add(previous.id);}
    }
    // Layers under an opaque, untransformed full-frame picture cannot show: skip drawing them.
    // (Their camera sound still plays; this only saves compositing work on stacked tracks.)
    let firstShown=0;
    for (let index=visible.length-1;index>=0;index--) {
      if (coversFrame(visible[index],canvas,outgoingIds)) { firstShown=index; break; }
    }
    const layerOp=(clip,alpha=1,brightness=1,wipe=null,wipeOut=null)=>{
      const element=videoFor(clip), item=mediaOf(clip);
      if (!element || (element instanceof HTMLVideoElement && element.readyState < 2)) return null;
      const clipBypass=bypass || !!clip.color?.grade?.bypass;
      const gradingSource=item?.hdr ? window.SpliceMedia?.hdrPreviewStatus(item,clipAudioStream(clip,item))?.src : null;
      const gradingData=!!gradingSource && element instanceof HTMLVideoElement && element.getAttribute('src')===gradingSource;
      const hdr=item?.hdr && element instanceof HTMLVideoElement && (clipBypass ? gradingData : hasHdrGrade(clip));
      const ref=clipBypass && gradingData && comparisonVideos.get(clip.id);
      const ready=ref?.readyState>=2 && ref.currentSrc===ref.src && !ref.seeking &&
        Math.abs(ref.currentTime-element.currentTime)<1/(item.fps || project.fps);
      const source=ready ? ref : hdr?hdrLayer(element,clip,canvas,clipBypass):element;
      return {type:'video',element:source,clip,exact:source instanceof HTMLVideoElement,
        input:{matrix:item?.hdr?'bt709':window.SpliceGrade?.matrixName(item?.colorSpace,item?.colorMatrix)||'bt601',fullRange:false},
        grade:gradeOf(clip,bypass), brightness, wipe, wipeOut,
        alpha:alpha*clamp(animated(clip,"opacity",Number(clip.opacity??100))/100,0,1),
        geometry:{sourceWidth:source.videoWidth||source.width||canvas.width,sourceHeight:source.videoHeight||source.height||canvas.height,
          scale:clamp(animated(clip,"scale",Number(clip.transform?.scale??100))/100,.1,4),
          x:clamp(animated(clip,"x",Number(clip.transform?.x??0)),-100,100),
          y:clamp(animated(clip,"y",Number(clip.transform?.y??0)),-100,100),
          rotation:clamp(animated(clip,"rotation",Number(clip.transform?.rotation??0)),-180,180)*Math.PI/180}};
    };
    for (const clip of visible.slice(firstShown)) {
      if (clip.type==='title') {
        plan.ops.push({type:'title',clip,paint:(ctx,target)=>paintTitle(ctx,target,clip)});
        continue;
      }
      if (outgoingIds.has(clip.id)) continue;
      if (mediaOf(clip)?.adjustment) {
        const grade=gradeOf(clip,bypass);
        if (grade && isEnabled(clip)) plan.ops.push({type:'adjust',clip,grade,alpha:clamp(animated(clip,"opacity",Number(clip.opacity??100))/100,0,1)});
        continue;
      }
      if (mediaOf(clip)?.nativeGenerator===1414680150) continue;
      if (clip.transition==='fade' && time<clip.start) continue;
      const progress=clamp((time-(clip.start-transitionBefore(clip)))/transitionLength(clip),0,1);
      const previous=dissolvePairs.get(clip.id);
      if (previous) {
        // As the export blends: (beneath + outgoing) * (1-p) + (beneath + incoming) * p.
        // A disabled incoming clip is transparent: outgoing dissolves out to what is underneath.
        const incoming=isEnabled(clip)?layerOp(clip):null, outgoing=layerOp(previous);
        if (outgoing || incoming) plan.ops.push({type:'dissolve',outgoing,incoming,progress});
        continue;
      }
      const wipedOff=wipeOuts.get(clip.id);
      if (wipedOff) {
        const op=layerOp(wipedOff,1,1,null,progress);
        if (op) plan.ops.push(op);
        continue;
      }
      // Any other disabled stand-in paints nothing (a dip it carries still darkens the clip before).
      if (!isEnabled(clip)) continue;
      let brightness=1;
      const head=transitionLength(clip)-transitionBefore(clip);
      if (clip.transition==='fade' && head>0) brightness=clamp((time-clip.start)/head,0,1);
      const nextDip=videoDipFollowers.get(clip.id);
      if (nextDip && transitionBefore(nextDip)>0) brightness*=clamp((nextDip.start-time)/transitionBefore(nextDip),0,1);
      if (clip.transition==='wipe') {
        const tail=transitionTail(clip,time), under=tail&&layerOp(tail);
        if (under) plan.ops.push(under);
      }
      const op=layerOp(clip,clip.transition==='dissolve'?progress:1,brightness,clip.transition==='wipe'?progress:null);
      if (op) plan.ops.push(op);
    }
    return plan;
  }
  function drawFrame(canvas) {
    const active = at('video', time);
    if (!mutedTracks.video && active && mediaOf(active)?.src &&
        video?.readyState < 2 && previewProjects.get(canvas) === project) return;
    previewProjects.set(canvas, project);
    const plan=framePlan(canvas);
    if (monitor.split!=null) { plan.split=monitor.split; plan.beforeOps=framePlan(canvas,true).ops; }
    window.SplicePicture.render(canvas,plan);
    const shown=new Set();
    for (const op of [...plan.ops,...(plan.beforeOps||[])]) for (const part of [op,op.outgoing,op.incoming]) if (part?.element) shown.add(part.element);
    window.SplicePicture.forget(shown);
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
  function validateTrackMix(key, mix) {
    // Premiere and working projects can have different track layouts.
    // Keep settings for empty working tracks within the editor's track limit.
    const track=Number(key);
    if (!Number.isInteger(track) || String(track)!==key || !track || Math.abs(track)>64)
      throw new Error("This project file has an invalid audio mixer track.");
    if (!mix || typeof mix !== "object" || Array.isArray(mix) ||
        (mix.gain !== undefined && !Number.isFinite(mix.gain)) ||
        (mix.pan !== undefined && !Number.isFinite(mix.pan)) ||
        (mix.mute !== undefined && typeof mix.mute !== "boolean") ||
        (mix.solo !== undefined && typeof mix.solo !== "boolean"))
      throw new Error("This project file has invalid audio mixer settings.");
    if (mix.gain < 0 || mix.gain > 200) throw new Error("Track gain must be between 0% and 200%.");
    if (mix.pan < -100 || mix.pan > 100) throw new Error("Track pan must be between -100 and 100.");
  }
  // LUT tables are immutable once validated; skip re-checking 100k samples on every edit.
  const validatedLuts = new WeakSet();
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
            (state.syncLocked !== undefined && typeof state.syncLocked !== "boolean") ||
            (state.hidden !== undefined && typeof state.hidden !== "boolean"))
          throw new Error("This project file has invalid track controls.");
    }
    if (value.trackCounts !== undefined && (!value.trackCounts ||
        !Number.isInteger(value.trackCounts.video) || !Number.isInteger(value.trackCounts.audio) ||
        value.trackCounts.video < 1 || value.trackCounts.video > 64 ||
        value.trackCounts.audio < 1 || value.trackCounts.audio > 64))
      throw new Error("This project file has invalid track counts.");
    if (value.trackMix != null) {
      if (typeof value.trackMix !== "object" || Array.isArray(value.trackMix))
        throw new Error("This project file has invalid audio mixer settings.");
      for (const [key,mix] of Object.entries(value.trackMix)) validateTrackMix(key,mix);
    }
    if (value.marks != null && (typeof value.marks !== "object" || Array.isArray(value.marks) ||
        ["in", "out"].some(edge => value.marks[edge] != null && (!Number.isFinite(value.marks[edge]) || value.marks[edge] < 0)) ||
        (value.marks.in != null && value.marks.out != null && value.marks.out <= value.marks.in)))
      throw new Error("This project file has an invalid timeline In/Out range.");
    if (value.startTimecode != null && (!Number.isInteger(value.startTimecode) || value.startTimecode < 0 ||
        value.startTimecode >= 24 * 3600 * Math.ceil(value.fps)))
      throw new Error("This project file has an invalid start timecode.");
    if (value.dropFrame != null && typeof value.dropFrame !== "boolean")
      throw new Error("This project file has an invalid timecode setting.");
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
      // Source colour settings: untagged matrix choice, technical input LUT, adjustment marker.
      if (item.colorMatrix !== undefined && !['bt709','bt601'].includes(item.colorMatrix))
        throw new Error("This project file has an invalid source colour setting.");
      if (item.adjustment !== undefined && typeof item.adjustment !== 'boolean')
        throw new Error("This project file has invalid media.");
      if (item.inputLut != null && !validatedLuts.has(item.inputLut)) {
        window.SpliceLUT.validate(item.inputLut,100);
        validatedLuts.add(item.inputLut);
      }
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
      if (clip.keyframes) window.SpliceGrade.keyTimes(clip);
      if (clip.color?.grade !== undefined) window.SpliceGrade.validateGrade(clip.color.grade);
      if (clip.color?.lut) {
        const intensity = clip.color.lutIntensity ?? 100;
        if (!Number.isFinite(intensity) || intensity < 0 || intensity > 100) throw new Error('Invalid LUT intensity.');
        if (!validatedLuts.has(clip.color.lut)) {
          window.SpliceLUT.validate(clip.color.lut,intensity);
          validatedLuts.add(clip.color.lut);
        }
      }
      clipIds.add(clip.id);
    }
  }
  // Older projects could give a title lane its own lock or visibility. Premiere has one video
  // track for both, so fold 'title:N' into 'video:N': locked or hidden if either was, sync-locked
  // only if both were.
  function mergeTitleTrackStates(source) {
    const states = source.trackState;
    if (!states) return;
    for (const key of Object.keys(states)) {
      if (!key.startsWith("title:")) continue;
      const videoKey = "video:" + key.slice(6), title = states[key], video = states[videoKey] || {};
      const merged = {locked: !!(title.locked || video.locked),
        syncLocked: title.syncLocked !== false && video.syncLocked !== false};
      if (title.hidden || video.hidden) merged.hidden = true;
      if (merged.locked || !merged.syncLocked || merged.hidden || states[videoKey]) states[videoKey] = merged;
      delete states[key];
    }
  }
  // Titles share the picture lane of their track number, as they do in Premiere.
  const laneOf = (clip) => (clip.type === "audio" ? "audio:" : "video:") + clip.track;
  const hasSound = (item) => item?.type === "video" && !item.still && item.audioChannels > 0;
  const startOfLane = (lane) => ({kind: lane.startsWith("audio") ? "audio" : "video", track: Number(lane.split(":")[1])});
  function freeAudioTrack(source, from, to) {
    for (let track = -1; track >= -64; track--) {
      if (trackLocked("audio", track, source)) continue;
      const busy = source.clips.some((clip) => clip.type === "audio" && clip.track === track &&
        clip.start < to - 1e-6 && clip.start + clip.duration > from + 1e-6);
      if (!busy) return track;
    }
    return null;
  }
  // Give a video clip's embedded camera sound its own audio clip, linked to the picture.
  function detachAudio(source, clip) {
    const track = freeAudioTrack(source, clip.start - transitionBefore(clip), clip.start + clip.duration);
    if (track === null) throw new Error("There is no free, unlocked audio track for this clip's sound.");
    const group = clip.linkGroup || uid();
    const sound = {id: uid(), mediaId: clip.mediaId, type: "audio", track, start: clip.start,
      duration: clip.duration, in: clip.in, title: "", volume: clip.volume ?? 100, transition: "none", linkGroup: group};
    for (const key of ["speed", "reverse", "pan", "fadeIn", "fadeOut", "audioStream"])
      if (key in clip) sound[key] = clip[key];
    if (clip.keyframes?.volume) {
      sound.keyframes = {volume: cloneState(clip.keyframes.volume)};
      delete clip.keyframes.volume;
    }
    if (["dissolve", "wipe", "fade"].includes(clip.transition)) {
      sound.transition = "dissolve";
      sound.transitionDuration = clip.transitionDuration;
      if (clip.transitionBefore) sound.transitionBefore = clip.transitionBefore;
    }
    clip.embeddedAudio = false;
    clip.linkGroup = group;
    for (const key of ["pan", "fadeIn", "fadeOut"]) delete clip[key];
    source.clips.push(sound);
    return sound;
  }
  // A new edit from a source. Camera media with sound becomes a linked picture/sound pair;
  // without a picture track only the sound is placed.
  function sourceClips(item, selection, start, videoTrack, audioTrack) {
    const base = {mediaId: item.id, start, title: "", volume: 100, transition: "none", ...selection};
    if (item.type === "audio" || videoTrack == null) return [{id: uid(), type: "audio", track: audioTrack, ...base}];
    const picture = {id: uid(), type: "video", track: videoTrack, color: {brightness: 100, contrast: 100, saturation: 100}, ...base};
    if (audioTrack == null || !hasSound(item)) return [picture];
    const group = uid();
    Object.assign(picture, {embeddedAudio: false, linkGroup: group});
    return [picture, {id: uid(), type: "audio", track: audioTrack, ...base, linkGroup: group}];
  }
  // Where a clip's source frame 0 sits on the timeline. Cutting a clip into pieces keeps this
  // anchor; moving a piece moves it. Linked pieces stay in sync while their anchors move together.
  function syncAnchor(clip) {
    const speed = Number(clip.speed) || 1;
    if (clip.reverse) return clip.start + clip.duration + clip.in / speed;
    return clip.start - clip.in / speed;
  }
  // Link bookkeeping for edits that cut link groups apart (lift, extract, overwrite, insert).
  // Every piece on one side of the cut joins a new group for that side. settle(), run once the
  // edit has moved what it moves, keeps a side linked when at least two of its pieces moved by the
  // same amount (they are still a valid pair, J- and L-cuts included); pieces that drifted from
  // that pair, and pieces left alone, lose the link.
  function linkSides(source, groups) {
    const anchors = new Map(), origins = new Map(), sides = new Map();
    for (const clip of source.clips) if (groups.has(clip.linkGroup)) {
      anchors.set(clip, syncAnchor(clip));
      origins.set(clip, clip.linkGroup);
    }
    return {
      assign(piece, original, side) {
        if (!origins.has(original)) return piece;
        const key = origins.get(original) + ":" + side;
        if (!sides.has(key)) sides.set(key, uid());
        anchors.set(piece, anchors.get(original));
        origins.set(piece, origins.get(original));
        piece.linkGroup = sides.get(key);
        return piece;
      },
      settle() {
        const owned = new Set(sides.values());
        const buckets = new Map();
        for (const clip of source.clips) {
          if (!anchors.has(clip) || !clip.linkGroup || !owned.has(clip.linkGroup)) continue;
          const moved = Math.round((syncAnchor(clip) - anchors.get(clip)) * source.fps * 100);
          if (!buckets.has(clip.linkGroup)) buckets.set(clip.linkGroup, new Map());
          const byShift = buckets.get(clip.linkGroup);
          if (!byShift.has(moved)) byShift.set(moved, []);
          byShift.get(moved).push(clip);
        }
        for (const byShift of buckets.values()) {
          const sets = [...byShift.values()].sort((a, b) => b.length - a.length);
          const keep = sets[0].length >= 2 ? sets[0] : [];
          for (const set of sets) if (set !== keep) for (const clip of set) delete clip.linkGroup;
        }
      },
    };
  }
  // Remove material between two times on the given lanes, cutting clips that cross the range.
  // Returns the link bookkeeping; call its settle() after the edit has finished moving clips.
  function clearRange(source, lanes, from, to, {lift = false} = {}) {
    const crossing = (clip) => lanes.has(laneOf(clip)) && clip.start < to - 1e-6 && clip.start + clip.duration > from + 1e-6;
    // A transition goes with the material it joins: drop it when the range removes part of the
    // transition or the clip before its cut. A lift ending on a cut leaves a gap before the next
    // clip, so its transition goes too; an overwrite puts new material there and keeps it.
    for (const clip of source.clips) {
      if (!lanes.has(laneOf(clip)) || !["dissolve", "wipe", "fade", "native"].includes(clip.transition)) continue;
      const head = clip.start - transitionBefore(clip), tail = head + requestedTransitionLength(clip);
      const cutInRange = clip.start >= from - 1e-6 && (lift ? clip.start <= to + 1e-6 : clip.start < to - 1e-6);
      if (cutInRange || (head < to - 1e-6 && tail > from + 1e-6)) {
        clip.transition = "none";
        for (const key of ["transitionBefore", "transitionDuration", "transitionMatchName"]) delete clip[key];
      }
    }
    const touched = new Set(source.clips.filter((clip) => clip.linkGroup && crossing(clip)).map((clip) => clip.linkGroup));
    // A member on a lane the edit leaves alone that spans the cleared range (the whole sound
    // under a picture-only overwrite) stays in sync with the pieces on both sides, so those
    // pieces and it remain one group; settle() still unlinks whatever the edit moved out of sync.
    const spanning = new Set(source.clips.filter((clip) => touched.has(clip.linkGroup) && !crossing(clip) &&
      clip.start < from - 1e-6 && clip.start + clip.duration > to + 1e-6).map((clip) => clip.linkGroup));
    const links = linkSides(source, touched);
    const kept = [];
    for (const clip of source.clips) {
      // Decided from the clip's group before assign() gives it its side's new group.
      const spans = spanning.has(clip.linkGroup);
      const sideOf = (side) => spans ? "spanning" : side;
      if (!crossing(clip)) {
        kept.push(links.assign(clip, clip, sideOf(clip.start >= to - 1e-6 ? "after" : "before")));
        continue;
      }
      const head = clip.start < from - 1e-6, tail = clip.start + clip.duration > to + 1e-6;
      if (head && tail) {
        const middle = splitClipAt(clip, from, source);
        const right = splitClipAt(middle, to, source);
        kept.push(links.assign(clip, clip, sideOf("before")), links.assign(right, clip, sideOf("after")));
      } else if (head) {
        splitClipAt(clip, from, source);
        kept.push(links.assign(clip, clip, sideOf("before")));
      } else if (tail) {
        const right = splitClipAt(clip, to, source);
        const native = {};
        for (const key of ["premiereIndex", "premiereTrack"]) if (key in clip) native[key] = clip[key];
        kept.push(links.assign({...right, ...native, id: clip.id}, clip, sideOf("after")));
      }
    }
    source.clips = kept;
    return links;
  }
  // Open a gap for an insert edit on the target lanes and every sync-locked track.
  function insertSpace(source, lanes, at, length) {
    const moves = (clip) => !trackLocked(clip.type, clip.track, source) &&
      (lanes.has(laneOf(clip)) || syncLocked(clip.type, clip.track, source));
    const cut = (clip) => moves(clip) && clip.start < at - 1e-5 && clip.start + clip.duration > at + 1e-5;
    const touched = new Set(source.clips.filter((clip) => clip.linkGroup && cut(clip)).map((clip) => clip.linkGroup));
    const links = linkSides(source, touched);
    const additions = [];
    for (const clip of [...source.clips]) {
      if (!moves(clip)) {
        links.assign(clip, clip, clip.start >= at - 1e-5 ? "after" : "before");
        continue;
      }
      if (clip.start >= at - 1e-5) {
        clip.start += length;
        links.assign(clip, clip, "after");
      } else if (clip.start + clip.duration > at + 1e-5) {
        const right = splitClipAt(clip, at, source);
        links.assign(clip, clip, "before");
        if (!right) continue;
        right.start += length;
        additions.push(links.assign(right, clip, "after"));
      } else links.assign(clip, clip, "before");
    }
    source.clips.push(...additions);
    links.settle();
  }
  // Cut the chosen clips at a frame. Members of a cut link group that lie wholly after
  // the cut join the right-hand group, so J- and L-cuts stay linked to their own side.
  function splitClips(source, ids, cut) {
    const crossing = new Set(source.clips.filter((clip) => ids.has(clip.id) &&
      clip.start < cut - 1e-6 && clip.start + clip.duration > cut + 1e-6).map((clip) => clip.id));
    const groups = new Map();
    for (const clip of source.clips)
      if (crossing.has(clip.id) && clip.linkGroup && !groups.has(clip.linkGroup)) groups.set(clip.linkGroup, uid());
    const rights = [];
    for (const clip of source.clips) {
      if (crossing.has(clip.id)) {
        const right = splitClipAt(clip, cut, source);
        if (!right) continue;
        if (clip.linkGroup) right.linkGroup = groups.get(clip.linkGroup);
        rights.push(right);
      } else if (groups.has(clip.linkGroup) && clip.start >= cut - 1e-6) clip.linkGroup = groups.get(clip.linkGroup);
    }
    source.clips.push(...rights);
    dropLoneLinks(source);
    return rights;
  }
  // SMPTE drop-frame applies to the NTSC rates 29.97 and 59.94 only.
  const isDropFrameRate = fps => Math.abs(fps - 30000/1001) < .001 || Math.abs(fps - 60000/1001) < .001;
  const usesDropFrame = (source = project) => isDropFrameRate(source.fps) && source.dropFrame !== false;
  // The position of a frame's label in an unbroken count of nominal-rate labels (drop-frame
  // timecode skips some labels, so there the count runs ahead of the frame number).
  function labelCount(frames, fps, dropFrame) {
    const nominal = Math.round(fps);
    let count = Math.max(0, Math.round(frames));
    if (dropFrame) {
      // Skip frame labels 00 and 01 (00-03 at 59.94) each minute except every tenth.
      const dropped = nominal / 15;
      const perTenMinutes = nominal * 600 - dropped * 9, perMinute = nominal * 60 - dropped;
      const tens = Math.floor(count / perTenMinutes), rest = count % perTenMinutes;
      count += dropped * 9 * tens + (rest > dropped ? dropped * Math.floor((rest - dropped) / perMinute) : 0);
    }
    return count;
  }
  // The frame that carries a label count; a skipped drop-frame label gives the next frame that exists.
  function frameOfLabel(count, fps, dropFrame) {
    if (!dropFrame) return count;
    const nominal = Math.round(fps), dropped = nominal / 15;
    const totalMinutes = Math.floor(count / (nominal * 60));
    const withinMinute = count % (nominal * 60);
    const skipped = totalMinutes % 10 !== 0 && withinMinute < dropped ? dropped - withinMinute : 0;
    return count + skipped - dropped * (totalMinutes - Math.floor(totalMinutes / 10));
  }
  function framesToTimecode(frames, fps, dropFrame) {
    const nominal = Math.round(fps);
    const count = labelCount(frames, fps, dropFrame);
    const whole = Math.floor(count / nominal);
    const fields = [Math.floor(whole / 3600), Math.floor(whole / 60) % 60, whole % 60, count % nominal]
      .map(value => String(value).padStart(2, "0"));
    return fields.slice(0, 3).join(":") + (dropFrame ? ";" : ":") + fields[3];
  }
  // Typed timecode, as fields from hours down to frames; only the fields typed are returned.
  // With ":" or ";" the text is split into fields ("." separates too). Without them, digits
  // fill pairs from the right and each "." stands for "00": "2." is 2 seconds,
  // "1.." is 1 minute and "1000" is 10 seconds.
  function typedFields(text) {
    const value = String(text).replace(/\s+/g, "");
    if (!value || !/^[\d:;.]+$/.test(value)) throw new Error("Type a timecode such as 01:00:10:00.");
    let parts;
    const dotted = value.split(".");
    if (/[:;]/.test(value)) parts = value.split(/[:;.]/);
    else if (value.replace(/\./g, "00").length > 8 && dotted.length <= 4 && dotted.every(part => /^\d{1,2}$/.test(part)))
      // Too long to read "." as "00" (e.g. 01.00.01.00): the dots separate fields.
      parts = dotted;
    else {
      const digits = value.replace(/\./g, "00");
      if (digits.length > 8) throw new Error("Type at most eight digits: hours, minutes, seconds and frames.");
      parts = [];
      for (let end = digits.length; end > 0; end -= 2) parts.unshift(digits.slice(Math.max(0, end - 2), end));
    }
    if (parts.length > 4 || parts.some(part => !/^\d{1,2}$/.test(part)))
      throw new Error("Type a timecode such as 01:00:10:00.");
    return parts.map(Number);
  }
  const timecodeFields = text => [0, 0, 0, ...typedFields(text)].slice(-4);
  function fieldsToFrames([hours, minutes, seconds, frames], fps, dropFrame) {
    const nominal = Math.round(fps);
    if (frames >= nominal || minutes > 59 || seconds > 59 || hours > 23)
      throw new Error("That timecode is out of range for " + Number(fps.toFixed(3)) + " fps.");
    const dropped = nominal / 15;
    // Drop-frame timecode skips these labels at the start of every minute except each tenth.
    if (dropFrame && seconds === 0 && frames < dropped && minutes % 10 !== 0)
      throw new Error(framesToLabel([hours, minutes, seconds, frames], true) + " does not exist in drop-frame timecode; that minute starts at " +
        framesToLabel([hours, minutes, 0, dropped], true) + ".");
    const totalMinutes = hours * 60 + minutes;
    let count = (totalMinutes * 60 + seconds) * nominal + frames;
    if (dropFrame) count -= dropped * (totalMinutes - Math.floor(totalMinutes / 10));
    return count;
  }
  const framesToLabel = (fields, dropFrame) => fields.slice(0, 3).map(value => String(value).padStart(2, "0")).join(":") +
    (dropFrame ? ";" : ":") + String(fields[3]).padStart(2, "0");
  const timecodeToFrames = (text, fps, dropFrame) => fieldsToFrames(timecodeFields(text), fps, dropFrame);
  // Return every effect and adjustment on a clip to its default, speed and direction included.
  // Callers wrap this in atomically(): if the speed cannot return to 100%, nothing changes.
  function resetClipNow(id) {
    Editor.editProject((next) => {
      const target = next.clips.find((c) => c.id === id);
      for (const key of ["pan", "opacity", "transform", "keyframes", "fadeIn", "fadeOut", "transitionBefore",
        "transitionDuration", "transitionMatchName"]) delete target[key];
      target.volume = 100;
      target.transition = "none";
      if (target.type === "video") target.color = {brightness: 100, contrast: 100, saturation: 100};
      else if (target.type === "title") target.color = {};
    });
    const current = project.clips.find((c) => c.id === id);
    if (current.reverse) Editor.updateClip(id, {reverse: false});
    if ((Number(current.speed) || 1) === 1 || current.nativeTimeRemap) return;
    try { Editor.setClipSpeed(id, 1); }
    catch (error) {
      throw new Error("Reset needs room to return the clip to 100% speed, so nothing was reset: " + error.message);
    }
  }
  // The project format keeps the sequence start timecode as `startTimecode`: a whole number of
  // frames at the project rate (01:00:00:00 at 24 fps is 86400; 01:00:00;00 drop-frame at 29.97
  // is 107892), and `dropFrame` (true/false, only for 29.97 and 59.94; absent there means drop).
  // Projects and drafts written by earlier builds may hold the start as text ("01:00:00:00");
  // they are converted here when opened, never refused: ";" in the label means drop-frame when
  // the project does not say.
  function normalizeTimecodeSettings(value, depth = 0) {
    if (!value || typeof value !== "object" || depth > 8) return value;
    const fps = Number(value.fps);
    if (typeof value.startTimecode === "string") {
      const label = value.startTimecode.trim();
      const drop = isDropFrameRate(fps) ? (typeof value.dropFrame === "boolean" ? value.dropFrame : label.includes(";")) : undefined;
      let frames = 0;
      try { frames = label ? timecodeToFrames(label, fps, drop === true) : 0; }
      catch (error) {
        frames = Number.isInteger(value.startFrame) && value.startFrame >= 0 ? value.startFrame : 0;
        // Open the project anyway, but say what was stored and what the sequence starts at now.
        const rate = Number.isFinite(fps) && fps > 0 ? fps : 30;
        const now = framesToTimecode(frames, rate, drop === true);
        const note = "The stored start timecode \"" + label.slice(0, 40) + "\" is not a valid timecode at " +
          Number(rate.toFixed(3)) + " fps (" + error.message.replace(/\.$/, "") + "). The sequence starts at " + now +
          "; change it in Sequence settings.";
        if (!Array.isArray(value.compatibilityWarnings)) value.compatibilityWarnings = [];
        value.compatibilityWarnings.push(note);
      }
      value.startTimecode = frames;
      if (drop !== undefined && value.dropFrame === undefined) value.dropFrame = drop;
    } else if (typeof value.startTimecode === "number" && Number.isFinite(value.startTimecode) && !Number.isInteger(value.startTimecode))
      value.startTimecode = Math.max(0, Math.round(value.startTimecode));
    if (Number.isFinite(fps) && fps > 0 && Number.isInteger(value.startTimecode) &&
        value.startTimecode >= 24 * 3600 * Math.ceil(fps)) value.startTimecode %= 24 * 3600 * Math.ceil(fps);
    if (value.startTimecode === null) delete value.startTimecode;
    for (const item of Array.isArray(value.media) ? value.media : [])
      if (item?.nestedProject) normalizeTimecodeSettings(item.nestedProject, depth + 1);
    return value;
  }
  const defaultStartTimecode = (fps, dropFrame) => timecodeToFrames("01:00:00:00", fps, dropFrame);
  const Editor = {
    formatTimecode(seconds, fps = 30, dropFrame = isDropFrameRate(fps)) {
      return framesToTimecode((seconds || 0) * fps, fps, dropFrame);
    },
    // Sequence timecode: the sequence start offset plus drop-frame labels when the rate uses them.
    sequenceTimecode(seconds, source = project) {
      return framesToTimecode(Math.round((seconds || 0) * source.fps) + (source.startTimecode || 0), source.fps, usesDropFrame(source));
    },
    // Ruler ticks between two sequence times, every `step` seconds of timecode: on whole
    // timecode seconds (labels ending in :00, or the first frame a drop-frame minute has), or
    // every few frames for steps under a second. Returns [{time, label}].
    rulerTicks(from, to, step) {
      const fps = project.fps, nominal = Math.round(fps), drop = usesDropFrame();
      const start = project.startTimecode || 0;
      const ticks = [];
      const add = (frame) => {
        if (frame < 0) return;
        const time = frame / fps;
        if (ticks.length && Math.abs(ticks.at(-1).time - time) < 1e-9) return;
        ticks.push({time, label: Editor.sequenceTimecode(time)});
      };
      const first = Math.max(0, Math.floor(from * fps)), last = Math.ceil(to * fps);
      if (step < 1) {
        // Frame steps: frames whose label's frame field is a multiple of the step, and in
        // drop-frame minutes the first real frame (;02 or ;04), whose labels before it are skipped.
        const every = Math.max(1, Math.round(step * nominal));
        for (let frame = first; frame <= last; frame++) {
          const count = labelCount(frame + start, fps, drop);
          const afterSkip = drop && frame + start > 0 && count - labelCount(frame + start - 1, fps, drop) > 1;
          if ((count % nominal) % every === 0 || afterSkip) add(frame);
        }
        return ticks;
      }
      const perTick = Math.round(step) * nominal;
      const fromCount = labelCount(first + start, fps, drop), toCount = labelCount(last + start, fps, drop);
      for (let count = Math.floor(fromCount / perTick) * perTick; count <= toCount + perTick; count += perTick)
        add(frameOfLabel(count, fps, drop) - start);
      return ticks;
    },
    // Typed positions. "+" or "-" moves relative to `base` (the playhead). An absolute entry with
    // fewer than four fields replaces the playhead's timecode from the right: at 01:00:05:12,
    // "1000" goes to 01:00:10:00 and "2." to 01:00:02:00. Labels that drop-frame timecode skips
    // are refused rather than moved.
    parseSequenceTimecode(text, base = time) {
      const value = String(text).trim();
      const relative = /^[+-]/.test(value) ? (value[0] === "-" ? -1 : 1) : 0;
      if (relative) {
        const [hours, minutes, seconds, frames] = timecodeFields(value.slice(1));
        const offset = ((hours * 60 + minutes) * 60 + seconds) * Math.round(project.fps) + frames;
        return Math.round(base * project.fps + relative * offset) / project.fps;
      }
      const typed = typedFields(value);
      const current = Editor.sequenceTimecode(base).split(/[:;]/).map(Number);
      const fields = [...current.slice(0, 4 - typed.length), ...typed];
      const frames = fieldsToFrames(fields, project.fps, usesDropFrame()) - (project.startTimecode || 0);
      if (frames < 0) throw new Error("That timecode is before the sequence start.");
      return frames / project.fps;
    },
    seekTimecode(text) { Editor.seek(Editor.parseSequenceTimecode(text)); },
    isDropFrameRate,
    setTimecodeSettings({startTimecode, dropFrame}) {
      const drop = isDropFrameRate(project.fps) ? dropFrame !== false : undefined;
      const start = timecodeToFrames(String(startTimecode), project.fps, drop === true);
      Editor.editProject(next => {
        next.startTimecode = start;
        if (drop === undefined) delete next.dropFrame;
        else next.dropFrame = drop;
      });
    },
    get: () => ({
      project,
      revision,
      // Counts projects opened or created, so views can reset their layout for a new project.
      opened,
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
      linkedSelection,
    }),
    drawFrame,
    framePlan,
    monitor,
    refreshMedia() { syncMedia(true);notify(); },
    editProject(fn) {
      const next = cloneState(project);
      fn(next);
      if (!linkingActive()) dropLoneLinks(next);
      shareUnchanged(project, next);
      validateProject(next);
      assertLockedTracksUnchanged(project, next);
      if (linkingActive()) assertLinksStayAligned(project, next);
      assertNoNewOverlaps(project, next);
      assertTransitionHandles(project, next);
      if (sameValue(project, next)) return;
      if (!gesture) remember(project);
      project = next;
      project.duration = duration();
      time = clamp(time, 0, project.duration);
      syncMedia(true);
      notify();
    },
    isTrackLocked(track, kind) { return trackLocked(kind || (track < 0 ? "audio" : "video"), track); },
    isTrackSyncLocked(track, kind) { return syncLocked(kind || (track < 0 ? "audio" : "video"), track); },
    isTrackHidden(track, kind) { return trackHidden(kind || (track < 0 ? "audio" : "video"), track); },
    // A title lane and the video lane of the same number are one Premiere video track, so they
    // share one state, stored under 'video:N' (a title lane without its own entry follows it).
    setTrackState(track, kind, patch) {
      const key=trackKey(kind==="title"?"video":kind, track);
      const current={locked:false,syncLocked:true,...trackState(kind, track)};
      const next={...current,...patch};
      if (next.locked===current.locked && next.syncLocked===current.syncLocked && !!next.hidden===!!current.hidden) return;
      if (typeof next.locked!=="boolean" || typeof next.syncLocked!=="boolean" ||
          (next.hidden!==undefined && typeof next.hidden!=="boolean")) throw new Error("Invalid track state.");
      if (next.hidden && kind==="audio") throw new Error("Mute audio tracks instead of hiding them.");
      if (!next.hidden) delete next.hidden;
      snapshot(); project.trackState ||= {}; project.trackState[key]=next;
      delete project.trackState[trackKey("title",track)];
      lastPaintedFrame=-1;
      syncMedia(true);
      notify();
    },
    updateTrackMix(track, patch) {
      const key=String(track);
      const next={ gain:100, pan:0, mute:false, solo:false, ...project.trackMix?.[key], ...patch };
      validateTrackMix(key,next);
      if (sameValue(next, project.trackMix?.[key])) return;
      snapshot();
      project.trackMix ||= {};
      project.trackMix[key] = next;
      syncMedia(); notify();
    },
    beginGesture() { if (gesture) return; gestureBefore=project; gesture=true; },
    endGesture() {
      if (!gesture) return;
      gesture=false;
      if (!sameValue(gestureBefore, project)) remember(gestureBefore);
      gestureBefore=null;notify();
    },
    get linkedSelection() { return linkedSelection; },
    setLinkedSelection(enabled) { linkedSelection=!!enabled; notify(); },
    // Run an edit with links ignored, e.g. an Alt-drag that trims one side of a linked pair.
    withoutLinks(action) {
      linkSuspended++;
      try { return action(); }
      finally { linkSuspended--; }
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
    // Source marks and the audio stream for new edits belong to the project and undo with it.
    // Media objects are shared between undo states, so the change goes on a copy, never in place.
    setSourceSettings(id, settings) {
      if (!project.media.some(entry=>entry.id===id)) return;
      Editor.editProject(next=>{
        const index=next.media.findIndex(entry=>entry.id===id);
        next.media[index]={...next.media[index],...structuredClone(settings)};
      });
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
      snapshot();
      Object.assign(project.media.find(m=>m.id===itemId),item);
      activeVideoId=null;syncMedia(true);notify();
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
      snapshot();
      for(const row of rows) Object.assign(project.media.find(m=>m.id===row.item.id),row.updated);
      activeVideoId=null;syncMedia(true);notify();
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
      const selection = sourceSelection(item);
      const track = placement?.track ?? (item.type === "audio" ? -1 : 1);
      const sound = item.type === "audio" ? track :
        hasSound(item) ? freeAudioTrack(project, start, start + selection.duration) : null;
      const clips = sourceClips(item, selection, start, track, sound);
      Editor.editProject(next=>next.clips.push(...clips));
      selectedId = clips[0].id;
      notify();
    },
    updateClip(id, patch) {
      const clip = project.clips.find((c) => c.id === id);
      if (!clip) return;
      if ('track' in patch && (!Number.isInteger(patch.track) || Math.abs(patch.track)>64 ||
          (clip.type==='audio' ? patch.track>=0 : patch.track<=0))) throw Error('Choose a valid timeline track.');
      ensureEditable(clip, "track" in patch ? Number(patch.track) : clip.track);
      if (Object.entries(patch).every(([key,value]) => sameValue(clip[key],value))) return;
      const linkedFields=["start","duration","in","speed","reverse"];
      // A trim through updateClip (a new duration) takes the partners at the edge it moves.
      const trimmed="duration" in patch && !sameValue(clip.duration,patch.duration);
      const edge=trimmed && "start" in patch && !sameValue(clip.start,patch.start) ? "start" : "end";
      const partners=!linkedFields.some(key=>key in patch) ? [] : trimmed ? partnersAtEdge(clip,edge) : linkedClips(clip);
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
      if(!Number.isFinite(speed) || speed<.1 || speed>16)throw Error('Choose a clip speed between 10% and 1600%.');
      const oldSpeed=Number(clip.speed)||1;
      if(Math.abs(speed-oldSpeed)<1e-6)return;
      const sourceLength=clip.duration*oldSpeed;
      // Nearest whole timeline frame, unless rounding up would read past the end of the source.
      const item=mediaOf(clip);
      const available=item?.still || !item?.duration ? Infinity : item.duration-clip.in;
      let frames=Math.round(sourceLength*project.fps/speed);
      if(frames*speed/project.fps>available+.01) frames=Math.floor(sourceLength*project.fps/speed+1e-6);
      if(frames<1)throw Error('This speed would make the clip shorter than a timeline frame.');
      Editor.updateClip(id,{speed,duration:frames/project.fps});
    },
    trimEdge(id, edge, atTime, { ripple = false } = {}) {
      const clip = project.clips.find(c => c.id === id);
      if (!clip) return;
      const partners = partnersAtEdge(clip, edge);
      for (const member of [clip, ...partners]) ensureEditable(member);
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
      const memberIds=new Set([clip.id,...partners.map(c=>c.id)]);
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
      const outgoing = [left, ...partnersAtEdge(left, "end")], incoming = [right, ...partnersAtEdge(right, "start")];
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
    // Slide: move a clip between its neighbours, which give and take the frames.
    slideClip(id, delta) {
      const clip = project.clips.find((c) => c.id === id);
      if (!clip) return;
      const frame = frameSeconds(), near = .5 / project.fps;
      const parts = [clip, ...partnersWithin(clip)].map((member) => {
        const lane = project.clips.filter((other) => other.id !== member.id && laneOf(other) === laneOf(member));
        const end = member.start + member.duration;
        return {member, lane,
          before: lane.find((other) => Math.abs(other.start + other.duration - member.start) < near),
          after: lane.find((other) => Math.abs(other.start - end) < near)};
      });
      let lower = -Infinity, upper = Infinity;
      for (const {member, lane, before, after} of parts) {
        for (const item of [member, before, after]) if (item) ensureEditable(item);
        const end = member.start + member.duration;
        if (before) {
          lower = Math.max(lower, frame - before.duration);
          upper = Math.min(upper, roomAfter(before));
        } else {
          lower = Math.max(lower, Math.max(0, ...lane.filter((other) => other.start < member.start)
            .map((other) => other.start + other.duration)) - member.start);
        }
        if (after) {
          upper = Math.min(upper, after.duration - frame);
          lower = Math.max(lower, -roomBefore(after));
        } else {
          upper = Math.min(upper, Math.min(Infinity, ...lane.filter((other) => other.start >= end - near)
            .map((other) => other.start)) - end);
        }
      }
      const shift = clamp(snap(delta), Math.ceil((lower - 1e-7) * project.fps) * frame, Math.floor((upper + 1e-7) * project.fps) * frame);
      if (!Number.isFinite(shift) || Math.abs(shift) < 1e-7) return;
      const before = project;
      Editor.editProject((next) => {
        const find = (item) => next.clips.find((other) => other.id === item.id);
        const original = (item) => before.clips.find((other) => other.id === item.id);
        for (const part of parts) {
          const member = find(part.member);
          member.start = snap(member.start + shift);
          if (part.before) {
            const left = find(part.before), speed = Number(left.speed) || 1;
            left.duration = snap(left.duration + shift);
            if (left.reverse) left.in = Math.max(0, left.in - shift * speed);
            resizeKeyframes(left, original(left), 0);
          }
          if (part.after) {
            const right = find(part.after), speed = Number(right.speed) || 1;
            right.start = snap(right.start + shift);
            right.duration = snap(right.duration - shift);
            if (!right.reverse) right.in = Math.max(0, right.in + shift * speed);
            resizeKeyframes(right, original(right), shift);
          }
        }
      });
    },
    // Edit points (clip starts and ends) in timeline order, for Up/Down navigation.
    editPoints() {
      const points = new Set([0]);
      for (const clip of project.clips) {
        points.add(Math.round(clip.start * project.fps));
        points.add(Math.round((clip.start + clip.duration) * project.fps));
      }
      return [...points].sort((a, b) => a - b).map((frame) => frame / project.fps);
    },
    // Timeline In/Out marks. Both marks include the frame they are set on, as in Premiere,
    // Avid and Resolve: In 50 / Out 80 is 31 frames. Stored representation (project.marks and
    // media.sourceMarks), in seconds on frame boundaries: `in` is the start of the first
    // included frame and `out` is the end of the last one, so marking Out on frame N stores
    // (N + 1) / fps and the range is [in, out). Projects saved before this change stored the
    // same [in, out) range, so they load unchanged.
    setMark(edge, at = time) {
      if (!["in", "out"].includes(edge)) throw new Error("Choose an In or Out mark.");
      const marks = {...project.marks};
      if (at === null) delete marks[edge];
      else {
        if (!Number.isFinite(Number(at))) throw new Error("Choose a timeline position.");
        let frame = Math.max(0, Math.round(Number(at) * project.fps));
        // The playhead parked at the sequence end sits after the last frame: Out marks that last
        // frame. (Marks set further out, past the end, are kept as set.)
        const endFrame = Math.ceil(duration() * project.fps - 1e-6);
        if (edge === "out" && endFrame > 0 && frame === endFrame) frame = endFrame - 1;
        marks[edge] = (edge === "out" ? frame + 1 : frame) / project.fps;
        // A new mark that crosses the other one replaces it, as in Premiere.
        if (Number.isFinite(marks.in) && Number.isFinite(marks.out) && marks.out <= marks.in) delete marks[edge === "in" ? "out" : "in"];
      }
      if (sameValue(marks, project.marks || {})) return;
      snapshot();
      project.marks = marks;
      notify();
    },
    clearMarks() {
      if (!project.marks || !Object.keys(project.marks).length) return;
      snapshot();
      delete project.marks;
      notify();
    },
    markClip(id = selectedId) {
      const clip = project.clips.find((c) => c.id === id) || at("video", time);
      if (!clip) throw new Error("Select a clip to mark its range.");
      snapshot();
      project.marks = {in: clip.start, out: snap(clip.start + clip.duration)};
      notify();
    },
    // The frame an Out mark is on: the last frame the range includes.
    markOutFrame(out) {
      return Math.max(0, Math.round(out * project.fps) - 1) / project.fps;
    },
    // Three- and four-point editing from a source into the timeline.
    // Ranges come from the source In/Out and the timeline In/Out; with both timeline marks
    // the timeline decides the length (back-timed from a lone source Out, or fit to fill
    // when all four marks disagree); with a lone timeline Out the edit is back-timed.
    // After the edit the playhead moves to the end of the placed clip, so mark-and-insert cycles
    // build a scene in order. An edit timed by the timeline In and/or Out clears both marks, as
    // Premiere does (undo brings them back). `at` (a drag onto the timeline) places the source
    // at that time instead and leaves the timeline marks alone.
    // `track` is the picture's video track (null places a camera clip's sound only); `audioTrack`
    // is where its sound goes (null keeps the sound inside the picture clip).
    placeSource(id, {mode = "overwrite", track, audioTrack = null, marks, at = null} = {}) {
      const item = project.media.find((entry) => entry.id === id);
      if (!item) throw new Error("Media is unavailable.");
      if (!["insert", "overwrite", "append"].includes(mode)) throw new Error("Choose insert, overwrite or append.");
      const pictureTrack = item.type === "video" && track != null ? Number(track) : null;
      let soundTrack = null;
      if (item.type === "audio") soundTrack = Number(track);
      else if (hasSound(item) && audioTrack != null) soundTrack = Number(audioTrack);
      if (pictureTrack === null && soundTrack === null) {
        if (item.type === "video" && !hasSound(item)) throw new Error(item.name + " has no sound to place on an audio track.");
        throw new Error("Choose a timeline track.");
      }
      const lanes = new Set();
      if (pictureTrack !== null) lanes.add("video:" + pictureTrack);
      if (soundTrack !== null) lanes.add("audio:" + soundTrack);
      for (const lane of lanes) {
        const {kind, track: number} = startOfLane(lane);
        if (!Number.isInteger(number) || !number || Math.abs(number) > 64 || (kind === "audio") !== number < 0)
          throw new Error("Choose a valid timeline track.");
        if (trackLocked(kind, number)) throw new Error(`Unlock ${kind} track ${Math.abs(number)} before editing.`);
      }
      const frame = frameSeconds();
      const sourceMarks = marks ?? item.sourceMarks ?? {};
      const hasIn = Number.isFinite(sourceMarks.in), hasOut = Number.isFinite(sourceMarks.out);
      const range = sourceSelection(item, sourceMarks, item.sourceAudioStream);
      const timeline = project.marks || {};
      const timelineIn = Number.isFinite(timeline.in) ? timeline.in : null;
      const timelineOut = Number.isFinite(timeline.out) ? timeline.out : null;
      let start, length = range.duration, sourceIn = range.in, speed = 1, method = "playhead";
      if (at !== null && at !== undefined) {
        if (!Number.isFinite(Number(at))) throw new Error("Choose a timeline position.");
        start = Math.max(0, Number(at));
        method = "drag";
      } else if (mode === "append") {
        start = Math.max(0, ...project.clips.filter((clip) => lanes.has(laneOf(clip))).map((clip) => clip.start + clip.duration));
        method = "append";
      } else if (timelineIn !== null && timelineOut !== null) {
        start = timelineIn;
        length = Math.round((timelineOut - timelineIn) * project.fps) / project.fps;
        // Four marks whose lengths differ by a frame or more fit to fill (lengths are whole frames).
        if (hasIn && hasOut && Math.round(range.duration * project.fps) !== Math.round(length * project.fps)) {
          speed = range.duration / length;
          if (speed < .1 || speed > 16) throw new Error("Fitting this source range to the timeline range needs a speed outside 10% to 1600%.");
          method = "fit to fill";
        } else if (!hasIn && hasOut) {
          sourceIn = sourceMarks.out - length;
          method = "back-timed from source Out";
        } else method = "timeline In/Out";
      } else if (timelineIn !== null) {
        start = timelineIn;
        method = "timeline In";
      } else if (timelineOut !== null) {
        start = timelineOut - length;
        method = "back-timed from timeline Out";
      } else start = time;
      start = snap(start);
      if (start < 0) throw new Error("The edit would start before the beginning of the sequence.");
      if (sourceIn < -1e-6) throw new Error("The source has too little media before its Out mark for this range.");
      if (!item.still && item.duration && sourceIn + length * speed > item.duration + .01)
        throw new Error("The source has too little media after its In mark to fill the timeline range.");
      const selection = {...range, in: Math.max(0, sourceIn), duration: length};
      if (speed !== 1) selection.speed = speed;
      const placed = [];
      const usedTimelineMarks = method !== "drag" && mode !== "append" && (timelineIn !== null || timelineOut !== null);
      Editor.editProject((next) => {
        if (mode === "insert") insertSpace(next, lanes, start, length);
        else clearRange(next, lanes, start, start + length).settle();
        const clips = sourceClips(item, selection, start, pictureTrack, soundTrack);
        next.clips.push(...clips);
        placed.push(...clips);
        if (usedTimelineMarks) delete next.marks;
      });
      selectedId = placed[0]?.id ?? selectedId;
      time = clamp(snap(start + length), 0, duration());
      syncMedia(true);
      notify();
      return {start, duration: length, speed, method, clips: placed.map((clip) => clip.id)};
    },
    // Lift leaves a gap; extract closes it. Both act on every unlocked track.
    liftRange({extract = false} = {}) {
      const {in: from, out: to} = project.marks || {};
      if (!Number.isFinite(from) || !Number.isFinite(to)) throw new Error("Mark a timeline In and Out first (I and O).");
      const lanes = new Set(project.clips.filter((clip) => !trackLocked(clip.type, clip.track)).map(laneOf));
      Editor.editProject((next) => {
        const links = clearRange(next, lanes, from, to, {lift: true});
        if (extract) {
          for (const clip of next.clips)
            if (lanes.has(laneOf(clip)) && clip.start >= to - 1e-6) clip.start = snap(clip.start - (to - from));
          delete next.marks;
        }
        links.settle();
      });
      if (extract) Editor.seek(from);
    },
    // Add an edit at the playhead on every unlocked track.
    addEditAll() {
      const cut = snap(time);
      const ids = new Set(project.clips.filter((clip) => !trackLocked(clip.type, clip.track) &&
        clip.start < cut - 1e-6 && clip.start + clip.duration > cut + 1e-6).map((clip) => clip.id));
      if (!ids.size) return;
      Editor.withoutLinks(() => Editor.editProject((next) => splitClips(next, ids, cut)));
    },
    // Ctrl/Cmd+L. Unlinks linked clips, separates a camera clip's embedded sound,
    // or links the selected picture and sound clips.
    toggleLink(ids) {
      const chosen = project.clips.filter((clip) => ids.includes(clip.id));
      if (!chosen.length) return null;
      const groups = new Set(chosen.map((clip) => clip.linkGroup).filter(Boolean));
      if (groups.size) {
        for (const clip of project.clips) if (groups.has(clip.linkGroup)) ensureEditable(clip);
        Editor.editProject((next) => {
          for (const clip of next.clips) if (groups.has(clip.linkGroup)) delete clip.linkGroup;
        });
        return "unlinked";
      }
      if (chosen.length === 1) {
        const [clip] = chosen;
        if (!hasEmbeddedAudio(clip)) throw new Error("Select a picture clip and a sound clip to link them.");
        ensureEditable(clip);
        Editor.editProject((next) => {
          const picture = next.clips.find((item) => item.id === clip.id);
          const sound = detachAudio(next, picture);
          delete picture.linkGroup;
          delete sound.linkGroup;
        });
        return "separated";
      }
      if (chosen.some((clip) => clip.type === "title")) throw new Error("Titles cannot be linked.");
      for (const clip of chosen) ensureEditable(clip);
      const group = uid();
      Editor.editProject((next) => {
        for (const clip of next.clips) if (ids.includes(clip.id)) clip.linkGroup = group;
      });
      return "linked";
    },
    // Enable or disable clips (Premiere's Clip > Enable, Shift+E). The command acts on the whole
    // selection as one, as Premiere does: when the lead clip (the one right-clicked, otherwise the
    // primary selection; by default the first id) is enabled, every chosen clip becomes disabled,
    // otherwise every chosen clip becomes enabled. With linked selection on, linked partners are
    // chosen too. One undo step; nothing changes when a clip that would change is on a locked
    // track. A disabled clip stores `enabled: false`; enabling removes the key, so older projects
    // and re-enabled clips look the same.
    toggleEnabled(ids, lead = ids?.[0]) {
      const wanted = new Set(ids);
      const chosen = project.clips.filter((clip) => wanted.has(clip.id));
      for (const clip of [...chosen]) for (const partner of partnersWithin(clip)) if (!wanted.has(partner.id)) {
        wanted.add(partner.id);
        chosen.push(partner);
      }
      if (!chosen.length) return null;
      const disable = isEnabled(chosen.find((clip) => clip.id === lead) || chosen[0]);
      const changing = chosen.filter((clip) => isEnabled(clip) === disable || (!disable && "enabled" in clip));
      for (const clip of changing) ensureEditable(clip);
      if (changing.length) Editor.editProject((next) => {
        for (const clip of next.clips) {
          if (!wanted.has(clip.id)) continue;
          if (disable) clip.enabled = false;
          else delete clip.enabled;
        }
      });
      const count = chosen.filter((clip) => isEnabled(clip) === disable).length;
      return disable ? {disabled: count, enabled: 0} : {disabled: 0, enabled: count};
    },
    isClipEnabled: (clip) => isEnabled(clip),
    // The gain each sound clip heard at the playhead gets (what playback sets on its gain node).
    audioGains() {
      indexTransitions();
      return Object.fromEntries(soundingAudioClips().map((clip) => [clip.id, audibility(clip)]));
    },
    // Give an embedded camera clip a linked audio clip (used before trimming one side).
    detachAudio(id) {
      const clip = project.clips.find((c) => c.id === id);
      if (!clip || !hasEmbeddedAudio(clip)) return null;
      ensureEditable(clip);
      let sound;
      Editor.editProject((next) => { sound = detachAudio(next, next.clips.find((item) => item.id === id)); });
      return sound.id;
    },
    hasEmbeddedAudio: (clip) => hasEmbeddedAudio(clip),
    syncOffset: (clip) => syncOffset(clip)?.frames ?? null,
    // Put a drifted linked clip back in sync by moving it or by slipping its source.
    bringIntoSync(id, how = "move") {
      const clip = project.clips.find((c) => c.id === id);
      const offset = clip && syncOffset(clip);
      if (!offset?.frames) return;
      const shift = offset.frames / project.fps;
      // All or nothing: a slip that runs out of source is undone rather than left halfway.
      atomically(() => {
        Editor.withoutLinks(() => {
          if (how === "slip") Editor.slipClip(id, shift * (Number(clip.speed) || 1));
          else Editor.updateClip(id, {start: clip.start - shift});
        });
        if (syncOffset(project.clips.find((c) => c.id === id))?.frames)
          throw new Error(how === "slip" ? "Not enough source media to slip this clip into sync." : "Another clip is in the way of moving this clip into sync.");
      });
    },
    // Return every effect and adjustment on a clip to its default.
    resetClip(id) {
      const clip = project.clips.find((c) => c.id === id);
      if (!clip) return;
      ensureEditable(clip);
      atomically(() => resetClipNow(id));
    },
    atomically,
    splitClipAt,
    split() {
      const clip = selected() || at("video", time);
      const cut = snap(time);
      if (!clip || cut <= clip.start || cut >= clip.start + clip.duration)
        return;
      ensureLinkedEditable(clip);
      const ids=new Set([clip,...linkedClips(clip)].map(member=>member.id));
      Editor.withoutLinks(()=>Editor.editProject(next=>splitClips(next,ids,cut)));
      time = cut;
    },
    deleteClips(ids, {ripple=false} = {}) {
      const selected=new Set(ids);
      const members=project.clips.filter(clip=>selected.has(clip.id));
      for(const clip of [...members])for(const linked of partnersWithin(clip))selected.add(linked.id);
      const removing=project.clips.filter(clip=>selected.has(clip.id));
      if(!removing.length)return;
      for(const clip of removing)ensureEditable(clip);
      // Ripple closes every stretch of time the removed clips covered: a later clip on a
      // sync-locked track moves left by the length of the stretches that end before it.
      const spans=[];
      for(const clip of [...removing].sort((a,b)=>a.start-b.start)){
        const last=spans.at(-1), end=clip.start+clip.duration;
        if(last && clip.start<=last[1]+.001) last[1]=Math.max(last[1],end);
        else spans.push([clip.start,end]);
      }
      const moves=clip=>!trackLocked(clip.type,clip.track)&&syncLocked(clip.type,clip.track);
      Editor.editProject(next=>{
        next.clips=next.clips.filter(clip=>!selected.has(clip.id));
        if(ripple){
          // A linked clip spanning a removed stretch (the sound under a picture piece or under a
          // clip cut into a linked shot) whose group has members after it that move left loses
          // the part under the stretch, so the whole group stays in sync.
          const settles=[];
          if(linkingActive())for(const [begin,end] of spans){
            const spanning=next.clips.filter(clip=>clip.linkGroup&&moves(clip)&&clip.start<begin-1e-6&&clip.start+clip.duration>end+1e-6&&
              next.clips.some(other=>other.linkGroup===clip.linkGroup&&other.id!==clip.id&&moves(other)&&other.start>=end-.001));
            if(spanning.length)settles.push(clearRange(next,new Set(spanning.map(laneOf)),begin,end));
          }
          for(const clip of next.clips){
            if(!moves(clip))continue;
            const shift=spans.filter(([,end])=>end<=clip.start+.001).reduce((sum,[begin,end])=>sum+end-begin,0);
            if(shift)clip.start=Math.max(0,snap(clip.start-shift));
          }
          for(const links of settles)links.settle();
        }
        // Deleting pieces of a shot can leave a group with one member: that is no longer a link.
        dropLoneLinks(next);
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
        start: snap(time),
        // Four seconds, rounded to whole frames so the title stays on the frame grid at 23.976 and 29.97.
        duration: Math.round(4 * project.fps) / project.fps,
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
      future.push(project);
      project = history.pop();
      revision += 1;
      time=clamp(time,0,duration());
      if (!project.clips.some(c=>c.id===selectedId)) selectedId=project.clips[0]?.id||null;
      syncMedia(true);
      notify();
    },
    redo() {
      if (!future.length) return;
      history.push(project);
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
      history=[];future=[];gesture=false;gestureBefore=null;revision++;opened++;
      project = {
        name,
        width,
        height,
        fps,
        // New sequences start at 01:00:00:00 like broadcast masters; older projects keep 00:00:00:00.
        startTimecode: defaultStartTimecode(fps, isDropFrameRate(fps)),
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
      normalizeTimecodeSettings(imported);
      validateProject(imported);
      mergeTitleTrackStates(imported);
      if (window.SpliceMedia) await window.SpliceMedia.restore(imported);
      history=[];future=[];gesture=false;gestureBefore=null;revision++;opened++;
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
