from .bar_overlay import BarOverlay
"""
overlay.py

Chrome-less, background-transparent bar overlays that float directly on the
game -- the ORBS/BARAS style -- rather than a window sitting on top of it.

The trick is Windows-only: `-transparentcolor` tells the window manager to
punch out every pixel of one exact colour. Paint the window and canvas that
colour and only the marks you draw remain visible, so there is no panel, no
border and no background at all. Those punched-out regions are also
click-through, so the overlay doesn't eat mouse input meant for the game.

Two deliberate departures from the chart palette used elsewhere:

- Colours are more saturated. The chart palette is validated against a known
  fixed surface; an overlay sits on whatever the game is rendering, which
  might be a white flash or a dark corridor a second later. Legibility on an
  unknown, moving backdrop wins.
- Every string is drawn with a 1px black outline (the same text stamped at
  eight offsets underneath). Canvas text has no stroke option, and without
  it light text vanishes the moment something bright passes behind it.

Falls back to a plain dark panel if `-transparentcolor` isn't supported, so
this degrades instead of breaking off-Windows.
"""

import ctypes
import os
import tkinter as tk
import tkinter.font as tkfont

# Any colour that will never be drawn deliberately. Pure magenta is the
# convention; a near-black is used here so the fallback (opaque) case still
# looks like a dark panel rather than a magenta slab.
TRANSPARENT_KEY = "#010203"

# Bars sit on a dim panel rather than bare game, so they don't need to shout.
DAMAGE_BAR = "#c2453c"
HEAL_BAR = "#2f8f63"
TAKEN_BAR = "#8d3b57"
ABSORBED_BAR = "#3170b8"  # same blue family as boss timers -- reads as "defence", not damage
THREAT_BAR = "#d9a53a"    # amber -- "you might pull this", distinct from damage/heal/defence colours
EFFECTIVE_HEAL_BAR = "#6fc79a"  # lighter shade of HEAL_BAR -- same family, distinct at a glance
BOSS_DAMAGE_BAR = "#dd7268"     # lighter shade of DAMAGE_BAR -- same family, distinct at a glance
NOTES_BAR = "#9a7fd1"           # soft lavender -- utility/info, not damage/heal/defence/threat
PANEL = "#15171d"        # cool charcoal, not flat black -- reads as "app", not "hole in the screen"
PANEL_EDGE = "#33363e"
LOCK_EDGE = "#4a9eff"  # panel border while locked -- the visual "don't touch"

# ---- Win32: true click-through while locked ---------------------------
# Disabling the drag/close handlers isn't enough on its own -- the window
# would still swallow the click (just do nothing with it), which means a
# locked frame sitting over a boss's clickable ground effect would eat the
# click instead of passing it to the game. WS_EX_TRANSPARENT makes Windows
# route mouse input straight through to whatever's underneath, regardless
# of what's drawn. -transparentcolor already implies WS_EX_LAYERED; adding
# WS_EX_TRANSPARENT on top is the standard, supported combination.
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_GA_ROOT = 2


def _set_clickthrough(win: tk.Toplevel, enable: bool) -> None:
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        win.update_idletasks()
        hwnd = user32.GetAncestor(win.winfo_id(), _GA_ROOT)
        style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        style = (style | _WS_EX_TRANSPARENT) if enable else (style & ~_WS_EX_TRANSPARENT)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style)
    except Exception:
        pass  # visual lock indicator still applies; worst case it stays draggable

TEXT = "#ffffff"
TEXT_DIM = "#9a9890"
OUTLINE = "#000000"
DIVIDER = "#3a3d44"

