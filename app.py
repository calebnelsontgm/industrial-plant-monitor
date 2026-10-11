"""Plant Monitor dashboard.

Five pages, selected from the sidebar: Overview, Machine detail, Patterns,
Alerts and Data. Everything lives in this one file because the build brief
fixes the project layout and has no ``pages/`` directory.

Colour is handled in one place. ``INK``, ``SERIES`` and ``STATUS`` hold the only
palette, every figure goes through ``style_figure()`` before it is drawn, and a
reading type keeps the same hue everywhere so temperature is always the same
blue. Status colours are reserved for machine state and are never reused for a
data series; each one ships with a marker shape and a label, so state never
depends on colour alone.

The plant map is the spine of the dashboard rather than an ornament. It appears
on Overview, where clicking a machine opens it, and again on Machine detail as a
locator showing where the machine being examined sits on its line.

The database and dataset paths can be redirected with the ``PLANTMONITOR_DB``
and ``PLANTMONITOR_DATA_DIR`` environment variables, so the dashboard tests can
drive a real Generate run without touching ``data/``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from plantmonitor import DATA_DIR, DB_PATH, load_config
from plantmonitor import queries
from plantmonitor.alerts import resolve_alert, run_alerts
from plantmonitor.db import connect
from plantmonitor.generate import PROBLEM_NAMES, SettingsError, generate
from plantmonitor.load import load_dataset

CONFIG = load_config()
DATA_ROOT = Path(os.environ.get("PLANTMONITOR_DATA_DIR", str(DATA_DIR)))
DB = Path(os.environ.get("PLANTMONITOR_DB", str(DB_PATH)))

PAGES = ("Overview", "Machine detail", "Patterns", "Alerts", "Data")

# --- palette ---------------------------------------------------------------
# Chart chrome and text. Text always wears one of these, never a series colour.
INK = {
    "surface": "#fcfcfb",
    "plane": "#f9f9f7",
    "primary": "#0b0b0b",
    "secondary": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
}

#: Categorical slots 1-3. Validated for colour-vision separation as a set, so
#: the three must be used in this order and not extended without re-checking.
SERIES = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a"}

#: Reserved for machine state. Never a data series.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b"}

#: Diverging pair for signed change: warm and cool poles with a grey middle.
DIVERGING = {"up": "#2a78d6", "down": "#d03b3b", "flat": "#c3c2b7"}

#: One hue per reading type, kept the same on every page so a colour means the
#: same thing wherever it appears.
READING_TYPES = (
    ("temperature_c", "Temperature", "°C", SERIES["blue"]),
    ("speed_rpm", "Speed", "rpm", SERIES["orange"]),
    ("vibration_mm_s", "Vibration", "mm/s", SERIES["aqua"]),
)

#: Machine state, in the order it appears in the map legend. The symbol is the
#: secondary encoding: the states stay distinguishable without colour.
MACHINE_STATES = (
    ("running", "Running", STATUS["good"], "circle"),
    ("alert", "Open alert", STATUS["warning"], "diamond"),
    ("down", "Down now", STATUS["critical"], "square"),
)

PROBLEM_LABELS = {
    "overheat": "Overheating on M-07",
    "slowdown": "Slowdown on M-03",
    "downtime": "Repeated downtime on M-11",
    "prefailure": "Rising vibration before failures",
}

#: What each planted problem looks like and where to go to see it. Machine
#: names come from config rather than being written in twice.
PLANTED = CONFIG["generation"]["planted"]
PROBLEM_GUIDE = {
    "overheat": (
        PLANTED["overheat_machine"],
        "Temperature climbs over the final {} days, far past the normal band.".format(
            PLANTED["overheat_days"]
        ),
        "Machine detail",
    ),
    "slowdown": (
        PLANTED["slowdown_machine"],
        "Speed steps down {:.0f}% exactly {} days before the last reading.".format(
            PLANTED["slowdown_percent"], PLANTED["slowdown_days"]
        ),
        "Machine detail",
    ),
    "downtime": (
        PLANTED["downtime_machine"],
        "{} downtime events inside the last {} days.".format(
            PLANTED["downtime_events"], PLANTED["downtime_window_days"]
        ),
        "Machine detail",
    ),
    "prefailure": (
        "3-5 machines",
        "Vibration climbs for {}-{} hours before each failure.".format(
            *PLANTED["prefailure_lead_hours"]
        ),
        "Patterns",
    ),
}

#: One line per page, so a reader knows what they are looking at before they
#: read a number.
PAGE_BLURB = {
    "Overview": (
        "The whole plant at a glance. Click a machine on the map to open it."
    ),
    "Machine detail": (
        "One machine over time. The shaded band is that machine's own normal "
        "range from its last 14 days; crosses mark readings outside it, grey "
        "spans are downtime and dashed red lines are failures."
    ),
    "Patterns": (
        "What the data looked like before each failure, which failure types "
        "occurred, and which machines are losing speed week on week."
    ),
    "Alerts": (
        "Everything the four rules raised. Each row is one continuous problem, "
        "timestamped at the moment it began."
    ),
    "Data": (
        "Build a new dataset. The faults are planted deliberately, so the "
        "queries have a known answer to find."
    ),
}

STYLE = """
<style>
  .block-container { padding-top: 2.2rem; max-width: 1180px; }

  h1, h2, h3, h4 { letter-spacing: -0.01em; }
  h1 { font-size: 1.75rem !important; margin-bottom: 0.1rem !important; }

  .pm-sub {
    color: #52514e; font-size: 0.95rem; line-height: 1.5;
    margin: 0.1rem 0 0.4rem 0; max-width: 76ch;
  }
  .pm-meta {
    color: #52514e; font-size: 0.82rem; margin: 0 0 1.1rem 0;
    padding: 0.5rem 0.75rem; background: #f3f2ee;
    border-left: 3px solid #2a78d6; border-radius: 3px;
  }
  .pm-section {
    font-size: 0.74rem; font-weight: 700; letter-spacing: 0.09em;
    text-transform: uppercase; color: #898781;
    margin: 1.9rem 0 0.5rem 0; padding-bottom: 0.3rem;
    border-bottom: 1px solid #e1e0d9;
  }
  .pm-note { color: #898781; font-size: 0.8rem; margin: -0.2rem 0 0.7rem 0; }

  /* Metric cards: a hairline ring and a surface, so four numbers in a row
     read as four cards instead of one undivided strip. */
  div[data-testid="stMetric"] {
    background: #fcfcfb; border: 1px solid rgba(11,11,11,0.10);
    border-radius: 6px; padding: 0.85rem 1rem;
  }
  div[data-testid="stMetricLabel"] p {
    font-size: 0.78rem !important; color: #52514e !important;
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  div[data-testid="stMetricValue"] { font-size: 1.75rem; }

  section[data-testid="stSidebar"] { border-right: 1px solid #e1e0d9; }
  div[data-testid="stDataFrame"] { border-radius: 6px; }
</style>
"""


# --- shared helpers --------------------------------------------------------


def style_figure(figure: go.Figure, title: str, height: int = 320) -> go.Figure:
    """Apply the one chart style: recessive chrome, readable ink, no clutter.

    Every figure in the app goes through here, so nothing inherits Plotly's
    defaults and the charts match each other rather than approximately matching.
    """
    figure.update_layout(
        title={"text": title, "font": {"size": 15, "color": INK["primary"]}, "x": 0},
        font={"family": "system-ui, -apple-system, 'Segoe UI', sans-serif",
              "size": 12, "color": INK["secondary"]},
        paper_bgcolor=INK["surface"],
        plot_bgcolor=INK["surface"],
        height=height,
        margin={"l": 8, "r": 8, "t": 46, "b": 8},
        hovermode="x unified",
        hoverlabel={"bgcolor": INK["surface"], "bordercolor": INK["axis"],
                    "font": {"color": INK["primary"], "size": 12}},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.0,
                "xanchor": "right", "x": 1.0,
                "bgcolor": "rgba(0,0,0,0)", "font": {"size": 11}},
    )
    # Hairline grid on one axis only, and no vertical rules: the time axis does
    # not need them and they fight with the data.
    figure.update_xaxes(
        showgrid=False, zeroline=False,
        linecolor=INK["axis"], tickcolor=INK["axis"],
        tickfont={"color": INK["muted"], "size": 11},
    )
    figure.update_yaxes(
        showgrid=True, gridcolor=INK["grid"], gridwidth=1, zeroline=False,
        linecolor="rgba(0,0,0,0)", tickcolor=INK["axis"],
        tickfont={"color": INK["muted"], "size": 11},
    )
    return figure


def database_ready() -> bool:
    """True when a loaded database exists."""
    return DB.is_file()


def settings_row() -> pd.Series | None:
    """The active dataset's settings, or None if nothing is loaded."""
    if not database_ready():
        return None
    connection = connect(DB)
    try:
        frame = queries.dataset_settings(connection)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return None
    finally:
        connection.close()
    return None if frame.empty else frame.iloc[0]


def dataset_range() -> tuple[date, date]:
    """First and last day the active dataset covers."""
    row = settings_row()
    if row is None:
        today = date.today()
        return today, today
    start = date.fromisoformat(row["start_date"])
    return start, start + timedelta(days=int(row["days"]) - 1)


def section(label: str, note: str | None = None) -> None:
    """A section rule, so a page reads as parts rather than one long scroll."""
    st.markdown('<div class="pm-section">{}</div>'.format(label),
                unsafe_allow_html=True)
    if note:
        st.markdown('<div class="pm-note">{}</div>'.format(note),
                    unsafe_allow_html=True)


def show_header(page: str) -> None:
    """Title, what this page is for, and the active dataset's settings."""
    st.markdown(STYLE, unsafe_allow_html=True)
    st.title("Plant Monitor")

    row = settings_row()
    if row is None:
        st.markdown(
            '<div class="pm-sub">Simulated plant monitoring: machines on two '
            'production lines reporting temperature, speed and vibration every '
            '15 minutes.</div>',
            unsafe_allow_html=True,
        )
        st.info("No dataset loaded yet. Open the Data page to generate one.")
        return

    problems = json.loads(row["problems"])
    active = [PROBLEM_LABELS.get(k, k) for k, on in sorted(problems.items()) if on]

    # Kept as a caption so the dataset settings stay machine-readable to the
    # dashboard tests, which look for "12 machines" and "90 days" here.
    st.caption(
        "Dataset **{}** - {} machines, {} days from {}, seed {}. "
        "Planted problems: {}".format(
            row["name"], row["machines"], row["days"], row["start_date"],
            row["seed"], ", ".join(active) if active else "none",
        )
    )
    st.markdown("### {}".format(page))
    st.markdown('<div class="pm-sub">{}</div>'.format(PAGE_BLURB[page]),
                unsafe_allow_html=True)


def what_to_look_for() -> None:
    """Name the planted faults and where to see each one.

    The whole dataset is synthetic and the faults are deliberate, so hiding
    that would make the dashboard look like a discovery it is not. Saying it
    plainly also turns the app into something navigable on a first run instead
    of five pages of numbers with no entry point.
    """
    row = settings_row()
    if row is None:
        return
    problems = json.loads(row["problems"])
    planted = [key for key, on in problems.items() if on and key in PROBLEM_GUIDE]
    if not planted:
        st.caption("No faults were planted in this dataset: it is all baseline.")
        return

    with st.expander("What is planted in this data, and where to look"):
        st.caption(
            "The data is generated, not measured. These faults were written in "
            "on purpose so every query and alert rule has a known right answer."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {"Machine": PROBLEM_GUIDE[k][0],
                     "What was planted": PROBLEM_GUIDE[k][1],
                     "See it on": PROBLEM_GUIDE[k][2]}
                    for k in PROBLEM_NAMES if k in planted
                ]
            ),
            hide_index=True,
            width="stretch",
        )


def machine_state(row: pd.Series) -> str:
    """Which of the three states a machine is in."""
    if int(row["is_down"]):
        return "down"
    if int(row["open_alerts"]):
        return "alert"
    return "running"


def plant_diagram(
    status: pd.DataFrame,
    selected: int | None = None,
    height: int = 260,
    title: str = "Plant layout and current state",
) -> go.Figure:
    """The plant as a picture: rails of machines, coloured by state.

    This is the orientation device for the whole dashboard. Without it the app
    opens on a table of machine names and the reader has no idea what shape the
    plant is. Passing ``selected`` rings one machine, which is what turns the
    same figure into a locator on the Machine detail page.
    """
    figure = go.Figure()
    line_ids = sorted(int(i) for i in status["line_id"].unique())
    width = float(status["position"].max() or 1)

    # A rail behind each line, so the nodes read as machines on a production
    # line rather than as points in a scatter plot.
    for line_id in line_ids:
        figure.add_shape(
            type="line", x0=0.6, x1=width + 0.4, y0=-line_id, y1=-line_id,
            line={"color": INK["grid"], "width": 8}, layer="below",
        )

    states = status.apply(machine_state, axis=1)

    # The selection ring goes on first, so the machine's own marker lands on
    # top of it.
    if selected is not None:
        chosen = status[status["machine_id"] == selected]
        if not chosen.empty:
            figure.add_trace(
                go.Scatter(
                    x=chosen["position"], y=-chosen["line_id"],
                    mode="markers", name="Selected", showlegend=False,
                    marker={"size": 46, "color": "rgba(0,0,0,0)",
                            "line": {"color": INK["primary"], "width": 2}},
                    hoverinfo="skip",
                )
            )

    for key, label, colour, symbol in MACHINE_STATES:
        part = status[states == key]
        if part.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=part["position"],
                y=-part["line_id"],
                mode="markers+text",
                name=label,
                marker={"size": 26, "color": colour, "symbol": symbol,
                        "line": {"color": INK["surface"], "width": 2}},
                text=part["machine_name"],
                textposition="bottom center",
                textfont={"color": INK["secondary"], "size": 11},
                # machine_id rides along last so a click can be mapped back to
                # a machine without guessing from the point index.
                customdata=part[
                    ["machine_name", "line_name", "temperature_c", "speed_rpm",
                     "vibration_mm_s", "open_alerts", "machine_id"]
                ].values,
                hovertemplate=(
                    "<b>%{customdata[0]}</b> · %{customdata[1]}<br>"
                    + label
                    + "<br>%{customdata[2]:.1f} °C · %{customdata[3]:.0f} rpm"
                    + " · %{customdata[4]:.2f} mm/s"
                    + "<br>%{customdata[5]} open alert(s)<extra></extra>"
                ),
            )
        )

    style_figure(figure, title, height=height)
    figure.update_layout(hovermode="closest", clickmode="event+select",
                         margin={"l": 8, "r": 8, "t": 46, "b": 24})
    figure.update_xaxes(visible=False, range=[0.3, width + 0.7])
    figure.update_yaxes(
        showgrid=False, visible=True, tickmode="array",
        tickvals=[-i for i in line_ids],
        ticktext=[
            str(status.loc[status["line_id"] == i, "line_name"].iloc[0])
            for i in line_ids
        ],
        tickfont={"color": INK["secondary"], "size": 12},
        range=[-max(line_ids) - 0.75, -min(line_ids) + 0.6],
        linecolor="rgba(0,0,0,0)",
    )
    return figure


