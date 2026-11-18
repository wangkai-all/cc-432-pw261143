"""Shared utility helpers: ids, timestamps, json, hashing."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

LINEAGE_PREFIX = "LN"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_lineage_id(source_id: str, row_index: int, row_payload: dict) -> str:
    """Deterministic lineage id for a raw row: LN_<source>_<sha12 of canonical row>."""
    digest = hashlib.sha1(canonical_json(row_payload).encode("utf-8")).hexdigest()[:12]
    return f"{LINEAGE_PREFIX}_{source_id}_{row_index:04d}_{digest}"


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
