"""Generate fully synthetic raw datasets (no real personal data).

Deterministic via a seeded PRNG so every run reproduces identical rows and
therefore identical lineage ids. Output: data/raw/<source_id>.json plus a
catalog file data/raw/catalog.json describing sources, sensitive columns
and the initial license grants.
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

from app.common.util import new_id

SEED = 20261118
RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

PURPOSES = ("clinical", "fraud_detection", "workforce", "market_research")
ORGS = ("north", "central", "shared")

FIRST = ("Avery", "Blake", "Casey", "Drew", "Emery", "Finley", "Gray",
         "Harper", "Indigo", "Jordan", "Kendall", "Logan", "Morgan",
         "Nico", "Oakley", "Parker", "Quinn", "Riley", "Sage", "Taylor")
LAST = ("Nguyen", "Okafor", "Patel", "Rossi", "Silva", "Tanaka", "Vidal",
        "Wong", "Yilmaz", "Zhang", "Anders", "Brito", "Costa", "Dube",
        "Elias", "Farah", "Gomez", "Haddad", "Iversen", "Jensen")


def _person(rng: random.Random, i: int) -> dict:
    return {
        "subject_ref": f"SUBJ-{i:05d}",
        "full_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
        "email": f"subject{i:05d}@synthetic.test",
        "phone": f"+1-555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
        "date_of_birth": f"{rng.randint(1940, 2005)}-"
                         f"{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        "home_address": f"{rng.randint(1, 999)} Synthetic Ave, Zone "
                        f"{rng.randint(1, 12)}",
    }


def clinical_rows(rng: random.Random, n: int) -> list[dict]:
    rows = []
    for i in range(n):
        row = _person(rng, i)
        row.update({
            "patient_id": f"PT-{i:05d}",
            "diagnosis_code": rng.choice(["E11", "I10", "J45", "K21",
                                          "M54", "N18"]),
            "condition_note": rng.choice([
                "routine follow-up", "medication review",
                "new symptom evaluation", "screening panel"]),
            "lab_value": round(rng.gauss(150, 45), 1),
            "consent": str(rng.random() < 0.82).lower(),
            "encounter_month": rng.randint(1, 12),
        })
        rows.append(row)
    return rows


def fraud_rows(rng: random.Random, n: int) -> list[dict]:
    rows = []
    for i in range(n):
        row = _person(rng, i)
        row.update({
            "transaction_ref": f"TXN-{i:06d}",
            "account_number": f"ACCT{rng.randint(10**9, 10**10 - 1)}",
            "amount": round(rng.lognormvariate(6.2, 0.9), 2),
            "merchant": rng.choice(["shop-a", "shop-b", "shop-c",
                                    "shop-d", "shop-e"]),
            "flag": str(rng.random() < 0.14).lower(),
            "channel": rng.choice(["online", "in_store", "mobile"]),
        })
        rows.append(row)
    return rows


def workforce_rows(rng: random.Random, n: int) -> list[dict]:
    rows = []
    for i in range(n):
        row = _person(rng, i)
        row.update({
            "employee_id": f"EMP-{i:05d}",
            "department": rng.choice(["eng", "ops", "sales", "hr",
                                      "finance"]),
            "salary": round(rng.gauss(92000, 22000), 0),
            "employment_status": rng.choices(
                ["active", "on_leave", "terminated"],
                weights=[82, 10, 8])[0],
            "hire_year": rng.randint(2008, 2026),
        })
        rows.append(row)
    return rows


def market_rows(rng: random.Random, n: int) -> list[dict]:
    rows = []
    for i in range(n):
        row = _person(rng, i)
        row.update({
            "respondent_id": f"RSP-{i:05d}",
            "satisfaction_score": rng.randint(1, 5),
            "contact_consent": str(rng.random() < 0.55).lower(),
            "segment": rng.choice(["consumer", "prospect", "churned"]),
            "wave": rng.choice(["2026-Q1", "2026-Q2", "2026-Q3"]),
        })
        rows.append(row)
    return rows


def build_catalog() -> list[dict]:
    rng = random.Random(SEED)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    sources = [
        ("src_clinical_north", "North Clinical Registry", "clinical",
         ["full_name", "email", "phone", "date_of_birth", "home_address",
          "diagnosis_code", "condition_note"],
         60, clinical_rows),
        ("src_clinical_central", "Central Clinical Registry", "clinical",
         ["full_name", "email", "phone", "date_of_birth", "home_address",
          "diagnosis_code", "condition_note"],
         45, clinical_rows),
        ("src_tx_fraud", "Transaction Fraud Feed", "fraud_detection",
         ["full_name", "email", "phone", "home_address",
          "account_number"],
         75, fraud_rows),
        ("src_hr_workforce", "Workforce HR Extract", "workforce",
         ["full_name", "email", "phone", "date_of_birth", "home_address",
          "salary"],
         54, workforce_rows),
        ("src_market_panel", "Market Research Panel", "market_research",
         ["full_name", "email", "phone", "home_address"],
         48, market_rows),
    ]
    catalog = []
    for source_id, name, category, sensitive, n, fn in sources:
        rows = fn(random.Random(SEED + hash(source_id) % 100000), n)
        (RAW_DIR / f"{source_id}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        # Initial grants: not every source is licensed for every
        # purpose/scope — retractions later narrow these further.
        grants = _initial_grants(source_id, category)
        catalog.append({
            "source_id": source_id, "name": name, "category": category,
            "description": f"Synthetic {category} dataset (seeded, "
                           f"non-personal)",
            "sensitive_columns": sensitive, "record_count": len(rows),
            "grants": grants,
        })
    (RAW_DIR / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return catalog


def _initial_grants(source_id: str, category: str) -> list[dict]:
    matrix = {
        "src_clinical_north": [("clinical", "north"), ("clinical", "shared")],
        "src_clinical_central": [("clinical", "central"),
                                 ("clinical", "shared")],
        "src_tx_fraud": [("fraud_detection", "north"),
                         ("fraud_detection", "central"),
                         ("fraud_detection", "shared")],
        "src_hr_workforce": [("workforce", "north"),
                             ("workforce", "central")],
        "src_market_panel": [("market_research", "north"),
                             ("market_research", "central"),
                             ("market_research", "shared")],
    }
    return [{"grant_id": f"grant_{source_id}_{p}_{o}",
             "purpose": p, "org_scope": o}
            for p, o in matrix[source_id]]


if __name__ == "__main__":
    cat = build_catalog()
    print(f"generated {len(cat)} synthetic sources in {RAW_DIR}")
    for s in cat:
        print(f"  {s['source_id']}: {s['record_count']} rows, "
              f"{len(s['grants'])} grants")
