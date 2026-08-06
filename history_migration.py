"""
history_migration.py

Rebuilds stored History rows that were written by an older parser.

Why this has to exist: History is *persisted aggregate* — each row keeps
per-player totals, not the events they came from — so a parser fix does
nothing for pulls already on disk. After the death-detection fix, the app
contradicted itself: Deep Dive re-reads the raw log and reported the right
death counts, while the History tab kept showing the inflated ones from
whatever build recorded them. An app that disagrees with itself is worse
than one that's merely wrong, because there's no way for the user to tell
which half to believe.

Measured on this developer's own 200-row history: 849 recorded deaths
rebuilt to 667. The extra 182 were "Deathmark" and friends — abilities
whose names merely contain the word.

Method: every row already stores the (log_path, start_line, end_line)
pointer Deep Dive uses, so a row can be rebuilt by re-reading exactly its
own slice of the original log through the current parser. Rows are grouped
by file and replayed in line order so each log is opened once, not once per
row — 200 rows across 5 files rebuild in about 5 seconds.

Deliberately conservative about scope. It rebuilds each row IN PLACE from
its own recorded line range rather than re-splitting the whole log:
boundaries stay exactly where the user already saw them, so pull numbering
doesn't shift under them. The one exception is rows that turn out not to be
fights at all (the between-pulls healing segments — see
Encounter.is_real_fight), which are dropped, because leaving them would
mean History still disagrees with Deep Dive about what a pull is.

A row whose log file is gone (archived beyond the retention window,
manually deleted) cannot be rebuilt. Those are KEPT and left stamped at
their old version rather than deleted or silently presented as current —
losing a raid night's history to a migration would be a far worse outcome
than a stale number, and the caller can surface the count.
"""

import collections
from typing import Callable, Dict, List, Optional, Tuple

from analysis.events import open_log, resolve_log_path
from log_merger import LogClock
from log_parser import parse_line
from stats import Encounter, HISTORY_DATA_VERSION

import os


def needs_migration(encounters: List[Encounter]) -> bool:
    """True if any row was written by an older parser AND hasn't already
    been found unrebuildable. Cheap enough to call on every startup -- it's
    two attribute checks per row.

    The `unverified` half matters: without it, rows that can never be
    rebuilt keep the answer True forever and the migration re-runs (and
    re-fails) on every single launch.
    """
    return any(getattr(e, "data_version", 0) < HISTORY_DATA_VERSION
               and not getattr(e, "unverified", False)
               for e in encounters)


def _rebuildable(enc: Encounter) -> bool:
    if getattr(enc, "data_version", 0) >= HISTORY_DATA_VERSION:
        return False
    if getattr(enc, "unverified", False):
        return False
    if not enc.log_path or enc.start_line is None or enc.end_line is None:
        return False
    return os.path.exists(resolve_log_path(enc.log_path))


def migrate(encounters: List[Encounter],
            progress: Optional[Callable[[int, int], None]] = None
            ) -> Tuple[List[Encounter], Dict[str, int]]:
    """Returns (encounters, report).

    report: {"rebuilt", "dropped", "unrebuildable", "checked"} -- counts the
    caller can log or show. The returned list is a NEW list; the input is
    not mutated.
    """
    report = {"checked": len(encounters), "rebuilt": 0, "dropped": 0,
              "unrebuildable": 0}

    todo = [e for e in encounters if _rebuildable(e)]
    for e in encounters:
        if getattr(e, "data_version", 0) < HISTORY_DATA_VERSION and e not in todo:
            report["unrebuildable"] += 1
    if not todo:
        return list(encounters), report

    by_file: Dict[str, List[Encounter]] = collections.defaultdict(list)
    for e in todo:
        by_file[e.log_path].append(e)

    # id() rather than the Encounter itself: Encounter isn't hashable by
    # value and two pulls can compare equal on their fields.
    rebuilt: Dict[int, Encounter] = {}
    done = 0

    for path, rows in by_file.items():
        lo = min(r.start_line for r in rows)
        hi = max(r.end_line for r in rows)
        clocks: Dict[int, LogClock] = {}
        try:
            with open_log(path) as fh:
                for i, line in enumerate(fh, 1):
                    if i < lo:
                        continue
                    if i > hi:
                        break
                    event = parse_line(line, line_number=i)
                    if event is None:
                        continue
                    for row in rows:
                        if not (row.start_line <= i <= row.end_line):
                            continue
                        key = id(row)
                        fresh = rebuilt.get(key)
                        if fresh is None:
                            fresh = rebuilt[key] = Encounter(label=row.label)
                            fresh.log_path = row.log_path
                            fresh.start_line = row.start_line
                            fresh.end_line = row.end_line
                            fresh.area_entered_line = row.area_entered_line
                            clocks[key] = LogClock()
                        fresh.apply(event, at_time=clocks[key](event.timestamp or ""),
                                    real_time=row.real_start_time)
        except OSError:
            # A file that vanished or won't read mid-migration: leave every
            # row from it exactly as it was rather than half-rebuilding.
            for row in rows:
                rebuilt.pop(id(row), None)
            report["unrebuildable"] += len(rows)
            continue
        done += len(rows)
        if progress:
            progress(done, len(todo))

    out: List[Encounter] = []
    for row in encounters:
        fresh = rebuilt.get(id(row))
        if fresh is None:
            if row in todo:
                # We had a file and a line range, read it, and got nothing
                # back. Measured on a real 200-row history: 9 rows point
                # PAST the end of the file they name (139,109 in a 138,663-
                # line log). Those were recorded around a log rollover,
                # where the reader's line numbering had already moved on to
                # the next file while log_path still named the previous one
                # -- so the pointer can't be resolved by anyone, this
                # migration or Deep Dive.
                #
                # Kept, not deleted: a stale number is a far better outcome
                # than silently losing a raid night. Marked so it is never
                # retried and never claimed as verified.
                row.unverified = True
                report["unrebuildable"] += 1
            out.append(row)
            continue
        if not fresh.is_real_fight():
            # Not a pull at all -- the raid healing up between attempts.
            report["dropped"] += 1
            continue
        # real_start_time is the row's own calendar anchor, reconstructed
        # from the log filename when it was first recorded; the rebuild has
        # no better source for it.
        fresh.real_start_time = row.real_start_time
        fresh.data_version = HISTORY_DATA_VERSION
        report["rebuilt"] += 1
        out.append(fresh)
    return out, report
