-- Downtime totals per machine, worst first.
--
-- QUESTION
-- Over a date range, how many downtime hours and how many events did each
-- machine have?
--
-- PARAMETERS (bind in this order)
--   1. start_ts         text     range start, inclusive
--   2. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   line_name                  text
--   event_count                integer
--   downtime_hours             real: hours inside the range, 2 decimals
--
-- Ordered by downtime_hours descending.

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
    COUNT(d.id) AS event_count,

    -- julianday turns a timestamp into a day number, so subtracting two of
    -- them gives a gap in days; multiplying by 24 makes it hours.
    --
    -- Each event is clipped to the range before measuring: MAX picks the later
    -- of the event start and the range start, MIN the earlier of the event end
    -- and the range end. An event half outside the range therefore contributes
    -- only the part inside it. With two arguments these are the scalar MIN and
    -- MAX, not the aggregate ones.
    ROUND(SUM(
        (julianday(MIN(d.end_ts, b.end_bound))
         - julianday(MAX(d.start_ts, b.start_ts))) * 24.0
    ), 2) AS downtime_hours

FROM downtime_events d
JOIN machines m ON m.id = d.machine_id
JOIN lines l ON l.id = m.line_id
CROSS JOIN bounds b
-- An event counts if it overlaps the range at all: it starts before the range
-- ends, and ends after the range starts.
WHERE d.start_ts <= b.end_bound
  AND d.end_ts >= b.start_ts
GROUP BY m.id, m.name, l.name
ORDER BY downtime_hours DESC;
