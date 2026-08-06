"""
Covers history_migration.py -- rebuilding stored History rows that were
written by an older parser.

This exists because History is persisted AGGREGATE: each row keeps
per-player totals, not the events behind them, so fixing the parser does
nothing for pulls already on disk. After the death-detection fix the app
contradicted itself -- Deep Dive re-read the raw log and reported the right
death counts while History kept showing the inflated ones.

Measured on a real 200-row history: 849 recorded deaths rebuilt to 667
(the extra 182 were "Deathmark" and other abilities whose names merely
contain the word), 56 rows turned out not to be fights at all, and 9 rows
could not be rebuilt because their line range points PAST the end of the
log they name -- recorded around a log rollover, when the reader's line
numbering had moved to the next file while log_path still named the
previous one.
"""
import json

import pytest

import history_migration
import storage
from conftest import log_line
from stats import Encounter, HISTORY_DATA_VERSION


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))


def _log(tmp_path, name="combat_2026-08-06_20_00_00_1.txt", *, deaths=0, damage=True):
    """A pull with `deaths` player deaths, plus a Deathmark stack change --
    the exact false positive the old parser counted as a death."""
    lines = [log_line("00:00:00.000", "@Dps#1", effect_type="Event",
                      effect_name="EnterCombat {1}")]
    if damage:
        for i in range(6):
            lines.append(log_line(f"00:00:{1 + i:02d}.000", "@Dps#1", target="Boss",
                                  ability="Smash", effect_name="Damage {2}", amount="1000"))
    else:
        for i in range(6):
            lines.append(log_line(f"00:00:{1 + i:02d}.000", "@Healer#2", target="@Dps#1",
                                  ability="Kolto", effect_name="Heal {2}", amount="500"))
    # Not a death -- a Marauder debuff re-logged on every stack change.
    lines.append(log_line("00:00:08.000", "@Dps#1", target="Boss", ability="Vicious Throw",
                          effect_type="ModifyCharges", effect_name="Deathmark {1}"))
    for i in range(deaths):
        lines.append(log_line(f"00:00:{9 + i:02d}.000", "Boss", target=f"@P{i}#{i}",
                              ability="Crush", effect_type="Event", effect_name="Death"))
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="cp1252")
    return str(path), len(lines)


def _legacy_row(path, n_lines, *, recorded_deaths, players=("Dps",)):
    """A row as an OLD build would have written it: no data_version, and a
    death count the old parser produced."""
    enc = Encounter()
    enc.log_path = path
    enc.start_line = 1
    enc.end_line = n_lines
    enc._fixed_duration = 30.0
    for name in players:
        p = enc._get(name)
        p.is_player = True
        p.deaths = recorded_deaths
        p.damage_done = 6000.0
    d = enc.to_dict()
    d.pop("data_version", None)      # written before the field existed
    d.pop("unverified", None)
    return Encounter.from_dict(d)


# --------------------------------------------------------------- detection

def test_a_legacy_row_needs_migration():
    assert Encounter.from_dict({"players": []}).data_version == 0
    assert history_migration.needs_migration([Encounter.from_dict({"players": []})])


def test_a_current_row_does_not():
    assert not history_migration.needs_migration([Encounter()])


def test_an_empty_history_does_not():
    assert not history_migration.needs_migration([])


# ----------------------------------------------------------------- rebuild

def test_phantom_deaths_are_removed(tmp_path):
    """The headline fix: the old parser counted Deathmark as a death."""
    path, n = _log(tmp_path, deaths=0)
    row = _legacy_row(path, n, recorded_deaths=3)
    assert sum(p.deaths for p in row.players.values()) == 3

    out, report = history_migration.migrate([row])
    assert report["rebuilt"] == 1
    assert sum(p.deaths for p in out[0].players.values()) == 0, (
        "Deathmark is a debuff, not a death"
    )


def test_real_deaths_survive(tmp_path):
    path, n = _log(tmp_path, deaths=2)
    out, report = history_migration.migrate([_legacy_row(path, n, recorded_deaths=99)])
    assert report["rebuilt"] == 1
    assert sum(p.deaths for p in out[0].players.values()) == 2


