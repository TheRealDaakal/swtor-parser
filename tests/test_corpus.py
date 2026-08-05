"""
Covers analysis/corpus.py's replay_pulls() reconstructing a real calendar
date for each pull from the log file's own SWTOR-style filename -- powers
the History tab's Date column (Encounter.real_start_time).
"""
import time
from datetime import datetime

from analysis.corpus import replay_pulls
from conftest import log_line
from stats import MIN_ENCOUNTER_SECONDS


def _write_log(tmp_path, filename, lines):
    path = tmp_path / filename
    path.write_text("\n".join(lines) + "\n", encoding="cp1252")
    return str(path)


def _pull_lines(start_hms: str, tick_seconds: float = MIN_ENCOUNTER_SECONDS + 1.0):
    """EnterCombat, a damage tick tick_seconds later (real duration, not a
    sliver), then ExitCombat -- the minimum shape replay_pulls() needs to
    keep a pull instead of discarding it as too short."""
    h, m, s = start_hms.split(":")
    start_seconds = int(h) * 3600 + int(m) * 60 + float(s)
    end_seconds = start_seconds + tick_seconds

    def hms(total):
        return f"{int(total // 3600):02d}:{int(total % 3600 // 60):02d}:{total % 60:06.3f}"

    return [
        log_line(start_hms, "@Dps#1", effect_type="Event", effect_name="EnterCombat {1}"),
        log_line(hms(end_seconds), "@Dps#1", target="Training Dummy",
                 ability="Some Attack {1}", effect_name="Damage {2}", amount="1000"),
        log_line(hms(end_seconds), "@Dps#1", effect_type="Event", effect_name="ExitCombat {1}"),
    ]


class TestReplayPullsRealStartTime:
    def test_reconstructs_the_real_date_from_the_swtor_filename(self, tmp_path):
        path = _write_log(
            tmp_path, "combat_2026-03-05_14_00_00_1234567.txt",
            _pull_lines("14:00:00"),
        )
        pulls = replay_pulls(path, {})
        assert len(pulls) == 1
        encounter = pulls[0]["encounter"]
        expected_midnight = time.mktime(datetime.strptime("2026-03-05", "%Y-%m-%d").timetuple())
        assert encounter.real_start_time == expected_midnight + 14 * 3600

    def test_a_second_pull_later_in_the_file_gets_its_own_offset_date(self, tmp_path):
        lines = _pull_lines("14:00:00") + _pull_lines("15:30:00")
        path = _write_log(tmp_path, "combat_2026-03-05_14_00_00_1234567.txt", lines)
        pulls = replay_pulls(path, {})
        assert len(pulls) == 2
        expected_midnight = time.mktime(datetime.strptime("2026-03-05", "%Y-%m-%d").timetuple())
        assert pulls[0]["encounter"].real_start_time == expected_midnight + 14 * 3600
        assert pulls[1]["encounter"].real_start_time == expected_midnight + 15.5 * 3600

    def test_unrecognized_filename_leaves_real_start_time_unset(self, tmp_path):
        """Never invent a date from a file that doesn't match SWTOR's own
        naming convention (e.g. a renamed/old file) -- see
        Encounter.apply()'s real_time semantics."""
        path = _write_log(tmp_path, "my_renamed_log.txt", _pull_lines("14:00:00"))
        pulls = replay_pulls(path, {})
        assert len(pulls) == 1
        assert pulls[0]["encounter"].real_start_time is None
