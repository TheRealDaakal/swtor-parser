"""
analysis/forensics.py

Death reconstruction: not "you died at 3:25" (every parser shows that), but
*what killed you* -- the damage sequence in the seconds beforehand, your HP
trajectory, and which defensive cooldowns were available but unused.

Why this is worth doing: the raw information is already in the log, but no
parser assembles it. Answering "could I have survived that?" by hand means
scrolling a combat log; here it's a table.

Method: the corpus index stores a (file, line range) pointer per encounter
rather than every event, so this re-reads just the one encounter it needs
and walks a window before each death.

Cooldown availability is a best-effort inference from the same data
cooldowns.py uses: we watch the player's own casts during the encounter and
assume an ability is available if it was never used, or if its cooldown has
elapsed since the last use. That can't see cooldowns burned *before* the
pull started, so it errs toward "was available" -- flagged in the output as
`inferred` rather than presented as certain.
"""

from typing import Dict, List, Optional

from cooldowns import DEFENSIVE_COOLDOWNS
from log_merger import _seconds_of_day
from log_parser import parse_line

# How far back to reconstruct before the killing blow.
WINDOW_SECONDS = 12.0

_CD_BY_NAME = {d.label: d for d in DEFENSIVE_COOLDOWNS}


def _iter_encounter_events(path: str, start_line: Optional[int], end_line: Optional[int]):
    """Yields parsed events for one encounter's line range (inclusive).
    Falls back to the whole file when the range is unknown."""
    lo = start_line or 1
    hi = end_line or float("inf")
    with open(path, "r", encoding="cp1252", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i < lo:
                continue
            if i > hi:
                break
            event = parse_line(line, line_number=i)
            if event is not None:
                yield event


def analyze_deaths(path: str, start_line: Optional[int], end_line: Optional[int],
                   player: Optional[str] = None,
                   window_seconds: float = WINDOW_SECONDS,
                   players_only: bool = True) -> List[dict]:
    """Returns one report per death in the encounter.

    Each report: who died, when, the incoming damage in the window before
    (grouped by ability and by source), their HP trajectory, and defensive
    cooldown availability at time of death.

    players_only (default True) restricts this to real player deaths. A boss
    pull kills dozens of adds -- "what killed this Grey Swarm Thrall" is
    noise, and on a typical pull it outnumbers real player deaths ~20:1.
    """
    events = list(_iter_encounter_events(path, start_line, end_line))
    if not events:
        return []

    base = _seconds_of_day(events[0].timestamp or "")

    # Who in this encounter is a real player (SWTOR's '@' marker)?
    player_names = set()
    for ev in events:
        if ev.source_is_player and ev.source:
            player_names.add(ev.source)
        if ev.target_is_player and ev.target:
            player_names.add(ev.target)

    def t(ev):
        return _seconds_of_day(ev.timestamp or "") - base

    # Track each player's own defensive casts, to infer availability later.
    last_defensive_use: Dict[str, Dict[str, float]] = {}
    for ev in events:
        name = ev.ability or ""
        if not ev.is_ability_activate or not ev.source:
            continue
        for label in _CD_BY_NAME:
            if label.lower() in name.lower():
                last_defensive_use.setdefault(ev.source, {})[label] = t(ev)

    reports = []
    for idx, ev in enumerate(events):
        if not ev.is_death or not ev.target:
            continue
        if player and ev.target != player:
            continue
        if players_only and ev.target not in player_names:
            continue
        victim = ev.target
        death_t = t(ev)
        lo_t = death_t - window_seconds

        by_ability: Dict[str, dict] = {}
        by_source: Dict[str, float] = {}
        timeline = []
        hp_track = []
        total = 0.0

        for prev in events[max(0, idx - 4000):idx]:
            pt = t(prev)
            if pt < lo_t or prev.target != victim:
                continue
            if prev.hp_current is not None and prev.hp_max:
                hp_track.append({"t": round(pt - death_t, 2),
                                 "pct": round(100.0 * prev.hp_current / prev.hp_max, 1)})
            if not (prev.is_damage and prev.amount):
                continue
            key = prev.ability or prev.effect_name or "Unknown"
            row = by_ability.setdefault(key, {"ability": key, "total": 0.0, "hits": 0,
                                              "max": 0.0, "sources": set()})
            row["total"] += prev.amount
            row["hits"] += 1
            row["max"] = max(row["max"], prev.amount)
            if prev.source:
                row["sources"].add(prev.source)
                by_source[prev.source] = by_source.get(prev.source, 0.0) + prev.amount
            total += prev.amount
            timeline.append({
                "t": round(pt - death_t, 2),
                "ability": key,
                "source": prev.source,
                "amount": round(prev.amount),
            })

        abilities = []
        for row in by_ability.values():
            row["sources"] = sorted(row["sources"])
            row["total"] = round(row["total"])
            row["max"] = round(row["max"])
            abilities.append(row)
        abilities.sort(key=lambda r: -r["total"])

        used = last_defensive_use.get(victim, {})
        cds = []
        for label, d in _CD_BY_NAME.items():
            last = used.get(label)
            if last is None:
                # Never seen this player cast it in this encounter. Could be a
                # class ability they don't have at all, so only report ones we
                # have actually seen them use at some point.
                continue
            since = death_t - last
            cds.append({
                "ability": label,
                "last_used_secs_before_death": round(since, 1),
                "cooldown": d.cooldown_seconds,
                "available_at_death": since >= d.cooldown_seconds,
                "used_in_window": since <= window_seconds,
                "inferred": True,
            })
        cds.sort(key=lambda r: (not r["available_at_death"], r["ability"]))

        timeline.sort(key=lambda r: r["t"])
        reports.append({
            "victim": victim,
            "death_time": round(death_t, 1),
            "timestamp": ev.timestamp,
            "line": ev.line_number,
            "window_seconds": window_seconds,
            "damage_in_window": round(total),
            "by_ability": abilities[:12],
            "by_source": sorted(
                ({"source": k, "total": round(v)} for k, v in by_source.items()),
                key=lambda r: -r["total"])[:8],
            "killing_blow": timeline[-1] if timeline else None,
            "timeline": timeline[-40:],
            "hp_track": hp_track[-60:],
            "defensives": cds,
        })

    return reports


def summarize_deaths(reports: List[dict]) -> dict:
    """Aggregates many death reports -- 'what keeps killing this raid'."""
    by_ability: Dict[str, dict] = {}
    by_victim: Dict[str, int] = {}
    for r in reports:
        by_victim[r["victim"]] = by_victim.get(r["victim"], 0) + 1
        kb = r.get("killing_blow")
        if kb:
            row = by_ability.setdefault(kb["ability"], {"ability": kb["ability"], "kills": 0})
            row["kills"] += 1
    return {
        "total_deaths": len(reports),
        "killing_abilities": sorted(by_ability.values(), key=lambda r: -r["kills"])[:15],
        "by_victim": sorted(({"name": k, "deaths": v} for k, v in by_victim.items()),
                            key=lambda r: -r["deaths"]),
    }
