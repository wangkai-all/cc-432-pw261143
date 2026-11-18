-- Authorization Snapshot Service: independent schema (snapshot.db)
CREATE TABLE IF NOT EXISTS sources (
    source_id         TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    category          TEXT NOT NULL,
    description       TEXT,
    sensitive_columns TEXT NOT NULL,           -- JSON array
    record_count      INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    grant_id        TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(source_id),
    purpose         TEXT NOT NULL,
    org_scope       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',  -- active | revoked
    granted_at      TEXT NOT NULL,
    revoked_at      TEXT,
    retraction_id   TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    version      INTEGER NOT NULL UNIQUE,
    fingerprint  TEXT NOT NULL,
    note         TEXT,
    created_at   TEXT NOT NULL
);

-- Materialized, immutable grant state captured inside each snapshot.
CREATE TABLE IF NOT EXISTS snapshot_grants (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    grant_id    TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    org_scope   TEXT NOT NULL,
    status      TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, grant_id)
);

CREATE TABLE IF NOT EXISTS retractions (
    retraction_id            TEXT PRIMARY KEY,
    source_id                TEXT NOT NULL,
    scope_purposes           TEXT NOT NULL,    -- JSON array; [] means all
    scope_orgs               TEXT NOT NULL,    -- JSON array; [] means all
    reason                   TEXT NOT NULL,
    effective_snapshot_id    TEXT NOT NULL,
    effective_version        INTEGER NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'effective',
    created_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,                 -- JSON
    created_at  TEXT NOT NULL
);

-- Per-consumer delivery state; a 'lost' row models a dropped notification.
CREATE TABLE IF NOT EXISTS outbox_deliveries (
    event_id     TEXT NOT NULL,
    consumer     TEXT NOT NULL,               -- builder | report
    status       TEXT NOT NULL,               -- delivered | lost | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    detail       TEXT,
    PRIMARY KEY (event_id, consumer)
);

-- Simulation rules: drop the next `remaining` matching pushes for a consumer.
CREATE TABLE IF NOT EXISTS sim_loss_rules (
    rule_id     TEXT PRIMARY KEY,
    consumer    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    remaining   INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
