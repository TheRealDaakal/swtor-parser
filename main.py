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

import os
import sys
import threading
import time
from pathlib import Path

import log_watcher
import storage
from version import __version__
from log_parser import parse_line
from log_merger import LogClock
from stats import StatsTracker
from timers import TimerEngine
from boss_definitions import load_definitions
from boss_intelligence import BossEncounterState
from cooldowns import register_defensive_cooldowns
from dots_hots import register_dots_hots, HotTracker
from alacrity import register_alacrity_buffs
from taunt_tracker import TauntTracker
from aggro_tracker import AggroTracker
from gui import OverlayManager
from app_runtime import (
    CharacterSettingsHolder,
    HistoryWriter,
    StatusHolder,
    UpdateHolder,
    archive_old_logs_once,
    check_for_update_once,
)

BUNDLED_BOSS_DIR = Path(__file__).parent / "boss_definitions_bundled"
USER_BOSS_DIR = Path(storage.data_dir()) / "boss_definitions"

# How long a phase-change callout stays up in the alert banner (and how long
# after the voice line before it can fire again for the SAME transition --
# each real transition only fires once anyway, this just bounds how long it
# lingers on screen). Deliberately generic -- announces every phase, not
# just ones named "Burn"/"Enrage": which phase matters is a per-fight,
# per-strat judgment call, not something to hardcode by name.
PHASE_ALERT_SECONDS = 5.0


def background_reader(
    log_dir: str,
    tracker: StatsTracker,
    timer_engine: TimerEngine,
    boss_state: BossEncounterState,
    hot_tracker: HotTracker,
    taunt_tracker: TauntTracker,
    aggro_tracker: AggroTracker,
    status: StatusHolder,
    history_writer: "HistoryWriter",
    character_settings: "CharacterSettingsHolder",
):
    status.text = f"Watching: {log_dir}"
    log_clock = LogClock()
    last_wall_time = None   # our corrected estimate of the previous event's real time
    last_log_time = None    # that previous event's LogClock reading, same file only
    try:
        for path, line_number, raw_line in log_watcher.watch_folder(log_dir):
            if path != tracker.current_log_path:
                tracker.set_log_path(path)
                backfilled = log_watcher.find_last_area_entered_line(path)
                if backfilled is not None:
                    tracker.last_area_entered_line = backfilled
                    tracker.current.area_entered_line = backfilled
                if boss_state.local_player_name is None:
                    seeded_name = log_watcher.find_local_player_name(path)
                    if seeded_name is not None:
                        boss_state.local_player_name = seeded_name
                # A new file's timestamps aren't comparable to the old
                # file's (different session, possibly a different day), so
                # don't try to bridge them -- the first event of a new file
                # just falls back to a plain wall-clock reading below, same
                # as it always has.
                log_clock = LogClock()
                last_log_time = None
            event = parse_line(raw_line, line_number=line_number)
            if event is not None:
                character_settings.sync_for_character(boss_state.local_player_name)

                # SWTOR can buffer its combat-log writes and flush a burst
                # of lines late -- confirmed against a real raid log where a
                # post-wipe stretch of buff/movement events all landed in
                # one delayed write. A raw time.time() gap across that stall
                # reads as several seconds (sometimes far more) of quiet and
                # closes the current pull early, even though the lines'
                # own in-log timestamps are seconds apart. Capping the
                # measured gap at what the log itself shows (never widening
                # it -- only narrowing) keeps pull-boundary detection honest
                # without giving up wall-clock's other job of eventually
                # closing a pull when nothing is being written at all.
                wall_now = time.time()
                log_now = log_clock(event.timestamp or "")
                if last_wall_time is not None and last_log_time is not None:
                    wall_gap = wall_now - last_wall_time
                    log_gap = log_now - last_log_time
                    at_time = last_wall_time + min(wall_gap, log_gap)
                else:
                    at_time = wall_now
                last_wall_time = at_time
                last_log_time = log_now

                completed = tracker.feed(event, at_time=at_time, real_time=wall_now)
                if completed is not None:
                    # Reset BEFORE feeding, not after: the event that rolls a
                    # pull over is the FIRST event of the NEXT one (usually
                    # its EnterCombat). Resetting afterwards fed it into the
                    # previous pull's state and then immediately wiped what it
                    # had just recorded -- which threw away the new pull's
                    # combat-start marker that combat_start-triggered timers
                    # are counted from (see
                    # BossEncounterState._fire_combat_start_timers). The
                    # offline replay path (analysis/corpus.py's replay_pulls)
                    # has always reset in this order.
                    history_writer.submit(completed)
                    boss_state.reset()
                    hot_tracker.reset()
                    aggro_tracker.reset()
                timer_engine.tick()  # prune/detect expiries before boss_state reads them
                had_phase = boss_state.active_phase_id is not None
                change = boss_state.feed(event, timer_engine=timer_engine)
                if change is not None and had_phase:
                    # `had_phase` above: the FIRST phase is assigned the moment
                    # the boss is recognized, which is just "the fight started",
                    # not a transition worth calling out -- and 133 of 163
                    # bosses name it something generic ("Main", "Phase 1"), so
                    # it was announcing "Main" on every single pull.
                    # Reuses the same is_alert/voice_alert pipeline authored
                    # boss timers already get (timers.py's start_timer ->
                    # audio.speak) -- a phase change just starts one of
                    # those automatically instead of requiring every boss
                    # definition to hand-author a phase_entered timer for it.
                    timer_engine.start_timer(
                        change.phase_name, PHASE_ALERT_SECONDS,
                        voice_alert=True, category="phase", is_alert=True,
                        # so muting an encounter silences its phase
                        # callouts too, not just its mechanic timers
                        boss_id=change.boss_id,
                    )
                timer_engine.feed(
                    event, boss_id=boss_state.active_boss and boss_state.active_boss.id,
                    phase_id=boss_state.active_phase_id,
                    local_player_name=boss_state.local_player_name,
                    alacrity_pct=character_settings.alacrity_pct,
                )
                hot_tracker.feed(event, local_player_name=boss_state.local_player_name,
                                  alacrity_pct=character_settings.alacrity_pct)
                taunt_tracker.feed(event, local_player_name=boss_state.local_player_name)
    except Exception as exc:  # keep the app alive even if the reader dies
        status.text = f"Reader error: {exc}"


