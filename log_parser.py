"""
log_parser.py

Parses SWTOR combat log lines into structured events.

SWTOR combat log lines look roughly like:

    [Time] [Source] [Target] [Ability {id}] [EventType {id}: EffectName {id}] (value ...)

We tokenize generically by pulling out every [bracketed] and (parenthesized)
group in order, rather than assuming a fixed number of fields. This makes the
parser resilient to minor format changes across game patches -- if BioWare
adds/removes a field, we still get the groups we care about instead of
throwing an IndexError.

The Source and Target brackets are themselves pipe-delimited, embedding live
position and HP data alongside the entity's name, e.g.:

    @Name#accountid|(x,y,z,heading)|(hp_current/hp_max)         -- player
    Mob Name {longid}:instanceid|(x,y,z,heading)|(hp/hp)        -- NPC
    @Owner#accountid/PetName {id}:instanceid|(...)|(hp/hp)      -- companion/pet
    =                                                            -- same entity as source
    (empty)                                                      -- no entity

_parse_entity() below is what untangles that -- see it for the full grammar.
Trailing value groups (damage/heal amount, crit marker, etc.) only ever
appear AFTER the core brackets, never inside them, so they're extracted
separately from the tail of the line rather than by scanning every
parenthesized group in the whole line (which would otherwise pick up a
position coordinate or HP fraction from inside an entity bracket and
mistake it for the event's amount).

If this mis-parses your real logs, send a raw sample line and the regexes
below (BRACKET_RE / PAREN_RE / TIME_RE / entity grammar in _parse_entity)
are the only things that need adjusting.

Event classification (is_death, is_damage, ...) and the value-extraction
helpers (amount, crit, shield absorbed, ...) now live in parser_core/ --
this module imports them so the tokenizer above and that classification
stay in one place for callers, without duplicating the logic.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from parser_core.classifier import _classify
from parser_core.field_extractors import (
    _extract_id, _clean_name, _first_balanced_paren, _extract_angle_value,
    _extract_amount, _extract_is_critical, _extract_avoidance,
    _extract_shield_absorbed, _extract_overheal,
)

BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
PAREN_RE = re.compile(r"\(([^()]*)\)")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
HP_FRACTION_RE = re.compile(r"^(\d+)/(\d+)$")
ENTITY_ID_RE = re.compile(r"\{[^{}]*\}")
ACCOUNT_SUFFIX_RE = re.compile(r"#\d+$")

# Matches HH:MM:SS(.mmm) or MM/DD/YYYY HH:MM:SS style timestamps
TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}(?:\.\d+)?")

# Keywords that identify event *types* inside the effect bracket, e.g.
# "[ApplyEffect {id}: Death {id}]" or "[Event {id}: AbilityActivate {id}]"
# A death is the exact event `[Event {id}: Death {id}]` -- NOT any effect
# whose name happens to contain "death". This was a substring match against
# effect_type + effect_name, and SWTOR is full of ability names that trip it:
# measured over 25 real log files, 4,309 genuine deaths were accompanied by
# 26,141 false ones -- "Deathmark" (a Marauder debuff, re-logged on every
# ModifyCharges tick) alone accounted for 23,631, plus "Penetrating Death",
# "Death Brand (Slow)", "Feign Death", "Death's Embrace" and others.
#
# Everything downstream inherited that: History death counts, the death
# forensics reports, and kill/wipe classification. The old "expire" keyword
# is dropped with it -- it matched nothing at all in those 25 files, and
# SWTOR's only Event names are AbilityActivate, TargetSet, TargetCleared,
# ModifyThreat, AbilityCancel, Death, AbilityDeactivate, Crouch, LeaveCover,
# AbilityInterrupt, Taunt, Revived, FailedEffect, EnterCombat, ExitCombat.
DEATH_EVENT_TYPE = "event"
DEATH_EFFECT_NAME = "death"
HEAL_KEYWORDS = ("heal",)
DAMAGE_KEYWORDS = ("damage",)
COMBAT_START_KEYWORDS = ("entercombat",)
COMBAT_END_KEYWORDS = ("exitcombat",)
AREA_ENTERED_KEYWORDS = ("areaentered",)
EFFECT_REMOVED_KEYWORDS = ("removeeffect",)
# Shield abilities with a charge count (Kolto Shell, Trauma Probe) log a
# distinct "[ModifyCharges {id}: <ability> {id}] (N charges {id})" line each
# time a charge is consumed, separate from the ApplyEffect that first cast
# it -- e.g. "[ModifyCharges {id}: Kolto Shell {id}] (3 charges {id})".
MODIFY_CHARGES_KEYWORDS = ("modifycharges",)
# Identifies the log's own "this entity actually activated an ability" event,
# as distinct from the damage ticks / effect applications that ability then
# produces (all of which carry the SAME ability name in the log line).
ABILITY_ACTIVATE_KEYWORDS = ("abilityactivate",)
# The log's own threat-generation event -- e.g. "[Event {id}: ModifyThreat
# {id}] <406.0>". Neither BARAS nor ORBS's public docs mention reading this
# directly and StarParse's own threat number is presumably built from it,
# but nothing in this project touched it before now.
THREAT_MODIFIED_KEYWORDS = ("modifythreat",)
# The log's own interrupt event -- source is whoever's cast GOT interrupted,
# not whoever performed the interrupt (SWTOR doesn't attribute the other
# side without correlating a separate per-class interrupt-ability id list,
# which this project doesn't have), so this can only answer "how often was
# I interrupted", not "how many interrupts did I land".
ABILITY_INTERRUPT_KEYWORDS = ("abilityinterrupt",)
# Hard CC only (stun/mez/sleep/incapacitate) -- deliberately excludes
# "slowed", which showed up on nearly every high-frequency damage ability in
# a real corpus scan (a minor movement-slow tacked onto normal attacks, not
# an intentional crowd-control choice) and would just be noise for "did we
# lock the add down". Matched against effect_name, same as every other
# classification here -- never the ability name (see _classify's comment on
# why: "Death Field" would misclassify against a "death" keyword the same
# way "Force Storm" would misfire against an ability-name CC check here).
HARD_CC_KEYWORDS = ("stunned", "incapacitated", "asleep", "sleeping", "lifted")
# Raid-wide utility cooldowns -- verified against this user's own 216-log
# corpus by BEHAVIOR, not by guessing names: one player casts it, and the
# identical ability name applies as an effect to several OTHER players
# within about a second (the same structural signature Inspiration and
# Predation both showed). Unlike HARD_CC_KEYWORDS this is matched by EXACT
# ability name, not a substring: it's a small curated list of real,
# corpus-verified abilities, not a generic keyword that could misfire
# against an unrelated ability sharing a word (same reasoning dots_hots.py
# and boss ability_cast triggers already rely on for exact-name matching).
RAID_BUFF_ABILITY_NAMES = frozenset({
    "Transcendence", "Aegis Shield", "Predation", "Warding Shield",
    "Unlimited Power", "Inspiration", "Tactical Superiority",
    "Supercharged Celerity", "Stack the Deck", "Force Empowerment",
    "Bloodthirst", "Rally",
})


@dataclass
class CombatEvent:
    raw: str
    timestamp: Optional[str]
    source: Optional[str]
    target: Optional[str]
    ability: Optional[str]
    effect_type: Optional[str]   # e.g. ApplyEffect, Event, RemoveEffect
    effect_name: Optional[str]   # e.g. "Force Scream", "Death", "AbilityActivate"
    values: List[str] = field(default_factory=list)  # raw parenthetical groups
    # The ability field's own numeric {id}, kept as a string. `ability`
    # above is the human-readable name with that id stripped -- but SWTOR
    # logs some abilities with NO name at all, just the id (a real example
    # from this user's logs: "[ {3016484380999680}]", The Writhing Horror's
    # Burrow). Keyword matching can never reach those, so triggers that
    # need them match on this instead. See boss_definitions' ability_ids.
    ability_id: Optional[str] = None
    # On AreaEntered lines only: the instance's difficulty ("story",
    # "veteran", "master") and group size, parsed from e.g.
    # "[AreaEntered {id}: Darvannis {id} 8 Player Master {id}]".
    # BARAS scopes 953 of its 994 timers by difficulty; without this the
    # engine has no way to honour that and fires Master-mode mechanics at
    # a Story-mode group. None on every other line.
    difficulty: Optional[str] = None
    group_size: Optional[int] = None

    # Derived, filled in by classify()
    is_damage: bool = False
    is_heal: bool = False
    is_death: bool = False
    is_combat_start: bool = False
    is_combat_end: bool = False
    is_area_entered: bool = False
    amount: float = 0.0
    line_number: Optional[int] = None
    # HP snapshot, if a parenthetical group looked like "current/max" -- used
    # for HP-threshold boss phase triggers (e.g. "enrage at 50%").
    hp_current: Optional[float] = None
    hp_max: Optional[float] = None
    # Whether the source/target bracket had a leading '@' (SWTOR's marker for
    # a player-controlled entity vs an NPC) -- used to guess the "local
    # player" for target=local_player timer scoping. Best-effort: same
    # heuristic BARAS itself uses (first player entity seen), not a
    # guaranteed-correct identification in a full group.
    source_is_player: bool = False
    target_is_player: bool = False
    is_effect_removed: bool = False
    # True for a charge-shield's "a charge was just consumed" line -- see
    # MODIFY_CHARGES_KEYWORDS. Distinct from is_effect_removed: the shield is
    # still up after this (unless it was the last charge, which logs
    # RemoveEffect instead, not a ModifyCharges down to 0).
    is_charges_modified: bool = False
    # The game's *type* id for a non-player source/target (the `{...}` on an
    # NPC entity), or None for players and empty fields. Display names aren't
    # unique across fights -- these ids are what BARAS matches on, and what
    # boss recognition prefers when a definition supplies them.
    source_npc_id: Optional[str] = None
    target_npc_id: Optional[str] = None
    # True only for the actual ability-activation event. A single cast also
    # produces damage/heal ticks and effect applications that repeat the same
    # ability name, so anything that means "was cast" (as opposed to "is
    # mentioned") must check this -- see boss_definitions' ability_cast.
    is_ability_activate: bool = False
    # Damage prevented by the TARGET's Shield Chance stat (tank shield/absorb
    # mitigation -- Vanguard/Powertech, Guardian/Juggernaut, Shadow/Assassin
    # all use the identical log annotation for this, since it's a stat proc,
    # not a class ability). `amount` above is already the post-mitigation
    # damage that landed; this is the portion that DIDN'T, which the log
    # only ever mentions inside a nested "(N absorbed {id})" group tagged
    # with a sibling "-shield" marker -- see _extract_amount_and_shield.
    shield_absorbed: float = 0.0
    # Portion of a heal that couldn't land because the target was already
    # near/at full HP. SWTOR logs a heal's full cast power as the primary
    # amount and this as a nested "~N" suffix -- e.g. "(10926* ~7382)" is a
    # 10926-power heal that only restored 3544 HP, wasting 7382. amount
    # above is that raw cast power, NOT reduced by this -- subtract it
    # yourself for the effective (actually-restored) amount.
    overheal: float = 0.0
    # True when the value group's leading number carries SWTOR's own crit
    # marker (a literal trailing '*', e.g. '9578*') -- applies to both damage
    # and heals, since both can crit.
    is_critical: bool = False
    # Set to "miss"/"dodge"/"parry"/"deflect"/"resist" when a damage attempt
    # didn't land at all (accuracy/defense roll failed), else None. Distinct
    # from shield_absorbed: that's a hit that got partially/fully mitigated
    # *after* landing; this is an attack that never landed in the first
    # place. Heals have no equivalent -- they don't roll to hit.
    avoidance: Optional[str] = None
    # True only for the log's own threat-generation event (source generated/
    # lost threat_delta points on target). Distinct from is_damage/is_heal --
    # a single ability cast can log ITS OWN separate ModifyThreat line, not
    # derived from the damage/heal amount, so this can't be inferred from
    # amount alone (e.g. Chaff Flare logs a large NEGATIVE delta with no
    # damage/heal at all).
    is_threat_modified: bool = False
    threat_delta: float = 0.0
    # True for the log's own AbilityInterrupt event. See
    # ABILITY_INTERRUPT_KEYWORDS -- source is the player who GOT
    # interrupted, not the interrupter.
    is_interrupted: bool = False
    # True when a player applied hard CC (stun/mez/sleep) to an NPC -- see
    # HARD_CC_KEYWORDS. Scoped to player->NPC only: the same effect names
    # fire in the other direction (a boss stunning a player) and that's a
    # different question ("was I CC'd") from what this answers ("did we CC
    # the add").
    is_hard_cc: bool = False
    # True for the actual cast (not the broadcast applications) of a
    # RAID_BUFF_ABILITY_NAMES ability -- one per activation, so casting
    # Predation on 15 people counts as 1, not 15. See RAID_BUFF_ABILITY_NAMES.
    is_raid_buff_cast: bool = False


ABILITY_ID_RE = re.compile(r"\{(\d+)\}")

# "8 Player Master", "4 Player Veteran", "16 Player Story" -- the instance
# difficulty SWTOR records on every AreaEntered line. Matched on the
# id-stripped text, so "Darvannis  8 Player Master" reduces cleanly.
DIFFICULTY_RE = re.compile(r"(\d+)\s+Player\s+([A-Za-z_]+)", re.IGNORECASE)


def _parse_entity(
    field: str, self_name: Optional[str] = None,
    self_is_player: bool = False, self_npc_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[float], Optional[float], bool, Optional[str]]:
    """Parses one raw Source/Target bracket capture. Returns
    (name, hp_current, hp_max, is_player, npc_type_id).

    npc_type_id is the `{...}` id on an NPC entity -- the game's *type* id
    for that mob (the trailing `:digits` is the per-instance id, which
    differs for every individual spawn and is not useful for identification).
    This matters because display names are NOT unique: the Dread Council
    fight and the solo Dread Master fights log identical names but different
    type ids, so name-only matching cannot tell them apart. Boss recognition
    prefers this id when a definition provides one.

    Grammar (see module docstring):
      ''                                                -> no entity
      '='                                                -> same entity as source (self_name)
      '@Name#accountid|(x,y,z,heading)|(hp/hp)'          -> player
      'Mob Name {id}:instanceid|(x,y,z,heading)|(hp/hp)' -> NPC
      '@Owner#acctid/PetName {id}:instanceid|(...)|(hp/hp)' -> companion/pet,
        reported under its own name, not folded into the owning player --
        same tradeoff BARAS makes (see README's known limitations).
    """
    if not field:
        return None, None, None, False, None
    if field == "=":
        # '=' means "the target IS the source" (self-cast: a DCD, a self-heal,
        # a self-applied buff). It therefore inherits the source's identity --
        # including whether that entity is a player. Returning False here
        # instead marked every self-targeted event on a player as an NPC
        # event, which silently excluded self-heals and self-buffs from
        # anything gated on target_is_player.
        return self_name, None, None, self_is_player, self_npc_id

    parts = field.split("|")
    name_token = parts[0]
    is_player = name_token.startswith("@")
    npc_type_id = None

    if is_player:
        name_token = name_token[1:]
        if "/" in name_token:
            _, pet_part = name_token.split("/", 1)
            id_match = ENTITY_ID_RE.search(pet_part)
            if id_match:
                npc_type_id = id_match.group()[1:-1] or None
            pet_part = ENTITY_ID_RE.sub("", pet_part).split(":")[0].strip()
            name = pet_part or None
            is_player = False
        else:
            name = ACCOUNT_SUFFIX_RE.sub("", name_token).strip() or None
    else:
        id_match = ENTITY_ID_RE.search(name_token)
        if id_match:
            npc_type_id = id_match.group()[1:-1] or None
        cleaned = ENTITY_ID_RE.sub("", name_token)
        name = cleaned.split(":")[0].strip() or None

    hp_current = hp_max = None
    if len(parts) >= 3:
        hp_match = HP_FRACTION_RE.match(parts[2].strip("() "))
        if hp_match:
            hp_current, hp_max = float(hp_match.group(1)), float(hp_match.group(2))

    return name, hp_current, hp_max, is_player, npc_type_id


def parse_line(line: str, line_number: Optional[int] = None) -> Optional[CombatEvent]:
    line = line.strip()
    if not line:
        return None

    bracket_matches = list(BRACKET_RE.finditer(line))
    if len(bracket_matches) < 3:
        # Not a recognizable combat line (could be a log header/footer)
        return None

    brackets = [m.group(1) for m in bracket_matches]

    timestamp_raw = brackets[0]
    time_match = TIME_RE.search(timestamp_raw)
    timestamp = time_match.group() if time_match else timestamp_raw

    source, _src_hp_current, _src_hp_max, source_is_player, source_npc_id = _parse_entity(
        brackets[1] if len(brackets) > 1 else ""
    )
    target, hp_current, hp_max, target_is_player, target_npc_id = _parse_entity(
        brackets[2] if len(brackets) > 2 else "", self_name=source,
        self_is_player=source_is_player, self_npc_id=source_npc_id,
    )
    ability = _clean_name(brackets[3]) if len(brackets) > 3 else None
    ability_id = _extract_id(brackets[3]) if len(brackets) > 3 else None

    effect_type = None
    effect_name = None
    if len(brackets) > 4:
        # Field 4 is usually "EffectType {id}: EffectName {id}"
        raw_effect = brackets[4]
        if ":" in raw_effect:
            etype, ename = raw_effect.split(":", 1)
            effect_type = _clean_name(etype)
            effect_name = _clean_name(ename)
        else:
            effect_type = _clean_name(raw_effect)

    # Trailing value text (damage/heal amount, crit marker, etc.) only ever
    # appears after the core [time][source][target][ability][effect]
    # brackets -- scan just that tail, not the whole line, so a position
    # coordinate or HP fraction embedded in the source/target brackets
    # above can never be mistaken for the event's own amount.
    last_core_end = bracket_matches[min(4, len(bracket_matches) - 1)].end()
    tail = line[last_core_end:]
    values = PAREN_RE.findall(tail)

    event = CombatEvent(
        raw=line,
        timestamp=timestamp,
        source=source,
        target=target,
        ability=ability,
        ability_id=ability_id,
        effect_type=effect_type,
        effect_name=effect_name,
        values=values,
        line_number=line_number,
        source_is_player=source_is_player,
        target_is_player=target_is_player,
        source_npc_id=source_npc_id,
        target_npc_id=target_npc_id,
    )
    event.hp_current, event.hp_max = hp_current, hp_max
    _classify(event, tail)
    return event