def test_a_row_that_was_never_a_fight_is_dropped(tmp_path):
    """56 of the real 200 rows were between-pull healing segments."""
    path, n = _log(tmp_path, damage=False)
    out, report = history_migration.migrate([_legacy_row(path, n, recorded_deaths=0)])
    assert report["dropped"] == 1
    assert out == []


def test_the_line_range_is_preserved_so_deep_dive_still_works(tmp_path):
    path, n = _log(tmp_path, deaths=1)
    out, _ = history_migration.migrate([_legacy_row(path, n, recorded_deaths=1)])
    assert out[0].log_path == path
    assert (out[0].start_line, out[0].end_line) == (1, n)


def test_rebuilt_rows_are_stamped_current(tmp_path):
    path, n = _log(tmp_path, deaths=1)
    out, _ = history_migration.migrate([_legacy_row(path, n, recorded_deaths=1)])
    assert out[0].data_version == HISTORY_DATA_VERSION
    assert not history_migration.needs_migration(out)


# ------------------------------------------------------------ unrebuildable

def test_a_range_past_the_end_of_the_file_is_kept_but_flagged(tmp_path):
    """The real case: 9 of 200 rows referenced line 139,109 in a 138,663
    line log, recorded around a rollover. Losing a raid night to a
    migration would be far worse than a stale number."""
    path, n = _log(tmp_path, deaths=1)
    row = _legacy_row(path, n, recorded_deaths=7)
    row.start_line, row.end_line = n + 500, n + 900

    out, report = history_migration.migrate([row])
    assert report["unrebuildable"] == 1
    assert len(out) == 1, "the row must be kept, not deleted"
    assert out[0].unverified is True
    assert sum(p.deaths for p in out[0].players.values()) == 7, "its old numbers are untouched"


def test_a_missing_log_file_is_kept_but_not_retried(tmp_path):
    row = _legacy_row(str(tmp_path / "gone.txt"), 50, recorded_deaths=4)
    out, report = history_migration.migrate([row])
    assert report["unrebuildable"] == 1
    assert len(out) == 1


def test_an_unverified_row_stops_the_migration_from_re_running(tmp_path):
    """Without this the answer stays True forever and every launch re-runs
    (and re-fails) the whole migration."""
    path, n = _log(tmp_path, deaths=1)
    row = _legacy_row(path, n, recorded_deaths=7)
    row.start_line, row.end_line = n + 500, n + 900

    out, _ = history_migration.migrate([row])
    assert history_migration.needs_migration(out) is False


def test_migration_is_idempotent(tmp_path):
    """Second pass must be a no-op, not a re-rebuild."""
    path, n = _log(tmp_path, deaths=2)
    first, r1 = history_migration.migrate([_legacy_row(path, n, recorded_deaths=9)])
    second, r2 = history_migration.migrate(first)
    assert r2["rebuilt"] == 0 and r2["dropped"] == 0
    assert len(second) == len(first)
    assert (sum(p.deaths for e in second for p in e.players.values())
            == sum(p.deaths for e in first for p in e.players.values()))


def test_the_input_list_is_not_mutated(tmp_path):
    path, n = _log(tmp_path, damage=False)
    rows = [_legacy_row(path, n, recorded_deaths=0)]
    out, _ = history_migration.migrate(rows)
    assert len(rows) == 1, "caller's list must survive even when rows are dropped"
    assert out == []


# ------------------------------------------------------------- persistence

def test_the_result_round_trips_through_storage(tmp_path):
    path, n = _log(tmp_path, deaths=2)
    out, _ = history_migration.migrate([_legacy_row(path, n, recorded_deaths=9)])
    storage.save_history(out)
    reloaded = storage.load_history()
    assert len(reloaded) == 1
    assert reloaded[0].data_version == HISTORY_DATA_VERSION
    assert sum(p.deaths for p in reloaded[0].players.values()) == 2
    assert not history_migration.needs_migration(reloaded)


def test_the_unverified_flag_survives_a_save(tmp_path):
    path, n = _log(tmp_path, deaths=1)
    row = _legacy_row(path, n, recorded_deaths=7)
    row.start_line, row.end_line = n + 500, n + 900
    out, _ = history_migration.migrate([row])
    storage.save_history(out)
    assert storage.load_history()[0].unverified is True
