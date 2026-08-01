"""
Translates ORBS's (github.com/L34T/ORBS) real boss timer JSON into our JSON
boss schema, as a supplement to translate_baras_toml.py -- ORBS is an
independently-authored data source (not copied from BARAS), and for some
bosses (Dread Master Styrak: 32 real timers vs. our 3) it has dramatically
more real mechanic coverage.

Key schema gap this bridges: ORBS timers key off a numeric TriggerType enum
(TimerKeyType in their DataStructures/Timer.cs) plus a mix of fields whose
meaning depends on that type -- Ability/Effect hold plain display-text
strings already (ORBS does its own resolution, so unlike BARAS's numeric
ability ids, no id_to_names lookup is needed here), Source/Target hold
either a literal NPC id string or the sentinels "Any"/"Ignore"/"Local".

Deliberately scoped to a SAFE SUBSET this pass, not the full 19-value enum:
  AbilityUsed, NewEntitySpawn, EntityDeath, TimerExpired, EntityHP
These five map cleanly 1:1 onto condition types our engine already has
(ability_cast, npc_appears, entity_death, timer_expires, hp_below) and cover
27 of Styrak's 32 real timers. The rest (And/Or/VariableCheck, TriggerType
11/12/17) use nested "Clause1"/"Clause2" sub-timer objects implementing a
small variable-tracking state machine (increment/compare counters) that
doesn't map onto our all_of/any_of/counter_* primitives without real design
work -- skipped and reported this pass rather than approximated wrong.
Every skip is recorded, never silently dropped, matching translate_baras_toml.py's
convention.
"""
import json
import re
import uuid
from pathlib import Path
from typing import List, Optional

TOOLS_DIR = Path(__file__).resolve().parent
OUT_DIR = TOOLS_DIR.parent / "boss_definitions_bundled"

SUPPORTED_TRIGGER_TYPES = {2, 14, 16, 6, 1}  # AbilityUsed, NewEntitySpawn, EntityDeath, TimerExpired, EntityHP

skipped = []  # (context, reason)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _entity_selector(value: Optional[str]) -> Optional[List[str]]:
    """ORBS's Source/Target fields are either a literal NPC id string, or a
    sentinel ("Any", "Ignore", "Local", None) meaning "not a specific
    entity" -- only the former translates to a selector list."""
    if not value or value in ("Any", "Ignore", "Local"):
        return None
    return [value]


def translate_timer(t: dict, id_prefix: str, uuid_to_ourid: dict, ctx_prefix: str):
    """Returns (our_id, translated_dict) or (our_id, None) if this timer's
    trigger type/shape isn't in the supported subset. Always returns an
    our_id (even for skipped timers) so sibling timers referencing this
    one's ORBS uuid via ExperiationTimerId can detect "points at something
    we dropped" and skip cleanly too, instead of guessing."""
    orbs_id = t["Id"]
    our_id = f"{id_prefix}_{_slugify(t.get('Name') or orbs_id)}"
    uuid_to_ourid[orbs_id] = our_id
    ctx = f"{ctx_prefix} timer '{t.get('Name')}' ({orbs_id})"

    if t.get("Clause1") or t.get("Clause2"):
        skipped.append((ctx, "compound And/Or/VariableCheck timer (nested clauses) -- not supported this pass"))
        return our_id, None

    trigger_type = t.get("TriggerType")
    if trigger_type not in SUPPORTED_TRIGGER_TYPES:
        skipped.append((ctx, f"TriggerType {trigger_type} not in the supported subset this pass"))
        return our_id, None

    trigger = None
    if trigger_type == 2:  # AbilityUsed
        keyword = t.get("Ability") or t.get("Effect")
        if not keyword:
            skipped.append((ctx, "AbilityUsed with no Ability/Effect text"))
            return our_id, None
        trigger = {"type": "ability_cast", "keyword": keyword}
    elif trigger_type == 14:  # NewEntitySpawn
        selector = _entity_selector(t.get("Source"))
        if not selector:
            skipped.append((ctx, "NewEntitySpawn with no specific Source npc id"))
            return our_id, None
        trigger = {"type": "npc_appears", "selector": selector}
    elif trigger_type == 16:  # EntityDeath
        selector = _entity_selector(t.get("Source")) or _entity_selector(t.get("Target"))
        if not selector:
            skipped.append((ctx, "EntityDeath with no specific Source/Target npc id"))
            return our_id, None
        trigger = {"type": "entity_death", "selector": selector}
    elif trigger_type == 6:  # TimerExpired
        ref_orbs_id = t.get("ExperiationTimerId")
        ref_our_id = uuid_to_ourid.get(ref_orbs_id)
        if not ref_our_id:
            skipped.append((ctx, "TimerExpired referencing a timer not yet translated (forward ref) or dropped"))
            return our_id, None
        trigger = {"type": "timer_expires", "timer_id": ref_our_id}
    elif trigger_type == 1:  # EntityHP
        selector = _entity_selector(t.get("Target"))
        out_trigger = {"type": "hp_below", "percent": t.get("HPPercentage", 0.0)}
        if selector:
            out_trigger["selector"] = selector
        trigger = out_trigger

    out = {
        "id": our_id,
        "label": t.get("AlertText") or t.get("Name") or our_id,
        "duration_seconds": t.get("DurationSec") or 10.0,
        "trigger": trigger,
    }
    if t.get("IsAlert"):
        out["is_alert"] = True
    return our_id, out


