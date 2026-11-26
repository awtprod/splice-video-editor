(() => {
  // Video scopes read the program monitor canvas, which already carries the grade
  // (exposure, white balance, LUT). They sample at most 960 columns and at most 960x540 pixels
  // (rows and columns picked evenly, no blending), so a 9:16 or 1:1 picture costs what 16:9 does.
  const LEVELS = 256, COLUMNS = 480, MAX_SAMPLE_WIDTH = 960, MAX_SAMPLES = 960*540, VECTOR_CELLS = 128, VECTOR_RANGE = .55;
  const KINDS = [['waveform','Waveform'],['parade','RGB parade'],['vector','Vectorscope'],['histogram','Histogram']];
  const HEIGHT = {min:120, max:420, step:10};
  const CHANNELS = [[255,110,110],[110,255,130],[110,160,255]];
  const TRACE = [190,255,190];
  const BACKGROUND = '#0c110d';
  const SKIN_DEGREES = 123;
  // Rec. 709: chroma axes are (B-Y)/1.8556 and (R-Y)/1.5748, luma weights below.
  const KR = .2126, KG = .7152, KB = .0722, CB_SCALE = 1.8556, CR_SCALE = 1.5748;
  // Colour-bar targets: the six 75 % bars, in the order they appear around the scope.
  const TARGETS = [['R',[1,0,0]],['Mg',[1,0,1]],['B',[0,0,1]],['Cy',[0,1,1]],['G',[0,1,0]],['Yl',[1,1,0]]];
  const state = {kind:'parade', height:200, luma:true};
  const layouts = new WeakMap(), signatures = new WeakMap(), samples = new WeakMap();
  let lastPaint = -Infinity, lastCost = 0, work, workContext, trace;

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const chroma = (r, g, b) => {
    const y = KR*r+KG*g+KB*b;
    return [(b-y)/CB_SCALE, (r-y)/CR_SCALE];
  };
  // Luma code of an 8-bit pixel with the exact Rec. 709 weights, rounded to the nearest code
  // (at most 0.5 code from the exact value). Products are tabled once per channel.
  const LUMA_R = new Float64Array(256), LUMA_G = new Float64Array(256), LUMA_B = new Float64Array(256);
  for (let value = 0; value < 256; value++) { LUMA_R[value] = KR*value; LUMA_G[value] = KG*value; LUMA_B[value] = KB*value; }
  const lumaCode = (r, g, b) => (LUMA_R[r]+LUMA_G[g]+LUMA_B[b]+.5)|0;
  // Vectorscope cell coordinates, (B-Y)/1.8556 and (R-Y)/1.5748 scaled to cells, split into one
  // term per channel value (red, green, blue tables of 256 each; the centre offset rides on red).
  const CB_CELL = new Float64Array(768), CR_CELL = new Float64Array(768);
  for (let value = 0, scale = VECTOR_CELLS/(2*VECTOR_RANGE)/255; value < 256; value++) {
    CB_CELL[value] = -KR*value/CB_SCALE*scale+VECTOR_CELLS/2; CB_CELL[256+value] = -KG*value/CB_SCALE*scale; CB_CELL[512+value] = (1-KB)*value/CB_SCALE*scale;
    CR_CELL[value] = (1-KR)*value/CR_SCALE*scale+VECTOR_CELLS/2; CR_CELL[256+value] = -KG*value/CR_SCALE*scale; CR_CELL[512+value] = -KB*value/CR_SCALE*scale;
  }
  // The picture size the scopes sample from a source of width x height.
  function sampleSize(width, height) {
    const scale = Math.min(1, MAX_SAMPLE_WIDTH/width, Math.sqrt(MAX_SAMPLES/(width*height)));
    let w = Math.max(1, Math.round(width*scale)), h = Math.max(1, Math.round(height*scale));
    while (w*h > MAX_SAMPLES) if (w/width > h/height) w--; else h--;
    return {width:w, height:h};
  }

  // Count pixels into the bins the chosen scope needs. Pure: takes RGBA bytes.
  function analyze(rgba, width, height, kind, columns = COLUMNS) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || rgba.length < width*height*4)
      throw new Error('Scopes need a picture of at least one pixel.');
    const result = {kind, width, height, pixels:width*height};
    if (kind === 'waveform' || kind === 'parade') {
      const count = Math.max(1, Math.min(columns, width)), column = new Uint16Array(width);
      for (let x = 0; x < width; x++) column[x] = Math.floor(x*count/width);
      result.columns = count;
      // Picture columns go in strips of TILE, so the bins of one strip stay in cache while its rows are read.
      const TILE = 64;
      if (kind === 'waveform') {
        const luma = new Uint32Array(count*LEVELS);
        for (let x0 = 0; x0 < width; x0 += TILE) for (let y = 0, x1 = Math.min(width, x0+TILE); y < height; y++)
          for (let x = x0, i = (y*width+x0)*4; x < x1; x++, i += 4) luma[column[x]*LEVELS+lumaCode(rgba[i], rgba[i+1], rgba[i+2])]++;
        result.luma = luma;
      } else {
        const red = new Uint32Array(count*LEVELS), green = new Uint32Array(count*LEVELS), blue = new Uint32Array(count*LEVELS);
        for (let x0 = 0; x0 < width; x0 += TILE) for (let y = 0, x1 = Math.min(width, x0+TILE); y < height; y++)
          for (let x = x0, i = (y*width+x0)*4; x < x1; x++, i += 4) {
            const base = column[x]*LEVELS;
            red[base+rgba[i]]++; green[base+rgba[i+1]]++; blue[base+rgba[i+2]]++;
          }
        result.parade = [red, green, blue];
      }
    } else if (kind === 'histogram') {
      const red = new Uint32Array(LEVELS), green = new Uint32Array(LEVELS), blue = new Uint32Array(LEVELS), luma = new Uint32Array(LEVELS);
      for (let i = 0, end = width*height*4; i < end; i += 4) {
        red[rgba[i]]++; green[rgba[i+1]]++; blue[rgba[i+2]]++;
        luma[lumaCode(rgba[i], rgba[i+1], rgba[i+2])]++;
      }
      result.histogram = [red, green, blue, luma];
    } else if (kind === 'vector') {
      const cells = new Uint32Array(VECTOR_CELLS*VECTOR_CELLS);
      for (let i = 0, end = width*height*4; i < end; i += 4) {
        const r = rgba[i], g = rgba[i+1], b = rgba[i+2];
        const x = Math.floor(CB_CELL[r]+CB_CELL[256+g]+CB_CELL[512+b]), v = Math.floor(CR_CELL[r]+CR_CELL[256+g]+CR_CELL[512+b]);
        cells[(VECTOR_CELLS-1-v)*VECTOR_CELLS+x]++;
      }
      result.vector = cells;
    } else throw new Error('Unknown scope.');
    return result;
  }

  // Where each scope draws, in bitmap pixels. `u` is bitmap pixels per CSS pixel.
  function layoutFor(kind, width, height, u) {
    const font = Math.max(9, Math.round(10*u)), pad = Math.round(4*u), head = Math.round(font*1.6), compact = width < 180*u;
    if (kind === 'vector') {
      const side = Math.max(20, Math.min(width-2*pad, height-head-pad));
      return {kind, width, height, font, pad, compact, plot:{x:Math.round((width-side)/2), y:head+Math.round((height-head-pad-side)/2), w:side, h:side}};
    }
    const left = Math.round(font*2.6), right = kind === 'histogram' ? pad : Math.round(font*(compact ? 3.4 : 5.8));
    const bottom = kind === 'histogram' ? Math.round(font*3.6) : pad+Math.round(font*.6);
    const plot = {x:left, y:head, w:Math.max(30, width-left-right), h:Math.max(30, height-head-bottom)};
    const layout = {kind, width, height, font, pad, compact, plot};
    if (kind === 'parade') {
      layout.panels = [0,1,2].map(index => ({x:Math.round(index*plot.w/3), w:Math.round((index+1)*plot.w/3)-Math.round(index*plot.w/3)}));
    }
    return layout;
  }

  // 8-bit code to its 10-bit equivalent: broadcast levels shift by two bits (16 -> 64, 235 -> 940); full scale is 1023.
  const tenBit = code => code === 255 ? 1023 : code*4;
  const levelY = (plot, level) => plot.y+(LEVELS-1-level)/(LEVELS-1)*(plot.h-1);
  function text(ctx, value, x, y, align = 'left', color = '#b9c9b5') {
    ctx.fillStyle = color; ctx.textAlign = align; ctx.fillText(value, x, y);
  }
  // Text drawn over traces gets a dark outline so it stays readable on top of them.
  function haloText(ctx, value, x, y, align, color, width) {
    ctx.textAlign = align; ctx.lineWidth = width; ctx.lineJoin = 'round'; ctx.strokeStyle = BACKGROUND;
    ctx.strokeText(value, x, y); ctx.lineWidth = 1;
    text(ctx, value, x, y, align, color);
  }
  function line(ctx, x0, y0, x1, y1, color, dash = []) {
    ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); ctx.setLineDash([]);
  }
  // Place labels in priority order and drop any that would touch one already placed.
  function placeLabels(items, font) {
    const placed = [];
    for (const item of items) if (!placed.some(other => Math.abs(other.y-item.y) < font*1.05)) placed.push(item);
    return placed;
  }

  function drawLevelGraticule(ctx, layout) {
    const {plot, font, width} = layout, unit = font/10;
    ctx.lineWidth = Math.max(1, Math.round(unit*.75));
    for (const percent of [0,25,50,75,100]) line(ctx, plot.x, Math.round(levelY(plot, percent/100*255))+.5, plot.x+plot.w, Math.round(levelY(plot, percent/100*255))+.5, 'rgba(160,185,160,.26)');
    // Labelled levels: percent on the left, 8-bit and 10-bit code values on the right.
    ctx.font = `${font}px "IBM Plex Mono", monospace`; ctx.textBaseline = 'middle';
    for (const item of placeLabels([100,0,50,75,25].map(percent => ({y:levelY(plot, percent/100*255), percent})), font))
      text(ctx, String(item.percent), plot.x-Math.round(font*.4), item.y, 'right');
    text(ctx, '%', plot.x-Math.round(font*.4), plot.y-Math.round(font*.9), 'right');
    const codes = [235,16,255,0,128].map(code => ({y:levelY(plot, code), code, wide:tenBit(code)}));
    for (const item of placeLabels(codes, font))
      text(ctx, layout.compact ? String(item.code) : `${item.code} ${item.wide}`, plot.x+plot.w+Math.round(font*.4), item.y, 'left', item.code === 16 || item.code === 235 ? '#e0a44a' : '#b9c9b5');
    text(ctx, layout.compact ? '8b' : '8b 10b', plot.x+plot.w+Math.round(font*.4), plot.y-Math.round(font*.9), 'left');
    // Legal (broadcast) range 16–235 in 8 bits, 64–940 in 10 bits.
    for (const level of [16,235]) line(ctx, plot.x, Math.round(levelY(plot, level))+.5, plot.x+plot.w, Math.round(levelY(plot, level))+.5, 'rgba(224,164,74,.8)', [Math.round(4*unit), Math.round(3*unit)]);
    ctx.textBaseline = 'alphabetic';
  }

  function ensureTrace(width, height) {
    if (!trace) trace = document.createElement('canvas');
    if (trace.width !== width || trace.height !== height) { trace.width = width; trace.height = height; }
    return trace.getContext('2d');
  }
  function traceColumns(image, plotWidth, plotHeight, bins, count, left, right, color, binPixels) {
    const span = right-left, half = (plotHeight-1)/(LEVELS-1)/2;
    // Narrow panels: add neighbouring picture columns together first, so the work follows the panel width.
    if (span < count) {
      const merged = new Uint32Array(span*LEVELS);
      for (let column = 0; column < count; column++) {
        const target = Math.floor(column*span/count)*LEVELS, source = column*LEVELS;
        for (let level = 0; level < LEVELS; level++) merged[target+level] += bins[source+level];
      }
      binPixels *= count/span; bins = merged; count = span;
    }
    for (let column = 0; column < count; column++) {
      const xa = left+Math.floor(column*span/count), xb = Math.max(xa+1, left+Math.floor((column+1)*span/count));
      for (let level = 0; level < LEVELS; level++) {
        const n = bins[column*LEVELS+level];
        if (!n) continue;
        const alpha = Math.round(255*(.25+.75*Math.min(1, Math.sqrt(n/binPixels*12))));
        const center = (LEVELS-1-level)/(LEVELS-1)*(plotHeight-1);
        const ya = Math.max(0, Math.round(center-half)), yb = Math.min(plotHeight-1, Math.max(ya, Math.round(center+half)));
        for (let y = ya; y <= yb; y++) for (let x = xa; x < xb && x < right; x++) {
          const i = (y*plotWidth+x)*4;
          if (image[i+3] < alpha) { image[i] = color[0]; image[i+1] = color[1]; image[i+2] = color[2]; image[i+3] = alpha; }
        }
      }
    }
  }
  function blitTrace(ctx, layout, image, plotWidth, plotHeight) {
    const target = ensureTrace(plotWidth, plotHeight);
    target.putImageData(new ImageData(image, plotWidth, plotHeight), 0, 0);
    ctx.drawImage(trace, layout.plot.x, layout.plot.y);
  }

  function drawWaveform(ctx, layout, data) {
    drawLevelGraticule(ctx, layout);
    const {plot} = layout, image = new Uint8ClampedArray(plot.w*plot.h*4);
    traceColumns(image, plot.w, plot.h, data.luma, data.columns, 0, plot.w, TRACE, data.pixels/data.columns);
    blitTrace(ctx, layout, image, plot.w, plot.h);
  }
  function drawParade(ctx, layout, data) {
    drawLevelGraticule(ctx, layout);
    const {plot, panels, font} = layout, image = new Uint8ClampedArray(plot.w*plot.h*4);
    panels.forEach((panel, index) => traceColumns(image, plot.w, plot.h, data.parade[index], data.columns, panel.x, panel.x+panel.w, CHANNELS[index], data.pixels/data.columns));
    blitTrace(ctx, layout, image, plot.w, plot.h);
    ctx.lineWidth = 1; ctx.font = `${font}px "IBM Plex Mono", monospace`;
    panels.forEach((panel, index) => {
      if (index) line(ctx, plot.x+panel.x+.5, plot.y, plot.x+panel.x+.5, plot.y+plot.h, 'rgba(160,185,160,.5)');
      text(ctx, 'RGB'[index], plot.x+panel.x+panel.w/2, plot.y-Math.round(font*.35), 'center', `rgb(${CHANNELS[index]})`);
    });
  }

  const vectorPoint = (plot, cb, cr) => [plot.x+(cb+VECTOR_RANGE)/(2*VECTOR_RANGE)*plot.w, plot.y+(1-(cr+VECTOR_RANGE)/(2*VECTOR_RANGE))*plot.h];
  function drawVector(ctx, layout, data) {
    const {plot, font} = layout, unit = font/10;
    const [cx, cy] = vectorPoint(plot, 0, 0), radius = size => size/(2*VECTOR_RANGE)*plot.w;
    ctx.lineWidth = Math.max(1, Math.round(unit*.75));
    ctx.strokeStyle = 'rgba(160,185,160,.3)';
    ctx.strokeRect(plot.x+.5, plot.y+.5, plot.w-1, plot.h-1);
    for (const size of [.25,.5]) { ctx.beginPath(); ctx.arc(cx, cy, radius(size), 0, Math.PI*2); ctx.stroke(); }
    line(ctx, plot.x, cy, plot.x+plot.w, cy, 'rgba(160,185,160,.3)'); line(ctx, cx, plot.y, cx, plot.y+plot.h, 'rgba(160,185,160,.3)');
    // Skin-tone line: 123° counter-clockwise from the +Cb axis, drawn to the 50 % ring.
    const angle = SKIN_DEGREES*Math.PI/180, skin = vectorPoint(plot, Math.cos(angle)*.5, Math.sin(angle)*.5);
    line(ctx, cx, cy, skin[0], skin[1], 'rgba(224,164,74,.9)', [Math.round(5*unit), Math.round(3*unit)]);
    // 75 % colour-bar targets.
    const box = Math.max(5, radius(.05)), labels = [];
    for (const [name, rgb] of TARGETS) {
      const [cb, cr] = chroma(...rgb.map(value => value*.75)), [x, y] = vectorPoint(plot, cb, cr);
      ctx.strokeStyle = 'rgba(224,230,224,.85)'; ctx.strokeRect(Math.round(x-box/2)+.5, Math.round(y-box/2)+.5, Math.round(box), Math.round(box));
      // Beside the box, clockwise. Straight outward is where the same hue at 100 % lands.
      const length = Math.hypot(x-cx, y-cy) || 1, side = box/2+font*.85;
      labels.push([name, x+(cy-y)/length*side, y+(x-cx)/length*side]);
    }
    // Each occupied cell is drawn as a small dot so single colours stay visible at any size.
    const side = plot.w, image = new Uint8ClampedArray(side*side*4), cell = side/VECTOR_CELLS, dot = Math.max(Math.ceil(cell), Math.round(2.5*unit));
    for (let row = 0; row < VECTOR_CELLS; row++) for (let column = 0; column < VECTOR_CELLS; column++) {
      const n = data.vector[row*VECTOR_CELLS+column];
      if (!n) continue;
      const alpha = Math.round(255*(.3+.7*Math.min(1, Math.sqrt(n/data.pixels*60))));
      const xa = Math.round((column+.5)*cell-dot/2), ya = Math.round((row+.5)*cell-dot/2);
      for (let y = Math.max(0, ya); y < Math.min(side, ya+dot); y++) for (let x = Math.max(0, xa); x < Math.min(side, xa+dot); x++) {
        const i = (y*side+x)*4;
        if (image[i+3] < alpha) { image[i] = TRACE[0]; image[i+1] = TRACE[1]; image[i+2] = TRACE[2]; image[i+3] = alpha; }
      }
    }
    blitTrace(ctx, layout, image, side, side);
    ctx.font = `${font}px "IBM Plex Mono", monospace`; ctx.textBaseline = 'middle';
    const halo = Math.max(2, Math.round(3*unit));
    haloText(ctx, 'skin', skin[0], skin[1]-Math.round(font*.7), 'center', '#e0a44a', halo);
    for (const [name, x, y] of labels) haloText(ctx, name, x, y, 'center', '#d6e0d3', halo);
    ctx.textBaseline = 'alphabetic';
  }

  // The pixel column a level occupies: its centre, so one rule serves narrow and wide plots.
  const histogramColumn = (level, width) => Math.min(width-1, Math.floor((level+.5)*width/LEVELS));
  function drawHistogram(ctx, layout, data) {
    const {plot, font} = layout, unit = font/10, bins = data.histogram;
    ctx.lineWidth = Math.max(1, Math.round(unit*.75));
    ctx.font = `${font}px "IBM Plex Mono", monospace`; ctx.textBaseline = 'alphabetic';
    // Three rows of tick labels: percent, 8-bit and 10-bit code values. Ticks are labelled in
    // priority order (ends, middle, quarters); one whose labels would touch another stays bare.
    const ticks = [0,64,128,192,255], placed = [], gap = font*.6;
    for (const level of ticks) { const x = plot.x+histogramColumn(level, plot.w)+.5; line(ctx, x, plot.y, x, plot.y+plot.h, 'rgba(160,185,160,.26)'); }
    for (const index of [0,4,2,1,3]) {
      const level = ticks[index], x = plot.x+histogramColumn(level, plot.w)+.5;
      const align = index === 0 ? 'left' : index === ticks.length-1 ? 'right' : 'center';
      const values = [`${Math.round(level/255*100)}%`, String(level), String(tenBit(level))];
      const wide = Math.max(...values.map(value => ctx.measureText(value).width)), anchor = align === 'left' ? plot.x : align === 'right' ? plot.x+plot.w : x;
      const span = align === 'left' ? [anchor, anchor+wide] : align === 'right' ? [anchor-wide, anchor] : [anchor-wide/2, anchor+wide/2];
      if (placed.some(other => span[0] < other[1]+gap && span[1] > other[0]-gap)) continue;
      placed.push(span);
      values.forEach((value, row) => text(ctx, value, anchor, plot.y+plot.h+Math.round(font*(1.15+row*1.05)), align, row === 0 ? '#b9c9b5' : '#8fa08c'));
    }
    for (const level of [16,235]) {
      const label = layout.compact ? String(level) : `${level} ${tenBit(level)}`, x = plot.x+histogramColumn(level, plot.w)+.5;
      line(ctx, x, plot.y, x, plot.y+plot.h, 'rgba(224,164,74,.8)', [Math.round(4*unit), Math.round(3*unit)]);
      text(ctx, label, x+(level < 128 ? 3 : -3)*unit, plot.y-Math.round(font*.35), level < 128 ? 'left' : 'right', '#e0a44a');
    }
    // Collapse the 256 bins onto the plot's pixel columns, keeping the tallest bin of each.
    const columns = bins.map(channel => {
      const heights = new Float64Array(plot.w);
      for (let level = 0; level < LEVELS; level++) {
        const first = plot.w < LEVELS ? histogramColumn(level, plot.w) : Math.round(level*plot.w/LEVELS);
        const last = plot.w < LEVELS ? first : Math.max(first, Math.round((level+1)*plot.w/LEVELS)-1);
        for (let x = first; x <= last && x < plot.w; x++) heights[x] = Math.max(heights[x], channel[level]);
      }
      return heights;
    });
    // Scale to the tallest bin away from the ends, so piles of clipped black or white (which show
    // as full-height bars at the edges) do not flatten the rest of the picture.
    const peak = Math.max(1, ...bins.slice(0, 3).map(channel => channel.slice(1, LEVELS-1).reduce((a, b) => Math.max(a, b), 0)));
    ctx.save(); ctx.globalCompositeOperation = 'lighter';
    columns.slice(0, 3).forEach((heights, index) => {
      ctx.fillStyle = `rgba(${CHANNELS[index].map(value => Math.round(value*.55))},1)`;
      for (let x = 0; x < plot.w; x++) {
        if (!heights[x]) continue;
        const height = Math.max(1, Math.round(Math.min(1, heights[x]/peak)*(plot.h-1)));
        ctx.fillRect(plot.x+x, plot.y+plot.h-height, 1, height);
      }
    });
    ctx.restore();
    if (!state.luma) return;
    ctx.strokeStyle = 'rgba(235,240,230,.85)'; ctx.beginPath();
    for (let x = 0; x < plot.w; x++) {
      const y = plot.y+plot.h-Math.min(plot.h-1, columns[3][x]/peak*(plot.h-1));
      if (x) ctx.lineTo(plot.x+x+.5, y); else ctx.moveTo(plot.x+x+.5, y);
    }
    ctx.stroke();
  }

  const DRAW = {waveform:drawWaveform, parade:drawParade, vector:drawVector, histogram:drawHistogram};

  // Draw one scope of an RGBA frame into a 2D context of width x height bitmap pixels.
  function render(ctx, kind, width, height, u, frame) {
    const layout = layoutFor(kind, width, height, u);
    DRAW[kind](ctx, layout, analyze(frame.data, frame.width, frame.height, kind));
    return layout;
  }

  // Read the program frame once per repaint, at most MAX_SAMPLES pixels. Smoothing is off, so a
  // smaller sample keeps evenly spaced rows and columns with their own values.
  function capture() {
    const source = document.querySelector('#preview-canvas');
    if (!source || !source.width || !source.height) return null;
    const {width, height} = sampleSize(source.width, source.height);
    if (!work) { work = document.createElement('canvas'); workContext = work.getContext('2d', {willReadFrequently:true}); }
    if (work.width !== width || work.height !== height) { work.width = width; work.height = height; }
    workContext.imageSmoothingEnabled = false;
    workContext.drawImage(source, 0, 0, width, height);
    const image = workContext.getImageData(0, 0, width, height);
    const words = new Uint32Array(image.data.buffer);
    let hash = 2166136261;
    for (let i = 0; i < words.length; i++) hash = Math.imul(hash ^ words[i], 16777619);
    return {data:image.data, width, height, hash};
  }

  function clear(canvas, ctx, message) {
    ctx.fillStyle = BACKGROUND; ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (message) { ctx.fillStyle = '#b9c9b5'; ctx.font = '12px monospace'; ctx.textAlign = 'center'; ctx.fillText(message, canvas.width/2, canvas.height/2); }
  }
  // Repaint `canvas` from the program monitor. Throttled to the cost of the last paint unless forced.
  function paint(canvas = document.querySelector('.splice-scope'), force = false) {
    if (!canvas?.isConnected || document.hidden) return;
    const started = performance.now();
    if (!force && started-lastPaint < Math.max(100, lastCost*8)) return;
    const box = {width:canvas.clientWidth, height:canvas.clientHeight};
    if (!box.width || !box.height) return;
    lastPaint = started;
    const u = clamp(window.devicePixelRatio || 1, 1, 2);
    const width = clamp(Math.round(box.width*u), 160, 1600), height = clamp(Math.round(box.height*u), 80, 1200);
    const resized = canvas.width !== width || canvas.height !== height;
    if (resized) { canvas.width = width; canvas.height = height; }
    const ctx = canvas.getContext('2d');
    let frame;
    try { frame = capture(); } catch (error) {
      if (error?.name !== 'SecurityError') throw error;
      clear(canvas, ctx, 'This picture cannot be read for scopes.'); layouts.delete(canvas); return;
    }
    if (!frame) { clear(canvas, ctx, 'No picture'); layouts.delete(canvas); return; }
    const signature = `${state.kind}|${state.luma}|${width}x${height}|${frame.hash}`;
    if (!force && !resized && signatures.get(canvas) === signature) { lastCost = performance.now()-started; return; }
    clear(canvas, ctx);
    const layout = render(ctx, state.kind, width, height, width/Math.max(1, box.width), frame);
    layouts.set(canvas, layout); signatures.set(canvas, signature); samples.set(canvas, {width:frame.width, height:frame.height});
    lastCost = performance.now()-started;
  }

  function markup() {
    return `<div class="splice-scopes" data-scopes><div class="splice-scope-tabs" role="tablist" aria-label="Scope type">` +
      KINDS.map(([kind, title]) => `<button type="button" role="tab" data-scope="${kind}" aria-selected="${kind === state.kind}">${title}</button>`).join('') +
      `</div><canvas class="splice-scope" role="img" aria-label="Video scope of the graded program frame" title="Levels in percent, with 8-bit and 10-bit code values. Amber lines mark the legal range, 16–235 (64–940)." style="height:${state.height}px"></canvas>` +
      `<label class="splice-scope-luma" ${state.kind === 'histogram' ? '' : 'hidden'}><input type="checkbox" aria-label="Show luma" data-scope-luma ${state.luma ? 'checked' : ''}> Show luma curve</label>` +
      `<button type="button" class="splice-scope-enlarge" data-scope-dock title="Dock a large scope beside the program monitor">Enlarge scope</button>` +
      `<label class="splice-scope-size"><span>Scope height</span><input type="range" aria-label="Scope height" data-scope-height min="${HEIGHT.min}" max="${HEIGHT.max}" step="${HEIGHT.step}" value="${state.height}"></label></div>`;
  }
  // The controls live inside the inspector markup and go away with it; nothing outside is subscribed.
  function bind(root) {
    const panel = root.querySelector('[data-scopes]'), canvas = panel?.querySelector('.splice-scope');
    if (!panel) return;
    for (const button of panel.querySelectorAll('[data-scope]')) button.addEventListener('click', () => {
      state.kind = button.dataset.scope;
      for (const other of panel.querySelectorAll('[data-scope]')) other.setAttribute('aria-selected', String(other === button));
      panel.querySelector('.splice-scope-luma').hidden = state.kind !== 'histogram';
      paint(canvas, true);
    });
    panel.querySelector('[data-scope-luma]').addEventListener('change', event => { state.luma = event.target.checked; paint(canvas, true); });
    panel.querySelector('[data-scope-height]').addEventListener('input', event => {
      state.height = clamp(Math.round(Number(event.target.value)) || state.height, HEIGHT.min, HEIGHT.max);
      canvas.style.height = `${state.height}px`;
      paint(canvas, true);
    });
  }
  window.SpliceScopes = {analyze, render, paint, markup, bind, sampleSize, lumaCode, layoutOf:canvas => layouts.get(canvas),
    sampleOf:canvas => samples.get(canvas), kinds:KINDS.map(item => item[0]), range:VECTOR_RANGE};
})();
