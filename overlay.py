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
    "threat": "Threat",
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
    ("threat",        "Threat",                      "Metrics"),
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


class BarOverlay:
    """One floating metric list (DPS, HPS, ...). Drag anywhere to move, drag
    the bottom-right corner grip to resize."""

    MIN_WIDTH = 160
    # Room for the header plus at least one row -- matches the (rows+2)*
    # ROW_H+10 sizing formula at rows=1, so it lines up exactly with
    # _rows_for_height(MIN_HEIGHT) == 1.
    MIN_HEIGHT = 3 * ROW_H + 10
    GRIP_SIZE = 14

    def __init__(self, root, kind="dps", x=40, y=200, width=250, rows=8,
                 on_close=None, on_move=None, height=None):
        self.kind = kind
        self.width = width
        self.on_close = on_close
        self.on_move = on_move  # called once per drag/resize, on release -- not per pixel
        self.locked = False
        self._drag = (0, 0)
        self._resize_origin = None  # (pointer_x, pointer_y, start_width, start_height)
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

        # An explicit height (restoring a previously resized frame) wins;
        # otherwise derive it from `rows` the way this always worked.
        self.height = int(height) if height is not None else (rows + 2) * ROW_H + 10
        self.max_rows = self._rows_for_height(self.height)
        self.win.geometry(f"{width}x{self.height}+{x}+{y}")

        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT_KEY, highlightthickness=0,
                                bd=0, width=width, height=self.height)
        self.canvas.pack(fill="both", expand=True)

        for seq, fn in (("<ButtonPress-1>", self._drag_start),
                        ("<B1-Motion>", self._drag_move),
                        ("<ButtonRelease-1>", self._drag_end),
                        ("<Button-3>", self._close)):
            self.canvas.bind(seq, fn)

        # Resize grip: a separate tiny widget pinned to the corner via
        # place() rather than a canvas item, so it survives every render()
        # call's canvas.delete("all") without special-casing (NotesOverlay
        # in particular can't have canvas items coming and going under its
        # embedded Text widget).
        self.grip = tk.Canvas(self.win, width=self.GRIP_SIZE, height=self.GRIP_SIZE,
                              bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
                              cursor="size_nw_se")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        for seq, fn in (("<ButtonPress-1>", self._resize_start),
                        ("<B1-Motion>", self._resize_move),
                        ("<ButtonRelease-1>", self._resize_end)):
            self.grip.bind(seq, fn)
        self._draw_grip()

    # ---------------------------------------------------------------- lock

    def set_locked(self, locked: bool) -> None:
        """Locked = genuinely click-through (see _set_clickthrough), not just
        'the handlers refuse to act'. The Python-side guards on drag/close
        below are a second line of defence for the non-Windows fallback path,
        where true click-through isn't available at all."""
        self.locked = locked
        if self.transparent:
            _set_clickthrough(self.win, locked)
        self._draw_grip()
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

    # -------------------------------------------------------------- resize

    @staticmethod
    def _rows_for_height(height):
        """Inverse of the (rows+2)*ROW_H+10 sizing formula -- how many
        rows fit in a given window height."""
        return max(1, int((height - 10) / ROW_H) - 2)

    def _draw_grip(self):
        """Small diagonal-lines resize handle, hidden while locked -- a
        locked overlay is meant to be untouchable, same as drag-to-move."""
        self.grip.delete("all")
        if self.locked:
            return
        s = self.GRIP_SIZE
        for offset in (4, 8, 12):
            self.grip.create_line(s - offset, s - 2, s - 2, s - offset,
                                  fill=PANEL_EDGE, width=1)

    def _resize_start(self, _e=None):
        if self.locked:
            return
        self._resize_origin = (self.win.winfo_pointerx(), self.win.winfo_pointery(),
                               self.width, self.height)

    def _resize_move(self, _e=None):
        if self.locked or self._resize_origin is None:
            return
        ox, oy, ow, oh = self._resize_origin
        dx = self.win.winfo_pointerx() - ox
        dy = self.win.winfo_pointery() - oy
        self._apply_size(max(self.MIN_WIDTH, ow + dx), max(self.MIN_HEIGHT, oh + dy))

    def _resize_end(self, _e=None):
        if self.locked:
            return
        self._resize_origin = None
        if self.on_move:
            self.on_move(self)

    def _apply_size(self, width, height):
        """Live-resizes the window/canvas and recomputes how many rows fit,
        then repaints with the last known data. NotesOverlay overrides this
        -- its content isn't row-based, it resizes an embedded Text widget
        instead."""
        self.width = int(width)
        self.height = int(height)
        self.win.geometry(f"{self.width}x{self.height}")
        self.canvas.configure(width=self.width, height=self.height)
        self.max_rows = self._rows_for_height(self.height)
        self._redraw()

    # ---------------------------------------------------------------- draw

    def _text(self, x, y, s, fill=TEXT, anchor="w", font=FONT, tags=()):
        """Text with a 1px black outline -- Canvas has no stroke option, and
        unoutlined text disappears against a bright game background.
        Returns the visible (non-outline) item id, e.g. for bbox() to place
        another piece of text right after this one.

        tags: forwarded to every item drawn (outline + visible) -- lets a
        caller like NotesOverlay tag its chrome text "chrome" so it can be
        deleted/redrawn without touching unrelated canvas items (e.g. an
        embedded widget window)."""
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (1, -1), (-1, 1), (1, 1)):
            self.canvas.create_text(x + dx, y + dy, text=s, fill=OUTLINE,
                                    anchor=anchor, font=font, tags=tags)
        return self.canvas.create_text(x, y, text=s, fill=fill, anchor=anchor,
                                       font=font, tags=tags)

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

    def _truncate_to_width(self, text, max_width, font=FONT_SMALL):
        """Shrinks text (appending an ellipsis as needed) until it actually
        fits within max_width pixels, measured with real font metrics --
        unlike a fixed character-count slice, this can't still overflow
        depending on what's actually in the string. Used for header
        subtitles (e.g. a boss + phase name) sharing the same row as the
        title text: a fixed-length slice left them visibly colliding
        ("words are clipping each other") whenever the title itself ran
        long enough to eat into the subtitle's assumed space."""
        if max_width <= 0:
            return ""
        fnt = tkfont.Font(font=font)
        if fnt.measure(text) <= max_width:
            return text
        while text and fnt.measure(text + "…") > max_width:
            text = text[:-1]
        return (text + "…") if text else ""

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
        head_id = self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)
        if subtitle:
            _x0, _y0, head_right, _y1 = c.bbox(head_id)
            fitted = self._truncate_to_width(subtitle, (self.width - PAD_X) - (head_right + 10))
            if fitted:
                self._text(self.width - PAD_X, PAD_TOP + 12, fitted,
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
                 on_move=None, within_seconds=None, height=None):
        self.within_seconds = within_seconds
        super().__init__(root, kind="hots", x=x, y=y, width=width, rows=rows,
                         on_close=on_close, on_move=on_move, height=height)

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


