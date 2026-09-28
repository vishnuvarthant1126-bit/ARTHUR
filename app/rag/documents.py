"""DocumentService: the whole RAG ingestion pipeline plus document management.

    upload ─► validate (type, size, name) ─► duplicate? (SHA-256 of the bytes)
           ─► extract + clean (ingestion.py) ─► chunk (chunking.py)
           ─► embed in batches ─► vector store (text + filename/page metadata)
           ─► save the original file ─► SQLite record

Three stores must stay consistent (SQLite row, vectors, file on disk). If a
later step fails, earlier ones are undone, so no half-imported document remains.
"""

import asyncio
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.database.database import Database
from app.database.models import DocumentRecord
from app.memory.vector_store import VectorItem, VectorStore
from app.observability.logging import get_logger
from app.rag.chunking import chunk_pages
from app.rag.embeddings import EmbeddingProvider
from app.rag.ingestion import SUPPORTED_TYPES, DocumentError, extract

log = get_logger(__name__)

EMBED_BATCH_SIZE = 32


class DocumentInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: str
    size_bytes: int
    pages: int
    chunks: int
    created_at: datetime


def safe_filename(name: str) -> str:
    """Keep only the base name and harmless characters ('../../x.pdf' -> 'x.pdf')."""
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")
    return name[:120] or "document"


class DocumentService:
    def __init__(
        self,
        db: Database,
        vectors: VectorStore,
        embeddings: EmbeddingProvider,
        storage_dir: Path,
        *,
        max_bytes: int = 20 * 1024 * 1024,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
    ) -> None:
        self.db = db
        self.vectors = vectors
        self.embeddings = embeddings
        self.storage_dir = storage_dir
        self.max_bytes = max_bytes
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    async def ingest(self, filename: str, data: bytes) -> tuple[DocumentInfo, bool]:
        """Import a document. Returns (info, created); created=False means already imported."""
        start = time.perf_counter()
        filename = safe_filename(filename)
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_TYPES:
            allowed = ", ".join(sorted(SUPPORTED_TYPES))
            raise DocumentError(
                f"Unsupported file type '{extension or '?'}'. Supported: {allowed}."
            )
        if not data:
            raise DocumentError("The file is empty.")
        if len(data) > self.max_bytes:
            raise DocumentError(f"File too large (max {self.max_bytes // (1024 * 1024)} MB).")

        sha256 = hashlib.sha256(data).hexdigest()
        if existing := await asyncio.to_thread(self._find_by_hash, sha256):
            return existing, False

        # Parsing is CPU work: run it in a thread so the server stays responsive.
        pages = await asyncio.to_thread(extract, data, extension)
        chunks = chunk_pages(pages, self.chunk_size, self.chunk_overlap)

        vectors: list[list[float]] = []
        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[i : i + EMBED_BATCH_SIZE]
            vectors.extend(await self.embeddings.embed_documents([c.text for c in batch]))

        doc_id = str(uuid4())
        items = [
            VectorItem(
                id=f"{doc_id}:{c.index}",
                embedding=vector,
                text=c.text,
                metadata={
                    "doc_id": doc_id,
                    "filename": filename,
                    "chunk": c.index,
                    "page": c.page_number or 0,
                    "page_label": c.page_label or "",
                },
            )
            for c, vector in zip(chunks, vectors, strict=True)
        ]
        path = self.storage_dir / f"{doc_id}{extension}"
        try:
            await asyncio.to_thread(self.vectors.upsert_many, items)
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, data)
            info = await asyncio.to_thread(
                self._insert,
                DocumentRecord(
                    id=doc_id,
                    filename=filename,
                    file_type=extension.lstrip("."),
                    size_bytes=len(data),
                    sha256=sha256,
                    pages=len(pages),
                    chunks=len(chunks),
                ),
            )
        except Exception:
            await asyncio.to_thread(self.vectors.delete_where, "doc_id", doc_id)
            path.unlink(missing_ok=True)
            raise

        log.info(
            "document_ingested",
            doc_id=doc_id,
            file_type=info.file_type,
            pages=info.pages,
            chunks=info.chunks,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return info, True

    async def list_all(self) -> list[DocumentInfo]:
        return await asyncio.to_thread(self._list)

    async def delete(self, doc_id: str) -> bool:
        record = await asyncio.to_thread(self._delete_row, doc_id)
        if record is None:
            return False
        await asyncio.to_thread(self.vectors.delete_where, "doc_id", doc_id)
        (self.storage_dir / f"{doc_id}.{record.file_type}").unlink(missing_ok=True)
        log.info("document_deleted", doc_id=doc_id)
        return True

    # ---------- database helpers (run in threads) ----------

    def _find_by_hash(self, sha256: str) -> DocumentInfo | None:
        with self.db.session() as s:
            record = s.scalar(select(DocumentRecord).where(DocumentRecord.sha256 == sha256))
            return DocumentInfo.model_validate(record) if record else None

    def _insert(self, record: DocumentRecord) -> DocumentInfo:
        with self.db.session() as s:
            s.add(record)
            s.flush()
            return DocumentInfo.model_validate(record)

    def _list(self) -> list[DocumentInfo]:
        with self.db.session() as s:
            records = s.scalars(select(DocumentRecord).order_by(DocumentRecord.created_at.desc()))
            return [DocumentInfo.model_validate(r) for r in records]

    def _delete_row(self, doc_id: str) -> DocumentInfo | None:
        with self.db.session() as s:
            record = s.get(DocumentRecord, doc_id)
            if record is None:
                return None
            info = DocumentInfo.model_validate(record)
            s.delete(record)
            return info
