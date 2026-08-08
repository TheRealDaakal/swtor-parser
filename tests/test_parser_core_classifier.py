from parser_core.classifier import _classify


class DummyEvent:
    def __init__(self, effect_type="", effect_name="", source_is_player=False,
                 target_is_player=False, ability=None):
        self.effect_type = effect_type
        self.effect_name = effect_name
        self.source_is_player = source_is_player
        self.target_is_player = target_is_player
        self.ability = ability
        self.is_damage = False
        self.is_heal = False
        self.is_death = False
        self.is_combat_start = False
        self.is_combat_end = False
        self.is_area_entered = False
        self.group_size = None
        self.difficulty = None
        self.is_effect_removed = False
        self.is_charges_modified = False
        self.is_ability_activate = False
        self.is_threat_modified = False
        self.is_interrupted = False
        self.is_hard_cc = False
        self.is_raid_buff_cast = False
        self.amount = 0.0
        self.is_critical = False
        self.overheal = 0.0
        self.shield_absorbed = 0.0
        self.avoidance = None
        self.threat_delta = 0.0


def test_classifier_handles_unknown_event():
    event = DummyEvent(effect_type="future event")
    _classify(event, "")
    assert event.is_damage is False
    assert event.is_heal is False


def test_classifier_sets_damage_from_effect_type():
    event = DummyEvent(effect_type="Damage")
    _classify(event, "(1234 energy {1})")
    assert event.is_damage is True
    assert event.amount == 1234