def selected_machine_from(event) -> int | None:
    """Read a machine_id out of a plotly selection event, if there is one."""
    try:
        points = event["selection"]["points"]
    except (TypeError, KeyError):
        return None
    for point in points:
        data = point.get("customdata")
        if data:
            return int(data[-1])
    return None


def week_blocks_chart(blocks: pd.DataFrame, machine_name: str) -> go.Figure:
    """One machine's average speed per 7-day block, oldest block first.

    Bars rather than a line: the blocks are discrete buckets, and a step
    between two of them should look like a step rather than a slope.
    """
    ordered = blocks.sort_values("block_index", ascending=False).copy()
    labels = ["" for _ in range(len(ordered))]
    for position, (_, row) in enumerate(ordered.iterrows()):
        index = int(row["block_index"])
        labels[position] = "latest" if index == 0 else "-{}w".format(index)

    changes = [
        "" if pd.isna(value) else "{:+.1f}%".format(value)
        for value in ordered["percent_change"]
    ]

    figure = go.Figure(
        go.Bar(
            x=labels, y=ordered["avg_speed_rpm"],
            marker={"color": SERIES["orange"],
                    "line": {"color": INK["surface"], "width": 2}},
            text=changes, textposition="outside",
            textfont={"color": INK["secondary"], "size": 11},
            customdata=ordered["block_start"],
            hovertemplate="%{y:.0f} rpm<br>block starts %{customdata}<extra></extra>",
        )
    )
    figure.update_layout(bargap=0.3)
    figure.update_yaxes(title={"text": "rpm",
                              "font": {"size": 11, "color": INK["muted"]}})
    return style_figure(
        figure,
        "{} average speed per 7-day block, with change on the block before".format(
            machine_name
        ),
        height=320,
    )


