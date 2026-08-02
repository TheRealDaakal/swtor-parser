"""
Covers a real live-raid bug: boss alerts silently never firing. Root cause
was in Condition.matches()'s one-time edge-trigger dedup for hp_below and
counter_reaches -- the key was (selector, percent) / (counter_id, value)
only, shared across ANY Condition object with those same values. Two
SEPARATE timer definitions that happen to check the same real threshold
(routine after merging BARAS + ORBS data, since both sources often
independently define a timer for the same moment) collided: whichever
condition got evaluated first on the crossing event "claimed" the shared
key, and the other -- a different Condition object, functionally
identical -- found it already consumed and never fired again that pull,
even though it was never actually checked itself.

Confirmed live: Soa's 75%/30% is_alert=true "Transition incoming" alerts
(chained via timer_expires off an hp_below timer) never fired, because an
earlier, non-alert hp_below timer at the identical threshold silently
stole the one-time trigger first. A full scan of boss_definitions_bundled
found this exact collision in 23 different bosses, 55 colliding key
groups -- this was not a Soa-specific gap.
"""
from boss_definitions import Condition, EvalContext
from log_parser import CombatEvent


def _hp_event(target, hp_current, hp_max):
    return CombatEvent(raw="", timestamp="00:00:00.000", source="@Player#1", target=target,
                       ability="Attack", effect_type="ApplyEffect", effect_name="Damage",
                       amount=100.0, hp_current=hp_current, hp_max=hp_max)


def _ctx(event):
    return EvalContext(
        event=event, boss_names=["Boss"], local_player_name="Player", counters={},
        seen_entities=set(), recently_expired_timer_ids=[], recently_ended_phase_ids=[],
        fired_hp_thresholds=set(), recently_entered_phase_ids=[], active_phase_id=None,
        fired_counter_reaches=set(),
    )


def test_two_separate_hp_below_conditions_at_same_threshold_both_fire():
    """The actual reported bug: two independently-defined timers both
    watching for the boss crossing 75% HP -- e.g. one from BARAS, one
    added later from ORBS -- must each get their own one-time trigger,
    not share a single global slot."""
    cond_a = Condition(type="hp_below", percent=75.0)
    cond_b = Condition(type="hp_below", percent=75.0)  # a SEPARATE definition, same threshold

    ctx = _ctx(_hp_event("Boss", 700, 1000))  # 70% -- below 75%
    # Both share ctx.fired_hp_thresholds, matching how _process_timers
    # evaluates every timer's condition against the SAME context object.
    assert cond_a.matches(ctx) is True
    assert cond_b.matches(ctx) is True, (
        "a second, independent hp_below(75%) condition must fire on its own -- "
        "the first condition firing must not silently consume it"
    )


def test_same_hp_below_condition_only_fires_once_per_pull():
    """The dedup itself must still work for its original purpose: the SAME
    condition object shouldn't re-fire on every subsequent event while HP
    stays below the threshold."""
    cond = Condition(type="hp_below", percent=75.0)
    ctx = _ctx(_hp_event("Boss", 700, 1000))

    assert cond.matches(ctx) is True
    assert cond.matches(ctx) is False, "the same condition must not re-fire for the same crossing"
    assert cond.matches(ctx) is False


def test_two_separate_counter_reaches_conditions_at_same_value_both_fire():
    cond_a = Condition(type="counter_reaches", counter_id="adds", value=3)
    cond_b = Condition(type="counter_reaches", counter_id="adds", value=3)

    ev = _hp_event("Boss", 1000, 1000)
    ctx = EvalContext(
        event=ev, boss_names=["Boss"], local_player_name="Player", counters={"adds": 3},
        seen_entities=set(), recently_expired_timer_ids=[], recently_ended_phase_ids=[],
        fired_hp_thresholds=set(), recently_entered_phase_ids=[], active_phase_id=None,
        fired_counter_reaches=set(),
    )
    assert cond_a.matches(ctx) is True
    assert cond_b.matches(ctx) is True, (
        "a second, independent counter_reaches condition at the same value must fire on its own"
    )


def test_different_hp_below_thresholds_are_independent_regardless():
    """Sanity check the fix didn't accidentally over-scope: two conditions
    at DIFFERENT percentages were never supposed to collide in the first
    place, and still shouldn't."""
    cond_75 = Condition(type="hp_below", percent=75.0)
    cond_30 = Condition(type="hp_below", percent=30.0)

    ctx = _ctx(_hp_event("Boss", 250, 1000))  # 25% -- below both
    assert cond_75.matches(ctx) is True
    assert cond_30.matches(ctx) is True
