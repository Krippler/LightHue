const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let PATTERNS = { builtin: [], custom: [] };
let LIGHTS = [];
let GROUPS = [];
let STATUS = {}; // light_id -> settings from server
let cardEls = {}; // card key -> DOM element
let cardEntities = {}; // card key -> { kind, id, name, lightIds }
let waveformTimers = {}; // card key -> interval id for local playhead animation
let selected = new Set(); // light ids ticked for a new group
let SNAPSHOTS = {}; // light_id -> pre-flicker bulb state the server can restore

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

// ---------- Lights, groups + patterns ----------

async function loadPatternsAndLights() {
  const [patterns, lightsRes, statusRes, groupsRes] = await Promise.all([
    api('/api/patterns'),
    api('/api/lights'),
    api('/api/status'),
    api('/api/groups'),
  ]);
  PATTERNS = patterns;
  LIGHTS = lightsRes.lights;
  STATUS = statusRes.lights;
  SNAPSHOTS = statusRes.snapshots || {};
  GROUPS = groupsRes.groups;
  $('#light-count-label').textContent = `${LIGHTS.length} light${LIGHTS.length === 1 ? '' : 's'}`;
  // Drop selections for lights the bridge no longer reports.
  const known = new Set(LIGHTS.map(l => l.id));
  selected = new Set([...selected].filter(id => known.has(id)));
  renderCustomList();
  renderGrid();
}

function allPatternOptions() {
  return [...PATTERNS.builtin, ...PATTERNS.custom];
}

function sequenceFor(patternId) {
  const p = allPatternOptions().find(p => p.id === patternId);
  return p ? p.sequence : 'm';
}

// A card drives one light or a whole group; everything below treats them the
// same way, keyed by "light:<id>" / "group:<id>".
function lightEntity(light) {
  return { kind: 'light', id: light.id, key: `light:${light.id}`, name: light.name,
           lightIds: [light.id], light };
}

function groupEntity(group) {
  const names = group.light_ids
    .map(id => (LIGHTS.find(l => l.id === id) || {}).name)
    .filter(Boolean);
  return { kind: 'group', id: group.id, key: `group:${group.id}`, name: group.name,
           lightIds: group.light_ids, memberNames: names, group };
}

// A group counts as running only when every member is; anything in between is
// reported so one stuck light doesn't read as the whole group flickering.
function entityState(entity) {
  const states = entity.lightIds.map(id => STATUS[id]).filter(Boolean);
  const running = states.filter(s => s.running);
  return {
    running: running.length > 0 && running.length === entity.lightIds.length,
    partial: running.length > 0 && running.length < entity.lightIds.length,
    runningCount: running.length,
    settings: running[0] || states[0] || null,
  };
}

function currentColorOf(entity) {
  for (const id of entity.lightIds) {
    const light = LIGHTS.find(l => l.id === id);
    if (light && light.has_color && light.hue !== null && light.sat !== null) {
      return hueSatToHex(light.hue, light.sat);
    }
  }
  return null;
}

// A revert is offered whenever the server still holds a pre-flicker snapshot —
// which is exactly when it has something to put back.
function hasSnapshot(entity) {
  return entity.lightIds.some(id => SNAPSHOTS[id]);
}

function renderGrid() {
  const grid = $('#lights-grid');
  grid.innerHTML = '';
  cardEls = {};
  cardEntities = {};

  GROUPS.map(groupEntity).forEach(e => grid.appendChild(buildCard(e)));
  LIGHTS.map(lightEntity).forEach(e => grid.appendChild(buildCard(e)));

  renderSelection();
  applyStatus();
}

