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
               hp_max=None, boss_target=None, subtitle=None, hp_markers=None):
        self._last_render = ((), {
            "boss_name": boss_name, "hp_percent": hp_percent, "hp_current": hp_current,
            "hp_max": hp_max, "boss_target": boss_target, "subtitle": subtitle,
            "hp_markers": hp_markers,
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
            # Phase-transition tick marks -- see
            # BossDefinition.hp_phase_markers(). Dark outline behind a thin
            # white line so it stays visible against every fill colour
            # (red/amber/green) without needing a colour-specific case.
            for marker_pct in (hp_markers or []):
                if not (0 < marker_pct < 100):
                    continue
                mx = cx + (bar_right - cx) * (marker_pct / 100.0)
                c.create_line(mx, bar_y - 2, mx, bar_y + self.BAR_H + 2, fill="#000000", width=3)
                c.create_line(mx, bar_y - 2, mx, bar_y + self.BAR_H + 2, fill="#ffffff", width=1)
            label = f"{hp_percent:.0f}%"
            if hp_current is not None and hp_max is not None:
                label += f"  ({compact(hp_current)}/{compact(hp_max)})"
            self._text(cx + 8, bar_y + self.BAR_H / 2 + 1, label,
                       fill=TEXT, anchor="w", font=FONT_VALUE)

        target_y = bar_y + self.BAR_H + 16
        self._text(cx, target_y, "Target:", fill=TEXT_DIM, font=FONT_SMALL)
        self._text(cx + 52, target_y, (boss_target or "—")[:20], fill=TEXT, font=FONT)
