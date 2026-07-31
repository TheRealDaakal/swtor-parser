"""
taunt_tracker.py

Answers "did my taunt actually land" -- something none of the existing
panels show. SWTOR's combat log has no explicit resisted/immune event for
taunt: a failed taunt (out of range, or a boss/add with taunt immunity up)
just produces no follow-up line at all. So success has to be inferred from
absence vs presence of the debuff landing, not read off a single event.

Single-target Taunt is logged as a self-targeted AbilityActivate (the
caster doesn't pick a target for it -- the target bracket is "=", i.e. the
caster themselves) and is always literally named "Taunt" in that line,
confirmed across every tank AC in this user's own corpus (Vanguard/
Powertech, Guardian/Juggernaut, Shadow/Assassin all log it identically).
Whether it actually lands shows up a moment later as a separate ApplyEffect
line, on the real target, naming the effect "Taunt" too -- but under
whatever the ability's *internal* skill name happens to be (observed as
"Sonic Missile", "Mass Mind Control", etc. -- not the tooltip name), so
that line can't be matched by ability name at all, only by effect name.

AoE taunt's own cast line is therefore not reliably recognizable by name,
so it isn't matched at cast time -- only its result is: several distinct
targets receiving the "Taunt" effect at the same instant, with no matching
single-target cast pending. That can't fail silently the way single-target
can (no valid nearby target just means it does nothing detectable), so AoE
attempts are only ever recorded as hits.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

MATCH_WINDOW_SECONDS = 1.0   # cast-to-effect latency observed in real logs is <0.3s
AOE_COALESCE_SECONDS = 0.25  # targets hit by the same AoE cast share one timestamp
HISTORY_SIZE = 6


@dataclass
class TauntResult:
    at: float                        # wall-clock time this entry was resolved
    kind: str                        # "single" or "aoe"
    hit: bool
    targets: List[str] = field(default_factory=list)


class TauntTracker:
    """Feed every parsed event; read `.history` for the GUI panel."""

    def __init__(self):
        self.history: Deque[TauntResult] = deque(maxlen=HISTORY_SIZE)
        self._pending_since: Optional[float] = None

    def feed(self, event, local_player_name: Optional[str], now: Optional[float] = None) -> None:
        if local_player_name is None or event.source != local_player_name:
            return
        now = now if now is not None else time.time()

        if event.is_ability_activate and event.ability == "Taunt" and event.target == local_player_name:
            # A new cast supersedes any prior one still waiting -- the GCD
            # makes two genuinely in-flight at once impossible, so if one is
            # still pending here it already missed its window.
            self._resolve_pending(now, hit=False, targets=[])
            self._pending_since = now
            return

        if event.effect_type == "ApplyEffect" and event.effect_name == "Taunt" and event.target:
            if self._pending_since is not None and (now - self._pending_since) <= MATCH_WINDOW_SECONDS:
                self._resolve_pending(now, hit=True, targets=[event.target])
            else:
                self._record_aoe_hit(now, event.target)

    def tick(self, now: Optional[float] = None) -> None:
        """Call periodically so a taunt with no reply at all still resolves
        to a visible miss instead of sitting pending forever."""
        now = now if now is not None else time.time()
        if self._pending_since is not None and (now - self._pending_since) > MATCH_WINDOW_SECONDS:
            self._resolve_pending(now, hit=False, targets=[])

    def _resolve_pending(self, now: float, hit: bool, targets: List[str]) -> None:
        if self._pending_since is None:
            return
        self.history.appendleft(TauntResult(at=now, kind="single", hit=hit, targets=targets))
        self._pending_since = None

    def _record_aoe_hit(self, now: float, target: str) -> None:
        if self.history and self.history[0].kind == "aoe" and (now - self.history[0].at) < AOE_COALESCE_SECONDS:
            if target not in self.history[0].targets:
                self.history[0].targets.append(target)
            self.history[0].at = now
        else:
            self.history.appendleft(TauntResult(at=now, kind="aoe", hit=True, targets=[target]))
