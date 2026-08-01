"""
log_watcher.py

Locates the SWTOR CombatLogs folder and tails the most recently modified
log file, yielding new lines as they're written -- like the real BARAS.

Also tracks each line's absolute 1-based line number within its file (not
just "lines seen since we started watching"), since Parsely uploads need
real line ranges to extract just one encounter from a file (see
parsely_upload.py). To get real absolute numbers even though we start
tailing from the end of the file, we do a fast newline count over the
already-written portion when a file is first opened.
"""

import os
import time
import glob
from pathlib import Path
from typing import Iterator, Optional, Tuple

DEFAULT_LOG_DIRS = [
    # Windows default install path
    os.path.expanduser(r"~\Documents\Star Wars - The Old Republic\CombatLogs"),
    os.path.expanduser(r"~\Documents\Star Wars The Old Republic\CombatLogs"),
]


def find_log_dir() -> Optional[str]:
    for d in DEFAULT_LOG_DIRS:
        if os.path.isdir(d):
            return d
    return None


def find_latest_log(log_dir: str) -> Optional[str]:
    candidates = glob.glob(os.path.join(log_dir, "*.txt"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _count_existing_lines(path: str) -> int:
    count = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            count += chunk.count(b"\n")
    return count


def find_last_area_entered_line(path: str) -> Optional[int]:
    """Scans a file's already-written content for the last AreaEntered
    line, so a pull that starts right as the app launches (before we've
    live-observed our own AreaEntered event) still has zone context for
    Parsely uploads. Simple substring check -- good enough since we only
    need the line number, not a full parse."""
    last_match: Optional[int] = None
    line_number = 0
    with open(path, "r", encoding="cp1252", errors="replace") as f:
        for line in f:
            line_number += 1
            if "AreaEntered" in line:
                last_match = line_number
    return last_match


def find_local_player_name(path: str) -> Optional[str]:
    """Scans a file's already-written content for the last AreaEntered line
    and returns its source name. Verified against a real 34k-line raid log:
    AreaEntered appears only for the local player (7 hits, all the same
    name), unlike DisciplineChanged, which turned out to also be broadcast
    for any group member's loadout swap -- a teammate who respecced mid-log
    showed up under that event just as often. AreaEntered is a far more
    reliable local-player signal than "first player entity seen"
    (BossEncounterState._note_local_player), which can lock onto a teammate
    if the app is (re)started out-of-combat right as their action happens
    to be the first one tailed."""
    from log_parser import parse_line

    last_match: Optional[str] = None
    line_number = 0
    with open(path, "r", encoding="cp1252", errors="replace") as f:
        for line in f:
            line_number += 1
            if "AreaEntered" not in line:
                continue
            event = parse_line(line, line_number=line_number)
            if event is not None and event.source:
                last_match = event.source
    return last_match


def tail_file(path: str, poll_interval: float = 0.25) -> Iterator[Tuple[int, str]]:
    """Yields (absolute_line_number, line) for new lines appended to `path`,
    forever. Line numbers are 1-based and account for lines already in the
    file before we started watching. If the file shrinks or a newer file
    appears in the same directory, the caller should re-invoke with the new
    path (see watch_folder)."""
    line_number = _count_existing_lines(path)
    # SWTOR writes combat logs as Windows-1252, not UTF-8 -- reading them as
    # UTF-8 doesn't just render accented player names wrong, it silently
    # drops the offending bytes entirely (e.g. "Daust\xe9n" -> "Daustn"),
    # which also breaks exact-name matching for boss/timer rules whose
    # target name has an accent.
    with open(path, "r", encoding="cp1252", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                line_number += 1
                yield line_number, line
            else:
                time.sleep(poll_interval)
                # Detect log rotation: file truncated/replaced
                try:
                    if os.path.getsize(path) < f.tell():
                        break
                except OSError:
                    break


def watch_folder(log_dir: str, poll_interval: float = 0.25) -> Iterator[Tuple[str, int, str]]:
    """Continuously watches log_dir, always tailing whichever file is
    currently the newest (SWTOR starts a new log file each session).
    Yields (file_path, absolute_line_number, line)."""
    current_path = find_latest_log(log_dir)
    while current_path is None:
        time.sleep(1.0)
        current_path = find_latest_log(log_dir)

    while True:
        for line_number, line in tail_file(current_path, poll_interval):
            latest = find_latest_log(log_dir)
            if latest and latest != current_path:
                current_path = latest
                break
            yield current_path, line_number, line
        else:
            continue
        # A newer file appeared -- loop again and start tailing it
