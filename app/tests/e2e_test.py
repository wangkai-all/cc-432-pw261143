"""End-to-end test: full compliance flow over real HTTP services.

Run: python -m app.tests.e2e_test
Starts the three services itself against throwaway data directories.
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.bootstrap.generate_fixtures import build_catalog  # noqa: E402
from app.builder_service.app import build_service as build_builder  # noqa: E402
from app.common import httpclient  # noqa: E402
from app.common.scrubber import assert_no_sensitive_detail  # noqa: E402
from app.report_service.app import build_service as build_report  # noqa: E402
from app.snapshot_service.app import build_service as build_snapshot  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Dynamic ports per test process: no collision with leftover services.
P_SNAP = _free_port()
P_BUILDER = _free_port()
P_REPORT = _free_port()
SNAP = f"http://127.0.0.1:{P_SNAP}"
BUILDER = f"http://127.0.0.1:{P_BUILDER}"
REPORT = f"http://127.0.0.1:{P_REPORT}"


class E2ETestCase(unittest.TestCase):
    servers: list = []
    tmpdir: Path
    raw_dir: Path
    runtime: Path
    artifacts: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="compliance-e2e-"))
        cls.raw_dir = cls.tmpdir / "raw"
        cls.runtime = cls.tmpdir / "runtime"
        cls.artifacts = cls.tmpdir / "artifacts"
        cls.raw_dir.mkdir()
        cls.runtime.mkdir()
        cls.artifacts.mkdir()
        # Generate synthetic fixtures into the temp raw dir.
        import app.bootstrap.generate_fixtures as gen
        original = gen.RAW_DIR
        gen.RAW_DIR = cls.raw_dir
        cls.catalog = build_catalog()
        gen.RAW_DIR = original

        import os
        os.environ["BUILDER_URL"] = BUILDER
        os.environ["REPORT_URL"] = REPORT
        os.environ["SNAPSHOT_URL"] = SNAP

        snapshot = build_snapshot(str(cls.runtime))
        builder = build_builder(str(cls.runtime), str(cls.raw_dir))
        report = build_report(str(cls.runtime), str(cls.artifacts))
        cls.servers = [
            snapshot.app.start("127.0.0.1", P_SNAP),
            builder.app.start("127.0.0.1", P_BUILDER),
            report.app.start("127.0.0.1", P_REPORT),
        ]
        for base in (SNAP, BUILDER, REPORT):
            cls._wait_ready(base)
        # Register sources + initial snapshot.
        for src in cls.catalog:
            snapshot.create_source(src)
        cls.snap1 = snapshot.take_snapshot("baseline")

    @classmethod
    def tearDownClass(cls) -> None:
        for srv in cls.servers:
            srv.shutdown()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @staticmethod
    def _wait_ready(base: str) -> None:
        for _ in range(50):
            try:
                httpclient.get(f"{base}/health")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError(f"{base} never became ready")

    # ------------------------------------------------------------ 1. baseline
    def test_01_snapshot_immutable_and_auth_check(self) -> None:
        snap = httpclient.get(f"{SNAP}/snapshots/current")
        self.assertEqual(snap["snapshot_id"], self.snap1["snapshot_id"])
        ok = httpclient.post(f"{SNAP}/authorization/check", {
            "source_id": "src_clinical_north", "purpose": "clinical",
            "org_scope": "north"})
        self.assertTrue(ok["authorized"])
        denied = httpclient.post(f"{SNAP}/authorization/check", {
            "source_id": "src_hr_workforce", "purpose": "market_research",
            "org_scope": "shared"})
        self.assertFalse(denied["authorized"])
        # Baseline snapshot stays immutable after later changes.
        baseline = httpclient.get(
            f"{SNAP}/snapshots/{self.snap1['snapshot_id']}")
        self.assertEqual(len(baseline["grants"]), self.snap1["grant_count"])

    def test_02_unauthorized_build_is_rejected(self) -> None:
        with self.assertRaises(httpclient.ApiError) as ctx:
            httpclient.post(f"{BUILDER}/builds", {
                "source_id": "src_hr_workforce",
                "purpose": "market_research", "org_scope": "shared"})
        self.assertEqual(ctx.exception.code, "unauthorized_grant")

    # ------------------------------------------------------------ 2. builds
    def test_03_build_samples_with_lineage(self) -> None:
        job = httpclient.post(f"{BUILDER}/builds", {
            "source_id": "src_clinical_north", "purpose": "clinical",
            "org_scope": "north"})
        self.assertEqual(job["stage"], "completed")
        self.__class__.clinical_sample = job["sample_id"]
        sample = httpclient.get(f"{BUILDER}/samples/{job['sample_id']}")
        self.assertGreater(sample["row_count"], 0)
        # Lineage view contains no sensitive values.
        ln = sample["lineage"][0]
        view = httpclient.get(f"{BUILDER}/lineage/{ln['lineage_id']}")
        self.assertNotIn("raw_value", view)
        self.assertNotIn("full_name", view)
        self.__class__.clinical_lineage = ln["lineage_id"]

        # Fraud sample (3 grants incl. shared) — used for the rebuild case.
        fraud_job = httpclient.post(f"{BUILDER}/builds", {
            "source_id": "src_tx_fraud", "purpose": "fraud_detection",
            "org_scope": "north"})
        self.assertEqual(fraud_job["stage"], "completed")
        self.__class__.fraud_sample = fraud_job["sample_id"]

        # Workforce sample for the blocked-report case.
        wf_job = httpclient.post(f"{BUILDER}/builds", {
            "source_id": "src_hr_workforce", "purpose": "workforce",
            "org_scope": "north"})
        self.__class__.wf_sample = wf_job["sample_id"]
        self.assertEqual(wf_job["stage"], "completed")

    # ------------------------------------------------ 3. interruption / resume
    def test_04_build_interruption_and_resume(self) -> None:
        httpclient.post(f"{BUILDER}/sim/fail",
                        {"fail_stage": "materialized", "count": 1})
        with self.assertRaises(httpclient.ApiError) as ctx:
            httpclient.post(f"{BUILDER}/builds", {
                "source_id": "src_market_panel",
                "purpose": "market_research", "org_scope": "north"})
        self.assertEqual(ctx.exception.code, "build_interrupted")
        job_id = ctx.exception.message  # message carries job id? fallback:
        jobs = httpclient.get(f"{BUILDER}/builds")["jobs"]
        interrupted = next(j for j in jobs if j["stage"] == "interrupted")
        self.assertGreater(interrupted["checkpoint"], 0)
        resumed = httpclient.post(
            f"{BUILDER}/builds/{interrupted['job_id']}/run", {})
        self.assertEqual(resumed["stage"], "completed")
        # Resumed sample row count matches a clean deterministic build.
        sample = httpclient.get(f"{BUILDER}/samples/{resumed['sample_id']}")
        raw = json.loads((self.raw_dir / "src_market_panel.json").read_text())
        self.assertEqual(sample["row_count"], len([i for i in range(len(raw))
                                                   if i % 3 == 0]))
        self.__class__.market_sample = resumed["sample_id"]

    # ------------------------------------------------------------ 4. reports
    def test_05_create_reports_real_metrics_and_cache(self) -> None:
        r1 = httpclient.post(f"{REPORT}/reports", {
            "title": "北区临床分析", "sample_id": self.clinical_sample})
        self.__class__.clinical_report = r1["report_id"]
        names = {m["metric_name"] for m in r1["metrics"]}
        self.assertEqual(
            names, {"eligible_participants", "mean_lab_value",
                    "high_risk_cases"})
        for m in r1["metrics"]:
            self.assertGreaterEqual(m["row_count"], 0)
            self.assertTrue(m["lineage_ids"])
            self.assertIn("src_clinical_north", m["subtotals"])

        r2 = httpclient.post(f"{REPORT}/reports", {
            "title": "欺诈交易分析", "sample_id": self.fraud_sample})
        self.__class__.fraud_report = r2["report_id"]

        r3 = httpclient.post(f"{REPORT}/reports", {
            "title": "人力分析", "sample_id": self.wf_sample})
        self.__class__.wf_report = r3["report_id"]

        caches = httpclient.get(f"{REPORT}/caches")["caches"]
        self.assertTrue(all(c["status"] == "valid" for c in caches))

    def test_06_sealed_audit_is_retained_but_locked(self) -> None:
        sealed = httpclient.post(
            f"{REPORT}/reports/{self.market_report_id()}/seal",
            {"reason": "pre-retraction audit legal hold"})
        self.assertTrue(Path(sealed["path"]).exists())
        # The sealed export exists and is locked.
        exports = httpclient.get(f"{REPORT}/exports")["exports"]
        sealed_exp = next(e for e in exports if e["kind"] == "sealed_audit")
        self.assertEqual(sealed_exp["status"], "sealed")
        with self.assertRaises(httpclient.ApiError) as ctx:
            httpclient.get(
                f"{REPORT}/exports/{sealed_exp['export_id']}/download")
        self.assertEqual(ctx.exception.code, "sealed_legal_hold")

    def market_report_id(self) -> str:
        # Build+report for the market sample (created in resume test).
        r = httpclient.post(f"{REPORT}/reports", {
            "title": "市场调研分析", "sample_id": self.market_sample})
        return r["report_id"]

    # -------------------------------------------------- 5. notification loss
    def test_07_notification_loss_then_reconcile(self) -> None:
        # Drop the builder's push; report service still receives it.
        httpclient.post(f"{SNAP}/sim/loss",
                        {"consumer": "builder", "count": 1})
        result = httpclient.post(f"{SNAP}/retractions", {
            "source_id": "src_clinical_north",
            "scope_purposes": ["clinical"], "scope_orgs": ["north"],
            "reason": "license scope reduced"})
        self.__class__.retraction = result["retraction_id"]
        deliveries = httpclient.get(f"{SNAP}/sim/deliveries")["deliveries"]
        builder_delivery = next(
            d for d in deliveries
            if d["consumer"] == "builder" and d["status"] == "lost")
        self.assertEqual(builder_delivery["status"], "lost")
        # Builder inbox must NOT contain the event yet.
        inbox_before = httpclient.get(f"{BUILDER}/inbox")["events"]
        self.assertFalse(
            any(not e["consumed"] for e in inbox_before))
        # Reconcile: pull recovers the event; replay applies it.
        pulled = httpclient.get(
            f"{SNAP}/events/pull?consumer=builder"
            "&event_type=license.retracted")
        self.assertGreaterEqual(pulled["count"], 1)
        replay = httpclient.post(f"{BUILDER}/inbox/consume", {})
        self.assertGreaterEqual(replay["consumed"], 1)
        quarantined = replay["results"][0]["quarantined_samples"]
        self.assertIn(self.clinical_sample, quarantined)

    # ------------------------------------------------------------ 6. impact
    def test_08_impact_decision_block_and_cache_export_handling(self) -> None:
        # Report service got the event via push; ensure it is consumed.
        httpclient.post(f"{REPORT}/inbox/consume", {})
        findings = httpclient.get(
            f"{REPORT}/findings?report_id={self.clinical_report}")["findings"]
        self.assertTrue(findings)
        report = httpclient.get(
            f"{REPORT}/reports/{self.clinical_report}")
        # Single-source sample, whole source retracted => blocked.
        self.assertEqual(report["status"], "blocked")
        self.assertIn("exceeds rebuild threshold", report["block_reason"])
        # Caches quarantined; download exports revoked.
        caches = [c for c in httpclient.get(
            f"{REPORT}/caches")["caches"]
            if c["report_id"] == self.clinical_report]
        self.assertTrue(caches)
        self.assertTrue(all(c["status"] == "quarantined" for c in caches))
        # The download export belonging to this report must be revoked.
        exports = httpclient.get(f"{REPORT}/exports")["exports"]
        revoked = next(e for e in exports
                       if e["report_id"] == self.clinical_report
                       and e["kind"] == "download")
        self.assertEqual(revoked["status"], "revoked")
        with self.assertRaises(httpclient.ApiError) as ctx:
            httpclient.get(
                f"{REPORT}/exports/{revoked['export_id']}/download")
        self.assertEqual(ctx.exception.code, "export_revoked")
        # A blocked report cannot be rebuilt.
        with self.assertRaises(httpclient.ApiError) as ctx:
            httpclient.post(
                f"{REPORT}/reports/{self.clinical_report}/rebuild", {})
        self.assertEqual(ctx.exception.code, "report_blocked")

    def test_09_partial_retraction_allows_safe_rebuild(self) -> None:
        # Retract only the 'shared' fraud grant; north report is unaffected
        # because its org_scope is north. Instead retract central on a new
        # central fraud sample to exercise partial scope, then rebuild path
        # via the north report is unaffected. Use purpose-scoped retraction
        # of workforce central while north report stays active.
        httpclient.post(f"{SNAP}/retractions", {
            "source_id": "src_hr_workforce",
            "scope_purposes": ["workforce"], "scope_orgs": ["central"],
            "reason": "central workforce license ended"})
        httpclient.post(f"{REPORT}/inbox/consume", {})
        wf = httpclient.get(f"{REPORT}/reports/{self.wf_report}")
        # North-scoped report is outside the retracted org => still active.
        self.assertEqual(wf["status"], "active")

        # Rebuild-safe path: retract north workforce (single source) would
        # block; instead demonstrate rebuild with the fraud source by
        # retracting its shared grant only — the north fraud report uses no
        # shared rows, so it remains active and a manual rebuild produces a
        # new version preserving every row.
        httpclient.post(f"{SNAP}/retractions", {
            "source_id": "src_tx_fraud",
            "scope_purposes": ["fraud_detection"],
            "scope_orgs": ["shared"], "reason": "shared feed retired"})
        httpclient.post(f"{REPORT}/inbox/consume", {})
        fraud = httpclient.get(f"{REPORT}/reports/{self.fraud_report}")
        self.assertEqual(fraud["status"], "active")
        rebuilt = httpclient.post(
            f"{REPORT}/reports/{self.fraud_report}/rebuild", {})
        self.assertEqual(rebuilt["removed_rows"], 0)
        self.assertEqual(
            rebuilt["new_report"]["snapshot_id"],
            httpclient.get(f"{SNAP}/snapshots/current")["snapshot_id"])

    def test_10_rebuild_with_actual_removed_rows(self) -> None:
        # Build a sample whose grant is then fully retracted but where the
        # source also survives under another org: rebuild drops rows whose
        # (purpose, org) grant is gone. Use fraud central sample, then
        # retract the central fraud grant.
        job = httpclient.post(f"{BUILDER}/builds", {
            "source_id": "src_tx_fraud", "purpose": "fraud_detection",
            "org_scope": "central"})
        central_sample = job["sample_id"]
        rpt = httpclient.post(f"{REPORT}/reports", {
            "title": "中区欺诈分析", "sample_id": central_sample})
        before = sum(m["row_count"] for m in rpt["metrics"])
        self.assertGreater(before, 0)
        httpclient.post(f"{SNAP}/retractions", {
            "source_id": "src_tx_fraud",
            "scope_purposes": ["fraud_detection"],
            "scope_orgs": ["central"], "reason": "central license revoked"})
        httpclient.post(f"{BUILDER}/inbox/consume", {})
        httpclient.post(f"{REPORT}/inbox/consume", {})
        impacted = httpclient.get(
            f"{REPORT}/reports/{rpt['report_id']}")
        self.assertEqual(impacted["status"], "blocked")
        # Builder-level safe rebuild still works and drops every row.
        rebuilt_sample = httpclient.post(
            f"{BUILDER}/samples/{central_sample}/rebuild-safe", {})
        self.assertGreater(rebuilt_sample["removed_rows"], 0)
        self.assertEqual(rebuilt_sample["retained_rows"], 0)

    # ------------------------------------------------------------ 7. purpose
    def test_11_purpose_change_approval_and_rejection(self) -> None:
        # clinical -> workforce has no grant on that source => rejected.
        rejected = httpclient.post(f"{BUILDER}/purpose-changes", {
            "lineage_id": self.clinical_lineage,
            "new_purpose": "workforce"})
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("no active grant", rejected["rejection_reason"])
        # An authorized purpose change is approved. Build a fresh sample
        # against a grant no earlier scenario retracts (fraud/north), so
        # this test is independent of suite ordering and retraction state.
        fresh_job = httpclient.post(f"{BUILDER}/builds", {
            "source_id": "src_tx_fraud", "purpose": "fraud_detection",
            "org_scope": "north"})
        fresh_sample = httpclient.get(
            f"{BUILDER}/samples/{fresh_job['sample_id']}")
        fraud_ln = fresh_sample["lineage"][0]["lineage_id"]
        approved = httpclient.post(f"{BUILDER}/purpose-changes", {
            "lineage_id": fraud_ln,
            "new_purpose": "fraud_detection"})
        self.assertEqual(approved["status"], "approved")

    # ------------------------------------------------------------ 8. privacy
    def test_12_explanation_package_has_no_sensitive_detail(self) -> None:
        pkg = httpclient.post(
            f"{REPORT}/reports/{self.clinical_report}/explanation",
            {"retraction_id": self.retraction})
        self.assertEqual(pkg["sensitive_scan"]["offenses"], [])
        blob = json.dumps(pkg["package"])
        # Sensitive *values* (synthetic identities, subject refs) must never
        # appear. Metric names such as mean_lab_value are aggregates, not
        # detail, and are allowed; raw row keys are checked separately.
        for forbidden in ("Avery", "Nguyen", "SUBJ-", "PT-",
                          "synthetic.test", "555-", "Synthetic Ave"):
            self.assertNotIn(forbidden, blob,
                             f"sensitive value {forbidden} leaked")
        # No raw-row payload keys may appear anywhere in the package.
        raw_keys = {"full_name", "email", "phone", "date_of_birth",
                    "home_address", "diagnosis_code", "condition_note",
                    "consent", "encounter_month", "patient_id",
                    "account_number", "salary", "raw_value"}
        present = {k for k in raw_keys if f'"{k}"' in blob}
        self.assertEqual(present, set(),
                         f"raw row keys present in explanation: {present}")
        # Aggregates and lineage ids are still present.
        self.assertTrue(pkg["package"]["explanation_package"]["metrics"])
        self.assertTrue(
            pkg["package"]["explanation_package"]["metrics"][0]["lineage"])
        offenses = assert_no_sensitive_detail(pkg["package"])
        self.assertEqual(offenses, [])

    def test_13_full_lineage_trace_end_to_end(self) -> None:
        # Pick a lineage id that a metric actually consumed (the sample's
        # first row may have consent=false and thus appear in no metric).
        report = httpclient.get(
            f"{REPORT}/reports/{self.clinical_report}")
        lineage_id = next(
            m for m in report["metrics"] if m["lineage_ids"]
        )["lineage_ids"][0]
        trace = httpclient.get(
            f"{REPORT}/lineage/trace/{lineage_id}")
        self.assertEqual(trace["builder_lineage"]["lineage_id"], lineage_id)
        self.assertGreaterEqual(len(trace["consumed_by"]), 1)
        self.assertNotIn("raw_value", json.dumps(trace))

    def test_14_sealed_audit_survives_retraction(self) -> None:
        # Self-contained: seal a workforce report, retract its source, then
        # verify the legal-hold artifact is retained/locked while the
        # ordinary download is revoked — differential retention in action.
        job = httpclient.post(f"{BUILDER}/builds", {
            "source_id": "src_hr_workforce", "purpose": "workforce",
            "org_scope": "north"})
        rpt = httpclient.post(f"{REPORT}/reports", {
            "title": "北区人力封存验证", "sample_id": job["sample_id"]})
        httpclient.post(
            f"{REPORT}/reports/{rpt['report_id']}/seal",
            {"reason": "legal hold before license reduction"})
        httpclient.post(f"{SNAP}/retractions", {
            "source_id": "src_hr_workforce",
            "scope_purposes": ["workforce"], "scope_orgs": ["north"],
            "reason": "north workforce license ended"})
        httpclient.post(f"{REPORT}/inbox/consume", {})

        sealed = httpclient.get(f"{REPORT}/sealed")["sealed"]
        self.assertTrue(sealed)
        path = Path(sealed[0]["artifact_path"])
        self.assertTrue(path.exists())
        content = json.loads(path.read_text())
        self.assertIn("snapshot", content)
        self.assertNotIn("full_name", json.dumps(content))

        rep = httpclient.get(f"{REPORT}/reports/{rpt['report_id']}")
        self.assertEqual(rep["status"], "sealed")  # legal hold preserved
        self.assertIsNotNone(rep["block_reason"])   # impact still recorded
        findings = httpclient.get(
            f"{REPORT}/findings?report_id={rpt['report_id']}")["findings"]
        self.assertTrue(findings)

        exports = httpclient.get(f"{REPORT}/exports")["exports"]
        mine = [e for e in exports if e["report_id"] == rpt["report_id"]]
        sealed_exp = next(e for e in mine if e["kind"] == "sealed_audit")
        download_exp = next(e for e in mine if e["kind"] == "download")
        self.assertEqual(sealed_exp["status"], "sealed")
        self.assertEqual(sealed_exp["legal_hold"], 1)
        self.assertEqual(download_exp["status"], "revoked")
        with self.assertRaises(httpclient.ApiError) as ctx:
            httpclient.get(
                f"{REPORT}/exports/{sealed_exp['export_id']}/download")
        self.assertEqual(ctx.exception.code, "sealed_legal_hold")
        with self.assertRaises(httpclient.ApiError) as ctx:
            httpclient.get(
                f"{REPORT}/exports/{download_exp['export_id']}/download")
        self.assertEqual(ctx.exception.code, "export_revoked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
