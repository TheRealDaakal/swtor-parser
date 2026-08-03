"""
Covers three P1 fixes in StatsTracker/PlayerStats:
  - the lost-final-pull bug (flush_current didn't exist before tonight)
  - live/offline pull-split divergence (StatsTracker.feed ignored
    EnterCombat entirely; measured 93 vs 75 pulls on the same real log
    before the fix)
  - sliver filtering matching offline's MIN_ENCOUNTER_SECONDS
Plus the raw/effective healing and boss-only DPS additions.
"""
from log_parser import parse_line
from stats import (
    StatsTracker, PlayerStats, Encounter, NEW_PULL_MIN_GAP_SECONDS, HISTORY_LIMIT,
    TRAILING_CAPTURE_SECONDS,
)
from conftest import log_line


def _heal_tick(tracker, sim_clock, t, source="@Healer#1", target="@Ally#2", amount="1000"):
    sim_clock(t)
    ev = parse_line(
        log_line(f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}",
                  source, target=target, ability="Kolto Missile {1}",
                  effect_name="Heal {2}", amount=amount),
        line_number=1,
    )
    return tracker.feed(ev)


def _damage_tick(tracker, sim_clock, t, source="@Dps#1", target="Training Dummy", amount="1000"):
    sim_clock(t)
    ev = parse_line(
        log_line(f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}",
                  source, target=target, ability="Some Attack {1}",
                  effect_name="Damage {2}", amount=amount),
        line_number=1,
    )
    return tracker.feed(ev)


def _exit_combat(tracker, sim_clock, t, source="@Dps#1"):
    sim_clock(t)
    ev = parse_line(
        log_line(f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}",
                  source, effect_type="Event", effect_name="ExitCombat {1}"),
        line_number=1,
    )
    ev.is_combat_end = True  # explicit, matching the EnterCombat tests' own convention
    return tracker.feed(ev)


def _stray_ability_activate(tracker, sim_clock, t, source="@Dps#1"):
    """A non-combat event -- e.g. popping a stim between pulls. No damage,
    no heal, but still names a player (source_is_player), so it's enough to
    make StatsTracker.feed() roll `current` over to a fresh Encounter once
    the trailing window has passed, without itself being a real new pull."""
    sim_clock(t)
    ev = parse_line(
        log_line(f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}",
                  source, effect_name="AbilityActivate {1}"),
        line_number=1,
    )
    return tracker.feed(ev)


class TestFlushCurrent:
    def test_realistic_fight_flushes_and_resets(self, sim_clock):
        tracker = StatsTracker()
        t = 100.0
        for _ in range(10):
            _heal_tick(tracker, sim_clock, t)
            t += 2.0  # 18s of continuous activity, well under the 8s gap

        assert len(tracker.current.players) == 2
        flushed = tracker.flush_current()

        assert flushed is not None
        assert len(flushed.players) == 2
        assert len(tracker.current.players) == 0, "current must reset after flush"
        assert len(tracker.history) == 1, "flushed encounter must land in history"

    def test_sub_threshold_sliver_is_discarded_not_persisted(self, sim_clock):
        tracker = StatsTracker()
        _heal_tick(tracker, sim_clock, 100.0)
        sim_clock(100.7)  # 0.7s total -- well under MIN_ENCOUNTER_SECONDS
        _heal_tick(tracker, sim_clock, 100.7)

        flushed = tracker.flush_current()
        assert flushed is None, "a sliver must not be reported as a real completed pull"
        assert len(tracker.history) == 0, "a sliver must not be persisted"

    def test_flush_on_empty_current_is_a_safe_noop(self, sim_clock):
        tracker = StatsTracker()
        assert tracker.flush_current() is None
        assert tracker.flush_current() is None  # idempotent


