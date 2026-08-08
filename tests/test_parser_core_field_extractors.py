from parser_core.field_extractors import (
    _clean_name, _extract_amount, _extract_id, _extract_is_critical,
    _extract_overheal, _extract_shield_absorbed,
)

def test_clean_name_handles_entity_id():
    assert _clean_name("Alice {123}") == "Alice"

def test_extract_amount_uses_first_parenthesized_number():
    assert _extract_amount("(12345 energy {1})") == 12345

def test_extract_id_handles_ability_id():
    assert _extract_id("Force Leap {12345}") == "12345"

def test_extract_is_critical_handles_star_marker():
    assert _extract_is_critical("(9578* energy {1})") is True

def test_extract_overheal_handles_tilde_suffix():
    assert _extract_overheal("(10926* ~7382)") == 7382

def test_extract_shield_absorbed_handles_shield_annotation():
    assert _extract_shield_absorbed(
        "(3238 energy {1} -shield {2} (4121 absorbed {3}))"
    ) == 4121
