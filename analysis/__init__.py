"""
analysis/

Corpus-wide analytics: the layer that reads your ENTIRE log history as one
dataset rather than one session at a time.

  corpus.py     -- indexes every log file (cached, incremental)
  forensics.py  -- reconstructs what actually killed someone
  webapp.py     -- local web UI over both

Run the UI with:  python -m analysis.webapp
"""
