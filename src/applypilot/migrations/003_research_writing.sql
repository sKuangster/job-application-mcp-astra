CREATE TABLE research_tasks (
    task_key TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    scope TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    diagnostics TEXT NOT NULL DEFAULT '{}'
);
ALTER TABLE reasoning_routes ADD COLUMN revisions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reasoning_routes ADD COLUMN feedback TEXT NOT NULL DEFAULT '[]';
ALTER TABLE reasoning_routes ADD COLUMN first_attempt_at REAL;
CREATE TABLE experiences (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    core TEXT NOT NULL,
    themes TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('suggested','confirmed','rejected')),
    updated_at REAL NOT NULL
);
