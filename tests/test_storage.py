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
