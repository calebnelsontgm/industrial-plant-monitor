"""Failure model tests, run against the default dataset."""

from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest

from plantmonitor.db import connect
from plantmonitor.predict import (
    TrainResult,
    build_dataset,
    current_risk,
    feature_columns,
    history_hours,
    hourly_features,
    load_test_risk,
    loaded_dataset_name,
    time_split,
    train_model,
    train_on_training_set,
    training_db_path,
)


@pytest.fixture(scope="session")
def trained(
    tmp_path_factory: pytest.TempPathFactory, full_db_path: Path
) -> tuple[Path, TrainResult]:
    """Train once on the default dataset. Returns the model directory and result."""
    model_dir = tmp_path_factory.mktemp("model")
    connection = connect(full_db_path)
    try:
        result = train_model(connection, model_dir=model_dir)
    finally:
        connection.close()
    assert result.trained, result.reason
    return model_dir, result


def test_time_split_has_no_overlap(full_db_path: Path, config: dict) -> None:
    """Every training hour, and its whole label window, precedes every test hour."""
    connection = connect(full_db_path)
    try:
        dataset, _ = build_dataset(connection, config)
    finally:
        connection.close()

    train, test, cutoff = time_split(dataset, config)
    assert not train.empty and not test.empty

    assert train["hour"].max() < test["hour"].min()
    assert test["hour"].min() >= cutoff

    # The gap: a training hour's label looks horizon_hours past the end of the
    # hour, and that look-ahead must stop at the cutoff.
    horizon = pd.Timedelta(hours=1 + config["predict"]["horizon_hours"])
    assert (train["hour"] + horizon).max() <= cutoff

    shared = train.merge(test, on=["machine_id", "hour"])
    assert shared.empty


def test_model_catches_most_planted_failures(trained) -> None:
    """Most test-period failures are flagged before they happen."""
    _, result = trained
    metrics = result.metrics

    assert metrics["test_failures"] >= 1, "the test period should hold failures"
    assert metrics["caught"] / metrics["test_failures"] > 0.5
    for failure in metrics["failures"]:
        if failure["caught"]:
            assert failure["hours_early"] > 0


def test_current_risk_one_value_per_machine(trained, full_db_path: Path) -> None:
    """One risk per machine, each a probability between 0 and 1."""
    model_dir, _ = trained
    connection = connect(full_db_path)
    try:
        risk = current_risk(connection, model_dir=model_dir)
        machine_ids = {row[0] for row in connection.execute("SELECT id FROM machines")}
    finally:
        connection.close()

    assert risk is not None
    assert len(risk) == len(machine_ids)
    assert set(risk["machine_id"]) == machine_ids
    assert risk["risk"].between(0, 1).all()


def test_current_risk_without_a_model(full_db_path: Path, tmp_path: Path) -> None:
    """With nothing trained, current_risk says so instead of raising."""
    connection = connect(full_db_path)
    try:
        assert current_risk(connection, model_dir=tmp_path) is None
    finally:
        connection.close()


def test_test_risk_saved_for_replay(trained) -> None:
    """Training saves a score for every test hour, and only test hours."""
    model_dir, result = trained
    scores = load_test_risk(model_dir)

    assert scores is not None
    assert len(scores) == result.metrics["test_hours"]
    assert scores["hour"].min() >= pd.Timestamp(result.metrics["test_start"])
    assert scores["risk"].between(0, 1).all()


def test_false_alarms_counted(trained) -> None:
    """false_alarms is the flagged test hours with no failure within the horizon."""
    model_dir, result = trained
    scores = load_test_risk(model_dir)
    flagged = scores["risk"] >= result.metrics["threshold"]

    assert result.metrics["flagged_hours"] == int(flagged.sum())
    assert result.metrics["false_alarms"] == int((flagged & (scores["label"] == 0)).sum())


