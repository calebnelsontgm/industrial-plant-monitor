"""Alert engine: four config-driven rules, with duplicate protection.

Each rule's thresholds and severity come from ``config.toml``. Every alert is
timestamped at the moment that triggered it -- the first reading of a run, the
start of a 7-day block, the start of the event that reached the threshold -- so
re-running the engine lands on the same timestamp and the UNIQUE key on
``(machine_id, rule, ts)`` rejects the duplicate.

The analysis is in ``sql/``: band breaches, downtime windows and the
week-over-week comparison are all query files. Three short bookkeeping
statements are inline here -- reading the latest timestamp, probing the reading
interval, and the INSERT -- on the same basis as the loader's inserts, since
the file layout has no place for non-analysis SQL.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from . import load_config
from .db import connect, query
from .queries import speed_week_over_week

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

RULES = ("sustained_heat", "speed_drop", "repeated_downtime", "vibration_rising")


@dataclass
class Alert:
    """One alert, before it reaches the database."""

    machine_id: int
    ts: str
    rule: str
    severity: str
    message: str

    def as_row(self) -> tuple:
        return (self.machine_id, self.ts, self.rule, self.severity, self.message)


@dataclass
class AlertResult:
    """What one run of the engine produced."""

    as_of: str
    created: int = 0
    duplicates: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    machines: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        lines = ["Alerts as of {}".format(self.as_of)]
        lines.append("  {} created, {} already present".format(self.created, self.duplicates))
        for rule in RULES:
            count = self.by_rule.get(rule, 0)
            if count:
                lines.append("  {:<20} {:>3}".format(rule, count))
        for machine in sorted(self.machines):
            lines.append("  {:<8} {}".format(machine, ", ".join(sorted(self.machines[machine]))))
        return "\n".join(lines)


def _latest_timestamp(connection: sqlite3.Connection) -> tuple[str, str]:
    """The first and last reading timestamps in the database."""
    row = connection.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
    if not row or row[0] is None:
        raise ValueError("No readings to run alerts against")
    return row[0], row[1]


def _interval_hours(connection: sqlite3.Connection) -> float:
    """Hours between consecutive readings, measured from the data itself.

    Taken from the data rather than config because the fixture database is
    hourly while a generated dataset is every 15 minutes, and the consecutive
    hour rules have to be right for both.
    """
    rows = connection.execute(
        "SELECT ts FROM readings WHERE machine_id = (SELECT MIN(machine_id) FROM readings)"
        " ORDER BY ts LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        return 1.0
    first = datetime.strptime(rows[0][0], TS_FORMAT)
    second = datetime.strptime(rows[1][0], TS_FORMAT)
    return (second - first).total_seconds() / 3600.0


def _runs(
    stamps: list[str], interval_hours: float
) -> Iterator[tuple[str, float]]:
    """Group consecutive timestamps into runs, yielding ``(first_ts, hours)``.

    Two readings belong to the same run when they are exactly one interval
    apart. A gap of two or more intervals ends the run, so a problem that
    stops and restarts is two alerts rather than one long one.
    """
    if not stamps:
        return

    moments = [datetime.strptime(stamp, TS_FORMAT) for stamp in stamps]
    run_start = 0
    for index in range(1, len(moments)):
        gap = (moments[index] - moments[index - 1]).total_seconds() / 3600.0
        if abs(gap - interval_hours) > 1e-6:
            length = index - run_start
            yield stamps[run_start], length * interval_hours
            run_start = index

    length = len(moments) - run_start
    yield stamps[run_start], length * interval_hours


def _band_rules(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    as_of: str,
    interval_hours: float,
) -> list[Alert]:
    """Sustained heat and rising vibration: runs above the upper band."""
    band = config["band"]
    breaches = query(
        "alert_band_breaches.sql",
        (as_of, band["evaluation_days"], band["baseline_days"], band["sigma"]),
        connection,
    )
    if breaches.empty:
        return []

    specs = (
        ("temperature_above", "sustained_heat", "Temperature"),
        ("vibration_above", "vibration_rising", "Vibration"),
    )

    alerts = []
    for column, rule, label in specs:
        threshold = config["alerts"][rule]["consecutive_hours"]
        severity = config["alerts"][rule]["severity"]
        flagged = breaches[breaches[column] == 1]

        for machine_id, rows in flagged.groupby("machine_id", sort=True):
            stamps = rows.sort_values("ts")["ts"].tolist()
            for first_ts, hours in _runs(stamps, interval_hours):
                if hours >= threshold:
                    alerts.append(
                        Alert(
                            machine_id=int(machine_id),
                            ts=first_ts,
                            rule=rule,
                            severity=severity,
                            message="{} above the normal band for {:.1f} hours".format(
                                label, hours
                            ),
                        )
                    )
    return alerts


def _speed_drop_rule(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    first_ts: str,
    as_of: str,
) -> list[Alert]:
    """A machine whose most recent 7-day block is well below the one before."""
    settings = config["alerts"]["speed_drop"]
    threshold = settings["percent"]

    blocks = speed_week_over_week(first_ts, as_of, connection)
    if blocks.empty:
        return []

    recent = blocks[(blocks["block_index"] == 0) & blocks["percent_change"].notna()]
    return [
        Alert(
            machine_id=int(row["machine_id"]),
            ts=row["block_start"],
            rule="speed_drop",
            severity=settings["severity"],
            message="Average speed down {:.1f}% on the previous 7 days".format(
                abs(row["percent_change"])
            ),
        )
        for _, row in recent.iterrows()
        if row["percent_change"] <= -threshold
    ]


def _repeated_downtime_rule(
    connection: sqlite3.Connection, config: dict[str, Any], as_of: str
) -> list[Alert]:
    """A machine with too many downtime events inside the window."""
    settings = config["alerts"]["repeated_downtime"]
    needed = settings["events"]

    events = query(
        "alert_downtime_window.sql", (as_of, settings["window_days"]), connection
    )
    if events.empty:
        return []

    # The alert is timestamped at the event that reached the threshold, not the
    # most recent one, so the timestamp does not move as more events arrive.
    triggering = events[events["event_number"] == needed]
    return [
        Alert(
            machine_id=int(row["machine_id"]),
            ts=row["start_ts"],
            rule="repeated_downtime",
            severity=settings["severity"],
            message="{} downtime events in {} days".format(
                int(events[events["machine_id"] == row["machine_id"]]["event_number"].max()),
                settings["window_days"],
            ),
        )
        for _, row in triggering.iterrows()
    ]


def run_alerts(
    connection: sqlite3.Connection | None = None,
    *,
    as_of: str | None = None,
    config: dict[str, Any] | None = None,
) -> AlertResult:
    """Evaluate every rule and write any new alerts.

    Returns counts of what was created and what was already there. Running
    twice in a row creates nothing the second time.
    """
    config = config or load_config()
    owns_connection = connection is None
    connection = connection or connect()

    try:
        first_ts, last_ts = _latest_timestamp(connection)
        as_of = as_of or last_ts
        interval_hours = _interval_hours(connection)

        alerts: list[Alert] = []
        alerts.extend(_band_rules(connection, config, as_of, interval_hours))
        alerts.extend(_speed_drop_rule(connection, config, first_ts, as_of))
        alerts.extend(_repeated_downtime_rule(connection, config, as_of))

        result = AlertResult(as_of=as_of)
        before = connection.total_changes
        connection.executemany(
            "INSERT OR IGNORE INTO alerts"
            " (machine_id, ts, rule, severity, message)"
            " VALUES (?, ?, ?, ?, ?)",
            [alert.as_row() for alert in alerts],
        )
        connection.commit()

        result.created = connection.total_changes - before
        result.duplicates = len(alerts) - result.created

        names = dict(connection.execute("SELECT id, name FROM machines").fetchall())
        for alert in alerts:
            result.by_rule[alert.rule] = result.by_rule.get(alert.rule, 0) + 1
            result.machines.setdefault(names[alert.machine_id], []).append(alert.rule)

        return result
    finally:
        if owns_connection:
            connection.close()


def resolve_alert(
    alert_id: int, connection: sqlite3.Connection | None = None
) -> None:
    """Mark one alert resolved, for the dashboard's Alerts page."""
    owns_connection = connection is None
    connection = connection or connect()
    try:
        connection.execute("UPDATE alerts SET resolved = 1 WHERE id = ?", (alert_id,))
        connection.commit()
    finally:
        if owns_connection:
            connection.close()


__all__ = ["RULES", "Alert", "AlertResult", "resolve_alert", "run_alerts"]
