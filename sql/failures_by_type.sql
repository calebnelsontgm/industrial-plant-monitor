-- Failure counts by type and machine.
--
-- QUESTION
-- Over a date range, how many failures of each type did each machine have?
--
-- PARAMETERS (bind in this order)
--   1. start_ts         text     range start, inclusive
--   2. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   line_name                  text
--   failure_type               text: overheat, bearing or power
--   failure_count              integer
--
-- Only combinations that actually occurred, worst first.

WITH params AS (
    SELECT ? AS start_ts, ? AS end_ts
),

bounds AS (
    SELECT start_ts, date(end_ts) || 'T23:59:59' AS end_bound
    FROM params
)

SELECT
    m.id AS machine_id,
    m.name AS machine_name,
    l.name AS line_name,
    f.failure_type,
    COUNT(*) AS failure_count
FROM failures f
JOIN machines m ON m.id = f.machine_id
JOIN lines l ON l.id = m.line_id
CROSS JOIN bounds b
WHERE f.ts >= b.start_ts
  AND f.ts <= b.end_bound
-- Grouping by machine and type together is what gives one row per
-- machine-and-type pair rather than one per machine.
GROUP BY m.id, m.name, l.name, f.failure_type
ORDER BY failure_count DESC, m.name;
