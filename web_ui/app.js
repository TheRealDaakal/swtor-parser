'use strict';
/* DPS frontend -- no dependencies, no CDN, works fully offline. Polls the
   local backend (web_server.py) and renders in place. File pickers go
   through pywebview's js_api bridge (see main.py's Api class) since a
   browser <input type=file> can't hand back a real filesystem path. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// parsely_upload.py's <file> response field is documented as already being
// the full report link, but the API isn't ours to fully trust the shape
// of -- if it ever comes back as a bare path/id instead, prefix it onto
// the site's own domain rather than rendering an unclickable half-URL.
const parselyLinkHtml = link => {
  const href = /^https?:\/\//i.test(link) ? link : `https://parsely.io/${link.replace(/^\/+/, '')}`;
  return `Uploaded. <a class="btn-link" href="${esc(href)}" target="_blank" rel="noopener">Go to Parsely &rarr;</a>`;
};
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
// real_start_time is a Unix epoch (seconds) reconstructed from the log
// file's own filename date -- None for pulls where that wasn't available
// (e.g. a multi-file merge import), which just render as "—" rather than
// a made-up date. See stats.py's Encounter.real_start_time.
const historyDate = epochSeconds => {
  if (epochSeconds == null) return '—';
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
    + ' ' + d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
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
  else if (activeView === 'encounters') loadEncounters();
  else if (activeView === 'overlays') { loadOverlays(); loadCharacterSettings(); }
  else if (activeView === 'import') loadCleanupSettings();
  else if (activeView === 'parsely') loadParselySettings();
  else if (activeView === 'settings') loadAudioSettings();
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
  // target tells apart e.g. an AoE DoT genuinely landing on several
  // different adds at once -- several real simultaneous rows that would
  // otherwise all read as the exact same label with no way to tell which
  // is which. Leads with target, same convention as the floating HoT
  // overlay ("you re-target by name, not by buff name"). timerRow() itself
  // escapes the whole label string -- don't pre-escape the pieces here too.
  box.innerHTML = rows.map(r => timerRow(
    r.target ? `${r.target}: ${r.tag} ${r.label}` : `${r.tag}  ${r.label}`,
    r.remaining, r.total
  )).join('');
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
const selectedPulls = new Set();

async function loadHistory() {
  const rows = await api('/api/history');
  const tbody = $('#history-table tbody');
  const empty = $('#history-empty');
  if (!rows.length) { tbody.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  // A pull no longer present (e.g. cleared) shouldn't linger selected.
  const stillThere = new Set(rows.map(r => r.pull));
  for (const p of Array.from(selectedPulls)) if (!stillThere.has(p)) selectedPulls.delete(p);
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td><input type="checkbox" onclick="event.stopPropagation(); togglePullSelected(${r.pull}, this.checked)"
        ${selectedPulls.has(r.pull) ? 'checked' : ''}></td>
      <td class="clickable" onclick="openPull(${r.pull})">${r.pull}</td>
      <td class="clickable dim" onclick="openPull(${r.pull})">${historyDate(r.real_start_time)}</td>
      <td class="clickable" onclick="openPull(${r.pull})">${r.duration.toFixed(1)}s</td>
      <td class="clickable dim" onclick="openPull(${r.pull})">${r.top.map(p => `${esc(p.name)} ${fmt(p.dps)}`).join(', ')}</td>
    </tr>`).join('');
  updateCompareButton();
}

function togglePullSelected(pullNum, checked) {
  if (checked) selectedPulls.add(pullNum); else selectedPulls.delete(pullNum);
  updateCompareButton();
}

function updateCompareButton() {
  $('#history-compare-btn').disabled = selectedPulls.size !== 2;
}
window.togglePullSelected = togglePullSelected;

$('#history-compare-btn').addEventListener('click', async () => {
  const [a, b] = Array.from(selectedPulls).sort((x, y) => x - y);
  const data = await api(`/api/history/compare?a=${a}&b=${b}`);
  if (data.error) return;
  renderCompareModal(data);
});

function renderCompareModal(data) {
  const { a, b } = data;
  const names = Array.from(new Set([...a.players.map(p => p.name), ...b.players.map(p => p.name)])).sort();
  const byName = enc => Object.fromEntries(enc.players.map(p => [p.name, p]));
  const pa = byName(a), pb = byName(b);
  const deltaClass = d => d > 0 ? 'good' : (d < 0 ? 'critical' : 'dim');
  const deltaText = d => (d > 0 ? '+' : '') + fmt(d);

  const rows = names.map(name => {
    const dpsA = pa[name]?.dps || 0, dpsB = pb[name]?.dps || 0;
    const hpsA = pa[name]?.hps || 0, hpsB = pb[name]?.hps || 0;
    return `<tr>
      <td class="name">${esc(name)}</td>
      <td class="accent">${fmt(dpsA)}</td>
      <td class="accent">${fmt(dpsB)}</td>
      <td class="${deltaClass(dpsB - dpsA)}">${deltaText(dpsB - dpsA)}</td>
      <td class="good">${fmt(hpsA)}</td>
      <td class="good">${fmt(hpsB)}</td>
      <td class="${deltaClass(hpsB - hpsA)}">${deltaText(hpsB - hpsA)}</td>
    </tr>`;
  }).join('');

  $('#modal-body').innerHTML = `
    <h2>Comparing Pull ${a.pull} vs Pull ${b.pull}</h2>
    <p class="sub">${esc(a.label || '—')} (${a.duration.toFixed(1)}s) vs
      ${esc(b.label || '—')} (${b.duration.toFixed(1)}s)</p>
    <div class="tw"><table>
      <thead><tr>
        <th>Player</th><th>DPS (${a.pull})</th><th>DPS (${b.pull})</th><th>Δ DPS</th>
        <th>HPS (${a.pull})</th><th>HPS (${b.pull})</th><th>Δ HPS</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  $('#modal').classList.add('open');
}

async function openPull(pullNum) {
  const d = await api(`/api/history/${pullNum}`);
  if (d.error) return;
  renderPullModal(d);
  if (d.can_upload) {
    loadPullSummary(d.pull);
    loadPullTimeline(d.pull, d.duration);
    loadPullDeaths(d.pull, d);
  }
}

async function loadPullSummary(pullNum) {
  const box = $('#m-summary-box');
  if (!box) return;
  const s = await api(`/api/history/${pullNum}/summary`);
  if (s.error) return;
  renderFightSummaryInto(box, s);
}

function renderFightSummaryInto(box, s) {
  if (!s.boss_name) return;  // no recognized boss -- nothing factual to say, don't clutter with "Unknown"
  // .pill's own `color` wins over .good/.critical/.dim at equal CSS
  // specificity (it's declared later in app.css) -- set the color inline
  // instead of relying on class-combination cascade order.
  const outcomeColor = s.outcome === 'kill' ? 'var(--good)' : (s.outcome === 'wipe' ? 'var(--critical)' : 'var(--ink-muted)');
  const outcomeText = s.outcome === 'kill' ? 'KILL' : (s.outcome === 'wipe' ? 'WIPE' : 'unclear');
  box.innerHTML = `
    <div class="panel" style="margin-top:14px">
      <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
        <span class="pill" style="font-weight:700;letter-spacing:.03em;color:${outcomeColor}">${outcomeText}</span>
        <b>${esc(s.boss_name)}</b>
        ${s.phases_seen.length ? `<span class="dim">reached: ${s.phases_seen.map(esc).join(' → ')}</span>` : ''}
      </div>
    </div>`;
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
    ${d.can_upload ? '<div id="m-summary-box"></div>' : ''}
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
  $('#modal-upload-status').innerHTML = result.success
    ? parselyLinkHtml(result.link) : esc(`Failed: ${result.error}`);
}

async function openBreakdown(pullNum, name) {
  const b = await api(`/api/history/${pullNum}/player/${encodeURIComponent(name)}`);
  if (b.error) return;
  const s = b.stats;
  const statParts = [`APM ${s.apm}`];
  if (s.burst_dps != null) statParts.push(`Burst DPS ${fmt(s.burst_dps)}`);
  if (s.burst_hps != null) statParts.push(`Burst HPS ${fmt(s.burst_hps)}`);
  if (s.accuracy_pct != null) statParts.push(`Accuracy ${s.accuracy_pct}%`);
  if (s.crit_pct != null) statParts.push(`Crit ${s.crit_pct}%`);
  if (s.heal_crit_pct != null) statParts.push(`Heal Crit ${s.heal_crit_pct}%`);
  if (s.effective_hps != null) statParts.push(`Healing (Eff.) ${fmt(s.effective_hps)}`);
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
    <h2 style="font-size:13px;margin-top:14px">Rotation Viewer</h2>
    <div class="sub" style="margin-bottom:8px">Splits this pull into segments bounded by every
      occurrence of an ability/effect name -- e.g. a recurring boss mechanic -- and shows this
      player's own cast sequence within each, with idle gaps (a GCD or more with no ability
      activated) flagged in red. Not a rotation optimizer -- it can't tell you what to cast
      instead, only where nothing was cast at all.</div>
    <div class="rotation-controls">
      <input type="text" id="rot-keyword" placeholder="e.g. Creeping Terror">
      <button id="rot-create">Create</button>
    </div>
    <div id="rot-results"></div>
    <div style="margin-top:14px"><button onclick="openPull(${pullNum})">&larr; Back to pull</button></div>`;
  $('#modal').classList.add('open');
  $('#rot-create').addEventListener('click', () => createRotation(pullNum, b.name));
  $('#rot-keyword').addEventListener('keydown', e => {
    if (e.key === 'Enter') createRotation(pullNum, b.name);
  });
}

async function createRotation(pullNum, name) {
  const keyword = $('#rot-keyword').value.trim();
  const results = $('#rot-results');
  if (!keyword) { results.innerHTML = '<p class="empty">Enter a keyword first.</p>'; return; }
  results.innerHTML = '<p class="empty">Loading…</p>';
  const r = await api(`/api/history/${pullNum}/player/${encodeURIComponent(name)}/rotation`
    + `?keyword=${encodeURIComponent(keyword)}`);
  if (r.error) { results.innerHTML = `<p class="empty">${esc(r.error)}</p>`; return; }
  results.innerHTML = r.segments.map(seg => {
    const statLine = [`DPS ${fmt(seg.dps)}`];
    if (seg.ehps) statLine.push(`EHPS ${fmt(seg.ehps)}`);
    if (seg.crit_pct) statLine.push(`Crit ${seg.crit_pct}%`);
    if (seg.idle_seconds) statLine.push(`Idle ${seg.idle_seconds}s`);
    const chips = seg.casts.length ? seg.casts.map(c => c.kind === 'gap' ? `
      <div class="rot-chip gap" title="No ability activated for this long">
        <div class="rot-chip-name">idle</div>
        <div class="rot-chip-amount">${c.seconds}s</div>
      </div>` : `
      <div class="rot-chip${c.is_heal ? ' heal' : ''}${c.is_critical ? ' crit' : ''}">
        <div class="rot-chip-name">${esc(c.ability)}</div>
        <div class="rot-chip-amount">${fmt(c.amount)}</div>
      </div>`).join('') : '<span class="empty">no casts landed</span>';
    return `
      <div class="rot-segment">
        <div class="rot-segment-head">
          <span class="accent" style="font-weight:600">${statLine.join('   ')}</span>
          <span class="sub">${seg.duration}s</span>
        </div>
        <div class="rot-chips">${chips}</div>
      </div>`;
  }).join('');
}
window.createRotation = createRotation;

window.openPull = openPull;
window.openBreakdown = openBreakdown;
window.closeModal = () => $('#modal').classList.remove('open');
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ------------------------------------------------------------ timers tab
const basename = p => p ? p.replace(/^.*[\\/]/, '') : '';

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
      <td>${r.audio_path ? esc(basename(r.audio_path)) : '-'}</td>
      <td><button class="rule-del" onclick="deleteRule(${r.index})">remove</button></td>
    </tr>`).join('');
}

async function deleteRule(index) {
  await post('/api/timer_rules/delete', { index });
  loadTimerRules();
}
window.deleteRule = deleteRule;

let pendingAudioPath = null;

$('#t-audio-pick').addEventListener('click', async () => {
  if (!window.pywebview) return;  // browser-only dev preview, no native dialog available
  const path = await pywebview.api.pick_audio_file();
  if (!path) return;
  pendingAudioPath = path;
  $('#t-audio-name').textContent = basename(path);
  $('#t-audio-clear').style.display = '';
});

$('#t-audio-clear').addEventListener('click', () => {
  pendingAudioPath = null;
  $('#t-audio-name').textContent = 'None (TTS)';
  $('#t-audio-clear').style.display = 'none';
});

$('#t-add').addEventListener('click', async () => {
  const keyword = $('#t-keyword').value.trim();
  const duration = parseFloat($('#t-duration').value);
  if (!keyword || Number.isNaN(duration)) return;
  await post('/api/timer_rules', {
    keyword, label: $('#t-label').value.trim(), duration,
    warn: parseFloat($('#t-warn').value) || 0, voice: $('#t-voice').checked,
    audio_path: pendingAudioPath,
  });
  $('#t-keyword').value = ''; $('#t-label').value = '';
  $('#t-duration').value = ''; $('#t-warn').value = '';
  pendingAudioPath = null;
  $('#t-audio-name').textContent = 'None (TTS)';
  $('#t-audio-clear').style.display = 'none';
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

// ---------------------------------------------------- character settings
async function loadCharacterSettings() {
  const data = await api('/api/character_settings');
  const input = $('#cs-alacrity');
  const saveBtn = $('#cs-save');
  if (data.character) {
    $('#cs-character').textContent = `Settings for: ${data.character}`;
    input.disabled = false;
    saveBtn.disabled = false;
    // Don't stomp on a value the user is actively typing mid-poll.
    if (document.activeElement !== input) input.value = data.alacrity_pct;
  } else {
    $('#cs-character').textContent = 'No character detected yet -- start watching a live log first.';
    input.disabled = true;
    saveBtn.disabled = true;
  }
}

$('#cs-save').addEventListener('click', async () => {
  const pct = parseFloat($('#cs-alacrity').value);
  if (Number.isNaN(pct) || pct < 0) return;
  const btn = $('#cs-save');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving...';
  const result = await post('/api/character_settings', { alacrity_pct: pct });
  btn.textContent = result.ok ? 'Saved!' : (result.error || 'Failed');
  setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
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

async function loadCleanupSettings() {
  const s = await api('/api/cleanup_settings');
  $('#cleanup-retention').value = s.retention_days || 0;
}

$('#cleanup-save').addEventListener('click', async () => {
  const retention_days = parseFloat($('#cleanup-retention').value) || 0;
  const result = await post('/api/cleanup_settings', { retention_days });
  $('#cleanup-result').textContent = result.ok ? 'Saved.' : (result.error || 'Failed');
});

$('#cleanup-now').addEventListener('click', async () => {
  $('#cleanup-result').textContent = 'Cleaning up...';
  const result = await post('/api/cleanup_now', {});
  $('#cleanup-result').textContent = result.ok
    ? `Compressed ${result.archived_count} old log file(s).`
    : (result.error || 'Failed');
});

// -------------------------------------------------------------- settings tab
async function loadAudioSettings() {
  const s = await api('/api/audio_settings');
  $('#snd-muted').checked = !!s.muted;
  const cats = s.category_muted || {};
  $('#snd-cat-boss').checked = !!cats.boss;
  $('#snd-cat-phase').checked = !!cats.phase;
  $('#snd-cat-custom').checked = !!cats.custom;
  $('#snd-categories').classList.toggle('disabled', !!s.muted);
}

async function saveAudioSettings() {
  await post('/api/audio_settings', {
    muted: $('#snd-muted').checked,
    category_muted: {
      boss: $('#snd-cat-boss').checked,
      phase: $('#snd-cat-phase').checked,
      custom: $('#snd-cat-custom').checked,
    },
  });
  $('#snd-categories').classList.toggle('disabled', $('#snd-muted').checked);
}

['#snd-muted', '#snd-cat-boss', '#snd-cat-phase', '#snd-cat-custom'].forEach(id => {
  $(id).addEventListener('change', saveAudioSettings);
});

$('#anon-btn').addEventListener('click', async () => {
  const capi = pywebviewApi();
  if (!capi) { $('#anon-result').textContent = 'File picker unavailable.'; return; }
  const path = await capi.pick_file();
  if (!path) return;
  $('#anon-result').textContent = 'Anonymizing...';
  const result = await post('/api/anonymize_log', { path });
  $('#anon-result').textContent = result.ok
    ? `Saved ${result.players_replaced} player name(s) scrubbed -> ${result.dest_path}`
    : (result.error || 'Failed');
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
  $('#p-status').innerHTML = result.success
    ? parselyLinkHtml(result.link) : esc(`Upload failed: ${result.error}`);
});

$('#p-upload-current').addEventListener('click', async () => {
  const notes = prompt('Optional note for this upload:') || null;
  $('#p-status').textContent = 'Uploading...';
  const result = await post('/api/parsely/upload_current', { notes });
  $('#p-status').innerHTML = result.success
    ? parselyLinkHtml(result.link) : esc(`Upload failed: ${result.error}`);
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
      // Older releases (or a hand-cut one) might not have a zip attached --
      // self-update needs it, manual download from the link always works.
      $('#update-banner-apply').style.display = data.zip_url ? '' : 'none';
      $('#update-banner').style.display = '';
    }
  } catch (e) { /* silent -- an update check failing is never worth surfacing */ }
}
$('#update-banner-dismiss').addEventListener('click', () => {
  updateDismissed = true;
  $('#update-banner').style.display = 'none';
});
$('#update-banner-apply').addEventListener('click', async () => {
  const btn = $('#update-banner-apply');
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Downloading...';
  $('#update-banner-text').textContent = 'Downloading the update -- this window will restart automatically.';
  try {
    const result = await post('/api/update/apply', {});
    if (!result.success) {
      btn.disabled = false;
      btn.textContent = original;
      $('#update-banner-text').textContent = `Update failed: ${result.error}`;
    }
    // On success the app closes and relaunches on its own shortly after --
    // nothing further to do here; the page just goes away.
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    $('#update-banner-text').textContent = 'Update failed: could not reach the app.';
  }
});
checkForUpdate();
setTimeout(checkForUpdate, 5000);

