"""Loader: build a fresh database from a generated dataset's CSVs.

Every row is checked before it is inserted. A row that fails is skipped and
counted under the reason it failed, so a bad dataset produces a summary rather
than a traceback. Each row is counted once, under the first check it fails.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from . import DATA_DIR, DB_PATH, load_config
from .db import connect, run_script

#: Tables are loaded in this order so a foreign key always points at a row
#: that is already there.
TABLE_ORDER = ("lines", "machines", "readings", "downtime_events", "failures")

COLUMNS = {
    "lines": ("id", "name"),
    "machines": ("id", "name", "line_id"),
    "readings": (
        "id",
        "machine_id",
        "ts",
        "temperature_c",
        "speed_rpm",
        "vibration_mm_s",
    ),
    "downtime_events": ("id", "machine_id", "start_ts", "end_ts", "reason"),
    "failures": ("id", "machine_id", "ts", "failure_type"),
}

MEASURES = ("temperature_c", "speed_rpm", "vibration_mm_s")


@dataclass
class LoadResult:
    """Row counts and rejections from one load."""

    dataset: str
    db_path: Path
    loaded: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_rejected(self) -> int:
        return sum(sum(reasons.values()) for reasons in self.rejected.values())

    def summary(self) -> str:
        lines = ["Loaded {} into {}".format(self.dataset, self.db_path)]
        for table in TABLE_ORDER:
            lines.append("  {:<16} {:>8,} rows".format(table, self.loaded.get(table, 0)))
        if self.total_rejected:
            lines.append("  {} rows skipped:".format(self.total_rejected))
            for table in TABLE_ORDER:
                for reason, count in sorted(self.rejected.get(table, {}).items()):
                    lines.append("    {:<16} {:<28} {:>6,}".format(table, reason, count))
        else:
            lines.append("  no rows skipped")
        return "\n".join(lines)


def _flag(frame: pd.DataFrame, mask: pd.Series, reason: str) -> None:
    """Mark rows matching ``mask`` with ``reason``, if not already rejected."""
    frame.loc[mask & frame["_reason"].isna(), "_reason"] = reason


def _read_csv(directory: Path, table: str) -> pd.DataFrame:
    """Read one table's CSV, keeping only the columns the schema has."""
    frame = pd.read_csv(directory / "{}.csv".format(table))
    for column in COLUMNS[table]:
        if column not in frame.columns:
            frame[column] = None
    return frame[list(COLUMNS[table])].copy()


def _validate(
    table: str,
    frame: pd.DataFrame,
    config: dict[str, Any],
    known_machines: set[int],
    known_lines: set[int],
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Return the rows worth inserting, plus a count of rejections by reason."""
    frame = frame.copy()
    frame["_reason"] = pd.Series([pd.NA] * len(frame), dtype="object")

    # Numeric columns are coerced first, so text in a number column becomes
    # NaN and is caught as a missing value rather than blowing up later.
    numeric = {
        "lines": ("id",),
        "machines": ("id", "line_id"),
        "readings": ("id", "machine_id") + MEASURES,
        "downtime_events": ("id", "machine_id"),
        "failures": ("id", "machine_id"),
    }[table]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    required = [c for c in COLUMNS[table] if c != "reason"]
    _flag(frame, frame[required].isna().any(axis=1), "missing value")

    if table == "machines":
        _flag(frame, ~frame["line_id"].isin(known_lines), "unknown line")

    if table in ("readings", "downtime_events", "failures"):
        _flag(frame, ~frame["machine_id"].isin(known_machines), "unknown machine")

    if table == "readings":
        for measure in MEASURES:
            low, high = config["validation"][measure]
            outside = (frame[measure] < low) | (frame[measure] > high)
            _flag(frame, outside, "{} out of range".format(measure.split("_")[0]))

    if table == "downtime_events":
        _flag(frame, frame["end_ts"] <= frame["start_ts"], "end_ts not after start_ts")

    if table == "failures":
        allowed = set(config["generation"]["planted"]["failure_types"])
        _flag(frame, ~frame["failure_type"].isin(allowed), "unknown failure_type")

    rejected = frame["_reason"].dropna().value_counts().to_dict()
    keep = frame[frame["_reason"].isna()].drop(columns="_reason")
    return keep, {str(k): int(v) for k, v in rejected.items()}


def _insert(connection: sqlite3.Connection, table: str, frame: pd.DataFrame) -> int:
    """Insert a validated frame with executemany."""
    if frame.empty:
        return 0
    columns = list(COLUMNS[table])
    placeholders = ", ".join("?" for _ in columns)
    rows = list(frame[columns].itertuples(index=False, name=None))
    connection.executemany(
        "INSERT INTO {} ({}) VALUES ({})".format(
            table, ", ".join(columns), placeholders
        ),
        rows,
    )
    return len(rows)


def _write_dataset_meta(connection: sqlite3.Connection, directory: Path) -> None:
    """Record the dataset's settings so every dashboard page can show them."""
    manifest = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO dataset_meta"
        " (id, name, seed, machines, days, start_date, problems, generated_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
        (
            manifest["name"],
            manifest["seed"],
            manifest["machines"],
            manifest["days"],
            manifest["start_date"],
            json.dumps(manifest["problems"], sort_keys=True),
            manifest["generated_at"],
        ),
    )


def load_dataset(
    dataset: str,
    *,
    data_root: Path | None = None,
    db_path: Path | None = None,
    config: dict[str, Any] | None = None,
) -> LoadResult:
    """Replace the database with a fresh one holding this dataset.

    The database file is deleted and rebuilt from ``schema.sql`` every time, so
    loading the same dataset twice gives the same row counts and cannot
    accumulate duplicates.
    """
    config = config or load_config()
    directory = (data_root or DATA_DIR) / dataset
    if not directory.is_dir():
        raise FileNotFoundError("No dataset at {}".format(directory))

    target = Path(db_path) if db_path is not None else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)

    result = LoadResult(dataset=dataset, db_path=target)
    connection = connect(target)
    try:
        run_script("schema.sql", connection)

        known_lines: set[int] = set()
        known_machines: set[int] = set()
        for table in TABLE_ORDER:
            frame = _read_csv(directory, table)
            keep, rejected = _validate(
                table, frame, config, known_machines, known_lines
            )
            result.loaded[table] = _insert(connection, table, keep)
            if rejected:
                result.rejected[table] = rejected
            if table == "lines":
                known_lines = set(keep["id"].astype(int))
            elif table == "machines":
                known_machines = set(keep["id"].astype(int))

        _write_dataset_meta(connection, directory)
        connection.commit()
    finally:
        connection.close()

    return result


__all__ = ["COLUMNS", "TABLE_ORDER", "LoadResult", "load_dataset"]
