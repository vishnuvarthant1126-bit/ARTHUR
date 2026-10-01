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


# Secrets hiding inside ordinary text values (e.g. the `text` of a refused type_text call).
_SECRET_PHRASE = re.compile(
    r"\b(password|passcode|passphrase|pin|otp|api[ _-]?key|token|secret)\b(\s*(?:is|:|=)\s*)\S+",
    re.IGNORECASE,
)
_CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){12,19}\b")
_KEY_SHAPED = re.compile(r"\b(?:sk|pk|ghp|gho|xox[abprs])[-_][A-Za-z0-9_-]{12,}")


def scrub_text(value: str) -> str:
    """Mask secrets inside free text, keeping the rest readable for the audit trail."""
    value = _SECRET_PHRASE.sub(r"\1\2***", value)
    value = _CARD_NUMBER.sub("****", value)
    return _KEY_SHAPED.sub("***", value)


def redact(arguments: Any) -> Any:
    """Mask secrets, recursively: whole values of secret-looking keys, and secrets
    found inside any other text value."""
    if isinstance(arguments, dict):
        return {
            k: "***" if _SECRET_KEYS.search(str(k)) else redact(v) for k, v in arguments.items()
        }
    if isinstance(arguments, list):
        return [redact(v) for v in arguments]
    if isinstance(arguments, str):
        return scrub_text(arguments)
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
