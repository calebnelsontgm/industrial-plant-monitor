-- Raw readings for every machine over a date range, for the failure model.
--
-- QUESTION
-- What did every machine report over a date range? The feature math (hourly
-- means, trailing windows, slopes) is done in pandas by predict.py; this query
-- only fetches the rows.
--
-- PARAMETERS (bind in this order)
--   1. start_ts         text     range start, inclusive
--   2. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   ts                         text
--   temperature_c              real
--   speed_rpm                  real
--   vibration_mm_s             real
--
-- Ordered by machine, then ts. The (machine_id, ts) index serves both.

WITH params AS (
    SELECT ? AS start_ts, ? AS end_ts
),

bounds AS (
    SELECT start_ts, date(end_ts) || 'T23:59:59' AS end_bound
    FROM params
)

SELECT
    r.machine_id,
    m.name AS machine_name,
    r.ts,
    r.temperature_c,
    r.speed_rpm,
    r.vibration_mm_s
FROM readings r
CROSS JOIN bounds b
JOIN machines m ON m.id = r.machine_id
WHERE r.ts >= b.start_ts
  AND r.ts <= b.end_bound
ORDER BY r.machine_id, r.ts;
