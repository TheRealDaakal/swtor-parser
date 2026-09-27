"""
Covers resource_tracker.py -- net Spend/Restore flow for the local player,
since SWTOR's log only ever gives deltas, never an absolute current/max
value (see the module docstring). All timing is driven by explicit `now=`
arguments rather than the sim_clock fixture, since ResourceTracker takes an
explicit `now` everywhere instead of reading time.time() itself.
"""
from resource_tracker import ResourceTracker, RATE_WINDOW_SECONDS


class _Event:
    def __init__(self, source, is_resource_spend=False, is_resource_restore=False,
                 resource_type=None, resource_amount=0.0):
        self.source = source
        self.is_resource_spend = is_resource_spend
        self.is_resource_restore = is_resource_restore
        self.resource_type = resource_type
        self.resource_amount = resource_amount


def _spend(source, amount, resource_type="energy"):
    return _Event(source, is_resource_spend=True, resource_type=resource_type,
                  resource_amount=amount)


def _restore(source, amount, resource_type="energy"):
    return _Event(source, is_resource_restore=True, resource_type=resource_type,
                  resource_amount=amount)


def test_ignores_events_from_other_players():
    tracker = ResourceTracker()
    tracker.feed(_spend("SomeoneElse", 20.0), local_player_name="Me", now=0.0)
    snap = tracker.snapshot(now=0.0)
    assert snap["resource_type"] is None
    assert snap["spent_total"] == 0.0


def test_ignores_non_resource_events():
    tracker = ResourceTracker()
    event = _Event("Me")  # neither spend nor restore
    tracker.feed(event, local_player_name="Me", now=0.0)
    assert tracker.snapshot(now=0.0)["resource_type"] is None


def test_net_is_restored_minus_spent():
    tracker = ResourceTracker()
    tracker.feed(_spend("Me", 30.0), local_player_name="Me", now=0.0)
    tracker.feed(_restore("Me", 12.0), local_player_name="Me", now=1.0)
    snap = tracker.snapshot(now=1.0)
    assert snap["spent_total"] == 30.0
    assert snap["restored_total"] == 12.0
    assert snap["net"] == -18.0
    assert snap["resource_type"] == "energy"


def test_resource_type_tracks_the_most_recent_event():
    """A stance/form swap mid-session can change which pool is active --
    always reflect whatever the MOST RECENT event says."""
    tracker = ResourceTracker()
    tracker.feed(_spend("Me", 10.0, resource_type="rage point"), local_player_name="Me", now=0.0)
    tracker.feed(_spend("Me", 5.0, resource_type="Force"), local_player_name="Me", now=1.0)
    assert tracker.snapshot(now=1.0)["resource_type"] == "Force"


def test_spend_rate_averages_over_the_trailing_window():
    tracker = ResourceTracker()
    tracker.feed(_spend("Me", 10.0), local_player_name="Me", now=0.0)
    tracker.feed(_spend("Me", 10.0), local_player_name="Me", now=1.0)
    # 20 spent over a 1s span at the point of the second spend -- rate is
    # measured over max(now - earliest_spend, 1.0) seconds.
    assert tracker.spend_rate(now=1.0) == 20.0


def test_spend_rate_drops_old_spends_outside_the_window():
    tracker = ResourceTracker()
    tracker.feed(_spend("Me", 50.0), local_player_name="Me", now=0.0)
    # Well past RATE_WINDOW_SECONDS later, that spend should no longer
    # count toward the "recent" rate.
    rate = tracker.spend_rate(now=RATE_WINDOW_SECONDS + 5.0)
    assert rate == 0.0


def test_reset_clears_everything():
    tracker = ResourceTracker()
    tracker.feed(_spend("Me", 30.0), local_player_name="Me", now=0.0)
    tracker.feed(_restore("Me", 12.0), local_player_name="Me", now=1.0)
    tracker.reset()
    snap = tracker.snapshot(now=1.0)
    assert snap == {
        "resource_type": None, "net": 0.0, "spent_total": 0.0,
        "restored_total": 0.0, "spend_rate_per_sec": 0.0,
    }


def test_none_local_player_name_is_ignored_not_a_crash():
    """Before the local player is identified from the log yet (boss_state.
    local_player_name is None at startup) -- must not attribute every
    player's resource use to "no one" instead of silently doing nothing."""
    tracker = ResourceTracker()
    tracker.feed(_spend("Someone", 10.0), local_player_name=None, now=0.0)
    assert tracker.snapshot(now=0.0)["resource_type"] is None
