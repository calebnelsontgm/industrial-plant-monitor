"""Alert engine tests, against the fixture and the full generated dataset."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from plantmonitor import load_config
from plantmonitor.alerts import resolve_alert, run_alerts
from plantmonitor.db import connect
from plantmonitor.load import load_dataset
from tests import fixtures


def alerts_in(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    """Every alert as ``(machine_name, rule)``."""
    rows = connection.execute(
        "SELECT m.name, a.rule FROM alerts a JOIN machines m ON m.id = a.machine_id"
    ).fetchall()
    return set(rows)


def test_sustained_heat_fires_once(fixture_db, fixture_facts) -> None:
    """7 hours above the band creates exactly one high alert."""
    run_alerts(fixture_db)

    rows = fixture_db.execute(
        "SELECT a.severity FROM alerts a JOIN machines m ON m.id = a.machine_id"
        " WHERE m.name = 'M-04' AND a.rule = 'sustained_heat'"
    ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == load_config()["alerts"]["sustained_heat"]["severity"]


def test_long_run_still_fires_once(fixture_db) -> None:
    """14 hours above the band still creates exactly one alert, not two."""
    run_alerts(fixture_db)

    count = fixture_db.execute(
        "SELECT COUNT(*) FROM alerts a JOIN machines m ON m.id = a.machine_id"
        " WHERE m.name = 'M-05' AND a.rule = 'sustained_heat'"
    ).fetchone()[0]

    assert count == 1


def test_single_spike_creates_no_alert(fixture_db, fixture_facts) -> None:
    """One hot reading creates no alert, however far outside the band it is."""
    run_alerts(fixture_db)

    count = fixture_db.execute(
        "SELECT COUNT(*) FROM alerts a JOIN machines m ON m.id = a.machine_id"
        " WHERE m.name = ? AND a.rule = 'sustained_heat'",
        (fixture_facts.outlier_machine,),
    ).fetchone()[0]

    assert count == 0


def test_speed_threshold(fixture_db, fixture_facts) -> None:
    """A 12% drop fires; a 5% drop does not."""
    run_alerts(fixture_db)
    found = alerts_in(fixture_db)

    assert (fixture_facts.step_machine, "speed_drop") in found
    assert (fixture_facts.small_step_machine, "speed_drop") not in found


def test_repeated_downtime(fixture_db, fixture_facts) -> None:
    """Two downtime events inside the window fire the rule once."""
    run_alerts(fixture_db)

    rows = fixture_db.execute(
        "SELECT a.ts FROM alerts a JOIN machines m ON m.id = a.machine_id"
        " WHERE m.name = ? AND a.rule = 'repeated_downtime'",
        (fixture_facts.downtime_machine,),
    ).fetchall()

    assert len(rows) == 1
    # Timestamped at the second event, the one that reached the threshold.
    assert rows[0][0] == fixtures.DOWNTIME[1][1].strftime(fixtures.TS_FORMAT)


def test_vibration_rising(fixture_db) -> None:
    """A 4-hour vibration run clears the 3-hour threshold."""
    run_alerts(fixture_db)

    assert ("M-06", "vibration_rising") in alerts_in(fixture_db)


def test_clean_machine_has_no_alerts(fixture_db) -> None:
    """The control machine stays quiet."""
    run_alerts(fixture_db)

    assert not any(machine == "M-01" for machine, _rule in alerts_in(fixture_db))


def test_fixture_alerts_are_exactly_as_planted(fixture_db) -> None:
    """Every planted condition fires, and nothing else does."""
    run_alerts(fixture_db)

    assert alerts_in(fixture_db) == fixtures.EXPECTED_ALERTS


def test_running_twice_creates_no_duplicates(fixture_db) -> None:
    """Running twice keeps the same alert count."""
    first = run_alerts(fixture_db)
    before = fixture_db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

    second = run_alerts(fixture_db)
    after = fixture_db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

    assert before == after
    assert first.created > 0
    assert second.created == 0
    assert second.duplicates == first.created


def test_resolve_alert(fixture_db) -> None:
    """Marking an alert resolved takes it out of the open count."""
    run_alerts(fixture_db)
    alert_id = fixture_db.execute("SELECT MIN(id) FROM alerts").fetchone()[0]

    resolve_alert(alert_id, fixture_db)

    assert fixture_db.execute(
        "SELECT resolved FROM alerts WHERE id = ?", (alert_id,)
    ).fetchone()[0] == 1
    assert fixture_db.execute(
        "SELECT COUNT(*) FROM alerts WHERE resolved = 0"
    ).fetchone()[0] == len(fixtures.EXPECTED_ALERTS) - 1


def test_full_dataset_alerts(default_dataset, tmp_path: Path) -> None:
    """Default generated data produces alerts for M-07, M-03 and M-11.

    M-07's temperature climb is the case the band gap exists for: measured
    against a baseline that included the climb, the band widens to 102 C and
    the alert never fires.
    """
    db_path = tmp_path / "full.db"
    load_dataset(
        default_dataset.settings.name,
        data_root=default_dataset.directory.parent,
        db_path=db_path,
    )

    connection = connect(db_path)
    try:
        run_alerts(connection)
        found = alerts_in(connection)
    finally:
        connection.close()

    assert ("M-07", "sustained_heat") in found
    assert ("M-03", "speed_drop") in found
    assert ("M-11", "repeated_downtime") in found
