"""Database tables, described as Python classes (SQLAlchemy ORM)."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MemoryRecord(Base):
    """One long-term memory. SQLite is the source of truth; the vector store
    holds a searchable copy (the embedding) under the same id."""

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), default="fact")
    source: Mapped[str] = mapped_column(String(32), default="chat")  # chat | api | tool
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DocumentRecord(Base):
    """One uploaded document. Its chunks live in the vector store under doc_id."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)  # detects re-uploads
    pages: Mapped[int] = mapped_column(Integer)
    chunks: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditRecord(Base):
    """One tool execution attempt - allowed, denied or failed. Append-only."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[str] = mapped_column(Text)  # JSON, truncated
    permission_level: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(32))  # allowed | needs_confirmation | denied
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReminderRecord(Base):
    """One reminder. `due_at` is stored in UTC (SQLite keeps no time zone)."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    repeat: Mapped[str] = mapped_column(String(16), default="none")  # none|daily|weekdays|weekly
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    # scheduled -> delivered | cancelled   (a repeating reminder stays "scheduled")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
