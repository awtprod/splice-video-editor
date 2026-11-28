/* The Effects inspector, mask handles, the eyedropper, the alpha matte view, stabilization and
 * unnesting with effects, driven like a user (clicks, drags, keys), with undo, save and reopen.
 * Screenshots of the inspector, the curve editor, the mask overlay, the chroma key and the
 * stabilization controls at 1440x900, 1180x780 and 1024x640 go to SPLICE_PROOF_DIR/screens when set.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {startSplice, openPage, ffmpeg} = require('./browser-harness.cjs');

const W = 640, H = 360;
function source(file, graph, seconds = 4) {
  ffmpeg(['-y', '-f', 'lavfi', '-i', graph, '-f', 'lavfi', '-i', 'sine=f=440:r=48000', '-t', String(seconds),
    '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-c:v', 'libx264', '-qp', '0', '-g', '1',
    '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-c:a', 'aac', '-shortest', file]);
}
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const project = page => page.evaluate(() => Editor.get().project);
const clipById = (page, id) => page.evaluate(id => Editor.get().project.clips.find(c => c.id === id), id);

async function main() {
  const splice = await startSplice('splice-effects-controls-');
  const proof = process.env.SPLICE_PROOF_DIR, screens = proof && path.join(proof, 'screens');
  if (screens) fs.mkdirSync(screens, {recursive:true});
  let pageErrors = [];
  try {
    const files = {bg:path.join(splice.temp, 'bg.mp4'), green:path.join(splice.temp, 'green.mp4'), shaky:path.join(splice.temp, 'shaky.mp4')};
    source(files.bg, `testsrc2=s=${W}x${H}:r=25`);
    source(files.green, `color=c=0x20c040:s=${W}x${H}:r=25,drawbox=x=240:y=110:w=160:h=140:c=0xc04060:t=fill`);
    source(files.shaky, "testsrc2=s=960x540:r=25,crop=640:360:x='160+60*(random(1)-0.5)':y='90+40*(random(2)-0.5)'", 5);
    const {page, errors} = await openPage(splice, {width:1440, height:900});
    pageErrors = errors;
    page.setDefaultTimeout(60000);
    await page.evaluate(({W, H}) => SpliceSession.newProject({name:'Effects controls', width:W, height:H, fps:25}), {W, H});
    await page.locator('#media-input').setInputFiles(Object.values(files));
    await page.waitForFunction(() => Editor.get().project.media.length === 3 && Editor.get().project.media.every(item => item.src && item.duration));
    await page.evaluate(() => {
      const id = name => Editor.get().project.media.find(item => item.name === name+'.mp4').id;
      Editor.editProject(p => {
        p.clips = [
          {id:'bg', type:'video', mediaId:id('bg'), track:1, start:0, duration:3, in:0, embeddedAudio:false, transition:'none', color:{}},
          {id:'key', type:'video', mediaId:id('green'), track:2, start:0, duration:3, in:0, embeddedAudio:false, transition:'none', color:{}},
          {id:'ramp', type:'video', mediaId:id('bg'), track:1, start:4, duration:2, in:.5, embeddedAudio:false, transition:'none', color:{}},
          {id:'shaky', type:'video', mediaId:id('shaky'), track:1, start:14, duration:3, in:1, embeddedAudio:false, transition:'none', color:{}}];
      });
    });
    const inspector = page.locator('#inspector-content, .inspector-content').first();
    const openGroup = async name => {
      const group = page.locator(`[data-fx-group="${name}"]`);
      if (await group.locator('.fx-body').evaluate(node => node.hidden)) await group.locator('[data-fx-summary]').click();
      return group;
    };
    const select = async (id, time) => { await page.evaluate(({id, time}) => { Editor.select(id); if (time != null) Editor.seek(time); }, {id, time}); await settle(page); };

    // ---- 1. Speed ramp: a preset keeps the source In and Out, undo restores the clip, ticks mark the keys.
    await select('ramp', 4.2);
    await openGroup('speed');
    const before = await clipById(page, 'ramp');
    await page.locator('[data-fx-preset="smoothSlowMo"]').click();
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'ramp').keyframes?.speed?.length === 4);
    const ramped = await clipById(page, 'ramp');
    const span = await page.evaluate(clip => SpliceEffects.sourceDelta(clip, 0, clip.duration), ramped);
    assert.ok(Math.abs(span-before.duration) < 1/25+1e-6, `source span kept: ${span} vs ${before.duration}`);
    assert.ok(ramped.duration > before.duration*1.5, 'a slow-motion ramp lengthens the clip');
    assert.ok(Math.abs(ramped.speed-span/ramped.duration) < 1e-4, 'the nominal speed is the average');
    await page.waitForFunction(() => document.querySelectorAll('.timeline-clip[data-clip="ramp"] .fx-knot-strip i').length >= 3);
    await page.locator('[data-fx-knot="1"]').fill('40');
    await page.locator('[data-fx-knot="1"]').dispatchEvent('change');
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'ramp').keyframes.speed[1].value === 40);
    await page.selectOption('[data-fx-speed-audio]', 'varispeed');
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'ramp').effects?.speedAudio === 'varispeed');
    if (screens) await page.screenshot({path:path.join(screens, 'speed-ramp-1440x900.png')});
    for (let i = 0; i < 3; i++) await page.evaluate(() => Editor.undo());
    assert.deepEqual((await clipById(page, 'ramp')).keyframes?.speed, undefined, 'undo removes the ramp');
    assert.equal((await clipById(page, 'ramp')).duration, before.duration, 'and restores the duration');
    await page.evaluate(() => Editor.redo());
    assert.equal((await clipById(page, 'ramp')).keyframes.speed.length, 4, 'redo brings it back');
    // The monitor plays the ramp on the same curve: the video element seeks to in + S(t).
    await page.evaluate(() => Editor.seek(4+1.1));
    await page.waitForFunction(() => { const v = Editor.get().video; return v && !v.seeking && v.readyState >= 2; });
    const seeked = await page.evaluate(() => { const c = Editor.get().project.clips.find(c => c.id === 'ramp'); return {want:c.in+SpliceEffects.rampSource(SpliceEffects.rampPoints(c), 1.1), got:Editor.get().video.currentTime}; });
    assert.ok(Math.abs(seeked.want-seeked.got) < .05, JSON.stringify(seeked));
    console.log('Speed ramp: preset keeps the source span, knots edit, sound mode, ticks, undo/redo, monitor seeks on the curve.');

    // ---- 2. Crop with a keyframe, easing and the curve editor.
    await select('key', .5);
    await openGroup('crop');
    const left = page.locator('input[data-fx="crop.left"]');
    await left.fill('10'); await left.dispatchEvent('change');
    await page.locator('[data-fx-key="crop.left"]').click();
    await page.evaluate(() => Editor.seek(2)); await settle(page);
    await page.locator('input[data-fx="crop.left"]').fill('40'); await page.locator('input[data-fx="crop.left"]').dispatchEvent('input');
    await page.locator('input[data-fx="crop.left"]').dispatchEvent('change');
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'key').keyframes?.['crop.left']?.length === 2);
    await openGroup('easing');
    await page.selectOption('[data-fx-ease-key]', 'crop.left');
    await page.locator('[data-fx-ease]').waitFor({timeout:5000}).catch(async error => {
      console.error('easing group:', await page.evaluate(() => document.querySelector('[data-fx-group="easing"]')?.outerHTML.slice(0, 600)),
        JSON.stringify(await page.evaluate(() => Editor.get().project.clips.find(c => c.id === 'key').keyframes)));
      throw error;
    });
    await page.selectOption('[data-fx-ease]', 'bezier');
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'key').keyframes['crop.left'][0].interp === 'bezier');
    const beforeDrag = JSON.stringify((await clipById(page, 'key')).keyframes['crop.left']);
    const handle = page.locator('.fx-handle.live[data-fx-handle="out"]');
    await handle.scrollIntoViewIfNeeded();
    const box = await handle.boundingBox();
    await page.mouse.move(box.x+box.width/2, box.y+box.height/2);
    // Up and to the right: a fast start (the default ease-in-out handle is at 0.42, 0).
    await page.mouse.down(); await page.mouse.move(box.x+box.width/2+12, box.y+box.height/2-40, {steps:5}); await page.mouse.up();
    const eased = (await clipById(page, 'key')).keyframes['crop.left'][0];
    assert.ok(eased.outHandle[1] > .2 && eased.outHandle[0] > .42, 'dragging the handle reshapes the curve: '+JSON.stringify(eased));
    if (screens) await page.locator('[data-fx-group="easing"]').screenshot({path:path.join(screens, 'curve-editor-1440x900.png')});
    const mid = await page.evaluate(() => { const c = Editor.get().project.clips.find(c => c.id === 'key'); return SpliceEffects.curveValue(c.keyframes['crop.left'], 1.25, 0); });
    assert.ok(mid > 26, 'the reshaped curve rises early: '+mid);
    await page.evaluate(() => Editor.undo());   // the drag is one undo step
    assert.equal(JSON.stringify((await clipById(page, 'key')).keyframes['crop.left']), beforeDrag, 'one undo reverts the whole drag');
    console.log('Easing: keyframe from the crop control, Custom bezier, handle drag in the curve editor, one undo step.');

    // ---- 3. Masks: add, handles on the monitor, drag the centre, undo.
    await page.evaluate(() => { const c = Editor.get().project.clips.find(c => c.id === 'key'); const {['crop.left']:_, ...k} = c.keyframes; Editor.updateClip('key', {keyframes:k, effects:{}}); });
    await select('key', 1);
    await openGroup('masks');
    await page.locator('[data-fx-mask-add="ellipse"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.fx-overlay .fx-mh').length >= 5);
    const centre = await page.locator('.fx-overlay .fx-mh.centre').boundingBox();
    await page.mouse.move(centre.x+centre.width/2, centre.y+centre.height/2);
    await page.mouse.down(); await page.mouse.move(centre.x+centre.width/2+60, centre.y+centre.height/2+20, {steps:5}); await page.mouse.up();
    const moved = (await clipById(page, 'key')).effects.masks[0];
    assert.ok(moved.x > 55 && moved.y > 51, 'dragging the centre handle moves the mask: '+JSON.stringify(moved));
    const size = await page.locator('.fx-overlay .fx-mh[data-mh="size"]').first().boundingBox();
    await page.mouse.move(size.x+5, size.y+5); await page.mouse.down(); await page.mouse.move(size.x+40, size.y+30, {steps:4}); await page.mouse.up();
    assert.ok((await clipById(page, 'key')).effects.masks[0].width > 50, 'a corner handle sizes the mask');
    if (screens) await page.screenshot({path:path.join(screens, 'mask-overlay-1440x900.png')});
    await page.evaluate(() => Editor.undo());
    assert.ok(Math.abs((await clipById(page, 'key')).effects.masks[0].width-50) < 1e-6, 'undo restores the size');
    // The overlay is DOM over the monitor: the picture itself has no handle pixels.
    const overlayInPicture = await page.evaluate(() => getComputedStyle(document.querySelector('.fx-overlay')).position === 'absolute' &&
      !document.querySelector('#preview-canvas').contains(document.querySelector('.fx-overlay')));
    assert.ok(overlayInPicture);
    await page.locator('[data-fx-mask-delete="0"]').click();
    await page.waitForFunction(() => !Editor.get().project.clips.find(c => c.id === 'key').effects?.masks);
    console.log('Masks: added, handles drawn over the monitor, centre and corner drags, undo, delete.');

    // ---- 4. Chroma key: eyedropper on the monitor, alpha matte view, blend mode.
    await openGroup('key');
    await page.locator('[data-fx-eyedropper]').click();
    const canvasBox = await page.locator('#preview-canvas').boundingBox();
    await page.mouse.click(canvasBox.x+canvasBox.width*.1, canvasBox.y+canvasBox.height*.15);
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'key').effects?.key?.color);
    const picked = (await clipById(page, 'key')).effects.key.color;
    const rgb = [1, 3, 5].map(i => parseInt(picked.slice(i, i+2), 16));
    assert.ok(Math.abs(rgb[0]-32) < 8 && Math.abs(rgb[1]-192) < 8 && Math.abs(rgb[2]-64) < 8, 'the eyedropper samples the green screen: '+picked);
    await page.locator('[data-fx-alpha-view]').check();
    await page.waitForFunction(() => Editor.monitor.alphaView === 'key');
    await settle(page);
    const matte = await page.evaluate(() => {
      const c = document.querySelector('#preview-canvas'), ctx = c.getContext('2d');
      const at = (u, v) => [...ctx.getImageData(Math.round(c.width*u), Math.round(c.height*v), 1, 1).data].slice(0, 3);
      return {screen:at(.1, .15), subject:at(.5, .5)};
    });
    assert.ok(Math.max(...matte.screen) < 30 && Math.min(...matte.subject) > 225, 'alpha matte view: '+JSON.stringify(matte));
    if (screens) await page.screenshot({path:path.join(screens, 'chroma-key-alpha-1440x900.png')});
    await page.locator('[data-fx-alpha-view]').uncheck();
    await openGroup('blend');
    await page.selectOption('[data-fx-blend]', 'screen');
    await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'key').effects.blend === 'screen');
    console.log('Chroma key: eyedropper from the clip under the click, alpha matte view (monitor only), blend mode.');

    // ---- 5. Stabilization: analyze in the background, then the monitor plays the stabilized preview.
    await select('shaky', 15);
    await openGroup('stabilize');
    await page.waitForFunction(() => SpliceEffectsUI.capabilities.checked);
    const vidstab = await page.evaluate(() => SpliceEffectsUI.capabilities.vidstab);
    if (vidstab) {
      await page.locator('[data-fx-analyze]').click();
      await page.waitForFunction(() => { const s = Editor.get().project.clips.find(c => c.id === 'shaky').effects?.stabilize; return s && SpliceEffectsUI.jobs.get(s.analysis)?.state === 'done'; }, null, {timeout:120000})
        .catch(async error => { console.error('stabilize:', await page.evaluate(() => JSON.stringify([...SpliceEffectsUI.jobs], null, 0)+' '+JSON.stringify(Editor.get().project.clips.find(c => c.id === 'shaky').effects))); throw error; });
      await page.waitForFunction(() => /shake/.test(document.querySelector('[data-fx-group="stabilize"]').textContent));
      await page.waitForFunction(() => /\/media\/\.effects\/stabilize\//.test(Editor.get().video?.currentSrc || ''), null, {timeout:120000});
      const smoothing = page.locator('input[data-fx="stabilize.smoothing"]');
      await smoothing.fill('25'); await smoothing.dispatchEvent('input'); await smoothing.dispatchEvent('change');
      await page.waitForFunction(() => Editor.get().project.clips.find(c => c.id === 'shaky').effects.stabilize.smoothing === 25);
      if (screens) await page.screenshot({path:path.join(screens, 'stabilization-1440x900.png')});
    } else {
      assert.match(await page.locator('[data-fx-group="stabilize"]').textContent(), /libvidstab/);
    }
    console.log(`Stabilization: ${vidstab ? 'analyzed in the background, preview on the monitor, settings saved' : 'FFmpeg lacks vidstab: the inspector says so'}.`);

    // ---- 6. Save and reopen: every effect persists.
    const saved = await project(page);
    const download = page.waitForEvent('download');
    await page.evaluate(() => SpliceSession.saveAs('splice'));
    const file = await (await download).path();
    const text = JSON.parse(fs.readFileSync(file, 'utf8'));
    for (const id of ['ramp', 'key', 'shaky']) {
      const a = saved.clips.find(c => c.id === id), b = text.clips.find(c => c.id === id);
      assert.deepEqual(b.effects ?? null, a.effects ?? null, id+' effects saved');
      assert.deepEqual(b.keyframes ?? null, a.keyframes ?? null, id+' keyframes saved');
    }
    await page.evaluate(() => SpliceSession.newProject({name:'Blank', width:640, height:360, fps:25}));
    await page.locator('#project-input').setInputFiles(file);
    await page.waitForFunction(() => Editor.get().project.clips.some(c => c.id === 'ramp' && c.keyframes?.speed));
    const reopened = await project(page);
    assert.deepEqual(reopened.clips.find(c => c.id === 'key').effects, saved.clips.find(c => c.id === 'key').effects, 'reopened with its effects');
    console.log('Persistence: effects and ramp keyframes survive save and reopen.');

    // ---- 7. Unnest: outer effects copied intact when lossless (bypassed ones too), otherwise refused.
    const unnest = async (outerEffects, outerKeys, childEffects) => page.evaluate(async ({outerEffects, outerKeys, childEffects}) => {
      const id = name => Editor.get().project.media.find(item => item.name === name+'.mp4').id;
      Editor.editProject(p => { p.clips = [{id:'c1', type:'video', mediaId:id('bg'), track:1, start:0, duration:2, in:0, embeddedAudio:false, transition:'none', color:{},
        ...(childEffects ? {effects:childEffects} : {})}]; });
      SpliceNested.nestSelection(['c1'], {name:'Nest'});
      const outer = Editor.get().project.clips.find(c => c.type === 'video');
      const edited = JSON.stringify(Editor.get().project);
      try { Editor.updateClip(outer.id, {...(outerEffects ? {effects:outerEffects} : {}), ...(outerKeys ? {keyframes:outerKeys} : {})}); }
      catch (error) { return {ok:false, refusedEdit:true, message:error.message, unchanged:JSON.stringify(Editor.get().project) === edited}; }
      const before = JSON.stringify(Editor.get().project);
      try {
        const out = SpliceNested.unnestClip(outer.id).filter(c => c.type === 'video');
        return {ok:true, effects:out.map(c => c.effects), keys:out.map(c => c.keyframes)};
      } catch (error) { return {ok:false, message:error.message, unchanged:JSON.stringify(Editor.get().project) === before}; }
    }, {outerEffects, outerKeys, childEffects});
    const masks = {masks:[{shape:'ellipse', x:40, y:50, width:30, height:40}], blend:'multiply', crop:{left:10, right:0, top:0, bottom:0}};
    let result = await unnest(masks);
    assert.ok(result.ok, JSON.stringify(result));
    assert.deepEqual(result.effects[0], masks, 'outer effects are copied intact onto the unnested clip');
    result = await unnest({...masks, bypass:true, key:{color:'#00ff00', enabled:false}});
    assert.ok(result.ok && result.effects[0].bypass === true && result.effects[0].key.enabled === false, 'bypassed and disabled effects are carried, never dropped');
    result = await unnest(null, {speed:[{time:0, value:100}, {time:1, value:50}]});
    assert.ok(!result.ok && /Cannot unnest losslessly: outer speed ramp/.test(result.message) && result.unchanged, JSON.stringify(result));
    // Stabilization never reaches a nested clip: the edit is refused with the export's words.
    result = await unnest({stabilize:{analysis:'0'.repeat(32), enabled:false}});
    assert.ok(!result.ok && result.refusedEdit && result.message.includes(await page.evaluate(() => SpliceEffects.MESSAGES.stabilizeSource)) &&
      result.unchanged, JSON.stringify(result));
    result = await unnest(masks, null, {blend:'screen'});
    assert.ok(!result.ok && /Cannot unnest losslessly/.test(result.message) && result.unchanged, 'a child with its own effects keeps the nest: '+JSON.stringify(result));
    console.log('Unnest: outer effects copied intact (bypassed too) over a plain child; ramps or effected children refuse and leave the project unchanged; stabilizing a nest is refused at the edit.');

    // ---- 8. Screenshots at three sizes: the panels must not overlap or clip.
    await page.locator('#project-input').setInputFiles(file);
    await page.waitForFunction(() => Editor.get().project.clips.some(c => c.id === 'ramp' && c.keyframes?.speed));
    for (const [width, height] of [[1440, 900], [1180, 780], [1024, 640]]) {
      await page.setViewportSize({width, height});
      await select('key', 1);
      await page.evaluate(() => Editor.updateClip('key', {effects:{...Editor.get().project.clips.find(c => c.id === 'key').effects,
        masks:[{shape:'polygon', x:50, y:50, width:100, height:100, points:[[-30, -30], [30, -25], [25, 30], [-28, 25]]}]},
        keyframes:{opacity:[{time:0, value:20, interp:'bezier', outHandle:[.25, .9]}, {time:2, value:100, inHandle:[.7, .4]}]}}));
      for (const group of ['crop', 'masks', 'key', 'blend', 'easing', 'stabilize']) await openGroup(group).catch(() => {});
      await settle(page);
      const overflow = await page.evaluate(() => {
        const panel = document.querySelector('[data-fx-tools]'), bounds = panel.getBoundingClientRect(), out = [];
        for (const node of panel.querySelectorAll('button, input, select, output, svg')) {
          const r = node.getBoundingClientRect();
          if (r.width && (r.right > bounds.right+1 || r.left < bounds.left-1)) out.push(node.outerHTML.slice(0, 80));
        }
        return out;
      });
      assert.deepEqual(overflow, [], `nothing in the Effects panel spills out of it at ${width}x${height}: ${overflow.join(" | ")}`);
      if (screens) {
        // Each group scrolled into the inspector's view, with the monitor beside it.
        const shot = async (id, time, group, name) => {
          await select(id, time); const node = await openGroup(group); await node.scrollIntoViewIfNeeded(); await settle(page);
          await page.screenshot({path:path.join(screens, `${name}-${width}x${height}.png`)});
        };
        await shot('ramp', 4.5, 'speed', 'speed-ramp');
        await shot('key', 1, 'masks', 'mask-overlay');
        await shot('key', 1, 'easing', 'curve-editor');
        await shot('key', 1, 'key', 'chroma-key');
        await shot('key', 1, 'blend', 'effects-inspector');
        await shot('shaky', 15, 'stabilize', 'stabilization');
      }
    }
    assert.deepEqual(errors, []);
    console.log('Effects controls: all flows pass at 1440x900, 1180x780 and 1024x640.');
  } catch (error) {
    if (pageErrors.length) console.error('Page errors:', pageErrors);
    throw error;
  } finally {
    await splice.stop();
  }
}
main().catch(error => { console.error(error); process.exit(1); });
