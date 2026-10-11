-- Downtime totals per production line, worst first.
--
-- QUESTION
-- Over a date range, which production line lost the most hours, and how many
-- events caused it?
--
-- PARAMETERS (bind in this order)
--   1. start_ts         text     range start, inclusive
--   2. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   line_id                    integer
--   line_name                  text
--   machines                   integer: machines on the line
--   event_count                integer
--   downtime_hours             real: hours inside the range, 2 decimals
--   hours_per_machine          real: downtime_hours / machines, 2 decimals
--
-- One row per line, worst first. A line with no downtime at all still appears,
-- with zeros, so the two lines are always comparable.

WITH params AS (
    SELECT ? AS start_ts, ? AS end_ts
),

bounds AS (
    SELECT start_ts, date(end_ts) || 'T23:59:59' AS end_bound
    FROM params
),

-- Every line with its machine count, built from lines rather than from the
-- downtime table so a line with no events is still a row.
roster AS (
    SELECT
        l.id AS line_id,
        l.name AS line_name,
        COUNT(DISTINCT m.id) AS machines
    FROM lines l
    LEFT JOIN machines m ON m.line_id = l.id
    GROUP BY l.id, l.name
),

-- Downtime rolled up to the line through machines.line_id. Each event is
-- clipped to the range first, exactly as downtime_by_machine.sql does it.
lost AS (
    SELECT
        m.line_id,
        COUNT(d.id) AS event_count,
        SUM(
            (julianday(MIN(d.end_ts, b.end_bound))
             - julianday(MAX(d.start_ts, b.start_ts))) * 24.0
        ) AS downtime_hours
    FROM downtime_events d
    JOIN machines m ON m.id = d.machine_id
    CROSS JOIN bounds b
    WHERE d.start_ts <= b.end_bound
      AND d.end_ts >= b.start_ts
    GROUP BY m.line_id
)

SELECT
    r.line_id,
    r.line_name,
    r.machines,
    COALESCE(lost.event_count, 0) AS event_count,
    ROUND(COALESCE(lost.downtime_hours, 0.0), 2) AS downtime_hours,
    -- Hours per machine, so a line with more machines is not punished for it.
    -- NULLIF guards the divide when a line somehow has no machines.
    ROUND(
        COALESCE(lost.downtime_hours, 0.0) / NULLIF(r.machines, 0), 2
    ) AS hours_per_machine
FROM roster r
LEFT JOIN lost ON lost.line_id = r.line_id
ORDER BY downtime_hours DESC, r.line_name;
