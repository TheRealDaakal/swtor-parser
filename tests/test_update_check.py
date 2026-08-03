"""
update_check.check_for_update() must never raise (runs on a background
startup thread) and must correctly pull the zip/checksum asset URLs out of
GitHub's release JSON for the self-updater (updater.py) to use.
"""
import update_check


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _release_payload(tag, assets=None):
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/TheRealDaakal/swtor-parser/releases/tag/{tag}",
        "assets": assets or [],
    }


def test_no_update_when_versions_equal(monkeypatch):
    monkeypatch.setattr(update_check.requests, "get",
                         lambda *a, **k: _FakeResponse(200, _release_payload("v0.2.4")))
    assert update_check.check_for_update("0.2.4") is None


def test_no_update_when_local_is_newer(monkeypatch):
    monkeypatch.setattr(update_check.requests, "get",
                         lambda *a, **k: _FakeResponse(200, _release_payload("v0.2.4")))
    assert update_check.check_for_update("0.3.0") is None


def test_update_found_extracts_zip_and_checksum_urls(monkeypatch):
    payload = _release_payload("v0.2.5", assets=[
        {"name": "swtor-parser-v0.2.5-setup.exe", "browser_download_url": "https://x/setup.exe"},
        {"name": "swtor-parser-v0.2.5-win64.zip", "browser_download_url": "https://x/zip"},
        {"name": "swtor-parser-v0.2.5-win64.zip.sha256", "browser_download_url": "https://x/sha256"},
    ])
    monkeypatch.setattr(update_check.requests, "get", lambda *a, **k: _FakeResponse(200, payload))
    result = update_check.check_for_update("0.2.4")
    assert result == {
        "version": "0.2.5",
        "url": "https://github.com/TheRealDaakal/swtor-parser/releases/tag/v0.2.5",
        "zip_url": "https://x/zip",
        "sha256_url": "https://x/sha256",
    }


def test_update_found_with_no_zip_asset_returns_none_urls_not_a_crash(monkeypatch):
    """An older/hand-cut release without a zip attached must still report
    the update (so the banner/manual-download path works), just with
    zip_url=None -- the self-updater checks for that and falls back to
    manual instead of crashing on a missing asset."""
    payload = _release_payload("v0.2.5", assets=[
        {"name": "swtor-parser-v0.2.5-setup.exe", "browser_download_url": "https://x/setup.exe"},
    ])
    monkeypatch.setattr(update_check.requests, "get", lambda *a, **k: _FakeResponse(200, payload))
    result = update_check.check_for_update("0.2.4")
    assert result["version"] == "0.2.5"
    assert result["zip_url"] is None
    assert result["sha256_url"] is None


def test_non_200_status_returns_none(monkeypatch):
    monkeypatch.setattr(update_check.requests, "get", lambda *a, **k: _FakeResponse(404))
    assert update_check.check_for_update("0.2.4") is None


def test_network_failure_returns_none_not_raises(monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("offline")
    monkeypatch.setattr(update_check.requests, "get", _raise)
    assert update_check.check_for_update("0.2.4") is None


def test_malformed_response_missing_tag_returns_none(monkeypatch):
    monkeypatch.setattr(update_check.requests, "get",
                         lambda *a, **k: _FakeResponse(200, {"html_url": "https://x"}))
    assert update_check.check_for_update("0.2.4") is None
