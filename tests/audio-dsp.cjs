/* audio-dsp.js without a browser: the loudness meter against FFmpeg's ebur128 (integrated,
 * loudness range, max momentary and short-term within 0.3 LU, true peak within 0.3 dB) on test
 * programs, and the compressor/gate port against FFmpeg acompressor/agate sample by sample. */
const assert = require('node:assert/strict');
const {execFileSync, spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const dsp = require('../audio-dsp.js');

const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(process.env.SPLICE_TEMP_DIR || os.tmpdir(), 'splice-dsp-'));
process.on('exit', () => fs.rmSync(temp, {recursive: true, force: true}));
function decode(file, af) {
  const raw = execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-i', file, ...(af ? ['-af', af] : []), '-ac', '2', '-ar', '48000',
    '-f', 'f64le', '-c:a', 'pcm_f64le', '-'], {maxBuffer: 1 << 30});
  const all = new Float64Array(raw.buffer, raw.byteOffset, raw.length/8);
  const left = new Float32Array(all.length/2), right = new Float32Array(all.length/2);
  for (let i = 0; i < left.length; i++) { left[i] = all[2*i]; right[i] = all[2*i+1]; }
  return [left, right];
}
function ebur128(file) {
  const log = path.join(temp, 'frames.txt');
  const err = spawnSync('ffmpeg', ['-nostdin', '-nostats', '-v', 'info', '-i', file, '-af',
    `ebur128=peak=true:framelog=quiet:metadata=1,ametadata=mode=print:file=${log}`, '-f', 'null', '-'], {encoding: 'utf8'}).stderr;
  const summary = err.slice(err.lastIndexOf('Integrated loudness:'));
  const frames = fs.readFileSync(log, 'utf8');
  const max = key => Math.max(-Infinity, ...[...frames.matchAll(new RegExp(`lavfi\\.r128\\.${key}=(-?[\\d.]+)`, 'g'))].map(m => +m[1]));
  const integrated = [...frames.matchAll(/lavfi\.r128\.I=(-?[\d.]+|-inf)/g)].at(-1)[1];
  return {integrated: integrated === '-inf' ? -Infinity : +integrated, range: +summary.match(/LRA:\s+(-?[\d.]+)/)[1],
    truePeak: +(summary.match(/Peak:\s+(-?[\d.]+|-inf)/)[1].replace('-inf', '-Infinity')), maxMomentary: max('M'), maxShortTerm: max('S')};
}
function meter(file) {
  const [left, right] = decode(file);
  const m = new dsp.LoudnessMeter(48000, 2);
  for (let i = 0; i < left.length; i += 128) { const n = Math.min(128, left.length - i); m.process([left.subarray(i, i+n), right.subarray(i, i+n)], n); }
  return m.snapshot();
}
const program = (name, graph, seconds) => {
  const file = path.join(temp, name + '.wav');
  execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-f', 'lavfi', '-i', graph, '-t', String(seconds), '-ac', '2', '-ar', '48000', '-c:a', 'pcm_f32le', file]);
  return file;
};
const speech = path.join(temp, 'speech.wav');
execFileSync('python3', [path.join(root, 'tests', 'audio_fixtures.py'), 'speech', speech, '30', '5']);
const programs = {
  sine: program('sine', "aevalsrc='0.25*sin(2*PI*997*t)|0.25*sin(2*PI*997*t)':s=48000", 15),
  pink: program('pink', 'anoisesrc=c=pink:a=0.3:r=48000:seed=3', 20),
  speech,
  music: program('music', "aevalsrc='0.2*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*0.2*t))+0.1*sin(2*PI*440*t)*(1+sin(t))+0.05*sin(2*PI*3000*t)|" +
    "0.2*sin(2*PI*165*t)+0.1*sin(2*PI*660*t)*(1+cos(t*0.7))':s=48000", 30),
  silence: program('silence', 'anullsrc=r=48000:cl=stereo', 10),
  gated: program('gated', "aevalsrc='if(lt(mod(t,10),5),0.5*sin(2*PI*1000*t),0.003*sin(2*PI*1000*t))|if(lt(mod(t,10),5),0.5*sin(2*PI*1000*t),0.003*sin(2*PI*1000*t))':s=48000", 40),
};
const results = {};
for (const [name, file] of Object.entries(programs)) {
  const ours = meter(file), ffmpeg = ebur128(file);
  results[name] = {ours, ffmpeg};
  if (name === 'silence') {
    assert.equal(ours.integrated, -Infinity);
    assert.ok(ffmpeg.integrated <= -70); // FFmpeg uses its absolute gate for digital silence.
    assert.equal(ffmpeg.truePeak, -Infinity);
    continue;
  }
  for (const key of ['integrated', 'range', 'maxMomentary', 'maxShortTerm'])
    assert.ok(Math.abs(ours[key] - ffmpeg[key]) <= .3, `${name} ${key}: meter ${ours[key].toFixed(2)}, ebur128 ${ffmpeg[key]}`);
  assert.ok(Math.abs(ours.truePeak - ffmpeg.truePeak) <= .3, `${name} true peak: meter ${ours.truePeak.toFixed(2)}, ebur128 ${ffmpeg.truePeak}`);
}
// Dynamics: the preview's port against FFmpeg on the same samples.
const source = program('dyn', "aevalsrc='(0.6*gt(mod(t,1),0.5)+0.05)*sin(2*PI*440*t)|(0.6*gt(mod(t,1),0.5)+0.05)*sin(2*PI*440*t+1)':s=48000", 4);
const filters = JSON.parse(execFileSync('python3', ['-c', `
import json, audio_dsp
c = {'threshold': -24, 'ratio': 4, 'attack': 5, 'release': 80, 'knee': 6, 'makeup': 3}
g = {'threshold': -30, 'ratio': 6, 'attack': 1, 'release': 60, 'range': -30}
print(json.dumps({'comp': [audio_dsp.ffmpeg_filters({'comp': audio_dsp.compressor(c)})[0], c], 'gate': [audio_dsp.ffmpeg_filters({'gate': audio_dsp.gate(g)})[0], g]}))`],
  {cwd: root, encoding: 'utf8'}));
for (const [kind, [filter, settings]] of Object.entries(filters)) {
  const [left, right] = decode(source), [wantLeft] = decode(source, filter);
  const stage = kind === 'comp' ? dsp.compressorState(settings, 48000) : dsp.gateState(settings, 48000);
  for (let i = 0; i < left.length; i += 128) { const n = Math.min(128, left.length - i); dsp.runDynamics([stage], [left.subarray(i, i+n), right.subarray(i, i+n)], n); }
  let worst = 0;
  for (let i = 0; i < left.length; i++) worst = Math.max(worst, Math.abs(left[i] - wantLeft[i]));
  results[kind] = {maxAbsDifference: worst};
  assert.ok(worst < 1e-6, `${kind}: preview port differs from FFmpeg by ${worst}`);
}
if (process.env.SPLICE_EVIDENCE_DIR) fs.writeFileSync(path.join(process.env.SPLICE_EVIDENCE_DIR, 'meter-vs-ebur128.json'), JSON.stringify(results, null, 2));
console.log('audio DSP checks passed', JSON.stringify(Object.fromEntries(Object.entries(results).map(([k, v]) => [k, v.ours ?
  {integrated: [v.ours.integrated.toFixed(2), v.ffmpeg.integrated], range: [v.ours.range.toFixed(2), v.ffmpeg.range]} : v]))));
