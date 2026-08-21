const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let PATTERNS = { builtin: [], custom: [] };
let LIGHTS = [];
let GROUPS = [];
let STATUS = {}; // light_id -> settings from server
let cardEls = {}; // card key -> DOM element
let cardEntities = {}; // card key -> { kind, id, name, lightIds }
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
  const statusEl = $('#manual-status');
  if (!ip) {
    statusEl.textContent = 'Enter the bridge IP.';
    statusEl.className = 'status-line err';
    return;
  }
  statusEl.textContent = '';
  statusEl.className = 'status-line';
  try {
    // An empty key means "same bridge, new address": the key belongs to the
    // bridge and survives it moving network, so it is left out rather than
    // blanked.
    const body = key ? { bridge_ip: ip, api_key: key } : { bridge_ip: ip };
    await api('/api/bridge/set', { method: 'POST', body: JSON.stringify(body) });
    await checkBridge();
  } catch (e) {
    // The address is checked server-side now, so a typo lands here rather
    // than silently storing something the console can never reach.
    statusEl.textContent = e.message;
    statusEl.className = 'status-line err';
  }
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
  const [patterns, statusRes, groupsRes] = await Promise.all([
    api('/api/patterns'),
    api('/api/status'),
    api('/api/groups'),
  ]);
  PATTERNS = patterns;
  STATUS = statusRes.lights;
  SNAPSHOTS = statusRes.snapshots || {};
  STREAM = statusRes.stream || { running: false };
  noteServerClock(statusRes.now);
  GROUPS = groupsRes.groups;

  // Only the light list actually needs the bridge. If it's unreachable, say so
  // and keep the rest of the console usable rather than failing everything.
  let lightsError = null;
  try {
    LIGHTS = (await api('/api/lights')).lights;
  } catch (e) {
    lightsError = e.message;
  }
  $('#light-count-label').textContent = lightsError
    ? lightsError
    : `${LIGHTS.length} light${LIGHTS.length === 1 ? '' : 's'}`;
  $('#light-count-label').classList.toggle('err', !!lightsError);
  // Drop selections for lights the bridge no longer reports.
  const known = new Set(LIGHTS.map(l => l.id));
  selected = new Set([...selected].filter(id => known.has(id)));
  renderCustomList();
  renderGrid();
  // The entertainment panel draws from the same pattern list as the cards.
  fillPatternSelect(streamPattern);
  selectPattern(streamPattern, 'flicker_a');
  applyStreamFraming();
  applyStreamStatus();
  await loadStreamAreas();
}

function allPatternOptions() {
  return [...PATTERNS.builtin, ...PATTERNS.custom];
}

// Built-in names carry their own game ("Quake — 1 Flicker"); strip it so the
// menu the pattern is listed under can supply the prefix instead.
function bareName(name) {
  return name.replace(/^[^—]+ — /, '');
}

// A shared style is listed once per game that inherited it, so the same id can
// sit in several optgroups. Setting select.value takes whichever comes first
// alphabetically, which would credit Quake's table to Half-Life; pick the copy
// filed under the pattern's own game instead.
function selectPattern(select, patternId) {
  const home = (allPatternOptions().find(p => p.id === patternId) || {}).game;
  const options = [...select.options].filter(o => o.value === patternId);
  if (!options.length) return false;
  const own = home && options.find(o => o.parentElement.label === home);
  (own || options[0]).selected = true;
  return true;
}

// Builds the whole pattern menu into a <select>. Shared by every light card
// and by the entertainment panel, which needs exactly the same list.
function fillPatternSelect(select) {
  select.innerHTML = '';
  // Which optgroups name a game, and so should re-prefix their options.
  // "Custom" and "Other" don't: their entries already read the way they should.
  const gameGroups = new Set(PATTERNS.games || []);
  const addGroup = (label, items) => {
    if (!items.length) return;
    const group = document.createElement('optgroup');
    group.label = label;
    items.forEach(p => {
      const o = document.createElement('option');
      o.value = p.id;
      // A closed <select> shows only the chosen option's own text, so the game
      // has to be in it — the optgroup heading is visible while the list is
      // open and gone the moment it isn't. Named for the menu it was picked
      // from, so a Quake style chosen under Half-Life reads "Half-Life — ...";
      // the tooltip is where the inheritance gets explained.
      o.textContent = gameGroups.has(label) ? `${label} — ${bareName(p.name)}` : p.name;
      if (p.game !== label && label !== 'Custom') {
        o.title = `${p.game}'s lightstyle, inherited wholesale by ${label}`;
      } else if (p.origin === 'inspired') {
        o.title = `Inspired by ${p.game}; not an engine lightstyle table`;
      }
      group.appendChild(o);
    });
    select.appendChild(group);
  };
  const games = PATTERNS.games || [];
  // A pattern can belong to more than one game's menu: GoldSrc inherited
  // Quake's table, so those styles appear under Half-Life too.
  const forGame = g => PATTERNS.builtin.filter(
    p => p.game === g || (p.shared_with || []).includes(g));
  games.forEach(game => addGroup(game, forGame(game)));
  // Anything whose game isn't in the ordered list still has to appear.
  addGroup('Other', PATTERNS.builtin.filter(p => !games.includes(p.game)));
  addGroup('Custom', PATTERNS.custom);
}