class TestPullSplitting:
    def test_gap_based_rollover_still_works(self, sim_clock):
        """The pre-existing behavior (no EnterCombat involved): a long
        enough quiet gap alone ends a pull. Spans 10s of activity (over
        MIN_ENCOUNTER_SECONDS, so it counts as a real pull, not a sliver)."""
        tracker = StatsTracker()
        for i in range(6):
            _heal_tick(tracker, sim_clock, 100.0 + i * 2.0)  # 100..110, 10s span

        sim_clock(110.0 + 20.0)  # well past ENCOUNTER_GAP_SECONDS (8.0)
        completed = _heal_tick(tracker, sim_clock, 110.0 + 20.0, target="@Other#3")
        assert completed is not None
        assert completed.players.keys() == {"Healer", "Ally"}

    def test_fresh_entercombat_splits_a_pull_even_without_a_gap(self, sim_clock):
        """The actual bug: adds never stop swinging, so the inactivity gap
        never fires, but a real new pull still needs to be recognized.
        EnterCombat far enough from the last one must force a rollover on
        its own."""
        tracker = StatsTracker()
        t = 0.0

        def enter_combat(ts):
            ev = parse_line(
                log_line(ts, "@Daakal#1", effect_type="Event", effect_name="EnterCombat {1}"),
                line_number=1,
            )
            ev.is_combat_start = True  # EnterCombat classification, set directly for clarity
            return tracker.feed(ev)

        sim_clock(0.0)
        enter_combat("00:00:00.000")
        for i in range(6):
            _heal_tick(tracker, sim_clock, 1.0 + i * 2.0)  # 1..11, 10s of activity, no gap

        gap = NEW_PULL_MIN_GAP_SECONDS + 5.0
        sim_clock(11.0 + gap)
        completed = enter_combat(f"00:{int((11 + gap) // 60):02d}:{(11 + gap) % 60:06.3f}")
        assert completed is not None, (
            "a fresh EnterCombat far enough from the last one must end the "
            "previous pull even though activity never actually stopped"
        )

    def test_entercombat_too_soon_after_the_last_one_does_not_split(self, sim_clock):
        """The delay guard: combat dropping and re-establishing within the
        same fight (9-16s apart in real data) must NOT be treated as a new
        pull."""
        tracker = StatsTracker()

        def enter_combat(t):
            sim_clock(t)
            ev = parse_line(
                log_line(f"00:00:{t:06.3f}", "@Daakal#1", effect_type="Event",
                         effect_name="EnterCombat {1}"),
                line_number=1,
            )
            ev.is_combat_start = True
            return tracker.feed(ev)

        enter_combat(0.0)
        _heal_tick(tracker, sim_clock, 1.0)
        result = enter_combat(10.0)  # well under NEW_PULL_MIN_GAP_SECONDS (30.0)
        assert result is None, "a same-fight EnterCombat re-entry must not split the pull"


class TestRawEffectiveHealing:
    def test_effective_healing_subtracts_overheal(self, sim_clock):
        tracker = StatsTracker()
        _heal_tick(tracker, sim_clock, 0.0, amount="10926* ~7382")
        p = tracker.current.players["Healer"]
        assert p.healing_done == 10926.0, "raw healing is the full cast power, unreduced"
        assert p.healing_overheal == 7382.0
        assert p.effective_healing_done() == 10926.0 - 7382.0

    def test_effective_hps_uses_duration(self, sim_clock):
        p = PlayerStats(name="X")
        p.healing_done = 1000.0
        p.healing_overheal = 400.0
        assert p.effective_hps(10.0) == 60.0  # (1000-400)/10
        assert p.hps(10.0) == 100.0


class TestDisplayEncounter:
    """The DPS/HPS overlay reads display_encounter() (via snapshot()), not
    .current directly -- reported live: meters were resetting to blank in
    the downtime between pulls, well before the next real fight started,
    because ANY event (not just a real new pull) rolls `current` over to a
    fresh Encounter() once the trailing/gap window has passed. A stray
    non-combat event between pulls (rebuffing, a stim pop) was enough to
    trigger that and blank the bars."""

    def test_last_meaningful_fight_stays_visible_through_a_stray_post_fight_event(self, sim_clock):
        tracker = StatsTracker()
        for i in range(6):
            _damage_tick(tracker, sim_clock, 100.0 + i * 2.0)  # 100..110: a real fight

        # Well past the inactivity gap -- rolls `current` over to a fresh,
        # empty Encounter, but this event itself carries no damage/healing.
        _stray_ability_activate(tracker, sim_clock, 130.0)

        assert len(tracker.current.players) == 1, "the stray event itself still registers on current"
        assert not tracker._has_meter_data(tracker.current), "but it carries no damage/healing"

        enc = tracker.display_encounter()
        assert enc is not tracker.current, "must show the last REAL fight, not the empty new one"
        assert enc.players["Dps"].damage_done > 0

        rows, duration = tracker.snapshot()
        assert rows, "snapshot() (what the overlay/live tab actually reads) must not go blank either"

    def test_display_encounter_switches_over_once_the_new_fight_lands_real_damage(self, sim_clock):
        tracker = StatsTracker()
        for i in range(6):
            _damage_tick(tracker, sim_clock, 100.0 + i * 2.0)

        _stray_ability_activate(tracker, sim_clock, 130.0)
        assert tracker.display_encounter() is not tracker.current, "still showing the old fight"

        _damage_tick(tracker, sim_clock, 131.0, amount="500")  # next pull's first real hit

        enc = tracker.display_encounter()
        assert enc is tracker.current, "must switch to the live encounter once it has its own real data"
        assert enc.players["Dps"].damage_done == 500.0


