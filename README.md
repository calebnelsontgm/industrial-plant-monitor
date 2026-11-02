# Industrial Plant Monitor

Equipment monitoring for a factory floor. Twelve machines on two production
lines each report temperature, speed and vibration every fifteen minutes. The
app computes uptime and downtime per machine, builds a statistical control band
for each machine from its own recent history, flags the readings that fall
outside it, and raises alerts for sustained overheating, rising vibration ahead
of a failure, and week-over-week speed loss. A machine-learning model estimates
each machine's chance of failing in the next 24 hours. Everything is presented
in a dashboard.

**The data is simulated.** There is no real plant behind this. A generator
fabricates ninety days of readings — about 104,000 of them — in the shape a
plant historian records them: one machine, one timestamp, one row, continuously,
including while a machine is down. Faults are then deliberately planted into
that data and the SQL has to find them again.

That is the point of the project. The planted faults are a known answer key, so
every query can be checked against the right answer instead of eyeballed, and
every alert rule can be shown to fire when it should and stay quiet when it
should not.

Everything is local: Python, SQLite, and a browser.

## How it fits together

```
config.toml          every threshold, default and limit
      |
generate.py          fabricates readings, plants the faults
      |
data/<name>/*.csv    one CSV per table, plus dataset.json
      |
load.py              validates every row, builds a fresh database
      |
data/plant.db        seven tables, created from sql/schema.sql
      |
sql/*.sql            the analysis: thirteen query files
      |              (db.py reads them, queries.py wraps them)
      |
alerts.py            four rules, written to the alerts table
      |
predict.py           failure model, trained on its own dataset in
      |              data/training.db, saved to data/model.joblib
      |
app.py               the dashboard
```

The same functions back both the command line and the dashboard's Data page, so
generating from either route does exactly the same thing.

## Getting set up

Python 3.11 or newer is required, because the config file is read with the
standard library's `tomllib`.

