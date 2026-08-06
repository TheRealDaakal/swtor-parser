"""
The aggro warning is deliberately disabled -- see aggro_tracker.py's module
docstring for the measurements. These tests replace the old behavioural
ones (which asserted a threshold-crossing warning that could never be
correct) and exist to stop it being quietly switched back on.

Reported live: "kept getting alerts that everybody was pulling aggro -- not
everybody can pull aggro at one time."
"""
from aggro_tracker import AggroTracker


class FakeTimerEngine:
    def __init__(self):
        self.started = []

    def start_timer(self, label, duration, **kwargs):
        self.started.append(label)


class FakePlayer:
    def __init__(self, threat, is_player=True):
        self.threat = threat
        self.is_player = is_player

    def tps(self, duration):
        return self.threat / duration if duration > 0 else 0.0


def test_it_never_fires_even_when_the_old_threshold_would_have():
    """The exact shape that used to warn: a DPS at 95% of the holder's
    threat. SWTOR's log can't actually establish either number (only 2.2%
    of threat-relevant events are logged at all), so no warning is honest
    here."""
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {"Tank": FakePlayer(1000.0), "Dps": FakePlayer(950.0)}
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)
    assert engine.started == []


def test_it_never_fires_for_a_whole_raid_at_once():
    """The reported symptom. It happened because boss_target tracked
    whoever the boss last HIT -- which on a cleaving boss is everyone (828
    changes across 8 players in one real pull) -- so whenever it landed on
    a low-threat player, every other player cleared the ratio at once."""
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {f"P{i}": FakePlayer(1000.0) for i in range(8)}
    players["Unlucky"] = FakePlayer(1.0)          # boss cleaved a healer
    tracker.check(players, boss_target="Unlucky", duration=10.0, timer_engine=engine)
    assert engine.started == [], "must not warn about the entire raid"


def test_negative_threat_totals_produce_no_warning():
    """Real measured state: every player finished a real Styrak pull with
    NEGATIVE threat on the boss, because only threat DROPS are logged."""
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {"Tank": FakePlayer(-54_668.0), "Dps": FakePlayer(-6_317_238.0)}
    tracker.check(players, boss_target="Tank", duration=100.0, timer_engine=engine)
    assert engine.started == []


def test_reset_is_safe_to_call():
    """main.py still calls this on every pull rollover."""
    AggroTracker().reset()
