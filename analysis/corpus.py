"""
analysis/corpus.py

Indexes the ENTIRE CombatLogs folder into one queryable dataset -- the thing
a per-session parser structurally can't offer. BARAS's Data Explorer shows
you the session you just ran; this shows you every session you've ever run.

Design notes:

- Encounter boundaries come from the log's OWN timestamps, not wall clock.
  StatsTracker's live path is correct for tailing (real gaps between lines
  arrive in real time) but replaying a finished file takes milliseconds, so
  wall-clock rollover would lump an entire raid night into one "encounter".

- The index stores SUMMARIES plus a pointer back to (file, line range). It
  deliberately does NOT store every event: the corpus here is 1.4GB, and
  re-parsing one encounter on demand is fast. Death forensics uses that
  pointer to re-read just the window it needs (see forensics.py).

- Results are cached under the app data dir, keyed by each file's
  (size, mtime). Re-running only re-parses files that actually changed, so
  the first scan costs minutes and every later one costs ~nothing. This is
  what makes a responsive UI over 200+ files possible.
"""

import glob
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import storage
from boss_definitions import load_definitions
from boss_intelligence import BossEncounterState
from log_merger import LogClock
from log_parser import parse_line
from stats import Encounter, MIN_ENCOUNTER_SECONDS, NEW_PULL_MIN_GAP_SECONDS

CACHE_VERSION = 7  # bump to invalidate every cached entry after a parser change
MIN_FILE_BYTES = 50_000         # skip near-empty logs (login blips)

# NEW_PULL_MIN_GAP_SECONDS / MIN_ENCOUNTER_SECONDS now live in stats.py --
# the live tracker (StatsTracker.feed) needs the exact same EnterCombat-
# based pull-splitting this replay path uses, not a second copy that can
# drift out of sync (it already had: offline used this signal, live never
# did, and the same log split into 93 pulls offline vs 75 live).

_DATE_RE = re.compile(r"combat_(\d{4})-(\d{2})-(\d{2})_(\d{2})_(\d{2})_(\d{2})")

BUNDLED_BOSS_DIR = Path(__file__).resolve().parent.parent / "boss_definitions_bundled"


def default_log_dir() -> Optional[str]:
    import log_watcher
    return log_watcher.find_log_dir()


def _cache_path() -> Path:
    return Path(storage.data_dir()) / "corpus_index.json"


def _file_key(path: str) -> str:
    st = os.stat(path)
    return f"{int(st.st_size)}:{int(st.st_mtime)}"


def session_datetime(basename: str):
    """(date, time) parsed out of SWTOR's own log filename, or (None, None)."""
    m = _DATE_RE.search(basename)
    if not m:
        return None, None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}", f"{h}:{mi}:{s}"


def replay_pulls(path: str, definitions) -> List[dict]:
    """Replays one log file and splits it into real per-pull Encounter
    objects (see NEW_PULL_MIN_GAP_SECONDS/MIN_ENCOUNTER_SECONDS above for
    why this, and not StatsTracker's live wall-clock rollover, is what
    replay needs). Returns one dict per pull:
        {"encounter": Encounter, "boss_name": str|None, "boss_id": str|None,
         "phases": [str, ...], "first_ts": str|None}
    Used both by scan_file() (which reduces these to corpus summary rows)
    and by anything that wants the full Encounter back -- e.g. importing
    an old session log into history exactly as if it had been captured
    live."""
    boss_state = BossEncounterState(definitions)
    current = Encounter()
    boss_name = None
    boss_id = None
    phases: List[str] = []
    first_ts = None
    out: List[dict] = []

    # Real calendar anchor for every pull in this file, parsed from SWTOR's
    # own log filename (e.g. "combat_2026-08-03_20_15_30_....txt") -- None
    # if the filename doesn't match (an old/renamed file), in which case
    # pulls from it just get no real_start_time (see Encounter.apply).
    # LogClock's `now` below is seconds-since-midnight-of-this-file's-date
    # (extended across rollovers), so midnight_epoch + now reconstructs an
    # absolute local Unix epoch for every event, not just the first.
    date_str, _time_str = session_datetime(os.path.basename(path))
    midnight_epoch = (
        time.mktime(datetime.strptime(date_str, "%Y-%m-%d").timetuple())
        if date_str else None
    )

    def flush():
        if not current.players or current.duration() < MIN_ENCOUNTER_SECONDS:
            return
        # Same gate the live tracker applies -- see Encounter.is_real_fight().
        if not current.is_real_fight():
            return
        out.append({
            "encounter": current,
            "boss_name": boss_name,
            "boss_id": boss_id,
            "phases": phases[:],
            "first_ts": first_ts,
        })

    last_enter_combat = None
    # Monotonic across midnight and mid-session clock adjustments -- raw
    # seconds-of-day goes backwards at both, which silently deletes any pull
    # spanning the jump. See LogClock.
    clock = LogClock()

    with open(path, "r", encoding="cp1252", errors="replace") as f:
        for i, line in enumerate(f, 1):
            event = parse_line(line, line_number=i)
            if event is None:
                continue
            now = clock(event.timestamp or "")

            # A fresh EnterCombat means a new pull (see NEW_PULL_MIN_GAP_SECONDS).
            new_pull = False
            if event.is_combat_start:
                if (last_enter_combat is not None
                        and (now - last_enter_combat) >= NEW_PULL_MIN_GAP_SECONDS
                        and current.players):
                    new_pull = True
                last_enter_combat = now

            if new_pull or (current.players and current.past_trailing_window(now)):
                flush()
                current = Encounter()
                current.log_path = path
                boss_name = boss_id = None
                phases = []
                first_ts = None
                boss_state.reset()
            if first_ts is None:
                first_ts = event.timestamp
            real_time = midnight_epoch + now if midnight_epoch is not None else None
            current.apply(event, at_time=now, real_time=real_time)
            change = boss_state.feed(event)
            if boss_state.active_boss is not None and boss_name is None:
                boss_name = boss_state.active_boss.name
                boss_id = boss_state.active_boss.id
            if change is not None and change.phase_name not in phases:
                phases.append(change.phase_name)
    flush()
    return out


