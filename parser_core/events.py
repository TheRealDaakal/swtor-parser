"""Normalized combat-event primitives.

This module is intentionally independent of the legacy log parser. It provides
a stable vocabulary for future parser-core extraction without changing the
existing parser's behavior.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """A normalized combat-log event.

    ``kind`` is the canonical event category (damage, heal, death, cast, etc.).
    ``timestamp`` is the source timestamp when available.
    ``source`` and ``target`` are normalized actor identifiers.
    ``amount`` is the primary numeric value when applicable.
    ``raw`` preserves the original parsed fields for compatibility/debugging.
    """

    kind: str
    timestamp: Any = None
    source: Optional[str] = None
    target: Optional[str] = None
    ability: Optional[str] = None
    amount: Optional[float] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


def normalize_event(event: Mapping[str, Any]) -> NormalizedEvent:
    """Normalize common event field aliases without mutating the source."""
    kind = event.get("kind") or event.get("event") or event.get("type") or "unknown"
    timestamp = event.get("timestamp", event.get("time"))
    source = event.get("source", event.get("attacker", event.get("caster")))
    target = event.get("target", event.get("victim"))
    ability = event.get("ability", event.get("ability_name", event.get("action")))
    amount = event.get("amount", event.get("value", event.get("damage", event.get("healing"))))
    return NormalizedEvent(
        kind=str(kind),
        timestamp=timestamp,
        source=source,
        target=target,
        ability=ability,
        amount=amount,
        raw=dict(event),
    )
