"""Core combat statistics models."""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from log_parser import CombatEvent

ENCOUNTER_GAP_SECONDS = 8.0        # inactivity fallback for ending a fight
TRAILING_CAPTURE_SECONDS = 4.0     # StarParse-style post-ExitCombat grace window
BURST_WINDOW_SECONDS = 10.0        # ORBS/StarParse's own "burst" window width

# Treat a fresh EnterCombat as the start of a new pull.
#
# Needed because ExitCombat alone is not a reliable end-of-pull signal: SWTOR
# logs it far less often than EnterCombat (one real raid log here: 21
# EnterCombat vs 8 ExitCombat), and during progression the adds never stop
# swinging, so the ENCOUNTER_GAP_SECONDS inactivity fallback never fires
# either. Symptom when that happens: five separate Styrak pulls fused into
# one 36-minute "encounter" credited with 92 deaths for a single player.
#
# EnterCombat is logged for the LOCAL PLAYER ONLY (verified: all 21 in that
# file share one source), so it is a clean per-pull signal rather than
# something that fires once per raid member.
#
# The delay guard exists because combat can drop and re-establish within a
# single fight -- those re-entries show up 9-16s apart, while genuine
# re-pulls in the same data are 39s+ apart.
#
# Lives here (not in analysis/corpus.py, where it was first written) because
# the live tracker and the offline replay MUST split pulls the same way. They
# didn't: the offline path adopted this signal, the live path never did, and
# the same log split into 93 pulls offline vs 75 live -- a 19% divergence
# that also produced near-duplicate History rows, since the dedup key is
# (log_path, start_line, end_line) and the boundaries genuinely differed.
NEW_PULL_MIN_GAP_SECONDS = 30.0

# Below this, an encounter is a sliver (looting, a stray DoT tick) rather
# than a pull. Used when explicitly closing out the in-flight encounter --
# see StatsTracker.flush_current().
MIN_ENCOUNTER_SECONDS = 5.0

# Duration alone was never enough to call something a pull. Between attempts
# a raid stands around healing each other up and re-applying buffs, which is
# a continuous stream of player-to-player events: `players` fills up, the 8s
# inactivity gap never elapses, and the segment sails past 5s. Measured on
# the real 230-file archive, 1,789 of 3,433 indexed encounters (52%) had
# ZERO damage in them -- 986 of those had healing, i.e. they were exactly
# this between-pulls topping-off. 233 were even boss-tagged, which is what
# made kill/wipe stats nonsense: Styrak showed 21 "wipes" of which 15 had no
# deaths at all, because they weren't fights.
#
# The test is damage in EITHER direction, not healing: a raid that gets
# flattened without landing a hit still took damage, while a raid buffing up
# between pulls does neither. See Encounter.is_real_fight().

# Bumped whenever a parser/aggregation change alters what a STORED history
# row means, so rows written by an older build can be spotted and rebuilt
# from their own log rather than sitting there quietly wrong. See
# history_migration.py.
#
# 1: the first version to carry the field at all. Everything written before
#    it counted "Deathmark" (and every other ability with "death" in its
#    name) as a player death, and kept between-pull segments -- the raid
#    healing up with no damage anywhere -- as if they were pulls.
HISTORY_DATA_VERSION = 1

# What share of the group has to die before a failed pull counts as the
# raid being DEFEATED rather than the group disengaging and re-pulling.
# See Encounter.raid_was_defeated() for the measurement behind 0.5.
RAID_DEFEATED_FRACTION = 0.5

# Caps StatsTracker.history in memory, not just on disk. storage.py's
# save_history()/append_history_entry() already trim the FILE to this many
# pulls, but the in-memory list itself was never capped -- a long-running
# session, or one big "Import an old session log" click (which can add
# hundreds of pulls in a loop, see web_server.py's /api/import/session), grew
# it forever, each entry carrying its own per-player bucketed event lists.
# storage.py imports this constant rather than defining its own so the two
# caps can't drift apart.
HISTORY_LIMIT = 200


