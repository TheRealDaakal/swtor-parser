"""Covers web_server_helpers.py's pure functions -- these back /api/live,
/api/history, and the Deep Dive tab, and were entirely untested despite
this module existing specifically "so analytics snapshots, history
serialization, and corpus state are testable without constructing a
server" (its own docstring). Two real bugs were already found and fixed
in this file's sibling (web_server_routes.py) this way -- missing
imports that only a real call site would ever trip -- which is exactly
what a pure unit test catches before it ships.
"""
import time

import pytest

from stats import Encounter, PlayerStats, StatsTracker
from boss_intelligence import BossEncounterState
from boss_definitions import _definition_from_dict
from taunt_tracker import TauntTracker, TauntResult
from timers import TimerEngine
from web_server_helpers import (
    CorpusState, _safe_encounter_id, _timer_rows, build_ability_breakdown,
    build_history_detail, build_history_list, build_live_snapshot,
)


class _Status:
    text = "watching test log"


# --------------------------------------------------------------- _safe_encounter_id

class TestSafeEncounterId:
    def test_a_plain_id_passes_through(self):
        assert _safe_encounter_id("styrak_k") == "styrak_k"

    def test_strips_surrounding_whitespace(self):
        assert _safe_encounter_id("  styrak_k  ") == "styrak_k"

    @pytest.mark.parametrize("bad", [
        "", "   ", None, ".", "..", "../secret", "..\\secret",
        "a/b", "a\\b", "/etc/passwd",
    ])
    def test_rejects_anything_that_could_escape_the_directory(self, bad):
        # Operates on an already-decoded string -- GET callers unquote()
        # the URL segment first, POST callers get a raw JSON string value
        # (where "%2f" is 3 literal characters, not a slash). The
        # URL-decoding step itself is covered at the HTTP level in
        # test_web_server_routes.py, where a %2f payload really is a "/"
        # by the time it reaches this function.
        assert _safe_encounter_id(bad) == ""


# --------------------------------------------------------------- _timer_rows

def test_timer_rows_rounds_remaining_and_drops_category_and_alert_flag():
    rows = [("Enrage", 12.34, 30.0, "boss", False, "Boss"),
            ("Slam", 3.999, 10.0, "boss", True, None)]
    assert _timer_rows(rows) == [
        {"label": "Enrage", "remaining": 12.3, "total": 30.0, "target": "Boss"},
        {"label": "Slam", "remaining": 4.0, "total": 10.0, "target": None},
    ]


# --------------------------------------------------------------- build_live_snapshot

class TestBuildLiveSnapshot:
    def test_uses_display_encounter_not_current_directly(self, monkeypatch):
        """The whole reason build_live_snapshot exists as a separate,
        testable function: it must read display_encounter(), not .current,
        so a just-finished pull's numbers survive the between-pulls
        downtime instead of blanking instantly. See StatsTracker's own
        display_encounter() docstring."""
        tracker = StatsTracker()
        stale = Encounter()
        stale.start_time = 0.0
        stale.last_activity = 10.0
        p = PlayerStats(name="Dps", is_player=True)
        p.damage_done = 5000.0
        stale.players["Dps"] = p
        monkeypatch.setattr(tracker, "display_encounter", lambda: stale)

        result = build_live_snapshot(tracker, TimerEngine(), None, TauntTracker(), _Status())
        assert result["players"][0]["name"] == "Dps"
        assert result["duration"] == 10.0

    def test_watching_and_boss_fields(self):
        tracker = StatsTracker()
        engine = TimerEngine()
        defs = {"tb": _definition_from_dict({
            "id": "tb", "name": "Test Boss", "boss_names": ["Test Boss"],
        })}
        boss_state = BossEncounterState(defs)
        result = build_live_snapshot(tracker, engine, boss_state, TauntTracker(), _Status())
        assert result["watching"] == "watching test log"
        # No active_boss yet -- None, not the placeholder string (that used
        # to collide visually with the overlay's own title text).
        assert result["boss"] is None

    def test_timers_split_into_active_cooldowns_dots_hots_and_alerts(self, sim_clock):
        tracker = StatsTracker()
        engine = TimerEngine()
        sim_clock(0.0)
        engine.start_timer("Enrage", 30.0, category="boss")
        engine.start_timer("Adrenaline Rush", 90.0, category="cooldown")
        engine.start_timer("Corrosive Dart", 15.0, category="dot")
        engine.start_timer("Kolto Probe", 12.0, category="hot")
        engine.start_timer("STACK", 5.0, category="mechanic", is_alert=True)

        result = build_live_snapshot(tracker, engine, None, TauntTracker(), _Status())

        assert [t["label"] for t in result["timers"]] == ["Enrage"]
        assert [t["label"] for t in result["cooldowns"]] == ["Adrenaline Rush"]
        assert {t["label"] for t in result["dots_hots"]} == {"Corrosive Dart", "Kolto Probe"}
        assert result["alerts"] == ["STACK"]

    def test_dots_hots_tagged_and_sorted_by_remaining(self, sim_clock):
        tracker = StatsTracker()
        engine = TimerEngine()
        sim_clock(0.0)
        engine.start_timer("Kolto Probe", 20.0, category="hot")
        engine.start_timer("Corrosive Dart", 5.0, category="dot")

        result = build_live_snapshot(tracker, engine, None, TauntTracker(), _Status())
        tags = [(row["tag"], row["label"]) for row in result["dots_hots"]]
        assert tags == [("DoT", "Corrosive Dart"), ("HoT", "Kolto Probe")], \
            "shortest remaining first, regardless of dot/hot"

    def test_taunt_history_formats_hits_and_misses(self, monkeypatch):
        tracker = StatsTracker()
        taunts = TauntTracker()
        now = 1000.0
        monkeypatch.setattr(time, "time", lambda: now)
        taunts.history.appendleft(TauntResult(at=now - 2, kind="single", hit=True, targets=["Boss"]))
        taunts.history.appendleft(TauntResult(at=now - 1, kind="aoe", hit=True, targets=["A", "B"]))
        taunts.history.appendleft(TauntResult(at=now - 0.5, kind="single", hit=False, targets=[]))

        result = build_live_snapshot(tracker, TimerEngine(), None, taunts, _Status())
        assert result["taunts"][0]["text"] == "no target hit — resisted, out of range, or immune"
        assert result["taunts"][0]["hit"] is False
        assert result["taunts"][1]["text"] == "landed on 2 targets"
        assert result["taunts"][2]["text"] == "landed on Boss"

    def test_boss_dps_and_effective_hps_are_attached_per_player(self):
        tracker = StatsTracker()
        stale = Encounter()
        stale.start_time = 0.0
        stale.last_activity = 10.0
        dps = PlayerStats(name="Dps", is_player=True)
        dps.damage_done = 1000.0
        dps.damage_by_target["Boss"] = 1000.0
        stale.players["Dps"] = dps
        tracker.current = stale  # has meter data -> display_encounter() returns it directly

        defs = {"tb": _definition_from_dict({
            "id": "tb", "name": "Test Boss", "boss_names": ["Boss"],
        })}
        boss_state = BossEncounterState(defs)
        boss_state.active_boss = defs["tb"]

        result = build_live_snapshot(tracker, TimerEngine(), boss_state, TauntTracker(), _Status())
        row = result["players"][0]
        assert row["boss_dps"] == pytest.approx(100.0)  # 1000 dmg / 10s


