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

import pytest

from app_runtime import CharacterSettingsHolder
from boss_definitions import _definition_from_dict
from boss_intelligence import BossEncounterState
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


def _add_pull(tracker, label=None, players=None):
    enc = Encounter(label=label)
    enc.start_time = 0.0
    enc.last_activity = 10.0
    for name, dmg in (players or {"Dps": 5000.0}).items():
        p = PlayerStats(name=name, is_player=True)
        p.damage_done = dmg
        enc.players[name] = p
    tracker.history.append(enc)
    return enc


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
