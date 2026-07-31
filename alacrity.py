"""
alacrity.py

Personal alacrity-buff uptime tracking.

SWTOR's combat log never exposes a character's actual Alacrity Rating/%
(no combat log line reports raw stats at all -- only events), so unlike
crit/accuracy this can't be read directly. What CAN be read is uptime on
the burst-alacrity COOLDOWN ABILITIES classes actually cast, which is the
practical thing raiders check anyway ("did I keep my alacrity window up").

All three entries and their durations were derived empirically from this
user's own real log corpus (no BARAS source covers these -- dcds.toml's
`is_affected_by_alacrity` field is about OTHER cooldowns scaling with your
alacrity stat, not an alacrity-granting buff itself):

  - "Mental Alacrity" + "Metaphysical Alacrity" apply together from the same
    self-cast (also grants "Unshakable" CC immunity) -- this matches
    Sorcerer/Sage's burst-alacrity cooldown. Buff duration clusters at both
    15s and 20s across the corpus; 15s is the dominant mode (283 samples vs
    86 near 20s) and is used here -- the 20s cases are almost certainly a
    utility-point build choice extending it, which this engine has no way
    to detect per-caster (same category of limitation as cooldowns.py not
    modeling `is_affected_by_alacrity`).
  - "Alacrity Boost" is a separate self-buff (id 4626766404517888), 30s
    duration (154+50 samples at ~30.0-30.1s, clearly dominant). Its source
    class/spec isn't confirmed -- generic enough it may be an on-use relic
    or adrenal rather than a class ability; included anyway since the name
    and duration are solidly verified from real data regardless of source.

No cooldown/reuse-timer half is registered (unlike cooldowns.py's DCDs):
the real reuse timer for these isn't clearly and consistently derivable
from cast-to-cast gaps in the corpus (raid usage timing varies too much to
separate "still on cooldown" from "held it for a better window"), so
showing a guessed cooldown would be worse than not showing one. Reuses
cooldowns.py's category="cooldown" so it appears in the existing Cooldowns
panel without needing new GUI plumbing for three rows.
"""

from dataclasses import dataclass
from typing import List

from timers import TimerRule


@dataclass
class AlacrityBuffDefinition:
    label: str
    duration_seconds: float


ALACRITY_BUFFS: List[AlacrityBuffDefinition] = [
    AlacrityBuffDefinition("Mental Alacrity", duration_seconds=15.0),
    AlacrityBuffDefinition("Metaphysical Alacrity", duration_seconds=15.0),
    AlacrityBuffDefinition("Alacrity Boost", duration_seconds=30.0),
]


def register_alacrity_buffs(timer_engine, definitions: List[AlacrityBuffDefinition] = ALACRITY_BUFFS) -> None:
    """Adds one buff-uptime TimerRule per definition. Safe to call once at
    startup -- not boss-scoped, matches cooldowns.py/dots_hots.py."""
    for d in definitions:
        timer_engine.add_rule(TimerRule(
            keyword=d.label,
            label=d.label,
            duration_seconds=d.duration_seconds,
            voice_alert=False,
            category="cooldown",
            event_type="applied",
            only_local_player=True,
        ))
