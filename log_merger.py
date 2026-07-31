"""
log_merger.py

Import and merge one or more *saved* SWTOR combat log files into a single
Encounter -- useful when your own log has a gap (something happened out of
your client's visibility range) and a teammate's log covers it.

Important: a single player's SWTOR combat log already records every raid
member's actions within the same instance, not just their own -- that's why
a lone parser can normally show whole-raid DPS/HPS from one file. Merging
multiple logs is for filling visibility gaps, not a requirement for basic
raid-wide numbers.

Because of that overlap, naively concatenating multiple raid members' logs
would double- or triple-count nearly every event (everyone logged the same
fight). This module de-duplicates by treating events with the same
(timestamp, source, target, ability, effect_name, amount) as one, so
overlapping coverage doesn't inflate the numbers -- only genuinely new
events an individual log was missing get added.
"""

from typing import Iterable, List, Tuple

from log_parser import CombatEvent, parse_line
from stats import Encounter


def _dedup_key(event: CombatEvent) -> Tuple:
    return (
        event.timestamp,
        event.source,
        event.target,
        event.ability,
        event.effect_name,
        event.amount,
    )


def load_events(path: str) -> List[CombatEvent]:
    events = []
    with open(path, "r", encoding="cp1252", errors="replace") as f:
        for i, line in enumerate(f, 1):
            event = parse_line(line, line_number=i)
            if event is not None:
                events.append(event)
    return events


def _seconds_of_day(timestamp: str) -> float:
    """Converts 'HH:MM:SS(.mmm)' (optionally prefixed with a date) into
    seconds since midnight, for duration math on a replayed (not live) log.
    Falls back to 0.0 if it can't parse -- callers should still get a
    plausible relative ordering since we sort by the raw string first.
    """
    try:
        time_part = timestamp.split()[-1]  # drop a leading date if present
        h, m, s = time_part.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, IndexError, AttributeError):
        return 0.0


def merge_logs(paths: Iterable[str]) -> Encounter:
    """Parses each path, de-duplicates overlapping events across all of
    them, sorts by timestamp, and replays them into one combined Encounter.
    """
    paths = list(paths)
    all_events: List[CombatEvent] = []
    for path in paths:
        all_events.extend(load_events(path))

    seen = set()
    unique_events = []
    for event in all_events:
        key = _dedup_key(event)
        if key in seen:
            continue
        seen.add(key)
        unique_events.append(event)

    # Sort by the raw timestamp string. SWTOR timestamps are consistently
    # formatted (HH:MM:SS.mmm) so lexicographic sort matches chronological
    # order within a single log session.
    unique_events.sort(key=lambda e: e.timestamp or "")

    encounter = Encounter(label="Merged import")
    if len(paths) == 1:
        # A single file has one unambiguous, real line range, so it can be
        # uploaded to Parsely like any other pull. A true multi-file merge
        # can't: each file's line numbers are independent of the others',
        # so there's no single (path, start_line, end_line) that means
        # anything -- log_path stays unset and the Parsely upload button
        # correctly refuses those.
        encounter.log_path = paths[0]
    for event in unique_events:
        encounter.apply(event, at_time=_seconds_of_day(event.timestamp or ""))

    return encounter
