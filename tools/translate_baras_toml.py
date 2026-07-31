"""
Translates BARAS's real TOML encounter definitions into our JSON boss schema.

Key semantic gap this bridges: BARAS matches abilities/effects by numeric ID
(e.g. `effects = [4471271408533778]`); our engine matches by keyword TEXT
parsed from the log. id_to_names.json (built by scanning the user's entire
5.4M-line real log corpus) resolves each numeric ID to its real, currently-
verified display name. IDs that never appeared in the corpus get an explicit
placeholder (never a guess), matching this project's existing convention
(see boss_definitions_bundled/pylons.json).

Condition types BARAS uses that our engine has NO equivalent for are
skipped/simplified rather than approximated incorrectly:
  - ability / target / phase (challenge-only conditions -- we don't model
    "challenges" at all, so anything only reachable via those is dropped)
  - target_set, timer_canceled, timer_started, damage_taken, threat_modified
  - shields (boss.entities.shields) -- absorb/shield tracking, documented
    as a pre-existing "not translated" limitation
  - roles, difficulties, icon_ability_id, color, display_target,
    can_be_refreshed, show_at_secs -- cosmetic/filtering metadata with no
    functional equivalent in our engine
Every drop is recorded and reported, not silently swallowed.
"""
import json
import re
import tomllib
from pathlib import Path
from typing import List, Optional

TOOLS_DIR = Path(__file__).resolve().parent
OUT_DIR = TOOLS_DIR.parent / "boss_definitions_bundled"

# Two name tables, deliberately kept separate by provenance:
#   id_to_names  -- resolved from REAL logs, i.e. the exact text the game
#                   client wrote. Authoritative, because that's what our
#                   keyword matching actually compares against.
#   fallback_names -- BARAS's own English display labels (attack_types.csv /
#                   icons.csv). Broad coverage, but a display label is not
#                   guaranteed to equal the logged text: BARAS lists the R-4
#                   boss as "Kanoth" while the log says "Lord Kanoth". Used
#                   only where the log corpus has no entry at all, and the
#                   result is reported as unverified.
# Where both know an id, they agree 5,314/5,321 (99.87%) -- the exceptions
# are all cosmetic toy renames, not combat abilities.
id_to_names = json.loads(
    (TOOLS_DIR / "ability_id_names.json").read_text(encoding="utf-8")
)
_fallback_path = TOOLS_DIR / "baras_ability_names.json"
fallback_names = (
    json.loads(_fallback_path.read_text(encoding="utf-8"))
    if _fallback_path.exists() else {}
)

# ids resolved only via the CSV fallback, so callers can report them as
# "real BARAS data, but not confirmed against a live log"
fallback_resolved_ids = set()

unresolved_ids = set()
dropped_conditions = []  # (context, reason)


def resolve(entity_id) -> str:
    """Log-verified name if we have one, else BARAS's display label, else an
    inert placeholder. Never guesses."""
    key = str(entity_id)
    names = id_to_names.get(key)
    if names:
        # prefer the shortest clean name (avoids any stray artifacts)
        return sorted(names, key=len)[0]
    names = fallback_names.get(key)
    if names:
        fallback_resolved_ids.add(key)
        return sorted(names, key=len)[0]
    unresolved_ids.add(key)
    return f"REPLACE_WITH_REAL_ABILITY_NAME_{key}"


def resolve_first(ids) -> Optional[str]:
    """Resolves the first id that has a known name, else returns an inert
    placeholder keyed to the first id. Returns None for an empty/missing id
    list, which the caller must treat as untranslatable -- returning a bare
    placeholder there would be indistinguishable from a real-but-unknown
    ability."""
    if isinstance(ids, int):
        ids = [ids]
    if not ids:
        return None
    # Prefer an id the real logs know, then one BARAS's tables know, and only
    # fall back to a placeholder if no id in the list resolves at all.
    for table in (id_to_names, fallback_names):
        for i in ids:
            if str(i) in table:
                return resolve(i)
    return resolve(ids[0])


