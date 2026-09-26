"""Convert exceptions into consistent JSON error responses.

Every error the API returns has the same shape, so the UI can handle them uniformly:
    {"error": {"type": "llm_unavailable", "message": "..."}, "request_id": "..."}
"""

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.llm.base import LLMError, LLMResponseError, LLMTimeoutError, LLMUnavailableError
from app.observability.logging import get_logger

log = get_logger(__name__)

# exception class -> (HTTP status, machine-readable type)
_LLM_ERRORS: dict[type[LLMError], tuple[int, str]] = {
    LLMUnavailableError: (503, "llm_unavailable"),  # Service Unavailable
    LLMTimeoutError: (504, "llm_timeout"),  # Gateway Timeout
    LLMResponseError: (502, "llm_bad_response"),  # Bad Gateway
}


def _error_body(error_type: str, message: str) -> dict:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return {"error": {"type": error_type, "message": message}, "request_id": request_id}


async def _handle_llm_error(request: Request, exc: Exception) -> JSONResponse:
    status, error_type = _LLM_ERRORS.get(type(exc), (502, "llm_error"))
    log.warning("llm_error", error_type=error_type, detail=str(exc))
    return JSONResponse(status_code=status, content=_error_body(error_type, str(exc)))


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Full traceback goes to the log; the client gets a generic message
    # so internal details never leak.
    log.exception("unhandled_error")
    return JSONResponse(
        status_code=500,
        content=_error_body("internal_error", "Something went wrong inside ARTHUR."),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LLMError, _handle_llm_error)
    app.add_exception_handler(Exception, _handle_unexpected)
