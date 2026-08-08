"""Public compatibility facade for combat statistics.

Implementation is split into focused modules while existing imports from
``stats`` remain supported.
"""

# Compatibility: older tests/integrations patch stats.time directly.
import time

from stats_models import (
    PlayerStats, Encounter,
    ENCOUNTER_GAP_SECONDS, TRAILING_CAPTURE_SECONDS, BURST_WINDOW_SECONDS,
    NEW_PULL_MIN_GAP_SECONDS, MIN_ENCOUNTER_SECONDS, HISTORY_DATA_VERSION,
    RAID_DEFEATED_FRACTION, HISTORY_LIMIT,
)
from stats_tracker import StatsTracker
from rotation_analysis import rotation_segments, GCD_BASE_SECONDS, GCD_GAP_MULTIPLIER

__all__ = [
    "PlayerStats", "Encounter", "StatsTracker", "rotation_segments",
    "ENCOUNTER_GAP_SECONDS", "TRAILING_CAPTURE_SECONDS", "BURST_WINDOW_SECONDS",
    "NEW_PULL_MIN_GAP_SECONDS", "MIN_ENCOUNTER_SECONDS", "HISTORY_DATA_VERSION",
    "RAID_DEFEATED_FRACTION", "HISTORY_LIMIT", "GCD_BASE_SECONDS",
    "GCD_GAP_MULTIPLIER",
]
