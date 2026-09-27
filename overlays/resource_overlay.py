"""
resource_overlay.py

Shows the local player's resource (energy/rage/ammo/heat/Force -- whatever
their class pool is called) as NET FLOW since the current pull started, not
a current/max bar. SWTOR's combat log only ever emits Spend/Restore as
deltas -- there is no absolute value logged anywhere -- so a real "you're at
62%" bar would mean guessing a starting point and hardcoding each
discipline's max pool, an assumption that silently drifts wrong the moment
someone starts a pull without a full meter. See resource_tracker.py.

Single "current status" panel like BossHealthOverlay, not a per-row list
like HotOverlay/BarOverlay's leaderboards -- there's one number to show, not
several ranked entries.
"""
from .bar_overlay import (
    BarOverlay, compact,
    PAD_X, PAD_TOP, HEADER_H, DIVIDER,
    TEXT, TEXT_DIM, FONT_TITLE, FONT, FONT_VALUE, FONT_SMALL,
)

NET_POSITIVE = "#3aa876"   # same green family as HEAL_BAR -- "you're fine"
NET_NEGATIVE = "#e2564a"   # same red family as URGENT hp -- "you're bleeding resource"


class ResourceOverlay(BarOverlay):
    def __init__(self, root, x=40, y=610, width=250, height=110, on_close=None, on_move=None):
        super().__init__(root, kind="resource", x=x, y=y, width=width, rows=0,
                         on_close=on_close, on_move=on_move, height=height)

    def render(self, snapshot=None):
        """snapshot: a ResourceTracker.snapshot() dict, or None/empty before
        any Spend/Restore has been seen this pull."""
        snapshot = snapshot or {}
        self._last_render = ((), {"snapshot": snapshot})
        c = self.canvas
        c.delete("all")
        h = self.height
        if self.locked:
            c.create_rectangle(0, 0, self.width, h, fill="#15171d", outline="#4a9eff", width=2)
        else:
            c.create_rectangle(0, 0, self.width, h, fill="#15171d", outline="")
        cx = self.content_x()

        resource_type = snapshot.get("resource_type")
        head = resource_type.title() if resource_type else "Resource Flow"
        if self.locked:
            head = f"\U0001F512 {head}"
        self._text(cx, PAD_TOP + 12, head, fill=TEXT, font=FONT_TITLE)
        c.create_line(cx, PAD_TOP + HEADER_H - 6, self.width - PAD_X,
                      PAD_TOP + HEADER_H - 6, fill=DIVIDER)

        row_y = PAD_TOP + HEADER_H + 8
        if resource_type is None:
            self._text(cx, row_y + 8, "no resource activity yet", fill=TEXT_DIM, font=FONT_SMALL)
            return

        net = snapshot.get("net", 0.0)
        colour = NET_POSITIVE if net >= 0 else NET_NEGATIVE
        self._text(cx, row_y, "Net this pull:", fill=TEXT_DIM, font=FONT_SMALL)
        self._text(self.width - PAD_X, row_y, f"{net:+.0f}", fill=colour,
                   anchor="e", font=FONT_VALUE)

        row_y += 22
        spent, restored = snapshot.get("spent_total", 0.0), snapshot.get("restored_total", 0.0)
        self._text(cx, row_y, f"Spent {compact(spent)}", fill=TEXT_DIM, font=FONT_SMALL)
        self._text(self.width - PAD_X, row_y, f"Restored {compact(restored)}",
                   fill=TEXT_DIM, anchor="e", font=FONT_SMALL)

        row_y += 22
        rate = snapshot.get("spend_rate_per_sec", 0.0)
        self._text(cx, row_y, "Spend rate:", fill=TEXT_DIM, font=FONT_SMALL)
        self._text(self.width - PAD_X, row_y, f"{rate:.1f}/s", fill=TEXT,
                   anchor="e", font=FONT)
