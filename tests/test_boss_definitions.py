"""
Covers a real live-raid bug: boss alerts silently never firing. Root cause
was in Condition.matches()'s one-time edge-trigger dedup for hp_below and
counter_reaches -- the key was (selector, percent) / (counter_id, value)
only, shared across ANY Condition object with those same values. Two
SEPARATE timer definitions that happen to check the same real threshold
(routine after merging BARAS + ORBS data, since both sources often
independently define a timer for the same moment) collided: whichever
condition got evaluated first on the crossing event "claimed" the shared
key, and the other -- a different Condition object, functionally
identical -- found it already consumed and never fired again that pull,
even though it was never actually checked itself.

Confirmed live: Soa's 75%/30% is_alert=true "Transition incoming" alerts
(chained via timer_expires off an hp_below timer) never fired, because an
earlier, non-alert hp_below timer at the identical threshold silently
stole the one-time trigger first. A full scan of boss_definitions_bundled
found this exact collision in 23 different bosses, 55 colliding key
groups -- this was not a Soa-specific gap.
"""
import json

import pytest

from boss_definitions import Condition, EvalContext, _definition_from_dict
from log_parser import CombatEvent, parse_line


def _hp_event(target, hp_current, hp_max):
    return CombatEvent(raw="", timestamp="00:00:00.000", source="@Player#1", target=target,
                       ability="Attack", effect_type="ApplyEffect", effect_name="Damage",
                       amount=100.0, hp_current=hp_current, hp_max=hp_max)


def _ctx(event):
    return EvalContext(
        event=event, boss_names=["Boss"], local_player_name="Player", counters={},
        seen_entities=set(), recently_expired_timer_ids=[], recently_ended_phase_ids=[],
        fired_hp_thresholds=set(), recently_entered_phase_ids=[], active_phase_id=None,
        fired_counter_reaches=set(),
    )


def test_two_separate_hp_below_conditions_at_same_threshold_both_fire():
    """The actual reported bug: two independently-defined timers both
    watching for the boss crossing 75% HP -- e.g. one from BARAS, one
    added later from ORBS -- must each get their own one-time trigger,
    not share a single global slot."""
    cond_a = Condition(type="hp_below", percent=75.0)
    cond_b = Condition(type="hp_below", percent=75.0)  # a SEPARATE definition, same threshold

    ctx = _ctx(_hp_event("Boss", 700, 1000))  # 70% -- below 75%
    # Both share ctx.fired_hp_thresholds, matching how _process_timers
    # evaluates every timer's condition against the SAME context object.
    assert cond_a.matches(ctx) is True
    assert cond_b.matches(ctx) is True, (
        "a second, independent hp_below(75%) condition must fire on its own -- "
        "the first condition firing must not silently consume it"
    )


def test_same_hp_below_condition_only_fires_once_per_pull():
    """The dedup itself must still work for its original purpose: the SAME
    condition object shouldn't re-fire on every subsequent event while HP
    stays below the threshold."""
    cond = Condition(type="hp_below", percent=75.0)
    ctx = _ctx(_hp_event("Boss", 700, 1000))

    assert cond.matches(ctx) is True
    assert cond.matches(ctx) is False, "the same condition must not re-fire for the same crossing"
    assert cond.matches(ctx) is False


def test_two_separate_counter_reaches_conditions_at_same_value_both_fire():
    cond_a = Condition(type="counter_reaches", counter_id="adds", value=3)
    cond_b = Condition(type="counter_reaches", counter_id="adds", value=3)

    ev = _hp_event("Boss", 1000, 1000)
    ctx = EvalContext(
        event=ev, boss_names=["Boss"], local_player_name="Player", counters={"adds": 3},
        seen_entities=set(), recently_expired_timer_ids=[], recently_ended_phase_ids=[],
        fired_hp_thresholds=set(), recently_entered_phase_ids=[], active_phase_id=None,
        fired_counter_reaches=set(),
    )
    assert cond_a.matches(ctx) is True
    assert cond_b.matches(ctx) is True, (
        "a second, independent counter_reaches condition at the same value must fire on its own"
    )


