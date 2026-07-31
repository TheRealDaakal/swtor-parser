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

# Any colour that will never be drawn deliberately. Pure magenta is the
# convention; a near-black is used here so the fallback (opaque) case still
# looks like a dark panel rather than a magenta slab.
TRANSPARENT_KEY = "#010203"

# Bars sit on a dim panel rather than bare game, so they don't need to shout.
DAMAGE_BAR = "#c2453c"
HEAL_BAR = "#2f8f63"
TAKEN_BAR = "#8d3b57"
ABSORBED_BAR = "#3170b8"  # same blue family as boss timers -- reads as "defence", not damage
PANEL = "#141414"
PANEL_EDGE = "#2e2e2c"
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
TEXT_DIM = "#b9b6ad"
OUTLINE = "#000000"

ROW_H = 18
BAR_H = 14
PAD_X = 7
# Window alpha applies to everything NOT punched out by the colour key, so
# the panel reads as translucent while the region around it stays fully
# absent. Tk can't do per-pixel alpha, and this combination is the only way
# to get "dim slab, no window edges" -- which is what BARAS actually does.
PANEL_ALPHA = 0.82
FONT = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8, "bold")

KIND_COLOURS = {"dps": DAMAGE_BAR, "hps": HEAL_BAR, "taken": TAKEN_BAR, "absorbed": ABSORBED_BAR}
KIND_TITLES = {
    "dps": "Damage", "hps": "Effective Healing", "taken": "Damage Taken",
    # Raw absorbed magnitude, not a percentage -- see gui.py's
    # _refresh_bar_overlays for why (bars need a comparable quantity; the
    # live table's "Mitigated" column already carries the per-person %).
    "absorbed": "Shield Absorbed",
}