class Api:
    """pywebview js_api bridge -- a browser <input type=file> can only hand
    back file CONTENTS, never a real filesystem path, so the file pickers
    for Import Logs and Parsely's "Upload a Log File..." go through
    pywebview's native dialog instead, called from JS as
    `await pywebview.api.pick_files()` / `pick_file()`."""

    def __init__(self, window_ref: dict):
        self._window_ref = window_ref  # {"window": ...}, filled in after create_window

    def pick_files(self):
        import webview
        result = self._window_ref["window"].create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("Log files (*.txt)", "All files (*.*)"),
        )
        return list(result) if result else []

    def pick_file(self):
        import webview
        result = self._window_ref["window"].create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Log files (*.txt)", "All files (*.*)"),
        )
        return result[0] if result else None

    def pick_audio_file(self):
        # Custom Timers' optional "play this sound instead of speaking"
        # attachment -- winsound.PlaySound (see audio.py) only plays .wav,
        # not mp3/etc, so the filter matches what will actually work.
        import webview
        result = self._window_ref["window"].create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Sound files (*.wav)", "All files (*.*)"),
        )
        return result[0] if result else None


def main():
    if len(sys.argv) > 1:
        log_dir = sys.argv[1]
    else:
        log_dir = log_watcher.find_log_dir()

    import audio
    audio.apply_settings(storage.load_audio_settings())

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
                is_alert=t.is_alert,
            ))
    # Personal defensive cooldowns aren't boss-scoped -- register once,
    # always active (trash fights need them tracked too, not just bosses).
    register_defensive_cooldowns(timer_engine)
    # Same for personal DoT/HoT uptime tracking.
    register_dots_hots(timer_engine)
    # Same for personal alacrity-burst-buff uptime -- see alacrity.py.
    register_alacrity_buffs(timer_engine)
    # Per-person HoT expiry, for the "who needs a re-Probe" overlay -- see
    # dots_hots.HotTracker. Separate from the category rules above: those
    # answer "is one of my HoTs running", this answers "on whom".
    hot_tracker = HotTracker()
    # Did-my-taunt-land tracking -- see taunt_tracker.py. Not boss-scoped
    # either: a trash-pull taunt swap matters just as much as a boss one.
    taunt_tracker = TauntTracker()
    # "You're about to pull" warning -- see aggro_tracker.py. Checked from
    # OverlayManager's periodic refresh tick (gui.py), not per-event here,
    # so the live log-tailing hot path never pays for it.
    aggro_tracker = AggroTracker()
    status = StatusHolder()
    history_writer = HistoryWriter(status)
    update_holder = UpdateHolder()
    character_settings = CharacterSettingsHolder()
    threading.Thread(target=check_for_update_once, args=(update_holder,), daemon=True).start()
    threading.Thread(target=archive_old_logs_once, args=(log_dir,), daemon=True).start()

    # Custom (Timers-tab) rules and completed pulls both need to survive a
    # restart. This used to happen in gui.py's MeterWindow.__init__; now
    # that the whole tabbed UI is gone from Tkinter, it happens here instead.
    for rule in storage.load_timer_rules():
        timer_engine.add_rule(rule)
    loaded_history = storage.load_history()
    if loaded_history:
        # Rows written by an older parser are rebuilt from their own logs
        # before anything gets to display them -- otherwise the History tab
        # shows one set of numbers and Deep Dive, which re-reads the raw
        # log, shows another. See history_migration.py. One-time: rebuilt
        # rows are stamped with the current version, so the next launch
        # skips this entirely.
        import history_migration
        if history_migration.needs_migration(loaded_history):
            status.text = "Updating saved history to the current parser…"
            # Taken before anything is rewritten: the migration legitimately
            # drops rows that were never fights, and a wrong call there
            # would otherwise be unrecoverable.
            backup = storage.backup_history("pre-v1-migration.bak")
            try:
                loaded_history, report = history_migration.migrate(loaded_history)
            except Exception as exc:
                # A failed migration must never stop the app coming up --
                # worst case the user sees the old numbers for another
                # launch, which is where they already were.
                print(f"history migration failed, leaving history as-is: {exc}")
            else:
                storage.save_history(loaded_history)
                print(f"history migration: {report}"
                      + (f" (backup: {backup})" if backup else ""))
        from stats import HISTORY_LIMIT
        tracker.history = (loaded_history + tracker.history)[-HISTORY_LIMIT:]

    if not log_dir:
        status.text = (
            "Could not auto-find CombatLogs folder. "
            "Pass the path as an argument, e.g.: "
            r'python main.py "C:\Users\You\Documents\Star Wars - The Old Republic\CombatLogs"'
        )
    else:
        thread = threading.Thread(
            target=background_reader,
            args=(log_dir, tracker, timer_engine, boss_state, hot_tracker, taunt_tracker,
                  aggro_tracker, status, history_writer, character_settings),
            daemon=True,
        )
        thread.start()

    # The whole UI (Live/History/Timers/Overlays/Import Logs/Parsely) is a
    # pywebview page now (see web_server.py); only the floating bar overlays
    # stay Tkinter (see gui.py's module docstring for why). pywebview wants
    # the main thread for its own event loop, so Tk runs on a background
    # thread instead -- confirmed safe on Windows (unlike macOS, Tk has no
    # main-thread requirement here). Tcl is thread-affine though: the Tk
    # root has to be BOTH created and mainloop()'d on that same thread, or
    # Tcl raises "Calling Tcl from different apartment" -- so
    # OverlayManager() itself, not just .run(), has to happen inside the
    # thread target.
    import web_server
    import webview

    overlay_manager_ref = {}
    overlay_manager_ready = threading.Event()

    def _run_tk():
        manager = OverlayManager(tracker, timer_engine, boss_state=boss_state,
                                  hot_tracker=hot_tracker, taunt_tracker=taunt_tracker,
                                  aggro_tracker=aggro_tracker)
        overlay_manager_ref["manager"] = manager
        overlay_manager_ready.set()
        manager.run()

    threading.Thread(target=_run_tk, daemon=True).start()
    overlay_manager_ready.wait()  # web_server needs the manager before it can start
    overlay_manager = overlay_manager_ref["manager"]

    # Created before make_server() so request_shutdown can close over it --
    # the callback only reads window_ref["window"] when actually CALLED (by
    # the self-updater, well after the window exists below), not at
    # definition time, so the not-yet-populated dict here is fine.
    window_ref = {}

    def request_shutdown():
        # Used by the self-updater (web_server.py's /api/update/apply) right
        # after it launches the relaunch helper, which waits up to 60s for
        # THIS process's pid to exit before swapping the install directory
        # in place (see updater.py). window.destroy() alone isn't enough:
        # it closes the visible window, but doesn't reliably unblock
        # webview.start() (destroy() is being called from a background
        # thread, off the pywebview main loop) -- reported live as "the
        # viewer closed, but it didn't restart" and the update banner still
        # showing the old version after reopening manually. If this process
        # is still alive when the helper's wait times out, the swap fails
        # (its own DLLs are still open) and it silently gives up -- no
        # relaunch, no error shown, since the old window is already gone.
        # Flush explicitly first (mirrors the normal-close path below,
        # which os._exit(0) would otherwise skip) then hard-exit so the
        # helper always sees this pid gone well within its wait window.
        try:
            completed = tracker.flush_current()
            if completed is not None:
                storage.append_history_entry(completed)
        except Exception:
            pass
        window_ref["window"].destroy()
        os._exit(0)

    web_port = 8766
    server = web_server.make_server(tracker, timer_engine, boss_state, taunt_tracker,
                                     overlay_manager, status, port=web_port,
                                     update_holder=update_holder, request_shutdown=request_shutdown,
                                     character_settings=character_settings,
                                     bundled_boss_dir=BUNDLED_BOSS_DIR, user_boss_dir=USER_BOSS_DIR)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    window_ref["window"] = webview.create_window(
        "DPS — Dynamic Parse System", url=f"http://127.0.0.1:{web_port}/",
        width=960, height=720, js_api=Api(window_ref),
    )
    webview.start()  # blocks until the window closes (or self-update calls .destroy())

    # Without this, the pull in progress when the app is closed is never
    # rolled over (StatsTracker.feed only does that when the NEXT event
    # arrives) and so never reaches history.json -- the last fight of every
    # session was silently lost. Confirmed on a real log: a 144.7s,
    # 10-player pull that vanished this exact way.
    completed = tracker.flush_current()
    if completed is not None:
        storage.append_history_entry(completed)


if __name__ == "__main__":
    main()
