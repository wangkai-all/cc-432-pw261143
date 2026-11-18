"""Minimal dependency-free JSON HTTP framework (stdlib only)."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

Handler = Callable[["RequestContext"], dict | tuple[int, dict]]


class Route:
    def __init__(self, method: str, pattern: str, handler: Handler):
        self.method = method
        self.regex = re.compile("^" + re.sub(r"{(\w+)}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.handler = handler


class RequestContext:
    def __init__(self, method: str, path: str, params: dict, body: dict | None,
                 query: dict[str, str]):
        self.method = method
        self.path = path
        self.params = params
        self.body = body or {}
        self.query = query


class ServiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, routes: list[Route], service_name: str):
        super().__init__(addr, handler_cls)
        self.routes = routes
        self.service_name = service_name


def make_handler(routes: list[Route], service_name: str):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # quiet; services log explicitly if needed
            pass

        def _dispatch(self, method: str):
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            body = None
            if method in ("POST", "PUT", "PATCH"):
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except json.JSONDecodeError:
                        self._write(400, {"error": "invalid_json"})
                        return
            for route in routes:
                if route.method != method:
                    continue
                m = route.regex.match(parsed.path)
                if not m:
                    continue
                ctx = RequestContext(method, parsed.path, m.groupdict(), body, query)
                try:
                    result = route.handler(ctx)
                    if isinstance(result, tuple):
                        status, payload = result
                    else:
                        status, payload = 200, result
                except ApiError as exc:
                    status, payload = exc.status, {"error": exc.code,
                                                   "message": exc.message}
                except Exception as exc:  # noqa: BLE001 - service boundary
                    status, payload = 500, {"error": "internal_error",
                                            "message": str(exc)}
                self._write(status, payload)
                return
            self._write(404, {"error": "not_found", "path": parsed.path})

        def _write(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Service", service_name)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

    return _Handler


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message


class HttpApp:
    def __init__(self, service_name: str):
        self.service_name = service_name
        self.routes: list[Route] = []

    def route(self, method: str, pattern: str):
        def deco(fn: Handler):
            self.routes.append(Route(method, pattern, fn))
            return fn
        return deco

    def start(self, host: str, port: int) -> ServiceHTTPServer:
        server = ServiceHTTPServer(
            (host, port), make_handler(self.routes, self.service_name),
            self.routes, self.service_name,
        )
        thread = threading.Thread(
            target=server.serve_forever, name=f"{self.service_name}-http", daemon=True
        )
        thread.start()
        return server
