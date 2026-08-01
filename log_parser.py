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
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

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
DEATH_KEYWORDS = ("death", "expire")
HEAL_KEYWORDS = ("heal",)
DAMAGE_KEYWORDS = ("damage",)
COMBAT_START_KEYWORDS = ("entercombat",)
COMBAT_END_KEYWORDS = ("exitcombat",)
AREA_ENTERED_KEYWORDS = ("areaentered",)
EFFECT_REMOVED_KEYWORDS = ("removeeffect",)
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


def _clean_name(text: str) -> Optional[str]:
    """Strip trailing {id} tags and leading '@' from a bracket field, e.g.
    'Force Leap {812105301229568}' -> 'Force Leap'
    '@Idrurrez' -> 'Idrurrez'
    """
    if text is None:
        return None
    text = re.sub(r"\{[^{}]*\}", "", text).strip()
    text = text.lstrip("@").strip()
    return text or None


LEADING_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def _first_balanced_paren(text: str) -> Optional[str]:
    """Returns the content of the first top-level parenthesized group in
    text (honoring nesting), or None if there isn't a balanced one. Needed
    because SWTOR sometimes nests a sub-annotation inside the value group,
    e.g. '(3238 energy {id} -shield {id} (4121 absorbed {id}))' or
    '(488 kinetic {id}(reflected {id}))' -- a naive non-nesting regex
    matches only the inner '(4121 absorbed {id})' / '(reflected {id})'
    piece, which silently substitutes the wrong number (the absorbed
    sub-amount, or even a stray {id} digit string when there's no leading
    number at all) for the real damage/heal amount.
    """
    start = text.find("(")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return None  # unbalanced -- truncated/malformed line


SHIELD_ABSORBED_RE = re.compile(r"-shield\b.*?\((\d+(?:\.\d+)?)\s+absorbed", re.DOTALL)

# The trailing "<N>" value SWTOR appends to most lines, outside any paren
# group -- e.g. "... ModifyThreat {id}] <406.0>" or "... Damage {id}]
# (1878 energy {id}) <1878.0>". Only ModifyThreat's use of it is read today;
# for damage/heal lines it duplicates the paren amount and isn't needed.
ANGLE_VALUE_RE = re.compile(r"<(-?\d+(?:\.\d+)?)>")


def _extract_angle_value(tail: str) -> float:
    match = ANGLE_VALUE_RE.search(tail)
    return float(match.group(1)) if match else 0.0


def _extract_amount(tail: str) -> float:
    """Pulls the event's primary amount out of the trailing value text
    (everything after the core brackets). The real amount is always the
    leading numeric token of the FIRST top-level parenthesized group, even
    when that group carries a nested reflected/absorbed-damage annotation
    (see _first_balanced_paren) -- e.g. '1234 energy...' or '1234*' (crit).
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return 0.0
    match = LEADING_NUMBER_RE.match(group)
    return float(match.group(1)) if match else 0.0


def _extract_is_critical(tail: str) -> bool:
    """The leading number in the value group carries the crit marker
    directly, e.g. '9578* energy {id}' -> True, '9578 energy {id}' -> False.
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return False
    match = LEADING_NUMBER_RE.match(group)
    if not match:
        return False
    return group[match.end():match.end() + 1] == "*"


AVOIDANCE_RE = re.compile(r"-(miss|dodge|parry|deflect|resist)\b")


def _extract_avoidance(tail: str) -> Optional[str]:
    """Pulls the defense-roll outcome out of the same value group
    `_extract_amount` reads, e.g. '(0 -dodge {id})' -> "dodge". Always
    accompanies a 0 amount -- the attack never landed, as opposed to a
    landed hit that got shielded down to 0 (see _extract_shield_absorbed).
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return None
    match = AVOIDANCE_RE.search(group)
    return match.group(1) if match else None


def _extract_shield_absorbed(tail: str) -> float:
    """Pulls the tank shield-mitigation amount out of the same value group
    `_extract_amount` reads, e.g. '3238 energy {id} -shield {id} (4121
    absorbed {id})' -> 4121. `amount` above already captured the 3238 that
    got through; this is the part that didn't.

    Deliberately keyed on the literal '-shield' marker, not just the
    presence of "(N absorbed {id})" alone: a bare "(N absorbed {id})" with
    no '-shield' sibling is a different mechanic entirely (a hard
    absorb-shield BUFF like Static Barrier/Force Armor covering its full
    hit, already tracked separately in dots_hots.py) -- conflating the two
    would misattribute a healer's shield buff as tank stat mitigation.
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return 0.0
    match = SHIELD_ABSORBED_RE.search(group)
    return float(match.group(1)) if match else 0.0


