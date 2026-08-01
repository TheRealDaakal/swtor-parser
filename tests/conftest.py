"""
Shared test fixtures. Deliberately self-contained: no dependency on the
developer's own SWTOR CombatLogs folder or %APPDATA%\\swtor-parser data --
this suite has to run in CI on a machine that has neither.

log_line() builds real-shaped SWTOR log lines (verified against this
project's own corpus throughout development) rather than hand-rolling ad
hoc strings per test, so a change to the line FORMAT gets caught the same
place a change to the parsing LOGIC would.
"""
import sys
import time as time_module
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def log_line(
    timestamp: str,
    source: str,
    target: str = "",
    ability: str = "",
    effect_type: str = "ApplyEffect",
    effect_name: str = "",
    amount: str = "",
    source_hp: str = "100000/100000",
    target_hp: str = "100000/100000",
) -> str:
    """One real-shaped SWTOR combat log line. `source`/`target` starting
    with '@' are players (matches source_is_player/target_is_player
    detection); anything else is an NPC. Pass target="" for a self-only
    line (source acting with no target, e.g. AbilityActivate)."""
    src = f"[{source}|(0,0,0,0)|({source_hp})]"
    tgt = f"[{target}|(0,0,0,0)|({target_hp})]" if target else "[]"
    ability_part = f"[{ability}]" if ability else "[]"
    amount_part = f" ({amount})" if amount else ""
    return f"[{timestamp}] {src} {tgt} {ability_part} [{effect_type} {{1}}: {effect_name}]{amount_part}"


@pytest.fixture
def sim_clock(monkeypatch):
    """Monkeypatches time.time() to a controllable value, matching the
    pattern used throughout this project's own verification scripts --
    without it, StatsTracker/TimerEngine compute near-zero durations
    because real wall-clock time barely advances between feed() calls in a
    tight test loop."""
    box = {"t": 0.0}

    def fake_time():
        return box["t"]

    import stats as stats_mod
    import timers as timers_mod
    monkeypatch.setattr(time_module, "time", fake_time)
    monkeypatch.setattr(stats_mod.time, "time", fake_time)
    monkeypatch.setattr(timers_mod.time, "time", fake_time)

    def set_time(t):
        box["t"] = t

    set_time.get = lambda: box["t"]
    return set_time
