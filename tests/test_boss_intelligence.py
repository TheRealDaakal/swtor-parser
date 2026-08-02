"""
Covers a new feature: a boss health + target overlay needs live HP and
"who is the boss currently hitting" data, which BossEncounterState didn't
track before -- phase transitions only ever checked an event's HP fraction
inline (boss_definitions.py's hp_below condition), nothing persisted it.
"""
from boss_definitions import BossDefinition, BossPhase, Condition
from boss_intelligence import BossEncounterState
from conftest import log_line
from log_parser import parse_line


def _boss(name="TestBoss"):
    return BossDefinition(
        id="test_boss", name="Test Boss", boss_names=[name],
        phases=[BossPhase(id="p1", name="Phase 1", start_trigger=Condition(type="combat_start"))],
    )


def _ev(source, target, ability="Slam {1}", effect_name="Damage {2}", amount="",
        source_hp="100000/100000", target_hp="100000/100000"):
    return parse_line(
        log_line("00:00:00.000", source, target=target, ability=ability,
                  effect_name=effect_name, amount=amount,
                  source_hp=source_hp, target_hp=target_hp),
        line_number=1,
    )


def test_hp_is_captured_once_boss_is_recognized():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="900000/1000000"))

    assert state.active_boss is not None
    assert state.boss_hp_current == 900000.0
    assert state.boss_hp_max == 1000000.0
    assert state.boss_hp_percent() == 90.0


def test_hp_updates_across_subsequent_events():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="900000/1000000"))
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="750000/1000000"))

    assert state.boss_hp_current == 750000.0
    assert state.boss_hp_percent() == 75.0


def test_boss_target_tracks_who_the_boss_is_hitting():
    state = BossEncounterState({"test_boss": _boss()})
    # Recognize the boss first (as a target of a player's attack).
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="1000000/1000000"))
    assert state.boss_target is None  # boss hasn't hit anyone yet

    state.feed(_ev("TestBoss", "@Tank#1", ability="Cleave {1}",
                    source_hp="1000000/1000000"))
    assert state.boss_target == "Tank"

    # Tank swap -- target should update to whoever it hits next.
    state.feed(_ev("TestBoss", "@OffTank#2", ability="Cleave {1}",
                    source_hp="1000000/1000000"))
    assert state.boss_target == "OffTank"


def test_recognizing_event_itself_can_seed_target():
    """The exact same log line that first identifies the boss (here, via
    the boss being the SOURCE of an attack on a player) must also seed
    boss_target immediately -- not require a second event, since
    active_boss was still None the first time _update_hp_and_target ran
    within this same feed() call."""
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("TestBoss", "@Tank#1", ability="Cleave {1}",
                    source_hp="950000/1000000"))

    assert state.active_boss is not None
    assert state.boss_target == "Tank"
    # CombatEvent.hp_current/hp_max only ever reflect the TARGET's HP
    # (log_parser never parses the source bracket's HP fraction) -- a line
    # where the boss is the source carries the PLAYER's HP, not the
    # boss's, so no HP reading should come from this one.
    assert state.boss_hp_current is None


def test_non_boss_entities_dont_update_hp_or_target():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="1000000/1000000"))

    # An unrelated trash mob shouldn't overwrite the tracked boss's state.
    state.feed(_ev("@Tank#1", "SomeAdd", target_hp="500/500"))
    state.feed(_ev("SomeAdd", "@Healer#2", ability="Bite {1}", source_hp="500/500"))

    assert state.boss_hp_current == 1000000.0
    assert state.boss_target is None


def test_reset_clears_hp_and_target_between_pulls():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="500000/1000000"))
    state.feed(_ev("TestBoss", "@Tank#1", ability="Cleave {1}", source_hp="500000/1000000"))
    assert state.boss_hp_current is not None
    assert state.boss_target is not None

    state.reset()

    assert state.boss_hp_current is None
    assert state.boss_hp_max is None
    assert state.boss_target is None
    assert state.boss_hp_percent() is None


def test_hp_percent_none_before_any_data():
    state = BossEncounterState({"test_boss": _boss()})
    assert state.boss_hp_percent() is None
