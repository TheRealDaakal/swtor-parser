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
`cooldown_ready_secs`).

`is_affected_by_alacrity` (duration scaling with the player's alacrity
stat) IS translated, as of the alacrity-scaling feature -- see
timers.apply_alacrity() for the formula and why. SWTOR's combat log never
reports a character's actual Alacrity Rating/% directly (no line reports
raw stats, only events -- see alacrity.py's own docstring for the same
finding), so this can't be read automatically; the app has a manual
per-character "Alacrity %" setting (Overlays tab) instead, and every
DOTS/HOTS entry's is_affected_by_alacrity flag below was re-verified
against BARAS's CURRENT source (core/definitions/effects/dots.toml and
hots.toml -- fetched fresh, not the original translation pass, since
BARAS's repo has since migrated from a flat dots.toml/hots.toml to this
effects/ subdirectory) rather than guessed. Still not implemented for
defensive cooldowns (cooldowns.py) -- BARAS's dcds.toml has the same flag
for a different reason (activation-time reduction, not tick-count-based
duration), a separate feature.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from timers import TimerRule, apply_alacrity

_CHARGES_RE = re.compile(r"(\d+)\s+charges?", re.IGNORECASE)


def _extract_remaining_charges(values: List[str]) -> Optional[int]:
    """Pulls the charge count out of a ModifyCharges line's value group,
    e.g. values=["3 charges {836045448953667}"] -> 3."""
    for v in values:
        m = _CHARGES_RE.search(v)
        if m:
            return int(m.group(1))
    return None


@dataclass
class DotHotDefinition:
    label: str  # real ability/effect name, matched as the trigger keyword
    duration_seconds: float
    # "source" (default): track when the local player CAST it -- right for a
    # healer's own rotation (Kolto Probe, Resurgence, ...), where "is my HoT
    # up on someone" is the useful question. "target": track when it's
    # CURRENTLY ON the local player, regardless of who cast it -- right for
    # a shield/utility buff a teammate is just as likely to put on you as
    # you are to put on them (Kolto Shell, Trauma Probe) -- confirmed from a
    # real log where "kolto shells aren't showing" turned out to be exactly
    # this: a teammate's Kolto Shell landing on the local player never
    # matched a source=local_player rule at all.
    track_by: str = "source"
    # Charge-based shields (Kolto Shell, Trauma Probe) disappear once their
    # charges run out even if the nominal duration hasn't elapsed -- each
    # absorbed hit logs its own ModifyCharges line (see log_parser.py's
    # is_charges_modified) with the charge count remaining. None for plain
    # time-based HoTs, which have no such mechanic. 7 confirmed as this
    # user's actual max (not the base 6 -- their utility/gear setup grants
    # the extra charge) by scanning their own log corpus for the highest
    # charge count either ability ever logged.
    max_charges: Optional[int] = None
    # Whether this effect's on-target duration shrinks with the caster's
    # Alacrity -- re-verified per-entry against BARAS's current source (see
    # module docstring). Almost always False alongside max_charges set
    # (charge-based shields drop when consumed, not on a tick-scaled
    # clock) and True for most plain time-based dots/hots -- but NOT all:
    # a handful of "grenade"/"blast" tech dots (Plasma Probe, Incendiary
    # Grenade, Weakening Blast, Hemorrhaging Blast, Toxic Blast, Sanguinary
    # Shot) are real exceptions confirmed straight from BARAS's table, not
    # a guessed pattern.
    is_affected_by_alacrity: bool = False


