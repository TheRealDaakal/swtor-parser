"""
update_check.py

One-shot "is a newer version out" check against GitHub Releases. Called
once at startup from main.py on a background thread -- never polled
continuously, since it's just telling the user a newer build exists, not
anything time-sensitive.

Deliberately built against GitHub's public, unauthenticated releases
endpoint -- no token, no private-repo workaround. While the repo stays
private this 404s and resolves to "no update," which is expected, not a
bug: the same code starts working the moment the repo goes public, with
no changes needed here.
"""

from typing import Optional

import requests

REPO = "TheRealDaakal/swtor-parser"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
REQUEST_TIMEOUT_SECONDS = 5.0


def _parse_version(v: str) -> tuple:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3). Non-numeric/malformed parts fall
    back to 0 rather than raising -- a weird tag shouldn't crash the
    check, it should just compare as "not newer"."""
    v = v.strip()
    if v.startswith("v"):
        v = v[1:]
    parts = []
    for part in v.split(".")[:3]:
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def check_for_update(current_version: str) -> Optional[dict]:
    """Returns {"version": "1.2.3", "url": "https://..."} if GitHub's
    latest release is newer than current_version, else None -- including
    on any failure (offline, GitHub down, repo still private and 404ing,
    unexpected response shape). This must never raise: it runs on a
    background thread at startup and a broken update check should never
    be the reason the app doesn't come up."""
    try:
        resp = requests.get(LATEST_RELEASE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        data = resp.json()
        tag = data.get("tag_name")
        url = data.get("html_url")
        if not tag or not url:
            return None
        if _parse_version(tag) > _parse_version(current_version):
            return {"version": tag.lstrip("v"), "url": url}
        return None
    except Exception:
        return None