# Which frames can be toggled, grouped the way BARAS groups them. Each entry
# is (key, label, group). The key is what gui.py switches on when building
# and refreshing the frame.
AVAILABLE_OVERLAYS = [
    ("dps",       "Damage",          "Metrics"),
    ("hps",       "Effective Healing", "Metrics"),
    ("taken",     "Damage Taken",    "Metrics"),
    ("absorbed",  "Shield Absorbed", "Metrics"),
    ("timers",    "Timers",          "Encounter"),
    ("cooldowns", "Cooldowns",       "Effects"),
    ("hots",      "HoTs expiring",   "Effects"),
    ("dots",      "DoT tracker",     "Effects"),
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


class BarOverlay:
    """One floating metric list (DPS, HPS, ...). Drag anywhere to move."""

    def __init__(self, root, kind="dps", x=40, y=200, width=250, rows=8,
                 on_close=None, on_move=None):
        self.kind = kind
        self.max_rows = rows
        self.width = width
        self.on_close = on_close
        self.on_move = on_move  # called once per drag, on release -- not per pixel
        self.locked = False
        self._drag = (0, 0)
        self._last_render = None  # (args, kwargs) of the last render() call,
                                   # so toggling the lock can repaint immediately

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=TRANSPARENT_KEY)
        self.transparent = True
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT_KEY)
            self.win.attributes("-alpha", PANEL_ALPHA)
        except tk.TclError:
            # not Windows -- keep an opaque dark panel rather than failing
            self.transparent = False
            self.win.attributes("-alpha", 0.88)

        height = (rows + 2) * ROW_H + 10
        self.win.geometry(f"{width}x{height}+{x}+{y}")

        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT_KEY, highlightthickness=0,
                                bd=0, width=width, height=height)
        self.canvas.pack(fill="both", expand=True)

        for seq, fn in (("<ButtonPress-1>", self._drag_start),
                        ("<B1-Motion>", self._drag_move),
                        ("<ButtonRelease-1>", self._drag_end),
                        ("<Button-3>", self._close)):
            self.canvas.bind(seq, fn)

    # ---------------------------------------------------------------- lock

    def set_locked(self, locked: bool) -> None:
        """Locked = genuinely click-through (see _set_clickthrough), not just
        'the handlers refuse to act'. The Python-side guards on drag/close
        below are a second line of defence for the non-Windows fallback path,
        where true click-through isn't available at all."""
        self.locked = locked
        if self.transparent:
            _set_clickthrough(self.win, locked)
        self._redraw()

    def _redraw(self):
        """Re-invokes whatever was last rendered, so the lock indicator
        updates immediately instead of waiting for the next refresh tick."""
        if self._last_render is not None:
            args, kwargs = self._last_render
            self.render(*args, **kwargs)

    # ---------------------------------------------------------------- drag

    def _drag_start(self, e):
        if self.locked:
            return
        self._drag = (e.x, e.y)

    def _drag_move(self, e):
        if self.locked:
            return
        self.win.geometry(f"+{self.win.winfo_pointerx() - self._drag[0]}"
                          f"+{self.win.winfo_pointery() - self._drag[1]}")

    def _drag_end(self, _e=None):
        if self.locked:
            return
        if self.on_move:
            self.on_move(self)

    def _close(self, _e=None):
        if self.locked:
            return
        if self.on_close:
            self.on_close(self)
        self.win.destroy()

    # ---------------------------------------------------------------- draw

    def _text(self, x, y, s, fill=TEXT, anchor="w", font=FONT):
        """Text with a 1px black outline -- Canvas has no stroke option, and
        unoutlined text disappears against a bright game background."""
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (1, -1), (-1, 1), (1, 1)):
            self.canvas.create_text(x + dx, y + dy, text=s, fill=OUTLINE,
                                    anchor=anchor, font=font)
        self.canvas.create_text(x, y, text=s, fill=fill, anchor=anchor, font=font)

    def _panel(self, rows_drawn, has_total):
        """Dim slab sized to the content actually drawn. Sizing it to
        max_rows instead would leave a dead translucent block hanging below
        a short raid, which reads as a broken window."""
        h = 4 + ROW_H + rows_drawn * ROW_H + (ROW_H if has_total else 0) + 4
        edge = LOCK_EDGE if self.locked else PANEL_EDGE
        width = 2 if self.locked else 1
        self.canvas.create_rectangle(0, 0, self.width, h,
                                     fill=PANEL, outline=edge, width=width)
        return h

    def render(self, rows, total=None, subtitle=None):
        """rows: list of (name, value), already sorted desc."""
        self._last_render = ((rows,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        colour = KIND_COLOURS.get(self.kind, DAMAGE_BAR)
        rows = rows[: self.max_rows]
        self._panel(len(rows), total is not None)

        y = 5
        head = KIND_TITLES.get(self.kind, self.kind.upper())
        if self.locked:
            head = f"\U0001F512 {head}"
        self._text(PAD_X, y + 6, head, fill=TEXT_DIM, font=FONT_SMALL)
        if subtitle:
            self._text(self.width - PAD_X, y + 6, subtitle[:26],
                       fill=TEXT_DIM, anchor="e", font=FONT_SMALL)
        y += ROW_H

        top = max((v for _n, v in rows), default=0) or 1
        for name, value in rows:
            frac = max(0.0, min(1.0, value / top))
            bar_w = int((self.width - PAD_X * 2) * frac)
            if bar_w > 0:
                c.create_rectangle(PAD_X, y, PAD_X + bar_w, y + BAR_H,
                                   fill=colour, outline="")
            self._text(PAD_X + 5, y + BAR_H / 2, name[:18])
            self._text(self.width - PAD_X - 4, y + BAR_H / 2,
                       compact(value), anchor="e")
            y += ROW_H

        if total is not None:
            self._text(PAD_X + 5, y + BAR_H / 2, "Total", fill=TEXT_DIM, font=FONT_SMALL)
            self._text(self.width - PAD_X - 4, y + BAR_H / 2, compact(total),
                       fill=TEXT_DIM, anchor="e", font=FONT_SMALL)


class HotOverlay(BarOverlay):
    """Whose HoTs are about to drop.

    The point of this one: it answers the healer's actual question -- "who do
    I need to re-Probe" -- without parking an overlay on top of the group
    frames. Rows are per PERSON, urgency-sorted, and the bar drains as the
    HoT runs down so a nearly-empty bar is the thing that catches your eye.
    """

    URGENT = 4.0   # seconds -- red
    SOON = 8.0     # seconds -- amber

    def __init__(self, root, x=40, y=760, width=250, rows=8, on_close=None,
                 on_move=None, within_seconds=None):
        self.within_seconds = within_seconds
        super().__init__(root, kind="hots", x=x, y=y, width=width,
                         rows=rows, on_close=on_close, on_move=on_move)

    def render(self, rows, total=None, subtitle=None):
        """rows: dicts from HotTracker.expiring()."""
        self._last_render = ((rows,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        rows = rows[: self.max_rows]
        self._panel(len(rows), False)

        y = 5
        head = "\U0001F512 HoTs expiring" if self.locked else "HoTs expiring"
        self._text(PAD_X, y + 6, head, fill=TEXT_DIM, font=FONT_SMALL)
        y += ROW_H

        if not rows:
            self._text(PAD_X + 5, y + BAR_H / 2, "all covered", fill=TEXT_DIM,
                       font=FONT_SMALL)
            return

        for r in rows:
            remaining, duration = r["remaining"], max(r["duration"], 0.001)
            if remaining <= self.URGENT:
                colour = "#c2453c"
            elif remaining <= self.SOON:
                colour = "#b8871f"
            else:
                colour = "#2f8f63"
            bar_w = int((self.width - PAD_X * 2) * max(0.0, min(1.0, remaining / duration)))
            if bar_w > 0:
                c.create_rectangle(PAD_X, y, PAD_X + bar_w, y + BAR_H,
                                   fill=colour, outline="")
            # person first -- you re-target by name, not by buff name
            self._text(PAD_X + 5, y + BAR_H / 2, r["target"][:14])
            self._text(self.width - PAD_X - 4, y + BAR_H / 2,
                       f"{remaining:.1f}s", anchor="e")
            y += ROW_H


class TimerOverlay(BarOverlay):
    """Countdown bars -- same floating treatment, bar shrinks as it runs."""

    def __init__(self, root, x=40, y=520, width=250, rows=6, on_close=None,
                 on_move=None):
        super().__init__(root, kind="timers", x=x, y=y, width=width,
                         rows=rows, on_close=on_close, on_move=on_move)

    def render(self, timers, total=None, subtitle=None):
        """timers: list of (label, remaining, total_seconds, category)."""
        self._last_render = ((timers,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        timers = timers[: self.max_rows]
        self._panel(len(timers), False)

        y = 5
        head = "\U0001F512 Timers" if self.locked else "Timers"
        self._text(PAD_X, y + 6, head, fill=TEXT_DIM, font=FONT_SMALL)
        y += ROW_H

        palette = {"boss": "#3170b8", "cooldown": "#b8871f",
                   "dot": "#2f8f63", "hot": "#2f8f63"}
        for label, remaining, total_s, category in timers:
            frac = max(0.0, min(1.0, remaining / total_s)) if total_s else 0
            colour = palette.get(category, "#3170b8")
            bar_w = int((self.width - PAD_X * 2) * frac)
            if bar_w > 0:
                c.create_rectangle(PAD_X, y, PAD_X + bar_w, y + BAR_H,
                                   fill=colour, outline="")
            self._text(PAD_X + 5, y + BAR_H / 2, label[:20])
            self._text(self.width - PAD_X - 4, y + BAR_H / 2,
                       f"{remaining:.1f}s", anchor="e")
            y += ROW_H
