"""
Covers analysis/events.py -- the single event source every analytics
module reads a pull through.

Two behaviours here are real shipped-bug regressions rather than
hypotheticals:

* The .gz fallback. log_archive.py gzips logs past the retention window
  and deletes the .txt, but History entries keep pointing at the .txt
  path, so every Deep Dive on an archived pull failed. Nothing in the
  suite covered analysis/ against an archived log, which is why it went
  unnoticed. test_forensics_still_works_after_the_log_is_archived drives
  the real analyze_deaths() through the real archive_old_logs().

* mtime in the cache key. The cache is a single slot keyed by
  (path, mtime, lo, hi). Without mtime, an in-flight pull -- whose
  end_line keeps growing as SWTOR appends -- could serve a stale
  truncated range for the rest of the session.
"""
import gzip
import os
import time

import pytest

from analysis import events
from conftest import log_line


@pytest.fixture(autouse=True)
def _clear_cache():
    events.clear_cache()
    yield
    events.clear_cache()


def _write_log(tmp_path, lines, name="combat_2026-08-06_20_00_00_123456.txt"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="cp1252")
    return str(path)


def _damage(ts, amount="1000"):
    return log_line(ts, "@Player#1", target="Test Boss", ability="Smash",
                    effect_name="Damage {2}", amount=amount)


# --------------------------------------------------------------- line ranges

def test_iterates_only_the_requested_line_range(tmp_path):
    path = _write_log(tmp_path, [_damage(f"00:00:{i:02d}.000", str(i * 100))
                                 for i in range(1, 11)])
    got = list(events.iter_encounter_events(path, 3, 5))
    assert [e.line_number for e in got] == [3, 4, 5]
    assert [e.amount for e in got] == [300, 400, 500]


def test_missing_range_falls_back_to_the_whole_file(tmp_path):
    path = _write_log(tmp_path, [_damage(f"00:00:{i:02d}.000") for i in range(1, 6)])
    assert len(list(events.iter_encounter_events(path, None, None))) == 5


def test_stops_reading_at_end_line_rather_than_scanning_the_rest(tmp_path):
    """The whole point of the (file, line range) pointer design: a pull in
    a 59 MB log must not cost a full-file read."""
    path = _write_log(tmp_path, [_damage(f"00:00:{i:02d}.000") for i in range(1, 100)])
    reads = {"lines": 0}
    real_open = events.open_log

    def counting_open(p):
        handle = real_open(p)

        class Counter:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return handle.__exit__(*a)

            def __iter__(self):
                for line in handle:
                    reads["lines"] += 1
                    yield line

        handle.__enter__()
        return Counter()

    events.open_log = counting_open
    try:
        list(events.iter_encounter_events(path, 1, 5))
    finally:
        events.open_log = real_open
    assert reads["lines"] <= 6, f"read {reads['lines']} lines to get 5"


# ------------------------------------------------------------------- gzip

def test_reads_a_gzipped_log_directly(tmp_path):
    plain = tmp_path / "combat.txt"
    plain.write_text(_damage("00:00:01.000") + "\n", encoding="cp1252")
    gz = tmp_path / "combat.txt.gz"
    with open(plain, "rb") as src, gzip.open(gz, "wb") as dst:
        dst.write(src.read())
    plain.unlink()

    got = list(events.iter_encounter_events(str(plain), None, None))
    assert len(got) == 1, "must follow the .txt -> .txt.gz rename log_archive.py performs"


def test_resolve_prefers_the_plain_file_when_both_exist(tmp_path):
    plain = tmp_path / "combat.txt"
    plain.write_text("", encoding="cp1252")
    (tmp_path / "combat.txt.gz").write_bytes(b"")
    assert events.resolve_log_path(str(plain)) == str(plain)


def test_missing_file_still_raises_naming_the_path_the_user_knows(tmp_path):
    missing = str(tmp_path / "combat.txt")
    with pytest.raises(OSError) as exc:
        list(events.iter_encounter_events(missing, None, None))
    assert "combat.txt" in str(exc.value)


def test_forensics_still_works_after_the_log_is_archived(tmp_path):
    """The actual regression: archive a log the way the app really does,
    then run the real Deep Dive death analysis over it."""
    import log_archive
    from analysis import forensics

    lines = [
        _damage("00:00:01.000", "5000"),
        log_line("00:00:02.000", "Test Boss", target="@Player#1", ability="Crush",
                 effect_name="Damage {2}", amount="99000", target_hp="1000/100000"),
        log_line("00:00:03.000", "Test Boss", target="@Player#1", ability="Crush",
                 effect_type="Event", effect_name="Death {836045448945472}", target_hp="0/100000"),
    ]
    path = _write_log(tmp_path, lines)
    # A second, newer log so the one under test isn't the "currently live"
    # file archive_old_logs() deliberately never touches.
    newer = _write_log(tmp_path, [_damage("00:00:01.000")], name="combat_newer.txt")
    os.utime(path, (time.time() - 90 * 86400,) * 2)

    before = forensics.analyze_deaths(path, None, None)
    assert before, "test needs at least one death to be meaningful"

    assert log_archive.archive_old_logs(str(tmp_path), retention_days=30) == [path]
    assert not os.path.exists(path) and os.path.exists(path + ".gz")
    assert os.path.exists(newer)

    events.clear_cache()
    after = forensics.analyze_deaths(path, None, None)
    assert after == before, "archiving must be transparent to analysis"


# ------------------------------------------------------------------- cache

def test_repeated_calls_for_the_same_range_reuse_one_parse(tmp_path):
    """The 2.7x Deep Dive speedup: summary, deaths and timeline all ask for
    the same range, and only the first should touch the disk."""
    path = _write_log(tmp_path, [_damage(f"00:00:{i:02d}.000") for i in range(1, 6)])
    first = events.load_encounter_events(path, 1, 5)
    opens = {"n": 0}
    real_open = events.open_log

    def counting_open(p):
        opens["n"] += 1
        return real_open(p)

    events.open_log = counting_open
    try:
        second = events.load_encounter_events(path, 1, 5)
    finally:
        events.open_log = real_open

    assert second is first, "same range must come back from cache, not a re-parse"
    assert opens["n"] == 0


def test_a_different_range_is_not_served_from_cache(tmp_path):
    path = _write_log(tmp_path, [_damage(f"00:00:{i:02d}.000", str(i * 100))
                                 for i in range(1, 11)])
    assert len(events.load_encounter_events(path, 1, 5)) == 5
    assert len(events.load_encounter_events(path, 1, 10)) == 10


def test_appending_to_the_log_invalidates_the_cache(tmp_path):
    """An in-flight pull grows as SWTOR appends. Without mtime in the key,
    the range would stay pinned to whatever it was first read as."""
    path = _write_log(tmp_path, [_damage("00:00:01.000")])
    assert len(events.load_encounter_events(path, None, None)) == 1

    with open(path, "a", encoding="cp1252") as f:
        f.write(_damage("00:00:02.000") + "\n")
    os.utime(path, (time.time() + 5,) * 2)  # ensure a distinct mtime on coarse clocks

    assert len(events.load_encounter_events(path, None, None)) == 2


def test_clear_cache_forces_a_fresh_read(tmp_path):
    path = _write_log(tmp_path, [_damage("00:00:01.000")])
    first = events.load_encounter_events(path, None, None)
    events.clear_cache()
    assert events.load_encounter_events(path, None, None) is not first
