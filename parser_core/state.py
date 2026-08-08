"""Small, explicit combat-state primitives for the parser refactor."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CombatState:
    """Mutable state for one active combat stream.

    The legacy parser remains authoritative during the migration. This class
    gives future event processors a single place to store encounter state.
    """

    active: bool = False
    started_at: Any = None
    ended_at: Any = None
    source: Optional[str] = None
    encounter_name: Optional[str] = None
    phase: Optional[str] = None
    players: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def start(self, timestamp: Any = None, encounter_name: Optional[str] = None) -> None:
        self.active = True
        self.started_at = timestamp
        self.ended_at = None
        if encounter_name is not None:
            self.encounter_name = encounter_name

    def stop(self, timestamp: Any = None) -> None:
        self.active = False
        self.ended_at = timestamp
