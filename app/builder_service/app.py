"""Sample Builder Service.

Builds samples row-by-row against an authorization snapshot, recording
deterministic per-row lineage. Build jobs are checkpointed and can be
interrupted (simulation) then resumed. Retraction events arrive on an
independent inbox; tainted sample versions are quarantined. Sensitive raw
values never leave this service.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..common import httpclient
from ..common.db import Database
from ..common.util import new_id, now_iso, row_lineage_id
from ..common.webserver import ApiError, HttpApp

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
STAGES = ("queued", "authorized", "materialized", "completed")


class BuilderService:
    def __init__(self, db_path: str, raw_dir: str, snapshot_url: str):
        self.db = Database(db_path)
        self.db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.raw_dir = Path(raw_dir)
        self.snapshot_url = snapshot_url
        self.app = HttpApp("builder-service")
        self._routes()

    # ------------------------------------------------------------ raw catalog
    def _raw_file(self, source_id: str) -> Path:
        return self.raw_dir / f"{source_id}.json"

    def _load_raw(self, source_id: str) -> list[dict]:
        path = self._raw_file(source_id)
        if not path.exists():
            raise ApiError(404, "raw_source_missing",
                           f"no raw data for {source_id}")
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    # ----------------------------------------------------------------- builds
    def create_build(self, source_id: str, purpose: str, org_scope: str,
                     snapshot_id: str | None = None) -> dict:
        if snapshot_id is None:
            snap = httpclient.get(f"{self.snapshot_url}/snapshots/current")
            snapshot_id = snap["snapshot_id"]
        # Gate 1: authorization is enforced server-side against the snapshot.
        auth = httpclient.post(
            f"{self.snapshot_url}/authorization/check",
            {"source_id": source_id, "purpose": purpose,
             "org_scope": org_scope, "snapshot_id": snapshot_id})
        if not auth["authorized"]:
            raise ApiError(403, "unauthorized_grant",
                           f"no active grant for {source_id}/{purpose}/"
                           f"{org_scope} at {snapshot_id}")
        job_id = new_id("job")
        self.db.insert("build_jobs", {
            "job_id": job_id, "source_id": source_id, "purpose": purpose,
            "org_scope": org_scope, "snapshot_id": snapshot_id,
            "stage": "queued", "progress": 0, "checkpoint": 0,
            "fail_stage": None, "fail_reason": None, "sample_id": None,
            "error": None, "created_at": now_iso(), "updated_at": now_iso(),
        })
        return self.run_build(job_id)

    def run_build(self, job_id: str) -> dict:
        job = self.db.query_one(
            "SELECT * FROM build_jobs WHERE job_id=?", (job_id,))
        if not job:
            raise ApiError(404, "job_not_found")
        if job["stage"] == "completed":
            return job

        fail_rule = self.db.query_one(
            "SELECT * FROM sim_fail_rules WHERE remaining>0 AND "
            "(job_id IS NULL OR job_id=?) ORDER BY created_at LIMIT 1",
            (job_id,))

        def maybe_fail(stage: str):
            if fail_rule and fail_rule["fail_stage"] == stage:
                self.db.execute(
                    "UPDATE sim_fail_rules SET remaining=remaining-1 "
                    "WHERE rule_id=?", (fail_rule["rule_id"],))
                self.db.execute(
                    "UPDATE build_jobs SET stage='interrupted', fail_stage=?, "
                    "fail_reason=?, error=?, updated_at=? WHERE job_id=?",
                    (stage, f"simulated failure at {stage}",
                     f"simulated failure at {stage}", now_iso(), job_id))
                raise ApiError(409, "build_interrupted",
                               f"build {job_id} interrupted at stage {stage}")

        if job["stage"] == "queued":
            maybe_fail("queued")
            self._set_stage(job_id, "authorized", 0)
            job["stage"] = "authorized"

        if job["stage"] in ("authorized", "interrupted"):
            # A materialized-stage rule is consumed inside the row loop so
            # the checkpoint is committed before the interruption.
            sample = self._materialize(job, fail_rule)
            self._set_stage(job_id, "materialized", 100,
                            sample_id=sample["sample_id"])
            maybe_fail("completed")
            self._set_stage(job_id, "completed", 100,
                            sample_id=sample["sample_id"])
            self.db.execute(
                "UPDATE sample_versions SET status='active' WHERE sample_id=?",
                (sample["sample_id"],))
        return self.get_job(job_id)

    def _set_stage(self, job_id: str, stage: str, progress: int,
                   sample_id: str | None = None) -> None:
        sets = ["stage=?", "progress=?", "updated_at=?"]
        params: list = [stage, progress, now_iso()]
        if sample_id is not None:
            sets.append("sample_id=?")
            params.append(sample_id)
        params.append(job_id)
        self.db.execute(
            f"UPDATE build_jobs SET {', '.join(sets)} WHERE job_id=?", params)

    def _materialize(self, job: dict, fail_rule: dict | None) -> dict:
        rows = self._load_raw(job["source_id"])
        sample_id = job["sample_id"] or new_id("samp")
        if not job["sample_id"]:
            ver_row = self.db.query_one(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM sample_versions "
                "WHERE source_id=? AND purpose=? AND org_scope=?",
                (job["source_id"], job["purpose"], job["org_scope"]))
            self.db.insert("sample_versions", {
                "sample_id": sample_id, "source_id": job["source_id"],
                "purpose": job["purpose"], "org_scope": job["org_scope"],
                "snapshot_id": job["snapshot_id"],
                "version": ver_row["v"], "status": "building",
                "row_count": 0, "retraction_id": None,
                "superseded_by": None, "created_at": now_iso(),
            })
        start = job["checkpoint"]
        # Deterministic inclusion rule: every 3rd row -> stable, reproducible
        # sample composition for a given snapshot.
        chosen = [(i, r) for i, r in enumerate(rows) if i % 3 == 0]
        total = len(chosen)
        for n, (row_index, row) in enumerate(chosen):
            if n < start:
                continue
            # Mid-materialization interruption: checkpoint rows stay committed.
            if fail_rule and fail_rule["fail_stage"] == "materialized" \
                    and fail_rule["remaining"] > 0 and n == max(total // 2, 1):
                self.db.execute(
                    "UPDATE sim_fail_rules SET remaining=remaining-1 "
                    "WHERE rule_id=?", (fail_rule["rule_id"],))
                self.db.execute(
                    "UPDATE build_jobs SET stage='interrupted', "
                    "fail_stage='materialized', checkpoint=?, progress=?, "
                    "fail_reason=?, error=?, updated_at=? WHERE job_id=?",
                    (n, int(n / total * 100),
                     "simulated failure during row materialization",
                     "simulated failure during row materialization",
                     now_iso(), job["job_id"]))
                self.db.execute(
                    "UPDATE sample_versions SET row_count=? WHERE sample_id=?",
                    (n, sample_id))
                raise ApiError(409, "build_interrupted",
                               f"build {job['job_id']} interrupted at row "
                               f"{n}/{total}")
            lineage_id = row_lineage_id(job["source_id"], row_index, row)
            existing = self.db.query_one(
                "SELECT 1 FROM row_lineage WHERE lineage_id=? AND sample_id=?",
                (lineage_id, sample_id))
            if not existing:
                self.db.insert("row_lineage", {
                    "lineage_id": lineage_id, "sample_id": sample_id,
                    "source_id": job["source_id"], "row_index": row_index,
                    "purpose": job["purpose"], "org_scope": job["org_scope"],
                    "snapshot_id": job["snapshot_id"],
                    "raw_value": json.dumps(row, ensure_ascii=False),
                    "is_sensitive": 1, "created_at": now_iso(),
                })
            self.db.execute(
                "UPDATE build_jobs SET checkpoint=?, progress=?, updated_at=? "
                "WHERE job_id=?",
                (n + 1, int((n + 1) / total * 100), now_iso(), job["job_id"]))
        self.db.execute(
            "UPDATE sample_versions SET row_count=? WHERE sample_id=?",
            (total, sample_id))
        return {"sample_id": sample_id, "row_count": total}

    def get_job(self, job_id: str) -> dict:
        job = self.db.query_one(
            "SELECT * FROM build_jobs WHERE job_id=?", (job_id,))
        if not job:
            raise ApiError(404, "job_not_found")
        return job

    def list_jobs(self) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM build_jobs ORDER BY created_at DESC")

    def add_fail_rule(self, fail_stage: str, job_id: str | None,
                      count: int) -> dict:
        if fail_stage not in STAGES:
            raise ApiError(400, "bad_stage", f"one of {STAGES}")
        rule_id = new_id("fail")
        self.db.insert("sim_fail_rules", {
            "rule_id": rule_id, "job_id": job_id,
            "fail_stage": fail_stage, "remaining": count,
            "created_at": now_iso(),
        })
        return {"rule_id": rule_id, "fail_stage": fail_stage,
                "job_id": job_id, "remaining": count}

    # ---------------------------------------------------------------- samples
    def list_samples(self, include_quarantined: bool = True) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM sample_versions ORDER BY created_at DESC")

    def get_sample(self, sample_id: str) -> dict:
        sample = self.db.query_one(
            "SELECT * FROM sample_versions WHERE sample_id=?", (sample_id,))
        if not sample:
            raise ApiError(404, "sample_not_found")
        sample["lineage"] = self.db.query_all(
            "SELECT lineage_id,source_id,row_index,purpose,org_scope,"
            "snapshot_id,created_at FROM row_lineage WHERE sample_id=? "
            "ORDER BY row_index", (sample_id,))
        return sample

    def get_lineage(self, lineage_id: str) -> dict:
        # External view: raw_value is never exposed. Prefer the lineage
        # materialization belonging to an active sample version.
        row = self.db.query_one(
            "SELECT rl.lineage_id,rl.sample_id,rl.source_id,rl.row_index,"
            "rl.purpose,rl.org_scope,rl.snapshot_id,rl.created_at "
            "FROM row_lineage rl JOIN sample_versions sv "
            "ON sv.sample_id = rl.sample_id "
            "WHERE rl.lineage_id=? "
            "ORDER BY CASE sv.status WHEN 'active' THEN 0 ELSE 1 END, "
            "sv.created_at DESC LIMIT 1",
            (lineage_id,))
        if not row:
            raise ApiError(404, "lineage_not_found")
        return row

    # ------------------------------------------------------------ retractions
    def consume_inbox(self) -> dict:
        pending = self.db.query_all(
            "SELECT * FROM event_inbox WHERE consumed=0 ORDER BY received_at")
        results = []
        for evt in pending:
            envelope = json.loads(evt["payload"])
            if evt["event_type"] == "license.retracted":
                results.append(self._apply_retraction(envelope["payload"]))
            self.db.execute(
                "UPDATE event_inbox SET consumed=1, consumed_at=? "
                "WHERE event_id=?", (now_iso(), evt["event_id"]))
        return {"consumed": len(pending), "results": results}

    def _apply_retraction(self, payload: dict) -> dict:
        src = payload["source_id"]
        purposes = payload["scope_purposes"]
        orgs = payload["scope_orgs"]
        samples = self.db.query_all(
            "SELECT * FROM sample_versions WHERE source_id=? AND status IN "
            "('active','building')", (src,))
        quarantined = []
        for s in samples:
            if purposes and s["purpose"] not in purposes:
                continue
            if orgs and s["org_scope"] not in orgs:
                continue
            self.db.execute(
                "UPDATE sample_versions SET status='quarantined', "
                "retraction_id=? WHERE sample_id=?",
                (payload["retraction_id"], s["sample_id"]))
            quarantined.append(s["sample_id"])
        return {"retraction_id": payload["retraction_id"],
                "quarantined_samples": quarantined}

    def rebuild_safe_sample(self, old_sample_id: str,
                            new_snapshot_id: str | None = None) -> dict:
        """Rebuild from rows whose grants survive on the new snapshot."""
        old = self.db.query_one(
            "SELECT * FROM sample_versions WHERE sample_id=?",
            (old_sample_id,))
        if not old:
            raise ApiError(404, "sample_not_found")
        snap_id = new_snapshot_id
        if snap_id is None:
            snap = httpclient.get(f"{self.snapshot_url}/snapshots/current")
            snap_id = snap["snapshot_id"]
        lineage_rows = self.db.query_all(
            "SELECT * FROM row_lineage WHERE sample_id=? ORDER BY row_index",
            (old_sample_id,))
        new_sample_id = new_id("samp")
        ver_row = self.db.query_one(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM sample_versions "
            "WHERE source_id=? AND purpose=? AND org_scope=?",
            (old["source_id"], old["purpose"], old["org_scope"]))
        self.db.insert("sample_versions", {
            "sample_id": new_sample_id, "source_id": old["source_id"],
            "purpose": old["purpose"], "org_scope": old["org_scope"],
            "snapshot_id": snap_id, "version": ver_row["v"],
            "status": "active", "row_count": 0, "retraction_id": None,
            "superseded_by": None, "created_at": now_iso(),
        })
        kept, dropped = [], []
        for lr in lineage_rows:
            auth = httpclient.post(
                f"{self.snapshot_url}/authorization/check",
                {"source_id": lr["source_id"], "purpose": lr["purpose"],
                 "org_scope": lr["org_scope"], "snapshot_id": snap_id})
            if not auth["authorized"]:
                dropped.append(lr["lineage_id"])
                continue
            row = dict(lr)
            row.update({"sample_id": new_sample_id, "snapshot_id": snap_id,
                        "created_at": now_iso()})
            self.db.insert("row_lineage", row)
            kept.append(lr["lineage_id"])
        self.db.execute(
            "UPDATE sample_versions SET row_count=? WHERE sample_id=?",
            (len(kept), new_sample_id))
        self.db.execute(
            "UPDATE sample_versions SET status='superseded', superseded_by=? "
            "WHERE sample_id=?", (new_sample_id, old_sample_id))
        return {"new_sample_id": new_sample_id, "retained_rows": len(kept),
                "removed_rows": len(dropped), "removed_lineage_ids": dropped,
                "snapshot_id": snap_id}

    # --------------------------------------------------------- purpose change
    def request_purpose_change(self, lineage_id: str, new_purpose: str) -> dict:
        # A lineage id can appear in several sample versions (rebuilds).
        # Resolve against the most recent materialization so the org_scope
        # reflects the current authorized sample, not a superseded one.
        lr = self.db.query_one(
            "SELECT rl.* FROM row_lineage rl "
            "JOIN sample_versions sv ON sv.sample_id = rl.sample_id "
            "WHERE rl.lineage_id=? "
            "ORDER BY sv.created_at DESC, rl.created_at DESC LIMIT 1",
            (lineage_id,))
        if not lr:
            raise ApiError(404, "lineage_not_found")
        snap = httpclient.get(f"{self.snapshot_url}/snapshots/current")
        auth = httpclient.post(
            f"{self.snapshot_url}/authorization/check",
            {"source_id": lr["source_id"], "purpose": new_purpose,
             "org_scope": lr["org_scope"], "snapshot_id": snap["snapshot_id"]})
        request_id = new_id("pchg")
        if auth["authorized"]:
            new_lineage_id = row_lineage_id(
                lr["source_id"], lr["row_index"],
                json.loads(lr["raw_value"]))
            status, reason, new_ln = (
                "approved", None, new_lineage_id)
        else:
            status, reason, new_ln = (
                "rejected",
                f"no active grant for purpose {new_purpose} on snapshot "
                f"{snap['snapshot_id']}", None)
        self.db.insert("purpose_changes", {
            "request_id": request_id, "lineage_id": lineage_id,
            "sample_id": lr["sample_id"], "old_purpose": lr["purpose"],
            "new_purpose": new_purpose, "org_scope": lr["org_scope"],
            "snapshot_id": snap["snapshot_id"], "status": status,
            "rejection_reason": reason, "new_lineage_id": new_ln,
            "created_at": now_iso(),
        })
        return {"request_id": request_id, "status": status,
                "rejection_reason": reason, "new_lineage_id": new_ln,
                "snapshot_id": snap["snapshot_id"]}

    def list_purpose_changes(self) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM purpose_changes ORDER BY created_at DESC")

    # -------------------------------------------------- internal: report pulls
    def internal_sample_rows(self, sample_id: str,
                             only_lineage_ids: list[str] | None = None) -> dict:
        """Service-to-service: report service needs raw values to compute
        real metrics. This endpoint is not exposed through the web gateway."""
        sample = self.db.query_one(
            "SELECT * FROM sample_versions WHERE sample_id=?", (sample_id,))
        if not sample:
            raise ApiError(404, "sample_not_found")
        sql = ("SELECT lineage_id,row_index,raw_value,source_id,purpose,"
               "org_scope,snapshot_id FROM row_lineage WHERE sample_id=?")
        params: list = [sample_id]
        if only_lineage_ids:
            sql += (" AND lineage_id IN ("
                    + ",".join("?" * len(only_lineage_ids)) + ")")
            params.extend(only_lineage_ids)
        sql += " ORDER BY row_index"
        rows = []
        for r in self.db.query_all(sql, params):
            r["raw_value"] = json.loads(r["raw_value"])
            rows.append(r)
        return {"sample": sample, "rows": rows}

    # ------------------------------------------------------------------ routes
    def _routes(self) -> None:
        app = self.app

        @app.route("GET", "/health")
        def _health(_ctx):
            return {"service": "builder", "status": "ok"}

        @app.route("POST", "/builds")
        def _create(ctx):
            b = ctx.body
            return 202, self.create_build(
                b["source_id"], b["purpose"], b["org_scope"],
                b.get("snapshot_id"))

        @app.route("POST", "/builds/{job_id}/run")
        def _run(ctx):
            return self.run_build(ctx.params["job_id"])

        @app.route("GET", "/builds/{job_id}")
        def _job(ctx):
            return self.get_job(ctx.params["job_id"])

        @app.route("GET", "/builds")
        def _jobs(_ctx):
            return {"jobs": self.list_jobs()}

        @app.route("POST", "/sim/fail")
        def _fail(ctx):
            b = ctx.body
            return self.add_fail_rule(
                b["fail_stage"], b.get("job_id"), int(b.get("count", 1)))

        @app.route("GET", "/samples")
        def _samples(_ctx):
            return {"samples": self.list_samples()}

        @app.route("GET", "/samples/{sample_id}")
        def _sample(ctx):
            return self.get_sample(ctx.params["sample_id"])

        @app.route("GET", "/lineage/{lineage_id}")
        def _lineage(ctx):
            return self.get_lineage(ctx.params["lineage_id"])

        @app.route("POST", "/samples/{sample_id}/rebuild-safe")
        def _rebuild(ctx):
            return self.rebuild_safe_sample(
                ctx.params["sample_id"], ctx.body.get("snapshot_id"))

        @app.route("POST", "/purpose-changes")
        def _pchg(ctx):
            b = ctx.body
            return self.request_purpose_change(
                b["lineage_id"], b["new_purpose"])

        @app.route("GET", "/purpose-changes")
        def _pchgs(_ctx):
            return {"requests": self.list_purpose_changes()}

        @app.route("POST", "/inbox/consume")
        def _consume(_ctx):
            return self.consume_inbox()

        @app.route("GET", "/inbox")
        def _inbox(_ctx):
            return {"events": self.db.query_all(
                "SELECT event_id,event_type,consumed,received_at,consumed_at "
                "FROM event_inbox ORDER BY received_at")}

        @app.route("POST", "/internal/events")
        def _events(ctx):
            evt = ctx.body
            self.db.execute(
                "INSERT OR IGNORE INTO event_inbox(event_id,event_type,"
                "payload,consumed,received_at) VALUES(?,?,?,0,?)",
                (evt["event_id"], evt["event_type"],
                 json.dumps(evt, ensure_ascii=False), now_iso()))
            return {"accepted": True, "event_id": evt["event_id"]}

        @app.route("POST", "/internal/samples/{sample_id}/rows")
        def _rows(ctx):
            return self.internal_sample_rows(
                ctx.params["sample_id"], ctx.body.get("lineage_ids"))


def build_service(runtime_dir: str, raw_dir: str) -> BuilderService:
    return BuilderService(
        db_path=os.path.join(runtime_dir, "builder.db"),
        raw_dir=raw_dir,
        snapshot_url=os.environ.get("SNAPSHOT_URL",
                                    "http://127.0.0.1:8071"),
    )
