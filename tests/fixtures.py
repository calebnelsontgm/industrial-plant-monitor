"""The fixture database the query and alert tests run against.

Small, but built so the expected answers are known exactly rather than
approximately:

* Readings vary on a 24-hour sine wave instead of random noise. Over any whole
  number of days the wave averages to zero, so a machine's mean is exactly its
  base value and its variance is exactly ``amplitude ** 2 / 2``.
* Because three standard deviations of that wave is ``3 * amplitude / 1.414``,
  which is wider than the wave's own peak, no ordinary reading can fall outside
  the normal band by accident. Every abnormal reading is one that was planted.
* The speed steps are applied to exactly the final 7-day block, so the
  week-over-week change is exactly -12.00% and -5.00%.
* The two downtime events are 3 and 5 hours, so the total is exactly 8 hours.

Each machine carries one job, so the alert tests can assert "exactly one alert"
without two planted conditions interfering:

===== =================================================================
M-01  clean control: no alert of any kind
M-02  speed steps down 12% for the final block: speed_drop fires
M-03  three single-hour temperature spikes, 2 downtime events, 1 failure:
      repeated_downtime fires, sustained_heat must NOT
M-04  7-hour temperature run above the band: sustained_heat fires once
M-05  14-hour temperature run above the band: still exactly one alert
M-06  speed steps down 5% (too small to fire) and a 4-hour vibration run
      above the band: vibration_rising fires, speed_drop does not
===== =================================================================
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from plantmonitor.db import connect, run_script

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: Hourly readings keep the fixture small; 15-minute spacing is not needed to
#: test SQL, and the alert rules are expressed in hours either way.
INTERVAL_HOURS = 1
DAYS = 21
START = datetime(2026, 9, 10, 0, 0, 0)
READINGS_PER_MACHINE = DAYS * 24 // INTERVAL_HOURS
WAVE_PERIOD_HOURS = 24

#: ``name -> (id, line_id, base temperature, base speed, base vibration)``
MACHINES = {
    "M-01": (1, 1, 70.0, 1500.0, 3.00),
    "M-02": (2, 2, 75.0, 1450.0, 2.50),
    "M-03": (3, 1, 80.0, 1550.0, 3.50),
    "M-04": (4, 2, 72.0, 1480.0, 2.80),
    "M-05": (5, 1, 78.0, 1520.0, 3.20),
    "M-06": (6, 2, 74.0, 1500.0, 3.00),
}

#: Peak of the 24-hour wave for each reading type.
AMPLITUDE = {"temperature_c": 2.0, "speed_rpm": 20.0, "vibration_mm_s": 0.20}

# --- Planted facts: these are the expected answers. ------------------------

#: ``machine -> percent`` speed step applied to the final 7-day block.
SPEED_STEPS = {"M-02": 12.0, "M-06": 5.0}
STEP_DAYS = 7

#: The machine whose step is large enough to fire the speed_drop rule.
STEP_MACHINE = "M-02"
STEP_PERCENT = SPEED_STEPS[STEP_MACHINE]
#: The machine whose step is deliberately too small to fire it.
SMALL_STEP_MACHINE = "M-06"

#: Single-hour temperature spikes: too short to be a sustained run.
OUTLIER_MACHINE = "M-03"
OUTLIER_HOURS_FROM_END = (80, 60, 40)
OUTLIER_RISE_C = 15.0

#: ``(machine, hours_from_end_of_run_start, length_in_hours)``. Both runs sit
#: inside the recent window the alert engine judges, and well after the
#: baseline window it measures normal from.
HEAT_RUNS = (("M-04", 30, 7), ("M-05", 60, 14))
HEAT_RUN_RISE_C = 15.0

VIBRATION_RUNS = (("M-06", 40, 4),)
VIBRATION_RUN_RISE = 2.0

#: Machines whose temperature is deliberately disturbed, so tests that expect
#: an exact mean know to skip them.
TEMPERATURE_ANOMALY_MACHINES = {"M-03", "M-04", "M-05"}

#: ``(machine, start, end)`` downtime events, 3 hours and 5 hours.
DOWNTIME = (
    ("M-03", datetime(2026, 9, 20, 2, 0, 0), datetime(2026, 9, 20, 5, 0, 0)),
    ("M-03", datetime(2026, 9, 25, 10, 0, 0), datetime(2026, 9, 25, 15, 0, 0)),
)
DOWNTIME_HOURS_TOTAL = 8.0

#: The single failure.
FAILURE = ("M-03", datetime(2026, 9, 28, 12, 0, 0), "bearing")

#: Every alert the engine should raise on this fixture, as ``(machine, rule)``.
EXPECTED_ALERTS = {
    ("M-02", "speed_drop"),
    ("M-03", "repeated_downtime"),
    ("M-04", "sustained_heat"),
    ("M-05", "sustained_heat"),
    ("M-06", "vibration_rising"),
}


@dataclass
class Facts:
    """Known answers for the fixture, for tests to assert against."""

    db_path: Path
    machine_ids: dict[str, int]
    first_ts: str
    last_ts: str
    readings_per_machine: int
    step_machine: str
    step_percent: float
    small_step_machine: str
    downtime_machine: str
    downtime_hours: float
    downtime_events: int
    failure_machine: str
    failure_ts: str
    failure_type: str
    outlier_machine: str
    outlier_timestamps: tuple[str, ...]
    base_values: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def date_range(self) -> tuple[str, str]:
        """The full range, as the query wrappers expect it."""
        return self.first_ts, self.last_ts

    def clean_temperature_machines(self) -> list[str]:
        """Machines whose temperature was left alone."""
        return [
            name for name in self.base_values if name not in TEMPERATURE_ANOMALY_MACHINES
        ]


def _wave(index: int, amplitude: float) -> float:
    """The 24-hour cycle: averages to zero over any whole number of days."""
    return amplitude * math.sin(
        2 * math.pi * index * INTERVAL_HOURS / WAVE_PERIOD_HOURS
    )


def _timestamps() -> list[datetime]:
    return [
        START + timedelta(hours=i * INTERVAL_HOURS)
        for i in range(READINGS_PER_MACHINE)
    ]


def expected_variance(reading_type: str) -> float:
    """Variance of the wave alone: ``amplitude ** 2 / 2``."""
    return AMPLITUDE[reading_type] ** 2 / 2


def _run_indexes(hours_from_end: int, length: int) -> set[int]:
    """Reading indexes covered by a planted run."""
    start = READINGS_PER_MACHINE - hours_from_end
    return set(range(start, start + length))


def build(db_path: Path) -> Facts:
    """Create the fixture database at ``db_path`` and return its known facts."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    connection = connect(db_path)
    try:
        run_script("schema.sql", connection)
        _insert_lines_and_machines(connection)
        connection.executemany(
            "INSERT INTO readings"
            " (id, machine_id, ts, temperature_c, speed_rpm, vibration_mm_s)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            _build_readings(),
        )
        _insert_downtime(connection)
        _insert_failure(connection)
        _insert_meta(connection)
        connection.commit()
    finally:
        connection.close()

    stamps = _timestamps()
    outlier_stamps = tuple(
        stamps[READINGS_PER_MACHINE - hours].strftime(TS_FORMAT)
        for hours in OUTLIER_HOURS_FROM_END
    )

    return Facts(
        db_path=db_path,
        machine_ids={name: spec[0] for name, spec in MACHINES.items()},
        first_ts=stamps[0].strftime(TS_FORMAT),
        last_ts=stamps[-1].strftime(TS_FORMAT),
        readings_per_machine=READINGS_PER_MACHINE,
        step_machine=STEP_MACHINE,
        step_percent=STEP_PERCENT,
        small_step_machine=SMALL_STEP_MACHINE,
        downtime_machine=DOWNTIME[0][0],
        downtime_hours=DOWNTIME_HOURS_TOTAL,
        downtime_events=len(DOWNTIME),
        failure_machine=FAILURE[0],
        failure_ts=FAILURE[1].strftime(TS_FORMAT),
        failure_type=FAILURE[2],
        outlier_machine=OUTLIER_MACHINE,
        outlier_timestamps=outlier_stamps,
        base_values={
            name: {
                "temperature_c": spec[2],
                "speed_rpm": spec[3],
                "vibration_mm_s": spec[4],
            }
            for name, spec in MACHINES.items()
        },
    )


