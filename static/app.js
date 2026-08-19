const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let PATTERNS = { builtin: [], custom: [] };
let LIGHTS = [];
let STATUS = {}; // light_id -> settings from server
let cardEls = {}; // light_id -> DOM element
let waveformTimers = {}; // light_id -> interval id for local playhead animation

const setupPanel = $('#setup-panel');
const mainPanel = $('#main-panel');
const connStatus = $('#conn-status');

// ---------- API helpers ----------

function errorText(data, res) {
  const detail = data.detail;
  if (typeof detail === 'string') return detail;
  // FastAPI returns a list of field errors for request-validation failures.
  if (Array.isArray(detail)) {
    return detail.map(e => (e && e.msg ? e.msg.replace(/^Value error, /, '') : String(e))).join('; ');
  }
  return res.statusText || 'Request failed';
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    showLoginGate();
    throw new Error(errorText(data, res));
  }
  if (!res.ok) throw new Error(errorText(data, res));
  return data;
}

// ---------- Bridge setup ----------

async function checkBridge() {
  const info = await api('/api/bridge');
  if (info.configured) {
    setupPanel.classList.add('hidden');
    mainPanel.classList.remove('hidden');
    $('#bridge-ip-label').textContent = `bridge @ ${info.bridge_ip}`;
    await loadPatternsAndLights();
  } else {
    setupPanel.classList.remove('hidden');
    mainPanel.classList.add('hidden');
  }
}

$('#btn-discover').addEventListener('click', async () => {
  const box = $('#discover-results');
  box.textContent = 'Searching...';
  try {
    const { bridges } = await api('/api/bridge/discover');
    box.innerHTML = '';
    if (!bridges.length) {
      box.textContent = 'No bridges found automatically — enter the IP manually.';
      return;
    }
    bridges.forEach(b => {
      const el = document.createElement('div');
      el.className = 'discover-item';
      const ipEl = document.createElement('span');
      ipEl.textContent = b.internalipaddress;
      const useEl = document.createElement('span');
      useEl.className = 'dim';
      useEl.textContent = 'use';
      el.append(ipEl, useEl);
      el.addEventListener('click', () => { $('#pair-ip').value = b.internalipaddress; });
      box.appendChild(el);
    });
  } catch (e) {
    box.textContent = `Discovery failed: ${e.message}`;
  }
});

$('#btn-pair').addEventListener('click', async () => {
  const ip = $('#pair-ip').value.trim();
  const statusEl = $('#pair-status');
  if (!ip) { statusEl.textContent = 'Enter a bridge IP first.'; statusEl.className = 'status-line err'; return; }
  statusEl.textContent = 'Pairing... (make sure you pressed the link button)';
  statusEl.className = 'status-line';
  try {
    await api('/api/bridge/pair', { method: 'POST', body: JSON.stringify({ bridge_ip: ip }) });
    statusEl.textContent = 'Paired!';
    statusEl.className = 'status-line ok';
    await checkBridge();
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'status-line err';
  }
});

$('#btn-manual-save').addEventListener('click', async () => {
  const ip = $('#manual-ip').value.trim();
  const key = $('#manual-key').value.trim();
  if (!ip || !key) return;
  await api('/api/bridge/set', { method: 'POST', body: JSON.stringify({ bridge_ip: ip, api_key: key }) });
  await checkBridge();
});

$('#btn-reconfigure').addEventListener('click', () => {
  setupPanel.classList.remove('hidden');
  mainPanel.classList.add('hidden');
});

$('#btn-refresh-lights').addEventListener('click', loadPatternsAndLights);

$('#btn-stop-all').addEventListener('click', async () => {
  await api('/api/flicker/stop', { method: 'POST', body: JSON.stringify({}) });
});

// ---------- Lights + patterns ----------

async function loadPatternsAndLights() {
  const [patterns, lightsRes, statusRes] = await Promise.all([
    api('/api/patterns'),
    api('/api/lights'),
    api('/api/status'),
  ]);
  PATTERNS = patterns;
  LIGHTS = lightsRes.lights;
  STATUS = statusRes.lights;
  $('#light-count-label').textContent = `${LIGHTS.length} light${LIGHTS.length === 1 ? '' : 's'}`;
  renderCustomList();
  renderLights();
}

function allPatternOptions() {
  return [...PATTERNS.builtin, ...PATTERNS.custom];
}

function sequenceFor(patternId) {
  const p = allPatternOptions().find(p => p.id === patternId);
  return p ? p.sequence : 'm';
}

