/* Runs the browser LUT code under Node for the Python tests: JSON request on stdin, JSON reply on stdout. */
const fs = require('node:fs');
const path = require('node:path');

global.window = {};
(0, eval)(fs.readFileSync(path.join(__dirname, '..', 'lut.js'), 'utf8'));
const LUT = global.window.SpliceLUT;

// The preview's trilinear lookup as first written (before any table or cache), kept here as an
// independent reference: weights at single precision, lattice read from the plain array.
function reference(pixels, lut, intensity) {
  const {size, data, domainMin = [0,0,0], domainMax = [1,1,1]} = lut, mix = Math.max(0, Math.min(1, intensity/100));
  if (mix === 0) return;
  const axis = channel => Array.from({length:256}, (_, value) => {
    const position = Math.max(0, Math.min(size-1, (value/255-domainMin[channel])/(domainMax[channel]-domainMin[channel])*(size-1)));
    const low = Math.floor(position);
    return [low, Math.min(size-1, low+1), Math.fround(position-low)];
  });
  const [R, G, B] = [axis(0), axis(1), axis(2)], at = (r, g, b) => 3*(r+size*g+size*size*b);
  for (let p = 0; p < pixels.length; p += 4) {
    const [r0, r1, fr] = R[pixels[p]], [g0, g1, fg] = G[pixels[p+1]], [b0, b1, fb] = B[pixels[p+2]];
    const out = [0,1,2].map(c => {
      const c00 = data[at(r0,g0,b0)+c]+fr*(data[at(r1,g0,b0)+c]-data[at(r0,g0,b0)+c]);
      const c10 = data[at(r0,g1,b0)+c]+fr*(data[at(r1,g1,b0)+c]-data[at(r0,g1,b0)+c]);
      const c01 = data[at(r0,g0,b1)+c]+fr*(data[at(r1,g0,b1)+c]-data[at(r0,g0,b1)+c]);
      const c11 = data[at(r0,g1,b1)+c]+fr*(data[at(r1,g1,b1)+c]-data[at(r0,g1,b1)+c]);
      const c0 = c00+fg*(c10-c00), c1 = c01+fg*(c11-c01);
      return Math.round(pixels[p+c]*(1-mix)+255*mix*(c0+fb*(c1-c0)));
    });
    pixels[p] = out[0]; pixels[p+1] = out[1]; pixels[p+2] = out[2];
  }
}
// Deterministic test pictures, RGBA: smooth gradients (colours repeat, the cache serves them),
// gradients with grain, and uniform noise (nearly every pixel new).
function pictures(width, height, seed) {
  let state = seed >>> 0;
  const random = () => (state = (Math.imul(state, 1664525)+1013904223) >>> 0)/4294967296;
  const make = pixel => { const data = new Uint8ClampedArray(width*height*4); for (let y = 0, i = 0; y < height; y++) for (let x = 0; x < width; x++, i += 4) data.set([...pixel(x, y), 255], i); return data; };
  const smooth = (x, y) => [x*255/(width-1), y*255/(height-1), (x+y)*127/(width+height)];
  return {
    smooth:make((x, y) => smooth(x, y).map(Math.round)),
    grain:make((x, y) => smooth(x, y).map(value => Math.round(value+(random()-.5)*40))),
    noise:make(() => [random()*256, random()*256, random()*256].map(Math.floor)),
    // Neighbouring blue codes side by side, repeated, so a key that dropped a bit would collide.
    pairs:make(x => [(x*7)&255, (x*13)&255, x&255]),
  };
}

const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const reply = {};
try {
  if (request.op === 'parse') {
    reply.lut = LUT.parseCube(request.text, request.title);
  } else if (request.op === 'validate') {
    LUT.validate(request.lut, request.intensity);
  } else if (request.op === 'apply') {
    // Apply to RGB triples, one per pixel, exactly as the preview does to canvas pixels.
    const image = {data: new Uint8ClampedArray(request.pixels.flatMap(([r, g, b]) => [r, g, b, 255]))};
    LUT.applyImageData(image, request.lut, request.intensity, request.options);
    reply.pixels = request.pixels.map((_, index) => Array.from(image.data.slice(index*4, index*4+3)));
  } else if (request.op === 'curveError') {
    reply.error8 = LUT.curveError(request.lut);
  } else if (request.op === 'sequence') {
    // Frames go through one LUT the way playback sends them: the same lut.data array, with the
    // intensity, the domain and then the lattice changing between frames. Every frame is compared
    // byte for byte with the uncached path and with the reference implementation.
    const lattice = (size, f) => { const data = []; for (let b = 0; b < size; b++) for (let g = 0; g < size; g++) for (let r = 0; r < size; r++) data.push(...f(r/(size-1), g/(size-1), b/(size-1))); return data; };
    const warm = lattice(request.size, (r, g, b) => [Math.min(1, r**.8*1.05+.1*g), g**1.2*.9+.05*b, Math.max(0, b*.8+.15*r*g)]);
    const cool = lattice(request.size, (r, g, b) => [r*.9, Math.min(1, g*1.05), Math.min(1, b**.7)]);
    const lut = {size:request.size, data:warm, domainMin:[0,0,0], domainMax:[1,1,1]};
    const steps = [[100], [50], [50], [100, [0,.1,0], [1,.9,1.2]], [100, [0,0,0], [1,1,1]], [37, [-.1,0,.05], [1,1,1]], [37, [-.1,0,.05], [1,.95,1]], [37, null, null, cool], [100, [0,0,0], [1,1,1], cool]];
    const totals = {pixels:0, cached:0, reference:0, frames:0};
    for (const seed of request.seeds) {
      const frames = pictures(request.width, request.height, seed);
      for (const [intensity, min, max, data] of steps) {
        if (min) { lut.domainMin = min; lut.domainMax = max; }
        if (data) lut.data = data;
        for (const picture of Object.values(frames)) {
          const cached = picture.slice(), uncached = picture.slice(), expected = picture.slice();
          LUT.applyImageData({data:cached}, lut, intensity);
          LUT.applyImageData({data:uncached}, lut, intensity, {cache:false});
          reference(expected, lut, intensity);
          for (let i = 0; i < picture.length; i++) { if (cached[i] !== uncached[i]) totals.cached++; if (cached[i] !== expected[i]) totals.reference++; }
          totals.pixels += picture.length/4; totals.frames++;
        }
      }
      lut.data = warm; lut.domainMin = [0,0,0]; lut.domainMax = [1,1,1];
    }
    reply.totals = totals;
  } else if (request.op === 'error') {
    reply.curveError = LUT.curveError(request.lut);
  } else if (request.op === 'limits') {
    reply.limits = LUT.limits; reply.exportApplies1D = LUT.exportApplies1D;
  } else throw new Error('unknown op');
} catch (error) {
  reply.error = String(error.message || error);
}
process.stdout.write(JSON.stringify(reply));
