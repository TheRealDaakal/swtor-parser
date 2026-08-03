"""
aggro_tracker.py

Flags a DPS/healer approaching the boss's current target's threat -- the
"you're about to pull, dump now" warning raiding groups actually want.
No role detection needed: boss_intelligence.BossEncounterState.boss_target
already tracks who the boss is CURRENTLY attacking (updated from the
log's own events, source=boss/target=player), which is the one true
"who has aggro right now" signal regardless of whether that's the
intended tank, an off-tank, or (on a mispull) nobody in particular.

Threat itself comes straight from the log's own ModifyThreat events (see
log_parser.py/stats.py) -- already baked in with whatever role
multipliers/taunt-copy math the game actually applied, so this never
needs to reimplement that itself.
"""

from typing import Dict, Optional

# 90% of the aggro-holder's threat triggers the warning; must drop back
# under 70% before the SAME player can re-fire -- a dead zone between the
# two, not one shared threshold, so a player sitting right at ~90% doesn't
# fire the alert again on every single tick while they hover near it.
APPROACH_THRESHOLD = 0.9
RESET_THRESHOLD = 0.7


class AggroTracker:
    def __init__(self):
        self._fired: set = set()  # names currently past APPROACH_THRESHOLD

    def check(self, players: Dict[str, object], boss_target: Optional[str],
              duration: float, timer_engine) -> None:
        """players: Encounter.players (name -> PlayerStats). Call once per
        refresh tick -- see gui.py's OverlayManager._refresh()."""
        if not boss_target or duration <= 0:
            return
        holder = players.get(boss_target)
        if holder is None:
            return
        holder_tps = holder.tps(duration)
        if holder_tps <= 0:
            return
        for name, p in players.items():
            if name == boss_target or not getattr(p, "is_player", False):
                continue
            ratio = p.tps(duration) / holder_tps
            if ratio >= APPROACH_THRESHOLD:
                if name not in self._fired:
                    self._fired.add(name)
                    timer_engine.start_timer(
                        f"{name} pulling aggro!", 4.0, voice_alert=True,
                        category="aggro", is_alert=True,
                    )
            elif ratio < RESET_THRESHOLD:
                self._fired.discard(name)

    def reset(self) -> None:
        """Called on a new pull -- a threat lead from the last fight must
        not suppress a real warning on this one."""
        self._fired.clear()
