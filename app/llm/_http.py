"""HTTP helpers shared by providers that talk to a model over HTTP."""

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from app.llm.base import LLMResponseError, LLMTimeoutError, LLMUnavailableError

# Keep connections to a model server open for minutes, not httpx's default 5 seconds: a
# person pauses longer than that between messages, and every new connection costs time
# (measured in Phase 24: ~0.3 s each, twice per message).
KEEP_ALIVE = httpx.Limits(max_keepalive_connections=10, keepalive_expiry=300.0)

# Status codes worth retrying: rate limited, or a temporary server-side problem.
RETRYABLE_STATUS = {429, 502, 503, 504}


@contextmanager
def translate_http_errors(label: str, base_url: str) -> Iterator[None]:
    """Turn low-level httpx errors into ARTHUR's provider-neutral LLM errors."""
    try:
        yield
    except httpx.ConnectError as exc:
        raise LLMUnavailableError(f"Cannot reach {label} at {base_url}. Is it running?") from exc
    except httpx.TimeoutException as exc:
        raise LLMTimeoutError(f"{label} did not respond in time ({exc!r}).") from exc


def error_detail(response: httpx.Response) -> str:
    """Best-effort extraction of an error message from a JSON or text body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    error = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(error, dict):
        error = error.get("message", error)
    return str(error)[:300]


def status_error(label: str, response: httpx.Response) -> LLMResponseError:
    return LLMResponseError(
        f"{label} returned HTTP {response.status_code}: {error_detail(response)}",
        retryable=response.status_code in RETRYABLE_STATUS,
    )