DIFFICULTY_SUFFIX_RE = re.compile(
    r"\s+(\d+m|VM|SM|MM|HM|NiM|Story|Veteran|Master)$", re.IGNORECASE
)


def _difficulty_base_name(name: str) -> str:
    """Strips trailing difficulty-mode tokens (repeatedly, since ORBS
    sometimes stacks two -- "Vomit Pool 8m VM") to find the real mechanic
    name underneath. Only strips KNOWN difficulty tokens -- an ordinal like
    "1st" is left alone, since that's a genuinely different trigger
    mechanism (predictive npc_appears vs. reactive ability_cast), not a
    duplicate of the same one."""
    prev = None
    while prev != name:
        prev = name
        name = DIFFICULTY_SUFFIX_RE.sub("", name)
    return name.strip()


def _dedupe_difficulty_variants(rows: List[dict], names: dict, ctx_prefix: str) -> List[dict]:
    """ORBS keeps separate timer entries per difficulty for the same real
    mechanic (e.g. "Vomit Pool 16m" / "Vomit Pool 8m VM" / "Vomit Pool 8m"),
    each with a different assumed duration -- our engine has no difficulty
    filtering, so translating all of them verbatim makes one real cast
    announce 2-3 times. Collapses entries that share BOTH an identical
    trigger and the same mechanic name once difficulty suffixes are
    stripped, keeping the LONGEST duration (a countdown running a little
    long is safer than one that expires while the real hazard is still up)."""
    groups = {}
    for row in rows:
        tr = row["trigger"]
        trigger_key = (tr["type"], tr.get("keyword") or tuple(tr.get("selector", ())) or tr.get("timer_id"))
        base_name = _difficulty_base_name(names[row["id"]])
        groups.setdefault((trigger_key, base_name), []).append(row)

    out = []
    for (trigger_key, base_name), group in groups.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        kept = max(group, key=lambda r: r["duration_seconds"])
        out.append(kept)
        dropped_ids = [r["id"] for r in group if r is not kept]
        skipped.append((
            ctx_prefix,
            f"collapsed {len(group)} difficulty variants of '{base_name}' "
            f"({trigger_key}) into {kept['id']} ({kept['duration_seconds']}s, "
            f"longest) -- dropped {dropped_ids}, no difficulty filtering in our engine",
        ))
    return out


def translate_source_block(source: dict, id_prefix: str, ctx_prefix: str) -> List[dict]:
    """source: one {"TimerSource": ..., "Timers": [...]} block for a single
    boss, in ORBS's own trigger-order (needed since TimerExpired can only
    resolve a same-pass ExperiationTimerId reference to an EARLIER timer in
    the list -- a forward reference is reported and dropped rather than
    reordered/guessed at)."""
    uuid_to_ourid = {}
    out = []
    names = {}  # our_id -> ORBS's original Name, for difficulty-suffix stripping
    for t in source.get("Timers", []):
        our_id, translated = translate_timer(t, id_prefix, uuid_to_ourid, ctx_prefix)
        if translated is not None:
            out.append(translated)
            names[our_id] = t.get("Name") or our_id
    return _dedupe_difficulty_variants(out, names, ctx_prefix)


def find_boss_source(path: Path, boss_name: str) -> Optional[dict]:
    """Finds the {"TimerSource": "<Encounter>|<boss_name>", ...} block for
    boss_name within one of ORBS's per-raid JSON files. Matches on the
    suffix after '|' since TimerSource is "Encounter|Boss", and a raid file
    can bundle unrelated encounters (Scum and Villainy's file includes
    Dread Master Styrak alongside its own bosses)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data:
        if entry.get("TimerSource", "").split("|")[-1] == boss_name:
            return entry
    return None
