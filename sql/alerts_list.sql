-- Every alert with its machine name, for the Alerts page.
--
-- QUESTION
-- What alerts exist, and which machine does each belong to?
--
-- PARAMETERS
--   none
--
-- OUTPUT COLUMNS
--   alert_id                   integer
--   machine_id                 integer
--   machine_name               text
--   line_name                  text
--   ts                         text
--   rule                       text
--   severity                   text
--   message                    text
--   resolved                   integer: 0 open, 1 resolved
--
-- Newest first. The page filters by severity and machine itself, so the
-- filters respond without another trip to the database.

SELECT
    a.id AS alert_id,
    a.machine_id,
    m.name AS machine_name,
    l.name AS line_name,
    a.ts,
    a.rule,
    a.severity,
    a.message,
    a.resolved
FROM alerts a
JOIN machines m ON m.id = a.machine_id
JOIN lines l ON l.id = m.line_id
ORDER BY a.ts DESC, m.name;