def test_different_hp_below_thresholds_are_independent_regardless():
    """Sanity check the fix didn't accidentally over-scope: two conditions
    at DIFFERENT percentages were never supposed to collide in the first
    place, and still shouldn't."""
    cond_75 = Condition(type="hp_below", percent=75.0)
    cond_30 = Condition(type="hp_below", percent=30.0)

    ctx = _ctx(_hp_event("Boss", 250, 1000))  # 25% -- below both
    assert cond_75.matches(ctx) is True
    assert cond_30.matches(ctx) is True


# ---------------------------------------------------------------------
# _definition_from_dict -- extracted from _load_one so /api/encounters can
# validate/construct a definition straight from a POSTed JSON body without
# touching the filesystem (see web_server.py). These tests cover the
# extraction itself: a full round-trip through every condition type
# (including nested any_of/all_of/not) must produce the right dataclasses,
# and structurally malformed input must raise a catchable error instead of
# silently producing a broken definition -- the API layer turns that
# exception into a 400 for the encounter editor's user to see.

def test_definition_from_dict_round_trips_every_condition_type():
    """One of each of the 21 real condition types boss_definitions.py
    supports, including the three nesting forms (any_of/all_of/not) two
    levels deep -- the exact shape the encounter editor's recursive
    condition-tree UI can produce."""
    data = {
        "id": "full_test", "name": "Full Test Boss", "boss_names": ["Full Test Boss"],
        "boss_npc_ids": ["12345"],
        "encounter_trigger": {"type": "ability_cast", "keyword": "Intro"},
        "phases": [
            {"id": "p1", "name": "Phase 1"},
            {"id": "p2", "name": "Phase 2", "start_trigger": {"type": "hp_below", "percent": 50},
             "conditions": [{"type": "phase_active", "phase_ids": ["p1"]}],
             "end_trigger": {"type": "combat_end"}},
        ],
        "counters": [
            {"id": "adds", "name": "Adds", "initial_value": 0,
             "increment_on": {"type": "npc_appears", "selector": ["Add"]},
             "decrement_on": {"type": "entity_death", "selector": ["Add"]},
             "reset_on": {"type": "any_phase_change"}},
        ],
        "timers": [
            {"id": "t1", "label": "Nested Trigger", "duration_seconds": 20.0,
             "warn_seconds_before": 5.0, "voice_alert": True, "is_alert": False,
             "phases": ["p1"], "repeat_interval_seconds": 30.0, "repeat_count": 3,
             "trigger": {
                 "type": "all_of",
                 "conditions": [
                     {"type": "any_of", "conditions": [
                         {"type": "ability_cast", "keyword": "Boom", "target": "local_player"},
                         {"type": "effect_applied", "keyword": "Marked"},
                     ]},
                     {"type": "not", "condition": {"type": "phase_ended", "phase_id": "p2"}},
                 ],
             },
             "conditions": [{"type": "counter_compare", "counter_id": "adds", "operator": "gte", "value": 1}],
             "cancel_trigger": {"type": "timer_expires", "timer_id": "other"}},
            {"id": "t2", "label": "Simple Ones", "duration_seconds": 5.0,
             "trigger": {"type": "timer_time_remaining", "timer_id": "t1", "operator": "lte", "value": 3.0}},
            {"id": "t3", "label": "Effect Removed", "duration_seconds": 5.0,
             "trigger": {"type": "effect_removed", "keyword": "Shield"}},
            {"id": "t4", "label": "Timer Started", "duration_seconds": 5.0,
             "trigger": {"type": "timer_started", "timer_id": "t1"}},
            {"id": "t5", "label": "Phase Entered", "duration_seconds": 5.0,
             "trigger": {"type": "phase_entered", "phase_id": "p2"}},
            {"id": "t6", "label": "Counter Reaches", "duration_seconds": 5.0,
             "trigger": {"type": "counter_reaches", "counter_id": "adds", "value": 3}},
            {"id": "t7", "label": "Counter Changes", "duration_seconds": 5.0,
             "trigger": {"type": "counter_changes", "counter_id": "adds"}},
            {"id": "t8", "label": "Combat Start", "duration_seconds": 5.0,
             "trigger": {"type": "combat_start"}},
        ],
    }

    definition = _definition_from_dict(data)

    assert definition.id == "full_test"
    assert definition.boss_npc_ids == ["12345"]
    assert definition.encounter_trigger.type == "ability_cast"
    assert len(definition.phases) == 2
    assert definition.phases[1].start_trigger.type == "hp_below"
    assert definition.phases[1].conditions[0].type == "phase_active"
    assert definition.phases[1].end_trigger.type == "combat_end"
    assert len(definition.counters) == 1
    assert definition.counters[0].increment_on.type == "npc_appears"
    assert definition.counters[0].decrement_on.type == "entity_death"
    assert definition.counters[0].reset_on.type == "any_phase_change"

    t1 = definition.timers[0]
    assert t1.trigger.type == "all_of"
    nested_any_of = t1.trigger.conditions[0]
    assert nested_any_of.type == "any_of"
    assert nested_any_of.conditions[0].target == "local_player"
    nested_not = t1.trigger.conditions[1]
    assert nested_not.type == "not"
    assert nested_not.condition.type == "phase_ended"
    assert t1.conditions[0].type == "counter_compare"
    assert t1.cancel_trigger.type == "timer_expires"
    assert len(definition.timers) == 8  # every remaining simple type constructed without error