function sequenceFor(patternId) {
  const p = allPatternOptions().find(p => p.id === patternId);
  return p ? p.sequence : 'm';
}

// A pattern isn't just its letters: the speed, how far the bulb swings and how
// hard the steps are all belong to the effect. A sputtering bulb and a slow
// gothic throb aren't the same shape played faster or slower.
const FRAMING_DEFAULTS = {
  hz: 10, min_bri: 1, max_bri: 254, transition_ms: 0, hue: null, sat: null,
};

function framingFor(patternId) {
  const p = allPatternOptions().find(p => p.id === patternId) || {};
  const framing = {};
  for (const [field, fallback] of Object.entries(FRAMING_DEFAULTS)) {
    framing[field] = p[field] === undefined || p[field] === null ? fallback : p[field];
  }
  return framing;
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
      return { hue: light.hue, sat: light.sat };
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
  const groupGrid = $('#groups-grid');
  const lightGrid = $('#lights-grid');
  groupGrid.innerHTML = '';
  lightGrid.innerHTML = '';
  cardEls = {};
  cardEntities = {};

  GROUPS.map(groupEntity).forEach(e => groupGrid.appendChild(buildCard(e)));
  LIGHTS.map(lightEntity).forEach(e => lightGrid.appendChild(buildCard(e)));

  // An empty grid would otherwise be an unexplained gap in its panel.
  if (!LIGHTS.length) {
    lightGrid.appendChild(emptyState('No lights or plugs reported by this bridge.'));
  }

  renderSelection();
  applyStatus();
}

function emptyState(text) {
  const el = document.createElement('div');
  el.className = 'empty-state';
  el.textContent = text;
  return el;
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
  fillPatternSelect(select);
  selectPattern(select, 'flicker_a');

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
  // applyPatternFraming below overrides this when the pattern names a colour.
  const seed = currentColorOf(entity);
  if (seed) setCardColor(card, seed.hue, seed.sat);

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
  const applyPatternFraming = () => {
    const framing = framingFor(select.value);
    const set = (input, label, value) => {
      input.value = value;
      label.textContent = value;
    };
    set(hzInput, hzValue, framing.hz);
    set(minBriInput, minBriValue, framing.min_bri);
    set(maxBriInput, maxBriValue, framing.max_bri);
    set(transInput, transValue, framing.transition_ms);
    // A pattern that names a colour ticks the box and fills the swatch; one
    // that doesn't leaves the bulb's own colour alone rather than forcing
    // white on it.
    if (framing.hue !== null && framing.sat !== null) {
      setCardColor(card, framing.hue, framing.sat);
      colorEnable.checked = true;
    } else {
      colorEnable.checked = false;
    }
  };
  select.addEventListener('change', () => {
    applyPatternFraming();
    drawWaveform(entity.key, sequenceFor(select.value));
    pushLive(entity);
  });

  // The swatch is always live: picking a color means you want it, so it ticks
  // the box for you rather than being greyed out until you find the box.
  colorInput.addEventListener('input', () => {
    const hs = rgbToHueSat(hexToRgb(colorInput.value));
    setCardColor(card, hs.hue, hs.sat);
    if (!colorEnable.checked) colorEnable.checked = true;
    pushLive(entity);
  });
  colorEnable.addEventListener('change', () => pushLive(entity));

  const colorCode = node.querySelector('.color-code');
  colorCode.addEventListener('input', () => {
    const parsed = parseColorCode(colorCode.value);
    if (!parsed) {
      // Flag it rather than silently keeping the old colour, but leave what
      // they typed alone so it can be corrected.
      colorCode.classList.toggle('err', colorCode.value.trim() !== '');
      return;
    }
    colorCode.classList.remove('err');
    card.dataset.hue = parsed.hue;
    card.dataset.sat = parsed.sat;
    colorInput.value = hueSatToHex(parsed.hue, parsed.sat);
    if (!colorEnable.checked) colorEnable.checked = true;
    pushLive(entity);
  });
  // Tidy the typed form up to the canonical one once they're done.
  colorCode.addEventListener('change', () => {
    const parsed = parseColorCode(colorCode.value);
    if (parsed) setCardColor(card, parsed.hue, parsed.sat);
  });

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

  applyPatternFraming();
  cardEls[entity.key] = card;
  cardEntities[entity.key] = entity;
  drawWaveform(entity.key, sequenceFor(select.value));
  return node;
}

