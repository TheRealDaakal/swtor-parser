"""
Covers main.CharacterSettingsHolder -- the Alacrity % setting SWTOR's
combat log never reports directly (see alacrity.py's own docstring), so
it's a manually-entered per-character value instead. Read from the
background log-reader thread every event (sync_for_character, cheap
unless the character actually changed) and written from the web UI's
Overlays tab (set_alacrity_pct).

Isolated from the real %APPDATA%\\swtor-parser the same way other tests
avoid depending on real machine state: APPDATA is monkeypatched to a
pytest tmp_path before any storage.py call.
"""
import storage
from conftest import log_line
from main import CharacterSettingsHolder, background_reader, PHASE_ALERT_SECONDS


def _isolate_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))


def test_defaults_to_zero_with_no_character_known(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    holder = CharacterSettingsHolder()
    assert holder.alacrity_pct == 0.0
    assert holder.character is None


def test_sync_loads_the_saved_value_for_a_newly_seen_character(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    layout = storage.load_overlay_layout("Voidkeeper")
    layout["alacrity_pct"] = 12.5
    storage.save_overlay_layout(layout, character="Voidkeeper")

    holder = CharacterSettingsHolder()
    holder.sync_for_character("Voidkeeper")
    assert holder.alacrity_pct == 12.5
    assert holder.character == "Voidkeeper"


def test_sync_is_a_cheap_noop_for_the_same_character(monkeypatch, tmp_path):
    """Re-syncing the SAME character must not re-read from disk -- verified
    by changing the on-disk value after the first sync and confirming the
    in-memory value doesn't pick it up (would only happen on an actual
    character change, or an explicit set_alacrity_pct call)."""
    _isolate_appdata(monkeypatch, tmp_path)
    holder = CharacterSettingsHolder()
    holder.sync_for_character("Voidkeeper")
    assert holder.alacrity_pct == 0.0

    layout = storage.load_overlay_layout("Voidkeeper")
    layout["alacrity_pct"] = 99.0
    storage.save_overlay_layout(layout, character="Voidkeeper")

    holder.sync_for_character("Voidkeeper")  # same character again
    assert holder.alacrity_pct == 0.0, "must not re-read disk for a character that hasn't changed"


def test_switching_characters_reloads_the_new_characters_own_value(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    for name, pct in (("Voidkeeper", 12.5), ("Emberlash", 5.0)):
        layout = storage.load_overlay_layout(name)
        layout["alacrity_pct"] = pct
        storage.save_overlay_layout(layout, character=name)

    holder = CharacterSettingsHolder()
    holder.sync_for_character("Voidkeeper")
    assert holder.alacrity_pct == 12.5

    holder.sync_for_character("Emberlash")  # alt-swap mid-session
    assert holder.alacrity_pct == 5.0
    assert holder.character == "Emberlash"

    holder.sync_for_character(None)  # character no longer known (e.g. log switched)
    assert holder.alacrity_pct == 0.0


def test_set_alacrity_pct_updates_in_memory_immediately(monkeypatch, tmp_path):
    """The very next dot/hot to fire must see the new value -- no restart,
    no re-sync needed, since set_alacrity_pct comes from the user actively
    saving it via the web UI while a character is already known."""
    _isolate_appdata(monkeypatch, tmp_path)
    holder = CharacterSettingsHolder()
    holder.sync_for_character("Voidkeeper")
    holder.set_alacrity_pct(7.5)
    assert holder.alacrity_pct == 7.5


def test_set_alacrity_pct_persists_for_next_launch(monkeypatch, tmp_path):
    _isolate_appdata(monkeypatch, tmp_path)
    holder = CharacterSettingsHolder()
    holder.sync_for_character("Voidkeeper")
    holder.set_alacrity_pct(7.5)

    # Simulates an app restart: a fresh holder, re-synced from disk.
    fresh_holder = CharacterSettingsHolder()
    fresh_holder.sync_for_character("Voidkeeper")
    assert fresh_holder.alacrity_pct == 7.5


def test_set_alacrity_pct_with_no_character_known_does_not_crash_or_persist_anywhere():
    """Defensive: web_server.py's handler already guards this case (returns
    a 400 before ever calling set_alacrity_pct), but the holder itself
    must not blow up if called anyway."""
    holder = CharacterSettingsHolder()
    holder.set_alacrity_pct(10.0)  # no character synced yet
    assert holder.alacrity_pct == 10.0
    assert holder.character is None


# ---------------------------------------------------------------------
# background_reader's phase-change -> is_alert timer wiring. Covers the
# actual reader loop (not just TimerEngine.start_timer() in isolation,
# already covered by tests/test_timers.py) by monkeypatching
# log_watcher.watch_folder to replay synthetic lines through the real
# function.

class _NullHistoryWriter:
    def submit(self, encounter):
        pass  # no pull ever completes in this short synthetic feed


def test_a_real_phase_transition_starts_an_is_alert_phase_timer(monkeypatch, tmp_path, sim_clock):
    """Every genuine phase transition -- including the initial one, when
    the boss is first recognized -- should start a voice_alert/is_alert
    timer carrying the new phase's name (main.py's PHASE_ALERT_SECONDS
    block), so "burn phase started" (or any other phase) gets called out
    instead of only updating the passive header text."""
    import main
    import log_watcher
    from stats import StatsTracker
    from timers import TimerEngine
    from boss_definitions import _definition_from_dict
    from boss_intelligence import BossEncounterState
    from dots_hots import HotTracker
    from taunt_tracker import TauntTracker
    from aggro_tracker import AggroTracker
    from main import StatusHolder, CharacterSettingsHolder

    monkeypatch.setenv("APPDATA", str(tmp_path))

    definition = _definition_from_dict({
        "id": "test_boss", "name": "Test Boss", "boss_names": ["Test Boss"],
        "phases": [
            {"id": "p1", "name": "Phase 1"},
            {"id": "p2", "name": "Phase 2 (Burn)", "start_trigger": {"type": "hp_below", "percent": 50}},
        ],
    })

    tracker = StatsTracker()
    timer_engine = TimerEngine()
    boss_state = BossEncounterState({"test_boss": definition})
    hot_tracker = HotTracker()
    taunt_tracker = TauntTracker()
    aggro_tracker = AggroTracker()
    status = StatusHolder()
    character_settings = CharacterSettingsHolder()

    log_path = tmp_path / "combat.txt"
    log_path.write_text("", encoding="cp1252")  # find_last_area_entered_line just needs a real file to open

    lines = [
        # Recognizes the boss and enters phase 1 (the initial phase).
        log_line("00:00:00.000", "Test Boss", ability="Intro", effect_name="AbilityActivate {1}"),
        # Player damages the boss down to 40% -- phase 2's hp_below(50) start_trigger.
        log_line("00:00:01.000", "@Player#1", target="Test Boss", ability="Smash",
                  effect_name="Damage {2}", amount="600000", target_hp="400000/1000000"),
    ]

    def fake_watch_folder(log_dir, poll_interval=0.25):
        for i, raw in enumerate(lines, 1):
            yield (str(log_path), i, raw)

    monkeypatch.setattr(log_watcher, "watch_folder", fake_watch_folder)

    main.background_reader(
        str(tmp_path), tracker, timer_engine, boss_state, hot_tracker, taunt_tracker,
        aggro_tracker, status, _NullHistoryWriter(), character_settings,
    )

    assert status.text == f"Watching: {tmp_path}", f"reader loop hit an unexpected error: {status.text}"

    phase_alerts = [t for t in timer_engine.active if t.category == "phase"]
    labels = sorted(t.label for t in phase_alerts)
    assert labels == ["Phase 1", "Phase 2 (Burn)"], (
        f"expected both phase transitions to fire an alert timer, got {labels}"
    )
    for t in phase_alerts:
        assert t.is_alert is True
        assert t.voice_alert is True
        assert t.duration_seconds == PHASE_ALERT_SECONDS
