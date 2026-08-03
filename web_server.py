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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import storage
from stats import rotation_segments

STATIC_DIR = Path(__file__).resolve().parent / "web_ui"
SHARED_CSS = Path(__file__).resolve().parent / "analysis" / "static" / "app.css"

# Categories with their own dedicated panel, so they're excluded from the
# general "Active Timers" list -- mirrors the old gui.py's _OWN_PANEL_CATEGORIES.
_OWN_PANEL_CATEGORIES = ("cooldown", "dot", "hot")


def _safe_encounter_id(raw: str) -> str:
    """Encounter ids become filenames (`{id}.json`) under the user boss
    dir -- reject anything that isn't a plain path segment so a malicious
    or buggy request body can't escape that directory (e.g. id="../../foo")."""
    raw = (raw or "").strip()
    if not raw or "/" in raw or "\\" in raw or raw in (".", ".."):
        return ""
    return raw


def _timer_rows(rows):
    return [
        {"label": label, "remaining": round(remaining, 1), "total": total, "target": target}
        for label, remaining, total, _category, _is_alert, target in rows
    ]


def build_live_snapshot(tracker, timer_engine, boss_state, taunt_tracker, status) -> dict:
    # display_encounter() (not .current directly): keeps a just-finished
    # pull's numbers on the live tab through the between-pulls downtime
    # instead of blanking the instant a stray non-combat event rolls
    # `current` over to a fresh, empty Encounter() -- see
    # StatsTracker.display_encounter(). One call reused for both the row
    # data and the per-player extras below so they can't disagree about
    # which encounter is being shown.
    encounter = tracker.display_encounter()
    rows, duration = encounter.snapshot(), encounter.duration()
    active_boss = boss_state.active_boss if boss_state else None
    boss_names = active_boss.boss_names if active_boss else []
    players = []
    for name, dps, hps, taken, mitigated, deaths in rows:
        p = encounter.players.get(name)
        players.append({
            "name": name, "dps": dps, "hps": hps, "taken": taken,
            "mitigated": mitigated, "deaths": deaths,
            "boss_dps": p.boss_dps(boss_names, duration) if p else 0.0,
            "effective_hps": p.effective_hps(duration) if p else 0.0,
        })

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
         "remaining": round(remaining, 1), "total": total, "target": target}
        for label, remaining, total, category, _is_alert, target in dots_hots_raw
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
        stats["effective_hps"] = round(player.effective_hps(duration))
    if player.times_interrupted > 0:
        stats["times_interrupted"] = player.times_interrupted
    if player.cc_casts > 0:
        stats["cc_casts"] = player.cc_casts
    if player.raid_buff_casts > 0:
        stats["raid_buff_casts"] = player.raid_buff_casts
    # Boss-only DPS for THIS pull's own boss -- matched by encounter.label
    # (set from the boss name at import/completion time) against the
    # definitions' own .name, not boss_state.active_boss. That used to mean
    # a historical pull's boss DPS reflected whatever boss happened to be
    # live right now (wrong/missing for anything reviewed outside of an
    # active encounter) -- fixed here since History is exactly where this
    # stat needs to work.
    boss_names = None
    if encounter.label and boss_state:
        for definition in boss_state.definitions.values():
            if definition.name == encounter.label:
                boss_names = definition.boss_names
                break
    if boss_names:
        boss_dmg = player.damage_to(boss_names)
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


