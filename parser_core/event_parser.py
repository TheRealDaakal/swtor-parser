"""Event parsing boundary for SWTOR combat records.

This adapter is deliberately tolerant: SWTOR parser revisions and fixtures can
use different field names. It converts an already-tokenized record into the
canonical ``NormalizedEvent`` without changing the legacy log parser.
"""

from collections.abc import Mapping
from .events import NormalizedEvent, normalize_event
from .event_kinds import (
    DAMAGE, HEAL, DEATH, CAST, MISS, EFFECT_APPLIED, EFFECT_REMOVED,
    RESOURCE, UNKNOWN,
)

_KIND_ALIASES = {
    "damage": DAMAGE,
    "damaged": DAMAGE,
    "heal": HEAL,
    "healing": HEAL,
    "death": DEATH,
    "killed": DEATH,
    "cast": CAST,
    "ability": CAST,
    "miss": MISS,
    "effect_applied": EFFECT_APPLIED,
    "apply_effect": EFFECT_APPLIED,
    "effect_removed": EFFECT_REMOVED,
    "remove_effect": EFFECT_REMOVED,
    "resource": RESOURCE,
}

def parse_event_record(record) -> NormalizedEvent:
    """Convert a mapping-like parsed record to a canonical event.

    Unknown event kinds are preserved as ``unknown`` rather than discarded.
    The original fields remain available through ``NormalizedEvent.raw``.
    """
    if isinstance(record, NormalizedEvent):
        return record
    if not isinstance(record, Mapping):
        raise TypeError("record must be a mapping or NormalizedEvent")

    data = dict(record)
    raw_kind = data.get("kind") or data.get("event") or data.get("type")
    kind = _KIND_ALIASES.get(str(raw_kind).strip().lower(), UNKNOWN) if raw_kind is not None else UNKNOWN
    data["kind"] = kind
    return normalize_event(data)
