"""Per-request context and the API's front-door checks.

Every request passes, in this order:
  1. Host allow-list   - only requests addressed to localhost/127.0.0.1 (DNS rebinding)
  2. Origin check      - changes only from ARTHUR's own page (CSRF)
  3. Rate limit        - at most N requests per minute per kind of request
then gets a request ID (attached to every log line it causes), security headers on
the way out, and one access-log line.
"""

import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.observability.logging import get_logger
from app.security.network import host_allowed
from app.security.rate_limit import group_for

log = get_logger("arthur.request")

# Only accept safe client-supplied IDs, so nobody can inject text into our logs.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# What the browser may load and run on ARTHUR's page. Even if text from a web page,
# document or model slipped past the HTML escaping, it could not run as a script.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' blob:",  # blob: = the small audio worklet for "Hey Arthur"
        "worker-src 'self' blob:",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",  # spoken replies
        "connect-src 'self' ws://localhost:* ws://127.0.0.1:*",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",  # no other site may show ARTHUR inside a frame
    ]
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(self)",
}
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")  # Swagger UI loads its scripts from a CDN


def same_origin(origin: str | None, host: str | None) -> bool:
    """CSRF protection for state-changing requests (upload, delete, ...).

    A web page you visit can send a form or file upload to http://localhost:8000
    without the browser asking first. Browsers always attach an Origin header to
    such requests, so we only accept it when it's ARTHUR's own page. Scripts and
    tests send no Origin and are allowed (they run on your machine anyway).
    """
    if origin is None:
        return True
    return urlsplit(origin).netloc == host


def _refuse(status: int, error_type: str, message: str, request_id: str, **headers) -> Response:
    return JSONResponse(
        status_code=status,
        content={"error": {"type": error_type, "message": message}, "request_id": request_id},
        headers={"X-Request-ID": request_id, **SECURITY_HEADERS, **headers},
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid4().hex[:12]
        path = request.url.path

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=path
        )
        state = request.app.state
        metrics = getattr(state, "metrics", None)

        host = request.headers.get("host")
        if not host_allowed(host, getattr(state, "allowed_hosts", frozenset())):
            log.warning("host_rejected", host=(host or "")[:100])
            _count_block(metrics, "wrong_host")
            return _refuse(421, "wrong_host", "ARTHUR only answers on localhost.", request_id)

        if request.method not in SAFE_METHODS and not same_origin(
            request.headers.get("origin"), host
        ):
            log.warning("cross_origin_request_blocked", origin=request.headers.get("origin"))
            _count_block(metrics, "cross_origin")
            return _refuse(403, "forbidden", "Cross-site request blocked.", request_id)

        limiter = getattr(state, "rate_limiter", None)
        group = group_for(request.method, path)
        if limiter is not None and group is not None:
            client = request.client.host if request.client else "unknown"
            wait = limiter.check(client, group)
            if wait is not None:
                log.warning("rate_limited", group=group)
                _count_block(metrics, "rate_limited")
                return _refuse(
                    429,
                    "rate_limited",
                    f"Too many requests - try again in {round(wait)} seconds.",
                    request_id,
                    **{"Retry-After": str(round(wait))},
                )

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # An unexpected crash: the error handler outside this middleware answers with
            # HTTP 500. Count it here first - crashes are the requests you most want to see
            # (the load test found that they were missing from the metrics).
            if metrics is not None:
                route = _route_label(request, 500)
                metrics.http_requests.labels(request.method, route, "500").inc()
                metrics.http_duration.labels(route).observe(time.perf_counter() - start)
            raise
        response.headers["X-Request-ID"] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        if not path.startswith(_DOCS_PATHS):
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        if "cache-control" not in response.headers:
            # Re-check the page's files on every load (unchanged ones answer "304 not
            # modified"), so an updated ARTHUR never runs with old JavaScript (Phase 28).
            response.headers["Cache-Control"] = "no-cache"
        duration = time.perf_counter() - start
        if metrics is not None and not path.startswith("/metrics"):  # don't measure measuring
            route = _route_label(request, response.status_code)
            metrics.http_requests.labels(request.method, route, str(response.status_code)).inc()
            metrics.http_duration.labels(route).observe(duration)
        log.info(
            "request_completed",
            status=response.status_code,
            duration_ms=round(duration * 1000, 1),
        )
        return response


def _count_block(metrics, reason: str) -> None:
    if metrics is not None:
        metrics.security_blocks.labels(reason).inc()


def _route_label(request: Request, status: int) -> str:
    """The route TEMPLATE ("/memories/{memory_id}"), never the raw path: raw paths contain
    ids and anything a client types, which would create unlimited metric series."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    # No API route matched: either one of the page's own files, or a path that doesn't exist.
    return "unmatched" if status == 404 else "static"
