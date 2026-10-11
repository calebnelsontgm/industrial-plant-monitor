-- The active dataset's settings, for the header every dashboard page shows.
--
-- QUESTION
-- Which dataset is loaded, and what settings produced it?
--
-- PARAMETERS
--   none
--
-- OUTPUT COLUMNS
--   name                       text
--   seed                       integer
--   machines                   integer
--   days                       integer
--   start_date                 text
--   problems                   text: JSON object of the four toggles
--   generated_at               text
--
-- At most one row: dataset_meta is constrained to a single row.

SELECT
    name,
    seed,
    machines,
    days,
    start_date,
    problems,
    generated_at
FROM dataset_meta
WHERE id = 1;
