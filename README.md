# SWTOR Live Parser

A lightweight, BARAS-style desktop combat log parser for SWTOR. Watches your
CombatLogs folder live and shows DPS, HPS, damage taken, and deaths per
player in an always-on-top window while you raid.

## Requirements

- Python 3.8+ (tkinter is included with standard Python on Windows)
- `pip install requests` — needed for Parsely uploads
- SWTOR combat logging enabled: in-game, press **Ctrl+P** → Preferences →
  Combat Logging → check "Enable combat logging to file"
- Optional, for spoken (not just beeped) timer alerts:
  `pip install pyttsx3`

## Run it

```
python main.py
```

It auto-detects the default log folder:
`Documents\Star Wars - The Old Republic\CombatLogs`

If it can't find it (custom install location), pass the path directly:

```
python main.py "D:\Games\SWTOR\CombatLogs"
```

## Corpus analytics (the part that isn't a BARAS clone)

```
python -m analysis.webapp
```

Opens a local dark-themed web UI at `http://127.0.0.1:8765` that reads your
**entire** CombatLogs folder as one dataset — every session, not just the
current one. Longitudinal DPS/HPS trends per boss, pull history, and death
forensics that reconstruct what actually killed each player. See
[`analysis/README.md`](analysis/README.md).

This is the deliberate point of difference: BARAS and StarParse answer "what
happened in this pull?" very well, and their data model is per-session. Over
209 real log files (5.4M lines) this found 91 sessions, ~2,000 encounters and
28 distinct bosses spanning two months — and surfaced three parser bugs that
per-session testing had missed.

## How it works

- `log_watcher.py` finds the newest log file in the folder and tails it,
  switching automatically to a new file whenever SWTOR starts one (each
  login/relog creates a new log).
- `log_parser.py` tokenizes each line generically by bracket/paren groups
  instead of assuming fixed field positions — this is the part most likely
  to need tweaking if a game patch changes the log layout. It also detects
  explicit EnterCombat/ExitCombat markers.
- `stats.py` aggregates parsed events into a live encounter, ending it on
  ExitCombat (falling back to an 8s inactivity gap if that marker isn't
  seen). Like StarParse, damage/heal events up to 4s after ExitCombat still
  count toward the fight that just ended, so trailing DoT/HoT ticks aren't
  lost or misattributed to the next pull. It also tracks a per-ability
  breakdown of damage/healing for every player, not just totals.
- `audio.py` provides the alert sounds: a beep via `winsound` on Windows
  (terminal bell fallback elsewhere), and spoken alerts via `pyttsx3` if
  it's installed (`pip install pyttsx3`) — falls back to a beep if not.
- `timers.py` is an ORBS-style custom timer engine: define a keyword (matched
  against an ability/effect name), a label, and a duration, and it tracks a
  live countdown from the moment that trigger is seen. Each rule can speak
  its label out loud the moment it fires, and optionally fire a second
  spoken/beeped warning a configurable number of seconds before the
  countdown ends. Timers can also self-repeat: after first expiring, a
  timer with a repeat interval and count re-arms itself that many more
  times instead of vanishing (e.g. "spawns every 90s, up to 10 times"),
  each re-arm re-announcing the label -- used for Writhing Horror's
  recurring Jelly Male add spawns.
