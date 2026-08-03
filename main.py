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
from typing import Optional
from pathlib import Path

import log_watcher
import storage
import update_check
from version import __version__
from log_parser import parse_line
from stats import StatsTracker
from timers import TimerEngine
from boss_definitions import load_definitions
from boss_intelligence import BossEncounterState
from cooldowns import register_defensive_cooldowns
from dots_hots import register_dots_hots, HotTracker
from alacrity import register_alacrity_buffs
from taunt_tracker import TauntTracker
from gui import OverlayManager

BUNDLED_BOSS_DIR = Path(__file__).parent / "boss_definitions_bundled"
USER_BOSS_DIR = Path(storage.data_dir()) / "boss_definitions"


class StatusHolder:
    """Replaces the old status_q -- that only ever had one consumer (the Tk
    toolbar's status label), and the web UI just wants "whatever the latest
    status text is" on each poll, not a queue to drain."""

    def __init__(self):
        self.text = "Waiting for combat log..."


class UpdateHolder:
    """Holds the result of the one-shot startup update check (see
    update_check.py) -- None until that background thread finishes, then
    either stays None (nothing newer / check failed) or holds
    {"version", "url"} for the web UI's update banner to read via
    /api/update. Never re-checked after startup."""

    def __init__(self):
        self.result = None


class CharacterSettingsHolder:
    """The one per-character setting that isn't overlay-layout-shaped:
    Alacrity %, used to scale DoT/HoT durations (see timers.apply_alacrity
    -- SWTOR's combat log never reports a character's actual Alacrity
    Rating/% directly, so this has to be typed in manually). Still
    persisted alongside the overlay layout blob (storage.py's
    load_overlay_layout/save_overlay_layout already key everything by
    character; no reason for a second per-character file).

    Read from the background log-reader thread (background_reader, every
    event) and written from web_server.py's HTTP handler thread (the
    Overlays tab's Alacrity % field) -- a plain float attribute read/write
    is atomic enough under the GIL for this (worst case, one event uses
    the old value a moment longer), so this doesn't need its own lock the
    way StatsTracker/TimerEngine do for their multi-step mutations."""

    def __init__(self):
        self.alacrity_pct: float = 0.0
        self._character: Optional[str] = None

    def sync_for_character(self, character: Optional[str]) -> None:
        """Called every event from background_reader -- cheap (just a
        comparison) unless the character actually changed, in which case
        it reloads from disk once. Without this, an alt-swap mid-session
        would keep using the PREVIOUS character's alacrity setting."""
        if character == self._character:
            return
        self._character = character
        if character:
            layout = storage.load_overlay_layout(character)
            try:
                self.alacrity_pct = float(layout.get("alacrity_pct", 0.0) or 0.0)
            except (TypeError, ValueError):
                self.alacrity_pct = 0.0
        else:
            self.alacrity_pct = 0.0

    def set_alacrity_pct(self, pct: float) -> None:
        """From the web UI's Save button -- updates the in-memory value
        immediately (the very next dot/hot to fire uses it, no restart
        needed) and persists it for next launch."""
        self.alacrity_pct = pct
        if self._character:
            layout = storage.load_overlay_layout(self._character)
            layout["alacrity_pct"] = pct
            storage.save_overlay_layout(layout, character=self._character)

    @property
    def character(self) -> Optional[str]:
        return self._character


def _check_for_update_once(holder: "UpdateHolder"):
    holder.result = update_check.check_for_update(__version__)


class HistoryWriter:
    """Persists completed encounters on a dedicated background thread, so
    the log-reader loop never blocks on disk I/O.

    storage.append_history_entry() does a full read-modify-write of
    history.json on every call -- on a real 200-pull, 15.8MB history file
    that's ~330ms. Calling it directly from background_reader() means every
    pull completion stalls live event processing for that long, right when
    the next pull is often already starting. This queues the write instead:
    submit() only appends to an in-memory queue (effectively instant), and
    a single worker thread drains it and calls the real (still synchronous)
    append_history_entry() one at a time -- one writer, so concurrent
    submissions can't race each other into a corrupted file.

    Deliberately NOT used for storage.append_history_entry() callers outside
    the live reader (e.g. the Import Logs HTTP endpoints) -- those already
    return their HTTP response only once the write actually lands, and
    making that async would let the response claim success before the data
    is durably on disk. This class exists for the one caller where blocking
    is a real, measured problem: the live reader thread."""

    def __init__(self, status: "StatusHolder"):
        self._queue: "queue.Queue" = queue.Queue()
        self._status = status
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, encounter) -> None:
        self._queue.put(encounter)

    def _run(self):
        while True:
            encounter = self._queue.get()
            try:
                storage.append_history_entry(encounter)
            except OSError as exc:
                # Visible, not silent -- same convention background_reader
                # uses for its own errors. A dropped write shouldn't crash
                # the app, but it also shouldn't vanish with no trace.
                self._status.text = f"Failed to save a completed pull: {exc}"


def background_reader(
    log_dir: str,
    tracker: StatsTracker,
    timer_engine: TimerEngine,
    boss_state: BossEncounterState,
    hot_tracker: HotTracker,
    taunt_tracker: TauntTracker,
    status: StatusHolder,
    history_writer: "HistoryWriter",
    character_settings: "CharacterSettingsHolder",
):
    status.text = f"Watching: {log_dir}"
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
            event = parse_line(raw_line, line_number=line_number)
            if event is not None:
                character_settings.sync_for_character(boss_state.local_player_name)
                completed = tracker.feed(event)
                timer_engine.tick()  # prune/detect expiries before boss_state reads them
                boss_state.feed(event, timer_engine=timer_engine)
                timer_engine.feed(
                    event, boss_id=boss_state.active_boss and boss_state.active_boss.id,
                    phase_id=boss_state.active_phase_id,
                    local_player_name=boss_state.local_player_name,
                    alacrity_pct=character_settings.alacrity_pct,
                )
                hot_tracker.feed(event, local_player_name=boss_state.local_player_name,
                                  alacrity_pct=character_settings.alacrity_pct)
                taunt_tracker.feed(event, local_player_name=boss_state.local_player_name)
                if completed is not None:
                    history_writer.submit(completed)
                    boss_state.reset()
                    hot_tracker.reset()
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
    status = StatusHolder()
    history_writer = HistoryWriter(status)
    update_holder = UpdateHolder()
    character_settings = CharacterSettingsHolder()
    threading.Thread(target=_check_for_update_once, args=(update_holder,), daemon=True).start()

    # Custom (Timers-tab) rules and completed pulls both need to survive a
    # restart. This used to happen in gui.py's MeterWindow.__init__; now
    # that the whole tabbed UI is gone from Tkinter, it happens here instead.
    for rule in storage.load_timer_rules():
        timer_engine.add_rule(rule)
    loaded_history = storage.load_history()
    if loaded_history:
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
            args=(log_dir, tracker, timer_engine, boss_state, hot_tracker, taunt_tracker, status,
                  history_writer, character_settings),
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
                                  hot_tracker=hot_tracker, taunt_tracker=taunt_tracker)
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
        window_ref["window"].destroy()

    web_port = 8766
    server = web_server.make_server(tracker, timer_engine, boss_state, taunt_tracker,
                                     overlay_manager, status, port=web_port,
                                     update_holder=update_holder, request_shutdown=request_shutdown,
                                     character_settings=character_settings)
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
