-- Downtime events inside the recent window, numbered, for the alert engine.
--
-- QUESTION
-- Which downtime events started within the last window_days, and in what
-- order did each machine's events occur?
--
-- PARAMETERS (bind in this order)
--   1. as_of            text     ISO timestamp the window ends at
--   2. window_days      integer  length of the window
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   start_ts                   text
--   event_number               integer: 1 for this machine's first event in
--                              the window, 2 for the second, and so on
--
-- Ordered by machine then start_ts.

WITH params AS (
    SELECT ? AS as_of, ? AS window_days
),

bounds AS (
    SELECT
        as_of,
        strftime('%Y-%m-%dT%H:%M:%S', as_of, '-' || window_days || ' days')
            AS window_start
    FROM params
)

SELECT
    d.machine_id,
    m.name AS machine_name,
    d.start_ts,

    -- ROW_NUMBER restarts at 1 for each machine, so the alert engine can find
    -- the event that took a machine to its threshold just by looking for the
    -- row whose event_number equals it. That timestamp is what makes the alert
    -- stable: re-running the engine finds the same event and the UNIQUE key on
    -- (machine_id, rule, ts) rejects the duplicate.
    ROW_NUMBER() OVER (
        PARTITION BY d.machine_id
        ORDER BY d.start_ts
    ) AS event_number

FROM downtime_events d
JOIN machines m ON m.id = d.machine_id
CROSS JOIN bounds b
WHERE d.start_ts > b.window_start
  AND d.start_ts <= b.as_of
ORDER BY d.machine_id, d.start_ts;
