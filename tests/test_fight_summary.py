"""
Covers analysis/fight_summary.py's outcome detection (kill/wipe/unknown) --
the one thing here that has to be right, since a wrong "wipe" reads as a
false accusation and a wrong "kill" hides a real wipe. Deliberately does
NOT test for any synthesized causal explanation text, because this module
doesn't produce one on purpose (see its own module docstring) -- only
structured facts.
"""
from conftest import log_line
from boss_definitions import _definition_from_dict
from analysis.fight_summary import build_fight_summary, kill_names


def _write_log(tmp_path, lines):
    path = tmp_path / "combat.txt"
    path.write_text("\n".join(lines) + "\n", encoding="cp1252")
    return str(path)


def _definitions():
    d = _definition_from_dict({
        "id": "test_boss", "name": "Test Boss", "boss_names": ["Test Boss"],
        "phases": [
            {"id": "p1", "name": "Phase 1"},
            {"id": "p2", "name": "Phase 2", "start_trigger": {"type": "hp_below", "percent": 50}},
        ],
    })
    return {"test_boss": d}


def test_outcome_is_kill_when_the_recognized_bosss_death_is_seen(tmp_path):
    lines = [
        log_line("00:00:00.000", "Test Boss", ability="Intro", effect_name="AbilityActivate {1}"),
        log_line("00:00:05.000", "@Player#1", target="Test Boss", ability="Smash",
                  effect_name="Damage {2}", amount="1000000", target_hp="0/1000000"),
        log_line("00:00:05.100", "@Player#1", target="Test Boss", ability="Smash",
                  effect_type="Event", effect_name="Death"),
    ]
    path = _write_log(tmp_path, lines)
    result = build_fight_summary(path, 1, len(lines), _definitions())
    assert result["boss_name"] == "Test Boss"
    assert result["outcome"] == "kill"


def test_outcome_is_wipe_when_the_boss_is_recognized_but_never_dies(tmp_path):
    lines = [
        log_line("00:00:00.000", "Test Boss", ability="Intro", effect_name="AbilityActivate {1}"),
        log_line("00:00:05.000", "Test Boss", target="@Player#1", ability="Smash",
                  effect_name="Damage {2}", amount="500000", target_hp="0/500000"),
        log_line("00:00:05.100", "Test Boss", target="@Player#1", ability="Smash",
                  effect_type="Event", effect_name="Death"),
    ]
    path = _write_log(tmp_path, lines)
    result = build_fight_summary(path, 1, len(lines), _definitions())
    assert result["boss_name"] == "Test Boss"
    assert result["outcome"] == "wipe"


def test_outcome_is_unknown_when_no_boss_is_recognized_at_all(tmp_path):
    """Trash/leveling/PvP content -- must not read as a "wipe" just
    because nobody named-boss-related died."""
    lines = [
        log_line("00:00:00.000", "@Player#1", target="Some Mob", ability="Smash",
                  effect_name="Damage {2}", amount="500", target_hp="0/500"),
        log_line("00:00:00.100", "@Player#1", target="Some Mob", ability="Smash",
                  effect_type="Event", effect_name="Death"),
    ]
    path = _write_log(tmp_path, lines)
    result = build_fight_summary(path, 1, len(lines), _definitions())
    assert result["boss_name"] is None
    assert result["outcome"] == "unknown"


def test_a_player_death_unrelated_to_the_boss_does_not_flip_the_outcome_to_kill(tmp_path):
    """A raid member dying mid-fight is not the boss dying -- outcome
    must stay 'wipe' (the boss recognized, never actually killed)."""
    lines = [
        log_line("00:00:00.000", "Test Boss", ability="Intro", effect_name="AbilityActivate {1}"),
        log_line("00:00:05.000", "Test Boss", target="@Player#1", ability="Smash",
                  effect_name="Damage {2}", amount="500000", target_hp="0/500000"),
        log_line("00:00:05.100", "Test Boss", target="@Player#1", ability="Smash",
                  effect_type="Event", effect_name="Death"),
    ]
    path = _write_log(tmp_path, lines)
    result = build_fight_summary(path, 1, len(lines), _definitions())
    assert result["outcome"] == "wipe"
    assert len(result["deaths"]) == 1
    assert result["deaths"][0]["victim"] == "Player"  # log_parser strips the leading @


