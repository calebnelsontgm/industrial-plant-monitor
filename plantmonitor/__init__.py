"""Plant Monitor: sensor data generation, loading, analysis and alerting.

Holds the project paths and the single config reader. Config lives here rather
than in a new module because the build brief fixes the file layout.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Repository root, i.e. the directory holding config.toml.
ROOT = Path(__file__).resolve().parent.parent

#: Directory holding the .sql files.
SQL_DIR = ROOT / "sql"

#: Directory holding generated datasets and plant.db. Git-ignored.
DATA_DIR = ROOT / "data"

#: The one active database.
DB_PATH = DATA_DIR / "plant.db"

CONFIG_PATH = ROOT / "config.toml"


@lru_cache(maxsize=None)
def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Read and return config.toml.

    Cached, since the file is read on nearly every call path and never changes
    during a run. Pass an explicit path in tests to read a different file.
    """
    config_path = Path(path) if path is not None else CONFIG_PATH
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


__all__ = [
    "ROOT",
    "SQL_DIR",
    "DATA_DIR",
    "DB_PATH",
    "CONFIG_PATH",
    "load_config",
]