def week_change_chart(blocks: pd.DataFrame) -> go.Figure:
    """Every machine's latest 7-day change, worst first.

    A signed quantity, so the colour diverges: cool for a gain, warm for a
    loss, and a neutral grey when the move is too small to mean anything.
    """
    latest = blocks[(blocks["block_index"] == 0) & blocks["percent_change"].notna()]
    latest = latest.sort_values("percent_change")
    if latest.empty:
        return go.Figure()

    threshold = float(CONFIG["alerts"]["speed_drop"]["percent"])
    colours = [
        DIVERGING["down"] if value <= -threshold
        else DIVERGING["up"] if value > 0.5
        else DIVERGING["flat"]
        for value in latest["percent_change"]
    ]

    figure = go.Figure(
        go.Bar(
            x=latest["percent_change"], y=latest["machine_name"],
            orientation="h",
            marker={"color": colours,
                    "line": {"color": INK["surface"], "width": 2}},
            text=["{:+.1f}%".format(v) for v in latest["percent_change"]],
            textposition="outside",
            textfont={"color": INK["secondary"], "size": 11},
            hovertemplate="%{y}: %{x:+.2f}%<extra></extra>",
        )
    )
    figure.add_vline(x=-threshold, line_dash="dot", line_width=1,
                     line_color=STATUS["critical"])
    figure.update_layout(bargap=0.35, hovermode="closest")
    figure.update_xaxes(
        showgrid=True, gridcolor=INK["grid"], zeroline=True,
        zerolinecolor=INK["axis"], zerolinewidth=1,
        title={"text": "% change on the previous 7 days",
               "font": {"size": 11, "color": INK["muted"]}},
    )
    figure.update_yaxes(showgrid=False)
    return style_figure(
        figure, "Speed change, latest 7 days against the 7 before",
        height=max(260, 34 * len(latest) + 90),
    )


