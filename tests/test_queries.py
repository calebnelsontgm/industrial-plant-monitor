"""Query tests.

The seven ``sql/*.sql`` files are stubs until the developer writes them, so
every test in the "developer's queries" section below fails with a SQL error
until then. That is the expected state at the end of phase 4.

The fixture tests at the top do not touch the stubs. They check that the
fixture database really holds the values the query tests assert against, so a
failure there means the fixture is wrong, not the SQL.
"""

from __future__ import annotations

import pandas as pd
import pytest

from plantmonitor import queries
from tests import fixtures

# --- Fixture self-checks: these pass without any developer SQL. ------------


def test_fixture_row_counts(fixture_db, fixture_facts) -> None:
    """The fixture holds 3 machines of hourly readings over 21 days."""
    counts = dict(
        fixture_db.execute(
            "SELECT m.name, COUNT(*) FROM readings r"
            " JOIN machines m ON m.id = r.machine_id GROUP BY m.name"
        ).fetchall()
    )
    assert set(counts) == set(fixtures.MACHINES)
    assert all(count == fixture_facts.readings_per_machine for count in counts.values())


def test_fixture_means_are_exact(fixture_db, fixture_facts) -> None:
    """The 24-hour wave averages out, so each machine's mean is its base value.

    M-03 is excluded: its planted temperature outliers shift its mean on
    purpose.
    """
    rows = fixture_db.execute(
        "SELECT m.name, AVG(r.temperature_c), AVG(r.speed_rpm)"
        " FROM readings r JOIN machines m ON m.id = r.machine_id"
        " GROUP BY m.name"
    ).fetchall()

    clean = set(fixture_facts.clean_temperature_machines())
    for name, mean_temperature, mean_speed in rows:
        base = fixture_facts.base_values[name]
        if name in clean:
            assert mean_temperature == pytest.approx(base["temperature_c"], abs=0.01)
        if name not in fixtures.SPEED_STEPS:
            assert mean_speed == pytest.approx(base["speed_rpm"], abs=0.01)


def test_fixture_has_no_accidental_outliers(fixture_db, fixture_facts) -> None:
    """Only the planted readings sit outside the band.

    Three standard deviations of the wave is wider than the wave's own peak, so
    an ordinary reading cannot breach the band by accident. This is what makes
    "returns exactly the planted outliers" an exact expectation.
    """
    machine_id = fixture_facts.machine_ids[fixture_facts.outlier_machine]
    rows = fixture_db.execute(
        "SELECT ts, temperature_c FROM readings WHERE machine_id = ?", (machine_id,)
    ).fetchall()

    breached = {
        ts
        for ts, temperature in rows
        if abs(temperature - fixture_facts.base_values[fixture_facts.outlier_machine]["temperature_c"])
        > 3 * (fixtures.expected_variance("temperature_c") ** 0.5)
    }
    assert breached == set(fixture_facts.outlier_timestamps)


def test_fixture_step_is_exactly_twelve_percent(fixture_db, fixture_facts) -> None:
    """The final 7-day block is exactly 12% below the block before it."""
    machine_id = fixture_facts.machine_ids[fixture_facts.step_machine]
    speeds = [
        row[0]
        for row in fixture_db.execute(
            "SELECT speed_rpm FROM readings WHERE machine_id = ? ORDER BY ts",
            (machine_id,),
        ).fetchall()
    ]
    block = fixtures.STEP_DAYS * 24
    final = sum(speeds[-block:]) / block
    previous = sum(speeds[-2 * block : -block]) / block

    assert (previous - final) / previous * 100 == pytest.approx(
        fixture_facts.step_percent, abs=0.01
    )


def test_fixture_downtime_totals_eight_hours(fixture_db, fixture_facts) -> None:
    """Two events of 3 and 5 hours."""
    count, hours = fixture_db.execute(
        "SELECT COUNT(*),"
        " SUM((julianday(end_ts) - julianday(start_ts)) * 24)"
        " FROM downtime_events"
    ).fetchone()

    assert count == fixture_facts.downtime_events
    assert hours == pytest.approx(fixture_facts.downtime_hours, abs=1e-6)


# --- The developer's queries: these fail until the stubs are written. ------