def _append_bucketed(events: List[Tuple[float, float]], now: float, amount: float) -> None:
    """Adds `amount` to events, merging into the current whole-second bucket
    if the last entry is still in it, else starting a new one. Bucket
    boundary is the floor of `now`, which _max_window_sum reads as that
    bucket's timestamp -- comparing against a floored start time only ever
    makes a sliding window look up to ~1s NARROWER than it really was, never
    wider, so burst_dps()/burst_hps() can undercount by a bounded, small
    amount but never overcount."""
    bucket = float(int(now))
    if events and events[-1][0] == bucket:
        ts, total = events[-1]
        events[-1] = (ts, total + amount)
    else:
        events.append((bucket, amount))


def _max_window_sum(events: List[Tuple[float, float]], window_seconds: float) -> float:
    """Max sum of `amount` over any window_seconds-wide sliding window across
    `events` (assumed already in chronological order, which every caller here
    guarantees by only ever appending as the log streams in). Two-pointer
    scan, O(n) -- fine to recompute on every GUI refresh tick even for a
    several-thousand-event pull; no need for incremental caching."""
    if not events:
        return 0.0
    best = 0.0
    running = 0.0
    left = 0
    for right in range(len(events)):
        running += events[right][1]
        while events[right][0] - events[left][0] > window_seconds:
            running -= events[left][1]
            left += 1
        best = max(best, running)
    return best