// --------------------------------------------------------- encounters tab
// Full-schema visual editor for boss_definitions.py's Condition/BossPhase/
// BossCounter/BossTimerDef dataclasses. `encDraft` mirrors the exact JSON
// shape the backend reads/writes -- no separate frontend model -- so
// saving is just `post('/api/encounters', encDraft)`. Editing is done via
// `data-path` attributes (a dot-joined path into encDraft, e.g.
// "timers.2.trigger.conditions.0.percent") read by one delegated
// input/change/click listener on #modal-body, which the History/Rotation
// modals also use -- safe to share since those never emit data-path/
// data-action attributes (grepped for collisions before adding this).
const COND_TYPES = [
  'combat_start', 'combat_end', 'hp_below', 'ability_cast', 'effect_applied',
  'effect_removed', 'npc_appears', 'entity_death', 'timer_expires', 'timer_started',
  'timer_time_remaining', 'phase_ended', 'phase_entered', 'phase_active',
  'any_phase_change', 'counter_compare', 'counter_reaches', 'counter_changes',
  'any_of', 'all_of', 'not',
];
const COND_FIELDS = {
  combat_start: [], combat_end: [], any_phase_change: [],
  hp_below: ['percent', 'selector'],
  ability_cast: ['keyword', 'selector'],
  effect_applied: ['keyword', 'selector'],
  effect_removed: ['keyword', 'selector'],
  npc_appears: ['selector'],
  entity_death: ['selector'],
  timer_expires: ['timer_id'],
  timer_started: ['timer_id'],
  timer_time_remaining: ['timer_id', 'operator', 'value'],
  phase_ended: ['phase_id'],
  phase_entered: ['phase_id'],
  phase_active: ['phase_ids'],
  counter_compare: ['counter_id', 'operator', 'value'],
  counter_reaches: ['counter_id', 'value'],
  counter_changes: ['counter_id'],
  any_of: ['conditions'],
  all_of: ['conditions'],
  not: ['condition'],
};

