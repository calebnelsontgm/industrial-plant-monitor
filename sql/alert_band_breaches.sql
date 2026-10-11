-- Readings above the upper band, for the alert engine.
--
-- QUESTION
-- In the recent period being judged, which readings sit above their machine's
-- upper band for temperature or vibration?
--
-- PARAMETERS (bind in this order)
--   1. as_of            text     ISO timestamp the judgement is made at
--   2. evaluation_days  integer  how far back to look for breaches
--   3. baseline_days    integer  length of the window that defines normal
--   4. sigma            integer  how many standard deviations wide the band is
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   ts                         text
--   temperature_above          integer: 1 if above the upper band, else 0
--   vibration_above            integer
--
-- Only rows where at least one flag is 1, ordered by machine then ts.

WITH params AS (
    SELECT ? AS as_of, ? AS evaluation_days, ? AS baseline_days, ? AS sigma
),

-- Two windows that do not overlap. The evaluation window is the recent period
-- being judged; the baseline window is the stretch of normal running
-- immediately BEFORE it.
--
-- The gap matters. If the baseline included the period being judged, a problem
-- lasting several days would raise its own mean and inflate its own standard
-- deviation, widening the band until it swallowed the problem. A 4-day
-- temperature climb pushes the band from 80 C up to 102 C that way and escapes
-- detection entirely.
bounds AS (
    SELECT
        as_of,
        sigma,
        strftime('%Y-%m-%dT%H:%M:%S', as_of, '-' || evaluation_days || ' days')
            AS evaluation_start,
        strftime(
            '%Y-%m-%dT%H:%M:%S',
            as_of,
            '-' || evaluation_days || ' days',
            '-' || baseline_days || ' days'
        ) AS baseline_start
    FROM params
),

-- What normal looks like per machine, measured only on the baseline window.
band AS (
    SELECT
        r.machine_id,
        AVG(r.temperature_c) AS mean_temperature,
        AVG(r.temperature_c * r.temperature_c)
            - AVG(r.temperature_c) * AVG(r.temperature_c) AS var_temperature,
        AVG(r.vibration_mm_s) AS mean_vibration,
        AVG(r.vibration_mm_s * r.vibration_mm_s)
            - AVG(r.vibration_mm_s) * AVG(r.vibration_mm_s) AS var_vibration
    FROM readings r, bounds b
    WHERE r.ts > b.baseline_start
      AND r.ts <= b.evaluation_start
    GROUP BY r.machine_id
),

-- Only breaches ABOVE the band count, so each test has two parts: the reading
-- is on the high side of the mean, and its squared distance from the mean
-- exceeds sigma squared times the variance. Squaring both sides is what avoids
-- needing sqrt(), which SQLite may not have.
flagged AS (
    SELECT
        r.machine_id,
        m.name AS machine_name,
        r.ts,
        CASE
            WHEN r.temperature_c > band.mean_temperature
             AND (r.temperature_c - band.mean_temperature)
               * (r.temperature_c - band.mean_temperature)
               > b.sigma * b.sigma * band.var_temperature
            THEN 1 ELSE 0
        END AS temperature_above,
        CASE
            WHEN r.vibration_mm_s > band.mean_vibration
             AND (r.vibration_mm_s - band.mean_vibration)
               * (r.vibration_mm_s - band.mean_vibration)
               > b.sigma * b.sigma * band.var_vibration
            THEN 1 ELSE 0
        END AS vibration_above
    FROM readings r
    JOIN machines m ON m.id = r.machine_id
    JOIN band ON band.machine_id = r.machine_id
    CROSS JOIN bounds b
    WHERE r.ts > b.evaluation_start
      AND r.ts <= b.as_of
)

SELECT
    machine_id,
    machine_name,
    ts,
    temperature_above,
    vibration_above
FROM flagged
WHERE temperature_above = 1
   OR vibration_above = 1
ORDER BY machine_id, ts;
