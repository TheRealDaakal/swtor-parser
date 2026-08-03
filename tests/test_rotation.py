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
