"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pandas as pd
import pytest

from plantmonitor import db, load_config
from plantmonitor.generate import Result, generate
from tests import fixtures


@pytest.fixture(scope="session")
def config() -> dict:
    return load_config()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path to a database file inside this test's temporary directory."""
    return tmp_path / "test.db"


@pytest.fixture
def empty_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """An open connection to a fresh database with the schema applied.

    A file rather than ``:memory:`` so it behaves like the real database, and
    temporary so tests never touch ``data/plant.db``.
    """
    connection = db.connect(db_path)
    db.run_script("schema.sql", connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def default_dataset(tmp_path_factory: pytest.TempPathFactory) -> Result:
    """The default dataset, with every planted problem on.

    Session-scoped: it is the full ~104k-row dataset, so it is generated once
    and shared by the tests that need the planted problems present.
    """
    root = tmp_path_factory.mktemp("default")
    return generate(data_root=root)


@pytest.fixture(scope="session")
def dataset_without_overheat(tmp_path_factory: pytest.TempPathFactory) -> Result:
    """The default dataset with only the overheating problem turned off."""
    root = tmp_path_factory.mktemp("no_overheat")
    return generate(data_root=root, problems={"overheat": False})


@pytest.fixture(scope="session")
def fixture_facts(tmp_path_factory: pytest.TempPathFactory) -> fixtures.Facts:
    """Build the fixture database once and hand back its known answers."""
    path = tmp_path_factory.mktemp("fixture") / "fixture.db"
    return fixtures.build(path)


@pytest.fixture
def fixture_db(fixture_facts: fixtures.Facts) -> Iterator[sqlite3.Connection]:
    """An open connection to the fixture database.

    The readings are built once per session because they are slow, but
    ``alerts`` is emptied for each test: it is the one table tests write to, so
    leaving it populated would make them depend on the order they ran in.
    """
    connection = db.connect(fixture_facts.db_path)
    connection.execute("DELETE FROM alerts")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def full_db_path(
    tmp_path_factory: pytest.TempPathFactory, default_dataset: Result
) -> Path:
    """The default dataset loaded into a database once, for timing tests."""
    from plantmonitor.load import load_dataset

    path = tmp_path_factory.mktemp("full") / "plant.db"
    load_dataset(
        default_dataset.settings.name,
        data_root=default_dataset.directory.parent,
        db_path=path,
    )
    return path


@pytest.fixture(scope="session")
def machine_readings():
    """Return ``f(result, name)`` giving one machine's readings by name.

    A fixture returning a callable rather than a module-level helper, so test
    modules never import conftest directly: pytest would then load it twice
    under two module names.
    """

    def load(result: Result, name: str) -> pd.DataFrame:
        machines = pd.read_csv(result.directory / "machines.csv")
        match = machines.loc[machines["name"] == name, "id"]
        assert not match.empty, "{} is not in this dataset".format(name)

        readings = pd.read_csv(result.directory / "readings.csv")
        readings["ts"] = pd.to_datetime(readings["ts"])
        return (
            readings[readings["machine_id"] == match.iloc[0]]
            .sort_values("ts")
            .reset_index(drop=True)
        )

    return load
