"""
Covers storage.py's custom-timer-rule persistence -- specifically the
audio_path field added alongside the existing .wav trigger support, since
save/load_timer_rules is a manual field-by-field round-trip (not a generic
dataclass dump), so a new TimerRule field has to be threaded through by
hand on both sides or it silently gets dropped on save/reload.
"""
import json
import time
from datetime import datetime

from timers import TimerRule
from conftest import log_line
from stats import MIN_ENCOUNTER_SECONDS
import storage


def _isolate_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))


def test_audio_path_round_trips_through_save_and_load(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    rules = [
        TimerRule(keyword="Slam", label="Slam", duration_seconds=10.0,
                  audio_path=r"C:\sounds\slam.wav"),
        TimerRule(keyword="Taunt", label="Taunt", duration_seconds=5.0),  # no audio_path
    ]
    storage.save_timer_rules(rules)

    reloaded = storage.load_timer_rules()
    assert reloaded[0].audio_path == r"C:\sounds\slam.wav"
    assert reloaded[1].audio_path is None


def test_cleanup_settings_default_to_disabled(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    settings = storage.load_cleanup_settings()
    assert settings["retention_days"] == 0, "must default to off, not silently archive on a fresh install"
    assert settings["last_run"] is None


def test_cleanup_settings_round_trip(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    storage.save_cleanup_settings({"retention_days": 30, "last_run": "2026-08-03T00:00:00+00:00"})
    reloaded = storage.load_cleanup_settings()
    assert reloaded["retention_days"] == 30
    assert reloaded["last_run"] == "2026-08-03T00:00:00+00:00"


def test_audio_settings_default_to_unmuted(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    settings = storage.load_audio_settings()
    assert settings["muted"] is False
    assert settings["category_muted"] == {"boss": False, "custom": False, "phase": False}


def test_audio_settings_round_trip(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    storage.save_audio_settings({
        "muted": True,
        "category_muted": {"boss": True, "custom": False, "phase": True},
    })
    reloaded = storage.load_audio_settings()
    assert reloaded["muted"] is True
    assert reloaded["category_muted"] == {"boss": True, "custom": False, "phase": True}


def test_audio_settings_merges_partial_saved_categories_with_defaults(monkeypatch, tmp_path):
    """An older save (or a hand-edited file) missing a category key
    shouldn't crash or silently drop the other two -- merge onto the
    current default shape instead."""
    _isolate_appdata(monkeypatch, tmp_path)
    storage._audio_settings_path().write_text('{"muted": false, "category_muted": {"boss": true}}',
                                                encoding="utf-8")
    settings = storage.load_audio_settings()
    assert settings["category_muted"] == {"boss": True, "custom": False, "phase": False}


class TestHistoryRealStartTimeBackfill:
    """load_history() backfills real_start_time (see stats.py's Encounter)
    for entries saved before that field existed, by replaying their
    still-on-disk log file once and matching pulls back by line range --
    see storage._backfill_real_start_times."""

    def _write_pull_log(self, tmp_path, filename="combat_2026-03-05_14_00_00_1234567.txt"):
        end_seconds = 14 * 3600 + MIN_ENCOUNTER_SECONDS + 1.0

        def hms(total):
            return f"{int(total // 3600):02d}:{int(total % 3600 // 60):02d}:{total % 60:06.3f}"

        lines = [
            log_line("14:00:00", "@Dps#1", effect_type="Event", effect_name="EnterCombat {1}"),
            log_line(hms(end_seconds), "@Dps#1", target="Training Dummy",
                     ability="Some Attack {1}", effect_name="Damage {2}", amount="1000"),
            log_line(hms(end_seconds), "@Dps#1", effect_type="Event", effect_name="ExitCombat {1}"),
        ]
        log_path = tmp_path / filename
        log_path.write_text("\n".join(lines) + "\n", encoding="cp1252")
        return str(log_path)

    def _write_legacy_history_entry(self, log_path, start_line=1, end_line=3):
        # Shape a to_dict() would have produced before real_start_time
        # existed -- the key is simply absent, not present-and-null.
        entry = {
            "label": "Old pull", "duration": MIN_ENCOUNTER_SECONDS + 1.0, "players": [],
            "log_path": log_path, "start_line": start_line, "end_line": end_line,
            "area_entered_line": None,
        }
        storage._history_path().write_text(json.dumps([entry]), encoding="utf-8")

    def test_backfills_from_the_still_on_disk_log_file(self, monkeypatch, tmp_path):
        _isolate_appdata(monkeypatch, tmp_path)
        log_path = self._write_pull_log(tmp_path)
        self._write_legacy_history_entry(log_path)

        loaded = storage.load_history()

        expected_midnight = time.mktime(datetime.strptime("2026-03-05", "%Y-%m-%d").timetuple())
        assert loaded[0].real_start_time == expected_midnight + 14 * 3600

    def test_backfilled_value_is_persisted_so_it_only_runs_once(self, monkeypatch, tmp_path):
        _isolate_appdata(monkeypatch, tmp_path)
        log_path = self._write_pull_log(tmp_path)
        self._write_legacy_history_entry(log_path)

        storage.load_history()
        raw = json.loads(storage._history_path().read_text(encoding="utf-8"))
        assert raw[0]["real_start_time"] is not None

    def test_missing_log_file_is_skipped_not_an_error(self, monkeypatch, tmp_path):
        _isolate_appdata(monkeypatch, tmp_path)
        self._write_legacy_history_entry(str(tmp_path / "gone.txt"))
        loaded = storage.load_history()
        assert loaded[0].real_start_time is None

    def test_line_range_that_no_longer_matches_any_replayed_pull_is_skipped(self, monkeypatch, tmp_path):
        _isolate_appdata(monkeypatch, tmp_path)
        log_path = self._write_pull_log(tmp_path)
        self._write_legacy_history_entry(log_path, start_line=99, end_line=101)
        loaded = storage.load_history()
        assert loaded[0].real_start_time is None