function renderLights() {
  const grid = $('#lights-grid');
  grid.innerHTML = '';
  cardEls = {};
  const tpl = $('#light-card-template');

  LIGHTS.forEach(light => {
    const node = tpl.content.cloneNode(true);
    const card = node.querySelector('.light-card');
    card.dataset.lightId = light.id;

    node.querySelector('.light-name').textContent = light.name;
    const dot = node.querySelector('.reachable-dot');
    const reachText = node.querySelector('.reachable-text');
    if (light.reachable) {
      reachText.textContent = 'reachable';
    } else {
      dot.classList.add('off');
      reachText.textContent = 'unreachable';
    }

    const select = node.querySelector('.pattern-select');
    const optGroupBuiltin = document.createElement('optgroup');
    optGroupBuiltin.label = 'Built-in (Quake)';
    PATTERNS.builtin.forEach(p => {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name;
      optGroupBuiltin.appendChild(o);
    });
    select.appendChild(optGroupBuiltin);
    if (PATTERNS.custom.length) {
      const optGroupCustom = document.createElement('optgroup');
      optGroupCustom.label = 'Custom';
      PATTERNS.custom.forEach(p => {
        const o = document.createElement('option');
        o.value = p.id; o.textContent = p.name;
        optGroupCustom.appendChild(o);
      });
      select.appendChild(optGroupCustom);
    }
    select.value = 'flicker_a';

    const waveform = node.querySelector('.waveform');

    const hzInput = node.querySelector('.hz-input');
    const hzValue = node.querySelector('.hz-value');
    hzInput.addEventListener('input', () => { hzValue.textContent = hzInput.value; restartWaveform(light.id); });

    const transInput = node.querySelector('.trans-input');
    const transValue = node.querySelector('.trans-value');
    transInput.addEventListener('input', () => { transValue.textContent = transInput.value; });

    const minBriInput = node.querySelector('.minbri-input');
    const minBriValue = node.querySelector('.minbri-value');
    minBriInput.addEventListener('input', () => { minBriValue.textContent = minBriInput.value; });

    const maxBriInput = node.querySelector('.maxbri-input');
    const maxBriValue = node.querySelector('.maxbri-value');
    maxBriInput.addEventListener('input', () => { maxBriValue.textContent = maxBriInput.value; });

    select.addEventListener('change', () => drawWaveform(light.id, sequenceFor(select.value)));

    const colorEnable = node.querySelector('.color-enable');
    const colorInput = node.querySelector('.color-input');
    colorInput.disabled = true;
    colorEnable.addEventListener('change', () => { colorInput.disabled = !colorEnable.checked; });

    const btnStart = node.querySelector('.btn-start');
    const btnStop = node.querySelector('.btn-stop');

    btnStart.addEventListener('click', async () => {
      let hue = null, sat = null;
      if (colorEnable.checked) {
        const rgb = hexToRgb(colorInput.value);
        const hs = rgbToHueSat(rgb);
        hue = hs.hue; sat = hs.sat;
      }
      try {
        await api('/api/flicker/start', {
          method: 'POST',
          body: JSON.stringify({
            light_ids: [light.id],
            pattern_id: select.value,
            hz: Number(hzInput.value),
            min_bri: Number(minBriInput.value),
            max_bri: Number(maxBriInput.value),
            hue, sat,
            transition_ms: Number(transInput.value),
          }),
        });
      } catch (e) {
        alert(`Couldn't start flicker: ${e.message}`);
      }
    });

    btnStop.addEventListener('click', async () => {
      await api('/api/flicker/stop', { method: 'POST', body: JSON.stringify({ light_ids: [light.id] }) });
    });

    grid.appendChild(node);
    cardEls[light.id] = card;
    drawWaveform(light.id, sequenceFor(select.value));
  });

  applyStatus();
}

// ---------- Waveform (signature visual element) ----------

function levelForChar(c) {
  c = c.toLowerCase();
  const code = c.charCodeAt(0) - 97;
  if (code < 0 || code > 25) return 0.5;
  return code / 25;
}

function renderBars(container, sequence) {
  container.innerHTML = '';
  for (const ch of sequence) {
    const bar = document.createElement('div');
    bar.className = 'bar';
    const level = levelForChar(ch);
    bar.style.height = `${8 + level * 92}%`;
    container.appendChild(bar);
  }
}

function drawWaveform(lightId, sequence) {
  const card = cardEls[lightId];
  if (!card) return;
  renderBars(card.querySelector('.waveform'), sequence);
}

function restartWaveform(lightId) {
  const card = cardEls[lightId];
  if (!card) return;
  const running = card.classList.contains('is-running');
  stopWaveformAnimation(lightId);
  if (running) startWaveformAnimation(lightId);
}

function startWaveformAnimation(lightId) {
  const card = cardEls[lightId];
  if (!card) return;
  const hzInput = card.querySelector('.hz-input');
  const bars = $$('.bar', card.querySelector('.waveform'));
  if (!bars.length) return;
  let idx = 0;
  stopWaveformAnimation(lightId);
  const hz = Number(hzInput.value) || 10;
  waveformTimers[lightId] = setInterval(() => {
    bars.forEach(b => b.classList.remove('active'));
    bars[idx % bars.length].classList.add('active');
    idx++;
  }, 1000 / hz);
}

