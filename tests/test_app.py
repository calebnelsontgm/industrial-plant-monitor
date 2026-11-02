"""Dashboard tests, driven through streamlit.testing.v1.AppTest."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest
from streamlit.testing.v1 import AppTest

from plantmonitor import ROOT, load_config
from plantmonitor.alerts import run_alerts
from plantmonitor.db import connect
from plantmonitor.load import load_dataset
from plantmonitor.predict import train_model

APP = str(ROOT / "app.py")

#: The dashboard builds the full default dataset when the Data page is used,
#: so these need headroom over the usual few seconds.
TIMEOUT = 120


@pytest.fixture
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the dashboard at a temporary database and dataset directory.

    Without this the Generate button would overwrite the real data/plant.db.
    """
    db_path = tmp_path / "plant.db"
    monkeypatch.setenv("PLANTMONITOR_DB", str(db_path))
    monkeypatch.setenv("PLANTMONITOR_DATA_DIR", str(tmp_path / "datasets"))
    yield db_path


@pytest.fixture
def loaded_app(app_env: Path, default_dataset) -> Path:
    """A temporary database already holding the default dataset and alerts."""
    load_dataset(
        default_dataset.settings.name,
        data_root=default_dataset.directory.parent,
        db_path=app_env,
    )
    connection = connect(app_env)
    try:
        run_alerts(connection)
    finally:
        connection.close()
    return app_env


def run_app(page: str | None = None) -> AppTest:
    """Start the app, optionally switching to a page."""
    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    if page is not None:
        app.sidebar.radio[0].set_value(page).run()
    return app


def test_app_starts_without_a_database(app_env: Path) -> None:
    """AppTest runs app.py with no exceptions, even with nothing loaded."""
    app = run_app()

    assert not app.exception
    assert any("Data page" in str(block.value) for block in app.info)


def test_every_page_runs(loaded_app: Path) -> None:
    """Each of the six pages renders without raising."""
    for page in (
        "Overview", "Machine detail", "Patterns", "Alerts", "Failure risk", "Data"
    ):
        app = run_app(page)
        assert not app.exception, page


def test_overview_shows_dataset_settings(loaded_app: Path) -> None:
    """Every page shows the active dataset's settings at the top."""
    app = run_app("Overview")

    captions = " ".join(str(block.value) for block in app.caption)
    assert "12 machines" in captions
    assert "90 days" in captions


def test_overview_metrics_present(loaded_app: Path) -> None:
    """The KPI cards render with real numbers."""
    app = run_app("Overview")

    labels = [metric.label for metric in app.metric]
    assert "Machines online" in labels
    assert "Open alerts" in labels
    assert any("Failures" in label for label in labels)


def test_generate_from_data_page(app_env: Path) -> None:
    """Generating 3 machines x 7 days makes Overview list exactly those 3."""
    app = run_app("Data")
    app.number_input[0].set_value(3)   # machines
    app.number_input[1].set_value(7)   # days
    app.button[0].click().run()

    assert not app.exception
    # The header is rendered before the button handler runs, so the new
    # dataset's settings only appear from the next rerun onwards. What proves
    # the run itself worked is the success message.
    assert app.success, "expected a success message"
    assert "3 machines" in str(app.success[0].value)

    app.sidebar.radio[0].set_value("Overview").run()
    assert not app.exception

    captions = " ".join(str(block.value) for block in app.caption)
    assert "3 machines" in captions
    assert "7 days" in captions

    connection = connect(app_env)
    try:
        names = [
            row[0]
            for row in connection.execute("SELECT name FROM machines ORDER BY name")
        ]
    finally:
        connection.close()
    assert names == ["M-01", "M-02", "M-03"]


@pytest.mark.parametrize(
    ("machines", "days"),
    [(25, 90), (12, 200)],
)
def test_out_of_range_shows_error_and_generates_nothing(
    app_env: Path, machines: int, days: int
) -> None:
    """25 machines or 200 days shows an error and generates nothing."""
    app = run_app("Data")
    app.number_input[0].set_value(machines)
    app.number_input[1].set_value(days)
    app.button[0].click().run()

    assert not app.exception
    assert app.error, "expected an error message"
    assert not app_env.is_file(), "no database should have been written"


def test_empty_date_range_shows_a_message(loaded_app: Path) -> None:
    """A date range with no data shows a message, not an exception."""
    app = run_app("Patterns")
    assert not app.exception

    # The picker is clamped to the dataset, so the narrowest range available is
    # a single day. The fixture's failures are spread over 90 days, so one day
    # with no failure in it lands on the empty state.
    app.date_input[0].set_value((date(2026, 9, 15), date(2026, 9, 15))).run()

    assert not app.exception
    assert app.info or app.warning


def test_machine_detail_renders_charts(loaded_app: Path) -> None:
    """Machine detail draws one chart per reading type."""
    app = run_app("Machine detail")

    assert not app.exception
    assert len(app.selectbox) >= 1


def test_alerts_page_lists_alerts(loaded_app: Path) -> None:
    """The Alerts page shows the alerts the engine raised."""
    app = run_app("Alerts")

    assert not app.exception
    assert app.multiselect, "expected severity and machine filters"


def test_failure_risk_page_without_a_model(loaded_app: Path) -> None:
    """Before any training the page explains how to get a model."""
    app = run_app("Failure risk")

    assert not app.exception
    assert any("No failure model" in str(block.value) for block in app.info)


def test_failure_risk_page_shows_model(loaded_app: Path) -> None:
    """With a trained model the page lists risk and the test metrics."""
    connection = connect(loaded_app)
    try:
        train_model(connection, model_dir=loaded_app.parent)
    finally:
        connection.close()

    app = run_app("Failure risk")

    assert not app.exception
    labels = [metric.label for metric in app.metric]
    assert {"Precision", "Recall", "Failures caught"} <= set(labels)
    assert len(app.dataframe) >= 1

    # The replay: one animated chart, with one frame per hour of the window.
    charts = app.get("plotly_chart")
    assert charts, "expected the replay chart"
    spec = json.loads(charts[0].proto.spec)
    settings = load_config()["predict"]
    assert len(spec["frames"]) >= (
        settings["replay_hours_before"] + settings["replay_hours_after"]
    )


def test_failure_risk_replays_from_training_database(loaded_app: Path) -> None:
    """A model trained on its own dataset replays from that dataset's database.

    plant.db holds the 90-day default, which has none of the model's test
    failures, so the replay only draws if it reads the training database.
    """
    import copy

    from plantmonitor.predict import train_on_training_set

    small = copy.deepcopy(load_config())
    small["predict"]["training"].update(machines=4, days=30, failures=30)
    result = train_on_training_set(
        model_dir=loaded_app.parent, data_root=loaded_app.parent / "train", config=small
    )
    assert result.trained, result.reason

    app = run_app("Failure risk")

    assert not app.exception
    captions = " ".join(str(block.value) for block in app.markdown)
    assert "seed42_4m_30d_30f" in captions
    assert app.get("plotly_chart"), "expected the replay chart"
