"""
web_server.py

Local web server behind the whole pywebview UI (Live, History, Timers,
Overlays picker, Import Logs, Parsely) -- stdlib only (http.server +
json), same approach as analysis/webapp.py, reusing that module's
app.css so every web surface of this app shares one visual language.

Binds to 127.0.0.1 only: no auth, local data.

Endpoints that need a native file-picker (Import Logs, "Upload a Log
File...") are NOT here -- browsers can't hand back a real filesystem
path from a picker, only file contents. Those go through pywebview's
own js_api bridge instead (see main.py's Api class); this server just
receives the paths that bridge already resolved.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import storage

STATIC_DIR = Path(__file__).resolve().parent / "web_ui"
SHARED_CSS = Path(__file__).resolve().parent / "analysis" / "static" / "app.css"

# Categories with their own dedicated panel, so they're excluded from the
# general "Active Timers" list -- mirrors the old gui.py's _OWN_PANEL_CATEGORIES.
_OWN_PANEL_CATEGORIES = ("cooldown", "dot", "hot")


def _timer_rows(rows):
    return [
        {"label": label, "remaining": round(remaining, 1), "total": total}
        for label, remaining, total, _category, _is_alert in rows
    ]


def build_live_snapshot(tracker, timer_engine, boss_state, taunt_tracker, status) -> dict:
    rows, duration = tracker.snapshot()
    players = [
        {"name": name, "dps": dps, "hps": hps, "taken": taken,
         "mitigated": mitigated, "deaths": deaths}
        for name, dps, hps, taken, mitigated, deaths in rows
    ]

    all_timers = timer_engine.snapshot()
    alerts = [t[0] for t in all_timers if t[4]]
    active_timers = _timer_rows(
        t for t in all_timers if t[3] not in _OWN_PANEL_CATEGORIES and not t[4]
    )
    cooldowns = _timer_rows(timer_engine.snapshot("cooldown"))
    dots_hots_raw = timer_engine.snapshot("dot") + timer_engine.snapshot("hot")
    dots_hots_raw.sort(key=lambda r: r[1])
    dots_hots = [
        {"tag": "DoT" if category == "dot" else "HoT", "label": label,
         "remaining": round(remaining, 1), "total": total}
        for label, remaining, total, category, _is_alert in dots_hots_raw
    ]

    now = time.time()
    taunts = []
    for result in taunt_tracker.history:
        ago = max(0.0, now - result.at)
        if result.hit:
            text = (f"landed on {len(result.targets)} targets" if result.kind == "aoe"
                    else f"landed on {result.targets[0]}")
        else:
            text = "no target hit — resisted, out of range, or immune"
        taunts.append({"hit": result.hit, "text": text, "ago": round(ago, 1)})

    return {
        "boss": boss_state.status_text() if boss_state else None,
        "watching": status.text,
        "duration": round(duration, 1),
        "players": players,
        "alerts": alerts,
        "timers": active_timers,
        "cooldowns": cooldowns,
        "dots_hots": dots_hots,
        "taunts": taunts,
    }


def _player_row(name, dps, hps, taken, mitigated, deaths):
    return {"name": name, "dps": dps, "hps": hps, "taken": taken,
            "mitigated": mitigated, "deaths": deaths}


def build_history_list(tracker) -> list:
    rows = tracker.history_snapshot()  # most-recent-first: (pull_num, duration, player_rows)
    out = []
    for pull_num, duration, player_rows in rows:
        top = [{"name": n, "dps": round(d)} for n, d, _h, _t, _m, _dth in player_rows[:3]]
        out.append({"pull": pull_num, "duration": round(duration, 1), "top": top})
    out.reverse()  # oldest-first, matching the old Tk tree's insert order
    return out


def build_history_detail(tracker, idx: int):
    if not (0 <= idx < len(tracker.history)):
        return None
    encounter = tracker.history[idx]
    players = [_player_row(*row) for row in encounter.snapshot()]
    return {
        "pull": idx + 1,
        "label": encounter.label,
        "duration": round(encounter.duration(), 1),
        "players": players,
        "can_upload": bool(encounter.log_path) and encounter.start_line is not None
                       and encounter.end_line is not None,
    }


def build_ability_breakdown(encounter, player_name, boss_state):
    player = encounter.player(player_name)
    if player is None:
        return None
    duration = encounter.duration()
    dmg_rows, heal_rows = player.ability_breakdown()
    target_dmg_rows, _target_heal_rows = player.target_breakdown()

    stats = {"apm": round(player.apm(duration), 1)}
    if player.damage_events:
        stats["burst_dps"] = round(player.burst_dps())
    if player.heal_events:
        stats["burst_hps"] = round(player.burst_hps())
    if player.damage_attempts > 0:
        stats["accuracy_pct"] = round(player.accuracy_pct(), 1)
        stats["crit_pct"] = round(player.crit_pct(), 1)
    if player.heal_casts > 0:
        stats["heal_crit_pct"] = round(player.heal_crit_pct(), 1)
    if player.times_interrupted > 0:
        stats["times_interrupted"] = player.times_interrupted
    if player.cc_casts > 0:
        stats["cc_casts"] = player.cc_casts
    if player.raid_buff_casts > 0:
        stats["raid_buff_casts"] = player.raid_buff_casts
    # Boss-only DPS uses the CURRENT live boss_state, same as the old Tk
    # ability-breakdown popup did -- for a historical pull this reflects
    # whatever boss is active right now, not necessarily that pull's boss.
    # Preserved as-is rather than "fixed" here since it's a pre-existing
    # behavior, not something introduced by this migration.
    boss = boss_state.active_boss if boss_state else None
    if boss is not None:
        boss_dmg = player.damage_to(boss.boss_names)
        if boss_dmg > 0:
            stats["boss_dps"] = round(boss_dmg / duration) if duration > 0 else None

    return {
        "name": player.name,
        "stats": stats,
        "damage_by_ability": [{"ability": a, "amount": round(v)} for a, v in dmg_rows],
        "healing_by_ability": [{"ability": a, "amount": round(v)} for a, v in heal_rows],
        "damage_by_target": [{"target": t, "amount": round(v)} for t, v in target_dmg_rows],
        "cc_by_ability": [{"ability": a, "amount": n} for a, n in
                           sorted(player.cc_by_ability.items(), key=lambda kv: -kv[1])],
        "raid_buff_by_ability": [{"ability": a, "amount": n} for a, n in
                                  sorted(player.raid_buff_by_ability.items(), key=lambda kv: -kv[1])],
    }


def make_handler(tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # keep the console clean

        # ---------------------------------------------------------- utils

        def _send(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                pass

        def _json(self, obj, status: int = 200):
            self._send(json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", status)

        def _static(self, path: Path, ctype: str):
            if not path.exists():
                self._send(b"not found", "text/plain", 404)
                return
            self._send(path.read_bytes(), ctype)

        def _body_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        def _upload_result_json(self, result):
            if result.success:
                return {"success": True, "link": result.link}
            return {"success": False, "error": result.error or "Unknown error"}

        def _parsely_settings_from_body(self, body: dict) -> dict:
            existing = storage.load_parsely_settings()
            return {
                "username": (body.get("username") or "").strip(),
                "password": body.get("password") or "",
                "guild": (body.get("guild") or "").strip(),
                "guild_log": bool(body.get("guild_log", existing.get("guild_log", False))),
                "visibility": int(body.get("visibility", existing.get("visibility", 1))),
            }

        def _save_custom_rules(self):
            custom_rules = [r for r in timer_engine.rules if r.category == "custom"]
            storage.save_timer_rules(custom_rules)

        # ------------------------------------------------------------ GET

        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]

            if u.path in ("/", "/index.html"):
                return self._static(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if u.path == "/app.js":
                return self._static(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            if u.path == "/app.css":
                return self._static(SHARED_CSS, "text/css; charset=utf-8")

            if u.path == "/api/live":
                return self._json(build_live_snapshot(tracker, timer_engine, boss_state, taunt_tracker, status))

            if u.path == "/api/history":
                return self._json(build_history_list(tracker))

            # /api/history/<idx>
            if len(parts) == 3 and parts[:2] == ["api", "history"] and parts[2].isdigit():
                idx = int(parts[2]) - 1
                detail = build_history_detail(tracker, idx)
                if detail is None:
                    return self._json({"error": "no such pull"}, 404)
                return self._json(detail)

            # /api/history/<idx>/player/<name>
            if (len(parts) == 5 and parts[:2] == ["api", "history"] and parts[2].isdigit()
                    and parts[3] == "player"):
                idx = int(parts[2]) - 1
                if not (0 <= idx < len(tracker.history)):
                    return self._json({"error": "no such pull"}, 404)
                breakdown = build_ability_breakdown(tracker.history[idx], unquote(parts[4]), boss_state)
                if breakdown is None:
                    return self._json({"error": "no such player"}, 404)
                return self._json(breakdown)

            if u.path == "/api/timer_rules":
                custom = [r for r in timer_engine.rules if r.category == "custom"]
                return self._json([
                    {"index": i, "keyword": r.keyword, "label": r.label,
                     "duration": r.duration_seconds, "warn": r.warn_seconds_before,
                     "voice": r.voice_alert}
                    for i, r in enumerate(custom)
                ])

            if u.path == "/api/overlays":
                return self._json(overlay_manager.overlay_state())

            if u.path == "/api/parsely_settings":
                settings = dict(storage.load_parsely_settings())
                settings.pop("password", None)  # never echo the stored password back
                return self._json(settings)

            return self._send(b"not found", "text/plain", 404)

        # ----------------------------------------------------------- POST

        def do_POST(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            body = self._body_json()

            if u.path == "/api/timer_rules":
                from timers import TimerRule
                try:
                    duration = float(body.get("duration"))
                except (TypeError, ValueError):
                    return self._json({"error": "invalid duration"}, 400)
                keyword = (body.get("keyword") or "").strip()
                if not keyword:
                    return self._json({"error": "keyword required"}, 400)
                label = (body.get("label") or "").strip() or keyword
                try:
                    warn = float(body.get("warn") or 0.0)
                except (TypeError, ValueError):
                    warn = 0.0
                rule = TimerRule(
                    keyword=keyword, label=label, duration_seconds=duration,
                    voice_alert=bool(body.get("voice", True)), warn_seconds_before=warn,
                )
                timer_engine.add_rule(rule)
                self._save_custom_rules()
                return self._json({"ok": True})

            if u.path == "/api/timer_rules/delete":
                try:
                    display_index = int(body.get("index"))
                except (TypeError, ValueError):
                    return self._json({"error": "invalid index"}, 400)
                # timer_engine.rules mixes boss/cooldown/custom rules together
                # (boss/cooldown ones registered first by main.py) -- the
                # index the web page shows is 0-based among CUSTOM rules
                # only, so it has to be mapped to its real position, not
                # used directly against the full list.
                custom_indices = [i for i, r in enumerate(timer_engine.rules) if r.category == "custom"]
                if not (0 <= display_index < len(custom_indices)):
                    return self._json({"error": "no such rule"}, 404)
                timer_engine.remove_rule(custom_indices[display_index])
                self._save_custom_rules()
                return self._json({"ok": True})

            if u.path == "/api/overlays/toggle":
                overlay_manager.toggle_overlay(body.get("key", ""))
                return self._json({"ok": True})

            if u.path == "/api/overlays/lock":
                overlay_manager.set_lock(bool(body.get("locked", False)))
                return self._json({"ok": True})

            if u.path == "/api/overlays/clear":
                overlay_manager.clear_all()
                return self._json({"ok": True})

            if u.path == "/api/parsely_settings":
                settings = self._parsely_settings_from_body(body)
                # a blank password field means "keep what's already saved",
                # not "erase the stored password" -- the web form never
                # shows the real password back (see the GET handler), so an
                # empty submit is ambiguous with "user cleared it on purpose"
                # and keeping the old value is the safer default.
                if not settings["password"]:
                    settings["password"] = storage.load_parsely_settings().get("password", "")
                storage.save_parsely_settings(settings)
                return self._json({"ok": True})

            if u.path == "/api/parsely/upload_path":
                from parsely_upload import upload_file
                path = body.get("path")
                if not path:
                    return self._json({"success": False, "error": "no file selected"})
                settings = storage.load_parsely_settings()
                result = upload_file(
                    path, visibility=settings["visibility"], notes=body.get("notes") or None,
                    username=settings["username"] or None, password=settings["password"] or None,
                    guild=settings["guild"] or None, guild_log=settings["guild_log"],
                )
                return self._json(self._upload_result_json(result))

            if u.path == "/api/parsely/upload_current":
                path = tracker.current_log_path
                if not path:
                    return self._json({"success": False,
                                        "error": "No active log file yet -- get into combat first."})
                from parsely_upload import upload_file
                settings = storage.load_parsely_settings()
                result = upload_file(
                    path, visibility=settings["visibility"], notes=body.get("notes") or None,
                    username=settings["username"] or None, password=settings["password"] or None,
                    guild=settings["guild"] or None, guild_log=settings["guild_log"],
                )
                return self._json(self._upload_result_json(result))

            # /api/history/<idx>/upload
            if (len(parts) == 4 and parts[:2] == ["api", "history"] and parts[2].isdigit()
                    and parts[3] == "upload"):
                idx = int(parts[2]) - 1
                if not (0 <= idx < len(tracker.history)):
                    return self._json({"error": "no such pull"}, 404)
                encounter = tracker.history[idx]
                if not encounter.log_path or encounter.start_line is None or encounter.end_line is None:
                    return self._json({
                        "success": False,
                        "error": "This pull doesn't have line-range data (recorded before this "
                                 "feature, or an imported/merged log) -- can't upload just this pull.",
                    })
                from parsely_upload import upload_encounter
                settings = storage.load_parsely_settings()
                result = upload_encounter(
                    encounter.log_path, encounter.start_line, encounter.end_line,
                    area_entered_line=encounter.area_entered_line,
                    visibility=settings["visibility"], notes=body.get("notes") or None,
                    username=settings["username"] or None, password=settings["password"] or None,
                    guild=settings["guild"] or None, guild_log=settings["guild_log"],
                )
                return self._json(self._upload_result_json(result))

            if u.path == "/api/import/merge":
                from log_merger import merge_logs
                paths = body.get("paths") or []
                if not paths:
                    return self._json({"error": "no files selected"}, 400)
                try:
                    encounter = merge_logs(paths)
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)
                if not encounter.players:
                    return self._json({
                        "imported": 0,
                        "message": "No recognizable combat events found in the selected file(s).",
                    })
                tracker.history.append(encounter)
                storage.append_history_entry(encounter)
                return self._json({
                    "imported": 1,
                    "message": f"Imported {len(paths)} file(s) -> merged encounter, "
                               f"{encounter.duration():.1f}s, {len(encounter.players)} players. "
                               f"See it in the History tab.",
                })

            if u.path == "/api/import/session":
                from analysis.corpus import replay_pulls
                paths = body.get("paths") or []
                if not paths:
                    return self._json({"error": "no files selected"}, 400)
                definitions = boss_state.definitions if boss_state else {}
                existing = {(e.log_path, e.start_line, e.end_line) for e in tracker.history}
                imported = skipped = duplicates = 0
                labels = []
                try:
                    for path in paths:
                        for pull in replay_pulls(path, definitions):
                            encounter = pull["encounter"]
                            total_damage = sum(p.damage_done for p in encounter.players.values())
                            if total_damage <= 0:
                                skipped += 1
                                continue
                            key = (encounter.log_path, encounter.start_line, encounter.end_line)
                            if key in existing:
                                duplicates += 1
                                continue
                            encounter.label = pull["boss_name"] or "Unknown fight"
                            tracker.history.append(encounter)
                            storage.append_history_entry(encounter)
                            existing.add(key)
                            imported += 1
                            labels.append(encounter.label)
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)

                if imported == 0:
                    reason = "already in History" if duplicates else "no real fights found"
                    return self._json({
                        "imported": 0,
                        "message": f"Nothing new to import from the selected file(s) ({reason}).",
                    })
                preview = ", ".join(labels[:5]) + ("..." if len(labels) > 5 else "")
                extra = f", {duplicates} already imported" if duplicates else ""
                return self._json({
                    "imported": imported,
                    "message": f"Imported {imported} pull(s) from {len(paths)} file(s) "
                               f"({skipped} trivial slivers skipped{extra}): {preview}. See History tab.",
                })

            return self._send(b"not found", "text/plain", 404)

    return Handler


def make_server(tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status,
                 port: int = 8766) -> ThreadingHTTPServer:
    handler = make_handler(tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status)
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
