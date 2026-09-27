"""
resource_tracker.py

Net resource flow (energy/rage/ammo/heat/Force -- whatever the local
player's class pool is called) since the current pull started. SWTOR's
combat log only ever emits Spend/Restore as DELTAS -- there is no absolute
current/max value anywhere in the log (see
CombatEvent.is_resource_spend/is_resource_restore) -- so this deliberately
does not attempt to reconstruct or guess an absolute amount. What it CAN
answer honestly from only what the log contains: are you spending faster
than you're regenerating, right now, this pull.

Resets each pull (main.py calls .reset() alongside hot_tracker/aggro_tracker
on rollover) since "since combat started" is the only window the running
net is a meaningful number for -- carrying it across a wipe+re-pull would
mix two unrelated windows into one misleading total.
"""
import time
from collections import deque
from typing import Deque, Optional, Tuple

# How far back "recent spend rate" looks -- long enough to smooth over
# single-GCD noise, short enough to react to a rotation change within a few
# seconds rather than dragging in the whole pull's average.
RATE_WINDOW_SECONDS = 10.0


class ResourceTracker:
    def __init__(self):
        self.resource_type: Optional[str] = None
        self.spent_total: float = 0.0
        self.restored_total: float = 0.0
        self._spends: Deque[Tuple[float, float]] = deque()  # (wall_time, amount)

    def feed(self, event, local_player_name: Optional[str], now: Optional[float] = None) -> None:
        if local_player_name is None or event.source != local_player_name:
            return
        if not (event.is_resource_spend or event.is_resource_restore):
            return
        now = now if now is not None else time.time()
        # A stance/form swap mid-session can change which pool is active --
        # always reflect whatever the MOST RECENT event says, not whichever
        # type was first seen.
        self.resource_type = event.resource_type
        if event.is_resource_spend:
            self.spent_total += event.resource_amount
            self._spends.append((now, event.resource_amount))
            self._trim(now)
        else:
            self.restored_total += event.resource_amount

    def _trim(self, now: float) -> None:
        cutoff = now - RATE_WINDOW_SECONDS
        while self._spends and self._spends[0][0] < cutoff:
            self._spends.popleft()

    def spend_rate(self, now: Optional[float] = None) -> float:
        """Resource spent per second, averaged over however much of the
        trailing RATE_WINDOW_SECONDS window actually has spends in it --
        0.0 once nothing's been spent recently."""
        now = now if now is not None else time.time()
        self._trim(now)
        if not self._spends:
            return 0.0
        span = max(now - self._spends[0][0], 1.0)
        return sum(amt for _ts, amt in self._spends) / span

    def snapshot(self, now: Optional[float] = None) -> dict:
        return {
            "resource_type": self.resource_type,
            "net": round(self.restored_total - self.spent_total, 1),
            "spent_total": round(self.spent_total, 1),
            "restored_total": round(self.restored_total, 1),
            "spend_rate_per_sec": round(self.spend_rate(now), 1),
        }

    def reset(self) -> None:
        self.resource_type = None
        self.spent_total = 0.0
        self.restored_total = 0.0
        self._spends.clear()