function stopWaveformAnimation(lightId) {
  if (waveformTimers[lightId]) {
    clearInterval(waveformTimers[lightId]);
    delete waveformTimers[lightId];
  }
  const card = cardEls[lightId];
  if (card) $$('.bar', card.querySelector('.waveform')).forEach(b => b.classList.remove('active'));
}

// ---------- Custom lightstyles ----------

const customName = $('#custom-name');
const customSeq = $('#custom-seq');
const customStatus = $('#custom-status');

function normalizeSequence(raw) {
  return raw.trim().toLowerCase().replace(/\s+/g, '');
}

function setCustomStatus(text, kind = '') {
  customStatus.textContent = text;
  customStatus.className = `status-line ${kind}`.trim();
}

customSeq.addEventListener('input', () => {
  const seq = normalizeSequence(customSeq.value);
  renderBars($('#custom-preview'), /^[a-z]*$/.test(seq) ? seq : '');
});

$('#btn-save-pattern').addEventListener('click', async () => {
  const name = customName.value.trim();
  const sequence = normalizeSequence(customSeq.value);
  if (!name) return setCustomStatus('Give the pattern a name.', 'err');
  if (!sequence) return setCustomStatus('Write a sequence first.', 'err');
  if (!/^[a-z]+$/.test(sequence)) return setCustomStatus('Sequence must only contain letters a-z.', 'err');
  try {
    await api('/api/patterns', { method: 'POST', body: JSON.stringify({ name, sequence }) });
    customName.value = '';
    customSeq.value = '';
    renderBars($('#custom-preview'), '');
    setCustomStatus(`Saved "${name}".`, 'ok');
    await loadPatternsAndLights();
  } catch (e) {
    setCustomStatus(e.message, 'err');
  }
});

