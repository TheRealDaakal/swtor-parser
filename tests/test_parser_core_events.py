from parser_core.events import NormalizedEvent, normalize_event


def test_normalize_common_damage_aliases():
    event = normalize_event({
        "event": "damage",
        "time": 123,
        "attacker": "Alice",
        "victim": "Boss",
        "ability_name": "Snipe",
        "damage": 4200,
    })
    assert isinstance(event, NormalizedEvent)
    assert event.kind == "damage"
    assert event.timestamp == 123
    assert event.source == "Alice"
    assert event.target == "Boss"
    assert event.ability == "Snipe"
    assert event.amount == 4200


def test_normalize_does_not_mutate_input():
    source = {"type": "heal", "source": "Alice", "target": "Bob", "value": 100}
    before = dict(source)
    normalize_event(source)
    assert source == before
