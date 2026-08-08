"""Compatibility entry point for the local SWTOR Parser web server.

The implementation is split into helpers and routes, while these imports keep
the original ``web_server`` API stable for existing callers and tests.
"""

from http.server import ThreadingHTTPServer

from web_server_helpers import (
    CorpusState, _safe_encounter_id, _timer_rows, build_live_snapshot,
    build_history_list, build_history_detail, build_ability_breakdown,
)
from web_server_routes import make_handler


def make_server(tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status,
                port: int = 8766, update_holder=None, request_shutdown=None,
                character_settings=None, bundled_boss_dir=None, user_boss_dir=None) -> ThreadingHTTPServer:
    handler = make_handler(
        tracker, timer_engine, boss_state, taunt_tracker, overlay_manager, status,
        update_holder=update_holder, request_shutdown=request_shutdown,
        character_settings=character_settings, bundled_boss_dir=bundled_boss_dir,
        user_boss_dir=user_boss_dir,
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
