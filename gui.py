"""
gui.py

Live meter + timers + history browser + log import, with a BARAS-style
overlay mode (borderless, semi-transparent, draggable, always-on-top) you
can toggle on top of the normal windowed view.

Persistence: history and timer rules are loaded from disk on startup
(storage.py) and saved as they change, so both survive a restart.
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import storage
from stats import Encounter
from timers import TimerRule

REFRESH_MS = 500

# Same palette as the corpus web UI (analysis/static/app.css) so the two
# halves of the tool don't look like different products. Tk's default theme
# is a light grey Windows dialog, which over a fullscreen game reads as a
# misplaced settings window rather than an overlay.
BG        = "#1a1a19"   # surface
BG_2      = "#232321"   # raised
BG_3      = "#2c2c2a"
INK       = "#ffffff"
INK_2     = "#c3c2b7"
INK_MUTED = "#898781"
HAIRLINE  = "#33332f"
ACCENT    = "#3987e5"   # series 1
GOOD      = "#199e70"   # series 3
CRITICAL  = "#d03b3b"
WARNING   = "#fab219"


class MeterWindow:
    def __init__(self, tracker, timer_engine, status_queue: "queue.Queue[str]",
                 boss_state=None, hot_tracker=None, taunt_tracker=None):
        self.tracker = tracker
        self.timer_engine = timer_engine
        self.status_queue = status_queue
        self.boss_state = boss_state
        self.overlay_mode = False
        self.bar_overlays = []
        self._overlay_vars = {}   # key -> BooleanVar, built lazily by the picker
        self._overlays_locked = None  # BooleanVar, created after self.root exists
        self._picker = None
        if hot_tracker is None:
            # standalone/demo use -- main.py normally supplies the one the
            # background reader thread is actually feeding
            from dots_hots import HotTracker
            hot_tracker = HotTracker()
        self.hot_tracker = hot_tracker
        if taunt_tracker is None:
            from taunt_tracker import TauntTracker
            taunt_tracker = TauntTracker()
        self.taunt_tracker = taunt_tracker
        self._drag_offset = (0, 0)
        self._history_rows_shown = 0  # how many history rows we've rendered so far

        self.root = tk.Tk()
        self.root.title("DPS — Dynamic Parse System")
        self.root.geometry("680x460")
        self.root.attributes("-topmost", True)
        self._overlays_locked = tk.BooleanVar(value=False)
        self._apply_theme()
        # stash the real tab layout so overlay mode can hide and restore it
        self._tab_layout = ttk.Style(self.root).layout("TNotebook.Tab")

        self._build_toolbar()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.live_tab = ttk.Frame(self.notebook)
        self.history_tab = ttk.Frame(self.notebook)
        self.timers_tab = ttk.Frame(self.notebook)
        self.import_tab = ttk.Frame(self.notebook)
        self.parsely_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.live_tab, text="Live")
        self.notebook.add(self.history_tab, text="History")
        self.notebook.add(self.timers_tab, text="Timers")
        self.notebook.add(self.import_tab, text="Import Logs")
        self.notebook.add(self.parsely_tab, text="Parsely")

        self._build_live_tab()
        self._build_history_tab()
        self._build_timers_tab()
        self._build_import_tab()
        self._build_parsely_tab()

        self._load_persisted_state()

        self.root.after(REFRESH_MS, self._refresh)

    # ---------- theme ----------

    def _apply_theme(self):
        """Dark theme via ttk's 'clam' engine. The native Windows theme
        ignores most colour options -- background/fieldbackground on a
        Treeview simply don't take -- so clam is the only one that can be
        recoloured reliably."""
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=INK_2,
                        fieldbackground=BG, borderwidth=0, focuscolor=BG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK_2)
        style.configure("TCheckbutton", background=BG, foreground=INK_2)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TRadiobutton", background=BG, foreground=INK_2)
        style.map("TRadiobutton", background=[("active", BG)])

        style.configure("TButton", background=BG_2, foreground=INK_2,
                        borderwidth=1, relief="flat", padding=(10, 4))
        style.map("TButton",
                  background=[("active", BG_3), ("pressed", BG_3)],
                  foreground=[("active", INK)])

        style.configure("TEntry", fieldbackground=BG_2, foreground=INK,
                        insertcolor=INK, borderwidth=1)

        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", background=BG, foreground=INK_MUTED,
                        padding=(12, 6), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", BG_2)],
                  foreground=[("selected", INK)])

        style.configure("Treeview", background=BG, fieldbackground=BG,
                        foreground=INK_2, borderwidth=0, rowheight=21)
        style.configure("Treeview.Heading", background=BG_2, foreground=INK_MUTED,
                        borderwidth=0, relief="flat")
        style.map("Treeview.Heading", background=[("active", BG_3)])
        style.map("Treeview",
                  background=[("selected", BG_3)], foreground=[("selected", INK)])
        # kill the dotted focus ring clam draws around the tree
        style.layout("Treeview.Item", [
            ("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.image", {"side": "left", "sticky": ""}),
                ("Treeitem.text", {"side": "left", "sticky": ""}),
            ]})])

        # Progress bars: default clam green is loud and means nothing here.
        for name, colour in (("Boss", ACCENT), ("Cooldown", WARNING), ("Dot", GOOD)):
            style.configure(f"{name}.Horizontal.TProgressbar",
                            background=colour, troughcolor=BG_2,
                            borderwidth=0, thickness=6)

        # Overlay picker: a checkbox rendered as a solid toggle button --
        # dim/outlined when off, filled accent-blue when on -- reads as a
        # clean button grid rather than a plain checkbox list. "Toolbutton"
        # is clam's own flat-toggle variant of Checkbutton, so this reuses
        # the BooleanVar wiring already in place instead of hand-rolling
        # button state tracking.
        style.configure("Overlay.Toolbutton", background=BG_2, foreground=INK_MUTED,
                        borderwidth=1, relief="flat", anchor="w",
                        padding=(14, 10), font=("Segoe UI", 9, "bold"))
        style.map("Overlay.Toolbutton",
                  background=[("selected", ACCENT), ("active", BG_3)],
                  foreground=[("selected", INK), ("active", INK)])

    # ---------- persistence ----------

    def _load_persisted_state(self):
        self._restore_overlay_layout()
        loaded_rules = storage.load_timer_rules()
        for rule in loaded_rules:
            self.timer_engine.add_rule(rule)
            self.rules_tree.insert(
                "", "end",
                values=(
                    rule.keyword, rule.label, rule.duration_seconds,
                    rule.warn_seconds_before or "-", "on" if rule.voice_alert else "off",
                ),
            )

        loaded_history = storage.load_history()
        if loaded_history:
            self.tracker.history = loaded_history + self.tracker.history
        self._history_rows_shown = 0  # force full re-render including loaded entries

    # ---------- toolbar / overlay mode ----------

    def _build_toolbar(self):
        self.toolbar = ttk.Frame(self.root)
        self.toolbar.pack(fill="x", padx=8, pady=6)

        self.status_var = tk.StringVar(value="Waiting for combat log...")
        ttk.Label(self.toolbar, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(self.toolbar, text="Overlay", command=self._toggle_overlay).pack(
            side="right"
        )
        ttk.Button(self.toolbar, text="Bars", command=self._toggle_bar_overlays).pack(
            side="right", padx=(0, 6)
        )

        self.drag_strip = tk.Label(
            self.root, text="⠿  drag  ·  double-click to exit overlay",
            bg=BG_2, fg=INK_MUTED, anchor="w", padx=10, pady=3,
        )
        self.drag_strip.bind("<ButtonPress-1>", self._drag_start)
        self.drag_strip.bind("<B1-Motion>", self._drag_move)
        self.drag_strip.bind("<Double-Button-1>", lambda e: self._toggle_overlay())

    def _toggle_overlay(self):
        """Overlay mode isn't just 'borderless + translucent' -- it has to
        drop the app chrome too. Leaving the tab strip, the toolbar and the
        watched-folder path on screen means the thing sitting over your game
        is mostly UI you can't act on mid-pull."""
        self.overlay_mode = not self.overlay_mode
        style = ttk.Style(self.root)
        if self.overlay_mode:
            self.root.overrideredirect(True)
            self.root.attributes("-alpha", 0.92)
            self.toolbar.pack_forget()
            # hide the notebook's tab row rather than reparenting the frame,
            # which would lose the geometry of everything inside it
            style.layout("TNotebook.Tab", [])
            self.hint_label.pack_forget()
            self.drag_strip.pack(fill="x", before=self.notebook)
            self.notebook.select(self.live_tab)
        else:
            self.drag_strip.pack_forget()
            style.layout("TNotebook.Tab", self._tab_layout)
            self.toolbar.pack(fill="x", padx=8, pady=6, before=self.notebook)
            self.hint_label.pack(anchor="w", padx=4, after=self.tree)
            self.root.overrideredirect(False)
            self.root.attributes("-alpha", 1.0)

    # ---------- floating bar overlays ----------

    def _toggle_bar_overlays(self):
        """Open the frame picker -- which floating frames are shown is a
        per-raider choice (a healer wants HoTs, a dps wants damage), so this
        is a checklist rather than a fixed set."""
        import overlay as ov
        if getattr(self, "_picker", None) and tk.Toplevel.winfo_exists(self._picker):
            self._picker.destroy()
            self._picker = None
            return

        win = tk.Toplevel(self.root)
        self._picker = win
        win.title("Overlay frames")
        win.configure(bg=BG)
        win.attributes("-topmost", True)

        # Toolbar row: lock toggle reads the same way the frames themselves
        # do (a filled pill = active), plus Clear all -- one row of actions
        # up top rather than a checkbox buried among the frame buttons.
        controls = ttk.Frame(win)
        controls.grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(12, 4))
        ttk.Checkbutton(
            controls, text="Lock positions", variable=self._overlays_locked,
            command=self._apply_lock_state, style="Overlay.Toolbutton",
        ).pack(side="left")
        ttk.Button(controls, text="Clear all", command=self._clear_overlays).pack(
            side="left", padx=(8, 0))
        ttk.Label(
            win, text="Locked frames are click-through and can't be dragged or closed.",
            foreground=INK_MUTED,
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 10))

        for col, group in enumerate(ov.OVERLAY_GROUPS):
            ttk.Label(win, text=group.upper(), foreground=INK_MUTED,
                      font=("Segoe UI", 8, "bold")).grid(
                row=2, column=col, padx=(12, 6), pady=(0, 6), sticky="w")
            row = 3
            for key, label, grp in ov.AVAILABLE_OVERLAYS:
                if grp != group:
                    continue
                var = self._overlay_vars.setdefault(key, tk.BooleanVar(value=False))
                ttk.Checkbutton(
                    win, text=label, variable=var, style="Overlay.Toolbutton",
                    width=20, command=lambda k=key: self._apply_overlay(k),
                ).grid(row=row, column=col, sticky="ew", padx=(12, 6), pady=3)
                row += 1

    def _restore_overlay_layout(self):
        """Recreates whatever frames were open, where they were, and the
        lock state -- called once at startup, before the picker has even
        been opened this session."""
        import overlay as ov
        layout = storage.load_overlay_layout()
        self._overlays_locked.set(bool(layout.get("locked", False)))
        valid_keys = {k for k, _label, _group in ov.AVAILABLE_OVERLAYS}
        for key, pos in layout.get("frames", {}).items():
            if key not in valid_keys:
                continue  # a saved key from a version that no longer exists
            self._overlay_vars.setdefault(key, tk.BooleanVar(value=True)).set(True)
            self._apply_overlay(key, pos=pos, persist=False)

    def _persist_overlay_layout(self):
        frames = {}
        for o in self.bar_overlays:
            key = getattr(o, "kind", None)
            if key:
                frames[key] = {"x": o.win.winfo_x(), "y": o.win.winfo_y()}
        storage.save_overlay_layout({
            "locked": self._overlays_locked.get(),
            "frames": frames,
        })

    def _apply_overlay(self, key, pos=None, persist=True):
        """Show or hide one frame, honouring its checkbox.
        pos: optional {"x": int, "y": int} to restore a saved position
        instead of the default stacked placement -- used on startup only."""
        import overlay as ov
        want = self._overlay_vars[key].get()
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
        if key == "timers":
            o = ov.TimerOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved)
        elif key == "alerts":
            o = ov.AlertOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved)
        elif key == "hots":
            o = ov.HotOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved)
        elif key in ("cooldowns", "dots"):
            o = ov.TimerOverlay(self.root, x=x, y=y, on_close=drop, on_move=moved)
            o.kind = key
        else:
            o = ov.BarOverlay(self.root, kind=key, x=x, y=y, on_close=drop, on_move=moved)
        # a frame opened while "Lock positions" is already checked should
        # start locked too, not silently draggable until the next toggle
        if self._overlays_locked.get():
            o.set_locked(True)
        self.bar_overlays.append(o)
        if persist:
            self._persist_overlay_layout()

    def _apply_lock_state(self):
        """One toggle governs every currently-visible frame, same as
        BARAS's single LOCKED control -- not a per-frame setting."""
        locked = self._overlays_locked.get()
        for o in self.bar_overlays:
            o.set_locked(locked)
        self._persist_overlay_layout()

    def _on_overlay_closed(self, o):
        """Right-clicking a frame dismisses it -- keep the checkbox in sync,
        otherwise the box stays ticked for a frame that no longer exists."""
        if o in self.bar_overlays:
            self.bar_overlays.remove(o)
        var = self._overlay_vars.get(getattr(o, "kind", None))
        if var is not None:
            var.set(False)
        self._persist_overlay_layout()

    def _clear_overlays(self):
        for o in list(self.bar_overlays):
            o.win.destroy()
        self.bar_overlays = []
        for var in self._overlay_vars.values():
            var.set(False)
        self._persist_overlay_layout()

    def _refresh_bar_overlays(self):
        if not self.bar_overlays:
            return
        rows, _dur = self.tracker.snapshot()
        boss = self.boss_state.status_text() if self.boss_state else None
        local = self.boss_state.local_player_name if self.boss_state else None
        for o in self.bar_overlays:
            kind = getattr(o, "kind", None)
            if kind in ("dps", "hps", "taken"):
                idx = {"dps": 1, "hps": 2, "taken": 3}[kind]
                data = sorted(((r[0], r[idx]) for r in rows), key=lambda r: -r[1])
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data),
                         subtitle=boss if kind == "dps" else None)
            elif kind == "absorbed":
                # Raw absorbed magnitude, not the percentage -- bars only
                # make sense as a comparable quantity across players, and
                # the live table already shows the percentage per person.
                # Reads PlayerStats directly since the snapshot() tuple is
                # kept compact and doesn't carry the raw number.
                entities = [p for p in self.tracker.current.players.values() if p.is_player]
                data = sorted(((p.name, p.damage_absorbed) for p in entities),
                              key=lambda r: -r[1])
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data))
            elif kind == "threat":
                # Same reasoning as "absorbed": reads PlayerStats directly
                # since the compact snapshot() tuple doesn't carry threat.
                entities = [p for p in self.tracker.current.players.values() if p.is_player]
                data = sorted(((p.name, p.threat) for p in entities), key=lambda r: -r[1])
                data = [r for r in data if r[1] > 0]
                o.render(data, total=sum(r[1] for r in data))
            elif kind == "hots":
                o.render(self.hot_tracker.expiring(within_seconds=o.within_seconds))
            elif kind == "cooldowns":
                o.render(self.timer_engine.snapshot("cooldown"))
            elif kind == "dots":
                o.render(self.timer_engine.snapshot("dot"))
            elif kind == "timers":
                o.render([t for t in self.timer_engine.snapshot()
                          if t[3] not in ("cooldown", "dot", "hot") and not t[4]])
            elif kind == "alerts":
                o.render([t for t in self.timer_engine.snapshot() if t[4]])

    def _drag_start(self, event):
        self._drag_offset = (event.x, event.y)

    def _drag_move(self, event):
        x = self.root.winfo_pointerx() - self._drag_offset[0]
        y = self.root.winfo_pointery() - self._drag_offset[1]
        self.root.geometry(f"+{x}+{y}")

    # ---------- live tab ----------

    def _build_live_tab(self):
        self.boss_var = tk.StringVar(value="")
        ttk.Label(self.live_tab, textvariable=self.boss_var, anchor="w",
                  foreground=INK, font=("Segoe UI", 11, "bold")).pack(
            fill="x", padx=4, pady=(6, 0))

        self.duration_var = tk.StringVar(value="Encounter: 0.0s")
        ttk.Label(self.live_tab, textvariable=self.duration_var, anchor="w",
                  foreground=INK_MUTED).pack(fill="x", padx=4, pady=(0, 4))

        columns = ("name", "dps", "hps", "taken", "mitigated", "deaths")
        self.tree = ttk.Treeview(self.live_tab, columns=columns, show="headings", height=8)
        headings = {
            "name": "Player", "dps": "DPS", "hps": "HPS", "taken": "Damage Taken",
            "mitigated": "Mitigated", "deaths": "Deaths",
        }
        widths = {"name": 150, "dps": 85, "hps": 85, "taken": 105, "mitigated": 85, "deaths": 60}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center")
        self.tree.column("name", anchor="w")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<Double-Button-1>", self._on_live_row_double_click)
        self.hint_label = ttk.Label(
            self.live_tab, text="double-click a player for an ability breakdown",
            foreground=INK_MUTED,
        )
        self.hint_label.pack(anchor="w", padx=4)

        self.alert_list_frame = ttk.Frame(self.live_tab)
        self.alert_list_frame.pack(fill="x", padx=4, pady=(4, 0))

        self._section(self.live_tab, "Active Timers")
        self.timer_list_frame = ttk.Frame(self.live_tab)
        self.timer_list_frame.pack(fill="x", padx=4, pady=(2, 4))

        self._section(self.live_tab, "Cooldowns")
        self.cooldown_list_frame = ttk.Frame(self.live_tab)
        self.cooldown_list_frame.pack(fill="x", padx=4, pady=(2, 4))

        self._section(self.live_tab, "DoTs / HoTs")
        self.dot_hot_list_frame = ttk.Frame(self.live_tab)
        self.dot_hot_list_frame.pack(fill="x", padx=4, pady=(2, 4))

        self._section(self.live_tab, "Taunts")
        self.taunt_list_frame = ttk.Frame(self.live_tab)
        self.taunt_list_frame.pack(fill="x", padx=4, pady=(2, 4))

    def _section(self, parent, text):
        """Small uppercase section heading — same visual role as the web UI's
        panel titles, so the two views read as one product."""
        ttk.Label(parent, text=text.upper(), anchor="w",
                  foreground=INK_MUTED, font=("Segoe UI", 8)).pack(
            fill="x", padx=4, pady=(8, 0))

    def _on_live_row_double_click(self, _event):
        item = self.tree.focus()
        if not item:
            return
        name = self.tree.item(item, "values")[0]
        player = self.tracker.current.player(name)
        if player:
            self._show_ability_breakdown(player)

    # ---------- history tab ----------

    def _build_history_tab(self):
        columns = ("pull", "duration", "top")
        self.history_tree = ttk.Treeview(
            self.history_tab, columns=columns, show="headings", height=12
        )
        headings = {"pull": "Pull #", "duration": "Duration", "top": "Top DPS"}
        widths = {"pull": 70, "duration": 100, "top": 340}
        for col in columns:
            self.history_tree.heading(col, text=headings[col])
            self.history_tree.column(col, width=widths[col], anchor="w")
        self.history_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.history_tree.bind("<Double-Button-1>", self._on_history_row_double_click)
        ttk.Label(
            self.history_tab, text="(double-click a pull to see per-player detail; "
            "history persists across restarts)", foreground="#666",
        ).pack(anchor="w", padx=4)

    def _on_history_row_double_click(self, _event):
        item = self.history_tree.focus()
        if not item:
            return
        pull_num = self.history_tree.item(item, "values")[0]
        # history is stored oldest-first; pull numbers were assigned 1-based
        idx = int(pull_num) - 1
        if 0 <= idx < len(self.tracker.history):
            self._show_pull_detail(self.tracker.history[idx], pull_num)

    def _show_pull_detail(self, encounter: Encounter, pull_num):
        win = tk.Toplevel(self.root)
        win.title(f"Pull #{pull_num} detail")
        win.geometry("480x300")

        columns = ("name", "dps", "hps", "taken", "mitigated", "deaths")
        tree = ttk.Treeview(win, columns=columns, show="headings")
        headings = {
            "name": "Player", "dps": "DPS", "hps": "HPS", "taken": "Damage Taken",
            "mitigated": "Mitigated", "deaths": "Deaths",
        }
        for col in columns:
            tree.heading(col, text=headings[col])
            tree.column(col, width=85, anchor="center")
        tree.column("name", anchor="w", width=140)
        tree.pack(fill="both", expand=True, padx=8, pady=8)

        for name, dps, hps, taken, mitigated, deaths in encounter.snapshot():
            mit_text = f"{mitigated:.0f}%" if (taken > 0 or mitigated > 0) else "—"
            tree.insert("", "end", values=(name, f"{dps:,.0f}", f"{hps:,.0f}", f"{taken:,.0f}", mit_text, deaths))

        def on_double_click(_event):
            item = tree.focus()
            if not item:
                return
            name = tree.item(item, "values")[0]
            player = encounter.player(name)
            if player:
                self._show_ability_breakdown(player)

        tree.bind("<Double-Button-1>", on_double_click)
        ttk.Label(win, text="(double-click a player for ability breakdown)", foreground="#666").pack(
            anchor="w", padx=8
        )
        ttk.Button(
            win, text="Upload This Pull to Parsely",
            command=lambda: self._upload_encounter(encounter),
        ).pack(anchor="w", padx=8, pady=(6, 8))

    def _show_ability_breakdown(self, player):
        win = tk.Toplevel(self.root)
        win.title(f"{player.name} — ability breakdown")
        win.geometry("420x400")

        dmg_rows, heal_rows = player.ability_breakdown()

        summary = ttk.Frame(win)
        summary.pack(fill="x", padx=8, pady=(8, 0))
        stat_parts = []
        if player.damage_attempts > 0:
            stat_parts.append(f"Accuracy {player.accuracy_pct():.1f}%")
            stat_parts.append(f"Crit {player.crit_pct():.1f}%")
        if player.heal_casts > 0:
            stat_parts.append(f"Heal Crit {player.heal_crit_pct():.1f}%")
        ttk.Label(summary, text="   |   ".join(stat_parts) or "No attacks/heals yet",
                  foreground=ACCENT, font=("", 10, "bold")).pack(anchor="w")

        ttk.Label(win, text="Damage by ability", font=("", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 0)
        )
        dmg_tree = ttk.Treeview(win, columns=("ability", "amount"), show="headings", height=6)
        dmg_tree.heading("ability", text="Ability")
        dmg_tree.heading("amount", text="Total")
        dmg_tree.column("ability", width=250, anchor="w")
        dmg_tree.column("amount", width=100, anchor="center")
        dmg_tree.pack(fill="x", padx=8, pady=4)
        for ability, amount in dmg_rows:
            dmg_tree.insert("", "end", values=(ability, f"{amount:,.0f}"))

        ttk.Label(win, text="Healing by ability", font=("", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 0)
        )
        heal_tree = ttk.Treeview(win, columns=("ability", "amount"), show="headings", height=6)
        heal_tree.heading("ability", text="Ability")
        heal_tree.heading("amount", text="Total")
        heal_tree.column("ability", width=250, anchor="w")
        heal_tree.column("amount", width=100, anchor="center")
        heal_tree.pack(fill="x", padx=8, pady=4)
        for ability, amount in heal_rows:
            heal_tree.insert("", "end", values=(ability, f"{amount:,.0f}"))

    # ---------- timers config tab ----------

    def _build_timers_tab(self):
        form = ttk.Frame(self.timers_tab)
        form.pack(fill="x", padx=4, pady=6)

        ttk.Label(form, text="Trigger keyword").grid(row=0, column=0, sticky="w")
        ttk.Label(form, text="Label / spoken text").grid(row=0, column=1, sticky="w")
        ttk.Label(form, text="Seconds").grid(row=0, column=2, sticky="w")
        ttk.Label(form, text="Warn (s before end)").grid(row=0, column=3, sticky="w")

        self.kw_entry = ttk.Entry(form, width=16)
        self.label_entry = ttk.Entry(form, width=18)
        self.dur_entry = ttk.Entry(form, width=7)
        self.warn_entry = ttk.Entry(form, width=7)
        self.kw_entry.grid(row=1, column=0, padx=(0, 4))
        self.label_entry.grid(row=1, column=1, padx=(0, 4))
        self.dur_entry.grid(row=1, column=2, padx=(0, 4))
        self.warn_entry.grid(row=1, column=3, padx=(0, 4))

        self.voice_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Voice alert", variable=self.voice_var).grid(
            row=1, column=4, padx=(6, 0)
        )

        ttk.Button(form, text="Add Timer Rule", command=self._add_rule).grid(
            row=1, column=5, padx=(6, 0)
        )

        ttk.Label(
            self.timers_tab,
            text="Fires a spoken alert (falls back to a beep if no TTS engine "
                 "is installed) the moment an ability/effect matching the "
                 "keyword appears, then counts down. Set 'Warn' to also get a "
                 "second alert that many seconds before the countdown ends "
                 "(e.g. 15s timer, warn at 3s). Rules are saved and reloaded "
                 "automatically.",
            wraplength=650,
            foreground="#555",
        ).pack(fill="x", padx=6, pady=(0, 6))

        columns = ("keyword", "label", "duration", "warn", "voice")
        self.rules_tree = ttk.Treeview(self.timers_tab, columns=columns, show="headings", height=8)
        for col, text, w in (
            ("keyword", "Keyword", 130), ("label", "Label", 160),
            ("duration", "Seconds", 70), ("warn", "Warn at", 70), ("voice", "Voice", 60),
        ):
            self.rules_tree.heading(col, text=text)
            self.rules_tree.column(col, width=w, anchor="w")
        self.rules_tree.pack(fill="both", expand=True, padx=4, pady=4)

        ttk.Button(self.timers_tab, text="Remove Selected Rule", command=self._remove_rule).pack(
            anchor="w", padx=4, pady=(0, 6)
        )

    def _add_rule(self):
        keyword = self.kw_entry.get().strip()
        label = self.label_entry.get().strip() or keyword
        try:
            duration = float(self.dur_entry.get().strip())
        except ValueError:
            return
        if not keyword:
            return
        warn_text = self.warn_entry.get().strip()
        try:
            warn_before = float(warn_text) if warn_text else 0.0
        except ValueError:
            warn_before = 0.0
        voice = self.voice_var.get()

        rule = TimerRule(
            keyword=keyword,
            label=label,
            duration_seconds=duration,
            voice_alert=voice,
            warn_seconds_before=warn_before,
        )
        self.timer_engine.add_rule(rule)
        self.rules_tree.insert(
            "", "end",
            values=(keyword, label, duration, warn_before or "-", "on" if voice else "off"),
        )
        self.kw_entry.delete(0, "end")
        self.label_entry.delete(0, "end")
        self.dur_entry.delete(0, "end")
        self.warn_entry.delete(0, "end")
        self._save_custom_rules()

    def _remove_rule(self):
        item = self.rules_tree.focus()
        if not item:
            return
        idx = self.rules_tree.index(item)
        self.rules_tree.delete(item)
        self.timer_engine.remove_rule(idx)
        self._save_custom_rules()

    def _save_custom_rules(self):
        # Only persist manually-added (Timers tab) rules -- boss and
        # cooldown rules are re-registered fresh by main.py on every
        # startup, so saving them here too would duplicate them each time
        # the user adds/removes a custom rule and the app is restarted.
        custom_rules = [r for r in self.timer_engine.rules if r.category == "custom"]
        storage.save_timer_rules(custom_rules)

    # ---------- import tab ----------

    def _build_import_tab(self):
        ttk.Label(
            self.import_tab,
            text="Import one or more saved .txt log files (e.g. from teammates) "
                 "and merge them into a single combined encounter. Overlapping "
                 "events across files are de-duplicated automatically -- this "
                 "is for filling visibility gaps, not required for normal use, "
                 "since your own log already includes the whole group's actions.",
            wraplength=620,
            foreground="#555",
        ).pack(fill="x", padx=6, pady=8)

        ttk.Button(self.import_tab, text="Choose Log Files...", command=self._import_logs).pack(
            anchor="w", padx=6
        )

        self.import_result_var = tk.StringVar(value="")
        ttk.Label(self.import_tab, textvariable=self.import_result_var, foreground="#333").pack(
            anchor="w", padx=6, pady=(8, 0)
        )

    def _import_logs(self):
        paths = filedialog.askopenfilenames(
            title="Select SWTOR combat log file(s)", filetypes=[("Log files", "*.txt")]
        )
        if not paths:
            return
        try:
            from log_merger import merge_logs
            encounter = merge_logs(paths)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))
            return

        if not encounter.players:
            self.import_result_var.set("No recognizable combat events found in the selected file(s).")
            return

        self.tracker.history.append(encounter)
        storage.append_history_entry(encounter)
        self.import_result_var.set(
            f"Imported {len(paths)} file(s) -> merged encounter, "
            f"{encounter.duration():.1f}s, {len(encounter.players)} players. "
            f"See it in the History tab."
        )

    # ---------- parsely tab ----------

    def _build_parsely_tab(self):
        settings = storage.load_parsely_settings()

        form = ttk.Frame(self.parsely_tab)
        form.pack(fill="x", padx=6, pady=8)

        ttk.Label(form, text="Parsely username (optional)").grid(row=0, column=0, sticky="w")
        self.parsely_user_entry = ttk.Entry(form, width=24)
        self.parsely_user_entry.insert(0, settings.get("username", ""))
        self.parsely_user_entry.grid(row=0, column=1, sticky="w", padx=(4, 0))

        ttk.Label(form, text="Password").grid(row=1, column=0, sticky="w")
        self.parsely_pass_entry = ttk.Entry(form, width=24, show="*")
        self.parsely_pass_entry.insert(0, settings.get("password", ""))
        self.parsely_pass_entry.grid(row=1, column=1, sticky="w", padx=(4, 0))

        ttk.Label(form, text="Guild (optional)").grid(row=2, column=0, sticky="w")
        self.parsely_guild_entry = ttk.Entry(form, width=24)
        self.parsely_guild_entry.insert(0, settings.get("guild", ""))
        self.parsely_guild_entry.grid(row=2, column=1, sticky="w", padx=(4, 0))

        self.parsely_guild_log_var = tk.BooleanVar(value=settings.get("guild_log", False))
        ttk.Checkbutton(
            form, text="Tag all participants as guild members", variable=self.parsely_guild_log_var
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Label(form, text="Default visibility").grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.parsely_visibility_var = tk.IntVar(value=settings.get("visibility", 1))
        vis_frame = ttk.Frame(form)
        vis_frame.grid(row=4, column=1, sticky="w", pady=(4, 0))
        for label, value in (("Public", 1), ("Guild only", 2), ("Private", 0)):
            ttk.Radiobutton(
                vis_frame, text=label, value=value, variable=self.parsely_visibility_var
            ).pack(side="left")

        ttk.Button(form, text="Save Settings", command=self._save_parsely_settings).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        ttk.Label(
            self.parsely_tab,
            text="Password is stored locally in plain text (same tradeoff BARAS itself "
                 "makes) -- fine for a personal machine, don't share the settings file.",
            foreground="#888", wraplength=560,
        ).pack(anchor="w", padx=6, pady=(0, 8))

        ttk.Separator(self.parsely_tab).pack(fill="x", padx=6, pady=6)

        ttk.Button(
            self.parsely_tab, text="Upload Current Log (whole file)",
            command=self._upload_current_log,
        ).pack(anchor="w", padx=6)

        self.parsely_status_var = tk.StringVar(value="")
        ttk.Label(self.parsely_tab, textvariable=self.parsely_status_var, foreground="#333").pack(
            anchor="w", padx=6, pady=(6, 0)
        )

        ttk.Label(
            self.parsely_tab,
            text="To upload just one pull instead of the whole session log, use the "
                 "History tab -> double-click a pull -> Upload This Pull.",
            foreground="#666", wraplength=560,
        ).pack(anchor="w", padx=6, pady=(10, 0))

    def _save_parsely_settings(self):
        settings = {
            "username": self.parsely_user_entry.get().strip(),
            "password": self.parsely_pass_entry.get(),
            "guild": self.parsely_guild_entry.get().strip(),
            "guild_log": self.parsely_guild_log_var.get(),
            "visibility": self.parsely_visibility_var.get(),
        }
        storage.save_parsely_settings(settings)
        self.parsely_status_var.set("Settings saved.")

    def _current_parsely_settings(self) -> dict:
        return {
            "username": self.parsely_user_entry.get().strip() or None,
            "password": self.parsely_pass_entry.get() or None,
            "guild": self.parsely_guild_entry.get().strip() or None,
            "guild_log": self.parsely_guild_log_var.get(),
            "visibility": self.parsely_visibility_var.get(),
        }

    def _upload_current_log(self):
        path = self.tracker.current_log_path
        if not path:
            messagebox.showinfo("Parsely", "No active log file yet -- get into combat first.")
            return
        notes = simpledialog.askstring("Parsely upload", "Optional note for this upload:") or None
        settings = self._current_parsely_settings()
        self.parsely_status_var.set("Uploading...")

        def _run():
            from parsely_upload import upload_file
            result = upload_file(
                path,
                visibility=settings["visibility"],
                notes=notes,
                username=settings["username"],
                password=settings["password"],
                guild=settings["guild"],
                guild_log=settings["guild_log"],
            )
            self.root.after(0, lambda: self._on_parsely_result(result))

        threading.Thread(target=_run, daemon=True).start()

    def _upload_encounter(self, encounter: Encounter):
        if not encounter.log_path or encounter.start_line is None or encounter.end_line is None:
            messagebox.showinfo(
                "Parsely",
                "This pull doesn't have line-range data (recorded before this feature, "
                "or an imported/merged log) -- can't upload just this pull.",
            )
            return
        notes = simpledialog.askstring("Parsely upload", "Optional note for this upload:") or None
        settings = self._current_parsely_settings()
        self.parsely_status_var.set("Uploading...")

        def _run():
            from parsely_upload import upload_encounter
            result = upload_encounter(
                encounter.log_path,
                encounter.start_line,
                encounter.end_line,
                area_entered_line=encounter.area_entered_line,
                visibility=settings["visibility"],
                notes=notes,
                username=settings["username"],
                password=settings["password"],
                guild=settings["guild"],
                guild_log=settings["guild_log"],
            )
            self.root.after(0, lambda: self._on_parsely_result(result))

        threading.Thread(target=_run, daemon=True).start()

    def _on_parsely_result(self, result):
        if result.success:
            self.parsely_status_var.set(f"Uploaded: {result.link}")
            messagebox.showinfo("Parsely upload", f"Uploaded successfully:\n{result.link}")
        else:
            self.parsely_status_var.set(f"Upload failed: {result.error}")
            messagebox.showerror("Parsely upload failed", result.error or "Unknown error")

    # ---------- refresh loop ----------

    def _drain_status(self):
        latest = None
        try:
            while True:
                latest = self.status_queue.get_nowait()
        except queue.Empty:
            pass
        if latest:
            self.status_var.set(latest)

    def _refresh(self):
        self._drain_status()
        self.timer_engine.tick()

        rows, duration = self.tracker.snapshot()
        self.duration_var.set(f"Encounter: {duration:.1f}s")
        if self.boss_state is not None:
            self.boss_var.set(self.boss_state.status_text())
        self.tree.delete(*self.tree.get_children())
        for name, dps, hps, taken, mitigated, deaths in rows:
            # blank rather than "0%" when nothing has hit this entity yet --
            # a dps who's taken zero damage isn't "mitigating 0%", there's
            # just no data. Checking taken OR mitigated (not just taken)
            # matters for the edge case of a hit that was 100% absorbed:
            # taken stays 0 but mitigated_pct is a real 100, not "no data".
            mit_text = f"{mitigated:.0f}%" if (taken > 0 or mitigated > 0) else "—"
            self.tree.insert(
                "", "end",
                values=(name, f"{dps:,.0f}", f"{hps:,.0f}", f"{taken:,.0f}", mit_text, deaths),
            )

        self._refresh_alerts()
        self._refresh_timers()
        self._refresh_cooldowns()
        self._refresh_dots_hots()
        self._refresh_taunts()
        self._refresh_history()
        self._refresh_bar_overlays()

        self.root.after(REFRESH_MS, self._refresh)

    # Categories that have their own dedicated panel below the meter, and so
    # must be excluded from the general "Active Timers" list (which shows boss
    # mechanics and manual Timers-tab rules).
    _OWN_PANEL_CATEGORIES = ("cooldown", "dot", "hot")

    def _refresh_timers(self):
        for child in self.timer_list_frame.winfo_children():
            child.destroy()
        for label, remaining, total, category, is_alert in self.timer_engine.snapshot():
            if category in self._OWN_PANEL_CATEGORIES or is_alert:
                continue
            self._timer_row(self.timer_list_frame, label, remaining, total, "Boss")

    def _timer_row(self, parent, label, remaining, total, style_name, width=30):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=label, width=width, anchor="w",
                  foreground=INK).pack(side="left")
        ttk.Label(row, text=f"{remaining:4.1f}s", width=6, anchor="e",
                  foreground=INK_MUTED).pack(side="left", padx=(0, 8))
        ttk.Progressbar(row, maximum=total, value=remaining, length=180,
                        style=f"{style_name}.Horizontal.TProgressbar"
                        ).pack(side="left", fill="x", expand=True)

    def _refresh_dots_hots(self):
        for child in self.dot_hot_list_frame.winfo_children():
            child.destroy()
        rows = self.timer_engine.snapshot("dot") + self.timer_engine.snapshot("hot")
        rows.sort(key=lambda r: r[1])  # soonest-expiring first, across both
        for label, remaining, total, category, _is_alert in rows:
            tag = "DoT" if category == "dot" else "HoT"
            self._timer_row(self.dot_hot_list_frame, f"{tag}  {label}",
                            remaining, total, "Dot", width=32)

    def _refresh_cooldowns(self):
        for child in self.cooldown_list_frame.winfo_children():
            child.destroy()
        for label, remaining, total, _category, _is_alert in self.timer_engine.snapshot("cooldown"):
            self._timer_row(self.cooldown_list_frame, label, remaining, total, "Cooldown")

    def _refresh_alerts(self):
        # Alert-flagged boss timers skip the numeric countdown entirely --
        # just a bold callout for as long as it's active (e.g. "Move Out",
        # "Spread!"), matching BARAS's own is_alert display mode.
        for child in self.alert_list_frame.winfo_children():
            child.destroy()
        rows = [r for r in self.timer_engine.snapshot() if r[4]]
        if not rows:
            return
        for label, _remaining, _total, _category, _is_alert in rows:
            ttk.Label(self.alert_list_frame, text=label.upper(), foreground=CRITICAL,
                      font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=1)

    def _refresh_taunts(self):
        # tick() here (rather than only on the reader thread) is what lets a
        # taunt that got no reply at all still flip from pending to a
        # visible miss -- otherwise a genuinely failed taunt with no further
        # log lines would sit unresolved until the next unrelated event.
        self.taunt_tracker.tick()
        for child in self.taunt_list_frame.winfo_children():
            child.destroy()
        if not self.taunt_tracker.history:
            ttk.Label(self.taunt_list_frame, text="no taunts cast yet",
                      foreground=INK_MUTED).pack(anchor="w")
            return
        now = time.time()
        for result in self.taunt_tracker.history:
            ago = max(0.0, now - result.at)
            if result.hit:
                if result.kind == "aoe":
                    text = f"landed on {len(result.targets)} targets"
                else:
                    text = f"landed on {result.targets[0]}"
                colour = GOOD
                mark = "✓"
            else:
                text = "no target hit — resisted, out of range, or immune"
                colour = CRITICAL
                mark = "✗"
            row = ttk.Frame(self.taunt_list_frame)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=f"{mark} {text}", foreground=colour, anchor="w").pack(
                side="left", fill="x", expand=True)
            ttk.Label(row, text=f"{ago:4.1f}s ago", foreground=INK_MUTED, width=10, anchor="e").pack(
                side="left")

    def _refresh_history(self):
        rows = self.tracker.history_snapshot()
        if len(rows) <= self._history_rows_shown:
            return
        new_count = len(rows) - self._history_rows_shown
        # rows are most-recent-first; the newest `new_count` entries are new
        for pull_num, duration, player_rows in rows[:new_count]:
            top = ", ".join(f"{n} {d:,.0f}" for n, d, h, t, mit, dth in player_rows[:3])
            self.history_tree.insert("", 0, values=(pull_num, f"{duration:.1f}s", top))
        self._history_rows_shown = len(rows)

    def run(self):
        self.root.mainloop()
