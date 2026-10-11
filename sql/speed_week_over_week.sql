-- Average speed per 7-day block, with the change from the block before.
--
-- QUESTION
-- Over a date range, what is each machine's average speed per 7-day block,
-- and how does each block compare with the previous one?
--
-- PARAMETERS (bind in this order)
--   1. start_ts         text     range start, inclusive
--   2. end_ts           text     range end, inclusive to end of day
--
-- OUTPUT COLUMNS
--   machine_id                 integer
--   machine_name               text
--   block_index                integer: 0 is the most recent 7 days
--   block_start                text: first timestamp the block covers
--   avg_speed_rpm              real
--   previous_avg_speed_rpm     real: NULL for the oldest block
--   percent_change             real: negative means slower
--
-- Ordered by machine_id, then block_index.

WITH params AS (
    SELECT ? AS start_ts, ? AS end_ts
),

bounds AS (
    SELECT start_ts, date(end_ts) || 'T23:59:59' AS end_bound
    FROM params
),

scoped AS (
    SELECT r.machine_id, r.ts, r.speed_rpm
    FROM readings r
    CROSS JOIN bounds b
    WHERE r.ts >= b.start_ts
      AND r.ts <= b.end_bound
),

-- Blocks are counted back from the newest reading actually present, not from
-- calendar weeks and not from the requested end date, so a range that stops
-- mid-week still has its most recent 7 days as block 0.
latest AS (
    SELECT MAX(ts) AS last_ts FROM scoped
),

blocked AS (
    SELECT
        s.machine_id,
        s.ts,
        s.speed_rpm,
        -- Days between this reading and the newest one, divided by 7. CAST to
        -- INTEGER truncates, so 0 to 6.99 days becomes block 0, 7 to 13.99
        -- becomes block 1, and so on.
        CAST((julianday(l.last_ts) - julianday(s.ts)) / 7 AS INTEGER)
            AS block_index
    FROM scoped s
    CROSS JOIN latest l
),

per_block AS (
    SELECT
        machine_id,
        block_index,
        MIN(ts) AS block_start,
        AVG(speed_rpm) AS avg_speed_rpm
    FROM blocked
    GROUP BY machine_id, block_index
)

SELECT
    p.machine_id,
    m.name AS machine_name,
    p.block_index,
    p.block_start,
    p.avg_speed_rpm,

    -- LAG reaches back one row within each machine. The ordering is block_index
    -- DESCENDING, so the rows arrive oldest first and "the previous row" is the
    -- older block -- which is the one we want to compare against. Ordering
    -- ascending would make LAG return the *newer* block instead, inverting
    -- every percentage. The oldest block has no earlier row, so it gets NULL.
    LAG(p.avg_speed_rpm) OVER (
        PARTITION BY p.machine_id
        ORDER BY p.block_index DESC
    ) AS previous_avg_speed_rpm,

    ROUND(
        (p.avg_speed_rpm - LAG(p.avg_speed_rpm) OVER (
            PARTITION BY p.machine_id
            ORDER BY p.block_index DESC
        ))
        / LAG(p.avg_speed_rpm) OVER (
            PARTITION BY p.machine_id
            ORDER BY p.block_index DESC
        ) * 100.0,
        2
    ) AS percent_change

FROM per_block p
JOIN machines m ON m.id = p.machine_id
ORDER BY p.machine_id, p.block_index;
