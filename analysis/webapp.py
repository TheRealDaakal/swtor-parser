"""
analysis/webapp.py

Local web UI over the corpus. Stdlib only (http.server + json) -- no Flask,
no CDN, no network access at all. Everything is served from localhost and
the frontend embeds its own CSS/JS, so this works fully offline.

Run:  python -m analysis.webapp
      python -m analysis.webapp --port 8770 --no-browser

Binds to 127.0.0.1 only: this reads your local combat logs and there is no
authentication, so it must not be exposed to the network.
"""

import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from analysis import corpus, forensics, timeline

STATIC_DIR = Path(__file__).resolve().parent / "static"

_index_lock = threading.Lock()
_index = None
_build_state = {"running": False, "done": 0, "total": 0, "file": ""}


def get_index(build_if_missing: bool = True):
    global _index
    with _index_lock:
        if _index is None:
            _index = corpus.load_index()
    if _index is None and build_if_missing:
        rebuild()
    return _index or {"sessions": []}


def rebuild(force: bool = False):
    global _index
    if _build_state["running"]:
        return
    _build_state.update(running=True, done=0, total=0, file="")

    def progress(done, total, name):
        _build_state.update(done=done, total=total, file=name)

    try:
        idx = corpus.build_index(progress=progress, force=force)
        with _index_lock:
            _index = idx
    finally:
        _build_state["running"] = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the console clean

    # ---------------------------------------------------------------- utils

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

    def _static(self, name: str, ctype: str):
        path = STATIC_DIR / name
        if not path.exists():
            self._send(b"not found", "text/plain", 404)
            return
        self._send(path.read_bytes(), ctype)

    # ----------------------------------------------------------------- API

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]

        if u.path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if u.path == "/app.js":
            return self._static("app.js", "application/javascript; charset=utf-8")
        if u.path == "/app.css":
            return self._static("app.css", "text/css; charset=utf-8")

        if u.path == "/api/status":
            idx = get_index(build_if_missing=False)
            return self._json({
                "built": bool(idx.get("sessions")),
                "log_dir": idx.get("log_dir"),
                "sessions": len(idx.get("sessions", [])),
                "building": _build_state["running"],
                "progress": {"done": _build_state["done"], "total": _build_state["total"],
                             "file": _build_state["file"]},
            })

        if u.path == "/api/overview":
            idx = get_index()
            encs = list(corpus.all_encounters(idx))
            boss = corpus.boss_summary(idx)
            dates = sorted({s.get("date") for s, _ in encs if s.get("date")})
            all_players = corpus.players_seen(idx)
            return self._json({
                "sessions": len(idx.get("sessions", [])),
                "encounters": len(encs),
                "boss_encounters": sum(1 for _s, e in encs if e.get("boss")),
                "distinct_bosses": len(boss),
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "bosses": boss,
                # Only the top slice is sent for display, but the true count
                # goes separately -- otherwise the UI reports the display cap
                # as if it were the number of characters in the corpus.
                "player_count": len(all_players),
                "players": all_players[:40],
            })

        if u.path == "/api/trend":
            idx = get_index()
            return self._json(corpus.player_trend(
                idx, one("player", ""), one("boss") or None, one("metric", "dps")))

        if u.path == "/api/pulls":
            idx = get_index()
            boss_id = one("boss") or None
            rows = []
            for s, e in corpus.boss_encounters(idx, boss_id):
                rows.append({
                    "date": s.get("date"), "time": s.get("time"), "file": s.get("file"),
                    "boss": e.get("boss"), "boss_id": e.get("boss_id"),
                    "duration": e.get("duration"), "deaths": e.get("deaths"),
                    "phases": e.get("phases"), "players": e.get("players"),
                    "start_line": e.get("start_line"), "end_line": e.get("end_line"),
                })
            rows.reverse()
            return self._json(rows)

        if u.path == "/api/deaths":
            idx = get_index()
            fname = one("file")
            try:
                sl = int(one("start_line", "0")) or None
                el = int(one("end_line", "0")) or None
            except ValueError:
                sl = el = None
            sess = next((s for s in idx.get("sessions", []) if s.get("file") == fname), None)
            if not sess or not sess.get("path"):
                return self._json({"error": "unknown session file"}, 404)
            try:
                reports = forensics.analyze_deaths(sess["path"], sl, el, one("player") or None)
            except OSError as exc:
                return self._json({"error": str(exc)}, 500)
            return self._json({"reports": reports, "summary": forensics.summarize_deaths(reports)})

        if u.path == "/api/timeline":
            idx = get_index()
            fname = one("file")
            try:
                sl = int(one("start_line", "0")) or None
                el = int(one("end_line", "0")) or None
            except ValueError:
                sl = el = None
            sess = next((s for s in idx.get("sessions", []) if s.get("file") == fname), None)
            if not sess or not sess.get("path"):
                return self._json({"error": "unknown session file"}, 404)
            try:
                data = timeline.build_timeline(sess["path"], sl, el)
            except OSError as exc:
                return self._json({"error": str(exc)}, 500)
            return self._json(data)

        return self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/rebuild":
            force = "force" in (u.query or "")
            threading.Thread(target=rebuild, kwargs={"force": force}, daemon=True).start()
            return self._json({"started": True})
        return self._send(b"not found", "text/plain", 404)


def main():
    ap = argparse.ArgumentParser(description="Local web UI for SWTOR log corpus analytics")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # 127.0.0.1 deliberately: local log data, no auth.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"SWTOR corpus analytics -> {url}")
    print("(Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
