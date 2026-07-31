'use strict';
/* DPS live-meter frontend -- no dependencies, no CDN, works fully offline.
   Polls /api/live on the same cadence the old Tk "Live" tab refreshed at
   (500ms) and re-renders in place. */

const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => n == null ? '—' : Math.round(n).toLocaleString();

const POLL_MS = 500;

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
  const box = $('#alerts');
  box.innerHTML = alerts.map(a => `<div class="alert-banner">${esc(a.toUpperCase())}</div>`).join('');
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
  if (!rows.length) {
    box.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  box.innerHTML = rows.map(r => timerRow(r.label, r.remaining, r.total)).join('');
}

function renderDotsHots(rows) {
  const box = $('#dotshots');
  const empty = $('#dotshots-empty');
  if (!rows.length) {
    box.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  box.innerHTML = rows.map(r => timerRow(`${r.tag}  ${r.label}`, r.remaining, r.total)).join('');
}

function renderTaunts(taunts) {
  const box = $('#taunts');
  const empty = $('#taunts-empty');
  if (!taunts.length) {
    box.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  box.innerHTML = taunts.map(t => `
    <div class="timer-row">
      <div class="lbl" style="flex:1;color:${t.hit ? 'var(--good)' : 'var(--critical)'}">
        ${t.hit ? '✓' : '✗'} ${esc(t.text)}
      </div>
      <div class="rem">${t.ago.toFixed(1)}s ago</div>
    </div>`).join('');
}

async function poll() {
  try {
    const snap = await fetch('/api/live', { cache: 'no-store' }).then(r => r.json());
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
    setTimeout(poll, POLL_MS);
  }
}

poll();
