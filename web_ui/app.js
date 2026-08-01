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
// Stat-tile contract: 1,284 / 12.9K / $4.2M -- anything under 10K keeps its
// exact digits. Compacting 1,983 to "2K" throws away real precision.
const compact = n => {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, '') + 'K';
  return Math.round(n).toLocaleString();
};
const dur = s => {
  if (s == null) return '—';
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`;
};
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
      <td class="accent">${fmt(p.boss_dps)}</td>
      <td class="good">${fmt(p.hps)}</td>
      <td class="good">${fmt(p.effective_hps)}</td>
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
  if (d.can_upload) {
    loadPullTimeline(d.pull, d.duration);
    loadPullDeaths(d.pull, d);
  }
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
    </div>
    ${d.can_upload ? `
      <div class="panel" id="m-timeline-panel" style="margin-top:14px">
        <h2>Timeline<span class="sub" style="margin-left:8px;font-weight:400">
          drag across the chart to select a range and recalculate stats for just that slice</span></h2>
        <div id="m-timeline-box" class="chart-wrap" style="min-height:220px"><div class="loading">Re-reading the log…</div></div>
        <div id="m-timeline-summary"></div>
      </div>
      <div id="m-deaths-box" style="margin-top:14px"></div>` : ''}`;
  if (d.can_upload) {
    $('#modal-upload').onclick = () => uploadPull(d.pull);
  }
  $('#modal').classList.add('open');
}

async function loadPullTimeline(pullNum, totalDuration) {
  const box = $('#m-timeline-box');
  if (!box) return;
  let tl;
  try {
    tl = await api(`/api/history/${pullNum}/timeline`);
  } catch {
    tl = null;
  }
  if (!tl || tl.error || !Object.keys(tl.players).length) {
    box.innerHTML = '<div class="empty">No timeline data for this pull.</div>';
    return;
  }
  timelineChart(box, $('#m-timeline-summary'), tl, totalDuration);
}

async function loadPullDeaths(pullNum, pull) {
  const box = $('#m-deaths-box');
  if (!box) return;
  const res = await api(`/api/history/${pullNum}/deaths`);
  if (res.error || !res.reports || !res.reports.length) return;  // no deaths -- say nothing, don't clutter
  renderDeathsInto(box, res, pull);
}

/* Ported from analysis/static/app.js -- same StarParse-style timeline
   scrubber the corpus browser uses, just fed from a live/History pull
   instead of the corpus index. */
