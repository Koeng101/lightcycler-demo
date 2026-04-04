CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_code INTEGER NOT NULL,
    sub_code TEXT,
    message TEXT,
    raw_fields TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    guid TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    config TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS acquisitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_guid TEXT NOT NULL REFERENCES experiments(guid),
    acquisition_num INTEGER NOT NULL,
    stage_index INTEGER NOT NULL DEFAULT 0,
    step_index INTEGER NOT NULL DEFAULT 0,
    cycle INTEGER NOT NULL DEFAULT 1,
    temperature_c REAL,
    time_s REAL,
    ref_channel INTEGER,
    well_data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_code ON events(event_code);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_acquisitions_guid ON acquisitions(experiment_guid);
