/* Manual benchmark (not part of npm test): node tests/lut-bench.cjs [path/to/lut.js ...]
 * Applies a LUT to real 960x540 frames decoded from the bundled sample clips, as the preview does. */
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const WIDTH = 960, HEIGHT = 540, FRAME = WIDTH*HEIGHT*4, FRAMES = 6;
const median = values => values.slice().sort((a, b) => a-b)[values.length >> 1];

function lutOfSize(size) {
  const data = [];
  for (let b = 0; b < size; b++) for (let g = 0; g < size; g++) for (let r = 0; r < size; r++) {
    const x = r/(size-1), y = g/(size-1), z = b/(size-1);
    data.push(Math.min(1, x*1.1), y**.9, z*.95+.02);
  }
  return {size, data, domainMin:[0,0,0], domainMax:[1,1,1]};
}
function load(file) {
  global.window = {};
  (0, eval)(fs.readFileSync(file, 'utf8'));
  return global.window.SpliceLUT;
}

for (const file of process.argv.length > 2 ? process.argv.slice(2) : [path.join(root, 'lut.js')]) {
  for (const clip of ['ridge', 'forest']) {
    const raw = execFileSync('ffmpeg', ['-nostdin','-v','error','-ss','1','-i',path.join(root,'assets',clip+'.mp4'),
      '-vf',`scale=${WIDTH}:${HEIGHT}`,'-frames:v',String(FRAMES),'-pix_fmt','rgba','-f','rawvideo','pipe:1'], {maxBuffer:FRAME*FRAMES*2});
    const frames = Array.from({length:FRAMES}, (_, index) => raw.subarray(index*FRAME, (index+1)*FRAME));
    for (const size of [33, 65]) {
      const LUT = load(file);
      if (size > (LUT.limits?.size ?? 33)) continue;            // an older lut.js stops at 33 points
      const lut = lutOfSize(size), time = frame => {
        const image = {data:new Uint8ClampedArray(frame)}, start = process.hrtime.bigint();
        LUT.applyImageData(image, lut, 100);
        return Number(process.hrtime.bigint()-start)/1e6;
      };
      const cold = time(frames[0]);                                    // first frame this LUT has seen
      const playback = frames.slice(1).concat(frames, frames).map(time); // later frames of the same clip
      const paused = Array.from({length:20}, () => time(frames[2]));     // the same frame again and again
      console.log(`${path.basename(path.dirname(file)) || '.'}/${path.basename(file)}  ${clip}  ${size}-point  first frame ${cold.toFixed(1)} ms  playback median ${median(playback).toFixed(1)} ms  paused median ${median(paused).toFixed(1)} ms`);
    }
  }
}
