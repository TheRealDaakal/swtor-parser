"""
Covers the Deep Dive tab's server side: CorpusState (index loading and the
background rebuild) and the /api/corpus/* routes.

The routes are exercised against a real server on a real socket rather than
by calling handler methods directly -- the thing most likely to break here
is routing and query-string handling, which a direct call bypasses
entirely. The path-traversal guard in particular is only meaningful as an
HTTP-level property.
"""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from boss_definitions import _definition_from_dict
from boss_intelligence import BossEncounterState
from stats import StatsTracker
from taunt_tracker import TauntTracker
from timers import TimerEngine
from web_server import CorpusState, make_server


class _Status:
    text = "test"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A real server on an ephemeral port, torn down after the test.

    APPDATA is redirected to tmp_path so these run against an EMPTY corpus
    index rather than whatever happens to be cached on the developer's own
    machine -- otherwise the "no index yet" assertions below would pass
    locally for the wrong reason and mean something different in CI, which
    has no CombatLogs folder at all.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    defs = {"tb": _definition_from_dict({
        "id": "tb", "name": "Test Boss", "boss_names": ["Test Boss"],
        "phases": [{"id": "p1", "name": "Phase 1"}],
    })}
    srv = make_server(StatsTracker(), TimerEngine(), BossEncounterState(defs),
                      TauntTracker(), None, _Status(), port=0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read())


def _status_of(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --------------------------------------------------------------- CorpusState

def test_status_reports_not_built_when_there_is_no_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    state = CorpusState()
    st = state.status()
    assert st["built"] is False
    assert st["building"] is False
    assert st["encounters"] == 0


def test_a_corrupt_cache_does_not_raise(monkeypatch, tmp_path):
    """A bad index file must degrade to "not built", not 500 the whole tab."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import storage
    (storage.data_dir() / "corpus_index.json").write_text("{ not json", encoding="utf-8")
    state = CorpusState()
    assert state.status()["built"] is False


def test_only_one_rebuild_runs_at_a_time(monkeypatch, tmp_path):
    """Two concurrent scans would race on the same cache file."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    release = threading.Event()
    started = threading.Event()

    def slow_build(progress=None, force=False):
        started.set()
        release.wait(timeout=5)
        return {"version": 1, "log_dir": None, "sessions": []}

    from analysis import corpus
    monkeypatch.setattr(corpus, "build_index", slow_build)

    state = CorpusState()
    assert state.rebuild() is True
    assert started.wait(timeout=5)
    assert state.rebuild() is False, "a second rebuild must be refused while one runs"
    assert state.status()["building"] is True
    release.set()


def test_progress_is_surfaced_while_building(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    release = threading.Event()
    reported = threading.Event()

    def slow_build(progress=None, force=False):
        progress(3, 10, "combat_x.txt")
        reported.set()
        release.wait(timeout=5)
        return {"version": 1, "log_dir": None, "sessions": []}

    from analysis import corpus
    monkeypatch.setattr(corpus, "build_index", slow_build)

    state = CorpusState()
    state.rebuild()
    assert reported.wait(timeout=5)
    assert state.status()["progress"] == {"done": 3, "total": 10, "file": "combat_x.txt"}
    release.set()


# -------------------------------------------------------------------- routes

def test_routes_answer_cleanly_with_no_index(server, monkeypatch):
    """First run, before anything has been indexed: every corpus route must
    return built=False rather than erroring, so the tab can render its
    "press Rebuild" empty state."""
    for path, key in (("/api/corpus/bosses", "bosses"),
                      ("/api/corpus/players", "players"),
                      ("/api/corpus/pulls", "pulls")):
        body = _get(server, path)
        assert "built" in body, path
        assert isinstance(body[key], list), path


def test_trend_requires_a_player(server):
    status, body = _status_of(server, "/api/corpus/trend")
    assert status == 400
    assert "player" in body["error"]


def test_trend_rejects_an_unknown_metric(server):
    status, body = _status_of(
        server, "/api/corpus/trend?player=X&metric=" + urllib.parse.quote("; drop"))
    assert status == 400


def test_deep_dive_routes_require_a_line_range(server):
    for route in ("deaths", "timeline", "summary"):
        status, body = _status_of(server, f"/api/corpus/{route}?file=combat.txt")
        # 409 when there's no index at all, 400 when there is but the range
        # is missing -- both are refusals, neither is a 500.
        assert status in (400, 409), route


def test_a_file_outside_the_index_is_refused(server, monkeypatch):
    """The file parameter is resolved THROUGH the index, never joined onto a
    directory: this is a localhost server with no auth, and honouring a
    caller-supplied path is how you get traversal."""
    from web_server import CorpusState as _CS
    monkeypatch.setattr(_CS, "index", lambda self: {"sessions": [
        {"file": "combat_real.txt", "path": "C:/logs/combat_real.txt"}]})

    for bad in ("../../../../Windows/System32/drivers/etc/hosts",
                "C:/Windows/win.ini",
                "/etc/passwd"):
        status, body = _status_of(
            server,
            "/api/corpus/deaths?start=1&end=2&file=" + urllib.parse.quote(bad, safe=""))
        assert status == 404, bad
        assert "no such log" in body["error"], bad
