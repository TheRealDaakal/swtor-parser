"""
Covers storage.py's custom-timer-rule persistence -- specifically the
audio_path field added alongside the existing .wav trigger support, since
save/load_timer_rules is a manual field-by-field round-trip (not a generic
dataclass dump), so a new TimerRule field has to be threaded through by
hand on both sides or it silently gets dropped on save/reload.
"""
from timers import TimerRule
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
