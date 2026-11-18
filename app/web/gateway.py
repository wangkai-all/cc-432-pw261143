"""Web gateway + static console (port 8080).

Reverse proxy for the three services. Internal endpoints that return raw
row values (/internal/...) are blocked at the gateway — browsers and the
console can only ever see aggregates and lineage identifiers.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UPSTREAMS = {
    "snapshot": ("127.0.0.1", 8071),
    "builder": ("127.0.0.1", 8072),
    "report": ("127.0.0.1", 8073),
}
STATIC_DIR = Path(__file__).with_name("static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _proxy(self, method: str):
        parts = self.path.split("/", 3)  # ['', 'api', 'service', 'rest']
        if len(parts) < 3 or parts[1] != "api" or parts[2] not in UPSTREAMS:
            self._json(404, {"error": "unknown_upstream"})
            return
        service = parts[2]
        upstream_path = "/" + parts[3] if len(parts) > 3 else "/"
        # Hard block: raw-value/internal endpoints are not browser-reachable.
        if "/internal/" in upstream_path:
            self._json(403, {"error": "internal_endpoint_blocked",
                             "message": "raw row endpoints are never exposed "
                                        "to the web tier"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        host, port = UPSTREAMS[service]
        try:
            conn = http.client.HTTPConnection(host, port, timeout=15)
            conn.request(method, upstream_path, body=body,
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json"})
            resp = conn.getresponse()
            payload = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type",
                             resp.getheader("Content-Type",
                                            "application/json"))
            self.send_header("X-Upstream-Service", service)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            self._json(503, {"error": "upstream_unavailable",
                             "service": service})

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self):
        rel = urlsplit(self.path).path
        if rel == "/":
            rel = "/index.html"
        target = (STATIC_DIR / rel.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) \
                or not target.is_file():
            self._json(404, {"error": "not_found"})
            return
        content = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(target.suffix,
                                           "application/octet-stream"))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy("GET")
        else:
            self._static()

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8080), GatewayHandler)
    server.daemon_threads = True
    print("web gateway + console on http://127.0.0.1:8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
