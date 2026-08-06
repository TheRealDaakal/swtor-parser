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

from analysis.events import load_encounter_events
from analysis.forensics import analyze_deaths
from boss_definitions import load_definitions
from boss_intelligence import BossEncounterState

BUNDLED_BOSS_DIR = Path(__file__).resolve().parent.parent / "boss_definitions_bundled"


def kill_names(definition) -> List[str]:
    """Which entities must die for this encounter to count as killed.

    NOT the same list as `boss_names`, which exists for RECOGNITION -- "if
    you see any of these, you're in this fight" -- and so includes adds that
    identify an encounter without being its boss. Styrak's is
    ['Kell Dragon', 'Dread Master Styrak'], and a Styrak pull kills dozens
    of Kell Dragons: using boss_names as the kill test marked 74 of 92
    Styrak pulls a kill, including ones nobody survived.

    The rule: entities whose name is part of the encounter's own display
    name are the bosses. That resolves 149 of the 162 bundled definitions
    with boss_names. The 13 it doesn't are multi-boss encounters where
    every listed name genuinely IS a boss and all of them must die --
    Dread Council, Cartel Warlords, The Dread Guard, Trandoshans,
    Firebrand & Stormcaller and friends -- plus a couple whose display name
    is spelled slightly differently from the entity ("Colosssal Monolith").
    Falling back to the full list is right for all of them.
    """
    names = list(getattr(definition, "boss_names", None) or [])
    if not names:
        return []
    display = (getattr(definition, "name", "") or "").lower()
    scoped = [n for n in names if n.lower() in display or display in n.lower()]
    return scoped or names


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
    # Names still owed a death before this counts as a kill. Populated once
    # the boss is recognized; a multi-boss encounter needs ALL of them, so
    # downing only Bestia is not a Dread Council kill.
    #
    # Matched by name only. The npc-id path this used to also accept can't
    # be used here: boss_npc_ids is a flat list per definition with no
    # mapping back to which name each id belongs to, and Styrak's includes
    # the Kell Dragon's id -- so it reintroduces exactly the add-counts-as-
    # the-boss bug. Death lines always carry the entity's name, so the name
    # test is sufficient.
    still_alive = None

    for event in load_encounter_events(path, start_line, end_line):
        change = boss_state.feed(event, timer_engine=None)
        if change is not None and change.phase_name not in phases_seen:
            phases_seen.append(change.phase_name)
        active = boss_state.active_boss
        if active is None:
            continue
        if still_alive is None:
            still_alive = set(kill_names(active))
        if event.is_death and event.target:
            still_alive.discard(event.target)

    boss_name = boss_state.active_boss.name if boss_state.active_boss else None
    if boss_name is None:
        outcome = "unknown"
    else:
        # still_alive is an empty set only once every boss has been seen
        # dying; None means the boss was recognized but no event followed.
        outcome = "kill" if still_alive is not None and not still_alive else "wipe"

    deaths = analyze_deaths(path, start_line, end_line)

    return {
        "boss_name": boss_name,
        "outcome": outcome,
        "phases_seen": phases_seen,
        "deaths": deaths,
    }
