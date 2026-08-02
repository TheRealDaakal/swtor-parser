"""
Covers a real live-raid bug: after the game crashes and the player relogs,
SWTOR starts a brand-new timestamped combat log file, and the old one
simply stops receiving writes forever -- it isn't truncated or shrunk.
watch_folder() used to only re-check for a newer file from inside the
"a new line just arrived" branch, so it would block on the dead old file
indefinitely and never notice the new one, which read to the user as "the
parser stopped" after a crash + relog. The fix checks for a newer file on
every idle poll tick, not just when new content arrives.

A second bug surfaced while writing this test: once a newer file IS
found, the original code unconditionally seeked to its end before
reading -- which silently discards whatever content was already written
in the gap between the file appearing and the poll loop noticing it. For
a fresh post-relog session that gap can include the very first lines
(e.g. AreaEntered, which main.py relies on to identify the local player),
so the fix also reads a freshly-discovered/rotated file from byte 0
instead, and only ever seeks-to-end for the very first file this
generator opens (the normal "don't replay old history on startup" case).

Uses real time.sleep()-based polling (log_watcher has no injectable clock,
unlike stats.py/timers.py) with a small poll_interval and generous
timeouts, so this stays fast without being flaky.
"""
import queue
import threading
import time

import log_watcher


def _start_puller(gen):
    """Runs the (blocking) generator on a background thread, pushing each
    yielded item onto a queue -- lets the test interleave "wait a beat"
    and "write a new line" between reads without ever calling next() from
    the main thread and risking a real hang."""
    q = queue.Queue()

    def run():
        try:
            for item in gen:
                q.put(item)
        except Exception as e:  # surfaced via _next() so failures show up as test failures, not a silent hang
            q.put(e)

    threading.Thread(target=run, daemon=True).start()
    return q


def _next(q, timeout=5.0):
    try:
        item = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(f"timed out after {timeout}s waiting for a line from the watcher")
    if isinstance(item, Exception):
        raise item
    return item


def test_picks_up_new_log_file_after_old_one_goes_silent(tmp_path):
    old_path = tmp_path / "combat_2026-01-01_00_00_00_000000.txt"
    old_path.write_text("", encoding="cp1252")

    gen = log_watcher.watch_folder(str(tmp_path), poll_interval=0.02)
    q = _start_puller(gen)

    time.sleep(0.2)  # let the watcher open the (empty) file and seek to its end
    with open(old_path, "a", encoding="cp1252") as f:
        f.write("line one\n")

    path1, num1, line1 = _next(q)
    assert path1 == str(old_path)
    assert num1 == 1
    assert line1 == "line one\n"

    # Simulate the crash: a brand-new session log appears (later mtime,
    # since it's created afterward), and the OLD file never gets another
    # write -- ever, not even a truncation.
    new_path = tmp_path / "combat_2026-01-01_00_05_00_000000.txt"
    with open(new_path, "w", encoding="cp1252") as f:
        f.write("relog line\n")

    path2, num2, line2 = _next(q, timeout=5.0)
    assert path2 == str(new_path), (
        f"expected the watcher to notice the post-crash relog's new log file "
        f"and switch to it, but it's still stuck on {path2!r}"
    )
    assert line2 == "relog line\n"
    assert num2 == 1, "the new file's already-written content must not be silently skipped"


def test_still_switches_when_old_file_is_truncated_in_place(tmp_path):
    """Not a crash -- the ordinary in-place rotation case (a file replaced
    at the same path) still has to keep working after the rewrite."""
    path = tmp_path / "combat_2026-01-01_00_00_00_000000.txt"
    path.write_text("", encoding="cp1252")

    gen = log_watcher.watch_folder(str(tmp_path), poll_interval=0.02)
    q = _start_puller(gen)

    time.sleep(0.2)
    with open(path, "a", encoding="cp1252") as f:
        f.write("first session line\n")
    p1, n1, l1 = _next(q)
    assert (p1, n1, l1) == (str(path), 1, "first session line\n")

    # Truncate and rewrite the SAME path with fresh, shorter content.
    with open(path, "w", encoding="cp1252") as f:
        f.write("new session line\n")

    p2, n2, l2 = _next(q, timeout=5.0)
    assert p2 == str(path)
    assert l2 == "new session line\n"
    assert n2 == 1, "line numbering should restart for the rewritten file, not continue from before"