def test_definition_from_dict_raises_on_missing_definition_id():
    with pytest.raises(KeyError):
        _definition_from_dict({"name": "No ID", "boss_names": [], "phases": [], "timers": []})


def test_definition_from_dict_raises_on_missing_phase_id():
    with pytest.raises(KeyError):
        _definition_from_dict({
            "id": "x", "name": "X", "boss_names": [],
            "phases": [{"name": "no id field"}], "timers": [],
        })


def test_definition_from_dict_raises_on_missing_counter_id():
    with pytest.raises(KeyError):
        _definition_from_dict({
            "id": "x", "name": "X", "boss_names": [], "phases": [],
            "counters": [{"name": "no id field"}], "timers": [],
        })


def test_definition_from_dict_defaults_are_sane_for_a_minimal_definition():
    """The encounter editor's "New Encounter" blank draft -- an id/name
    plus one phase and nothing else -- must construct cleanly with sane
    defaults, not require every optional field to be present."""
    definition = _definition_from_dict({
        "id": "minimal", "name": "Minimal", "boss_names": ["Minimal"],
        "phases": [{"id": "main", "name": "Main"}],
    })
    assert definition.timers == []
    assert definition.counters == []
    assert definition.boss_npc_ids == []
    assert definition.encounter_trigger is None


# ---------------------------------------------------------------------
# hp_phase_markers() -- HP% tick marks for the boss HP bar overlay
# (see overlay.py's BossHealthOverlay). Deliberately narrow: only a
# phase's DIRECT hp_below start_trigger counts, not one buried inside an
# any_of/all_of gate.

def test_hp_phase_markers_collects_plain_hp_below_triggers():
    definition = _definition_from_dict({
        "id": "x", "name": "X", "boss_names": ["X"],
        "phases": [
            {"id": "p1", "name": "Main"},
            {"id": "p2", "name": "Adds", "start_trigger": {"type": "hp_below", "percent": 75}},
            {"id": "p3", "name": "Burn", "start_trigger": {"type": "hp_below", "percent": 20}},
        ],
    })
    assert definition.hp_phase_markers() == [75, 20]


