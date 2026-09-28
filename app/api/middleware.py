"""Per-request context: a request ID, a cross-site check, and one access-log line.

The request ID is attached to every log line produced while handling the
request, so all logs for one chat message can be found together.
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

log = get_logger("arthur.request")

# Only accept safe client-supplied IDs, so nobody can inject text into our logs.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


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


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid4().hex[:12]

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=request.url.path
        )

        if request.method not in SAFE_METHODS and not same_origin(
            request.headers.get("origin"), request.headers.get("host")
        ):
            log.warning("cross_origin_request_blocked", origin=request.headers.get("origin"))
            return JSONResponse(
                status_code=403,
                content={
                    "error": {"type": "forbidden", "message": "Cross-site request blocked."},
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )

        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request_completed",
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response
