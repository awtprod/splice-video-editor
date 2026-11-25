/* Runs the browser LUT code under Node for the Python tests: JSON request on stdin, JSON reply on stdout. */
const fs = require('node:fs');
const path = require('node:path');

global.window = {};
(0, eval)(fs.readFileSync(path.join(__dirname, '..', 'lut.js'), 'utf8'));
const LUT = global.window.SpliceLUT;

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
    LUT.applyImageData(image, request.lut, request.intensity);
    reply.pixels = request.pixels.map((_, index) => Array.from(image.data.slice(index*4, index*4+3)));
  } else if (request.op === 'limits') {
    reply.limits = LUT.limits;
  } else throw new Error('unknown op');
} catch (error) {
  reply.error = String(error.message || error);
}
process.stdout.write(JSON.stringify(reply));
