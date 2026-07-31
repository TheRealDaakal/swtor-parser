'use strict';
/* DPS corpus-analytics frontend — no dependencies, no CDN, works fully offline.
   Charts are hand-rolled SVG built to the dataviz mark specs:
   2px lines, r>=4 markers with a 2px surface ring, 10% area wash,
   solid hairline gridlines, crosshair tooltip, and a table-view twin so no
   value is ever gated behind a hover. */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmt = n => n == null ? '—' : Math.round(n).toLocaleString();
// Stat-tile contract: 1,284 / 12.9K / $4.2M -- so anything under 10K keeps
// its exact digits. Compacting 1,983 to "2K" throws away real precision.
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
const durLong = s => {
  if (s == null) return '—';
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
};
const api = (p, o) => fetch(p, o).then(r => r.json());

const S = { ov: null, pulls: [], trend: [], dfPulls: [], trendView: 'chart' };

/* ------------------------------------------------------------------ tabs
   Views are hash-routed so a particular tab is linkable and survives a
   reload -- otherwise every refresh dumps you back on Overview. */
const VIEWS = ['overview', 'trends', 'pulls', 'deaths'];

function activate(name, push = true) {
  if (!VIEWS.includes(name)) name = 'overview';
  $$('.tab').forEach(x => x.classList.toggle('active', x.dataset.view === name));
  $$('.view').forEach(x => x.classList.toggle('active', x.id === 'view-' + name));
  if (push && location.hash.slice(1) !== name) location.hash = name;
  if (name === 'trends') loadTrend();
  if (name === 'pulls') loadPulls();
  // Landing on an empty panel with a button is a dead end -- if a pull is
  // already selected, just show the analysis.
  if (name === 'deaths' && !S.deathsRun && $('#df-pull').value !== '') runForensics();
}
$$('.tab').forEach(t => {
  t.onclick = () => activate(t.dataset.view);
  t.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); t.click(); } };
});
window.addEventListener('hashchange', () => activate(location.hash.slice(1), false));

/* ---------------------------------------------------------------- status */
async function poll() {
  const s = await api('/api/status');
  const el = $('#status');
  if (s.building) {
    const p = s.progress;
    el.textContent = `scanning ${p.done}/${p.total}`;
    $('#rebuild').disabled = true;
    return setTimeout(poll, 600);
  }
  $('#rebuild').disabled = false;
  el.textContent = s.built ? `${s.sessions} sessions` : 'no index';
  if (!S.ov && s.built) boot();
}
$('#rebuild').onclick = async () => {
  await api('/api/rebuild', { method: 'POST' });
  S.ov = null; setTimeout(poll, 250);
};

