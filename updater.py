"""
updater.py

Self-updater: downloads the latest release zip, verifies it (checksum when
the release has one attached), stages it, and hands off to a small
detached PowerShell helper that waits for THIS process to exit, swaps the
files in place, and relaunches. Windows won't let a running .exe delete or
overwrite its own files, so the actual swap can't happen from inside the
process being replaced -- it has to be a separate process that outlives
this one.

Only meaningful for a frozen (PyInstaller) build: there's no fixed
"install directory" to swap when running from source with `python
main.py`. is_frozen() gates every entry point here for that reason.

Caller's responsibility (see web_server.py's /api/update/apply): call
prepare_update() to download+verify+extract, then stage_relaunch() to
launch the helper, then close the app window right after -- the helper is
already waiting on this process's pid the moment stage_relaunch() returns.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import requests

REQUEST_TIMEOUT_SECONDS = 30.0
DOWNLOAD_CHUNK_BYTES = 1 << 16


class UpdateError(Exception):
    """Anything that stops an update from being applied. Callers catch
    this and report the message back to the UI rather than crashing --
    a failed update should never take down an otherwise-working app."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def update_log_path() -> Path:
    """Where the relaunch helper writes what it did. Lives in %TEMP% (not
    the app's own directory) because that directory is being renamed out
    from under it mid-update. Surfaced to the user when an update fails so
    there's something concrete to look at -- every failure path used to be
    entirely silent."""
    return Path(tempfile.gettempdir()) / "dps-update.log"


def install_dir() -> Path:
    """Where the running app's own files live -- only meaningful when
    frozen. sys.executable is the .exe itself; its parent is the install
    directory PyInstaller's onedir build unpacks into (that's also where
    _internal/ lives, which is why the swap replaces the whole folder,
    not just the .exe)."""
    return Path(sys.executable).resolve().parent


def _download(url: str, dest: Path) -> None:
    try:
        with requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, stream=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(DOWNLOAD_CHUNK_BYTES):
                    f.write(chunk)
    except requests.RequestException as exc:
        raise UpdateError(f"Download failed: {exc}") from exc


def _verify_checksum(zip_path: Path, sha256_url: Optional[str]) -> None:
    """No-ops if the release doesn't have a checksum asset attached --
    older releases predate this, and a missing checksum shouldn't block
    an update that would otherwise work fine. A checksum MISMATCH still
    hard-fails; a missing checksum just skips the check."""
    if not sha256_url:
        return
    try:
        resp = requests.get(sha256_url, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        expected = resp.text.strip().lower().split()[0]
    except requests.RequestException:
        return  # couldn't fetch the checksum -- don't block the update over it
    digest = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected:
        raise UpdateError(
            f"Downloaded file doesn't match the published checksum (expected "
            f"{expected[:12]}…, got {actual[:12]}…) -- the download may be "
            f"corrupted or incomplete. Try again, or download manually from the "
            f"GitHub release page."
        )


def _extract(zip_path: Path, dest_dir: Path) -> Path:
    """Extracts into dest_dir, returns the path to the app folder inside --
    build.ps1's zip always contains one top-level folder
    (DPS-Dynamic-Parse-System/), not the files loose at the zip root."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest_dir)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"Downloaded file isn't a valid zip: {exc}") from exc
    entries = [p for p in dest_dir.iterdir() if p.is_dir()]
    if len(entries) == 1:
        return entries[0]
    return dest_dir  # unexpected shape -- fall back to the extraction root itself


def prepare_update(zip_url: Optional[str], sha256_url: Optional[str] = None) -> Path:
    """Downloads, verifies, and extracts the update. Returns the path to
    the extracted app folder, ready for stage_relaunch(). Raises
    UpdateError on any failure -- never silently no-ops, since the caller
    needs to report a real failure back to the UI rather than pretend
    nothing happened."""
    if not is_frozen():
        raise UpdateError("Self-update only works in the packaged app, not when running from source.")
    if not zip_url:
        raise UpdateError("This release doesn't have a downloadable zip attached.")
    work_dir = Path(tempfile.gettempdir()) / "dps-update-staging"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    zip_path = work_dir / "update.zip"
    _download(zip_url, zip_path)
    _verify_checksum(zip_path, sha256_url)
    return _extract(zip_path, work_dir / "extracted")


# %TEMP% (not the app's own dir) -- must survive the install dir being
# renamed/replaced out from under it a moment later.
_RELAUNCH_SCRIPT_TEMPLATE = r"""
$ErrorActionPreference = "Stop"
$targetPid = __PID__
$installDir = "__INSTALL_DIR__"
$stagedDir = "__STAGED_DIR__"
$exeName = "__EXE_NAME__"
$logPath = "__LOG_PATH__"

# Every failure path here used to be completely silent: output went to
# DEVNULL and the catch block just `exit 1`d. Reported twice as "it closed
# and never came back" with nothing to go on. Log every step so a failed
# update can actually be diagnosed instead of guessed at.
function Log($m) {
    try { Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o)  $m" } catch { }
}

# Never stand inside the directory we're about to rename. stage_relaunch
# already launches us with a neutral cwd; this is the second line of defence
# in case anything (a profile, a future caller) puts us back there.
Set-Location -LiteralPath ([System.IO.Path]::GetTempPath())

Log "helper started (pid to wait for: $targetPid)"
Log "installDir=$installDir"
Log "stagedDir=$stagedDir"

# Wait for the old process to fully exit -- it's still holding its own
# .exe/.dll files open until then. 60s cap so a stuck process can't hang
# this forever; the old app just keeps running untouched if that happens.
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Process -Id $targetPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300
}
if (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) {
    Log "ABORT: pid $targetPid still alive after 60s; install left untouched"
    exit 1
}
Log "old process has exited"

