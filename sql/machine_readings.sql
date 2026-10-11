-- One machine's readings over a date range, for the Machine detail charts.
--
-- QUESTION
-- What did one machine report over a date range?
--
-- PARAMETERS (bind in this order)
--   1. machine_id       integer  the machine to chart
--   2. start_ts         text     range start, inclusive
--   3. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   reading_id                 integer
--   ts                         text
--   temperature_c              real
--   speed_rpm                  real
--   vibration_mm_s             real
--
-- Ordered by ts, so the charts can plot it directly.

WITH params AS (
    SELECT ? AS machine_id, ? AS start_ts, ? AS end_ts
),

bounds AS (
    SELECT
        machine_id,
        start_ts,
        date(end_ts) || 'T23:59:59' AS end_bound
    FROM params
)

SELECT
    r.id AS reading_id,
    r.ts,
    r.temperature_c,
    r.speed_rpm,
    r.vibration_mm_s
FROM readings r
CROSS JOIN bounds b
WHERE r.machine_id = b.machine_id
  AND r.ts >= b.start_ts
  AND r.ts <= b.end_bound
ORDER BY r.ts;