def test_hp_phase_markers_ignores_non_hp_triggers():
    definition = _definition_from_dict({
        "id": "x", "name": "X", "boss_names": ["X"],
        "phases": [
            {"id": "p1", "name": "Main"},
            {"id": "p2", "name": "P2", "start_trigger": {"type": "ability_cast", "keyword": "Intro"}},
        ],
    })
    assert definition.hp_phase_markers() == []


def test_hp_phase_markers_ignores_hp_below_nested_inside_all_of():
    """A marker implies "the transition happens exactly here" -- not true
    once other conditions gate it too, so a nested hp_below must not
    produce a (misleadingly precise) tick mark."""
    definition = _definition_from_dict({
        "id": "x", "name": "X", "boss_names": ["X"],
        "phases": [
            {"id": "p1", "name": "Main"},
            {"id": "p2", "name": "P2", "start_trigger": {
                "type": "all_of",
                "conditions": [
                    {"type": "hp_below", "percent": 50},
                    {"type": "counter_reaches", "counter_id": "adds", "value": 3},
                ],
            }},
        ],
    })
    assert definition.hp_phase_markers() == []


class TestAbilityIdTriggers:
    """BARAS's own encounter data is ability-ID based throughout. This
    project translates those IDs to keyword text, and any ID whose name was
    never observed in a real log became an inert
    REPLACE_WITH_REAL_ABILITY_NAME_<id> placeholder that could never fire --
    133 of them across 56 bosses. Matching the ID directly fixes those
    without needing a name, and is the ONLY thing that can work for
    abilities SWTOR logs with no name at all."""

    def _ctx(self, event):
        return EvalContext(
            event=event, boss_names=[], local_player_name=None, counters={},
            seen_entities=set(), recently_expired_timer_ids=[],
            recently_ended_phase_ids=[], fired_hp_thresholds=set(),
            recently_entered_phase_ids=[], active_phase_id=None,
            fired_counter_reaches=set(), recently_started_timer_ids=[],
            changed_counter_ids=set(),
        )

    def _cast(self, ability_id, name=""):
        name_part = f"{name} " if name else " "
        raw = (f"[00:00:00.000] [Boss {{1}}:2|(0,0,0,0)|(100/100)] [=] "
               f"[{name_part}{{{ability_id}}}] [Event {{3}}: AbilityActivate {{4}}]")
        return parse_line(raw, line_number=1)

    def test_matches_an_unnamed_ability_by_id(self):
        cond = Condition(type="ability_cast", ability_ids=["3016484380999680"])
        assert cond.matches(self._ctx(self._cast("3016484380999680"))) is True

    def test_does_not_match_a_different_id(self):
        cond = Condition(type="ability_cast", ability_ids=["3016484380999680"])
        assert cond.matches(self._ctx(self._cast("999999999999999"))) is False

    def test_id_match_is_exact_not_substring(self):
        """Keyword matching is substring-based; IDs must not be, or a short
        id would spuriously match inside a longer one."""
        cond = Condition(type="ability_cast", ability_ids=["3016484380999680"])
        assert cond.matches(self._ctx(self._cast("30164843809996801"))) is False

    def test_keyword_matching_still_works_when_no_ids_are_given(self):
        cond = Condition(type="ability_cast", keyword="Force Leap")
        assert cond.matches(self._ctx(self._cast("812105301229568", "Force Leap"))) is True

    def test_ability_ids_load_from_json_as_strings(self):
        """A definition may write these as JSON numbers; they must still
        compare equal to the parsed event's string id."""
        definition = _definition_from_dict({
            "id": "x", "name": "X", "boss_names": ["X"],
            "phases": [{"id": "p1", "name": "Main"}],
            "timers": [{
                "label": "Burrow", "duration_seconds": 45.0,
                "trigger": {"type": "ability_cast", "ability_ids": [3016484380999680]},
            }],
        })
        assert definition.timers[0].trigger.ability_ids == ["3016484380999680"]

    def test_announce_on_start_defaults_true_and_loads_from_json(self):
        definition = _definition_from_dict({
            "id": "x", "name": "X", "boss_names": ["X"],
            "phases": [{"id": "p1", "name": "Main"}],
            "timers": [
                {"label": "A", "duration_seconds": 5.0},
                {"label": "B", "duration_seconds": 5.0, "announce_on_start": False},
            ],
        })
        assert definition.timers[0].announce_on_start is True
        assert definition.timers[1].announce_on_start is False


