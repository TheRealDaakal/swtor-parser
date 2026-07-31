'use strict';
/* DPS frontend -- no dependencies, no CDN, works fully offline. Polls the
   local backend (web_server.py) and renders in place. File pickers go
   through pywebview's js_api bridge (see main.py's Api class) since a
   browser <input type=file> can't hand back a real filesystem path. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => n == null ? '—' : Math.round(n).toLocaleString();
const api = (p, opts) => fetch(p, opts).then(r => r.json());
const post = (p, body) => api(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});

const LIVE_POLL_MS = 500;
const TAB_POLL_MS = 2000;

// -------------------------------------------------------------- tab nav
let activeView = 'live';
function activate(name) {
  activeView = name;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
  refreshActiveTab();
}
$$('.tab').forEach(t => {
  t.addEventListener('click', () => activate(t.dataset.view));
  t.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') activate(t.dataset.view); });
});

function refreshActiveTab() {
  if (activeView === 'history') loadHistory();
  else if (activeView === 'timers') loadTimerRules();
  else if (activeView === 'overlays') loadOverlays();
  else if (activeView === 'parsely') loadParselySettings();
}

// -------------------------------------------------------------- live tab
function renderMeter(players) {
  const tbody = $('#meter tbody');
  const empty = $('#meter-empty');
  if (!players.length) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = players.map(p => {
    const mitText = (p.taken > 0 || p.mitigated > 0) ? `${Math.round(p.mitigated)}%` : '—';
    return `<tr>
      <td class="name">${esc(p.name)}</td>
      <td class="accent">${fmt(p.dps)}</td>
      <td class="good">${fmt(p.hps)}</td>
      <td>${fmt(p.taken)}</td>
      <td>${mitText}</td>
      <td>${p.deaths}</td>
    </tr>`;
  }).join('');
}

function renderAlerts(alerts) {
  $('#alerts').innerHTML = alerts.map(a => `<div class="alert-banner">${esc(a.toUpperCase())}</div>`).join('');
}

function timerRow(label, remaining, total) {
  const pct = total > 0 ? Math.max(0, Math.min(100, (remaining / total) * 100)) : 0;
  return `<div class="timer-row">
    <div class="lbl">${esc(label)}</div>
    <div class="rem">${remaining.toFixed(1)}s</div>
    <div class="meter"><i style="width:${pct}%"></i></div>
  </div>`;
}

function renderTimerList(id, rows) {
  const box = $(`#${id}`);
  const empty = $(`#${id}-empty`);
  if (!rows.length) { box.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  box.innerHTML = rows.map(r => timerRow(r.label, r.remaining, r.total)).join('');
}

function renderDotsHots(rows) {
  const box = $('#dotshots');
  const empty = $('#dotshots-empty');
  if (!rows.length) { box.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  box.innerHTML = rows.map(r => timerRow(`${r.tag}  ${r.label}`, r.remaining, r.total)).join('');
}

function renderTaunts(taunts) {
  const box = $('#taunts');
  const empty = $('#taunts-empty');
  if (!taunts.length) { box.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  box.innerHTML = taunts.map(t => `
    <div class="timer-row">
      <div class="lbl" style="flex:1;color:${t.hit ? 'var(--good)' : 'var(--critical)'}">
        ${t.hit ? '✓' : '✗'} ${esc(t.text)}
      </div>
      <div class="rem">${t.ago.toFixed(1)}s ago</div>
    </div>`);
}

async function pollLive() {
  try {
    const snap = await fetch('/api/live', { cache: 'no-store' }).then(r => r.json());
    $('#watch-status').textContent = snap.watching || '';
    $('#boss-status').textContent = snap.boss || 'Waiting for combat...';
    $('#duration').textContent = `Encounter: ${snap.duration.toFixed(1)}s`;
    renderMeter(snap.players);
    renderAlerts(snap.alerts);
    renderTimerList('timers', snap.timers);
    renderTimerList('cooldowns', snap.cooldowns);
    renderDotsHots(snap.dots_hots);
    renderTaunts(snap.taunts);
  } catch (e) {
    $('#boss-status').textContent = 'disconnected — retrying…';
  } finally {
    setTimeout(pollLive, LIVE_POLL_MS);
  }
}

// ----------------------------------------------------------- history tab
async function loadHistory() {
  const rows = await api('/api/history');
  const tbody = $('#history-table tbody');
  const empty = $('#history-empty');
  if (!rows.length) { tbody.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map(r => `
    <tr class="clickable" onclick="openPull(${r.pull})">
      <td>${r.pull}</td>
      <td>${r.duration.toFixed(1)}s</td>
      <td class="dim">${r.top.map(p => `${esc(p.name)} ${fmt(p.dps)}`).join(', ')}</td>
    </tr>`).join('');
}

async function openPull(pullNum) {
  const d = await api(`/api/history/${pullNum}`);
  if (d.error) return;
  renderPullModal(d);
}

function renderPullModal(d) {
  const rows = d.players.map(p => {
    const mitText = (p.taken > 0 || p.mitigated > 0) ? `${Math.round(p.mitigated)}%` : '—';
    return `<tr class="clickable" onclick="openBreakdown(${d.pull}, '${esc(p.name).replace(/'/g, "\\'")}')">
      <td class="name">${esc(p.name)}</td>
      <td class="accent">${fmt(p.dps)}</td>
      <td class="good">${fmt(p.hps)}</td>
      <td>${fmt(p.taken)}</td>
      <td>${mitText}</td>
      <td>${p.deaths}</td>
    </tr>`;
  }).join('');
  $('#modal-body').innerHTML = `
    <div class="modal-head">
      <div>
        <h3>Pull #${d.pull}${d.label ? ' — ' + esc(d.label) : ''}</h3>
        <div class="sub">${d.duration.toFixed(1)}s · double-click a player for ability breakdown</div>
      </div>
    </div>
    <div class="tw"><table><thead><tr>
      <th>Player</th><th>DPS</th><th>HPS</th><th>Damage Taken</th><th>Mitigated</th><th>Deaths</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div style="margin-top:14px">
      <button id="modal-upload" ${d.can_upload ? '' : 'disabled title="No line-range data for this pull (imported/merged, or recorded before this feature)"'}>
        Upload This Pull to Parsely
      </button>
      <span id="modal-upload-status" class="sub" style="margin-left:8px"></span>
    </div>`;
  if (d.can_upload) {
    $('#modal-upload').onclick = () => uploadPull(d.pull);
  }
  $('#modal').classList.add('open');
}

async function uploadPull(pullNum) {
  const notes = prompt('Optional note for this upload:') || null;
  $('#modal-upload-status').textContent = 'Uploading...';
  const result = await post(`/api/history/${pullNum}/upload`, { notes });
  $('#modal-upload-status').textContent = result.success
    ? `Uploaded: ${result.link}` : `Failed: ${result.error}`;
}

async function openBreakdown(pullNum, name) {
  const b = await api(`/api/history/${pullNum}/player/${encodeURIComponent(name)}`);
  if (b.error) return;
  const s = b.stats;
  const statParts = [`APM ${s.apm}`];
  if (s.burst_dps != null) statParts.push(`Burst DPS ${fmt(s.burst_dps)}`);
  if (s.burst_hps != null) statParts.push(`Burst EHPS ${fmt(s.burst_hps)}`);
  if (s.accuracy_pct != null) statParts.push(`Accuracy ${s.accuracy_pct}%`);
  if (s.crit_pct != null) statParts.push(`Crit ${s.crit_pct}%`);
  if (s.heal_crit_pct != null) statParts.push(`Heal Crit ${s.heal_crit_pct}%`);
  if (s.times_interrupted) statParts.push(`Interrupted ${s.times_interrupted}x`);
  if (s.cc_casts) statParts.push(`CC Applied ${s.cc_casts}x`);
  if (s.boss_dps != null) statParts.push(`Boss DPS ${fmt(s.boss_dps)}`);

  const abilityTable = (rows, keyLabel) => rows.length ? `
    <div class="tw"><table><thead><tr><th>${keyLabel}</th><th>Total</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td class="name">${esc(r.ability ?? r.target)}</td><td>${fmt(r.amount)}</td></tr>`).join('')}
    </tbody></table></div>` : '<p class="empty">none</p>';

  $('#modal-body').innerHTML = `
    <div class="modal-head">
      <div>
        <h3>${esc(b.name)} — ability breakdown</h3>
        <div class="sub accent" style="font-weight:600">${statParts.join('   |   ') || 'No attacks/heals yet'}</div>
      </div>
    </div>
    <h2 style="font-size:13px">Damage by ability</h2>
    ${abilityTable(b.damage_by_ability, 'Ability')}
    <h2 style="font-size:13px;margin-top:14px">Healing by ability</h2>
    ${abilityTable(b.healing_by_ability, 'Ability')}
    <h2 style="font-size:13px;margin-top:14px">Damage by target</h2>
    ${abilityTable(b.damage_by_target, 'Target')}
    <h2 style="font-size:13px;margin-top:14px">Crowd control applied</h2>
    ${abilityTable(b.cc_by_ability, 'Ability')}
    <div style="margin-top:14px"><button onclick="openPull(${pullNum})">&larr; Back to pull</button></div>`;
  $('#modal').classList.add('open');
}

window.openPull = openPull;
window.openBreakdown = openBreakdown;
window.closeModal = () => $('#modal').classList.remove('open');
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ------------------------------------------------------------ timers tab
async function loadTimerRules() {
  const rules = await api('/api/timer_rules');
  const tbody = $('#rules-table tbody');
  const empty = $('#rules-empty');
  if (!rules.length) { tbody.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  tbody.innerHTML = rules.map(r => `
    <tr>
      <td>${esc(r.keyword)}</td><td>${esc(r.label)}</td><td>${r.duration}</td>
      <td>${r.warn || '-'}</td><td>${r.voice ? 'on' : 'off'}</td>
      <td><button class="rule-del" onclick="deleteRule(${r.index})">remove</button></td>
    </tr>`).join('');
}

async function deleteRule(index) {
  await post('/api/timer_rules/delete', { index });
  loadTimerRules();
}
window.deleteRule = deleteRule;

$('#t-add').addEventListener('click', async () => {
  const keyword = $('#t-keyword').value.trim();
  const duration = parseFloat($('#t-duration').value);
  if (!keyword || Number.isNaN(duration)) return;
  await post('/api/timer_rules', {
    keyword, label: $('#t-label').value.trim(), duration,
    warn: parseFloat($('#t-warn').value) || 0, voice: $('#t-voice').checked,
  });
  $('#t-keyword').value = ''; $('#t-label').value = '';
  $('#t-duration').value = ''; $('#t-warn').value = '';
  loadTimerRules();
});

// ----------------------------------------------------------- overlays tab
async function loadOverlays() {
  const state = await api('/api/overlays');
  $('#ov-lock').checked = state.locked;
  const box = $('#overlay-groups');
  box.innerHTML = state.groups.map(group => `
    <div class="panel overlay-group">
      <h3>${esc(group)}</h3>
      ${state.items.filter(i => i.group === group).map(i => `
        <button class="overlay-toggle ${i.on ? 'on' : ''}" onclick="toggleOverlay('${i.key}')">
          ${esc(i.label)}
        </button>`).join('')}
    </div>`).join('');
}

async function toggleOverlay(key) {
  await post('/api/overlays/toggle', { key });
  loadOverlays();
}
window.toggleOverlay = toggleOverlay;

$('#ov-lock').addEventListener('change', async e => {
  await post('/api/overlays/lock', { locked: e.target.checked });
});
$('#ov-clear').addEventListener('click', async () => {
  await post('/api/overlays/clear');
  loadOverlays();
});

// ------------------------------------------------------------ import tab
function pywebviewApi() {
  if (window.pywebview && window.pywebview.api) return window.pywebview.api;
  return null;
}

$('#import-merge-btn').addEventListener('click', async () => {
  const capi = pywebviewApi();
  if (!capi) { $('#import-merge-result').textContent = 'File picker unavailable.'; return; }
  const paths = await capi.pick_files();
  if (!paths || !paths.length) return;
  $('#import-merge-result').textContent = 'Importing...';
  const result = await post('/api/import/merge', { paths });
  $('#import-merge-result').textContent = result.message || result.error || '';
});

$('#import-session-btn').addEventListener('click', async () => {
  const capi = pywebviewApi();
  if (!capi) { $('#import-session-result').textContent = 'File picker unavailable.'; return; }
  const paths = await capi.pick_files();
  if (!paths || !paths.length) return;
  $('#import-session-result').textContent = 'Importing...';
  const result = await post('/api/import/session', { paths });
  $('#import-session-result').textContent = result.message || result.error || '';
});

// ----------------------------------------------------------- parsely tab
async function loadParselySettings() {
  const s = await api('/api/parsely_settings');
  $('#p-username').value = s.username || '';
  $('#p-password').value = '';
  $('#p-guild').value = s.guild || '';
  $('#p-guildlog').checked = !!s.guild_log;
  $('#p-visibility').value = String(s.visibility ?? 1);
}

$('#p-save').addEventListener('click', async () => {
  await post('/api/parsely_settings', {
    username: $('#p-username').value, password: $('#p-password').value,
    guild: $('#p-guild').value, guild_log: $('#p-guildlog').checked,
    visibility: parseInt($('#p-visibility').value, 10),
  });
  $('#p-status').textContent = 'Settings saved.';
});

$('#p-upload-file').addEventListener('click', async () => {
  const capi = pywebviewApi();
  if (!capi) { $('#p-status').textContent = 'File picker unavailable.'; return; }
  const path = await capi.pick_file();
  if (!path) return;
  const notes = prompt('Optional note for this upload:') || null;
  $('#p-status').textContent = 'Uploading...';
  const result = await post('/api/parsely/upload_path', { path, notes });
  $('#p-status').textContent = result.success ? `Uploaded: ${result.link}` : `Upload failed: ${result.error}`;
});

$('#p-upload-current').addEventListener('click', async () => {
  const notes = prompt('Optional note for this upload:') || null;
  $('#p-status').textContent = 'Uploading...';
  const result = await post('/api/parsely/upload_current', { notes });
  $('#p-status').textContent = result.success ? `Uploaded: ${result.link}` : `Upload failed: ${result.error}`;
});

// ------------------------------------------------------------- tab poll
setInterval(refreshActiveTab, TAB_POLL_MS);

pollLive();
