/* Runs the browser grade code (color-grade.js, lut.js) under Node for the Python tests.
 * Request on stdin: {op:'tables', color} | {op:'apply', color, inputLut, pixels:[[r,g,b],...]} |
 * {op:'keyed', clip, local, pixels} | {op:'cube', color, size} | {op:'validate', grade}. Reply on stdout. */
const fs = require('node:fs');
const path = require('node:path');
global.window = {};
for (const file of ['lut.js', 'color-grade.js']) (0, eval)(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'));
const G = global.window.SpliceGrade;
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const plain = value => value && (value instanceof Float32Array ? Array.from(value) : value);
function grade(prepared, pixel) {
  const rgb = pixel.map(value => value/255);
  G.evaluate(prepared, rgb);
  return rgb.map(value => Math.round(value*255));
}
function reply() {
  switch (request.op) {
    case 'tables': {
      const t = G.tables(request.color);
      return {pre:plain(t.pre), matrix:t.matrix, post:t.post && t.post.map(plain), hueSat:plain(t.hueSat)};
    }
    case 'apply': {
      const prepared = G.prepare(request.color, request.inputLut || null);
      return {pixels:request.pixels.map(pixel => grade(prepared, pixel))};
    }
    case 'keyed': {
      // Two gradings mixed by time, as the monitor does between grade keyframes.
      const segment = G.keySegment(request.clip, request.local);
      const a = G.prepare(G.colorAt(request.clip, segment.a)), b = G.prepare(G.colorAt(request.clip, segment.b));
      return {segment, pixels:request.pixels.map(pixel => {
        const x = pixel.map(v => v/255), y = pixel.map(v => v/255);
        G.evaluate(a, x); G.evaluate(b, y);
        return x.map((value, c) => Math.round(255*(value+(y[c]-value)*segment.weight)));
      })};
    }
    case 'cube': return {text:G.cubeText(request.color, request.size)};
    case 'validate':
      try { G.validateGrade(request.grade); return {ok:true}; } catch (error) { return {error:error.message}; }
    case 'parse': {
      try { return {lut:global.window.SpliceLUT.parseCube(request.text)}; } catch (error) { return {error:error.message}; }
    }
    default: throw Error('Unknown op ' + request.op);
  }
}
process.stdout.write(JSON.stringify(reply()));
