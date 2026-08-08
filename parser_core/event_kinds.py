"""Canonical event-kind names used by parser-core code.

These constants are additive and do not alter the legacy parser's behavior.
"""

DAMAGE = "damage"
HEAL = "heal"
DEATH = "death"
CAST = "cast"
MISS = "miss"
EFFECT_APPLIED = "effect_applied"
EFFECT_REMOVED = "effect_removed"
RESOURCE = "resource"
UNKNOWN = "unknown"

KNOWN_EVENT_KINDS = frozenset({
    DAMAGE, HEAL, DEATH, CAST, MISS,
    EFFECT_APPLIED, EFFECT_REMOVED, RESOURCE,
})
