"""Pure/shared helpers for the local SWTOR Parser web server.

Kept separate from HTTP routing so analytics snapshots, history serialization,
and corpus state are testable without constructing a server.
"""

import threading
import time

# general "Active Timers" list -- mirrors the old gui.py's _OWN_PANEL_CATEGORIES.
_OWN_PANEL_CATEGORIES = ("cooldown", "dot", "hot")

def _safe_encounter_id(raw: str) -> str:
    """Encounter ids become filenames (`{id}.json`) under the user boss
    dir -- reject anything that isn't a plain path segment so a malicious
    or buggy request body can't escape that directory (e.g. id="../../foo")."""
    raw = (raw or "").strip()
    if not raw or "/" in raw or "\\" in raw or raw in (".", ".."):
        return ""
    return raw


def _timer_rows(rows):
    return [
        {"label": label, "remaining": round(remaining, 1), "total": total, "target": target}
        for label, remaining, total, _category, _is_alert, target in rows
    ]


def build_live_snapshot(tracker, timer_engine, boss_state, taunt_tracker, status) -> dict:
    # display_encounter() (not .current directly): keeps a just-finished
    # pull's numbers on the live tab through the between-pulls downtime
    # instead of blanking the instant a stray non-combat event rolls
    # `current` over to a fresh, empty Encounter() -- see
    # StatsTracker.display_encounter(). One call reused for both the row
    # data and the per-player extras below so they can't disagree about
    # which encounter is being shown.
    encounter = tracker.display_encounter()
    rows, duration = encounter.snapshot(), encounter.duration()
    active_boss = boss_state.active_boss if boss_state else None
    boss_names = active_boss.boss_names if active_boss else []
    players = []
    for name, dps, hps, taken, mitigated, deaths in rows:
        p = encounter.players.get(name)
        players.append({
            "name": name, "dps": dps, "hps": hps, "taken": taken,
            "mitigated": mitigated, "deaths": deaths,
            "boss_dps": p.boss_dps(boss_names, duration) if p else 0.0,
            "effective_hps": p.effective_hps(duration) if p else 0.0,
        })

    all_timers = timer_engine.snapshot()
    alerts = [t[0] for t in all_timers if t[4]]
    active_timers = _timer_rows(
        t for t in all_timers if t[3] not in _OWN_PANEL_CATEGORIES and not t[4]
    )
    cooldowns = _timer_rows(timer_engine.snapshot("cooldown"))
    dots_hots_raw = timer_engine.snapshot("dot") + timer_engine.snapshot("hot")
    dots_hots_raw.sort(key=lambda r: r[1])
    dots_hots = [
        {"tag": "DoT" if category == "dot" else "HoT", "label": label,
         "remaining": round(remaining, 1), "total": total, "target": target}
        for label, remaining, total, category, _is_alert, target in dots_hots_raw
    ]

    now = time.time()
    taunts = []
    for result in taunt_tracker.history:
        ago = max(0.0, now - result.at)
        if result.hit:
            text = (f"landed on {len(result.targets)} targets" if result.kind == "aoe"
                    else f"landed on {result.targets[0]}")
        else:
            text = "no target hit — resisted, out of range, or immune"
        taunts.append({"hit": result.hit, "text": text, "ago": round(ago, 1)})

    return {
        "boss": boss_state.status_text() if boss_state else None,
        "watching": status.text,
        "duration": round(duration, 1),
        "players": players,
        "alerts": alerts,
        "timers": active_timers,
        "cooldowns": cooldowns,
        "dots_hots": dots_hots,
        "taunts": taunts,
    }


def _player_row(name, dps, hps, taken, mitigated, deaths):
    return {"name": name, "dps": dps, "hps": hps, "taken": taken,
            "mitigated": mitigated, "deaths": deaths}


def build_history_list(tracker) -> list:
    # most-recent-first: (pull_num, duration, player_rows, real_start_time)
    rows = tracker.history_snapshot()
    out = []
    for pull_num, duration, player_rows, real_start_time in rows:
        top = [{"name": n, "dps": round(d)} for n, d, _h, _t, _m, _dth in player_rows[:3]]
        # A row whose numbers could not be re-derived from its own log --
        # its (log_path, line range) doesn't resolve, which happens to
        # rows recorded around a log-file rollover. Flagged rather than
        # hidden or quietly deleted: these still carry the old parser's
        # inflated death counts, and the user deserves to know which rows
        # those are. See history_migration.py.
        idx = pull_num - 1
        unverified = bool(
            0 <= idx < len(tracker.history)
            and getattr(tracker.history[idx], "unverified", False)
        )
        out.append({"pull": pull_num, "duration": round(duration, 1), "top": top,
                    "real_start_time": real_start_time, "unverified": unverified})
    out.reverse()  # oldest-first, matching the old Tk tree's insert order
    return out