/* -------------------------------------------------------------- overview */
async function boot() {
  const ov = await api('/api/overview');
  S.ov = ov;

  $('#hero-value').textContent = fmt(ov.boss_encounters);
  $('#hero-note').textContent =
    `across ${ov.sessions} sessions · ${ov.first_date} → ${ov.last_date}`;

  $('#ov-tiles').innerHTML = [
    ['Sessions', fmt(ov.sessions), ''],
    ['All encounters', compact(ov.encounters), 'boss pulls plus trash'],
    ['Distinct bosses', fmt(ov.distinct_bosses), ''],
    ['Characters', compact(ov.player_count ?? ov.players.length), 'players seen'],
  ].map(([l, v, d]) => `<div class="tile"><div class="label">${l}</div>
      <div class="value">${v}</div>${d ? `<div class="delta">${d}</div>` : ''}</div>`).join('');

  $('#ov-bosses tbody').innerHTML = ov.bosses.map(b => {
    const dp = b.deaths / Math.max(b.pulls, 1);
    return `<tr class="clickable" data-boss="${esc(b.boss_id)}">
      <td class="name">${esc(b.boss)}</td>
      <td>${fmt(b.pulls)}</td>
      <td class="dim">${durLong(b.total_seconds)}</td>
      <td class="dim">${dur(b.median)}</td>
      <td class="dim">${dur(b.longest)}</td>
      <td class="${dp >= 10 ? 'critical' : dp >= 5 ? 'warning' : 'dim'}">${dp.toFixed(1)}</td>
      <td class="dim">${esc(b.first_seen)}</td>
      <td class="dim">${esc(b.last_seen)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="8" class="empty">no boss encounters found</td></tr>';

  $$('#ov-bosses tbody tr.clickable').forEach(tr => tr.onclick = () => {
    $('#pl-boss').value = tr.dataset.boss;
    activate('pulls');
  });

  $('#ov-players tbody').innerHTML = ov.players.slice(0, 20).map(p => `
    <tr><td class="name">${esc(p.name)}</td>
      <td><span class="pill">${esc(p.role || '')}</span></td>
      <td>${fmt(p.encounters)}</td>
      <td class="dim">${compact(p.damage)}</td>
      <td class="dim">${compact(p.healing)}</td>
      <td class="${p.deaths > 300 ? 'warning' : 'dim'}">${fmt(p.deaths)}</td></tr>`).join('');

  const bossOpts = '<option value="">All bosses</option>' +
    ov.bosses.map(b => `<option value="${esc(b.boss_id)}">${esc(b.boss)} · ${b.pulls}</option>`).join('');
  $('#tr-boss').innerHTML = bossOpts;
  $('#pl-boss').innerHTML = bossOpts;
  $('#df-boss').innerHTML = bossOpts;
  // Trends defaults to ONE boss. "All bosses" plots HPS on Styrak next to
  // HPS on a 45s trash boss as if they were the same measure -- the line is
  // then noise, not a trend. Comparing a metric across time only means
  // something when the encounter is held constant.
  if (ov.bosses.length) $('#tr-boss').value = ov.bosses[0].boss_id;
  $('#tr-player').innerHTML = ov.players.slice(0, 30)
    .map(p => `<option>${esc(p.name)}</option>`).join('');
  // Populate the death-pull list BEFORE routing: activating #deaths only
  // auto-runs if a pull is already selected, and this is async.
  await loadDeathPulls();

  // honour a deep link once data exists to render into
  activate(location.hash.slice(1) || 'overview', false);
}

/* ----------------------------------------------------------------- chart
   Two series when the run is long: the raw per-pull value (de-emphasised)
   and a rolling mean that carries the actual story. Both on ONE axis, same
   unit -- never a second y-scale.

   Markers are dropped past ~45 points: at r>=4 with a 2px ring, 120+ dots
   merge into a solid band and stop being marks at all. Deaths and the peak
   stay marked because those are the points worth finding. */
const ROLL = 5;

function rollingMean(vals, w) {
  return vals.map((_, i) => {
    const lo = Math.max(0, i - Math.floor(w / 2)), hi = Math.min(vals.length, lo + w);
    const s = vals.slice(lo, hi);
    return s.reduce((a, b) => a + b, 0) / s.length;
  });
}

function lineChart(box, pts, unit) {
  if (!pts.length) { box.innerHTML = '<div class="empty">No pulls match this selection.</div>'; return null; }

  const dense = pts.length > 45;
  // Size the viewBox to the container so 1 SVG unit == 1 CSS pixel. A fixed
  // W scaled by `width:100%` shrinks the whole drawing -- in a narrow pane a
  // 1460x300 viewBox collapsed to an 88px-tall sliver with unreadable text.
  // The bottom padding is part of H, so the x-axis band is always inside the
  // box and never causes a nested scrollbar.
  const H = 300, pad = { l: 66, r: 22, t: 16, b: 42 };
  const W = Math.max(560, Math.round(box.clientWidth || 900));
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const vals = pts.map(p => p.value);

  // Round the axis top to a clean number so ticks read 0 / 20K / 40K rather
  // than 0 / 17.4K / 34.9K.
  const niceTop = raw => {
    if (raw <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    return Math.ceil(raw / (mag / 2)) * (mag / 2);
  };
  const hi = niceTop(Math.max(...vals) * 1.08);
  const X = i => pad.l + (pts.length === 1 ? iw / 2 : (i / (pts.length - 1)) * iw);
  const Y = v => pad.t + ih - (v / hi) * ih;

  // gridlines: solid hairlines, clean tick values
  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const v = hi * i / 4, y = Y(v);
    grid += `<line class="gridline" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}"/>
             <text x="${pad.l - 10}" y="${y + 3.5}" text-anchor="end">${compact(v)}</text>`;
  }

  // least-squares fit — dashed because it IS a derived projection, not data
  const n = pts.length, sx = (n - 1) * n / 2, sy = vals.reduce((a, b) => a + b, 0);
  const sxy = vals.reduce((a, v, i) => a + i * v, 0);
  const sxx = pts.reduce((a, _, i) => a + i * i, 0);
  const den = n * sxx - sx * sx;
  const slope = den ? (n * sxy - sx * sy) / den : 0;
  const b0 = (sy - slope * sx) / n;
  const trend = n > 2 ? `<line x1="${X(0)}" y1="${Y(Math.max(b0, 0))}"
      x2="${X(n - 1)}" y2="${Y(Math.max(b0 + slope * (n - 1), 0))}"
      stroke="var(--ink-muted)" stroke-width="1.5" stroke-dasharray="5 5" opacity=".8"/>` : '';

  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(p.value).toFixed(1)}`).join('');
  const area = `${line}L${X(n - 1).toFixed(1)},${Y(0)}L${X(0).toFixed(1)},${Y(0)}Z`;

  // rolling mean — the readable signal once the raw run gets noisy
  const roll = dense ? rollingMean(vals, ROLL) : null;
  const rollPath = roll
    ? roll.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join('')
    : '';

  // Markers: r>=4 with a 2px surface ring. When dense, only the pulls that
  // matter get one — otherwise they stop reading as discrete marks.
  const dots = pts.map((p, i) => {
    if (dense && !p.deaths) return '';
    const c = p.deaths ? 'var(--critical)' : 'var(--s1)';
    return `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.value).toFixed(1)}" r="4"
      fill="${c}" stroke="var(--surface)" stroke-width="2"/>`;
  }).join('');

  // One tick per distinct raid night, placed at that night's first pull.
  // Stepping by index instead repeats the same date several times over,
  // because a single night contributes 20+ consecutive pulls.
  let ticks = '';
  const firstIdxByDate = new Map();
  pts.forEach((p, i) => { if (p.date && !firstIdxByDate.has(p.date)) firstIdxByDate.set(p.date, i); });
  const dates = [...firstIdxByDate.entries()];
  const dstep = Math.max(1, Math.ceil(dates.length / 10));
  dates.filter((_, k) => k % dstep === 0).forEach(([d, i]) => {
    ticks += `<text x="${X(i)}" y="${H - 16}" text-anchor="middle">${esc(d)}</text>`;
  });

  // label only the extreme — never a number on every point
  const maxI = vals.indexOf(Math.max(...vals));
  const peak = `<text x="${X(maxI)}" y="${Y(vals[maxI]) - 12}" text-anchor="middle"
      fill="var(--ink)" style="font-weight:600">${compact(vals[maxI])}</text>`;

  box.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
         aria-label="${esc(unit)} per pull over time">
      ${grid}${ticks}
      <path d="${area}" fill="var(--s1)" opacity=".10"/>
      <path d="${line}" fill="none" stroke="${dense ? 'var(--ink-muted)' : 'var(--s1)'}"
            stroke-width="2" stroke-linejoin="round" stroke-linecap="round"
            opacity="${dense ? '.42' : '1'}"/>
      ${rollPath ? `<path d="${rollPath}" fill="none" stroke="var(--s1)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>` : ''}
      ${trend}${dots}${peak}
      <line class="axis" x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${pad.t + ih}"/>
      <line class="axis" x1="${pad.l}" y1="${pad.t + ih}" x2="${W - pad.r}" y2="${pad.t + ih}"/>
      <line id="cross" y1="${pad.t}" y2="${pad.t + ih}" stroke="var(--ink-muted)"
            stroke-width="1" opacity="0"/>
      <rect id="hit" x="${pad.l}" y="${pad.t}" width="${iw}" height="${ih}" fill="transparent"/>
    </svg>
    <div class="tooltip" id="tip"></div>`;

  // crosshair + tooltip: hit area spans the whole plot, so no pinpoint targets
  const svg = box.querySelector('svg'), tip = box.querySelector('#tip'),
        cross = box.querySelector('#cross'), hit = box.querySelector('#hit');
  const show = e => {
    const r = svg.getBoundingClientRect();
    const sx2 = (e.clientX - r.left) / r.width * W;
    let i = pts.length === 1 ? 0 : Math.round((sx2 - pad.l) / iw * (pts.length - 1));
    i = Math.max(0, Math.min(pts.length - 1, i));
    const p = pts[i];
    cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i));
    cross.setAttribute('opacity', '.35');
    tip.classList.add('show');
    tip.style.left = (X(i) / W * r.width) + 'px';
    tip.style.top = (Y(p.value) / H * r.height - 12) + 'px';
    tip.innerHTML = `<div class="t-title">${fmt(p.value)} ${esc(unit)}</div>
      <div class="t-row">${esc(p.date)} · ${esc(p.boss || '')}</div>
      <div class="t-row">pull length <b>${dur(p.duration)}</b></div>
      ${p.deaths ? `<div class="t-row" style="color:var(--critical)">died ${p.deaths}×</div>` : ''}`;
  };
  hit.addEventListener('mousemove', show);
  hit.addEventListener('mouseleave', () => {
    tip.classList.remove('show'); cross.setAttribute('opacity', '0');
  });
  return { slope, first: b0, last: b0 + slope * (n - 1) };
}

/* -------------------------------------------------------------- timeline
   StarParse has this ("adjustable timeline intervals ... recalculation of
   all personal values"), neither BARAS nor ORBS does. Total raid damage as
   the area (the only series that's always meaningful regardless of comp),
   an optional single highlighted player line on top -- more than that
   would need a categorical palette past the validated 3-colour cap this
   design system caps at everywhere else. Drag across it to select a
   range; the summary table below recalculates from the SAME bucket
   arrays already in memory, no second network round-trip. */
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
      // a plain click, not a drag -- treat as "clear selection"
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

/* ---------------------------------------------------------------- trends */
const METRIC = { dps: 'DPS', hps: 'HPS', dtps: 'DTPS', deaths: 'deaths' };

async function loadTrend() {
  const player = $('#tr-player').value, boss = $('#tr-boss').value, metric = $('#tr-metric').value;
  if (!player) return;
  const box = $('#tr-chartbox');
  box.classList.add('stale');                       // hold render, no skeleton flash
  const q = new URLSearchParams({ player, metric });
  if (boss) q.set('boss', boss);
  const pts = await api('/api/trend?' + q);
  S.trend = pts;
  box.classList.remove('stale');

  const unit = METRIC[metric];
  const stat = lineChart(box, pts, unit);

  const dense = pts.length > 45;
  $('#tr-sub').textContent = pts.length
    ? `${pts.length} pulls, oldest to newest.`
    : 'One point per pull, oldest to newest.';

  // Raw stays neutral and the smoothed line carries the accent: putting the
  // smoothed line in slot-2 orange sat it right next to the red death
  // markers, a pair the palette notes are already close.
  $('#tr-legend').innerHTML = pts.length ? `
    <span><i style="background:${dense ? 'var(--ink-muted)' : 'var(--s1)'}"></i>${dense ? 'each pull' : 'pull'}</span>
    ${dense ? `<span><i style="background:var(--s1)"></i>${ROLL}-pull average</span>` : ''}
    <span><i style="background:var(--critical)"></i>died during this pull</span>
    <span style="color:var(--ink-muted)">— — overall trend</span>` : '';

  if (pts.length > 2 && stat && Math.abs(stat.first) > 0.01) {
    const ch = ((stat.last - stat.first) / Math.abs(stat.first)) * 100;
    const better = metric === 'dtps' || metric === 'deaths' ? ch < 0 : ch > 0;
    $('#tr-sub').innerHTML += ` Trend <b class="${better ? 'good' : 'critical'}">`
      + `${ch >= 0 ? '+' : ''}${ch.toFixed(0)}%</b> across the period.`;
  }

  // table-view twin — every value reachable without hovering
  $('#tr-tablebox').innerHTML = `<table><thead><tr>
      <th>Date</th><th>Boss</th><th>${esc(unit)}</th><th>Duration</th><th>Deaths</th>
    </tr></thead><tbody>${pts.map(p => `<tr>
      <td>${esc(p.date)}</td><td class="dim">${esc(p.boss || '')}</td>
      <td>${fmt(p.value)}</td><td class="dim">${dur(p.duration)}</td>
      <td class="${p.deaths ? 'critical' : 'dim'}">${p.deaths}</td></tr>`).join('')}
    </tbody></table>`;

  const by = {};
  pts.forEach(p => (by[p.date] = by[p.date] || []).push(p));
  $('#tr-nights tbody').innerHTML = Object.keys(by).sort().map(d => {
    const g = by[d], v = g.map(x => x.value), dt = g.reduce((a, b) => a + b.deaths, 0);
    return `<tr><td>${esc(d)}</td><td class="dim">${g.length}</td>
      <td>${fmt(v.reduce((a, b) => a + b, 0) / g.length)}</td>
      <td class="good">${fmt(Math.max(...v))}</td>
      <td class="dim">${dur(g.reduce((a, b) => a + b.duration, 0) / g.length)}</td>
      <td class="${dt ? 'warning' : 'dim'}">${dt}</td></tr>`;
  }).join('') || '<tr><td colspan="6" class="empty">no data</td></tr>';
}
['#tr-player', '#tr-boss', '#tr-metric'].forEach(s => $(s).addEventListener('change', loadTrend));

$('#tr-vchart').onclick = () => setTrendView('chart');
$('#tr-vtable').onclick = () => setTrendView('table');
function setTrendView(v) {
  S.trendView = v;
  $('#tr-vchart').classList.toggle('on', v === 'chart');
  $('#tr-vtable').classList.toggle('on', v === 'table');
  $('#tr-chartbox').style.display = v === 'chart' ? '' : 'none';
  $('#tr-tablebox').style.display = v === 'table' ? '' : 'none';
  $('#tr-legend').style.display = v === 'chart' ? '' : 'none';
}

/* ----------------------------------------------------------------- pulls */
async function loadPulls() {
  const boss = $('#pl-boss').value;
  const rows = await api('/api/pulls' + (boss ? '?boss=' + encodeURIComponent(boss) : ''));
  S.pulls = rows;
  $('#pl-table tbody').innerHTML = rows.slice(0, 400).map((r, i) => `
    <tr class="clickable" data-i="${i}">
      <td>${esc(r.date)}</td><td class="dim">${esc(r.time || '')}</td>
      <td class="name">${esc(r.boss)}</td>
      <td>${dur(r.duration)}</td>
      <td class="${r.deaths ? 'critical' : 'dim'}">${r.deaths || 0}</td>
      <td class="dim">${r.players.length}</td>
      <td>${(r.phases || []).map(p => `<span class="pill">${esc(p)}</span>`).join('') || '<span class="dim">—</span>'}</td>
    </tr>`).join('') || '<tr><td colspan="7" class="empty">no pulls</td></tr>';
  $$('#pl-table tbody tr.clickable').forEach(tr =>
    tr.onclick = () => showPull(S.pulls[+tr.dataset.i]));
}
$('#pl-boss').addEventListener('change', loadPulls);

async function showPull(r) {
  const mx = Math.max(...r.players.map(p => p.damage), 1);
  const mh = Math.max(...r.players.map(p => p.healing), 1);
  $('#m-title').textContent = r.boss;
  $('#m-sub').innerHTML = `${esc(r.date)} ${esc(r.time || '')} · ${dur(r.duration)} · `
    + `${r.deaths || 0} deaths ${(r.phases || []).map(p => `<span class="pill">${esc(p)}</span>`).join('')}`;
  $('#m-body').innerHTML = `<div class="tw"><table>
    <thead><tr><th>Player</th><th>DPS</th><th></th><th>HPS</th><th></th>
      <th>Damage</th><th>Healing</th><th>Taken</th><th>Mitigated</th><th>Deaths</th></tr></thead>
    <tbody>${r.players.map(p => `<tr>
      <td class="name">${esc(p.name)}</td>
      <td>${fmt(p.damage / Math.max(r.duration, 1))}</td>
      <td style="width:88px"><div class="meter"><i style="width:${(p.damage / mx * 100).toFixed(0)}%"></i></div></td>
      <td>${fmt(p.healing / Math.max(r.duration, 1))}</td>
      <td style="width:88px"><div class="meter heal"><i style="width:${(p.healing / mh * 100).toFixed(0)}%"></i></div></td>
      <td class="dim">${compact(p.damage)}</td>
      <td class="dim">${compact(p.healing)}</td>
      <td class="dim">${compact(p.taken)}</td>
      <td class="${p.mitigated_pct >= 30 ? 'accent' : 'dim'}"
          title="${compact(p.absorbed)} absorbed of ${compact(p.taken + p.absorbed)} raw incoming"
          >${(p.taken || p.absorbed) ? p.mitigated_pct.toFixed(0) + '%' : '—'}</td>
      <td class="${p.deaths ? 'critical' : 'dim'}">${p.deaths}</td></tr>`).join('')}
    </tbody></table></div>
    <div class="panel" id="m-timeline-panel" style="margin-top:14px">
      <h2>Timeline<span class="sub" style="margin-left:8px;font-weight:400">
        drag across the chart to select a range and recalculate stats for just that slice</span></h2>
      <div id="m-timeline-box" class="chart-wrap" style="min-height:220px"><div class="loading">Re-reading the log…</div></div>
      <div id="m-timeline-summary"></div>
    </div>`;
  $('#modal').classList.add('open');

  const q = new URLSearchParams({ file: r.file, start_line: r.start_line || 0, end_line: r.end_line || 0 });
  let tl;
  try {
    tl = await api('/api/timeline?' + q);
  } catch {
    tl = null;
  }
  const box = $('#m-timeline-box');
  if (!tl || tl.error || !Object.keys(tl.players).length) {
    box.innerHTML = '<div class="empty">No timeline data for this pull.</div>';
    return;
  }
  timelineChart(box, $('#m-timeline-summary'), tl, r.duration);
}
window.closeModal = () => $('#modal').classList.remove('open');
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

/* -------------------------------------------------------------- forensics */
async function loadDeathPulls() {
  const boss = $('#df-boss').value;
  const rows = await api('/api/pulls' + (boss ? '?boss=' + encodeURIComponent(boss) : ''));
  S.dfPulls = rows.filter(r => r.deaths > 0).slice(0, 200);
  $('#df-pull').innerHTML = S.dfPulls.map((r, i) =>
    `<option value="${i}">${esc(r.date)} · ${esc(r.boss)} · ${dur(r.duration)} · ${r.deaths} deaths</option>`
  ).join('') || '<option value="">no pulls with deaths</option>';
  S.deathsRun = false;
}
$('#df-boss').addEventListener('change', async () => {
  await loadDeathPulls();
  if ($('#view-deaths').classList.contains('active')) runForensics();
});

async function runForensics() {
  const i = $('#df-pull').value;
  if (i === '' || !S.dfPulls.length) return;
  S.deathsRun = true;
  const r = S.dfPulls[+i];
  $('#df-out').innerHTML = '<div class="panel"><div class="loading">Re-reading the log…</div></div>';
  const q = new URLSearchParams({ file: r.file, start_line: r.start_line || 0, end_line: r.end_line || 0 });
  const res = await api('/api/deaths?' + q);
  if (res.error) {
    $('#df-out').innerHTML = `<div class="panel"><div class="empty critical">${esc(res.error)}</div></div>`;
    return;
  }
  renderDeaths(res, r);
}
$('#df-run').onclick = runForensics;
$('#df-pull').addEventListener('change', runForensics);

function renderDeaths(res, pull) {
  if (!res.reports.length) {
    $('#df-out').innerHTML = '<div class="panel"><div class="empty">No player deaths in this pull.</div></div>';
    return;
  }
  const s = res.summary;
  const top = s.killing_abilities[0];
  const wipe = res.reports.length >= 5 &&
    (Math.max(...res.reports.map(r => r.death_time)) - Math.min(...res.reports.map(r => r.death_time))) < 15;

  let html = `<div class="hero">
      <div class="label">Player deaths in this pull</div>
      <div class="value">${s.total_deaths}</div>
      <div class="note">${esc(pull.boss)} · ${esc(pull.date)} · ${dur(pull.duration)}</div>
    </div>`;

  if (wipe && top) html += `<div class="panel"><div class="callout crit">
      <span class="ico">!</span><div><b>Looks like a wipe.</b>
      ${res.reports.length} players died within
      ${(Math.max(...res.reports.map(r => r.death_time)) - Math.min(...res.reports.map(r => r.death_time))).toFixed(1)}s
      of each other, most to <b>${esc(top.ability)}</b>.</div></div></div>`;

  html += `<div class="panel"><h2>Killing blows</h2>
    <p class="sub">The ability that landed the final hit, counted across every death in this pull.</p>
    <div class="tw"><table><thead><tr><th>Ability</th><th>Kills</th></tr></thead>
    <tbody>${s.killing_abilities.map(a =>
      `<tr><td class="name">${esc(a.ability)}</td><td class="critical">${a.kills}</td></tr>`).join('')}
    </tbody></table></div></div>`;

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

  $('#df-out').innerHTML = html;
}

/* The chart is drawn at the container's pixel width, so a resize needs a
   redraw rather than a CSS rescale. Debounced. */
let resizeT;
window.addEventListener('resize', () => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => {
    if ($('#view-trends').classList.contains('active') && S.trend.length) {
      lineChart($('#tr-chartbox'), S.trend, METRIC[$('#tr-metric').value]);
    }
  }, 180);
});

poll();
