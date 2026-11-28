/* Effects the export refuses are refused in the editor too, with the export's own words: at load,
 * in an edit (updateClip) and on paste. A ramp on a multicam clip, stabilization of a nested or
 * multicam clip, and keyframes of a ninth mask. Also: the picture's "Ramp sound" choice reaches its
 * linked sound clip, at 1024x640 the Effects groups use the inspector's width, and an analysis no
 * clip uses any more (Reset, a deleted clip) stops on the server.
 */
const assert = require('node:assert/strict');
const path = require('node:path');
const {startSplice, openPage, ffmpeg} = require('./browser-harness.cjs');

const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

async function main() {
  const splice = await startSplice('splice-effects-consistency-');
  let pageErrors = [];
  try {
    const files = ['a', 'b'].map(name => path.join(splice.temp, name+'.mp4'));
    for (const file of files) ffmpeg(['-y', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=25', '-f', 'lavfi', '-i', 'sine=f=440:r=48000',
      '-t', '6', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-g', '1', '-c:a', 'aac', '-shortest', file]);
    const {page, errors} = await openPage(splice, {width:1440, height:900});
    pageErrors = errors;
    page.setDefaultTimeout(60000);
    await page.evaluate(() => SpliceSession.newProject({name:'Consistency', width:320, height:180, fps:25}));
    await page.locator('#media-input').setInputFiles(files);
    await page.waitForFunction(() => Editor.get().project.media.length === 2 && Editor.get().project.media.every(m => m.src && m.duration));
    const messages = await page.evaluate(() => SpliceEffects.MESSAGES);
    assert.equal(messages.multicamRamp, 'Speed ramps on multicam clips are not supported. Flatten the multicam clip first.');
    assert.equal(messages.stabilizeSource, 'Stabilize the clips inside a nested or multicam clip, or a video clip');
    const ninth = 'Invalid keyframes: mask.8.x (a clip has at most 8 masks)';

    // A multicam item and a nested sequence to put the refused effects on.
    const ids = await page.evaluate(() => {
      const [a, b] = Editor.get().project.media.map(m => m.id);
      Editor.editProject(p => { p.clips = [{id:'r', type:'video', mediaId:a, track:1, start:0, duration:2, in:.5, embeddedAudio:false, transition:'none', color:{}}]; });
      SpliceMulticam.createMulticam({angleMediaIds:[a, b]});
      Editor.select('r'); SpliceNested.nestSelection(['r'], {name:'Nest'});
      const media = Editor.get().project.media;
      return {a, b, multicam:media.find(m => m.multicam).id, nest:media.find(m => m.nestedProject).id};
    });
    const speed = [{time:0, value:100}, {time:2, value:150}];
    const stabilize = {stabilize:{analysis:'0'.repeat(32)}};
    const cases = [
      ['a ramp on a multicam clip', {mediaId:ids.multicam, multicamAngle:1, keyframes:{speed}}, messages.multicamRamp],
      ['stabilization of a multicam clip', {mediaId:ids.multicam, multicamAngle:1, effects:stabilize}, messages.stabilizeSource],
      ['stabilization of a bypassed multicam clip', {mediaId:ids.multicam, multicamAngle:1, effects:{...stabilize, bypass:true}}, messages.stabilizeSource],
      ['stabilization of a nested clip', {mediaId:ids.nest, effects:stabilize}, messages.stabilizeSource],
      ['keyframes of a ninth mask', {mediaId:ids.a, keyframes:{'mask.8.x':[{time:0, value:50}]}}, ninth],
    ];
    const base = await page.evaluate(() => structuredClone(Editor.get().project));
    const clip = extra => ({id:'c', type:'video', track:1, start:0, duration:2, in:0, embeddedAudio:false, transition:'none', color:{}, ...extra});

    // ---- 1. Load: each project is refused with the export's message; a plain multicam clip loads.
    const load = async (name, extra) => page.evaluate(async ({project, name}) => {
      project.name = name;
      try { await Editor.openProject(new File([JSON.stringify(project)], name+'.splice.json')); return Editor.get().project.name === name ? 'loaded' : 'not loaded'; }
      catch (error) { return error.message; }
    }, {project:{...base, clips:[clip(extra)]}, name});
    assert.equal(await load('plain multicam', {mediaId:ids.multicam, multicamAngle:1}), 'loaded');
    for (const [label, extra, message] of cases) {
      const result = await load(label, extra);
      assert.ok(result.includes(message), `load refuses ${label}: ${result}`);
    }

    // ---- 2. An edit (updateClip) is refused the same way and leaves the project as it was.
    await page.evaluate(async project => { await Editor.openProject(new File([JSON.stringify(project)], 'edits.splice.json')); },
      {...base, name:'edits', clips:[clip({id:'mc', mediaId:ids.multicam, multicamAngle:1}), clip({id:'nest', track:2, mediaId:ids.nest}),
        clip({id:'plain', track:3, mediaId:ids.a})]});
    for (const [label, extra, message] of cases) {
      const target = extra.mediaId === ids.multicam ? 'mc' : extra.mediaId === ids.nest ? 'nest' : 'plain';
      const result = await page.evaluate(({target, patch}) => {
        const before = JSON.stringify(Editor.get().project);
        try { Editor.updateClip(target, patch); return {accepted:true}; }
        catch (error) { return {message:error.message, unchanged:JSON.stringify(Editor.get().project) === before}; }
      }, {target, patch:{...(extra.keyframes ? {keyframes:extra.keyframes} : {}), ...(extra.effects ? {effects:extra.effects} : {})}});
      assert.ok(!result.accepted && result.message.includes(message), `updateClip refuses ${label}: ${JSON.stringify(result)}`);
      assert.ok(result.unchanged, `a refused edit changes nothing (${label})`);
    }

    // ---- 3. Paste: a ramped clip copied from plain media, pasted after that media became multicam.
    const template = base.media.find(m => m.multicam).multicam;
    await page.evaluate(async project => { await Editor.openProject(new File([JSON.stringify(project)], 'paste.splice.json')); },
      {...base, name:'paste', media:base.media.filter(m => !m.multicam && !m.nestedProject),
        clips:[clip({id:'ramped', mediaId:ids.b, keyframes:{speed}, duration:1.6})]});
    await page.evaluate(() => { Editor.select('ramped'); document.activeElement?.blur(); });
    await settle(page);
    await page.keyboard.press('Control+c');
    await page.evaluate(({ids, template}) => Editor.editProject(p => {
      p.clips = [];
      p.media.find(m => m.id === ids.b).multicam = {...template, angles:template.angles.map(angle => ({...angle, mediaId:ids.a}))};
    }), {ids, template});
    const before = await page.evaluate(() => Editor.get().project.clips.length);
    await page.keyboard.press('Control+v');
    await settle(page);
    const pasted = await page.evaluate(() => ({clips:Editor.get().project.clips.length, notice:document.body.innerText}));
    assert.equal(pasted.clips, before, 'the paste is refused');
    assert.ok(pasted.notice.includes(messages.multicamRamp), 'the paste tells why, in the export\'s words');

    // ---- 4. The picture's Ramp sound reaches its linked sound clip (one undo step for both).
    await page.evaluate(async project => { await Editor.openProject(new File([JSON.stringify(project)], 'linked.splice.json')); },
      {...base, name:'linked', clips:[
        clip({id:'pic', mediaId:ids.a, linkGroup:'g', keyframes:{speed}}),
        {id:'snd', type:'audio', mediaId:ids.a, track:-1, start:0, duration:2, in:0, linkGroup:'g', transition:'none', keyframes:{speed}}]});
    await page.evaluate(() => Editor.select('pic'));
    await settle(page);
    const group = page.locator('[data-fx-group="speed"]');
    if (await group.locator('.fx-body').evaluate(node => node.hidden)) await group.locator('[data-fx-summary]').click();
    await page.selectOption('[data-fx-speed-audio]', 'varispeed');
    await page.waitForFunction(() => Editor.get().project.clips.every(c => c.effects?.speedAudio === 'varispeed'));
    await page.evaluate(() => Editor.undo());
    assert.deepEqual(await page.evaluate(() => Editor.get().project.clips.map(c => c.effects?.speedAudio ?? null)), [null, null],
      'one undo restores both clips');

    // ---- 5. At 1024x640 the Effects groups use the inspector's width: "Custom bezier" shows in full.
    await page.setViewportSize({width:1024, height:640});
    await page.evaluate(() => {
      Editor.updateClip('pic', {keyframes:{...Editor.get().project.clips.find(c => c.id === 'pic').keyframes,
        opacity:[{time:0, value:20, interp:'bezier', outHandle:[.25, .9]}, {time:1.5, value:100, inHandle:[.7, .4]}]}});
      Editor.select('pic'); Editor.seek(.5);
    });
    await settle(page);
    const easing = page.locator('[data-fx-group="easing"]');
    if (await easing.locator('.fx-body').evaluate(node => node.hidden)) await easing.locator('[data-fx-summary]').click();
    await page.selectOption('[data-fx-ease-key]', 'opacity');
    await settle(page);
    await easing.scrollIntoViewIfNeeded();
    await settle(page);
    const fit = await page.evaluate(() => {
      const panel = document.getElementById('inspector-content').getBoundingClientRect();
      const tools = document.querySelector('[data-fx-tools]').getBoundingClientRect();
      const selects = [...document.querySelectorAll('[data-fx-group="easing"] select')].map(select => {
        const style = getComputedStyle(select), context = document.createElement('canvas').getContext('2d');
        context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
        const text = select.options[select.selectedIndex].text;
        // Room for the text: the select's content box less the arrow (about 20 px).
        return {text, need:context.measureText(text).width, room:select.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)-20};
      });
      return {inset:tools.left-panel.left, right:panel.right-tools.right, selects};
    });
    assert.ok(fit.inset <= 20, `the Effects groups start at the inspector's padding (${fit.inset.toFixed(0)} px)`);
    for (const select of fit.selects) assert.ok(select.need <= select.room, `"${select.text}" fits: ${select.need.toFixed(0)} <= ${select.room.toFixed(0)} px`);
    assert.ok(fit.selects.some(select => select.text === 'Custom bezier'), JSON.stringify(fit.selects));

    // ---- 6. An analysis no clip uses any more stops on the server: Reset, then a deleted clip.
    if (await page.evaluate(async () => (await (await fetch('/api/effects/capabilities')).json()).vidstab)) {
      const long = path.join(splice.temp, 'long.mp4');
      ffmpeg(['-y', '-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=25', '-t', '30', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '1', long]);
      await page.locator('#media-input').setInputFiles([long]);
      await page.waitForFunction(() => Editor.get().project.media.some(m => m.name === 'long.mp4' && m.duration));
      await page.evaluate(() => {
        const id = Editor.get().project.media.find(m => m.name === 'long.mp4').id;
        Editor.editProject(p => { p.clips.push(
          {id:'s1', type:'video', mediaId:id, track:3, start:0, duration:12, in:0, embeddedAudio:false, transition:'none', color:{}},
          {id:'s2', type:'video', mediaId:id, track:4, start:0, duration:12, in:14, embeddedAudio:false, transition:'none', color:{}}); });
      });
      const serverState = id => page.evaluate(async id => (await (await fetch('/api/effects/stabilize?id='+id)).json()).state, id);
      const analyze = id => page.evaluate(async id => {
        await SpliceEffectsUI.analyze(Editor, Editor.get().project.clips.find(c => c.id === id), () => {});
        return Editor.get().project.clips.find(c => c.id === id).effects.stabilize.analysis;
      }, id);
      const first = await analyze('s1');
      assert.equal(await serverState(first), 'running');
      await page.evaluate(() => Editor.select('s1'));
      await settle(page);
      const stab = page.locator('[data-fx-group="stabilize"]');
      if (await stab.locator('.fx-body').evaluate(node => node.hidden)) await stab.locator('[data-fx-summary]').click();
      await stab.locator('[data-fx-stabilize-reset]').click();
      const waitCancelled = async id => {
        const deadline = Date.now()+15000;
        let state;
        while (Date.now() < deadline && (state = await serverState(id)) !== 'cancelled') await page.waitForTimeout(200);
        return state;
      };
      assert.equal(await waitCancelled(first), 'cancelled', 'Reset cancels the running analysis');
      const second = await analyze('s2');
      assert.equal(await serverState(second), 'running');
      await page.evaluate(() => Editor.deleteClips(['s2']));
      assert.equal(await waitCancelled(second), 'cancelled', 'deleting the clip cancels its analysis');
    }
    assert.deepEqual(errors, []);
    console.log('Effects consistency: load, edit and paste refuse what the export refuses; linked Ramp sound; 1024x640 layout; analyses cancelled.');
  } finally {
    await splice.stop();
  }
}

main().catch(error => { console.error(error); process.exit(1); });
