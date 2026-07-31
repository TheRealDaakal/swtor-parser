# tools/

Helpers for adding more boss definitions from BARAS's real encounter data.
Not needed to *run* the parser — only to extend it.

## Why these exist

BARAS matches abilities/effects by **numeric id**
(`effects = [4471271408533778]`); this project's engine matches by **keyword
text** parsed out of the log line. Bridging that is the whole job:

- `ability_id_names.json` — **authoritative** id → name table (6,561 ids)
  built by scanning a real 5.4M-line SWTOR log corpus. These are names the
  game client actually logged, which is exactly what keyword matching
  compares against.
- `baras_ability_names.json` — **fallback** table (34,279 ids) from BARAS's
  `core/data/attack_types.csv` + `icons/icons.csv`. Broad coverage, but these
  are display labels and a label isn't guaranteed to equal logged text —
  BARAS lists the R-4 boss as "Kanoth" while the log says "Lord Kanoth".
  Used only where the corpus has no entry. Where both tables know an id they
  agree 5,314/5,321 (99.87%); the 7 exceptions are cosmetic toy renames.
- `translate_baras_toml.py` — converts BARAS's
  `core/definitions/encounters/*.toml` into this project's JSON boss schema,
  resolving ids through those tables (corpus first, then fallback, then an
  inert placeholder — never a guess).

Boss *recognition* uses `boss_npc_ids` (the game's NPC type ids) rather than
names, because display names collide across encounters — the Dread Council
and the solo Dread Masters log identical names under different ids. The
translator emits those ids automatically.

## Current coverage

All 53 BARAS encounter files are already translated: **63 definitions, 188
phases, 944 timers**. Of BARAS's 236 boss entries, 175 have no timers at all
(they exist only so BARAS can split encounters), so 61 was the real target.

Re-run this after BARAS updates, or to fill in placeholders once you've
raided content that was previously unlogged.

## Re-translating / adding an operation

1. Grab the TOML(s):

```bash
curl -sLO https://raw.githubusercontent.com/baras-app/baras/master/core/definitions/encounters/operations/dread_fortress.toml
```

2. Pass `None` for the names to auto-resolve them from the entity ids
   (recommended — the TOML's `name` field is a display label and doesn't
   always match what the game logs):

```python
from pathlib import Path
from translate_baras_toml import run_operation, unresolved_ids, dropped_conditions

run_operation(
    Path("dread_fortress.toml"),
    {"nefra": ("nefra.json", None)},
    Path("../boss_definitions_bundled"),
)
print(unresolved_ids)       # ids with no known name -> inert placeholders
print(dropped_conditions)   # anything not translatable, with the reason
```

   Do not overwrite the 12 hand-written definitions (`writhing_horror`,
   `red`, `colonel_vorgath`, `soa`, `xrr3`, `gharj`, `zorn_toth`,
   `infernal_council`, `breach`, `holding_pens`, `pylons`, `test_dummy`) —
   they predate the translator and have no id data in BARAS's files.

3. **Read the two reports before trusting the output.** `unresolved_ids`
   become `REPLACE_WITH_REAL_ABILITY_NAME_<id>` placeholders — inert, so they
   never false-match, but the trigger won't fire until filled in.
   `dropped_conditions` explains every construct that couldn't be
   represented. Watch for two severities in particular:
   `WILL NEVER FIRE` (a phase lost its trigger) and `DROPPED ENTIRELY`
   (a timer lost its trigger).

4. Replay a real log through the engine to confirm the boss is actually
   recognized and its phases advance sensibly. Loading without an exception
   only proves the JSON is well-formed, not that it works.

## Known translation gaps

Conditions BARAS supports that this engine does not, dropped explicitly
rather than approximated (with how often they appear across all 53 files):
`damage_taken` (30), `target_set` (10), `threat_modified` (2),
`timer_canceled` (1), `track_effect_stacks` (effect-stack counting), plus
challenges, shields, and role/difficulty scoping.

Already implemented, so no longer gaps: `timer_started`, `combat_end`,
`timer_time_remaining`, `not`, `counter_changes`, `any_phase_change`,
`all_of`.

Note one deliberate asymmetry: an `any_of` that loses an untranslatable
branch is kept (it only gets narrower), but an `all_of` that loses a branch
is dropped entirely — a weakened AND-gate fires in cases it shouldn't, which
is worse than not firing.

## License

The translated data originates from [BARAS](https://github.com/baras-app/baras),
MIT licensed, Copyright (c) 2025 The BARAS Authors.
