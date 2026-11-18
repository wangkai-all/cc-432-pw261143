"""Authorization Snapshot Service.

Owns source catalog, grants, immutable authorization snapshots and
retractions. Retractions are distributed through an outbox; push delivery
can be deliberately lost (notification-loss simulation) and replayed later
via the reconcile/pull endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..common import httpclient
from ..common.db import Database
from ..common.policy import VALID_ORG_SCOPES, VALID_PURPOSES
from ..common.util import new_id, now_iso, payload_hash
from ..common.webserver import ApiError, HttpApp

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class SnapshotService:
    def __init__(self, db_path: str, builder_url: str, report_url: str):
        self.db = Database(db_path)
        self.db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.builder_url = builder_url
        self.report_url = report_url
        self.app = HttpApp("snapshot-service")
        self._routes()

    # ---------------------------------------------------------------- catalog
    def create_source(self, source: dict) -> dict:
        existing = self.db.query_one(
            "SELECT source_id FROM sources WHERE source_id=?",
            (source["source_id"],))
        if existing:
            return source
        self.db.insert("sources", {
            "source_id": source["source_id"],
            "name": source["name"],
            "category": source["category"],
            "description": source.get("description", ""),
            "sensitive_columns": json.dumps(source["sensitive_columns"]),
            "record_count": source["record_count"],
            "created_at": now_iso(),
        })
        for g in source["grants"]:
            self.db.insert("grants", {
                "grant_id": g["grant_id"], "source_id": source["source_id"],
                "purpose": g["purpose"], "org_scope": g["org_scope"],
                "status": "active", "granted_at": now_iso(),
                "revoked_at": None, "retraction_id": None,
            })
        return source

    def list_sources(self) -> list[dict]:
        out = []
        for s in self.db.query_all("SELECT * FROM sources ORDER BY source_id"):
            s["sensitive_columns"] = json.loads(s["sensitive_columns"])
            s["grants"] = self.db.query_all(
                "SELECT * FROM grants WHERE source_id=? ORDER BY purpose",
                (s["source_id"],))
            out.append(s)
        return out

    # -------------------------------------------------------------- snapshots
    def take_snapshot(self, note: str = "") -> dict:
        active = self.db.query_all(
            "SELECT grant_id, source_id, purpose, org_scope, status "
            "FROM grants ORDER BY grant_id")
        version_row = self.db.query_one(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM snapshots")
        version = version_row["v"]
        snapshot_id = f"snap_v{version}"
        fingerprint = payload_hash(active)
        self.db.insert("snapshots", {
            "snapshot_id": snapshot_id, "version": version,
            "fingerprint": fingerprint, "note": note, "created_at": now_iso(),
        })
        for g in active:
            self.db.insert("snapshot_grants", {
                "snapshot_id": snapshot_id, **g,
            })
        return {"snapshot_id": snapshot_id, "version": version,
                "fingerprint": fingerprint, "grant_count": len(active)}

    def get_snapshot(self, snapshot_id: str) -> dict:
        snap = self.db.query_one(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
        if not snap:
            raise ApiError(404, "snapshot_not_found")
        snap["grants"] = self.db.query_all(
            "SELECT grant_id, source_id, purpose, org_scope, status "
            "FROM snapshot_grants WHERE snapshot_id=? ORDER BY grant_id",
            (snapshot_id,))
        return snap

    def current_snapshot(self) -> dict:
        row = self.db.query_one(
            "SELECT snapshot_id FROM snapshots ORDER BY version DESC LIMIT 1")
        if not row:
            raise ApiError(409, "no_snapshot", "take a snapshot first")
        return self.get_snapshot(row["snapshot_id"])

    def check_authorization(self, source_id: str, purpose: str, org_scope: str,
                            snapshot_id: str | None = None) -> dict:
        """Authority endpoint: other services re-verify against a snapshot."""
        if purpose not in VALID_PURPOSES or org_scope not in VALID_ORG_SCOPES:
            raise ApiError(400, "invalid_scope")
        if snapshot_id is None:
            snapshot_id = self.current_snapshot()["snapshot_id"]
        row = self.db.query_one(
            "SELECT status FROM snapshot_grants "
            "WHERE snapshot_id=? AND source_id=? AND purpose=? AND org_scope=?",
            (snapshot_id, source_id, purpose, org_scope))
        authorized = bool(row and row["status"] == "active")
        return {"authorized": authorized, "snapshot_id": snapshot_id,
                "source_id": source_id, "purpose": purpose,
                "org_scope": org_scope}

    # -------------------------------------------------------------- retraction
    def retract(self, source_id: str, scope_purposes: list[str],
                scope_orgs: list[str], reason: str) -> dict:
        if not self.db.query_one(
                "SELECT 1 FROM sources WHERE source_id=?", (source_id,)):
            raise ApiError(404, "source_not_found")
        # Atomic: grant revocation and the follow-up snapshot must never be
        # observed interleaved with another concurrent retraction.
        with self.db.transaction() as conn:
            snap_row = conn.execute(
                "SELECT * FROM snapshots ORDER BY version DESC LIMIT 1"
            ).fetchone()
            snap = dict(snap_row)
            retraction_id = new_id("retr")
            conn.execute(
                "INSERT INTO retractions(retraction_id,source_id,"
                "scope_purposes,scope_orgs,reason,effective_snapshot_id,"
                "effective_version,status,created_at) VALUES(?,?,?,?,?,?,?,"
                "'effective',?)",
                (retraction_id, source_id, json.dumps(scope_purposes),
                 json.dumps(scope_orgs), reason, snap["snapshot_id"],
                 snap["version"], now_iso()))
            grants = conn.execute(
                "SELECT * FROM grants WHERE source_id=? AND status='active'",
                (source_id,)).fetchall()
            revoked = []
            for g in grants:
                if scope_purposes and g["purpose"] not in scope_purposes:
                    continue
                if scope_orgs and g["org_scope"] not in scope_orgs:
                    continue
                conn.execute(
                    "UPDATE grants SET status='revoked', revoked_at=?, "
                    "retraction_id=? WHERE grant_id=?",
                    (now_iso(), retraction_id, g["grant_id"]))
                revoked.append(g["grant_id"])
            active = conn.execute(
                "SELECT grant_id, source_id, purpose, org_scope, status "
                "FROM grants ORDER BY grant_id").fetchall()
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM snapshots"
            ).fetchone()["v"]
            snapshot_id = f"snap_v{version}"
            fingerprint = payload_hash([dict(g) for g in active])
            conn.execute(
                "INSERT INTO snapshots(snapshot_id,version,fingerprint,note,"
                "created_at) VALUES(?,?,?,?,?)",
                (snapshot_id, version, fingerprint,
                 f"after retraction {retraction_id}", now_iso()))
            for g in active:
                conn.execute(
                    "INSERT INTO snapshot_grants(snapshot_id,grant_id,"
                    "source_id,purpose,org_scope,status) VALUES(?,?,?,?,?,?)",
                    (snapshot_id, g["grant_id"], g["source_id"],
                     g["purpose"], g["org_scope"], g["status"]))
        new_snap = {"snapshot_id": snapshot_id, "version": version,
                    "fingerprint": fingerprint, "grant_count": len(active)}
        event = {
            "event_type": "license.retracted",
            "event_id": new_id("evt"),
            "created_at": now_iso(),
            "payload": {
                "retraction_id": retraction_id,
                "source_id": source_id,
                "scope_purposes": scope_purposes,
                "scope_orgs": scope_orgs,
                "reason": reason,
                "previous_snapshot_id": snap["snapshot_id"],
                "new_snapshot_id": new_snap["snapshot_id"],
                "new_snapshot_version": new_snap["version"],
                "revoked_grants": revoked,
            },
        }
        self.db.insert("outbox", {
            "event_id": event["event_id"], "event_type": event["event_type"],
            "payload": json.dumps(event, ensure_ascii=False),
            "created_at": event["created_at"],
        })
        self._push_event(event)
        return {"retraction_id": retraction_id,
                "revoked_grant_ids": revoked,
                "new_snapshot": new_snap}

    def list_retractions(self) -> list[dict]:
        rows = self.db.query_all(
            "SELECT * FROM retractions ORDER BY created_at")
        for r in rows:
            r["scope_purposes"] = json.loads(r["scope_purposes"])
            r["scope_orgs"] = json.loads(r["scope_orgs"])
        return rows

    # ------------------------------------------------------------ outbox / sim
    def _push_event(self, event: dict) -> None:
        for consumer, base in (("builder", self.builder_url),
                               ("report", self.report_url)):
            rule = self.db.query_one(
                "SELECT * FROM sim_loss_rules WHERE consumer=? AND event_type=? "
                "AND remaining>0 ORDER BY created_at LIMIT 1",
                (consumer, event["event_type"]))
            if rule:
                self.db.execute(
                    "UPDATE sim_loss_rules SET remaining=remaining-1 "
                    "WHERE rule_id=?", (rule["rule_id"],))
                self._record_delivery(event["event_id"], consumer, "lost",
                                      "simulated notification loss")
                continue
            try:
                httpclient.post(f"{base}/internal/events", event)
                self._record_delivery(event["event_id"], consumer,
                                      "delivered")
            except ApiError as exc:
                self._record_delivery(event["event_id"], consumer, "failed",
                                      f"{exc.code}: {exc.message}")

    def _record_delivery(self, event_id: str, consumer: str, status: str,
                         detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO outbox_deliveries(event_id,consumer,status,attempts,"
            "updated_at,detail) VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(event_id,consumer) DO UPDATE SET status=excluded.status,"
            "attempts=attempts+1,updated_at=excluded.updated_at,"
            "detail=excluded.detail",
            (event_id, consumer, status, now_iso(), detail))

    def add_loss_rule(self, consumer: str, event_type: str,
                      count: int) -> dict:
        if consumer not in ("builder", "report"):
            raise ApiError(400, "bad_consumer")
        rule_id = new_id("loss")
        self.db.insert("sim_loss_rules", {
            "rule_id": rule_id, "consumer": consumer,
            "event_type": event_type, "remaining": count,
            "created_at": now_iso(),
        })
        return {"rule_id": rule_id, "consumer": consumer,
                "event_type": event_type, "remaining": count}

    def delivery_status(self) -> list[dict]:
        return self.db.query_all(
            "SELECT o.event_id,o.event_type,d.consumer,d.status,d.attempts,"
            "d.detail,d.updated_at FROM outbox o JOIN outbox_deliveries d "
            "ON o.event_id=d.event_id ORDER BY o.created_at, d.consumer")

    def pull_events(self, consumer: str, after_event_id: str | None = None,
                    event_type: str | None = None) -> dict:
        """Reconciliation endpoint: consumers pull missed events.

        A lost push never reached the consumer's inbox, so a successful pull
        re-delivers the event (HTTP) in addition to marking delivery
        recovered — this is the durable compensation path.
        """
        sql = ("SELECT o.event_id,o.event_type,o.payload,o.created_at,"
               "d.status FROM outbox o LEFT JOIN outbox_deliveries d "
               "ON o.event_id=d.event_id AND d.consumer=? WHERE 1=1")
        params: list = [consumer]
        if after_event_id:
            sql += " AND o.event_id > ?"
            params.append(after_event_id)
        if event_type:
            sql += " AND o.event_type=?"
            params.append(event_type)
        sql += " ORDER BY o.created_at"
        events = []
        redelivered = 0
        base = (self.builder_url if consumer == "builder"
                else self.report_url)
        for r in self.db.query_all(sql, params):
            evt = json.loads(r["payload"])
            evt["delivery"] = r["status"]
            events.append(evt)
            if r["status"] != "delivered":
                try:
                    httpclient.post(f"{base}/internal/events", evt)
                    redelivered += 1
                    self._record_delivery(
                        r["event_id"], consumer, "delivered",
                        "recovered via pull reconcile")
                except ApiError as exc:
                    self._record_delivery(
                        r["event_id"], consumer, "failed",
                        f"redelivery failed: {exc.code}")
        return {"consumer": consumer, "events": events, "count": len(events),
                "redelivered": redelivered}

    # ------------------------------------------------------------------ routes
    def _routes(self) -> None:
        app = self.app

        @app.route("GET", "/health")
        def _health(_ctx):
            return {"service": "snapshot", "status": "ok"}

        @app.route("GET", "/sources")
        def _sources(_ctx):
            return {"sources": self.list_sources()}

        @app.route("POST", "/sources")
        def _register(ctx):
            src = ctx.body
            for required in ("source_id", "name", "category",
                             "sensitive_columns", "grants"):
                if required not in src:
                    raise ApiError(400, "missing_field", required)
            created = self.create_source(src)
            return 201, {"source_id": created["source_id"],
                         "grants_registered": len(created["grants"])}

        @app.route("POST", "/admin/snapshots")
        def _snapshot(_ctx):
            return self.take_snapshot(_ctx.body.get("note", ""))

        @app.route("GET", "/snapshots/current")
        def _current(_ctx):
            return self.current_snapshot()

        @app.route("GET", "/snapshots/{snapshot_id}")
        def _one(ctx):
            return self.get_snapshot(ctx.params["snapshot_id"])

        @app.route("GET", "/snapshots")
        def _all_snaps(_ctx):
            return {"snapshots": self.db.query_all(
                "SELECT snapshot_id,version,fingerprint,note,created_at "
                "FROM snapshots ORDER BY version")}

        @app.route("POST", "/authorization/check")
        def _check(ctx):
            b = ctx.body
            return self.check_authorization(
                b["source_id"], b["purpose"], b["org_scope"],
                b.get("snapshot_id"))

        @app.route("POST", "/retractions")
        def _retract(ctx):
            b = ctx.body
            return self.retract(
                b["source_id"], b.get("scope_purposes", []),
                b.get("scope_orgs", []), b["reason"])

        @app.route("GET", "/retractions")
        def _retractions(_ctx):
            return {"retractions": self.list_retractions()}

        @app.route("POST", "/sim/loss")
        def _loss(ctx):
            b = ctx.body
            return self.add_loss_rule(
                b["consumer"], b.get("event_type", "license.retracted"),
                int(b.get("count", 1)))

        @app.route("GET", "/sim/deliveries")
        def _deliveries(_ctx):
            return {"deliveries": self.delivery_status()}

        @app.route("GET", "/events/pull")
        def _pull(ctx):
            return self.pull_events(
                ctx.query["consumer"],
                ctx.query.get("after_event_id"),
                ctx.query.get("event_type"))


def build_service(runtime_dir: str) -> SnapshotService:
    return SnapshotService(
        db_path=os.path.join(runtime_dir, "snapshot.db"),
        builder_url=os.environ.get("BUILDER_URL", "http://127.0.0.1:8072"),
        report_url=os.environ.get("REPORT_URL", "http://127.0.0.1:8073"),
    )