- `storage.py` persists timer rules and completed-encounter history to JSON
  under `%APPDATA%\swtor-parser\` (Windows) or `~/.swtor-parser/` (other),
  so both survive closing and reopening the app.
- `log_merger.py` lets you import saved `.txt` log files (e.g. copies from
  teammates) and merges them into one combined encounter. It de-duplicates
  overlapping events (same timestamp/source/target/ability/amount) across
  files, since SWTOR logs already record the whole group's actions — this
  is for filling visibility gaps between clients, not required for normal
  single-log use.
- `parsely_upload.py` uploads to parsely.io — either the whole current log
  file, or just one pull's line range (with the AreaEntered line prepended
  for zone context and a 5-second trailing window for late damage/death
  events). This mirrors BARAS's own real implementation, confirmed from its
  open-source Rust code — same endpoint, same multipart form fields, same
  gzip compression, same XML response format — rather than a guess at an
  undocumented API.
- `boss_definitions.py` + `boss_intelligence.py` are a data-driven boss/phase
  engine, in the same spirit as BARAS's own approach (per their docs: bosses
  are configured via definition files, not hardcoded, so a new boss doesn't
  need a code change). A JSON file per boss lists the entity name(s) that
  identify the fight, phases, counters, and timers. All of it is expressed
  through one shared `Condition` vocabulary: `combat_start`, `hp_below`
  (edge-triggered — fires once per threshold per pull, not continuously for
  the rest of the fight once HP drops past it), `ability_cast`,
  `effect_applied`/`effect_removed` (optionally restricted to
  `"target": "local_player"` — best-effort guess at which entity is you,
  same heuristic BARAS itself uses), `npc_appears`, `entity_death`,
  `counter_compare`/`counter_reaches`, `phase_ended`/`phase_entered`/
  `phase_active` (the last one gates a trigger to only fire from a specific
  current phase — useful when two phases share an unavoidably-guessed
  trigger and need disambiguating some other way), `timer_expires` (a named
  timer chains into a different one when it finally expires), and `any_of`
  (branching). Timers can self-repeat (re-arm N more times on a fixed
  interval) and can have a `cancel_trigger` that removes them early when
  some other condition makes them moot — give the timer an `id` or this
  silently does nothing; the loader now warns at load time if you forget.
  A phase can cycle back to one it already left, but only if it declares
  its own `end_trigger`, OR another phase's trigger fires independently of
  that signal (in which case the old phase is implicitly reported as
  ended for anything else watching `phase_ended` on it) — this two-path
  model is deliberate: it's what correctly distinguishes Red's Bull phase
  (only ends via its own explicit death-triggered `end_trigger`) from Soa's
  platform phases (each one just implicitly ends the moment the next one's
  own unrelated trigger fires, no `end_trigger` needed on the one before).
  Eleven definitions ship: a synthetic `test_dummy.json` proving the basic
  mechanics, and ten **real** ones translated directly from BARAS's own
  open-source definition data — `writhing_horror.json` and `red.json`
  (self-repeat and timer chaining), `colonel_vorgath.json` (`cancel_trigger`
  plus the HP-threshold fix), `soa.json` (implicit vs. explicit
  `phase_ended`, and `phase_active` disambiguating two phases that share a
  guessed trigger keyword), the simpler `xrr3.json`/`gharj.json`
  (single-phase bosses, pure HP-threshold and ability-cast alerts), and
  four sub-encounters: `infernal_council.json` (trivial — no timers needed,
  by BARAS's own comment), `breach.json`/`holding_pens.json` (recognized by
  a specific non-generic mob name, same as any other boss), and
  `pylons.json`, which needed a real engine addition — `encounter_trigger`
  lets a definition recognize a fight by a specific ability being cast
  instead of by entity name, for cases where the only named entity (here,
  literally "Adds") would be too generic to trust; Pylons' actual trigger
  ability has no textual hint anywhere in the data seen so far (pure
  numeric IDs), so the keyword is left as an obvious placeholder
  (`REPLACE_WITH_REAL_ABILITY_NAME`) rather than a fabricated guess — it
  won't falsely recognize anything, it just won't recognize Pylons either
  until a real log line or ID lookup fills it in. Most of the earlier
  keyword guesses have since been checked against BARAS's own
  ability-ID-to-name table: `Corrosive Slime`, `Acidic Jet`,
  `Leaping Smash`, and `Pulverizing Slam` all matched exactly and are now
  confirmed correct, not guesses. One was actually wrong and got fixed
  from this: Writhing Horror's "Adds Spawn" timer had been keyed to the
  descriptive label BARAS uses for display, not the real logged effect
  name (`Incubation`) — it would never have fired as originally written.
  `Hydrochloric Pool` is still an unverified guess (that specific ability
  wasn't in the table checked), and Pylons'/Holding Pens' interaction
  abilities weren't in it either — that table appears to cover
  damage-dealing combat abilities specifically, not environment/interaction
  ones. Not translated: per-difficulty (story/veteran/master) scoping and
  shield/absorb tracking. User-added definitions go in `boss_definitions/`
  under the same data folder as history/timer rules and override bundled
  ones with the same id.

  **51 more definitions were later added by machine-translating all 53 of
  BARAS's encounter files** (`core/definitions/encounters/`) rather than by
  hand — see `tools/` for the translator and how to re-run it. Two things
  made this reliable rather than guesswork:

  1. BARAS matches abilities by **numeric id**, we match by keyword text. So
     every id was resolved through an id→name table: first a real 5.4M-line
     log corpus (authoritative — the text the client actually wrote), then
     BARAS's own `attack_types.csv`/`icons.csv` labels for ids the corpus
     never saw. Ids in neither get an explicit
     `REPLACE_WITH_REAL_ABILITY_NAME_<id>` placeholder (inert, never a false
     match), same convention as `pylons.json`. Nothing is ever guessed.
  2. The result was verified by replaying a real R-4 Anomaly raid log with
     all 63 definitions loaded: all four R-4 bosses are recognized and their
     phases advance in the expected order (IP-CPT main→Bombs→Burn, Watchdog
     main→Stalker→Polarized Beam, Kanoth phase transitions, Dominique
     Aria→Generators→Burn across multiple pulls) — and, importantly, loading
     63 definitions instead of 19 produced *identical* results, i.e. the 44
     added definitions cause no false recognition.

  Conditions BARAS supports that this engine does not are **dropped
  explicitly, not approximated**. A frequency count over all 53 encounter
  files drove which ones were worth building: `timer_started` (126 uses),
  `combat_end` (64), `timer_time_remaining` (51), `not` (21),
  `counter_changes` (16), `any_phase_change` (11) and `all_of` (6) are now
  **implemented** — about 80% of the gap. Still unsupported:
  `damage_taken` (30), `target_set` (10), `timer_canceled` (1),
  `threat_modified` (2), effect-stack counting (`track_effect_stacks`), plus
  challenges, shields, and role/difficulty scoping. Where a `cancel_trigger`
  couldn't be translated the timer runs its full duration instead of
  cancelling early; where a timer's *own* trigger couldn't be translated the
  timer is dropped outright rather than left to fire wrongly.

  One deliberate asymmetry in the translator: an `any_of` that loses a branch
  is merely narrower, so it's kept; an `all_of` that loses a branch becomes
  *weaker* (it would fire in cases it shouldn't), so the whole gate is
  dropped rather than silently loosened.

- **Boss recognition is by NPC type id, not name** (`boss_npc_ids`), which is
  what BARAS matches on. This isn't cosmetic: display names are not unique.
  The Dread Council fight and the four solo Dread Master fights log
  *identical* names under different type ids, and R-4's Watchdog appears both
  as its own boss and as an add in the Lady Dominique fight — six real
  cross-encounter collisions that name matching resolved arbitrarily by
  filename order. With ids there are zero collisions. `boss_names` remains as
  a readable label and as the fallback for the 12 older hand-written
  definitions, which have no id data available.
- `cooldowns.py` tracks personal defensive cooldowns (Adrenaline Rush,
  Deflection, Energy Shield, Saber Ward, and 11 others), translated from
  BARAS's own `core/definitions/effects/dcds.toml` — verified as the
  *complete* set of 15 defensives in that file, plus a raid-wide combat-rez
  cooldown. Not boss-scoped, so they track in trash fights too, not just
  pulls. Personal cooldowns are scoped to the detected local player, so a
  teammate's same-named defensive can't hijack your display; the combat rez
  is deliberately raid-wide.
- `dots_hots.py` tracks your own DoT/HoT uptime (37 DoTs, 8 HoTs),
  translated from BARAS's sibling `dots.toml`/`hots.toml` and shown in
  their own Live-tab panel. Two rules per ability: a long cooldown timer
  and a short "actively up" buff timer. Most start their cooldown the
  instant you cast (matching most of BARAS's own data); Adrenaline Rush and
  Kolto Overload instead start their cooldown when the buff *wears off*,
  which needed a real fix to support — the buff timer is keyed specifically
  to the effect being applied, not just the ability name appearing in the
  log at all, so the same name showing up again in the log line that ends
  the buff can't spuriously restart it. Not translated: `cooldown_ready_secs`
  (a distinct "ready" glow after the cooldown ends) and alacrity-scaled
  durations — both real BARAS features, not implemented here yet.
- `gui.py` / `main.py` tie it together in five tabs — **Live** (the meter,
  current boss/phase status, active timer countdowns, plus separate
  **Cooldowns** and **DoTs / HoTs** panels — double-click a player for
  their ability breakdown), **History** (past pulls, persisted across
  restarts, double-click for per-player detail and a Parsely upload
  button), **Timers** (configure timer rules), **Import Logs** (merge saved
  log files), **Parsely** (credentials + upload the current live log) — plus
  a BARAS-style **overlay mode** (toolbar toggle) that makes the window
  borderless, semi-transparent, always-on-top, and draggable so it sits over
  the game like a real overlay. Overlay mode also strips the app chrome —
  tab strip, toolbar and watched-folder path all hide, leaving just the
  meter. Double-click the drag strip to exit overlay mode.
- `overlay.py` — the **Bars** toolbar button, which is a different thing to
  overlay mode. Overlay mode reshapes the main window; this puts
  chrome-less bar lists (DPS, HPS, timers) straight onto the game with *no
  window behind them at all*, the way ORBS and BARAS do it. Windows'
  `-transparentcolor` punches out one exact colour, so painting the window
  and canvas that colour leaves only the bars visible — and those punched
  regions are click-through, so the overlay doesn't swallow input meant for
  the game. Drag a list to move it, right-click to dismiss it.

  Two deliberate departures from the chart palette used everywhere else:
  colours are more saturated (the chart palette is validated against one
  fixed surface; an overlay sits on whatever the game is drawing), and every
  string carries a 1px black outline — Canvas text has no stroke option, and
  without it light text vanishes against a bright background. Verified
  legible over pure white, which is the worst case.

## Parser status: validated against real logs

`log_parser.py` was originally written against a *documented sample* log
line, which turned out not to match the format SWTOR actually writes today.
It has since been rewritten and validated by replaying a real 113,759-line
combat log (and an ID-resolution pass over a 5.4M-line, 208-file corpus):
0 parse errors, 0 implausible values, correct player/NPC/companion
separation, and realistic per-pull DPS/HPS/deaths.

What the modern format actually requires, all now handled:

- Source/Target brackets are pipe-delimited, embedding live position and HP
  data *inside* the name field
  (`@Name#accountid|(x,y,z,heading)|(hp/hp)`). Parsing the bracket as a
  plain name yields a "player" whose name changes every line, and reading
  the first parenthesized group as the event amount yields a **position
  coordinate** instead of damage.