def make_handler(tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status,
                  update_holder=None, request_shutdown=None, character_settings=None,
                  bundled_boss_dir=None, user_boss_dir=None):
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

            if u.path == "/api/update":
                # update_holder.result is set once, in the background, by
                # the startup check in main.py -- this just returns whatever
                # it currently is (None until that check completes, or
                # forever if it found nothing newer / couldn't reach GitHub).
                result = update_holder.result if update_holder else None
                if result is None:
                    return self._json({"available": False})
                return self._json({"available": True, **result})

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

            # /api/history/<idx>/deaths and /api/history/<idx>/timeline -- same
            # (log_path, start_line, end_line) re-read the corpus browser's
            # forensics/timeline views already use, just pointed at a pull
            # from live/imported History instead of the corpus index.
            if (len(parts) == 4 and parts[:2] == ["api", "history"] and parts[2].isdigit()
                    and parts[3] in ("deaths", "timeline")):
                idx = int(parts[2]) - 1
                if not (0 <= idx < len(tracker.history)):
                    return self._json({"error": "no such pull"}, 404)
                encounter = tracker.history[idx]
                if not encounter.log_path or encounter.start_line is None or encounter.end_line is None:
                    return self._json({"error": "This pull doesn't have line-range data "
                                                 "(imported/merged, or recorded before this feature)."}, 404)
                try:
                    if parts[3] == "deaths":
                        from analysis import forensics
                        reports = forensics.analyze_deaths(encounter.log_path, encounter.start_line,
                                                             encounter.end_line)
                        return self._json({"reports": reports, "summary": forensics.summarize_deaths(reports)})
                    else:
                        from analysis import timeline
                        return self._json(timeline.build_timeline(encounter.log_path, encounter.start_line,
                                                                    encounter.end_line))
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)

            # /api/history/<idx>/player/<name>/rotation?keyword=...
            # Splits the pull into segments bounded by every occurrence of
            # `keyword` and shows this player's own cast sequence + DPS/EHPS/
            # crit% within each -- re-parses the raw log's own line range on
            # demand rather than being backed by stored per-event data (see
            # stats.rotation_segments' own comment on why).
            if (len(parts) == 6 and parts[:2] == ["api", "history"] and parts[2].isdigit()
                    and parts[3] == "player" and parts[5] == "rotation"):
                idx = int(parts[2]) - 1
                if not (0 <= idx < len(tracker.history)):
                    return self._json({"error": "no such pull"}, 404)
                encounter = tracker.history[idx]
                if not encounter.log_path or encounter.start_line is None or encounter.end_line is None:
                    return self._json({"error": "This pull doesn't have line-range data "
                                                 "(imported/merged, or recorded before this feature)."}, 404)
                keyword = (parse_qs(u.query).get("keyword", [""])[0]).strip()
                if not keyword:
                    return self._json({"error": "keyword is required"}, 400)
                try:
                    lo = encounter.start_line or 1
                    hi = encounter.end_line or float("inf")
                    with open(encounter.log_path, "r", encoding="cp1252", errors="replace") as f:
                        lines = [line for i, line in enumerate(f, 1) if lo <= i <= hi]
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)
                player_name = unquote(parts[4])
                # Only apply the known Alacrity% when it's actually THIS
                # player's own rotation being analyzed -- for a teammate's,
                # we don't know their alacrity, so rotation_segments() falls
                # back to the unscaled base GCD rather than guess.
                alacrity_pct = (
                    character_settings.alacrity_pct
                    if character_settings is not None and character_settings.character == player_name
                    else 0.0
                )
                segments = rotation_segments(lines, player_name, keyword, alacrity_pct=alacrity_pct)
                if not segments:
                    return self._json({"error": f'"{keyword}" doesn\'t occur at least twice in this pull '
                                                 "(need two occurrences to bound a segment)."}, 404)
                return self._json({"segments": segments})

            if u.path == "/api/timer_rules":
                custom = [r for r in timer_engine.rules if r.category == "custom"]
                return self._json([
                    {"index": i, "keyword": r.keyword, "label": r.label,
                     "duration": r.duration_seconds, "warn": r.warn_seconds_before,
                     "voice": r.voice_alert, "audio_path": r.audio_path}
                    for i, r in enumerate(custom)
                ])

            if u.path == "/api/overlays":
                return self._json(overlay_manager.overlay_state())

            if u.path == "/api/parsely_settings":
                settings = dict(storage.load_parsely_settings())
                settings.pop("password", None)  # never echo the stored password back
                return self._json(settings)

            if u.path == "/api/character_settings":
                # None until the local player's been identified from the
                # log (see boss_intelligence.BossEncounterState) -- the
                # frontend shows a "not detected yet" state for that case
                # rather than a misleading 0%.
                if character_settings is None:
                    return self._json({"character": None, "alacrity_pct": 0.0})
                return self._json({
                    "character": character_settings.character,
                    "alacrity_pct": character_settings.alacrity_pct,
                })

            if u.path == "/api/encounters":
                if user_boss_dir is None or bundled_boss_dir is None:
                    return self._json({"error": "encounter editor not configured"}, 501)
                definitions = boss_state.definitions if boss_state else {}
                return self._json([
                    {
                        "id": d.id, "name": d.name, "boss_names": d.boss_names,
                        "phase_count": len(d.phases), "timer_count": len(d.timers),
                        "source": "user" if (user_boss_dir / f"{d.id}.json").exists() else "bundled",
                    }
                    for d in definitions.values()
                ])

            if len(parts) == 3 and parts[:2] == ["api", "encounters"]:
                if user_boss_dir is None or bundled_boss_dir is None:
                    return self._json({"error": "encounter editor not configured"}, 501)
                enc_id = _safe_encounter_id(unquote(parts[2]))
                if not enc_id:
                    return self._json({"error": "invalid encounter id"}, 400)
                user_path = user_boss_dir / f"{enc_id}.json"
                bundled_path = bundled_boss_dir / f"{enc_id}.json"
                path = user_path if user_path.exists() else bundled_path
                if not path.exists():
                    return self._json({"error": "no such encounter"}, 404)
                try:
                    return self._json(json.loads(path.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError) as exc:
                    return self._json({"error": str(exc)}, 500)

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
                audio_path = (body.get("audio_path") or "").strip() or None
                rule = TimerRule(
                    keyword=keyword, label=label, duration_seconds=duration,
                    voice_alert=bool(body.get("voice", True)), warn_seconds_before=warn,
                    audio_path=audio_path,
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

            if u.path == "/api/character_settings":
                if character_settings is None or character_settings.character is None:
                    return self._json({"error": "No character detected yet -- "
                                                 "start watching a live log first."}, 400)
                try:
                    pct = float(body.get("alacrity_pct"))
                except (TypeError, ValueError):
                    return self._json({"error": "alacrity_pct must be a number"}, 400)
                if pct < 0:
                    return self._json({"error": "alacrity_pct can't be negative"}, 400)
                character_settings.set_alacrity_pct(pct)
                return self._json({"ok": True, "alacrity_pct": pct})

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
                tracker.add_imported_encounter(encounter)
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
                            tracker.add_imported_encounter(encounter)
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

            if u.path == "/api/anonymize_log":
                from anonymize import anonymize_file
                source = (body.get("path") or "").strip()
                if not source:
                    return self._json({"error": "no file selected"}, 400)
                source_path = Path(source)
                if not source_path.exists():
                    return self._json({"error": "file not found"}, 404)
                dest_path = source_path.with_name(f"{source_path.stem}_anonymized{source_path.suffix}")
                try:
                    name_map = anonymize_file(str(source_path), str(dest_path))
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)
                return self._json({
                    "ok": True, "dest_path": str(dest_path), "players_replaced": len(name_map),
                })

            if u.path == "/api/encounters":
                if user_boss_dir is None or bundled_boss_dir is None:
                    return self._json({"error": "encounter editor not configured"}, 501)
                import boss_definitions as bd
                enc_id = _safe_encounter_id(body.get("id") or "")
                if not enc_id:
                    return self._json({"error": "id required"}, 400)
                try:
                    bd._definition_from_dict(body)
                except (KeyError, TypeError, ValueError) as exc:
                    return self._json({"error": f"invalid encounter definition: {exc}"}, 400)
                user_boss_dir.mkdir(parents=True, exist_ok=True)
                (user_boss_dir / f"{enc_id}.json").write_text(
                    json.dumps(body, indent=2), encoding="utf-8"
                )
                if boss_state is not None:
                    boss_state.definitions = bd.load_definitions(bundled_boss_dir, user_boss_dir)
                return self._json({"ok": True})

            if u.path == "/api/encounters/delete":
                if user_boss_dir is None:
                    return self._json({"error": "encounter editor not configured"}, 501)
                enc_id = _safe_encounter_id(body.get("id") or "")
                path = user_boss_dir / f"{enc_id}.json"
                if not enc_id or not path.exists():
                    return self._json({"error": "no such user encounter"}, 404)
                path.unlink()
                if boss_state is not None:
                    import boss_definitions as bd
                    boss_state.definitions = bd.load_definitions(bundled_boss_dir, user_boss_dir)
                return self._json({"ok": True})

            if u.path == "/api/update/apply":
                # Blocks on the download (a few seconds for a ~20MB zip) --
                # acceptable for a one-off, explicitly user-triggered action;
                # the frontend shows a "Downloading..." state for it. The
                # actual file swap happens in a detached helper AFTER this
                # process exits (see updater.py's module docstring for why
                # it can't happen from inside the running app) -- so once
                # this returns success, the window closes shortly after via
                # request_shutdown, on a short delay so this HTTP response
                # has time to actually reach the browser first.
                import updater
                result = update_holder.result if update_holder else None
                if not result:
                    return self._json({"error": "No update is available."}, 400)
                try:
                    staged = updater.prepare_update(result.get("zip_url"), result.get("sha256_url"))
                    updater.stage_relaunch(staged)
                except updater.UpdateError as exc:
                    return self._json({"error": str(exc)}, 500)
                if request_shutdown is not None:
                    threading.Timer(1.0, request_shutdown).start()
                return self._json({"success": True})

            return self._send(b"not found", "text/plain", 404)

    return Handler


def make_server(tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status,
                 port: int = 8766, update_holder=None, request_shutdown=None,
                 character_settings=None, bundled_boss_dir=None, user_boss_dir=None) -> ThreadingHTTPServer:
    handler = make_handler(tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status,
                            update_holder=update_holder, request_shutdown=request_shutdown,
                            character_settings=character_settings,
                            bundled_boss_dir=bundled_boss_dir, user_boss_dir=user_boss_dir)
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