UNSUPPORTED_TYPES = {
    # Challenge-only conditions (we don't model challenges at all)
    "ability", "target", "phase",
    # No engine equivalent yet
    "target_set", "timer_canceled", "damage_taken", "threat_modified",
    "counter", "manual", "effect",
}


def translate_condition(c, ctx: str):
    """Returns our Condition dict, or None if unsupported/malformed."""
    if c is None:
        return None
    try:
        return _translate_condition_inner(c, ctx)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        dropped_conditions.append((
            ctx,
            f"condition type '{c.get('type')}' has a shape the translator "
            f"doesn't handle ({type(e).__name__}: {e})"
        ))
        return None


def _translate_condition_inner(c, ctx: str):
    t = c.get("type")

    if t in UNSUPPORTED_TYPES:
        dropped_conditions.append((ctx, f"unsupported condition type '{t}'"))
        return None

    if t == "combat_start":
        return {"type": "combat_start"}

    if t == "combat_end":
        return {"type": "combat_end"}

    if t == "any_phase_change":
        return {"type": "any_phase_change"}

    if t == "timer_started":
        return {"type": "timer_started", "timer_id": c["timer_id"]}

    if t == "timer_time_remaining":
        return {
            "type": "timer_time_remaining", "timer_id": c["timer_id"],
            "operator": c.get("operator", "gte"), "value": c.get("value", 0),
        }

    if t == "counter_changes":
        return {"type": "counter_changes", "counter_id": c["counter_id"]}

    if t == "not":
        inner = translate_condition(c.get("condition"), ctx + " not()")
        if inner is None:
            dropped_conditions.append((ctx, "not() wrapping an untranslatable condition"))
            return None
        return {"type": "not", "condition": inner}

    if t == "boss_hp_below":
        out = {"type": "hp_below", "percent": c["hp_percent"]}
        if c.get("selector"):
            out["selector"] = c["selector"]
        return out

    if t == "ability_cast":
        keyword = resolve_first(c.get("abilities"))
        if keyword is None:
            dropped_conditions.append((ctx, "ability_cast with no ability ids to match on"))
            return None
        return {"type": "ability_cast", "keyword": keyword}

    if t in ("effect_applied", "effect_removed"):
        keyword = resolve_first(c.get("effects"))
        if keyword is None:
            # e.g. counters using a sibling `track_effect_stacks` table --
            # stack-count tracking has no equivalent in our counter engine
            # (ours only does discrete increment/decrement/reset on events)
            dropped_conditions.append((ctx, f"{t} with no 'effects' list (likely track_effect_stacks -- stack counting unsupported)"))
            return None
        out = {"type": t, "keyword": keyword}
        if c.get("target") == "local_player":
            out["target"] = "local_player"
        return out

    if t == "npc_appears":
        return {"type": "npc_appears", "selector": c.get("selector", [])}

    if t == "entity_death":
        return {"type": "entity_death", "selector": c.get("selector", [])}

    if t == "timer_expires":
        return {"type": "timer_expires", "timer_id": c["timer_id"]}

    if t == "phase_ended":
        return {"type": "phase_ended", "phase_id": c["phase_id"]}

    if t == "phase_entered":
        return {"type": "phase_entered", "phase_id": c["phase_id"]}

    if t == "phase_active":
        return {"type": "phase_active", "phase_ids": c.get("phase_ids", [])}

    if t == "counter_compare":
        return {
            "type": "counter_compare", "counter_id": c["counter_id"],
            "operator": c["operator"], "value": c["value"],
        }

    if t == "counter_reaches":
        return {"type": "counter_reaches", "counter_id": c["counter_id"], "value": c["value"]}

    if t in ("any_of", "all_of"):
        raw_subs = c.get("conditions", [])
        subs = [translate_condition(sc, ctx) for sc in raw_subs]
        kept = [s for s in subs if s is not None]
        if not kept:
            dropped_conditions.append((ctx, f"{t} with no translatable branches"))
            return None
        if t == "all_of" and len(kept) != len(raw_subs):
            # An AND-gate that lost a branch becomes WEAKER (fires in cases it
            # shouldn't), unlike any_of which merely becomes narrower. Refuse
            # rather than silently loosen the trigger.
            dropped_conditions.append(
                (ctx, "all_of lost a branch to an untranslatable condition -- "
                      "dropped whole gate rather than weakening it")
            )
            return None
        if len(kept) == 1:
            return kept[0]
        return {"type": t, "conditions": kept}

    dropped_conditions.append((ctx, f"unrecognized condition type '{t}'"))
    return None


