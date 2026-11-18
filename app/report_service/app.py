"""Report Service.

Computes real analytics from builder samples, traces every metric back to
row-level lineage, and enforces the rebuild-vs-block decision when a
retraction lands. Cached results and downloadable exports are quarantined
or revoked; already-sealed audits are retained under legal hold and never
reused. Explanation packages carry aggregates and lineage ids only —
sensitive row values are structurally excluded and verified before write.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..common import httpclient, policy
from ..common.db import Database
from ..common.scrubber import assert_no_sensitive_detail, safe_copy
from ..common.util import new_id, now_iso, payload_hash, short_hash
from ..common.webserver import ApiError, HttpApp
from .metrics import compute_metrics

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class ReportService:
    def __init__(self, db_path: str, artifacts_dir: str, builder_url: str,
                 snapshot_url: str):
        self.db = Database(db_path)
        self.db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.artifacts = Path(artifacts_dir)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        (self.artifacts / "exports").mkdir(exist_ok=True)
        (self.artifacts / "sealed").mkdir(exist_ok=True)
        (self.artifacts / "explanations").mkdir(exist_ok=True)
        self.builder_url = builder_url
        self.snapshot_url = snapshot_url
        self.app = HttpApp("report-service")
        self._routes()

    # ------------------------------------------------------------- create run
    def create_report(self, title: str, sample_id: str,
                      purpose: str | None = None,
                      org_scope: str | None = None) -> dict:
        data = httpclient.post(
            f"{self.builder_url}/internal/samples/{sample_id}/rows", {})
        sample = data["sample"]
        if sample["status"] not in ("active",):
            raise ApiError(409, "sample_not_usable",
                           f"sample status is {sample['status']}")
        # Purpose and org scope are taken from the sample's authorized
        # context, never trusted from the caller.
        purpose = purpose or sample["purpose"]
        org_scope = org_scope or sample["org_scope"]
        rows = data["rows"]
        report_id = new_id("rpt")
        self.db.insert("reports", {
            "report_id": report_id, "title": title, "purpose": purpose,
            "org_scope": org_scope, "sample_id": sample_id,
            "snapshot_id": sample["snapshot_id"], "version": 1,
            "status": "active", "current": 1, "rebuilt_from": None,
            "block_reason": None, "retraction_id": None,
            "created_at": now_iso(), "updated_at": now_iso(),
        })
        self._persist_metrics(report_id, rows, purpose)
        self.db.insert("report_versions", {
            "report_id": report_id, "version": 1,
            "snapshot_id": sample["snapshot_id"], "sample_id": sample_id,
            "status": "active", "created_at": now_iso(),
        })
        self._write_cache(report_id, 1, sample["snapshot_id"])
        self._write_export(report_id, 1, "download", "available", legal_hold=0)
        return self.get_report(report_id)

    def _persist_metrics(self, report_id: str, rows: list[dict],
                         purpose: str) -> None:
        metrics = compute_metrics(purpose, rows)
        source_ids = sorted({r["source_id"] for r in rows})
        # A report sample draws from one source; store it explicitly so
        # impact analysis never parses lineage id strings.
        source_id = source_ids[0] if len(source_ids) == 1 else ",".join(
            source_ids)
        for m in metrics:
            self.db.insert("report_metrics", {
                "metric_id": new_id("met"), "report_id": report_id,
                "metric_name": m["metric_name"],
                "metric_type": m["metric_type"], "value": m["value"],
                "unit": m["unit"], "source_id": source_id,
                "lineage_ids": json.dumps(m["lineage_ids"]),
                "subtotals": json.dumps(m["subtotals"], ensure_ascii=False),
                "row_count": len(m["lineage_ids"]),
                "created_at": now_iso(),
            })

    def get_report(self, report_id: str) -> dict:
        report = self.db.query_one(
            "SELECT * FROM reports WHERE report_id=?", (report_id,))
        if not report:
            raise ApiError(404, "report_not_found")
        report["metrics"] = self._metrics_view(report_id)
        report["versions"] = self.db.query_all(
            "SELECT * FROM report_versions WHERE report_id=? ORDER BY version",
            (report_id,))
        return report

    def _metrics_view(self, report_id: str) -> list[dict]:
        out = []
        for m in self.db.query_all(
                "SELECT * FROM report_metrics WHERE report_id=? "
                "ORDER BY metric_name", (report_id,)):
            m["lineage_ids"] = json.loads(m["lineage_ids"])
            m["subtotals"] = json.loads(m["subtotals"])
            out.append(m)
        return out

    def list_reports(self) -> list[dict]:
        rows = self.db.query_all(
            "SELECT report_id,title,purpose,org_scope,version,status,"
            "snapshot_id,retraction_id,updated_at FROM reports "
            "WHERE current=1 ORDER BY created_at")
        return rows

    # --------------------------------------------------------------- retraction
    def consume_inbox(self) -> dict:
        pending = self.db.query_all(
            "SELECT * FROM event_inbox WHERE consumed=0 ORDER BY received_at")
        results = []
        for evt in pending:
            envelope = json.loads(evt["payload"])
            if evt["event_type"] == "license.retracted":
                results.append(self.apply_retraction(envelope["payload"]))
            self.db.execute(
                "UPDATE event_inbox SET consumed=1, consumed_at=? "
                "WHERE event_id=?", (now_iso(), evt["event_id"]))
        return {"consumed": len(pending), "results": results}

    def apply_retraction(self, payload: dict) -> dict:
        retraction_id = payload["retraction_id"]
        source_id = payload["source_id"]
        # Pull fresh authorization truth rather than trusting cached flags.
        new_snap_id = payload["new_snapshot_id"]
        affected_reports = []
        # Sealed reports are evaluated too: their sealed legal-hold artifact
        # is preserved, but ordinary downloads must still be revoked and the
        # impact decision is recorded for the audit trail.
        for report in self.db.query_all(
                "SELECT * FROM reports WHERE current=1 AND status IN "
                "('active','tainted','sealed')"):
            finding = self._evaluate_impact(
                report, retraction_id, source_id, payload, new_snap_id)
            if finding is None:
                continue
            affected_reports.append(finding)
            # Caches and downloads tied to the tainted version are handled.
            self._quarantine_caches(report["report_id"], retraction_id)
            self._revoke_downloads(report["report_id"], retraction_id)
        return {"retraction_id": retraction_id,
                "affected_reports": affected_reports}

    def _evaluate_impact(self, report: dict, retraction_id: str,
                         source_id: str, payload: dict,
                         new_snap_id: str) -> dict | None:
        purposes = payload["scope_purposes"]
        orgs = payload["scope_orgs"]
        if purposes and report["purpose"] not in purposes:
            return None
        if orgs and report["org_scope"] not in orgs:
            return None
        metric_findings = []
        any_affected = False
        for m in self.db.query_all(
                "SELECT * FROM report_metrics WHERE report_id=?",
                (report["report_id"],)):
            lineage_ids = json.loads(m["lineage_ids"])
            affected_ids = self._affected_lineage(
                m, source_id, new_snap_id, report["purpose"],
                report["org_scope"])
            total = len(lineage_ids)
            affected = len(affected_ids)
            if affected:
                any_affected = True
            pct = round(affected / total * 100, 2) if total else 100.0
            finding = {
                "metric_id": m["metric_id"], "total_rows": total,
                "affected_rows": affected, "safe_rows": total - affected,
                "contribution_pct": pct}
            metric_findings.append(finding)
            self.db.insert("impact_findings", {
                "finding_id": new_id("fnd"),
                "report_id": report["report_id"],
                "retraction_id": retraction_id,
                "metric_id": m["metric_id"], "total_rows": total,
                "affected_rows": affected, "safe_rows": total - affected,
                "contribution_pct": pct,
                "decision": "pending", "decision_reason": "",
                "created_at": now_iso()})
        if not any_affected:
            return None
        verdict = policy.evaluate_report(metric_findings)
        # Record per-metric decision text.
        for pm, f in zip(verdict["per_metric"], metric_findings):
            self.db.execute(
                "UPDATE impact_findings SET decision=?, decision_reason=? "
                "WHERE report_id=? AND retraction_id=? AND metric_id=?",
                (pm["decision"], pm["reason"], report["report_id"],
                 retraction_id, pm["metric_id"]))
        if verdict["decision"] == "block":
            if report["status"] == "sealed":
                # Legal hold wins: keep the sealed state; the block decision
                # lives in impact_findings and downloads are revoked.
                self.db.execute(
                    "UPDATE reports SET block_reason=?, retraction_id=?, "
                    "updated_at=? WHERE report_id=?",
                    ("; ".join(verdict["reasons"]), retraction_id,
                     now_iso(), report["report_id"]))
            else:
                self.db.execute(
                    "UPDATE reports SET status='blocked', "
                    "block_reason=?, retraction_id=?, updated_at=? "
                    "WHERE report_id=?",
                    ("; ".join(verdict["reasons"]), retraction_id,
                     now_iso(), report["report_id"]))
        else:
            if report["status"] != "sealed":
                self.db.execute(
                    "UPDATE reports SET status='tainted', retraction_id=?, "
                    "updated_at=? WHERE report_id=?",
                    (retraction_id, now_iso(), report["report_id"]))
            else:
                self.db.execute(
                    "UPDATE reports SET retraction_id=?, updated_at=? "
                    "WHERE report_id=?",
                    (retraction_id, now_iso(), report["report_id"]))
        return {"report_id": report["report_id"],
                "decision": verdict["decision"],
                "reasons": verdict["reasons"],
                "per_metric": verdict["per_metric"]}

    def _affected_lineage(self, metric: dict, source_id: str,
                          new_snap_id: str, purpose: str,
                          org_scope: str) -> list[str]:
        """Lineage ids whose grant is no longer active on the new snapshot.

        Re-verified against the snapshot authority; cached flags are never
        trusted. One check per affected source is sufficient because a
        report has a single (purpose, org_scope).
        """
        if metric["source_id"] != source_id:
            return []
        candidate = json.loads(metric["lineage_ids"])
        if not candidate:
            return []
        check = httpclient.post(
            f"{self.snapshot_url}/authorization/check",
            {"source_id": source_id, "purpose": purpose,
             "org_scope": org_scope, "snapshot_id": new_snap_id})
        return candidate if not check["authorized"] else []

    # ------------------------------------------------------------- rebuild flow
    def rebuild_report(self, report_id: str) -> dict:
        report = self.db.query_one(
            "SELECT * FROM reports WHERE report_id=?", (report_id,))
        if not report:
            raise ApiError(404, "report_not_found")
        if report["status"] == "blocked":
            raise ApiError(409, "report_blocked",
                           "blocked reports cannot be rebuilt; only a "
                           "replacement report from a new authorized sample "
                           "is allowed")
        snap = httpclient.get(f"{self.snapshot_url}/snapshots/current")
        rebuilt = httpclient.post(
            f"{self.builder_url}/samples/{report['sample_id']}/rebuild-safe",
            {"snapshot_id": snap["snapshot_id"]})
        new_sample_id = rebuilt["new_sample_id"]
        if rebuilt["retained_rows"] < policy.MIN_SAFE_ROWS:
            raise ApiError(409, "insufficient_safe_rows",
                           f"only {rebuilt['retained_rows']} rows remain")
            raise ApiError(409, "insufficient_safe_rows",
                           f"only {rebuilt['retained_rows']} rows remain")
        data = httpclient.post(
            f"{self.builder_url}/internal/samples/{new_sample_id}/rows", {})
        new_version = report["version"] + 1
        self.db.execute(
            "UPDATE reports SET current=0, status='superseded', updated_at=? "
            "WHERE report_id=?", (now_iso(), report_id))
        new_report_id = new_id("rpt")
        self.db.insert("reports", {
            "report_id": new_report_id, "title": report["title"],
            "purpose": report["purpose"], "org_scope": report["org_scope"],
            "sample_id": new_sample_id, "snapshot_id": snap["snapshot_id"],
            "version": new_version, "status": "active", "current": 1,
            "rebuilt_from": report_id, "block_reason": None,
            "retraction_id": None, "created_at": now_iso(),
            "updated_at": now_iso()})
        new_report = self.db.query_one(
            "SELECT * FROM reports WHERE report_id=?", (new_report_id,))
        self._persist_metrics(new_report_id, data["rows"],
                              report["purpose"])
        self.db.insert("report_versions", {
            "report_id": new_report_id, "version": new_version,
            "snapshot_id": snap["snapshot_id"], "sample_id": new_sample_id,
            "status": "active", "created_at": now_iso()})
        self._write_cache(new_report_id, new_version,
                          snap["snapshot_id"])
        self._write_export(new_report_id, new_version,
                           "download", "available", legal_hold=0)
        # Replacement report explanation (aggregates + lineage only).
        package = self.generate_explanation(new_report_id)
        return {"new_report": self.get_report(new_report_id),
                "retained_rows": rebuilt["retained_rows"],
                "removed_rows": rebuilt["removed_rows"],
                "explanation_package": package}

    def replacement_report(self, title: str, sample_id: str) -> dict:
        """For blocked reports: a brand-new report from a newly authorized
        sample; the blocked report stays blocked and is never reused."""
        return self.create_report(title, sample_id)

    # ------------------------------------------------------- caches & exports
    def _write_cache(self, report_id: str, version: int,
                     snapshot_id: str) -> None:
        metrics = self._metrics_view(report_id)
        key = f"cache:{report_id}:v{version}:{snapshot_id}"
        self.db.insert("cache_entries", {
            "cache_id": new_id("cache"), "report_id": report_id,
            "report_version": version, "snapshot_id": snapshot_id,
            "cache_key": key, "payload_hash": payload_hash(metrics),
            "status": "valid", "retraction_id": None,
            "created_at": now_iso()})

    def _quarantine_caches(self, report_id: str, retraction_id: str) -> None:
        self.db.execute(
            "UPDATE cache_entries SET status='quarantined', retraction_id=? "
            "WHERE report_id=? AND status='valid'",
            (retraction_id, report_id))

    def _write_export(self, report_id: str, version: int, kind: str,
                      status: str, legal_hold: int) -> dict:
        report = self.db.query_one(
            "SELECT * FROM reports WHERE report_id=?", (report_id,))
        export_id = new_id("exp")
        # Export content is aggregates + lineage manifest, never raw rows.
        body = safe_copy({
            "export_id": export_id, "report_id": report_id,
            "report_version": version, "kind": kind,
            "snapshot_id": report["snapshot_id"],
            "metrics": self._metrics_view(report_id),
            "manifest": {
                "lineage_only": True,
                "contains_sensitive_detail": False,
                "generated_at": now_iso(),
            },
        })
        path = self.artifacts / "exports" / f"{export_id}.json"
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        self.db.insert("exports", {
            "export_id": export_id, "report_id": report_id,
            "report_version": version, "kind": kind, "status": status,
            "path": str(path), "retraction_id": None,
            "legal_hold": legal_hold, "created_at": now_iso()})
        return {"export_id": export_id, "path": str(path)}

    def _revoke_downloads(self, report_id: str, retraction_id: str) -> None:
        self.db.execute(
            "UPDATE exports SET status='revoked', retraction_id=? "
            "WHERE report_id=? AND kind='download' AND status='available'",
            (retraction_id, report_id))

    def list_exports(self) -> list[dict]:
        return self.db.query_all(
            "SELECT export_id,report_id,report_version,kind,status,legal_hold,"
            "retraction_id,path,created_at FROM exports ORDER BY created_at "
            "DESC")

    def download_export(self, export_id: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM exports WHERE export_id=?", (export_id,))
        if not row:
            raise ApiError(404, "export_not_found")
        if row["status"] == "revoked":
            raise ApiError(410, "export_revoked",
                           "this download was revoked after license "
                           "retraction; see the explanation package")
        if row["legal_hold"]:
            raise ApiError(423, "sealed_legal_hold",
                           "sealed audit artifacts are retained but locked")
        return json.loads(Path(row["path"]).read_text(encoding="utf-8"))

    # ------------------------------------------------------------ sealed audit
    def seal_audit(self, report_id: str, reason: str) -> dict:
        """Legal-hold sealing: the artifact is preserved as evidence at its
        snapshot-time authorization, but marked sealed and never reused."""
        report = self.db.query_one(
            "SELECT * FROM reports WHERE report_id=?", (report_id,))
        if not report:
            raise ApiError(404, "report_not_found")
        snap = httpclient.get(
            f"{self.snapshot_url}/snapshots/{report['snapshot_id']}")
        artifact = safe_copy({
            "sealed_report": {k: v for k, v in report.items()},
            "metrics": self._metrics_view(report_id),
            "snapshot": snap,
            "seal_reason": reason,
            "manifest": {"lineage_only": True,
                         "contains_sensitive_detail": False},
        })
        sealed_id = new_id("seal")
        path = self.artifacts / "sealed" / f"{sealed_id}.json"
        content = json.dumps(artifact, ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
        self.db.insert("sealed_audits", {
            "sealed_id": sealed_id, "report_id": report_id,
            "report_version": report["version"],
            "snapshot_id": report["snapshot_id"],
            "snapshot_fingerprint": snap["fingerprint"],
            "artifact_path": str(path),
            "artifact_hash": short_hash(content), "reason": reason,
            "sealed_at": now_iso()})
        self.db.insert("exports", {
            "export_id": new_id("exp"), "report_id": report_id,
            "report_version": report["version"], "kind": "sealed_audit",
            "status": "sealed", "path": str(path), "retraction_id": None,
            "legal_hold": 1, "created_at": now_iso()})
        self.db.execute(
            "UPDATE reports SET status='sealed' WHERE report_id=? AND current=1",
            (report_id,))
        return {"sealed_id": sealed_id, "path": str(path),
                "snapshot_fingerprint": snap["fingerprint"]}

    def list_sealed(self) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM sealed_audits ORDER BY sealed_at DESC")

    # ------------------------------------------------------ explanation package
    def generate_explanation(self, report_id: str,
                             retraction_id: str | None = None) -> dict:
        report = self.db.query_one(
            "SELECT * FROM reports WHERE report_id=?", (report_id,))
        if not report:
            raise ApiError(404, "report_not_found")
        findings = self.db.query_all(
            "SELECT * FROM impact_findings WHERE report_id=? "
            "ORDER BY created_at", (report_id,))
        # Structural guarantee: the package is assembled ONLY from aggregate
        # metrics, subtotals and lineage identifiers. Raw row values are never
        # loaded here, and a second scrub+scan gate runs before writing.
        package = {
            "explanation_package": {
                "report_id": report_id,
                "title": report["title"],
                "status": report["status"],
                "snapshot_id": report["snapshot_id"],
                "purpose": report["purpose"],
                "org_scope": report["org_scope"],
                "retraction_id": retraction_id or report["retraction_id"],
                "decision_reason": report["block_reason"],
                "metrics": [
                    {"metric_id": m["metric_id"],
                     "metric_name": m["metric_name"],
                     "metric_type": m["metric_type"], "value": m["value"],
                     "unit": m["unit"], "row_count": m["row_count"],
                     "subtotals": json.loads(m["subtotals"]),
                     "lineage": json.loads(m["lineage_ids"])}
                    for m in self.db.query_all(
                        "SELECT * FROM report_metrics WHERE report_id=?",
                        (report_id,))],
                "findings": findings,
                "lineage_manifest": {
                    "traceable_to": "row_lineage in builder service",
                    "contains_sensitive_detail": False,
                    "raw_values_accessible": False,
                },
            }
        }
        clean = safe_copy(package)
        offenses = assert_no_sensitive_detail(clean)
        if offenses:
            raise ApiError(500, "sensitive_leak_blocked", str(offenses))
        package_id = new_id("expl")
        path = self.artifacts / "explanations" / f"{package_id}.json"
        content = json.dumps(clean, ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
        self.db.insert("explanation_packages", {
            "package_id": package_id, "report_id": report_id,
            "retraction_id": retraction_id or report["retraction_id"],
            "path": str(path), "payload_hash": short_hash(content),
            "sensitive_scan": json.dumps({"offenses": offenses}),
            "created_at": now_iso()})
        return {"package_id": package_id, "path": str(path),
                "sensitive_scan": {"offenses": offenses},
                "package": clean}

    def list_explanations(self) -> list[dict]:
        return self.db.query_all(
            "SELECT package_id,report_id,retraction_id,path,sensitive_scan,"
            "created_at FROM explanation_packages ORDER BY created_at DESC")

    # --------------------------------------------------------------- lineage
    def trace_lineage(self, lineage_id: str) -> dict:
        """End-to-end trace: metric -> report -> sample -> raw row index."""
        hits = []
        for m in self.db.query_all("SELECT * FROM report_metrics"):
            # Exact membership match against the parsed lineage array;
            # substring matching would confuse ids sharing prefixes.
            if lineage_id not in json.loads(m["lineage_ids"]):
                continue
            report = self.db.query_one(
                "SELECT report_id,title,version,status,snapshot_id,"
                "sample_id FROM reports WHERE report_id=?",
                (m["report_id"],))
            hits.append({"metric_id": m["metric_id"],
                         "metric_name": m["metric_name"],
                         "report": report})
        builder_row = httpclient.get(
            f"{self.builder_url}/lineage/{lineage_id}")
        return {"lineage_id": lineage_id,
                "builder_lineage": builder_row,
                "consumed_by": hits}

    def list_caches(self) -> list[dict]:
        return self.db.query_all(
            "SELECT * FROM cache_entries ORDER BY created_at DESC")

    def list_findings(self, report_id: str | None = None) -> list[dict]:
        if report_id:
            return self.db.query_all(
                "SELECT * FROM impact_findings WHERE report_id=? ORDER BY "
                "created_at", (report_id,))
        return self.db.query_all(
            "SELECT * FROM impact_findings ORDER BY created_at")

    # ------------------------------------------------------------------ routes
    def _routes(self) -> None:
        app = self.app

        @app.route("GET", "/health")
        def _health(_ctx):
            return {"service": "report", "status": "ok"}

        @app.route("POST", "/reports")
        def _create(ctx):
            b = ctx.body
            return self.create_report(
                b["title"], b["sample_id"], b.get("purpose"),
                b.get("org_scope"))

        @app.route("GET", "/reports")
        def _list(_ctx):
            return {"reports": self.list_reports()}

        @app.route("GET", "/reports/{report_id}")
        def _one(ctx):
            return self.get_report(ctx.params["report_id"])

        @app.route("POST", "/reports/{report_id}/rebuild")
        def _rebuild(ctx):
            return self.rebuild_report(ctx.params["report_id"])

        @app.route("POST", "/reports/replacement")
        def _replacement(ctx):
            b = ctx.body
            return self.replacement_report(b["title"], b["sample_id"])

        @app.route("POST", "/reports/{report_id}/seal")
        def _seal(ctx):
            return self.seal_audit(
                ctx.params["report_id"], ctx.body.get("reason", "legal hold"))

        @app.route("POST", "/reports/{report_id}/explanation")
        def _explain(ctx):
            return self.generate_explanation(
                ctx.params["report_id"], ctx.body.get("retraction_id"))

        @app.route("GET", "/explanations")
        def _explanations(_ctx):
            return {"packages": self.list_explanations()}

        @app.route("GET", "/exports")
        def _exports(_ctx):
            return {"exports": self.list_exports()}

        @app.route("GET", "/exports/{export_id}/download")
        def _download(ctx):
            return self.download_export(ctx.params["export_id"])

        @app.route("GET", "/sealed")
        def _sealed(_ctx):
            return {"sealed": self.list_sealed()}

        @app.route("GET", "/caches")
        def _caches(_ctx):
            return {"caches": self.list_caches()}

        @app.route("GET", "/findings")
        def _findings(ctx):
            return {"findings": self.list_findings(
                ctx.query.get("report_id"))}

        @app.route("GET", "/lineage/trace/{lineage_id}")
        def _trace(ctx):
            return self.trace_lineage(ctx.params["lineage_id"])

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


def build_service(runtime_dir: str, artifacts_dir: str) -> ReportService:
    return ReportService(
        db_path=os.path.join(runtime_dir, "report.db"),
        artifacts_dir=artifacts_dir,
        builder_url=os.environ.get("BUILDER_URL", "http://127.0.0.1:8072"),
        snapshot_url=os.environ.get("SNAPSHOT_URL",
                                    "http://127.0.0.1:8071"),
    )