@dataclass
class PlayerStats:
    name: str
    damage_done: float = 0.0
    healing_done: float = 0.0
    # Portion of healing_done that was wasted because the target was already
    # near/at full HP -- see CombatEvent.overheal. healing_done itself is raw
    # cast power (unaffected by this); effective_healing_done() subtracts it.
    healing_overheal: float = 0.0
    damage_taken: float = 0.0
    # Damage prevented by Shield Chance mitigation -- see CombatEvent.shield_
    # absorbed. damage_taken above is already post-mitigation (what actually
    # landed); this is the part that didn't, which no existing number showed
    # at all -- e.g. a real tank pull where this was 43% of all raw incoming
    # damage, entirely invisible before this field existed.
    damage_absorbed: float = 0.0
    # Running threat total -- see CombatEvent.threat_delta. Can go negative
    # (threat dumps like Chaff Flare log a large negative ModifyThreat), so
    # this is a signed running sum, not a monotonic counter.
    threat: float = 0.0
    deaths: int = 0
    damage_by_ability: Dict[str, float] = field(default_factory=dict)
    healing_by_ability: Dict[str, float] = field(default_factory=dict)
    # Same totals, broken down by WHO instead of WHICH ABILITY -- lets a
    # tank/dps see "how much of my damage was actually on the boss" instead
    # of it being diluted by every add in the pull, and a healer see who
    # they actually spent their output on.
    damage_by_target: Dict[str, float] = field(default_factory=dict)
    healing_by_target: Dict[str, float] = field(default_factory=dict)
    # (timestamp, amount) per landed hit/heal, in arrival order -- the raw
    # material burst_dps()/burst_hps() slide a window over. Not aggregated
    # away like everything else above because "best 10s window" genuinely
    # needs individual timestamps, not just a running total -- but bucketed
    # to whole seconds (see _append_bucketed) rather than kept per-tick: a
    # busy pull logs many hits a second, and burst math only needs ~1s
    # resolution against a BURST_WINDOW_SECONDS=10.0 window. Measured on a
    # real 200-pull history: these two fields were 79% of history.json's
    # 15.8MB, and re-serializing the whole file on every single pull
    # completion (append_history_entry rewrites it in full) cost ~330ms of
    # blocking I/O on the log-reader thread. Bucketing doesn't fix the
    # rewrite-the-whole-file design, but it cuts what there is to rewrite by
    # roughly the average events-per-second-per-player, which in practice is
    # most of it.
    damage_events: List[Tuple[float, float]] = field(default_factory=list)
    heal_events: List[Tuple[float, float]] = field(default_factory=list)
    # Outgoing-attack accuracy/crit tallies. damage_attempts counts every
    # damage tick this entity dealt regardless of outcome (landed or not);
    # damage_avoided is the subset the target's defense roll stopped
    # (miss/dodge/parry/deflect/resist -- see CombatEvent.avoidance);
    # damage_crits is the subset of LANDED hits that crit. Heals don't roll
    # to hit, so there's no heal_avoided -- only a crit tally.
    damage_attempts: int = 0
    damage_avoided: int = 0
    damage_crits: int = 0
    heal_casts: int = 0
    heal_crits: int = 0
    # Every actual ability activation (see CombatEvent.is_ability_activate),
    # for APM -- deliberately NOT damage_attempts/heal_casts, which are per
    # TICK (a channelled/AoE ability produces many); this is per CAST.
    ability_casts: int = 0
    # How often THIS player's own cast got interrupted -- see
    # CombatEvent.is_interrupted for why this can't be "interrupts I landed
    # on someone else" instead.
    times_interrupted: int = 0
    # Hard CC (stun/mez/sleep) landed on an NPC -- see CombatEvent.is_hard_cc.
    cc_casts: int = 0
    cc_by_ability: Dict[str, int] = field(default_factory=dict)
    # Raid-wide utility cooldowns (Predation, Bloodthirst, ...) -- see
    # CombatEvent.is_raid_buff_cast.
    raid_buff_casts: int = 0
    raid_buff_by_ability: Dict[str, int] = field(default_factory=dict)
    # True once this entity has been seen with SWTOR's '@' player marker.
    # Without it, NPCs are indistinguishable from players downstream, and any
    # "deaths" total silently counts every add and trash mob that died --
    # e.g. 15,514 "deaths" across 96 Styrak pulls, which is add spawns, not
    # the raid wiping 161 times a pull.
    is_player: bool = False

    def dps(self, duration: float) -> float:
        return self.damage_done / duration if duration > 0 else 0.0

    def hps(self, duration: float) -> float:
        return self.healing_done / duration if duration > 0 else 0.0

    def effective_healing_done(self) -> float:
        """Actual HP restored -- healing_done minus the portion that
        overhealed a target already near/at full."""
        return max(self.healing_done - self.healing_overheal, 0.0)

    def effective_hps(self, duration: float) -> float:
        return self.effective_healing_done() / duration if duration > 0 else 0.0

    def boss_dps(self, boss_names, duration: float) -> float:
        """Damage-per-second to `boss_names` only -- see damage_to()."""
        return self.damage_to(boss_names) / duration if duration > 0 else 0.0

    def tps(self, duration: float) -> float:
        return self.threat / duration if duration > 0 else 0.0

    def apm(self, duration: float) -> float:
        return (60.0 * self.ability_casts / duration) if duration > 0 else 0.0

    def burst_dps(self, window_seconds: float = BURST_WINDOW_SECONDS) -> float:
        """Best `window_seconds`-wide stretch of this fight, not the whole-
        pull average -- answers "how hard did my cooldown window actually
        hit" rather than diluting it across every quiet moment too."""
        return _max_window_sum(self.damage_events, window_seconds) / window_seconds

    def burst_hps(self, window_seconds: float = BURST_WINDOW_SECONDS) -> float:
        return _max_window_sum(self.heal_events, window_seconds) / window_seconds

    def mitigation_pct(self) -> float:
        """What fraction of RAW incoming damage (taken + absorbed) the
        shield ate. 0 when nothing has hit this entity yet, not a divide-
        by-zero crash."""
        raw = self.damage_taken + self.damage_absorbed
        return (100.0 * self.damage_absorbed / raw) if raw > 0 else 0.0

    def accuracy_pct(self) -> float:
        """% of outgoing attacks that landed at all (weren't missed/dodged/
        parried/deflected/resisted). 0 with no attempts yet, not a crash."""
        return (100.0 * (self.damage_attempts - self.damage_avoided) / self.damage_attempts
                if self.damage_attempts > 0 else 0.0)

    def crit_pct(self) -> float:
        """% of LANDED damage hits that crit -- a hit that never landed
        can't crit, so avoided attempts are excluded from the denominator."""
        landed = self.damage_attempts - self.damage_avoided
        return (100.0 * self.damage_crits / landed) if landed > 0 else 0.0

    def heal_crit_pct(self) -> float:
        return (100.0 * self.heal_crits / self.heal_casts) if self.heal_casts > 0 else 0.0

    def ability_breakdown(self):
        """Returns (damage_rows, healing_rows), each a list of (ability, amount)
        sorted descending by amount."""
        dmg = sorted(self.damage_by_ability.items(), key=lambda kv: kv[1], reverse=True)
        heal = sorted(self.healing_by_ability.items(), key=lambda kv: kv[1], reverse=True)
        return dmg, heal

    def target_breakdown(self):
        """Same shape as ability_breakdown(), but by WHO instead of WHICH
        ability -- (damage_rows, healing_rows) of (target_name, amount)."""
        dmg = sorted(self.damage_by_target.items(), key=lambda kv: kv[1], reverse=True)
        heal = sorted(self.healing_by_target.items(), key=lambda kv: kv[1], reverse=True)
        return dmg, heal

    def damage_to(self, names) -> float:
        """Sum of damage done to any target whose name is in `names` (e.g.
        a boss's recognized name set) -- the building block for "boss-only
        DPS", which excludes trash/adds diluting the real progression number."""
        name_set = set(names)
        return sum(v for k, v in self.damage_by_target.items() if k in name_set)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "damage_done": self.damage_done,
            "healing_done": self.healing_done,
            "healing_overheal": self.healing_overheal,
            "damage_taken": self.damage_taken,
            "damage_absorbed": self.damage_absorbed,
            "threat": self.threat,
            "deaths": self.deaths,
            "damage_by_ability": self.damage_by_ability,
            "healing_by_ability": self.healing_by_ability,
            "damage_by_target": self.damage_by_target,
            "damage_events": self.damage_events,
            "heal_events": self.heal_events,
            "healing_by_target": self.healing_by_target,
            "is_player": self.is_player,
            "damage_attempts": self.damage_attempts,
            "damage_avoided": self.damage_avoided,
            "damage_crits": self.damage_crits,
            "heal_casts": self.heal_casts,
            "heal_crits": self.heal_crits,
            "ability_casts": self.ability_casts,
            "times_interrupted": self.times_interrupted,
            "cc_casts": self.cc_casts,
            "cc_by_ability": self.cc_by_ability,
            "raid_buff_casts": self.raid_buff_casts,
            "raid_buff_by_ability": self.raid_buff_by_ability,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerStats":
        return cls(
            name=d["name"],
            damage_done=d.get("damage_done", 0.0),
            healing_done=d.get("healing_done", 0.0),
            healing_overheal=d.get("healing_overheal", 0.0),
            damage_taken=d.get("damage_taken", 0.0),
            damage_absorbed=d.get("damage_absorbed", 0.0),
            threat=d.get("threat", 0.0),
            deaths=d.get("deaths", 0),
            damage_by_ability=dict(d.get("damage_by_ability", {})),
            healing_by_ability=dict(d.get("healing_by_ability", {})),
            damage_by_target=dict(d.get("damage_by_target", {})),
            damage_events=[tuple(x) for x in d.get("damage_events", [])],
            heal_events=[tuple(x) for x in d.get("heal_events", [])],
            healing_by_target=dict(d.get("healing_by_target", {})),
            is_player=d.get("is_player", False),
            damage_attempts=d.get("damage_attempts", 0),
            damage_avoided=d.get("damage_avoided", 0),
            damage_crits=d.get("damage_crits", 0),
            heal_casts=d.get("heal_casts", 0),
            heal_crits=d.get("heal_crits", 0),
            ability_casts=d.get("ability_casts", 0),
            times_interrupted=d.get("times_interrupted", 0),
            cc_casts=d.get("cc_casts", 0),
            cc_by_ability=dict(d.get("cc_by_ability", {})),
            raid_buff_casts=d.get("raid_buff_casts", 0),
            raid_buff_by_ability=dict(d.get("raid_buff_by_ability", {})),
        )


