"""Compatibility facade for the overlay implementations.

Concrete overlay classes live in the ``overlays`` package. The historical
``overlay`` module remains import-compatible for existing callers.
"""

from overlays.bar_overlay import (
    BarOverlay,
    TRANSPARENT_KEY, DAMAGE_BAR, HEAL_BAR, TAKEN_BAR, ABSORBED_BAR, THREAT_BAR,
    EFFECTIVE_HEAL_BAR, BOSS_DAMAGE_BAR, NOTES_BAR, PANEL, PANEL_EDGE, LOCK_EDGE,
    TEXT, TEXT_DIM, OUTLINE, DIVIDER, ROW_H, PAD_X, PAD_TOP, PAD_BOTTOM,
    CORNER_RADIUS, STRIPE_W, HEADER_H, TRACK_H, FONT_TITLE, FONT, FONT_VALUE, FONT_SMALL,
    PANEL_ALPHA, KIND_COLOURS, KIND_TITLES, AVAILABLE_OVERLAYS, OVERLAY_GROUPS, compact,
)
from overlays.hot_overlay import HotOverlay
from overlays.hot_grid_overlay import HotGridOverlay
from overlays.timer_overlay import TimerOverlay
from overlays.alert_overlay import AlertOverlay
from overlays.boss_health_overlay import BossHealthOverlay
from overlays.notes_overlay import NotesOverlay
