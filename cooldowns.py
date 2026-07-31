"""
cooldowns.py

Personal defensive-cooldown tracking, translated from BARAS's own
core/definitions/effects/dcds.toml. That file's 31 entries are 15 defensive
abilities x 2 (a cooldown entry and a buff entry each), plus a combat-rez
cooldown -- so the 15 CooldownDefinitions below are the complete set, not a
subset. Every name and duration here was re-verified line-by-line against
that source file. Each entry becomes two TimerRules:

  - a COOLDOWN timer: how long until the ability is available again
  - a BUFF timer: how long the defensive is actively up right now

Both are always-armed (no boss/phase scoping -- these matter in trash
fights too, not just boss pulls) and tagged category="cooldown" so the
GUI can show them in their own panel, separate from boss mechanics.

One real behavioral detail preserved from the source data: most of these
start their cooldown the instant you CAST the ability (trigger =
effect_applied on the same id as the buff). Two of them -- Adrenaline Rush
and Kolto Overload -- instead start the cooldown when the buff WEARS OFF
(trigger = effect_removed), matching BARAS's own definitions. The buff
timer is always keyed to "applied" specifically, so it doesn't incorrectly
re-arm itself off the same ability name appearing again in the log line
that ends it.

Not translated from the source schema: `cooldown_ready_secs` (a distinct
"ready" glow state for N seconds after the cooldown ends, before this
just disappears) and `is_affected_by_alacrity` (duration scaling with the
player's own alacrity stat) -- both are real BARAS features, just not
implemented here yet. See dots_hots.py for the DoT/HoT counterpart,
translated from the sibling dots.toml/hots.toml.
"""

from dataclasses import dataclass
from typing import List

from timers import TimerRule


@dataclass
class CooldownDefinition:
    label: str                    # real ability name, matched as the trigger keyword
    cooldown_seconds: float
    buff_seconds: float
    cooldown_on_buff_removed: bool = False  # False = cooldown starts at cast (effect_applied)


# Real ability names and durations, translated from BARAS's own
# effects.toml -- these are genuine class defensive cooldowns, not guesses.
DEFENSIVE_COOLDOWNS: List[CooldownDefinition] = [
    CooldownDefinition("Adrenaline Rush", cooldown_seconds=180.0, buff_seconds=60.0, cooldown_on_buff_removed=True),
    CooldownDefinition("Battle Readiness", cooldown_seconds=120.0, buff_seconds=15.0),
    CooldownDefinition("Deflection", cooldown_seconds=120.0, buff_seconds=12.0),
    CooldownDefinition("Energy Shield", cooldown_seconds=120.0, buff_seconds=12.0),
    CooldownDefinition("Enraged Defense", cooldown_seconds=120.0, buff_seconds=15.0),
    CooldownDefinition("Focused Defense", cooldown_seconds=120.0, buff_seconds=15.0),
    CooldownDefinition("Force Shroud", cooldown_seconds=60.0, buff_seconds=3.0),
    CooldownDefinition("Invincible", cooldown_seconds=180.0, buff_seconds=10.0),
    CooldownDefinition("Kolto Overload", cooldown_seconds=180.0, buff_seconds=60.0, cooldown_on_buff_removed=True),
    CooldownDefinition("Overcharge Saber", cooldown_seconds=120.0, buff_seconds=15.0),
    CooldownDefinition("Power Yield", cooldown_seconds=60.0, buff_seconds=10.0),
    CooldownDefinition("Reactive Shield", cooldown_seconds=120.0, buff_seconds=12.0),
    CooldownDefinition("Resilience", cooldown_seconds=60.0, buff_seconds=3.0),
    CooldownDefinition("Saber Ward", cooldown_seconds=90.0, buff_seconds=12.0),
    CooldownDefinition("Warding Call", cooldown_seconds=180.0, buff_seconds=10.0),
]

# In-combat revive ("rez") cooldown, also from BARAS's dcds.toml. Structurally
# different from every entry above, which is why it needs its own list:
#   - It is RAID-WIDE, not personal (BARAS uses source = "any_player") -- you
#     want to see that *somebody* used a combat rez, so it is deliberately NOT
#     scoped to the local player.
#   - It is cooldown-only, with no "actively up" buff phase.
#   - It is detected via the rez-sickness debuff applied to the revived player,
#     which logs under a different name per class. All four names below were
#     resolved from this user's own real log corpus (not guesses), covering the
#     six ability ids BARAS lists.
# Not translated: BARAS's `persist_past_death` (the timer surviving your own
# death) and `display_source` (naming who cast it) -- no equivalent here.
REVIVE_COOLDOWN_SECONDS = 300.0
REVIVE_DEBUFF_KEYWORDS: List[str] = [
    "Tenuous Force",
    "Lucky Break",
    "Communication Breakdown",
    "Technical Difficulties",
]


def register_defensive_cooldowns(timer_engine, definitions: List[CooldownDefinition] = DEFENSIVE_COOLDOWNS) -> None:
    """Adds one cooldown rule + one buff rule per definition to the given
    TimerEngine. Safe to call once at startup -- these aren't boss-scoped,
    so they don't need to be re-registered per pull."""
    for d in definitions:
        timer_engine.add_rule(TimerRule(
            keyword=d.label,
            label=f"{d.label} (cooldown)",
            duration_seconds=d.cooldown_seconds,
            voice_alert=False,  # cooldown ending isn't urgent enough to speak
            category="cooldown",
            event_type="removed" if d.cooldown_on_buff_removed else "applied",
            # These are personal defensives -- a teammate casting the same-
            # named ability (e.g. two Guardians both with Saber Ward)
            # shouldn't re-trigger your own cooldown/buff display.
            only_local_player=True,
        ))
        # Buff timer is always keyed to "applied" specifically -- without
        # this, the same ability name appearing in the log's RemoveEffect
        # line (when the buff ends) would incorrectly re-arm a fresh buff
        # countdown at the exact moment it should be disappearing.
        timer_engine.add_rule(TimerRule(
            keyword=d.label,
            label=d.label,
            duration_seconds=d.buff_seconds,
            voice_alert=False,
            category="cooldown",
            event_type="applied",
            only_local_player=True,
        ))

    # Combat rez: one rule per class-specific debuff name, all reporting under
    # the same label so they read as one shared raid cooldown in the GUI
    # regardless of which class actually cast it. Not local-player-scoped --
    # see REVIVE_DEBUFF_KEYWORDS above.
    for keyword in REVIVE_DEBUFF_KEYWORDS:
        timer_engine.add_rule(TimerRule(
            keyword=keyword,
            label="Combat Rez (cooldown)",
            duration_seconds=REVIVE_COOLDOWN_SECONDS,
            voice_alert=False,
            category="cooldown",
            event_type="applied",
            only_local_player=False,
        ))