def translate_conditions_list(conds, ctx: str):
    out = []
    for c in conds or []:
        tc = translate_condition(c, ctx)
        if tc is not None:
            out.append(tc)
        else:
            dropped_conditions.append((ctx, "dropped an extra AND-gate condition"))
    return out


def translate_phase(p, ctx_prefix: str, is_first: bool):
    ctx = f"{ctx_prefix} phase {p['id']}"
    out = {"id": p["id"], "name": p.get("name", p["id"])}
    if not is_first:
        # some bosses (e.g. apex_vanguard) use `trigger` as an alias for
        # `start_trigger` on phases -- same semantic, just a schema variant
        raw_trigger = p.get("start_trigger") or p.get("trigger")
        trig = translate_condition(raw_trigger, ctx)
        if trig is not None:
            out["start_trigger"] = trig
        else:
            dropped_conditions.append((ctx, "phase has no translatable start_trigger -- WILL NEVER FIRE"))
            out["start_trigger"] = {"type": "ability_cast", "keyword": "REPLACE_WITH_REAL_ABILITY_NAME_untranslatable_trigger"}
    if p.get("end_trigger"):
        et = translate_condition(p["end_trigger"], ctx)
        if et is not None:
            out["end_trigger"] = et
    conds = translate_conditions_list(p.get("conditions"), ctx)
    if conds:
        out["conditions"] = conds
    return out


def translate_counter(c, ctx_prefix: str):
    ctx = f"{ctx_prefix} counter {c['id']}"
    out = {"id": c["id"], "name": c.get("name", c["id"])}
    if "initial_value" in c:
        out["initial_value"] = c["initial_value"]
    inc = c.get("increment_on")
    if inc:
        tc = translate_condition(inc, ctx + " increment_on")
        if tc is not None:
            out["increment_on"] = tc
    dec = c.get("decrement_on")
    if dec:
        tc = translate_condition(dec, ctx + " decrement_on")
        if tc is not None:
            out["decrement_on"] = tc
    rst = c.get("reset_on")
    if rst:
        tc = translate_condition(rst, ctx + " reset_on")
        if tc is not None:
            out["reset_on"] = tc
    return out


def translate_timer(t, ctx_prefix: str):
    ctx = f"{ctx_prefix} timer {t['id']}"
    trigger_src = t.get("trigger")
    trig = translate_condition(trigger_src, ctx + " trigger") if trigger_src else None
    if trig is None:
        dropped_conditions.append((ctx, "timer has no translatable trigger -- DROPPED ENTIRELY"))
        return None

    out = {
        "id": t["id"],
        "label": t.get("display_text") or t.get("alert_text") or t.get("name", t["id"]),
        "duration_seconds": t.get("duration_secs", 10.0),
        "trigger": trig,
    }
    if t.get("alert_on") == "countdown" and t.get("alert_countdown_secs"):
        out["warn_seconds_before"] = t["alert_countdown_secs"]
    if t.get("is_alert"):
        out["is_alert"] = True

    conds = translate_conditions_list(t.get("conditions"), ctx)
    if conds:
        out["conditions"] = conds

    if t.get("cancel_trigger"):
        ct = translate_condition(t["cancel_trigger"], ctx + " cancel_trigger")
        if ct is not None:
            out["cancel_trigger"] = ct
        else:
            dropped_conditions.append((ctx, "cancel_trigger unsupported -- timer will run full duration uncancelled"))

    return out


