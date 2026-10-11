-- One machine's downtime events and failures, as a single timeline.
--
-- QUESTION
-- For one machine over a date range, when was it down and when did it fail?
--
-- PARAMETERS (bind in this order)
--   1. machine_id       integer  the machine to examine
--   2. start_ts         text     range start, inclusive
--   3. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   kind                       text: 'downtime' or 'failure'
--   start_ts                   text
--   end_ts                     text: equal to start_ts for a failure
--   label                      text: the downtime reason, or the failure type
--   hours                      real: event length, NULL for a failure
--
-- Ordered by start_ts. Two kinds of event in one result so the chart can draw
-- both in one pass: downtime as a shaded span, a failure as a single line.
--
-- UNION ALL rather than UNION: the two halves can never produce a duplicate
-- row, so there is no reason to pay for the de-duplication pass. Column names
-- come from the first SELECT, which is why only that half needs aliases.

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

-- Downtime: anything overlapping the range at all, reported with its own true
-- start and end rather than clipped, so the chart can show a span running off
-- the edge of the view.
SELECT
    'downtime' AS kind,
    d.start_ts AS start_ts,
    d.end_ts AS end_ts,
    COALESCE(d.reason, 'unknown') AS label,
    ROUND((julianday(d.end_ts) - julianday(d.start_ts)) * 24.0, 2) AS hours
FROM downtime_events d
CROSS JOIN bounds b
WHERE d.machine_id = b.machine_id
  AND d.start_ts <= b.end_bound
  AND d.end_ts >= b.start_ts

UNION ALL

-- Failures are instants, so both ends are the same timestamp and there is no
-- length to report.
SELECT
    'failure',
    f.ts,
    f.ts,
    f.failure_type,
    NULL
FROM failures f
CROSS JOIN bounds b
WHERE f.machine_id = b.machine_id
  AND f.ts >= b.start_ts
  AND f.ts <= b.end_bound

ORDER BY start_ts;
