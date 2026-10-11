"""Loader tests: row counts, bad rows, and safe re-runs."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from plantmonitor.generate import generate
from plantmonitor.load import TABLE_ORDER, load_dataset


@pytest.fixture
def small_dataset(tmp_path: Path):
    """A 3-machine, 5-day dataset with no planted problems."""
    return generate(
        data_root=tmp_path / "datasets",
        machines=3,
        days=5,
        problems={
            "overheat": False,
            "slowdown": False,
            "downtime": False,
            "prefailure": False,
        },
    )


def csv_rows(directory: Path, table: str) -> int:
    return len(pd.read_csv(directory / "{}.csv".format(table)))


def db_rows(connection, table: str) -> int:
    return connection.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]


def test_row_counts_match_csvs(small_dataset, tmp_path: Path) -> None:
    """Database rows equal CSV rows minus rejected rows."""
    result = load_dataset(
        small_dataset.settings.name,
        data_root=small_dataset.directory.parent,
        db_path=tmp_path / "plant.db",
    )

    from plantmonitor.db import connect

    connection = connect(result.db_path)
    try:
        for table in TABLE_ORDER:
            expected = csv_rows(small_dataset.directory, table)
            rejected = sum(result.rejected.get(table, {}).values())
            assert db_rows(connection, table) == expected - rejected, table
    finally:
        connection.close()

    assert result.total_rejected == 0


def test_bad_rows_skipped_and_counted(small_dataset, tmp_path: Path) -> None:
    """A missing temperature and a 400 C reading are skipped and counted."""
    readings_path = small_dataset.directory / "readings.csv"
    frame = pd.read_csv(readings_path)
    clean_count = len(frame)

    frame.loc[0, "temperature_c"] = None      # missing value
    frame.loc[1, "temperature_c"] = 400.0     # out of range
    frame.to_csv(readings_path, index=False)

    result = load_dataset(
        small_dataset.settings.name,
        data_root=small_dataset.directory.parent,
        db_path=tmp_path / "plant.db",
    )

    reasons = result.rejected["readings"]
    assert reasons["missing value"] == 1
    assert reasons["temperature out of range"] == 1
    assert result.loaded["readings"] == clean_count - 2


def test_unknown_machine_rejected(small_dataset, tmp_path: Path) -> None:
    """A reading for a machine that does not exist is skipped, not an error."""
    readings_path = small_dataset.directory / "readings.csv"
    frame = pd.read_csv(readings_path)
    frame.loc[0, "machine_id"] = 999
    frame.to_csv(readings_path, index=False)

    result = load_dataset(
        small_dataset.settings.name,
        data_root=small_dataset.directory.parent,
        db_path=tmp_path / "plant.db",
    )

    assert result.rejected["readings"]["unknown machine"] == 1


def test_reload_is_safe(small_dataset, tmp_path: Path) -> None:
    """Loading twice leaves identical row counts."""
    db_path = tmp_path / "plant.db"
    kwargs = dict(
        data_root=small_dataset.directory.parent, db_path=db_path
    )

    first = load_dataset(small_dataset.settings.name, **kwargs)
    second = load_dataset(small_dataset.settings.name, **kwargs)

    assert first.loaded == second.loaded

    from plantmonitor.db import connect

    connection = connect(db_path)
    try:
        assert db_rows(connection, "dataset_meta") == 1
        for table in TABLE_ORDER:
            assert db_rows(connection, table) == first.loaded[table]
    finally:
        connection.close()


def test_dataset_meta_recorded(small_dataset, tmp_path: Path) -> None:
    """The loaded dataset's settings land in dataset_meta."""
    result = load_dataset(
        small_dataset.settings.name,
        data_root=small_dataset.directory.parent,
        db_path=tmp_path / "plant.db",
    )

    from plantmonitor.db import connect

    connection = connect(result.db_path)
    try:
        row = connection.execute(
            "SELECT name, seed, machines, days, problems FROM dataset_meta"
        ).fetchone()
    finally:
        connection.close()

    assert row[0] == small_dataset.settings.name
    assert row[1] == small_dataset.settings.seed
    assert row[2] == 3
    assert row[3] == 5
    assert json.loads(row[4])["overheat"] is False


def test_missing_dataset_raises(tmp_path: Path) -> None:
    """Naming a dataset that is not there is an error, not a crash mid-load."""
    with pytest.raises(FileNotFoundError):
        load_dataset("nope", data_root=tmp_path, db_path=tmp_path / "plant.db")


def test_foreign_keys_hold_after_load(small_dataset, tmp_path: Path) -> None:
    """Every loaded reading points at a machine that exists."""
    result = load_dataset(
        small_dataset.settings.name,
        data_root=small_dataset.directory.parent,
        db_path=tmp_path / "plant.db",
    )

    from plantmonitor.db import connect

    connection = connect(result.db_path)
    try:
        orphans = connection.execute(
            "SELECT COUNT(*) FROM readings r"
            " LEFT JOIN machines m ON m.id = r.machine_id"
            " WHERE m.id IS NULL"
        ).fetchone()[0]
    finally:
        connection.close()

    assert orphans == 0


def test_full_dataset_loads_quickly(default_dataset, tmp_path: Path) -> None:
    """The default dataset (~104,000 readings) loads in under 10 seconds."""
    import time

    started = time.perf_counter()
    result = load_dataset(
        default_dataset.settings.name,
        data_root=default_dataset.directory.parent,
        db_path=tmp_path / "plant.db",
    )
    elapsed = time.perf_counter() - started

    assert result.loaded["readings"] > 100_000
    assert elapsed < 10.0, "load took {:.2f}s".format(elapsed)