let encDraft = null;
let encSourceId = null; // id being edited/customized; null while creating new

function getAtPath(path) {
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), encDraft);
}
function setAtPath(path, value) {
  const parts = path.split('.');
  let obj = encDraft;
  for (let i = 0; i < parts.length - 1; i++) obj = obj[parts[i]];
  obj[parts[parts.length - 1]] = value;
}
function removeAtPath(path) {
  const parts = path.split('.');
  const idx = Number(parts.pop());
  const arr = parts.length ? getAtPath(parts.join('.')) : encDraft;
  if (Array.isArray(arr)) arr.splice(idx, 1);
}

function encFieldValue(el) {
  let value = el.type === 'checkbox' ? el.checked : el.value;
  if (el.dataset.num) value = value === '' ? null : parseFloat(value);
  if (el.dataset.list) value = String(value).split(',').map(s => s.trim()).filter(Boolean);
  return value;
}

function renderCondField(path, cond, field) {
  const v = k => cond[k];
  if (field === 'percent') return `<label class="fld"><span>HP % below</span>
    <input type="number" step="0.1" value="${v('percent') ?? ''}" data-path="${path}.percent" data-num="1"></label>`;
  if (field === 'keyword') return `<label class="fld"><span>Keyword (substring match)</span>
    <input value="${esc(v('keyword') ?? '')}" data-path="${path}.keyword"></label>`;
  if (field === 'selector') return `<label class="fld"><span>Selector (names, comma-sep; blank = boss's own names)</span>
    <input value="${esc((v('selector') || []).join(', '))}" data-path="${path}.selector" data-list="1"></label>`;
  if (field === 'timer_id') return `<label class="fld"><span>Timer ID</span>
    <input value="${esc(v('timer_id') ?? '')}" data-path="${path}.timer_id"></label>`;
  if (field === 'phase_id') return `<label class="fld"><span>Phase ID</span>
    <input value="${esc(v('phase_id') ?? '')}" data-path="${path}.phase_id"></label>`;
  if (field === 'phase_ids') return `<label class="fld"><span>Phase IDs (comma-sep)</span>
    <input value="${esc((v('phase_ids') || []).join(', '))}" data-path="${path}.phase_ids" data-list="1"></label>`;
  if (field === 'counter_id') return `<label class="fld"><span>Counter ID</span>
    <input value="${esc(v('counter_id') ?? '')}" data-path="${path}.counter_id"></label>`;
  if (field === 'operator') return `<label class="fld"><span>Operator</span>
    <select data-path="${path}.operator">${['eq', 'ne', 'gte', 'lte', 'gt', 'lt']
      .map(op => `<option value="${op}" ${v('operator') === op ? 'selected' : ''}>${op}</option>`).join('')}</select></label>`;
  if (field === 'value') return `<label class="fld"><span>Value</span>
    <input type="number" step="0.1" value="${v('value') ?? ''}" data-path="${path}.value" data-num="1"></label>`;
  if (field === 'conditions') return `<div class="cond-children">
    ${(v('conditions') || []).map((c, i) => renderCondition(`${path}.conditions.${i}`, c)).join('')}
    <button type="button" class="cond-add-btn" data-action="add-child" data-path="${path}.conditions">+ condition</button></div>`;
  if (field === 'condition') return `<div class="cond-children">${renderCondition(`${path}.condition`, v('condition'))}</div>`;
  return '';
}

