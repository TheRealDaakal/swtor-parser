"""
Covers everything in updater.py that doesn't require actually running a
detached PowerShell process or a real frozen build: checksum verification,
zip extraction shape-handling, prepare_update()'s orchestration/error
paths, and that stage_relaunch() writes a correctly-substituted script
(without ever launching it -- subprocess.Popen is monkeypatched out).
"""
import hashlib
import os
import zipfile
from pathlib import Path

import pytest

import updater


class _FakeStreamResponse:
    """Mimics requests.get(..., stream=True) used as a context manager."""

    def __init__(self, content: bytes, status_code: int = 200):
        self._content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")


def _make_zip(tmp_path, top_folder="DPS-Dynamic-Parse-System", files=("app.exe", "readme.txt")):
    zip_path = tmp_path / "release.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in files:
            zf.writestr(f"{top_folder}/{name}", "content")
    return zip_path


class TestIsFrozen:
    def test_not_frozen_when_running_from_source(self):
        assert updater.is_frozen() is False  # pytest itself is never a frozen build


class TestVerifyChecksum:
    def test_no_sha256_url_is_a_silent_noop(self, tmp_path):
        f = tmp_path / "x.zip"
        f.write_bytes(b"anything")
        updater._verify_checksum(f, None)  # must not raise

    def test_matching_checksum_passes(self, tmp_path, monkeypatch):
        f = tmp_path / "x.zip"
        f.write_bytes(b"hello world")
        digest = hashlib.sha256(b"hello world").hexdigest()
        monkeypatch.setattr(updater.requests, "get", lambda *a, **k: _FakeResponse(digest))
        updater._verify_checksum(f, "https://x/checksum")  # must not raise

    def test_mismatched_checksum_raises(self, tmp_path, monkeypatch):
        f = tmp_path / "x.zip"
        f.write_bytes(b"hello world")
        monkeypatch.setattr(updater.requests, "get",
                             lambda *a, **k: _FakeResponse("0" * 64))
        with pytest.raises(updater.UpdateError, match="doesn't match"):
            updater._verify_checksum(f, "https://x/checksum")

    def test_checksum_fetch_failure_does_not_block_the_update(self, tmp_path, monkeypatch):
        f = tmp_path / "x.zip"
        f.write_bytes(b"hello world")

        def _raise(*a, **k):
            import requests
            raise requests.ConnectionError("offline")
        monkeypatch.setattr(updater.requests, "get", _raise)
        updater._verify_checksum(f, "https://x/checksum")  # must not raise


class TestExtract:
    def test_extracts_the_single_top_level_folder(self, tmp_path):
        zip_path = _make_zip(tmp_path)
        result = updater._extract(zip_path, tmp_path / "out")
        assert result.name == "DPS-Dynamic-Parse-System"
        assert (result / "app.exe").exists()
        assert (result / "readme.txt").exists()

    def test_bad_zip_raises_update_error(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip file")
        with pytest.raises(updater.UpdateError, match="isn't a valid zip"):
            updater._extract(bad, tmp_path / "out")

    def test_reextracting_clears_the_previous_extraction(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        stale = dest / "stale_leftover.txt"
        stale.write_text("old")
        zip_path = _make_zip(tmp_path)
        updater._extract(zip_path, dest)
        assert not stale.exists()


class TestPrepareUpdate:
    def test_raises_when_not_frozen(self):
        assert updater.is_frozen() is False
        with pytest.raises(updater.UpdateError, match="packaged app"):
            updater.prepare_update("https://x/zip")

    def test_raises_when_zip_url_missing(self, monkeypatch):
        monkeypatch.setattr(updater, "is_frozen", lambda: True)
        with pytest.raises(updater.UpdateError, match="doesn't have a downloadable zip"):
            updater.prepare_update(None)

    def test_full_happy_path_downloads_verifies_and_extracts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater, "is_frozen", lambda: True)
        monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))

        src_dir = tmp_path / "src"
        src_dir.mkdir()
        zip_path = _make_zip(src_dir)
        zip_bytes = zip_path.read_bytes()
        digest = hashlib.sha256(zip_bytes).hexdigest()

        def fake_get(url, timeout=None, stream=None):
            if stream:
                return _FakeStreamResponse(zip_bytes)
            return _FakeResponse(digest)
        monkeypatch.setattr(updater.requests, "get", fake_get)

        result = updater.prepare_update("https://x/zip", "https://x/zip.sha256")
        assert result.name == "DPS-Dynamic-Parse-System"
        assert (result / "app.exe").exists()


class TestStageRelaunch:
    def test_writes_a_script_with_all_placeholders_substituted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(updater, "install_dir", lambda: tmp_path / "install")
        monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "install" / "MyApp.exe"))

        launched = {}
        monkeypatch.setattr(updater.subprocess, "Popen",
                             lambda cmd, **kw: launched.update(cmd=cmd, kw=kw))

        staged = tmp_path / "staged_app"
        staged.mkdir()
        updater.stage_relaunch(staged)

        script_path = tmp_path / "dps-update-relaunch.ps1"
        assert script_path.exists()
        content = script_path.read_text(encoding="utf-8")

        assert "__PID__" not in content and "__INSTALL_DIR__" not in content
        assert "__STAGED_DIR__" not in content and "__EXE_NAME__" not in content
        assert str(os.getpid()) in content
        assert str(tmp_path / "install") in content
        assert str(staged) in content
        assert "MyApp.exe" in content

        # Must actually launch the helper, detached (not waited on inline).
        assert launched["cmd"][0] == "powershell.exe"
        assert str(script_path) in launched["cmd"]


