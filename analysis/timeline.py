"""
analysis/timeline.py

Per-pull time-bucketed damage/healing series, plus boss phase-change
offsets -- the data behind a StarParse-style timeline scrubber (drag a
range, see recalculated stats for just that slice) that neither BARAS nor
ORBS has at all.

Same re-read method as forensics.py: the corpus index stores a (file, line
range) pointer per encounter rather than every event, so this re-reads
just the one encounter it needs rather than keeping a live per-second
history for every pull ever played (which is what a naive "just track it
as you go" approach would require, for a feature only used occasionally
after the fact).
"""

from pathlib import Path
from typing import Dict, List, Optional

from analysis.events import load_encounter_events
from boss_definitions import load_definitions
from boss_intelligence import BossEncounterState
from log_merger import _seconds_of_day

BUNDLED_BOSS_DIR = Path(__file__).resolve().parent.parent / "boss_definitions_bundled"

# Target roughly this many buckets across the pull, so a 40s add-phase skirmish
# and a 12-minute progression attempt both render as a readable chart instead
# of either a handful of blocky bars or thousands of illegible slivers.
TARGET_BUCKETS = 90
MIN_BUCKET_SECONDS = 1.0
MAX_BUCKET_SECONDS = 20.0


def build_timeline(path: str, start_line: Optional[int], end_line: Optional[int],
                    players_only: bool = True) -> dict:
    """Returns {"duration", "bucket_seconds", "players": {name: {"damage":
    [...], "healing": [...]}}, "phases": [{"name", "start_offset"}]}.

    Bucket arrays are all the same length (ceil(duration / bucket_seconds)),
    zero-filled for buckets where that player did nothing -- so the frontend
    can assume every player's arrays line up index-for-index with every
    other player's and with the phase offsets, no per-row length checks.
    """
    events = load_encounter_events(path, start_line, end_line)
    if not events:
        return {"duration": 0.0, "bucket_seconds": 1.0, "players": {}, "phases": []}

    base = _seconds_of_day(events[0].timestamp or "")
    duration = max(_seconds_of_day(events[-1].timestamp or "") - base, 0.001)

    bucket_seconds = max(MIN_BUCKET_SECONDS,
                         min(MAX_BUCKET_SECONDS, duration / TARGET_BUCKETS))
    n_buckets = int(duration // bucket_seconds) + 1

    player_names = set()
    for ev in events:
        if ev.source_is_player and ev.source:
            player_names.add(ev.source)
        if ev.target_is_player and ev.target:
            player_names.add(ev.target)

    def bucket_of(ev) -> int:
        offset = _seconds_of_day(ev.timestamp or "") - base
        idx = int(offset // bucket_seconds)
        return max(0, min(n_buckets - 1, idx))

    players: Dict[str, dict] = {}

    def row(name: str) -> dict:
        if name not in players:
            players[name] = {
                "damage": [0.0] * n_buckets,
                "healing": [0.0] * n_buckets,
            }
        return players[name]

    for ev in events:
        # Same "is this a real attack" gating stats.py uses -- environmental
        # damage (falling, etc.) has an empty ability field and isn't a
        # player's own output, so it must not inflate their timeline either.
        is_attack = ev.is_damage and ev.ability is not None
        if is_attack and ev.amount and ev.source:
            if players_only and ev.source not in player_names:
                continue
            row(ev.source)["damage"][bucket_of(ev)] += ev.amount
        if ev.is_heal and ev.amount and ev.source:
            if players_only and ev.source not in player_names:
                continue
            row(ev.source)["healing"][bucket_of(ev)] += ev.amount

    # Boss phase-change offsets, replayed the same way the live app derives
    # them -- reuses the exact same engine live tracking uses, rather than
    # a second, potentially-inconsistent notion of "what phase were we in".
    phases: List[dict] = []
    try:
        definitions = load_definitions(BUNDLED_BOSS_DIR)
        boss_state = BossEncounterState(definitions)
        for ev in events:
            change = boss_state.feed(ev, timer_engine=None)
            if change is not None:
                offset = _seconds_of_day(ev.timestamp or "") - base
                phases.append({"name": change.phase_name, "start_offset": round(offset, 1)})
    except Exception:
        phases = []  # phase shading is a nice-to-have; never let it break the timeline

    return {
        "duration": round(duration, 1),
        "bucket_seconds": round(bucket_seconds, 2),
        "players": players,
        "phases": phases,
    }
