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

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    recommendation_threshold REAL,
    scoring_model TEXT,
    error_type TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_date ON pipeline_runs (recommendation_date);

CREATE TABLE IF NOT EXISTS pipeline_source_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES pipeline_runs (id),
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    discovered_count INTEGER,
    error_type TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_source_attempts_run ON pipeline_source_attempts (run_id);

CREATE TABLE IF NOT EXISTS pipeline_run_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES pipeline_runs (id),
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    source TEXT NOT NULL,
    filter_passed INTEGER NOT NULL,
    filter_reasons TEXT,
    suppressed_pre_score INTEGER NOT NULL DEFAULT 0,
    scoring_attempted INTEGER NOT NULL DEFAULT 0,
    scoring_succeeded INTEGER NOT NULL DEFAULT 0,
    score INTEGER,
    fit_explanation TEXT,
    strengths TEXT,
    gaps TEXT,
    meets_threshold INTEGER NOT NULL DEFAULT 0,
    resolution_attempted INTEGER NOT NULL DEFAULT 0,
    resolution_succeeded INTEGER NOT NULL DEFAULT 0,
    resolution_method TEXT,
    employer_url TEXT,
    requisition_id TEXT,
    suppressed_post_resolution INTEGER NOT NULL DEFAULT 0,
    suppressed_canonical_duplicate INTEGER NOT NULL DEFAULT 0,
    accepted_rank INTEGER,
    scoring_error_type TEXT,
    scoring_error_message TEXT,
    UNIQUE (run_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run_jobs_run ON pipeline_run_jobs (run_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_jobs_job ON pipeline_run_jobs (job_id);
