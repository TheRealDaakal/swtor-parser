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
from stats import RAID_DEFEATED_FRACTION

BUNDLED_BOSS_DIR = Path(__file__).resolve().parent.parent / "boss_definitions_bundled"


def kill_names(definition) -> List[str]:
    """Which entities must all die for this encounter to count as killed.

    Lives on BossDefinition (see its docstring for why it isn't boss_names);
    kept here as the name this module and its tests already use, and because
    corpus.py records the same outcome at scan time from the same rule.
    """
    return definition.kill_names()


def build_fight_summary(path: str, start_line: Optional[int], end_line: Optional[int],
                         definitions: Optional[dict] = None) -> dict:
    """Returns {"boss_name": str|None,
    "outcome": "kill"|"wipe"|"reset"|"unknown",
    "phases_seen": [str, ...], "deaths": [...]}.

    outcome is "unknown" (not a failure) when no boss was recognized at all
    -- trash/leveling/PvP content isn't a wipe just because nobody died to
    a named boss.

    A failed pull splits into "wipe" (the raid was defeated) and "reset"
    (the group disengaged and re-pulled), on the same measured threshold
    the corpus uses -- see stats.Encounter.raid_was_defeated(). Calling
    both of them "wipe" is what made the wipe population half no-death
    resets, and any comparison of kills against wipes read as noise.
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
    # Who was in the group, and who died -- for the wipe/reset split. Built
    # from the same per-event player flags stats.py uses, rather than from
    # the death reports alone, so a pull where nobody died still knows how
    # many people were there to not die.
    players_seen = set()
    players_died = set()

    for event in load_encounter_events(path, start_line, end_line):
        if event.source_is_player and event.source:
            players_seen.add(event.source)
        if event.target_is_player and event.target:
            players_seen.add(event.target)
        if event.is_death and event.target and event.target_is_player:
            players_died.add(event.target)
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
    elif still_alive is not None and not still_alive:
        # still_alive is an empty set only once every boss has been seen
        # dying; None means the boss was recognized but no event followed.
        outcome = "kill"
    elif players_seen and len(players_died) / len(players_seen) >= RAID_DEFEATED_FRACTION:
        outcome = "wipe"
    else:
        outcome = "reset"

    deaths = analyze_deaths(path, start_line, end_line)

    return {
        "boss_name": boss_name,
        "outcome": outcome,
        "phases_seen": phases_seen,
        "deaths": deaths,
    }
