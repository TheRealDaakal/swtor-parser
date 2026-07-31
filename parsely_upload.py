"""
parsely_upload.py

Uploads combat logs (or a specific encounter's line range) to parsely.io,
matching the real API BARAS uses under the hood (confirmed from BARAS's own
open-source Rust implementation, MIT licensed):

    POST https://parsely.io/api/upload2
    multipart/form-data:
        file        -- gzip-compressed log bytes
        public      -- visibility: 0=Private, 1=Public, 2=Guild only
        notes       -- optional
        username / password -- optional Parsely account credentials
        guild       -- optional guild name
        guild-log   -- "1" to tag all participants as guild members

    Response is XML:
        success: <file>LINK</file>
        error:   <status>error</status> and <error>MESSAGE</error>
        legacy error: response contains "NOT OK"

We identify ourselves honestly with our own User-Agent rather than
pretending to be BARAS or any other client.
"""

import gzip
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

PARSELY_URL = "https://parsely.io/api/upload2"
USER_AGENT = "swtor-live-parser/1.0"
REQUEST_TIMEOUT = 300  # seconds; large logs can take a while to upload

# Extra seconds of trailing log lines to include beyond an encounter's last
# line, so late-arriving damage/death events (SWTOR can log a few after the
# encounter is "done") are captured for accurate classification -- matches
# BARAS's own PARSELY_TRAILING_SECS.
TRAILING_SECS = 5


@dataclass
class ParselyUploadResult:
    success: bool
    link: Optional[str] = None
    error: Optional[str] = None


def _gzip_bytes(raw: bytes) -> bytes:
    return gzip.compress(raw, compresslevel=6)


_TIMESTAMP_RE = re.compile(rb"^\[(\d{1,2}):(\d{2}):(\d{2})")


def _timestamp_secs(line: bytes) -> Optional[int]:
    """Extracts HH:MM:SS as total seconds from a line starting '[HH:MM:SS.mmm]'.
    Matches on the digits themselves rather than fixed byte offsets, since
    the hour field isn't guaranteed to always be zero-padded to 2 digits
    (log_parser.py's own TIME_RE tolerates 1-2 digit hours for the same
    reason)."""
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return None
    h, m, s = (int(g) for g in match.groups())
    return h * 3600 + m * 60 + s


def extract_encounter_bytes(
    path: str,
    start_line: int,
    end_line: int,
    area_entered_line: Optional[int] = None,
) -> bytes:
    """Extracts just one encounter's lines (1-based, inclusive) from a raw
    log file, optionally prepending the AreaEntered line if it fell before
    start_line (Parsely needs that line for zone/operation context), and
    including a trailing window of lines within TRAILING_SECS seconds past
    end_line's timestamp. Returns the raw (uncompressed) extracted bytes.

    Log files are read as raw bytes rather than decoded text, matching
    BARAS's approach, since SWTOR logs use Windows-1252 encoding and we
    don't need to interpret the text here, just split it on newlines.
    """
    raw = Path(path).read_bytes()
    lines = raw.split(b"\n")

    capture_area_line = area_entered_line is not None and area_entered_line < start_line
    area_line_content: Optional[bytes] = None

    out = bytearray()
    end_line_secs: Optional[int] = None

    for i, raw_line in enumerate(lines, start=1):
        line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line

        if capture_area_line and i == area_entered_line:
            area_line_content = line

        if start_line <= i <= end_line:
            if area_line_content is not None:
                out += area_line_content + b"\n"
                area_line_content = None
            out += line + b"\n"
            if i == end_line:
                end_line_secs = _timestamp_secs(line)
        elif i > end_line:
            if end_line_secs is None:
                break
            line_secs = _timestamp_secs(line)
            if line_secs is None:
                break
            elapsed = line_secs - end_line_secs
            if elapsed < 0:
                break  # timestamp went backwards (midnight rollover) -- stop
            if elapsed <= TRAILING_SECS:
                out += line + b"\n"
            else:
                break

    return bytes(out)


def _parse_response_xml(xml_text: str) -> ParselyUploadResult:
    if "<status>error</status>" in xml_text:
        match = re.search(r"<error>(.*?)</error>", xml_text, re.DOTALL)
        return ParselyUploadResult(
            success=False, error=match.group(1) if match else "Unknown error"
        )
    if "NOT OK" in xml_text:
        return ParselyUploadResult(success=False, error="Upload rejected by server")
    match = re.search(r"<file>(.*?)</file>", xml_text, re.DOTALL)
    if match:
        return ParselyUploadResult(success=True, link=match.group(1))
    return ParselyUploadResult(success=False, error=f"Unexpected response: {xml_text[:200]}")


def _build_form(
    visibility: int,
    notes: Optional[str],
    username: Optional[str],
    password: Optional[str],
    guild: Optional[str],
    guild_log: bool,
    no_guild: bool,
) -> dict:
    form = {"public": str(visibility)}
    if notes:
        form["notes"] = notes
    if username and password:
        form["username"] = username
        form["password"] = password
        if not no_guild and guild:
            form["guild"] = guild
    if guild_log and not no_guild:
        form["guild-log"] = "1"
    return form


def _send(compressed: bytes, filename: str, form: dict) -> ParselyUploadResult:
    files = {"file": (filename, compressed, "text/html")}
    try:
        response = requests.post(
            PARSELY_URL,
            headers={"User-Agent": USER_AGENT},
            data=form,
            files=files,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return ParselyUploadResult(success=False, error=f"Upload failed: {exc}")
    return _parse_response_xml(response.text)


def upload_file(
    path: str,
    visibility: int = 1,
    notes: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    guild: Optional[str] = None,
    guild_log: bool = False,
    no_guild: bool = False,
) -> ParselyUploadResult:
    """Uploads an entire log file."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return ParselyUploadResult(success=False, error="File is empty or missing")

    compressed = _gzip_bytes(p.read_bytes())
    form = _build_form(visibility, notes, username, password, guild, guild_log, no_guild)
    return _send(compressed, p.name, form)


def upload_encounter(
    path: str,
    start_line: int,
    end_line: int,
    area_entered_line: Optional[int] = None,
    visibility: int = 1,
    notes: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    guild: Optional[str] = None,
    guild_log: bool = False,
    no_guild: bool = False,
) -> ParselyUploadResult:
    """Uploads just one encounter's line range from a log file."""
    extracted = extract_encounter_bytes(path, start_line, end_line, area_entered_line)
    if not extracted:
        return ParselyUploadResult(success=False, error="No lines extracted")

    compressed = _gzip_bytes(extracted)
    filename = f"{Path(path).name}_lines_{start_line}-{end_line}.txt"
    form = _build_form(visibility, notes, username, password, guild, guild_log, no_guild)
    return _send(compressed, filename, form)