def test_kpi_summary(fixture_db, fixture_facts) -> None:
    """Failures in the past 7 days and uptime % for both windows."""
    as_of = fixture_facts.last_ts

    for window in (7, 30):
        frame = queries.kpi_summary(as_of, window, fixture_db)
        assert len(frame) == 1
        row = frame.iloc[0]
        assert row["failures_recent"] == 1
        assert 0 <= row["uptime_percent"] <= 100
        assert row["machines_online"] == len(fixtures.MACHINES)
        assert row["open_alerts"] == 0

    machines = len(fixtures.MACHINES)

    # Only the 5-hour event falls in the last 7 days; the 3-hour one is 10 days
    # back. Uptime is measured against every machine-hour in the window, so
    # 3 machines x 7 days x 24 = 504 machine-hours.
    week = queries.kpi_summary(as_of, 7, fixture_db).iloc[0]
    week_hours = machines * 7 * 24
    assert week["uptime_percent"] == pytest.approx(
        (week_hours - 5.0) / week_hours * 100, abs=0.05
    )

    # The 30-day window reaches back past both events, so all 8 hours count.
    month = queries.kpi_summary(as_of, 30, fixture_db).iloc[0]
    month_hours = machines * 30 * 24
    assert month["uptime_percent"] == pytest.approx(
        (month_hours - fixture_facts.downtime_hours) / month_hours * 100, abs=0.05
    )


def test_machine_baseline_means(fixture_db, fixture_facts) -> None:
    """Per-machine mean temperature matches the fixture's base values."""
    frame = queries.machine_baseline(fixture_facts.last_ts, fixture_db)
    assert len(frame) == len(fixtures.MACHINES)

    by_name = frame.set_index("machine_name")
    for name in fixture_facts.clean_temperature_machines():
        expected = fixture_facts.base_values[name]["temperature_c"]
        assert by_name.loc[name, "mean_temperature_c"] == pytest.approx(
            expected, abs=0.05
        ), name

    # The machines with planted heat read higher than their base value.
    for name in fixtures.TEMPERATURE_ANOMALY_MACHINES:
        base = fixture_facts.base_values[name]["temperature_c"]
        assert by_name.loc[name, "mean_temperature_c"] > base


def test_machine_baseline_variance(fixture_db, fixture_facts) -> None:
    """Variance of the wave is amplitude squared over two."""
    frame = queries.machine_baseline(fixture_facts.last_ts, fixture_db)
    by_name = frame.set_index("machine_name")

    for name in fixture_facts.clean_temperature_machines():
        assert by_name.loc[name, "var_temperature_c"] == pytest.approx(
            fixtures.expected_variance("temperature_c"), abs=0.05
        )


def test_abnormal_readings_finds_exactly_the_planted_outliers(
    fixture_db, fixture_facts
) -> None:
    """Returns exactly the readings placed outside the band, and nothing else."""
    machine_id = fixture_facts.machine_ids[fixture_facts.outlier_machine]
    frame = queries.abnormal_readings(
        machine_id, fixture_facts.first_ts, fixture_facts.last_ts, fixture_db
    )

    assert set(frame["ts"]) == set(fixture_facts.outlier_timestamps)
    assert frame["temperature_abnormal"].sum() == len(fixture_facts.outlier_timestamps)


def test_abnormal_readings_respects_the_date_range(fixture_db, fixture_facts) -> None:
    """No rows outside the requested range.

    A range ending on a date includes that whole day, so the upper bound is
    that date at 23:59:59, not midnight.
    """
    machine_id = fixture_facts.machine_ids[fixture_facts.outlier_machine]
    start, end = "2026-09-15", "2026-09-20"
    frame = queries.abnormal_readings(machine_id, start, end, fixture_db)

    assert (frame["ts"] >= start).all()
    assert (frame["ts"] <= end + "T23:59:59").all()


def test_abnormal_readings_empty_range_is_empty(fixture_db, fixture_facts) -> None:
    """A range with no data returns an empty DataFrame, not an error."""
    machine_id = fixture_facts.machine_ids[fixture_facts.outlier_machine]
    frame = queries.abnormal_readings(
        machine_id, "2020-01-01T00:00:00", "2020-01-02T00:00:00", fixture_db
    )

    assert isinstance(frame, pd.DataFrame)
    assert frame.empty