function buildCard(entity) {
  const tpl = $('#light-card-template');
  const node = tpl.content.cloneNode(true);
  const card = node.querySelector('.light-card');
  card.dataset.cardKey = entity.key;
  card.classList.toggle('group-card', entity.kind === 'group');

  node.querySelector('.light-name').textContent = entity.name;

  const dot = node.querySelector('.reachable-dot');
  const reachText = node.querySelector('.reachable-text');
  const selectWrap = node.querySelector('.card-select');
  const deleteBtn = node.querySelector('.btn-delete-group');

  if (entity.kind === 'group') {
    selectWrap.classList.add('hidden');
    deleteBtn.classList.remove('hidden');
    dot.classList.add('group-dot');
    // Keep the header to one or two lines however many lights are in here.
    const shown = entity.memberNames.slice(0, 3).join(', ');
    const extra = entity.memberNames.length - 3;
    reachText.textContent = entity.memberNames.length
      ? `${entity.lightIds.length} lights · ${shown}${extra > 0 ? ` +${extra} more` : ''}`
      : `${entity.lightIds.length} lights`;
    deleteBtn.addEventListener('click', async () => {
      try {
        await api(`/api/groups/${encodeURIComponent(entity.id)}`, { method: 'DELETE' });
        setGroupStatus(`Deleted "${entity.name}".`);
        await loadPatternsAndLights();
      } catch (e) {
        setGroupStatus(e.message, 'err');
      }
    });
  } else {
    deleteBtn.classList.add('hidden');
    if (entity.light.reachable) {
      reachText.textContent = 'reachable';
    } else {
      dot.classList.add('off');
      reachText.textContent = 'unreachable';
    }
    const box = node.querySelector('.select-light');
    box.checked = selected.has(entity.id);
    box.addEventListener('change', () => {
      if (box.checked) selected.add(entity.id); else selected.delete(entity.id);
      renderSelection();
    });
  }

  const select = node.querySelector('.pattern-select');
  const addGroup = (label, items) => {
    if (!items.length) return;
    const group = document.createElement('optgroup');
    group.label = label;
    items.forEach(p => {
      const o = document.createElement('option');
      o.value = p.id;
      // The game is already the optgroup heading, so don't repeat it.
      o.textContent = p.game ? p.name.replace(`${p.game} — `, '') : p.name;
      if (p.origin === 'inspired') o.title = `Inspired by ${p.game}; not an engine lightstyle table`;
      group.appendChild(o);
    });
    select.appendChild(group);
  };
  (PATTERNS.games || []).forEach(game => {
    addGroup(game, PATTERNS.builtin.filter(p => p.game === game));
  });
  // Anything whose game isn't in the ordered list still has to appear.
  addGroup('Other', PATTERNS.builtin.filter(p => !(PATTERNS.games || []).includes(p.game)));
  addGroup('Custom', PATTERNS.custom);
  select.value = 'flicker_a';

  const hzInput = node.querySelector('.hz-input');
  const hzValue = node.querySelector('.hz-value');
  const transInput = node.querySelector('.trans-input');
  const transValue = node.querySelector('.trans-value');
  const minBriInput = node.querySelector('.minbri-input');
  const minBriValue = node.querySelector('.minbri-value');
  const maxBriInput = node.querySelector('.maxbri-input');
  const maxBriValue = node.querySelector('.maxbri-value');
  const colorEnable = node.querySelector('.color-enable');
  const colorInput = node.querySelector('.color-input');
  const btnStart = node.querySelector('.btn-start');
  const btnStop = node.querySelector('.btn-stop');
  const btnRevert = node.querySelector('.btn-revert');

  // Start from the colour the bulb is showing right now rather than a
  // hardcoded default, so "Set color" doesn't jump it somewhere unexpected.
  const seed = currentColorOf(entity);
  if (seed) colorInput.value = seed;

  btnRevert.addEventListener('click', async () => {
    try {
      await api('/api/flicker/restore', {
        method: 'POST',
        body: JSON.stringify({ light_ids: entity.lightIds }),
      });
      await loadPatternsAndLights();
    } catch (e) {
      alert(`Couldn't revert: ${e.message}`);
    }
  });

  hzInput.addEventListener('input', () => {
    hzValue.textContent = hzInput.value;
    restartWaveform(entity.key);
    pushLive(entity);
  });
  transInput.addEventListener('input', () => {
    transValue.textContent = transInput.value;
    pushLive(entity);
  });
  minBriInput.addEventListener('input', () => {
    minBriValue.textContent = minBriInput.value;
    if (Number(maxBriInput.value) < Number(minBriInput.value)) {
      maxBriInput.value = minBriInput.value;
      maxBriValue.textContent = maxBriInput.value;
    }
    pushLive(entity);
  });
  maxBriInput.addEventListener('input', () => {
    maxBriValue.textContent = maxBriInput.value;
    if (Number(maxBriInput.value) < Number(minBriInput.value)) {
      minBriInput.value = maxBriInput.value;
      minBriValue.textContent = minBriInput.value;
    }
    pushLive(entity);
  });
  select.addEventListener('change', () => {
    drawWaveform(entity.key, sequenceFor(select.value));
    pushLive(entity);
  });

  // The swatch is always live: picking a color means you want it, so it ticks
  // the box for you rather than being greyed out until you find the box.
  colorInput.addEventListener('input', () => {
    if (!colorEnable.checked) colorEnable.checked = true;
    pushLive(entity);
  });
  colorEnable.addEventListener('change', () => pushLive(entity));

  btnStart.addEventListener('click', async () => {
    try {
      await api('/api/flicker/start', {
        method: 'POST',
        body: JSON.stringify({ light_ids: entity.lightIds, ...cardSettings(card) }),
      });
    } catch (e) {
      alert(`Couldn't start flicker: ${e.message}`);
    }
  });

  btnStop.addEventListener('click', async () => {
    await api('/api/flicker/stop', { method: 'POST', body: JSON.stringify({ light_ids: entity.lightIds }) });
  });

  cardEls[entity.key] = card;
  cardEntities[entity.key] = entity;
  drawWaveform(entity.key, sequenceFor(select.value));
  return node;
}

