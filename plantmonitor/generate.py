"""Dataset generation: synthetic sensor data with optional planted problems.

Writes one CSV per table plus ``dataset.json`` into ``data/<dataset>/``. The
same inputs and seed always produce byte-identical CSVs.

Each concern draws from its own random stream, derived from the seed by a fixed
offset. Toggling one planted problem therefore cannot shift the numbers of any
other, so turning a problem off leaves the baseline data untouched and an
on/off comparison of the same machine is meaningful.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import DATA_DIR, load_config

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: The four planted problems.
PROBLEM_NAMES = ("overheat", "slowdown", "downtime", "prefailure")

# Fixed offsets added to the seed, one per concern. See the module docstring.
_STREAM_PROFILES = 0
_STREAM_NOISE = 1
_STREAM_DOWNTIME = 2
_STREAM_PREFAILURE = 3

# Decimal places written to CSV. Rounding keeps output byte-identical across
# runs and platforms instead of depending on full float repr.
_ROUNDING = {"temperature_c": 2, "speed_rpm": 1, "vibration_mm_s": 3}


class SettingsError(ValueError):
    """Generator inputs fell outside the limits in config.toml.

    A named class so the dashboard can catch bad input and show an error
    without generating anything, rather than catching every ValueError.
    """


@dataclass(frozen=True)
class Settings:
    """Fully resolved generator inputs: no defaults left to apply."""

    name: str
    seed: int
    machines: int
    days: int
    start_date: str
    interval_minutes: int
    lines: int
    problems: dict[str, bool]

    @property
    def readings_per_machine(self) -> int:
        return self.days * (24 * 60 // self.interval_minutes)

    @property
    def total_readings(self) -> int:
        return self.machines * self.readings_per_machine


@dataclass
class Result:
    """What a generation run produced."""

    settings: Settings
    directory: Path
    row_counts: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def resolve_settings(
    *,
    name: str | None = None,
    seed: int | None = None,
    machines: int | None = None,
    days: int | None = None,
    start_date: str | None = None,
    problems: dict[str, bool] | None = None,
    config: dict[str, Any] | None = None,
) -> Settings:
    """Apply config defaults, then validate against the config limits.

    Raises :class:`SettingsError` if the machine count or day count is out of
    range, or the start date is not an ISO date.
    """
    config = config or load_config()
    generation = config["generation"]

    seed = generation["seed"] if seed is None else int(seed)
    machines = generation["machines"] if machines is None else int(machines)
    days = generation["days"] if days is None else int(days)
    start_date = generation["start_date"] if start_date is None else start_date

    low, high = generation["min_machines"], generation["max_machines"]
    if not low <= machines <= high:
        raise SettingsError(
            f"machines must be between {low} and {high}, got {machines}"
        )

    low, high = generation["min_days"], generation["max_days"]
    if not low <= days <= high:
        raise SettingsError(f"days must be between {low} and {high}, got {days}")

    try:
        datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError as exc:
        raise SettingsError(
            f"start_date must be YYYY-MM-DD, got {start_date!r}"
        ) from exc

    resolved_problems = dict(generation["problems"])
    for key, value in (problems or {}).items():
        if key not in PROBLEM_NAMES:
            raise SettingsError(f"unknown problem {key!r}")
        resolved_problems[key] = bool(value)

    return Settings(
        name=name or f"seed{seed}_{machines}m_{days}d",
        seed=seed,
        machines=machines,
        days=days,
        start_date=start_date,
        interval_minutes=generation["interval_minutes"],
        lines=generation["lines"],
        problems=resolved_problems,
    )


def _timestamps(settings: Settings) -> list[str]:
    """Every reading timestamp, shared by all machines."""
    start = datetime.strptime(settings.start_date, "%Y-%m-%d")
    step = timedelta(minutes=settings.interval_minutes)
    return [
        (start + i * step).strftime(TS_FORMAT)
        for i in range(settings.readings_per_machine)
    ]


def _machine_rows(settings: Settings) -> list[tuple[int, str, int]]:
    """``(id, name, line_id)`` per machine, alternating across lines.

    Alternating rather than splitting in half keeps the lines within one
    machine of each other at any machine count.
    """
    return [
        (i + 1, "M-{:02d}".format(i + 1), (i % settings.lines) + 1)
        for i in range(settings.machines)
    ]


def _machine_id_by_name(settings: Settings, name: str) -> int | None:
    for machine_id, machine_name, _line in _machine_rows(settings):
        if machine_name == name:
            return machine_id
    return None


def _readings_per_hour(settings: Settings) -> int:
    return 60 // settings.interval_minutes


def _readings_per_day(settings: Settings) -> int:
    return 24 * 60 // settings.interval_minutes


def _build_readings(
    settings: Settings, config: dict[str, Any], timestamps: Sequence[str]
) -> dict[int, list[list[Any]]]:
    """Normal readings per machine: a per-machine typical value plus noise.

    Each row is ``[machine_id, ts, temperature, speed, vibration]``; the row id
    is assigned later, once the planted problems have been applied.
    """
    normal = config["generation"]["normal"]
    profiles = random.Random(settings.seed + _STREAM_PROFILES)
    noise = random.Random(settings.seed + _STREAM_NOISE)

    rows: dict[int, list[list[Any]]] = {}
    for machine_id, _name, _line in _machine_rows(settings):
        base_t = profiles.uniform(*normal["temperature_c"])
        base_s = profiles.uniform(*normal["speed_rpm"])
        base_v = profiles.uniform(*normal["vibration_mm_s"])
        rows[machine_id] = [
            [
                machine_id,
                ts,
                base_t + noise.gauss(0.0, normal["temperature_noise_sd"]),
                base_s + noise.gauss(0.0, normal["speed_noise_sd"]),
                base_v + noise.gauss(0.0, normal["vibration_noise_sd"]),
            ]
            for ts in timestamps
        ]
    return rows


def _plant_overheat(
    settings: Settings,
    config: dict[str, Any],
    rows: dict[int, list[list[Any]]],
    result: Result,
) -> None:
    """Ramp one machine's temperature up steadily over the final days."""
    planted = config["generation"]["planted"]
    target = planted["overheat_machine"]
    machine_id = _machine_id_by_name(settings, target)
    if machine_id is None:
        result.skipped.append(
            "overheat: no {} at {} machines".format(target, settings.machines)
        )
        return

    span = min(
        planted["overheat_days"] * _readings_per_day(settings), len(rows[machine_id])
    )
    if span < 2:
        result.skipped.append("overheat: dataset too short for the climb")
        return

    rise = planted["overheat_rise_c"]
    for index, row in enumerate(rows[machine_id][-span:]):
        row[2] += rise * index / (span - 1)


