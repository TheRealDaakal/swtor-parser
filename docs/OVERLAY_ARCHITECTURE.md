# Overlay Architecture — Pass 5

The legacy `overlay.py` import path is retained as a compatibility facade.
Concrete overlay classes now live under `overlays/`.

The extraction is intentionally behavior-preserving; future passes can clean
individual overlay implementations without coupling them to the legacy module.
