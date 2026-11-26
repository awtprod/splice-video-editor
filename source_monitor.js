(() => {
  // Source marks: either may be unset, which three-point editing treats differently. Both
  // include the frame they are set on; `out` is stored as the end of that frame (see Editor.setMark).
  let dialog, media, player, marks = {}, fps = 25, shuttleTimer, shuttleRate = 0;
  let unsubscribe = null, committedMarks = '{}', frameRequest = 0;
  const markIn = () => Number.isFinite(marks.in) ? marks.in : 0;
  const markOut = () => Number.isFinite(marks.out) ? marks.out : media.duration;
  // The last frame the range includes, for display.
  const lastFrame = () => Math.max(markIn(), Math.round(markOut() * fps - 1) / fps);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const snap = value => Math.round(value * fps) / fps;
  const format = seconds => Editor.formatTimecode(seconds,fps);
  const show = (message,error=true) => {const node=dialog.querySelector('.source-status');node.textContent=message;node.classList.toggle('error',!!message&&error);};
  const current = () => Math.max(0,Math.min(media.duration,player?.currentTime || 0));
  function update() {
    if (!dialog?.open) return;
    dialog.querySelector('.source-time').textContent = format(current())+' / '+format(media.duration);
    dialog.querySelector('.source-range').textContent = 'In '+format(markIn())+' · Out '+format(lastFrame())+' · '+format(markOut()-markIn());
    dialog.querySelector('.source-plan').textContent = plan();
    dialog.querySelector('.source-scrub').value = current();
    dialog.querySelector('.source-scrub').max = media.duration;
    frameRequest = requestAnimationFrame(update);
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
    document.querySelector('.workspace')?.classList.remove('source-open');
  }
  // Follow the project: undo can change the saved marks, and a new project can drop the source.
  function follow(state) {
    if (!dialog || !media) return;
    const item=state.project.media.find(entry=>entry.id===media.id);
    if (!item) { close(); return; }
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
    if(direction>0){player.playbackRate=rate;player.play().catch(error=>show(error.message));return;}
    shuttleTimer=setInterval(()=>{
      if(!dialog?.open || current()<=0){stopShuttle();return;}
      if(player.seeking)return;
      player.currentTime=Math.max(0,current()-rate/fps);
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
  function place(mode) {
    if (!dialog || dialog.dataset.preparing) return;
    show('');
    const track=Number(dialog.querySelector('.source-track').value);
    if (Editor.isTrackLocked(track, media.type)) return show(`Unlock ${media.type} track ${Math.abs(track)} before editing.`);
    const soundPicker=dialog.querySelector('.source-audio-track');
    const audioTrack=soundPicker && soundPicker.value!=='' ? Number(soundPicker.value) : null;
    let result;
    try { result=Editor.placeSource(media.id,{mode,track,audioTrack,marks:{...marks}}); }
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
    if (window.SpliceKeys?.keyBelongsToControl(document.activeElement,event)) return;
    const key=event.key.toLowerCase();
    if(key==='escape'){event.preventDefault();close();}
    if(key==='i'||key==='o'){event.preventDefault();mark(key==='i'?'in':'out');}
    if(key===' '){event.preventDefault();shuttleRate?stopShuttle():shuttle(1);}
    if(key==='j'){event.preventDefault();shuttle(-1);}
    if(key==='k'){event.preventDefault();stopShuttle();}
    if(key==='l'){event.preventDefault();shuttle(1);}
    if(key==='arrowleft'){event.preventDefault();stopShuttle();player.currentTime=Math.max(0,current()-1/fps);}
    if(key==='arrowright'){event.preventDefault();stopShuttle();player.currentTime=Math.min(media.duration,current()+1/fps);}
    if(key==='enter'||key===','){event.preventDefault();place('insert');}
    if(key==='.'){event.preventDefault();place('overwrite');}
  }
  function open(id) {
    close();
    const item=Editor.get().project.media.find(entry=>entry.id===id);
    if(!item){window.alert('Media is unavailable.');return;}
    if(!item.src || item.offline){window.alert('Relink '+item.name+' before opening it.');return;}
    media={...item,duration:Number.isFinite(Number(item.duration)) && Number(item.duration)>0 ? Number(item.duration) : 0};
    fps=Number.isFinite(media.fps) && media.fps>0 ? media.fps : Editor.get().project.fps;
    const state=Editor.get().project;
    const trackCount=Math.min(64,Math.max(state.trackCounts?.[media.type] || 3,
      ...state.clips.filter(clip=>clip.type===media.type).map(clip=>Math.abs(clip.track)),2)+1);
    const nativeTrackLimit=media.type==='video' && !state.premiereOriginal ? 64 : (state.trackCounts?.[media.type] || 3);
    const saved=item.sourceMarks||{};
    marks={};
    if(Number.isFinite(saved.out)) marks.out=Math.max(0,Math.min(media.duration,saved.out));
    if(Number.isFinite(saved.in)) marks.in=Math.max(0,Math.min(markOut(),saved.in));
    if(markOut()<=markIn()) marks={};
    const soundTracks=Math.min(64,Math.max(state.trackCounts?.audio || 3,
      ...state.clips.filter(clip=>clip.type==='audio').map(clip=>Math.abs(clip.track)),2)+1);
    const soundTarget=media.type==='video' && !media.still && media.audioChannels>0 ?
      `<label class="source-track-label">Camera sound to <select class="source-audio-track"><option value="">Keep inside the picture clip</option>${Array.from({length:soundTracks},(_,i)=>`<option value="${-i-1}"${i===0?' selected':''}>Audio ${i+1}</option>`).join('')}</select></label>` : '';
    dialog=document.createElement('dialog');dialog.className='source-dialog';
    dialog.setAttribute('aria-label','Source monitor: '+media.name);
    const tag=media.type==='audio'?'audio':'video';
    const trackOptions=Array.from({length:trackCount},(_,i)=>media.type==='video'?i+1:-i-1)
      .map(n=>`<option value="${n}">${media.type==='video'?'Video':'Audio'} ${Math.abs(n)}${Math.abs(n)>nativeTrackLimit?' · working project only':''}</option>`).join('');
    // Head, picture, settings (they scroll when the window is short) and a footer whose transport,
    // mark and edit buttons are always in view.
    dialog.innerHTML=`<div class="source-head"><div><small>SOURCE</small><strong>${esc(media.name)}</strong></div><button class="source-close" aria-label="Close source monitor">×</button></div>`+
      `<div class="source-stage"><${tag} controls playsinline preload="auto"></${tag}></div>`+
      `<div class="source-controls"><input class="source-scrub" type="range" min="0" max="${media.duration}" step="${1/fps}" value="0" aria-label="Source playhead">`+
      `<div class="source-readout"><span class="source-time"></span><span class="source-range"></span></div><small class="source-plan"></small>`+
      `<small class="source-shortcuts">I · O mark (both frames included) · J K L shuttle · , insert · . overwrite</small>`+
      `<label class="source-track-label">Timeline track <select class="source-track">${trackOptions}</select></label>${soundTarget}`+
      `<label class="source-bin">Bin path <input type="text" value="${esc(media.bin || '')}" placeholder="e.g. Interview / Camera A" aria-label="Media bin path"></label></div>`+
      `<div class="source-footer"><p class="source-status" role="status"></p><div class="source-bar">`+
      `<div class="source-buttons"><button data-action="back" title="Back one frame (←)">−1</button><button data-action="play" title="Play / pause (Space)">Play</button><button data-action="forward" title="Forward one frame (→)">+1</button><button data-action="in" title="Mark In on this frame (I)">Mark In</button><button data-action="out" title="Mark Out on this frame, included (O)">Mark Out</button></div>`+
      `<div class="source-actions"><button data-action="append">Append</button><button data-action="overwrite" title="Overwrite (.)">Overwrite</button><button class="source-primary" data-action="insert" title="Insert (,)">Insert</button></div></div></div>`;
    // A panel of the workspace, left of the program monitor (the inspector makes room).
    const workspace=document.querySelector('.workspace'), viewer=workspace?.querySelector('.viewer');
    if (viewer) { viewer.before(dialog); workspace.classList.add('source-open'); }
    else document.body.append(dialog);
    player=dialog.querySelector('video,audio');player.src=media.src;player.onerror=()=>show('The source file could not be decoded.');player.onended=stopShuttle;
    player.onloadedmetadata=()=>{
      if(!dialog?.open)return;
      if(Number.isFinite(player.duration)&&player.duration>0){
        const old=media.duration;
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
        player.addEventListener('loadedmetadata',()=>{if(dialog===active)player.currentTime=position;},{once:true});
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
    if(media.type==='video' && !media.still && !Number.isInteger(media.nestedSequence)) {
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
    if(media.audioStreams>1) {
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
    dialog.querySelector('.source-scrub').oninput=event=>{player.pause();player.currentTime=Number(event.target.value);};
    dialog.querySelector('.source-bin input').onchange=event=>{
      const value=event.target.value.trim().replace(/\s*\/\s*/g,'/').replace(/^\/+|\/+$/g,'');
      Editor.editProject(project=>{const item=project.media.find(entry=>entry.id===id);item.bin=value;});
    };
    for(const button of dialog.querySelectorAll('[data-action]'))button.onclick=()=>{
      switch(button.dataset.action){
        case 'back':stopShuttle();player.currentTime=Math.max(0,current()-1/fps);break;
        case 'forward':stopShuttle();player.currentTime=Math.min(media.duration,current()+1/fps);break;
        case 'play':shuttleRate?stopShuttle():shuttle(1);break;
        case 'in':mark('in');break;
        case 'out':mark('out');break;
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
    if(media.audioStreams>1 && (choice===null || Number.isInteger(choice) && choice>=0 && choice<media.audioStreams) &&
        choice!==(media.audioStream??null)){
      trackPicker.value=choice??'';
      trackPicker.onchange();
    }
  }
  window.SpliceSource={open,place,isOpen:()=>!!dialog?.open};
})();