def resolve_boss_names(b):
    """Derives the entity name(s) this boss is recognized by, resolving each
    boss entity's numeric `ids` through the real-log corpus.

    This matters more than it looks: the TOML's entity `name` is BARAS's own
    display label and does NOT always equal what the game logs. Lord Kanoth
    is listed as "Kanoth" but logs as "Lord Kanoth" -- matching on the TOML
    label would never recognize the fight. Resolving via ids uses the name the
    client actually wrote.

    Returns (names, unverified_toml_names). Names resolved from the corpus are
    trustworthy; anything in the second list is a TOML label we could not
    confirm, included as a best-effort fallback and reported to the caller.
    """
    resolved, unverified = [], []
    for e in b.get("entities", []):
        if not e.get("is_boss"):
            continue
        names = set()
        for i in e.get("ids", []):
            key = str(i)
            if key in id_to_names:
                names.update(id_to_names[key])
        if names:
            resolved.extend(sorted(names))
        elif e.get("name"):
            unverified.append(e["name"])
    # de-dupe, preserve order
    seen, out = set(), []
    for n in resolved + unverified:
        if n not in seen:
            seen.add(n); out.append(n)
    return out, unverified


def translate_boss(b, real_boss_names=None):
    ctx_prefix = f"[{b['id']}]"
    if real_boss_names is None:
        real_boss_names, unverified = resolve_boss_names(b)
        if unverified:
            dropped_conditions.append((
                ctx_prefix,
                f"boss entity name(s) {unverified} could not be resolved from the log "
                f"corpus -- using BARAS's own label as a best-effort fallback, which "
                f"may not match what the game actually logs (verify against a real log)"
            ))
        if not real_boss_names:
            dropped_conditions.append((
                ctx_prefix, "NO boss entity names at all -- this fight can never be recognized"
            ))
    phases_src = b.get("phases", [])
    phases = [translate_phase(p, ctx_prefix, i == 0) for i, p in enumerate(phases_src)]
    counters = [translate_counter(c, ctx_prefix) for c in b.get("counters", [])]
    timers = [translate_timer(t, ctx_prefix) for t in b.get("timer", [])]
    timers = [t for t in timers if t is not None]

    if any(e.get("shields") for e in b.get("entities", [])):
        dropped_conditions.append((ctx_prefix, "boss has shield/absorb mechanics -- not modeled (pre-existing limitation)"))

    # The authoritative recognition key: the game's NPC type ids. Display
    # names collide across encounters (Dread Council vs the solo Dread
    # Masters), these don't. boss_names is kept as a human-readable label and
    # as the fallback for any definition without ids.
    boss_npc_ids = []
    for e in b.get("entities", []):
        if e.get("is_boss"):
            boss_npc_ids.extend(str(i) for i in e.get("ids", []))

    return {
        "id": b["id"],
        "name": b["name"],
        "boss_names": real_boss_names,
        "boss_npc_ids": sorted(set(boss_npc_ids)),
        "phases": phases,
        "counters": counters,
        "timers": timers,
    }


def run_operation(toml_path: Path, boss_filter: dict, out_dir: Path):
    """boss_filter: {baras_boss_id: (our_filename, [real boss_names] or None)}
    Pass None for the names to auto-resolve them from the entity ids."""
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    written = []
    for b in data["boss"]:
        if b["id"] not in boss_filter:
            continue
        fname, real_names = boss_filter[b["id"]]
        translated = translate_boss(b, real_names)
        out_path = out_dir / fname
        out_path.write_text(json.dumps(translated, indent=2), encoding="utf-8")
        written.append((b["id"], out_path, len(translated["timers"]), len(translated["phases"])))
    return written
