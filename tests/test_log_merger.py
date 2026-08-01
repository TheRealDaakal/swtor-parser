"""
Covers LogClock: raw seconds-of-day goes backwards at midnight, and
(observed in a real log) whenever the system clock is adjusted mid-session.
Either one, unhandled, makes an encounter spanning the jump compute a
negative duration -> clamped to 0.001s -> silently dropped by
MIN_ENCOUNTER_SECONDS. A whole pull vanishes with no error. Confirmed on a
real log before the fix: 94 pulls -> 95 after.
"""
import pytest

from log_merger import LogClock, _seconds_of_day


def test_clock_is_monotonic_across_midnight():
    clock = LogClock()
    readings = [clock(ts) for ts in
                ("23:59:58.000", "23:59:59.500", "00:00:00.250", "00:00:01.000")]
    assert readings == sorted(readings), "clock must never go backwards across midnight"
    # a day was correctly added, not just clamped
    assert readings[-1] - readings[0] == pytest.approx(3.0)


def test_clock_pins_rather_than_reverses_on_a_clock_adjustment():
    """The real case this was built for: 18:23:34.684 -> 16:23:35.036 mid-
    combat, same players, ~0.35s of actual elapsed time -- a DST/NTP
    correction written straight into the log, not a date rollover (the
    jump is ~2 hours, far under the half-day rollover threshold). The real
    elapsed time isn't recoverable from the file, so the clock holds
    still instead of inventing a number or running backwards."""
    clock = LogClock()
    before = clock("18:23:34.684")
    after_jump = clock("16:23:35.036")
    assert after_jump >= before, "clock must never decrease"
    assert after_jump == before, "an unrecoverable jump should pin, not guess at elapsed time"

    later = clock("16:23:36.000")
    assert later > after_jump, "normal forward progression resumes after the pin"


def test_a_short_ordinary_gap_is_unaffected():
    """A rollover/adjustment isn't the only way time can look like it
    moved -- make sure the common case (nothing weird happening) just
    passes through unchanged."""
    clock = LogClock()
    a = clock("10:00:00.000")
    b = clock("10:00:05.500")
    assert b - a == pytest.approx(5.5)


def test_seconds_of_day_still_wraps_raw():
    """The raw helper is documented as NOT safe alone across a whole file
    -- confirm it still exhibits exactly the failure LogClock exists to
    fix, so a future refactor can't quietly make LogClock redundant
    without anyone noticing the underlying problem changed shape."""
    assert _seconds_of_day("00:00:01.000") < _seconds_of_day("23:59:59.000")
