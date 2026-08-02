"""
boss_intelligence.py

Orchestrates boss recognition, general (non-linear) phase transitions,
counters, and timer firing/chaining/cancellation for the currently active
pull, using the loaded BossDefinitions. This sits alongside StatsTracker
and TimerEngine, fed the same event stream.

Unlike a strictly-linear phase model, ANY defined phase can become active
whenever its start_trigger (+ conditions) matches and it isn't already the
active phase -- so fights that cycle between phases (e.g. alternating
"boss" and "add" phases, like Red <-> Bull) are representable, not just
fights that only ever move forward.

Per event, in order:
  1. Update counters (increment/decrement/reset).
  2. Try a phase transition: check every OTHER phase's start_trigger
     (including phase_ended conditions referencing the phase we're about
     to leave), and take the first match.
  3. Process boss timers: cancel any whose cancel_trigger now matches
     (removed silently, not counted as expiry), then fire any whose
     trigger (+ conditions) matches, respecting phase scoping. Chained
     timers (trigger type "timer_expires") use
     TimerEngine.pop_recently_expired_ids(), collected at the start of
     this event's processing.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from boss_definitions import BossDefinition, EvalContext, match_boss
from log_parser import CombatEvent


@dataclass
class PhaseChange:
    boss_id: str
    boss_name: str
    phase_id: str
    phase_name: str
    at: float = field(default_factory=time.time)


class BossEncounterState:
    def __init__(self, definitions: Dict[str, BossDefinition]):
        self.definitions = definitions
        self.active_boss: Optional[BossDefinition] = None
        self.active_phase_id: Optional[str] = None
        self.history: List[PhaseChange] = []
        self.counters: Dict[str, float] = {}
        self.seen_entities: set = set()
        self.fired_hp_thresholds: set = set()
        self.fired_counter_reaches: set = set()
        # Per-event scratch: which counters moved while processing the current
        # event. Cleared at the top of each feed() so `counter_changes` means
        # "changed on THIS event", not "changed at some point this pull".
        self._changed_counter_ids: set = set()
        # Best-effort local player detection: first player entity seen this
        # whole run (not reset between pulls). Same heuristic BARAS itself
        # uses -- not a guaranteed-correct identification in a full group,
        # but a reasonable one since it's your own client's log.
        self.local_player_name: Optional[str] = None
        # Live HP snapshot for the boss overlay -- updated from whichever
        # event most recently carried an HP fraction for an entity matching
        # active_boss.boss_names (the same selector hp_below conditions
        # already use). None until the first such event arrives.
        self.boss_hp_current: Optional[float] = None
        self.boss_hp_max: Optional[float] = None
        # Who the boss is currently attacking -- the target of the most
        # recent event where a boss_names entity was the SOURCE and the
        # target was a player. Multi-body encounters (several simultaneous
        # boss_names entities) can make this jump between bodies; same
        # known trade-off hp_below's selector already accepts.
        self.boss_target: Optional[str] = None

    def _note_local_player(self, event: CombatEvent) -> None:
        if self.local_player_name is not None:
            return
        if event.source_is_player and event.source:
            self.local_player_name = event.source
        elif event.target_is_player and event.target:
            self.local_player_name = event.target

    def _update_hp_and_target(self, event: CombatEvent) -> None:
        """No-op until a boss is recognized -- called both before and after
        recognition happens within the same feed() call, so the very event
        that identifies the boss can also seed its first HP/target reading
        if that line happens to carry one."""
        if self.active_boss is None:
            return
        names = self.active_boss.boss_names
        if event.target in names and event.hp_current is not None and event.hp_max:
            self.boss_hp_current = event.hp_current
            self.boss_hp_max = event.hp_max
        if event.source in names and event.target_is_player and event.target:
            self.boss_target = event.target

    def boss_hp_percent(self) -> Optional[float]:
        if self.boss_hp_current is None or not self.boss_hp_max:
            return None
        return max(0.0, min(100.0, (self.boss_hp_current / self.boss_hp_max) * 100))

    def reset(self) -> None:
        """Call when a new pull starts (StatsTracker rolled over to a fresh
        Encounter) so boss/phase/counter state doesn't leak from the
        previous fight. Local player detection persists across pulls."""
        self.active_boss = None
        self.active_phase_id = None
        self.counters = {}
        self.seen_entities = set()
        self.fired_hp_thresholds = set()
        self.fired_counter_reaches = set()
        self._changed_counter_ids = set()
        self.boss_hp_current = None
        self.boss_hp_max = None
        self.boss_target = None

    def _init_counters(self) -> None:
        self.counters = {c.id: c.initial_value for c in self.active_boss.counters}

    def _update_counters(self, ctx: EvalContext) -> None:
        """Applies counter changes for this event, and records which counters
        actually moved so a `counter_changes` condition can react to it later
        in the same event's processing."""
        if self.active_boss is None:
            return
        for c in self.active_boss.counters:
            before = self.counters.get(c.id, c.initial_value)
            if c.reset_on and c.reset_on.matches(ctx):
                self.counters[c.id] = c.initial_value
            else:
                if c.increment_on and c.increment_on.matches(ctx):
                    self.counters[c.id] = self.counters.get(c.id, c.initial_value) + 1
                if c.decrement_on and c.decrement_on.matches(ctx):
                    self.counters[c.id] = self.counters.get(c.id, c.initial_value) - 1
            if self.counters.get(c.id, c.initial_value) != before:
                self._changed_counter_ids.add(c.id)

    def _make_context(self, event: CombatEvent, expired_ids: List[str],
                       ended_ids: List[str], entered_ids: List[str],
                       active_phase_id: Optional[str] = None,
                       started_ids: Optional[List[str]] = None,
                       timer_engine=None) -> EvalContext:
        boss_names = self.active_boss.boss_names if self.active_boss else []
        return EvalContext(
            event=event, boss_names=boss_names, local_player_name=self.local_player_name,
            counters=self.counters, seen_entities=self.seen_entities,
            recently_expired_timer_ids=expired_ids, recently_ended_phase_ids=ended_ids,
            fired_hp_thresholds=self.fired_hp_thresholds, recently_entered_phase_ids=entered_ids,
            active_phase_id=active_phase_id if active_phase_id is not None else self.active_phase_id,
            fired_counter_reaches=self.fired_counter_reaches,
            recently_started_timer_ids=started_ids if started_ids is not None else [],
            changed_counter_ids=self._changed_counter_ids,
            timer_engine=timer_engine,
        )

    def feed(self, event: CombatEvent, timer_engine=None) -> Optional[PhaseChange]:
        self._note_local_player(event)
        self._update_hp_and_target(event)
        expired_ids = timer_engine.pop_recently_expired_ids() if timer_engine else []
        started_ids = timer_engine.pop_recently_started_ids() if timer_engine else []
        # `counter_changes` means "changed on this event" -- reset the scratch
        # set before anything can add to it.
        self._changed_counter_ids = set()

        if self.active_boss is None:
            recognition_ctx = EvalContext(
                event=event, boss_names=[], local_player_name=self.local_player_name,
                counters={}, seen_entities=self.seen_entities,
                recently_expired_timer_ids=expired_ids, recently_ended_phase_ids=[],
                fired_hp_thresholds=self.fired_hp_thresholds, recently_entered_phase_ids=[],
                active_phase_id=None,
                fired_counter_reaches=self.fired_counter_reaches,
                recently_started_timer_ids=started_ids,
                changed_counter_ids=self._changed_counter_ids,
                timer_engine=timer_engine,
            )
            matched = match_boss(self.definitions, event, recognition_ctx)
            if matched is None or not matched.phases:
                return None
            self.active_boss = matched
            self._update_hp_and_target(event)  # active_boss was still None on this event's first call above
            self._init_counters()
            first = matched.phases[0]
            self.active_phase_id = first.id
            change = PhaseChange(matched.id, matched.name, first.id, first.name)
            self.history.append(change)
            ctx = self._make_context(
                event, expired_ids, [], [first.id],
                started_ids=started_ids, timer_engine=timer_engine,
            )
            self._update_counters(ctx)
            self._process_timers(ctx, timer_engine)
            return change

        # Build a preliminary context (no ended/entered yet) for counters
        # and for probing phase transitions.
        prelim_ctx = self._make_context(
            event, expired_ids, [], [], started_ids=started_ids, timer_engine=timer_engine,
        )
        self._update_counters(prelim_ctx)

        change, ended_id = self._try_phase_transition(prelim_ctx)
        ended_ids = [ended_id] if ended_id else []
        entered_ids = [change.phase_id] if change else []

        final_ctx = self._make_context(
            event, expired_ids, ended_ids, entered_ids,
            started_ids=started_ids, timer_engine=timer_engine,
        )
        self._process_timers(final_ctx, timer_engine)
        return change

    def _try_phase_transition(self, ctx: EvalContext):
        """Returns (PhaseChange or None, ended_phase_id or None). A phase
        only counts as "ended" (for other phases' phase_ended conditions)
        if it has an end_trigger and that independently matches THIS event
        -- otherwise phase_ended would be trivially true the instant any
        other phase's start_trigger matches, regardless of what actually
        happened.

        Two passes, since a phase can end two different ways:
        - DIRECTLY: some other phase's start_trigger matches on its own
          (e.g. a specific ability cast), with no dependency on
          phase_ended at all. That phase becomes active immediately, and
          the old phase is implicitly reported as ended (useful for a
          THIRD timer/phase elsewhere that reacts to phase_ended on it,
          even though this phase never declared its own end_trigger).
        - VIA end_trigger: nothing else's start_trigger matches directly,
          but this phase's own end_trigger does -- only then does
          phase_ended(this) become available for other phases' start
          conditions to react to (checked in a second pass), preventing
          phase_ended from being trivially true whenever the ONLY thing
          that would ever consume it is that same signal (a phase whose
          sole path back in is via phase_ended must have an explicit,
          independently-matching end_trigger on the phase it's leaving).
        """
        boss = self.active_boss
        old_phase_id = self.active_phase_id
        old_phase = boss.phase_by_id(old_phase_id)

        bare_ctx = EvalContext(
            ctx.event, ctx.boss_names, ctx.local_player_name, ctx.counters,
            ctx.seen_entities, ctx.recently_expired_timer_ids, [],
            ctx.fired_hp_thresholds, [], old_phase_id,
            fired_counter_reaches=ctx.fired_counter_reaches,
            recently_started_timer_ids=ctx.recently_started_timer_ids,
            changed_counter_ids=ctx.changed_counter_ids,
            timer_engine=ctx.timer_engine,
        )
        for phase in boss.phases:
            if phase.id == old_phase_id:
                continue
            if phase.can_start(bare_ctx):
                self.active_phase_id = phase.id
                change = PhaseChange(boss.id, boss.name, phase.id, phase.name)
                self.history.append(change)
                return change, old_phase_id

        if not (old_phase and old_phase.end_trigger and old_phase.end_trigger.matches(ctx)):
            return None, None

        ended_ids = [old_phase_id]
        for phase in boss.phases:
            if phase.id == old_phase_id:
                continue
            trial_ctx = EvalContext(
                ctx.event, ctx.boss_names, ctx.local_player_name, ctx.counters,
                ctx.seen_entities, ctx.recently_expired_timer_ids, ended_ids,
                ctx.fired_hp_thresholds, [], old_phase_id,
                fired_counter_reaches=ctx.fired_counter_reaches,
                recently_started_timer_ids=ctx.recently_started_timer_ids,
                changed_counter_ids=ctx.changed_counter_ids,
                timer_engine=ctx.timer_engine,
            )
            if phase.can_start(trial_ctx):
                self.active_phase_id = phase.id
                change = PhaseChange(boss.id, boss.name, phase.id, phase.name)
                self.history.append(change)
                return change, old_phase_id

        return None, old_phase_id

    def _process_timers(self, ctx: EvalContext, timer_engine) -> None:
        if timer_engine is None or self.active_boss is None:
            return
        for t in self.active_boss.timers:
            if t.cancel_trigger is not None and t.id and t.cancel_trigger.matches(ctx):
                timer_engine.cancel_by_definition_id(t.id)
            if not t.active_in(self.active_phase_id):
                continue
            if t.matches(ctx):
                timer_engine.start_timer(
                    t.label, t.duration_seconds, t.warn_seconds_before, t.voice_alert,
                    repeat_interval_seconds=t.repeat_interval_seconds,
                    repeat_count=t.repeat_count, definition_id=t.id, category="boss",
                    is_alert=t.is_alert,
                )

    def status_text(self) -> str:
        if self.active_boss is None:
            return "No boss encounter active"
        phase = self.active_boss.phase_by_id(self.active_phase_id)
        phase_name = phase.name if phase else "?"
        return f"{self.active_boss.name} \u2014 {phase_name}"
