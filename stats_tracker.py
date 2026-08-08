"""Thread-safe live encounter tracking."""

import threading
import time
from typing import List, Optional

from log_parser import CombatEvent, parse_line
from stats_models import (
    Encounter, PlayerStats,
    ENCOUNTER_GAP_SECONDS, TRAILING_CAPTURE_SECONDS, NEW_PULL_MIN_GAP_SECONDS,
    MIN_ENCOUNTER_SECONDS, HISTORY_DATA_VERSION, RAID_DEFEATED_FRACTION, HISTORY_LIMIT,
)

class StatsTracker:
    """Owns the current encounter and rolls over to a fresh one once the
    trailing capture window has elapsed."""

    def __init__(self, preloaded_history: Optional[List[Encounter]] = None):
        self.current = Encounter()
        self.history: List[Encounter] = list(preloaded_history or [])[-HISTORY_LIMIT:]
        self.current_log_path: Optional[str] = None
        self.last_area_entered_line: Optional[int] = None
        # The last encounter that had real damage/healing in it -- see
        # display_encounter(). Rolling `current` over to a fresh Encounter()
        # happens on ANY event once the trailing window has passed, not just
        # a real new pull (e.g. someone popping a stim between pulls counts),
        # so without this the meter would blank out the instant that stray
        # event arrives, well before the next actual fight starts.
        self._last_meaningful: Optional[Encounter] = None
        # Wall-clock time of the last EnterCombat, for the same new-pull
        # detection the offline replay uses -- see NEW_PULL_MIN_GAP_SECONDS.
        self._last_enter_combat: Optional[float] = None
        # Guards self.current/self.history: feed() is called from the
        # background log-reader thread while snapshot()/history_snapshot()
        # are polled from the Tk GUI thread roughly every 500ms. Without
        # this, the GUI thread can iterate self.current.players.values()
        # (in Encounter.snapshot()) at the same moment the reader thread
        # inserts a new player into that dict, raising "dictionary changed
        # size during iteration" and crashing the refresh loop.
        self._lock = threading.Lock()

    def set_log_path(self, path: str) -> None:
        self.current_log_path = path
        self.current.log_path = path

    def _append_history_unlocked(self, encounter: "Encounter") -> None:
        """Appends and trims to HISTORY_LIMIT. Callers already inside
        `with self._lock:` use this directly; external callers (imports)
        go through add_imported_encounter() instead -- self._lock is a
        plain, non-reentrant Lock, so this must never acquire it itself."""
        self.history.append(encounter)
        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]

    def add_imported_encounter(self, encounter: "Encounter") -> None:
        """For callers outside this class adding an already-built Encounter
        (log merge / session import) -- takes the lock itself, unlike
        feed()/flush_current() which are already inside one. Without this,
        those importers were reaching into tracker.history.append(...)
        directly: unlocked, so a concurrent feed() on the live reader thread
        could race it, and uncapped, the actual worst case for unbounded
        growth -- a single "Import an old session log" can add hundreds of
        pulls in one loop, not just an unusually long live session."""
        with self._lock:
            self._append_history_unlocked(encounter)

    def feed(self, event: CombatEvent) -> Optional[Encounter]:
        """Feeds one event in. Returns the just-completed Encounter if this
        call caused a rollover (useful for persisting it immediately),
        otherwise None."""
        with self._lock:
            now = time.time()
            completed = None

            if event.is_area_entered and event.line_number is not None:
                self.last_area_entered_line = event.line_number

            # A fresh EnterCombat, far enough from the last one, starts a new
            # pull even if the previous encounter never saw ExitCombat and
            # never went quiet -- see NEW_PULL_MIN_GAP_SECONDS.
            new_pull = False
            if event.is_combat_start:
                if (self._last_enter_combat is not None
                        and (now - self._last_enter_combat) >= NEW_PULL_MIN_GAP_SECONDS
                        and self.current.players):
                    new_pull = True
                self._last_enter_combat = now

            # Roll over when the previous encounter is done -- either a new
            # pull started, or it aged past its trailing window.
            if self.current.players and (new_pull or self.current.past_trailing_window(now)):
                # Slivers (looting, a stray tick, combat dropping and never
                # re-establishing) are discarded rather than persisted --
                # matches the offline replay's flush(), which has always
                # done this. Without this filter here, unifying the live
                # path with EnterCombat-based splitting (above) surfaces
                # MORE boundaries than before, and every one of them used to
                # get written to history.json unfiltered: on a real log this
                # produced 116 "completed" pulls where only 93 were real
                # fights -- confirmed by filtering to >=5s, which lands on
                # exactly 93, matching the offline count precisely.
                if (self.current.duration() >= MIN_ENCOUNTER_SECONDS
                        and self.current.is_real_fight()):
                    completed = self.current
                    self._append_history_unlocked(completed)
                if self._has_meter_data(self.current):
                    self._last_meaningful = self.current
                self.current = Encounter()
                self.current.log_path = self.current_log_path
                self.current.area_entered_line = self.last_area_entered_line

            if self.current.area_entered_line is None:
                self.current.area_entered_line = self.last_area_entered_line

            self.current.apply(event)
            return completed

    def flush_current(self) -> Optional[Encounter]:
        """Closes out the in-flight encounter and returns it, or None if
        there's nothing worth keeping.

        feed() only rolls over when the NEXT event arrives, so without this
        the last pull of a session is never appended to history and never
        persisted -- close the app after your final boss and it's gone.
        Measured on a real log: a 144.7s, 10-player pull silently lost.

        Slivers under MIN_ENCOUNTER_SECONDS are dropped rather than saved:
        quitting during looting or a stray DoT tick shouldn't leave a junk
        row in History."""
        with self._lock:
            if not self.current.players:
                return None
            if self.current.duration() < MIN_ENCOUNTER_SECONDS:
                return None
            if not self.current.is_real_fight():
                return None
            completed = self.current
            self._append_history_unlocked(completed)
            self.current = Encounter()
            self.current.log_path = self.current_log_path
            self.current.area_entered_line = self.last_area_entered_line
            return completed

    @staticmethod
    def _has_meter_data(encounter: "Encounter") -> bool:
        return any(
            p.damage_done > 0 or p.healing_done > 0 for p in encounter.players.values()
        )

    def _display_encounter_unlocked(self) -> Encounter:
        if self._has_meter_data(self.current):
            return self.current
        if self._last_meaningful is not None:
            return self._last_meaningful
        return self.current

    def display_encounter(self) -> Encounter:
        """Which encounter the meter/overlays should show right now: the
        live one if it already has real damage/healing in it, otherwise the
        last one that did -- so a just-finished pull's numbers stay on
        screen through the between-pulls downtime instead of blanking the
        moment `current` rolls over to a fresh, empty Encounter(). Switches
        back to the live encounter the instant it lands its own first real
        hit."""
        with self._lock:
            return self._display_encounter_unlocked()

    def snapshot(self):
        with self._lock:
            enc = self._display_encounter_unlocked()
            return enc.snapshot(), enc.duration()

    def history_snapshot(self):
        """Returns list of (index, duration, rows, real_start_time) for
        completed encounters, most recent first."""
        with self._lock:
            out = []
            for i, enc in enumerate(reversed(self.history)):
                out.append((len(self.history) - i, enc.duration(), enc.snapshot(), enc.real_start_time))
            return out
