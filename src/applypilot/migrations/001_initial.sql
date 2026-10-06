-- ApplyPilot schema v1. Applied by applypilot.db.Database.initialize().

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    simplify_id TEXT UNIQUE,
    company TEXT NOT NULL,
    company_norm TEXT NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    locations TEXT NOT NULL DEFAULT '[]',
    category TEXT,
    terms TEXT NOT NULL DEFAULT '[]',
    sponsorship TEXT,
    degrees TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    simplify_url TEXT,
    direct_url TEXT,
    apply_url TEXT,
    ats TEXT,
    ats_job_key TEXT,
    date_posted REAL,
    date_updated REAL,
    details TEXT NOT NULL DEFAULT '{}',
    details_fetched_at REAL,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'simplify_listings',
    source_batch TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX jobs_company_title ON jobs(company_norm, title_norm);
CREATE INDEX jobs_ats_key ON jobs(ats_job_key);
CREATE INDEX jobs_batch ON jobs(source_batch, active);

CREATE TABLE job_aliases (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    PRIMARY KEY (kind, value)
);
CREATE INDEX job_aliases_job ON job_aliases(job_id);

CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    state TEXT NOT NULL,
    state_reason TEXT,
    priority INTEGER NOT NULL DEFAULT 5,
    available_at REAL NOT NULL DEFAULT 0,
    blocked_on TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_token TEXT,
    lease_until REAL,
    domain TEXT,
    ats TEXT,
    resume TEXT,
    tab_id INTEGER,
    window_id INTEGER,
    current_url TEXT,
    page_index INTEGER NOT NULL DEFAULT 0,
    mode TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    submitted_at REAL
);
CREATE INDEX applications_claim ON applications(state, blocked_on, available_at, priority);
CREATE INDEX applications_lease ON applications(lease_until);
CREATE INDEX applications_domain ON applications(domain, lease_until);
CREATE INDEX applications_updated ON applications(updated_at);

CREATE TABLE application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT,
    job_id TEXT,
    worker_id TEXT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX events_application ON application_events(application_id, id);
CREATE INDEX events_level ON application_events(level, id);
CREATE INDEX events_ts ON application_events(ts);

CREATE TABLE answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications(id),
    field_key TEXT NOT NULL,
    page_index INTEGER NOT NULL DEFAULT 0,
    question TEXT NOT NULL,
    normalized TEXT NOT NULL,
    qclass TEXT NOT NULL,
    catalog_id TEXT,
    field_type TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    required INTEGER NOT NULL DEFAULT 0,
    limits TEXT NOT NULL DEFAULT '{}',
    value TEXT,
    method TEXT,
    source TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (application_id, field_key)
);
CREATE INDEX answers_application ON answers(application_id, page_index, id);

CREATE TABLE answer_memory (
    id TEXT PRIMARY KEY,
    normalized TEXT NOT NULL,
    question TEXT NOT NULL,
    qclass TEXT NOT NULL,
    catalog_id TEXT,
    company_norm TEXT NOT NULL DEFAULT '',
    role TEXT,
    options_sig TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL,
    method TEXT NOT NULL,
    facts_used TEXT NOT NULL DEFAULT '[]',
    template TEXT,
    research_ids TEXT NOT NULL DEFAULT '[]',
    limits TEXT NOT NULL DEFAULT '{}',
    profile_hash TEXT NOT NULL,
    application_id TEXT,
    submitted INTEGER NOT NULL DEFAULT 0,
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX answer_memory_lookup ON answer_memory(normalized, company_norm);
CREATE INDEX answer_memory_class ON answer_memory(qclass, updated_at);

CREATE TABLE user_answers (
    normalized TEXT NOT NULL,
    company_norm TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (normalized, company_norm)
);

CREATE TABLE reasoning_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'LEASED', 'DONE', 'FAILED', 'CANCELLED')),
    payload TEXT NOT NULL,
    result TEXT,
    lease_owner TEXT,
    lease_until REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE UNIQUE INDEX reasoning_open_dedupe ON reasoning_items(dedupe_key)
    WHERE status IN ('PENDING', 'LEASED');
CREATE INDEX reasoning_status ON reasoning_items(status, created_at);

CREATE TABLE reasoning_waiters (
    item_id TEXT NOT NULL REFERENCES reasoning_items(id),
    application_id TEXT NOT NULL REFERENCES applications(id),
    field_key TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (item_id, application_id, field_key)
);
CREATE INDEX reasoning_waiters_application ON reasoning_waiters(application_id);

CREATE TABLE company_research (
    id TEXT PRIMARY KEY,
    company_norm TEXT NOT NULL,
    company TEXT NOT NULL,
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    facts TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL,
    retrieved_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX research_lookup ON company_research(company_norm, topic, retrieved_at);

CREATE TABLE locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE domain_health (
    domain TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    username TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    application_id TEXT,
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    verified_at REAL
);

CREATE TABLE submissions (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    dedupe_key TEXT NOT NULL,
    ats_job_key TEXT,
    status TEXT NOT NULL CHECK (status IN ('SUBMITTING', 'SUBMITTED', 'SUBMISSION_UNCONFIRMED', 'ABORTED')),
    worker_id TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    evidence TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX submissions_dedupe ON submissions(dedupe_key, status);
CREATE INDEX submissions_ats ON submissions(ats_job_key);

CREATE TABLE model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    items INTEGER NOT NULL DEFAULT 0,
    application_ids TEXT NOT NULL DEFAULT '[]',
    request_chars INTEGER NOT NULL DEFAULT 0,
    response_chars INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms INTEGER,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT
);
CREATE INDEX model_calls_ts ON model_calls(ts);

CREATE TABLE ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    mode TEXT,
    source TEXT,
    etag TEXT,
    fetched INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0,
    queued INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