function renderCondition(path, cond, label) {
  if (!cond) {
    return `<div class="cond-empty">
      ${label ? `<span class="cond-label">${label}</span>` : ''}
      <button type="button" class="cond-add-btn" data-action="set-cond" data-path="${path}">+ condition</button>
    </div>`;
  }
  const type = cond.type || 'combat_start';
  const fields = COND_FIELDS[type] || [];
  return `<div class="cond-node">
    ${label ? `<span class="cond-label">${label}</span>` : ''}
    <div class="cond-head">
      <select class="cond-type" data-action="set-type" data-path="${path}">
        ${COND_TYPES.map(t => `<option value="${t}" ${t === type ? 'selected' : ''}>${t}</option>`).join('')}
      </select>
      <label class="cond-target"><input type="checkbox" data-action="set-target" data-path="${path}.target"
        ${cond.target === 'local_player' ? 'checked' : ''}> local player only</label>
      <button type="button" class="cond-remove" data-action="remove-cond" data-path="${path}">remove</button>
    </div>
    <div class="cond-fields">${fields.map(f => renderCondField(path, cond, f)).join('')}</div>
  </div>`;
}

function renderPhases() {
  const phases = encDraft.phases || [];
  return phases.map((p, i) => `
    <div class="enc-row">
      <div class="enc-row-head">
        <input placeholder="phase id" value="${esc(p.id ?? '')}" data-path="phases.${i}.id" style="width:130px">
        <input placeholder="Display name" value="${esc(p.name ?? '')}" data-path="phases.${i}.name" style="width:170px">
        <button type="button" class="cond-remove" data-action="remove-phase" data-path="phases.${i}">remove phase</button>
      </div>
      <div class="cond-slot">${renderCondition(`phases.${i}.start_trigger`, p.start_trigger,
        'Start trigger (blank = this is the initial phase)')}</div>
      <div class="cond-slot">
        <span class="cond-label">Extra AND-conditions</span>
        <div class="cond-children">
          ${(p.conditions || []).map((c, j) => renderCondition(`phases.${i}.conditions.${j}`, c)).join('')}
          <button type="button" class="cond-add-btn" data-action="add-child" data-path="phases.${i}.conditions">+ condition</button>
        </div>
      </div>
      <div class="cond-slot">${renderCondition(`phases.${i}.end_trigger`, p.end_trigger, 'End trigger (optional)')}</div>
    </div>`).join('') + `<button type="button" class="cond-add-btn" data-action="add-phase">+ Add Phase</button>`;
}

