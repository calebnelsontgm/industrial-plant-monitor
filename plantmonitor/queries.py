"""One Python function per file in ``sql/``, named after it.

Each wrapper binds its parameters positionally, in the order the matching
``.sql`` file documents, and returns the result as a DataFrame. No SQL appears
here: the statements live in ``sql/``.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from .db import query


def kpi_summary(
    as_of: str,
    window_days: int = 7,
    connection: sqlite3.Connection | None = None,
) -> pd.DataFrame:
    """Machines online, uptime %, recent failures and open alerts. One row."""
    return query("kpi_summary.sql", (as_of, window_days), connection)


def machine_baseline(
    as_of: str, connection: sqlite3.Connection | None = None
) -> pd.DataFrame:
    """Mean and variance of each reading type per machine, one row per machine."""
    return query("machine_baseline.sql", (as_of,), connection)


def abnormal_readings(
    machine_id: int,
    start_ts: str,
    end_ts: str,
    connection: sqlite3.Connection | None = None,
) -> pd.DataFrame:
    """Readings for one machine that fall outside its normal band."""
    return query("abnormal_readings.sql", (machine_id, start_ts, end_ts), connection)


def downtime_by_machine(
    start_ts: str, end_ts: str, connection: sqlite3.Connection | None = None
) -> pd.DataFrame:
    """Downtime hours and event count per machine, worst first."""
    return query("downtime_by_machine.sql", (start_ts, end_ts), connection)


def downtime_by_line(
    start_ts: str, end_ts: str, connection: sqlite3.Connection | None = None
) -> pd.DataFrame:
    """Downtime hours and event count per production line, worst first."""
    return query("downtime_by_line.sql", (start_ts, end_ts), connection)


def failures_by_type(
    start_ts: str, end_ts: str, connection: sqlite3.Connection | None = None
) -> pd.DataFrame:
    """Failure counts by type and machine."""
    return query("failures_by_type.sql", (start_ts, end_ts), connection)


def speed_week_over_week(
    start_ts: str, end_ts: str, connection: sqlite3.Connection | None = None
) -> pd.DataFrame:
    """Average speed per 7-day block per machine, with the change from the previous."""
    return query("speed_week_over_week.sql", (start_ts, end_ts), connection)


def before_failure(
    start_ts: str, end_ts: str, connection: sqlite3.Connection | None = None
) -> pd.DataFrame:
    """Average readings in the 24 hours before each failure, against normal."""
    return query("before_failure.sql", (start_ts, end_ts), connection)


def machine_timeline(
    machine_id: int,
    start_ts: str,
    end_ts: str,
    connection: sqlite3.Connection | None = None,
) -> pd.DataFrame:
    """One machine's downtime events and failures, as one ordered timeline."""
    return query("machine_timeline.sql", (machine_id, start_ts, end_ts), connection)


def plant_status(
    as_of: str, connection: sqlite3.Connection | None = None
) -> pd.DataFrame:
    """Every machine's line, place, running state, open alerts and last reading."""
    return query("plant_status.sql", (as_of,), connection)


def dataset_settings(connection: sqlite3.Connection | None = None) -> pd.DataFrame:
    """The active dataset's settings. At most one row."""
    return query("dataset_settings.sql", (), connection)


def alerts_list(connection: sqlite3.Connection | None = None) -> pd.DataFrame:
    """Every alert with its machine name, newest first."""
    return query("alerts_list.sql", (), connection)


def machine_readings(
    machine_id: int,
    start_ts: str,
    end_ts: str,
    connection: sqlite3.Connection | None = None,
) -> pd.DataFrame:
    """One machine's readings over a date range, in timestamp order."""
    return query("machine_readings.sql", (machine_id, start_ts, end_ts), connection)


def standard_deviation(variance: float) -> float:
    """Square root of a variance.

    SQLite returns variances because it may have no ``sqrt()``; the square root
    is taken here, in Python, when a standard deviation is needed for display.
    """
    return float(variance) ** 0.5


__all__ = [
    "abnormal_readings",
    "alerts_list",
    "before_failure",
    "dataset_settings",
    "downtime_by_line",
    "downtime_by_machine",
    "failures_by_type",
    "kpi_summary",
    "machine_baseline",
    "machine_readings",
    "machine_timeline",
    "plant_status",
    "speed_week_over_week",
    "standard_deviation",
]
