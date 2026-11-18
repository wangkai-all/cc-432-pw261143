"""Seed already-running services with the synthetic catalog and baseline
snapshot. Idempotent: sources already registered are skipped.

Usage: python -m app.bootstrap.seed
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.common import httpclient

SNAP = "http://127.0.0.1:8071"
RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"


def main() -> None:
    for _ in range(50):
        try:
            httpclient.get(f"{SNAP}/health")
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise SystemExit("snapshot service is not running")

    catalog = json.loads((RAW_DIR / "catalog.json").read_text(encoding="utf-8"))
    existing = {s["source_id"]
                for s in httpclient.get(f"{SNAP}/sources")["sources"]}
    for src in catalog:
        if src["source_id"] in existing:
            print(f"skip existing source {src['source_id']}")
            continue
        httpclient.post(f"{SNAP}/sources", src)
        print(f"registered {src['source_id']} "
              f"({src['record_count']} rows, {len(src['grants'])} grants)")
    snaps = httpclient.get(f"{SNAP}/snapshots")["snapshots"]
    if not snaps:
        snap = httpclient.post(f"{SNAP}/admin/snapshots",
                               {"note": "baseline"})
        print(f"baseline snapshot: {snap['snapshot_id']} "
              f"({snap['grant_count']} grants)")
    else:
        print(f"snapshot already exists: {snaps[-1]['snapshot_id']}")


if __name__ == "__main__":
    sys.exit(main())
