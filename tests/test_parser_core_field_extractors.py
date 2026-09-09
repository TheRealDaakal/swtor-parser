from parser_core.field_extractors import (
    _clean_name, _extract_amount, _extract_damage_type, _extract_id,
    _extract_is_critical, _extract_overheal, _extract_shield_absorbed,
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

def test_extract_damage_type_plain_hit():
    assert _extract_damage_type("(1234 energy {1})") == "energy"

def test_extract_damage_type_crit_hit():
    assert _extract_damage_type("(9578* elemental {1})") == "elemental"

def test_extract_damage_type_all_four_real_values():
    for t in ("kinetic", "energy", "elemental", "internal"):
        assert _extract_damage_type(f"(500 {t} {{1}})") == t

def test_extract_damage_type_none_for_an_avoided_attack():
    # No type word at all when the hit never landed -- just the avoidance
    # marker right after the amount.
    assert _extract_damage_type("(0 -dodge {1})") is None

def test_extract_damage_type_none_for_a_heal():
    assert _extract_damage_type("(10926* ~7382)") is None

def test_extract_damage_type_skips_an_extra_tilde_token():
    # Real example: an absorbed hit sometimes carries a "~N" token between
    # the amount and the type word that a first version of this regex
    # didn't account for, silently returning None for it.
    assert _extract_damage_type(
        "(44833 ~0 kinetic {836045448940873} (6411 absorbed {836045448945511}))"
    ) == "kinetic"
