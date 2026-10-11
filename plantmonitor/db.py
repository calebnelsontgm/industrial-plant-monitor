"""Database access: the only place that opens connections or reads .sql files.

Three helpers, per the build brief:

- ``connect()``    open the database with foreign keys enforced
- ``run_script()`` run a multi-statement file such as schema.sql
- ``query()``      run a single-statement file and return a DataFrame
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from . import DATA_DIR, DB_PATH, SQL_DIR


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with ``PRAGMA foreign_keys = ON``.

    Defaults to the one active database, ``data/plant.db``, creating ``data/``
    if it does not exist. ``path`` exists so tests can use a temporary file
    instead of the real database; pass ``":memory:"`` for an in-memory one.
    """
    if path is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        target: Path | str = DB_PATH
    else:
        target = path
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(target)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def read_sql_file(name: str) -> str:
    """Return the text of ``sql/<name>``, adding the .sql suffix if omitted."""
    filename = name if name.endswith(".sql") else f"{name}.sql"
    sql_path = SQL_DIR / filename
    if not sql_path.is_file():
        raise FileNotFoundError(f"No SQL file at {sql_path}")
    return sql_path.read_text(encoding="utf-8")


def run_script(name: str, connection: sqlite3.Connection | None = None) -> None:
    """Run every statement in ``sql/<name>``, e.g. ``schema.sql``.

    Uses ``executescript``, which ``query()`` cannot do because it handles one
    statement and returns rows. Opens and closes its own connection when none
    is given.
    """
    script = read_sql_file(name)
    owns_connection = connection is None
    connection = connection or connect()
    try:
        connection.executescript(script)
        connection.commit()
    finally:
        if owns_connection:
            connection.close()


def query(
    name: str,
    params: Sequence[Any] = (),
    connection: sqlite3.Connection | None = None,
) -> pd.DataFrame:
    """Run ``sql/<name>`` with ``?`` parameters and return a DataFrame.

    Parameters are always bound, never formatted into the SQL text. Opens and
    closes its own connection when none is given.
    """
    sql = read_sql_file(name)
    owns_connection = connection is None
    connection = connection or connect()
    try:
        return pd.read_sql_query(sql, connection, params=tuple(params))
    finally:
        if owns_connection:
            connection.close()


__all__ = ["connect", "read_sql_file", "run_script", "query"]