def add_events(figure: go.Figure, timeline: pd.DataFrame) -> None:
    """Draw downtime as a shaded span and each failure as a dashed line.

    Both go underneath the data. A reading during downtime is still recorded,
    so the trace continues across the span rather than breaking, and the shading
    is what explains why it looks different there.
    """
    if timeline.empty:
        return

    for _, row in timeline.iterrows():
        if row["kind"] == "downtime":
            figure.add_vrect(
                x0=row["start_ts"], x1=row["end_ts"],
                fillcolor="rgba(137,135,129,0.22)", line_width=0, layer="below",
            )
        else:
            figure.add_vline(
                x=row["start_ts"], line_width=2, line_dash="dot",
                line_color=STATUS["critical"], layer="below",
            )


def band_for(machine_id: int, as_of: str, connection) -> dict[str, tuple[float, float]]:
    """Lower and upper band per reading type, for one machine.

    The query returns variances because SQLite may have no ``sqrt()``; the
    square root happens here.
    """
    baseline = queries.machine_baseline(as_of, connection)
    row = baseline[baseline["machine_id"] == machine_id]
    if row.empty:
        return {}

    row = row.iloc[0]
    sigma = CONFIG["band"]["sigma"]
    bands = {}
    for column, _label, _unit, _colour in READING_TYPES:
        mean = row["mean_" + column]
        variance = row["var_" + column]
        if pd.isna(mean) or pd.isna(variance):
            continue
        spread = sigma * queries.standard_deviation(max(float(variance), 0.0))
        bands[column] = (float(mean) - spread, float(mean) + spread)
    return bands


