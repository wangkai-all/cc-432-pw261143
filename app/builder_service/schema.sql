-- Sample Builder Service: independent schema (builder.db)
CREATE TABLE IF NOT EXISTS build_jobs (
    job_id         TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    org_scope      TEXT NOT NULL,
    snapshot_id    TEXT NOT NULL,
    stage          TEXT NOT NULL,           -- queued|authorized|materialized|completed|interrupted|failed
    progress       INTEGER NOT NULL DEFAULT 0,
    checkpoint     INTEGER NOT NULL DEFAULT 0,
    fail_stage     TEXT,                    -- simulated failure target
    fail_reason    TEXT,
    sample_id      TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sample_versions (
    sample_id        TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    purpose          TEXT NOT NULL,
    org_scope        TEXT NOT NULL,
    snapshot_id      TEXT NOT NULL,
    version          INTEGER NOT NULL,
    status           TEXT NOT NULL,         -- active | quarantined | rebuilt | superseded
    row_count        INTEGER NOT NULL DEFAULT 0,
    retraction_id    TEXT,
    superseded_by    TEXT,
    created_at       TEXT NOT NULL
);

-- Row-level lineage. raw_value stays inside this service; external APIs only
-- ever return lineage_id / source / index, never the sensitive payload.
-- Composite key: the same lineage id can appear in multiple sample versions
-- (e.g. a row retained through a safe-subset rebuild).
CREATE TABLE IF NOT EXISTS row_lineage (
    lineage_id       TEXT NOT NULL,
    sample_id        TEXT NOT NULL REFERENCES sample_versions(sample_id),
    source_id        TEXT NOT NULL,
    row_index        INTEGER NOT NULL,
    purpose          TEXT NOT NULL,
    org_scope        TEXT NOT NULL,
    snapshot_id      TEXT NOT NULL,
    raw_value        TEXT NOT NULL,         -- JSON, access-controlled
    is_sensitive     INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (lineage_id, sample_id)
);
CREATE INDEX IF NOT EXISTS idx_lineage_sample ON row_lineage(sample_id);
CREATE INDEX IF NOT EXISTS idx_lineage_source ON row_lineage(source_id, purpose, org_scope);

CREATE TABLE IF NOT EXISTS purpose_changes (
    request_id        TEXT PRIMARY KEY,
    lineage_id        TEXT NOT NULL,
    sample_id         TEXT NOT NULL,
    old_purpose       TEXT NOT NULL,
    new_purpose       TEXT NOT NULL,
    org_scope         TEXT NOT NULL,
    snapshot_id       TEXT NOT NULL,
    status            TEXT NOT NULL,        -- approved | rejected
    rejection_reason  TEXT,
    new_lineage_id    TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_inbox (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0,
    received_at  TEXT NOT NULL,
    consumed_at  TEXT
);

CREATE TABLE IF NOT EXISTS sim_fail_rules (
    rule_id     TEXT PRIMARY KEY,
    job_id      TEXT,                       -- null = next matching job
    fail_stage  TEXT NOT NULL,
    remaining   INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
