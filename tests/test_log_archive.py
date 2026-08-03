"""
Covers log_archive.py -- compressing (not deleting) old SWTOR combat logs
to free disk space, since the game itself never rotates or deletes them.
The one safety property that actually matters: whichever file is
currently newest by mtime is NEVER touched, no matter how old it looks --
that's the one a live session could still be tailing, and gzipping it out
from under an active tail would corrupt things.
"""
import gzip
import os
import time

from log_archive import archive_old_logs


def _write(path, content="some combat log content\n"):
    path.write_text(content, encoding="utf-8")


def _age(path, days_old):
    old_time = time.time() - days_old * 86400
    os.utime(path, (old_time, old_time))


def test_old_files_are_compressed_and_original_removed(tmp_path):
    old_file = tmp_path / "combat_old.txt"
    _write(old_file, "the real content\n")
    _age(old_file, 40)
    newest = tmp_path / "combat_newest.txt"
    _write(newest)

    archived = archive_old_logs(str(tmp_path), retention_days=30)

    assert archived == [str(old_file)]
    assert not old_file.exists()
    gz_path = tmp_path / "combat_old.txt.gz"
    assert gz_path.exists()
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        assert f.read() == "the real content\n"


def test_the_current_newest_file_is_never_touched_regardless_of_age(tmp_path):
    """The critical safety property: even a file that LOOKS ancient by
    mtime must be skipped if it's currently the newest in the folder --
    that's exactly the file log_watcher.find_latest_log() would be
    tailing live."""
    only_file = tmp_path / "combat_only.txt"
    _write(only_file)
    _age(only_file, 365)  # a whole year old, but it's the ONLY file -- still "newest"

    archived = archive_old_logs(str(tmp_path), retention_days=1)

    assert archived == []
    assert only_file.exists()
    assert not (tmp_path / "combat_only.txt.gz").exists()


def test_recent_files_are_left_alone(tmp_path):
    recent = tmp_path / "combat_recent.txt"
    _write(recent)
    _age(recent, 5)
    newest = tmp_path / "combat_newest.txt"
    _write(newest)

    archived = archive_old_logs(str(tmp_path), retention_days=30)

    assert archived == []
    assert recent.exists()


def test_already_archived_gz_files_are_ignored(tmp_path):
    gz_file = tmp_path / "combat_old.txt.gz"
    gz_file.write_bytes(b"already compressed")
    _age(gz_file, 90)
    newest = tmp_path / "combat_newest.txt"
    _write(newest)

    archived = archive_old_logs(str(tmp_path), retention_days=30)

    assert archived == []  # *.txt glob never matches .gz files at all


def test_multiple_old_files_all_get_archived_except_the_newest(tmp_path):
    a = tmp_path / "combat_a.txt"
    b = tmp_path / "combat_b.txt"
    c_newest = tmp_path / "combat_c.txt"
    for p in (a, b, c_newest):
        _write(p)
    _age(a, 60)
    _age(b, 45)
    _age(c_newest, 40)  # oldest by explicit mtime request...
    os.utime(c_newest, None)  # ...but touched last, so it's actually the newest now

    archived = archive_old_logs(str(tmp_path), retention_days=30)

    assert set(archived) == {str(a), str(b)}
    assert c_newest.exists()
    assert not a.exists() and not b.exists()