# -- DoTs (translated from BARAS's dots.toml) --------------------------------
# is_affected_by_alacrity re-verified 2026-08-03 against BARAS's CURRENT
# source (core/definitions/effects/dots.toml, fetched fresh -- their repo
# has since moved off the original flat dots.toml this file's names/
# durations came from). Six real exceptions confirmed there, not guessed:
# Plasma Probe, Incendiary Grenade, Weakening Blast, Hemorrhaging Blast,
# Toxic Blast, Sanguinary Shot are NOT alacrity-affected; every other dot
# below is.
DOTS: List[DotHotDefinition] = [
    DotHotDefinition('Affliction', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Bleeding', duration_seconds=15.0, is_affected_by_alacrity=True),
    DotHotDefinition('Bleeding (Deadly Saber)', duration_seconds=6.0, is_affected_by_alacrity=True),
    DotHotDefinition('Bleeding (Draining Scream)', duration_seconds=6.0, is_affected_by_alacrity=True),
    DotHotDefinition('Bleeding (Eviscerate)', duration_seconds=6.0, is_affected_by_alacrity=True),
    DotHotDefinition('Bleeding (Rupture)', duration_seconds=9.0, is_affected_by_alacrity=True),
    DotHotDefinition('Bleeding (Shatter)', duration_seconds=12.0, is_affected_by_alacrity=True),
    DotHotDefinition('Burning (Burning Blade)', duration_seconds=6.0, is_affected_by_alacrity=True),
    DotHotDefinition('Burning (Burning Purpose)', duration_seconds=6.0, is_affected_by_alacrity=True),
    DotHotDefinition('Burning (Incendiary Missile)', duration_seconds=15.0, is_affected_by_alacrity=True),
    DotHotDefinition('Burning (Incendiary Round)', duration_seconds=15.0, is_affected_by_alacrity=True),
    DotHotDefinition('Burning (Plasma Brand)', duration_seconds=12.0, is_affected_by_alacrity=True),
    DotHotDefinition('Burning (Priming Shot)', duration_seconds=15.0, is_affected_by_alacrity=True),
    DotHotDefinition('Burning Purpose', duration_seconds=6.0, is_affected_by_alacrity=True),
    DotHotDefinition('Corrosive Dart', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Corrosive Grenade (Op)', duration_seconds=24.0, is_affected_by_alacrity=True),  # unverified: BARAS name, not in this user's corpus (alacrity flag IS confirmed from BARAS's table)
    DotHotDefinition('Corrosive Grenade (Sniper)', duration_seconds=24.0, is_affected_by_alacrity=True),  # unverified: BARAS name, not in this user's corpus (alacrity flag IS confirmed from BARAS's table)
    DotHotDefinition('Creeping Terror', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Discharge', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Force Breach', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Force Rend', duration_seconds=9.0, is_affected_by_alacrity=True),
    DotHotDefinition('Hemorrhaging Blast', duration_seconds=10.0, is_affected_by_alacrity=False),
    DotHotDefinition('Incendiary Grenade', duration_seconds=9.0, is_affected_by_alacrity=False),
    DotHotDefinition('Interrogation Probe', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Marked (Physical)', duration_seconds=45.0, is_affected_by_alacrity=True),
    DotHotDefinition('Plasma Probe', duration_seconds=9.0, is_affected_by_alacrity=False),
    DotHotDefinition('Plasmatize', duration_seconds=30.0, is_affected_by_alacrity=True),
    DotHotDefinition('Sanguinary Shot', duration_seconds=10.0, is_affected_by_alacrity=False),
    DotHotDefinition('Scorch', duration_seconds=30.0, is_affected_by_alacrity=True),
    DotHotDefinition('Sever Force', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Shock Charge', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Shrap Bomb (Gunslinger)', duration_seconds=24.0, is_affected_by_alacrity=True),  # unverified: BARAS name, not in this user's corpus (alacrity flag IS confirmed from BARAS's table)
    DotHotDefinition('Shrap Bomb (Ruffian)', duration_seconds=24.0, is_affected_by_alacrity=True),  # unverified: BARAS name, not in this user's corpus (alacrity flag IS confirmed from BARAS's table)
    DotHotDefinition('Toxic Blast', duration_seconds=10.0, is_affected_by_alacrity=False),
    DotHotDefinition('Vital Shot', duration_seconds=24.0, is_affected_by_alacrity=True),
    DotHotDefinition('Weaken Mind', duration_seconds=18.0, is_affected_by_alacrity=True),
    DotHotDefinition('Weakening Blast', duration_seconds=10.0, is_affected_by_alacrity=False),
]

# -- HoTs (translated from BARAS's hots.toml) --------------------------------
# Same re-verification as DOTS above, against BARAS's current hots.toml.
# The charge-based shields (Kolto Shell, Trauma Probe) and the two plain
# absorb shields (Static Barrier, Force Armor) are confirmed NOT
# alacrity-affected -- consistent with is_affected_by_alacrity being
# fundamentally about tick-based effects, not absorb-until-consumed ones.
HOTS: List[DotHotDefinition] = [
    DotHotDefinition('Force Armor', duration_seconds=30.0, is_affected_by_alacrity=False),  # unverified: BARAS name, not in this user's corpus (alacrity flag IS confirmed from BARAS's table)
    DotHotDefinition('Kolto Probe', duration_seconds=21.0, is_affected_by_alacrity=True),
    # Mercenary/Commando's Kolto Missile channel -- logs as its own ability
    # name ("Kolto Pods"), separate from "Kolto Missile" itself, ticking ~3
    # times about 0.9s apart (real spacing measured from this user's own
    # log). No RemoveEffect ever fires for it -- turns out that's fine, a
    # TimerRule's countdown runs off its own fixed duration_seconds and
    # doesn't need one; each new tick just re-arms the same short window.
    # Not in BARAS's hots.toml at all (they track it differently) -- no
    # source data to verify an alacrity flag against, so left at the
    # default False rather than guessed.
    DotHotDefinition('Kolto Pods', duration_seconds=2.5),
    DotHotDefinition('Kolto Shell', duration_seconds=180.0, track_by="target", max_charges=7,
                      is_affected_by_alacrity=False),
    DotHotDefinition('Rejuvenate', duration_seconds=15.0, is_affected_by_alacrity=True),
    DotHotDefinition('Resurgence', duration_seconds=15.0, is_affected_by_alacrity=True),
    DotHotDefinition('Slow-release Medpac', duration_seconds=21.0, is_affected_by_alacrity=True),
    DotHotDefinition('Static Barrier', duration_seconds=30.0, is_affected_by_alacrity=False),
    DotHotDefinition('Trauma Probe', duration_seconds=180.0, track_by="target", max_charges=7,
                      is_affected_by_alacrity=False),
]


@dataclass
class _ActiveHot:
    expires_at: float  # wall clock
    duration_seconds: float  # the (possibly alacrity-scaled) duration actually used to arm this
    charges_lost: int = 0


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
        self._active: Dict[Tuple[str, str], _ActiveHot] = {}

    def _match(self, effect_name: Optional[str], ability: Optional[str] = None):
        """Checks effect_name first, then falls back to ability. Reported
        live ("no hots showing now"): SWTOR logs plenty of real heal ticks
        with effect_name genericized to plain "Heal" -- the ability's real
        name only shows up in the `ability` field instead (confirmed
        against a real log: EVERY one of this user's own Kolto Pods ticks,
        154 of them, had effect_name=="Heal", never effect_name=="Kolto
        Pods") -- so effect_name-only matching silently never tracked it at
        all, despite it already being a defined HOTS entry.

        Returns (definition, matched_by) where matched_by is "effect" for a
        real application line and "ability" for a fallback match -- the
        caller needs that distinction, see feed()."""
        for source, text in (("effect", effect_name), ("ability", ability)):
            if not text:
                continue
            low = text.lower()
            for key, d in self._by_name.items():
                if key in low:
                    return d, source
        return None, None

    def feed(self, event, local_player_name: Optional[str], now: Optional[float] = None,
              alacrity_pct: float = 0.0) -> None:
        """Feed every parsed event. Only HoTs applied BY the local player are
        tracked -- another healer's Kolto Probe isn't yours to refresh."""
        if event.is_death and event.target:
            # A dead target can't have anything still ticking on it, no
            # matter what our own countdown thinks -- SWTOR doesn't
            # reliably log a RemoveEffect line for every dot/hot at the
            # moment of death (reported live: a dot kept "ticking" on an
            # NPC well after it died), so death itself has to be a second,
            # independent clear signal alongside the explicit removal path
            # below. Checked before the local_player_name gate below since
            # a target's death is true regardless of who this event's
            # source is.
            stale = [key for key in self._active if key[0] == event.target]
            for key in stale:
                del self._active[key]
            return
        if local_player_name is None or event.source != local_player_name:
            return
        if event.is_ability_activate:
            # The cast line itself, not the effect landing -- carries the
            # same ability name _match() now also checks via the `ability`
            # fallback above, but its target defaults to "=" (the CASTER),
            # not whoever the effect actually lands on (same issue
            # timers.py's TimerRule path already guards against). Letting
            # it through would create a bogus entry keyed to the caster.
            return
        definition, matched_by = self._match(event.effect_name, event.ability)
        if definition is None or not event.target:
            return
        now = now if now is not None else time.time()
        key = (event.target, definition.label)
        if event.is_effect_removed:
            self._active.pop(key, None)
            return
        if definition.max_charges and event.is_charges_modified:
            # A charge just got consumed absorbing a hit -- not a fresh
            # cast, so don't touch expires_at. Read the ABSOLUTE remaining
            # count off the line rather than just decrementing by one, so a
            # missed/duplicated event can't drift the tracked count out of
            # sync with what the game actually shows.
            active = self._active.get(key)
            if active is None:
                return  # a charge-loss line with no known active instance to update
            remaining = _extract_remaining_charges(event.values)
            if remaining is not None:
                active.charges_lost = max(0, definition.max_charges - remaining)
            return
        if matched_by == "ability" and key in self._active:
            # A HEAL TICK from an effect already being tracked, not a fresh
            # cast. Every real HoT logs BOTH: a proper "ApplyEffect: <name>"
            # when cast, and separate "ApplyEffect: Heal" lines carrying only
            # the ability name as it ticks (measured on a real log -- Trauma
            # Probe: 106 applies vs 1,133 heal ticks; Slow-release Medpac:
            # 187 vs 1,797). Treating a tick as a re-application re-armed the
            # full duration on every tick, so a charge shield's countdown sat
            # permanently at ~179s of 180 and never visibly drained -- and
            # its charges_lost reset each time too. Kolto Pods is the one
            # effect with NO apply line at all (4,434 ticks, 0 applies),
            # which is exactly why the ability fallback exists; a tick still
            # CREATES an entry below when there isn't one.
            return
        duration = (
            apply_alacrity(definition.duration_seconds, alacrity_pct)
            if definition.is_affected_by_alacrity else definition.duration_seconds
        )
        self._active[key] = _ActiveHot(expires_at=now + duration, duration_seconds=duration)

    def expiring(self, within_seconds: Optional[float] = None,
                 now: Optional[float] = None) -> List[dict]:
        """Soonest-to-drop first. Expired entries are pruned as we go.

        Pruning is keyed ONLY to the real wall-clock timer (active.expires_at
        -- the shield's actual nominal duration), never to the charge-based
        display adjustment below. Reported live: "hots fall off and don't
        come back on" -- traced to a real log: Trauma Probe (180s duration,
        7 max_charges, same mechanic as Kolto Shell) had all its charges
        consumed within the first 20s of a fight. Losing every charge costs
        a FULL duration's worth of penalty ((duration/max_charges)*max_charges
        == duration), which is always >= whatever time_remaining actually
        is -- so once THIS SAME VALUE also decided pruning, the entry got
        deleted the moment the last charge dropped, tens of seconds (or, per
        the real log, well over a minute) before the shield actually broke
        (confirmed: ModifyCharges lines kept arriving long after the computed
        "remaining" had already gone negative). Once pruned, the entry was
        gone from _active entirely, so every further ModifyCharges line for
        it silently no-op'd (feed()'s charge branch returns early when
        there's no active entry to update) -- it just sat invisible, still
        genuinely up in-game, until the real RemoveEffect/recast eventually
        arrived, reading as "fell off and never came back." SWTOR itself
        already tells us exactly when a shield breaks from charges hitting
        zero (an immediate RemoveEffect, handled in feed()) -- there's no
        need to approximate that here at all, let alone let the approximation
        delete the entry early."""
        now = now if now is not None else time.time()
        rows = []
        for (target, label), active in list(self._active.items()):
            total = self._by_name.get(label.lower())
            # active.duration_seconds (what was actually used to arm this
            # instance, possibly alacrity-scaled) not total.duration_seconds
            # (the definition's raw base value) -- otherwise the progress
            # bar's denominator wouldn't match what its own countdown is
            # actually counting down from.
            duration = active.duration_seconds
            time_remaining = active.expires_at - now
            if time_remaining <= 0:
                del self._active[(target, label)]
                continue
            remaining = time_remaining
            if total and total.max_charges and active.charges_lost:
                # Visual-only from here down: a shield down to its last
                # charge should LOOK urgent even while its nominal duration
                # has plenty left, without that estimate ever being allowed
                # to prune the entry above -- floored just above zero
                # instead of going negative.
                charge_penalty = (duration / total.max_charges) * active.charges_lost
                remaining = max(0.001, remaining - charge_penalty)
            if within_seconds is not None and remaining > within_seconds:
                continue
            rows.append({
                "target": target, "effect": label,
                "remaining": remaining,
                "duration": duration,
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
            alacrity_affected=d.is_affected_by_alacrity,
        ))
    for d in hots:
        timer_engine.add_rule(TimerRule(
            keyword=d.label,
            label=d.label,
            duration_seconds=d.duration_seconds,
            voice_alert=False,
            category="hot",
            event_type="applied",
            only_local_player=(d.track_by == "source"),
            only_target_is_local_player=(d.track_by == "target"),
            alacrity_affected=d.is_affected_by_alacrity,
        ))
