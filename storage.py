"""
storage.py

Local JSON persistence so history and timer rules survive restarts.
Everything lives under a per-user data directory:

    Windows: %APPDATA%\\swtor-parser\\
    other:   ~/.swtor-parser/

Files:
    history.json         -- list of completed Encounter.to_dict() entries
    timer_rules.json     -- list of TimerRule.to_dict() entries
    overlay_layout.json  -- which floating bar frames were open, where, and
                             whether they were locked, so the picker and the
                             frames themselves come back exactly as left

Writes are append-friendly: save_history_entry() appends one encounter
without rewriting rules, and vice versa, so a crash mid-session doesn't
lose everything already flushed to disk.
"""

import json
import os
from pathlib import Path
from typing import List

from stats import Encounter
from timers import TimerRule

HISTORY_LIMIT = 200  # keep at most this many past encounters on disk


def data_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home()
    d = base / ("swtor-parser" if appdata else ".swtor-parser")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_path() -> Path:
    return data_dir() / "history.json"


def _rules_path() -> Path:
    return data_dir() / "timer_rules.json"


def load_history() -> List[Encounter]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [Encounter.from_dict(d) for d in raw]


def save_history(encounters: List[Encounter]) -> None:
    trimmed = encounters[-HISTORY_LIMIT:]
    data = [e.to_dict() for e in trimmed]
    _history_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_history_entry(encounter: Encounter) -> None:
    """Append one completed encounter without needing the full in-memory list."""
    existing = []
    path = _history_path()
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append(encounter.to_dict())
    existing = existing[-HISTORY_LIMIT:]
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _parsely_settings_path() -> Path:
    return data_dir() / "parsely_settings.json"


def load_parsely_settings() -> dict:
    """Returns dict with keys: username, password, guild, guild_log, visibility.
    Note: password is stored in plaintext locally, same tradeoff BARAS itself
    makes for this -- there's no more secure option without a proper OS
    keychain integration."""
    path = _parsely_settings_path()
    defaults = {
        "username": "", "password": "", "guild": "",
        "guild_log": False, "visibility": 1,
    }
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return defaults
    defaults.update(data)
    return defaults


def save_parsely_settings(settings: dict) -> None:
    _parsely_settings_path().write_text(json.dumps(settings, indent=2), encoding="utf-8")


def load_timer_rules() -> List[TimerRule]:
    path = _rules_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    rules = []
    for d in raw:
        rules.append(
            TimerRule(
                keyword=d["keyword"],
                label=d.get("label", d["keyword"]),
                duration_seconds=d.get("duration_seconds", 10.0),
                only_target=d.get("only_target"),
                only_source=d.get("only_source"),
                voice_alert=d.get("voice_alert", True),
                warn_seconds_before=d.get("warn_seconds_before", 0.0),
            )
        )
    return rules


def save_timer_rules(rules: List[TimerRule]) -> None:
    data = [
        {
            "keyword": r.keyword,
            "label": r.label,
            "duration_seconds": r.duration_seconds,
            "only_target": r.only_target,
            "only_source": r.only_source,
            "voice_alert": r.voice_alert,
            "warn_seconds_before": r.warn_seconds_before,
        }
        for r in rules
    ]
    _rules_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _overlay_layout_path() -> Path:
    return data_dir() / "overlay_layout.json"


def load_overlay_layout() -> dict:
    """Returns {"locked": bool, "frames": {key: {"x": int, "y": int}}}.
    A frame's mere presence in "frames" is what the picker's checkboxes read
    on startup to decide which boxes start ticked -- this file is both the
    position store and the "what was open" store, so there's one source of
    truth instead of two files that could disagree."""
    path = _overlay_layout_path()
    defaults = {"locked": False, "frames": {}}
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return defaults
    defaults.update(data)
    if not isinstance(defaults.get("frames"), dict):
        defaults["frames"] = {}
    return defaults


def save_overlay_layout(layout: dict) -> None:
    _overlay_layout_path().write_text(json.dumps(layout, indent=2), encoding="utf-8")
