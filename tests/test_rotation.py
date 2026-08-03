"""
Tests for stats.rotation_segments -- the Rotation Viewer's core: splits a
pull's raw log lines into segments bounded by every occurrence of a keyword,
and reports one player's own cast sequence within each segment.
"""
from stats import rotation_segments
from conftest import log_line


def _ts(t):
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"


def test_splits_into_segments_between_keyword_occurrences_and_reports_only_that_player():
    lines = [
        # Boundary 1: boss casts "Creeping Terror" at t=0
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        # Segment 1 (0..10): the player we care about lands two hits
        log_line(_ts(2.0), "@Voidkeeper#1", target="Watchdog", ability="Saber Strike {1}",
                 effect_name="Damage {2}", amount="1500"),
        log_line(_ts(6.0), "@Voidkeeper#1", target="Watchdog", ability="Slash {1}",
                 effect_name="Damage {2}", amount="1800*"),
        # A different player's hit in the same window must NOT show up in
        # Voidkeeper's segment.
        log_line(_ts(7.0), "@Emberlash#2", target="Watchdog", ability="Shock {1}",
                 effect_name="Damage {2}", amount="900"),
        # Boundary 2: t=10
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        # Segment 2 (10..15): one hit
        log_line(_ts(12.0), "@Voidkeeper#1", target="Watchdog", ability="Saber Strike {1}",
                 effect_name="Damage {2}", amount="1000"),
        # Boundary 3: t=15 -- closes segment 2
        log_line(_ts(15.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]

    segments = rotation_segments(lines, "Voidkeeper", "Creeping Terror")

    assert len(segments) == 2

    seg1 = segments[0]
    assert seg1["duration"] == 10.0
    assert [c["ability"] for c in seg1["casts"]] == ["Saber Strike", "Slash"]
    assert seg1["casts"][0]["is_critical"] is False
    assert seg1["casts"][1]["is_critical"] is True  # "1800*" -- asterisk marks a crit
    assert seg1["dps"] == round((1500 + 1800) / 10.0, 1)
    assert seg1["crit_pct"] == 50.0  # 1 of 2 landed hits crit

    seg2 = segments[1]
    assert seg2["duration"] == 5.0
    assert [c["ability"] for c in seg2["casts"]] == ["Saber Strike"]
    assert seg2["dps"] == round(1000 / 5.0, 1)


def test_keyword_matching_is_case_insensitive_substring():
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        log_line(_ts(5.0), "@Voidkeeper#1", target="Watchdog", ability="Saber Strike {1}",
                 effect_name="Damage {2}", amount="1000"),
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]
    segments = rotation_segments(lines, "Voidkeeper", "creeping")
    assert len(segments) == 1
    assert segments[0]["casts"][0]["amount"] == 1000


def test_fewer_than_two_occurrences_returns_no_segments():
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        log_line(_ts(5.0), "@Voidkeeper#1", target="Watchdog", ability="Saber Strike {1}",
                 effect_name="Damage {2}", amount="1000"),
    ]
    assert rotation_segments(lines, "Voidkeeper", "Creeping Terror") == []
    assert rotation_segments(lines, "Voidkeeper", "nonexistent keyword") == []


def test_heals_are_tallied_separately_as_ehps_and_flagged():
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        log_line(_ts(5.0), "@Emily#3", target="@Voidkeeper#1", ability="Kolto Missile {1}",
                 effect_name="Heal {2}", amount="2000"),
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]
    segments = rotation_segments(lines, "Emily", "Creeping Terror")
    assert len(segments) == 1
    assert segments[0]["ehps"] == 200.0  # 2000 / 10s
    assert segments[0]["dps"] == 0.0
    assert segments[0]["casts"][0]["is_heal"] is True


def _activate(t, source, ability):
    return log_line(_ts(t), source, ability=ability, effect_name="AbilityActivate {1}")