function cardSettings(card) {
  const colorEnable = card.querySelector('.color-enable');
  let hue = null, sat = null;
  if (colorEnable.checked) {
    const hs = rgbToHueSat(hexToRgb(card.querySelector('.color-input').value));
    hue = hs.hue; sat = hs.sat;
  }
  return {
    pattern_id: card.querySelector('.pattern-select').value,
    hz: Number(card.querySelector('.hz-input').value),
    min_bri: Number(card.querySelector('.minbri-input').value),
    max_bri: Number(card.querySelector('.maxbri-input').value),
    hue, sat,
    transition_ms: Number(card.querySelector('.trans-input').value),
  };
}

// ---------- Live retuning ----------

const liveTimers = {};

// Sliders fire per pixel of travel, so coalesce into one PUT per card.
function pushLive(entity) {
  markTouched(entity.key);
  const card = cardEls[entity.key];
  if (!card || !card.classList.contains('is-running')) return;
  clearTimeout(liveTimers[entity.key]);
  liveTimers[entity.key] = setTimeout(async () => {
    const running = entity.lightIds.filter(id => STATUS[id] && STATUS[id].running);
    if (!running.length) return;
    const settings = cardSettings(card);
    // Leaving "Set color" unticked means don't touch the bulb's colour at all;
    // there is no Hue call that puts a colour back the way it was.
    if (settings.hue === null) { delete settings.hue; delete settings.sat; }
    try {
      await api('/api/flicker/update', {
        method: 'POST',
        body: JSON.stringify({ light_ids: running, ...settings }),
      });
    } catch (e) {
      if (!/not currently flickering/i.test(e.message)) console.warn('live update failed', e);
    }
  }, 180);
}

// ---------- Group selection ----------

function setGroupStatus(text, kind = '') {
  const el = $('#group-status');
  el.textContent = text;
  el.className = `status-line ${kind}`.trim();
}

function renderSelection() {
  const n = selected.size;
  $('#selection-count').textContent = n
    ? `${n} light${n === 1 ? '' : 's'} selected`
    : 'No lights selected';
  $('#btn-save-group').disabled = n === 0;
  $('#btn-clear-selection').disabled = n === 0;
}

$('#btn-clear-selection').addEventListener('click', () => {
  selected.clear();
  $$('.select-light').forEach(b => { b.checked = false; });
  renderSelection();
});

$('#btn-save-group').addEventListener('click', async () => {
  const name = $('#group-name').value.trim();
  if (!name) return setGroupStatus('Give the group a name.', 'err');
  if (!selected.size) return setGroupStatus('Tick at least one light first.', 'err');
  try {
    await api('/api/groups', {
      method: 'POST',
      body: JSON.stringify({ name, light_ids: [...selected] }),
    });
    $('#group-name').value = '';
    setGroupStatus(`Saved "${name}".`, 'ok');
    selected.clear();
    await loadPatternsAndLights();
  } catch (e) {
    setGroupStatus(e.message, 'err');
  }
});

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

// Controls are only pulled back into line with the server when the user isn't
// actively working them, otherwise a broadcast lands mid-drag and yanks the
// slider out from under them.
const lastTouched = {};

function markTouched(key) {
  lastTouched[key] = Date.now();
}

function recentlyTouched(key) {
  return Date.now() - (lastTouched[key] || 0) < 2000;
}

