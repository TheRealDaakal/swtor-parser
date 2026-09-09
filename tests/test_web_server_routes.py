"""Covers the local web UI's HTTP routes that test_corpus_api.py doesn't --
history, timer rules, overlays, character/parsely/cleanup/audio settings,
and the encounter editor's read/write/delete paths.

Exercised against a real server on a real ephemeral-port socket, same
pattern as test_corpus_api.py: the thing most likely to break here is
routing, query-string/body parsing, and index-mapping arithmetic (e.g.
"the display index is 0-based among CUSTOM rules only"), none of which a
direct handler-method call would exercise realistically. The encounter
editor's path-traversal guard in particular is only meaningful as an
HTTP-level property, since the real attack surface is a URL-encoded
path segment that unquote() turns into a literal "/" before
_safe_encounter_id ever sees it.
"""
import json
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import log_watcher
from app_runtime import CharacterSettingsHolder
from boss_definitions import _definition_from_dict
from boss_intelligence import BossEncounterState
from conftest import log_line
from gui import OverlayManager
from stats import Encounter, PlayerStats, StatsTracker
from taunt_tracker import TauntTracker
from timers import TimerEngine
from web_server import make_server


class _Status:
    text = "test"


class _FakeOverlayManager:
    """Stands in for gui.py's OverlayManager: overlay_state/toggle_overlay/
    set_lock/clear_all never touch Tk (they just update plain dict/bool
    state and queue work for the Tk thread to drain later), so the real
    methods can be borrowed directly without a live display -- same
    technique tests/test_overlay_layout.py already uses for the same class."""

    overlay_state = OverlayManager.overlay_state
    toggle_overlay = OverlayManager.toggle_overlay
    set_lock = OverlayManager.set_lock
    clear_all = OverlayManager.clear_all
    # list_profiles/delete_profile are pure storage reads/writes in the real
    # class too; save_profile/apply_profile just validate and enqueue (the
    # part that actually touches Tk -- reading window geometry, tearing
    # down/rebuilding Toplevels -- runs on the Tk thread via _drain_commands,
    # which nothing here ever drives, so these routes are only checked for
    # correct validation/wiring; full save->apply persistence is covered
    # against a real _Manager stand-in in test_overlay_layout.py).
    list_profiles = OverlayManager.list_profiles
    save_profile = OverlayManager.save_profile
    apply_profile = OverlayManager.apply_profile
    delete_profile = OverlayManager.delete_profile

    def __init__(self):
        self._overlay_state = {"dps": False, "hps": False}
        self._locked = False
        self._commands = queue.Queue()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A real server on an ephemeral port with a real (but empty/isolated)
    tracker/timer_engine/boss_state, an encounter-editor directory pair,
    and character_settings -- everything do_GET/do_POST can reach into.
    Returns (base_url, tracker, timer_engine, boss_state, character_settings,
    user_boss_dir)."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    tracker = StatsTracker()
    timer_engine = TimerEngine()
    defs = {"tb": _definition_from_dict({
        "id": "tb", "name": "Test Boss", "boss_names": ["Test Boss"],
    })}
    boss_state = BossEncounterState(defs)
    character_settings = CharacterSettingsHolder()
    bundled_boss_dir = tmp_path / "bundled_bosses"
    bundled_boss_dir.mkdir()
    user_boss_dir = tmp_path / "user_bosses"

    srv = make_server(
        tracker, timer_engine, boss_state, TauntTracker(), _FakeOverlayManager(), _Status(),
        port=0, character_settings=character_settings,
        bundled_boss_dir=bundled_boss_dir, user_boss_dir=user_boss_dir,
    )
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", tracker, timer_engine, boss_state, character_settings, user_boss_dir
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _parse_body(raw: bytes):
    # The catch-all 404 (unmatched route) sends plain text, not JSON --
    # every real route sends JSON, so falling back to the raw text lets a
    # test assert on "this fell through to the catch-all" too instead of
    # crashing on the json.loads() call before the assertion even runs.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return r.status, _parse_body(r.read())


