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


class TestAbilityId:
    """SWTOR logs some abilities with NO name at all, just the numeric id --
    a real line from this user's corpus reads "[ {3016484380999680}]" (The
    Writhing Horror's Burrow). Keyword matching can never reach those, so
    the id has to survive parsing for id-based triggers to work at all."""

    def test_ability_id_is_captured_alongside_the_name(self):
        ev = parse_line(log_line("00:00:00.000", "@Dps#1", target="Boss",
                                  ability="Force Leap {812105301229568}",
                                  effect_name="Damage {2}", amount="100"),
                         line_number=1)
        assert ev.ability == "Force Leap"
        assert ev.ability_id == "812105301229568"

    def test_unnamed_ability_still_yields_its_id(self):
        """The exact shape that made this necessary: no name, id only."""
        raw = ("[00:00:00.000] [Boss {1}:2|(0,0,0,0)|(100/100)] [=] "
               "[ {3016484380999680}] [Event {3}: AbilityActivate {4}]")
        ev = parse_line(raw, line_number=1)
        assert ev.ability is None, "there genuinely is no name to parse"
        assert ev.ability_id == "3016484380999680"
        assert ev.is_ability_activate

    def test_no_ability_bracket_leaves_the_id_unset(self):
        ev = parse_line(log_line("00:00:00.000", "@Dps#1", target="Boss",
                                  effect_name="Damage {2}", amount="100"),
                         line_number=1)
        assert ev.ability_id is None


class TestDeathDetection:
    """`is_death` used to be a substring match for "death"/"expire" against
    effect_type + effect_name. SWTOR is full of ability names containing
    "Death", so this fired constantly on things that are not deaths.

    Measured across 25 real log files from this project's own corpus:
    4,309 genuine deaths against 26,141 false positives -- "Deathmark" (a
    Marauder debuff re-logged on every ModifyCharges tick) accounted for
    23,631 on its own. Everything downstream inherited it: History death
    counts, forensics reports, and kill/wipe classification, which is what
    made the corpus show bosses whose "wipes" had no deaths in them.

    A death is exactly `[Event {id}: Death {id}]`. The names below are all
    real, taken from that same survey.
    """

    def _ev(self, effect_type, effect_name, ability="Some Ability {1}"):
        return parse_line(
            log_line("00:00:01.000", "@Dps#1", target="@Ally#2",
                     ability=ability, effect_type=effect_type,
                     effect_name=effect_name),
            line_number=1,
        )

    def test_the_real_death_event_is_detected(self):
        assert self._ev("Event", "Death {836045448945472}").is_death

    def test_deathmark_is_not_a_death(self):
        """23,631 of the 26,141 false positives, by itself."""
        for effect_type in ("ApplyEffect", "RemoveEffect", "ModifyCharges"):
            assert not self._ev(effect_type, "Deathmark {1}").is_death, effect_type

    def test_other_real_ability_names_containing_death_are_not_deaths(self):
        for name in ("Penetrating Death", "Death Brand (Slow)", "Feign Death",
                     "Death's Embrace", "Fire Wheel of Death",
                     "Death of a Loved One", "Baron Deathmark's Swashbuckling Cutter"):
            for effect_type in ("ApplyEffect", "RemoveEffect"):
                assert not self._ev(effect_type, f"{name} {{1}}").is_death, name

    def test_other_event_types_are_not_deaths(self):
        """Death is the only Event name that counts -- Revived in particular
        is adjacent to it and must not be confused for one."""
        for name in ("Revived", "AbilityActivate", "TargetSet", "ModifyThreat",
                     "AbilityInterrupt", "Taunt", "FailedEffect"):
            assert not self._ev("Event", f"{name} {{1}}").is_death, name


class TestSelfTargetIdentity:
    """'[=]' in the target bracket means "the target is the source" -- a
    self-cast: a defensive cooldown, a self-heal, a self-applied buff. It
    used to return is_player=False unconditionally, so every self-targeted
    event on a player looked like an NPC event to anything gated on
    target_is_player. Found via death forensics attributing 11 deaths to a
    player who died once: their self-applied "Penetrating Death" debuff
    ticks were being read as NPC deaths.
    """

    def test_self_target_on_a_player_stays_a_player(self):
        raw = ("[00:00:00.000] [@Darkrea#689221366739602|(0,0,0,0)|(453246/453246)] [=] "
               "[Force Shroud {1}] [ApplyEffect {2}: Force Shroud {1}]")
        ev = parse_line(raw, line_number=1)
        assert ev.target == "Darkrea"
        assert ev.target_is_player is True
        assert ev.source_is_player is True

    def test_self_target_on_an_npc_stays_an_npc(self):
        raw = ("[00:00:00.000] [Kell Dragon {3067057620910080}:12577000243195|(0,0,0,0)|(100/100)] [=] "
               "[Enrage {1}] [ApplyEffect {2}: Enrage {1}]")
        ev = parse_line(raw, line_number=1)
        assert ev.target == "Kell Dragon"
        assert ev.target_is_player is False
        assert ev.target_npc_id == "3067057620910080", "self-target inherits the source's npc id too"

    def test_a_real_target_bracket_is_unaffected(self):
        ev = parse_line(log_line("00:00:00.000", "@Dps#1", target="Boss",
                                 effect_name="Damage {2}", amount="100"),
                        line_number=1)
        assert ev.target == "Boss"
        assert ev.target_is_player is False