function applyStatus() {
  Object.entries(cardEls).forEach(([key, card]) => {
    const entity = cardEntities[key];
    if (!entity) return;
    const { running, partial, runningCount, settings } = entityState(entity);
    const active = running || partial;

    const badge = card.querySelector('.running-badge');
    const btnStart = card.querySelector('.btn-start');
    const btnStop = card.querySelector('.btn-stop');

    card.classList.toggle('is-running', active);
    card.classList.toggle('is-partial', partial);
    badge.classList.toggle('hidden', !active);
    badge.textContent = partial
      ? `${runningCount}/${entity.lightIds.length} FLICKERING`
      : 'FLICKERING';
    // A partly-running group keeps Start available so the stragglers can be
    // brought in line without stopping the ones already going.
    btnStart.classList.toggle('hidden', running);
    btnStop.classList.toggle('hidden', !active);
    btnStart.textContent = partial ? 'Start the rest' : 'Start flicker';
    // Only useful once the flicker has stopped and the bulb is sitting on
    // whatever the last tick left it at.
    card.querySelector('.btn-revert').classList.toggle('hidden', active || !hasSnapshot(entity));

    const rateNote = card.querySelector('.rate-note');
    if (active && settings) {
      if (!recentlyTouched(key)) syncControls(card, key, settings);
      startWaveformAnimation(key);
      // The bridge budget is shared, so what you asked for and what the light
      // actually gets can differ once several are running.
      const eff = settings.effective_hz;
      const capped = eff !== undefined && eff !== null && eff < settings.hz - 0.05;
      rateNote.classList.toggle('hidden', !capped);
      if (capped) rateNote.textContent = `bridge budget: running at ${eff} Hz`;
    } else {
      stopWaveformAnimation(key);
      rateNote.classList.add('hidden');
    }
  });
}

function syncControls(card, key, st) {
  const select = card.querySelector('.pattern-select');
  const hasOption = [...select.options].some(o => o.value === st.pattern_id);
  if (hasOption && select.value !== st.pattern_id) select.value = st.pattern_id;
  // Prefer the sequence the server is actually playing: a pattern can be
  // renamed or removed while a light is still running it.
  drawWaveform(key, st.sequence || sequenceFor(st.pattern_id));

  const set = (sel, valueSel, value) => {
    card.querySelector(sel).value = value;
    const label = card.querySelector(valueSel);
    if (label) label.textContent = value;
  };
  set('.hz-input', '.hz-value', st.hz);
  set('.minbri-input', '.minbri-value', st.min_bri);
  set('.maxbri-input', '.maxbri-value', st.max_bri);
  set('.trans-input', '.trans-value', st.transition_ms);

  const colorEnable = card.querySelector('.color-enable');
  if (st.hue !== null && st.hue !== undefined && st.sat !== null && st.sat !== undefined) {
    colorEnable.checked = true;
    card.querySelector('.color-input').value = hueSatToHex(st.hue, st.sat);
  } else {
    colorEnable.checked = false;
  }
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

function hueSatToHex(hue, sat) {
  const h = (hue / 65535) * 360;
  const s = sat / 254;
  const v = 1;   // the swatch shows hue and saturation; brightness has its own sliders
  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;
  const i = Math.floor(h / 60) % 6;
  const [r1, g1, b1] = [
    [c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x],
  ][i];
  const hex = n => Math.round((n + m) * 255).toString(16).padStart(2, '0');
  return `#${hex(r1)}${hex(g1)}${hex(b1)}`;
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
      STATUS = msg.lights;
      SNAPSHOTS = msg.snapshots || {};
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

const restoreToggle = $('#restore-toggle');

async function loadSettings() {
  const settings = await api('/api/settings');
  rateInput.value = settings.max_commands_per_second;
  rateValue.textContent = settings.max_commands_per_second;
  restoreToggle.checked = settings.restore_on_stop;
}

async function saveSettings() {
  return api('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({
      max_commands_per_second: Number(rateInput.value),
      restore_on_stop: restoreToggle.checked,
    }),
  });
}

restoreToggle.addEventListener('change', async () => {
  const statusEl = $('#restore-status');
  try {
    await saveSettings();
    statusEl.textContent = restoreToggle.checked
      ? 'Lights will be put back when flicker stops.'
      : 'Lights will be left where the flicker ends.';
    statusEl.className = 'status-line ok';
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'status-line err';
  }
});

$('#btn-save-rate').addEventListener('click', async () => {
  const statusEl = $('#rate-status');
  try {
    await saveSettings();
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
