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
        if (values.length !== 3 || values.some(v=>!Number.isFinite(v)||v<0||v>1)) throw new Error('Invalid LUT color sample.');
        rows.push(...values);
        if (rows.length > MAX_SIZE**3*3) throw new Error('The LUT has too many samples.');
      }
    }
    if (domainMin.some((v,i)=>v>=domainMax[i])) throw new Error('The LUT domain has no usable range.');
    if (!size || rows.length !== size**3*3) throw new Error('The LUT sample count does not match its size.');
    return {size,data:rows,domainMin,domainMax,title:String(title).slice(0,120)};
  }
  function applyImageData(imageData,lut,intensity=100) {
    const {size,data,domainMin=[0,0,0],domainMax=[1,1,1]}=lut;
    if (!Number.isInteger(size)||size<2||size>MAX_SIZE||!Array.isArray(data)||data.length!==size**3*3) throw new Error('Invalid project LUT.');
    const pixels=imageData.data, mix=Math.max(0,Math.min(1,Number(intensity)/100));
    const scale=size-1;
    const index=(r,g,b)=>3*(r+size*(g+size*b));
    for(let p=0;p<pixels.length;p+=4){
      const source=[pixels[p],pixels[p+1],pixels[p+2]];
      const coords=source.map((v,c)=>Math.max(0,Math.min(scale,(v/255-domainMin[c])/(domainMax[c]-domainMin[c])*scale)));
      const lo=coords.map(Math.floor),hi=lo.map(v=>Math.min(size-1,v+1)),t=coords.map((v,i)=>v-lo[i]);
      for(let c=0;c<3;c++){
        let output=0;
        for(let b=0;b<2;b++)for(let g=0;g<2;g++)for(let r=0;r<2;r++){
          const weight=(r?t[0]:1-t[0])*(g?t[1]:1-t[1])*(b?t[2]:1-t[2]);
          output+=weight*data[index(r?hi[0]:lo[0],g?hi[1]:lo[1],b?hi[2]:lo[2])+c];
        }
        pixels[p+c]=Math.round(source[c]*(1-mix)+Math.max(0,Math.min(255,output*255))*mix);
      }
    }
    return imageData;
  }
  window.SpliceLUT={parseCube,applyImageData};
})();
