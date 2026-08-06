"""
analysis/

Corpus-wide analytics: the layer that reads your ENTIRE log history as one
dataset rather than one session at a time.

  events.py        -- the one place a pull's events are read and parsed
  corpus.py        -- indexes every log file (cached, incremental)
  forensics.py     -- reconstructs what actually killed someone
  timeline.py      -- time-bucketed damage/healing + phase offsets
  fight_summary.py -- boss, outcome, phases seen, deaths
  webapp.py        -- local web UI over all of it

Everything except corpus.py reads a pull through events.py. Nothing here
should open a log file directly -- that's what put three byte-identical
copies of the same reader in three modules, tripled the parse cost of one
Deep Dive, and left gzipped (archived) logs unreadable.

Run the UI with:  python -m analysis.webapp
"""