def end_of_day(day: date) -> str:
    return "{}T23:59:59".format(day.isoformat())


def start_of_day(day: date) -> str:
    return "{}T00:00:00".format(day.isoformat())


def date_range_picker(label: str = "Date range") -> tuple[date, date] | None:
    """A date range defaulting to the whole dataset."""
    first, last = dataset_range()
    chosen = st.date_input(label, value=(first, last), min_value=first, max_value=last)
    if not isinstance(chosen, (tuple, list)) or len(chosen) != 2:
        st.info("Pick an end date to see results.")
        return None
    return chosen[0], chosen[1]


# --- pages ----------------------------------------------------------------


def page_overview() -> None:
    connection = connect(DB)
    try:
        first, last = dataset_range()
        what_to_look_for()

        section("Plant state",
                "Hover a machine for its latest reading, or click it to open it.")
        status = queries.plant_status(end_of_day(last), connection)
        if status.empty:
            st.info("No machines in this dataset.")
            return

        event = st.plotly_chart(
            plant_diagram(status, selected=st.session_state.get("machine_id")),
            width="stretch", key="plant_map", on_select="rerun",
        )
        clicked = selected_machine_from(event)
        if clicked is not None:
            # The page selector is created before this point, so its state
            # cannot be written now. Park the request and let main() apply it
            # on the next run, before the radio exists.
            st.session_state["machine_id"] = clicked
            st.session_state["pending_page"] = "Machine detail"
            st.rerun()

        states = status.apply(machine_state, axis=1)
        counts = states.value_counts()
        section("Key figures")
        window = st.radio(
            "Uptime window", CONFIG["kpi"]["uptime_windows"], horizontal=True,
            format_func=lambda days: "{} days".format(days),
        )
        kpi = queries.kpi_summary(end_of_day(last), window, connection)
        if kpi.empty:
            st.info("No data for this dataset yet.")
            return

        row = kpi.iloc[0]
        columns = st.columns(4)
        columns[0].metric(
            "Machines online", int(row["machines_online"]),
            help="Machines with no downtime event open at the last reading.",
        )
        columns[1].metric(
            "Uptime, {} days".format(window),
            "{:.2f}%".format(row["uptime_percent"]),
            help="Machine-hours in the window, less downtime hours inside it. "
                 "An event crossing the edge counts only the part inside.",
        )
        columns[2].metric(
            "Failures, past 7 days", int(row["failures_recent"]),
            help="Recorded failure events, not alerts.",
        )
        columns[3].metric(
            "Open alerts", int(row["open_alerts"]),
            delta="{} machines affected".format(int(counts.get("alert", 0))),
            delta_color="off",
            help="Unresolved alerts across all four rules.",
        )

        section("Open alerts")
        alerts = queries.alerts_list(connection)
        open_alerts = alerts[alerts["resolved"] == 0]
        if open_alerts.empty:
            st.info("No open alerts. Run the alert rules from the Data page.")
        else:
            st.dataframe(
                open_alerts[
                    ["ts", "machine_name", "line_name", "rule", "severity", "message"]
                ],
                hide_index=True, width="stretch",
            )

        section("Lost time", "Downtime over the whole dataset, worst first.")
        downtime = queries.downtime_by_machine(
            start_of_day(first), end_of_day(last), connection
        )
        by_line = queries.downtime_by_line(
            start_of_day(first), end_of_day(last), connection
        )

        left, right = st.columns([3, 2])
        with left:
            if downtime.empty:
                st.info("No downtime recorded for this dataset.")
            else:
                ranked = downtime.sort_values("downtime_hours")
                figure = go.Figure(
                    go.Bar(
                        x=ranked["downtime_hours"], y=ranked["machine_name"],
                        orientation="h",
                        marker={"color": SERIES["blue"],
                                "line": {"color": INK["surface"], "width": 2}},
                        text=["{:.1f} h".format(v) for v in ranked["downtime_hours"]],
                        textposition="outside",
                        textfont={"color": INK["secondary"], "size": 11},
                        customdata=ranked[["line_name", "event_count"]].values,
                        hovertemplate="%{y} · %{customdata[0]}<br>"
                                      "%{x:.2f} hours over %{customdata[1]} "
                                      "event(s)<extra></extra>",
                    )
                )
                figure.update_layout(bargap=0.35, hovermode="closest")
                figure.update_xaxes(
                    showgrid=True, gridcolor=INK["grid"],
                    title={"text": "hours",
                           "font": {"size": 11, "color": INK["muted"]}},
                )
                figure.update_yaxes(showgrid=False)
                st.plotly_chart(
                    style_figure(figure, "Downtime hours by machine",
                                 height=max(240, 34 * len(ranked) + 90)),
                    width="stretch",
                )
        with right:
            st.caption("By production line")
            if by_line.empty:
                st.info("No lines recorded.")
            else:
                st.dataframe(
                    by_line[["line_name", "machines", "event_count",
                             "downtime_hours", "hours_per_machine"]],
                    hide_index=True, width="stretch",
                )
            st.caption("By machine")
            if downtime.empty:
                st.info("None.")
            else:
                st.dataframe(
                    downtime[["machine_name", "line_name", "event_count",
                              "downtime_hours"]],
                    hide_index=True, width="stretch",
                )
    finally:
        connection.close()


