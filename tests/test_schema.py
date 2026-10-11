"""Schema tests: the tables exist and foreign keys are enforced."""

from __future__ import annotations

import sqlite3

import pytest

#: Every table schema.sql is expected to create.
EXPECTED_TABLES = {
    "lines",
    "machines",
    "readings",
    "downtime_events",
    "failures",
    "alerts",
    "dataset_meta",
}


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def test_all_tables_exist(empty_db: sqlite3.Connection) -> None:
    """All seven tables are created from schema.sql."""
    assert EXPECTED_TABLES <= table_names(empty_db)


def test_foreign_keys_enforced(empty_db: sqlite3.Connection) -> None:
    """Inserting a reading for machine 999 raises an error."""
    with pytest.raises(sqlite3.IntegrityError):
        empty_db.execute(
            "INSERT INTO readings"
            " (machine_id, ts, temperature_c, speed_rpm, vibration_mm_s)"
            " VALUES (?, ?, ?, ?, ?)",
            (999, "2026-09-30T14:15:00", 75.0, 1500.0, 3.0),
        )


def test_alerts_reject_duplicates(empty_db: sqlite3.Connection) -> None:
    """The UNIQUE key stops a second alert for the same machine, rule and ts."""
    empty_db.execute("INSERT INTO lines (id, name) VALUES (1, 'Line 1')")
    empty_db.execute(
        "INSERT INTO machines (id, name, line_id) VALUES (1, 'M-01', 1)"
    )
    alert = (1, "2026-09-30T14:15:00", "sustained_heat", "High", "too hot")
    columns = (
        "INSERT INTO alerts (machine_id, ts, rule, severity, message)"
        " VALUES (?, ?, ?, ?, ?)"
    )
    empty_db.execute(columns, alert)

    with pytest.raises(sqlite3.IntegrityError):
        empty_db.execute(columns, alert)


def test_downtime_requires_end_after_start(empty_db: sqlite3.Connection) -> None:
    """The CHECK constraint rejects an event ending before it starts."""
    empty_db.execute("INSERT INTO lines (id, name) VALUES (1, 'Line 1')")
    empty_db.execute(
        "INSERT INTO machines (id, name, line_id) VALUES (1, 'M-01', 1)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        empty_db.execute(
            "INSERT INTO downtime_events (machine_id, start_ts, end_ts, reason)"
            " VALUES (?, ?, ?, ?)",
            (1, "2026-09-30T14:00:00", "2026-09-30T13:00:00", "jam"),
        )


def test_failure_type_restricted(empty_db: sqlite3.Connection) -> None:
    """failure_type only accepts overheat, bearing or power."""
    empty_db.execute("INSERT INTO lines (id, name) VALUES (1, 'Line 1')")
    empty_db.execute(
        "INSERT INTO machines (id, name, line_id) VALUES (1, 'M-01', 1)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        empty_db.execute(
            "INSERT INTO failures (machine_id, ts, failure_type)"
            " VALUES (?, ?, ?)",
            (1, "2026-09-30T14:15:00", "gremlins"),
        )
