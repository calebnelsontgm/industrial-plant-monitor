-- KPI summary for the Overview page.
--
-- QUESTION
-- How many machines are online, what is uptime % over the chosen window,
-- how many failures happened in the past 7 days, and how many alerts are
-- unresolved?
--
-- PARAMETERS (bind in this order)
--   1. as_of            text     ISO timestamp the figures are measured at
--   2. window_days      integer  uptime window: 7 or 30
--
-- OUTPUT COLUMNS
--   machines_online            integer: machines with no downtime covering as_of
--   uptime_percent             real
--   failures_recent            integer: failures in the 7 days ending at as_of
--   open_alerts                integer: alerts with resolved = 0
--
-- Returns exactly one row.

-- Each parameter is bound once here, then referred to by name below. That
-- keeps the number of ? placeholders equal to the number of real inputs,
-- however many times a value gets used.
WITH params AS (
    SELECT ? AS as_of, ? AS window_days
),

-- strftime rebuilds the timestamp with a 'T' between the date and the time.
-- SQLite's own date functions return a space there instead, and since these
-- timestamps are compared as text, a space would sort before 'T' and shift
-- every comparison by up to a day.
window_bounds AS (
    SELECT
        as_of,
        window_days,
        strftime('%Y-%m-%dT%H:%M:%S', as_of, '-' || window_days || ' days')
            AS window_start,
        strftime('%Y-%m-%dT%H:%M:%S', as_of, '-7 days') AS failures_start
    FROM params
),

-- A machine counts as online unless a downtime event is in progress at as_of.
online AS (
    SELECT COUNT(*) AS machines_online
    FROM machines m, window_bounds w
    WHERE NOT EXISTS (
        SELECT 1
        FROM downtime_events d
        WHERE d.machine_id = m.id
          AND d.start_ts <= w.as_of
          AND d.end_ts > w.as_of
    )
),

-- Total hours the plant could have run: every machine, every hour of the
-- window. This is the denominator of uptime %.
capacity AS (
    SELECT (SELECT COUNT(*) FROM machines) * w.window_days * 24.0 AS machine_hours
    FROM window_bounds w
),

-- Downtime hours that fall inside the window. An event starting before the
-- window or ending after as_of is clipped to the window edge, so only the
-- hours actually inside it are subtracted. MIN and MAX with two arguments are
-- the scalar versions, not the aggregates.
lost AS (
    SELECT COALESCE(SUM(
        (julianday(MIN(d.end_ts, w.as_of))
         - julianday(MAX(d.start_ts, w.window_start))) * 24.0
    ), 0.0) AS downtime_hours
    FROM downtime_events d, window_bounds w
    WHERE d.start_ts <= w.as_of
      AND d.end_ts >= w.window_start
),

recent_failures AS (
    SELECT COUNT(*) AS failures_recent
    FROM failures f, window_bounds w
    WHERE f.ts > w.failures_start
      AND f.ts <= w.as_of
),

unresolved AS (
    SELECT COUNT(*) AS open_alerts
    FROM alerts
    WHERE resolved = 0
)

SELECT
    online.machines_online,
    ROUND(
        (capacity.machine_hours - lost.downtime_hours)
        / capacity.machine_hours * 100.0,
        2
    ) AS uptime_percent,
    recent_failures.failures_recent,
    unresolved.open_alerts
FROM online, capacity, lost, recent_failures, unresolved;
