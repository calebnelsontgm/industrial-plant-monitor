"""Command line: ``python -m plantmonitor <command>``.

Commands: ``generate``, ``load``, ``alerts``, ``dashboard``, and ``sql`` for
the scratchpad.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

import pandas as pd

from . import DB_PATH, ROOT, load_config
from .db import connect
from .alerts import run_alerts
from .generate import PROBLEM_NAMES, SettingsError, generate
from .load import load_dataset

#: Scratchpad file, kept out of sql/ so the seven project queries stay a clean
#: set. Not part of the app: it exists to try SQL against the database.
SCRATCH_PATH = ROOT / "scratch.sql"

#: Rows printed before output is truncated, so SELECT * cannot flood the
#: terminal with 100,000 lines.
SCRATCH_ROW_LIMIT = 50


def _build_parser() -> argparse.ArgumentParser:
    generation = load_config()["generation"]

    parser = argparse.ArgumentParser(
        prog="python -m plantmonitor",
        description="Plant Monitor: generate, load and analyse sensor data.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    gen = commands.add_parser(
        "generate",
        help="Write a dataset of CSVs to data/<name>/",
        description=(
            "Anything not given falls back to the default in config.toml "
            "(seed {seed}, {machines} machines, {days} days)."
        ).format(**generation),
    )
    gen.add_argument("--name", help="dataset folder name")
    gen.add_argument("--seed", type=int, help="random seed")
    gen.add_argument("--machines", type=int, help="number of machines")
    gen.add_argument("--days", type=int, help="days of readings")
    gen.add_argument("--start", dest="start_date", help="first reading date, YYYY-MM-DD")
    for problem in PROBLEM_NAMES:
        gen.add_argument(
            "--no-{}".format(problem),
            dest="no_{}".format(problem),
            action="store_true",
            help="turn off the {} planted problem".format(problem),
        )

    loader = commands.add_parser(
        "load",
        help="Build data/plant.db from a dataset's CSVs",
        description="Replaces data/plant.db with a fresh database holding this dataset.",
    )
    loader.add_argument("dataset", help="dataset folder name under data/")

    alerts = commands.add_parser(
        "alerts",
        help="Run the alert rules against data/plant.db",
        description=(
            "Evaluates the four rules and writes any new alerts. Running it "
            "twice creates nothing the second time."
        ),
    )
    alerts.add_argument(
        "--as-of", dest="as_of", help="judge as at this timestamp (default: latest reading)"
    )

    commands.add_parser(
        "dashboard",
        help="Launch the Streamlit dashboard",
        description="Starts Streamlit on app.py. Stop it with Ctrl+C.",
    )

    scratch = commands.add_parser(
        "sql",
        help="Run scratch.sql against data/plant.db and print the results",
        description=(
            "Runs each statement in scratch.sql and prints any rows returned. "
            "Edit the file, run this again. Use --file to run a different one."
        ),
    )
    scratch.add_argument("--file", help="run this .sql file instead of scratch.sql")
    scratch.add_argument(
        "--all", action="store_true", help="print every row, not just the first 50"
    )

    return parser


def _run_generate(args: argparse.Namespace) -> int:
    problems = {
        problem: False
        for problem in PROBLEM_NAMES
        if getattr(args, "no_{}".format(problem))
    }

    try:
        result = generate(
            name=args.name,
            seed=args.seed,
            machines=args.machines,
            days=args.days,
            start_date=args.start_date,
            problems=problems or None,
        )
    except SettingsError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2

    print("Wrote dataset {} to {}".format(result.settings.name, result.directory))
    for table, count in result.row_counts.items():
        print("  {:<16} {:>8,} rows".format(table, count))
    for note in result.skipped:
        print("  skipped {}".format(note))
    return 0


def _run_load(args: argparse.Namespace) -> int:
    try:
        result = load_dataset(args.dataset)
    except FileNotFoundError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2

    print(result.summary())
    return 0


def _run_alerts(args: argparse.Namespace) -> int:
    if not DB_PATH.is_file():
        print(
            "error: no database at {}. Run 'load <dataset>' first.".format(DB_PATH),
            file=sys.stderr,
        )
        return 2

    try:
        result = run_alerts(as_of=args.as_of)
    except ValueError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2

    print(result.summary())
    return 0


def _skip_streamlit_email_prompt() -> None:
    """Opt out of Streamlit's one-time "enter your email" prompt.

    On a first run Streamlit asks for an email on stdin. With no terminal
    attached it reads end-of-file and quits before the server starts, so
    writing the same empty answer it would have saved keeps the launch
    non-interactive. Only written if the file does not already exist.
    """
    credentials = Path.home() / ".streamlit" / "credentials.toml"
    if credentials.exists():
        return
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text('[general]\nemail = ""\n', encoding="utf-8")


def _run_dashboard(_args: argparse.Namespace) -> int:
    """Hand over to Streamlit's own CLI, which owns the server loop."""
    from streamlit.web import cli as streamlit_cli

    app_path = ROOT / "app.py"
    if not app_path.is_file():
        print("error: no app.py at {}".format(app_path), file=sys.stderr)
        return 2

    _skip_streamlit_email_prompt()
    sys.argv = ["streamlit", "run", str(app_path)]
    return int(streamlit_cli.main(standalone_mode=False) or 0)


def _statements(sql: str) -> list[str]:
    """Split a file into statements, dropping blanks and comment-only chunks."""
    chunks = []
    for raw in sql.split(";"):
        stripped = raw.strip()
        if not stripped:
            continue
        lines = [
            line
            for line in stripped.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        if lines:
            chunks.append(stripped)
    return chunks


def _run_sql(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else SCRATCH_PATH
    if not path.is_file():
        print("error: no SQL file at {}".format(path), file=sys.stderr)
        return 2
    if not DB_PATH.is_file():
        print(
            "error: no database at {}. Run 'load <dataset>' first.".format(DB_PATH),
            file=sys.stderr,
        )
        return 2

    statements = _statements(path.read_text(encoding="utf-8"))
    if not statements:
        print("{} has no statements yet.".format(path.name))
        return 0

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)

    connection = connect()
    status = 0
    try:
        for index, statement in enumerate(statements, start=1):
            if len(statements) > 1:
                print("--- statement {} of {} ---".format(index, len(statements)))
            try:
                frame = pd.read_sql_query(statement, connection)
            except (pd.errors.DatabaseError, sqlite3.Error) as exc:
                print("SQL error: {}".format(exc), file=sys.stderr)
                status = 1
                continue

            if frame.empty:
                print("(no rows)")
            elif args.all or len(frame) <= SCRATCH_ROW_LIMIT:
                print(frame.to_string(index=False))
            else:
                print(frame.head(SCRATCH_ROW_LIMIT).to_string(index=False))
                print(
                    "... {:,} rows total, showing the first {}. "
                    "Add --all to see them all.".format(len(frame), SCRATCH_ROW_LIMIT)
                )
            print()
    finally:
        connection.close()
    return status


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "generate": _run_generate,
        "load": _run_load,
        "alerts": _run_alerts,
        "dashboard": _run_dashboard,
        "sql": _run_sql,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
