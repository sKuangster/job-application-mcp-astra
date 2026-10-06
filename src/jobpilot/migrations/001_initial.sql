CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO meta(key, value) VALUES ('control', 'STOPPED');
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    simplify_url TEXT NOT NULL DEFAULT '',
    application_url TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    identity_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX jobs_identity ON jobs(identity_key);
CREATE TABLE job_aliases (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    PRIMARY KEY(kind, value)
);
CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    state TEXT NOT NULL,
    worker_id TEXT,
    lease_token TEXT,
    lease_until REAL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX applications_state ON applications(state);
CREATE INDEX applications_leases ON applications(lease_until);
CREATE TABLE queue_items (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    status TEXT NOT NULL CHECK(status IN ('AVAILABLE', 'LEASED', 'BLOCKED', 'DONE')),
    priority INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX queue_ready ON queue_items(status, available_at, priority);
CREATE TABLE application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    worker_id TEXT,
    timestamp REAL NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX events_application_order ON application_events(application_id, id);
CREATE TABLE answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications(id),
    question TEXT NOT NULL,
    normalized_question TEXT NOT NULL,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    answer TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX answers_memory ON answers(normalized_question, company);
CREATE INDEX answers_application ON answers(application_id, id);
CREATE TABLE answer_memory (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    normalized_question TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    job_id TEXT,
    record TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX answer_memory_lookup ON answer_memory(profile_hash, normalized_question, company);
CREATE TABLE submission_intents (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    worker_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('SUBMITTING', 'SUBMITTED', 'SUBMISSION_UNCONFIRMED')),
    evidence TEXT NOT NULL DEFAULT '{}',
    finished_at REAL
);
CREATE TABLE shared_cache (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    expires_at REAL,
    owner TEXT,
    lease_until REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(namespace, key)
);
CREATE TABLE reasoning_tasks (
    id TEXT PRIMARY KEY,
    cache_key TEXT NOT NULL UNIQUE,
    application_id TEXT REFERENCES applications(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'COMPLETE', 'FAILED')),
    payload TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX reasoning_pending ON reasoning_tasks(status, created_at);