class TestBossDps:
    def test_boss_dps_only_counts_the_named_targets(self):
        p = PlayerStats(name="DPS")
        p.damage_by_target = {"Boss": 8000.0, "Add1": 2000.0, "Add2": 1000.0}
        assert p.damage_to(["Boss"]) == 8000.0
        assert p.boss_dps(["Boss"], duration=10.0) == 800.0
        # total damage_done (not summed here) would include the adds too --
        # boss_dps must never silently include them.
        assert p.boss_dps(["Boss"], duration=10.0) < sum(p.damage_by_target.values()) / 10.0


class TestTrailingCapture:
    """StarParse-style behavior: damage/heal events arriving up to
    TRAILING_CAPTURE_SECONDS after ExitCombat still count toward the fight
    that just ended, instead of being dropped or bleeding into the next
    pull -- SWTOR can log a DoT/HoT tick after the official combat-exit
    event. Previously unexercised by any test."""

    def test_damage_within_the_grace_window_still_counts_toward_the_closed_pull(self, sim_clock):
        tracker = StatsTracker()
        for i in range(6):
            _damage_tick(tracker, sim_clock, 100.0 + i * 2.0)  # 100..110

        completed = _exit_combat(tracker, sim_clock, 110.0)
        assert completed is None, "ExitCombat alone must not roll the pull over immediately"

        # A trailing DoT tick lands well inside TRAILING_CAPTURE_SECONDS (4.0).
        assert TRAILING_CAPTURE_SECONDS == 4.0, "test assumes the documented default"
        trailing = _damage_tick(tracker, sim_clock, 112.0, amount="777")
        assert trailing is None, "still within the grace window -- must not roll over yet"

        # Push well past the grace window and feed a stray event -- THAT
        # finally triggers the rollover.
        stray = _stray_ability_activate(tracker, sim_clock, 130.0)
        assert stray is not None
        total_expected = 6 * 1000.0 + 777.0  # six default-amount ticks (see _damage_tick) + the trailing one
        assert stray.players["Dps"].damage_done == total_expected, (
            "the trailing tick must be attributed to the FINISHED pull, not dropped "
            "and not bled into whatever comes next"
        )

    def test_damage_past_the_grace_window_starts_a_new_pull_instead(self, sim_clock):
        tracker = StatsTracker()
        _damage_tick(tracker, sim_clock, 100.0)
        completed = _exit_combat(tracker, sim_clock, 101.0)
        assert completed is None

        # Arrives after the grace window has elapsed -- this event itself
        # both closes the old pull AND becomes the first hit of a new one.
        _damage_tick(tracker, sim_clock, 101.0 + TRAILING_CAPTURE_SECONDS + 5.0, amount="999")

        assert len(tracker.history) == 0, "the old pull was a sub-5s sliver, correctly discarded"
        assert tracker.current.players["Dps"].damage_done == 999.0, (
            "the late hit must start the NEW current encounter, not get folded into the old one"
        )


class TestHistoryMemoryCap:
    """StatsTracker.history used to grow without bound in memory -- only the
    on-disk file was ever trimmed to HISTORY_LIMIT. A long session, or one
    big 'Import an old session log' click (which can add hundreds of pulls
    in a single loop -- see web_server.py's /api/import/session), kept every
    Encounter (each with its own per-player event lists) in RAM forever."""

    def test_rollover_trims_in_memory_history_to_the_limit(self, sim_clock):
        tracker = StatsTracker()
        t = 0.0
        for _ in range(HISTORY_LIMIT + 5):
            _damage_tick(tracker, sim_clock, t)
            t += NEW_PULL_MIN_GAP_SECONDS + 1.0
            _damage_tick(tracker, sim_clock, t)  # forces the previous one to roll over
        assert len(tracker.history) <= HISTORY_LIMIT

    def test_flush_current_also_respects_the_cap(self, sim_clock):
        tracker = StatsTracker()
        tracker.history = [Encounter() for _ in range(HISTORY_LIMIT)]
        for i in range(10):
            _damage_tick(tracker, sim_clock, 100.0 + i * 2.0)
        tracker.flush_current()
        assert len(tracker.history) == HISTORY_LIMIT

    def test_imported_encounters_are_capped_and_dont_bypass_the_lock(self, sim_clock):
        tracker = StatsTracker()
        tracker.history = [Encounter() for _ in range(HISTORY_LIMIT)]
        newest = Encounter(label="Imported")
        tracker.add_imported_encounter(newest)
        assert len(tracker.history) == HISTORY_LIMIT
        assert tracker.history[-1] is newest, "trimming must drop the OLDEST entries, not the new one"