def test_casts_are_tagged_with_kind_cast():
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        log_line(_ts(5.0), "@Voidkeeper#1", target="Watchdog", ability="Saber Strike {1}",
                 effect_name="Damage {2}", amount="1000"),
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]
    segments = rotation_segments(lines, "Voidkeeper", "Creeping Terror")
    assert segments[0]["casts"][0]["kind"] == "cast"


def test_a_long_idle_window_is_flagged_as_a_gap():
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        _activate(1.0, "@Voidkeeper#1", "Saber Strike {1}"),
        # A real rotation gap: nothing activated for 5s (base GCD is 1.5s,
        # threshold is 1.5x that = 2.25s -- 5s comfortably clears it).
        _activate(6.0, "@Voidkeeper#1", "Slash {1}"),
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]
    segments = rotation_segments(lines, "Voidkeeper", "Creeping Terror")
    assert len(segments) == 1
    gaps = [c for c in segments[0]["casts"] if c["kind"] == "gap"]
    assert len(gaps) == 1
    assert abs(gaps[0]["seconds"] - 5.0) < 0.01
    assert abs(segments[0]["idle_seconds"] - 5.0) < 0.01


def test_normal_gcd_paced_activations_are_not_flagged():
    """Back-to-back casts at roughly the GCD's own pace (1.5s apart, base
    GCD) must NOT be flagged -- that's just normal play, not idle time."""
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        _activate(1.0, "@Voidkeeper#1", "Ability A {1}"),
        _activate(2.5, "@Voidkeeper#1", "Ability B {1}"),
        _activate(4.0, "@Voidkeeper#1", "Ability C {1}"),
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]
    segments = rotation_segments(lines, "Voidkeeper", "Creeping Terror")
    gaps = [c for c in segments[0]["casts"] if c["kind"] == "gap"]
    assert gaps == []
    assert segments[0]["idle_seconds"] == 0.0


def test_alacrity_shrinks_the_gap_detection_threshold():
    """A 2.0s gap sits BELOW the base-GCD threshold (1.5 * 1.5 = 2.25s) but
    ABOVE the 20%-alacrity threshold (1.5/1.2 * 1.5 = 1.875s) -- so it must
    only be flagged once alacrity_pct is actually passed through."""
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        _activate(1.0, "@Voidkeeper#1", "Ability A {1}"),
        _activate(3.0, "@Voidkeeper#1", "Ability B {1}"),  # exactly a 2.0s gap
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]
    unscaled = rotation_segments(lines, "Voidkeeper", "Creeping Terror", alacrity_pct=0.0)
    assert [c for c in unscaled[0]["casts"] if c["kind"] == "gap"] == []

    scaled = rotation_segments(lines, "Voidkeeper", "Creeping Terror", alacrity_pct=20.0)
    gaps = [c for c in scaled[0]["casts"] if c["kind"] == "gap"]
    assert len(gaps) == 1
    assert abs(gaps[0]["seconds"] - 2.0) < 0.01


def test_gap_entries_are_ordered_correctly_relative_to_casts():
    """A gap detected between two activations must be interleaved at the
    right point in the timeline, not just appended at the end."""
    lines = [
        log_line(_ts(0.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
        _activate(1.0, "@Voidkeeper#1", "Ability A {1}"),
        log_line(_ts(1.2), "@Voidkeeper#1", target="Watchdog", ability="Ability A {1}",
                 effect_name="Damage {2}", amount="500"),
        # 6s idle gap
        _activate(7.0, "@Voidkeeper#1", "Ability B {1}"),
        log_line(_ts(7.2), "@Voidkeeper#1", target="Watchdog", ability="Ability B {1}",
                 effect_name="Damage {2}", amount="500"),
        log_line(_ts(10.0), "Watchdog", ability="Creeping Terror {1}", effect_name="AbilityActivate {1}"),
    ]
    segments = rotation_segments(lines, "Voidkeeper", "Creeping Terror")
    kinds = [c["kind"] for c in segments[0]["casts"]]
    assert kinds == ["cast", "gap", "cast"], f"expected cast, then the gap, then the next cast -- got {kinds}"
