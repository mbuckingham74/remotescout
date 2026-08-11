CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    employer TEXT NOT NULL,
    description TEXT,
    location TEXT,
    compensation TEXT,
    source TEXT,
    source_url TEXT,
    source_job_id TEXT,
    employer_url TEXT,
    requisition_id TEXT,
    posted_at TEXT,
    identity_key TEXT,
    score REAL,
    fit_explanation TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_identity_key ON jobs (identity_key);
CREATE INDEX IF NOT EXISTS idx_jobs_employer_url ON jobs (employer_url);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 3),
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    score REAL,
    explanation TEXT,
    UNIQUE (date, rank),
    UNIQUE (date, job_id)
);

CREATE INDEX IF NOT EXISTS idx_recommendations_date ON recommendations (date);

CREATE TABLE IF NOT EXISTS recommendation_days (
    recommendation_date TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs (id),
    applied_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Applied'
        CHECK (status IN ('Applied', 'Screen', 'Interview', 'Offer', 'Rejected', 'Withdrawn', 'Ghosted')),
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (status);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    event_date TEXT NOT NULL,
    status TEXT,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_application_events_application ON application_events (application_id);