class Encounter:
    def __init__(self, label: Optional[str] = None):
        self.label = label
        self.start_time: Optional[float] = None
        # Real (calendar) Unix epoch of this encounter's first event, for
        # DISPLAY only (e.g. History tab dates) -- never used for duration
        # math, which stays on start_time/last_activity/exit_combat_time.
        # Those use whatever clock the caller is replaying with (live
        # wall-clock, or replay_pulls()'s LogClock, which is relative and
        # NOT a real calendar time -- see apply()). None if no real anchor
        # was available (e.g. a multi-file merge_logs() import).
        self.real_start_time: Optional[float] = None
        self.last_activity: Optional[float] = None
        self.exit_combat_time: Optional[float] = None  # when ExitCombat was seen
        self.players: Dict[str, PlayerStats] = {}
        self._fixed_duration: Optional[float] = None  # set when loaded from disk
        # For Parsely per-encounter uploads: which file this came from, the
        # absolute line range it spans, and the most recent AreaEntered line
        # seen before/at this encounter (Parsely needs that line for context
        # even if it happened before this encounter's own first line).
        self.log_path: Optional[str] = None
        # Which parser generation produced these numbers -- see
        # HISTORY_DATA_VERSION. A live encounter is by definition current.
        self.data_version: int = HISTORY_DATA_VERSION
        # Set when a rebuild was attempted and could not be completed --
        # the row's (log_path, line range) no longer resolves, so its
        # numbers can't be re-derived. Kept rather than deleted, but never
        # retried and never presented as verified. See history_migration.
        self.unverified: bool = False
        self.start_line: Optional[int] = None
        self.end_line: Optional[int] = None
        self.area_entered_line: Optional[int] = None

    def _get(self, name: str) -> PlayerStats:
        if name not in self.players:
            self.players[name] = PlayerStats(name=name)
        return self.players[name]

    def duration(self) -> float:
        if self._fixed_duration is not None:
            return self._fixed_duration
        if self.start_time is None or self.last_activity is None:
            return 0.0
        end = self.exit_combat_time or self.last_activity
        return max(end - self.start_time, 0.001)

    def is_real_fight(self) -> bool:
        """True if any damage was dealt or taken during this encounter.

        The one signal that separates a pull from the raid standing around
        between pulls -- see the note above MIN_ENCOUNTER_SECONDS for the
        measurements. Both the live tracker and the offline replay gate on
        this, for the same reason they share NEW_PULL_MIN_GAP_SECONDS: if
        they disagree about what counts as a pull, the same log produces
        different History rows depending on how it was captured.
        """
        return any(p.damage_done > 0 or p.damage_taken > 0
                   for p in self.players.values())

    def raid_was_defeated(self) -> bool:
        """True if this pull ended with the raid wiped, as opposed to the
        group disengaging and re-pulling.

        Distinguishing the two matters because "we didn't kill it" covers
        both, and lumping them together makes the failure population
        meaningless: measured on the corpus, Styrak's 21 non-kills split
        into 7 real wipes (6-9 deaths each) and 9 pulls where NOBODY died,
        so the median "wipe" had half a death in it. Anything comparing
        kills against wipes -- coaching especially -- reads that as noise.

        The threshold is empirical, not a guess. Across all 385 non-kill
        pulls in the corpus, the fraction of the group that died is
        sharply bimodal: 142 pulls at 0.0 and 210 at 1.0, with only 33
        anywhere in between and a genuine valley through the middle.
        """
        players = [p for p in self.players.values() if p.is_player]
        if not players:
            return False
        died = sum(1 for p in players if p.deaths > 0)
        return (died / len(players)) >= RAID_DEFEATED_FRACTION

    def past_trailing_window(self, now: float) -> bool:
        """True once the trailing grace window after ExitCombat has elapsed
        (or, if we never saw ExitCombat, once the inactivity gap has)."""
        if self.exit_combat_time is not None:
            return (now - self.exit_combat_time) > TRAILING_CAPTURE_SECONDS
        if self.last_activity is not None:
            return (now - self.last_activity) > ENCOUNTER_GAP_SECONDS
        return False

    def apply(self, event: CombatEvent, at_time: Optional[float] = None,
              real_time: Optional[float] = None) -> None:
        now = at_time if at_time is not None else time.time()
        if self.start_time is None:
            self.start_time = now
            if real_time is not None:
                self.real_start_time = real_time
            elif at_time is None:
                # True live call (no synthetic at_time given) -- `now` IS
                # time.time(), safe to reuse as the real calendar anchor.
                self.real_start_time = now
            # else: at_time was given (a replay) but no real_time -- the
            # caller has no real calendar anchor for this file (e.g.
            # merge_logs(), or a filename that didn't match SWTOR's date
            # pattern). Leave real_start_time None rather than stamping a
            # bogus one from the replay clock's relative seconds value.
        self.last_activity = now

        if event.line_number is not None:
            if self.start_line is None or event.line_number < self.start_line:
                self.start_line = event.line_number
            if self.end_line is None or event.line_number > self.end_line:
                self.end_line = event.line_number

        # Record player-vs-NPC identity on every event that names an entity,
        # not just damage/heal ones, so an entity first seen on e.g. a death
        # line is still classified correctly. Sticky: once seen as a player,
        # always a player.
        if event.source and event.source_is_player:
            self._get(event.source).is_player = True
        if event.target and event.target_is_player:
            self._get(event.target).is_player = True

        if event.is_combat_end and self.exit_combat_time is None:
            self.exit_combat_time = now
            return

        if event.is_death and event.target:
            self._get(event.target).deaths += 1
            return

        ability_key = event.ability or event.effect_name or "Unknown"

        # Environmental damage (falling, etc.) logs is_damage=True with an
        # EMPTY ability field -- it's not an attack (no accuracy roll, can't
        # crit, no attacker "dealt" it in any meaningful sense), so it must
        # not count toward damage_done/damage_taken/attempts. Found this via
        # the accuracy% validation below: a player's own fall damage was
        # silently inflating their damage_done total.
        is_attack = event.is_damage and event.ability is not None

        if is_attack and event.amount:
            if event.source:
                p = self._get(event.source)
                p.damage_done += event.amount
                p.damage_by_ability[ability_key] = (
                    p.damage_by_ability.get(ability_key, 0.0) + event.amount
                )
                if event.target:
                    p.damage_by_target[event.target] = (
                        p.damage_by_target.get(event.target, 0.0) + event.amount
                    )
                _append_bucketed(p.damage_events, now, event.amount)
            if event.target:
                self._get(event.target).damage_taken += event.amount

        # Separate condition from the amount check above: a fully-shielded
        # hit can log a 0 post-mitigation amount with the entire hit
        # absorbed, and that's still real mitigation worth counting.
        if is_attack and event.shield_absorbed and event.target:
            self._get(event.target).damage_absorbed += event.shield_absorbed

        # Accuracy/crit tallies key off is_attack alone, not amount>0: a
        # miss/dodge/parry/deflect/resist logs a 0 amount too, and needs to
        # count as an ATTEMPT (so accuracy% has the right denominator)
        # without counting as a landed hit that could crit.
        if is_attack and event.source:
            p = self._get(event.source)
            p.damage_attempts += 1
            if event.avoidance:
                p.damage_avoided += 1
            elif event.is_critical:
                p.damage_crits += 1

        if event.is_heal and event.amount:
            if event.source:
                p = self._get(event.source)
                p.healing_done += event.amount
                p.healing_overheal += event.overheal
                p.healing_by_ability[ability_key] = (
                    p.healing_by_ability.get(ability_key, 0.0) + event.amount
                )
                if event.target:
                    p.healing_by_target[event.target] = (
                        p.healing_by_target.get(event.target, 0.0) + event.amount
                    )
                _append_bucketed(p.heal_events, now, event.amount)
        if event.is_heal and event.source:
            p = self._get(event.source)
            p.heal_casts += 1
            if event.is_critical:
                p.heal_crits += 1

        if event.is_threat_modified and event.source:
            self._get(event.source).threat += event.threat_delta

        if event.is_ability_activate and event.source:
            self._get(event.source).ability_casts += 1

        if event.is_interrupted and event.source:
            self._get(event.source).times_interrupted += 1

        if event.is_hard_cc and event.source:
            p = self._get(event.source)
            p.cc_casts += 1
            key = event.ability or event.effect_name or "Unknown"
            p.cc_by_ability[key] = p.cc_by_ability.get(key, 0) + 1

        if event.is_raid_buff_cast and event.source:
            p = self._get(event.source)
            p.raid_buff_casts += 1
            key = event.ability or "Unknown"
            p.raid_buff_by_ability[key] = p.raid_buff_by_ability.get(key, 0) + 1

    def snapshot(self, players_only: bool = True):
        """Returns list of (name, dps, hps, damage_taken, mitigated_pct, deaths)
        sorted by dps desc. mitigated_pct is what fraction of raw incoming
        damage (taken + absorbed) Shield Chance ate -- for the raw absorbed
        NUMBER (rather than the percentage), read PlayerStats.damage_absorbed
        directly; this tuple stays compact because every caller renders it as
        one table row.

        players_only hides NPCs by default -- every caller renders this under
        a "Player" heading, and a boss or add sitting in that table is simply
        wrong (a trash mob was out-DPSing the raid in the live meter).

        The filter is skipped entirely when nothing is flagged as a player:
        encounters restored from history written before is_player existed
        have it False everywhere, and filtering those would empty the table.
        """
        dur = self.duration()
        entities = list(self.players.values())
        if players_only and any(p.is_player for p in entities):
            entities = [p for p in entities if p.is_player]
        rows = [(p.name, p.dps(dur), p.hps(dur), p.damage_taken,
                 p.mitigation_pct(), p.deaths)
                for p in entities]
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows

    def player(self, name: str) -> Optional[PlayerStats]:
        return self.players.get(name)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "duration": self.duration(),
            "players": [p.to_dict() for p in self.players.values()],
            "log_path": self.log_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "area_entered_line": self.area_entered_line,
            "real_start_time": self.real_start_time,
            "data_version": self.data_version,
            "unverified": self.unverified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Encounter":
        enc = cls(label=d.get("label"))
        enc._fixed_duration = d.get("duration", 0.0)
        for pd in d.get("players", []):
            ps = PlayerStats.from_dict(pd)
            enc.players[ps.name] = ps
        enc.log_path = d.get("log_path")
        enc.start_line = d.get("start_line")
        enc.end_line = d.get("end_line")
        enc.area_entered_line = d.get("area_entered_line")
        enc.real_start_time = d.get("real_start_time")
        # Absent on anything written before the field existed -- which is
        # exactly what marks a row as needing rebuilding. See
        # history_migration.py.
        enc.data_version = d.get("data_version", 0)
        enc.unverified = bool(d.get("unverified", False))
        return enc


# SWTOR's base global cooldown. The engine's own combat log never marks
# WHICH abilities are on-GCD vs off-GCD, so gap detection (below) can't
# know that precisely either -- this is a floor, not a per-ability model.
GCD_BASE_SECONDS = 1.5
# A gap has to clear this multiple of the (possibly alacrity-scaled) GCD
# before it's flagged as idle time, not just normal cast-to-cast latency/
# animation variance. Picked empirically: back-to-back real casts land
# ~1.0-1.2x the nominal GCD apart in practice, so 1.5x leaves real
# rotation gaps (a full extra GCD or more of nothing) as what gets
# flagged, not routine noise.
GCD_GAP_MULTIPLIER = 1.5
