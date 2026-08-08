from .hot_overlay import HotOverlay
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

    Cell positions are user-assigned (click a cell, then click another to
    swap), not urgency-sorted -- see self.slots. Deliberately NOT a full
    raid-roster overlay: a player only gets a slot the first time one of
    your HoTs is tracked on them, and their cell disappears again (leaving
    its position reserved, not backfilled by anyone else) whenever they
    have nothing active, exactly like the old auto-sorted version did --
    the only change is WHERE a given player's cell lives.
    """

    GRID_COLS = 3
    CELL_H = 40
    CELL_GAP = 6
    CLICK_SLOP = 4  # px -- release within this of the press = a click, not a drag
    SELECTED_EDGE = LOCK_EDGE  # reuse the existing "interactive state" blue, not a new colour

    def __init__(self, root, x=40, y=760, width=280, rows=12, on_close=None,
                 on_move=None, within_seconds=None, height=None, initial_slots=None):
        super().__init__(root, x=x, y=y, width=width, rows=rows,
                         on_close=on_close, on_move=on_move,
                         within_seconds=within_seconds, height=height)
        self.kind = "hots_grid"
        # index = grid position, value = assigned player name (or None for
        # an unassigned/reserved-but-empty slot). Restored from the saved
        # per-character layout so positions survive a restart.
        self.slots: list = [s if s else None for s in (initial_slots or [])]
        self._selected_slot = None
        self._cell_rects = []  # [(slot_index, x0, y0, x1, y1), ...] from the last render(), for hit-testing
        self._click_origin = None

    def _slot_for(self, name: str) -> int:
        """Returns name's existing slot, or lazily assigns it the first
        free (None) slot, or appends a new one -- capped at whatever the
        panel currently has room for, matching the same silent-overflow-
        drop precedent as the old rows[:self.max_rows] truncation (resize
        the panel bigger to fit more)."""
        if name in self.slots:
            return self.slots.index(name)
        capacity = self.GRID_COLS * max(1, self._rows_for_height(self.height))
        if None in self.slots:
            idx = self.slots.index(None)
            self.slots[idx] = name
            return idx
        if len(self.slots) < capacity:
            self.slots.append(name)
            return len(self.slots) - 1
        return -1  # grid's full -- this player's cell just won't render this tick

    def render(self, rows, total=None, subtitle=None):
        """rows: dicts from HotTracker.expiring() -- identical input shape
        to HotOverlay.render(), just drawn at each player's assigned
        position instead of urgency-sort order."""
        self._last_render = ((rows,), {"total": total, "subtitle": subtitle})
        c = self.canvas
        c.delete("all")
        self._cell_rects = []
        cx = self.content_x()
        cols = self.GRID_COLS

        by_slot = {}
        for r in rows:
            idx = self._slot_for(r["target"])
            if idx >= 0:
                by_slot[idx] = r
        # Grid footprint covers every RESERVED slot (even ones with nothing
        # active right now), not just the ones with data this tick -- a
        # player's position must stay put while they have no HoT, not
        # collapse and reappear somewhere else next time they do.
        slot_count = len(self.slots)
        grid_rows = max(1, -(-slot_count // cols)) if slot_count else 1
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
        if not slot_count:
            self._text(cx, y0 + 12, "all covered", fill=TEXT_DIM, font=FONT_SMALL)
            return

        cell_w = (self.width - PAD_X - cx - (cols - 1) * self.CELL_GAP) / cols
        for idx in range(slot_count):
            col, row_i = idx % cols, idx // cols
            x0 = cx + col * (cell_w + self.CELL_GAP)
            y = y0 + row_i * (self.CELL_H + self.CELL_GAP)
            self._cell_rects.append((idx, x0, y, x0 + cell_w, y + self.CELL_H))

            r = by_slot.get(idx)
            if r is not None:
                remaining, duration = r["remaining"], max(r["duration"], 0.001)
                if remaining <= self.URGENT:
                    colour = "#e2564a"
                elif remaining <= self.SOON:
                    colour = "#d9a53a"
                else:
                    colour = "#3aa876"
                self._rounded_rect(x0, y, x0 + cell_w, y + self.CELL_H, 8,
                                   fill=colour, outline="", stipple="gray25")
                self._text(x0 + 8, y + 12, r["target"][:12], font=FONT_SMALL)
                self._text(x0 + 8, y + 28, f"{remaining:.1f}s", fill=colour, font=FONT_VALUE)
            if idx == self._selected_slot:
                # Selection highlight draws regardless of whether the slot
                # currently has data -- you can select an empty reserved
                # slot as the swap target too.
                self._rounded_rect(x0, y, x0 + cell_w, y + self.CELL_H, 8,
                                   fill="", outline=self.SELECTED_EDGE, width=2)

    # ------------------------------------------------------- click-to-swap

    def _slot_at(self, x, y):
        for idx, x0, y0, x1, y1 in self._cell_rects:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return idx
        return None

    def _drag_start(self, e):
        super()._drag_start(e)
        self._click_origin = (e.x, e.y)

    def _drag_end(self, e=None):
        super()._drag_end(e)
        if self.locked or self._click_origin is None or e is None:
            self._click_origin = None
            return
        ox, oy = self._click_origin
        self._click_origin = None
        if abs(e.x - ox) > self.CLICK_SLOP or abs(e.y - oy) > self.CLICK_SLOP:
            return  # a real drag -- already handled by the base class's move
        slot = self._slot_at(e.x, e.y)
        if slot is None:
            return
        if self._selected_slot is None:
            self._selected_slot = slot
        elif self._selected_slot == slot:
            self._selected_slot = None  # clicked the same cell again -- deselect
        else:
            a, b = self._selected_slot, slot
            self.slots[a], self.slots[b] = self.slots[b], self.slots[a]
            self._selected_slot = None
        self._redraw()