def _plant_slowdown(
    settings: Settings,
    config: dict[str, Any],
    rows: dict[int, list[list[Any]]],
    result: Result,
) -> None:
    """Step one machine's speed down for exactly the final 7-day block.

    Applied by row count rather than by comparing timestamps, so the final
    block and the one before it differ by exactly the configured percentage.
    """
    planted = config["generation"]["planted"]
    target = planted["slowdown_machine"]
    machine_id = _machine_id_by_name(settings, target)
    if machine_id is None:
        result.skipped.append(
            "slowdown: no {} at {} machines".format(target, settings.machines)
        )
        return

    span = planted["slowdown_days"] * _readings_per_day(settings)
    machine_rows = rows[machine_id]
    if len(machine_rows) < 2 * span:
        result.skipped.append(
            "slowdown: needs {} days, have {}".format(
                2 * planted["slowdown_days"], settings.days
            )
        )
        return

    factor = 1.0 - planted["slowdown_percent"] / 100.0
    for row in machine_rows[-span:]:
        row[3] *= factor


def _build_downtime(
    settings: Settings, config: dict[str, Any], result: Result
) -> list[tuple[int, int, str, str, str]]:
    """Repeated downtime events on one machine, inside the recent window.

    The window is cut into one segment per event and one event is placed at a
    random offset inside each, which keeps them non-overlapping by
    construction rather than by rejection sampling.
    """
    planted = config["generation"]["planted"]
    target = planted["downtime_machine"]
    machine_id = _machine_id_by_name(settings, target)
    if machine_id is None:
        result.skipped.append(
            "downtime: no {} at {} machines".format(target, settings.machines)
        )
        return []

    events = planted["downtime_events"]
    window_days = min(planted["downtime_window_days"], settings.days)
    segment_minutes = window_days * 24 * 60 // events
    longest = planted["downtime_hours"][1]
    if segment_minutes <= longest * 60:
        result.skipped.append("downtime: window too short for the configured events")
        return []

    rng = random.Random(settings.seed + _STREAM_DOWNTIME)
    start = datetime.strptime(settings.start_date, "%Y-%m-%d")
    total_minutes = settings.readings_per_machine * settings.interval_minutes
    window_start = total_minutes - window_days * 24 * 60

    rows = []
    for index in range(events):
        hours = rng.uniform(*planted["downtime_hours"])
        latest = segment_minutes - int(hours * 60) - 1
        offset = window_start + index * segment_minutes + rng.randint(0, latest)
        begins = start + timedelta(minutes=offset)
        rows.append(
            (
                index + 1,
                machine_id,
                begins.strftime(TS_FORMAT),
                (begins + timedelta(hours=hours)).strftime(TS_FORMAT),
                rng.choice(planted["downtime_reasons"]),
            )
        )
    return rows


