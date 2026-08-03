"""
log_archive.py

SWTOR itself never rotates or deletes its own combat logs -- every session
gets a brand new file in CombatLogs and old ones just sit there forever,
which adds up to real disk bloat for anyone who's been playing a while
(a single busy raid night can be tens of MB; multiplied by months of
sessions, that's real space). This compresses old ones down to a fraction
of their size rather than deleting them outright -- reversible (just
unzip it back) instead of destructive, since a raid log might matter again
later (a parse dispute, a "when did we first see that mechanic" question).

Safety: the file log_watcher.find_latest_log() would currently tail is
NEVER touched, regardless of its age -- gzipping the log SWTOR might
still be actively writing to would corrupt live tailing. This is checked
by mtime, the same signal find_latest_log() itself uses, not by name.
"""

import gzip
import os
import shutil
import time
from pathlib import Path
from typing import List, Optional


def archive_old_logs(log_dir: str, retention_days: float) -> List[str]:
    """Gzips every *.txt file in log_dir older than retention_days (by
    mtime), except whichever one is currently the newest -- that one is
    never touched, no matter how old it looks, since it's the one a live
    session could still be tailing. Returns the list of original paths
    that were archived (now replaced by a same-named .gz sibling)."""
    directory = Path(log_dir)
    if not directory.is_dir():
        return []

    candidates = sorted(directory.glob("*.txt"))
    if not candidates:
        return []

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - retention_days * 86400

    archived = []
    for path in candidates:
        if path == newest:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        gz_path = path.with_suffix(path.suffix + ".gz")
        try:
            with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
        except OSError:
            # Leave both the source and any partial .gz in place rather
            # than silently losing data -- next run will just retry it.
            if gz_path.exists():
                try:
                    gz_path.unlink()
                except OSError:
                    pass
            continue
        archived.append(str(path))
    return archived