function cardSettings(card) {
  const colorEnable = card.querySelector('.color-enable');
  let hue = null, sat = null;
  if (colorEnable.checked) {
    ({ hue, sat } = cardColor(card));
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

// The bridge already knows the user's rooms and zones — the Hue app calls a
// group a Room (a light lives in exactly one) or a Zone (any set, overlapping
// allowed). Ours behave like zones, so both can be copied straight over rather
// than rebuilt light by light.
const bridgeGroupsBox = $('#bridge-groups');

const bridgeGroupsBtn = $('#btn-bridge-groups');

function showBridgeGroups(open) {
  // The button is a toggle, so it has to look like one: without this it was
  // possible to open the list, not notice it, click again and close it, and
  // conclude the button did nothing at all.
  bridgeGroupsBox.classList.toggle('hidden', !open);
  bridgeGroupsBtn.classList.toggle('btn-active', open);
  bridgeGroupsBtn.textContent = open ? 'Hide bridge rooms' : 'Use a room from the bridge';
}

function bridgeGroupsMessage(text, kind = 'dim') {
  bridgeGroupsBox.className = `bridge-groups ${kind}`;
  bridgeGroupsBox.textContent = text;
}

bridgeGroupsBtn.addEventListener('click', async () => {
  if (!bridgeGroupsBox.classList.contains('hidden')) {
    showBridgeGroups(false);
    return;
  }
  showBridgeGroups(true);
  bridgeGroupsMessage('Asking the bridge…');
  try {
    renderBridgeGroups(await api('/api/bridge/groups'));
  } catch (e) {
    // Kept open with the reason in it. Hiding the panel on failure was
    // indistinguishable from the button doing nothing.
    bridgeGroupsMessage(`Couldn't read the bridge's rooms: ${e.message}`, 'err');
  }
});

function renderBridgeGroups({ groups, seen = {}, total = 0 }) {
  bridgeGroupsBox.textContent = '';
  if (!groups.length) {
    // Say which of the two it is: a bridge with nothing set up and one whose
    // groups are all luminaires are quite different problems.
    const other = Object.entries(seen)
      .map(([kind, n]) => `${n} ${kind}`)
      .join(', ');
    bridgeGroupsMessage(
      total
        ? `The bridge returned ${total} group${total === 1 ? '' : 's'} (${other}), `
          + 'but no rooms or zones. Rooms are set up in the Hue app under Settings.'
        : 'The bridge has no rooms or zones set up. They are created in the Hue app.',
    );
    return;
  }
  bridgeGroupsBox.className = 'bridge-groups';

  groups.forEach(g => {
    // Only lights this console can actually see: a room may include a bulb
    // that has since gone, and driving an id that isn't there just burns
    // bridge budget until it gets written off.
    const usable = g.light_ids.filter(id => LIGHTS.some(l => l.id === id));
    const already = GROUPS.some(existing =>
      existing.name === g.name
      && [...existing.light_ids].sort().join() === [...usable].sort().join());

    const row = document.createElement('div');
    row.className = `bridge-group${already ? ' taken' : ''}`;

    const type = document.createElement('span');
    type.className = 'bg-type';
    type.textContent = g.type === 'Room' ? 'room' : g.type === 'Zone' ? 'zone' : 'group';

    const name = document.createElement('span');
    name.className = 'bg-name';
    name.textContent = g.name;

    const meta = document.createElement('span');
    meta.className = 'bg-meta';
    const missing = g.light_ids.length - usable.length;
    meta.textContent = `${usable.length} light${usable.length === 1 ? '' : 's'}`
      + (missing ? ` · ${missing} not on this bridge` : '');

    row.append(type, name, meta);

    if (already) {
      const done = document.createElement('span');
      done.className = 'bg-meta';
      done.textContent = 'already added';
      row.appendChild(done);
    } else if (!usable.length) {
      const none = document.createElement('span');
      none.className = 'bg-meta';
      none.textContent = 'no usable lights';
      row.appendChild(none);
    } else {
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.type = 'button';
      btn.textContent = 'Add';
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          await api('/api/groups', {
            method: 'POST',
            body: JSON.stringify({ name: g.name, light_ids: usable }),
          });
          setGroupStatus(`Added "${g.name}" from the bridge.`, 'ok');
          await loadPatternsAndLights();
          renderBridgeGroups({ groups, seen, total });
        } catch (e) {
          btn.disabled = false;
          setGroupStatus(e.message, 'err');
        }
      });
      row.appendChild(btn);
    }
    bridgeGroupsBox.appendChild(row);
  });
}

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

function drawWaveform(key, sequence, container = null) {
  // The entertainment panel has a waveform but is not a card, so it passes its
  // own element rather than being looked up in cardEls.
  const waveform = container || (cardEls[key] && cardEls[key].querySelector('.waveform'));
  if (!waveform) return;
  // Status arrives on every change anyone makes, and rebuilding identical bars
  // each time threw the playhead away and blinked it off for a frame.
  if (waveform.dataset.sequence === sequence) return;
  waveform.dataset.sequence = sequence;
  renderBars(waveform, sequence);
  delete lastFrame[key];   // bars were replaced; the old index means nothing
}

// The playhead is driven off the server's clock, not a local timer: the engine
// caps each light to its share of the bridge budget, so what you asked for and
// what the bulb actually does are often different numbers. Animating at the
// slider value made the bar run visibly faster than the light.

let playheadTimer = null;
let serverClockOffset = 0;   // server monotonic seconds minus our own
const lastFrame = {};        // card key -> frame currently lit, to avoid churn

function clientSeconds() {
  return performance.now() / 1000;
}

