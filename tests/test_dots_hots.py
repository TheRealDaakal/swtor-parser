"""
Covers a live report: Kolto Shell (and Trauma Probe, same mechanic) is a
charge-based shield -- 7 charges for this user's build, confirmed against
their own log corpus -- that disappears once its charges run out even if
its nominal 180s duration hasn't elapsed. Each absorbed hit logs its own
"[ModifyCharges {id}: Kolto Shell {id}] (N charges {id})" line, separate
from the ApplyEffect that cast it. HotTracker used to only understand
ApplyEffect/RemoveEffect, so the "HoTs expiring" overlay showed a flat
wall-clock countdown from 180s regardless of how many charges were left --
a heavily-tanked shield down to its last charge still looked nearly full
right up until it vanished. Losing a charge now also costs displayed time,
proportional to the shield's share-per-charge (duration / max_charges), so
the bar visibly drains as charges deplete instead of only reacting at the
very end.
"""
from conftest import log_line
from dots_hots import HotTracker
from log_parser import parse_line


def _feed(tracker, sim_clock, t, source, target, ability, effect_type, effect_name, amount=""):
    sim_clock(t)
    ts = f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"
    ev = parse_line(
        log_line(ts, source, target=target, ability=ability,
                  effect_type=effect_type, effect_name=effect_name, amount=amount),
        line_number=1,
    )
    tracker.feed(ev, local_player_name="Healer")
    return ev


def test_charge_shield_counts_down_normally_with_no_hits(sim_clock):
    """No ModifyCharges lines at all -- should behave exactly like the old
    plain wall-clock countdown."""
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "ApplyEffect", "Kolto Shell {1}", amount="7 charges {2}")

    sim_clock(10.0)
    rows = tracker.expiring(now=10.0)
    assert len(rows) == 1
    assert rows[0]["target"] == "Tank"
    assert abs(rows[0]["remaining"] - 170.0) < 0.01


def test_losing_charges_drains_remaining_time_faster_than_wall_clock(sim_clock):
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "ApplyEffect", "Kolto Shell {1}", amount="7 charges {2}")

    # Tank gets hit hard and fast -- 4 of the 7 charges gone in the first
    # few seconds, well before any meaningful wall-clock time has passed.
    for remaining_charges in (6, 5, 4, 3):
        _feed(tracker, sim_clock, 2.0, "@Healer#1", "@Tank#1",
              "Kolto Shell {1}", "ModifyCharges", "Kolto Shell {1}",
              amount=f"{remaining_charges} charges {{2}}")

    rows = tracker.expiring(now=2.0)
    assert len(rows) == 1
    # Wall-clock alone would show ~178s left (180 - 2). 4 charges lost out
    # of 7, each worth 180/7 =~25.71s, should cost ~102.86s off the top.
    expected = (180.0 - 2.0) - (180.0 / 7.0) * 4
    assert abs(rows[0]["remaining"] - expected) < 0.01
    assert rows[0]["remaining"] < 178.0 - 90.0, (
        "losing charges should drain displayed time well below the pure "
        "wall-clock countdown, not just track elapsed seconds"
    )
    # duration stays the full nominal value -- that's what makes the
    # progress bar itself visibly shrink (remaining/duration), not just
    # the raw number.
    assert rows[0]["duration"] == 180.0


def test_running_out_of_charges_before_the_clock_expires_it_anyway(sim_clock):
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "ApplyEffect", "Kolto Shell {1}", amount="7 charges {2}")

    for remaining_charges in (6, 5, 4, 3, 2, 1):
        _feed(tracker, sim_clock, 5.0, "@Healer#1", "@Tank#1",
              "Kolto Shell {1}", "ModifyCharges", "Kolto Shell {1}",
              amount=f"{remaining_charges} charges {{2}}")

    # 6 charges lost * (180/7) =~154.3s penalty, on top of only 5s elapsed
    # -- the estimate should already read as expired/nearly gone even
    # though the real RemoveEffect hasn't fired yet.
    rows = tracker.expiring(now=5.0)
    assert rows == [] or rows[0]["remaining"] < 25.0


def test_remove_effect_still_clears_it_regardless_of_charges_tracked(sim_clock):
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "ApplyEffect", "Kolto Shell {1}", amount="7 charges {2}")
    _feed(tracker, sim_clock, 1.0, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "ModifyCharges", "Kolto Shell {1}", amount="6 charges {2}")
    _feed(tracker, sim_clock, 1.5, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "RemoveEffect", "Kolto Shell {1}")

    assert tracker.expiring(now=2.0) == []


def test_reapplying_resets_both_the_clock_and_the_charge_penalty(sim_clock):
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "ApplyEffect", "Kolto Shell {1}", amount="7 charges {2}")
    for remaining_charges in (6, 5, 4):
        _feed(tracker, sim_clock, 1.0, "@Healer#1", "@Tank#1",
              "Kolto Shell {1}", "ModifyCharges", "Kolto Shell {1}",
              amount=f"{remaining_charges} charges {{2}}")

    # A fresh cast (e.g. re-shielded after it fully dropped and got put
    # back up) should restore the full 180s AND clear the old charge
    # penalty, not carry the drained state forward.
    _feed(tracker, sim_clock, 3.0, "@Healer#1", "@Tank#1",
          "Kolto Shell {1}", "ApplyEffect", "Kolto Shell {1}", amount="7 charges {2}")

    rows = tracker.expiring(now=3.0)
    assert len(rows) == 1
    assert abs(rows[0]["remaining"] - 180.0) < 0.01


def test_non_charge_hot_is_unaffected(sim_clock):
    """A plain time-based HoT (no max_charges) must ignore ModifyCharges
    entirely and just be a normal countdown -- this mechanic is specific to
    the two charge-shield abilities, not every HoT."""
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Ally#1",
          "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")

    rows = tracker.expiring(now=5.0)
    assert len(rows) == 1
    assert abs(rows[0]["remaining"] - 16.0) < 0.01  # 21s duration - 5s elapsed