ROW_H = 32
PAD_X = 16
PAD_TOP = 14
PAD_BOTTOM = 12
# Rounded-card chrome -- a flat-rectangle panel with a 1px border is what
# reads as a dated Windows utility; a rounded card with a coloured left
# accent stripe is the actual visual language BARAS/modern overlays use.
CORNER_RADIUS = 14
STRIPE_W = 4
HEADER_H = 34
TRACK_H = 3
FONT_TITLE = ("Segoe UI", 12, "bold")
FONT = ("Segoe UI", 10)
FONT_VALUE = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)
# Window alpha applies to everything NOT punched out by the colour key, so
# the panel reads as translucent while the region around it stays fully
# absent. Tk can't do per-pixel alpha, and this combination is the only way
# to get "dim slab, no window edges" -- which is what BARAS actually does.
PANEL_ALPHA = 0.85

KIND_COLOURS = {"dps": DAMAGE_BAR, "hps": HEAL_BAR, "taken": TAKEN_BAR,
                "absorbed": ABSORBED_BAR, "alerts": "#ff7a68", "threat": THREAT_BAR,
                "effective_hps": EFFECTIVE_HEAL_BAR, "boss_dps": BOSS_DAMAGE_BAR,
                "notes": NOTES_BAR, "hots_grid": "#3aa876", "boss_hp": BOSS_DAMAGE_BAR,
                # Matches TimerOverlay.render()'s own internal row palette
                # (boss/cooldown/dot categories) -- without these, all three
                # of "timers"/"cooldowns"/"dots" fell back to the same red
                # DAMAGE_BAR stripe, on top of already sharing the same
                # "Timers" header text (see KIND_TITLES).
                "timers": "#3170b8", "cooldowns": "#d9a53a", "dots": "#3aa876"}
KIND_TITLES = {
    # "hps" is raw healing power output, overheal included -- see
    # PlayerStats.healing_done. Used to be mislabeled "Effective Healing"
    # here despite never having subtracted overheal; "effective_hps" below
    # is the real thing, added once overheal parsing existed to compute it.
    "dps": "Damage", "hps": "Healing (Raw)", "taken": "Damage Taken",
    # Raw absorbed magnitude, not a percentage -- see gui.py's
    # _refresh_bar_overlays for why (bars need a comparable quantity; the
    # live table's "Mitigated" column already carries the per-person %).
    "absorbed": "Shield Absorbed",
    "alerts": "Alerts",
    "effective_hps": "Healing (Effective)",
    "boss_dps": "Boss DPS",
    "notes": "Notes",
    "hots_grid": "HoTs expiring (grid)",
    "boss_hp": "Boss Health",
    "timers": "Timers",
    "cooldowns": "Cooldowns",
    "dots": "DoT Tracker",
}

# Which frames can be toggled, grouped the way BARAS groups them. Each entry
# is (key, label, group). The key is what gui.py switches on when building
# and refreshing the frame.
AVAILABLE_OVERLAYS = [
    ("boss_hp",       "Boss Health + Target",       "Encounter"),
    ("dps",           "Damage (Raw, all targets)", "Metrics"),
    ("boss_dps",      "Boss DPS (no fluff)",        "Metrics"),
    ("hps",           "Healing (Raw)",              "Metrics"),
    ("effective_hps", "Healing (Effective)",        "Metrics"),
    ("taken",         "Damage Taken",                "Metrics"),
    ("absorbed",      "Shield Absorbed",             "Metrics"),
    ("timers",        "Timers",                      "Encounter"),
    ("alerts",        "Mechanic Alerts (Stack/Move/Spread)", "Encounter"),
    ("cooldowns",     "Cooldowns",                    "Effects"),
    ("hots",          "HoTs expiring",                "Effects"),
    ("hots_grid",     "HoTs expiring (grid)",         "Effects"),
    ("dots",          "DoT tracker",                  "Effects"),
    ("notes",         "Notes",                        "Effects"),
]
OVERLAY_GROUPS = ["Metrics", "Encounter", "Effects"]


def compact(v):
    """23,540 -> 23.54K. Overlay columns are narrow and a raid's numbers run
    to six digits; full separators don't fit and aren't read at a glance."""
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{v / 1_000:.2f}K"
    return f"{v:,.0f}"


