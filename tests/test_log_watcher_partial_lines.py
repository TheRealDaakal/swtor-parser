"""
Covers the line-numbering drift in log_watcher.watch_folder.

readline() on a file that's actively being appended to returns whatever
bytes are there, newline or not. SWTOR writes a combat log continuously,
so a line caught mid-write came back as a fragment, got counted as a line,
and the remainder arrived on the next poll and got counted as ANOTHER one.
line_number therefore ran ahead of the file's real line count, and the
error was permanent for the rest of the session.

Why that matters beyond a cosmetic off-by-N: every completed pull is
persisted with the (log_path, start_line, end_line) pointer that Deep Dive
re-reads to reconstruct it. Once numbering drifts, that pointer aims past
the end of the file and the pull can't be re-read at all.

Measured on a real 200-row history before the fix: the last pulls of each
session referenced lines beyond the end of the log they named -- 1,082
over on one night, 892 on another, 407 on a third. Always growing through
the session, always correct at its start, which is the signature of an
error that accumulates per partial write rather than one bad offset.
"""
import itertools
import threading
import time

import pytest

import log_watcher


def _drain(log_dir, expected, poll_interval=0.01, timeout=10.0):
    """Collects `expected` yields from watch_folder, or fails on timeout.

    watch_folder never returns, so it's consumed on a worker thread and the
    test asserts on what arrived. islice stops pulling once satisfied.
    """
    got = []

    def run():
        for item in itertools.islice(
                log_watcher.watch_folder(log_dir, poll_interval=poll_interval), expected):
            got.append(item)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return got


def test_a_line_split_across_two_writes_is_counted_once(tmp_path):
    """The actual bug. The first file is tailed from its end, so the
    pre-existing content sets the starting line number and everything
    appended after is what gets counted."""
    log = tmp_path / "combat_2026-08-06_20_00_00_1.txt"
    log.write_text("existing line 1\nexisting line 2\n", encoding="cp1252")

    def append_in_fragments():
        time.sleep(0.15)
        with open(log, "a", encoding="cp1252") as f:
            # One logical line, written in three pieces -- exactly what
            # tailing a file mid-write sees.
            f.write("[00:00:01.000] first ")
            f.flush()
            time.sleep(0.10)
            f.write("half and second ")
            f.flush()
            time.sleep(0.10)
            f.write("half\n")
            f.flush()
            time.sleep(0.10)
            f.write("[00:00:02.000] a whole second line\n")
            f.flush()

    writer = threading.Thread(target=append_in_fragments, daemon=True)
    writer.start()
    got = _drain(str(tmp_path), expected=2)
    writer.join(timeout=5)

    assert len(got) == 2, f"expected 2 whole lines, got {[g[2] for g in got]}"
    numbers = [n for _p, n, _l in got]
    assert numbers == [3, 4], (
        f"line numbers must continue the file's own count (2 existing lines), got {numbers}"
    )
    assert got[0][2] == "[00:00:01.000] first half and second half\n", (
        "the fragments must be reassembled into the original line, not emitted piecemeal"
    )
    assert got[1][2] == "[00:00:02.000] a whole second line\n"


def test_numbering_stays_in_step_with_the_files_real_line_count(tmp_path):
    """The property that actually protects Deep Dive: the number handed out
    for a line must be the number a plain re-read of the file would give
    it, so (start_line, end_line) still resolves later."""
    log = tmp_path / "combat_2026-08-06_20_00_00_2.txt"
    log.write_text("", encoding="cp1252")

    total = 6

    def append_messily():
        time.sleep(0.15)
        with open(log, "a", encoding="cp1252") as f:
            for i in range(1, total + 1):
                # Every other line arrives in two pieces.
                if i % 2:
                    f.write(f"[00:00:{i:02d}.000] line {i}\n")
                else:
                    f.write(f"[00:00:{i:02d}.000] li")
                    f.flush()
                    time.sleep(0.05)
                    f.write(f"ne {i}\n")
                f.flush()
                time.sleep(0.05)

    writer = threading.Thread(target=append_messily, daemon=True)
    writer.start()
    got = _drain(str(tmp_path), expected=total)
    writer.join(timeout=5)

    assert len(got) == total
    assert [n for _p, n, _l in got] == list(range(1, total + 1))

    # The authority: what the file itself says each line is.
    on_disk = open(log, encoding="cp1252").read().splitlines(keepends=True)
    for _path, number, line in got:
        assert line == on_disk[number - 1], (
            f"line {number} yielded {line!r} but the file's line {number} is "
            f"{on_disk[number - 1]!r}"
        )


def test_a_trailing_fragment_is_never_emitted_as_a_line(tmp_path):
    """A half-written line must not reach the parser at all -- it would be
    parsed as a malformed event, and counting it is what caused the drift."""
    log = tmp_path / "combat_2026-08-06_20_00_00_3.txt"
    log.write_text("", encoding="cp1252")

    def append():
        time.sleep(0.15)
        with open(log, "a", encoding="cp1252") as f:
            f.write("[00:00:01.000] complete line\n")
            f.flush()
            time.sleep(0.10)
            f.write("[00:00:02.000] this one never finish")  # no newline, ever
            f.flush()

    writer = threading.Thread(target=append, daemon=True)
    writer.start()
    # Ask for 2 but only 1 is ever completable; the timeout is the assertion.
    got = _drain(str(tmp_path), expected=2, timeout=2.0)
    writer.join(timeout=5)

    assert len(got) == 1, f"only the complete line should be emitted, got {[g[2] for g in got]}"
    assert got[0][2] == "[00:00:01.000] complete line\n"
