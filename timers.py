"""
timers.py

ORBS-style custom timers: define a trigger (a keyword matched against an
event's ability/effect name) and a duration, and the timer engine tracks a
live countdown from the moment that trigger fires -- e.g. "warn me 3 seconds
before this boss's next Massive Slam" or "track my own cooldown on X".

Alerts:
- voice_alert: speaks the label out loud the moment the trigger fires
  (falls back to a beep if TTS isn't available -- see audio.py).
- warn_seconds_before: an additional alert fired once, when the countdown
  reaches that many seconds remaining -- e.g. a 15s timer with
  warn_seconds_before=3 speaks/beeps again at the 3-second mark, on top of
  the initial trigger alert.

Two ways a timer can end and hand off to the next mechanic:
- Self-repeat: repeat_interval_seconds/repeat_count re-arm the SAME timer
  N more times (e.g. "spawns every 90s, up to 10 times").
- Chaining: a boss timer with an `id` reports into
  TimerEngine.pop_recently_expired_ids() when it finally, permanently
  expires (after any repeats are exhausted). boss_intelligence.py uses that
  to fire a DIFFERENT timer whose trigger is "timer_expires" referencing
  this one's id -- e.g. a fixed rotation of distinct mechanics, each
  starting exactly when the previous one's window closes.

This is intentionally simple (keyword match for the legacy path, or a
Condition for the richer boss-intelligence path) -- not a general scripting
language. Manually-added rules from the Timers tab use the legacy keyword
path and don't chain. Call TimerEngine.tick() periodically (e.g. from a
GUI refresh loop) so the warn-before-expiry alert fires even between log
events.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from log_parser import CombatEvent
import audio


@dataclass
class TimerRule:
    keyword: str          # matched case-insensitively against ability/effect name
    label: str             # display name for the countdown, and what gets spoken
    duration_seconds: float
    only_target: Optional[str] = None  # if set, only trigger when this is the target
    only_source: Optional[str] = None  # if set, only trigger when this is the source
    voice_alert: bool = True           # speak/beep the moment the trigger fires
    warn_seconds_before: float = 0.0   # extra alert this many seconds before expiry (0 = off)
    # Boss-intelligence gating: if set, this rule only arms while that boss
    # (and, if phases given, one of those phases) is the active encounter.
    # Manually-added rules from the Timers tab leave these unset, so they
    # keep working globally exactly as before.
    required_boss: Optional[str] = None
    required_phases: List[str] = field(default_factory=list)
    # "custom" (manual Timers-tab rules), "boss" (from a boss definition),
    # or "cooldown" (personal defensive-cooldown tracking) -- lets the GUI
    # show these in separate panels instead of one mixed list.
    category: str = "custom"
    # "applied" | "removed" | None (either) -- lets the legacy keyword path
    # distinguish an effect being applied from it being removed, instead of
    # matching on name alone regardless of which. Needed for e.g. a buff
    # timer that shouldn't re-arm itself when the SAME ability name shows
    # up again in the log line that ends it.
    event_type: Optional[str] = None
    # If set, only trigger when the event's source is the detected local
    # player -- used for personal cooldowns, where a teammate casting the
    # same-named ability (e.g. two Guardians both with Saber Ward) shouldn't
    # re-trigger your own cooldown display. Ignored (permissive) until the
    # local player has actually been detected.
    only_local_player: bool = False
    # Same idea, but for buffs where WHO cast it doesn't matter -- only
    # whether it landed on you (e.g. a shield a healer put on you). Distinct
    # from only_local_player: that answers "did I cast this", this answers
    # "is this currently on me".
    only_target_is_local_player: bool = False
    # See boss_definitions.BossTimerDef.is_alert -- same flag, threaded
    # through the legacy keyword path too so a manually/boss-registered
    # TimerRule can request the callout display instead of a countdown.
    is_alert: bool = False

    def armed_for(self, boss_id: Optional[str], phase_id: Optional[str]) -> bool:
        if self.required_boss is None:
            return True
        if boss_id != self.required_boss:
            return False
        return not self.required_phases or phase_id in self.required_phases


@dataclass
class ActiveTimer:
    label: str
    started_at: float
    duration_seconds: float
    warn_seconds_before: float = 0.0
    warned: bool = False
    voice_alert: bool = True
    # Self-repeating timer support (e.g. "spawns every 90s, up to 10 times"):
    # when this timer expires, if repeats_remaining > 0, it re-arms itself
    # with repeat_interval_seconds as the new duration instead of vanishing.
    repeat_interval_seconds: Optional[float] = None
    repeats_remaining: int = 0
    # Links back to the BossTimerDef.id that created this, so its FINAL
    # expiry (after any repeats) can be reported for chaining. None for
    # timers not tied to a boss definition (e.g. manual Timers-tab rules).
    definition_id: Optional[str] = None
    category: str = "custom"
    is_alert: bool = False
    # When set, a later start_timer() call with the same (label, category,
    # dedupe_key) refreshes this entry in place instead of appending a new
    # one -- e.g. re-landing your Kolto Probe on the SAME target restarts
    # its countdown (what the game does), but Kolto Probe on a different
    # target is a genuinely separate instance and still gets its own row.
    dedupe_key: Optional[str] = None

    def remaining(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        return max(self.duration_seconds - (now - self.started_at), 0.0)

    def expired(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) <= 0.0


class TimerEngine:
    def __init__(self):
        self.rules: List[TimerRule] = []
        self.active: List[ActiveTimer] = []
        self._recently_expired_ids: List[str] = []
        # Mirror of _recently_expired_ids for the START of a boss timer, so a
        # definition can chain off "that other timer just began" as well as
        # "that other timer finally ended" (BARAS's `timer_started`).
        self._recently_started_ids: List[str] = []
        # Guards all state above: fed from the background log-reader thread
        # (main.py) and read/mutated from the Tk GUI thread's refresh loop
        # (gui.py) roughly every 500ms -- without this, concurrent tick()s
        # from both threads can race in _prune_and_warn (resurrecting an
        # already-expired timer, dropping/duplicating expired-id entries,
        # double-firing voice alerts). Reentrant since feed()/tick() call
        # _prune_and_warn() and start_timer() while already holding it.
        self._lock = threading.RLock()

    def add_rule(self, rule: TimerRule) -> None:
        with self._lock:
            self.rules.append(rule)

    def remove_rule(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self.rules):
                self.rules.pop(index)

    def start_timer(
        self, label: str, duration_seconds: float, warn_seconds_before: float = 0.0,
        voice_alert: bool = True, repeat_interval_seconds: Optional[float] = None,
        repeat_count: int = 0, definition_id: Optional[str] = None, category: str = "custom",
        is_alert: bool = False, dedupe_key: Optional[str] = None,
    ) -> None:
        """Directly starts a countdown, bypassing keyword matching -- used
        by boss_intelligence.py for the richer (non-keyword) trigger types.
        If repeat_interval_seconds is set, the timer re-arms itself that
        many times after first expiring. If definition_id is set, its FINAL
        expiry (after repeats) is reported via pop_recently_expired_ids()
        for other timers to chain off of.

        If dedupe_key is given and an active timer with the same (label,
        category, dedupe_key) already exists, that entry is refreshed in
        place instead of a new one being appended -- prevents a
        rapidly-retriggered effect (e.g. Kolto Pods ticking on the same
        target every ~0.9s) from piling up near-duplicate rows."""
        with self._lock:
            if dedupe_key is not None:
                for t in self.active:
                    if t.label == label and t.category == category and t.dedupe_key == dedupe_key:
                        t.started_at = time.time()
                        t.duration_seconds = duration_seconds
                        t.warn_seconds_before = warn_seconds_before
                        t.warned = False
                        t.voice_alert = voice_alert
                        if voice_alert:
                            audio.speak(label)
                        return
            self.active.append(
                ActiveTimer(
                    label=label,
                    started_at=time.time(),
                    duration_seconds=duration_seconds,
                    warn_seconds_before=warn_seconds_before,
                    voice_alert=voice_alert,
                    repeat_interval_seconds=repeat_interval_seconds,
                    repeats_remaining=repeat_count,
                    definition_id=definition_id,
                    category=category,
                    is_alert=is_alert,
                    dedupe_key=dedupe_key,
                )
            )
            if definition_id:
                self._recently_started_ids.append(definition_id)
        if voice_alert:
            audio.speak(label)

    def cancel_by_definition_id(self, definition_id: str) -> None:
        """Silently removes any active timer(s) tied to this boss timer
        definition -- used for cancel_trigger, where a timer stops early
        instead of running its full duration. Does not count as expiry, so
        it never fires timer_expires chains."""
        with self._lock:
            self.active = [t for t in self.active if t.definition_id != definition_id]

    def pop_recently_expired_ids(self) -> List[str]:
        """Returns the definition_ids of boss timers that FINALLY expired
        (repeats exhausted, if any) since the last call, and clears the
        list. Used by boss_intelligence.py to fire chained timers."""
        with self._lock:
            ids = self._recently_expired_ids
            self._recently_expired_ids = []
            return ids

    def pop_recently_started_ids(self) -> List[str]:
        """Returns the definition_ids of boss timers that STARTED since the
        last call, and clears the list -- the timer_started counterpart to
        pop_recently_expired_ids()."""
        with self._lock:
            ids = self._recently_started_ids
            self._recently_started_ids = []
            return ids

    def remaining_for_definition_id(self, definition_id: str) -> Optional[float]:
        """Seconds left on the active timer for this boss timer definition, or
        None if no instance of it is currently running. If several are somehow
        active at once, reports the one with the most time left (the newest
        arming), matching "is this mechanic still pending" intent."""
        with self._lock:
            now = time.time()
            remainings = [
                t.remaining(now) for t in self.active if t.definition_id == definition_id
            ]
            return max(remainings) if remainings else None

    def feed(
        self, event: CombatEvent, boss_id: Optional[str] = None, phase_id: Optional[str] = None,
        local_player_name: Optional[str] = None,
    ) -> None:
        with self._lock:
            haystack = " ".join(filter(None, [event.ability, event.effect_name])).lower()
            for rule in self.rules:
                if not rule.armed_for(boss_id, phase_id):
                    continue
                if rule.keyword.lower() not in haystack:
                    continue
                if rule.event_type == "applied" and event.is_effect_removed:
                    continue
                if rule.event_type == "removed" and not event.is_effect_removed:
                    continue
                if rule.only_target and event.target != rule.only_target:
                    continue
                if rule.only_source and event.source != rule.only_source:
                    continue
                if (
                    rule.only_local_player
                    and local_player_name is not None
                    and event.source != local_player_name
                ):
                    continue
                if (
                    rule.only_target_is_local_player
                    and local_player_name is not None
                    and event.target != local_player_name
                ):
                    continue

                self.start_timer(
                    rule.label, rule.duration_seconds, rule.warn_seconds_before, rule.voice_alert,
                    category=rule.category, is_alert=rule.is_alert,
                    dedupe_key=event.target if rule.category in ("dot", "hot") else None,
                )

            self._prune_and_warn()

    def tick(self) -> None:
        """Call periodically (independent of log events) so the
        warn-before-expiry alert fires even if no new log lines arrive."""
        with self._lock:
            self._prune_and_warn()

    def _prune_and_warn(self) -> None:
        now = time.time()
        still_active = []
        for t in self.active:
            if t.expired(now):
                if t.repeat_interval_seconds is not None and t.repeats_remaining > 0:
                    t.started_at = now
                    t.duration_seconds = t.repeat_interval_seconds
                    t.repeats_remaining -= 1
                    t.warned = False
                    if t.voice_alert:
                        audio.speak(t.label)
                    still_active.append(t)
                else:
                    if t.definition_id:
                        self._recently_expired_ids.append(t.definition_id)
                continue
            if (
                t.warn_seconds_before > 0
                and not t.warned
                and t.remaining(now) <= t.warn_seconds_before
            ):
                audio.speak(f"{t.label} ending")
                t.warned = True
            still_active.append(t)
        self.active = still_active

    def snapshot(self, category: Optional[str] = None):
        """Returns list of (label, remaining_seconds, total_seconds, category,
        is_alert) sorted soonest-expiring first. Pass category to filter
        (e.g. "cooldown" for the personal-cooldowns panel vs everything
        else)."""
        with self._lock:
            now = time.time()
            rows = [
                (t.label, t.remaining(now), t.duration_seconds, t.category, t.is_alert)
                for t in self.active
                if category is None or t.category == category
            ]
        rows.sort(key=lambda r: r[1])
        return rows