def _build_failures(
    settings: Settings, config: dict[str, Any], rows: dict[int, list[list[Any]]]
) -> list[tuple[int, int, str, str]]:
    """Random failures, each preceded by rising vibration on that machine."""
    planted = config["generation"]["planted"]
    rng = random.Random(settings.seed + _STREAM_PREFAILURE)
    per_hour = _readings_per_hour(settings)
    lead_low, lead_high = planted["prefailure_lead_hours"]
    machine_ids = sorted(rows)
    count = rng.randint(planted["prefailure_min"], planted["prefailure_max"])
    rise = planted["prefailure_rise_mm_s"]

    failures: list[tuple[int, str, str]] = []
    for _ in range(count):
        machine_id = rng.choice(machine_ids)
        machine_rows = rows[machine_id]
        lead = rng.randint(lead_low, lead_high) * per_hour
        if len(machine_rows) <= lead + 1:
            continue
        index = rng.randrange(lead, len(machine_rows))

        for step in range(lead + 1):
            machine_rows[index - lead + step][4] += rise * step / lead

        failures.append(
            (machine_id, machine_rows[index][1], rng.choice(planted["failure_types"]))
        )

    failures.sort(key=lambda row: (row[1], row[0]))
    return [(i + 1, machine_id, ts, kind) for i, (machine_id, ts, kind) in enumerate(failures)]


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """Write rows with LF endings so output is identical on every platform."""
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def generate(
    *,
    name: str | None = None,
    seed: int | None = None,
    machines: int | None = None,
    days: int | None = None,
    start_date: str | None = None,
    problems: dict[str, bool] | None = None,
    data_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> Result:
    """Generate a dataset and write it to ``<data_root>/<name>/``.

    ``data_root`` defaults to ``data/`` and exists so tests can write to a
    temporary directory instead of the real one. The dashboard's Data page and
    the CLI both call this same function.
    """
    config = config or load_config()
    settings = resolve_settings(
        name=name,
        seed=seed,
        machines=machines,
        days=days,
        start_date=start_date,
        problems=problems,
        config=config,
    )

    directory = (data_root or DATA_DIR) / settings.name
    directory.mkdir(parents=True, exist_ok=True)
    result = Result(settings=settings, directory=directory)

    reading_rows = _build_readings(settings, config, _timestamps(settings))

    if settings.problems.get("overheat"):
        _plant_overheat(settings, config, reading_rows, result)
    if settings.problems.get("slowdown"):
        _plant_slowdown(settings, config, reading_rows, result)

    failures: list[tuple[int, int, str, str]] = []
    if settings.problems.get("prefailure"):
        failures = _build_failures(settings, config, reading_rows)

    downtime: list[tuple[int, int, str, str, str]] = []
    if settings.problems.get("downtime"):
        downtime = _build_downtime(settings, config, result)

    flat_readings = []
    reading_id = 0
    for machine_id in sorted(reading_rows):
        for row in reading_rows[machine_id]:
            reading_id += 1
            flat_readings.append(
                [
                    reading_id,
                    row[0],
                    row[1],
                    round(row[2], _ROUNDING["temperature_c"]),
                    round(row[3], _ROUNDING["speed_rpm"]),
                    round(row[4], _ROUNDING["vibration_mm_s"]),
                ]
            )

    result.row_counts["lines"] = _write_csv(
        directory / "lines.csv",
        ("id", "name"),
        [(i + 1, "Line {}".format(i + 1)) for i in range(settings.lines)],
    )
    result.row_counts["machines"] = _write_csv(
        directory / "machines.csv",
        ("id", "name", "line_id"),
        _machine_rows(settings),
    )
    result.row_counts["readings"] = _write_csv(
        directory / "readings.csv",
        ("id", "machine_id", "ts", "temperature_c", "speed_rpm", "vibration_mm_s"),
        flat_readings,
    )
    result.row_counts["downtime_events"] = _write_csv(
        directory / "downtime_events.csv",
        ("id", "machine_id", "start_ts", "end_ts", "reason"),
        downtime,
    )
    result.row_counts["failures"] = _write_csv(
        directory / "failures.csv",
        ("id", "machine_id", "ts", "failure_type"),
        failures,
    )

    manifest = {
        "name": settings.name,
        "seed": settings.seed,
        "machines": settings.machines,
        "days": settings.days,
        "start_date": settings.start_date,
        "problems": settings.problems,
        "generated_at": datetime.now(timezone.utc).strftime(TS_FORMAT),
        "skipped": result.skipped,
    }
    (directory / "dataset.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    return result


__all__ = [
    "PROBLEM_NAMES",
    "Result",
    "Settings",
    "SettingsError",
    "generate",
    "resolve_settings",
]
