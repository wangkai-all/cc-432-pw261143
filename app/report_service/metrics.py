"""Real metric computation over sample rows.

Each metric is computed from actual row values and records:
  - lineage_ids: every row that contributed (full traceability)
  - subtotals: per-source aggregates, so retraction impact can be computed
    without ever reading sensitive values again.
"""
from __future__ import annotations


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def compute_metrics(purpose: str, rows: list[dict]) -> list[dict]:
    """Return metric dicts: name, type, value, unit, lineage_ids, subtotals."""
    if purpose == "clinical":
        return _clinical(rows)
    if purpose == "fraud_detection":
        return _fraud(rows)
    if purpose == "workforce":
        return _workforce(rows)
    return _market(rows)


def _group_subtotal(rows: list[dict], field: str, fn) -> dict:
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["source_id"], []).append(r)
    out = {}
    for src, grp in groups.items():
        out[src] = fn(grp)
    return out


def _clinical(rows: list[dict]) -> list[dict]:
    eligible = [r for r in rows
                if str(r["raw_value"].get("consent", "")).lower() == "true"]
    values = [v for r in eligible
              if (v := _num(r["raw_value"].get("lab_value"))) is not None]
    high = [r for r in eligible
            if (_num(r["raw_value"].get("lab_value")) or 0) > 180]
    ids = [r["lineage_id"] for r in eligible]
    return [
        {"metric_name": "eligible_participants", "metric_type": "count",
         "value": float(len(eligible)), "unit": "persons",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(eligible, "",
                                      lambda g: float(len(g)))},
        {"metric_name": "mean_lab_value", "metric_type": "mean",
         "value": _mean(values), "unit": "u/mL",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(
             eligible, "lab_value",
             lambda g: _mean([_num(x["raw_value"]["lab_value"])
                              for x in g if _num(
                                  x["raw_value"].get("lab_value")) is not None]))},
        {"metric_name": "high_risk_cases", "metric_type": "count",
         "value": float(len(high)), "unit": "cases",
         "lineage_ids": [r["lineage_id"] for r in high],
         "subtotals": _group_subtotal(high, "",
                                      lambda g: float(len(g)))},
    ]


def _fraud(rows: list[dict]) -> list[dict]:
    amounts = [v for r in rows
               if (v := _num(r["raw_value"].get("amount"))) is not None]
    flagged = [r for r in rows
               if str(r["raw_value"].get("flag", "")).lower() == "true"]
    ids = [r["lineage_id"] for r in rows]
    return [
        {"metric_name": "transaction_count", "metric_type": "count",
         "value": float(len(rows)), "unit": "transactions",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(rows, "", lambda g: float(len(g)))},
        {"metric_name": "total_amount", "metric_type": "sum",
         "value": round(sum(amounts), 2), "unit": "CNY",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(
             rows, "amount",
             lambda g: round(sum(_num(x["raw_value"]["amount"]) for x in g), 2))},
        {"metric_name": "mean_amount", "metric_type": "mean",
         "value": _mean(amounts), "unit": "CNY",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(
             rows, "amount",
             lambda g: _mean([_num(x["raw_value"]["amount"]) for x in g]))},
        {"metric_name": "flagged_rate", "metric_type": "ratio",
         "value": round(len(flagged) / len(rows) * 100, 2) if rows else None,
         "unit": "%", "lineage_ids": ids,
         "subtotals": _group_subtotal(
             rows, "",
             lambda g: round(
                 sum(1 for x in g if str(
                     x["raw_value"].get("flag", "")).lower() == "true")
                 / len(g) * 100, 2))},
    ]


def _workforce(rows: list[dict]) -> list[dict]:
    salaries = [v for r in rows
                if (v := _num(r["raw_value"].get("salary"))) is not None]
    active = [r for r in rows
              if str(r["raw_value"].get("employment_status", ""))
              == "active"]
    ids = [r["lineage_id"] for r in rows]
    return [
        {"metric_name": "headcount", "metric_type": "count",
         "value": float(len(rows)), "unit": "persons",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(rows, "", lambda g: float(len(g)))},
        {"metric_name": "mean_salary", "metric_type": "mean",
         "value": _mean(salaries), "unit": "CNY",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(
             rows, "salary",
             lambda g: _mean([_num(x["raw_value"]["salary"]) for x in g]))},
        {"metric_name": "active_employees", "metric_type": "count",
         "value": float(len(active)), "unit": "persons",
         "lineage_ids": [r["lineage_id"] for r in active],
         "subtotals": _group_subtotal(active, "",
                                      lambda g: float(len(g)))},
    ]


def _market(rows: list[dict]) -> list[dict]:
    scores = [v for r in rows
              if (v := _num(r["raw_value"].get("satisfaction_score")))
              is not None]
    opted = [r for r in rows
             if str(r["raw_value"].get("contact_consent", "")).lower()
             == "true"]
    ids = [r["lineage_id"] for r in rows]
    return [
        {"metric_name": "respondent_count", "metric_type": "count",
         "value": float(len(rows)), "unit": "respondents",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(rows, "", lambda g: float(len(g)))},
        {"metric_name": "mean_satisfaction", "metric_type": "mean",
         "value": _mean(scores), "unit": "score(1-5)",
         "lineage_ids": ids,
         "subtotals": _group_subtotal(
             rows, "satisfaction_score",
             lambda g: _mean(
                 [_num(x["raw_value"]["satisfaction_score"]) for x in g]))},
        {"metric_name": "contact_consent_rate", "metric_type": "ratio",
         "value": round(len(opted) / len(rows) * 100, 2) if rows else None,
         "unit": "%", "lineage_ids": ids,
         "subtotals": _group_subtotal(
             rows, "",
             lambda g: round(
                 sum(1 for x in g if str(
                     x["raw_value"].get("contact_consent", "")).lower()
                     == "true") / len(g) * 100, 2))},
    ]
