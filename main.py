"""
main.py

Entry point. Wires together:
  log_watcher  -> finds/tails the live SWTOR combat log
  log_parser   -> turns each raw line into a CombatEvent
  stats        -> aggregates events into DPS/HPS/damage-taken/deaths
  gui          -> displays the live table

Run with:  python main.py
Optional:  python main.py "C:\\path\\to\\CombatLogs"
"""

import queue
import sys
import threading
from pathlib import Path

import log_watcher
import storage
from log_parser import parse_line
from stats import StatsTracker
from timers import TimerEngine
from boss_definitions import load_definitions
from boss_intelligence import BossEncounterState
from cooldowns import register_defensive_cooldowns
from dots_hots import register_dots_hots, HotTracker
from taunt_tracker import TauntTracker
from gui import MeterWindow

BUNDLED_BOSS_DIR = Path(__file__).parent / "boss_definitions_bundled"
USER_BOSS_DIR = Path(storage.data_dir()) / "boss_definitions"


def background_reader(
    log_dir: str,
    tracker: StatsTracker,
    timer_engine: TimerEngine,
    boss_state: BossEncounterState,
    hot_tracker: HotTracker,
    taunt_tracker: TauntTracker,
    status_q: "queue.Queue[str]",
):
    status_q.put(f"Watching: {log_dir}")
    try:
        for path, line_number, raw_line in log_watcher.watch_folder(log_dir):
            if path != tracker.current_log_path:
                tracker.set_log_path(path)
                backfilled = log_watcher.find_last_area_entered_line(path)
                if backfilled is not None:
                    tracker.last_area_entered_line = backfilled
                    tracker.current.area_entered_line = backfilled
            event = parse_line(raw_line, line_number=line_number)
            if event is not None:
                completed = tracker.feed(event)
                timer_engine.tick()  # prune/detect expiries before boss_state reads them
                boss_state.feed(event, timer_engine=timer_engine)
                timer_engine.feed(
                    event, boss_id=boss_state.active_boss and boss_state.active_boss.id,
                    phase_id=boss_state.active_phase_id,
                    local_player_name=boss_state.local_player_name,
                )
                hot_tracker.feed(event, local_player_name=boss_state.local_player_name)
                taunt_tracker.feed(event, local_player_name=boss_state.local_player_name)
                if completed is not None:
                    storage.append_history_entry(completed)
                    boss_state.reset()
                    hot_tracker.reset()
    except Exception as exc:  # keep the GUI alive even if the reader dies
        status_q.put(f"Reader error: {exc}")


def main():
    if len(sys.argv) > 1:
        log_dir = sys.argv[1]
    else:
        log_dir = log_watcher.find_log_dir()

    tracker = StatsTracker()
    timer_engine = TimerEngine()
    definitions = load_definitions(BUNDLED_BOSS_DIR, USER_BOSS_DIR)
    boss_state = BossEncounterState(definitions)
    # Register each boss definition's own LEGACY (keyword-only) timers as
    # TimerRules, scoped so they only arm during that boss's active phase(s).
    # Timers using the richer trigger schema (combat_start/ability_cast/
    # effect_applied/effect_removed) are evaluated directly in
    # boss_state.feed() instead -- registering them here too would create a
    # TimerRule with an empty keyword, which matches every event.
    from timers import TimerRule
    for boss in definitions.values():
        for t in boss.timers:
            if t.trigger is not None:
                continue
            timer_engine.add_rule(TimerRule(
                keyword=t.keyword, label=t.label, duration_seconds=t.duration_seconds,
                warn_seconds_before=t.warn_seconds_before, voice_alert=t.voice_alert,
                required_boss=boss.id, required_phases=t.phases, category="boss",
            ))
    # Personal defensive cooldowns aren't boss-scoped -- register once,
    # always active (trash fights need them tracked too, not just bosses).
    register_defensive_cooldowns(timer_engine)
    # Same for personal DoT/HoT uptime tracking.
    register_dots_hots(timer_engine)
    # Per-person HoT expiry, for the "who needs a re-Probe" overlay -- see
    # dots_hots.HotTracker. Separate from the category rules above: those
    # answer "is one of my HoTs running", this answers "on whom".
    hot_tracker = HotTracker()
    # Did-my-taunt-land tracking -- see taunt_tracker.py. Not boss-scoped
    # either: a trash-pull taunt swap matters just as much as a boss one.
    taunt_tracker = TauntTracker()
    status_q: "queue.Queue[str]" = queue.Queue()

    if not log_dir:
        status_q.put(
            "Could not auto-find CombatLogs folder. "
            "Pass the path as an argument, e.g.:\n"
            r'python main.py "C:\Users\You\Documents\Star Wars - The Old Republic\CombatLogs"'
        )
    else:
        thread = threading.Thread(
            target=background_reader,
            args=(log_dir, tracker, timer_engine, boss_state, hot_tracker, taunt_tracker, status_q),
            daemon=True,
        )
        thread.start()

    window = MeterWindow(tracker, timer_engine, status_q, boss_state=boss_state,
                         hot_tracker=hot_tracker, taunt_tracker=taunt_tracker)
    window.run()


if __name__ == "__main__":
    main()
