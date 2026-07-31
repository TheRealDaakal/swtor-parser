"""
live_server.py

Local web server for the live meter -- the pywebview replacement for the
Tkinter "Live" tab. Stdlib only (http.server + json), same approach as
analysis/webapp.py, reusing that module's app.css so the two web surfaces
of this app share one visual language.

Binds to 127.0.0.1 only: no auth, local data.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "live_web"
SHARED_CSS = Path(__file__).resolve().parent / "analysis" / "static" / "app.css"

# Categories with their own dedicated panel, so they're excluded from the
# general "Active Timers" list -- mirrors gui.py's _OWN_PANEL_CATEGORIES.
_OWN_PANEL_CATEGORIES = ("cooldown", "dot", "hot")


def _timer_rows(rows):
    return [
        {"label": label, "remaining": round(remaining, 1), "total": total}
        for label, remaining, total, _category, _is_alert in rows
    ]


def build_snapshot(tracker, timer_engine, boss_state, taunt_tracker) -> dict:
    """Same data _refresh() used to compute for the Tk Live tab, as JSON."""
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
        "duration": round(duration, 1),
        "players": players,
        "alerts": alerts,
        "timers": active_timers,
        "cooldowns": cooldowns,
        "dots_hots": dots_hots,
        "taunts": taunts,
    }


def make_handler(tracker, timer_engine, boss_state, taunt_tracker):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # keep the console clean

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

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._static(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if self.path == "/app.js":
                return self._static(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            if self.path == "/app.css":
                return self._static(SHARED_CSS, "text/css; charset=utf-8")
            if self.path == "/api/live":
                return self._json(build_snapshot(tracker, timer_engine, boss_state, taunt_tracker))
            return self._send(b"not found", "text/plain", 404)

    return Handler


def make_server(tracker, timer_engine, boss_state, taunt_tracker, port: int = 8766) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(tracker, timer_engine, boss_state, taunt_tracker))
