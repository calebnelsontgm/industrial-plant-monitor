"""Generator tests: reproducibility, settings, and the planted problems."""

from __future__ import annotations

import filecmp
from pathlib import Path

import pandas as pd
import pytest

from plantmonitor.generate import SettingsError, generate

CSV_NAMES = (
    "lines.csv",
    "machines.csv",
    "readings.csv",
    "downtime_events.csv",
    "failures.csv",
)

ALL_PROBLEMS_OFF = {
    "overheat": False,
    "slowdown": False,
    "downtime": False,
    "prefailure": False,
}


def test_same_seed_gives_identical_csvs(tmp_path: Path) -> None:
    """Same inputs and seed give identical CSVs."""
    first = generate(data_root=tmp_path / "a", seed=7, machines=4, days=10)
    second = generate(data_root=tmp_path / "b", seed=7, machines=4, days=10)

    for csv_name in CSV_NAMES:
        left = first.directory / csv_name
        right = second.directory / csv_name
        assert left.read_bytes() == right.read_bytes(), csv_name
        assert filecmp.cmp(left, right, shallow=False)


def test_different_seed_gives_different_readings(tmp_path: Path) -> None:
    """A different seed changes the data, so the comparison above means something."""
    first = generate(data_root=tmp_path / "a", seed=7, machines=4, days=10)
    second = generate(data_root=tmp_path / "b", seed=8, machines=4, days=10)

    assert (first.directory / "readings.csv").read_bytes() != (
        second.directory / "readings.csv"
    ).read_bytes()


def test_settings_respected(tmp_path: Path) -> None:
    """With all problems off, 3 machines x 7 days gives 3 machines and 2,016 readings."""
    result = generate(
        data_root=tmp_path, machines=3, days=7, problems=ALL_PROBLEMS_OFF
    )

    machines = pd.read_csv(result.directory / "machines.csv")
    readings = pd.read_csv(result.directory / "readings.csv")

    assert len(machines) == 3
    assert len(readings) == 2016
    assert result.row_counts["readings"] == 2016
    assert readings["machine_id"].nunique() == 3


def test_overheat_present(default_dataset, machine_readings) -> None:
    """M-07's last-4-day mean temperature exceeds its first-30-day mean by 8 C or more."""
    readings = machine_readings(default_dataset, "M-07")
    last = readings["ts"].max()

    recent = readings[readings["ts"] > last - pd.Timedelta(days=4)]
    baseline = readings[
        readings["ts"] < readings["ts"].min() + pd.Timedelta(days=30)
    ]

    assert recent["temperature_c"].mean() - baseline["temperature_c"].mean() >= 8.0


def test_overheat_toggle(dataset_without_overheat, machine_readings, config) -> None:
    """With overheating off, M-07's last-4-day mean stays within its normal band."""
    readings = machine_readings(dataset_without_overheat, "M-07")
    last = readings["ts"].max()

    baseline_days = config["band"]["baseline_days"]
    sigma = config["band"]["sigma"]
    window = readings[readings["ts"] > last - pd.Timedelta(days=baseline_days)]
    mean = window["temperature_c"].mean()
    spread = sigma * window["temperature_c"].std(ddof=0)

    recent_mean = readings[
        readings["ts"] > last - pd.Timedelta(days=4)
    ]["temperature_c"].mean()

    assert mean - spread <= recent_mean <= mean + spread


def test_slowdown_step(default_dataset, machine_readings, config) -> None:
    """M-03's final 7-day average speed is 12% (+/-0.5%) below the previous 7 days."""
    readings = machine_readings(default_dataset, "M-03")
    per_block = config["generation"]["planted"]["slowdown_days"] * 96

    final = readings["speed_rpm"].iloc[-per_block:].mean()
    previous = readings["speed_rpm"].iloc[-2 * per_block : -per_block].mean()
    drop_percent = (previous - final) / previous * 100

    expected = config["generation"]["planted"]["slowdown_percent"]
    assert drop_percent == pytest.approx(expected, abs=0.5)


def test_named_machine_missing_is_skipped(tmp_path: Path) -> None:
    """Too few machines for M-07 or M-11: the problem is skipped and logged."""
    result = generate(data_root=tmp_path, machines=2, days=40)

    assert any("overheat" in note for note in result.skipped)
    assert any("downtime" in note for note in result.skipped)
    assert pd.read_csv(result.directory / "downtime_events.csv").empty


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"machines": 25}, "machines"),
        ({"machines": 0}, "machines"),
        ({"days": 200}, "days"),
        ({"days": 0}, "days"),
        ({"start_date": "not-a-date"}, "start_date"),
    ],
)
def test_out_of_range_inputs_rejected(
    tmp_path: Path, kwargs: dict, expected: str
) -> None:
    """Out-of-range inputs raise before anything is written."""
    with pytest.raises(SettingsError, match=expected):
        generate(data_root=tmp_path, **kwargs)

    assert not list(tmp_path.iterdir())


def test_manifest_records_every_input(tmp_path: Path) -> None:
    """dataset.json carries the settings the loader puts into dataset_meta."""
    import json

    result = generate(data_root=tmp_path, seed=5, machines=3, days=8)
    manifest = json.loads((result.directory / "dataset.json").read_text())

    assert manifest["seed"] == 5
    assert manifest["machines"] == 3
    assert manifest["days"] == 8
    assert set(manifest["problems"]) == {
        "overheat",
        "slowdown",
        "downtime",
        "prefailure",
    }
    assert manifest["name"] == "seed5_3m_8d"
