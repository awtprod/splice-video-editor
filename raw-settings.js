/* RAW Decoders Settings dialog (Slice R).
 *
 * Displays live status of all Tier 1 (built-in / open-source) and Tier 2
 * (auto-detected vendor SDK / CLI) Camera RAW backends via GET /api/raw/decoders
 * and allows configuring custom local SDK/CLI paths via POST /api/raw/settings.
 */
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      signal: AbortSignal.timeout(15000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function renderRows(container, decoders) {
    container.innerHTML = decoders.map(dec => {
      const stateClass = dec.available ? 'available' : 'missing';
      const stateLabel = dec.available ? 'Available' : 'Not installed';
      const tierLabel = !dec.configurable ? 'Built in' : dec.id === 'libraw' ? 'Open-source decoder' : 'Vendor decoder';
      const exts = (dec.extensions || []).join(', ');
      const detail = (dec.available
        ? `${esc(dec.version || dec.activeBackend || 'Detected')}${dec.path ? ` · <code>${esc(dec.path)}</code>` : ''}`
        : esc(dec.guidance || dec.installHint || 'Install the vendor decoder to enable this format.')) +
        (!dec.available && dec.sdkPath ? ` <span class="raw-decoder-sdk">SDK found: <code>${esc(dec.sdkPath)}</code></span>` : '');
      const pathControl = dec.configurable
        ? `<div class="raw-decoder-path-row">` +
            `<input type="text" data-decoder-path="${esc(dec.id)}" value="${esc(dec.configuredPath || '')}" ` +
              `placeholder="Path to the decoder program${dec.envVar ? ` (or set ${esc(dec.envVar)})` : ''}" ` +
              `aria-label="${esc(dec.label)} decoder path">` +
            `<button type="button" data-decoder-clear="${esc(dec.id)}" title="Clear custom path">Clear</button>` +
          `</div>`
        : '';
      return `<div class="raw-decoder-row" data-decoder-id="${esc(dec.id)}">` +
        `<div class="raw-decoder-head">` +
          `<span class="raw-decoder-title">${esc(dec.label)}</span>` +
          `<span class="raw-decoder-ext">${esc(exts)}</span>` +
          `<span class="raw-decoder-tier">${esc(tierLabel)}</span>` +
          `<span class="raw-decoder-state ${stateClass}">${stateLabel}</span>` +
        `</div>` +
        `<p class="raw-decoder-meta">${detail}</p>` +
        pathControl +
      `</div>`;
    }).join('');
    for (const btn of container.querySelectorAll('[data-decoder-clear]')) {
      btn.addEventListener('click', () => {
        const input = container.querySelector(`[data-decoder-path="${btn.dataset.decoderClear}"]`);
        if (input) input.value = '';
      });
    }
  }

  function open() {
    document.querySelector('.splice-raw-dialog')?.remove();
    const dialog = document.createElement('dialog');
    dialog.className = 'splice-dialog splice-raw-dialog';
    dialog.setAttribute('aria-label', 'Camera RAW Decoders');
    dialog.innerHTML =
      `<header><h2>Camera RAW Decoders</h2></header>` +
      `<p class="raw-dialog-sub">CinemaDNG and ProRes RAW decode out of the box. For other cameras, install the vendor's free decoder and set the path to its command-line program. Without one, clips still import with their sound, timecode and reel, and you can attach a ProRes or DNxHR proxy in the Inspector.</p>` +
      `<div class="raw-decoders-list" data-raw-decoders-list>Loading decoders…</div>` +
      `<p class="raw-dialog-message" data-raw-dialog-message aria-live="polite"></p>` +
      `<footer>` +
        `<button type="button" data-raw-dialog-action="refresh">Refresh</button>` +
        `<button type="button" data-raw-dialog-action="close">Close</button>` +
        `<button type="button" class="primary" data-raw-dialog-action="save">Save paths</button>` +
      `</footer>`;
    document.body.append(dialog);
    const list = dialog.querySelector('[data-raw-decoders-list]');
    const message = dialog.querySelector('[data-raw-dialog-message]');
    const setMsg = (text, isError = false) => {
      message.textContent = text;
      message.classList.toggle('error', isError);
    };

    async function load() {
      setMsg('Checking installed RAW decoders…');
      try {
        const data = await api('/api/raw/decoders');
        renderRows(list, data.decoders || []);
        setMsg(`Config file: ${data.configPath || 'local config'}`);
      } catch (error) {
        setMsg(error.message || 'Could not load RAW decoder status.', true);
      }
    }

    async function save() {
      const paths = {};
      for (const input of list.querySelectorAll('[data-decoder-path]')) {
        paths[input.dataset.decoderPath] = input.value.trim();
      }
      setMsg('Validating and saving decoder paths…');
      try {
        const data = await api('/api/raw/settings', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({paths}),
        });
        renderRows(list, data.decoders || []);
        setMsg('RAW decoder paths saved.');
      } catch (error) {
        setMsg(error.message || 'Could not save decoder paths.', true);
      }
    }

    dialog.querySelector('[data-raw-dialog-action="refresh"]').addEventListener('click', load);
    dialog.querySelector('[data-raw-dialog-action="close"]').addEventListener('click', () => dialog.close());
    dialog.querySelector('[data-raw-dialog-action="save"]').addEventListener('click', save);
    dialog.addEventListener('close', () => dialog.remove());
    dialog.showModal();
    load();
    return dialog;
  }

  document.getElementById('raw-decoders')?.addEventListener('click', event => {
    event.target.closest('details')?.removeAttribute('open');
    open();
  });

  window.SpliceRawSettings = {open};
})();
