-- Current state of every machine, for the plant diagram.
--
-- QUESTION
-- As at one moment, where is each machine, is it running, does it have open
-- alerts, and what was it last reading?
--
-- PARAMETERS (bind in this order)
--   1. as_of            text     ISO timestamp to judge as at
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   line_id                    integer
--   line_name                  text
--   position                   integer: 1-based place along its line
--   is_down                    integer: 1 if a downtime event spans as_of
--   open_alerts                integer: unresolved alerts
--   open_high                  integer: unresolved alerts of High severity
--   last_ts                    text: timestamp of its newest reading
--   temperature_c              real
--   speed_rpm                  real
--   vibration_mm_s             real
--
-- One row per machine, ordered by line then name. Every machine appears, even
-- one with no readings, no alerts and no downtime.

WITH params AS (
    SELECT ? AS as_of
),

-- The newest reading timestamp per machine, at or before the as-of moment.
-- Found first, then joined back to readings, because SQLite cannot return the
-- rest of a row from a MAX() without this two-step.
newest AS (
    SELECT r.machine_id, MAX(r.ts) AS last_ts
    FROM readings r
    CROSS JOIN params p
    WHERE r.ts <= p.as_of
    GROUP BY r.machine_id
)

SELECT
    m.id AS machine_id,
    m.name AS machine_name,
    l.id AS line_id,
    l.name AS line_name,

    -- Place along the line, so the diagram can lay the machines out without
    -- counting them in Python.
    ROW_NUMBER() OVER (
        PARTITION BY l.id
        ORDER BY m.name
    ) AS position,

    -- Down if some downtime event is open across the as-of moment: it began at
    -- or before, and has not ended yet.
    CASE WHEN EXISTS (
        SELECT 1
        FROM downtime_events d
        CROSS JOIN params p
        WHERE d.machine_id = m.id
          AND d.start_ts <= p.as_of
          AND d.end_ts > p.as_of
    ) THEN 1 ELSE 0 END AS is_down,

    (SELECT COUNT(*) FROM alerts a
      WHERE a.machine_id = m.id AND a.resolved = 0) AS open_alerts,
    (SELECT COUNT(*) FROM alerts a
      WHERE a.machine_id = m.id AND a.resolved = 0
        AND a.severity = 'High') AS open_high,

    n.last_ts,
    r.temperature_c,
    r.speed_rpm,
    r.vibration_mm_s

FROM machines m
JOIN lines l ON l.id = m.line_id
-- Both LEFT, so a machine with no readings at all is still a node on the
-- diagram rather than a hole in it.
LEFT JOIN newest n ON n.machine_id = m.id
LEFT JOIN readings r
       ON r.machine_id = m.id
      AND r.ts = n.last_ts
ORDER BY l.id, m.name;