# The app spawns child processes (the webview host among them) which inherit
# its working directory -- the install directory -- and can outlive the
# parent, keeping that directory locked exactly the same way. Waiting only on
# the launching pid isn't enough.
$waitKids = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $waitKids) {
    $holders = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $p = $null
        try { $p = $_.Path } catch { }
        $p -and $p.StartsWith($installDir, [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($holders.Count -eq 0) { break }
    Log "still waiting on $($holders.Count) process(es) running from the install dir: $(($holders | Select-Object -Expand Name -Unique) -join ', ')"
    Start-Sleep -Milliseconds 500
}

$backup = "$installDir.old"
if (Test-Path $backup) {
    try { Remove-Item -LiteralPath $backup -Recurse -Force; Log "cleared previous .old backup" }
    catch { Log "could not clear previous .old backup: $_" }
}

# Windows keeps handles open for a moment AFTER a process exits -- the
# antivirus scanning the just-closed .exe, the search indexer, Explorer.
# A single attempt that lost that race used to fall straight into the
# catch and exit silently, which is exactly what "closed and never came
# back" looks like from the outside. Retry instead of giving up at the
# first failure.
$swapped = $false
for ($i = 1; $i -le 10; $i++) {
    try {
        Rename-Item -LiteralPath $installDir -NewName (Split-Path $backup -Leaf) -ErrorAction Stop
        $swapped = $true
        Log "moved old install aside (attempt $i)"
        break
    } catch {
        Log "rename attempt $i failed: $_"
        Start-Sleep -Milliseconds 500
    }
}
if (-not $swapped) {
    Log "ABORT: could not move the old install aside; nothing changed"
    exit 1
}

try {
    Move-Item -LiteralPath $stagedDir -Destination $installDir -ErrorAction Stop
    Log "new version moved into place"
} catch {
    Log "FAILED to move the new version in: $_"
    if (-not (Test-Path $installDir)) {
        try {
            Rename-Item -LiteralPath $backup -NewName (Split-Path $installDir -Leaf) -ErrorAction Stop
            Log "restored the previous install"
        } catch {
            Log "CRITICAL: could not restore the previous install: $_"
            exit 1
        }
    }
    # Fall through: the previous version is back in place, so still
    # relaunch it rather than leaving the user with no running app.
}

# Deleting the backup is BEST EFFORT and must never be fatal. This line
# used to sit inside the same try as the swap, so a locked leftover (AV
# still scanning the old .exe) threw AFTER the update had already
# succeeded -- and the catch, seeing $installDir present, skipped the
# restore and exited without ever reaching Start-Process. The update
# applied; the app just never came back.
if (Test-Path $backup) {
    try { Remove-Item -LiteralPath $backup -Recurse -Force; Log "removed .old backup" }
    catch { Log "left .old backup in place (still locked): $_" }
}

$exePath = Join-Path $installDir $exeName
if (-not (Test-Path $exePath)) {
    Log "RELAUNCH SKIPPED: $exePath does not exist"
    exit 1
}
try {
    # -WorkingDirectory matters: the app resolves its own bundled data
    # relative to where it starts.
    Start-Process -FilePath $exePath -WorkingDirectory $installDir
    Log "relaunched $exePath"
} catch {
    Log "RELAUNCH FAILED: $_"
    exit 1
}
Log "update complete"
"""


def stage_relaunch(staged_app_dir: Path) -> None:
    """Writes the relaunch helper script and launches it, detached, right
    now. The helper won't actually start swapping files until THIS
    process's pid exits -- callers must close the app window/exit shortly
    after calling this, or the swap just waits (up to 60s) for that to
    happen and then proceeds anyway.

    Not gated on is_frozen() itself (prepare_update() already enforces
    that upstream, and this has no meaning to call standalone) -- but
    install_dir() below would be nonsense outside a frozen build regardless."""
    target = install_dir()
    exe_name = Path(sys.executable).name
    script = (
        _RELAUNCH_SCRIPT_TEMPLATE
        .replace("__PID__", str(os.getpid()))
        .replace("__INSTALL_DIR__", str(target))
        .replace("__STAGED_DIR__", str(staged_app_dir))
        .replace("__EXE_NAME__", exe_name)
        .replace("__LOG_PATH__", str(update_log_path()))
    )
    script_path = Path(tempfile.gettempdir()) / "dps-update-relaunch.ps1"
    script_path.write_text(script, encoding="utf-8")

    # CREATE_NO_WINDOW only -- NOT combined with DETACHED_PROCESS. Confirmed
    # by direct testing that DETACHED_PROCESS makes powershell.exe exit
    # immediately (code 0) without ever running the script, silently --
    # apparently it needs SOME console context to initialize, which
    # DETACHED_PROCESS denies it entirely. CREATE_NO_WINDOW alone still
    # launches with no visible window, and (confirmed) the child process
    # survives this process exiting regardless -- Python's subprocess.Popen
    # doesn't tie child lifetime to parent lifetime by default on Windows.
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
        # THE fix for "it closed and never came back". A child process
        # inherits the parent's working directory, and this app's working
        # directory IS its install directory (the Start Menu shortcut sets
        # no WorkingDir, so Windows defaults it to the exe's own folder).
        # The helper was therefore standing inside the very folder it had to
        # rename, and Windows refuses to rename a directory that is any
        # process's cwd -- "Cannot rename the item ... because it is in
        # use", exactly what the field log showed, on all 10 retries. That
        # made it a guaranteed failure rather than a race: no amount of
        # retrying could ever succeed. Start the helper somewhere neutral.
        cwd=tempfile.gettempdir(),
    )
