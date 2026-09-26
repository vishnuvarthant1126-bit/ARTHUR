"""Long-term memory records in SQLite (the permanent source of truth)."""

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.database.database import Database
from app.database.models import MemoryRecord


class Memory(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # build from a MemoryRecord

    id: str
    content: str
    category: str
    source: str
    created_at: datetime
    updated_at: datetime


class MemoryRepository:
    """Create/read/update/delete memory rows. Synchronous: callers run it in a thread."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, content: str, category: str, source: str) -> Memory:
        with self.db.session() as s:
            record = MemoryRecord(
                id=str(uuid4()), content=content, category=category, source=source
            )
            s.add(record)
            s.flush()  # fills in the default timestamps
            return Memory.model_validate(record)

    def get(self, memory_id: str) -> Memory | None:
        with self.db.session() as s:
            record = s.get(MemoryRecord, memory_id)
            return Memory.model_validate(record) if record else None

    def get_many(self, ids: list[str]) -> dict[str, Memory]:
        if not ids:
            return {}
        with self.db.session() as s:
            records = s.scalars(select(MemoryRecord).where(MemoryRecord.id.in_(ids)))
            return {r.id: Memory.model_validate(r) for r in records}

    def list_all(self) -> list[Memory]:
        with self.db.session() as s:
            records = s.scalars(select(MemoryRecord).order_by(MemoryRecord.created_at.desc()))
            return [Memory.model_validate(r) for r in records]

    def update(self, memory_id: str, content: str, category: str) -> Memory | None:
        with self.db.session() as s:
            record = s.get(MemoryRecord, memory_id)
            if record is None:
                return None
            record.content = content
            record.category = category
            s.flush()
            return Memory.model_validate(record)

    def delete(self, memory_id: str) -> bool:
        with self.db.session() as s:
            record = s.get(MemoryRecord, memory_id)
            if record is None:
                return False
            s.delete(record)
            return True

    def count(self) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(MemoryRecord)) or 0