function renderCustomList() {
  const list = $('#custom-list');
  list.innerHTML = '';
  if (!PATTERNS.custom.length) {
    list.textContent = 'No custom patterns saved yet.';
    list.className = 'custom-list dim';
    return;
  }
  list.className = 'custom-list';
  PATTERNS.custom.forEach(p => {
    const chip = document.createElement('div');
    chip.className = 'custom-chip';

    const nameEl = document.createElement('span');
    nameEl.className = 'chip-name';
    nameEl.textContent = p.name;

    const seqEl = document.createElement('span');
    seqEl.className = 'chip-seq';
    seqEl.textContent = p.sequence;

    const del = document.createElement('button');
    del.className = 'chip-del';
    del.type = 'button';
    del.textContent = '\u00d7';
    del.title = `Delete "${p.name}"`;
    del.addEventListener('click', async () => {
      try {
        await api(`/api/patterns/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
        setCustomStatus(`Deleted "${p.name}".`, '');
        await loadPatternsAndLights();
      } catch (e) {
        setCustomStatus(e.message, 'err');
      }
    });

    chip.append(nameEl, seqEl, del);
    list.appendChild(chip);
  });
}

// ---------- Status sync (drives multi-user shared state) ----------

function applyStatus() {
  Object.entries(cardEls).forEach(([lightId, card]) => {
    const st = STATUS[lightId];
    const running = !!(st && st.running);
    const badge = card.querySelector('.running-badge');
    const btnStart = card.querySelector('.btn-start');
    const btnStop = card.querySelector('.btn-stop');

    card.classList.toggle('is-running', running);
    badge.classList.toggle('hidden', !running);
    btnStart.classList.toggle('hidden', running);
    btnStop.classList.toggle('hidden', !running);

    if (running && st) {
      const select = card.querySelector('.pattern-select');
      if (select.value !== st.pattern_id) {
        select.value = st.pattern_id;
        drawWaveform(lightId, sequenceFor(st.pattern_id));
      }
      const hzInput = card.querySelector('.hz-input');
      hzInput.value = st.hz;
      card.querySelector('.hz-value').textContent = st.hz;
      startWaveformAnimation(lightId);
    } else {
      stopWaveformAnimation(lightId);
    }
  });
}

// ---------- Color helpers (hex -> Hue's hue/sat space) ----------

function hexToRgb(hex) {
  const v = hex.replace('#', '');
  return {
    r: parseInt(v.substring(0, 2), 16),
    g: parseInt(v.substring(2, 4), 16),
    b: parseInt(v.substring(4, 6), 16),
  };
}

function rgbToHueSat({ r, g, b }) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0;
  const d = max - min;
  const s = max === 0 ? 0 : d / max;
  if (d !== 0) {
    switch (max) {
      case r: h = ((g - b) / d) % 6; break;
      case g: h = (b - r) / d + 2; break;
      case b: h = (r - g) / d + 4; break;
    }
    h *= 60;
    if (h < 0) h += 360;
  }
  return { hue: Math.round((h / 360) * 65535), sat: Math.round(s * 254) };
}

// ---------- WebSocket ----------

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener('open', () => {
    connStatus.textContent = 'live';
    connStatus.className = 'conn-status ok';
  });

  ws.addEventListener('close', () => {
    connStatus.textContent = 'disconnected — retrying';
    connStatus.className = 'conn-status err';
    setTimeout(connectWs, 2000);
  });

  ws.addEventListener('error', () => ws.close());

  ws.addEventListener('message', (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === 'status') {
      STATUS = msg.data;
      applyStatus();
    }
  });
}

// ---------- Console password ----------

const loginPanel = $('#login-panel');
const settingsPanel = $('#settings-panel');
let AUTH = { required: false, authenticated: true };
let wsStarted = false;

function showLoginGate() {
  loginPanel.classList.remove('hidden');
  setupPanel.classList.add('hidden');
  mainPanel.classList.add('hidden');
}

$('#btn-login').addEventListener('click', login);
$('#login-password').addEventListener('keydown', e => { if (e.key === 'Enter') login(); });

async function login() {
  const statusEl = $('#login-status');
  const password = $('#login-password').value;
  if (!password) { statusEl.textContent = 'Enter the console password.'; statusEl.className = 'status-line err'; return; }
  try {
    await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ password }) });
    $('#login-password').value = '';
    statusEl.textContent = '';
    statusEl.className = 'status-line';
    loginPanel.classList.add('hidden');
    await bootstrap();
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'status-line err';
  }
}

$('#btn-logout').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' });
  location.reload();
});

// ---------- Settings ----------

$('#btn-settings').addEventListener('click', () => {
  settingsPanel.classList.toggle('hidden');
});

const rateInput = $('#rate-input');
const rateValue = $('#rate-value');
rateInput.addEventListener('input', () => { rateValue.textContent = rateInput.value; });

async function loadSettings() {
  const settings = await api('/api/settings');
  rateInput.value = settings.max_commands_per_second;
  rateValue.textContent = settings.max_commands_per_second;
}

$('#btn-save-rate').addEventListener('click', async () => {
  const statusEl = $('#rate-status');
  try {
    await api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ max_commands_per_second: Number(rateInput.value) }),
    });
    statusEl.textContent = `Send rate capped at ${rateInput.value}/sec.`;
    statusEl.className = 'status-line ok';
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'status-line err';
  }
});

function renderAuthControls() {
  const set = AUTH.required;
  $('#pw-current-field').classList.toggle('hidden', !set);
  $('#btn-clear-pw').classList.toggle('hidden', !set);
  $('#btn-logout').classList.toggle('hidden', !set);
  $('#btn-save-pw').textContent = set ? 'Change password' : 'Set password';
  $('#pw-explainer').textContent = set
    ? 'A password is set. Anyone opening this console has to enter it first.'
    : 'No password set — anyone on your network can drive these lights. Set one to lock the console.';
}

$('#btn-save-pw').addEventListener('click', async () => {
  const statusEl = $('#pw-status');
  const body = { new_password: $('#pw-new').value };
  if (AUTH.required) body.current_password = $('#pw-current').value;
  try {
    await api('/api/auth/password', { method: 'PUT', body: JSON.stringify(body) });
    $('#pw-new').value = '';
    $('#pw-current').value = '';
    AUTH = { required: true, authenticated: true };
    renderAuthControls();
    statusEl.textContent = 'Password saved. Other open consoles will need to sign in again.';
    statusEl.className = 'status-line ok';
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'status-line err';
  }
});

$('#btn-clear-pw').addEventListener('click', async () => {
  const statusEl = $('#pw-status');
  try {
    await api('/api/auth/password', {
      method: 'DELETE',
      body: JSON.stringify({ current_password: $('#pw-current').value }),
    });
    $('#pw-current').value = '';
    AUTH = { required: false, authenticated: true };
    renderAuthControls();
    statusEl.textContent = 'Password removed — the console is open again.';
    statusEl.className = 'status-line ok';
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'status-line err';
  }
});

// ---------- Init ----------

async function bootstrap() {
  AUTH = await api('/api/auth');
  if (AUTH.required && !AUTH.authenticated) {
    showLoginGate();
    return;
  }
  loginPanel.classList.add('hidden');
  renderAuthControls();
  await loadSettings();
  try {
    await checkBridge();
  } catch (e) {
    // An unreachable bridge shouldn't cost us the live status feed.
    connStatus.textContent = e.message;
    connStatus.className = 'conn-status err';
  }
  if (!wsStarted) { wsStarted = true; connectWs(); }
}

bootstrap();