def test_downtime_by_machine(fixture_db, fixture_facts) -> None:
    """Fixture downtime hours and event count match."""
    frame = queries.downtime_by_machine(*fixture_facts.date_range, fixture_db)
    row = frame.set_index("machine_name").loc[fixture_facts.downtime_machine]

    assert row["event_count"] == fixture_facts.downtime_events
    assert row["downtime_hours"] == pytest.approx(fixture_facts.downtime_hours, abs=0.01)


def test_downtime_sorted_worst_first(fixture_db, fixture_facts) -> None:
    """Rows come back ordered by downtime, descending."""
    frame = queries.downtime_by_machine(*fixture_facts.date_range, fixture_db)
    hours = frame["downtime_hours"].tolist()

    assert hours == sorted(hours, reverse=True)


def test_failures_by_type(fixture_db, fixture_facts) -> None:
    """The fixture's single failure is counted once, under its own type."""
    frame = queries.failures_by_type(*fixture_facts.date_range, fixture_db)

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["machine_name"] == fixture_facts.failure_machine
    assert row["failure_type"] == fixture_facts.failure_type
    assert row["failure_count"] == 1


def test_speed_week_over_week(fixture_db, fixture_facts) -> None:
    """The machine made 12% slower shows -12% (+/-0.5%)."""
    frame = queries.speed_week_over_week(*fixture_facts.date_range, fixture_db)
    recent = frame[
        (frame["machine_name"] == fixture_facts.step_machine)
        & (frame["block_index"] == 0)
    ]

    assert len(recent) == 1
    assert recent.iloc[0]["percent_change"] == pytest.approx(
        -fixture_facts.step_percent, abs=0.5
    )


def test_speed_week_over_week_oldest_block_has_no_previous(
    fixture_db, fixture_facts
) -> None:
    """LAG leaves the oldest block's comparison empty rather than zero."""
    frame = queries.speed_week_over_week(*fixture_facts.date_range, fixture_db)
    oldest = frame.sort_values("block_index").groupby("machine_id").tail(1)

    assert oldest["previous_avg_speed_rpm"].isna().all()


def test_before_failure(fixture_db, fixture_facts) -> None:
    """The 24-hour run-up to the fixture's failure is reported against normal."""
    frame = queries.before_failure(*fixture_facts.date_range, fixture_db)

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["machine_name"] == fixture_facts.failure_machine
    assert row["failure_ts"] == fixture_facts.failure_ts

    base = fixture_facts.base_values[fixture_facts.failure_machine]
    assert row["mean_speed_before"] == pytest.approx(base["speed_rpm"], abs=1.0)
    assert row["baseline_speed_rpm"] == pytest.approx(base["speed_rpm"], abs=1.0)


def test_empty_range_returns_empty_frames(fixture_db, fixture_facts) -> None:
    """Every range query survives a range with no data."""
    empty = ("2020-01-01T00:00:00", "2020-01-02T00:00:00")

    for call in (
        queries.downtime_by_machine,
        queries.failures_by_type,
        queries.speed_week_over_week,
        queries.before_failure,
    ):
        frame = call(*empty, fixture_db)
        assert isinstance(frame, pd.DataFrame), call.__name__
        assert frame.empty, call.__name__


def test_every_query_runs_under_a_second(full_db_path) -> None:
    """Each dashboard query runs in under 1 second on the full dataset."""
    import time

    from plantmonitor.db import connect

    as_of, start, end = "2026-10-01T23:45:00", "2026-07-04", "2026-10-01"
    calls = {
        "kpi_summary": (queries.kpi_summary, (as_of, 30)),
        "machine_baseline": (queries.machine_baseline, (as_of,)),
        "abnormal_readings": (queries.abnormal_readings, (7, start, end)),
        "downtime_by_machine": (queries.downtime_by_machine, (start, end)),
        "failures_by_type": (queries.failures_by_type, (start, end)),
        "speed_week_over_week": (queries.speed_week_over_week, (start, end)),
        "before_failure": (queries.before_failure, (start, end)),
        "machine_readings": (queries.machine_readings, (7, start, end)),
    }

    connection = connect(full_db_path)
    try:
        slow = {}
        for name, (call, args) in calls.items():
            started = time.perf_counter()
            call(*args, connection)
            elapsed = time.perf_counter() - started
            if elapsed >= 1.0:
                slow[name] = elapsed
    finally:
        connection.close()

    assert not slow, "slower than 1s: {}".format(slow)
