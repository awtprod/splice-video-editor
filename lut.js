(() => {
  const MAX_SIZE = 33;
  function parseCube(content, title = 'Color LUT') {
    if (typeof content !== 'string' || content.length > 4_000_000) throw new Error('The LUT file is too large.');
    let size, domainMin = [0,0,0], domainMax = [1,1,1], rows = [];
    for (const source of content.split(/\r?\n/)) {
      const line = source.split('#',1)[0].trim();
      if (!line) continue;
      if (/^TITLE\s/i.test(line)) continue;
      const parts = line.split(/\s+/);
      const key = parts[0].toUpperCase();
      if (key === 'LUT_3D_SIZE') {
        size = Number(parts[1]);
        if (!Number.isInteger(size) || size < 2 || size > MAX_SIZE) throw new Error('Use a 3D LUT with 2–33 points per axis.');
      } else if (key === 'LUT_1D_SIZE') throw new Error('This editor supports 3D .cube LUTs.');
      else if (key === 'DOMAIN_MIN' || key === 'DOMAIN_MAX') {
        const values = parts.slice(1).map(Number);
        if (values.length !== 3 || values.some(v=>!Number.isFinite(v)||Math.abs(v)>16)) throw new Error('Invalid LUT domain.');
        if (key === 'DOMAIN_MIN') domainMin = values; else domainMax = values;
      } else {
        if (!size) throw new Error('The 3D LUT size must appear before its samples.');
        const values = parts.map(Number);
        if (values.length !== 3 || values.some(v=>!Number.isFinite(v)||v < -4||v > 4)) throw new Error('Invalid LUT color sample.');
        rows.push(...values);
        if (rows.length > MAX_SIZE**3*3) throw new Error('The LUT has too many samples.');
      }
    }
    if (domainMin.some((v,i)=>v>=domainMax[i])) throw new Error('The LUT domain has no usable range.');
    if (!size || rows.length !== size**3*3) throw new Error('The LUT sample count does not match its size.');
    return {size,data:rows,domainMin,domainMax,title:String(title).slice(0,120)};
  }
  function validate(lut) {
    const {size,data,domainMin=[0,0,0],domainMax=[1,1,1]}=lut;
    if (!Number.isInteger(size)||size<2||size>MAX_SIZE||!Array.isArray(data)||data.length!==size**3*3) throw new Error('Invalid project LUT.');
    for (let channel=0;channel<3;channel++) {
      const min=domainMin?.[channel],max=domainMax?.[channel];
      if (!Number.isFinite(min)||!Number.isFinite(max)||min>=max) throw new Error('Invalid project LUT domain.');
    }
  }
  function applyImageData(imageData,lut,intensity=100) {
    validate(lut);
    const {size,data,domainMin=[0,0,0],domainMax=[1,1,1]}=lut;
    const pixels=imageData.data, mix=Math.max(0,Math.min(1,Number(intensity)/100));
    if (mix===0) return imageData;
    const low=[],high=[],fraction=[];
    for(let channel=0;channel<3;channel++) {
      const lo=new Uint8Array(256),hi=new Uint8Array(256),weight=new Float32Array(256);
      const min=domainMin[channel],max=domainMax[channel];
      for(let value=0;value<256;value++) {
        const position=Math.max(0,Math.min(size-1,(value/255-min)/(max-min)*(size-1)));
        lo[value]=Math.floor(position);hi[value]=Math.min(size-1,lo[value]+1);weight[value]=position-lo[value];
      }
      low.push(lo);high.push(hi);fraction.push(weight);
    }
    const redLow=low[0],greenLow=low[1],blueLow=low[2];
    const redHigh=high[0],greenHigh=high[1],blueHigh=high[2];
    const redFraction=fraction[0],greenFraction=fraction[1],blueFraction=fraction[2];
    const plane=size*size;
    for(let p=0;p<pixels.length;p+=4){
      const red=pixels[p],green=pixels[p+1],blue=pixels[p+2];
      const r0=redLow[red],r1=redHigh[red],g0=greenLow[green],g1=greenHigh[green],b0=blueLow[blue],b1=blueHigh[blue];
      const fr=redFraction[red],fg=greenFraction[green],fb=blueFraction[blue];
      const i000=3*(r0+size*g0+plane*b0),i100=3*(r1+size*g0+plane*b0);
      const i010=3*(r0+size*g1+plane*b0),i110=3*(r1+size*g1+plane*b0);
      const i001=3*(r0+size*g0+plane*b1),i101=3*(r1+size*g0+plane*b1);
      const i011=3*(r0+size*g1+plane*b1),i111=3*(r1+size*g1+plane*b1);
      for(let channel=0;channel<3;channel++) {
        const c00=data[i000+channel]+fr*(data[i100+channel]-data[i000+channel]);
        const c10=data[i010+channel]+fr*(data[i110+channel]-data[i010+channel]);
        const c01=data[i001+channel]+fr*(data[i101+channel]-data[i001+channel]);
        const c11=data[i011+channel]+fr*(data[i111+channel]-data[i011+channel]);
        const c0=c00+fg*(c10-c00),c1=c01+fg*(c11-c01);
        pixels[p+channel]=Math.round(pixels[p+channel]*(1-mix)+255*mix*(c0+fb*(c1-c0)));
      }
    }
    return imageData;
  }
  window.SpliceLUT={parseCube,validate,applyImageData};
})();
