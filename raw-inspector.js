/* Camera RAW Inspector controls (Slice R).
 *
 * Shown automatically in the Inspector whenever a Camera RAW video clip is selected
 * (Edit and Color workspaces). Changes are stored on clip.rawSettings via
 * Editor.updateClip() so every adjustment participates in Undo/Redo, save/reopen,
 * live preview re-decode (/api/raw/preview), and full-res 16-bit export.
 */
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  const num = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;

  const COLOR_SPACES = {
    cinemadng: [
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
      ['rec2020', 'Rec.2020'],
      ['bmd_wide_gamut_gen5', 'BMD Wide Gamut Gen 5'],
    ],
    prores_raw: [
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
      ['rec2020', 'Rec.2020'],
    ],
    braw: [
      ['bmd_wide_gamut_gen5', 'BMD Wide Gamut Gen 5'],
      ['rec709', 'Rec.709'],
      ['rec2020', 'Rec.2020'],
      ['linear', 'Linear'],
    ],
    r3d: [
      ['red_wide_gamut', 'REDWideGamutRGB'],
      ['rec709', 'Rec.709'],
      ['rec2020', 'Rec.2020'],
      ['linear', 'Linear'],
    ],
    arriraw: [
      ['arri_wide_gamut_4', 'ARRI Wide Gamut 4'],
      ['arri_wide_gamut_3', 'ARRI Wide Gamut 3'],
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
    ],
    default: [
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
      ['rec2020', 'Rec.2020'],
      ['bmd_wide_gamut_gen5', 'BMD Wide Gamut Gen 5'],
      ['red_wide_gamut', 'REDWideGamutRGB'],
      ['arri_wide_gamut_4', 'ARRI Wide Gamut 4'],
    ],
  };

  const GAMMAS = {
    cinemadng: [
      ['rec709', 'Rec.709'],
      ['cinemadng_log', 'CinemaDNG Log'],
      ['linear', 'Linear'],
      ['bmd_film_gen5', 'BMD Film Gen 5'],
    ],
    prores_raw: [
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
      ['cinemadng_log', 'CinemaDNG Log'],
    ],
    braw: [
      ['bmd_film_gen5', 'BMD Film Gen 5'],
      ['bmd_extended_video', 'BMD Extended Video'],
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
    ],
    r3d: [
      ['log3g10', 'Log3G10'],
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
    ],
    arriraw: [
      ['arri_logc4', 'ARRI LogC4'],
      ['arri_logc3', 'ARRI LogC3'],
      ['rec709', 'Rec.709'],
      ['linear', 'Linear'],
    ],
    default: [
      ['rec709', 'Rec.709'],
      ['cinemadng_log', 'CinemaDNG Log'],
      ['linear', 'Linear'],
      ['bmd_film_gen5', 'BMD Film Gen 5'],
      ['log3g10', 'Log3G10'],
      ['arri_logc4', 'ARRI LogC4'],
    ],
  };

  const VALID_COLOR_SPACES = new Set([
    'rec709', 'linear', 'rec2020', 'bmd_wide_gamut_gen5',
    'red_wide_gamut', 'arri_wide_gamut_3', 'arri_wide_gamut_4',
  ]);
  const VALID_GAMMAS = new Set([
    'rec709', 'linear', 'cinemadng_log', 'bmd_film_gen5',
    'bmd_extended_video', 'log3g10', 'arri_logc3', 'arri_logc4',
  ]);
  const VALID_DEBAYER = new Set(['full', 'half', 'quarter', 'eighth']);

  function defaultsFor(source) {
    const base = source?.rawDefaults || {};
    return {
      decodeUsing: 'camera',
      iso: num(base.iso, 800),
      exposure: num(base.exposure, 0),
      kelvin: num(base.kelvin, 5600),
      tint: num(base.tint, 0),
      colorSpace: base.colorSpace || 'rec709',
      gamma: base.gamma || 'rec709',
      highlightRecovery: !!base.highlightRecovery,
      debayer: base.debayerQuality || base.debayer || 'half',
    };
  }

  function effectiveSettings(clip, source) {
    const defs = defaultsFor(source);
    const saved = clip?.rawSettings && typeof clip.rawSettings === 'object' ? clip.rawSettings : {};
    const decodeUsing = saved.decodeUsing === 'clip' ? 'clip' : 'camera';
    const rawDebayer = saved.debayer ?? saved.debayerQuality;
    const debayer = VALID_DEBAYER.has(rawDebayer) ? rawDebayer : defs.debayer;
    if (decodeUsing === 'camera') {
      return {...defs, decodeUsing: 'camera', debayer};
    }
    return {
      decodeUsing: 'clip',
      iso: Math.round(Math.max(50, Math.min(102400, num(saved.iso, defs.iso)))),
      exposure: Math.round(Math.max(-4, Math.min(4, num(saved.exposure, defs.exposure))) * 100) / 100,
      kelvin: Math.round(Math.max(2000, Math.min(15000, num(saved.kelvin, defs.kelvin)))),
      tint: Math.round(Math.max(-100, Math.min(100, num(saved.tint, defs.tint))) * 10) / 10,
      colorSpace: VALID_COLOR_SPACES.has(saved.colorSpace) ? saved.colorSpace : defs.colorSpace,
      gamma: VALID_GAMMAS.has(saved.gamma) ? saved.gamma : defs.gamma,
      highlightRecovery: saved.highlightRecovery === true,
      debayer,
    };
  }

  function validateRawSettings(settings) {
    if (settings == null) return;
    if (typeof settings !== 'object' || Array.isArray(settings)) {
      throw new Error('This project file has invalid Camera RAW settings.');
    }
    if (settings.decodeUsing !== undefined && !['camera', 'clip'].includes(settings.decodeUsing)) {
      throw new Error('This project file has invalid Camera RAW decode mode.');
    }
    if (settings.iso !== undefined && (!Number.isFinite(settings.iso) || settings.iso < 50 || settings.iso > 102400)) {
      throw new Error('This project file has an invalid Camera RAW ISO.');
    }
    if (settings.exposure !== undefined && (!Number.isFinite(settings.exposure) || settings.exposure < -4 || settings.exposure > 4)) {
      throw new Error('This project file has an invalid Camera RAW exposure.');
    }
    if (settings.kelvin !== undefined && (!Number.isFinite(settings.kelvin) || settings.kelvin < 2000 || settings.kelvin > 15000)) {
      throw new Error('This project file has an invalid Camera RAW white balance.');
    }
    if (settings.tint !== undefined && (!Number.isFinite(settings.tint) || settings.tint < -100 || settings.tint > 100)) {
      throw new Error('This project file has an invalid Camera RAW tint.');
    }
    if (settings.colorSpace !== undefined && !VALID_COLOR_SPACES.has(settings.colorSpace)) {
      throw new Error('This project file has an invalid Camera RAW color space.');
    }
    if (settings.gamma !== undefined && !VALID_GAMMAS.has(settings.gamma)) {
      throw new Error('This project file has an invalid Camera RAW gamma.');
    }
    if (settings.highlightRecovery !== undefined && typeof settings.highlightRecovery !== 'boolean') {
      throw new Error('This project file has an invalid Camera RAW highlight recovery setting.');
    }
    if (settings.debayer !== undefined && !VALID_DEBAYER.has(settings.debayer)) {
      throw new Error('This project file has an invalid Camera RAW debayer quality.');
    }
    if (settings.debayerQuality !== undefined && !VALID_DEBAYER.has(settings.debayerQuality)) {
      throw new Error('This project file has an invalid Camera RAW debayer quality.');
    }
  }

  function optionList(entries, selected, allowed = null) {
    const has = entries.some(([value]) => value === selected);
    const list = has ? entries : [[selected, selected], ...entries];
    return list.map(([value, title]) => {
      const off = allowed && !allowed(value) && value !== selected;
      return `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}${off ? ' disabled' : ''}>${esc(title)}</option>`;
    }).join('');
  }

  const SETTING_NAMES = {
    iso: 'ISO', exposure: 'exposure', kelvin: 'white balance', tint: 'tint', colorSpace: 'color space',
    gamma: 'gamma', highlightRecovery: 'highlight recovery', debayer: 'debayer quality',
  };

  // What the clip's decoder applies (source.rawCapabilities from the probe). No capabilities: the
  // decoder applies every setting (Splice's own CinemaDNG developer or a Splice-contract bridge).
  function capabilitiesOf(source) {
    const caps = source?.rawCapabilities;
    if (!caps || typeof caps !== 'object' || !Array.isArray(caps.settings)) return null;
    return caps;
  }

  function takes(caps, key) {
    return !caps || caps.settings.includes(key);
  }

  function colorSpaceAllowed(caps, value) {
    return !caps || (takes(caps, 'colorSpace') && (!Array.isArray(caps.colorSpaces) || caps.colorSpaces.includes(value)));
  }

  function gammaAllowed(caps, value, colorSpace) {
    if (!caps) return true;
    if (!takes(caps, 'gamma') || (Array.isArray(caps.gammas) && !caps.gammas.includes(value))) return false;
    return !Array.isArray(caps.pairs) || caps.pairs.some(([cs, gamma]) => cs === colorSpace && gamma === value);
  }

  // Look values the file did not record: the Inspector shows the placeholder and says so.
  function estimatedLook(source) {
    const list = Array.isArray(source?.rawEstimated) ? source.rawEstimated : [];
    return new Set(['iso', 'kelvin', 'tint', 'camera'].filter(key => list.includes(key)));
  }

  function markup(clip, source) {
    if (!source || source.raw !== true || clip?.type !== 'video') return '';
    const fmt = source.rawFormat || 'default';
    const settings = effectiveSettings(clip, source);
    const meta = source.rawMetadata || {};
    const formatBadge = source.rawFormatLabel || 'Camera RAW';
    const bitSuffix = meta.bitDepth && !formatBadge.includes(`${meta.bitDepth}-bit`) ? ` ${meta.bitDepth}-bit` : '';
    const decoderAvailable = source.rawDecoderAvailable !== false;
    const decoderBadge = decoderAvailable
      ? `<span class="raw-badge raw-badge-decoder" title="Active RAW decoder">${esc(source.rawDecoderName || 'Native decoder')}</span>`
      : `<span class="raw-badge raw-badge-warn" title="Install vendor SDK or attach proxy">Decoder missing</span>`;
    const caps = capabilitiesOf(source);
    const estimated = estimatedLook(source);
    // In Camera Metadata mode the look IS the file's metadata, so a placeholder is flagged on its control.
    const flag = key => estimated.has(key) && settings.decodeUsing === 'camera'
      ? `<em class="raw-est" data-raw-est="${key}" title="Not recorded in the file: Splice placeholder">estimated</em>` : '';
    const metaParts = [];
    if (meta.camera) metaParts.push(estimated.has('camera') ? `${meta.camera} (model not in the file)` : meta.camera);
    if (source.reel) metaParts.push('Reel ' + source.reel);
    if (source.timecode) metaParts.push('TC ' + source.timecode);
    if (meta.frameCount > 1) metaParts.push(`${meta.frameCount} frames`);
    if (meta.missingFrames > 0) metaParts.push(`${meta.missingFrames} frame numbers missing: later frames play early`);
    const names = {timecode: 'timecode', fps: 'frame rate', duration: 'duration', width: 'frame size',
      iso: 'ISO', kelvin: 'white balance', tint: 'tint', camera: 'camera model'};
    const guessed = [...new Set((source.rawEstimated || []).filter(key => key !== 'height').map(key => names[key]).filter(Boolean))];
    const job = window.SpliceMedia?.rawPreviewStatus?.(source, clip.rawSettings);
    const statusText = job?.pending ? 'Decoding Camera RAW preview…' : (job?.error ? job.error : '');
    const statusClass = job?.error ? 'raw-pending raw-error' : 'raw-pending';
    const csList = COLOR_SPACES[fmt] || COLOR_SPACES.default;
    const gmList = GAMMAS[fmt] || GAMMAS.default;
    const off = key => takes(caps, key) ? '' : ` disabled data-raw-unsupported title="${esc(caps.tool)} cannot take ${SETTING_NAMES[key]}"`;
    const ignored = caps ? Object.keys(SETTING_NAMES).filter(key => !takes(caps, key)).map(key => SETTING_NAMES[key]) : [];
    return `<section class="splice-section splice-raw-section" data-raw-panel>` +
      `<h3>Camera RAW</h3>` +
      `<div class="splice-raw">` +
        `<div class="raw-badges">` +
          `<span class="raw-badge" data-raw-format-badge>${esc(formatBadge + bitSuffix)}</span>` +
          decoderBadge +
        `</div>` +
        (metaParts.length ? `<p class="raw-camera-meta">${esc(metaParts.join(' · '))}</p>` : '') +
        (guessed.length ? `<p class="raw-camera-meta raw-estimated" data-raw-estimated>Not in the file: ${esc(guessed.join(', '))}. ` +
          `Splice shows a placeholder, marked “estimated”; interchange reports say so.</p>` : '') +
        (ignored.length ? `<p class="raw-camera-meta raw-unsupported" data-raw-unsupported-note>${esc(caps.tool)} cannot take ` +
          `${esc(ignored.join(', '))}: ${ignored.length === 1 ? 'that control is' : 'those controls are'} off for this clip.</p>` : '') +
        (!decoderAvailable ? `<div class="raw-status-banner" role="status">` +
          `<p>${esc(source.rawDecoderMessage || 'Native vendor RAW decoder is not installed yet. Container metadata and audio were imported.')}</p>` +
          `<div class="raw-banner-actions">` +
            `<button type="button" data-raw-action="open-decoders">Configure RAW Decoders…</button>` +
            `<button type="button" data-raw-action="attach-proxy">Attach Proxy…</button>` +
          `</div>` +
        `</div>` : '') +
        (source.rawAttachedProxy ? `<div class="raw-attached-proxy"><span>Attached proxy active</span>` +
          `<button type="button" data-raw-action="detach-proxy">Remove</button></div>` : '') +
        `<input type="file" class="raw-proxy-file" accept="video/*,.mov,.mp4,.mxf,.mkv" hidden>` +
        `<label class="splice-number"><span>Decode using</span>` +
          `<select aria-label="RAW Decode Using" data-raw-field="decodeUsing">` +
            `<option value="camera" ${settings.decodeUsing === 'camera' ? 'selected' : ''}>Camera Metadata</option>` +
            `<option value="clip" ${settings.decodeUsing === 'clip' ? 'selected' : ''}>Clip</option>` +
          `</select></label>` +
        `<label class="splice-number"><span>ISO ${flag('iso')}</span>` +
          `<input aria-label="RAW ISO" type="number" data-raw-field="iso" min="50" max="102400" step="50" value="${settings.iso}"${off('iso')}></label>` +
        `<label class="splice-control"><span>Exposure<output data-raw-out="exposure">${settings.exposure >= 0 ? '+' : ''}${settings.exposure.toFixed(1)} EV</output></span>` +
          `<input aria-label="RAW Exposure" type="range" data-raw-field="exposure" min="-4" max="4" step="0.1" value="${settings.exposure}"${off('exposure')}></label>` +
        `<label class="splice-control"><span>White Balance ${flag('kelvin')}<output data-raw-out="kelvin">${settings.kelvin}K</output></span>` +
          `<input aria-label="RAW White Balance Kelvin" type="range" data-raw-field="kelvin" min="2000" max="15000" step="50" value="${settings.kelvin}"${off('kelvin')}></label>` +
        `<label class="splice-control"><span>Tint ${flag('tint')}<output data-raw-out="tint">${settings.tint >= 0 ? '+' : ''}${settings.tint}</output></span>` +
          `<input aria-label="RAW Tint" type="range" data-raw-field="tint" min="-100" max="100" step="1" value="${settings.tint}"${off('tint')}></label>` +
        `<label class="splice-number"><span>Color space</span>` +
          `<select aria-label="RAW Color Space" data-raw-field="colorSpace"${off('colorSpace')}>${optionList(csList, settings.colorSpace, value => colorSpaceAllowed(caps, value))}</select></label>` +
        `<label class="splice-number"><span>Gamma</span>` +
          `<select aria-label="RAW Gamma" data-raw-field="gamma"${off('gamma')}>${optionList(gmList, settings.gamma, value => gammaAllowed(caps, value, settings.colorSpace))}</select></label>` +
        `<label class="splice-safe-toggle">` +
          `<input aria-label="RAW Highlight Recovery" type="checkbox" data-raw-field="highlightRecovery" ${settings.highlightRecovery ? 'checked' : ''}${off('highlightRecovery')}> Highlight recovery</label>` +
        `<label class="splice-number"><span>Preview debayer</span>` +
          `<select aria-label="RAW Preview Debayer Quality" data-raw-field="debayer"${off('debayer')}>` +
            `<option value="full" ${settings.debayer === 'full' ? 'selected' : ''}>Full</option>` +
            `<option value="half" ${settings.debayer === 'half' ? 'selected' : ''}>Half</option>` +
            `<option value="quarter" ${settings.debayer === 'quarter' ? 'selected' : ''}>Quarter</option>` +
          `</select></label>` +
        `<p class="${statusClass}" data-raw-status aria-live="polite">${esc(statusText)}</p>` +
        `<div class="raw-actions">` +
          `<button type="button" data-raw-action="reset">Reset to Camera Metadata</button>` +
          `<button type="button" data-raw-action="open-decoders">RAW Decoders…</button>` +
        `</div>` +
      `</div>` +
    `</section>`;
  }

  function bind(element, clip, editor, rerender) {
    const panel = element.querySelector('[data-raw-panel]');
    if (!panel) return;
    const locked = !!editor.isTrackLocked?.(clip.track, clip.type);
    const current = () => editor.get().selected;
    const currentSource = () => {
      const state = editor.get();
      const active = state.selected;
      if (!active) return null;
      // A multicam clip decodes its active angle; a subclip decodes its original media.
      return editor.resolveClipMedia ? editor.resolveClipMedia(active, state.project).item
        : state.project.media.find(item => item.id === active.mediaId) || null;
    };

    if (locked) {
      for (const node of panel.querySelectorAll('[data-raw-field],[data-raw-action="reset"],[data-raw-action="attach-proxy"],[data-raw-action="detach-proxy"]')) {
        node.disabled = true;
      }
    }
    // Controls the clip's decoder cannot take were rendered disabled: keep them so even if a
    // shared helper re-enables Inspector inputs.
    for (const node of panel.querySelectorAll('[data-raw-unsupported]')) node.disabled = true;

    const formatOutput = (field, raw) => {
      const v = Number(raw);
      if (field === 'exposure') return `${v >= 0 ? '+' : ''}${v.toFixed(1)} EV`;
      if (field === 'kelvin') return `${Math.round(v)}K`;
      if (field === 'tint') return `${v >= 0 ? '+' : ''}${Math.round(v)}`;
      return String(raw);
    };

    const commit = nextSettings => {
      const active = current();
      const source = currentSource();
      if (!active || active.id !== clip.id || !source) return;
      if (editor.isTrackLocked?.(active.track, active.type)) return;
      editor.updateClip(active.id, {rawSettings: nextSettings});
      if (source.rawDecoderAvailable !== false) {
        window.SpliceMedia?.prepareRawPreview?.(source, nextSettings);
      }
      rerender();
    };

    for (const input of panel.querySelectorAll('[data-raw-field]')) {
      const key = input.dataset.rawField;
      input.addEventListener('input', () => {
        const out = panel.querySelector(`[data-raw-out="${key}"]`);
        if (out) out.textContent = formatOutput(key, input.value);
      });
      input.addEventListener('change', () => {
        const active = current();
        const source = currentSource();
        if (!active || active.id !== clip.id || !source) return;
        const base = effectiveSettings(active, source);
        const value = input.type === 'checkbox'
          ? input.checked
          : (input.type === 'number' || input.type === 'range') ? Number(input.value) : input.value;
        if ((input.type === 'number' || input.type === 'range') && !Number.isFinite(value)) {
          rerender();
          return;
        }
        if (key === 'decodeUsing') {
          if (value === 'camera') {
            commit({...defaultsFor(source), decodeUsing: 'camera', debayer: base.debayer});
          } else {
            commit({...base, decodeUsing: 'clip'});
          }
          return;
        }
        const next = {...base, [key]: value};
        if (key !== 'debayer') next.decodeUsing = 'clip';
        // A decoder that only renders some colour space + gamma pairs: follow the colour space
        // with the first gamma it pairs with, instead of exporting a combination it cannot render.
        const caps = capabilitiesOf(source);
        if (key === 'colorSpace' && !gammaAllowed(caps, next.gamma, next.colorSpace)) {
          const gamma = (GAMMAS[source.rawFormat] || GAMMAS.default).map(([g]) => g)
            .find(g => gammaAllowed(caps, g, next.colorSpace));
          if (gamma) next.gamma = gamma;
        }
        commit(next);
      });
    }

    panel.querySelector('[data-raw-action="reset"]')?.addEventListener('click', () => {
      const source = currentSource();
      if (!source) return;
      const base = defaultsFor(source);
      commit({...base, decodeUsing: 'camera'});
    });

    for (const btn of panel.querySelectorAll('[data-raw-action="open-decoders"]')) {
      btn.addEventListener('click', () => window.SpliceRawSettings?.open?.());
    }

    // The proxy belongs to the RAW file itself, not to a multicam or subclip wrapper.
    const sourceId = currentSource()?.id || clip.mediaId;
    const proxyInput = panel.querySelector('.raw-proxy-file');
    panel.querySelector('[data-raw-action="attach-proxy"]')?.addEventListener('click', () => proxyInput?.click());
    proxyInput?.addEventListener('change', async () => {
      const file = proxyInput.files?.[0];
      if (!file) return;
      const status = panel.querySelector('[data-raw-status]');
      if (status) { status.className = 'raw-pending'; status.textContent = 'Attaching proxy…'; }
      try {
        const imported = await window.SpliceMedia.importFile(file);
        // The RAW original stays the clip's source: the attached file only stands in for its
        // picture (preview: its edit proxy; export: the attached file itself) until a decoder exists.
        editor.editProject(project => {
          const item = project.media.find(entry => entry.id === sourceId);
          if (item) {
            item.rawAttachedProxy = imported.originalSrc || imported.src;
            item.rawAttachedPreview = imported.src;
          }
        });
        editor.refreshMedia?.();
        rerender();
      } catch (error) {
        if (status) { status.className = 'raw-pending raw-error'; status.textContent = error.message || 'Could not attach proxy.'; }
      }
    });

    panel.querySelector('[data-raw-action="detach-proxy"]')?.addEventListener('click', () => {
      editor.editProject(project => {
        const item = project.media.find(entry => entry.id === sourceId);
        if (item) {
          delete item.rawAttachedProxy;
          delete item.rawAttachedPreview;
        }
      });
      editor.refreshMedia?.();
      rerender();
    });
  }

  window.SpliceRawUI = {
    markup,
    bind,
    validateRawSettings,
    defaultsFor,
    effectiveSettings,
    capabilitiesOf,
  };
})();
