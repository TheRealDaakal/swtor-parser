# Architecture Cleanup — Pass 3

## Web server
`web_server.py` remains the compatibility entry point. Its responsibilities are candidates for separation into server/bootstrap, routes, history/session APIs, analytics/corpus APIs, static serving, and serializers.

## Overlay
`overlay.py` remains the compatibility entry point. Its responsibilities are candidates for separation into lifecycle/manager, windows, layout/state, rendering, and widgets.

## Refactor rule
Preserve behavior and keep the regression suite green after each extraction. Introduce compatibility imports before changing public interfaces.

## Web server extraction — Pass 4

The web layer is now separated into:

- `web_server.py` — stable public entry point and server bootstrap
- `web_server_routes.py` — HTTP handler and endpoint routing
- `web_server_helpers.py` — live/history serialization and corpus state

Existing callers can continue importing `make_server`, `make_handler`, and the helper
functions from `web_server` while the implementation is split internally.