def page_machine_detail() -> None:
    connection = connect(DB)
    try:
        first, last = dataset_range()
        machines = queries.machine_baseline(end_of_day(last), connection)
        if machines.empty:
            st.info("No machines in this dataset.")
            return

        names = machines["machine_name"].tolist()
        # A machine clicked on the Overview map arrives through session state,
        # so the picker opens on it rather than resetting to the first machine.
        preset = st.session_state.get("machine_id")
        start_index = 0
        if preset is not None:
            match = machines.index[machines["machine_id"] == preset]
            if len(match):
                start_index = int(machines.index.get_loc(match[0]))

        picker, ranger = st.columns([1, 2])
        with picker:
            chosen_name = st.selectbox("Machine", names, index=start_index)
        row = machines.loc[machines["machine_name"] == chosen_name].iloc[0]
        machine_id = int(row["machine_id"])
        st.session_state["machine_id"] = machine_id

        with ranger:
            chosen = date_range_picker()
        if chosen is None:
            return
        start, end = chosen

        # The locator: the same plant map, with this machine ringed. It answers
        # "where is M-07?" without the reader having to hold the layout in
        # their head between pages.
        status = queries.plant_status(end_of_day(last), connection)
        if not status.empty:
            st.plotly_chart(
                plant_diagram(
                    status, selected=machine_id, height=230,
                    title="{} is on {}, position {} of {}".format(
                        chosen_name, row["line_name"],
                        int(status.loc[status["machine_id"] == machine_id,
                                       "position"].iloc[0]),
                        int(status["position"].max()),
                    ),
                ),
                width="stretch",
            )

        readings = queries.machine_readings(
            machine_id, start_of_day(start), end_of_day(end), connection
        )
        if readings.empty:
            st.info("No readings for {} in that date range.".format(chosen_name))
            return

        abnormal = queries.abnormal_readings(
            machine_id, start_of_day(start), end_of_day(end), connection
        )
        timeline = queries.machine_timeline(
            machine_id, start_of_day(start), end_of_day(end), connection
        )
        bands = band_for(machine_id, end_of_day(end), connection)
        stamps = pd.to_datetime(readings["ts"])

        downtime_events = timeline[timeline["kind"] == "downtime"]
        failure_events = timeline[timeline["kind"] == "failure"]
        section(
            "Readings",
            "{:,} readings in range · {:,} outside the normal band · "
            "{} downtime event(s) shaded · {} failure(s) marked.".format(
                len(readings), 0 if abnormal.empty else len(abnormal),
                len(downtime_events), len(failure_events),
            ),
        )

        for column, label, unit, colour in READING_TYPES:
            figure = go.Figure()

            band = bands.get(column)
            if band:
                lower, upper = band
                figure.add_hrect(
                    y0=lower, y1=upper,
                    fillcolor="rgba(42,120,214,0.09)", line_width=0,
                    annotation_text="normal band",
                    annotation_position="top left",
                    annotation_font={"size": 10, "color": INK["muted"]},
                    layer="below",
                )

            add_events(figure, timeline)

            figure.add_trace(
                go.Scatter(
                    x=stamps, y=readings[column], mode="lines", name=label,
                    line={"color": colour, "width": 2},
                    hovertemplate="%{y:.2f} " + unit + "<extra></extra>",
                )
            )

            flag = "{}_abnormal".format(column.rsplit("_", 1)[0])
            if not abnormal.empty and flag in abnormal.columns:
                marked = abnormal[abnormal[flag] == 1]
                if not marked.empty:
                    figure.add_trace(
                        go.Scatter(
                            x=pd.to_datetime(marked["ts"]), y=marked[column],
                            mode="markers", name="Outside band",
                            marker={"size": 9, "symbol": "x",
                                    "color": STATUS["critical"],
                                    "line": {"color": INK["surface"], "width": 1}},
                            hovertemplate="%{y:.2f} " + unit
                                          + " — outside band<extra></extra>",
                        )
                    )

            figure.update_yaxes(title={"text": unit,
                                       "font": {"size": 11, "color": INK["muted"]}})
            st.plotly_chart(
                style_figure(figure, "{} ({})".format(label, unit)), width="stretch"
            )

        section("Speed, week on week",
                "7-day blocks counted back from the end of the range, not "
                "calendar weeks.")
        blocks = queries.speed_week_over_week(
            start_of_day(start), end_of_day(end), connection
        )
        mine = blocks[blocks["machine_id"] == machine_id]
        if len(mine) < 2:
            st.info("The range is too short to compare two 7-day blocks.")
        else:
            st.plotly_chart(week_blocks_chart(mine, chosen_name), width="stretch")

        section("Downtime and failures")
        left, right = st.columns(2)
        with left:
            st.caption("Downtime events")
            if downtime_events.empty:
                st.info("No downtime for {} in that range.".format(chosen_name))
            else:
                st.dataframe(
                    downtime_events[["start_ts", "end_ts", "label", "hours"]],
                    hide_index=True, width="stretch",
                )
        with right:
            st.caption("Failures")
            if failure_events.empty:
                st.info("No failures for {} in that range.".format(chosen_name))
            else:
                st.dataframe(
                    failure_events[["start_ts", "label"]],
                    hide_index=True, width="stretch",
                )
    finally:
        connection.close()


