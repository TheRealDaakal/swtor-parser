"""
stats.py

Aggregates parsed CombatEvents into a live per-player encounter breakdown:
DPS, HPS, damage taken, deaths, and a per-ability breakdown for each.

Encounter boundaries:
- Prefer explicit EnterCombat/ExitCombat events when the log provides them.
- Fall back to an inactivity gap (no combat events for ENCOUNTER_GAP_SECONDS)
  for logs/situations where those markers aren't seen.
- Like StarParse, damage/heal events arriving up to TRAILING_CAPTURE_SECONDS
  after ExitCombat still count toward the encounter that just ended (SWTOR
  can log a DoT tick or HoT after the official combat-exit event) instead of
  either being dropped or bleeding into the next pull's numbers.

Completed encounters are kept in `StatsTracker.history` so a GUI can offer
a session browser (BARAS/StarParse both have one). Encounter/PlayerStats
support to_dict()/from_dict() so history can be persisted to disk (see
storage.py) and reloaded as ordinary Encounter objects.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from log_parser import CombatEvent

ENCOUNTER_GAP_SECONDS = 8.0        # inactivity fallback for ending a fight
TRAILING_CAPTURE_SECONDS = 4.0     # StarParse-style post-ExitCombat grace window


@dataclass
class PlayerStats:
    name: str
    damage_done: float = 0.0
    healing_done: float = 0.0
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

    def tps(self, duration: float) -> float:
        return self.threat / duration if duration > 0 else 0.0

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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "damage_done": self.damage_done,
            "healing_done": self.healing_done,
            "damage_taken": self.damage_taken,
            "damage_absorbed": self.damage_absorbed,
            "threat": self.threat,
            "deaths": self.deaths,
            "damage_by_ability": self.damage_by_ability,
            "healing_by_ability": self.healing_by_ability,
            "is_player": self.is_player,
            "damage_attempts": self.damage_attempts,
            "damage_avoided": self.damage_avoided,
            "damage_crits": self.damage_crits,
            "heal_casts": self.heal_casts,
            "heal_crits": self.heal_crits,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerStats":
        return cls(
            name=d["name"],
            damage_done=d.get("damage_done", 0.0),
            healing_done=d.get("healing_done", 0.0),
            damage_taken=d.get("damage_taken", 0.0),
            damage_absorbed=d.get("damage_absorbed", 0.0),
            threat=d.get("threat", 0.0),
            deaths=d.get("deaths", 0),
            damage_by_ability=dict(d.get("damage_by_ability", {})),
            healing_by_ability=dict(d.get("healing_by_ability", {})),
            is_player=d.get("is_player", False),
            damage_attempts=d.get("damage_attempts", 0),
            damage_avoided=d.get("damage_avoided", 0),
            damage_crits=d.get("damage_crits", 0),
            heal_casts=d.get("heal_casts", 0),
            heal_crits=d.get("heal_crits", 0),
        )


class Encounter:
    def __init__(self, label: Optional[str] = None):
        self.label = label
        self.start_time: Optional[float] = None
        self.last_activity: Optional[float] = None
        self.exit_combat_time: Optional[float] = None  # when ExitCombat was seen
        self.players: Dict[str, PlayerStats] = {}
        self._fixed_duration: Optional[float] = None  # set when loaded from disk
        # For Parsely per-encounter uploads: which file this came from, the
        # absolute line range it spans, and the most recent AreaEntered line
        # seen before/at this encounter (Parsely needs that line for context
        # even if it happened before this encounter's own first line).
        self.log_path: Optional[str] = None
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

    def past_trailing_window(self, now: float) -> bool:
        """True once the trailing grace window after ExitCombat has elapsed
        (or, if we never saw ExitCombat, once the inactivity gap has)."""
        if self.exit_combat_time is not None:
            return (now - self.exit_combat_time) > TRAILING_CAPTURE_SECONDS
        if self.last_activity is not None:
            return (now - self.last_activity) > ENCOUNTER_GAP_SECONDS
        return False

    def apply(self, event: CombatEvent, at_time: Optional[float] = None) -> None:
        now = at_time if at_time is not None else time.time()
        if self.start_time is None:
            self.start_time = now
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
                p.healing_by_ability[ability_key] = (
                    p.healing_by_ability.get(ability_key, 0.0) + event.amount
                )
        if event.is_heal and event.source:
            p = self._get(event.source)
            p.heal_casts += 1
            if event.is_critical:
                p.heal_crits += 1

        if event.is_threat_modified and event.source:
            self._get(event.source).threat += event.threat_delta

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
        return enc


class StatsTracker:
    """Owns the current encounter and rolls over to a fresh one once the
    trailing capture window has elapsed."""

    def __init__(self, preloaded_history: Optional[List[Encounter]] = None):
        self.current = Encounter()
        self.history: List[Encounter] = list(preloaded_history or [])
        self.current_log_path: Optional[str] = None
        self.last_area_entered_line: Optional[int] = None
        # Guards self.current/self.history: feed() is called from the
        # background log-reader thread while snapshot()/history_snapshot()
        # are polled from the Tk GUI thread roughly every 500ms. Without
        # this, the GUI thread can iterate self.current.players.values()
        # (in Encounter.snapshot()) at the same moment the reader thread
        # inserts a new player into that dict, raising "dictionary changed
        # size during iteration" and crashing the refresh loop.
        self._lock = threading.Lock()

    def set_log_path(self, path: str) -> None:
        self.current_log_path = path
        self.current.log_path = path

    def feed(self, event: CombatEvent) -> Optional[Encounter]:
        """Feeds one event in. Returns the just-completed Encounter if this
        call caused a rollover (useful for persisting it immediately),
        otherwise None."""
        with self._lock:
            now = time.time()
            completed = None

            if event.is_area_entered and event.line_number is not None:
                self.last_area_entered_line = event.line_number

            # If the previous encounter is done (past its trailing window)
            # and a new combat event shows up, roll over to a fresh encounter.
            if self.current.players and self.current.past_trailing_window(now):
                completed = self.current
                self.history.append(completed)
                self.current = Encounter()
                self.current.log_path = self.current_log_path
                self.current.area_entered_line = self.last_area_entered_line

            if self.current.area_entered_line is None:
                self.current.area_entered_line = self.last_area_entered_line

            self.current.apply(event)
            return completed

    def snapshot(self):
        with self._lock:
            return self.current.snapshot(), self.current.duration()

    def history_snapshot(self):
        """Returns list of (index, duration, rows) for completed encounters,
        most recent first."""
        with self._lock:
            out = []
            for i, enc in enumerate(reversed(self.history)):
                out.append((len(self.history) - i, enc.duration(), enc.snapshot()))
            return out
