(() => {
  let dialog, media, player, markIn = 0, markOut = 0, fps = 25, shuttleTimer, shuttleRate = 0;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const snap = value => Math.round(value * fps) / fps;
  const format = seconds => Editor.formatTimecode(seconds,fps);
  const show = (message,error=true) => {const node=dialog.querySelector('.source-status');node.textContent=message;node.classList.toggle('error',!!message&&error);};
  const current = () => Math.max(0,Math.min(media.duration,player?.currentTime || 0));
  function update() {
    if (!dialog?.open) return;
    dialog.querySelector('.source-time').textContent = format(current())+' / '+format(media.duration);
    dialog.querySelector('.source-range').textContent = format(markIn)+' → '+format(markOut)+'  ·  '+format(markOut-markIn);
    dialog.querySelector('.source-scrub').value = current();
    dialog.querySelector('.source-scrub').max = media.duration;
    requestAnimationFrame(update);
  }
  function close() {
    if (!dialog) return;
    stopShuttle();
    player.pause();player.removeAttribute('src');player.load();
    dialog.close();dialog.remove();dialog=null;player=null;media=null;
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
  function newClip(start,track,duration) {
    return {id:crypto.randomUUID(),mediaId:media.id,start,duration,in:markIn,track,type:media.type,title:'',color:{brightness:100,contrast:100,saturation:100},volume:100,transition:'none'};
  }
  function place(mode) {
    show('');
    if (markOut-markIn < 1/fps) return show('Mark a range at least one frame long.');
    const track=Number(dialog.querySelector('.source-track').value);
    if (Editor.isTrackLocked(track, media.type)) return show(`Unlock ${media.type} track ${Math.abs(track)} before editing.`);
    const state=Editor.get(), time=mode==='append' ? Math.max(0,...state.project.clips.filter(c=>c.type===media.type && c.track===track).map(c=>c.start+c.duration)) : state.time;
    const sequenceFps=state.project.fps;
    const frames=Math.min(Math.round((markOut-markIn)*sequenceFps),Math.floor((media.duration-markIn)*sequenceFps+1e-6));
    if (frames<1) return show('Mark a range at least one sequence frame long.');
    const start=Math.round(time*sequenceFps)/sequenceFps, length=frames/sequenceFps, end=start+length;
    if (typeof Editor.editProject !== 'function') return show('Timeline edit service is unavailable.');
    const clip=newClip(start,track,length);
    try { Editor.editProject(project => {
      const additions=[];
      const rightGroups=new Map();
      if (mode==='insert') {
        for (const existing of project.clips) {
          if (Editor.isTrackLocked(existing.track, existing.type)) continue;
          if ((existing.track !== track || existing.type !== media.type) && !Editor.isTrackSyncLocked(existing.track, existing.type)) continue;
          if (existing.start >= start-1e-5) existing.start+=length;
          else if (existing.start+existing.duration > start+1e-5) {
            const right=Editor.splitClipAt(existing,start);
            if(right){if(right.linkGroup){if(!rightGroups.has(right.linkGroup))rightGroups.set(right.linkGroup,crypto.randomUUID());right.linkGroup=rightGroups.get(right.linkGroup);}
              right.start+=length;additions.push(right);}
          }
        }
      } else {
        for(let i=project.clips.length-1;i>=0;i--) {
          const existing=project.clips[i];
          if(existing.track!==track || existing.type!==media.type) continue;
          const a=existing.start,b=a+existing.duration;
          if(b<=start+1e-5 || a>=end-1e-5) continue;
          const head=a<start-1e-5, tail=b>end+1e-5;
          if(!head && !tail){project.clips.splice(i,1);continue;}
          if(head && tail){
            const middle=Editor.splitClipAt(existing,start);
            const right=Editor.splitClipAt(middle,end);
            delete existing.linkGroup;delete right.linkGroup;
            additions.push(right);continue;
          }
          if(head){
            Editor.splitClipAt(existing,start);
            delete existing.linkGroup;continue;
          }
          const right=Editor.splitClipAt(existing,end);
          const {id,premiereIndex,premiereTrack}=existing;
          for (const key of Object.keys(existing)) delete existing[key];
          Object.assign(existing,right,{id});
          if(premiereIndex!==undefined)existing.premiereIndex=premiereIndex;
          if(premiereTrack!==undefined)existing.premiereTrack=premiereTrack;
          delete existing.linkGroup;
        }
      }
      project.clips.push(...additions,clip);
      project.duration=Math.max(0,...project.clips.map(item=>item.start+item.duration));
    }); } catch (error) { return show(error.message); }
    Editor.select(clip.id);
    close();
  }
  function onKey(event) {
    if (!dialog?.open || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName) && document.activeElement?.type!=='range') return;
    const key=event.key.toLowerCase();
    if(key==='escape'){event.preventDefault();close();}
    if(key==='i'){event.preventDefault();markIn=snap(current());if(markIn>=markOut)markOut=Math.min(media.duration,markIn+1/fps);}
    if(key==='o'){event.preventDefault();markOut=snap(current());if(markOut<=markIn)markIn=Math.max(0,markOut-1/fps);}
    if(key===' '){event.preventDefault();shuttleRate?stopShuttle():shuttle(1);}
    if(key==='j'){event.preventDefault();shuttle(-1);}
    if(key==='k'){event.preventDefault();stopShuttle();}
    if(key==='l'){event.preventDefault();shuttle(1);}
    if(key==='arrowleft'){event.preventDefault();stopShuttle();player.currentTime=Math.max(0,current()-1/fps);}
    if(key==='arrowright'){event.preventDefault();stopShuttle();player.currentTime=Math.min(media.duration,current()+1/fps);}
    if(key==='enter'){event.preventDefault();place('insert');}
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
    markIn=0;markOut=media.duration;
    dialog=document.createElement('dialog');dialog.className='source-dialog';
    dialog.innerHTML=`<div class="source-head"><div><small>SOURCE MONITOR</small><strong>${esc(media.name)}</strong></div><button class="source-close" aria-label="Close source monitor">×</button></div><div class="source-stage"><${media.type==='audio'?'audio':'video'} controls playsinline preload="auto"></${media.type==='audio'?'audio':'video'}></div><div class="source-controls"><input class="source-scrub" type="range" min="0" max="${media.duration}" step="${1/fps}" value="0" aria-label="Source playhead"><span class="source-time"></span><div class="source-buttons"><button data-action="back">−1 frame</button><button data-action="play">Play / Pause</button><button data-action="forward">+1 frame</button><button data-action="in">Mark In (I)</button><button data-action="out">Mark Out (O)</button></div><span class="source-range"></span><small class="source-shortcuts">J reverse · K stop · L forward</small><label class="source-bin">Bin path <input type="text" value="${esc(media.bin || '')}" placeholder="e.g. Interview / Camera A" aria-label="Media bin path"></label><label class="source-track-label">Timeline track <select class="source-track">${Array.from({length:trackCount},(_,i)=>media.type==='video'?i+1:-i-1).map(n=>`<option value="${n}">${media.type==='video'?'Video':'Audio'} ${Math.abs(n)}${Math.abs(n)>(state.trackCounts?.[media.type]||3)?' · working project only':''}</option>`).join('')}</select></label><div class="source-actions"><button data-action="append">Append</button><button data-action="overwrite">Overwrite</button><button class="source-primary" data-action="insert">Insert at playhead</button></div><p class="source-status" role="status"></p></div>`;
    document.body.append(dialog);player=dialog.querySelector('video,audio');player.src=media.src;player.onerror=()=>show('The source file could not be decoded.');player.onended=stopShuttle;
    player.onloadedmetadata=()=>{
      if(!dialog?.open)return;
      if(Number.isFinite(player.duration)&&player.duration>0){
        const old=media.duration;
        media.duration=player.duration;
        if(!Number.isFinite(old)||old<=0||markOut>media.duration) markOut=media.duration;
      }
    };
    dialog.querySelector('.source-close').onclick=close;
    async function setPreviewSource(settings) {
      const active=dialog, position=current();
      stopShuttle();show('Building a playback proxy…',false);
      const replacement=await Editor.prepareSourceProxy(media.id,settings);
      if(dialog!==active)return false;
      media={...media,...replacement};
      player.addEventListener('loadedmetadata',()=>{if(dialog===active)player.currentTime=position;},{once:true});
      player.src=media.src;
      show('Playback proxy updated.',false);
      return true;
    }
    const proxyButton=document.createElement('button');
    const trackPicker=document.createElement('select');
    if(media.type==='video' && !media.still && !Number.isInteger(media.nestedSequence)) {
      const button=proxyButton;button.type='button';
      button.textContent=media.seekOptimized?'Seek proxy active':'Build seek proxy';
      button.disabled=!!media.seekOptimized;
      dialog.querySelector('.source-actions').prepend(button);
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
      label.append('Source audio ',trackPicker);
      trackPicker.append(new Option('Default stereo routing',''));
      for(let i=0;i<media.audioStreams;i++)trackPicker.append(new Option(`Stream ${i+1}`,String(i)));
      trackPicker.value=media.audioStream??'';
      dialog.querySelector('.source-actions').before(label);
      trackPicker.onchange=async()=>{
        const active=dialog;
        const choice=trackPicker.value===''?null:Number(trackPicker.value);
        proxyButton.disabled=trackPicker.disabled=true;
        try {await setPreviewSource({audioStream:choice});}
        catch(error){if(dialog===active){trackPicker.value=media.audioStream??'';show(error.message);}}
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
        case 'in':markIn=snap(current());if(markIn>=markOut)markOut=Math.min(media.duration,markIn+1/fps);break;
        case 'out':markOut=snap(current());if(markOut<=markIn)markIn=Math.max(0,markOut-1/fps);break;
        default:place(button.dataset.action);
      }
    };
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    dialog.addEventListener('click',event=>{if(event.target===dialog)close();});
    dialog.showModal();update();
  }
  document.addEventListener('keydown',onKey,true);
  window.SpliceSource={open};
})();