def page_patterns() -> None:
    connection = connect(DB)
    try:
        chosen = date_range_picker()
        if chosen is None:
            return
        start, end = chosen

        section(
            "Before each failure",
            "Average of the 24 hours before the failure, against that machine's "
            "own 14-day normal ending where those 24 hours start.",
        )
        before = queries.before_failure(
            start_of_day(start), end_of_day(end), connection
        )
        if before.empty:
            st.info("No failures in that date range.")
        else:
            labels = before["machine_name"] + " · " + before["failure_type"]
            figure = go.Figure()
            figure.add_trace(
                go.Bar(
                    x=labels, y=before["baseline_vibration_mm_s"], name="Normal",
                    marker={"color": SERIES["blue"],
                            "line": {"color": INK["surface"], "width": 2}},
                    hovertemplate="Normal %{y:.2f} mm/s<extra></extra>",
                )
            )
            figure.add_trace(
                go.Bar(
                    x=labels, y=before["mean_vibration_before"],
                    name="24 h before failure",
                    marker={"color": SERIES["orange"],
                            "line": {"color": INK["surface"], "width": 2}},
                    hovertemplate="Before %{y:.2f} mm/s<extra></extra>",
                )
            )
            figure.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
            figure.update_yaxes(title={"text": "mm/s",
                                       "font": {"size": 11, "color": INK["muted"]}})
            st.plotly_chart(
                style_figure(figure, "Vibration before failure against normal",
                             height=340),
                width="stretch",
            )

            st.caption("Every reading type, for each failure")
            st.dataframe(
                pd.DataFrame(
                    {
                        "machine": before["machine_name"],
                        "failure": before["failure_ts"],
                        "type": before["failure_type"],
                        "temp before": before["mean_temperature_before"].round(2),
                        "temp normal": before["baseline_temperature_c"].round(2),
                        "speed before": before["mean_speed_before"].round(1),
                        "speed normal": before["baseline_speed_rpm"].round(1),
                        "vibration before": before["mean_vibration_before"].round(3),
                        "vibration normal": before["baseline_vibration_mm_s"].round(3),
                    }
                ),
                hide_index=True, width="stretch",
            )

        section("Speed, week on week",
                "Latest 7-day block against the one before, every machine. The "
                "dotted line is the threshold the speed-drop rule fires at.")
        blocks = queries.speed_week_over_week(
            start_of_day(start), end_of_day(end), connection
        )
        change = week_change_chart(blocks) if not blocks.empty else go.Figure()
        if not blocks.empty and change.data:
            st.plotly_chart(change, width="stretch")
        else:
            st.info("The range is too short to compare two 7-day blocks.")

        section("Failures by type")
        by_type = queries.failures_by_type(
            start_of_day(start), end_of_day(end), connection
        )
        if by_type.empty:
            st.info("No failures in that date range.")
        else:
            totals = by_type.groupby("failure_type", as_index=False)[
                "failure_count"
            ].sum().sort_values("failure_count", ascending=False)
            figure = go.Figure(
                go.Bar(
                    x=totals["failure_type"], y=totals["failure_count"],
                    marker={"color": SERIES["blue"],
                            "line": {"color": INK["surface"], "width": 2}},
                    text=totals["failure_count"], textposition="outside",
                    textfont={"color": INK["secondary"], "size": 12},
                    hovertemplate="%{x}: %{y}<extra></extra>",
                )
            )
            figure.update_layout(bargap=0.45, hovermode="closest")
            st.plotly_chart(
                style_figure(figure, "Failures by type", height=300), width="stretch"
            )
            st.dataframe(
                by_type[["machine_name", "line_name", "failure_type",
                         "failure_count"]],
                hide_index=True, width="stretch",
            )
    finally:
        connection.close()