def scan_file(path: str, definitions) -> List[dict]:
    """Replays one log file and returns a summary per encounter."""
    out: List[dict] = []
    for pull in replay_pulls(path, definitions):
        current = pull["encounter"]
        players, npcs = [], []
        for p in current.players.values():
            dmg_rows, heal_rows = p.ability_breakdown()
            row = {
                "name": p.name,
                "damage": round(p.damage_done),
                "healing": round(p.healing_done),
                "taken": round(p.damage_taken),
                "absorbed": round(p.damage_absorbed),
                "mitigated_pct": round(p.mitigation_pct(), 1),
                "deaths": p.deaths,
                "top_damage": [[a, round(v)] for a, v in dmg_rows[:8]],
                "top_healing": [[a, round(v)] for a, v in heal_rows[:8]],
            }
            (players if p.is_player else npcs).append(row)
        players.sort(key=lambda r: -r["damage"])
        npcs.sort(key=lambda r: -r["damage"])
        out.append({
            "boss": pull["boss_name"],
            "boss_id": pull["boss_id"],
            "phases": pull["phases"],
            "duration": round(current.duration(), 1),
            "start_ts": pull["first_ts"],
            "start_line": current.start_line,
            "end_line": current.end_line,
            "players": players,
            # NPC rows are kept but summarised only -- useful for "how much
            # add damage went out" without bloating the index.
            "npc_count": len(npcs),
            "npc_damage": round(sum(n["damage"] for n in npcs)),
            # Deaths that matter for raid analysis are PLAYER deaths. NPC
            # deaths (add spawns dying) are counted separately, never mixed in.
            "deaths": sum(p["deaths"] for p in players),
            "npc_deaths": sum(n["deaths"] for n in npcs),
        })
    return out


def build_index(log_dir: Optional[str] = None, progress=None, force: bool = False) -> dict:
    """Scans every log file, reusing cached results for unchanged files.
    `progress` is an optional callable(done, total, filename)."""
    log_dir = log_dir or default_log_dir()
    if not log_dir:
        return {"version": CACHE_VERSION, "log_dir": None, "sessions": []}

    cache = {}
    cpath = _cache_path()
    if cpath.exists() and not force:
        try:
            raw = json.loads(cpath.read_text(encoding="utf-8"))
            if raw.get("version") == CACHE_VERSION:
                cache = {s["file"]: s for s in raw.get("sessions", [])}
        except (json.JSONDecodeError, OSError):
            cache = {}

    definitions = load_definitions(BUNDLED_BOSS_DIR)
    files = sorted(glob.glob(os.path.join(log_dir, "*.txt")))
    sessions = []
    for n, path in enumerate(files, 1):
        base = os.path.basename(path)
        if progress:
            progress(n, len(files), base)
        try:
            if os.path.getsize(path) < MIN_FILE_BYTES:
                continue
            key = _file_key(path)
        except OSError:
            continue
        cached = cache.get(base)
        if cached and cached.get("key") == key:
            sessions.append(cached)
            continue
        try:
            encounters = scan_file(path, definitions)
        except Exception as exc:  # one bad file must not kill the whole scan
            sessions.append({"file": base, "key": key, "error": str(exc),
                             "date": session_datetime(base)[0], "encounters": []})
            continue
        date, tod = session_datetime(base)
        sessions.append({
            "file": base, "key": key, "date": date, "time": tod,
            "path": path, "encounters": encounters,
        })

    index = {"version": CACHE_VERSION, "log_dir": log_dir,
             "built_at": time.time(), "sessions": sessions}
    try:
        cpath.write_text(json.dumps(index), encoding="utf-8")
    except OSError:
        pass
    return index


