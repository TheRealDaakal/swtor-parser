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


def test_targets_death_clears_a_hot_still_ticking_on_it(sim_clock):
    """Same class of live-reported bug as timers.py's TimerEngine.feed()
    fix (a dot kept "ticking" on an NPC well after it died): SWTOR doesn't
    reliably log a RemoveEffect line for every dot/hot at the moment of
    death, so the target's own Death event has to be a second, independent
    clear signal here too -- not just relying on RemoveEffect or the hot's
    own timer running out on its own."""
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")
    assert len(tracker.expiring(now=1.0)) == 1, "sanity check: the hot is tracked before the death"

    _feed(tracker, sim_clock, 2.0, "@Tank#1", "@Tank#1", "", "ApplyEffect", "Death {1}")

    assert tracker.expiring(now=3.0) == [], (
        "the hot must be cleared the instant its target dies, not linger until its own timer runs out"
    )


def test_a_different_targets_death_does_not_clear_unrelated_hots(sim_clock):
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Dps#1",
          "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")

    _feed(tracker, sim_clock, 2.0, "@Tank#1", "@Tank#1", "", "ApplyEffect", "Death {1}")

    rows = tracker.expiring(now=3.0)
    assert len(rows) == 1
    assert rows[0]["target"] == "Dps"


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


def test_same_hot_on_two_simultaneous_targets_tracks_both_independently(sim_clock):
    """A healer's AoE-ish HoT usage (or just healing two people with the
    same spell close together) lands the SAME label on two different
    targets at once -- _active is keyed by (target, label), not label
    alone, so both must stay tracked as separate entries, and refreshing
    one must not touch the other's countdown."""
    tracker = HotTracker()
    _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Tank#1",
          "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")
    _feed(tracker, sim_clock, 3.0, "@Healer#1", "@Dps#2",
          "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")

    rows = tracker.expiring(now=5.0)
    assert len(rows) == 2
    by_target = {r["target"]: r for r in rows}
    assert set(by_target) == {"Tank", "Dps"}
    # Tank's was cast at t=0 (5s elapsed), Dps's at t=3 (2s elapsed) --
    # different remaining times, proving these are two independent
    # countdowns, not one shared entry that a second cast just overwrote.
    assert abs(by_target["Tank"]["remaining"] - 16.0) < 0.01  # 21 - 5
    assert abs(by_target["Dps"]["remaining"] - 19.0) < 0.01   # 21 - 2

    # Refreshing the Tank's HoT must not affect Dps's independent countdown.
    _feed(tracker, sim_clock, 5.0, "@Healer#1", "@Tank#1",
          "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")
    rows = tracker.expiring(now=5.0)
    by_target = {r["target"]: r for r in rows}
    assert abs(by_target["Tank"]["remaining"] - 21.0) < 0.01  # fully refreshed
    assert abs(by_target["Dps"]["remaining"] - 19.0) < 0.01   # untouched

    # Tank's was refreshed at t=5 (expires t=26); Dps's was never refreshed
    # (cast at t=3, expires t=24). At t=25, Dps's must have expired on its
    # own while Tank's -- independently -- is still counting down.
    rows = tracker.expiring(now=25.0)
    assert len(rows) == 1
    assert rows[0]["target"] == "Tank"


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


def test_alacrity_scales_duration_for_affected_definitions(sim_clock):
    """Kolto Probe is_affected_by_alacrity=True (verified against BARAS's
    current source, see the module docstring) -- 20% alacrity must scale
    its 21s nominal duration down to 21/1.2 = 17.5s, matching
    timers.apply_alacrity()'s formula."""
    tracker = HotTracker()
    sim_clock(0.0)
    ev = _feed(tracker, sim_clock, 0.0, "@Healer#1", "@Ally#1",
               "Kolto Probe {1}", "ApplyEffect", "Kolto Probe {1}")
    # _feed() doesn't thread alacrity_pct through -- feed it directly here.
    tracker.feed(ev, local_player_name="Healer", now=0.0, alacrity_pct=20.0)

    rows = tracker.expiring(now=0.0)
    assert len(rows) == 1
    assert abs(rows[0]["remaining"] - 17.5) < 0.001
    assert abs(rows[0]["duration"] - 17.5) < 0.001, (
        "duration (the progress bar's denominator) must reflect the SCALED "
        "value actually used, not the raw nominal one -- otherwise the bar "
        "looks wrong relative to its own countdown"
    )


def test_alacrity_does_not_affect_charge_based_shields(sim_clock):
    """Kolto Shell is_affected_by_alacrity=False -- a charge-based shield
    isn't tick-based, so alacrity_pct must be a complete no-op for it."""
    tracker = HotTracker()
    ev = parse_line(
        log_line("00:00:00.000", "@Healer#1", target="@Tank#1", ability="Kolto Shell {1}",
                  effect_type="ApplyEffect", effect_name="Kolto Shell {1}", amount="7 charges {2}"),
        line_number=1,
    )
    tracker.feed(ev, local_player_name="Healer", now=0.0, alacrity_pct=20.0)

    rows = tracker.expiring(now=0.0)
    assert len(rows) == 1
    assert abs(rows[0]["remaining"] - 180.0) < 0.001, "alacrity must not touch a non-tick-based shield"


def test_register_dots_hots_wires_alacrity_affected_flag_through():
    """register_dots_hots() must copy each DotHotDefinition's
    is_affected_by_alacrity onto the TimerRule it creates -- otherwise the
    flags set on DOTS/HOTS above would be dead data, never reaching
    TimerEngine.feed()'s actual scaling logic."""
    from timers import TimerEngine
    from dots_hots import register_dots_hots, DotHotDefinition

    engine = TimerEngine()
    dots = [DotHotDefinition("Affected Dot", duration_seconds=18.0, is_affected_by_alacrity=True)]
    hots = [DotHotDefinition("Unaffected Hot", duration_seconds=180.0, is_affected_by_alacrity=False)]
    register_dots_hots(engine, dots=dots, hots=hots)

    by_label = {r.label: r for r in engine.rules}
    assert by_label["Affected Dot"].alacrity_affected is True
    assert by_label["Unaffected Hot"].alacrity_affected is False
