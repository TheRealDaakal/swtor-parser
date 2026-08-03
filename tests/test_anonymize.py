"""
Covers anonymize.py -- scrubbing player-identifying @Name#accountid tokens
from a raw combat log before it's shared. Real log lines used below are
shaped exactly like actual SWTOR output (confirmed against a real log
during development, including a real name containing a space: "Hawt
Sauce" -- an earlier, wrong assumption that names never contain spaces
would have broken the regex on exactly this kind of real data).
"""
from anonymize import anonymize_lines


def test_a_standalone_player_token_is_replaced():
    line = "[06:40:02.071] [@Hawt Sauce#689852070183169|(-4703.55,-4811.02,764.81,42.95)|(1/121913)] [] [] [AreaEntered {836045448953664}: Republic Fleet {137438989514}] (he3000) <v7.0.0b>\n"
    scrubbed, name_map = anonymize_lines([line])
    assert "@Hawt Sauce#689852070183169" not in scrubbed[0]
    assert "Republic Fleet" in scrubbed[0], "NPC/zone names must stay untouched"
    assert scrubbed[0].count("@Player1#") == 1


def test_the_same_real_player_gets_the_same_placeholder_every_time():
    lines = [
        "[00:00:00.000] [@Voidkeeper#111|(0,0,0,0)|(100/100)] [] [Ability {1}] [AbilityActivate {1}: X {1}]\n",
        "[00:00:01.000] [@Voidkeeper#111|(0,0,0,0)|(100/100)] [Boss|(0,0,0,0)|(1000/1000)] [Ability {1}] [Damage {2}: X {1}] (500)\n",
    ]
    scrubbed, name_map = anonymize_lines(lines)
    assert len(name_map) == 1, "one real player across two lines must map to exactly one placeholder"
    placeholder = name_map["@Voidkeeper#111"]
    assert placeholder in scrubbed[0]
    assert placeholder in scrubbed[1]


def test_different_players_get_different_placeholders_in_first_seen_order():
    lines = [
        "[00:00:00.000] [@Alice#111|(0,0,0,0)|(100/100)] [] [Ability {1}] [AbilityActivate {1}: X {1}]\n",
        "[00:00:01.000] [@Bob#222|(0,0,0,0)|(100/100)] [] [Ability {1}] [AbilityActivate {1}: X {1}]\n",
    ]
    scrubbed, name_map = anonymize_lines(lines)
    assert name_map["@Alice#111"] == "@Player1#00000001"
    assert name_map["@Bob#222"] == "@Player2#00000002"


def test_a_pet_owner_token_is_scrubbed_without_touching_the_pets_own_name():
    line = "[00:00:00.000] [@Owner#333/Deston {1}:2|(0,0,0,0)|(100/100)] [] [Ability {1}] [AbilityActivate {1}: X {1}]\n"
    scrubbed, name_map = anonymize_lines([line])
    assert "@Owner#333" not in scrubbed[0]
    assert "/Deston {1}:2" in scrubbed[0], "the pet's own name/instance id must stay intact"


def test_npc_only_lines_are_left_completely_unchanged():
    line = "[00:00:00.000] [Boss Name {1}:1|(0,0,0,0)|(1000/1000)] [Add {2}:3|(0,0,0,0)|(50/50)] [Ability {1}] [Damage {2}: X {1}] (100)\n"
    scrubbed, name_map = anonymize_lines([line])
    assert scrubbed[0] == line
    assert name_map == {}


def test_anonymized_output_still_parses_as_a_valid_player_entity():
    """The whole point of keeping a fake-but-well-formed @Player#id token
    (instead of e.g. just deleting the name) is that the scrubbed file
    still parses through log_parser.py exactly like a real log would."""
    from log_parser import parse_line
    line = "[00:00:00.000] [@RealName#12345|(0,0,0,0)|(100/100)] [] [Ability {1}] [AbilityActivate {1}: X {1}]\n"
    scrubbed, _ = anonymize_lines([line])
    event = parse_line(scrubbed[0], line_number=1)
    assert event is not None
    assert event.source_is_player is True, "must still parse as a player entity, not fall back to NPC"
    assert event.source == "Player1"  # log_parser strips the leading @ from every entity's name
