"""Central compliance policy rules.

The same rules are evaluated by the builder (sample construction gate) and
the report service (rebuild/block decision). Enforcement is therefore
server-side and independent of any UI affordance — hiding a web button is
never the control.
"""
from __future__ import annotations

# If strictly more than this fraction of a metric's contributing rows are
# under a retracted grant, the metric cannot be rebuilt from a safe subset.
MAX_CONTRIBUTION_FOR_REBUILD_PCT = 25.0

# A rebuilt report needs at least this many surviving rows to remain valid.
MIN_SAFE_ROWS = 5

# Retention treatment.
LEGAL_HOLD_SEALED = "sealed_legal_hold"          # immutable, no new use
DOWNLOAD_REVOKED = "download_revoked"            # normal exports are revoked
SAMPLE_QUARANTINED = "sample_quarantined"        # tainted sample versions
CACHE_QUARANTINED = "cache_quarantined"          # tainted cached results

VALID_PURPOSES = ("clinical", "fraud_detection", "workforce", "market_research")
VALID_ORG_SCOPES = ("north", "central", "shared")


def evaluate_report(metric_findings: list[dict]) -> dict:
    """Decide per report whether rebuild-from-safe-subset is possible.

    Each finding: {metric_id, total_rows, affected_rows, safe_rows,
                   contribution_pct}
    Returns {decision: rebuild_safe|block, reasons: [...], per_metric: [...]}
    """
    per_metric = []
    block_reasons: list[str] = []
    for f in metric_findings:
        total = f["total_rows"] or 0
        affected = f["affected_rows"] or 0
        safe = f["safe_rows"]
        pct = (affected / total * 100.0) if total else 100.0
        metric_decision = "rebuild"
        reason = None
        if total == 0:
            metric_decision = "block"
            reason = "metric has no contributing rows"
        elif pct > MAX_CONTRIBUTION_FOR_REBUILD_PCT:
            metric_decision = "block"
            reason = (f"affected contribution {pct:.1f}% exceeds rebuild "
                      f"threshold {MAX_CONTRIBUTION_FOR_REBUILD_PCT:.0f}%")
        elif safe < MIN_SAFE_ROWS:
            metric_decision = "block"
            reason = (f"only {safe} safe rows remain, minimum is "
                      f"{MIN_SAFE_ROWS}")
        if metric_decision == "block":
            block_reasons.append(f"{f['metric_id']}: {reason}")
        per_metric.append({
            "metric_id": f["metric_id"],
            "total_rows": total,
            "affected_rows": affected,
            "safe_rows": safe,
            "contribution_pct": round(pct, 2),
            "decision": metric_decision,
            "reason": reason or "within rebuild threshold",
        })
    decision = "block" if block_reasons else "rebuild_safe"
    return {"decision": decision, "reasons": block_reasons,
            "per_metric": per_metric}


def grant_covers(grant: dict, source_id: str, purpose: str,
                 org_scope: str) -> bool:
    return (grant["source_id"] == source_id
            and grant["purpose"] == purpose
            and grant["org_scope"] == org_scope
            and grant["status"] == "active")
