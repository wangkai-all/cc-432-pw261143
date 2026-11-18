"""Tiny JSON HTTP client for service-to-service calls."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .webserver import ApiError


def request(method: str, url: str, body: dict | None = None,
            timeout: float = 10.0) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"error": "http_error"}
        raise ApiError(exc.code, payload.get("error", "http_error"),
                       payload.get("message", "")) from exc
    except urllib.error.URLError as exc:
        raise ApiError(503, "service_unavailable", str(exc)) from exc


def get(url: str, **kw) -> dict:
    return request("GET", url, **kw)


def post(url: str, body: dict | None = None, **kw) -> dict:
    return request("POST", url, body= body or {}, **kw)


def put(url: str, body: dict | None = None, **kw) -> dict:
    return request("PUT", url, body=body or {}, **kw)
