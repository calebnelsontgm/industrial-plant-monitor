-- Scratchpad. Type a query, save, then run:
--
--     .venv/Scripts/python -m plantmonitor sql
--
-- It prints the first 50 rows. Add --all for everything.
-- Anything in here is throwaway: it is not part of the project.
--
-- The six tables you have:
--
--   lines            id, name
--   machines         id, name, line_id
--   readings         id, machine_id, ts, temperature_c, speed_rpm, vibration_mm_s
--   downtime_events  id, machine_id, start_ts, end_ts, reason
--   failures         id, machine_id, ts, failure_type
--   dataset_meta     the settings of the dataset you loaded
--
-- Timestamps are text like '2026-10-01T23:45:00', which means they sort and
-- compare correctly with <, > and BETWEEN without any date functions.


-- SELECT name, line_id
-- FROM machines
-- ORDER BY name;


SELECT m.name, COUNT(*) AS readings, ROUND(AVG(r.temperature_c), 1) AS avg_temp
FROM readings r
JOIN machines m ON m.id = r.machine_id
GROUP BY m.name
ORDER BY avg_temp DESC;
