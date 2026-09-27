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
        self.is_resource_spend = False
        self.is_resource_restore = False
        self.resource_type = None
        self.resource_amount = 0.0
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


def test_classifier_sets_resource_spend():
    event = DummyEvent(effect_type="Spend", effect_name="energy")
    _classify(event, "(15.0)")
    assert event.is_resource_spend is True
    assert event.is_resource_restore is False
    assert event.resource_type == "energy"
    assert event.resource_amount == 15.0


def test_classifier_sets_resource_restore():
    event = DummyEvent(effect_type="Restore", effect_name="rage point")
    _classify(event, "(3.0)")
    assert event.is_resource_restore is True
    assert event.is_resource_spend is False
    assert event.resource_type == "rage point"
    assert event.resource_amount == 3.0


def test_classifier_does_not_confuse_an_ability_named_restoration_for_a_resource_event():
    # is_resource_restore matches the whole effect_type word ("Restore"),
    # not a substring -- an ApplyEffect for an ability literally named
    # "Restoration" must not false-positive.
    event = DummyEvent(effect_type="ApplyEffect", effect_name="Restoration")
    _classify(event, "")
    assert event.is_resource_restore is False
    assert event.is_resource_spend is False
    assert event.resource_type is None