- `=` as a target means "same entity as source" (self-heals/self-buffs).
- Companions log as `@Owner#id/PetName {id}` and are reported under the
  pet's own name.
- Value groups nest: `(3238 energy {id} -shield {id} (4121 absorbed {id}))`
  and `(488 kinetic {id}(reflected {id}))`. A non-nesting regex silently
  grabs the *absorbed* sub-amount — or a bare ability id — instead of the
  real damage.
- Logs are **Windows-1252**, not UTF-8. Reading them as UTF-8 silently
  *drops* bytes in accented names (`Daustén` → `Daustn`), which also breaks
  exact-name matching in timer rules.

If numbers still look off, send a raw sample line — `log_parser.py` is
still the only file that needs adjusting.

## Writing a real boss definition

See `boss_definitions_bundled/soa.json` for the fullest example (multiple
phases, implicit vs. explicit `phase_ended`, `phase_active` disambiguation,
`cancel_trigger`) or `test_dummy.json` for the simplest possible one. To
add a real boss, drop a `.json` file in that folder (or in
`boss_definitions/` under the app's data directory) with:

- `id` / `name` — any identifiers you choose
- `boss_names` — the exact entity name(s) as they appear in your combat log
  for that boss (send me a sample line if you're not sure how a name shows
  up — enrage/add-phase bosses sometimes log under more than one name). If
  the only named entity is too generic to trust (shared trash-mob names
  like "Adds" that could belong to a different fight entirely), leave this
  empty and use `encounter_trigger` instead — a `Condition` (usually
  `ability_cast`) that recognizes the fight some other way
- `phases` — the first one is entered automatically when the boss is
  recognized and needs no trigger; every other phase needs a
  `start_trigger` (a `Condition`) and becomes active whenever that matches
  and it isn't already the current phase. If some OTHER phase's trigger
  depends on `phase_ended` referencing this one, you only need to give
  this phase its own `end_trigger` when nothing else's trigger fires
  independently to cause the transition — if a later phase's own trigger
  (e.g. a different ability cast) is what naturally ends this one, that's
  detected automatically and no `end_trigger` is needed
- if two phases would otherwise share an identical or unavoidably-guessed
  trigger, add `"conditions": [{"type": "phase_active", "phase_ids": [...]}]`
  to each to restrict which one it can fire from
- `counters` — optional; `increment_on`/`decrement_on`/`reset_on` are each
  a `Condition`
- `timers` — give one an `id` if you want a *different* timer to chain off
  its final expiry (`{"type": "timer_expires", "timer_id": "that_id"}`) or
  to be cancelled early by some other event (`cancel_trigger` — this is a
  no-op without an `id`, and the loader warns if you forget one);
  `repeat_interval_seconds`/`repeat_count` make a single timer re-arm
  itself instead
- if you genuinely don't know a real ability/effect's logged text, don't
  guess with something plausible-sounding — use an obvious placeholder
  string instead (see `pylons.json`), so it's clearly inert rather than
  silently wrong

## Credits

Boss/encounter definitions, defensive-cooldown data, DoT/HoT data, and the
parsely.io upload protocol are translated from
[BARAS](https://github.com/baras-app/baras) (`baras-app/baras`), a combat
parser for SWTOR written in Rust, used under its MIT license
(Copyright (c) 2025 The BARAS Authors). This project is not affiliated with
or endorsed by BARAS; any translation errors here are this project's own.

## Known limitations (v12)

- Companion damage/healing counts under the companion's own name, not
  folded into the owning player (real BARAS handles this — can add).
- Log import/merge relies on SWTOR's own timestamp text (HH:MM:SS) to order
  and time events, so it assumes a single log session that doesn't cross
  midnight.
- No live network sync between raid members' running instances of the tool
  (that would need real server infrastructure) — multi-log support here is
  strictly "import saved files after the fact."
- History file is capped at the 200 most recent encounters to keep it from
  growing unbounded.
- Per-pull Parsely upload only works for pulls that have line-range data
  attached (anything recorded live by this tool from this version onward).
  Imported/merged encounters and history entries from before this feature
  can still upload their whole source log, just not a single isolated pull.
- Parsely credentials are stored in plain text locally (same tradeoff BARAS
  makes) — fine on a personal machine, don't share `parsely_settings.json`.
- Personal cooldown tracking covers all 15 class defensives in BARAS's
  `dcds.toml` with real names/durations, now scoped to the detected local
  player. It still doesn't scope by discipline/class (you'll see rules for
  abilities your class doesn't have — harmless, they just never fire), and
  doesn't implement the "ready" glow state or alacrity-scaled durations
  BARAS's real schema supports.
- DoT/HoT tracking doesn't implement BARAS's `refresh_abilities` (re-applying
  a DoT should *refresh* the existing countdown; here it stacks a second
  one) or alacrity scaling. Abilities whose display text is identical across
  disciplines collapse to one rule, since matching is by text, not spec —
  so a duration that differs by discipline uses a single representative
  value. 5 of the 45 keywords are BARAS's own curated names that this
  corpus never logged (other classes' variants); they're flagged inline in
  `dots_hots.py`.
- Boss coverage: **63 definitions, 188 phases, 944 timers**, machine-
  translated from all 53 of BARAS's encounter files. That is every boss in
  BARAS's data that actually *has* timers: of its 236 boss entries, 175 have
  no timers at all (they exist purely so BARAS can split encounters), so the
  real target was 61 — not 236. Operations covered: R-4 Anomaly, Dxun, Gods
  from the Machine, Scum & Villainy, Dread Fortress, Dread Palace, Ravagers,
  Terror From Beyond, Temple of Sacrifice, Eternity Vault, Explosive
  Conflict, Karagga's Palace, Monolith, Toborro's Courtyard, Propagator
  Core, The Eyeless.
- 52 of those 944 timers still carry an inert
  `REPLACE_WITH_REAL_ABILITY_NAME_<id>` placeholder — an ability whose name
  is in neither the log corpus nor BARAS's own name tables. They can't
  false-match; they simply won't fire until filled in. 35 of the 63
  definitions are placeholder-free.
- Ability keywords come from two sources with different trust levels:
  resolved from real logs (authoritative — it's the text the client actually
  wrote), or from BARAS's `attack_types.csv`/`icons.csv` display labels
  (broad, but a label isn't guaranteed to equal logged text). Where both
  know an id they agree 5,314/5,321 — the 7 exceptions are cosmetic toy
  renames, not combat abilities.
- This is still a one-time snapshot and will drift as patches rename
  abilities, whereas BARAS's data is actively maintained. Re-running
  `tools/translate_baras_toml.py` against a fresh checkout is the intended
  way to catch up.
- Among the 12 older hand-written definitions, a few mechanics are still
  approximated from BARAS's display names rather than confirmed log text.
  Zorn & Toth's "Baradium Heave" keyword is a pattern-match guess;
  "Weakened" and "Fearful" are unverified. Pylons specifically can't
  recognize its fight yet — its trigger ability has zero textual hint in the
  source data, only a numeric ID, so it's a deliberate placeholder rather
  than a guess.
- Still no difficulty (story/veteran/master) scoping, and no shield/absorb
  attribution or damage-metric challenges — BARAS's format supports these;
  ours implements what the translated bosses' mechanics actually needed. A
  boss that needs one of these (Apex Vanguard's shield tracking, for
  instance) would need the engine extended further first.
- Two engine-semantics bugs surfaced only when the machine-translated
  definitions were replayed against a real log, and are worth knowing if you
  write your own definitions (both now fixed):
  `counter_reaches` was level-triggered (`current >= value`, so it stayed
  true and re-fired its timer on *every* subsequent event once reached)
  rather than edge-triggered as BARAS means it; and `ability_cast` matched
  any event merely *mentioning* the ability, including the damage ticks and
  effect applications a single cast produces — 56 matches for one real cast
  of Watchdog Protocol, 155 for 12 casts of Seed of Echoes. Together these
  inflated boss-timer firings ~70x (13,297 → 190 over one raid log). If you
  want "true for the rest of the fight once reached", use `counter_compare`
  with `gte`, not `counter_reaches`.
- The legacy keyword path used by manual Timers-tab rules deliberately still
  matches on *any* mention of the keyword (that's its documented behavior —
  "the moment an ability/effect matching the keyword appears"), so a spammed
  ability can stack duplicate countdown rows there. Boss-definition
  `ability_cast` triggers do not have this issue.
- Chain/repeat detection has a small timing dependency: a chained timer
  only fires once `TimerEngine.tick()` has pruned the timer it depends on,
  which happens on every log event and on the GUI's periodic refresh (every
  ~0.5s) — so chains fire within roughly half a second of the real expiry,
  not the exact instant.
- Local player detection is a best-effort heuristic (first player entity
  seen), same as BARAS's own approach — not guaranteed correct in every
  group composition.
