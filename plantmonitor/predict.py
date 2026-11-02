"""Failure prediction: will this machine fail within the next 24 hours?

The pipeline, in order:

1. ``hourly_features``  15-minute readings -> one row per machine per hour,
   with the mean, max and slope of each reading over trailing 6- and 12-hour
   windows, each measured against that machine's own baseline. Raw values
   would also tell the model which machine it is looking at, since every
   machine has its own typical temperature and speed.
2. ``label_hours``      1 if that machine fails within the next 24 hours.
3. ``time_split``       train on the earlier hours, test on the later ones,
   with a gap between them so no training label can see a test failure.
4. ``train_model``      fit a random forest, score it on the test hours, and
   save the model, its metrics and its test-hour scores next to the database.
5. ``current_risk``     the saved model's failure probability for every
   machine's most recent hour.

``train_on_training_set`` runs steps 1-4 on the model's own dataset, set in
``[predict.training]``: longer than the default and with more failures, so the
test period holds enough of them to measure. It is generated and loaded into a
separate database, so whatever the dashboard has loaded is left alone.

The raw rows come from ``sql/prediction_readings.sql`` and
``sql/prediction_failures.sql``; all the feature math is pandas.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import precision_score, recall_score

from . import DATA_DIR, load_config
from .db import connect
from .generate import generate, resolve_settings
from .load import load_dataset
from .queries import dataset_settings, prediction_failures, prediction_readings

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

MEASURES = ("temperature_c", "speed_rpm", "vibration_mm_s")

MODEL_FILE = "model.joblib"
METRICS_FILE = "model_metrics.json"
TEST_RISK_FILE = "model_test_risk.csv"


@dataclass
class TrainResult:
    """What one training run produced. ``trained`` is False when it was skipped."""

    trained: bool
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if not self.trained:
            return "Failure model not trained: {}".format(self.reason)
        m = self.metrics
        lines = [
            "Failure model trained on {}".format(m["dataset"]),
            "  {:,} training hours, {:,} test hours (from {})".format(
                m["train_hours"], m["test_hours"], m["test_start"]
            ),
            "  precision {:.2f}, recall {:.2f} (hourly, threshold {})".format(
                m["precision"], m["recall"], m["threshold"]
            ),
            "  false alarms: {} of {} flagged hours".format(
                m["false_alarms"], m["flagged_hours"]
            ),
            "  test failures flagged in advance: {} of {}".format(
                m["caught"], m["test_failures"]
            ),
        ]
        for f in m["failures"]:
            early = (
                "{:.1f} h early".format(f["hours_early"]) if f["caught"] else "missed"
            )
            lines.append("    {}  {}  {}".format(f["machine_name"], f["ts"], early))
        return "\n".join(lines)


# --- features ---------------------------------------------------------------


def _slope(values: np.ndarray) -> float:
    """Least-squares slope of evenly spaced values, in units per hour.

    The x values are centred (e.g. -2.5 .. 2.5 for six hours), which makes the
    textbook formula collapse to sum(x * y) / sum(x * x).
    """
    x = np.arange(len(values)) - (len(values) - 1) / 2
    return float(np.dot(x, values) / np.dot(x, x))


def feature_columns(windows: list[int]) -> list[str]:
    """Names of the model's input columns, e.g. ``vibration_mm_s_slope_dev_6h``.

    Every column is in standard deviations of that machine's baseline:
    ``mean_dev`` and ``max_dev`` are how far the window's mean and peak sit
    from the baseline mean (3.0 is the upper band), ``slope_dev`` is how fast
    the reading is moving, per hour.
    """
    return [
        "{}_{}_{}h".format(measure, stat, hours)
        for hours in windows
        for measure in MEASURES
        for stat in ("mean_dev", "max_dev", "slope_dev")
    ]


def history_hours(config: dict[str, Any] | None = None) -> int:
    """Hours of readings a feature row needs behind it: the lagged baseline."""
    config = config or load_config()
    baseline = config["band"]["baseline_days"] * 24 + config["predict"]["baseline_lag_hours"]
    return max(baseline, max(config["predict"]["feature_windows_hours"]))


def hourly_features(
    readings: pd.DataFrame, config: dict[str, Any] | None = None
) -> pd.DataFrame:
    """One row per machine per hour, with trailing-window statistics.

    A row stamped ``hour`` describes the readings up to the end of that hour,
    so ``14:00`` uses everything through ``14:45``.

    Each statistic is expressed against the machine's baseline: the mean and
    standard deviation of its readings over the band's ``baseline_days``,
    ending ``baseline_lag_hours`` before the hour ends. The lag stops a
    developing problem (a pre-failure vibration climb, a multi-day overheat)
    from inflating its own baseline and hiding inside it, the same reason the
    alert rules leave a gap. Hours without a full baseline behind them (a
    machine's first ~15 days) are dropped rather than given a partial one.
    """
    config = config or load_config()
    windows = config["predict"]["feature_windows_hours"]
    baseline_hours = config["band"]["baseline_days"] * 24
    lag_hours = config["predict"]["baseline_lag_hours"]

    frame = readings.copy()
    frame["hour"] = pd.to_datetime(frame["ts"]).dt.floor("h")
    for m in MEASURES:
        frame["{}_sq".format(m)] = frame[m] * frame[m]

    # Step 1: collapse each hour's four 15-minute readings to their mean and
    # max. The max keeps a short spike that the mean would smooth away. The
    # sums feed the baseline, which is computed over readings, not hours.
    hourly = frame.groupby(["machine_id", "machine_name", "hour"], as_index=False).agg(
        n=("ts", "count"),
        **{"{}_hmean".format(m): (m, "mean") for m in MEASURES},
        **{"{}_hmax".format(m): (m, "max") for m in MEASURES},
        **{"{}_sum".format(m): (m, "sum") for m in MEASURES},
        **{"{}_sumsq".format(m): ("{}_sq".format(m), "sum") for m in MEASURES},
    )

    # Step 2: trailing windows over those hours, per machine so one machine's
    # window never runs into the next machine's rows.
    parts = []
    for _, machine in hourly.groupby("machine_id", sort=True):
        machine = machine.sort_values("hour").reset_index(drop=True)
        out = machine[["machine_id", "machine_name", "hour"]].copy()

        # The baseline: sums over the last baseline_hours, shifted back by the
        # lag. Variance is mean(x*x) - mean(x)^2, the same formula the SQL
        # uses for the normal band.
        n = machine["n"].rolling(baseline_hours).sum().shift(lag_hours)
        for m in MEASURES:
            total = machine["{}_sum".format(m)].rolling(baseline_hours).sum()
            total_sq = machine["{}_sumsq".format(m)].rolling(baseline_hours).sum()
            base_mean = total.shift(lag_hours) / n
            base_sd = np.sqrt(total_sq.shift(lag_hours) / n - base_mean * base_mean)

            means = machine["{}_hmean".format(m)]
            for hours in windows:
                out["{}_mean_dev_{}h".format(m, hours)] = (
                    means.rolling(hours).mean() - base_mean
                ) / base_sd
                out["{}_max_dev_{}h".format(m, hours)] = (
                    machine["{}_hmax".format(m)].rolling(hours).max() - base_mean
                ) / base_sd
                out["{}_slope_dev_{}h".format(m, hours)] = (
                    means.rolling(hours).apply(_slope, raw=True) / base_sd
                )
        parts.append(out)

    features = pd.concat(parts, ignore_index=True)
    return features.dropna(subset=feature_columns(windows)).reset_index(drop=True)


def label_hours(
    features: pd.DataFrame, failures: pd.DataFrame, horizon_hours: int
) -> pd.DataFrame:
    """Add ``label``: 1 if the machine fails within ``horizon_hours`` of the hour ending.

    Also adds ``failure_id``, the failure that made the label 1, so the
    evaluation can ask "was this particular failure flagged?". When two
    failures fall in one window, the sooner one is kept.
    """
    labelled = features.copy()
    labelled["label"] = 0
    labelled["failure_id"] = np.nan  # float column, so NaN compares cleanly
    hour_end = labelled["hour"] + pd.Timedelta(hours=1)
    horizon = pd.Timedelta(hours=horizon_hours)

    # Latest failure first, so a sooner one overwrites it where windows overlap.
    for failure in failures.sort_values("ts", ascending=False).itertuples():
        failure_ts = pd.Timestamp(failure.ts)
        hit = (
            (labelled["machine_id"] == failure.machine_id)
            & (hour_end <= failure_ts)
            & (failure_ts < hour_end + horizon)
        )
        labelled.loc[hit, "label"] = 1
        labelled.loc[hit, "failure_id"] = failure.failure_id
    return labelled


def _dataset_range(connection: sqlite3.Connection) -> tuple[str, str]:
    """First and last day of the loaded dataset, as query parameters."""
    settings = dataset_settings(connection)
    if settings.empty:
        raise ValueError("No dataset loaded")
    start = date.fromisoformat(settings.iloc[0]["start_date"])
    end = start + timedelta(days=int(settings.iloc[0]["days"]) - 1)
    return start.isoformat() + "T00:00:00", end.isoformat()


def build_dataset(
    connection: sqlite3.Connection, config: dict[str, Any] | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Labelled feature rows for the whole loaded dataset, plus the failures.

    The final ``horizon_hours`` are dropped: a failure just after the data ends
    would make them 1, but we cannot know, so their labels are unknown rather
    than 0.
    """
    config = config or load_config()
    settings = config["predict"]
    start_ts, end_ts = _dataset_range(connection)
    readings = prediction_readings(start_ts, end_ts, connection)
    failures = prediction_failures(start_ts, end_ts, connection)

    features = hourly_features(readings, config)
    labelled = label_hours(features, failures, settings["horizon_hours"])

    last_known = labelled["hour"].max() - pd.Timedelta(hours=settings["horizon_hours"])
    return labelled[labelled["hour"] <= last_known].reset_index(drop=True), failures


# --- splitting --------------------------------------------------------------


def time_split(
    dataset: pd.DataFrame, config: dict[str, Any] | None = None
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Split by time: earlier hours train, later hours test.

    Never random. A random split would put 14:00 in training and 15:00 in
    testing, and since neighbouring hours share most of their window the model
    would effectively be tested on data it has seen.

    A training hour's label looks 24 hours ahead, so the last day before the
    cutoff would carry labels that know about failures in the test period.
    Those hours are dropped: training keeps only hours whose whole label window
    ends at or before the cutoff.
    """
    settings = (config or load_config())["predict"]
    first, last = dataset["hour"].min(), dataset["hour"].max()
    cutoff = (first + (last - first) * (1 - settings["test_fraction"])).ceil("h")

    label_window_end = (
        dataset["hour"] + pd.Timedelta(hours=1 + settings["horizon_hours"])
    )
    train = dataset[label_window_end <= cutoff]
    test = dataset[dataset["hour"] >= cutoff]
    return train.reset_index(drop=True), test.reset_index(drop=True), cutoff


# --- training and evaluation ------------------------------------------------


def _evaluate(
    test: pd.DataFrame,
    risk: np.ndarray,
    failures: pd.DataFrame,
    threshold: float,
) -> dict[str, Any]:
    """Hourly precision and recall, plus per-failure "caught, and how early"."""
    flagged = (risk >= threshold).astype(int)
    metrics: dict[str, Any] = {
        # zero_division=0: if nothing is flagged, precision is reported as 0
        # rather than raising.
        "precision": float(precision_score(test["label"], flagged, zero_division=0)),
        "recall": float(recall_score(test["label"], flagged, zero_division=0)),
        "flagged_hours": int(flagged.sum()),
        # Flagged hours with no failure on that machine within the horizon.
        "false_alarms": int(((flagged == 1) & (test["label"] == 0)).sum()),
    }

    # A failure counts as caught if any hour in the 24 before it was flagged.
    # "Hours early" runs from the end of the FIRST flagged hour, the moment an
    # operator would first have seen a warning, to the failure itself.
    scored = test.assign(flagged=flagged)
    rows = []
    for failure in failures.itertuples():
        before = scored[scored["failure_id"] == failure.failure_id]
        if before.empty:
            continue  # this failure is not in the test period
        hits = before[before["flagged"] == 1]
        hours_early = None
        if not hits.empty:
            first_warning = hits["hour"].min() + pd.Timedelta(hours=1)
            hours_early = round(
                (pd.Timestamp(failure.ts) - first_warning).total_seconds() / 3600, 2
            )
        rows.append(
            {
                "failure_id": int(failure.failure_id),
                "machine_name": failure.machine_name,
                "ts": failure.ts,
                "caught": hours_early is not None,
                "hours_early": hours_early,
            }
        )

    metrics["failures"] = rows
    metrics["test_failures"] = len(rows)
    metrics["caught"] = sum(1 for row in rows if row["caught"])
    early = [row["hours_early"] for row in rows if row["caught"]]
    metrics["mean_hours_early"] = round(float(np.mean(early)), 2) if early else None
    return metrics


def model_paths(model_dir: Path | None = None) -> tuple[Path, Path, Path]:
    """Where the model, its metrics and its test-hour scores are saved.

    All three sit next to the database.
    """
    directory = Path(model_dir) if model_dir is not None else DATA_DIR
    return directory / MODEL_FILE, directory / METRICS_FILE, directory / TEST_RISK_FILE


def train_model(
    connection: sqlite3.Connection | None = None,
    *,
    model_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> TrainResult:
    """Build features, split by time, fit, evaluate, and save.

    Skipped, with any old model removed, when the training period holds no
    failures: a model that has never seen one cannot learn what one looks
    like, and leaving the previous dataset's model behind would show risk
    scores for data it was not trained on.
    """
    config = config or load_config()
    settings = config["predict"]
    model_path, metrics_path, test_risk_path = model_paths(model_dir)
    owns_connection = connection is None
    connection = connection or connect()

    try:
        dataset, failures = build_dataset(connection, config)
        settings_row = dataset_settings(connection).iloc[0]
    finally:
        if owns_connection:
            connection.close()

    def skip(reason: str) -> TrainResult:
        for path in (model_path, metrics_path, test_risk_path):
            path.unlink(missing_ok=True)
        return TrainResult(trained=False, reason=reason)

    if dataset.empty:
        return skip("the dataset is too short to build features")
    train, test, cutoff = time_split(dataset, config)
    if train.empty or test.empty or train["label"].sum() == 0:
        return skip("no failures in the training period to learn from")

    columns = feature_columns(settings["feature_windows_hours"])
    model = RandomForestClassifier(
        n_estimators=settings["n_estimators"],
        min_samples_leaf=settings["min_samples_leaf"],
        # Failure hours are about 1% of all hours. Without re-weighting, the
        # forest scores best by always answering "no failure".
        class_weight="balanced",
        random_state=settings["random_state"],
        n_jobs=-1,
    )
    model.fit(train[columns], train["label"])

    risk = model.predict_proba(test[columns])[:, 1]
    metrics = _evaluate(test, risk, failures, settings["risk_threshold"])
    metrics.update(
        {
            "dataset": settings_row["name"],
            "trained_at": datetime.now().strftime(TS_FORMAT),
            "threshold": settings["risk_threshold"],
            "horizon_hours": settings["horizon_hours"],
            "test_start": cutoff.strftime(TS_FORMAT),
            "train_hours": len(train),
            "test_hours": len(test),
            "train_failures": int(train["failure_id"].dropna().nunique()),
        }
    )

    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "columns": columns}, model_path)
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    # The score of every test hour, so the dashboard's replay plays back the
    # exact predictions the metrics were computed from.
    test[["machine_id", "machine_name", "hour", "label", "failure_id"]].assign(
        risk=risk
    ).to_csv(test_risk_path, index=False, date_format=TS_FORMAT)
    return TrainResult(trained=True, metrics=metrics)


# --- the model's own training dataset ---------------------------------------


def training_db_path(
    model_dir: Path | None = None, config: dict[str, Any] | None = None
) -> Path:
    """Where the training dataset's database lives: next to the model."""
    settings = (config or load_config())["predict"]["training"]
    directory = Path(model_dir) if model_dir is not None else DATA_DIR
    return directory / settings["database"]


def loaded_dataset_name(db_path: Path) -> str | None:
    """Name of the dataset loaded in a database file, or None if there is none."""
    if not Path(db_path).is_file():
        return None
    connection = connect(db_path)
    try:
        settings = dataset_settings(connection)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return None  # not a plant database, or an empty file
    finally:
        connection.close()
    return None if settings.empty else str(settings.iloc[0]["name"])


def prepare_training_db(
    *,
    model_dir: Path | None = None,
    data_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> Path:
    """Generate and load the training dataset, skipping whatever is already done.

    Generation is deterministic, so a dataset folder with the right name
    already holds the right CSVs (unless the generator settings in config.toml
    were edited since, in which case delete the folder). The database is only
    rebuilt when it holds some other dataset.
    """
    config = config or load_config()
    spec = config["predict"]["training"]
    inputs = {
        "seed": spec["seed"],
        "machines": spec["machines"],
        "days": spec["days"],
        "start_date": spec["start_date"],
        "failures": spec["failures"],
    }
    name = resolve_settings(config=config, **inputs).name
    root = data_root or DATA_DIR

    if not (root / name / "dataset.json").is_file():
        generate(data_root=root, config=config, **inputs)

    db_path = training_db_path(model_dir, config)
    if loaded_dataset_name(db_path) != name:
        load_dataset(name, data_root=root, db_path=db_path, config=config)
    return db_path


def train_on_training_set(
    *,
    model_dir: Path | None = None,
    data_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> TrainResult:
    """Train and test the saved model on the ``[predict.training]`` dataset."""
    config = config or load_config()
    db_path = prepare_training_db(model_dir=model_dir, data_root=data_root, config=config)
    connection = connect(db_path)
    try:
        return train_model(connection, model_dir=model_dir, config=config)
    finally:
        connection.close()


# --- using the saved model --------------------------------------------------


def load_metrics(model_dir: Path | None = None) -> dict[str, Any] | None:
    """The saved test metrics, or None if no model has been trained."""
    _, metrics_path, _ = model_paths(model_dir)
    if not metrics_path.is_file():
        return None
    return json.loads(metrics_path.read_text(encoding="utf-8"))


def load_test_risk(model_dir: Path | None = None) -> pd.DataFrame | None:
    """The saved model's risk for every test hour, or None if none is saved.

    Columns: machine_id, machine_name, hour, label, failure_id, risk. Only test
    hours are kept: the model saw the training hours' answers, so replaying
    those would show it looking better than it is.
    """
    _, _, test_risk_path = model_paths(model_dir)
    if not test_risk_path.is_file():
        return None
    return pd.read_csv(test_risk_path, parse_dates=["hour"])


def current_risk(
    connection: sqlite3.Connection | None = None,
    *,
    model_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> pd.DataFrame | None:
    """Failure probability for each machine's most recent hour, highest first.

    Columns: machine_id, machine_name, hour, risk (0-1), flagged. Returns None
    when no model has been trained. Only the readings the final hour's
    baseline and windows need are fetched, about 15 days.
    """
    config = config or load_config()
    settings = config["predict"]
    model_path, _, _ = model_paths(model_dir)
    if not model_path.is_file():
        return None
    # joblib files are pickles, which can run code when loaded. This one is
    # only ever a file train_model() wrote itself.
    saved = joblib.load(model_path)

    owns_connection = connection is None
    connection = connection or connect()
    try:
        _, end_ts = _dataset_range(connection)
        # Back from the end of the last day by the history a feature row
        # needs, plus an hour of slack so the final row is never cut short.
        end_of_day = datetime.fromisoformat(end_ts + "T23:59:59")
        start_ts = (
            end_of_day - timedelta(hours=history_hours(config) + 1)
        ).strftime(TS_FORMAT)
        readings = prediction_readings(start_ts, end_ts, connection)
    finally:
        if owns_connection:
            connection.close()

    features = hourly_features(readings, config)
    latest = features.sort_values("hour").groupby("machine_id").tail(1)
    risk = saved["model"].predict_proba(latest[saved["columns"]])[:, 1]

    result = latest[["machine_id", "machine_name", "hour"]].assign(risk=risk)
    result["flagged"] = result["risk"] >= settings["risk_threshold"]
    return result.sort_values(["risk", "machine_name"], ascending=[False, True]).reset_index(
        drop=True
    )


__all__ = [
    "TrainResult",
    "build_dataset",
    "current_risk",
    "feature_columns",
    "history_hours",
    "hourly_features",
    "label_hours",
    "load_metrics",
    "load_test_risk",
    "loaded_dataset_name",
    "model_paths",
    "prepare_training_db",
    "time_split",
    "train_model",
    "train_on_training_set",
    "training_db_path",
]
