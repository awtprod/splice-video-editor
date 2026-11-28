/* Runs effects.js under Node for the Python tests (tests/test_effects.py).
 * Request on stdin: {calls:[[name, ...args], ...]}; reply: {results:[...]} in the same order. */
const fs = require('node:fs');
const path = require('node:path');
global.window = {};
(0, eval)(fs.readFileSync(path.join(__dirname, '..', 'effects.js'), 'utf8'));
const E = global.window.SpliceEffects;
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = request.calls.map(([name, ...args]) => {
  try { return {value:E[name](...args)}; }
  catch (error) { return {error:error.message}; }
});
process.stdout.write(JSON.stringify({results}));