def _get_status(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
            return r.status, _parse_body(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read())


def _post(base, path, payload=None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, _parse_body(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read())


def _add_pull(tracker, label=None, players=None, log_path=None, start_line=None, end_line=None):
    enc = Encounter(label=label)
    enc.start_time = 0.0
    enc.last_activity = 10.0
    for name, dmg in (players or {"Dps": 5000.0}).items():
        p = PlayerStats(name=name, is_player=True)
        p.damage_done = dmg
        enc.players[name] = p
    enc.log_path = log_path
    enc.start_line = start_line
    enc.end_line = end_line
    tracker.history.append(enc)
    return enc


def _write_sample_log(tmp_path, name="sample.txt"):
    """A small, realistic combat log with something for every route that
    reads real log content to work with: two identical-ability hits from
    the same player (rotation_segments needs >=2 occurrences of its
    keyword to bound a segment), a boss recognized via an AbilityActivate
    naming it, incoming damage immediately before a death (so forensics
    has a real "who dealt the killing blows" window to report), and a
    clean EnterCombat/ExitCombat bracket (so replay_pulls/merge_logs see
    one real, complete pull)."""
    lines = [
        log_line("00:00:00.000", "@Player#1", effect_type="Event", effect_name="EnterCombat {1}"),
        log_line("00:00:00.500", "Test Boss", ability="Intro {1}", effect_name="AbilityActivate {1}"),
        log_line("00:00:01.000", "@Player#1", target="Test Boss", ability="Smash {1}",
                 effect_name="Damage {2}", amount="1000"),
        log_line("00:00:02.000", "@Player#1", target="Test Boss", ability="Smash {1}",
                 effect_name="Damage {2}", amount="1200"),
        log_line("00:00:02.500", "Test Boss", target="@Player#1", ability="Slam {1}",
                 effect_name="Damage {2}", amount="50000", target_hp="0/50000"),
        log_line("00:00:03.000", "Test Boss", target="@Player#1", ability="Slam {1}",
                 effect_type="Event", effect_name="Death {2}", target_hp="0/50000"),
        # Past MIN_ENCOUNTER_SECONDS (5.0s) from the 00:00:00 EnterCombat --
        # replay_pulls()/merge_logs() silently discard anything shorter as
        # a trivial sliver, which a 4s pull here would have been.
        log_line("00:00:06.000", "@Player#1", effect_type="Event", effect_name="ExitCombat {1}"),
    ]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="cp1252")
    return path


# --------------------------------------------------------------- history

class TestHistoryRoutes:
    def test_empty_history_is_an_empty_list(self, env):
        base, *_ = env
        status, body = _get(base, "/api/history")
        assert status == 200 and body == []

    def test_history_detail_and_out_of_range(self, env):
        base, tracker, *_ = env
        _add_pull(tracker, label="Test Boss")
        status, body = _get(base, "/api/history/1")
        assert status == 200 and body["label"] == "Test Boss"

        status, body = _get_status(base, "/api/history/99")
        assert status == 404

    def test_player_breakdown_unknown_player_is_404(self, env):
        base, tracker, *_ = env
        _add_pull(tracker)
        status, body = _get_status(base, "/api/history/1/player/Nobody")
        assert status == 404

    def test_player_breakdown_known_player(self, env):
        base, tracker, *_ = env
        _add_pull(tracker, players={"Dps": 8000.0})
        status, body = _get(base, "/api/history/1/player/Dps")
        assert status == 200 and body["name"] == "Dps"

    def test_compare_two_pulls(self, env):
        base, tracker, *_ = env
        _add_pull(tracker, label="Pull 1")
        _add_pull(tracker, label="Pull 2")
        status, body = _get(base, "/api/history/compare?a=1&b=2")
        assert status == 200
        assert body["a"]["label"] == "Pull 1"
        assert body["b"]["label"] == "Pull 2"

    def test_compare_invalid_pull_numbers(self, env):
        base, *_ = env
        status, body = _get_status(base, "/api/history/compare?a=x&b=2")
        assert status == 400


# --------------------------------------------------------------- timer_rules

class TestTimerRules:
    def test_add_list_and_delete_a_custom_rule(self, env):
        base, *_ = env
        status, body = _post(base, "/api/timer_rules",
                              {"keyword": "Slam", "label": "Slam!", "duration": 10.0})
        assert status == 200 and body["ok"] is True

        status, body = _get(base, "/api/timer_rules")
        assert status == 200 and len(body) == 1 and body[0]["label"] == "Slam!"

        status, body = _post(base, "/api/timer_rules/delete", {"index": 0})
        assert status == 200 and body["ok"] is True

        status, body = _get(base, "/api/timer_rules")
        assert body == []

    def test_countdown_from_persists_through_get_and_the_real_rule(self, env):
        base, _tracker, timer_engine, *_ = env
        status, body = _post(base, "/api/timer_rules",
                              {"keyword": "Slam", "duration": 10.0, "countdown_from": 5})
        assert status == 200

        status, body = _get(base, "/api/timer_rules")
        assert body[0]["countdown_from"] == 5

        rule = next(r for r in timer_engine.rules if r.category == "custom")
        assert rule.countdown_from == 5

    def test_countdown_from_defaults_to_zero_and_rejects_garbage_quietly(self, env):
        base, *_ = env
        status, body = _post(base, "/api/timer_rules",
                              {"keyword": "Slam", "duration": 10.0, "countdown_from": "not a number"})
        assert status == 200
        status, body = _get(base, "/api/timer_rules")
        assert body[0]["countdown_from"] == 0

    def test_add_rejects_missing_keyword(self, env):
        base, *_ = env
        status, body = _post(base, "/api/timer_rules", {"duration": 10.0})
        assert status == 400

    def test_add_rejects_invalid_duration(self, env):
        base, *_ = env
        status, body = _post(base, "/api/timer_rules", {"keyword": "x", "duration": "not a number"})
        assert status == 400

    def test_delete_index_is_scoped_to_custom_rules_only(self, env):
        """The display index the web page shows is 0-based among CUSTOM
        rules only -- a boss/cooldown rule registered earlier in
        timer_engine.rules must not shift that mapping."""
        base, _tracker, timer_engine, *_ = env
        from timers import TimerRule
        timer_engine.add_rule(TimerRule(keyword="builtin", label="Builtin", duration_seconds=5.0,
                                        category="cooldown"))
        _post(base, "/api/timer_rules", {"keyword": "Custom", "duration": 10.0})

        status, body = _get(base, "/api/timer_rules")
        assert len(body) == 1 and body[0]["index"] == 0

        status, body = _post(base, "/api/timer_rules/delete", {"index": 0})
        assert status == 200
        # The builtin cooldown rule must survive -- only the custom one is deletable.
        assert any(r.category == "cooldown" for r in timer_engine.rules)
        assert not any(r.category == "custom" for r in timer_engine.rules)

    def test_delete_out_of_range_is_404(self, env):
        base, *_ = env
        status, body = _post(base, "/api/timer_rules/delete", {"index": 5})
        assert status == 404


# --------------------------------------------------------------- overlays

class TestOverlaysRoutes:
    def test_get_initial_state(self, env):
        base, *_ = env
        status, body = _get(base, "/api/overlays")
        assert status == 200
        assert body["locked"] is False
        assert any(item["key"] == "dps" and item["on"] is False for item in body["items"])

    def test_toggle_lock_and_clear(self, env):
        base, *_ = env
        status, body = _post(base, "/api/overlays/toggle", {"key": "dps"})
        assert status == 200
        _, body = _get(base, "/api/overlays")
        assert next(i for i in body["items"] if i["key"] == "dps")["on"] is True

        status, body = _post(base, "/api/overlays/lock", {"locked": True})
        assert status == 200
        _, body = _get(base, "/api/overlays")
        assert body["locked"] is True

        status, body = _post(base, "/api/overlays/clear")
        assert status == 200
        _, body = _get(base, "/api/overlays")
        assert all(i["on"] is False for i in body["items"])


class TestOverlayProfilesRoutes:
    def test_save_apply_delete_all_require_a_name(self, env):
        base, *_ = env
        for path in ("/api/overlay_profiles/save", "/api/overlay_profiles/apply",
                     "/api/overlay_profiles/delete"):
            status, body = _post(base, path, {"name": "  "})
            assert status == 400
            assert "name" in body["error"]

    def test_save_and_apply_are_accepted_and_queued_for_the_tk_thread(self, monkeypatch, tmp_path):
        # Needs the raw overlay_manager (not exposed by the shared `env`
        # fixture) to inspect what got queued -- see _FakeOverlayManager's
        # comment on why save/apply can't be verified end-to-end here.
        monkeypatch.setenv("APPDATA", str(tmp_path))
        overlay_manager = _FakeOverlayManager()
        srv = make_server(StatsTracker(), TimerEngine(), BossEncounterState({}),
                          TauntTracker(), overlay_manager, _Status(), port=0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            status, body = _post(base, "/api/overlay_profiles/save", {"name": "Healing"})
            assert status == 200 and body["ok"] is True
            assert overlay_manager._commands.get_nowait() == ("save_profile", "Healing")

            status, body = _post(base, "/api/overlay_profiles/apply", {"name": "Healing"})
            assert status == 200 and body["ok"] is True
            assert overlay_manager._commands.get_nowait() == ("apply_profile", "Healing")
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)

    def test_list_reflects_storage_and_delete_removes_from_it(self, env):
        import storage
        base, *_ = env
        assert _get(base, "/api/overlay_profiles") == (200, [])

        storage.save_overlay_profile("Healing", {"locked": False, "frames": {}, "notes": "",
                                                   "hot_grid_slots": []})
        status, body = _get(base, "/api/overlay_profiles")
        assert status == 200 and body == ["Healing"]

        status, body = _post(base, "/api/overlay_profiles/delete", {"name": "Healing"})
        assert status == 200 and body["ok"] is True
        assert _get(base, "/api/overlay_profiles") == (200, [])


# --------------------------------------------------------------- character_settings

class TestCharacterSettings:
    def test_get_before_any_character_detected(self, env):
        base, *_ = env
        status, body = _get(base, "/api/character_settings")
        assert status == 200
        assert body == {"character": None, "alacrity_pct": 0.0}

    def test_post_rejected_before_a_character_is_detected(self, env):
        base, *_ = env
        status, body = _post(base, "/api/character_settings", {"alacrity_pct": 10.0})
        assert status == 400

    def test_post_rejects_negative_alacrity(self, env):
        base, _t, _te, _bs, character_settings, _u = env
        character_settings.sync_for_character("@Daakal#1")
        status, body = _post(base, "/api/character_settings", {"alacrity_pct": -5.0})
        assert status == 400

    def test_post_sets_alacrity_once_a_character_is_known(self, env):
        base, _t, _te, _bs, character_settings, _u = env
        character_settings.sync_for_character("@Daakal#1")
        status, body = _post(base, "/api/character_settings", {"alacrity_pct": 12.5})
        assert status == 200
        assert character_settings.alacrity_pct == 12.5
        _, body = _get(base, "/api/character_settings")
        assert body == {"character": "@Daakal#1", "alacrity_pct": 12.5}


# --------------------------------------------------------------- parsely_settings

class TestParselySettings:
    def test_password_is_never_echoed_back(self, env):
        base, *_ = env
        _post(base, "/api/parsely_settings",
              {"username": "Daakal", "password": "hunter2", "guild": "", "visibility": 1})
        status, body = _get(base, "/api/parsely_settings")
        assert status == 200
        assert "password" not in body
        assert body["username"] == "Daakal"

    def test_blank_password_on_update_keeps_the_stored_one(self, env):
        base, *_ = env
        _post(base, "/api/parsely_settings",
              {"username": "Daakal", "password": "hunter2", "guild": "", "visibility": 1})
        # Re-save with username changed but password left blank -- must not
        # clobber the already-stored password.
        _post(base, "/api/parsely_settings",
              {"username": "Daakal2", "password": "", "guild": "", "visibility": 1})

        import storage
        assert storage.load_parsely_settings()["password"] == "hunter2"
        assert storage.load_parsely_settings()["username"] == "Daakal2"


# --------------------------------------------------------------- cleanup_settings / audio_settings

class TestCleanupAndAudioSettings:
    def test_cleanup_settings_roundtrip(self, env):
        base, *_ = env
        status, body = _post(base, "/api/cleanup_settings", {"retention_days": 30})
        assert status == 200
        status, body = _get(base, "/api/cleanup_settings")
        assert body["retention_days"] == 30

    def test_cleanup_settings_rejects_negative(self, env):
        base, *_ = env
        status, body = _post(base, "/api/cleanup_settings", {"retention_days": -1})
        assert status == 400

    def test_audio_settings_roundtrip(self, env):
        base, *_ = env
        status, body = _get(base, "/api/audio_settings")
        assert status == 200
        assert "muted" in body

        status, body = _post(base, "/api/audio_settings", {"muted": True})
        assert status == 200 and body["muted"] is True
        status, body = _get(base, "/api/audio_settings")
        assert body["muted"] is True

    def test_audio_settings_encounter_muted_free_form_keys(self, env):
        base, *_ = env
        status, body = _post(base, "/api/audio_settings", {"encounter_muted": {"styrak_k": True}})
        assert status == 200
        assert body["encounter_muted"] == {"styrak_k": True}
        # Setting it back to false drops the key rather than storing False.
        status, body = _post(base, "/api/audio_settings", {"encounter_muted": {"styrak_k": False}})
        assert body["encounter_muted"] == {}


# --------------------------------------------------------------- encounter editor / path traversal

class TestEncounterEditor:
    def test_list_is_empty_when_no_definitions_registered(self, env):
        base, _t, _te, boss_state, *_ = env
        boss_state.definitions = {}
        status, body = _get(base, "/api/encounters")
        assert status == 200 and body == []

    def test_create_read_and_delete_a_user_encounter(self, env):
        base, *_ = env
        payload = {"id": "my_boss", "name": "My Boss", "boss_names": ["My Boss"]}
        status, body = _post(base, "/api/encounters", payload)
        assert status == 200 and body["ok"] is True

        status, body = _get(base, "/api/encounters/my_boss")
        assert status == 200 and body["name"] == "My Boss"

        status, body = _post(base, "/api/encounters/delete", {"id": "my_boss"})
        assert status == 200 and body["ok"] is True

        status, body = _get_status(base, "/api/encounters/my_boss")
        assert status == 404

    def test_create_rejects_an_invalid_definition(self, env):
        # boss_names/name both default sensibly (_definition_from_dict),
        # so a payload needs a deeper malformation to actually fail
        # validation -- a phase missing its required "id" raises KeyError.
        base, *_ = env
        status, body = _post(base, "/api/encounters", {
            "id": "bad", "name": "Bad", "boss_names": ["Bad"],
            "phases": [{"name": "no id here"}],
        })
        assert status == 400
        assert "invalid encounter definition" in body["error"]

    @pytest.mark.parametrize("bad_id", ["../secret", "..%2Fsecret", "a%2Fb", "%2e%2e%2fsecret"])
    def test_get_rejects_a_url_encoded_traversal_id(self, env, bad_id):
        """The real attack surface: unquote() on the URL path segment turns
        %2f into a literal "/" before _safe_encounter_id ever sees it, so
        this must be exercised over real HTTP, not by calling the helper
        function directly with an already-decoded string."""
        base, *_ = env
        status, body = _get_status(base, f"/api/encounters/{bad_id}")
        assert status in (400, 404), bad_id

    def test_post_rejects_a_traversal_id_and_writes_nothing_outside_user_dir(self, env):
        base, _t, _te, _bs, _cs, user_boss_dir = env
        status, body = _post(base, "/api/encounters",
                              {"id": "../escaped", "name": "Evil", "boss_names": ["Evil"]})
        assert status == 400
        assert not (user_boss_dir.parent / "escaped.json").exists()

    def test_delete_rejects_a_traversal_id(self, env):
        base, *_ = env
        status, body = _post(base, "/api/encounters/delete", {"id": "../../etc/passwd"})
        assert status in (400, 404)

    def test_editor_not_configured_returns_501(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APPDATA", str(tmp_path))
        tracker = StatsTracker()
        srv = make_server(tracker, TimerEngine(), None, TauntTracker(),
                          _FakeOverlayManager(), _Status(), port=0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            status, body = _get_status(base, "/api/encounters")
            assert status == 501
            status, body = _post(base, "/api/encounters", {"id": "x", "name": "X", "boss_names": ["X"]})
            assert status == 501
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)


# --------------------------------------------------------------- import error paths (cheap, no real log files)

class TestImportErrorPaths:
    def test_import_merge_with_no_paths_is_400(self, env):
        base, *_ = env
        status, body = _post(base, "/api/import/merge", {"paths": []})
        assert status == 400

    def test_import_session_with_no_paths_is_400(self, env):
        base, *_ = env
        status, body = _post(base, "/api/import/session", {"paths": []})
        assert status == 400

    def test_anonymize_with_no_path_is_400(self, env):
        base, *_ = env
        status, body = _post(base, "/api/anonymize_log", {})
        assert status == 400

    def test_anonymize_missing_file_is_404(self, env):
        base, *_ = env
        status, body = _post(base, "/api/anonymize_log", {"path": "C:/does/not/exist.txt"})
        assert status == 404


# --------------------------------------------------------------- static routes / update

class TestStaticAndUpdate:
    def test_index_html_app_js_app_css_serve_with_content_type(self, env):
        base, *_ = env
        for path, ctype in (("/", "text/html"), ("/index.html", "text/html"),
                            ("/app.js", "application/javascript"), ("/app.css", "text/css")):
            with urllib.request.urlopen(base + path, timeout=30) as r:
                assert r.status == 200, path
                assert ctype in r.headers.get("Content-Type", ""), path

    def test_unknown_route_is_a_plain_text_404(self, env):
        base, *_ = env
        status, body = _get_status(base, "/api/totally-made-up")
        assert status == 404
        assert body == "not found"

    def test_update_with_no_holder_is_available_false(self, env):
        base, *_ = env
        status, body = _get(base, "/api/update")
        assert status == 200 and body == {"available": False}

    def test_update_with_a_result_reports_available_true(self, monkeypatch, tmp_path):
        from app_runtime import UpdateHolder
        monkeypatch.setenv("APPDATA", str(tmp_path))
        holder = UpdateHolder()
        holder.result = {"version": "9.9.9", "zip_url": "http://x/z.zip"}
        srv = make_server(StatsTracker(), TimerEngine(), None, TauntTracker(),
                          _FakeOverlayManager(), _Status(), port=0, update_holder=holder)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _get(f"http://127.0.0.1:{port}", "/api/update")
            assert status == 200
            assert body == {"available": True, "version": "9.9.9", "zip_url": "http://x/z.zip"}
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)


# --------------------------------------------------------------- corpus success paths

_FAKE_INDEX = {
    "version": 9, "log_dir": "C:/logs", "built_at": 1000.0,
    "sessions": [{
        "file": "combat_test.txt", "date": "2026-08-01", "time": "20:00:00",
        "path": None,  # filled in per-test to point at a real temp file
        "encounters": [{
            "boss": "Test Boss", "boss_id": "tb", "outcome": "kill",
            "duration": 60.0, "deaths": 1, "start_line": 1, "end_line": 7,
            "players": [{"name": "Dps", "damage": 60000, "healing": 0, "taken": 5000,
                        "deaths": 1, "mitigated_pct": 0.0, "absorbed": 0,
                        "top_damage": [], "top_healing": []}],
        }],
    }],
}


class TestCorpusRouteSuccessPaths:
    def _built_index(self, sample_log=None):
        idx = json.loads(json.dumps(_FAKE_INDEX))  # deep copy, JSON-safe shape anyway
        if sample_log is not None:
            idx["sessions"][0]["path"] = str(sample_log)
        return idx

    def test_bosses_players_pulls_trend_report_built_true_with_real_rows(self, env, monkeypatch):
        base, *_ = env
        fake_index = self._built_index()
        from web_server_helpers import CorpusState as _CS
        monkeypatch.setattr(_CS, "index", lambda self: fake_index)

        status, body = _get(base, "/api/corpus/bosses")
        assert status == 200 and body["built"] is True
        assert body["bosses"][0]["boss_id"] == "tb"
        assert body["bosses"][0]["kills"] == 1

        status, body = _get(base, "/api/corpus/players")
        assert status == 200 and body["built"] is True
        assert body["players"][0]["name"] == "Dps"

        status, body = _get(base, "/api/corpus/pulls?boss_id=tb")
        assert status == 200 and body["built"] is True
        assert body["pulls"][0]["boss_id"] == "tb"

        status, body = _get(base, "/api/corpus/trend?player=Dps&metric=dps")
        assert status == 200 and body["built"] is True
        assert body["series"][0]["boss_id"] == "tb"

    def test_deaths_timeline_summary_resolve_a_real_session_file(self, env, monkeypatch, tmp_path):
        base, *_ = env
        sample = _write_sample_log(tmp_path)
        fake_index = self._built_index(sample)
        from web_server_helpers import CorpusState as _CS
        monkeypatch.setattr(_CS, "index", lambda self: fake_index)

        for route in ("deaths", "timeline", "summary"):
            status, body = _get(
                base, f"/api/corpus/{route}?file=combat_test.txt&start=1&end=7")
            assert status == 200, (route, body)

        status, body = _get(base, "/api/corpus/deaths?file=combat_test.txt&start=1&end=7")
        assert len(body["reports"]) == 1
        assert body["reports"][0]["victim"] == "Player"


# --------------------------------------------------------------- routes needing a real log file

class TestRealLogRoutes:
    def test_rotation_route_success(self, env, tmp_path):
        base, tracker, *_ = env
        sample = _write_sample_log(tmp_path)
        _add_pull(tracker, log_path=str(sample), start_line=1, end_line=7)
        status, body = _get(
            base, "/api/history/1/player/" + urllib.parse.quote("Player") + "/rotation?keyword=Smash")
        assert status == 200, body
        assert len(body["segments"]) == 1

    def test_rotation_route_keyword_occurring_once_is_404(self, env, tmp_path):
        base, tracker, *_ = env
        sample = _write_sample_log(tmp_path)
        _add_pull(tracker, log_path=str(sample), start_line=1, end_line=7)
        status, body = _get_status(
            base, "/api/history/1/player/" + urllib.parse.quote("Player") + "/rotation?keyword=Intro")
        assert status == 404

    def test_history_deaths_timeline_summary_success(self, env, tmp_path):
        base, tracker, *_ = env
        sample = _write_sample_log(tmp_path)
        _add_pull(tracker, log_path=str(sample), start_line=1, end_line=7)
        for route in ("deaths", "timeline", "summary"):
            status, body = _get(base, f"/api/history/1/{route}")
            assert status == 200, (route, body)

    def test_import_merge_success(self, env, tmp_path):
        base, tracker, *_ = env
        sample = _write_sample_log(tmp_path)
        status, body = _post(base, "/api/import/merge", {"paths": [str(sample)]})
        assert status == 200 and body["imported"] == 1
        assert len(tracker.history) == 1

    def test_import_session_success(self, env, tmp_path):
        base, tracker, *_ = env
        sample = _write_sample_log(tmp_path)
        status, body = _post(base, "/api/import/session", {"paths": [str(sample)]})
        assert status == 200 and body["imported"] == 1
        assert len(tracker.history) == 1

    def test_import_session_is_idempotent_on_the_same_file(self, env, tmp_path):
        base, tracker, *_ = env
        sample = _write_sample_log(tmp_path)
        _post(base, "/api/import/session", {"paths": [str(sample)]})
        status, body = _post(base, "/api/import/session", {"paths": [str(sample)]})
        assert status == 200 and body["imported"] == 0
        assert len(tracker.history) == 1, "the second import must not duplicate the pull"

    def test_anonymize_success(self, env, tmp_path):
        base, *_ = env
        sample = _write_sample_log(tmp_path)
        status, body = _post(base, "/api/anonymize_log", {"path": str(sample)})
        assert status == 200 and body["ok"] is True
        assert body["players_replaced"] >= 1
        assert Path(body["dest_path"]).exists()


# --------------------------------------------------------------- validation edge cases

class TestValidationEdgeCases:
    def test_history_compare_out_of_range_is_404(self, env):
        base, tracker, *_ = env
        _add_pull(tracker)
        status, body = _get_status(base, "/api/history/compare?a=1&b=5")
        assert status == 404

    def test_player_breakdown_out_of_range_pull_is_404(self, env):
        base, *_ = env
        status, body = _get_status(base, "/api/history/5/player/Dps")
        assert status == 404

    def test_timer_rules_invalid_warn_falls_back_to_zero_not_an_error(self, env):
        base, _tracker, timer_engine, *_ = env
        status, body = _post(base, "/api/timer_rules",
                              {"keyword": "x", "duration": 10.0, "warn": "not a number"})
        assert status == 200
        rule = next(r for r in timer_engine.rules if r.category == "custom")
        assert rule.warn_seconds_before == 0.0

    def test_timer_rules_delete_with_a_non_integer_index_is_400(self, env):
        base, *_ = env
        status, body = _post(base, "/api/timer_rules/delete", {"index": "not a number"})
        assert status == 400

    def test_character_settings_post_rejects_a_non_numeric_alacrity(self, env):
        base, _t, _te, _bs, character_settings, _u = env
        character_settings.sync_for_character("@Daakal#1")
        status, body = _post(base, "/api/character_settings", {"alacrity_pct": "not a number"})
        assert status == 400

    def test_cleanup_settings_rejects_a_non_numeric_retention(self, env):
        base, *_ = env
        status, body = _post(base, "/api/cleanup_settings", {"retention_days": "not a number"})
        assert status == 400

    def test_audio_settings_ignores_unrecognised_category_keys(self, env):
        base, *_ = env
        status, body = _post(base, "/api/audio_settings",
                              {"category_muted": {"not_a_real_category": True}})
        assert status == 200
        assert "not_a_real_category" not in body["category_muted"]

    def test_corpus_rebuild_refuses_while_one_is_already_running(self, env, monkeypatch):
        import threading as threading_mod
        base, *_ = env
        release = threading_mod.Event()

        def slow_build(progress=None, force=True):
            release.wait(timeout=5)
            return {"sessions": []}

        import analysis.corpus as corpus_mod
        monkeypatch.setattr(corpus_mod, "build_index", slow_build)
        try:
            status, body = _post(base, "/api/corpus/rebuild")
            assert status == 200 and body["started"] is True
            status, body = _post(base, "/api/corpus/rebuild")
            assert status == 409
        finally:
            release.set()

    def test_encounters_single_get_not_configured_returns_501(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APPDATA", str(tmp_path))
        srv = make_server(StatsTracker(), TimerEngine(), None, TauntTracker(),
                          _FakeOverlayManager(), _Status(), port=0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            status, body = _get_status(base, "/api/encounters/anything")
            assert status == 501
            status, body = _post(base, "/api/encounters/delete", {"id": "anything"})
            assert status == 501
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)

    def test_encounters_get_with_a_corrupt_file_is_500_not_a_crash(self, env):
        base, _t, _te, _bs, _cs, user_boss_dir = env
        user_boss_dir.mkdir(parents=True, exist_ok=True)
        (user_boss_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
        status, body = _get_status(base, "/api/encounters/broken")
        assert status == 500


# --------------------------------------------------------------- parsely / update (mocked network)

class TestParselyAndUpdateMocked:
    def test_upload_path_success(self, env, monkeypatch):
        base, *_ = env
        from parsely_upload import ParselyUploadResult
        monkeypatch.setattr("parsely_upload.upload_file",
                            lambda *a, **kw: ParselyUploadResult(success=True, link="http://parsely/x"))
        status, body = _post(base, "/api/parsely/upload_path", {"path": "C:/fake.txt"})
        assert status == 200 and body == {"success": True, "link": "http://parsely/x"}

    def test_upload_current_with_no_active_log_reports_failure_not_500(self, env):
        base, *_ = env
        status, body = _post(base, "/api/parsely/upload_current", {})
        assert status == 200 and body["success"] is False

    def test_upload_current_success(self, env, monkeypatch):
        base, tracker, *_ = env
        tracker.current_log_path = "C:/fake.txt"
        from parsely_upload import ParselyUploadResult
        monkeypatch.setattr("parsely_upload.upload_file",
                            lambda *a, **kw: ParselyUploadResult(success=True, link="http://parsely/y"))
        status, body = _post(base, "/api/parsely/upload_current", {})
        assert status == 200 and body["link"] == "http://parsely/y"

    def test_history_upload_success(self, env, monkeypatch, tmp_path):
        base, tracker, *_ = env
        sample = _write_sample_log(tmp_path)
        _add_pull(tracker, log_path=str(sample), start_line=1, end_line=7)
        from parsely_upload import ParselyUploadResult
        monkeypatch.setattr("parsely_upload.upload_encounter",
                            lambda *a, **kw: ParselyUploadResult(success=True, link="http://parsely/z"))
        status, body = _post(base, "/api/history/1/upload", {})
        assert status == 200 and body["link"] == "http://parsely/z"

    def test_history_upload_without_line_range_reports_failure(self, env):
        base, tracker, *_ = env
        _add_pull(tracker)  # no log_path/start_line/end_line
        status, body = _post(base, "/api/history/1/upload", {})
        assert status == 200 and body["success"] is False

    def test_update_apply_with_no_result_is_400(self, env):
        base, *_ = env
        status, body = _post(base, "/api/update/apply", {})
        assert status == 400

    def test_update_apply_reports_an_update_error_as_a_clean_500(self, monkeypatch, tmp_path):
        from app_runtime import UpdateHolder
        monkeypatch.setenv("APPDATA", str(tmp_path))
        holder = UpdateHolder()
        holder.result = {"zip_url": "http://x/z.zip"}
        import updater
        monkeypatch.setattr(updater, "prepare_update",
                            lambda *a, **kw: (_ for _ in ()).throw(updater.UpdateError("checksum mismatch")))
        srv = make_server(StatsTracker(), TimerEngine(), None, TauntTracker(),
                          _FakeOverlayManager(), _Status(), port=0, update_holder=holder)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _post(f"http://127.0.0.1:{port}", "/api/update/apply", {})
            assert status == 500
            assert "checksum mismatch" in body["error"]
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)

    def test_update_apply_success(self, monkeypatch, tmp_path):
        from app_runtime import UpdateHolder
        monkeypatch.setenv("APPDATA", str(tmp_path))
        holder = UpdateHolder()
        holder.result = {"version": "9.9.9", "zip_url": "http://x/z.zip", "sha256_url": "http://x/z.sha256"}

        import updater
        monkeypatch.setattr(updater, "prepare_update", lambda *a, **kw: tmp_path)
        monkeypatch.setattr(updater, "stage_relaunch", lambda *a, **kw: None)
        monkeypatch.setattr(updater, "update_log_path", lambda: tmp_path / "update.log")

        shutdown_called = []
        srv = make_server(StatsTracker(), TimerEngine(), None, TauntTracker(),
                          _FakeOverlayManager(), _Status(), port=0, update_holder=holder,
                          request_shutdown=lambda: shutdown_called.append(1))
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            status, body = _post(base, "/api/update/apply", {})
            assert status == 200 and body["success"] is True
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)


# --------------------------------------------------------------- remaining gaps

class TestRemainingCoverageGaps:
    def test_live_route(self, env):
        base, *_ = env
        status, body = _get(base, "/api/live")
        assert status == 200 and "players" in body

    def test_corpus_status_route(self, env):
        base, *_ = env
        status, body = _get(base, "/api/corpus/status")
        assert status == 200 and body["built"] is False

    def test_corpus_trend_with_no_index_built_yet(self, env):
        base, *_ = env
        status, body = _get(base, "/api/corpus/trend?player=Dps")
        assert status == 200 and body == {"built": False, "series": []}

    def test_corpus_deaths_before_the_index_is_built_is_409(self, env):
        base, *_ = env
        status, body = _get_status(base, "/api/corpus/deaths?file=x.txt&start=1&end=2")
        assert status == 409

    def test_post_catch_all_route_is_also_a_plain_text_404(self, env):
        base, *_ = env
        status, body = _post(base, "/api/totally-made-up")
        assert status == 404 and body == "not found"

    def test_malformed_json_body_is_treated_as_an_empty_object_not_a_crash(self, env):
        base, *_ = env
        req = urllib.request.Request(base + "/api/timer_rules", data=b"{not json",
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, body = r.status, _parse_body(r.read())
        except urllib.error.HTTPError as e:
            status, body = e.code, _parse_body(e.read())
        # An empty body means "keyword" is missing -- same 400 a genuinely
        # empty POST would get, not a 500 from a JSON parse crash.
        assert status == 400

    def test_upload_result_json_failure_branch(self, env, monkeypatch):
        base, *_ = env
        from parsely_upload import ParselyUploadResult
        monkeypatch.setattr("parsely_upload.upload_file",
                            lambda *a, **kw: ParselyUploadResult(success=False, error="bad credentials"))
        status, body = _post(base, "/api/parsely/upload_path", {"path": "C:/fake.txt"})
        assert status == 200
        assert body == {"success": False, "error": "bad credentials"}

    def test_character_settings_get_with_no_holder_configured_at_all(self, monkeypatch, tmp_path):
        """Distinct from "no character detected yet" (a real holder whose
        .character is None) -- this is the server constructed WITHOUT a
        character_settings object passed in at all."""
        monkeypatch.setenv("APPDATA", str(tmp_path))
        srv = make_server(StatsTracker(), TimerEngine(), None, TauntTracker(),
                          _FakeOverlayManager(), _Status(), port=0)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _get(f"http://127.0.0.1:{port}", "/api/character_settings")
            assert status == 200 and body == {"character": None, "alacrity_pct": 0.0}
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)

    def test_cleanup_now_with_a_retention_period_set(self, env, monkeypatch, tmp_path):
        base, *_ = env
        empty_log_dir = tmp_path / "CombatLogs"
        empty_log_dir.mkdir()
        monkeypatch.setattr(log_watcher, "find_log_dir", lambda: str(empty_log_dir))
        _post(base, "/api/cleanup_settings", {"retention_days": 30})
        status, body = _post(base, "/api/cleanup_now", {})
        assert status == 200 and body["ok"] is True and body["archived_count"] == 0

    def test_cleanup_now_without_a_retention_period_set_is_400(self, env, monkeypatch, tmp_path):
        base, *_ = env
        monkeypatch.setattr(log_watcher, "find_log_dir", lambda: str(tmp_path))
        status, body = _post(base, "/api/cleanup_now", {})
        assert status == 400

    def test_cleanup_now_with_no_log_dir_found_is_404(self, env, monkeypatch):
        base, *_ = env
        monkeypatch.setattr(log_watcher, "find_log_dir", lambda: None)
        _post(base, "/api/cleanup_settings", {"retention_days": 30})
        status, body = _get_status(base, "/api/cleanup_settings")  # sanity: settings really saved
        assert body["retention_days"] == 30
        status, body = _post(base, "/api/cleanup_now", {})
        assert status == 404

    def test_history_deaths_route_no_such_pull_and_no_line_range(self, env, tmp_path):
        base, tracker, *_ = env
        status, body = _get_status(base, "/api/history/9/deaths")
        assert status == 404

        _add_pull(tracker)  # no log_path/start_line/end_line
        status, body = _get_status(base, "/api/history/1/deaths")
        assert status == 404
        assert "line-range" in body["error"]

    def test_history_deaths_route_unreadable_log_is_500_not_a_crash(self, env, tmp_path):
        base, tracker, *_ = env
        _add_pull(tracker, log_path=str(tmp_path / "does_not_exist.txt"), start_line=1, end_line=5)
        status, body = _get_status(base, "/api/history/1/deaths")
        assert status == 500

    def test_rotation_route_no_such_pull_no_line_range_and_missing_keyword(self, env, tmp_path):
        base, tracker, *_ = env
        status, body = _get_status(base, "/api/history/9/player/Dps/rotation?keyword=x")
        assert status == 404

        _add_pull(tracker)  # no log_path/start_line/end_line
        status, body = _get_status(base, "/api/history/1/player/Dps/rotation?keyword=x")
        assert status == 404
        assert "line-range" in body["error"]

        sample = _write_sample_log(tmp_path)
        _add_pull(tracker, log_path=str(sample), start_line=1, end_line=7)
        status, body = _get_status(base, "/api/history/2/player/Player/rotation")
        assert status == 400
        assert "keyword" in body["error"]

    def test_import_merge_with_no_recognizable_events_reports_zero_imported(self, env, tmp_path):
        base, *_ = env
        empty_log = tmp_path / "empty.txt"
        empty_log.write_text("", encoding="cp1252")
        status, body = _post(base, "/api/import/merge", {"paths": [str(empty_log)]})
        assert status == 200 and body["imported"] == 0
