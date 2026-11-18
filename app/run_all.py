"""Launch all three independent services in one process (dev/demo).

Each service still owns its own database and HTTP port, and can be started
standalone via run_service.py — they do not share code paths or storage.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.builder_service.app import build_service as build_builder  # noqa: E402
from app.report_service.app import build_service as build_report  # noqa: E402
from app.snapshot_service.app import build_service as build_snapshot  # noqa: E402

PORTS = {"snapshot": 8071, "builder": 8072, "report": 8073}


def main() -> None:
    runtime = ROOT / "data" / "runtime"
    raw = ROOT / "data" / "raw"
    artifacts = ROOT / "data" / "artifacts"
    for d in (runtime, artifacts):
        d.mkdir(parents=True, exist_ok=True)

    snapshot = build_snapshot(str(runtime))
    builder = build_builder(str(runtime), str(raw))
    report = build_report(str(runtime), str(artifacts))

    servers = [
        snapshot.app.start("127.0.0.1", PORTS["snapshot"]),
        builder.app.start("127.0.0.1", PORTS["builder"]),
        report.app.start("127.0.0.1", PORTS["report"]),
    ]
    print("services listening:",
          ", ".join(f"{k}={v}" for k, v in PORTS.items()))

    def _shutdown(*_args):
        for srv in servers:
            srv.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