class CorpusState:
    """Lazily holds the corpus index for the Deep Dive tab, and owns
    rebuilding it on a background thread.

    Rebuilding is not a request-scoped operation: a full scan of this
    developer's own 230-file folder takes ~165 seconds, so doing it inline
    would hang the HTTP handler (and with it the whole UI) well past any
    browser timeout. The UI kicks off /api/corpus/rebuild, then polls
    /api/corpus/status for progress -- the same shape analysis/webapp.py
    used, kept because it's the only workable one.

    load_index() only reads the cached file; it never triggers a scan. A
    first-run user gets built=False and an explicit "Build index" button
    rather than a silently frozen tab.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._index = None
        self._loaded = False       # distinguishes "never looked" from "no cache"
        self._building = False
        self._progress = {"done": 0, "total": 0, "file": ""}
        self._error = None

    def index(self):
        with self._lock:
            if not self._loaded:
                from analysis import corpus
                try:
                    self._index = corpus.load_index()
                except Exception as exc:      # a corrupt cache must not 500 the tab
                    self._index = None
                    self._error = str(exc)
                self._loaded = True
            return self._index

    def status(self) -> dict:
        idx = self.index()
        from analysis import corpus
        encounters = len(list(corpus.all_encounters(idx))) if idx else 0
        with self._lock:
            return {
                "built": idx is not None,
                "building": self._building,
                "progress": dict(self._progress),
                "error": self._error,
                "sessions": len(idx.get("sessions", [])) if idx else 0,
                "encounters": encounters,
                "built_at": idx.get("built_at") if idx else None,
                "log_dir": idx.get("log_dir") if idx else None,
            }

    def rebuild(self, force: bool = True) -> bool:
        """Starts a background rebuild. Returns False if one is already
        running -- two concurrent scans would race on the same cache file."""
        with self._lock:
            if self._building:
                return False
            self._building = True
            self._error = None
            self._progress = {"done": 0, "total": 0, "file": ""}

        def run():
            from analysis import corpus
            try:
                def progress(done, total, fname):
                    with self._lock:
                        self._progress = {"done": done, "total": total, "file": fname}
                index = corpus.build_index(progress=progress, force=force)
                with self._lock:
                    self._index = index
                    self._loaded = True
            except Exception as exc:
                with self._lock:
                    self._error = str(exc)
            finally:
                with self._lock:
                    self._building = False

        threading.Thread(target=run, daemon=True, name="corpus-rebuild").start()
        return True


def build_history_detail(tracker, idx: int):
    if not (0 <= idx < len(tracker.history)):
        return None
    encounter = tracker.history[idx]
    players = [_player_row(*row) for row in encounter.snapshot()]
    return {
        "pull": idx + 1,
        "label": encounter.label,
        "duration": round(encounter.duration(), 1),
        "players": players,
        "can_upload": bool(encounter.log_path) and encounter.start_line is not None
                       and encounter.end_line is not None,
    }


def build_ability_breakdown(encounter, player_name, boss_state):
    player = encounter.player(player_name)
    if player is None:
        return None
    duration = encounter.duration()
    dmg_rows, heal_rows = player.ability_breakdown()
    target_dmg_rows, _target_heal_rows = player.target_breakdown()

    stats = {"apm": round(player.apm(duration), 1)}
    if player.damage_events:
        stats["burst_dps"] = round(player.burst_dps())
    if player.heal_events:
        stats["burst_hps"] = round(player.burst_hps())
    if player.damage_attempts > 0:
        stats["accuracy_pct"] = round(player.accuracy_pct(), 1)
        stats["crit_pct"] = round(player.crit_pct(), 1)
    if player.heal_casts > 0:
        stats["heal_crit_pct"] = round(player.heal_crit_pct(), 1)
        stats["effective_hps"] = round(player.effective_hps(duration))
    if player.times_interrupted > 0:
        stats["times_interrupted"] = player.times_interrupted
    if player.cc_casts > 0:
        stats["cc_casts"] = player.cc_casts
    if player.raid_buff_casts > 0:
        stats["raid_buff_casts"] = player.raid_buff_casts
    # Boss-only DPS for THIS pull's own boss -- matched by encounter.label
    # (set from the boss name at import/completion time) against the
    # definitions' own .name, not boss_state.active_boss. That used to mean
    # a historical pull's boss DPS reflected whatever boss happened to be
    # live right now (wrong/missing for anything reviewed outside of an
    # active encounter) -- fixed here since History is exactly where this
    # stat needs to work.
    boss_names = None
    if encounter.label and boss_state:
        for definition in boss_state.definitions.values():
            if definition.name == encounter.label:
                boss_names = definition.boss_names
                break
    if boss_names:
        boss_dmg = player.damage_to(boss_names)
        if boss_dmg > 0:
            stats["boss_dps"] = round(boss_dmg / duration) if duration > 0 else None

    return {
        "name": player.name,
        "stats": stats,
        "damage_by_ability": [{"ability": a, "amount": round(v)} for a, v in dmg_rows],
        "healing_by_ability": [{"ability": a, "amount": round(v)} for a, v in heal_rows],
        "damage_by_target": [{"target": t, "amount": round(v)} for t, v in target_dmg_rows],
        "cc_by_ability": [{"ability": a, "amount": n} for a, n in
                           sorted(player.cc_by_ability.items(), key=lambda kv: -kv[1])],
        "raid_buff_by_ability": [{"ability": a, "amount": n} for a, n in
                                  sorted(player.raid_buff_by_ability.items(), key=lambda kv: -kv[1])],
    }
