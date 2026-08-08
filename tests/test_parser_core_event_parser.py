from parser_core import parse_event_record
from parser_core.event_kinds import DAMAGE, HEAL, UNKNOWN


def test_parse_event_record_normalizes_damage_kind():
    event = parse_event_record({
        "type": "Damaged",
        "attacker": "Alice",
        "victim": "Boss",
        "damage": 1234,
    })
    assert event.kind == DAMAGE
    assert event.source == "Alice"
    assert event.target == "Boss"
    assert event.amount == 1234


def test_parse_event_record_normalizes_healing_kind():
    event = parse_event_record({
        "event": "Healing",
        "source": "Alice",
        "target": "Bob",
        "value": 900,
    })
    assert event.kind == HEAL
    assert event.amount == 900


def test_parse_event_record_preserves_unknown_records():
    event = parse_event_record({"type": "future_event", "foo": "bar"})
    assert event.kind == UNKNOWN
    assert event.raw["foo"] == "bar"


def test_parse_event_record_is_idempotent_for_normalized_events():
    event = parse_event_record({"type": "damage", "damage": 1})
    assert parse_event_record(event) is event
