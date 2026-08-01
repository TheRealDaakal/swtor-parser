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
THREAT_BAR = "#d9a53a"    # amber -- "you might pull this", distinct from damage/heal/defence colours
EFFECTIVE_HEAL_BAR = "#6fc79a"  # lighter shade of HEAL_BAR -- same family, distinct at a glance
BOSS_DAMAGE_BAR = "#dd7268"     # lighter shade of DAMAGE_BAR -- same family, distinct at a glance
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
                "effective_hps": EFFECTIVE_HEAL_BAR, "boss_dps": BOSS_DAMAGE_BAR}
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
    "threat": "Threat",
    "effective_hps": "Healing (Effective)",
    "boss_dps": "Boss DPS",
}

# Which frames can be toggled, grouped the way BARAS groups them. Each entry
# is (key, label, group). The key is what gui.py switches on when building
# and refreshing the frame.
AVAILABLE_OVERLAYS = [
    ("dps",           "Damage (Raw, all targets)", "Metrics"),
    ("boss_dps",      "Boss DPS (no fluff)",        "Metrics"),
    ("hps",           "Healing (Raw)",              "Metrics"),
    ("effective_hps", "Healing (Effective)",        "Metrics"),
    ("taken",         "Damage Taken",                "Metrics"),
    ("absorbed",      "Shield Absorbed",             "Metrics"),
    ("threat",        "Threat",                      "Metrics"),
    ("timers",        "Timers",                      "Encounter"),
    ("alerts",        "Mechanic Alerts (Stack/Move/Spread)", "Encounter"),
    ("cooldowns",     "Cooldowns",                    "Effects"),
    ("hots",          "HoTs expiring",                "Effects"),
    ("dots",          "DoT tracker",                  "Effects"),
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
        unoutlined text disappears against a bright game background.
        Returns the visible (non-outline) item id, e.g. for bbox() to place
        another piece of text right after this one."""
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (1, -1), (-1, 1), (1, 1)):
            self.canvas.create_text(x + dx, y + dy, text=s, fill=OUTLINE,
                                    anchor=anchor, font=font)
        return self.canvas.create_text(x, y, text=s, fill=fill, anchor=anchor, font=font)

    def _rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
        """Smoothed 12-point polygon -- Canvas has no native rounded-rect
        primitive. A flat rectangle with a 1px border is the exact "old
        Windows utility" look this is meant to replace."""
        points = [
            x1 + radius, y1,  x2 - radius, y1,  x2, y1,
            x2, y1 + radius,  x2, y2 - radius,  x2, y2,
            x2 - radius, y2,  x1 + radius, y2,  x1, y2,
            x1, y2 - radius,  x1, y1 + radius,  x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def content_x(self):
        """Left edge for text/tracks -- clears the accent stripe."""
        return PAD_X + STRIPE_W + 6

    def _panel(self, rows_drawn, has_total):
        """Rounded card sized to the content actually drawn, with a
        coloured left accent stripe (rounded-cap line, not a hard-edged
        block) so each overlay reads at a glance even before the title is
        legible. Sizing to max_rows instead would leave a dead translucent
        block hanging below a short raid, which reads as a broken window."""
        h = PAD_TOP + HEADER_H + rows_drawn * ROW_H + (ROW_H if has_total else 0) + PAD_BOTTOM
        edge = LOCK_EDGE if self.locked else PANEL_EDGE
        width = 2 if self.locked else 1
        self._rounded_rect(0, 0, self.width, h, CORNER_RADIUS,
                           fill=PANEL, outline=edge, width=width)
        stripe_colour = KIND_COLOURS.get(self.kind, DAMAGE_BAR)
        self.canvas.create_line(6, 12, 6, h - 12, fill=stripe_colour,
                                width=STRIPE_W, capstyle=tk.ROUND)
        self.canvas.create_line(self.content_x(), PAD_TOP + HEADER_H - 6,
                                self.width - PAD_X, PAD_TOP + HEADER_H - 6, fill=DIVIDER)
        return h

    def render(self, rows, total=None, subtitle=None):
        """rows: list of (name, value) or (name, value, crit_pct), already
        sorted desc. crit_pct is optional (None for kinds where it isn't
        meaningful, e.g. Damage Taken/Threat) and drawn dim right after the
        name when present."""
        self._last_render = ((rows,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        colour = KIND_COLOURS.get(self.kind, DAMAGE_BAR)
        rows = rows[: self.max_rows]
        self._panel(len(rows), total is not None)
        cx = self.content_x()

        head = KIND_TITLES.get(self.kind, self.kind.upper())
        if self.locked:
            head = f"\U0001F512 {head}"
        self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)
        if subtitle:
            self._text(self.width - PAD_X, PAD_TOP + 12, subtitle[:26],
                       fill=TEXT_DIM, anchor="e", font=FONT_SMALL)

        y = PAD_TOP + HEADER_H
        top = max((r[1] for r in rows), default=0) or 1
        track_w = self.width - PAD_X - cx
        for row in rows:
            name, value = row[0], row[1]
            crit_pct = row[2] if len(row) > 2 else None
            name_id = self._text(cx, y + 12, name[:18], font=FONT)
            if crit_pct is not None:
                _x0, _y0, name_right, _y1 = c.bbox(name_id)
                self._text(name_right + 6, y + 12, f"{crit_pct:.0f}%",
                           fill=TEXT_DIM, font=FONT_SMALL)
            self._text(self.width - PAD_X, y + 12, compact(value),
                       fill=colour, anchor="e", font=FONT_VALUE)
            # Thin rounded-cap meter, not a full-height block -- shows the
            # same relative-magnitude comparison without the flat-rectangle
            # "old utility app" look.
            c.create_line(cx, y + 26, self.width - PAD_X, y + 26,
                          fill=PANEL_EDGE, width=TRACK_H, capstyle=tk.ROUND)
            frac = max(0.0, min(1.0, value / top))
            if frac > 0:
                c.create_line(cx, y + 26, cx + track_w * frac, y + 26,
                              fill=colour, width=TRACK_H, capstyle=tk.ROUND)
            y += ROW_H

        if total is not None:
            c.create_line(cx, y + 2, self.width - PAD_X, y + 2, fill=DIVIDER)
            self._text(cx, y + ROW_H / 2 + 3, "Total", fill=TEXT_DIM, font=FONT_SMALL)
            self._text(self.width - PAD_X, y + ROW_H / 2 + 3, compact(total),
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
        self._panel(max(len(rows), 1), False)
        cx = self.content_x()

        head = "\U0001F512 HoTs expiring" if self.locked else "HoTs expiring"
        self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)

        y = PAD_TOP + HEADER_H
        if not rows:
            self._text(cx, y + 12, "all covered", fill=TEXT_DIM, font=FONT_SMALL)
            return

        track_w = self.width - PAD_X - cx
        for r in rows:
            remaining, duration = r["remaining"], max(r["duration"], 0.001)
            if remaining <= self.URGENT:
                colour = "#e2564a"
            elif remaining <= self.SOON:
                colour = "#d9a53a"
            else:
                colour = "#3aa876"
            # person first -- you re-target by name, not by buff name
            self._text(cx, y + 12, r["target"][:14], font=FONT)
            self._text(self.width - PAD_X, y + 12, f"{remaining:.1f}s",
                       fill=colour, anchor="e", font=FONT_VALUE)
            c.create_line(cx, y + 26, self.width - PAD_X, y + 26,
                          fill=PANEL_EDGE, width=TRACK_H, capstyle=tk.ROUND)
            frac = max(0.0, min(1.0, remaining / duration))
            if frac > 0:
                c.create_line(cx, y + 26, cx + track_w * frac, y + 26,
                              fill=colour, width=TRACK_H, capstyle=tk.ROUND)
            y += ROW_H


class TimerOverlay(BarOverlay):
    """Countdown bars -- same floating treatment, bar shrinks as it runs."""

    def __init__(self, root, x=40, y=520, width=250, rows=6, on_close=None,
                 on_move=None):
        super().__init__(root, kind="timers", x=x, y=y, width=width,
                         rows=rows, on_close=on_close, on_move=on_move)

    def render(self, timers, total=None, subtitle=None):
        """timers: list of (label, remaining, total_seconds, category,
        is_alert). Caller filters out is_alert rows before this (they go to
        AlertOverlay instead), but tolerate them here too just in case."""
        self._last_render = ((timers,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        timers = timers[: self.max_rows]
        self._panel(len(timers), False)
        cx = self.content_x()

        head = "\U0001F512 Timers" if self.locked else "Timers"
        self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)

        y = PAD_TOP + HEADER_H
        track_w = self.width - PAD_X - cx
        palette = {"boss": "#3170b8", "cooldown": "#d9a53a",
                   "dot": "#3aa876", "hot": "#3aa876"}
        for row in timers:
            label, remaining, total_s, category = row[0], row[1], row[2], row[3]
            colour = palette.get(category, "#3170b8")
            self._text(cx, y + 12, label[:20], font=FONT)
            self._text(self.width - PAD_X, y + 12, f"{remaining:.1f}s",
                       fill=colour, anchor="e", font=FONT_VALUE)
            c.create_line(cx, y + 26, self.width - PAD_X, y + 26,
                          fill=PANEL_EDGE, width=TRACK_H, capstyle=tk.ROUND)
            frac = max(0.0, min(1.0, remaining / total_s)) if total_s else 0
            if frac > 0:
                c.create_line(cx, y + 26, cx + track_w * frac, y + 26,
                              fill=colour, width=TRACK_H, capstyle=tk.ROUND)
            y += ROW_H


class AlertOverlay(BarOverlay):
    """Brief callouts ("Move Out", "Spread!") instead of a numeric
    countdown -- BARAS's is_alert display mode. No track meter: the whole
    point is a fast, unambiguous glance, not a duration to read."""

    def __init__(self, root, x=40, y=20, width=260, rows=4, on_close=None,
                 on_move=None):
        super().__init__(root, kind="alerts", x=x, y=y, width=width,
                         rows=rows, on_close=on_close, on_move=on_move)

    def render(self, timers, total=None, subtitle=None):
        """timers: list of (label, remaining, total_seconds, category,
        is_alert) -- pass only the is_alert=True ones in."""
        self._last_render = ((timers,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        timers = timers[: self.max_rows]
        self._panel(max(len(timers), 1), False)
        cx = self.content_x()

        head = "\U0001F512 Alerts" if self.locked else "Alerts"
        self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)

        y = PAD_TOP + HEADER_H
        if not timers:
            self._text(cx, y + 12, "nothing pending", fill=TEXT_DIM, font=FONT_SMALL)
            return
        for row in timers:
            label = row[0]
            self._text(cx, y + 14, label.upper()[:24], fill="#ff7a68",
                       font=("Segoe UI", 13, "bold"))
            y += ROW_H