class TestRelaunchScriptRobustness:
    """Both live reports of the self-updater ("the viewer closed, but it
    didn't restart", then "the auto updater doesnt update and restart the
    parser") had the same shape: something went wrong and there was
    NOTHING to look at. Every failure path exited silently with output
    routed to DEVNULL. These lock in the structure that fixed that."""

    def test_log_path_is_substituted_into_the_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(updater, "install_dir", lambda: tmp_path / "install")
        monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "install" / "MyApp.exe"))
        monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: None)

        staged = tmp_path / "staged_app"
        staged.mkdir()
        updater.stage_relaunch(staged)

        content = (tmp_path / "dps-update-relaunch.ps1").read_text(encoding="utf-8")
        assert "__LOG_PATH__" not in content
        assert str(updater.update_log_path()) in content

    def test_log_lives_outside_the_install_dir(self, tmp_path, monkeypatch):
        """It has to survive the install directory being renamed out from
        under it mid-update."""
        monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(updater, "install_dir", lambda: tmp_path / "install")
        assert updater.install_dir() not in updater.update_log_path().parents

    def test_the_swap_retries_instead_of_giving_up_on_first_failure(self):
        """Windows keeps handles open briefly AFTER a process exits (AV
        scanning the closed .exe, search indexer). A single attempt that
        lost that race used to fall into the catch and exit silently."""
        script = updater._RELAUNCH_SCRIPT_TEMPLATE
        assert "for ($i = 1; $i -le 10; $i++)" in script
        assert "rename attempt $i failed" in script

    def test_backup_cleanup_cannot_abort_the_relaunch(self):
        """THE bug: `Remove-Item $backup` used to sit inside the same try as
        the swap. When it threw (locked leftover) AFTER the new version was
        already in place, the catch saw $installDir present, skipped the
        restore, and exit 1'd -- never reaching Start-Process. The update
        applied and the app simply never came back. The cleanup must now be
        its own best-effort block, positioned before the relaunch."""
        script = updater._RELAUNCH_SCRIPT_TEMPLATE
        cleanup_at = script.index("removed .old backup")
        relaunch_at = script.index("Start-Process -FilePath $exePath")
        assert cleanup_at < relaunch_at, "cleanup runs before the relaunch"

        # Scoped to the cleanup block ITSELF -- ending where the separate
        # (and legitimate) "the exe isn't there" guard begins, which does
        # exit on purpose.
        cleanup_block = script[cleanup_at:script.index("$exePath = Join-Path")]
        assert "exit 1" not in cleanup_block, (
            "a failed backup cleanup must only log, never abort the update"
        )

    def test_every_abort_path_logs_a_reason(self):
        script = updater._RELAUNCH_SCRIPT_TEMPLATE
        for marker in ("ABORT: pid", "ABORT: could not move the old install aside",
                       "RELAUNCH SKIPPED", "RELAUNCH FAILED"):
            assert marker in script, f"missing diagnostic for {marker!r}"

    def test_relaunch_sets_the_working_directory(self):
        """The app resolves its own bundled data relative to where it starts."""
        assert "-WorkingDirectory $installDir" in updater._RELAUNCH_SCRIPT_TEMPLATE


class TestHelperDoesNotLockTheInstallDir:
    """The third and final cause of "it closed and never came back".

    A child process inherits its parent's working directory, and this app's
    working directory IS its install directory (the Start Menu shortcut sets
    no WorkingDir, so Windows defaults it to the exe's folder). The relaunch
    helper was therefore launched standing inside the very directory it had
    to rename. Windows refuses to rename a directory that is any process's
    cwd, so this failed 100% of the time -- the field log showed the same
    "because it is in use" error on all 10 retry attempts. Retrying could
    never have helped; only moving out of the directory can.
    """

    def test_helper_is_launched_from_a_neutral_working_directory(self, tmp_path, monkeypatch):
        install = tmp_path / "install"
        monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(updater, "install_dir", lambda: install)
        monkeypatch.setattr(updater.sys, "executable", str(install / "MyApp.exe"))

        captured = {}
        monkeypatch.setattr(updater.subprocess, "Popen",
                             lambda cmd, **kw: captured.update(kw))

        staged = tmp_path / "staged_app"
        staged.mkdir()
        updater.stage_relaunch(staged)

        cwd = captured.get("cwd")
        assert cwd is not None, "must pass an explicit cwd, not inherit the app's"
        assert Path(cwd).resolve() != install.resolve(), (
            "the helper must not run from inside the directory it renames"
        )

    def test_script_also_moves_itself_out_of_the_install_dir(self):
        assert "Set-Location" in updater._RELAUNCH_SCRIPT_TEMPLATE

    def test_script_waits_for_child_processes_too(self):
        """Killing the launching pid isn't enough -- its children inherit the
        same working directory and hold the same lock."""
        script = updater._RELAUNCH_SCRIPT_TEMPLATE
        assert "still waiting on" in script
        assert "StartsWith($installDir" in script
