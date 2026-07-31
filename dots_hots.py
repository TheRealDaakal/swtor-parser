"""
dots_hots.py

Personal DoT/HoT uptime tracking, translated from BARAS's own dots.toml
(46 entries) and hots.toml (10 entries). Same pattern as cooldowns.py: one
always-armed TimerRule per effect, scoped to the local player, tagged
category="dot"/"hot" so the GUI can group them separately from boss timers
and defensive cooldowns.

Ability names came from two sources, in order of trust:
  1. This user's own real combat-log corpus (5.4M lines across 208 logs) --
     the effect's numeric ID, as logged by BARAS, resolved against every
     name that ID was ever seen under in this user's actual game client.
  2. BARAS's own curated `name` field, only for the handful of entries this
     user's corpus never logged (mostly other classes' discipline-specific
     variants, e.g. Gunslinger/Sniper's Corrosive Grenade) -- still real
     BARAS source data, just not independently confirmed against this
     user's own logs. Flagged individually below.

Several class abilities share identical display text across disciplines
(e.g. "Vital Shot" logs the same regardless of which skill tree buffed its
duration) -- since our engine matches by keyword text, not class/spec, only
one TimerRule per unique keyword is registered; duplicate entries in
BARAS's source data collapse to a single representative duration.

Not translated: `refresh_abilities` (a DoT/HoT re-applied via certain other
abilities resets its remaining duration instead of stacking a second
countdown -- our TimerRule engine has no keyword-based refresh-not-stack
mechanism, same category of limitation as cooldowns.py not implementing
`cooldown_ready_secs`) and `is_affected_by_alacrity` (duration scaling with
the player's alacrity stat, also not implemented for defensive cooldowns).
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from timers import TimerRule


@dataclass
class DotHotDefinition:
    label: str  # real ability/effect name, matched as the trigger keyword
    duration_seconds: float


# -- DoTs (translated from BARAS's dots.toml) --------------------------------
DOTS: List[DotHotDefinition] = [
    DotHotDefinition('Affliction', duration_seconds=18.0),
    DotHotDefinition('Bleeding', duration_seconds=15.0),
    DotHotDefinition('Bleeding (Deadly Saber)', duration_seconds=6.0),
    DotHotDefinition('Bleeding (Draining Scream)', duration_seconds=6.0),
    DotHotDefinition('Bleeding (Eviscerate)', duration_seconds=6.0),
    DotHotDefinition('Bleeding (Rupture)', duration_seconds=9.0),
    DotHotDefinition('Bleeding (Shatter)', duration_seconds=12.0),
    DotHotDefinition('Burning (Burning Blade)', duration_seconds=6.0),
    DotHotDefinition('Burning (Burning Purpose)', duration_seconds=6.0),
    DotHotDefinition('Burning (Incendiary Missile)', duration_seconds=15.0),
    DotHotDefinition('Burning (Incendiary Round)', duration_seconds=15.0),
    DotHotDefinition('Burning (Plasma Brand)', duration_seconds=12.0),
    DotHotDefinition('Burning (Priming Shot)', duration_seconds=15.0),
    DotHotDefinition('Burning Purpose', duration_seconds=6.0),
    DotHotDefinition('Corrosive Dart', duration_seconds=18.0),
    DotHotDefinition('Corrosive Grenade (Op)', duration_seconds=24.0),  # unverified: BARAS name, not in this user's corpus
    DotHotDefinition('Corrosive Grenade (Sniper)', duration_seconds=24.0),  # unverified: BARAS name, not in this user's corpus
    DotHotDefinition('Creeping Terror', duration_seconds=18.0),
    DotHotDefinition('Discharge', duration_seconds=18.0),
    DotHotDefinition('Force Breach', duration_seconds=18.0),
    DotHotDefinition('Force Rend', duration_seconds=9.0),
    DotHotDefinition('Hemorrhaging Blast', duration_seconds=10.0),
    DotHotDefinition('Incendiary Grenade', duration_seconds=9.0),
    DotHotDefinition('Interrogation Probe', duration_seconds=18.0),
    DotHotDefinition('Marked (Physical)', duration_seconds=45.0),
    DotHotDefinition('Plasma Probe', duration_seconds=9.0),
    DotHotDefinition('Plasmatize', duration_seconds=30.0),
    DotHotDefinition('Sanguinary Shot', duration_seconds=10.0),
    DotHotDefinition('Scorch', duration_seconds=30.0),
    DotHotDefinition('Sever Force', duration_seconds=18.0),
    DotHotDefinition('Shock Charge', duration_seconds=18.0),
    DotHotDefinition('Shrap Bomb (Gunslinger)', duration_seconds=24.0),  # unverified: BARAS name, not in this user's corpus
    DotHotDefinition('Shrap Bomb (Ruffian)', duration_seconds=24.0),  # unverified: BARAS name, not in this user's corpus
    DotHotDefinition('Toxic Blast', duration_seconds=10.0),
    DotHotDefinition('Vital Shot', duration_seconds=24.0),
    DotHotDefinition('Weaken Mind', duration_seconds=18.0),
    DotHotDefinition('Weakening Blast', duration_seconds=10.0),
]

# -- HoTs (translated from BARAS's hots.toml) --------------------------------
HOTS: List[DotHotDefinition] = [
    DotHotDefinition('Force Armor', duration_seconds=30.0),  # unverified: BARAS name, not in this user's corpus
    DotHotDefinition('Kolto Probe', duration_seconds=21.0),
    DotHotDefinition('Kolto Shell', duration_seconds=180.0),
    DotHotDefinition('Rejuvenate', duration_seconds=15.0),
    DotHotDefinition('Resurgence', duration_seconds=15.0),
    DotHotDefinition('Slow-release Medpac', duration_seconds=21.0),
    DotHotDefinition('Static Barrier', duration_seconds=30.0),
    DotHotDefinition('Trauma Probe', duration_seconds=180.0),
]


class HotTracker:
    """Tracks YOUR HoTs per raid member, so you can see whose is about to
    drop without needing an overlay parked over the group frames.

    The category rules registered below answer "is one of my HoTs running",
    which is the wrong question for a healer -- a TimerRule has a label but
    no notion of *who* it landed on, so eight Kolto Probes on eight people
    collapse into one countdown. This keeps a (target, effect) grid instead.

    Refresh semantics: re-applying a HoT restarts its own countdown, which is
    what the game does, and is why this can't be a plain list of timers.
    """

    def __init__(self, hots: List[DotHotDefinition] = HOTS):
        self._by_name = {h.label.lower(): h for h in hots}
        # (target, effect_label) -> expires_at (wall clock)
        self._active: Dict[Tuple[str, str], float] = {}

    def _match(self, effect_name: Optional[str]) -> Optional[DotHotDefinition]:
        if not effect_name:
            return None
        low = effect_name.lower()
        for key, d in self._by_name.items():
            if key in low:
                return d
        return None

    def feed(self, event, local_player_name: Optional[str], now: Optional[float] = None) -> None:
        """Feed every parsed event. Only HoTs applied BY the local player are
        tracked -- another healer's Kolto Probe isn't yours to refresh."""
        if local_player_name is None or event.source != local_player_name:
            return
        definition = self._match(event.effect_name)
        if definition is None or not event.target:
            return
        now = now if now is not None else time.time()
        key = (event.target, definition.label)
        if event.is_effect_removed:
            self._active.pop(key, None)
        else:
            self._active[key] = now + definition.duration_seconds

    def expiring(self, within_seconds: Optional[float] = None,
                 now: Optional[float] = None) -> List[dict]:
        """Soonest-to-drop first. Expired entries are pruned as we go."""
        now = now if now is not None else time.time()
        rows = []
        for (target, label), expires in list(self._active.items()):
            remaining = expires - now
            if remaining <= 0:
                del self._active[(target, label)]
                continue
            if within_seconds is not None and remaining > within_seconds:
                continue
            total = self._by_name.get(label.lower())
            rows.append({
                "target": target, "effect": label,
                "remaining": remaining,
                "duration": total.duration_seconds if total else remaining,
            })
        rows.sort(key=lambda r: r["remaining"])
        return rows

    def reset(self) -> None:
        self._active.clear()


def register_dots_hots(
    timer_engine, dots: List[DotHotDefinition] = DOTS, hots: List[DotHotDefinition] = HOTS,
) -> None:
    """Adds one always-armed TimerRule per DoT/HoT to the given TimerEngine,
    scoped to the local player only (matches BARAS's own source="local_player"
    -- this tracks your own DoT/HoT uptime, not the whole raid's). Safe to
    call once at startup, same as register_defensive_cooldowns."""
    for d in dots:
        timer_engine.add_rule(TimerRule(
            keyword=d.label,
            label=d.label,
            duration_seconds=d.duration_seconds,
            voice_alert=False,
            category="dot",
            event_type="applied",
            only_local_player=True,
        ))
    for d in hots:
        timer_engine.add_rule(TimerRule(
            keyword=d.label,
            label=d.label,
            duration_seconds=d.duration_seconds,
            voice_alert=False,
            category="hot",
            event_type="applied",
            only_local_player=True,
        ))
