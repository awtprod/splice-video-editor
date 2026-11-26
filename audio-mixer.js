// Splice preview mixer: the Web Audio side of the audio finishing chain.
//   clip:   media element -> channel map -> track channel layout -> clip effects -> clip gain (fades,
//           keyframes, track volume) -> pan -> track bus
//   track:  bus -> track EQ -> track gate/compressor -> master bus
//   master: bus -> master EQ -> master compressor -> loudness meter -> speakers
// EQ runs as IIRFilterNodes fed the same RBJ coefficients the renderer gives FFmpeg
// (audio-dsp.js / audio_dsp.py). Dynamics run in an AudioWorklet that ports FFmpeg's
// acompressor/agate. De-noise runs the same band-power subtraction on both paths.
(() => {
  const dsp = window.SpliceAudioDSP;
  let context = null, ready = null, workletLoaded = false;
  let master = null, meterNode = null, meterSnapshot = null, configured = null;
  const tracks = new Map();
  const reductions = new Map();
  const listeners = new Set();
  const fxKey = fx => JSON.stringify(fx || null);
  // Dynamics settings in the units the worklet takes; null when nothing is on.
  function dynamicsOf(fx) {
    const gate = fx?.gate && fx.gate.enabled !== false ? fx.gate : null;
    const comp = fx?.comp && fx.comp.enabled !== false ? fx.comp : null;
    if (!gate && !comp) return null;
    const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
    return {
      gate: gate && {threshold: number(gate.threshold, -50), ratio: number(gate.ratio, 4), attack: number(gate.attack, 2),
        release: number(gate.release, 120), range: number(gate.range, -24)},
      comp: comp && {threshold: number(comp.threshold, -18), ratio: number(comp.ratio, 3), attack: number(comp.attack, 10),
        release: number(comp.release, 120), knee: number(comp.knee, 6), makeup: number(comp.makeup, 0)},
    };
  }
  function attach(ctx) {
    if (context === ctx) return ready;
    context = ctx;
    master = {input: ctx.createGain(), nodes: [], key: null, owner: 'master'};
    master.output = ctx.createGain();
    master.input.connect(master.output);
    master.output.connect(ctx.destination);
    ready = ctx.audioWorklet.addModule('audio-dsp.js').then(() => {
      workletLoaded = true;
      meterNode = new AudioWorkletNode(ctx, 'splice-loudness', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2],
        processorOptions: {channels: 2}});
      meterNode.port.onmessage = ({data}) => { meterSnapshot = data; for (const listener of listeners) listener(data); };
      master.output.disconnect();
      master.output.connect(meterNode).connect(ctx.destination);
      // Rebuild every chain that waited for the dynamics processor.
      for (const bus of [master, ...tracks.values()]) if (bus.key !== null) { const key = bus.fx; bus.key = null; build(bus, key); }
    }).catch(error => { console.warn('Audio effects preview unavailable', error); });
    return ready;
  }
  function disposeNodes(bus) {
    for (const node of bus.nodes) {
      node.disconnect();
      if (node.port) { node.port.postMessage('dispose'); node.port.onmessage = null; }
    }
    bus.nodes = [];
    reductions.delete(bus.owner);
  }
  // A chain of effect nodes between `bus.input` and `bus.output`.
  function build(bus, fx) {
    const key = fxKey(fx) + (workletLoaded ? ':w' : '');
    if (bus.key === key) return;
    bus.key = key; bus.fx = fx;
    bus.input.disconnect();
    disposeNodes(bus);
    const chain = [];
    const dynamics = dynamicsOf(fx);
    // Order as in the renderer: (de-noise), gate, EQ, compressor.
    if (fx?.denoise && fx.denoise.enabled !== false && workletLoaded) chain.push(dynamicsNode(bus, {denoise: fx.denoise}));
    if (dynamics?.gate && workletLoaded) chain.push(dynamicsNode(bus, {gate: dynamics.gate}));
    for (const [b0,b1,b2,a1,a2] of dsp.eqSections(fx?.eq, context.sampleRate))
      chain.push(context.createIIRFilter([b0,b1,b2],[1,a1,a2]));
    if (dynamics?.comp && workletLoaded) chain.push(dynamicsNode(bus, {comp: dynamics.comp}));
    bus.nodes = chain;
    let node = bus.input;
    for (const next of chain) { node.connect(next); node = next; }
    node.connect(bus.output);
  }
  function dynamicsNode(bus, settings) {
    const node = new AudioWorkletNode(context, 'splice-dynamics', {numberOfInputs: 1, numberOfOutputs: 1, channelCount: 2, channelCountMode: 'clamped-max',
      processorOptions: settings});
    node.port.onmessage = ({data}) => {
      if (data?.type !== 'reduction') return;
      reductions.set(bus.owner, {...(reductions.get(bus.owner) || {}), ...data.reduction});
    };
    return node;
  }
  function trackBus(track) {
    const key = String(track);
    let bus = tracks.get(key);
    if (!bus) {
      bus = {input: context.createGain(), output: context.createGain(), nodes: [], key: null, owner: 'track:' + key};
      bus.input.connect(bus.output);
      bus.output.connect(master.input);
      tracks.set(key, bus);
      build(bus, configured?.trackMix?.[key]?.fx);
    }
    return bus;
  }
  // Called whenever the project changes: track and master effects.
  function configure(project) {
    if (!context) return;
    configured = project;
    build(master, project.masterFx);
    const used = new Set(project.clips.filter(clip => clip.type !== 'title').map(clip => String(clip.track)));
    for (const [key, bus] of tracks) {
      if (used.has(key)) { build(bus, project.trackMix?.[key]?.fx); continue; }
      // A track that no longer has clips: its bus and effects go (a clip moved back rebuilds it).
      bus.input.disconnect(); bus.output.disconnect(); disposeNodes(bus);
      tracks.delete(key);
    }
  }
  // The clip part of the chain, rebuilt only when the clip's effects or mapping change.
  const MAPS = {left: [[1,0],[1,0]], right: [[0,1],[0,1]], mono: [[.5,.5],[.5,.5]], swap: [[0,1],[1,0]]};
  function routeClip(route, clip, project, sourceChannels) {
    const mono = project.trackMix?.[String(clip.track)]?.channels === 'mono';
    const map = sourceChannels !== 1 && MAPS[clip.channelMap] ? clip.channelMap : 'stereo';
    const key = fxKey(clip.audioFx) + map + (mono ? ':mono' : '') + (workletLoaded ? ':w' : '');
    if (route.mixKey !== key) {
      route.mixKey = key;
      route.input.disconnect();
      if (route.mapNodes) for (const node of route.mapNodes) node.disconnect();
      route.mapNodes = [];
      if (!route.fxBus) {
        route.fxBus = {input: context.createGain(), output: context.createGain(), nodes: [], key: null, owner: 'clip:' + clip.id};
        route.fxBus.output.connect(route.gain);
      }
      route.fxBus.owner = 'clip:' + clip.id;
      if (MAPS[map]) {
        const split = context.createChannelSplitter(2), merge = context.createChannelMerger(2);
        route.input.connect(split);
        MAPS[map].forEach((row, out) => row.forEach((weight, input) => {
          if (!weight) return;
          const gain = context.createGain(); gain.gain.value = weight;
          split.connect(gain, input).connect(merge, 0, out);
          route.mapNodes.push(gain);
        }));
        merge.connect(route.fxBus.input);
        route.mapNodes.push(split, merge);
      } else route.input.connect(route.fxBus.input);
      // Dynamics must hear the same mono sum as the renderer, after channel mapping.
      route.fxBus.input.channelCount = mono ? 1 : 2;
      route.fxBus.input.channelCountMode = mono ? 'explicit' : 'max';
      build(route.fxBus, clip.audioFx);
    }
    const target = trackBus(clip.track).input;
    if (route.bus !== target) {
      route.analyser.disconnect();
      route.analyser.connect(target);
      route.bus = target;
    }
  }
  function releaseRoute(route) {
    if (route.fxBus) {
      route.fxBus.input.disconnect(); route.fxBus.output.disconnect();
      disposeNodes(route.fxBus);
    }
    for (const node of route.mapNodes || []) node.disconnect();
    route.mapNodes = []; route.fxBus = null; route.bus = null; route.mixKey = null;
  }
  window.SpliceMixer = {
    attach, configure, routeClip, releaseRoute,
    meter: () => meterSnapshot,
    resetMeter: () => meterNode?.port.postMessage('reset'),
    onMeter: listener => { listeners.add(listener); return () => listeners.delete(listener); },
    reduction: owner => reductions.get(owner) || null,
    // Live effect counts, for tests and the performance report.
    stats: () => ({tracks: tracks.size, iir: [master, ...tracks.values()].reduce((sum, bus) => sum + bus.nodes.length, 0),
      worklet: workletLoaded, sampleRate: context?.sampleRate || null}),
  };
})();