def test_train_on_training_set(tmp_path: Path, config: dict) -> None:
    """The model's own dataset is generated, loaded into its own database, and trained on.

    A small training dataset keeps this quick; the real one is set in config.toml.
    """
    small = copy.deepcopy(config)
    small["predict"]["training"].update(machines=4, days=30, failures=30)
    model_dir, data_root = tmp_path / "model", tmp_path / "datasets"

    result = train_on_training_set(model_dir=model_dir, data_root=data_root, config=small)

    assert result.trained, result.reason
    assert result.metrics["dataset"] == "seed42_4m_30d_30f"
    assert result.metrics["test_failures"] >= 1
    db_path = training_db_path(model_dir, small)
    assert loaded_dataset_name(db_path) == "seed42_4m_30d_30f"
    # Only the training database is written: plant.db is someone else's.
    assert not (model_dir / "plant.db").exists()

    # A second run reuses the dataset and database rather than rebuilding them.
    readings_csv = data_root / "seed42_4m_30d_30f" / "readings.csv"
    before = (readings_csv.stat().st_mtime_ns, db_path.stat().st_mtime_ns)
    again = train_on_training_set(model_dir=model_dir, data_root=data_root, config=small)
    assert again.metrics["precision"] == result.metrics["precision"]
    assert (readings_csv.stat().st_mtime_ns, db_path.stat().st_mtime_ns) == before


def _synthetic_readings(days: int, seed: int = 1) -> pd.DataFrame:
    """One machine's 15-minute readings: steady values plus random noise."""
    import numpy as np

    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-01-01", periods=days * 96, freq="15min")
    return pd.DataFrame(
        {
            "machine_id": 1,
            "machine_name": "M-01",
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "temperature_c": 75 + rng.normal(0, 0.8, len(ts)),
            "speed_rpm": 1500 + rng.normal(0, 15, len(ts)),
            "vibration_mm_s": 3 + rng.normal(0, 0.15, len(ts)),
        }
    )


def test_features_cannot_identify_a_machine(config: dict) -> None:
    """Two machines whose readings differ only in level and spread get identical features.

    Shifting and scaling a machine's readings is exactly what makes one machine
    differ from another, so features that ignore both cannot tell them apart.
    """
    first = _synthetic_readings(days=20)
    second = first.assign(machine_id=2, machine_name="M-02")
    for measure in ("temperature_c", "speed_rpm", "vibration_mm_s"):
        second[measure] = second[measure] * 1.3 + 40

    features = hourly_features(pd.concat([first, second]), config)
    columns = feature_columns(config["predict"]["feature_windows_hours"])
    left = features[features["machine_id"] == 1][columns].reset_index(drop=True)
    right = features[features["machine_id"] == 2][columns].reset_index(drop=True)

    assert not left.empty
    pd.testing.assert_frame_equal(left, right, atol=1e-6)


def test_features_use_only_the_past(config: dict) -> None:
    """Changing readings after an hour does not change that hour's features."""
    readings = _synthetic_readings(days=20)
    cut = pd.Timestamp("2026-01-18T00:00:00")
    later = pd.to_datetime(readings["ts"]) >= cut
    altered = readings.copy()
    altered.loc[later, "vibration_mm_s"] += 10.0

    columns = feature_columns(config["predict"]["feature_windows_hours"])
    before = hourly_features(readings, config)
    after = hourly_features(altered, config)
    earlier = before["hour"] < cut

    assert earlier.any()
    pd.testing.assert_frame_equal(
        before.loc[earlier, columns].reset_index(drop=True),
        after.loc[after["hour"] < cut, columns].reset_index(drop=True),
    )


def test_features_start_after_a_full_baseline(config: dict) -> None:
    """No feature row exists until the lagged baseline has all its history."""
    readings = _synthetic_readings(days=20)
    features = hourly_features(readings, config)

    first_hour = pd.Timestamp("2026-01-01") + pd.Timedelta(hours=history_hours(config) - 1)
    assert features["hour"].min() == first_hour