function renderCounters() {
  const counters = encDraft.counters || [];
  return counters.map((c, i) => `
    <div class="enc-row">
      <div class="enc-row-head">
        <input placeholder="counter id" value="${esc(c.id ?? '')}" data-path="counters.${i}.id" style="width:130px">
        <input placeholder="Display name" value="${esc(c.name ?? '')}" data-path="counters.${i}.name" style="width:170px">
        <label class="fld row"><span>Initial</span>
          <input type="number" step="1" value="${c.initial_value ?? 0}" data-path="counters.${i}.initial_value" data-num="1" style="width:60px"></label>
        <button type="button" class="cond-remove" data-action="remove-counter" data-path="counters.${i}">remove counter</button>
      </div>
      <div class="cond-slot">${renderCondition(`counters.${i}.increment_on`, c.increment_on, 'Increment on')}</div>
      <div class="cond-slot">${renderCondition(`counters.${i}.decrement_on`, c.decrement_on, 'Decrement on (optional)')}</div>
      <div class="cond-slot">${renderCondition(`counters.${i}.reset_on`, c.reset_on, 'Reset on (optional)')}</div>
    </div>`).join('') + `<button type="button" class="cond-add-btn" data-action="add-counter">+ Add Counter</button>`;
}

function renderTimers() {
  const timers = encDraft.timers || [];
  return timers.map((t, i) => `
    <div class="enc-row">
      <div class="enc-row-head">
        <input placeholder="timer id" value="${esc(t.id ?? '')}" data-path="timers.${i}.id" style="width:130px">
        <input placeholder="Label" value="${esc(t.label ?? '')}" data-path="timers.${i}.label" style="width:170px">
        <label class="fld row"><span>Seconds</span>
          <input type="number" step="0.1" value="${t.duration_seconds ?? 10}" data-path="timers.${i}.duration_seconds" data-num="1" style="width:65px"></label>
        <label class="fld row"><span>Warn before</span>
          <input type="number" step="0.1" value="${t.warn_seconds_before ?? 0}" data-path="timers.${i}.warn_seconds_before" data-num="1" style="width:65px"></label>
        <label class="fld row"><input type="checkbox" data-path="timers.${i}.voice_alert" ${t.voice_alert !== false ? 'checked' : ''}><span>Voice</span></label>
        <label class="fld row"><input type="checkbox" data-path="timers.${i}.is_alert" ${t.is_alert ? 'checked' : ''}><span>Alert style (no bar)</span></label>
        <button type="button" class="cond-remove" data-action="remove-timer" data-path="timers.${i}">remove timer</button>
      </div>
      <div class="enc-row-sub">
        <label class="fld"><span>Active in phases (comma-sep ids; blank = all)</span>
          <input value="${esc((t.phases || []).join(', '))}" data-path="timers.${i}.phases" data-list="1"></label>
        <label class="fld row"><span>Repeat every (s, optional)</span>
          <input type="number" step="0.1" value="${t.repeat_interval_seconds ?? ''}" data-path="timers.${i}.repeat_interval_seconds" data-num="1" style="width:65px"></label>
        <label class="fld row"><span>Repeat count (0 = while active)</span>
          <input type="number" step="1" value="${t.repeat_count ?? 0}" data-path="timers.${i}.repeat_count" data-num="1" style="width:60px"></label>
      </div>
      <div class="cond-slot">${renderCondition(`timers.${i}.trigger`, t.trigger, 'Trigger')}</div>
      <div class="cond-slot">
        <span class="cond-label">Extra AND-conditions</span>
        <div class="cond-children">
          ${(t.conditions || []).map((c, j) => renderCondition(`timers.${i}.conditions.${j}`, c)).join('')}
          <button type="button" class="cond-add-btn" data-action="add-child" data-path="timers.${i}.conditions">+ condition</button>
        </div>
      </div>
      <div class="cond-slot">${renderCondition(`timers.${i}.cancel_trigger`, t.cancel_trigger, 'Cancel trigger (optional)')}</div>
    </div>`).join('') + `<button type="button" class="cond-add-btn" data-action="add-timer">+ Add Timer</button>`;
}

