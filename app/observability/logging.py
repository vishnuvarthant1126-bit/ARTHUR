"""Structured logging setup.

Structured logs are key/value events instead of free text:
    event='chat_completed' latency_ms=812 model='qwen3:8b' request_id='a1b2c3'
They are easy to search and, in production, are emitted as JSON so tools
like Grafana Loki can index them.
"""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    # The Windows console defaults to an old encoding (cp1252) that can't show every
    # character. Without this, logging an unusual error message could itself crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,  # adds request_id etc. bound per request
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if json_logs:
        processors += [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
    else:
        processors += [structlog.dev.ConsoleRenderer()]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger(name)
