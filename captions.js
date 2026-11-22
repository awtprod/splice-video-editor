// Edit timed text using the same project transactions as other timeline edits.
window.SpliceCaptions = (() => {
  const timecode = value => {
    const ms=Math.max(0,Math.round(value*1000));
    const two=x=>String(x).padStart(2,'0');
    return two(Math.floor(ms/3600000))+':'+two(Math.floor(ms/60000)%60)+':'+two(Math.floor(ms/1000)%60)+','+String(ms%1000).padStart(3,'0');
  };
  function seconds(t) {
    const m=/^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$/.exec(t.trim());
    if(!m || Number(m[2])>=60 || Number(m[3])>=60) throw Error('Invalid SRT timecode '+t);
    return Number(m[1])*3600+Number(m[2])*60+Number(m[3])+Number(m[4])/1000;
  }
  function parse(text) {
    const blocks=text.replace(/^\uFEFF/,'').replace(/\r\n?/g,'\n').trim().split(/\n[ \t]*\n/);
    if(blocks.length>10000) throw Error('This subtitle file exceeds 10,000 cues.');
    return blocks.map((block,i)=>{
      const lines=block.split('\n');
      if(/^\d+$/.test(lines[0].trim()))lines.shift();
      const times=lines.shift().split(/\s*-->\s*/);
      if(times.length!==2)throw Error('Invalid timing in caption '+(i+1));
      const start=seconds(times[0]),end=seconds(times[1]),value=lines.join('\n').trim();
      if(!value || value.length>4000 || end<=start || end>86400)throw Error('Invalid text or duration in caption '+(i+1));
      return {id:crypto.randomUUID(),start,duration:end-start,text:value,track:0};
    });
  }
  function render(container,state,notify) {
    container.replaceChildren();
    const toolbar=document.createElement('div');toolbar.className='caption-actions';
    const button=(parent,label,action)=>{
      const b=document.createElement('button');b.textContent=label;b.type='button';b.onclick=action;parent.append(b);return b;
    };
    button(toolbar,'Add caption',()=>{
      Editor.editProject(p=>{(p.captions||=[]).push({id:crypto.randomUUID(),start:Editor.get().time,duration:2,text:'Caption',track:0});});
    });
    const picker=document.createElement('input');picker.type='file';picker.accept='.srt';picker.hidden=true;
    picker.onchange=async()=>{
      const file=picker.files[0];if(!file)return;
      const project=Editor.get().project;
      try{
        if(file.size>4*1024*1024)throw Error('The subtitle file exceeds 4 MiB.');
        const items=parse(await file.text());
        if(Editor.get().project!==project)throw Error('The active sequence changed during subtitle import.');
        Editor.editProject(p=>{p.captions=[...(p.captions||[]),...items];});
      }catch(e){notify(e.message);}
    };
    button(toolbar,'Import SRT',()=>picker.click());
    const list=[...(state.project.captions||[])].sort((a,b)=>a.start-b.start);
    const download=button(toolbar,'Export SRT',()=>{
      const srt=list.map((c,i)=>(i+1)+'\n'+timecode(c.start)+' --> '+timecode(c.start+c.duration)+'\n'+c.text+'\n').join('\n');
      const url=URL.createObjectURL(new Blob([srt],{type:'application/x-subrip;charset=utf-8'}));
      const a=document.createElement('a');a.href=url;a.download=state.project.name+'.srt';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
    });
    download.disabled=!list.length;toolbar.append(picker);container.append(toolbar);
    if(!list.length){
      const note=document.createElement('p');note.className='library-help';note.textContent='Add a caption at the playhead or import timed subtitles.';container.append(note);
    }
    for(const cue of list){
      const card=document.createElement('article');card.className='caption-card';
      const edit=patch=>Editor.editProject(p=>Object.assign(p.captions.find(c=>c.id===cue.id),patch));
      button(card,timecode(cue.start)+' → '+timecode(cue.start+cue.duration),()=>Editor.seek(cue.start));
      const field=document.createElement('textarea');field.value=cue.text;field.maxLength=4000;field.setAttribute('aria-label','Caption text');
      field.onchange=()=>{if(field.value.trim())edit({text:field.value});else field.value=cue.text;};card.append(field);
      const props=document.createElement('div');props.className='caption-timing';card.append(props);
      for(const key of ['start','duration']){
        const label=document.createElement('label');label.textContent=key==='start'?'Start':'Duration';
        const input=document.createElement('input');input.type='number';input.min=key==='start'?'0':String(1/state.project.fps);
        input.step=String(1/state.project.fps);input.value=String(Number(cue[key].toFixed(3)));input.setAttribute('aria-label','Caption '+key);
        input.onchange=()=>{const n=Number(input.value);if(input.value&&n>=Number(input.min)&&n<=86400)edit({[key]:n});else input.value=cue[key];};
        label.append(input);props.append(label);
      }
      button(card,'Remove',()=>Editor.editProject(p=>{p.captions=p.captions.filter(c=>c.id!==cue.id);}));
      container.append(card);
    }
  }
  function paint(state) {
    const node=document.getElementById('monitor-caption');
    const now=state.time;
    const cues=(state.project.captions||[]).filter(c=>c.start<=now&&now<c.start+c.duration);
    const value=cues.map(c=>c.text).join('\n');
    if(node.textContent!==value)node.textContent=value;
    node.hidden=!value;
    if(cues.length){
      const style=cues[0];node.style.color=style.fillColor||'white';
      node.style.fontFamily=style.fontFamily||'DM Sans';
    }
  }
  return {render,paint};
})();