def test_phases_seen_reflects_a_real_transition(tmp_path):
    lines = [
        log_line("00:00:00.000", "Test Boss", ability="Intro", effect_name="AbilityActivate {1}"),
        log_line("00:00:05.000", "@Player#1", target="Test Boss", ability="Smash",
                  effect_name="Damage {2}", amount="600000", target_hp="400000/1000000"),
    ]
    path = _write_log(tmp_path, lines)
    result = build_fight_summary(path, 1, len(lines), _definitions())
    assert result["phases_seen"] == ["Phase 1", "Phase 2"]


def test_no_definitions_argument_falls_back_to_loading_the_real_bundled_ones(tmp_path):
    """Smoke test: the default-arg path (definitions=None) must not crash
    -- it loads the real bundled boss_definitions_bundled/ directory."""
    lines = [
        log_line("00:00:00.000", "@Player#1", target="Some Mob", ability="Smash",
                  effect_name="Damage {2}", amount="500"),
    ]
    path = _write_log(tmp_path, lines)
    result = build_fight_summary(path, 1, len(lines))
    assert result["outcome"] == "unknown"


class TestKillNames:
    """`boss_names` is a RECOGNITION list -- "see any of these and you're in
    this fight" -- so it legitimately contains adds. Using it as the kill
    test meant any add dying counted as killing the boss. Real case: Styrak's
    boss_names is ['Kell Dragon', 'Dread Master Styrak'] and a Styrak pull
    kills dozens of Kell Dragons, so 74 of 92 pulls were scored kills.
    """

    def test_adds_in_boss_names_are_excluded(self):
        d = _definition_from_dict({
            "id": "styrak", "name": "Dread Master Styrak",
            "boss_names": ["Kell Dragon", "Dread Master Styrak"],
        })
        assert kill_names(d) == ["Dread Master Styrak"]

    def test_multi_boss_encounters_keep_every_name(self):
        """The 13 bundled definitions the display-name rule doesn't resolve
        are all genuine multi-boss fights -- no adds among them."""
        d = _definition_from_dict({
            "id": "dread_council", "name": "Dread Council",
            "boss_names": ["Dread Master Bestia", "Dread Master Tyrans",
                           "Dread Master Calphayus"],
        })
        assert kill_names(d) == ["Dread Master Bestia", "Dread Master Tyrans",
                                 "Dread Master Calphayus"]

    def test_no_boss_names_yields_nothing(self):
        d = _definition_from_dict({"id": "x", "name": "X"})
        assert kill_names(d) == []


class TestMultiBossKill:
    def _defs(self):
        return {"council": _definition_from_dict({
            "id": "council", "name": "Test Council",
            "boss_names": ["Boss A", "Boss B"],
            "phases": [{"id": "p1", "name": "Phase 1"}],
        })}

    def _lines(self, *dead):
        lines = [log_line("00:00:00.000", "Boss A", ability="Intro",
                          effect_name="AbilityActivate {1}"),
                 log_line("00:00:01.000", "@Player#1", target="Boss A", ability="Hit",
                          effect_name="Damage {2}", amount="100")]
        for i, name in enumerate(dead):
            lines.append(log_line(f"00:00:{10 + i:02d}.000", "@Player#1", target=name,
                                  ability="Hit", effect_type="Event", effect_name="Death"))
        return lines

    def test_killing_only_one_boss_is_not_a_kill(self, tmp_path):
        lines = self._lines("Boss A")
        path = _write_log(tmp_path, lines)
        assert build_fight_summary(path, 1, len(lines), self._defs())["outcome"] == "wipe"

    def test_killing_every_boss_is_a_kill(self, tmp_path):
        lines = self._lines("Boss A", "Boss B")
        path = _write_log(tmp_path, lines)
        assert build_fight_summary(path, 1, len(lines), self._defs())["outcome"] == "kill"
