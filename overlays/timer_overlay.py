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


class TimerOverlay(BarOverlay):
    """Countdown bars -- same floating treatment, bar shrinks as it runs."""

    def __init__(self, root, x=40, y=520, width=250, rows=6, on_close=None,
                 on_move=None, height=None):
        super().__init__(root, kind="timers", x=x, y=y, width=width, rows=rows,
                         on_close=on_close, on_move=on_move, height=height)

    def render(self, timers, total=None, subtitle=None):
        """timers: list of (label, remaining, total_seconds, category,
        is_alert, target). Caller filters out is_alert rows before this
        (they go to AlertOverlay instead), but tolerate them here too just
        in case."""
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
            target = row[5] if len(row) > 5 else None
            colour = palette.get(category, "#3170b8")
            # An AoE DoT genuinely landing on several different mobs at once
            # is several real simultaneous rows sharing one label -- without
            # the target, they're indistinguishable ("still showing more
            # than one plasma probe", which turned out to be 4 correct
            # instances on 4 different adds, not a duplicate bug). Target
            # leads (same convention as HotOverlay: "you re-target by name,
            # not by buff name") since it's the more differentiating half
            # once several rows share a label. Only shown for "dot" --
            # "cooldown"'s target is almost always yourself (redundant
            # clutter), and "boss"/"hot" don't carry a meaningful one here.
            if category == "dot" and target:
                # Cap the target itself first (matches HotOverlay's own
                # [:14] convention) so a real, often-long NPC name
                # ("Attack-Science Technician") can't eat the whole row on
                # its own and push the ability name off entirely -- the
                # pixel-width truncation below is then just a safety net
                # for narrow resized widths, not doing all the work alone.
                target_short = target if len(target) <= 14 else target[:13] + "..."
                row_label = f"{target_short}: {label}"
            else:
                row_label = label
            # Reserve room for the right-aligned remaining-time text so the
            # row text can't run underneath it; measured to fit rather than
            # a fixed character-count slice, which chopped names mid-word
            # regardless of how much space was actually free.
            self._text(cx, y + 12, self._truncate_to_width(row_label, self.width - PAD_X - cx - 46),
                       font=FONT)
            self._text(self.width - PAD_X, y + 12, f"{remaining:.1f}s",
                       fill=colour, anchor="e", font=FONT_VALUE)
            c.create_line(cx, y + 26, self.width - PAD_X, y + 26,
                          fill=PANEL_EDGE, width=TRACK_H, capstyle=tk.ROUND)
            frac = max(0.0, min(1.0, remaining / total_s)) if total_s else 0
            if frac > 0:
                c.create_line(cx, y + 26, cx + track_w * frac, y + 26,
                              fill=colour, width=TRACK_H, capstyle=tk.ROUND)
            y += ROW_H
