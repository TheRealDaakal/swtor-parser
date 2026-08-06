"""
Covers a live tester report: "I move an overlay frame to a desired
position, lock it. If I disable the overlay from the metrics menu and
re-enable it, it returns to its default position."

Two separate bugs caused it, and both are exercised here WITHOUT a real Tk
window -- OverlayManager's layout methods only touch storage plus a couple
of plain attributes, so they can be driven against a stand-in object. This
had no test coverage at all when the fix shipped.
"""
import storage
from gui import OverlayManager


class _FakeFrame:
    """Stands in for a live BarOverlay: the layout code only reads .kind,
    .width/.height and the window's x/y."""

    def __init__(self, kind, x, y, w=300, h=200):
        self.kind = kind
        self.width, self.height = w, h
        self.win = type("W", (), {"winfo_x": lambda s, v=x: v,
                                   "winfo_y": lambda s, v=y: v})()


class _Manager:
    """Minimal stand-in exposing exactly what the layout methods use."""

    def __init__(self):
        self.bar_overlays = []
        self._locked = False
        self._overlay_state = {"dps": False}

    _current_character = OverlayManager._current_character
    _persist_overlay_layout = OverlayManager._persist_overlay_layout
    boss_state = None


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))


def test_closing_a_frame_keeps_its_saved_position(monkeypatch, tmp_path):
    """The regression: _persist_overlay_layout() rebuilt "frames" from
    scratch on every save, so a frame's entry vanished from disk the
    moment it was closed -- leaving nothing to restore it from."""
    _isolate(monkeypatch, tmp_path)
    m = _Manager()

    m.bar_overlays = [_FakeFrame("dps", 850, 410)]
    m._persist_overlay_layout()
    assert storage.load_overlay_layout(None)["frames"]["dps"]["x"] == 850

    # User disables the overlay -> it's gone from bar_overlays, and the
    # layout gets persisted again.
    m.bar_overlays = []
    m._persist_overlay_layout()

    saved = storage.load_overlay_layout(None)["frames"]
    assert "dps" in saved, "closing a frame must not erase where it was"
    assert saved["dps"]["x"] == 850
    assert saved["dps"]["y"] == 410


def test_other_frames_positions_survive_an_unrelated_frames_move(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    m = _Manager()
    m.bar_overlays = [_FakeFrame("dps", 100, 100), _FakeFrame("hps", 700, 300)]
    m._persist_overlay_layout()

    # Only the dps frame is open now; moving it must not drop hps's entry.
    m.bar_overlays = [_FakeFrame("dps", 111, 222)]
    m._persist_overlay_layout()

    saved = storage.load_overlay_layout(None)["frames"]
    assert saved["dps"] == {"x": 111, "y": 222, "width": 300, "height": 200}
    assert saved["hps"]["x"] == 700, "an unrelated closed frame kept its spot"


def test_size_is_persisted_alongside_position(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    m = _Manager()
    m.bar_overlays = [_FakeFrame("dps", 10, 20, w=640, h=480)]
    m._persist_overlay_layout()
    saved = storage.load_overlay_layout(None)["frames"]["dps"]
    assert (saved["width"], saved["height"]) == (640, 480)


def test_reenabling_a_frame_reopens_it_where_it_was_left(monkeypatch, tmp_path):
    """The other half of the same report. _apply_overlay()'s toggle-on path
    (_drain_commands' "apply" command) always called with pos=None, so even
    once the position survived on disk it was never looked up -- the frame
    reopened at the default stacked spot."""
    _isolate(monkeypatch, tmp_path)
    import overlay as ov

    created = {}

    class _Stub:
        def __init__(self, root, x=0, y=0, **kw):
            created["x"], created["y"] = x, y
            created["kw"] = kw
            self.kind = "dps"
            self.width, self.height = kw.get("width", 300), kw.get("height", 200)
            self.win = type("W", (), {"winfo_x": lambda s: x, "winfo_y": lambda s: y,
                                       "destroy": lambda s: None})()

        def set_locked(self, v):
            created["locked"] = v

    monkeypatch.setattr(ov, "BarOverlay", _Stub)

    m = _Manager()
    m.root = None
    m._on_overlay_closed = lambda o: None
    m._apply_overlay = OverlayManager._apply_overlay.__get__(m)

    # The user had dragged it here in a previous session.
    storage.save_overlay_layout(
        {"locked": False, "frames": {"dps": {"x": 1420, "y": 660, "width": 512, "height": 128}},
         "notes": "", "hot_grid_slots": []},
        character=None,
    )

    m._overlay_state["dps"] = True
    m._apply_overlay("dps")           # no pos= -- exactly what the toggle does

    assert (created["x"], created["y"]) == (1420, 660), (
        "re-enabling must reopen the frame where the user left it, "
        "not at the default stacked position"
    )
    assert created["kw"].get("width") == 512
    assert created["kw"].get("height") == 128
