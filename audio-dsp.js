// Splice audio DSP, browser side. One implementation of each formula:
//  - RBJ cookbook biquads (same formulas as audio_dsp.py; the renderer feeds
//    its coefficients to FFmpeg's biquad filter, the preview feeds these to
//    IIRFilterNodes; tests/test_audio_parity.py checks both against each other)
//  - a port of FFmpeg's acompressor/agate per-sample algorithm (the export is
//    the authority; the measured preview tolerance is in tests/audio-dsp.cjs)
//  - ITU-R BS.1770-4 / EBU R128 loudness: K-weighting, gated integrated
//    loudness, momentary/short-term, loudness range (EBU Tech 3342) and a 4x
//    oversampled true-peak estimate.
// This file runs in three places: the page (window.SpliceAudioDSP), the
// AudioWorkletGlobalScope (registers the processors) and Node (tests).
(function(scope) {
  const NYQUIST_GUARD = .49;
  const EQ_TYPES = ['highpass','lowpass','lowshelf','highshelf','peak'];

  function biquad(kind, freq, gainDb, q, rate) {
    freq = Math.min(freq, NYQUIST_GUARD*rate);
    const w0 = 2*Math.PI*freq/rate, cosw = Math.cos(w0), sinw = Math.sin(w0);
    const alpha = sinw/(2*q), amp = 10**(gainDb/40);
    let b, a;
    if (kind==='lowpass') { b=[(1-cosw)/2,1-cosw,(1-cosw)/2]; a=[1+alpha,-2*cosw,1-alpha]; }
    else if (kind==='highpass') { b=[(1+cosw)/2,-(1+cosw),(1+cosw)/2]; a=[1+alpha,-2*cosw,1-alpha]; }
    else if (kind==='peak') { b=[1+alpha*amp,-2*cosw,1-alpha*amp]; a=[1+alpha/amp,-2*cosw,1-alpha/amp]; }
    else if (kind==='lowshelf') {
      const root=2*Math.sqrt(amp)*alpha;
      b=[amp*((amp+1)-(amp-1)*cosw+root),2*amp*((amp-1)-(amp+1)*cosw),amp*((amp+1)-(amp-1)*cosw-root)];
      a=[(amp+1)+(amp-1)*cosw+root,-2*((amp-1)+(amp+1)*cosw),(amp+1)+(amp-1)*cosw-root];
    } else if (kind==='highshelf') {
      const root=2*Math.sqrt(amp)*alpha;
      b=[amp*((amp+1)+(amp-1)*cosw+root),-2*amp*((amp-1)+(amp+1)*cosw),amp*((amp+1)+(amp-1)*cosw-root)];
      a=[(amp+1)-(amp-1)*cosw+root,2*((amp-1)-(amp+1)*cosw),(amp+1)-(amp-1)*cosw-root];
    } else throw Error('Invalid EQ band type');
    return [b[0]/a[0],b[1]/a[0],b[2]/a[0],a[1]/a[0],a[2]/a[0]];
  }
  // Same rules as audio_dsp.eq_bands: hp/lp ignore gain, 0 dB bells/shelves are skipped.
  function eqBands(eq) {
    if (!eq || eq.enabled===false || !Array.isArray(eq.bands)) return [];
    return eq.bands.filter(band=>band && band.enabled!==false && EQ_TYPES.includes(band.type) &&
      (band.type==='highpass' || band.type==='lowpass' || Math.abs(Number(band.gain)||0)>1e-9))
      .slice(0,8).map(band=>[band.type,Math.min(20000,Math.max(20,Number(band.freq)||1000)),
        band.type==='highpass'||band.type==='lowpass'?0:Math.max(-24,Math.min(24,Number(band.gain)||0)),
        Math.max(.1,Math.min(18,Number(band.q)||.7071))]);
  }
  const eqSections = (eq, rate) => eqBands(eq).map(([kind,freq,gain,q])=>biquad(kind,freq,gain,q,rate));
  function magnitudeDb(sections, freq, rate) {
    const w=2*Math.PI*freq/rate;
    let total=1;
    for (const [b0,b1,b2,a1,a2] of sections) {
      const nr=b0+b1*Math.cos(w)+b2*Math.cos(2*w), ni=-b1*Math.sin(w)-b2*Math.sin(2*w);
      const dr=1+a1*Math.cos(w)+a2*Math.cos(2*w), di=-a1*Math.sin(w)-a2*Math.sin(2*w);
      total*=Math.hypot(nr,ni)/Math.hypot(dr,di);
    }
    return 20*Math.log10(Math.max(total,1e-30));
  }

  // ---- Dynamics: FFmpeg acompressor / agate (peak detection, linked by maximum) ----
  const MAX_KNEE = 8;
  function hermite(x, x0, x1, p0, p1, m0, m1) {
    const width=x1-x0, t=(x-x0)/width;
    m0*=width; m1*=width;
    const t2=t*t, t3=t2*t;
    const ct2=-3*p0-2*m0+3*p1-m1, ct3=2*p0+m0-2*p1+m1;
    return ct3*t3+ct2*t2+m0*t+p0;
  }
  // settings in user units (dB, ms); returns a per-sample processor state.
  function compressorState(settings, rate) {
    const threshold=Math.max(10**(settings.threshold/20),0.000976563), ratio=settings.ratio;
    const knee=Math.min(MAX_KNEE,Math.max(1,10**((settings.knee??6)/20)));
    const s={kind:'comp',lin:0,ratio,makeup:10**((settings.makeup||0)/20),thres:Math.log(threshold),knee,
      attack:Math.min(1,1/(settings.attack*rate/4000)),release:Math.min(1,1/(settings.release*rate/4000)),
      linKneeStart:threshold/Math.sqrt(knee),linKneeStop:threshold*Math.sqrt(knee),reduction:1};
    s.kneeStart=Math.log(s.linKneeStart); s.kneeStop=Math.log(s.linKneeStop);
    s.compressedKneeStop=(s.kneeStop-s.thres)/ratio+s.thres;
    return s;
  }
  function compressorGain(s, level) {
    s.lin+=(level-s.lin)*(level>s.lin?s.attack:s.release);
    if (!(s.lin>0 && s.lin>s.linKneeStart)) return 1;
    const slope=Math.log(s.lin);
    let gain=(slope-s.thres)/s.ratio+s.thres;
    if (s.knee>1 && slope<s.kneeStop)
      gain=hermite(slope,s.kneeStart,s.kneeStop,s.kneeStart,s.compressedKneeStop,1,1/s.ratio);
    return Math.exp(gain-slope);
  }
  const GATE_KNEE = 2.828427125;
  function gateState(settings, rate) {
    const threshold=10**(settings.threshold/20);
    const s={kind:'gate',lin:0,ratio:settings.ratio,range:10**(settings.range/20),thres:Math.log(threshold),
      attack:Math.min(1,1/(settings.attack*rate/4000)),release:Math.min(1,1/(settings.release*rate/4000)),
      linKneeStart:threshold/Math.sqrt(GATE_KNEE),linKneeStop:threshold*Math.sqrt(GATE_KNEE),reduction:1};
    s.kneeStart=Math.log(s.linKneeStart); s.kneeStop=Math.log(s.linKneeStop);
    return s;
  }
  function gateGain(s, level) {
    s.lin+=(level-s.lin)*(level>s.lin?s.attack:s.release);
    if (!(s.lin>0 && s.lin<s.linKneeStop)) return 1;
    const slope=Math.log(s.lin), ratio=s.ratio;
    let gain=(slope-s.thres)*ratio+s.thres;
    if (s.kneeStart<s.kneeStop && slope>s.kneeStart)
      gain=hermite(slope,s.kneeStart,s.kneeStop,(s.kneeStart-s.thres)*ratio+s.thres,s.kneeStop,ratio,1);
    return Math.max(s.range,Math.exp(gain-slope));
  }
  // Process interleaved-by-channel arrays in place: channels = [Float32Array, ...].
  function runDynamics(stages, channels, length) {
    for (let n=0;n<length;n++) {
      for (const s of stages) {
        let level=0;
        for (const data of channels) level=Math.max(level,Math.abs(data[n]));
        const gain=s.kind==='comp'?compressorGain(s,level)*s.makeup:gateGain(s,level);
        if (s.kind==='comp') s.reduction=Math.min(s.reduction,gain/s.makeup); else s.reduction=Math.min(s.reduction,gain);
        for (const data of channels) data[n]*=gain;
      }
    }
  }

  // ---- Loudness (ITU-R BS.1770-4, EBU R128 / Tech 3341, 3342) ----
  // K-weighting as two biquads for any sample rate (the analogue prototype of
  // the standard's 48 kHz coefficients).
  function kWeighting(rate) {
    let f0=1681.974450955533, G=3.999843853973347, Q=.7071752369554196;
    let K=Math.tan(Math.PI*f0/rate);
    const Vh=10**(G/20), Vb=Vh**.4996667741545416, a0=1+K/Q+K*K;
    const shelf=[(Vh+Vb*K/Q+K*K)/a0,2*(K*K-Vh)/a0,(Vh-Vb*K/Q+K*K)/a0,2*(K*K-1)/a0,(1-K/Q+K*K)/a0];
    f0=38.13547087602444; Q=.5003270373238773; K=Math.tan(Math.PI*f0/rate);
    const d=1+K/Q+K*K;
    const highpass=[1,-2,1,2*(K*K-1)/d,(1-K/Q+K*K)/d];
    return [shelf,highpass];
  }
  // Channel weights for L R C LFE Ls Rs (LFE excluded).
  const WEIGHTS = {1:[1],2:[1,1],6:[1,1,1,0,1.41,1.41]};
  // 4x interpolation for the true-peak estimate: windowed sinc, 12 taps per phase.
  const TAPS = 12, PHASES = 4;
  const interpolation = (() => {
    const table=[];
    for (let phase=0;phase<PHASES;phase++) {
      const row=[];
      for (let k=0;k<TAPS;k++) {
        const x=(k-TAPS/2+1)-phase/PHASES; // offset in input samples
        const sinc=x===0?1:Math.sin(Math.PI*x)/(Math.PI*x);
        const window=.5+.5*Math.cos(Math.PI*x/(TAPS/2+.5));
        row.push(sinc*window);
      }
      const sum=row.reduce((a,b)=>a+b,0);
      table.push(row.map(v=>v/sum));
    }
    return table;
  })();
  const LOG_OFFSET = -.691;
  const energyToLufs = energy => energy>0 ? LOG_OFFSET+10*Math.log10(energy) : -Infinity;
  class LoudnessMeter {
    constructor(rate, channels=2) {
      this.rate=rate; this.channels=channels;
      this.weights=WEIGHTS[channels]||Array(channels).fill(1);
      this.filters=kWeighting(rate);
      this.step=Math.round(rate/10); // 100 ms
      this.reset();
    }
    reset() {
      this.state=Array.from({length:this.channels},()=>[[0,0,0,0],[0,0,0,0]]);
      this.history=Array.from({length:this.channels},()=>[0,0,0,0,0,0,0,0,0,0,0,0]);
      this.stepEnergy=0; this.stepCount=0; this.steps=[]; // per-100 ms weighted energy sums, last 30 kept
      this.blocks=[]; // gated 400 ms block energies (integrated)
      this.shortTerm=[]; // 3 s values every 100 ms (LRA)
      this.momentary=-Infinity; this.short=-Infinity; this.maxMomentary=-Infinity; this.maxShort=-Infinity;
      this.truePeak=0; this.samplePeak=0; this.elapsed=0;
    }
    // channels: array of Float32Array; processes `length` samples in exact 100 ms steps.
    process(channels, length) {
      let offset=0;
      while (offset<length) {
        const count=Math.min(length-offset,this.step-this.stepCount);
        this.stepEnergy+=this._segment(channels,offset,offset+count);
        this.stepCount+=count; this.elapsed+=count; offset+=count;
        if (this.stepCount===this.step) {
          this.steps.push(this.stepEnergy);
          this.stepEnergy=0; this.stepCount=0;
          if (this.steps.length>30) this.steps.shift();
          this._update();
        }
      }
    }
    _segment(channels, from, to) {
      const [shelf,high]=this.filters;
      let energy=0;
      for (let c=0;c<this.channels;c++) {
        const data=channels[c]||channels[0], weight=this.weights[c];
        const [s1,s2]=this.state[c], hist=this.history[c];
        let peak=this.truePeak, sp=this.samplePeak;
        for (let n=from;n<to;n++) {
          const x=data[n];
          // true peak: shift the 12-sample history, interpolate the 3 in-between phases
          hist.copyWithin(0,1); hist[TAPS-1]=x;
          const ax=Math.abs(x); if (ax>sp) sp=ax;
          for (let p=1;p<PHASES;p++) {
            const row=interpolation[p]; let v=0;
            for (let k=0;k<TAPS;k++) v+=row[k]*hist[k];
            const av=Math.abs(v); if (av>peak) peak=av;
          }
          if (ax>peak) peak=ax;
          if (!weight) continue;
          // direct form I, both stages
          const y=shelf[0]*x+shelf[1]*s1[0]+shelf[2]*s1[1]-shelf[3]*s1[2]-shelf[4]*s1[3];
          s1[1]=s1[0]; s1[0]=x; s1[3]=s1[2]; s1[2]=y;
          const z=high[0]*y+high[1]*s2[0]+high[2]*s2[1]-high[3]*s2[2]-high[4]*s2[3];
          s2[1]=s2[0]; s2[0]=y; s2[3]=s2[2]; s2[2]=z;
          energy+=weight*z*z;
        }
        this.truePeak=peak; this.samplePeak=sp;
      }
      return energy;
    }
    _mean(count) {
      if (this.steps.length<count) return null;
      let sum=0; for (let i=this.steps.length-count;i<this.steps.length;i++) sum+=this.steps[i];
      return sum/(count*this.step);
    }
    _update() {
      const block=this._mean(4);
      if (block!==null) {
        this.momentary=energyToLufs(block);
        this.maxMomentary=Math.max(this.maxMomentary,this.momentary);
        if (this.momentary>-70) this.blocks.push(block);
      }
      const short=this._mean(30);
      if (short!==null) {
        this.short=energyToLufs(short);
        this.maxShort=Math.max(this.maxShort,this.short);
        if (this.short>-70) this.shortTerm.push(short);
      }
    }
    integrated() {
      if (!this.blocks.length) return -Infinity;
      const mean=this.blocks.reduce((a,b)=>a+b,0)/this.blocks.length;
      const gate=10**((energyToLufs(mean)-10-LOG_OFFSET)/10);
      const kept=this.blocks.filter(v=>v>=gate);
      return kept.length?energyToLufs(kept.reduce((a,b)=>a+b,0)/kept.length):-Infinity;
    }
    range() {
      if (this.shortTerm.length<2) return 0;
      const mean=this.shortTerm.reduce((a,b)=>a+b,0)/this.shortTerm.length;
      const gate=10**((energyToLufs(mean)-20-LOG_OFFSET)/10);
      const kept=this.shortTerm.filter(v=>v>=gate).map(energyToLufs).sort((a,b)=>a-b);
      if (kept.length<2) return 0;
      const at=p=>kept[Math.min(kept.length-1,Math.max(0,Math.round(p*(kept.length-1))))];
      return at(.95)-at(.10);
    }
    snapshot() {
      const db=v=>v>0?20*Math.log10(v):-Infinity;
      return {momentary:this.momentary,shortTerm:this.short,integrated:this.integrated(),range:this.range(),
        maxMomentary:this.maxMomentary,maxShortTerm:this.maxShort,truePeak:db(this.truePeak),samplePeak:db(this.samplePeak),
        seconds:this.elapsed/this.rate};
    }
  }

  // Effect settings: the same bounds as audio_dsp.py (the renderer re-validates).
  const BOUNDS = {
    comp: {threshold:[-60,0,-18], ratio:[1,20,3], attack:[.01,2000,10], release:[.01,9000,120], knee:[0,20*Math.log10(8),6], makeup:[0,36,0]},
    gate: {threshold:[-80,0,-50], ratio:[1,9000,4], attack:[.01,9000,2], release:[.01,9000,120], range:[-90,0,-24]},
    denoise: {amount:[1,40,12], floor:[-80,-20,-50]},
    band: {freq:[20,20000,1000], q:[.1,18,.7071], gain:[-24,24,0]},
  };
  function validateFx(fx, kinds=['eq','comp','gate','denoise']) {
    if (fx==null) return;
    if (typeof fx!=='object' || Array.isArray(fx) || Object.keys(fx).some(key=>!kinds.includes(key)))
      throw Error('Invalid audio effects: use '+kinds.join(', '));
    const check=(section,bounds,label)=>{
      if (!section || typeof section!=='object' || Array.isArray(section)) throw Error(`Invalid ${label} settings`);
      if (section.enabled!==undefined && typeof section.enabled!=='boolean') throw Error(`Invalid ${label} setting: enabled must be true or false`);
      for (const [key,[low,high]] of Object.entries(bounds))
        if (section[key]!==undefined && !(Number.isFinite(section[key]) && section[key]>=low && section[key]<=high))
          throw Error(`Invalid ${label} ${key}: use a number from ${low} to ${+high.toFixed(2)}`);
    };
    if (fx.eq!==undefined) {
      check(fx.eq,{},'EQ');
      if (!Array.isArray(fx.eq.bands) || fx.eq.bands.length>8) throw Error('Invalid EQ: use at most 8 bands');
      for (const band of fx.eq.bands) {
        check(band,BOUNDS.band,'EQ band');
        if (!EQ_TYPES.includes(band.type)) throw Error('Invalid EQ band type');
        if (!Number.isFinite(band.freq)) throw Error('Invalid EQ band frequency');
      }
    }
    for (const kind of ['comp','gate','denoise']) if (fx[kind]!==undefined) check(fx[kind],BOUNDS[kind],kind==='comp'?'compressor':kind==='denoise'?'de-noise':'gate');
  }
  // Automation points: [{time, value, ease?}] within bounds, at distinct times.
  function validateAutomation(points, low, high, limit, label) {
    if (!Array.isArray(points) || points.length>limit) throw Error(`Invalid ${label} keyframes: at most ${limit}`);
    const times=new Set();
    for (const point of points) {
      if (!point || !Number.isFinite(point.time) || point.time<0 || !Number.isFinite(point.value) || point.value<low || point.value>high ||
          (point.ease!==undefined && typeof point.ease!=='boolean') || times.has(point.time))
        throw Error(`Invalid ${label} keyframe: value ${low} to ${high}, one per time`);
      times.add(point.time);
    }
  }
  // Keyframe curve (engine.js and the renderer's _curve_expr): linear or eased (smoothstep) segments.
  function curveValue(points, local, base) {
    if (!points.length) return base;
    if (local<=points[0].time) return points[0].value;
    for (let i=1;i<points.length;i++) if (local<=points[i].time) {
      const a=points[i-1], b=points[i];
      let u=(local-a.time)/(b.time-a.time);
      if (a.ease) u=u*u*(3-2*u);
      return a.value+(b.value-a.value)*u;
    }
    return points[points.length-1].value;
  }

  // Same complementary bands and band-power subtraction as audio_dsp.noise_filter.
  class NoiseReducer {
    constructor(settings, rate, channels) {
      this.low = 1-Math.exp(-2*Math.PI*350/rate);
      this.wide = 1-Math.exp(-2*Math.PI*3500/rate);
      this.smoothing = 1-Math.exp(-1/(.008*rate));
      const a=this.low, b=this.wide, low=a/(2-a), wide=b/(2-b);
      const power=2*10**((settings.floor ?? -50)/10);
      this.noise=[low, wide+low-2*a*b/(a+b-a*b), 1+wide-2*b].map(fraction => fraction*power);
      this.minimum=10**(-(settings.amount ?? 12)/20);
      this.state=Array.from({length:channels}, () => new Float64Array(5));
    }
    process(channels) {
      for (let ch=0; ch<channels.length; ch++) {
        const samples=channels[ch], state=this.state[ch];
        for (let i=0; i<samples.length; i++) {
          const input=samples[i];
          state[0]+=this.low*(input-state[0]);
          state[1]+=this.wide*(input-state[1]);
          let output=0;
          for (let band=0; band<3; band++) {
            const value=band===0 ? state[0] : band===1 ? state[1]-state[0] : input-state[1];
            const power=state[band+2]+=this.smoothing*(value*value-state[band+2]);
            output+=value*Math.max(this.minimum,1-this.noise[band]/Math.max(1e-20,power));
          }
          samples[i]=output;
        }
      }
    }
  }

  const api = {NoiseReducer,biquad,validateFx,validateAutomation,curveValue,BOUNDS,eqBands,eqSections,magnitudeDb,compressorState,compressorGain,gateState,gateGain,runDynamics,
    kWeighting,LoudnessMeter,EQ_TYPES,energyToLufs};

  // ---- AudioWorklet processors ----
  if (typeof registerProcessor==='function' && typeof AudioWorkletProcessor!=='undefined') {
    class DynamicsProcessor extends AudioWorkletProcessor {
      // Settings arrive with the node (processorOptions, so the first quantum is already
      // processed) and later through the port when the user edits them.
      constructor(options) {
        super();
        this.stages=[]; this.alive=true; this.sent=0;
        this.configure(options && options.processorOptions);
        this.port.onmessage=({data})=>{
          if (data==='dispose') { this.alive=false; this.port.close(); return; }
          if (data && data.type==='settings') this.configure(data);
        };
      }
      configure(data) {
        if (!data) return;
        const stages=[];
        this.noise=data.denoise ? new NoiseReducer(data.denoise,sampleRate,2) : null;
        if (data.gate) stages.push(gateState(data.gate,sampleRate));
        if (data.comp) stages.push(compressorState(data.comp,sampleRate));
        // Keep the detector state of an unchanged stage kind so edits do not click.
        for (const stage of stages) { const prior=this.stages.find(s=>s.kind===stage.kind); if (prior) stage.lin=prior.lin; }
        this.stages=stages;
      }
      process(inputs, outputs) {
        const input=inputs[0], output=outputs[0];
        if (!input.length) { for (const channel of output) channel.fill(0); return this.alive; }
        for (let c=0;c<output.length;c++) output[c].set(input[Math.min(c,input.length-1)]);
        this.noise?.process(output);
        if (this.stages.length) runDynamics(this.stages,output,output[0].length);
        if (currentTime-this.sent>.05) {
          this.sent=currentTime;
          const reduction={};
          for (const s of this.stages) { reduction[s.kind]=20*Math.log10(Math.max(1e-6,s.reduction)); s.reduction=1; }
          this.port.postMessage({type:'reduction',reduction});
        }
        return this.alive;
      }
    }
    class MeterProcessor extends AudioWorkletProcessor {
      constructor(options) {
        super();
        this.channels=(options.processorOptions&&options.processorOptions.channels)||2;
        this.meter=new LoudnessMeter(sampleRate,this.channels); this.alive=true; this.sent=0;
        this.port.onmessage=({data})=>{
          if (data==='reset') this.meter.reset();
          if (data==='dispose') { this.alive=false; this.port.close(); }
        };
      }
      process(inputs, outputs) {
        const input=inputs[0], output=outputs[0];
        for (let c=0;c<output.length;c++) {
          if (input.length) output[c].set(input[Math.min(c,input.length-1)]); else output[c].fill(0);
        }
        if (input.length) {
          const channels=[]; for (let c=0;c<this.channels;c++) channels.push(input[Math.min(c,input.length-1)]);
          this.meter.process(channels,channels[0].length);
        }
        if (currentTime-this.sent>.1) {
          this.sent=currentTime;
          this.port.postMessage(this.meter.snapshot());
        }
        return this.alive;
      }
    }
    registerProcessor('splice-dynamics',DynamicsProcessor);
    registerProcessor('splice-loudness',MeterProcessor);
  }
  if (typeof module!=='undefined' && module.exports) module.exports=api;
  else if (typeof window!=='undefined') window.SpliceAudioDSP=api;
})(typeof globalThis!=='undefined'?globalThis:this);
