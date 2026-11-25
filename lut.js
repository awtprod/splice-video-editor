(() => {
  // Keep these limits in step with lut.py. A 65-point cube is about 9 MB of text.
  const MAX_SIZE = 65;
  const MAX_SIZE_1D = 65536;
  const MAX_CHARS = 16_000_000;
  const MAX_EXTRA_LINES = 10_000;
  const NUMBER = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
  const validSample = value => Number.isFinite(value) && value >= -4 && value <= 4;
  const validDomain = values => Array.isArray(values) && values.length === 3 &&
    values.every(value => Number.isFinite(value) && Math.abs(value) <= 16);
  // Plain decimal text only: Number() would also accept hex, Infinity and empty strings.
  const numbers = parts => parts.map(part => NUMBER.test(part) ? Number(part) : NaN);
  const sizeToken = (token, limit, kind) => {
    const size = /^\d{1,6}$/.test(token ?? '') ? Number(token) : NaN;
    if (!Number.isInteger(size) || size < 2 || size > limit) throw new Error(`Use a ${kind} LUT with 2–${limit} points${kind === '3D' ? ' per axis' : ''}.`);
    return size;
  };
  // A 1D LUT is three independent curves, so a 3D lattice holding f_c(i/(n-1)) on
  // each axis is the same function under trilinear lookup. Curves longer than the
  // 3D limit are resampled linearly at that many points.
  function expand1D(curve, count) {
    const size = Math.min(count, MAX_SIZE), data = new Array(size**3*3);
    const level = (channel, index) => {
      if (size === count) return curve[index*3+channel];
      const position = index/(size-1)*(count-1), low = Math.floor(position), high = Math.min(count-1, low+1);
      return curve[low*3+channel]+(position-low)*(curve[high*3+channel]-curve[low*3+channel]);
    };
    const levels = [0,1,2].map(channel => Array.from({length:size}, (_, index) => level(channel, index)));
    let i = 0;
    for (let blue = 0; blue < size; blue++) for (let green = 0; green < size; green++) for (let red = 0; red < size; red++) {
      data[i++] = levels[0][red]; data[i++] = levels[1][green]; data[i++] = levels[2][blue];
    }
    return {size, data};
  }
  function parseCube(content, title = 'Color LUT') {
    if (typeof content !== 'string' || content.length > MAX_CHARS) throw new Error('The LUT file is too large.');
    let size, kind, expected = 0, domainMin = [0,0,0], domainMax = [1,1,1], count = 0, lines = 0, data;
    for (let position = 0; position <= content.length;) {
      let end = content.indexOf('\n', position);
      if (end < 0) end = content.length;
      if (++lines > MAX_SIZE**3 + MAX_EXTRA_LINES) throw new Error('The LUT has too many lines.');
      const source = content.slice(position, end);
      position = end+1;
      const line = source.split('#',1)[0].trim();
      if (!line) continue;
      if (/^TITLE\s/i.test(line)) continue;
      const parts = line.split(/\s+/);
      const key = parts[0].toUpperCase();
      if (key === 'LUT_3D_SIZE' || key === 'LUT_1D_SIZE') {
        if (kind) throw new Error('Use one LUT_3D_SIZE or LUT_1D_SIZE line.');
        kind = key === 'LUT_3D_SIZE' ? '3D' : '1D';
        if (parts.length !== 2) throw new Error('Invalid LUT size.');
        size = sizeToken(parts[1], kind === '3D' ? MAX_SIZE : MAX_SIZE_1D, kind);
        expected = kind === '3D' ? size**3 : size;
        data = new Array(expected*3);
      } else if (key === 'DOMAIN_MIN' || key === 'DOMAIN_MAX') {
        const values = numbers(parts.slice(1));
        if (!validDomain(values)) throw new Error('Invalid LUT domain.');
        if (key === 'DOMAIN_MIN') domainMin = values; else domainMax = values;
      } else {
        if (!size) throw new Error('The LUT size must appear before its samples.');
        const values = parts.length === 3 ? numbers(parts) : [];
        if (values.length !== 3 || !values.every(validSample)) throw new Error('Invalid LUT color sample.');
        if (count >= expected) throw new Error('The LUT has too many samples.');
        data[count*3] = values[0]; data[count*3+1] = values[1]; data[count*3+2] = values[2];
        count++;
      }
    }
    if (domainMin.some((v,i)=>v>=domainMax[i])) throw new Error('The LUT domain has no usable range.');
    if (!size || count !== expected) throw new Error('The LUT sample count does not match its size.');
    const lattice = kind === '1D' ? expand1D(data, size) : {size, data};
    return {size:lattice.size,data:lattice.data,domainMin,domainMax,title:String(title).slice(0,120)};
  }
  function validateShape(lut) {
    const {size,data,domainMin=[0,0,0],domainMax=[1,1,1]}=lut;
    if (!Number.isInteger(size)||size<2||size>MAX_SIZE||!Array.isArray(data)||data.length!==size**3*3) throw new Error('Invalid project LUT.');
    if (!validDomain(domainMin) || !validDomain(domainMax) || domainMin.some((min,channel)=>min>=domainMax[channel]))
      throw new Error('Invalid project LUT domain.');
  }
  function validate(lut, intensity=100) {
    validateShape(lut);
    if (!lut.data.every(validSample)) throw new Error('Invalid 3D LUT sample.');
    if (!Number.isFinite(intensity) || intensity<0 || intensity>100) throw new Error('Invalid LUT intensity.');
  }
  // Project LUTs are plain arrays so they serialize; lookups read a typed copy and a
  // colour cache, both built once per array. Results depend only on (r,g,b), the lattice,
  // the domain and the intensity, so the cache stays valid until one of those changes.
  const CACHE_BITS = 18;
  const lookups = new WeakMap();
  function lookupFor(data, signature) {
    let lookup = lookups.get(data);
    if (!lookup) { lookup = {table:Float64Array.from(data), signature:'', keys:new Int32Array(1<<CACHE_BITS), values:new Uint32Array(1<<CACHE_BITS)}; lookups.set(data, lookup); }
    if (lookup.signature !== signature) { lookup.keys.fill(-1); lookup.signature = signature; }
    return lookup;
  }
  // Per input value: lattice offsets (scaled by the stride of that axis) and the interpolation weight.
  function axisTables(size, min, max, stride) {
    const low = new Int32Array(256), high = new Int32Array(256), weight = new Float64Array(256);
    for (let value = 0; value < 256; value++) {
      const position = Math.max(0, Math.min(size-1, (value/255-min)/(max-min)*(size-1)));
      const floor = Math.floor(position);
      // The weight is held at single precision, as the first implementation stored it.
      low[value] = floor*stride; high[value] = Math.min(size-1, floor+1)*stride; weight[value] = Math.fround(position-floor);
    }
    return {low, high, weight};
  }
  function applyImageData(imageData,lut,intensity=100) {
    validateShape(lut);
    const {size,data,domainMin=[0,0,0],domainMax=[1,1,1]}=lut;
    const pixels=imageData.data, mix=Math.max(0,Math.min(1,Number(intensity)/100));
    if (mix===0) return imageData;
    const {table,keys,values}=lookupFor(data,`${size}|${domainMin}|${domainMax}|${mix}`);
    const plane=size*size, keep=1-mix, scale=255*mix, full=mix===1;
    const red=axisTables(size,domainMin[0],domainMax[0],3);
    const green=axisTables(size,domainMin[1],domainMax[1],3*size);
    const blue=axisTables(size,domainMin[2],domainMax[2],3*plane);
    const redLow=red.low,redHigh=red.high,redFraction=red.weight;
    const greenLow=green.low,greenHigh=green.high,greenFraction=green.weight;
    const blueLow=blue.low,blueHigh=blue.high,blueFraction=blue.weight;
    for(let p=0;p<pixels.length;p+=4){
      const r=pixels[p],g=pixels[p+1],b=pixels[p+2];
      const key=(r<<16)|(g<<8)|b, slot=Math.imul(key,0x9E3779B1)>>>(32-CACHE_BITS);
      if(keys[slot]===key){
        const packed=values[slot];
        pixels[p]=packed&255;pixels[p+1]=(packed>>>8)&255;pixels[p+2]=packed>>>16;
        continue;
      }
      const fr=redFraction[r],fg=greenFraction[g],fb=blueFraction[b];
      const r0=redLow[r],r1=redHigh[r],g0=greenLow[g],g1=greenHigh[g],b0=blueLow[b],b1=blueHigh[b];
      const i000=r0+g0+b0,i100=r1+g0+b0,i010=r0+g1+b0,i110=r1+g1+b0;
      const i001=r0+g0+b1,i101=r1+g0+b1,i011=r0+g1+b1,i111=r1+g1+b1;
      let a=table[i000],c00=a+fr*(table[i100]-a);
      a=table[i010];let c10=a+fr*(table[i110]-a);
      a=table[i001];let c01=a+fr*(table[i101]-a);
      a=table[i011];let c11=a+fr*(table[i111]-a);
      let c0=c00+fg*(c10-c00),c1=c01+fg*(c11-c01);
      const out0=scale*(c0+fb*(c1-c0));
      a=table[i000+1];c00=a+fr*(table[i100+1]-a);
      a=table[i010+1];c10=a+fr*(table[i110+1]-a);
      a=table[i001+1];c01=a+fr*(table[i101+1]-a);
      a=table[i011+1];c11=a+fr*(table[i111+1]-a);
      c0=c00+fg*(c10-c00);c1=c01+fg*(c11-c01);
      const out1=scale*(c0+fb*(c1-c0));
      a=table[i000+2];c00=a+fr*(table[i100+2]-a);
      a=table[i010+2];c10=a+fr*(table[i110+2]-a);
      a=table[i001+2];c01=a+fr*(table[i101+2]-a);
      a=table[i011+2];c11=a+fr*(table[i111+2]-a);
      c0=c00+fg*(c10-c00);c1=c01+fg*(c11-c01);
      const out2=scale*(c0+fb*(c1-c0));
      const x=full?Math.round(out0):Math.round(r*keep+out0);
      const y=full?Math.round(out1):Math.round(g*keep+out1);
      const z=full?Math.round(out2):Math.round(b*keep+out2);
      // Storing through the clamped array applies the same 0–255 clamp the pixels receive.
      pixels[p]=x;pixels[p+1]=y;pixels[p+2]=z;
      keys[slot]=key;values[slot]=pixels[p]|(pixels[p+1]<<8)|(pixels[p+2]<<16);
    }
    return imageData;
  }
  window.SpliceLUT={parseCube,validate,applyImageData,limits:{size:MAX_SIZE,size1D:MAX_SIZE_1D,chars:MAX_CHARS}};
})();
