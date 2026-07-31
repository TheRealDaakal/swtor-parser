# analysis/ — corpus analytics

The part of this project that is **not** trying to be BARAS.

BARAS (and StarParse) answer *"what happened in this pull?"* extremely well.
Neither answers *"what happened across my last 200 raid nights?"* — their
data model is per-session. This package reads your entire CombatLogs folder
as one dataset.

## Run it

```bash
python -m analysis.webapp
```

Opens `http://127.0.0.1:8765`. First run indexes every log file (a couple of
minutes for ~200 files); after that it's cached and loads instantly. Binds to
localhost only — it reads local logs and has no authentication, so it must
not be exposed to a network.

## What's here

| file | does |
|---|---|
| `corpus.py` | Indexes every log file. Cached by (size, mtime), so rescans only re-parse changed files — 123s cold, 0.12s warm. |
| `forensics.py` | Reconstructs what actually killed someone: incoming damage by ability, HP trajectory, defensive availability. |
| `webapp.py` | Stdlib-only local server + JSON API. No Flask, no CDN, works fully offline. |
| `static/` | Dark-themed frontend. Charts are hand-rolled SVG for the same reason — zero dependencies. |

## Design decisions worth knowing

**Encounter boundaries use log timestamps, not wall clock.** `StatsTracker`'s
live path is right for tailing — real gaps arrive in real time — but replaying
a finished file takes milliseconds, so wall-clock rollover would collapse an
entire raid night into one "encounter".

**The index stores summaries plus a `(file, line range)` pointer, not events.**
The corpus here is 1.4 GB. Death forensics re-reads just the one encounter it
needs, on demand.

**Players and NPCs are tracked separately.** This matters more than it sounds:
before the split, "deaths" counted every add that died — 15,514 across 96
Styrak pulls, which reads as the raid wiping 161 times per pull. Real player
deaths are the signal; add deaths are noise. Same for forensics, which
defaults to `players_only` (a typical pull kills adds ~20:1 over players).

## Three real bugs this surfaced

Corpus-wide analysis found things per-session testing didn't — each was
caught by a number that was merely *implausible* rather than obviously
broken, which is exactly what aggregate views are good for:

1. **`is_death` matched the ability name.** `_classify` built its keyword
   haystack from `effect_type + effect_name + ability`, so any ability *named*
   "Death Field" / "Death From Above" made its own damage ticks count as
   deaths — and because `stats.apply()` returns early on a death, that damage
   was silently dropped from DPS too. Deaths were over-reported ~25%.
   Fixed by classifying from the event-type fields only.

2. **NPC deaths counted as player deaths** (above) — 15,514 "deaths" across
   96 Styrak pulls.

3. **Pulls were being merged.** One player showed 92 deaths in a single
   encounter. The encounter was 36 minutes long and contained five
   EnterCombat events: the live tracker ends a fight on ExitCombat with an
   inactivity fallback, but SWTOR logs ExitCombat unreliably (21 EnterCombat
   vs 8 ExitCombat in that file) and during progression the adds never stop
   swinging, so neither boundary ever fired. The replay path now treats a
   fresh EnterCombat as a new pull — see `NEW_PULL_MIN_GAP_SECONDS`.

## Ideas not built yet

- Kill-vs-wipe diffing: same boss, what was different about the pulls that
  succeeded.
- Alt/legacy grouping. `Daakål` and `Daakal` never co-occur in any encounter
  and are both healers — almost certainly one person on two characters. The
  UI currently treats them as strangers.
- Raid-wide "who keeps dying to what" across weeks, not per pull.
