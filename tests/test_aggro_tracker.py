"""
Covers aggro_tracker.py -- flagging a DPS/healer approaching the boss's
current target's threat. No role detection: boss_target (whoever the
boss is currently attacking, tracked in boss_intelligence.py from the
log's own events) IS the reference point, so these tests build PlayerStats
directly rather than a full BossEncounterState.
"""
from aggro_tracker import AggroTracker
from stats import PlayerStats


class FakeTimerEngine:
    def __init__(self):
        self.started = []

    def start_timer(self, label, duration, **kwargs):
        self.started.append((label, duration, kwargs))


def _player(name, threat, is_player=True):
    return PlayerStats(name=name, threat=threat, is_player=is_player)


def test_a_dps_at_90_percent_of_the_tanks_threat_fires_a_warning():
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {
        "Tank": _player("Tank", threat=1000.0),
        "Dps": _player("Dps", threat=920.0),  # 92% -- past the 90% threshold
    }
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)

    assert len(engine.started) == 1
    label, duration, kwargs = engine.started[0]
    assert "Dps" in label
    assert kwargs["is_alert"] is True
    assert kwargs["voice_alert"] is True


def test_a_dps_well_under_the_threshold_does_not_fire():
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {
        "Tank": _player("Tank", threat=1000.0),
        "Dps": _player("Dps", threat=500.0),  # 50% -- nowhere close
    }
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)

    assert engine.started == []


def test_the_aggro_holder_itself_is_never_warned_about_their_own_threat():
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {"Tank": _player("Tank", threat=1000.0)}
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)

    assert engine.started == []


def test_it_only_fires_once_per_crossing_not_every_tick():
    """A player parked at 95% for ten consecutive ticks must only hear
    the warning once, not once per tick -- edge-triggered, same pattern
    as boss_definitions.Condition's hp_below."""
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {
        "Tank": _player("Tank", threat=1000.0),
        "Dps": _player("Dps", threat=950.0),
    }
    for _ in range(10):
        tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)

    assert len(engine.started) == 1


def test_it_re_fires_after_dropping_below_reset_and_climbing_back_up():
    """A real threat-dump-then-build-back-up sequence must warn again --
    the dead zone between APPROACH_THRESHOLD and RESET_THRESHOLD exists so
    hovering right at the edge doesn't spam, not so a genuine second
    approach gets silently swallowed."""
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    tank = _player("Tank", threat=1000.0)
    dps = _player("Dps", threat=950.0)
    players = {"Tank": tank, "Dps": dps}

    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)
    assert len(engine.started) == 1

    dps.threat = 600.0  # threat dump -- drops well below RESET_THRESHOLD
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)
    assert len(engine.started) == 1  # dropping doesn't itself fire anything

    dps.threat = 960.0  # climbs back up past APPROACH_THRESHOLD again
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)
    assert len(engine.started) == 2


def test_reset_clears_state_between_pulls():
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {
        "Tank": _player("Tank", threat=1000.0),
        "Dps": _player("Dps", threat=950.0),
    }
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)
    assert len(engine.started) == 1

    tracker.reset()
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)
    assert len(engine.started) == 2, "a new pull must not inherit the last pull's fired state"


def test_no_boss_target_yet_is_a_safe_no_op():
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {"Dps": _player("Dps", threat=500.0)}
    tracker.check(players, boss_target=None, duration=10.0, timer_engine=engine)
    assert engine.started == []


def test_non_player_entities_are_never_warned():
    """players dict can contain NPCs too (Encounter.players tracks every
    entity seen, not just real players) -- an add generating threat must
    never trigger a "pulling aggro" callout about itself."""
    tracker = AggroTracker()
    engine = FakeTimerEngine()
    players = {
        "Tank": _player("Tank", threat=1000.0),
        "Add": _player("Add", threat=950.0, is_player=False),
    }
    tracker.check(players, boss_target="Tank", duration=10.0, timer_engine=engine)
    assert engine.started == []
