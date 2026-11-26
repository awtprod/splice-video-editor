/* Delivery: presets, formats this machine can encode, timecode/metadata, burn-ins, output naming,
 * and the render queue panel. The codec table, presets and per-machine availability come from the
 * server (delivery.py through /api/delivery/capabilities): one definition for the dialog, the
 * server's validation and the FFmpeg command builder. */
window.SpliceDelivery = (() => {
  let table = null, loading = null;
  const LEGACY = ['h264', 'prores', 'wav'];
  const QUALITY = {h264: [['16', 'Master · CRF 16'], ['20', 'Delivery · CRF 20'], ['26', 'Review copy · CRF 26']],
    hevc: [['18', 'Master · CRF 18'], ['22', 'Delivery · CRF 22'], ['28', 'Review copy · CRF 28']],
    av1: [['24', 'Master · CRF 24'], ['30', 'Delivery · CRF 30'], ['38', 'Review copy · CRF 38']]};
  const DEFAULT_SPEED = {h264: 'veryfast', hevc: 'veryfast', av1: '8'};
  const FIELD_LABELS = {timecode: 'Sequence timecode', sourceTimecode: 'Source timecode', clipName: 'Clip name',
    frame: 'Frame number', watermark: 'Watermark'};

  async function json(url, init) {
    const response = await fetch(url, {signal: AbortSignal.timeout(120000), ...init});
    let body;
    try {
      body = await response.json();
    } catch (cause) {
      throw new Error(`Invalid response from ${url} (HTTP ${response.status})`, {cause});
    }
    if (!response.ok) { const error = new Error(body.error || response.statusText); error.status = response.status; error.body = body; throw error; }
    return body;
  }
  const post = (url, body) => json(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  function capabilities(refresh = false) {
    if (refresh || !loading) loading = json('/api/delivery/capabilities' + (refresh ? '?refresh=1' : '')).then(value => (table = value))
      .catch(error => { loading = null; throw error; });
    return loading;
  }
  // Match the capability keys already stored by delivery.variant_key, including Python booleans.
  const variantKey = o => [o.codec || '', o.profile || '', o.alpha == null ? '' : o.alpha ? 'True' : 'False', o.container || '', o.encoder || ''].join('|');
  function reasonFor(options) {
    if (!table) return '';
    const codec = table.codecs[options.codec];
    if (!codec || codec.kind === 'audio') return '';
    const variants = table.machine?.variants || {};
    if (options.encoder && options.encoder !== 'software')
      return variants[variantKey({codec: options.codec, profile: options.profile, encoder: options.encoder, container: codec.containers[0]})] ?? 'not tested';
    return variants[variantKey({codec: options.codec, profile: options.profile, alpha: !!options.alpha, container: options.container})] ?? '';
  }
  const el = (tag, props = {}, ...children) => { const node = Object.assign(document.createElement(tag), props); node.append(...children); return node; };
  const field = (label, control, extra = {}) => { const node = el('label', extra, label); node.append(control); return node; };
  const select = (name, options) => { const node = el('select', {name}); for (const [value, text] of options) node.append(new Option(text, value)); return node; };

  /* ---- the Export video dialog -------------------------------------------------------------- */
  function enhance(form, context) {
    const {state, syncFormat} = context;
    const project = state.project;
    const codec = form.elements.codec;
    const codecLabel = codec.closest('label');
    // Presets first: a preset fills the fields below; anything changed by hand reads "Custom".
    const presetRow = el('div', {className: 'export-preset-row'});
    const preset = select('preset', [['', 'Custom settings']]);
    preset.setAttribute('aria-describedby', 'export-preset-purpose');
    const savePreset = el('button', {type: 'button', className: 'export-small', textContent: 'Save preset…'});
    const managePresets = el('button', {type: 'button', className: 'export-small', textContent: 'Manage…'});
    presetRow.append(field('Preset', preset), savePreset, managePresets);
    const purpose = el('p', {className: 'export-help export-purpose', id: 'export-preset-purpose'});
    form.querySelector('.export-spec').after(presetRow, purpose);
    // Formats: the original three stay first; the rest arrive with the machine's capabilities.
    const formatGroup = el('fieldset', {className: 'export-format'}, el('legend', {textContent: 'Format'}));
    presetRow.after(formatGroup);
    purpose.after(formatGroup);
    formatGroup.append(codecLabel);
    const colorLabel = form.elements.colorSpace.closest('label');
    const qualityLabel = form.elements.quality.closest('label');
    const profile = select('profile', []), container = select('container', []), encoder = select('encoder', [['software', 'Software']]);
    const rateControl = select('rateControl', [['crf', 'Constant quality (CRF)'], ['bitrate', 'Target bitrate'], ['twopass', 'Target bitrate · two-pass']]);
    const videoBitrate = el('input', {name: 'videoBitrate', type: 'number', min: 100, max: 800000, step: 100, value: 20000});
    const speed = select('speed', []);
    const alpha = el('input', {name: 'alpha', type: 'checkbox'});
    const audioCodec = select('audioCodec', []);
    const rows = {profile: field('Profile', profile), container: field('Container', container), encoder: field('Encoder', encoder),
      rateControl: field('Rate control', rateControl), videoBitrate: field('Video bitrate (kbit/s)', videoBitrate),
      speed: field('Encoder speed', speed), alpha: field('Include alpha (transparency)', alpha, {className: 'export-check'}),
      audioCodec: field('Audio format', audioCodec)};
    formatGroup.append(rows.profile, rows.container, rows.encoder, rows.rateControl, qualityLabel, rows.videoBitrate, rows.speed,
      rows.alpha, colorLabel, rows.audioCodec);
    // HDR10 metadata (HEVC/AV1 with PQ).
    const hdr = el('fieldset', {className: 'export-hdr', hidden: true}, el('legend', {textContent: 'HDR10 metadata'}));
    const display = select('hdrDisplay', [['p3d65', 'Display P3 (D65)'], ['bt2020', 'Rec. 2020 (D65)']]);
    const maxLum = el('input', {name: 'hdrMaxLuminance', type: 'number', min: 100, max: 10000, step: 1, value: 1000});
    const minLum = el('input', {name: 'hdrMinLuminance', type: 'number', min: 0.0001, max: 5, step: 'any', value: 0.005});
    const levels = select('hdrLightLevels', [['measure', 'Measure MaxCLL/MaxFALL (one extra picture pass)'], ['source', 'From the HDR10 source'], ['manual', 'Enter MaxCLL/MaxFALL']]);
    const maxCll = el('input', {name: 'hdrMaxCll', type: 'number', min: 0, max: 10000, step: 1, value: 1000});
    const maxFall = el('input', {name: 'hdrMaxFall', type: 'number', min: 0, max: 10000, step: 1, value: 400});
    hdr.append(field('Mastering display', display), field('Display peak (nits)', maxLum), field('Display minimum (nits)', minLum),
      field('Content light levels', levels), field('MaxCLL (nits)', maxCll), field('MaxFALL (nits)', maxFall));
    colorLabel.after(hdr);
    if (!form.elements.colorSpace.querySelector('[value=hlg]')) form.elements.colorSpace.append(new Option('Rec. 2100 HLG (HLG sources)', 'hlg'));
    // Timecode and metadata, burn-ins, output: collapsed groups so Render stays in reach.
    const tcGroup = el('details', {className: 'export-group export-timecode'}, el('summary', {textContent: 'Timecode and metadata'}));
    const timecode = el('input', {name: 'timecode', type: 'checkbox', checked: true});
    const reel = el('input', {name: 'reel', type: 'text', maxLength: 32, placeholder: 'e.g. A001', autocomplete: 'off', spellcheck: false});
    const title = el('input', {name: 'metaTitle', type: 'text', maxLength: 200, autocomplete: 'off'});
    const comment = el('input', {name: 'metaComment', type: 'text', maxLength: 1000, autocomplete: 'off'});
    const startLabel = el('p', {className: 'export-help export-start-tc'});
    tcGroup.append(field('Timecode track', timecode, {className: 'export-check'}), startLabel, field('Reel / tape name', reel),
      field('Title', title), field('Comment', comment));
    const burnGroup = el('details', {className: 'export-group export-burnins'}, el('summary', {textContent: 'Burn-ins and watermark'}));
    const burnChecks = {};
    for (const [key, text] of Object.entries(FIELD_LABELS)) {
      burnChecks[key] = el('input', {name: 'burn-' + key, type: 'checkbox'});
      burnGroup.append(field(text, burnChecks[key], {className: 'export-check'}));
    }
    const positions = [['top-left', 'Top left'], ['top-center', 'Top centre'], ['top-right', 'Top right'], ['bottom-left', 'Bottom left'], ['bottom-center', 'Bottom centre'], ['bottom-right', 'Bottom right']];
    const burnPosition = select('burnPosition', positions), namePosition = select('burnNamePosition', positions);
    burnPosition.value = 'bottom-center'; namePosition.value = 'top-left';
    const burnSize = el('input', {name: 'burnSize', type: 'number', min: 2, max: 10, step: 1, value: 4});
    const watermark = el('input', {name: 'watermark', type: 'text', maxLength: 80, placeholder: 'e.g. CONFIDENTIAL · Review copy'});
    const watermarkOpacity = el('input', {name: 'watermarkOpacity', type: 'number', min: 5, max: 100, step: 1, value: 30});
    const safeMargins = el('input', {name: 'safeMargins', type: 'checkbox'});
    burnGroup.append(field('Timecode position', burnPosition), field('Clip name position', namePosition), field('Text size (% of height)', burnSize),
      field('Watermark text', watermark), field('Watermark opacity (%)', watermarkOpacity), field('Safe-area guides', safeMargins, {className: 'export-check'}),
      el('p', {className: 'export-help', textContent: 'Off by default. Drawn identically in every format, after any resize.'}));
    const outGroup = el('details', {className: 'export-group export-output'}, el('summary', {textContent: 'Output (render queue)'}));
    const outName = el('input', {name: 'outputName', type: 'text', maxLength: 120, value: project.name || 'Untitled', autocomplete: 'off'});
    const folder = el('input', {name: 'outputFolder', type: 'text', placeholder: 'Splice Exports folder', autocomplete: 'off', spellcheck: false});
    const sizeSelect = select('outputSize', [['', 'Sequence size'], ['half', 'Half size'], ['custom', 'Custom…']]);
    const outWidth = el('input', {name: 'outputWidth', type: 'number', min: 16, max: 8192, step: 2, value: project.width});
    const outHeight = el('input', {name: 'outputHeight', type: 'number', min: 16, max: 4320, step: 2, value: project.height});
    const fit = select('fit', [['fit', 'Fit (bars, nothing cropped)'], ['fill', 'Fill (crop the edges)'], ['stretch', 'Stretch']]);
    const startNumber = el('input', {name: 'startNumber', type: 'number', min: 0, max: 99999999, step: 1, value: 0});
    const padding = el('input', {name: 'padding', type: 'number', min: 1, max: 9, step: 1, value: 7});
    const srt = el('input', {name: 'captionSrt', type: 'checkbox'}), vtt = el('input', {name: 'captionVtt', type: 'checkbox'});
    const outRows = {width: field('Width', outWidth), height: field('Height', outHeight), fit: field('Resize', fit),
      startNumber: field('First frame number', startNumber), padding: field('Frame number digits', padding)};
    outGroup.append(field('File name', outName), field('Folder (full path)', folder), field('Frame size', sizeSelect), outRows.width, outRows.height,
      outRows.fit, outRows.startNumber, outRows.padding, field('Caption file · SubRip (.srt)', srt, {className: 'export-check'}),
      field('Caption file · WebVTT (.vtt)', vtt, {className: 'export-check'}));
    const audioGroup = form.querySelector('.export-audio');
    // Groups in order: Format, Range, Timecode, Burn-ins, Output, then the Audio group as one section.
    audioGroup.before(tcGroup, burnGroup, outGroup);
    const notices = el('ul', {className: 'export-notices', hidden: true});
    notices.setAttribute('aria-label', 'Before rendering');
    notices.setAttribute('aria-live', 'polite');
    audioGroup.after(notices);
    const queueRow = el('div', {className: 'export-queue-row'});
    const addQueue = el('button', {type: 'button', className: 'export-add-queue', textContent: 'Add to queue'});
    const batch = el('button', {type: 'button', className: 'export-batch', textContent: 'Batch…'});
    const openQueue = el('button', {type: 'button', className: 'export-open-queue', textContent: 'Render queue'});
    queueRow.append(addQueue, batch, openQueue);
    form.querySelector('footer').before(queueRow);

    const info = () => table?.codecs?.[codec.value];
    function fillCodecs() {
      for (const [key, value] of Object.entries(table.codecs)) {
        if (LEGACY.includes(key)) continue;
        if (!codec.querySelector(`option[value="${key}"]`)) codec.insertBefore(new Option(value.label, key), codec.querySelector('option[value=wav]'));
      }
      const prores = codec.querySelector('option[value=prores]');
      if (prores) prores.textContent = 'Apple ProRes / PCM · MOV';
      const h264 = codec.querySelector('option[value=h264]');
      if (h264) h264.textContent = 'H.264 / AAC · MP4';
      for (const option of codec.options) {
        const reasons = Object.entries(table.machine?.variants || {}).filter(([key]) => key.startsWith(option.value + '|') && !key.endsWith('|nvenc') && !/\|(qsv|vaapi|videotoolbox|amf|nvenc)$/.test(key));
        const usable = !reasons.length || reasons.some(([, reason]) => !reason);
        option.disabled = !usable;
        if (!usable && !option.textContent.includes('unavailable')) option.textContent += ' — unavailable: ' + reasons[0][1];
      }
    }
    function fillFormat(keep = {}) {
      const codecInfo = info();
      if (!codecInfo) return;
      const profiles = Object.entries(codecInfo.profiles);
      profile.replaceChildren(...profiles.map(([key, value]) => {
        const option = new Option(value.label, key);
        const reason = reasonFor({codec: codec.value, profile: key, alpha: false, container: keep.container || codecInfo.containers[0]});
        if (reason) { option.disabled = true; option.textContent += ' — unavailable: ' + reason; }
        return option;
      }));
      profile.value = keep.profile && codecInfo.profiles[keep.profile] ? keep.profile : codecInfo.defaultProfile || '';
      container.replaceChildren(...codecInfo.containers.map(key => new Option(table.containers[key].label, key)));
      container.value = keep.container && codecInfo.containers.includes(keep.container) ? keep.container : codecInfo.containers[0];
      encoder.replaceChildren(new Option('Software (best quality per bit)', 'software'), ...codecInfo.hardware.map(key => {
        const reason = reasonFor({codec: codec.value, profile: profile.value, encoder: key});
        const option = new Option(table.hardware[key] + ' · faster, lower quality per bit' + (reason ? ' — unavailable: ' + reason : ''), key);
        option.disabled = !!reason;
        return option;
      }));
      encoder.value = keep.encoder && [...encoder.options].some(o => o.value === keep.encoder && !o.disabled) ? keep.encoder : 'software';
      for (const option of rateControl.options) option.hidden = !codecInfo.rateModes.includes(option.value);
      rateControl.value = keep.rateControl && codecInfo.rateModes.includes(keep.rateControl) ? keep.rateControl : 'crf';
      speed.replaceChildren(...(codec.value === 'av1' ? Array.from({length: 14}, (_, n) => [String(n), n + (n === 8 ? ' (default)' : '')]) :
        table.speeds.map(name => [name, name])).map(([v, t]) => new Option(t, v)));
      speed.value = keep.speed || DEFAULT_SPEED[codec.value] || speed.options[0]?.value;
      const quality = QUALITY[codec.value] || QUALITY.h264;
      const current = keep.crf != null ? String(keep.crf) : form.elements.quality.value;
      form.elements.quality.replaceChildren(...quality.map(([v, t]) => new Option(t, v)));
      if (keep.crf != null && !quality.some(([v]) => v === String(keep.crf))) form.elements.quality.append(new Option('CRF ' + keep.crf, String(keep.crf)));
      form.elements.quality.value = quality.some(([v]) => v === current) || keep.crf != null ? current : quality[1][0];
      fillAudio(keep.audioCodec);
    }
    function fillAudio(keep) {
      const kind = container.value || (codec.value === 'wav' ? 'wav' : 'folder');
      const choices = Object.entries(table.audio).filter(([, value]) => value.containers.includes(kind));
      audioCodec.replaceChildren(...choices.map(([key, value]) => {
        const option = new Option(value.label, key);
        const reason = table.machine?.audio?.[key];
        if (reason) { option.disabled = true; option.textContent += ' — unavailable: ' + reason; }
        return option;
      }));
      audioCodec.value = keep && choices.some(([key]) => key === keep) ? keep : table.defaultAudio[kind];
    }
    function sync() {
      const codecInfo = info();
      const isNew = !!codecInfo && !LEGACY.includes(codec.value) || codec.value === 'prores';
      const profiles = codecInfo ? Object.keys(codecInfo.profiles).length : 0;
      rows.profile.hidden = !codecInfo || !profiles;
      rows.container.hidden = !codecInfo || codecInfo.containers.length < 2;
      rows.encoder.hidden = !codecInfo || !codecInfo.hardware.length;
      const longGop = !!codecInfo && codecInfo.rateModes.length > 0;
      rows.rateControl.hidden = !longGop;
      rows.speed.hidden = !longGop || encoder.value !== 'software';
      qualityLabel.hidden = !longGop || rateControl.value !== 'crf';
      rows.videoBitrate.hidden = !longGop || rateControl.value === 'crf';
      const alphaable = codecInfo?.profiles?.[profile.value]?.alpha;
      rows.alpha.hidden = !alphaable; if (!alphaable) alpha.checked = false;
      const highDepth = (codecInfo?.profiles?.[profile.value]?.depth || 0) >= 10;
      colorLabel.hidden = !(codecInfo?.hdr && highDepth) && codec.value !== 'prores';
      if (colorLabel.hidden) form.elements.colorSpace.value = 'source';
      for (const option of form.elements.colorSpace.options) option.hidden = option.value === 'hlg' && !(codecInfo?.hdr && highDepth);
      hdr.hidden = !(['hevc', 'av1'].includes(codec.value) && highDepth && form.elements.colorSpace.value === 'pq');
      for (const name of ['hdrMaxCll', 'hdrMaxFall']) form.elements[name].closest('label').hidden = levels.value !== 'manual';
      rows.audioCodec.hidden = !codecInfo;
      // The Audio group's AAC settings apply wherever the audio is AAC.
      const aac = codecInfo ? audioCodec.value === 'aac' : codec.value === 'h264';
      form.elements.bitrate.closest('label').hidden = !aac;
      form.elements.aacPeakSafe.closest('label').hidden = form.querySelector('#export-peak-safe-help').hidden = !aac;
      const images = codecInfo?.kind === 'images', audioOnly = codec.value === 'wav';
      tcGroup.hidden = audioOnly || images;
      burnGroup.hidden = audioOnly;
      outRows.startNumber.hidden = outRows.padding.hidden = !images;
      outRows.width.hidden = outRows.height.hidden = sizeSelect.value !== 'custom';
      outRows.fit.hidden = !sizeSelect.value;
      for (const name of ['burnPosition']) form.elements[name].closest('label').hidden = !burnChecks.timecode.checked && !burnChecks.frame.checked;
      namePosition.closest('label').hidden = !burnChecks.clipName.checked && !burnChecks.sourceTimecode.checked;
      watermark.closest('label').hidden = watermarkOpacity.closest('label').hidden = !burnChecks.watermark.checked;
      const fps = project.fps, start = (project.startTimecode || 0) + Math.round(rangeStart() * fps);
      startLabel.textContent = 'Starts at ' + timecodeLabel(start, fps, project.dropFrame) + (rangeStart() ? ' (the range’s first frame)' : ' (sequence start)') + '.';
      context.setRenderLabel?.(images ? 'Render ' + (codecInfo.label || 'sequence') : null);
      void isNew;
      scheduleCheck();
    }
    const rangeStart = () => form.elements.rangeMode.value === 'custom' && Number.isFinite(form.elements.rangeStart.valueAsNumber) ? form.elements.rangeStart.valueAsNumber : 0;

    /* The request: the original keys always, delivery keys only where they differ from the default. */
    function options() {
      const codecInfo = info();
      const opts = {};
      if (!codecInfo || LEGACY.includes(codec.value) && codec.value !== 'prores') {
        if (codec.value === 'h264' && codecInfo) addVideo(opts, codecInfo);
      } else addVideo(opts, codecInfo);
      if (codecInfo && audioCodec.value && audioCodec.value !== table.defaultAudio[container.value]) opts.audioCodec = audioCodec.value;
      if (codecInfo && codec.value !== 'wav') {
        if (!timecode.checked && table.containers[container.value]?.timecode) opts.timecode = false;
        if (reel.value.trim()) opts.reel = reel.value.trim();
        if (title.value.trim()) opts.title = title.value.trim();
        if (comment.value.trim()) opts.comment = comment.value.trim();
        const fields = Object.entries(burnChecks).filter(([, box]) => box.checked).map(([key]) => key);
        if (fields.length || safeMargins.checked) {
          opts.burnIns = {fields, position: burnPosition.value, clipNamePosition: namePosition.value, size: burnSize.valueAsNumber, safeMargins: safeMargins.checked};
          if (fields.includes('watermark')) Object.assign(opts.burnIns, {watermark: watermark.value.trim(), watermarkOpacity: watermarkOpacity.valueAsNumber});
        }
        if (sizeSelect.value) {
          const half = sizeSelect.value === 'half';
          const even = n => Math.max(16, Math.floor(n / 2) * 2);
          opts.outputSize = half ? {width: even(project.width / 2), height: even(project.height / 2)} : {width: outWidth.valueAsNumber, height: outHeight.valueAsNumber};
          opts.fit = fit.value;
        }
      }
      const files = [srt.checked && 'srt', vtt.checked && 'vtt'].filter(Boolean);
      if (files.length && codec.value !== 'wav') opts.captionFiles = files;
      return opts;
    }
    function addVideo(opts, codecInfo) {
      if (profile.value && profile.value !== codecInfo.defaultProfile) opts.profile = profile.value;
      if (container.value && container.value !== codecInfo.containers[0]) opts.container = container.value;
      if (encoder.value !== 'software') opts.encoder = encoder.value;
      if (codecInfo.rateModes.length) {
        if (rateControl.value !== 'crf') { opts.rateControl = rateControl.value; opts.videoBitrate = videoBitrate.valueAsNumber; }
        if (encoder.value === 'software' && speed.value !== DEFAULT_SPEED[codec.value]) opts.speed = speed.value;
      }
      if (alpha.checked) opts.alpha = true;
      if (codecInfo.kind === 'images') { opts.startNumber = startNumber.valueAsNumber; opts.padding = padding.valueAsNumber; }
      if (!hdr.hidden) {
        opts.hdr = {display: display.value, maxLuminance: maxLum.valueAsNumber, minLuminance: minLum.valueAsNumber, lightLevels: levels.value};
        if (levels.value === 'manual') Object.assign(opts.hdr, {maxCll: maxCll.valueAsNumber, maxFall: maxFall.valueAsNumber});
      }
    }
    function apply(options) {
      // A preset never changes the range, frame rate or loudness; size changes are announced below.
      codec.value = options.codec || 'h264';
      fillFormat({profile: options.profile, container: options.container, encoder: options.encoder, rateControl: options.rateControl,
        speed: options.speed, crf: options.crf, audioCodec: options.audioCodec});
      if (options.videoBitrate) videoBitrate.value = options.videoBitrate;
      alpha.checked = !!options.alpha;
      form.elements.colorSpace.value = options.colorSpace ?? 'source';
      srt.checked = !!options.captionFiles?.includes('srt');
      vtt.checked = !!options.captionFiles?.includes('vtt');
      if (options.audioBitrate && [...form.elements.bitrate.options].some(o => Number(o.value) === options.audioBitrate)) form.elements.bitrate.value = String(options.audioBitrate);
      else if (options.audioBitrate) { form.elements.bitrate.append(new Option(String(options.audioBitrate))); form.elements.bitrate.value = String(options.audioBitrate); }
      timecode.checked = options.timecode !== false;
      reel.value = options.reel ?? '';
      title.value = options.title ?? '';
      comment.value = options.comment ?? '';
      if (options.hdr) {
        display.value = options.hdr.display || 'p3d65';
        maxLum.value = options.hdr.maxLuminance ?? 1000;
        minLum.value = options.hdr.minLuminance ?? 0.005;
        levels.value = options.hdr.lightLevels || 'measure';
        maxCll.value = options.hdr.maxCll ?? 1000;
        maxFall.value = options.hdr.maxFall ?? 400;
      }
      const burn = options.burnIns || {};
      for (const [key, box] of Object.entries(burnChecks)) box.checked = (burn.fields || []).includes(key);
      burnPosition.value = burn.position || 'bottom-center'; namePosition.value = burn.clipNamePosition || 'top-left';
      burnSize.value = burn.size || 4; watermark.value = burn.watermark || ''; safeMargins.checked = !!burn.safeMargins;
      if (options.outputSize === 'half') sizeSelect.value = 'half';
      else if (options.outputSize) { sizeSelect.value = 'custom'; outWidth.value = options.outputSize.width; outHeight.value = options.outputSize.height; }
      else sizeSelect.value = '';
      fit.value = options.fit || 'fit';
      startNumber.value = options.startNumber ?? 0; padding.value = options.padding ?? 7;
      syncFormat(); sync();
    }
    function fillPresets() {
      const keep = preset.value;
      preset.replaceChildren(new Option('Custom settings', ''));
      const groups = {};
      for (const item of [...table.presets, ...table.userPresets]) {
        const group = groups[item.group] || (groups[item.group] = el('optgroup', {label: item.group}));
        const option = new Option(item.name, item.id);
        const reason = reasonFor({...item.options, alpha: !!item.options.alpha, container: item.options.container || table.codecs[item.options.codec]?.containers[0],
          profile: item.options.profile || table.codecs[item.options.codec]?.defaultProfile});
        if (reason) { option.disabled = true; option.textContent += ' — unavailable: ' + reason; }
        group.append(option);
      }
      preset.append(...Object.values(groups));
      preset.value = [...preset.options].some(o => o.value === keep) ? keep : '';
    }
    const findPreset = id => [...(table?.presets || []), ...(table?.userPresets || [])].find(item => item.id === id);
    preset.onchange = () => {
      const item = findPreset(preset.value);
      purpose.textContent = item ? item.purpose : '';
      purpose.hidden = !item;
      if (item) apply(item.options);
    };
    let applying = false;
    const touched = () => { if (!applying && preset.value) { preset.value = ''; purpose.hidden = true; } };
    form.addEventListener('change', event => {
      if (event.target === preset) return;
      if ([codec, profile, container].includes(event.target)) {
        if (event.target === codec) fillFormat();
        else if (event.target === container) fillAudio(audioCodec.value);
      }
      if (event.target !== form.elements.rangeMode && !event.target.closest('.export-output')) touched();
      sync();
    });
    form.addEventListener('input', event => { if (event.target.type === 'number' || event.target.type === 'text') sync(); });

    /* Announce conversions, unsupported combinations and unavailable encoders before rendering. */
    let checkTimer = 0, checkSerial = 0, blocking = [];
    function scheduleCheck() { clearTimeout(checkTimer); checkTimer = setTimeout(check, 150); }
    async function check() {
      const serial = ++checkSerial;
      const request = {...options(), codec: codec.value};
      let result;
      try { result = await post('/api/delivery/check', {options: request, sequence: {width: project.width, height: project.height, fps: project.fps}}); }
      catch (error) { result = {problems: [error.message], conversions: [], unavailable: ''}; }
      if (serial !== checkSerial) return;
      blocking = [...result.problems, ...(result.unavailable ? ['Not available on this machine: ' + result.unavailable] : [])];
      notices.replaceChildren(...blocking.map(text => el('li', {className: 'export-problem', textContent: text})),
        ...result.conversions.map(text => el('li', {className: 'export-conversion', textContent: text})));
      notices.hidden = !notices.childElementCount;
    }
    context.validate = () => blocking.length ? blocking[0] : '';
    context.deliveryOptions = options;
    context.queueOnly = () => info()?.kind === 'images';
    context.renderViaQueue = () => addQueue.onclick();

    /* Queue, batch and presets. */
    async function queue(entries, overwrite = false) {
      const prepared = await context.prepareProject();
      const body = {project: prepared, jobs: entries, folder: folder.value.trim() || undefined, overwrite};
      try { return await post('/api/queue', body); }
      catch (error) {
        if (error.status === 409 && error.body?.conflict && confirm(error.message)) return post('/api/queue', {...body, overwrite: true});
        throw error;
      }
    }
    const sessionOptionKeys = ['range', 'limiter', 'limiterCeiling', 'aacPeakSafe', 'subtitleLanguage'];
    const currentOptions = () => context.requestOptions();
    addQueue.onclick = async () => {
      await check();
      const problem = context.validate() || context.formProblem();
      if (problem) { context.status(problem); return; }
      try {
        await queue([{options: currentOptions(), name: outName.value.trim() || project.name, preset: findPreset(preset.value)?.name || ''}]);
        context.status('Added to the render queue.');
        queuePanel();
      } catch (error) { context.status(error.message); }
    };
    batch.onclick = () => batchDialog(async picked => {
      const current = currentOptions();
      const shared = {};
      for (const key of sessionOptionKeys) if (key in current) shared[key] = current[key];
      const entries = picked.map(item => {
        const resolved = {...resolvePreset(item.options), ...shared};
        return {options: resolved, name: (outName.value.trim() || project.name) + ' - ' + item.name.replace(/[\\/:*?"<>|]/g, '-').slice(0, 60), preset: item.name};
      });
      await queue(entries);
      context.status(entries.length + ' exports added to the render queue.');
      queuePanel();
    });
    function resolvePreset(options) {
      const copy = structuredClone(options);
      if (copy.outputSize === 'half') copy.outputSize = {width: Math.max(16, Math.floor(project.width / 4) * 2), height: Math.max(16, Math.floor(project.height / 4) * 2)};
      return copy;
    }
    openQueue.onclick = () => queuePanel();
    savePreset.onclick = async () => {
      const name = prompt('Name this preset');
      if (!name?.trim()) return;
      const options = currentOptions();
      for (const key of sessionOptionKeys) delete options[key];
      try {
        const saved = await post('/api/delivery/presets', {presets: [...table.userPresets, {name: name.trim(), purpose: 'Saved from the Export dialog.', options}]});
        table.userPresets = saved.userPresets; fillPresets();
        preset.value = saved.userPresets.find(item => item.name === name.trim())?.id || '';
        context.status('Preset saved.');
      } catch (error) { context.status(error.message); }
    };
    managePresets.onclick = () => manageDialog(() => { fillPresets(); });

    capabilities().then(() => {
      fillCodecs(); fillFormat({}); fillPresets(); folder.placeholder = table.folder; syncFormat(); sync();
    }).catch(error => context.status('Could not read this machine’s formats: ' + error.message));
    return {sync};
  }

  function timecodeLabel(frames, fps, dropFrame) {
    const rate = Math.round(fps), drop = dropFrame !== false && (Math.abs(fps - 29.97) < .01 || Math.abs(fps - 59.94) < .01);
    let count = Math.max(0, Math.round(frames));
    if (drop) {
      const skip = rate / 15, tens = Math.floor(count / (600 * rate - 9 * skip)), rest = count % (600 * rate - 9 * skip);
      count += 9 * skip * tens + (rest > skip ? skip * Math.floor((rest - skip) / (60 * rate - skip)) : 0);
    }
    const pad = n => String(n).padStart(2, '0');
    const seconds = Math.floor(count / rate);
    return [pad(Math.floor(seconds / 3600) % 24), pad(Math.floor(seconds / 60) % 60), pad(seconds % 60)].join(':') + (drop ? ';' : ':') + pad(count % rate);
  }

  /* ---- small dialogs ------------------------------------------------------------------------ */
  function modal(heading, kind) {
    const node = el('dialog', {className: 'studio-dialog delivery-dialog'}, el('h2', {textContent: heading}));
    node.dataset.dialog = kind;
    document.body.append(node);
    node.addEventListener('close', () => node.remove(), {once: true});
    return node;
  }
  function batchDialog(submit) {
    const node = modal('Export in several presets', 'batch');
    const form = el('form', {method: 'dialog'});
    const list = el('div', {className: 'batch-list'});
    for (const item of [...table.presets, ...table.userPresets]) {
      const box = el('input', {type: 'checkbox', value: item.id});
      const reason = reasonFor({...item.options, alpha: !!item.options.alpha, container: item.options.container || table.codecs[item.options.codec]?.containers[0],
        profile: item.options.profile || table.codecs[item.options.codec]?.defaultProfile});
      box.disabled = !!reason;
      list.append(field(item.name + (reason ? ' — unavailable: ' + reason : ''), box, {className: 'export-check', title: item.purpose}));
    }
    const status = el('p', {role: 'status', className: 'render-state'});
    const footer = el('footer', {}, el('button', {type: 'button', textContent: 'Cancel', onclick: () => node.close()}), el('button', {className: 'primary', textContent: 'Add to queue'}));
    form.append(el('p', {className: 'export-help', textContent: 'Each checked preset becomes its own job for this sequence and range, one after another.'}), list, status, footer);
    form.onsubmit = async event => {
      event.preventDefault();
      const picked = [...list.querySelectorAll('input:checked')].map(box => [...table.presets, ...table.userPresets].find(item => item.id === box.value));
      if (!picked.length) { status.textContent = 'Check at least one preset.'; return; }
      try { await submit(picked); node.close(); } catch (error) { status.textContent = error.message; }
    };
    node.append(form); node.showModal();
  }
  function manageDialog(changed) {
    const node = modal('My presets', 'presets');
    const list = el('ul', {className: 'preset-list'});
    const status = el('p', {role: 'status', className: 'render-state'});
    const save = async presets => { const result = await post('/api/delivery/presets', {presets}); table.userPresets = result.userPresets; render(); changed(); };
    function render() {
      list.replaceChildren(...table.userPresets.map(item => {
        const row = el('li', {}, el('span', {textContent: item.name}));
        row.append(el('button', {type: 'button', textContent: 'Rename', onclick: async () => {
          const name = prompt('Rename preset', item.name); if (!name?.trim()) return;
          try { await save(table.userPresets.map(p => p.id === item.id ? {...p, id: undefined, name: name.trim()} : p).map(stripId)); status.textContent = 'Renamed.'; } catch (error) { status.textContent = error.message; }
        }}), el('button', {type: 'button', textContent: 'Delete', onclick: async () => {
          if (!confirm('Delete the preset ' + item.name + '?')) return;
          try { await save(table.userPresets.filter(p => p.id !== item.id)); status.textContent = 'Deleted.'; } catch (error) { status.textContent = error.message; }
        }}), el('button', {type: 'button', textContent: 'Export', onclick: () => download(item.name + '.splice-preset.json', {splicePresets: 1, presets: [stripId(item)]})}));
        return row;
      }));
      if (!table.userPresets.length) list.append(el('li', {className: 'export-help', textContent: 'No presets of your own yet. Use “Save preset…” in the Export dialog.'}));
    }
    const stripId = item => { const copy = {...item}; delete copy.id; delete copy.group; return copy; };
    const file = el('input', {type: 'file', accept: '.json,application/json', hidden: true});
    file.onchange = async () => {
      const chosen = file.files[0]; if (!chosen) return;
      try {
        const imported = await post('/api/delivery/presets/import', {text: await chosen.text()});
        const names = new Set(table.userPresets.map(p => p.name.toLowerCase()));
        const fresh = imported.presets.filter(p => !names.has(p.name.toLowerCase())).map(stripId);
        await save([...table.userPresets, ...fresh]);
        status.textContent = 'Imported ' + fresh.length + ' preset' + (fresh.length === 1 ? '' : 's') + (fresh.length < imported.presets.length ? ' (names already used were skipped).' : '.');
      } catch (error) { status.textContent = 'Not imported: ' + error.message; }
      file.value = '';
    };
    const footer = el('footer', {},
      el('button', {type: 'button', textContent: 'Import…', onclick: () => file.click()}),
      el('button', {type: 'button', textContent: 'Export all', onclick: () => download('Splice presets.json', {splicePresets: 1, presets: table.userPresets.map(stripId)})}),
      el('button', {type: 'button', className: 'primary', textContent: 'Close', onclick: () => node.close()}));
    node.append(list, file, status, footer);
    render(); node.showModal();
  }
  function download(name, value) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + '\n'], {type: 'application/json'}));
    const anchor = el('a', {href: url, download: name}); anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /* ---- the render queue panel --------------------------------------------------------------- */
  let panel = null, timer = 0, lastStates = {};
  const STATE_TEXT = {queued: 'Waiting', held: 'Held', rendering: 'Rendering', complete: 'Done', failed: 'Failed', cancelled: 'Cancelled', interrupted: 'Interrupted'};
  function duration(seconds) {
    if (!Number.isFinite(seconds)) return '';
    seconds = Math.round(seconds);
    return seconds >= 3600 ? Math.floor(seconds / 3600) + ' h ' + Math.floor(seconds / 60 % 60) + ' min' : seconds >= 60 ? Math.floor(seconds / 60) + ' min ' + seconds % 60 + ' s' : seconds + ' s';
  }
  function queuePanel() {
    if (panel?.isConnected) { panel.open || panel.showModal(); panel.refresh(); return panel; }
    panel = modal('Render queue', 'queue');
    const toolbar = el('div', {className: 'queue-toolbar'});
    const pause = el('button', {type: 'button', className: 'queue-pause'});
    const notify = el('input', {type: 'checkbox', name: 'queueNotify', checked: localStorage.getItem('splice-queue-notify') === '1'});
    notify.onchange = () => { localStorage.setItem('splice-queue-notify', notify.checked ? '1' : '0'); if (notify.checked && window.Notification?.permission === 'default') Notification.requestPermission(); };
    const turnover = el('button', {type: 'button', className: 'queue-turnover', textContent: 'Turnover package…', onclick: () => turnoverDialog(() => refresh())});
    toolbar.append(pause, turnover, field('Sound and notification when a job finishes', notify, {className: 'export-check'}));
    const list = el('ol', {className: 'queue-list'});
    const status = el('p', {role: 'status', className: 'render-state'});
    const footer = el('footer', {}, el('button', {type: 'button', className: 'primary', textContent: 'Close', onclick: () => panel.close()}));
    panel.append(toolbar, list, status, footer);
    panel.addEventListener('close', () => { clearInterval(timer); timer = 0; panel = null; });
    pause.onclick = async () => { try { render(await post('/api/queue/pause', {paused: pause.dataset.paused !== '1'})); } catch (error) { status.textContent = error.message; } };
    async function act(id, action) {
      try { render(await post('/api/queue/action', {id, action})); status.textContent = ''; } catch (error) { status.textContent = error.message; }
    }
    function render(data) {
      pause.dataset.paused = data.paused ? '1' : '0';
      pause.textContent = data.paused ? 'Resume queue' : 'Pause queue';
      pause.title = data.paused ? 'Start the next waiting job' : 'Finish the current job, then wait';
      list.replaceChildren(...data.jobs.map(job => {
        const row = el('li', {className: 'queue-job state-' + job.state});
        row.dataset.id = job.id;
        const head = el('div', {className: 'queue-head'}, el('strong', {textContent: job.name}), el('span', {className: 'queue-state', textContent: STATE_TEXT[job.state] || job.state}));
        const detail = el('div', {className: 'queue-detail', textContent: [job.preset, job.summary, job.sequence !== job.name ? 'from ' + job.sequence : ''].filter(Boolean).join(' · ')});
        row.append(head, detail);
        if (job.state === 'rendering') {
          row.append(el('progress', {max: 1, value: job.progress || 0}), el('div', {className: 'queue-eta', textContent: Math.round(100 * (job.progress || 0)) + '%' + (job.eta != null ? ' · about ' + duration(job.eta) + ' left' : '')}));
        }
        if (job.error) row.append(el('p', {className: 'queue-error', textContent: job.error}));
        if (job.state === 'complete') {
          const audio = job.report?.audio;
          const lines = [job.outputs?.[0] && 'Saved to ' + job.outputs[0], job.finished && job.started && 'Rendered in ' + duration(job.finished - job.started)];
          if (audio && Number.isFinite(audio.decodedTruePeakDb)) lines.push('Decoded audio peak ' + audio.decodedTruePeakDb.toFixed(1) + ' dBTP · ' + (audio.loudnessLufs ?? -Infinity).toFixed(1) + ' LUFS');
          if (job.report?.lightLevels) lines.push('MaxCLL ' + job.report.lightLevels.maxCll + ' · MaxFALL ' + job.report.lightLevels.maxFall + ' nits');
          if (job.report?.delivery?.timecode) lines.push('Timecode starts ' + job.report.delivery.timecode);
          for (const line of lines.filter(Boolean)) row.append(el('p', {className: 'queue-output', textContent: line}));
          for (const note of job.report?.delivery?.notes || []) row.append(el('p', {className: 'queue-note', textContent: note}));
        }
        if (job.warnings?.length) row.append(el('ul', {className: 'export-warnings'}, ...job.warnings.map(text => el('li', {textContent: text}))));
        const actions = el('div', {className: 'queue-actions'});
        const button = (text, action, label) => { const node = el('button', {type: 'button', textContent: text, onclick: () => act(job.id, action)}); node.setAttribute('aria-label', label + ' ' + job.name); node.dataset.action = action; return node; };
        if (['queued', 'held'].includes(job.state)) actions.append(button('↑', 'up', 'Move up'), button('↓', 'down', 'Move down'), button(job.state === 'held' ? 'Release' : 'Hold', job.state === 'held' ? 'release' : 'hold', job.state === 'held' ? 'Release' : 'Hold'));
        if (['queued', 'held', 'rendering'].includes(job.state)) actions.append(button('Cancel', 'cancel', 'Cancel'));
        if (['failed', 'cancelled', 'interrupted', 'complete'].includes(job.state)) actions.append(button(job.state === 'complete' ? 'Render again' : 'Retry', 'retry', 'Retry'));
        if (job.state === 'complete') {
          const reveal = el('button', {type: 'button', textContent: 'Reveal file', onclick: async () => {
            try { const result = await post('/api/queue/reveal', {id: job.id}); status.textContent = 'In ' + result.path; } catch (error) { status.textContent = error.message; }
          }});
          reveal.dataset.action = 'reveal';
          actions.append(reveal);
        }
        if (job.state !== 'rendering') actions.append(button('Remove', 'remove', 'Remove'));
        row.append(actions);
        // A job that just finished: optional sound and notification.
        if (lastStates[job.id] === 'rendering' && job.state !== 'rendering' && localStorage.getItem('splice-queue-notify') === '1') announce(job);
        return row;
      }));
      lastStates = Object.fromEntries(data.jobs.map(job => [job.id, job.state]));
      if (!data.jobs.length) list.append(el('li', {className: 'export-help', textContent: 'No exports queued. Use “Add to queue” or “Batch…” in the Export dialog.'}));
    }
    let refreshing = false;
    const currentPanel = panel;
    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      try {
        const data = await json('/api/queue');
        if (currentPanel.open) render(data);
      } catch (error) {
        status.textContent = error.message;
      } finally {
        refreshing = false;
      }
    }
    panel.refresh = refresh;
    refresh();
    timer = setInterval(() => { if (!document.hidden) refresh(); }, 1000);
    panel.showModal();
    return panel;
  }
  /* A turnover package (timeline files + report) whose reference movie the queue renders, with
   * timecode and clip-name burn-ins, over the exported timeline's range and start timecode. */
  function turnoverDialog(done) {
    const node = modal('Turnover package with reference movie', 'turnover');
    const form = el('form', {method: 'dialog'});
    const project = Editor.get().project;
    const boxes = [['fcpxml', 'FCPXML (Resolve, Final Cut Pro)', true], ['xmeml', 'Final Cut Pro 7 XML (Premiere, Avid)', false],
      ['edl', 'CMX3600 EDL', true], ['otio', 'OpenTimelineIO', false]].map(([value, text, checked]) =>
      field(text, el('input', {type: 'checkbox', value, checked}), {className: 'export-check'}));
    const movie = select('movie', [['h264', 'H.264 review movie (MP4)'], ['prores', 'ProRes 422 Proxy (MOV)']]);
    const marks = project.marks || {};
    const range = select('range', [['all', 'Entire sequence'], ...(Number.isFinite(marks.in) || Number.isFinite(marks.out) ? [['marks', 'In to Out marks']] : [])]);
    const name = el('input', {name: 'name', type: 'text', maxLength: 120, value: (project.name || 'Untitled').replace(/[^A-Za-z0-9_-]+/g, '-')});
    const status = el('p', {role: 'status', className: 'render-state'});
    form.append(el('p', {className: 'export-help', textContent: 'The timeline files and report are written now; the reference movie renders in the queue and goes into the same package, matching the timeline’s range and start timecode, with timecode and clip-name burn-ins.'}),
      ...boxes, field('Reference movie', movie), field('Range', range), field('Package name', name), status,
      el('footer', {}, el('button', {type: 'button', textContent: 'Cancel', onclick: () => node.close()}), el('button', {className: 'primary', textContent: 'Add to queue'})));
    form.onsubmit = async event => {
      event.preventDefault();
      const formats = boxes.map(label => label.querySelector('input')).filter(box => box.checked).map(box => box.value);
      if (!formats.length) { status.textContent = 'Choose at least one timeline format.'; return; }
      const body = {project: await Editor.prepareExport(), formats, movie: movie.value, name: name.value.trim() || undefined};
      if (range.value === 'marks') body.range = {start: Number.isFinite(marks.in) ? marks.in : 0, end: Number.isFinite(marks.out) ? marks.out : Editor.get().duration};
      try {
        try { await post('/api/queue/turnover', body); }
        catch (error) { if (error.status === 409 && confirm(error.message)) await post('/api/queue/turnover', {...body, overwrite: true}); else throw error; }
        node.close(); done();
      } catch (error) { status.textContent = error.message; }
    };
    node.append(form); node.showModal();
  }
  function announce(job) {
    try {
      const audio = new AudioContext(), tone = audio.createOscillator(), gain = audio.createGain();
      tone.frequency.value = job.state === 'complete' ? 880 : 330; gain.gain.value = .08;
      tone.connect(gain).connect(audio.destination); tone.start(); tone.stop(audio.currentTime + .25);
      tone.onended = () => audio.close();
    } catch (error) { console.warn('Could not play the render queue notification', error); }
    if (window.Notification?.permission === 'granted') new Notification('Splice · ' + (STATE_TEXT[job.state] || job.state), {body: job.name});
  }
  // Check the queue once at start: a restarted server resumes waiting jobs as soon as Splice opens.
  addEventListener('load', () => { json('/api/queue').catch(error => console.warn('Could not resume the render queue', error)); });
  return {enhance, queuePanel, capabilities, timecodeLabel};
})();
