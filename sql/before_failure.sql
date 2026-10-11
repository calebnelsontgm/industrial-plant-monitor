-- Conditions in the 24 hours before each failure, against normal.
--
-- QUESTION
-- For each failure in a date range, what were that machine's average readings
-- in the 24 hours beforehand, and how do they compare with that machine's
-- normal?
--
-- PARAMETERS (bind in this order)
--   1. start_ts         text     range start, inclusive
--   2. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   failure_id                 integer
--   machine_id                 integer
--   machine_name               text
--   failure_ts                 text
--   failure_type               text
--   mean_temperature_before    real: average over the prior 24 hours
--   mean_speed_before          real
--   mean_vibration_before      real
--   baseline_temperature_c     real: that machine's band mean
--   baseline_speed_rpm         real
--   baseline_vibration_mm_s    real
--
-- One row per failure, ordered by failure_ts.

WITH params AS (
    SELECT ? AS start_ts, ? AS end_ts
),

bounds AS (
    SELECT start_ts, date(end_ts) || 'T23:59:59' AS end_bound
    FROM params
),

scoped_failures AS (
    SELECT f.id, f.machine_id, f.ts, f.failure_type
    FROM failures f
    CROSS JOIN bounds b
    WHERE f.ts >= b.start_ts
      AND f.ts <= b.end_bound
),

-- Two windows per failure. The run-up is the 24 hours leading to it. The
-- baseline is the 14 days BEFORE that run-up, deliberately stopping where the
-- run-up starts: if the baseline overlapped the run-up, the rising readings
-- would pull "normal" up towards themselves and the comparison would flatten
-- out the very thing it is meant to show.
windows AS (
    SELECT
        id,
        machine_id,
        ts,
        failure_type,
        strftime('%Y-%m-%dT%H:%M:%S', ts, '-24 hours') AS run_up_start,
        strftime('%Y-%m-%dT%H:%M:%S', ts, '-24 hours', '-14 days')
            AS baseline_start
    FROM scoped_failures
)

SELECT
    w.id AS failure_id,
    w.machine_id,
    m.name AS machine_name,
    w.ts AS failure_ts,
    w.failure_type,

    -- Correlated subqueries: each one runs against this failure's machine and
    -- this failure's window. A plain JOIN cannot do it, because every failure
    -- needs a different slice of readings.
    (SELECT AVG(r.temperature_c) FROM readings r
      WHERE r.machine_id = w.machine_id
        AND r.ts > w.run_up_start AND r.ts <= w.ts) AS mean_temperature_before,
    (SELECT AVG(r.speed_rpm) FROM readings r
      WHERE r.machine_id = w.machine_id
        AND r.ts > w.run_up_start AND r.ts <= w.ts) AS mean_speed_before,
    (SELECT AVG(r.vibration_mm_s) FROM readings r
      WHERE r.machine_id = w.machine_id
        AND r.ts > w.run_up_start AND r.ts <= w.ts) AS mean_vibration_before,

    (SELECT AVG(r.temperature_c) FROM readings r
      WHERE r.machine_id = w.machine_id
        AND r.ts > w.baseline_start
        AND r.ts <= w.run_up_start) AS baseline_temperature_c,
    (SELECT AVG(r.speed_rpm) FROM readings r
      WHERE r.machine_id = w.machine_id
        AND r.ts > w.baseline_start
        AND r.ts <= w.run_up_start) AS baseline_speed_rpm,
    (SELECT AVG(r.vibration_mm_s) FROM readings r
      WHERE r.machine_id = w.machine_id
        AND r.ts > w.baseline_start
        AND r.ts <= w.run_up_start) AS baseline_vibration_mm_s

FROM windows w
JOIN machines m ON m.id = w.machine_id
ORDER BY w.ts;
