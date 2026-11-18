"""Sensitive-data classification & scrubbing.

Explanation packages, notifications and audit summaries must NEVER carry raw
sensitive row values. Scrubbing happens at the structural boundary (report
service) and is additionally verified before any explanation package is
written to disk.
"""
from __future__ import annotations

import copy

# Field names (case-insensitive substring match) classified as sensitive detail.
SENSITIVE_KEY_HINTS = (
    "name", "email", "phone", "address", "ssn", "passport", "national_id",
    "account", "card", "iban", "diagnosis", "condition", "medical", "salary",
    "compensation", "dob", "birth", "biometric", "ip_address", "geo",
    "first_name", "last_name", "full_name",
)

# Structural keys that may legitimately appear in explanation packages.
ALLOWED_KEYS = {
    "lineage_id", "source_id", "source_name", "row_index", "purposes",
    "org_scopes", "license_id", "snapshot_id", "snapshot_version",
    "retraction_id", "sample_id", "sample_version", "report_id",
    "report_version", "export_id", "sealed_audit_id", "build_job_id",
    "metric_id", "metric_name", "value", "value_before", "value_after",
    "affected_rows", "total_rows", "contribution_pct", "decision",
    "decision_reason", "status", "metric_type", "unit", "created_at",
    "updated_at", "sealed_at", "rebuilt_from", "blocked", "rebuildable",
    "threshold_pct", "impact_pct", "retained_rows", "removed_rows",
    "grants", "retractions", "events", "subtotals", "findings",
    "explanation_package", "lineage", "manifest", "summary", "metrics",
    "artifacts", "downloads", "cache_entries", "outbox", "inbox",
    "simulation", "replayed", "event_id", "event_type", "delivered",
    "lost", "consumed", "stages", "stage", "progress", "resumable",
    "purpose", "org_scope", "old_purpose", "new_purpose", "request_id",
    "approved", "rejection_reason", "rows", "row_count", "version",
    "description", "retention_legal_hold", "snapshot_fingerprint",
}


def is_sensitive_key(key: str) -> bool:
    low = key.lower()
    if low in ALLOWED_KEYS:
        return False
    return any(hint in low for hint in SENSITIVE_KEY_HINTS)


def scrub(obj, _path: str = "$"):
    """Return a deep copy with every sensitive leaf replaced by '[REDACTED]'."""
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if is_sensitive_key(k):
                clean[k] = "[REDACTED]"
            else:
                clean[k] = scrub(v, f"{_path}.{k}")
        return clean
    if isinstance(obj, list):
        return [scrub(v, f"{_path}[{i}]") for i, v in enumerate(obj)]
    return obj


def assert_no_sensitive_detail(obj, _path: str = "$") -> list[str]:
    """Verify an explanation payload contains no sensitive leaves.

    Returns a list of offending paths (empty == clean). Used as a hard gate
    before writing explanation packages; callers must fail closed on a
    non-empty result.
    """
    offenses: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if is_sensitive_key(k):
                offenses.append(f"{_path}.{k}")
            else:
                offenses.extend(assert_no_sensitive_detail(v, f"{_path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            offenses.extend(assert_no_sensitive_detail(v, f"{_path}[{i}]"))
    return offenses


def safe_copy(obj):
    clean = scrub(copy.deepcopy(obj))
    offenses = assert_no_sensitive_detail(clean)
    if offenses:
        # Fail closed: explanation packages cannot contain raw detail.
        raise ValueError(
            f"sensitive detail would leak in explanation package: {offenses}"
        )
    return clean
