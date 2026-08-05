"""
version.py

Single source of truth for the app's version number. Bumped by hand on
each release -- update_check.py compares this against GitHub's latest
release tag to decide whether to show the "update available" banner.
"""

__version__ = "0.2.14"
