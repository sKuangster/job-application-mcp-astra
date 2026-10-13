ALTER TABLE model_calls ADD COLUMN model TEXT;
ALTER TABLE model_calls ADD COLUMN estimated_usd REAL NOT NULL DEFAULT 0;
ALTER TABLE model_calls ADD COLUMN escalation_reason TEXT;
ALTER TABLE model_calls ADD COLUMN usage_estimated INTEGER NOT NULL DEFAULT 0;
CREATE TABLE model_reservations (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    application_ids TEXT NOT NULL,
    reserved_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
);
CREATE TABLE reasoning_routes (
    item_id TEXT PRIMARY KEY REFERENCES reasoning_items(id),
    tier INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT 'initial',
    updated_at REAL NOT NULL
);
