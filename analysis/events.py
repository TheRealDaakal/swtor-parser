"""
analysis/events.py

The single source of parsed events for one encounter's line range.

Every analytics module here works the same way: the corpus index stores a
(file, start_line, end_line) pointer per encounter rather than the events
themselves, so opening a pull means re-reading just that slice of the
original log. This module owns that read. Nothing else should open a log
file directly.

Two problems this exists to fix, both measured on a real 230-file archive:

1. `_iter_encounter_events` was copy-pasted byte-for-byte into
   forensics.py, timeline.py and fight_summary.py. Opening one Deep Dive
   calls all three, so the same pull was scanned and re-parsed three
   times. On the worst pull in the archive (Dread Master Brontes, 38,210
   lines) that measured 3,657 ms total, of which 518 ms per pass was
   `parse_line` alone. `load_encounter_events` caches the most recent
   range so the 2nd and 3rd caller reuse the 1st one's work.

2. log_archive.py gzips logs past the retention window and deletes the
   .txt, but the history entry still points at the .txt path. Deep Dive
   silently 500'd on every archived pull -- the app's own disk-space
   feature was destroying its own analytics history. `open_log` falls
   back to the .gz sibling, so archiving is now transparent to analysis.

The cache is deliberately a single slot, not an LRU: the access pattern is
one pull at a time (summary + deaths + timeline on the same range), so one
slot captures the whole win while bounding memory to exactly one pull's
events. Anything larger would trade the "no duplicated event storage"
requirement for no measured benefit.
"""

import gzip
import os
import threading
from typing import Iterator, List, Optional

from log_parser import CombatEvent, parse_line

LOG_ENCODING = "cp1252"


def resolve_log_path(path: str) -> str:
    """Returns the path that actually exists on disk for `path`, following
    log_archive.py's .txt -> .txt.gz rename. Returns `path` unchanged when
    neither exists, so callers still get a normal FileNotFoundError naming
    the file the user would recognize."""
    if os.path.exists(path):
        return path
    gz = path + ".gz"
    if os.path.exists(gz):
        return gz
    return path


def open_log(path: str):
    """Opens a combat log for reading, transparently handling both plain
    and gzipped files (see log_archive.py)."""
    resolved = resolve_log_path(path)
    if resolved.endswith(".gz"):
        return gzip.open(resolved, "rt", encoding=LOG_ENCODING, errors="replace")
    return open(resolved, "r", encoding=LOG_ENCODING, errors="replace")


def iter_encounter_events(path: str, start_line: Optional[int],
                          end_line: Optional[int]) -> Iterator[CombatEvent]:
    """Yields parsed events for one encounter's line range (inclusive).
    Falls back to the whole file when the range is unknown.

    Streams -- use this when one pass is genuinely all you need. When the
    same range will be read more than once, use load_encounter_events."""
    lo = start_line or 1
    hi = end_line or float("inf")
    with open_log(path) as f:
        for i, line in enumerate(f, 1):
            if i < lo:
                continue
            if i > hi:
                break
            event = parse_line(line, line_number=i)
            if event is not None:
                yield event


_cache_lock = threading.Lock()
_cache_key = None            # (resolved_path, mtime, lo, hi)
_cache_events: List[CombatEvent] = []


def load_encounter_events(path: str, start_line: Optional[int],
                          end_line: Optional[int]) -> List[CombatEvent]:
    """Same events as iter_encounter_events, as a list, with the most
    recent range cached.

    The cache key includes the file's mtime so a log still being appended
    to live can never serve a stale truncated range -- the in-flight pull
    is exactly the case where the range keeps growing.

    Returns the cached list itself, not a copy: callers here only read it.
    """
    global _cache_key, _cache_events

    resolved = resolve_log_path(path)
    try:
        mtime = os.path.getmtime(resolved)
    except OSError:
        mtime = None
    key = (resolved, mtime, start_line, end_line)

    with _cache_lock:
        if key == _cache_key:
            return _cache_events

    events = list(iter_encounter_events(path, start_line, end_line))

    with _cache_lock:
        _cache_key = key
        _cache_events = events
    return events


def clear_cache() -> None:
    """Drops the cached range. For tests, and for any caller that knows the
    underlying file changed in a way mtime wouldn't catch."""
    global _cache_key, _cache_events
    with _cache_lock:
        _cache_key = None
        _cache_events = []