OVERHEAL_RE = re.compile(r"~(\d+(?:\.\d+)?)")


def _extract_overheal(tail: str) -> float:
    """Pulls a heal's wasted-overheal amount out of the same value group
    `_extract_amount` reads, e.g. '10926* ~7382' -> 7382. Zero overheal
    (target wasn't near full) doesn't get a '~0' suffix on every line --
    it does in practice (SWTOR always emits it), but treat absence the
    same as explicit zero either way."""
    group = _first_balanced_paren(tail)
    if group is None:
        return 0.0
    match = OVERHEAL_RE.search(group)
    return float(match.group(1)) if match else 0.0


def _parse_entity(
    field: str, self_name: Optional[str] = None
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
        return self_name, None, None, False, None

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
        brackets[2] if len(brackets) > 2 else "", self_name=source
    )
    ability = _clean_name(brackets[3]) if len(brackets) > 3 else None

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


def _classify(event: CombatEvent, tail: str) -> None:
    # Classify from the EVENT-TYPE fields only, never the ability name. The
    # ability is what was cast; the effect fields are what actually happened.
    # Including the ability name here misclassifies any ability whose name
    # merely contains a keyword -- "Death Field" / "Death From Above" ticks
    # were being counted as deaths, and because stats.apply() returns early
    # on a death, their damage was silently dropped from DPS as well.
    haystack = " ".join(
        filter(None, [event.effect_type, event.effect_name])
    ).lower()

    # Collapse whitespace so "Enter Combat" and "EnterCombat" both match
    tight = haystack.replace(" ", "")

    event.is_damage = any(k in haystack for k in DAMAGE_KEYWORDS)
    event.is_heal = any(k in haystack for k in HEAL_KEYWORDS)
    event.is_death = any(k in haystack for k in DEATH_KEYWORDS)
    event.is_combat_start = any(k in tight for k in COMBAT_START_KEYWORDS)
    event.is_combat_end = any(k in tight for k in COMBAT_END_KEYWORDS)
    event.is_area_entered = any(k in tight for k in AREA_ENTERED_KEYWORDS)
    effect_type_tight = (event.effect_type or "").lower().replace(" ", "")
    event.is_effect_removed = any(k in effect_type_tight for k in EFFECT_REMOVED_KEYWORDS)
    effect_name_tight = (event.effect_name or "").lower().replace(" ", "")
    event.is_ability_activate = any(
        k in effect_name_tight for k in ABILITY_ACTIVATE_KEYWORDS
    )
    event.is_threat_modified = any(
        k in effect_name_tight for k in THREAT_MODIFIED_KEYWORDS
    )
    event.is_interrupted = any(
        k in effect_name_tight for k in ABILITY_INTERRUPT_KEYWORDS
    )
    event.is_hard_cc = (
        event.source_is_player and not event.target_is_player
        and not event.is_effect_removed
        and any(k in effect_name_tight for k in HARD_CC_KEYWORDS)
    )
    event.is_raid_buff_cast = (
        event.is_ability_activate and event.source_is_player
        and event.ability in RAID_BUFF_ABILITY_NAMES
    )

    if event.is_damage or event.is_heal:
        event.amount = _extract_amount(tail)
        event.is_critical = _extract_is_critical(tail)
    if event.is_heal:
        event.overheal = _extract_overheal(tail)
    if event.is_damage:
        # Shield Chance mitigation and avoidance (miss/dodge/parry/deflect/
        # resist) only ever apply to incoming damage, not healing -- no need
        # to scan the tail on every heal tick too.
        event.shield_absorbed = _extract_shield_absorbed(tail)
        event.avoidance = _extract_avoidance(tail)
    if event.is_threat_modified:
        event.threat_delta = _extract_angle_value(tail)
