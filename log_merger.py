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

from typing import Iterable, List, Optional, Tuple

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

    Seconds-of-day alone is NOT safe to use as a clock across a whole file:
    it goes backwards at midnight, and (observed in a real log) whenever the
    system clock is adjusted mid-session. Use LogClock for that.
    """
    try:
        time_part = timestamp.split()[-1]  # drop a leading date if present
        h, m, s = time_part.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, IndexError, AttributeError):
        return 0.0


SECONDS_PER_DAY = 86400.0
# A backwards jump larger than this is read as a date rollover rather than a
# clock adjustment. Half a day is the natural split: a real midnight wrap
# looks like -86400+delta (nearly a full day back), while DST/NTP corrections
# are at most a few hours.
_ROLLOVER_THRESHOLD = SECONDS_PER_DAY / 2


class LogClock:
    """Turns a log's wall-clock timestamps into a monotonically
    non-decreasing seconds value, for replaying a file start to finish.

    Two things make raw seconds-of-day unusable here, both seen in this
    user's real corpus:
      - MIDNIGHT: 23:59:59 -> 00:00:00 reads as ~86400 seconds backwards.
      - CLOCK ADJUSTMENT: one real log jumps 18:23:34.684 -> 16:23:35.036
        mid-combat, same players, ~0.35s of actual elapsed time. A DST or
        NTP correction written straight into the log.

    Either one makes an encounter spanning the jump compute a NEGATIVE
    duration, which Encounter.duration() clamps to 0.001s, which
    replay_pulls() then silently discards for being under
    MIN_ENCOUNTER_SECONDS. A whole pull disappears with no error.

    A rollover adds a full day. An adjustment can't be recovered -- the real
    elapsed time simply isn't in the file -- so the clock holds still rather
    than running backwards: the pull survives with a slightly short duration
    instead of vanishing.
    """

    def __init__(self):
        self._prev_raw: Optional[float] = None
        self._offset = 0.0

    def __call__(self, timestamp: str) -> float:
        raw = _seconds_of_day(timestamp)
        if self._prev_raw is not None and raw < self._prev_raw:
            if self._prev_raw - raw > _ROLLOVER_THRESHOLD:
                self._offset += SECONDS_PER_DAY
            else:
                # Pin to the previous reading: never negative, never invented.
                self._offset += self._prev_raw - raw
        self._prev_raw = raw
        return raw + self._offset


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
    clock = LogClock()
    for event in unique_events:
        encounter.apply(event, at_time=clock(event.timestamp or ""))

    return encounter
