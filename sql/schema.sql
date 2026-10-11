-- Plant Monitor schema: seven tables.
--
-- Timestamps are ISO 8601 text (2026-09-30T14:15:00), which sorts
-- lexicographically in the same order as chronologically, so BETWEEN and
-- comparison operators work on them directly.
--
-- Reading ranges (0-150 C and so on) are deliberately NOT checked here. The
-- loader has to skip bad rows and count them by reason, so validation lives in
-- one place rather than raising at insert time.

CREATE TABLE lines (
    id   INTEGER PRIMARY KEY,
    name TEXT    NOT NULL UNIQUE
);

CREATE TABLE machines (
    id      INTEGER PRIMARY KEY,
    name    TEXT    NOT NULL UNIQUE,          -- e.g. 'M-07'
    line_id INTEGER NOT NULL REFERENCES lines(id)
);

CREATE TABLE readings (
    id             INTEGER PRIMARY KEY,
    machine_id     INTEGER NOT NULL REFERENCES machines(id),
    ts             TEXT    NOT NULL,
    temperature_c  REAL    NOT NULL,
    speed_rpm      REAL    NOT NULL,
    vibration_mm_s REAL    NOT NULL
);

-- Every analysis query filters by machine and scans a time range, so this
-- index serves both the filter and the ordering without a sort.
CREATE INDEX idx_readings_machine_ts ON readings(machine_id, ts);

CREATE TABLE downtime_events (
    id         INTEGER PRIMARY KEY,
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    start_ts   TEXT    NOT NULL,
    end_ts     TEXT    NOT NULL,
    reason     TEXT,
    CHECK (end_ts > start_ts)
);

CREATE INDEX idx_downtime_machine_start ON downtime_events(machine_id, start_ts);

CREATE TABLE failures (
    id           INTEGER PRIMARY KEY,
    machine_id   INTEGER NOT NULL REFERENCES machines(id),
    ts           TEXT    NOT NULL,
    failure_type TEXT    NOT NULL CHECK (failure_type IN ('overheat', 'bearing', 'power'))
);

CREATE INDEX idx_failures_machine_ts ON failures(machine_id, ts);

CREATE TABLE alerts (
    id         INTEGER PRIMARY KEY,
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    ts         TEXT    NOT NULL,
    rule       TEXT    NOT NULL,
    severity   TEXT    NOT NULL CHECK (severity IN ('High', 'Medium')),
    message    TEXT    NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    -- Re-running the alert engine must not duplicate an alert for the same
    -- machine, rule and run start. INSERT OR IGNORE relies on this.
    UNIQUE (machine_id, rule, ts)
);

-- The settings of the dataset currently loaded. CHECK (id = 1) enforces the
-- brief's "one row": a second insert fails instead of silently accumulating.
CREATE TABLE dataset_meta (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    name         TEXT    NOT NULL,
    seed         INTEGER NOT NULL,
    machines     INTEGER NOT NULL,
    days         INTEGER NOT NULL,
    start_date   TEXT    NOT NULL,
    problems     TEXT    NOT NULL,   -- JSON object of the four toggles
    generated_at TEXT    NOT NULL
);