```bash
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

On macOS or Linux the interpreter is at `.venv/bin/python` instead.

## Running it

```bash
.venv/Scripts/python -m plantmonitor generate     # write CSVs to data/<name>/
.venv/Scripts/python -m plantmonitor load <name>  # build data/plant.db
.venv/Scripts/python -m plantmonitor alerts       # apply the four alert rules
.venv/Scripts/python -m plantmonitor train        # build the failure model (its own dataset)
.venv/Scripts/python -m plantmonitor dashboard    # open the dashboard
```

`generate` prints the dataset name it chose, which is what `load` wants. With
the defaults that is `seed42_12m_90d`.

Or skip all of it and use the dashboard's **Data** page, which does the same
same steps behind a progress bar.

Every input to `generate` has a flag — `--seed`, `--machines`, `--days`,
`--start`, `--name`, `--failures` for an exact number of pre-failure events
instead of the random 12-16, and `--no-overheat` and friends to switch off a
planted problem. Anything not given falls back to `config.toml`. The same seed and
inputs always produce byte-identical CSVs.

### Writing your own queries

There is a scratchpad for trying SQL against the loaded database. Put a query
in [`scratch.sql`](scratch.sql), then:

```bash
.venv/Scripts/python -m plantmonitor sql
```

It prints the first 50 rows, or all of them with `--all`. Several statements
separated by `;` each get their own result table. A mistake gives you a plain
error message rather than a stack trace.

```sql
SELECT m.name, COUNT(*) AS readings, ROUND(AVG(r.temperature_c), 1) AS avg_temp
FROM readings r
JOIN machines m ON m.id = r.machine_id
GROUP BY m.name
ORDER BY avg_temp DESC;
```

## The database

Seven tables. Timestamps are stored as text in ISO 8601 form, like
`2026-10-01T23:45:00`, which sorts in the same order as it reads — so
`WHERE ts > '2026-09-01'` works without any date functions.

| Table | What it holds |
| --- | --- |
| `lines` | The two production lines |
| `machines` | `M-01` through `M-12`, each on a line |
| `readings` | Temperature, speed and vibration every 15 minutes |
| `downtime_events` | When a machine stopped, for how long, and why |
| `failures` | Overheat, bearing and power failures |
| `alerts` | What the alert rules found |
| `dataset_meta` | The settings that produced the loaded dataset |

`readings` carries an index on `(machine_id, ts)`. Every analysis query filters
by machine and scans a time range, so one composite index in that order serves
both the filter and the ordering without a separate sort.

The loader deletes and rebuilds the database from `schema.sql` on every run,
which is why loading the same dataset twice cannot produce duplicates. Each row
is validated first — no missing values, readings inside the ranges in
`config.toml`, a known machine, `end_ts` after `start_ts` — and a row that fails
is skipped and counted under the first check it failed, so a bad dataset
produces a summary rather than a traceback.

## What is planted in the data

Four problems, each one switchable from the Data page or with a `--no-...` flag
on `generate`:

| Problem | Where | What it looks like |
| --- | --- | --- |
| Overheating | M-07 | Temperature climbs steadily over the final 4 days |
| Slowdown | M-03 | Speed drops 12% exactly 7 days before the end |
| Repeated downtime | M-11 | Three downtime events in the final 30 days |
| Rising vibration | Random machines | 12-16 failures, each after 6-12 hours of climbing vibration |

Ask for fewer machines than a named one needs and that problem is skipped and
reported, rather than failing.

Each problem draws from its own random stream, derived from the seed by a fixed
offset. Switching one off therefore cannot shift the numbers behind any other,
which is what makes an on/off comparison of the same machine meaningful: with
overheating turned off, M-07's baseline data is unchanged rather than merely
similar.

## The queries

All SQL lives in [`sql/`](sql/) rather than inside Python strings. Each file
opens with a comment block giving the question it answers, its parameters in
binding order, and its output columns.
[`plantmonitor/queries.py`](plantmonitor/queries.py) has one Python function per
file, named after it, and binds the parameters positionally.

| File | Answers |
| --- | --- |
| `schema.sql` | The seven table definitions |
| `kpi_summary.sql` | Machines online, uptime %, recent failures, open alerts |
| `machine_baseline.sql` | Mean and variance per machine over 14 days |
| `abnormal_readings.sql` | Readings outside a machine's normal band |
| `downtime_by_machine.sql` | Downtime hours and event counts, worst first |
| `downtime_by_line.sql` | The same rolled up to the production line |
| `failures_by_type.sql` | Failure counts by type and machine |
| `speed_week_over_week.sql` | Average speed per 7-day block, with the change |
| `before_failure.sql` | The 24 hours before each failure, against normal |
| `alert_band_breaches.sql` | Readings above the upper band, for the alert engine |
| `alert_downtime_window.sql` | Recent downtime events, numbered per machine |
| `dataset_settings.sql` | The loaded dataset's settings |
| `alerts_list.sql` | Every alert with its machine name |
| `machine_readings.sql` | One machine's readings over a date range |
| `machine_timeline.sql` | One machine's downtime events and failures, as one list |
| `plant_status.sql` | Every machine's line, running state and last reading |
| `prediction_readings.sql` | Every machine's raw readings, for the failure model |
| `prediction_failures.sql` | Every failure's timestamp, for labelling the model's hours |

A few conventions run through all of them.

**Parameters arrive in a CTE.** SQLite cannot reuse a positional `?` by name, so
each query opens with `WITH params AS (SELECT ? AS start_ts, ? AS end_ts)` and
everything downstream refers to them by name. Each `?` then appears exactly once
however many times its value is needed.

**Date ranges include their final day.** A `bounds` CTE turns whatever was
passed in into `date(end_ts) || 'T23:59:59'`. That strips any time part and
pushes the bound to the end of the day, so a range ending `2026-09-20` covers
that whole day rather than stopping at midnight. Because `bounds` is always a
single row, it is cross-joined onto the table being scanned, which attaches the
computed bounds to every row without changing the row count.

**Downtime is clipped to the window.** An event that starts before the window or
ends after it should only contribute the hours actually inside, so the hours
come from `julianday(MIN(end_ts, window_end)) - julianday(MAX(start_ts,
window_start))`. With two arguments `MIN` and `MAX` are scalar functions rather
than aggregates, so these sit inside the surrounding `SUM`.

**Variance, not standard deviation.** SQLite has no standard deviation function
and may not even have `sqrt()`, so the queries compute variance as
`AVG(x*x) - AVG(x)*AVG(x)` and leave the square root to Python. To test whether
a value sits more than three standard deviations from the mean without a square
root, both sides get squared: `(x - mean)*(x - mean) > 9 * variance`.

**`strftime`, not `datetime`.** SQLite's `datetime()` puts a space between the
date and the time, while these timestamps use a `T`. Since they are compared as
text and a space sorts before `T`, mixing the two shifts every comparison by up
to a day. `strftime('%Y-%m-%dT%H:%M:%S', ...)` keeps the format consistent.

**Weeks are 7-day blocks, not calendar weeks.** `speed_week_over_week.sql`
assigns each reading a block index counted back from the latest reading in the
range, then uses `LAG` over `PARTITION BY machine_id ORDER BY block_index DESC`
to compare each block with the one before it. Calendar weeks would straddle a
step change and dilute it; blocks counted back from the end do not.

## The alert rules

| Rule | Fires when | Severity |
| --- | --- | --- |
| Sustained heat | Temperature above the upper band for 6+ consecutive hours | High |
| Speed drop | Average speed down 10%+ on the previous 7-day block | Medium |
| Repeated downtime | 2+ downtime events within 30 days | Medium |
| Vibration rising | Vibration above the upper band for 3+ consecutive hours | Medium |

The work is split: SQL finds which readings breach the band, and Python groups
those timestamps into consecutive runs and checks whether a run is long enough
to fire. Two readings belong to the same run when they are exactly one interval
apart, so a problem that stops and restarts raises two alerts rather than one
long one, and a fourteen-hour run still raises exactly one.

A rule judges only the most recent `evaluation_days` and measures normal from
the 14 days *ending* where that period begins. The gap matters: without it a
problem lasting several days inflates its own standard deviation and hides
inside its own band. A four-day temperature climb widens M-07's band from 80 °C
to 102 °C and escapes detection entirely.

Re-running the engine never duplicates an alert, and no Python does that work.
Every alert is timestamped at the moment that triggered it — the first reading
of a run, or the start of the event that reached the threshold — so the same
data always produces the same timestamp. A `UNIQUE (machine_id, rule, ts)`
constraint and `INSERT OR IGNORE` then do the rest.

## The dashboard

Six pages, chosen from the sidebar. Every page shows the loaded dataset's
settings at the top, read from `dataset_meta`.

- **Overview** - a diagram of the plant with every machine coloured by its
  current state, then KPI cards, open alerts, and lost time by line and by
  machine
- **Machine detail** - the plant map again with this machine ringed, so its
  place on the line is never in doubt, then one chart per reading type with the
  normal band shaded, out-of-band points crossed, downtime shaded and failures
  marked, and that machine's speed per 7-day block
- **Patterns** - conditions in the 24 hours before each failure against that
  machine's normal, every machine's week-on-week speed change against the
  threshold the alert rule fires at, and failures by type
- **Alerts** - every alert, filterable by severity and machine, with a
  mark-resolved button
- **Failure risk** - a replay of the hours before each test-period failure:
  drag a slider through time and watch the model's risk climb, and cross the
  warning line, before the failure happens. Then the test metrics, and each
  machine's risk at the final hour
- **Data** - generate a new dataset and load it

Overview opens with a panel naming the faults planted in the loaded dataset
and which page shows each one, so a first run has somewhere to start rather
than being five pages of numbers with no entry point. Clicking a machine on the
map opens it on Machine detail, which is also how the two pages stay tied
together: the map is the navigation, not an ornament.

Any view with no data shows a short message rather than an error. Out-of-range
inputs on the Data page are rejected before anything is written, so a bad
request generates nothing.

Colour is defined once, at the top of `app.py`. Each reading type keeps the
same hue on every page, every figure passes through one `style_figure()` helper
so the charts match rather than approximately match, and the machine-state
colours are reserved - never reused for a data series - and always paired with
a marker shape and a label, so state never depends on colour alone. The
categorical hues were checked for colour-vision separation as a set.
[`.streamlit/config.toml`](.streamlit/config.toml) pins the theme to light so
the app chrome and the chart surface agree.

<!-- Screenshots go here. -->

## Failure prediction

The alert rules say what is wrong *now*. The failure model tries to say what
will go wrong *next*: for every machine, every hour, how likely is it to fail
within the next 24 hours?

**What it looks at.** Every hour, for each machine, it summarises the last 6
hours and the last 12 hours of temperature, speed and vibration: the average,
the peak, and the trend (the slope of a straight line fitted through the hourly
averages, i.e. how fast the reading is climbing or falling). That is 18 numbers
per machine per hour. The trend matters most, because a machine heading for
failure is one whose vibration is *rising*, whatever its usual level.

Every one of those numbers is measured against **that machine's own normal**,
in standard deviations: how far this window's average and peak sit from the
machine's baseline mean (3.0 is the top of its normal band), and how many
standard deviations an hour it is climbing. The baseline is the same one the
alert rules use, the machine's mean and spread over 14 days of readings, and it
ends 24 hours before the hour being scored, so a problem that is building up
cannot drag its own baseline up with it and hide. Each machine's first 15 days
have no full baseline yet, so they are left out.

**What it learns from.** Each hour is labelled "yes" if that machine actually
failed within the following 24 hours, otherwise "no". The model is a random
forest: a few hundred decision trees, each trained on a random sample of the
hours, that vote. Failure hours are about 1% of the data, so the "yes" hours
are given extra weight. Without that, the model scores best by always saying
"no".

**How it is tested honestly.** The hours are split by time, not at random: the
earlier 70% train the model and the latest 30% test it. A random split would
cheat, because 14:00 and 15:00 share most of their 12-hour window, so the model
would be tested on hours it had nearly seen. The training hours also stop a day
short of the test period. A training hour's label looks 24 hours ahead, so the
last day before the split would otherwise carry answers about test-period
failures. The final day of data is not used for scoring either, since a failure
just after the data ends would be invisible.

**Its own dataset.** The default 90-day dataset leaves only 3 failures in the
test period, too few to measure anything. So the model trains and is tested on
a separate dataset, set in `[predict.training]` in `config.toml`: the same 12
machines and seed, 180 days, and exactly 70 planted failures. `train` generates
it into `data/seed42_12m_180d_70f/` and loads it into its own database,
`data/training.db`, leaving the dashboard's `data/plant.db` alone. Loading or
generating a dataset does not retrain the model; only `train` does. The
dashboard scores the loaded dataset with it, and replays failures from the
training database, which is where the test failures are.

**How to read its scores.** On the time-based test split (the last 30% of
the usable hours, 14,172 machine-hours over about 49 days) the model was
trained on 42 failures and tested on 20:

| Measure | Result |
| --- | --- |
| Failures flagged in advance | 20 of 20 |
| Average warning | 7.4 hours before the failure (4.0 to 10.2) |
| Precision | 0.91: 162 of the 178 flagged hours were within 24 hours of a failure |
| Recall | 0.36: of the 454 hours within 24 hours of a failure, it flagged 162 |
| False alarms | 16 flagged hours with no failure on that machine within 24 hours |

Recall is low and that is expected. Every hour in the 24 before a failure
counts as a "failure" hour, but the warning sign only starts 6-12 hours out, so
the earlier hours look completely normal and no model could flag them.
"Failures caught, and how early" is the number that matters.

Half of the false alarms need a footnote: 8 of the 16 are the hour a machine
actually failed in, or the hour after. A label only looks at failures *after*
its hour ends, so those hours score as false alarms even though the machine
really was failing.

**Why the features are relative.** The first version fed the model raw
averages. Each machine has its own typical speed and temperature, so those
also told the model *which machine* it was looking at, and machines that had
failed often in training looked risky all the time. To measure that, a
classifier was trained to guess the machine from the features alone. On raw
features it was right 99.9% of the time; on the relative features, 8.2%,
which is chance for 12 machines. Retrained on the same data with the same
split, the change cut false alarms from 60 to 16 (M-10 alone had 29 of the
60), raised precision from 0.75 to 0.91, and still caught every test failure.
The test set is slightly different (20 failures rather than 22) because the
15-day warm-up moves the split point about four days later.

The model, its test metrics and its score for every test hour are saved to
`data/model.joblib`, `data/model_metrics.json` and `data/model_test_risk.csv`.

**Seeing it work.** The dashboard's replay plays back those saved test-hour
scores. At each slider position it shows only what existed at that moment:
vibration up to then (the rest is a faint grey line still to come) and the
risk the model had reported so far. A score for an hour is plotted at the end
of that hour, because that is when its last reading arrives. Only test-period
failures can be replayed: the model saw the training hours' answers, so
replaying those would make it look better than it is. All of
its settings are in the `[predict]` section of `config.toml`.

## Settings

Every threshold, default and limit is in [`config.toml`](config.toml): the
generator's defaults and limits, the shape of each planted problem, the band
width, the four alert rules, the loader's validation ranges, and the dashboard's
uptime windows, and the failure model's windows, split and threshold. Nothing is
hard-coded in Python or SQL.

## Tests

```bash
.venv/Scripts/python -m pytest
```

Run it as `python -m pytest`, not bare `pytest`, so the project root lands on
`sys.path` and `import plantmonitor` resolves.

Eighty-three tests, covering the schema, the generator's reproducibility, planted
problems and failure-count option, the loader's row validation, every query against a
purpose-built fixture, all four alert rules, the failure model (no overlap
between training and test hours, most test failures caught, one risk between
0 and 1 per machine, a saved score for every test hour, training on its own
dataset without touching `plant.db`, features that are identical for two
machines differing only in level and spread, and that never look ahead), and
the dashboard through
`streamlit.testing.v1.AppTest`. Two are timing checks: the full dataset must
load in under ten seconds and every dashboard query must return in under one.

[`tests/fixtures.py`](tests/fixtures.py) is worth a look if you add tests. Its
readings follow a 24-hour sine wave rather than random noise, which makes the
expected answers exact: over any whole number of days the wave averages to
zero, so a machine's mean is precisely its base value and its variance is
precisely `amplitude² / 2`. Three standard deviations of that wave is wider
than the wave's own peak, so no ordinary reading can breach the band by
accident and "exactly the planted outliers" is a precise expectation rather
than a statistical one.

Each fixture machine also carries exactly one job — a clean control, a 12% step
down, a 5% step too small to fire, a seven-hour heat run, a fourteen-hour one,
and three one-hour spikes too short to count — so an assertion that one rule
fired once is never contaminated by another planted condition.

## Layout

```
├── .streamlit/
│   └── config.toml      the dashboard theme
├── CLAUDE.md            the build brief
├── README.md
├── requirements.txt
├── config.toml          every threshold, default and limit
├── scratch.sql          scratchpad for trying SQL
├── app.py               the Streamlit dashboard
├── sql/                 schema and every query
├── plantmonitor/
│   ├── __init__.py      paths and the config reader
│   ├── __main__.py      the command line
│   ├── db.py            connect, run_script, query
│   ├── generate.py      dataset generation
│   ├── load.py          CSV validation and loading
│   ├── queries.py       one function per .sql file
│   ├── alerts.py        the four alert rules
│   └── predict.py       the failure model
├── data/                datasets and plant.db (git-ignored)
└── tests/
```

Not built yet: uploading real datasets, column mapping, deployment.
