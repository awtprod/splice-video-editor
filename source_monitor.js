(() => {
  // Source marks: either may be unset, which three-point editing treats differently. Both
  // include the frame they are set on; `out` is stored as the end of that frame (see Editor.setMark).
  let dialog, media, player, marks = {}, fps = 25, shuttleTimer, shuttleRate = 0;
  let unsubscribe = null, committedMarks = '{}', frameRequest = 0;
  let isSubclip = false, sourceOffset = 0;
  let angleWindow = null, gapTime = null, gapSince = 0;
  const markIn = () => Number.isFinite(marks.in) ? marks.in : 0;
  const markOut = () => Number.isFinite(marks.out) ? marks.out : media.duration;
  // The last frame the range includes, for display.
  const lastFrame = () => Math.max(markIn(), Math.round(markOut() * fps - 1) / fps);
  const esc = value => String(value ?? '').replace(/[<>&"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const snap = value => Math.round(value * fps) / fps;
  const format = seconds => Editor.formatTimecode(seconds,fps);
  const show = (message,error=true) => {const node=dialog.querySelector('.source-status');node.textContent=message;node.classList.toggle('error',!!message&&error);};
  const current = () => Math.max(0,Math.min(media.duration,gapTime ?? ((player?.currentTime || 0) - sourceOffset)));
  const seekLocal = value => {
    if (!player || !media) return;
    const local = Math.max(0, Math.min(media.duration, value));
    if (angleWindow) {
      const unavailable = local < angleWindow.minTime || local >= angleWindow.maxTime;
      player.style.visibility = unavailable ? 'hidden' : '';
      gapTime = unavailable ? local : null;
      gapSince = performance.now();
      if (unavailable) { player.pause(); return; }
    }
    player.currentTime = sourceOffset + local;
  };
  function update() {
    if (!dialog?.open) return;
    if (angleWindow && player.readyState >= 1) {
      if (gapTime !== null && shuttleRate > 0) {
        seekLocal(gapTime + (performance.now() - gapSince) * shuttleRate / 1000);
        if (gapTime === null) player.play().catch(error=>show(error.message));
      } else if (gapTime === null && current() >= angleWindow.maxTime) {
        seekLocal(current());
      }
      if (current() >= media.duration && shuttleRate > 0) stopShuttle();
    }
    if (isSubclip && player && player.readyState >= 1) {
      if (player.currentTime < sourceOffset - 1e-3) player.currentTime = sourceOffset;
      else if (player.currentTime >= sourceOffset + media.duration - 1e-3 && !player.paused) {
        stopShuttle();
        player.currentTime = sourceOffset + media.duration;
      }
    }
    dialog.querySelector('.source-time').textContent = format(current())+' / '+format(media.duration);
    dialog.querySelector('.source-range').textContent = 'In '+format(markIn())+' · Out '+format(lastFrame())+' · '+format(markOut()-markIn());
    const planText = targetsText() + ' · ' + plan();
    const planNode = dialog.querySelector('.source-plan');
    if (planNode.textContent !== planText) {
      planNode.textContent = planText;
      planNode.title = planText;
    }
    dialog.querySelector('.source-scrub').value = current();
    dialog.querySelector('.source-scrub').max = media.duration;
    frameRequest = requestAnimationFrame(update);
  }
  // Track targeting, as in other editors' source patching. The picture goes to the chosen video
  // track (or the track it is dropped on); its sound follows to the audio track with the same
  // number (V2 → A2) unless another audio track, or "in the picture clip", is chosen. A drop
  // on an audio track places the sound only. Returns {picture, sound}: track numbers or null.
  function targets(drop = null) {
    const soundPicker = dialog.querySelector('.source-audio-track');
    if (media.type === 'audio') {
      const track = drop ? Number(drop.track) : Number(dialog.querySelector('.source-track').value);
      return {picture: null, sound: track};
    }
    if (drop && drop.kind === 'audio') return {picture: null, sound: hasSound() ? Number(drop.track) : null, soundOnly: true};
    const chosen = dialog.querySelector('.source-track').value;
    const picture = drop ? Number(drop.track) : chosen === 'none' ? null : Number(chosen);
    if (!soundPicker) return {picture, sound: null};
    const choice = soundPicker.value;
    if (choice === '') return {picture, sound: null, embedded: true};
    if (choice === 'follow') return {picture, sound: -Math.max(1, picture ?? 1)};
    return {picture, sound: Number(choice)};
  }
  const hasSound = () => media.type === 'video' && !media.still && media.audioChannels > 0;
  const lockedTrack = (kind, track) => track !== null && Editor.isTrackLocked(track, kind);
  function targetsText(drop = null) {
    const {picture, sound, embedded, soundOnly} = targets(drop);
    const locked = (kind, track) => lockedTrack(kind, track) ? ' (locked)' :
      kind === 'video' && track !== null && Editor.isTrackHidden(track, 'video') ? ' (hidden: the picture will not show)' : '';
    if (soundOnly && sound === null) return 'No sound in this clip to place on an audio track';
    const parts = [];
    if (picture !== null) parts.push('Picture → V' + picture + locked('video', picture));
    if (sound !== null) parts.push((picture === null ? 'Sound only → A' : 'sound → A') + Math.abs(sound) + locked('audio', sound));
    else if (embedded && picture !== null) parts.push('sound in the picture clip');
    else if (picture !== null && media.type === 'video' && !media.still) parts.push('no sound');
    return parts.join(', ') || 'Choose a track';
  }
  // Describe how the next edit will be timed: the three-point rule that applies.
  function plan() {
    const timeline=Editor.get().project.marks||{};
    const hasIn=Number.isFinite(timeline.in), hasOut=Number.isFinite(timeline.out);
    const timecode=Editor.sequenceTimecode;
    if(hasIn&&hasOut){
      const fourPoint=Number.isFinite(marks.in)&&Number.isFinite(marks.out);
      return 'Fills timeline In '+timecode(timeline.in)+' to Out '+timecode(Editor.markOutFrame(timeline.out))+
        (fourPoint?' (four-point: speed changes if lengths differ)':!Number.isFinite(marks.in)&&Number.isFinite(marks.out)?' back-timed from the source Out':'');
    }
    if(hasIn)return 'Starts at timeline In '+timecode(timeline.in);
    if(hasOut)return 'Ends at timeline Out '+timecode(Editor.markOutFrame(timeline.out))+' (back-timed)';
    return 'Starts at the playhead '+timecode(Editor.get().time);
  }
  function close() {
    if (!dialog) return;
    stopShuttle();
    cancelAnimationFrame(frameRequest);
    unsubscribe?.();unsubscribe=null;
    player.pause();player.removeAttribute('src');player.load();
    document.removeEventListener('keydown',onKey,true);
    dialog.close();dialog.remove();dialog=null;player=null;media=null;
    isSubclip = false; sourceOffset = 0; angleWindow = null; gapTime = null;
    document.querySelector('.workspace')?.classList.remove('source-open');
  }
  // Follow the project: undo can change the saved marks, and a new project can drop the source.
  function follow(state) {
    if (!dialog || !media) return;
    const item=state.project.media.find(entry=>entry.id===media.id);
    if (!item) { close(); return; }
    media.subclipLimits = item.subclipLimits;
    const limBtn = dialog.querySelector('[data-action="toggle-subclip-limits"]');
    if (limBtn) limBtn.textContent = item.subclipLimits === false ? 'Restore subclip limits' : 'Remove subclip limits';
    const saved=JSON.stringify(item.sourceMarks||{});
    if (saved===committedMarks) return;
    committedMarks=saved;
    marks={...(item.sourceMarks||{})};
  }
  function stopShuttle() {
    if(shuttleTimer)clearInterval(shuttleTimer);
    shuttleTimer=null;shuttleRate=0;
    if(player)player.pause();
  }
  function shuttle(direction) {
    const rate=Math.sign(shuttleRate)===direction?Math.min(4,Math.abs(shuttleRate)*2):1;
    stopShuttle();shuttleRate=direction*rate;
    if(direction>0){
      if((isSubclip || angleWindow) && current()>=media.duration-1e-3) seekLocal(0);
      gapSince=performance.now();
      player.playbackRate=rate;
      if(gapTime !== null)return;
      player.play().catch(error=>show(error.message));return;
    }
    shuttleTimer=setInterval(()=>{
      if(!dialog?.open || current()<=0){stopShuttle();return;}
      if(player.seeking)return;
      seekLocal(Math.max(0,current()-rate/fps));
    },1000/fps);
  }
  function mark(edge) {
    // The frame under the source playhead; an Out mark includes it, so it ends one frame later.
    const frame=Math.round(current()*fps);
    const lastStart=Math.max(0,Math.ceil(media.duration*fps-1e-6)-1);
    if (edge==='in') {
      marks.in=Math.min(frame,lastStart)/fps;
      if(Number.isFinite(marks.out) && marks.out<marks.in+1/fps-1e-9) delete marks.out;
    } else {
      marks.out=Math.min(Math.min(frame,lastStart)+1,Math.max(1,media.duration*fps))/fps;
      if(Number.isFinite(marks.in) && marks.in>marks.out-1/fps+1e-9) delete marks.in;
    }
    committedMarks=JSON.stringify(marks);
    Editor.setSourceSettings(media.id,{sourceMarks:{...marks}});
  }
  function makeSubclip() {
    if (!dialog?.open || !media) return;
    if (media.multicam) { show('Subclips cannot be created from a multicam clip.'); return; }
    window.SpliceBins?.openSubclipDialog(media.id, { in: markIn(), out: markOut() }, msg => show(msg, false));
  }
  // `drop` places the marked range where it was dragged on the timeline: {kind, track, at}.
  function place(mode, drop=null) {
    if (!dialog || dialog.dataset.preparing) return;
    show('');
    const {picture, sound, soundOnly} = targets(drop);
    if (soundOnly && sound === null) return show(media.name + ' has no sound to place on an audio track.');
    // Never edit a locked track: refuse, naming it, rather than leave part of the edit behind.
    if (lockedTrack('video', picture)) return show(`Unlock video track ${picture} before editing.`);
    if (lockedTrack('audio', sound))
      return show(`Unlock audio track ${Math.abs(sound)} before editing, or send the sound to another audio track.`);
    const options = {mode, marks: {...marks}};
    if (media.type === 'audio') options.track = sound;
    else {
      options.track = picture;
      options.audioTrack = sound;
    }
    if (drop) options.at = drop.at;
    let result;
    try { result=Editor.placeSource(media.id,options); }
    catch (error) { return show(error.message); }
    if (result.speed!==1) Editor.select(result.clips[0]);
    // The monitor stays open: three-point editing is mark, place, mark, place.
    const verb={insert:'Inserted',overwrite:'Overwrote',append:'Appended'}[mode];
    show(verb+' '+format(result.duration)+' at '+Editor.sequenceTimecode(result.start)+' · '+result.method+
      (result.speed!==1?' at '+Math.round(result.speed*100)+'% speed':''),false);
  }
  // The monitor is non-modal: its keys apply only while focus is inside it.
  function onKey(event) {
    if (!dialog?.open || !dialog.contains(document.activeElement)) return;
    const key=event.key.toLowerCase();
    if((event.ctrlKey||event.metaKey) && !event.shiftKey && !event.altKey && key==='u'){
      event.preventDefault();event.stopPropagation();makeSubclip();return;
    }
    if (window.SpliceKeys?.keyBelongsToControl(document.activeElement,event)) return;
    if(key==='escape'){event.preventDefault();close();}
    if(key==='i'||key==='o'){event.preventDefault();mark(key==='i'?'in':'out');}
    if(key===' '){event.preventDefault();shuttleRate?stopShuttle():shuttle(1);}
    if(key==='j'){event.preventDefault();shuttle(-1);}
    if(key==='k'){event.preventDefault();stopShuttle();}
    if(key==='l'){event.preventDefault();shuttle(1);}
    if(key==='arrowleft'){event.preventDefault();stopShuttle();seekLocal(Math.max(0,current()-1/fps));}
    if(key==='arrowright'){event.preventDefault();stopShuttle();seekLocal(Math.min(media.duration,current()+1/fps));}
    if(key==='enter'||key===','){event.preventDefault();place('insert');}
    if(key==='.'){event.preventDefault();place('overwrite');}
  }
  function open(id) {
    close();
    const state=Editor.get().project;
    const item=state.media.find(entry=>entry.id===id);
    if(!item){window.alert('Media is unavailable.');return;}
    const resolved = Editor.resolveClipMedia ? Editor.resolveClipMedia({ mediaId: item.id, type: item.type }, state) : { item, inShift: 0 };
    const playSrc = (item.multicam ? resolved.item?.src : item.src || resolved.item?.src) || '';
    if(!playSrc || item.offline){window.alert('Relink '+item.name+' before opening it.');return;}
    isSubclip = !!item.parentMediaId;
    angleWindow = resolved.angle ? resolved : null;
    sourceOffset = (resolved.inShift || 0) - (angleWindow?.minTime || 0);
    media={...item,src:playSrc,duration:Number.isFinite(Number(item.duration)) && Number(item.duration)>0 ? Number(item.duration) : 0};
    fps=Number.isFinite(media.fps) && media.fps>0 ? media.fps : state.fps;
    const trackCount=Math.min(64,Math.max(state.trackCounts?.[media.type] || 3,
      ...state.clips.filter(clip=>clip.type===media.type).map(clip=>Math.abs(clip.track)),2)+1);
    const nativeTrackLimit=media.type==='video' && !state.premiereOriginal ? 64 : (state.trackCounts?.[media.type] || 3);
    const saved=item.sourceMarks||{};
    marks={};
    if(Number.isFinite(saved.out)) marks.out=Math.max(0,Math.min(media.duration,saved.out));
    if(Number.isFinite(saved.in)) marks.in=Math.max(0,Math.min(markOut(),saved.in));
    if(markOut()<=markIn()) marks={};
    // Sound can go to any audio track up to one past the last used, and to the audio track
    // matching any picture track offered (it is created when the edit uses it).
    const soundTracks=Math.min(64,Math.max(state.trackCounts?.audio || 3,trackCount,
      ...state.clips.filter(clip=>clip.type==='audio').map(clip=>Math.abs(clip.track)),2)+1);
    const soundOptions=Array.from({length:soundTracks},(_,i)=>`<option value="${-i-1}">Audio ${i+1}</option>`).join('');
    const soundTarget=hasSound() ?
      `<label class="source-target">Sound <select class="source-audio-track" aria-label="Sound goes to"><option value="follow" selected>With picture</option>${soundOptions}<option value="">In picture clip</option></select></label>` : '';
    dialog=document.createElement('dialog');dialog.className='source-dialog';
    dialog.setAttribute('aria-label','Source monitor: '+media.name);
    const tag=media.type==='audio'?'audio':'video';
    const trackOptions=Array.from({length:trackCount},(_,i)=>media.type==='video'?i+1:-i-1)
      .map(n=>`<option value="${n}">${media.type==='video'?'Video':'Audio'} ${Math.abs(n)}${Math.abs(n)>nativeTrackLimit?' · working project only':''}</option>`).join('')+
      (hasSound()?'<option value="none" title="No picture: the sound only">None</option>':'');
    const pictureTarget=`<label class="source-target">${media.type==='video'?'Picture':'Sound'} <select class="source-track" aria-label="${media.type==='video'?'Picture goes to':'Sound goes to'}">${trackOptions}</select></label>`;
    const subclipBar = !media.multicam ? (
      `<div class="source-subclip-row">` +
      `<button type="button" class="source-subclip-btn" data-action="make-subclip" title="Create reusable subclip from In/Out marks (Ctrl+U / Cmd+U)">Make Subclip…</button>` +
      (isSubclip ? `<span class="source-subclip-badge">Subclip · ${format(sourceOffset)}–${format(sourceOffset + media.duration)}${media.timecode ? ' · TC ' + esc(media.timecode) : ''}</span>` +
        `<button type="button" class="bin-mini-btn" data-action="toggle-subclip-limits">${media.subclipLimits === false ? 'Restore subclip limits' : 'Remove subclip limits'}</button>` : '') +
      `</div>`
    ) : '';
    // Head, picture, settings (they scroll when the window is short) and a footer whose transport,
    // mark and edit buttons are always in view.
    dialog.innerHTML=`<div class="source-head"><div><small>SOURCE</small><strong>${esc(media.name)}</strong></div><button class="source-close" aria-label="Close source monitor">×</button></div>`+
      `<div class="source-stage"><${tag} ${angleWindow ? '' : 'controls'} playsinline preload="auto"></${tag}></div>`+
      `<div class="source-controls"><input class="source-scrub" type="range" min="0" max="${media.duration}" step="${1/fps}" value="0" aria-label="Source playhead">`+
      `<div class="source-readout"><span class="source-time"></span><span class="source-range"></span></div>`+
      `<small class="source-shortcuts">I · O mark (both frames included) · J K L shuttle · , insert · . overwrite · Ctrl+U subclip</small>`+
      subclipBar +
      `<label class="source-bin">Bin path <input type="text" value="${esc(media.bin || '')}" placeholder="e.g. Interview / Camera A" aria-label="Media bin path"></label></div>`+
      `<div class="source-footer"><p class="source-plan"></p><p class="source-status" role="status"></p><div class="source-bar">`+
      `<div class="source-targets" role="group" aria-label="Edit targets">${pictureTarget}${soundTarget}</div>`+
      `<div class="source-buttons"><button data-action="back" title="Back one frame (←)">−1</button><button data-action="play" title="Play / pause (Space)">Play</button><button data-action="forward" title="Forward one frame (→)">+1</button><button data-action="in" title="Mark In on this frame (I)">Mark In</button><button data-action="out" title="Mark Out on this frame, included (O)">Mark Out</button></div>`+
      `<div class="source-actions"><button data-action="append">Append</button><button data-action="overwrite" title="Overwrite (.)">Overwrite</button><button class="source-primary" data-action="insert" title="Insert (,)">Insert</button></div></div></div>`;
    window.SpliceBins?.mountSourceMetadata?.(dialog.querySelector('.source-controls'), media.id);
    // A panel of the workspace, left of the program monitor (the inspector makes room).
    const workspace=document.querySelector('.workspace'), viewer=workspace?.querySelector('.viewer');
    if (viewer) { viewer.before(dialog); workspace.classList.add('source-open'); }
    else document.body.append(dialog);
    // Drag the picture (or the sound's stage) onto a timeline track: overwrite there, Ctrl or ⌘ inserts.
    const stage=dialog.querySelector('.source-stage');
    if (angleWindow) stage.style.background = 'black';
    stage.draggable=true;stage.title='Drag onto a timeline track to overwrite there (hold Ctrl or ⌘ to insert)';
    stage.addEventListener('dragstart',event=>{
      if(!media){event.preventDefault();return;}
      event.dataTransfer.setData('application/x-splice-source',media.id);
      event.dataTransfer.effectAllowed='copy';
    });
    player=dialog.querySelector('video,audio');player.src=media.src;player.onerror=()=>show('The source file could not be decoded.');player.onended=()=>{
      if (angleWindow && angleWindow.maxTime < media.duration && shuttleRate > 0) seekLocal(angleWindow.maxTime);
      else stopShuttle();
    };
    player.onloadedmetadata=()=>{
      if(!dialog?.open)return;
      if (isSubclip || angleWindow) {
        seekLocal(0);
      } else if (!media.multicam && Number.isFinite(player.duration) && player.duration>0) {
        media.duration=player.duration;
        if(Number.isFinite(marks.out) && marks.out>media.duration) delete marks.out;
      }
    };
    dialog.querySelector('.source-close').onclick=close;
    async function setPreviewSource(settings) {
      const active=dialog, position=current();
      stopShuttle();show('Building a playback proxy…',false);
      const actions=[...dialog.querySelectorAll('.source-actions [data-action]')];
      active.dataset.preparing='true';
      for(const button of actions)button.disabled=true;
      try {
        const stream='audioStream' in settings?settings.audioStream:media.audioStream??null;
        const id=media.id;
        const item=settings.seekOptimized ? await Editor.prepareSourceProxy(id) :
          Editor.get().project.media.find(entry=>entry.id===id);
        if (dialog!==active) return false;
        let src=item.src;
        if(stream!==(item.audioStream??null)) {
          const job=SpliceMedia.preparePreview(item,stream,false,true);
          await job?.done;
          if (dialog!==active) return false;
          if (!job?.src) throw Error(job?.error||'Could not prepare this audio stream.');
          src=job.src;
        }
        media={...media,...item,src,audioStream:stream};
        player.addEventListener('loadedmetadata',()=>{if(dialog===active)seekLocal(position);},{once:true});
        player.src=src;
        show('Playback proxy updated.',false);
        return true;
      } finally {
        delete active.dataset.preparing;
        for(const button of actions)button.disabled=false;
      }
    }
    const proxyButton=document.createElement('button');
    const trackPicker=document.createElement('select');
    if(media.type==='video' && !media.still && !isSubclip && !media.multicam && !Number.isInteger(media.nestedSequence) && !media.nestedProject) {
      const button=proxyButton;button.type='button';button.className='source-proxy';
      button.textContent=media.seekOptimized?'Seek proxy active':'Build seek proxy';
      button.disabled=!!media.seekOptimized;
      dialog.querySelector('.source-bin').before(button);
      button.onclick=async()=>{
        const active=dialog;
        button.disabled=trackPicker.disabled=true;
        try {
          if(!await setPreviewSource({seekOptimized:true}))return;
          button.textContent='Seek proxy active';
          show('Preview uses the seek proxy. Export uses the original.',false);
        } catch(error) {
          if(dialog!==active)return;
          button.disabled=false;show(error.message);
        } finally {trackPicker.disabled=false;}
      };
    }
    if(media.audioStreams>1 && !isSubclip && !media.multicam) {
      const label=document.createElement('label');label.className='source-track-label';
      label.append('Audio for new edits ',trackPicker);
      trackPicker.append(new Option('Default stereo routing',''));
      for(let i=0;i<media.audioStreams;i++)trackPicker.append(new Option(`Stream ${i+1}`,String(i)));
      trackPicker.value=media.audioStream??'';
      dialog.querySelector('.source-bin').before(label);
      trackPicker.onchange=async()=>{
        const active=dialog;
        const choice=trackPicker.value===''?null:Number(trackPicker.value);
        Editor.setSourceSettings(media.id,{sourceAudioStream:choice});
        proxyButton.disabled=trackPicker.disabled=true;
        try {await setPreviewSource({audioStream:choice});}
        catch(error){
          if(dialog===active){
            const previous=media.audioStream??null;
            trackPicker.value=previous??'';
            Editor.setSourceSettings(media.id,{sourceAudioStream:previous});
            show(error.message);
          }
        }
        finally {trackPicker.disabled=false;proxyButton.disabled=!!media?.seekOptimized;}
      };
    }
    // Changing a target clears the last edit's message (it was about the old target). Sound "in
    // the picture clip" needs a picture track, so it is unavailable while Picture is None.
    const picturePicker=dialog.querySelector('.source-track'), soundPicker=dialog.querySelector('.source-audio-track');
    const syncTargets=()=>{
      const embeddedOption=soundPicker?.querySelector('option[value=""]');
      if(!embeddedOption)return;
      embeddedOption.disabled=picturePicker.value==='none';
      if(embeddedOption.disabled&&soundPicker.value==='')soundPicker.value='follow';
    };
    picturePicker.onchange=()=>{syncTargets();show('');};
    if(soundPicker)soundPicker.onchange=()=>show('');
    dialog.querySelector('.source-scrub').oninput=event=>{stopShuttle();seekLocal(Number(event.target.value));};
    dialog.querySelector('.source-bin input').onchange=event=>{
      const value=event.target.value.trim().replace(/\s*\/\s*/g,'/').replace(/^\/+|\/+$/g,'');
      if (window.SpliceBins?.updateMediaMetadata) window.SpliceBins.updateMediaMetadata(id, { bin: value });
      else Editor.editProject(project=>{const item=project.media.find(entry=>entry.id===id);item.bin=value;});
    };
    for(const button of dialog.querySelectorAll('[data-action]'))button.onclick=()=>{
      switch(button.dataset.action){
        case 'back':stopShuttle();seekLocal(Math.max(0,current()-1/fps));break;
        case 'forward':stopShuttle();seekLocal(Math.min(media.duration,current()+1/fps));break;
        case 'play':shuttleRate?stopShuttle():shuttle(1);break;
        case 'in':mark('in');break;
        case 'out':mark('out');break;
        case 'make-subclip':makeSubclip();break;
        case 'toggle-subclip-limits': {
          try {
            const limited = window.SpliceBins?.toggleSubclipLimits(media.id);
            show(limited ? 'Subclip bounds enforced on trim.' : 'Subclip limits removed: timeline trims can extend into parent media.', false);
          } catch (err) { show(err.message); }
          break;
        }
        default:place(button.dataset.action);
      }
    };
    // Non-modal, so the timeline (playhead, In/Out, scrolling) stays usable while it is open.
    dialog.tabIndex=-1;
    dialog.show();
    document.addEventListener('keydown',onKey,true);
    committedMarks=JSON.stringify(item.sourceMarks||{});
    unsubscribe=Editor.subscribe(follow);
    dialog.focus();
    update();
    const choice=item.sourceAudioStream;
    if(media.audioStreams>1 && !isSubclip && !media.multicam && (choice===null || Number.isInteger(choice) && choice>=0 && choice<media.audioStreams) &&
        choice!==(media.audioStream??null)){
      trackPicker.value=choice??'';
      trackPicker.onchange();
    }
  }
  // Where a drop on a timeline lane would put picture and sound: shown while dragging.
  function describeDrop(drop) {
    if (!dialog || !media) return '';
    return targetsText(drop);
  }
  window.SpliceSource={open,place,makeSubclip,describeDrop,isOpen:()=>!!dialog?.open,mediaId:()=>media?.id??null,mediaType:()=>media?.type??null};
})();