function noteServerClock(now) {
  if (typeof now === 'number') serverClockOffset = now - clientSeconds();
}

function serverSeconds() {
  return clientSeconds() + serverClockOffset;
}

function clearPlayhead(card, key) {
  delete lastFrame[key];
  card.querySelectorAll('.waveform .bar.active').forEach(b => b.classList.remove('active'));
}

function updatePlayheads() {
  let anyRunning = false;

  Object.entries(cardEls).forEach(([key, card]) => {
    const entity = cardEntities[key];
    if (!entity) return;
    const { running, partial, settings } = entityState(entity);
    const bars = card.querySelectorAll('.waveform .bar');
    if (!(running || partial) || !settings || !bars.length) {
      clearPlayhead(card, key);
      return;
    }
    anyRunning = true;

    // effective_hz is what the light is really being sent at; hz is what was
    // asked for. Prefer the truth.
    const hz = settings.effective_hz || settings.hz || 10;
    const epoch = settings.epoch;
    const elapsed = epoch === undefined ? 0 : serverSeconds() - epoch;
    const frame = Math.floor(Math.max(0, elapsed) * hz);
    const idx = ((frame % bars.length) + bars.length) % bars.length;
    if (lastFrame[key] === idx) return;

    if (lastFrame[key] !== undefined && bars[lastFrame[key]]) {
      bars[lastFrame[key]].classList.remove('active');
    } else {
      bars.forEach(b => b.classList.remove('active'));
    }
    bars[idx].classList.add('active');
    lastFrame[key] = idx;
  });

  if (!anyRunning) {
    clearInterval(playheadTimer);
    playheadTimer = null;
  }
}