def _insert_lines_and_machines(connection: sqlite3.Connection) -> None:
    lines = sorted({spec[1] for spec in MACHINES.values()})
    connection.executemany(
        "INSERT INTO lines (id, name) VALUES (?, ?)",
        [(line, "Line {}".format(line)) for line in lines],
    )
    connection.executemany(
        "INSERT INTO machines (id, name, line_id) VALUES (?, ?, ?)",
        [(spec[0], name, spec[1]) for name, spec in MACHINES.items()],
    )


def _build_readings() -> list[tuple]:
    """Every reading, with the steps, spikes and runs applied."""
    stamps = _timestamps()
    step_from = READINGS_PER_MACHINE - STEP_DAYS * 24 // INTERVAL_HOURS
    outlier_indexes = {
        READINGS_PER_MACHINE - hours for hours in OUTLIER_HOURS_FROM_END
    }
    heat_runs = {
        machine: _run_indexes(start, length) for machine, start, length in HEAT_RUNS
    }
    vibration_runs = {
        machine: _run_indexes(start, length)
        for machine, start, length in VIBRATION_RUNS
    }

    rows = []
    reading_id = 0
    for name, (machine_id, _line, base_t, base_s, base_v) in MACHINES.items():
        step_factor = 1.0 - SPEED_STEPS.get(name, 0.0) / 100.0
        heat = heat_runs.get(name, set())
        vibration_run = vibration_runs.get(name, set())

        for index, stamp in enumerate(stamps):
            reading_id += 1
            temperature = base_t + _wave(index, AMPLITUDE["temperature_c"])
            speed = base_s + _wave(index, AMPLITUDE["speed_rpm"])
            vibration = base_v + _wave(index, AMPLITUDE["vibration_mm_s"])

            if index >= step_from:
                speed *= step_factor
            if name == OUTLIER_MACHINE and index in outlier_indexes:
                temperature += OUTLIER_RISE_C
            if index in heat:
                temperature += HEAT_RUN_RISE_C
            if index in vibration_run:
                vibration += VIBRATION_RUN_RISE

            rows.append(
                (
                    reading_id,
                    machine_id,
                    stamp.strftime(TS_FORMAT),
                    round(temperature, 4),
                    round(speed, 4),
                    round(vibration, 4),
                )
            )
    return rows