function timelineChart(box, summaryEl, tl, totalDuration) {
  const names = Object.keys(tl.players);
  const n = tl.duration > 0 ? Math.max(1, Math.ceil(tl.duration / tl.bucket_seconds)) : 1;
  const totalDmg = new Array(n).fill(0);
  names.forEach(name => tl.players[name].damage.forEach((v, i) => { totalDmg[i] += v; }));

  const H = 220, pad = { l: 60, r: 16, t: 26, b: 30 };
  const W = Math.max(560, Math.round(box.clientWidth || 900));
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const hiRaw = Math.max(...totalDmg, 1) / tl.bucket_seconds;
  const mag = Math.pow(10, Math.floor(Math.log10(hiRaw || 1)));
  const hi = Math.ceil((hiRaw * 1.1) / (mag / 2 || 1)) * (mag / 2 || 1) || 1;

  const X = i => pad.l + (i / Math.max(n - 1, 1)) * iw;
  const Y = v => pad.t + ih - Math.min(v / hi, 1) * ih;

  let grid = '';
  for (let k = 0; k <= 3; k++) {
    const v = hi * k / 3, y = Y(v);
    grid += `<line class="gridline" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}"/>
             <text x="${pad.l - 8}" y="${y + 3.5}" text-anchor="end">${compact(v)}</text>`;
  }

  const totalLine = totalDmg.map((v, i) =>
    `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v / tl.bucket_seconds).toFixed(1)}`).join('');
  const totalArea = `${totalLine}L${X(n - 1).toFixed(1)},${Y(0)}L${X(0).toFixed(1)},${Y(0)}Z`;

  let phaseMarks = '';
  (tl.phases || []).forEach(p => {
    const bi = Math.min(n - 1, p.start_offset / tl.bucket_seconds);
    const x = X(bi);
    phaseMarks += `<line x1="${x.toFixed(1)}" y1="${pad.t}" x2="${x.toFixed(1)}" y2="${pad.t + ih}"
        stroke="var(--ink-muted)" stroke-width="1" stroke-dasharray="3 3" opacity=".5"/>
      <text x="${x.toFixed(1)}" y="${pad.t - 8}" text-anchor="start" transform="rotate(0)"
        style="font-size:9.5px">${esc(p.name)}</text>`;
  });

  const playerOptions = names
    .sort((a, b) => tl.players[b].damage.reduce((s, v) => s + v, 0) - tl.players[a].damage.reduce((s, v) => s + v, 0))
    .map(nm => `<option value="${esc(nm)}">${esc(nm)}</option>`).join('');

  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <label class="fld"><span>Highlight</span>
        <select id="tl-player"><option value="">— total raid damage only —</option>${playerOptions}</select>
      </label>
      <button id="tl-reset" class="ghost" style="display:none">Clear selection</button>
    </div>
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Damage over time for this pull">
      ${grid}
      <path d="${totalArea}" fill="var(--s1)" opacity=".12"/>
      <path d="${totalLine}" fill="none" stroke="var(--s1)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round" opacity=".55"/>
      <path id="tl-hl" d="" fill="none" stroke="var(--s2)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
      ${phaseMarks}
      <rect id="tl-sel" x="0" y="${pad.t}" width="0" height="${ih}" fill="var(--s1)" opacity=".14" style="display:none"/>
      <line class="axis" x1="${pad.l}" y1="${pad.t + ih}" x2="${W - pad.r}" y2="${pad.t + ih}"/>
      <rect id="tl-hit" x="${pad.l}" y="${pad.t}" width="${iw}" height="${ih}" fill="transparent" style="cursor:crosshair"/>
    </svg>
    <div class="tooltip" id="tl-tip"></div>`;

  const svg = box.querySelector('svg'), sel = box.querySelector('#tl-sel'), hit = box.querySelector('#tl-hit');
  const hl = box.querySelector('#tl-hl'), tip = box.querySelector('#tl-tip'), resetBtn = box.querySelector('#tl-reset');
  const playerSel = box.querySelector('#tl-player');

  const bucketAt = clientX => {
    const r = svg.getBoundingClientRect();
    const sx = (clientX - r.left) / r.width * W;
    return Math.max(0, Math.min(n - 1, Math.round((sx - pad.l) / iw * (n - 1))));
  };

  function drawHighlight() {
    const name = playerSel.value;
    if (!name) { hl.setAttribute('d', ''); return; }
    const arr = tl.players[name].damage;
    hl.setAttribute('d', arr.map((v, i) =>
      `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v / tl.bucket_seconds).toFixed(1)}`).join(''));
  }
  playerSel.onchange = drawHighlight;

  function renderSummary(fromBucket, toBucket) {
    const lo = Math.min(fromBucket, toBucket), hi2 = Math.max(fromBucket, toBucket);
    const span = (hi2 - lo + 1) * tl.bucket_seconds;
    const rows = names.map(name => {
      const p = tl.players[name];
      const dmg = p.damage.slice(lo, hi2 + 1).reduce((a, b) => a + b, 0);
      const heal = p.healing.slice(lo, hi2 + 1).reduce((a, b) => a + b, 0);
      return { name, dmg, heal };
    }).filter(r => r.dmg || r.heal).sort((a, b) => b.dmg - a.dmg);
    if (!rows.length) { summaryEl.innerHTML = ''; return; }
    summaryEl.innerHTML = `<div class="sub" style="margin:10px 0 6px">
        Selected ${dur(span)} (${dur(lo * tl.bucket_seconds)}–${dur((hi2 + 1) * tl.bucket_seconds)} into the pull)</div>
      <div class="tw"><table><thead><tr><th>Player</th><th>DPS</th><th>Damage</th><th>HPS</th><th>Healing</th></tr></thead>
      <tbody>${rows.map(r => `<tr><td class="name">${esc(r.name)}</td>
        <td>${fmt(r.dmg / Math.max(span, 1))}</td><td class="dim">${compact(r.dmg)}</td>
        <td>${fmt(r.heal / Math.max(span, 1))}</td><td class="dim">${compact(r.heal)}</td></tr>`).join('')}
      </tbody></table></div>`;
  }

  let dragStart = null;
  hit.addEventListener('mousedown', e => {
    dragStart = bucketAt(e.clientX);
    sel.style.display = 'block';
    resetBtn.style.display = 'inline-block';
  });
  hit.addEventListener('mousemove', e => {
    const b = bucketAt(e.clientX);
    tip.classList.add('show');
    const r = svg.getBoundingClientRect();
    tip.style.left = (X(b) / W * r.width) + 'px';
    tip.style.top = '4px';
    const t = b * tl.bucket_seconds;
    tip.innerHTML = `<div class="t-row">${dur(t)} into the pull</div>`;
    if (dragStart === null) return;
    const lo = Math.min(dragStart, b), hi2 = Math.max(dragStart, b);
    sel.setAttribute('x', X(lo)); sel.setAttribute('width', Math.max(1, X(hi2) - X(lo)));
  });
  hit.addEventListener('mouseleave', () => { tip.classList.remove('show'); });
  window.addEventListener('mouseup', e => {
    if (dragStart === null) return;
    const end = bucketAt(e.clientX);
    if (end === dragStart) {
      sel.style.display = 'none'; resetBtn.style.display = 'none';
      summaryEl.innerHTML = '';
    } else {
      renderSummary(dragStart, end);
    }
    dragStart = null;
  });
  resetBtn.onclick = () => {
    sel.style.display = 'none'; resetBtn.style.display = 'none'; summaryEl.innerHTML = '';
  };
}

/* Ported from analysis/static/app.js -- same death-forensics rendering the
   corpus browser uses, adapted to write into a given container instead of
   a fixed page section. */
function renderDeathsInto(container, res, pull) {
  if (!res.reports.length) return;
  const s = res.summary;
  const top = s.killing_abilities[0];
  const wipe = res.reports.length >= 5 &&
    (Math.max(...res.reports.map(r => r.death_time)) - Math.min(...res.reports.map(r => r.death_time))) < 15;

  let html = `<div class="panel"><h2>Deaths in this pull<span class="sub" style="margin-left:8px;font-weight:400">${s.total_deaths} total</span></h2>`;

  if (wipe && top) html += `<div class="callout crit">
      <span class="ico">!</span><div><b>Looks like a wipe.</b>
      ${res.reports.length} players died within
      ${(Math.max(...res.reports.map(r => r.death_time)) - Math.min(...res.reports.map(r => r.death_time))).toFixed(1)}s
      of each other, most to <b>${esc(top.ability)}</b>.</div></div>`;
  html += '</div>';

  html += res.reports.map(r => {
    const mx = Math.max(...r.by_ability.map(a => a.total), 1);
    const ready = r.defensives.filter(d => d.available_at_death && !d.used_in_window);
    return `<div class="death">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap">
        <div><span class="who">${esc(r.victim)}</span>
          <span class="when"> died at ${r.death_time.toFixed(1)}s</span></div>
        <div class="when">${fmt(r.damage_in_window)} damage taken in the last ${r.window_seconds}s</div>
      </div>
      ${r.killing_blow ? `<div class="callout crit"><span class="ico">✕</span><div>
        Killed by <b>${esc(r.killing_blow.ability)}</b>
        from ${esc(r.killing_blow.source || 'unknown')} for
        <b>${fmt(r.killing_blow.amount)}</b></div></div>` : ''}
      ${ready.length ? `<div class="callout"><span class="ico">▲</span><div>
        <b>${ready.map(d => esc(d.ability)).join(', ')}</b> looked available and wasn't used
        <span class="dim">— inferred from casts seen this pull, so treat as a hint not a verdict</span>
        </div></div>` : ''}
      <div class="tw"><table>
        <thead><tr><th>Incoming</th><th>Total</th><th></th><th>Hits</th><th>Biggest</th><th>From</th></tr></thead>
        <tbody>${r.by_ability.map(a => `<tr>
          <td class="name">${esc(a.ability)}</td>
          <td>${fmt(a.total)}</td>
          <td style="width:110px"><div class="meter" style="background:rgba(208,59,59,.18)">
            <i style="width:${(a.total / mx * 100).toFixed(0)}%;background:var(--critical)"></i></div></td>
          <td class="dim">${a.hits}</td>
          <td class="warning">${fmt(a.max)}</td>
          <td class="dim">${esc(a.sources.slice(0, 2).join(', '))}</td></tr>`).join('')}
        </tbody></table></div>
    </div>`;
  }).join('');

  container.innerHTML = html;
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
  if (s.raid_buff_casts) statParts.push(`Raid Buffs Used ${s.raid_buff_casts}x`);
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
    <h2 style="font-size:13px;margin-top:14px">Raid buffs used</h2>
    ${abilityTable(b.raid_buff_by_ability, 'Ability')}
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

// --------------------------------------------------------- update banner
// The backend only checks GitHub once, at startup (see main.py's
// UpdateHolder) -- no need to keep polling /api/update for the rest of
// the session. Check once on load, then once more shortly after in case
// that background thread hadn't finished yet on first paint.
let updateDismissed = false;
async function checkForUpdate() {
  if (updateDismissed) return;
  try {
    const data = await api('/api/update');
    if (data.available && !updateDismissed) {
      $('#update-banner-text').textContent = `A new version is available: v${data.version}`;
      $('#update-banner-link').href = data.url;
      $('#update-banner').style.display = '';
    }
  } catch (e) { /* silent -- an update check failing is never worth surfacing */ }
}
$('#update-banner-dismiss').addEventListener('click', () => {
  updateDismissed = true;
  $('#update-banner').style.display = 'none';
});
checkForUpdate();
setTimeout(checkForUpdate, 5000);

// ------------------------------------------------------------- tab poll
setInterval(refreshActiveTab, TAB_POLL_MS);

pollLive();