// One loop for every card. It recomputes position from the clock each tick, so
// a rate change or a light joining the budget is picked up without restarting
// anything.
function ensurePlayheadLoop() {
  if (!playheadTimer) playheadTimer = setInterval(updatePlayheads, 40);
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

// The custom form's colour works the same way: one exact pair, two views.
const customColor = $('#custom-color');
const customColorCode = $('#custom-color-code');
let customHue = null;
let customSat = null;

function setCustomColor(hue, sat) {
  customHue = hue;
  customSat = sat;
  customColorCode.classList.remove('err');
  if (hue === null) {
    customColorCode.value = '';
    return;
  }
  customColor.value = hueSatToHex(hue, sat);
  customColorCode.value = `${hue},${sat}`;
}

customColor.addEventListener('input', () => {
  const hs = rgbToHueSat(hexToRgb(customColor.value));
  setCustomColor(hs.hue, hs.sat);
  $('#custom-color-enable').checked = true;
});

customColorCode.addEventListener('input', () => {
  const parsed = parseColorCode(customColorCode.value);
  if (!parsed) {
    customColorCode.classList.toggle('err', customColorCode.value.trim() !== '');
    return;
  }
  customColorCode.classList.remove('err');
  customHue = parsed.hue;
  customSat = parsed.sat;
  customColor.value = hueSatToHex(parsed.hue, parsed.sat);
  $('#custom-color-enable').checked = true;
});

customColorCode.addEventListener('change', () => {
  const parsed = parseColorCode(customColorCode.value);
  if (parsed) setCustomColor(parsed.hue, parsed.sat);
});

[['#custom-hz', '#custom-hz-value'],
 ['#custom-minbri', '#custom-minbri-value'],
 ['#custom-maxbri', '#custom-maxbri-value'],
 ['#custom-trans', '#custom-trans-value']].forEach(([input, label]) => {
  const el = $(input);
  el.addEventListener('input', () => {
    $(label).textContent = el.value;
    // keep the window coherent while it's being dragged
    const lo = $('#custom-minbri'), hi = $('#custom-maxbri');
    if (Number(hi.value) < Number(lo.value)) {
      const follower = input === '#custom-minbri' ? hi : lo;
      follower.value = el.value;
      $(input === '#custom-minbri' ? '#custom-maxbri-value' : '#custom-minbri-value')
        .textContent = el.value;
    }
  });
});

customSeq.addEventListener('input', () => {
  const seq = normalizeSequence(customSeq.value);
  renderBars($('#custom-preview'), /^[a-z]*$/.test(seq) ? seq : '');
});

$('#btn-save-pattern').addEventListener('click', async () => {
  const name = customName.value.trim();
  const sequence = normalizeSequence(customSeq.value);
  const wantsColor = $('#custom-color-enable').checked;
  // Fall back to the swatch only if nothing exact has been set yet.
  const exact = customHue !== null
    ? { hue: customHue, sat: customSat }
    : rgbToHueSat(hexToRgb(customColor.value));
  const framing = {
    hz: Number($('#custom-hz').value),
    min_bri: Number($('#custom-minbri').value),
    max_bri: Number($('#custom-maxbri').value),
    transition_ms: Number($('#custom-trans').value),
    hue: wantsColor ? exact.hue : null,
    sat: wantsColor ? exact.sat : null,
  };
  if (!name) return setCustomStatus('Give the pattern a name.', 'err');
  if (!sequence) return setCustomStatus('Write a sequence first.', 'err');
  if (!/^[a-z]+$/.test(sequence)) return setCustomStatus('Sequence must only contain letters a-z.', 'err');
  try {
    await api('/api/patterns', {
      method: 'POST',
      body: JSON.stringify({ name, sequence, ...framing }),
    });
    customName.value = '';
    customSeq.value = '';
    setCustomColor(null, null);
    $('#custom-color-enable').checked = false;
    renderBars($('#custom-preview'), '');
    const seconds = (sequence.length / framing.hz).toFixed(1);
    setCustomStatus(`Saved "${name}" at ${framing.hz} Hz — a ${seconds}s cycle.`, 'ok');
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
    const f = framingFor(p.id);
    const trans = f.transition_ms ? ` · ${f.transition_ms}ms` : '';
    seqEl.textContent = `${p.sequence} · ${f.hz} Hz · ${f.min_bri}-${f.max_bri}${trans}`;
    if (f.hue !== null && f.sat !== null) {
      const dot = document.createElement('span');
      dot.className = 'chip-color';
      dot.style.background = hueSatToHex(f.hue, f.sat);
      dot.title = 'saved with a colour';
      chip.appendChild(dot);
    }

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

// ---------- Sharing patterns as a file ----------

function setShareStatus(text, kind = '') {
  const el = $('#share-status');
  el.textContent = text;
  el.className = `status-line ${kind}`.trim();
}

$('#btn-export-patterns').addEventListener('click', async () => {
  try {
    // Fetched rather than linked so a failure surfaces as a message here
    // instead of dumping JSON into a browser tab.
    const res = await fetch('/api/patterns/export');
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(errorText(data, res));
    }
    const text = await res.text();
    const exported = JSON.parse(text).patterns.length;
    const blob = new Blob([text], { type: 'application/json' });
    const match = /filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = match ? match[1] : 'patterns.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setShareStatus(`Exported ${exported} pattern${exported === 1 ? '' : 's'}.`, 'ok');
  } catch (e) {
    setShareStatus(e.message, 'err');
  }
});

$('#btn-import-patterns').addEventListener('click', () => $('#import-file').click());

$('#import-file').addEventListener('change', async (evt) => {
  const file = evt.target.files && evt.target.files[0];
  evt.target.value = '';          // so picking the same file twice still fires
  if (!file) return;
  setShareStatus(`Reading ${file.name}\u2026`);
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch {
    setShareStatus(`${file.name} isn't valid JSON.`, 'err');
    return;
  }
  try {
    const result = await api('/api/patterns/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    setShareStatus(describeImport(result, file.name), result.added.length ? 'ok' : '');
    await loadPatternsAndLights();
  } catch (e) {
    setShareStatus(e.message, 'err');
  }
});

function describeImport(result, filename) {
  const from = result.pack_name
    ? `"${result.pack_name}"${result.author ? ` by ${result.author}` : ''}`
    : filename;
  const added = result.added.length;
  const skipped = result.skipped.length;
  if (!added && !skipped) return `${from} had nothing to add.`;
  const parts = [];
  parts.push(added
    ? `Added ${added} pattern${added === 1 ? '' : 's'} from ${from}`
    : `Nothing new in ${from}`);
  if (skipped) {
    // Name the first couple so "skipped 3" isn't a mystery.
    const shown = result.skipped.slice(0, 2).map(s => `${s.name} (${s.reason})`).join('; ');
    const more = skipped > 2 ? `, and ${skipped - 2} more` : '';
    parts.push(`already had ${shown}${more}`);
  }
  return `${parts.join(' — ')}.`;
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
      ensurePlayheadLoop();
      // The bridge budget is shared, so what you asked for and what the light
      // actually gets can differ once several are running.
      const eff = settings.effective_hz;
      const capped = eff !== undefined && eff !== null && eff < settings.hz - 0.05;
      rateNote.classList.toggle('hidden', !capped);
      if (capped) rateNote.textContent = `bridge budget: running at ${eff} Hz`;
    } else {
      clearPlayhead(card, key);
      rateNote.classList.add('hidden');
    }
  });
}

function syncControls(card, key, st) {
  const select = card.querySelector('.pattern-select');
  if (select.value !== st.pattern_id) selectPattern(select, st.pattern_id);
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
    setCardColor(card, st.hue, st.sat);
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

// Accepts a hex code or Hue's own numbers. hue,sat is the exact form: it is
// what the bridge actually takes, and what the pattern table stores, so typing
// it avoids the rounding a trip through RGB costs.
function parseColorCode(text) {
  const raw = String(text).trim();
  if (!raw) return null;

  let hex = /^#?([0-9a-fA-F]{6})$/.exec(raw);
  if (hex) return rgbToHueSat(hexToRgb(hex[1]));

  hex = /^#?([0-9a-fA-F]{3})$/.exec(raw);
  if (hex) {
    const [r, g, b] = hex[1];      // #f80 is shorthand for #ff8800
    return rgbToHueSat(hexToRgb(`${r}${r}${g}${g}${b}${b}`));
  }

  const pair = /^(\d{1,5})\s*[,/ ]\s*(\d{1,3})$/.exec(raw);
  if (pair) {
    const hue = Number(pair[1]);
    const sat = Number(pair[2]);
    if (hue <= 65535 && sat <= 254) return { hue, sat };
  }
  return null;
}

// The swatch and the code box are two views of one exact pair held on the
// card. Reading the colour back off the swatch would re-round it every time.
function setCardColor(card, hue, sat) {
  const code = card.querySelector('.color-code');
  const swatch = card.querySelector('.color-input');
  code.classList.remove('err');
  if (hue === null || hue === undefined || sat === null || sat === undefined) {
    delete card.dataset.hue;
    delete card.dataset.sat;
    code.value = '';
    return;
  }
  card.dataset.hue = hue;
  card.dataset.sat = sat;
  swatch.value = hueSatToHex(hue, sat);
  code.value = `${hue},${sat}`;
}

// The exact hue/sat pair if one was recorded, otherwise derived from the
// swatch. The pair is held separately because deriving it back from a hex is
// lossy — but when none has been recorded the fallback is not optional: reading
// a missing dataset gives NaN, JSON turns NaN into null, and the server reads
// an explicit null as "this pattern names no colour". Ticking a colour box
// without touching the swatch then silently ran with no colour at all.
function exactColor(holder, swatch) {
  if (holder.dataset.hue !== undefined) {
    return { hue: Number(holder.dataset.hue), sat: Number(holder.dataset.sat) };
  }
  return rgbToHueSat(hexToRgb(swatch.value));
}

function cardColor(card) {
  return exactColor(card, card.querySelector('.color-input'));
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
      STREAM = msg.stream || { running: false };
      applyStreamStatus();
      noteServerClock(msg.now);
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

// ---------- Collapsible panels ----------

// Click a collapsible panel's header to fold it away. Which panels are folded
// is remembered per-browser so the console opens the way you left it.
const SECTIONS_KEY = 'ghf.sections';

function readSectionState() {
  try {
    const raw = JSON.parse(localStorage.getItem(SECTIONS_KEY));
    return raw && typeof raw === 'object' ? raw : {};
  } catch { return {}; }
}

function initCollapsibles() {
  const state = readSectionState();
  $$('.panel.collapsible').forEach(panel => {
    const name = panel.dataset.section;
    if (name in state) panel.classList.toggle('is-collapsed', !!state[name]);
    panel.querySelector('.panel-head').addEventListener('click', (ev) => {
      // A click on a button or input in the header is for that control, not
      // for folding the panel (e.g. Export / Import).
      if (ev.target.closest('button, input, select, label, a, .drag-handle')) return;
      const collapsed = !panel.classList.contains('is-collapsed');
      panel.classList.toggle('is-collapsed', collapsed);
      const next = readSectionState();
      next[name] = collapsed;
      try { localStorage.setItem(SECTIONS_KEY, JSON.stringify(next)); } catch { /* private mode */ }
    });
  });
}

initCollapsibles();

// ---------- Entertainment stream ----------

// The REST path divides the bridge's command budget between every flickering
// light, which is why a seven-bulb room lands near 1 Hz each. Streaming sends
// one frame carrying the whole area, so nothing is divided.
let STREAM = { running: false };
let streamAreas = [];

const streamArea = $('#stream-area');
const streamPattern = $('#stream-pattern');
const streamHz = $('#stream-hz');
const streamMinBri = $('#stream-minbri');
const streamMaxBri = $('#stream-maxbri');
const streamColorEnable = $('#stream-color-enable');
const streamColor = $('#stream-color');
const streamColorCode = $('#stream-color-code');

function setStreamStatus(text, kind = '') {
  const el = $('#stream-status');
  el.textContent = text;
  el.className = `status-line ${kind}`.trim();
}

function streamSettings() {
  // The dataset lives on the swatch itself here, so it is both holder and
  // swatch. Unlike a bulb over REST, a stream frame carries the colour every
  // time — so unticking really can go back to no colour, and null is sent
  // deliberately rather than being left out.
  const colour = streamColorEnable.checked ? exactColor(streamColor, streamColor) : null;
  return {
    hz: Number(streamHz.value),
    min_bri: Number(streamMinBri.value),
    max_bri: Number(streamMaxBri.value),
    hue: colour && colour.hue,
    sat: colour && colour.sat,
  };
}

function setStreamColor(hue, sat) {
  streamColor.dataset.hue = hue;
  streamColor.dataset.sat = sat;
  streamColor.value = hueSatToHex(hue, sat);
  streamColorCode.value = `${hue},${sat}`;
}

async function loadStreamAreas() {
  try {
    const body = await api('/api/stream/areas');
    streamAreas = body.areas || [];
    $('#stream-max-hz').textContent = body.max_stream_hz;
    streamHz.max = body.max_stream_hz;
    const warning = $('#stream-unavailable');
    if (!body.can_stream) {
      // The DTLS key is only handed out at pairing time, so there is no way to
      // acquire one for a console that was paired before streaming existed.
      warning.textContent = 'This console was paired before it could stream. '
        + 'Use Change bridge and pair again — press the link button, then Pair — '
        + 'to get a streaming key.';
      warning.classList.remove('hidden');
    } else {
      warning.classList.add('hidden');
    }
    streamArea.innerHTML = '';
    if (!streamAreas.length) {
      const o = document.createElement('option');
      o.textContent = 'No entertainment areas on this bridge';
      o.value = '';
      streamArea.appendChild(o);
    }
    streamAreas.forEach(a => {
      const o = document.createElement('option');
      o.value = a.id;
      const n = a.light_ids.length;
      o.textContent = `${a.name} (${n} light${n === 1 ? '' : 's'})`;
      if (a.claimed_by_us) {
        // Left behind by this console, so it is ours to take back rather than
        // a conflict — Start clears it first.
        o.textContent += ' — left claimed, will be taken back';
      } else if (a.in_use_by_someone_else) {
        o.textContent += ' — in use elsewhere';
        o.disabled = true;
      }
      streamArea.appendChild(o);
    });
    $('#btn-stream-start').disabled = !body.can_stream || !streamAreas.length;
  } catch (e) {
    setStreamStatus(e.message, 'err');
  }
}

function drawStreamWaveform() {
  drawWaveform('stream', sequenceFor(streamPattern.value), $('#stream-waveform'));
}

function applyStreamFraming() {
  const framing = framingFor(streamPattern.value);
  const set = (input, labelSel, value) => {
    input.value = Math.min(value, Number(input.max));
    $(labelSel).textContent = input.value;
  };
  set(streamHz, '#stream-hz-value', framing.hz);
  set(streamMinBri, '#stream-minbri-value', framing.min_bri);
  set(streamMaxBri, '#stream-maxbri-value', framing.max_bri);
  if (framing.hue !== null && framing.sat !== null) {
    streamColorEnable.checked = true;
    setStreamColor(framing.hue, framing.sat);
  } else {
    streamColorEnable.checked = false;
  }
  drawStreamWaveform();
}

function pushStreamLive() {
  if (!STREAM.running) return;
  clearTimeout(pushStreamLive._t);
  pushStreamLive._t = setTimeout(async () => {
    try {
      await api('/api/stream/update', {
        method: 'POST',
        body: JSON.stringify({ pattern_id: streamPattern.value, ...streamSettings() }),
      });
    } catch (e) {
      if (!/Nothing is streaming/i.test(e.message)) setStreamStatus(e.message, 'err');
    }
  }, 180);
}

[[streamHz, '#stream-hz-value'], [streamMinBri, '#stream-minbri-value'],
 [streamMaxBri, '#stream-maxbri-value']].forEach(([input, label]) => {
  input.addEventListener('input', () => {
    $(label).textContent = input.value;
    if (input === streamMinBri && Number(streamMaxBri.value) < Number(input.value)) {
      streamMaxBri.value = input.value;
      $('#stream-maxbri-value').textContent = input.value;
    }
    if (input === streamMaxBri && Number(streamMinBri.value) > Number(input.value)) {
      streamMinBri.value = input.value;
      $('#stream-minbri-value').textContent = input.value;
    }
    pushStreamLive();
  });
});

streamPattern.addEventListener('change', () => { applyStreamFraming(); pushStreamLive(); });
streamColor.addEventListener('input', () => {
  const hs = rgbToHueSat(hexToRgb(streamColor.value));
  setStreamColor(hs.hue, hs.sat);
  if (!streamColorEnable.checked) streamColorEnable.checked = true;
  pushStreamLive();
});
streamColorEnable.addEventListener('change', pushStreamLive);
streamColorCode.addEventListener('input', () => {
  const parsed = parseColorCode(streamColorCode.value);
  if (!parsed) {
    streamColorCode.classList.toggle('err', streamColorCode.value.trim() !== '');
    return;
  }
  streamColorCode.classList.remove('err');
  streamColor.dataset.hue = parsed.hue;
  streamColor.dataset.sat = parsed.sat;
  streamColor.value = hueSatToHex(parsed.hue, parsed.sat);
  if (!streamColorEnable.checked) streamColorEnable.checked = true;
  pushStreamLive();
});

$('#btn-stream-refresh').addEventListener('click', loadStreamAreas);

$('#btn-stream-diagnostics').addEventListener('click', async () => {
  const box = $('#stream-diagnostics');
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  try {
    // Verbatim, for pasting into a bug report: streaming fails against a
    // bridge that whoever is fixing it cannot see.
    box.textContent = JSON.stringify(await api('/api/stream/diagnostics'), null, 2);
    box.classList.remove('hidden');
  } catch (e) {
    setStreamStatus(e.message, 'err');
  }
});

$('#btn-stream-release').addEventListener('click', async () => {
  const areaId = streamArea.value || STREAM.area_id;
  if (!areaId) return;
  try {
    await api('/api/stream/release', { method: 'POST', body: JSON.stringify({ area_id: areaId }) });
    setStreamStatus('Area handed back to the bridge.', 'ok');
    await loadStreamAreas();
  } catch (e) {
    setStreamStatus(e.message, 'err');
  }
});

$('#btn-stream-start').addEventListener('click', async () => {
  if (!streamArea.value) return;
  setStreamStatus('Opening the stream...');
  try {
    await api('/api/stream/start', {
      method: 'POST',
      body: JSON.stringify({
        area_id: streamArea.value, pattern_id: streamPattern.value, ...streamSettings(),
      }),
    });
    setStreamStatus('Streaming.', 'ok');
  } catch (e) {
    setStreamStatus(e.message, 'err');
  }
});

$('#btn-stream-stop').addEventListener('click', async () => {
  try {
    await api('/api/stream/stop', { method: 'POST' });
    setStreamStatus('Stopped. The area is back with the Hue app.', 'ok');
  } catch (e) {
    setStreamStatus(e.message, 'err');
  }
});

function applyStreamStatus() {
  const running = !!STREAM.running;
  $('#btn-stream-start').classList.toggle('hidden', running);
  $('#btn-stream-stop').classList.toggle('hidden', !running);
  streamArea.disabled = running;
  const state = $('#stream-state');
  if (running) {
    const area = streamAreas.find(a => a.id === STREAM.area_id);
    const n = (STREAM.light_ids || []).length;
    state.textContent = `streaming ${area ? area.name : 'area'} · `
      + `${STREAM.effective_hz} Hz across ${n} light${n === 1 ? '' : 's'}`;
    state.className = 'dim ok';
  } else {
    state.textContent = 'idle';
    state.className = 'dim';
  }
  if (STREAM.error) setStreamStatus(STREAM.error, 'err');
}

// ---------- Panel order ----------

// Panels can be dragged into whatever order suits you, remembered per-browser.
// Driven with pointer events and a floating clone rather than native HTML5
// drag-and-drop: the real panel is moved as you go, so what you see mid-drag is
// exactly where it lands.
const LAYOUT_KEY = 'ghf.layout';
let dragState = null;

function savedOrder() {
  try {
    const raw = JSON.parse(localStorage.getItem(LAYOUT_KEY));
    return Array.isArray(raw) ? raw : null;
  } catch { return null; }
}

function applySavedOrder() {
  const dash = $('#dash');
  const order = savedOrder();
  $('#layout-tools').classList.toggle('show', !!order);
  if (!order) return;
  // Anything the saved order doesn't mention (a panel added by an update) keeps
  // its markup position by being left where it already is.
  order.forEach(name => {
    const panel = dash.querySelector(`.dash-card[data-section="${name}"]`);
    if (panel) dash.appendChild(panel);
  });
}

function persistOrder() {
  const order = $$('#dash > .dash-card').map(p => p.dataset.section);
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(order)); } catch { /* private mode */ }
  $('#layout-tools').classList.add('show');
}

function panelAfterPoint(y) {
  return $$('#dash > .dash-card:not(.is-dragging)')
    .find(p => { const r = p.getBoundingClientRect(); return y < r.top + r.height / 2; }) || null;
}

function onDragMove(ev) {
  if (!dragState) return;
  const { panel, clone, dx, dy } = dragState;
  clone.style.transform = `translate(${ev.clientX - dx}px, ${ev.clientY - dy}px)`;
  const after = panelAfterPoint(ev.clientY);
  const dash = $('#dash');
  if (after === null) { if (dash.lastElementChild !== panel) dash.appendChild(panel); }
  else if (after !== panel) dash.insertBefore(panel, after);
}

function endDrag() {
  if (!dragState) return;
  window.removeEventListener('pointermove', onDragMove);
  dragState.clone.remove();
  dragState.panel.classList.remove('is-dragging');
  $('#dash').classList.remove('dnd-active');
  dragState = null;
  persistOrder();
}

function startDrag(ev, panel) {
  if (ev.pointerType === 'mouse' && ev.button !== 0) return;   // left button only
  ev.preventDefault();
  ev.stopPropagation();
  const r = panel.getBoundingClientRect();
  const clone = panel.cloneNode(true);
  clone.classList.add('drag-clone');
  clone.classList.remove('is-dragging');
  clone.style.width = `${r.width}px`;
  clone.style.transform = `translate(${r.left}px, ${r.top}px)`;
  document.body.appendChild(clone);
  panel.classList.add('is-dragging');
  $('#dash').classList.add('dnd-active');
  dragState = { panel, clone, dx: ev.clientX - r.left, dy: ev.clientY - r.top };
  window.addEventListener('pointermove', onDragMove);
  window.addEventListener('pointerup', endDrag, { once: true });
  window.addEventListener('pointercancel', endDrag, { once: true });
}

function initLayout() {
  applySavedOrder();
  $$('#dash > .dash-card').forEach(panel => {
    const grip = document.createElement('span');
    grip.className = 'drag-handle';
    grip.title = 'Drag to reorder';
    grip.textContent = '\u2833';                 // braille grip
    const head = panel.querySelector('.panel-head');
    head.insertBefore(grip, head.firstChild);
    grip.addEventListener('pointerdown', ev => startDrag(ev, panel));
  });
  $('#btn-reset-layout').addEventListener('click', () => {
    try { localStorage.removeItem(LAYOUT_KEY); } catch { /* private mode */ }
    location.reload();
  });
}

initLayout();

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