class HotGridOverlay(HotOverlay):
    """Same HotTracker.expiring() data as HotOverlay, laid out as a grid of
    per-player cells instead of a sorted list -- one cell per raid member
    recently seen, coloured by how soon their nearest HoT expires (same
    URGENT/SOON thresholds as the list version).

    This is deliberately NOT BARAS's full raid-frame grid (every buff/debuff
    per player) -- that needs a whole per-player effect-tracking subsystem
    and icon assets this project doesn't have. It answers the same "who
    needs healing" question the list already does, just scannable as a
    block instead of read top-to-bottom -- a real layout upgrade, not a
    reimplementation of the bigger BARAS feature.
    """

    GRID_COLS = 3
    CELL_H = 40
    CELL_GAP = 6

    def __init__(self, root, x=40, y=760, width=280, rows=12, on_close=None,
                 on_move=None, within_seconds=None, height=None):
        super().__init__(root, x=x, y=y, width=width, rows=rows,
                         on_close=on_close, on_move=on_move,
                         within_seconds=within_seconds, height=height)
        self.kind = "hots_grid"

    def render(self, rows, total=None, subtitle=None):
        """rows: dicts from HotTracker.expiring() -- identical input shape
        to HotOverlay.render(), just drawn differently."""
        self._last_render = ((rows,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        rows = rows[: self.max_rows]
        cx = self.content_x()
        cols = self.GRID_COLS
        grid_rows = max(1, -(-len(rows) // cols)) if rows else 1
        content_h = grid_rows * self.CELL_H + (grid_rows - 1) * self.CELL_GAP
        h = PAD_TOP + HEADER_H + content_h + PAD_BOTTOM

        edge = LOCK_EDGE if self.locked else PANEL_EDGE
        border = 2 if self.locked else 1
        self._rounded_rect(0, 0, self.width, h, CORNER_RADIUS,
                           fill=PANEL, outline=edge, width=border)
        self.canvas.create_line(6, 12, 6, h - 12, fill=KIND_COLOURS.get("hots"),
                                width=STRIPE_W, capstyle=tk.ROUND)
        self.canvas.create_line(cx, PAD_TOP + HEADER_H - 6, self.width - PAD_X,
                                PAD_TOP + HEADER_H - 6, fill=DIVIDER)

        head = "\U0001F512 HoTs expiring" if self.locked else "HoTs expiring"
        self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)

        y0 = PAD_TOP + HEADER_H
        if not rows:
            self._text(cx, y0 + 12, "all covered", fill=TEXT_DIM, font=FONT_SMALL)
            return

        cell_w = (self.width - PAD_X - cx - (cols - 1) * self.CELL_GAP) / cols
        for i, r in enumerate(rows):
            remaining, duration = r["remaining"], max(r["duration"], 0.001)
            if remaining <= self.URGENT:
                colour = "#e2564a"
            elif remaining <= self.SOON:
                colour = "#d9a53a"
            else:
                colour = "#3aa876"
            col, row_i = i % cols, i // cols
            x0 = cx + col * (cell_w + self.CELL_GAP)
            y = y0 + row_i * (self.CELL_H + self.CELL_GAP)
            self._rounded_rect(x0, y, x0 + cell_w, y + self.CELL_H, 8,
                               fill=colour, outline="", stipple="gray25")
            self._text(x0 + 8, y + 12, r["target"][:12], font=FONT_SMALL)
            self._text(x0 + 8, y + 28, f"{remaining:.1f}s", fill=colour, font=FONT_VALUE)


class TimerOverlay(BarOverlay):
    """Countdown bars -- same floating treatment, bar shrinks as it runs."""

    def __init__(self, root, x=40, y=520, width=250, rows=6, on_close=None,
                 on_move=None, height=None):
        super().__init__(root, kind="timers", x=x, y=y, width=width, rows=rows,
                         on_close=on_close, on_move=on_move, height=height)

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

        # gui.py reuses this one class for three different overlay kinds
        # ("timers", "cooldowns", "dots") -- the header must reflect
        # whichever one this instance actually is, not a fixed string, or
        # all three read identically ("Timers") and there's no way to tell
        # them apart on screen.
        title = KIND_TITLES.get(self.kind, "Timers")
        head = f"\U0001F512 {title}" if self.locked else title
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
                 on_move=None, height=None):
        super().__init__(root, kind="alerts", x=x, y=y, width=width, rows=rows,
                         on_close=on_close, on_move=on_move, height=height)

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


class BossHealthOverlay(BarOverlay):
    """The active boss's HP and current target -- "who is it looking at"
    (tank swaps, cleave targets) alongside "how much longer". Not a row
    list like the other bars; one big health bar plus a target line, so it
    gets its own fully custom render() rather than reusing _panel().
    """

    URGENT_HP = 25.0  # percent -- red, execute-range
    SOON_HP = 50.0    # percent -- amber
    BAR_H = 28

    def __init__(self, root, x=40, y=460, width=280, height=130, on_close=None, on_move=None):
        super().__init__(root, kind="boss_hp", x=x, y=y, width=width, rows=0,
                         on_close=on_close, on_move=on_move, height=height)

    def render(self, boss_name=None, hp_percent=None, hp_current=None,
               hp_max=None, boss_target=None, subtitle=None):
        self._last_render = ((), {
            "boss_name": boss_name, "hp_percent": hp_percent, "hp_current": hp_current,
            "hp_max": hp_max, "boss_target": boss_target, "subtitle": subtitle,
        })
        c = self.canvas
        c.delete("all")
        h = self.height
        edge = LOCK_EDGE if self.locked else PANEL_EDGE
        border = 2 if self.locked else 1
        self._rounded_rect(0, 0, self.width, h, CORNER_RADIUS,
                           fill=PANEL, outline=edge, width=border)
        self.canvas.create_line(6, 12, 6, h - 12, fill=BOSS_DAMAGE_BAR,
                                width=STRIPE_W, capstyle=tk.ROUND)
        cx = self.content_x()

        head = (boss_name or "Boss Health")[:26]
        if self.locked:
            head = f"\U0001F512 {head}"
        head_id = self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)
        if subtitle:
            _x0, _y0, head_right, _y1 = c.bbox(head_id)
            fitted = self._truncate_to_width(subtitle, (self.width - PAD_X) - (head_right + 10))
            if fitted:
                self._text(self.width - PAD_X, PAD_TOP + 12, fitted,
                           fill=TEXT_DIM, anchor="e", font=FONT_SMALL)
        self.canvas.create_line(cx, PAD_TOP + HEADER_H - 6, self.width - PAD_X,
                                PAD_TOP + HEADER_H - 6, fill=DIVIDER)

        bar_y = PAD_TOP + HEADER_H + 6
        bar_right = self.width - PAD_X
        if hp_percent is None:
            self._text(cx, bar_y + self.BAR_H / 2 + 4, "no boss active",
                       fill=TEXT_DIM, font=FONT_SMALL)
        else:
            if hp_percent <= self.URGENT_HP:
                colour = "#e2564a"
            elif hp_percent <= self.SOON_HP:
                colour = "#d9a53a"
            else:
                colour = "#3aa876"
            c.create_rectangle(cx, bar_y, bar_right, bar_y + self.BAR_H,
                               fill=PANEL_EDGE, outline="")
            frac = max(0.0, min(1.0, hp_percent / 100.0))
            if frac > 0:
                c.create_rectangle(cx, bar_y, cx + (bar_right - cx) * frac, bar_y + self.BAR_H,
                                   fill=colour, outline="")
            label = f"{hp_percent:.0f}%"
            if hp_current is not None and hp_max is not None:
                label += f"  ({compact(hp_current)}/{compact(hp_max)})"
            self._text(cx + 8, bar_y + self.BAR_H / 2 + 1, label,
                       fill=TEXT, anchor="w", font=FONT_VALUE)

        target_y = bar_y + self.BAR_H + 16
        self._text(cx, target_y, "Target:", fill=TEXT_DIM, font=FONT_SMALL)
        self._text(cx + 52, target_y, (boss_target or "—")[:20], fill=TEXT, font=FONT)


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
