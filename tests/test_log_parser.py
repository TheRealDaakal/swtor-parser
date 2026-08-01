"""
Covers the overheal parsing added for the raw/effective healing split, and
a couple of pre-existing parse invariants worth pinning down (crit marker,
environmental damage exclusion) since they're exactly the kind of thing a
future log-format tweak could silently break.
"""
from log_parser import parse_line
from conftest import log_line


def test_overheal_parsed_from_tilde_suffix():
    # Real shape from this project's own corpus: "10926* ~7382" is a
    # 10926-power heal, critical, that only effectively restored 3544 HP.
    line = log_line(
        "20:09:19.158", "@Healer#1", target="@Ally#2",
        ability="Progressive Kolto Scan {1}", effect_name="Heal {2}",
        amount="10926* ~7382",
    )
    ev = parse_line(line, line_number=1)
    assert ev.is_heal
    assert ev.amount == 10926.0
    assert ev.overheal == 7382.0
    assert ev.is_critical is True


def test_zero_overheal_when_suffix_absent():
    line = log_line(
        "20:07:14.850", "@Healer#1", target="@Ally#2",
        ability="Kolto Missile {1}", effect_name="Heal {2}", amount="3526*",
    )
    ev = parse_line(line, line_number=1)
    assert ev.amount == 3526.0
    assert ev.overheal == 0.0


def test_overheal_not_read_for_damage_events():
    """The '~' suffix is heal-specific; a damage event must not pick up a
    stray overheal value even if the tail happens to contain one (guards
    against the extractor being applied unconditionally by a future
    refactor)."""
    line = log_line(
        "20:07:14.850", "@Boss#1", target="@Tank#2",
        ability="Smash {1}", effect_name="Damage {2}", amount="5000 kinetic {1}",
    )
    ev = parse_line(line, line_number=1)
    assert ev.is_damage
    assert ev.overheal == 0.0


def test_environmental_damage_has_no_ability():
    """Fall damage etc. logs is_damage=True with an empty ability field --
    stats.py relies on this to exclude it from damage_done (it isn't an
    attack: no accuracy roll, can't crit, no attacker). Pinned here so a
    parser change can't silently reclassify it as a normal attack."""
    line = f"[20:00:00.000] [@Player#1|(0,0,0,0)|(100/100)] [] [] [ApplyEffect {{1}}: Damage {{2}}] (500)"
    ev = parse_line(line, line_number=1)
    assert ev.is_damage
    assert ev.ability is None