class NotesOverlay(BarOverlay):
    """Free-form scratch pad -- matches BARAS's notes_overlay. Unlike every
    other bar overlay, the content is editable text the player types (raid
    plan, callouts, whatever), not read-only tracker data, so it embeds a
    real tk.Text widget via canvas.create_window() instead of drawing rows.

    Every other subclass's render() starts with canvas.delete("all"),
    which would destroy an embedded widget on every repaint (e.g. the
    lock-toggle-triggered _redraw()). So this one never uses "all": the
    Text widget is created once in __init__, and render() only
    deletes/redraws the "chrome" tag (panel + header), leaving the widget
    untouched across repeated calls.
    """

    MIN_WIDTH = 180
    MIN_HEIGHT = 90

    def __init__(self, root, x=40, y=340, width=260, height=200, on_close=None,
                 on_move=None, initial_text="", on_text_change=None):
        self.on_text_change = on_text_change
        super().__init__(root, kind="notes", x=x, y=y, width=width, rows=0,
                         on_close=on_close, on_move=on_move, height=height)
        # self.height is now exactly `height` (BarOverlay honours an
        # explicit height over the rows-derived default) -- notes has no
        # row concept, it just sizes the embedded Text widget directly.

        text_top = PAD_TOP + HEADER_H
        text_h = self.height - text_top - PAD_BOTTOM
        cx = self.content_x()
        self.text = tk.Text(self.canvas, wrap="word", bg=PANEL, fg=TEXT,
                            insertbackground=TEXT, relief="flat", bd=0,
                            font=FONT, highlightthickness=0)
        if initial_text:
            self.text.insert("1.0", initial_text)
        self.text.bind("<FocusOut>", self._on_focus_out)
        self._text_window_id = self.canvas.create_window(
            cx, text_top, anchor="nw", window=self.text,
            width=width - cx - PAD_X, height=text_h)

        self.render()

    def _on_focus_out(self, _e=None):
        if self.on_text_change:
            self.on_text_change(self.text.get("1.0", "end-1c"))

    def current_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def set_locked(self, locked: bool) -> None:
        # A locked Notes overlay should still be readable but not editable
        # or click-through-eaten -- the base class's true Win32
        # click-through would make the Text widget unfocusable/unreadable
        # via mouse, which defeats "notes you can glance at while locked".
        self.locked = locked
        self.text.configure(state="disabled" if locked else "normal")
        self._draw_grip()
        self._redraw()

    def _apply_size(self, width, height):
        """Notes has no row concept -- resize the embedded Text widget
        directly instead of recomputing max_rows."""
        self.width = int(width)
        self.height = int(height)
        self.win.geometry(f"{self.width}x{self.height}")
        self.canvas.configure(width=self.width, height=self.height)
        cx = self.content_x()
        text_top = PAD_TOP + HEADER_H
        text_h = self.height - text_top - PAD_BOTTOM
        self.canvas.itemconfig(self._text_window_id,
                               width=self.width - cx - PAD_X, height=text_h)
        self._redraw()

    def render(self, *_args, **_kwargs):
        self._last_render = ((), {})
        c = self.canvas
        c.delete("chrome")
        h = self.height
        edge = LOCK_EDGE if self.locked else PANEL_EDGE
        border = 2 if self.locked else 1
        panel_id = self._rounded_rect(0, 0, self.width, h, CORNER_RADIUS,
                                      fill=PANEL, outline=edge, width=border,
                                      tags=("chrome",))
        c.tag_lower(panel_id)  # keep the Text widget visible on top of it
        c.create_line(6, 12, 6, h - 12, fill=NOTES_BAR, width=STRIPE_W,
                      capstyle=tk.ROUND, tags=("chrome",))
        cx = self.content_x()
        head = "\U0001F512 Notes" if self.locked else "Notes"
        self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE, tags=("chrome",))
        c.create_line(cx, PAD_TOP + HEADER_H - 6, self.width - PAD_X,
                      PAD_TOP + HEADER_H - 6, fill=DIVIDER, tags=("chrome",))