# --------------------------------------------------------------- build_history_list

class TestBuildHistoryList:
    def test_empty_history_is_an_empty_list(self):
        assert build_history_list(StatsTracker()) == []

    def test_oldest_first_and_top_three_by_dps(self):
        tracker = StatsTracker()
        for i in range(3):
            enc = Encounter()
            enc.start_time = 0.0
            enc.last_activity = 10.0
            for j in range(4):
                p = PlayerStats(name=f"P{j}", is_player=True)
                p.damage_done = (j + 1) * 1000.0 * (i + 1)
                enc.players[p.name] = p
            tracker.history.append(enc)

        rows = build_history_list(tracker)
        assert [r["pull"] for r in rows] == [1, 2, 3], "oldest-first, matching the old Tk tree order"
        assert [t["name"] for t in rows[0]["top"]] == ["P3", "P2", "P1"], "top 3 by dps, highest first"

    def test_unverified_rows_are_flagged(self):
        tracker = StatsTracker()
        enc = Encounter()
        enc.start_time = 0.0
        enc.last_activity = 5.0
        enc.unverified = True
        tracker.history.append(enc)

        rows = build_history_list(tracker)
        assert rows[0]["unverified"] is True

    def test_verified_rows_are_not_flagged(self):
        tracker = StatsTracker()
        enc = Encounter()
        enc.start_time = 0.0
        enc.last_activity = 5.0
        tracker.history.append(enc)

        rows = build_history_list(tracker)
        assert rows[0]["unverified"] is False


# --------------------------------------------------------------- build_history_detail

class TestBuildHistoryDetail:
    def test_out_of_range_index_returns_none(self):
        tracker = StatsTracker()
        assert build_history_detail(tracker, 0) is None
        assert build_history_detail(tracker, -1) is None

    def test_can_upload_requires_log_path_and_line_range(self):
        tracker = StatsTracker()
        enc = Encounter()
        enc.start_time = 0.0
        enc.last_activity = 5.0
        tracker.history.append(enc)
        assert build_history_detail(tracker, 0)["can_upload"] is False

        enc.log_path = "combat.txt"
        enc.start_line = 1
        enc.end_line = 100
        assert build_history_detail(tracker, 0)["can_upload"] is True

    def test_detail_reports_pull_number_one_indexed(self):
        tracker = StatsTracker()
        enc = Encounter(label="Test Boss")
        enc.start_time = 0.0
        enc.last_activity = 5.0
        tracker.history.append(enc)
        detail = build_history_detail(tracker, 0)
        assert detail["pull"] == 1
        assert detail["label"] == "Test Boss"


# --------------------------------------------------------------- build_ability_breakdown

