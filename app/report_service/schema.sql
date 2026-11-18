-- Report Service: independent schema (report.db)
CREATE TABLE IF NOT EXISTS reports (
    report_id       TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    org_scope       TEXT NOT NULL,
    sample_id       TEXT NOT NULL,
    snapshot_id     TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL,  -- active|tainted|rebuilt|blocked|sealed
    current         INTEGER NOT NULL DEFAULT 1,
    rebuilt_from    TEXT,
    block_reason    TEXT,
    retraction_id   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_metrics (
    metric_id     TEXT PRIMARY KEY,
    report_id     TEXT NOT NULL REFERENCES reports(report_id),
    metric_name   TEXT NOT NULL,
    metric_type   TEXT NOT NULL,
    value         REAL,
    unit          TEXT,
    source_id     TEXT NOT NULL,
    lineage_ids   TEXT NOT NULL,    -- JSON array, every contributing row
    subtotals     TEXT NOT NULL,    -- JSON per source_id subtotal
    row_count     INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_report ON report_metrics(report_id);

CREATE TABLE IF NOT EXISTS report_versions (
    report_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    snapshot_id   TEXT NOT NULL,
    sample_id     TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (report_id, version)
);

CREATE TABLE IF NOT EXISTS cache_entries (
    cache_id       TEXT PRIMARY KEY,
    report_id      TEXT NOT NULL,
    report_version INTEGER NOT NULL,
    snapshot_id    TEXT NOT NULL,
    cache_key      TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    status         TEXT NOT NULL,    -- valid | quarantined
    retraction_id  TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exports (
    export_id      TEXT PRIMARY KEY,
    report_id      TEXT NOT NULL,
    report_version INTEGER NOT NULL,
    kind           TEXT NOT NULL,    -- download | sealed_audit
    status         TEXT NOT NULL,    -- available | revoked | sealed
    path           TEXT,
    retraction_id  TEXT,
    legal_hold     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sealed_audits (
    sealed_id           TEXT PRIMARY KEY,
    report_id           TEXT NOT NULL,
    report_version      INTEGER NOT NULL,
    snapshot_id         TEXT NOT NULL,
    snapshot_fingerprint TEXT NOT NULL,
    artifact_path       TEXT NOT NULL,
    artifact_hash       TEXT NOT NULL,
    reason              TEXT NOT NULL,
    sealed_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impact_findings (
    finding_id       TEXT PRIMARY KEY,
    report_id        TEXT NOT NULL,
    retraction_id    TEXT NOT NULL,
    metric_id        TEXT NOT NULL,
    total_rows       INTEGER NOT NULL,
    affected_rows    INTEGER NOT NULL,
    safe_rows        INTEGER NOT NULL,
    contribution_pct REAL NOT NULL,
    decision         TEXT NOT NULL,
    decision_reason  TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS explanation_packages (
    package_id     TEXT PRIMARY KEY,
    report_id      TEXT NOT NULL,
    retraction_id  TEXT,
    path           TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    sensitive_scan TEXT NOT NULL,    -- JSON: {offenses: []}
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_inbox (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0,
    received_at  TEXT NOT NULL,
    consumed_at  TEXT
);
