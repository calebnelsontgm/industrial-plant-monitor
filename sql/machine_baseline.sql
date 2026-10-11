-- Per-machine mean and variance over the baseline window.
--
-- QUESTION
-- For each machine, what is the mean and variance of each reading type over
-- the 14 days ending at the as-of time?
--
-- PARAMETERS (bind in this order)
--   1. as_of            text     ISO timestamp the window ends at
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   line_id                    integer
--   line_name                  text
--   readings                   integer: rows the window covered
--   mean_temperature_c         real
--   var_temperature_c          real
--   mean_speed_rpm             real
--   var_speed_rpm              real
--   mean_vibration_mm_s        real
--   var_vibration_mm_s         real
--
-- One row per machine, ordered by machine_id.

WITH params AS (
    SELECT ? AS as_of
),

-- The 14-day baseline window. See kpi_summary.sql for why strftime is used
-- rather than datetime: it keeps the 'T' separator these timestamps use.
window_bounds AS (
    SELECT
        as_of,
        strftime('%Y-%m-%dT%H:%M:%S', as_of, '-14 days') AS window_start
    FROM params
)

SELECT
    m.id AS machine_id,
    m.name AS machine_name,
    l.id AS line_id,
    l.name AS line_name,
    COUNT(r.id) AS readings,

    -- SQLite has no variance or standard deviation function, so variance is
    -- built from the two averages it can compute: the mean of the squares
    -- minus the square of the mean. Variance is returned rather than standard
    -- deviation because sqrt() may not be compiled in; Python takes the root
    -- when a figure is needed for display.
    AVG(r.temperature_c) AS mean_temperature_c,
    AVG(r.temperature_c * r.temperature_c)
        - AVG(r.temperature_c) * AVG(r.temperature_c) AS var_temperature_c,

    AVG(r.speed_rpm) AS mean_speed_rpm,
    AVG(r.speed_rpm * r.speed_rpm)
        - AVG(r.speed_rpm) * AVG(r.speed_rpm) AS var_speed_rpm,

    AVG(r.vibration_mm_s) AS mean_vibration_mm_s,
    AVG(r.vibration_mm_s * r.vibration_mm_s)
        - AVG(r.vibration_mm_s) * AVG(r.vibration_mm_s) AS var_vibration_mm_s

FROM machines m
-- An inner join: the foreign key guarantees every machine has a line, so this
-- cannot drop a row.
JOIN lines l ON l.id = m.line_id
-- A LEFT JOIN so a machine with no readings in the window still appears, with
-- nulls, rather than vanishing from the dashboard.
LEFT JOIN readings r
       ON r.machine_id = m.id
      AND r.ts > (SELECT window_start FROM window_bounds)
      AND r.ts <= (SELECT as_of FROM window_bounds)
GROUP BY m.id, m.name, l.id, l.name
ORDER BY m.id;