function renderEncEditor() {
  const d = encDraft;
  $('#modal-body').innerHTML = `
    <div class="enc-editor">
      <h2>${encSourceId ? `Edit "${esc(d.name || d.id)}"` : 'New Encounter'}</h2>
      <p class="sub" id="enc-error"></p>
      <div class="stack">
        <label class="fld"><span>ID (used as the filename; lowercase, no spaces)</span>
          <input value="${esc(d.id ?? '')}" data-path="id"></label>
        <label class="fld"><span>Display Name</span><input value="${esc(d.name ?? '')}" data-path="name"></label>
        <label class="fld"><span>Boss names (comma-sep, as they appear in the log)</span>
          <input value="${esc((d.boss_names || []).join(', '))}" data-path="boss_names" data-list="1"></label>
        <label class="fld"><span>Boss NPC ids (comma-sep, optional -- preferred over names when known)</span>
          <input value="${esc((d.boss_npc_ids || []).join(', '))}" data-path="boss_npc_ids" data-list="1"></label>
      </div>
      <div class="cond-slot">${renderCondition('encounter_trigger', d.encounter_trigger,
        'Encounter trigger (optional -- alternate recognition path)')}</div>

      <h3>Phases</h3>
      <div id="enc-phases">${renderPhases()}</div>
      <h3>Counters</h3>
      <div id="enc-counters">${renderCounters()}</div>
      <h3>Timers</h3>
      <div id="enc-timers">${renderTimers()}</div>

      <div class="filters" style="margin-top:16px">
        <button id="enc-save">${encSourceId ? 'Save' : 'Create'}</button>
        <button id="enc-cancel" type="button">Cancel</button>
      </div>
    </div>`;
  $('#enc-save').addEventListener('click', saveEncounter);
  $('#enc-cancel').addEventListener('click', closeModal);
}

