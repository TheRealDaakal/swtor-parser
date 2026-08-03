"""
gui.py

What's left of the Tkinter GUI after the pywebview migration: the
BARAS-style floating bar overlays (borderless, semi-transparent,
draggable, always-on-top) are genuine OS-level windows that a browser/
pywebview page structurally can't do -- true per-pixel transparency and
click-through on an always-on-top window is a Tk (or native) thing, not
a web one. Everything else (Live, History, Timers, Overlays picker,
Import Logs, Parsely) is now served by web_server.py and shown in the
pywebview window; see main.py.

OverlayManager owns a hidden Tk root purely to host the floating bar
Toplevels, plus the periodic tick that keeps timers/taunts/history
advancing and the bars re-rendering, independent of anything the web
UI is doing.

Threading note: Tcl/Tk is not safe to touch from any thread other than
the one running mainloop() (see main.py's comment on "Calling Tcl from
different apartment" for the related, sharper version of this that
bit pywebview specifically). web_server.py's HTTP handlers run on their
own per-request threads, so they can't call into Tk-touching methods
directly -- toggle_overlay()/set_lock()/clear_all() update a plain,
thread-safe dict/bool immediately (so a GET right after a POST reads
back correctly) and queue the actual Toplevel work for _refresh() to
drain on the Tk thread, same idea as Tkinter's own after()-from-another-
thread pattern.
"""

import queue
import tkinter as tk

import storage

REFRESH_MS = 500


