"""Request guard: host allowlist, CSRF defenses, body limits, rate limits, headers.

Implemented as one pure ASGI middleware so the order of checks is explicit:
Host allowlist (blocks DNS rebinding against the localhost API) -> rate limit ->
for unsafe methods: same-origin check, JSON content type and bounded body.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from app.api.errors import error_payload

Scope = dict[str, Any]
ASGIApp = Callable[..., Any]

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_HOST_EXEMPT = {"/health", "/api/v1/health"}
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


class RateLimiter:
    """In-memory token buckets keyed by (bucket, client)."""

    def __init__(self, per_minute: dict[str, int], clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self._clock = clock
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._guard = threading.Lock()

    def check(self, bucket: str, client: str) -> float:
        """0 if allowed (and consumes a token), else seconds until the next token."""
        capacity = float(self.per_minute[bucket])
        rate = capacity / 60.0
        now = self._clock()
        with self._guard:
            if len(self._buckets) > 20_000:
                self._buckets.clear()
            tokens, last = self._buckets.get((bucket, client), (capacity, now))
            tokens = min(capacity, tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._buckets[(bucket, client)] = (tokens - 1.0, now)
                return 0.0
            self._buckets[(bucket, client)] = (tokens, now)
            return (1.0 - tokens) / rate


def _host_only(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class RequestGuard:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: set[str],
        max_body_bytes: int,
        rate_limiter: RateLimiter | None,
        trust_proxy_headers: bool,
        hsts: bool,
        csp_exempt_docs: bool,
    ) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.max_body_bytes = max_body_bytes
        self.rate_limiter = rate_limiter
        self.trust_proxy_headers = trust_proxy_headers
        self.hsts = hsts
        self.csp_exempt_docs = csp_exempt_docs

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = request_id
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()
        send = self._with_headers(send, path, request_id)

        async def reject(status: int, code: str, message: str, extra: dict | None = None):
            body = json.dumps(error_payload(code, message, request_id)).encode()
            response_headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ]
            for key, value in (extra or {}).items():
                response_headers.append((key.encode(), value.encode()))
            await send({"type": "http.response.start", "status": status, "headers": response_headers})
            await send({"type": "http.response.body", "body": body})

        host_header = headers.get("host", "")
        if path not in _HOST_EXEMPT and _host_only(host_header) not in self.allowed_hosts:
            await reject(400, "invalid_host", "Requests must use an allowed host name.")
            return

        if self.rate_limiter is not None and path != "/health":
            if method in _UNSAFE:
                bucket = "session" if path == "/api/v1/session" else "mutate"
            else:
                bucket = "read"
            wait = self.rate_limiter.check(bucket, self._client(scope, headers))
            if wait > 0:
                retry = str(max(1, math.ceil(wait)))
                await reject(
                    429, "rate_limited", "Too many requests; slow down.", {"retry-after": retry}
                )
                return

        if method in _UNSAFE:
            origin = headers.get("origin")
            if origin is not None and (
                origin == "null" or urlsplit(origin).netloc.lower() != host_header.lower()
            ):
                await reject(403, "cross_origin_forbidden", "Cross-origin requests are not allowed.")
                return
            if headers.get("sec-fetch-site") == "cross-site":
                await reject(403, "cross_origin_forbidden", "Cross-origin requests are not allowed.")
                return
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                await reject(415, "unsupported_media_type", "Send a JSON body (application/json).")
                return
            declared = headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > self.max_body_bytes):
                await reject(413, "payload_too_large", "Request body is too large.")
                return
            body = b""
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body += message.get("body", b"")
                more = message.get("more_body", False)
                if len(body) > self.max_body_bytes:
                    await reject(413, "payload_too_large", "Request body is too large.")
                    return
            replayed = False

            async def replay() -> dict:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
            return

        await self.app(scope, receive, send)

    def _client(self, scope: Scope, headers: dict[str, str]) -> str:
        if self.trust_proxy_headers:
            hops = [h.strip() for h in headers.get("x-forwarded-for", "").split(",") if h.strip()]
            if hops:
                return hops[-1]
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _with_headers(self, send: Callable, path: str, request_id: str) -> Callable:
        is_doc = self.csp_exempt_docs and path.startswith(_DOC_PATHS)

        async def wrapped(message: dict) -> None:
            if message["type"] == "http.response.start":
                extra = [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ]
                if not is_doc:
                    extra.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))
                if path.startswith("/api/") or path == "/health":
                    extra.append((b"cache-control", b"no-store"))
                if self.hsts:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        return wrapped
