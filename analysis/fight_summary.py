"""
analysis/fight_summary.py

"Did we actually kill it, or wipe" plus the honest facts around any real
player deaths in the pull -- composed from data this app already collects
per-pull (forensics.py's death reconstruction, the same boss-recognition
replay timeline.py already uses), not a new tracking subsystem.

Deliberately does NOT synthesize a causal "here's why you wiped" sentence
-- that would be inventing certainty the data doesn't support (a death
report can say a defensive cooldown was available and unused; it can't
prove that's what actually caused a wipe). This returns structured facts
in death order; the frontend presents them as facts, not a verdict.

Same re-read method as forensics.py/timeline.py: the pull's own
(log_path, start_line, end_line) is re-parsed on demand rather than
tracking everything live for every pull ever played.
"""

from pathlib import Path
from typing import Dict, List, Optional

from boss_definitions import load_definitions
from boss_intelligence import BossEncounterState
from log_parser import parse_line
from analysis.forensics import analyze_deaths

BUNDLED_BOSS_DIR = Path(__file__).resolve().parent.parent / "boss_definitions_bundled"


def _iter_encounter_events(path: str, start_line: Optional[int], end_line: Optional[int]):
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


def build_fight_summary(path: str, start_line: Optional[int], end_line: Optional[int],
                         definitions: Optional[dict] = None) -> dict:
    """Returns {"boss_name": str|None, "outcome": "kill"|"wipe"|"unknown",
    "phases_seen": [str, ...], "deaths": [...]}.

    outcome is "unknown" (not "wipe") when no boss was recognized at all --
    trash/leveling/PvP content isn't a wipe just because nobody died to a
    named boss. "wipe" only means "a boss WAS recognized and its death was
    never seen in this pull's own line range" -- a pull that trails off
    mid-fight (e.g. the group regrouped and the log rolled to a new pull)
    reads the same as a real wipe, since from the log's perspective they
    genuinely look identical.
    """
    if definitions is None:
        definitions = load_definitions(BUNDLED_BOSS_DIR)

    boss_state = BossEncounterState(definitions)
    phases_seen: List[str] = []
    boss_died = False

    for event in _iter_encounter_events(path, start_line, end_line):
        change = boss_state.feed(event, timer_engine=None)
        if change is not None and change.phase_name not in phases_seen:
            phases_seen.append(change.phase_name)
        active = boss_state.active_boss
        if active is not None and event.is_death and event.target:
            if (event.target in active.boss_names
                    or (active.boss_npc_ids and event.target_npc_id in active.boss_npc_ids)):
                boss_died = True

    boss_name = boss_state.active_boss.name if boss_state.active_boss else None
    if boss_name is None:
        outcome = "unknown"
    else:
        outcome = "kill" if boss_died else "wipe"

    deaths = analyze_deaths(path, start_line, end_line)

    return {
        "boss_name": boss_name,
        "outcome": outcome,
        "phases_seen": phases_seen,
        "deaths": deaths,
    }
