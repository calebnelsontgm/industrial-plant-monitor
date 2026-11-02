-- Every failure over a date range, for labelling the failure model's hours.
--
-- QUESTION
-- When did each machine fail? failures_by_type.sql returns counts; the model
-- needs the individual timestamps.
--
-- PARAMETERS (bind in this order)
--   1. start_ts         text     range start, inclusive
--   2. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   failure_id                 integer
--   machine_id                 integer
--   machine_name               text
--   ts                         text
--   failure_type               text
--
-- Ordered by ts.

WITH params AS (
    SELECT ? AS start_ts, ? AS end_ts
),

bounds AS (
    SELECT start_ts, date(end_ts) || 'T23:59:59' AS end_bound
    FROM params
)

SELECT
    f.id AS failure_id,
    f.machine_id,
    m.name AS machine_name,
    f.ts,
    f.failure_type
FROM failures f
CROSS JOIN bounds b
JOIN machines m ON m.id = f.machine_id
WHERE f.ts >= b.start_ts
  AND f.ts <= b.end_bound
ORDER BY f.ts;
