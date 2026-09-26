"""Audit log: a permanent record of every tool call ARTHUR attempts.

Answers "what did ARTHUR do, when, and was it allowed?" - including calls
that were denied or failed. Secret-looking argument values are masked
before anything is written.
"""

import json
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.database.database import Database
from app.database.models import AuditRecord

_SECRET_KEYS = re.compile(r"pass|secret|token|key|pin|cvv|card", re.IGNORECASE)
MAX_ARGUMENT_CHARS = 2000


class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    tool: str
    arguments: str
    permission_level: int
    decision: str
    success: bool
    error: str | None
    duration_ms: float
    request_id: str | None


def redact(arguments: Any) -> Any:
    """Replace values of secret-looking keys with '***', recursively."""
    if isinstance(arguments, dict):
        return {
            k: "***" if _SECRET_KEYS.search(str(k)) else redact(v) for k, v in arguments.items()
        }
    if isinstance(arguments, list):
        return [redact(v) for v in arguments]
    return arguments


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        *,
        tool: str,
        arguments: Any,
        permission_level: int,
        decision: str,
        success: bool,
        error: str | None = None,
        duration_ms: float = 0.0,
        request_id: str | None = None,
    ) -> None:
        text = json.dumps(redact(arguments), default=str, ensure_ascii=False)
        with self.db.session() as s:
            s.add(
                AuditRecord(
                    tool=tool,
                    arguments=text[:MAX_ARGUMENT_CHARS],
                    permission_level=permission_level,
                    decision=decision,
                    success=success,
                    error=error,
                    duration_ms=duration_ms,
                    request_id=request_id,
                )
            )

    def recent(self, limit: int = 50) -> list[AuditEntry]:
        with self.db.session() as s:
            records = s.scalars(select(AuditRecord).order_by(AuditRecord.id.desc()).limit(limit))
            return [AuditEntry.model_validate(r) for r in records]