async function saveEncounter() {
  const result = await post('/api/encounters', encDraft);
  if (result.error) { $('#enc-error').textContent = result.error; return; }
  closeModal();
  loadEncounters();
}

function blankEncounter() {
  return { id: '', name: '', boss_names: [], boss_npc_ids: [], phases: [{ id: 'main', name: 'Main' }], counters: [], timers: [] };
}

async function loadEncounters() {
  const rows = await api('/api/encounters');
  const tbody = $('#encounters-table tbody');
  const empty = $('#encounters-empty');
  if (!Array.isArray(rows) || !rows.length) { tbody.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${esc(r.name)}</td><td>${esc(r.id)}</td>
      <td>${r.phase_count}</td><td>${r.timer_count}</td>
      <td>${r.source === 'user' ? 'Custom' : 'Bundled'}</td>
      <td>
        <button class="btn-link" onclick="editEncounter('${esc(r.id)}')">${r.source === 'user' ? 'Edit' : 'Customize'}</button>
        ${r.source === 'user' ? `<button class="rule-del" onclick="deleteEncounter('${esc(r.id)}')">delete</button>` : ''}
      </td>
    </tr>`).join('');
}

async function editEncounter(id) {
  const data = await api(`/api/encounters/${encodeURIComponent(id)}`);
  if (data.error) return;
  encDraft = data;
  encSourceId = id;
  renderEncEditor();
  $('#modal').classList.add('open');
}
window.editEncounter = editEncounter;

async function deleteEncounter(id) {
  if (!confirm(`Delete your custom "${id}"? (If it customized a bundled fight, the bundled version comes back.)`)) return;
  await post('/api/encounters/delete', { id });
  loadEncounters();
}
window.deleteEncounter = deleteEncounter;

$('#enc-new').addEventListener('click', () => {
  encDraft = blankEncounter();
  encSourceId = null;
  renderEncEditor();
  $('#modal').classList.add('open');
});

$('#modal-body').addEventListener('input', e => {
  const el = e.target;
  if (!encDraft || el.dataset.action) return;
  const path = el.dataset.path;
  if (!path) return;
  setAtPath(path, encFieldValue(el));
});
$('#modal-body').addEventListener('change', e => {
  const el = e.target;
  if (!encDraft) return;
  if (el.dataset.action === 'set-type') { setAtPath(el.dataset.path + '.type', el.value); renderEncEditor(); return; }
  if (el.dataset.action === 'set-target') { setAtPath(el.dataset.path, el.checked ? 'local_player' : null); return; }
  if (el.dataset.action) return;
  const path = el.dataset.path;
  if (path) setAtPath(path, encFieldValue(el));
});
$('#modal-body').addEventListener('click', e => {
  const btn = e.target.closest('[data-action]');
  if (!btn || !encDraft) return;
  const action = btn.dataset.action;
  const path = btn.dataset.path;
  if (action === 'remove-cond') setAtPath(path, null);
  else if (action === 'set-cond') setAtPath(path, { type: 'combat_start' });
  else if (action === 'add-child') { const arr = getAtPath(path) || []; arr.push({ type: 'combat_start' }); setAtPath(path, arr); }
  else if (action === 'add-phase') { if (!encDraft.phases) encDraft.phases = []; encDraft.phases.push({ id: '', name: '' }); }
  else if (action === 'remove-phase') removeAtPath(path);
  else if (action === 'add-counter') { if (!encDraft.counters) encDraft.counters = []; encDraft.counters.push({ id: '', name: '', initial_value: 0 }); }
  else if (action === 'remove-counter') removeAtPath(path);
  else if (action === 'add-timer') { if (!encDraft.timers) encDraft.timers = []; encDraft.timers.push({ id: '', label: '', duration_seconds: 10 }); }
  else if (action === 'remove-timer') removeAtPath(path);
  else return;
  renderEncEditor();
});

// ------------------------------------------------------------- tab poll
setInterval(refreshActiveTab, TAB_POLL_MS);

pollLive();
