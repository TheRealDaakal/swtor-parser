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
        # Plain ASCII "..." rather than the single-character ellipsis (…):
        # that glyph came back as a literal U+FFFD replacement character
        # from Tk's canvas text items on this setup (confirmed via repr()
        # on the round-tripped string, not just a console-print artifact --
        # the corruption is real, whatever Tcl/Tk layer causes it). Not
        # worth chasing further when three periods work everywhere.
        while text and fnt.measure(text + "...") > max_width:
            text = text[:-1]
        return (text + "...") if text else ""

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
