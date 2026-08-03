"""
boss_definitions.py

Data-driven boss/encounter definitions, in the same spirit as BARAS's own
approach (per their docs: boss fights are configured via definition files,
not hardcoded in the app, so new bosses don't need a code change).

Everything a boss can react to -- phase transitions, timer triggers, and
counter increments -- is expressed as a `Condition`. A Condition can be:

  {"type": "combat_start"}
  {"type": "combat_end"}
  {"type": "hp_below", "percent": 50, "selector": ["Boss Name"]}
  {"type": "ability_cast", "keyword": "X", "selector": ["Boss Name"]}
  {"type": "effect_applied", "keyword": "X", "target": "local_player"}
  {"type": "effect_removed", "keyword": "X"}
  {"type": "npc_appears", "selector": ["Entity Name"]}
  {"type": "entity_death", "selector": ["Entity Name"]}
  {"type": "timer_expires", "timer_id": "some_timer_id"}
  {"type": "timer_started", "timer_id": "some_timer_id"}
  {"type": "timer_time_remaining", "timer_id": "x", "operator": "gte", "value": 9.0}
  {"type": "phase_ended", "phase_id": "some_phase_id"}
  {"type": "phase_entered", "phase_id": "some_phase_id"}
  {"type": "phase_active", "phase_ids": ["a", "b"]}
  {"type": "any_phase_change"}
  {"type": "counter_compare", "counter_id": "x", "operator": "gte", "value": 1}
  {"type": "counter_reaches", "counter_id": "x", "value": 3}
  {"type": "counter_changes", "counter_id": "x"}
  {"type": "any_of", "conditions": [ ...nested Conditions... ]}
  {"type": "all_of", "conditions": [ ...nested Conditions... ]}
  {"type": "not", "condition": { ...nested Condition... }}

`selector` defaults to the boss's own boss_names when omitted for
hp_below/ability_cast, so most definitions don't need to repeat it.

A boss definition:

{
  "id": "example", "name": "Example Boss", "boss_names": ["Example"],
  "phases": [
    {"id": "p1", "name": "Phase 1"},                    -- no start_trigger = initial
    {"id": "p2", "name": "Phase 2", "start_trigger": {...}, "conditions": [...]}
  ],
  "counters": [
    {"id": "c1", "name": "...", "increment_on": {...}, "decrement_on": {...},
     "reset_on": {...}, "initial_value": 0}
  ],
  "timers": [
    {"id": "t1", "label": "...", "duration_seconds": N, "trigger": {...},
     "conditions": [...], "phases": [...], "repeat_interval_seconds": N,
     "repeat_count": N, "warn_seconds_before": N, "voice_alert": true}
  ]
}

Unlike the earlier linear-only phase model, phases here form a general
state machine: any defined phase can become active whenever its
start_trigger (+ conditions) matches, as long as it isn't already the
active phase -- this supports fights that cycle between phases (e.g.
alternating "boss" and "add" phases) rather than only ever moving forward.

Definitions load from a bundled directory and an optional user directory
(user files override bundled ones with the same id) -- same pattern BARAS
uses for its own encounter definitions.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from log_parser import CombatEvent

LOCAL_PLAYER = "local_player"


class EvalContext:
    """Everything a Condition might need to evaluate against, for one event."""

    def __init__(self, event: CombatEvent, boss_names: List[str], local_player_name: Optional[str],
                 counters: Dict[str, float], seen_entities: set,
                 recently_expired_timer_ids: List[str], recently_ended_phase_ids: List[str],
                 fired_hp_thresholds: set, recently_entered_phase_ids: List[str],
                 active_phase_id: Optional[str] = None,
                 fired_counter_reaches: Optional[set] = None,
                 recently_started_timer_ids: Optional[List[str]] = None,
                 changed_counter_ids: Optional[set] = None,
                 timer_engine=None):
        self.event = event
        self.boss_names = boss_names
        self.local_player_name = local_player_name
        self.counters = counters
        self.seen_entities = seen_entities
        self.recently_expired_timer_ids = recently_expired_timer_ids
        self.recently_ended_phase_ids = recently_ended_phase_ids
        # hp_below is otherwise continuously true for the rest of the fight
        # once HP drops past the threshold -- this set makes it edge-
        # triggered (fires once per threshold per pull), same problem BARAS
        # itself works around with counter-gating on HP markers.
        self.fired_hp_thresholds = fired_hp_thresholds
        self.recently_entered_phase_ids = recently_entered_phase_ids
        # The phase active BEFORE this event's transition is considered --
        # lets a "conditions" AND-gate restrict a trigger to only fire from
        # a specific phase, disambiguating two phases that share the same
        # (or an unavoidably guessed) trigger condition.
        self.active_phase_id = active_phase_id
        # Same edge-trigger problem as fired_hp_thresholds, for
        # counter_reaches: BARAS's semantics are "fires once, at the moment
        # the counter REACHES this value", but a plain `current >= value`
        # comparison stays true for the rest of the pull and re-fires the
        # timer on every single subsequent event. This set makes it fire
        # once per (counter, value) per pull.
        self.fired_counter_reaches = (
            fired_counter_reaches if fired_counter_reaches is not None else set()
        )
        # Boss timers that STARTED while processing this event (timer_started).
        self.recently_started_timer_ids = recently_started_timer_ids or []
        # Counters whose value changed while processing this event
        # (counter_changes) -- "it moved", regardless of direction or value.
        self.changed_counter_ids = (
            changed_counter_ids if changed_counter_ids is not None else set()
        )
        # Needed by timer_time_remaining, which has to ask how much time is
        # left on a *currently running* timer rather than reasoning purely
        # from the event stream.
        self.timer_engine = timer_engine


_OPERATORS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
}


@dataclass
class Condition:
    type: str
    percent: Optional[float] = None
    keyword: Optional[str] = None
    target: Optional[str] = None
    selector: List[str] = field(default_factory=list)
    timer_id: Optional[str] = None
    phase_id: Optional[str] = None
    phase_ids: List[str] = field(default_factory=list)
    counter_id: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[float] = None
    conditions: List["Condition"] = field(default_factory=list)  # for any_of / all_of
    condition: Optional["Condition"] = None  # for `not` (single nested condition)

    def matches(self, ctx: EvalContext, commit: bool = True) -> bool:
        """commit=False performs a read-only check: it evaluates whether
        this condition currently matches without applying any of the
        one-shot edge-trigger side effects (hp_below's fired-threshold set,
        npc_appears' seen-entities set). Used by callers that AND this
        condition together with extra `conditions` -- the side effect must
        only be applied once the WHOLE gate is confirmed to pass, otherwise
        an edge trigger can be silently "used up" by an event that ends up
        not actually causing the phase/timer to fire (see
        _evaluate_and_commit)."""
        event = ctx.event

        if self.target == LOCAL_PLAYER:
            if ctx.local_player_name is None or event.target != ctx.local_player_name:
                return False

        if self.type == "combat_start":
            return event.is_combat_start

        if self.type == "combat_end":
            return event.is_combat_end

        if self.type == "hp_below":
            if event.hp_current is None or event.hp_max is None or event.hp_max == 0:
                return False
            sel = self.selector or ctx.boss_names
            if event.target not in sel:
                return False
            if (event.hp_current / event.hp_max) * 100 > (self.percent or 0):
                return False
            # id(self) disambiguates between two SEPARATE hp_below timer
            # definitions that happen to share the same (selector, percent)
            # -- common after merging BARAS + ORBS data, since both sources
            # often independently define a timer for the same real
            # threshold. Without it, whichever definition's condition gets
            # evaluated first on the crossing event silently "claims" the
            # shared key, and the OTHER definition's condition -- a
            # different Condition object, functionally identical -- finds
            # it already consumed and never fires again for that pull, even
            # though it was never actually checked. Confirmed live: two
            # separate 75%/30% timers on Soa, only the non-alert one (which
            # happened to come first in the JSON array) ever fired; the
            # is_alert=true one chained off the other never did.
            key = (id(self), tuple(sorted(sel)), self.percent)
            if key in ctx.fired_hp_thresholds:
                return False
            if commit:
                ctx.fired_hp_thresholds.add(key)
            return True

        if self.type == "ability_cast":
            # Must be the actual activation event, not one of the damage
            # ticks / effect applications the cast goes on to produce -- all
            # of those repeat the same ability name in the log line, so
            # matching on the name alone fires a timer many times per cast
            # (measured on real logs: 56 matches for a single real cast of
            # Watchdog Protocol, 155 for 12 casts of Seed of Echoes).
            if not event.is_ability_activate:
                return False
            sel = self.selector or ctx.boss_names
            if sel and event.source not in sel:
                return False
            return (self.keyword or "").lower() in (event.ability or "").lower()

        if self.type == "effect_applied":
            if event.is_effect_removed:
                return False
            if self.selector and event.source not in self.selector:
                return False
            return (self.keyword or "").lower() in (event.effect_name or "").lower()

        if self.type == "effect_removed":
            if not event.is_effect_removed:
                return False
            if self.selector and event.source not in self.selector:
                return False
            return (self.keyword or "").lower() in (event.effect_name or "").lower()

        if self.type == "npc_appears":
            for name in (event.source, event.target):
                if name and name in self.selector and name not in ctx.seen_entities:
                    if commit:
                        ctx.seen_entities.add(name)
                    return True
            return False

        if self.type == "entity_death":
            return event.is_death and event.target in self.selector

        if self.type == "timer_expires":
            return self.timer_id in ctx.recently_expired_timer_ids

        if self.type == "timer_started":
            return self.timer_id in ctx.recently_started_timer_ids

        if self.type == "timer_time_remaining":
            # Compares the time left on a currently-running timer. A timer
            # that isn't running at all has no remaining time, so every
            # comparison against it is False -- callers that want "not
            # running" should wrap this in `not` (which is exactly how
            # BARAS's own definitions use it).
            if ctx.timer_engine is None or not self.timer_id:
                return False
            remaining = ctx.timer_engine.remaining_for_definition_id(self.timer_id)
            if remaining is None:
                return False
            op = _OPERATORS.get(self.operator or "gte")
            if op is None:
                return False
            return op(remaining, self.value if self.value is not None else 0)

        if self.type == "phase_ended":
            return self.phase_id in ctx.recently_ended_phase_ids

        if self.type == "phase_entered":
            return self.phase_id in ctx.recently_entered_phase_ids

        if self.type == "phase_active":
            return ctx.active_phase_id in self.phase_ids

        if self.type == "any_phase_change":
            return bool(ctx.recently_entered_phase_ids or ctx.recently_ended_phase_ids)

        if self.type == "counter_compare":
            current = ctx.counters.get(self.counter_id, 0)
            op = _OPERATORS.get(self.operator or "eq")
            return op(current, self.value if self.value is not None else 0) if op else False

        if self.type == "counter_reaches":
            # Edge-triggered: fires once when the counter first reaches the
            # value, NOT continuously for the rest of the pull afterwards
            # (that's what counter_compare with "gte" is for). Matches
            # BARAS's own naming/semantics -- "reaches" is a moment, not a
            # standing state.
            current = ctx.counters.get(self.counter_id, 0)
            target = self.value if self.value is not None else 0
            if current < target:
                return False
            # id(self) -- same reasoning as hp_below above: without it, two
            # separate counter_reaches timers sharing a (counter_id, value)
            # pair collide on one shared one-time edge trigger.
            key = (id(self), self.counter_id, target)
            if key in ctx.fired_counter_reaches:
                return False
            if commit:
                ctx.fired_counter_reaches.add(key)
            return True

        if self.type == "counter_changes":
            return self.counter_id in ctx.changed_counter_ids

        if self.type == "any_of":
            return any(c.matches(ctx, commit) for c in self.conditions)

        if self.type == "all_of":
            if not self.conditions:
                return False
            return all(c.matches(ctx, commit) for c in self.conditions)

        if self.type == "not":
            # Always evaluated read-only: a negated edge trigger must not
            # consume its one-shot state as a side effect of being *checked*
            # (and a `not` that matched would mean the inner one didn't fire
            # anyway, so there is nothing legitimate to commit).
            if self.condition is None:
                return False
            return not self.condition.matches(ctx, commit=False)

        return False


def _evaluate_and_commit(
    trigger: Optional[Condition], conditions: List[Condition], ctx: EvalContext
) -> bool:
    """Evaluates `trigger` (if any) AND every item in `conditions` as one
    gate, without applying any edge-trigger side effects until the WHOLE
    gate is confirmed to pass -- then re-evaluates with commit=True so
    those side effects (hp_below/npc_appears) actually get applied. This
    prevents a one-shot trigger from being permanently consumed by an event
    that matched the trigger but got blocked by one of the extra
    `conditions`, which would otherwise mean it could never fire again for
    the rest of the pull."""
    if trigger is not None and not trigger.matches(ctx, commit=False):
        return False
    for c in conditions:
        if not c.matches(ctx, commit=False):
            return False
    if trigger is not None:
        trigger.matches(ctx, commit=True)
    for c in conditions:
        c.matches(ctx, commit=True)
    return True


def _load_condition(data: Optional[dict]) -> Optional[Condition]:
    if not data:
        return None
    return Condition(
        type=data.get("type", ""),
        percent=data.get("percent"),
        keyword=data.get("keyword"),
        target=data.get("target"),
        selector=data.get("selector", []),
        timer_id=data.get("timer_id"),
        phase_id=data.get("phase_id"),
        phase_ids=data.get("phase_ids", []),
        counter_id=data.get("counter_id"),
        operator=data.get("operator"),
        value=data.get("value"),
        conditions=[_load_condition(c) for c in data.get("conditions", [])],
        condition=_load_condition(data.get("condition")),
    )


@dataclass
class BossPhase:
    id: str
    name: str
    start_trigger: Optional[Condition] = None  # None = the initial phase
    conditions: List[Condition] = field(default_factory=list)  # extra AND-gates
    # Must independently match before this phase counts as "ended" for any
    # OTHER phase's phase_ended(this_id) condition to react to -- without
    # this, phase_ended would be trivially true the instant this phase is
    # merely no longer the newest one, regardless of what actually happened.
    end_trigger: Optional[Condition] = None

    def can_start(self, ctx: EvalContext) -> bool:
        if self.start_trigger is None:
            return False
        return _evaluate_and_commit(self.start_trigger, self.conditions, ctx)


@dataclass
class BossCounter:
    id: str
    name: str
    increment_on: Optional[Condition] = None
    decrement_on: Optional[Condition] = None
    reset_on: Optional[Condition] = None
    initial_value: float = 0


@dataclass
class BossTimerDef:
    label: str
    duration_seconds: float
    id: Optional[str] = None  # needed for other timers to chain off this one
    warn_seconds_before: float = 0.0
    voice_alert: bool = True
    phases: List[str] = field(default_factory=list)  # empty = all phases
    trigger: Optional[Condition] = None
    conditions: List[Condition] = field(default_factory=list)  # extra AND-gates
    repeat_interval_seconds: Optional[float] = None
    repeat_count: int = 0
    # If set, matching this condition while an instance of this timer is
    # running removes it early (e.g. an enrage timer cancelled once you
    # reach a phase that ends the threat, instead of running its full
    # duration). Cancellation is silent -- it does not count as "expiry"
    # for timer_expires chaining.
    cancel_trigger: Optional[Condition] = None
    # Legacy simple form, kept for backward compatibility with definitions
    # that don't need a Condition at all -- just a keyword match.
    keyword: str = ""
    # BARAS's own display-mode flag: render as a brief callout (e.g. "Move
    # Out", "Spread!") instead of a numeric countdown bar -- the timing data
    # is identical either way, this only changes how the GUI/overlay shows
    # it. See timers.py's ActiveTimer.is_alert for where it actually lands.
    is_alert: bool = False

    def active_in(self, phase_id: Optional[str]) -> bool:
        return not self.phases or phase_id in self.phases

    def matches(self, ctx: EvalContext) -> bool:
        if self.trigger is None:
            return False
        return _evaluate_and_commit(self.trigger, self.conditions, ctx)


@dataclass
class BossDefinition:
    id: str
    name: str
    boss_names: List[str]
    phases: List[BossPhase]
    timers: List[BossTimerDef] = field(default_factory=list)
    counters: List[BossCounter] = field(default_factory=list)
    # Alternate recognition path for fights where boss_names would be too
    # generic to safely match on (e.g. an add-wave fight whose only named
    # entity is literally "Adds", which could collide with any other
    # fight's trash mobs) -- recognized via a specific ability cast instead.
    encounter_trigger: Optional[Condition] = None
    # The game's NPC *type* ids for this boss's own entities. Preferred over
    # boss_names when present, because display names are not unique: the
    # Dread Council fight and the four solo Dread Master fights log identical
    # names under different type ids, so name matching alone would recognize
    # the wrong encounter. This is what BARAS matches on.
    boss_npc_ids: List[str] = field(default_factory=list)

    def matches_event(self, event: CombatEvent, ctx: Optional[EvalContext] = None) -> bool:
        if self.boss_npc_ids:
            # Definition knows its real ids -- trust ONLY those. Falling back
            # to the name here would re-introduce exactly the cross-encounter
            # collision the ids exist to resolve.
            if (
                event.source_npc_id in self.boss_npc_ids
                or event.target_npc_id in self.boss_npc_ids
            ):
                return True
        elif event.source in self.boss_names or event.target in self.boss_names:
            return True
        if self.encounter_trigger is not None and ctx is not None:
            return self.encounter_trigger.matches(ctx)
        return False

    def first_phase(self) -> Optional[BossPhase]:
        return self.phases[0] if self.phases else None

    def phase_by_id(self, phase_id: Optional[str]) -> Optional[BossPhase]:
        for p in self.phases:
            if p.id == phase_id:
                return p
        return None

    def timer_by_id(self, timer_id: Optional[str]) -> Optional[BossTimerDef]:
        for t in self.timers:
            if t.id == timer_id:
                return t
        return None


def _definition_from_dict(data: dict) -> BossDefinition:
    """Constructs a BossDefinition straight from an already-parsed JSON
    dict -- the shared core of both _load_one (reads a bundled/user file)
    and the /api/encounters POST handler (validates+constructs from a
    request body without touching the filesystem). Raises KeyError/
    TypeError/ValueError on malformed input; callers that need a soft
    failure (like _load_one) catch it there, callers that need to report
    it to a user (the API) catch it at that layer instead."""
    phases = [
        BossPhase(
            id=p["id"], name=p.get("name", p["id"]),
            start_trigger=_load_condition(p.get("start_trigger") or p.get("trigger")),
            conditions=[_load_condition(c) for c in p.get("conditions", [])],
            end_trigger=_load_condition(p.get("end_trigger")),
        )
        for p in data.get("phases", [])
    ]

    counters = [
        BossCounter(
            id=c["id"], name=c.get("name", c["id"]),
            increment_on=_load_condition(c.get("increment_on")),
            decrement_on=_load_condition(c.get("decrement_on")),
            reset_on=_load_condition(c.get("reset_on")),
            initial_value=c.get("initial_value", 0),
        )
        for c in data.get("counters", [])
    ]

    timers = [
        BossTimerDef(
            id=t.get("id"),
            keyword=t.get("keyword", ""),
            label=t.get("label", t.get("keyword", "")),
            duration_seconds=t.get("duration_seconds", 10.0),
            warn_seconds_before=t.get("warn_seconds_before", 0.0),
            voice_alert=t.get("voice_alert", True),
            phases=t.get("phases", []),
            trigger=_load_condition(t.get("trigger")),
            conditions=[_load_condition(c) for c in t.get("conditions", [])],
            repeat_interval_seconds=t.get("repeat_interval_seconds"),
            repeat_count=t.get("repeat_count", 0),
            cancel_trigger=_load_condition(t.get("cancel_trigger")),
            is_alert=t.get("is_alert", False),
        )
        for t in data.get("timers", [])
    ]
    for t in timers:
        if t.cancel_trigger is not None and not t.id:
            print(
                f"WARNING [{data.get('id', '?')}]: timer '{t.label}' has a cancel_trigger "
                f"but no 'id' -- it will never actually be cancelled. Give it an id."
            )

    return BossDefinition(
        id=data["id"],
        name=data.get("name", data["id"]),
        boss_names=data.get("boss_names", []),
        phases=phases,
        timers=timers,
        counters=counters,
        encounter_trigger=_load_condition(data.get("encounter_trigger")),
        boss_npc_ids=[str(i) for i in data.get("boss_npc_ids", [])],
    )


def _load_one(path: Path) -> Optional[BossDefinition]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return _definition_from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def load_definitions(*directories: Path) -> Dict[str, BossDefinition]:
    """Loads all *.json boss definitions from the given directories, in
    order -- later directories override earlier ones for the same id (so
    pass bundled dir first, user override dir last)."""
    result: Dict[str, BossDefinition] = {}
    for directory in directories:
        if not directory or not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            definition = _load_one(path)
            if definition is not None:
                result[definition.id] = definition
    return result


def match_boss(
    definitions: Dict[str, BossDefinition], event: CombatEvent, ctx: Optional[EvalContext] = None
) -> Optional[BossDefinition]:
    for definition in definitions.values():
        if definition.matches_event(event, ctx):
            return definition
    return None
