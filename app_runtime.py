"""Runtime state helpers shared by the desktop application entry point.

Keeping these small state holders outside ``main.py`` keeps startup wiring and
background processing separate from mutable application state while preserving
the existing public behavior.
"""

import queue
import threading
from datetime import datetime, timezone
from typing import Optional

import storage
import log_archive
import update_check
from version import __version__


class StatusHolder:
    """Latest status text shown by the desktop/web UI."""

    def __init__(self):
        self.text = "Waiting for combat log..."


class UpdateHolder:
    """Result of the one-shot startup update check."""

    def __init__(self):
        self.result = None


def check_for_update_once(holder: "UpdateHolder") -> None:
    """Run the startup update check and store its result."""
    holder.result = update_check.check_for_update(__version__)


def archive_old_logs_once(log_dir: Optional[str]) -> None:
    """Archive eligible old logs without blocking application startup."""
    if not log_dir:
        return
    settings = storage.load_cleanup_settings()
    retention_days = settings.get("retention_days") or 0
    if retention_days <= 0:
        return

    archived = log_archive.archive_old_logs(log_dir, retention_days)
    if archived:
        settings["last_run"] = datetime.now(timezone.utc).isoformat()
        settings["last_archived_count"] = len(archived)
        storage.save_cleanup_settings(settings)


class CharacterSettingsHolder:
    """Per-character settings used by the live reader.

    Alacrity is entered manually because SWTOR combat logs do not expose the
    character's actual alacrity rating/percentage directly.
    """

    def __init__(self):
        self.alacrity_pct: float = 0.0
        self._character: Optional[str] = None

    def sync_for_character(self, character: Optional[str]) -> None:
        """Load settings only when the active character changes."""
        if character == self._character:
            return
        self._character = character
        if character:
            layout = storage.load_overlay_layout(character)
            try:
                self.alacrity_pct = float(layout.get("alacrity_pct", 0.0) or 0.0)
            except (TypeError, ValueError):
                self.alacrity_pct = 0.0
        else:
            self.alacrity_pct = 0.0

    def set_alacrity_pct(self, pct: float) -> None:
        """Update the live value and persist it for the active character."""
        self.alacrity_pct = pct
        if self._character:
            layout = storage.load_overlay_layout(self._character)
            layout["alacrity_pct"] = pct
            storage.save_overlay_layout(layout, character=self._character)

    @property
    def character(self) -> Optional[str]:
        return self._character


class HistoryWriter:
    """Persist completed encounters on a dedicated background thread."""

    def __init__(self, status: StatusHolder):
        self._queue: "queue.Queue" = queue.Queue()
        self._status = status
        threading.Thread(target=self._run, daemon=True, name="history-writer").start()

    def submit(self, encounter) -> None:
        self._queue.put(encounter)

    def _run(self) -> None:
        while True:
            encounter = self._queue.get()
            try:
                storage.append_history_entry(encounter)
            except OSError as exc:
                self._status.text = f"Failed to save a completed pull: {exc}"
