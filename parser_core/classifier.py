"""SWTOR combat-event classification extracted from log_parser.py."""
from __future__ import annotations

import re
from .field_extractors import (
    _extract_amount,
    _extract_angle_value,
    _extract_avoidance,
    _extract_is_critical,
    _extract_overheal,
    _extract_shield_absorbed,
)

ABILITY_ACTIVATE_KEYWORDS = ("abilityactivate",)
ABILITY_INTERRUPT_KEYWORDS = ("abilityinterrupt",)
AREA_ENTERED_KEYWORDS = ("areaentered",)
COMBAT_END_KEYWORDS = ("exitcombat",)
COMBAT_START_KEYWORDS = ("entercombat",)
DAMAGE_KEYWORDS = ("damage",)
DEATH_EFFECT_NAME = "death"
DEATH_EVENT_TYPE = "event"
DIFFICULTY_RE = re.compile(r"(\d+)\s+Player\s+([A-Za-z_]+)", re.IGNORECASE)
EFFECT_REMOVED_KEYWORDS = ("removeeffect",)
HARD_CC_KEYWORDS = ("stunned", "incapacitated", "asleep", "sleeping", "lifted")
HEAL_KEYWORDS = ("heal",)
MODIFY_CHARGES_KEYWORDS = ("modifycharges",)
RAID_BUFF_ABILITY_NAMES = frozenset({
    "Transcendence", "Aegis Shield", "Predation", "Warding Shield",
    "Unlimited Power", "Inspiration", "Tactical Superiority",
    "Supercharged Celerity", "Stack the Deck", "Force Empowerment",
    "Bloodthirst", "Rally",
})
THREAT_MODIFIED_KEYWORDS = ("modifythreat",)

def _classify(event: CombatEvent, tail: str) -> None:
    # Classify from the EVENT-TYPE fields only, never the ability name. The
    # ability is what was cast; the effect fields are what actually happened.
    # Including the ability name here misclassifies any ability whose name
    # merely contains a keyword -- "Death Field" / "Death From Above" ticks
    # were being counted as deaths, and because stats.apply() returns early
    # on a death, their damage was silently dropped from DPS as well.
    haystack = " ".join(
        filter(None, [event.effect_type, event.effect_name])
    ).lower()

    # Collapse whitespace so "Enter Combat" and "EnterCombat" both match
    tight = haystack.replace(" ", "")

    event.is_damage = any(k in haystack for k in DAMAGE_KEYWORDS)
    event.is_heal = any(k in haystack for k in HEAL_KEYWORDS)
    event.is_death = (
        (event.effect_type or "").strip().lower() == DEATH_EVENT_TYPE
        and (event.effect_name or "").strip().lower() == DEATH_EFFECT_NAME
    )
    event.is_combat_start = any(k in tight for k in COMBAT_START_KEYWORDS)
    event.is_combat_end = any(k in tight for k in COMBAT_END_KEYWORDS)
    event.is_area_entered = any(k in tight for k in AREA_ENTERED_KEYWORDS)
    if event.is_area_entered and event.effect_name:
        m = DIFFICULTY_RE.search(event.effect_name)
        if m:
            event.group_size = int(m.group(1))
            # BARAS spells these lowercase in its `difficulties` lists.
            event.difficulty = m.group(2).lower()
    effect_type_tight = (event.effect_type or "").lower().replace(" ", "")
    event.is_effect_removed = any(k in effect_type_tight for k in EFFECT_REMOVED_KEYWORDS)
    event.is_charges_modified = any(k in effect_type_tight for k in MODIFY_CHARGES_KEYWORDS)
    effect_name_tight = (event.effect_name or "").lower().replace(" ", "")
    event.is_ability_activate = any(
        k in effect_name_tight for k in ABILITY_ACTIVATE_KEYWORDS
    )
    event.is_threat_modified = any(
        k in effect_name_tight for k in THREAT_MODIFIED_KEYWORDS
    )
    event.is_interrupted = any(
        k in effect_name_tight for k in ABILITY_INTERRUPT_KEYWORDS
    )
    event.is_hard_cc = (
        event.source_is_player and not event.target_is_player
        and not event.is_effect_removed
        and any(k in effect_name_tight for k in HARD_CC_KEYWORDS)
    )
    event.is_raid_buff_cast = (
        event.is_ability_activate and event.source_is_player
        and event.ability in RAID_BUFF_ABILITY_NAMES
    )

    if event.is_damage or event.is_heal:
        event.amount = _extract_amount(tail)
        event.is_critical = _extract_is_critical(tail)
    if event.is_heal:
        event.overheal = _extract_overheal(tail)
    if event.is_damage:
        # Shield Chance mitigation and avoidance (miss/dodge/parry/deflect/
        # resist) only ever apply to incoming damage, not healing -- no need
        # to scan the tail on every heal tick too.
        event.shield_absorbed = _extract_shield_absorbed(tail)
        event.avoidance = _extract_avoidance(tail)
    if event.is_threat_modified:
        event.threat_delta = _extract_angle_value(tail)