class OverlayManager:
    def __init__(self, tracker, timer_engine, boss_state=None, hot_tracker=None, taunt_tracker=None,
                 aggro_tracker=None):
        self.tracker = tracker
        self.timer_engine = timer_engine
        self.boss_state = boss_state
        self.bar_overlays = []
        self._commands = queue.Queue()  # cross-thread -> drained on the Tk thread by _refresh()
        if hot_tracker is None:
            from dots_hots import HotTracker
            hot_tracker = HotTracker()
        self.hot_tracker = hot_tracker
        if taunt_tracker is None:
            from taunt_tracker import TauntTracker
            taunt_tracker = TauntTracker()
        self.taunt_tracker = taunt_tracker
        if aggro_tracker is None:
            from aggro_tracker import AggroTracker
            aggro_tracker = AggroTracker()
        self.aggro_tracker = aggro_tracker
        # Which character's overlay layout is currently loaded -- None until
        # boss_state identifies the local player from the log. A tank alt
        # and a healer alt want different frames up, so the layout swaps in
        # per character rather than staying one global "whoever's playing"
        # config. See _maybe_reload_layout_for_character().
        self._loaded_layout_character = None

        import overlay as ov
        self._overlay_state = {key: False for key, _label, _group in ov.AVAILABLE_OVERLAYS}
        self._locked = False

        self.root = tk.Tk()
        self.root.withdraw()  # no visible window -- just hosts the bar Toplevels

        self._restore_overlay_layout()
        self.root.after(REFRESH_MS, self._refresh)

    def run(self):
        self.root.mainloop()

    # ---------- cross-thread API (safe to call from any thread) ----------

    def overlay_state(self) -> dict:
        """Everything the web Overlays page needs to render its picker."""
        import overlay as ov
        return {
            "groups": ov.OVERLAY_GROUPS,
            "items": [
                {"key": key, "label": label, "group": group, "on": self._overlay_state.get(key, False)}
                for key, label, group in ov.AVAILABLE_OVERLAYS
            ],
            "locked": self._locked,
        }

    def toggle_overlay(self, key: str) -> None:
        if key not in self._overlay_state:
            return
        self._overlay_state[key] = not self._overlay_state[key]
        self._commands.put(("apply", key))

    def set_lock(self, locked: bool) -> None:
        self._locked = bool(locked)
        self._commands.put(("lock", None))

    def clear_all(self) -> None:
        for key in self._overlay_state:
            self._overlay_state[key] = False
        self._commands.put(("clear", None))

    # ---------- floating bar overlays (Tk thread only past this point) ----------

    def _restore_overlay_layout(self, character=None):
        """Recreates whatever frames were open, where they were, and the
        lock state -- called once at startup (character=None, before the
        local player is known yet) and again whenever
        _maybe_reload_layout_for_character() swaps characters."""
        import overlay as ov
        layout = storage.load_overlay_layout(character)
        self._locked = bool(layout.get("locked", False))
        valid_keys = {k for k, _label, _group in ov.AVAILABLE_OVERLAYS}
        for key, pos in layout.get("frames", {}).items():
            if key not in valid_keys:
                continue  # a saved key from a version that no longer exists
            self._overlay_state[key] = True
            self._apply_overlay(key, pos=pos, persist=False)

    def _maybe_reload_layout_for_character(self):
        """Called every refresh tick. Once boss_state identifies the local
        player (or that identity changes -- a relog to a different alt
        mid-session), swap in THEIR saved overlay layout instead of
        continuing to show whatever the pre-character-known "default"
        state had up."""
        if not self.boss_state:
            return
        name = self.boss_state.local_player_name
        if name is None or name == self._loaded_layout_character:
            return
        self._loaded_layout_character = name
        for o in list(self.bar_overlays):
            o.win.destroy()
        self.bar_overlays = []
        for key in self._overlay_state:
            self._overlay_state[key] = False  # stale state from the previous character's layout
        self._restore_overlay_layout(character=name)

    def _current_character(self):
        return self.boss_state.local_player_name if self.boss_state else None

    def _persist_overlay_layout(self):
        frames = {}
        notes_text = None
        hot_grid_slots = None
        for o in self.bar_overlays:
            key = getattr(o, "kind", None)
            if key:
                frames[key] = {"x": o.win.winfo_x(), "y": o.win.winfo_y(),
                               "width": o.width, "height": o.height}
            if key == "notes":
                notes_text = o.current_text()
            if key == "hots_grid":
                hot_grid_slots = o.slots
        if notes_text is None:
            # No Notes frame open right now -- keep whatever was last saved
            # instead of wiping it out just because the frame is closed.
            notes_text = storage.load_overlay_layout(self._current_character()).get("notes", "")
        if hot_grid_slots is None:
            hot_grid_slots = storage.load_overlay_layout(self._current_character()).get("hot_grid_slots", [])
        storage.save_overlay_layout({
            "locked": self._locked,
            "frames": frames,
            "notes": notes_text,
            "hot_grid_slots": hot_grid_slots,
        }, character=self._current_character())

    def _on_notes_changed(self, _text: str) -> None:
        """FocusOut on the Notes text widget -- save immediately rather
        than waiting for a drag/lock/toggle to trigger a layout persist."""
        self._persist_overlay_layout()

    def _apply_overlay(self, key, pos=None, persist=True):
        """Show or hide one frame, honouring self._overlay_state[key].
        pos: optional {"x": int, "y": int, "width": int, "height": int} to
        restore a saved position/size instead of the default stacked
        placement and each kind's own default size -- used on startup and
        whenever a frame is dragged, resized, or the lock toggles."""
        import overlay as ov
        want = self._overlay_state.get(key, False)
        existing = next((o for o in self.bar_overlays if getattr(o, "kind", None) == key), None)
        if not want:
            if existing:
                existing.win.destroy()
                self.bar_overlays.remove(existing)
                if persist:
                    self._persist_overlay_layout()
            return
        if existing:
            return
        drop = lambda o: self._on_overlay_closed(o)
        moved = lambda o: self._persist_overlay_layout()
        # stack new frames down the left so two don't land on top of each
        # other, unless we're restoring a specific saved position
        x = pos["x"] if pos else 40
        y = pos["y"] if pos else 180 + 150 * len(self.bar_overlays)
        # A previously resized frame's saved width/height win over each
        # kind's own default; omitted entirely (old saved layouts predate
        # resizing) so the class default still applies.
        size_kwargs = {}
        if pos and "width" in pos:
            size_kwargs["width"] = pos["width"]
        if pos and "height" in pos:
            size_kwargs["height"] = pos["height"]
        if key == "timers":
            o = ov.TimerOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved, **size_kwargs)
        elif key == "alerts":
            o = ov.AlertOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved, **size_kwargs)
        elif key == "hots":
            o = ov.HotOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved, **size_kwargs)
        elif key == "hots_grid":
            saved_slots = storage.load_overlay_layout(self._current_character()).get("hot_grid_slots", [])
            o = ov.HotGridOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved,
                                  initial_slots=saved_slots, **size_kwargs)
        elif key == "boss_hp":
            o = ov.BossHealthOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved, **size_kwargs)
        elif key in ("cooldowns", "dots"):
            o = ov.TimerOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved, **size_kwargs)
            o.kind = key
        elif key == "notes":
            saved_text = storage.load_overlay_layout(self._current_character()).get("notes", "")
            o = ov.NotesOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved,
                                initial_text=saved_text, on_text_change=self._on_notes_changed,
                                **size_kwargs)
        else:
            o = ov.BarOverlay(self.root, kind=key, x=x, y=y, on_close=drop, on_move=moved, **size_kwargs)
        # a frame opened while "Lock positions" is already checked should
        # start locked too, not silently draggable until the next toggle
        if self._locked:
            o.set_locked(True)
        self.bar_overlays.append(o)
        if persist:
            self._persist_overlay_layout()

    def _apply_lock_state(self):
        """One toggle governs every currently-visible frame, same as
        BARAS's single LOCKED control -- not a per-frame setting."""
        for o in self.bar_overlays:
            o.set_locked(self._locked)
        self._persist_overlay_layout()

    def _on_overlay_closed(self, o):
        """Right-clicking a frame dismisses it -- keep state in sync,
        otherwise the web picker shows a frame that no longer exists as
        still on. Runs as a Tk event callback, so already on the Tk thread."""
        if o in self.bar_overlays:
            self.bar_overlays.remove(o)
        key = getattr(o, "kind", None)
        if key in self._overlay_state:
            self._overlay_state[key] = False
        self._persist_overlay_layout()

    def _clear_overlays(self):
        for o in list(self.bar_overlays):
            o.win.destroy()
        self.bar_overlays = []
        for key in self._overlay_state:
            self._overlay_state[key] = False
        self._persist_overlay_layout()

    def _refresh_bar_overlays(self):
        if not self.bar_overlays:
            return
        # display_encounter() (not .current directly): keeps a just-finished
        # pull's numbers on these bars through the between-pulls downtime
        # instead of blanking the instant a stray non-combat event rolls
        # `current` over to a fresh, empty Encounter(). One call reused for
        # both rows and per-player extras below so they can't disagree about
        # which encounter is being shown.
        enc = self.tracker.display_encounter()
        rows, duration = enc.snapshot(), enc.duration()
        # None (not "No boss encounter active") when idle -- that placeholder
        # string is long enough to visibly collide with the overlay's own
        # title text (reported live: "words are clipping each other"), and
        # it's not useful clutter on a DPS meter when there's nothing to
        # report anyway.
        boss = self.boss_state.status_text() if self.boss_state and self.boss_state.active_boss else None
        for o in self.bar_overlays:
            kind = getattr(o, "kind", None)
            if kind == "taken":
                data = sorted(((r[0], r[3]) for r in rows), key=lambda r: -r[1])
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data))
            elif kind == "dps":
                # Attaches each player's crit_pct() as a 3rd tuple element --
                # see BarOverlay.render's optional crit_pct display.
                entities = enc.players
                data = sorted(
                    ((r[0], r[1], entities[r[0]].crit_pct()) for r in rows if r[0] in entities),
                    key=lambda r: -r[1],
                )
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data), subtitle=boss)
            elif kind == "hps":
                entities = enc.players
                data = sorted(
                    ((r[0], r[2], entities[r[0]].heal_crit_pct()) for r in rows if r[0] in entities),
                    key=lambda r: -r[1],
                )
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data))
            elif kind == "effective_hps":
                # Reads PlayerStats directly since the compact snapshot()
                # tuple only carries raw healing_done, not overheal.
                entities = [p for p in enc.players.values() if p.is_player]
                data = sorted(
                    ((p.name, p.effective_hps(duration), p.heal_crit_pct()) for p in entities),
                    key=lambda r: -r[1],
                )
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data))
            elif kind == "boss_dps":
                active = self.boss_state.active_boss if self.boss_state else None
                boss_names = active.boss_names if active else []
                entities = [p for p in enc.players.values() if p.is_player]
                data = sorted(
                    ((p.name, p.boss_dps(boss_names, duration), p.crit_pct()) for p in entities),
                    key=lambda r: -r[1],
                )
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data), subtitle=boss)
            elif kind == "absorbed":
                # Raw absorbed magnitude, not the percentage -- bars only
                # make sense as a comparable quantity across players, and
                # the live table already shows the percentage per person.
                # Reads PlayerStats directly since the snapshot() tuple is
                # kept compact and doesn't carry the raw number.
                entities = [p for p in enc.players.values() if p.is_player]
                data = sorted(((p.name, p.damage_absorbed) for p in entities),
                              key=lambda r: -r[1])
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data))
            elif kind == "threat":
                # TPS (rate), not the raw cumulative total -- a running total
                # only ever grows and says nothing comparable moment-to-
                # moment; TPS is what actually answers "am I about to pull."
                # Reads PlayerStats directly since the compact snapshot()
                # tuple doesn't carry threat.
                entities = [p for p in enc.players.values() if p.is_player]
                data = sorted(((p.name, p.tps(duration)) for p in entities), key=lambda r: -r[1])
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data))
            elif kind in ("hots", "hots_grid"):
                o.render(self.hot_tracker.expiring(within_seconds=o.within_seconds))
            elif kind == "boss_hp":
                active = self.boss_state.active_boss if self.boss_state else None
                phase_name = None
                if active and self.boss_state.active_phase_id:
                    phase = active.phase_by_id(self.boss_state.active_phase_id)
                    phase_name = phase.name if phase else None
                o.render(
                    boss_name=active.name if active else None,
                    hp_percent=self.boss_state.boss_hp_percent() if self.boss_state else None,
                    hp_current=self.boss_state.boss_hp_current if self.boss_state else None,
                    hp_max=self.boss_state.boss_hp_max if self.boss_state else None,
                    boss_target=self.boss_state.boss_target if self.boss_state else None,
                    subtitle=phase_name,
                )
            elif kind == "cooldowns":
                o.render(self.timer_engine.snapshot("cooldown"))
            elif kind == "dots":
                o.render(self.timer_engine.snapshot("dot"))
            elif kind == "timers":
                o.render([t for t in self.timer_engine.snapshot()
                          if t[3] not in ("cooldown", "dot", "hot") and not t[4]])
            elif kind == "alerts":
                o.render([t for t in self.timer_engine.snapshot() if t[4]])

    # ---------- refresh loop ----------

    def _drain_commands(self):
        while True:
            try:
                action, key = self._commands.get_nowait()
            except queue.Empty:
                return
            if action == "apply":
                self._apply_overlay(key)
            elif action == "lock":
                self._apply_lock_state()
            elif action == "clear":
                self._clear_overlays()

    def _refresh(self):
        """Drives all state forward on a fixed cadence, independent of the
        web UI: timers still need to expire and taunts still need to
        resolve to a miss even between log events, and the floating bar
        overlays need their own render tick and their commands drained."""
        self._drain_commands()
        self.timer_engine.tick()
        # tick()'d here (not just on the reader thread) so a taunt that got
        # no reply at all still flips from pending to a visible miss --
        # otherwise a genuinely failed taunt with no further log lines would
        # sit unresolved until the next unrelated event.
        self.taunt_tracker.tick()
        self._maybe_reload_layout_for_character()
        self._refresh_bar_overlays()
        self._check_aggro()

        self.root.after(REFRESH_MS, self._refresh)

    def _check_aggro(self):
        # Unconditional (not gated on self.bar_overlays like
        # _refresh_bar_overlays) -- this is a safety alert, not tied to
        # whether the user has a specific overlay panel open.
        if not self.boss_state or self.boss_state.active_boss is None:
            return
        enc = self.tracker.display_encounter()
        self.aggro_tracker.check(
            enc.players, self.boss_state.boss_target, enc.duration(), self.timer_engine,
        )