def page_alerts() -> None:
    connection = connect(DB)
    try:
        alerts = queries.alerts_list(connection)
        if alerts.empty:
            st.info("No alerts yet. Generate a dataset or run the alert rules.")
            return

        severities = sorted(alerts["severity"].unique())
        machines = sorted(alerts["machine_name"].unique())
        left, right, third = st.columns([1, 1, 1])
        with left:
            chosen_severity = st.multiselect("Severity", severities,
                                             default=severities)
        with right:
            chosen_machines = st.multiselect("Machine", machines, default=machines)
        with third:
            hide_resolved = st.checkbox("Hide resolved", value=False)

        shown = alerts[
            alerts["severity"].isin(chosen_severity)
            & alerts["machine_name"].isin(chosen_machines)
        ]
        if hide_resolved:
            shown = shown[shown["resolved"] == 0]

        if shown.empty:
            st.info("No alerts match those filters.")
            return

        section("Alerts", "{} shown, {} still open.".format(
            len(shown), int((shown["resolved"] == 0).sum())
        ))
        st.dataframe(
            shown[["alert_id", "ts", "machine_name", "line_name", "rule",
                   "severity", "message", "resolved"]],
            hide_index=True, width="stretch",
        )

        open_ids = shown.loc[shown["resolved"] == 0, "alert_id"].tolist()
        if not open_ids:
            st.info("Nothing left to resolve in this view.")
            return

        to_resolve = st.selectbox("Alert to mark resolved", open_ids)
        if st.button("Mark resolved"):
            resolve_alert(int(to_resolve), connection)
            st.success("Alert {} resolved.".format(to_resolve))
            st.rerun()
    finally:
        connection.close()


def page_data() -> None:
    generation = CONFIG["generation"]

    st.markdown(
        '<div class="pm-meta">Generating writes CSVs to <code>data/</code>, '
        "rebuilds the database from them, and runs the four alert rules. "
        "The same seed and inputs always produce the same dataset.</div>",
        unsafe_allow_html=True,
    )

    # Order matters: the dashboard tests address these by position, machines
    # first and days second.
    left, middle, right = st.columns(3)
    with left:
        machines = st.number_input(
            "Machines", value=int(generation["machines"]), step=1, format="%d",
            help="{}-{}".format(generation["min_machines"],
                                generation["max_machines"]),
        )
    with middle:
        days = st.number_input(
            "Days", value=int(generation["days"]), step=1, format="%d",
            help="{}-{}".format(generation["min_days"], generation["max_days"]),
        )
    with right:
        seed = st.number_input(
            "Seed", value=int(generation["seed"]), step=1, format="%d",
            help="Same seed, same data.",
        )

    section("Planted problems", "Switch one off and the others are unchanged.")
    problems = {
        problem: st.checkbox(
            PROBLEM_LABELS[problem], value=bool(generation["problems"][problem])
        )
        for problem in PROBLEM_NAMES
    }

    st.write("")
    if not st.button("Generate", type="primary"):
        return

    progress = st.progress(0.0, text="Generating readings...")
    try:
        result = generate(
            seed=int(seed), machines=int(machines), days=int(days),
            problems=problems, data_root=DATA_ROOT, config=CONFIG,
        )
    except SettingsError as exc:
        progress.empty()
        # Nothing has been written at this point: the settings are validated
        # before any file is created.
        st.error(str(exc))
        return

    progress.progress(0.45, text="Loading into the database...")
    load_result = load_dataset(
        result.settings.name, data_root=DATA_ROOT, db_path=DB, config=CONFIG,
    )

    progress.progress(0.8, text="Running the alert rules...")
    connection = connect(DB)
    try:
        alert_result = run_alerts(connection, config=CONFIG)
    finally:
        connection.close()

    progress.progress(1.0, text="Done.")
    # A new dataset renumbers the machines, so a machine remembered from the
    # old one would point at the wrong row.
    st.session_state.pop("machine_id", None)
    st.success(
        "Loaded {:,} readings for {} machines. {} alerts raised.".format(
            load_result.loaded.get("readings", 0), result.settings.machines,
            alert_result.created,
        )
    )
    if load_result.total_rejected:
        st.warning("{} rows were skipped as invalid.".format(
            load_result.total_rejected
        ))
    for note in result.skipped:
        st.warning("Skipped {}".format(note))


# --- entry point ----------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Plant Monitor", layout="wide", page_icon="•")

    # A click on the Overview map asks for a different page. Streamlit refuses
    # to let a widget's state be written once that widget exists, so the
    # request is applied here, before the radio is created.
    if "pending_page" in st.session_state:
        st.session_state["page"] = st.session_state.pop("pending_page")

    # The page selector must stay the only radio in the sidebar: the dashboard
    # tests address it by position.
    page = st.sidebar.radio("Page", PAGES, key="page")
    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Simulated plant data. Faults are planted on purpose so the SQL has a "
        "known answer to find."
    )
    row = settings_row()
    if row is not None:
        st.sidebar.caption("Loaded: **{}**".format(row["name"]))

    show_header(page)

    if page == "Data":
        page_data()
        return

    if not database_ready() or settings_row() is None:
        st.warning("Generate a dataset on the Data page first.")
        return

    if page == "Overview":
        page_overview()
    elif page == "Machine detail":
        page_machine_detail()
    elif page == "Patterns":
        page_patterns()
    elif page == "Alerts":
        page_alerts()


main()