class TestBuildAbilityBreakdown:
    def _encounter_with_player(self):
        enc = Encounter()
        enc.start_time = 0.0
        enc.last_activity = 10.0
        p = PlayerStats(name="Dps", is_player=True)
        p.damage_done = 5000.0
        p.damage_by_ability["Ability A"] = 3000.0
        p.damage_by_ability["Ability B"] = 2000.0
        p.ability_casts = 20
        enc.players["Dps"] = p
        return enc, p

    def test_unknown_player_returns_none(self):
        enc, _p = self._encounter_with_player()
        assert build_ability_breakdown(enc, "Nobody", None) is None

    def test_apm_always_present_but_optional_stats_only_when_relevant(self):
        enc, p = self._encounter_with_player()
        result = build_ability_breakdown(enc, "Dps", None)
        assert "apm" in result["stats"]
        assert result["stats"]["apm"] == pytest.approx(120.0)  # 20 casts / (10s/60)
        # No damage_attempts recorded -- accuracy/crit% must not appear.
        assert "accuracy_pct" not in result["stats"]
        assert "crit_pct" not in result["stats"]
        assert "burst_dps" not in result["stats"], "no damage_events recorded"

    def test_damage_by_ability_sorted_and_rounded(self):
        enc, _p = self._encounter_with_player()
        result = build_ability_breakdown(enc, "Dps", None)
        assert result["damage_by_ability"] == [
            {"ability": "Ability A", "amount": 3000},
            {"ability": "Ability B", "amount": 2000},
        ]

    def test_damage_taken_by_type_sorted_descending(self):
        enc, p = self._encounter_with_player()
        p.damage_taken_by_type = {"elemental": 500.0, "kinetic": 1200.0}
        result = build_ability_breakdown(enc, "Dps", None)
        assert result["damage_taken_by_type"] == [
            {"type": "kinetic", "amount": 1200},
            {"type": "elemental", "amount": 500},
        ]

    def test_damage_taken_by_type_empty_when_nothing_recorded(self):
        enc, _p = self._encounter_with_player()
        result = build_ability_breakdown(enc, "Dps", None)
        assert result["damage_taken_by_type"] == []

    def test_boss_dps_matched_by_encounter_label_not_live_active_boss(self):
        """A historical pull's boss DPS must reflect ITS OWN boss (matched
        via encounter.label), never whatever boss happens to be active
        live right now -- that was the actual bug this stat was fixed
        for (see the function's own comment)."""
        enc, p = self._encounter_with_player()
        enc.label = "Styrak"
        p.damage_by_target["Styrak Add"] = 4000.0

        defs = {
            "styrak": _definition_from_dict({
                "id": "styrak", "name": "Styrak", "boss_names": ["Styrak Add"],
            }),
            "other": _definition_from_dict({
                "id": "other", "name": "Some Other Boss", "boss_names": ["Irrelevant"],
            }),
        }
        boss_state = BossEncounterState(defs)
        # Live active boss is deliberately the WRONG one -- must be ignored.
        boss_state.active_boss = defs["other"]

        result = build_ability_breakdown(enc, "Dps", boss_state)
        assert result["stats"]["boss_dps"] == pytest.approx(400.0)  # 4000 / 10s

    def test_no_boss_state_omits_boss_dps_without_erroring(self):
        enc, _p = self._encounter_with_player()
        enc.label = "Styrak"
        result = build_ability_breakdown(enc, "Dps", None)
        assert "boss_dps" not in result["stats"]


# --------------------------------------------------------------- CorpusState

class TestCorpusState:
    def test_index_is_loaded_once_and_cached(self, monkeypatch):
        calls = []

        def fake_load_index():
            calls.append(1)
            return {"sessions": []}

        import analysis.corpus as corpus_mod
        monkeypatch.setattr(corpus_mod, "load_index", fake_load_index)

        state = CorpusState()
        state.index()
        state.index()
        assert len(calls) == 1, "load_index must only be called once, not on every poll"

    def test_a_corrupt_cache_does_not_raise(self, monkeypatch):
        import analysis.corpus as corpus_mod

        def boom():
            raise ValueError("corrupt json")

        monkeypatch.setattr(corpus_mod, "load_index", boom)
        state = CorpusState()
        assert state.index() is None
        assert state.status()["error"] == "corrupt json"
        assert state.status()["built"] is False

    def test_status_reports_built_false_before_any_load(self, monkeypatch):
        import analysis.corpus as corpus_mod
        monkeypatch.setattr(corpus_mod, "load_index", lambda: None)
        state = CorpusState()
        st = state.status()
        assert st["built"] is False
        assert st["sessions"] == 0
        assert st["encounters"] == 0

    def test_rebuild_refuses_when_already_running(self, monkeypatch):
        import threading as threading_mod

        release = threading_mod.Event()

        def slow_build(progress=None, force=True):
            release.wait(timeout=5)
            return {"sessions": []}

        import analysis.corpus as corpus_mod
        monkeypatch.setattr(corpus_mod, "build_index", slow_build)

        state = CorpusState()
        assert state.rebuild() is True
        assert state.rebuild() is False, "a second rebuild must be refused while one is in flight"
        release.set()