class TestBundledTimerInvariants:
    """Data invariants over the 163 bundled definitions. These are not
    hypotheticals -- both were measured firing on real kills.

    The app merges timers from two upstreams (BARAS and ORBS), and neither
    knows about the other. When both describe the same mechanic, the raid
    hears it twice: a real Torque kill fired 79 timers, 44 of which were a
    label repeating at the identical instant ("Vents" 19 times).
    """

    def _definitions(self):
        from pathlib import Path
        import glob
        root = Path(__file__).resolve().parent.parent / "boss_definitions_bundled"
        for path in sorted(glob.glob(str(root / "*.json"))):
            yield json.load(open(path, encoding="utf-8"))

    def test_no_orbs_timer_duplicates_a_baras_one(self):
        """The cross-source duplicate. Timers come from two upstreams that
        don't know about each other, so when both describe a mechanic the
        raid hears it twice: a real Torque kill fired 79 timers, 44 of them
        a label repeating at the identical instant ("Vents" 19 times).
        torque_vents (BARAS, 30s, veteran-only) and torque_orbs_vents
        (30s, no difficulty at all) had the same trigger and duration.

        Scoped to ORBS-vs-BARAS pairs on purpose. BARAS ships plenty of
        same-label timers of its own -- per-difficulty variants, per-phase
        variants, first-cast vs recurring -- and those are deliberate; only
        one is ever live. Asserting a rule over upstream data this suite
        hasn't validated would just encode a guess.

        Label matching keeps punctuation: BARAS uses a trailing underscore
        for a mechanic's recurring form (Olok's "Flashbang" 10s vs
        "Flashbang_" 40s), which are genuinely different timers.
        """
        def is_orbs(t):
            return "orbs" in (t.get("id") or "").lower()

        def key(t):
            trigger = t.get("trigger") or {}
            return (" ".join((t.get("label") or "").lower().split()),
                    trigger.get("type"),
                    " ".join((trigger.get("keyword") or "").lower().split()))

        offenders = []
        for d in self._definitions():
            timers = d.get("timers") or []
            baras = {key(t): t.get("id") for t in timers if not is_orbs(t)}
            for t in timers:
                if is_orbs(t) and key(t) in baras:
                    offenders.append(
                        f"{d.get('name')}: {t.get('id')} duplicates {baras[key(t)]}")
        assert not offenders, (
            "ORBS timers duplicating a BARAS one (same label + trigger):\n"
            + "\n".join(offenders[:15]))

    def test_group_size_suffixed_timers_declare_their_group_size(self):
        """BARAS names 8- and 16-player variants of a mechanic with _8m /
        _16m and gives them different durations (Cartel Warlords' Sunder
        The End: 16.0s at 8, 11.0s at 16). If the scoping is dropped, both
        fire on every pull and one of them is simply the wrong time."""
        import re
        pattern = re.compile(r"_(8|16)m(?:_|$)")
        offenders = []
        for d in self._definitions():
            for t in d.get("timers") or []:
                m = pattern.search(t.get("id") or "")
                if m and not t.get("group_sizes"):
                    offenders.append(f"{d.get('name')}: {t.get('id')}")
        assert not offenders, "timers whose id encodes a group size but don't declare it:\n" + "\n".join(offenders)
