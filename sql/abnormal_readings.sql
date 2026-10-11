-- Readings outside a machine's normal band.
--
-- QUESTION
-- For one machine over a date range, which readings fall outside that
-- machine's mean +/- 3 standard deviations?
--
-- PARAMETERS (bind in this order)
--   1. machine_id       integer  the machine to examine
--   2. start_ts         text     range start, inclusive
--   3. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   reading_id                 integer
--   ts                         text
--   temperature_c              real
--   speed_rpm                  real
--   vibration_mm_s             real
--   temperature_abnormal       integer: 1 if outside the band, else 0
--   speed_abnormal             integer
--   vibration_abnormal         integer
--
-- Only rows where at least one flag is 1, ordered by ts.

WITH params AS (
    SELECT ? AS machine_id, ? AS start_ts, ? AS end_ts
),

-- date(end_ts) strips any time part, so an end of either '2026-09-20' or
-- '2026-09-20T14:00:00' becomes the same end-of-day bound. That is what makes
-- a date range inclusive of its final day.
bounds AS (
    SELECT
        machine_id,
        start_ts,
        date(end_ts) || 'T23:59:59' AS end_bound,
        strftime(
            '%Y-%m-%dT%H:%M:%S', date(end_ts) || 'T23:59:59', '-14 days'
        ) AS band_start
    FROM params
),

-- The band: the same 14-day mean and variance as machine_baseline.sql, for
-- this one machine.
band AS (
    SELECT
        AVG(r.temperature_c) AS mean_temperature,
        AVG(r.temperature_c * r.temperature_c)
            - AVG(r.temperature_c) * AVG(r.temperature_c) AS var_temperature,
        AVG(r.speed_rpm) AS mean_speed,
        AVG(r.speed_rpm * r.speed_rpm)
            - AVG(r.speed_rpm) * AVG(r.speed_rpm) AS var_speed,
        AVG(r.vibration_mm_s) AS mean_vibration,
        AVG(r.vibration_mm_s * r.vibration_mm_s)
            - AVG(r.vibration_mm_s) * AVG(r.vibration_mm_s) AS var_vibration
    FROM readings r, bounds b
    WHERE r.machine_id = b.machine_id
      AND r.ts > b.band_start
      AND r.ts <= b.end_bound
),

-- Flag each reading in the range. "Outside 3 standard deviations" is tested
-- without a square root: squaring both sides of |x - mean| > 3 * sd gives
-- (x - mean)^2 > 9 * variance, and the variance is what SQL already has.
flagged AS (
    SELECT
        r.id AS reading_id,
        r.ts,
        r.temperature_c,
        r.speed_rpm,
        r.vibration_mm_s,
        CASE
            WHEN (r.temperature_c - band.mean_temperature)
               * (r.temperature_c - band.mean_temperature)
               > 9 * band.var_temperature
            THEN 1 ELSE 0
        END AS temperature_abnormal,
        CASE
            WHEN (r.speed_rpm - band.mean_speed)
               * (r.speed_rpm - band.mean_speed)
               > 9 * band.var_speed
            THEN 1 ELSE 0
        END AS speed_abnormal,
        CASE
            WHEN (r.vibration_mm_s - band.mean_vibration)
               * (r.vibration_mm_s - band.mean_vibration)
               > 9 * band.var_vibration
            THEN 1 ELSE 0
        END AS vibration_abnormal
    FROM readings r, bounds b, band
    WHERE r.machine_id = b.machine_id
      AND r.ts >= b.start_ts
      AND r.ts <= b.end_bound
)

-- The flags are filtered in an outer query because a column alias cannot be
-- used in the WHERE clause of the SELECT that defines it.
SELECT
    reading_id,
    ts,
    temperature_c,
    speed_rpm,
    vibration_mm_s,
    temperature_abnormal,
    speed_abnormal,
    vibration_abnormal
FROM flagged
WHERE temperature_abnormal = 1
   OR speed_abnormal = 1
   OR vibration_abnormal = 1
ORDER BY ts;