def _insert_downtime(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO downtime_events (id, machine_id, start_ts, end_ts, reason)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (
                index + 1,
                MACHINES[machine][0],
                start.strftime(TS_FORMAT),
                end.strftime(TS_FORMAT),
                "fixture",
            )
            for index, (machine, start, end) in enumerate(DOWNTIME)
        ],
    )


def _insert_failure(connection: sqlite3.Connection) -> None:
    machine, stamp, kind = FAILURE
    connection.execute(
        "INSERT INTO failures (id, machine_id, ts, failure_type) VALUES (1, ?, ?, ?)",
        (MACHINES[machine][0], stamp.strftime(TS_FORMAT), kind),
    )


def _insert_meta(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO dataset_meta"
        " (id, name, seed, machines, days, start_date, problems, generated_at)"
        " VALUES (1, 'fixture', 0, ?, ?, ?, ?, ?)",
        (
            len(MACHINES),
            DAYS,
            START.date().isoformat(),
            json.dumps({"fixture": True}),
            START.strftime(TS_FORMAT),
        ),
    )


__all__ = [
    "AMPLITUDE",
    "DAYS",
    "DOWNTIME_HOURS_TOTAL",
    "EXPECTED_ALERTS",
    "HEAT_RUNS",
    "MACHINES",
    "OUTLIER_RISE_C",
    "READINGS_PER_MACHINE",
    "SMALL_STEP_MACHINE",
    "START",
    "STEP_DAYS",
    "STEP_PERCENT",
    "TEMPERATURE_ANOMALY_MACHINES",
    "Facts",
    "build",
    "expected_variance",
]
