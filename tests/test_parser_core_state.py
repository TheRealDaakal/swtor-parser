from parser_core.state import CombatState


def test_combat_state_lifecycle():
    state = CombatState()
    assert not state.active

    state.start(100, "Test Boss")
    assert state.active
    assert state.started_at == 100
    assert state.encounter_name == "Test Boss"

    state.stop(200)
    assert not state.active
    assert state.ended_at == 200


def test_combat_state_can_store_players_and_metadata():
    state = CombatState()
    state.players["Alice"] = {"role": "DPS"}
    state.metadata["source"] = "fixture"
    assert state.players["Alice"]["role"] == "DPS"
    assert state.metadata["source"] == "fixture"