def load_index() -> Optional[dict]:
    """Returns the cached index without scanning, or None if never built."""
    cpath = _cache_path()
    if not cpath.exists():
        return None
    try:
        raw = json.loads(cpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return raw if raw.get("version") == CACHE_VERSION else None


# ---------------------------------------------------------------- queries

def all_encounters(index: dict):
    """Flattens to (session, encounter) pairs, oldest first."""
    for s in sorted(index.get("sessions", []), key=lambda s: (s.get("date") or "", s.get("time") or "")):
        for e in s.get("encounters", []):
            yield s, e


def boss_encounters(index: dict, boss_id: Optional[str] = None):
    for s, e in all_encounters(index):
        if e.get("boss") and (boss_id is None or e.get("boss_id") == boss_id):
            yield s, e


def boss_summary(index: dict) -> List[dict]:
    """One row per boss: how many pulls, total time, best/median clear."""
    by_boss: Dict[str, dict] = {}
    for s, e in boss_encounters(index):
        row = by_boss.setdefault(e["boss_id"], {
            "boss_id": e["boss_id"], "boss": e["boss"], "pulls": 0,
            "total_seconds": 0.0, "deaths": 0, "durations": [],
            "first_seen": s.get("date"), "last_seen": s.get("date"),
        })
        row["pulls"] += 1
        row["total_seconds"] += e["duration"]
        row["deaths"] += e.get("deaths", 0)
        row["durations"].append(e["duration"])
        if s.get("date"):
            row["last_seen"] = s["date"]
    out = []
    for row in by_boss.values():
        d = sorted(row.pop("durations"))
        row["longest"] = d[-1] if d else 0
        row["median"] = d[len(d) // 2] if d else 0
        row["total_seconds"] = round(row["total_seconds"])
        out.append(row)
    out.sort(key=lambda r: -r["pulls"])
    return out


def player_trend(index: dict, player: str, boss_id: Optional[str] = None,
                 metric: str = "dps") -> List[dict]:
    """Per-encounter time series for one player -- the longitudinal view.
    metric: dps | hps | dtps | deaths
    """
    series = []
    for s, e in boss_encounters(index, boss_id):
        for p in e["players"]:
            if p["name"] != player:
                continue
            dur = max(e["duration"], 0.001)
            value = {
                "dps": p["damage"] / dur,
                "hps": p["healing"] / dur,
                "dtps": p["taken"] / dur,
                "deaths": float(p["deaths"]),
            }.get(metric, 0.0)
            series.append({
                "date": s.get("date"), "time": s.get("time"), "file": s.get("file"),
                "boss": e["boss"], "boss_id": e["boss_id"],
                "duration": e["duration"], "value": round(value, 1),
                "deaths": p["deaths"],
            })
    return series


def players_seen(index: dict) -> List[dict]:
    """Every real player character in the corpus (NPCs already excluded by
    the indexer), with how often they appear and their damage/healing split
    so the UI can tell healers from dps."""
    agg: Dict[str, dict] = {}
    for _s, e in all_encounters(index):
        for p in e["players"]:
            row = agg.setdefault(p["name"], {"name": p["name"], "encounters": 0,
                                             "damage": 0, "healing": 0, "deaths": 0})
            row["encounters"] += 1
            row["damage"] += p["damage"]
            row["healing"] += p["healing"]
            row["deaths"] += p["deaths"]
    rows = list(agg.values())
    for r in rows:
        r["role"] = "healer" if r["healing"] > r["damage"] else "dps"
    rows.sort(key=lambda r: -r["encounters"])
    return rows
