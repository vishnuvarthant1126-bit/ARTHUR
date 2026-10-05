"""POST /attachments - attach a picture, screenshot or document to the next chat message.

Returns an id; the chat message carries the id (see app/agent/attachments.py).
"""

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from app.agent.attachments import IMAGE_TYPES, Attachment
from app.rag.documents import safe_filename
from app.rag.ingestion import SUPPORTED_TYPES, DocumentError, extract
from app.vision.provider import MAX_IMAGE_BYTES, VisionError, prepare_image

router = APIRouter(prefix="/attachments", tags=["attachments"])

KINDS = {"pdf": "PDF", "docx": "Word", "text": "text", "csv": "CSV"}


class AttachmentOut(BaseModel):
    id: str
    kind: str
    name: str
    detail: str


def _kind_of(upload: UploadFile) -> str | None:
    extension = Path(upload.filename or "").suffix.lower()
    if extension in IMAGE_TYPES or (
        not extension and (upload.content_type or "").startswith("image/")
    ):
        return "image"  # a pasted screenshot may come without a usable name
    return "document" if extension in SUPPORTED_TYPES else None


def _picture_detail(jpeg: bytes) -> str:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        return f"{image.width}×{image.height} picture"


@router.post("", response_model=AttachmentOut, status_code=status.HTTP_201_CREATED)
async def attach(file: UploadFile, request: Request) -> AttachmentOut:
    state = request.app.state
    kind = _kind_of(file)
    if kind is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Unsupported file. ARTHUR reads pictures (PNG, JPG, WebP, GIF, BMP) and documents "
            "(PDF, Word, text, Markdown, CSV).",
        )
    name = safe_filename(file.filename or ("screenshot.png" if kind == "image" else "file"))

    if kind == "image":
        if getattr(state, "vision", None) is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Pictures need the vision model, which is switched off (VISION_ENABLED).",
            )
        data = await file.read(MAX_IMAGE_BYTES + 1)
        try:
            jpeg = await asyncio.to_thread(prepare_image, data)
        except VisionError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        attachment = Attachment(
            kind="image",
            name=name,
            detail=await asyncio.to_thread(_picture_detail, jpeg),
            image=jpeg,
        )
    else:
        documents = state.documents
        data = await file.read(documents.max_bytes + 1)
        extension = Path(name).suffix.lower()
        try:
            # Imported first: that also checks size and emptiness. Kept, so later
            # questions can find it too.
            await documents.ingest(name, data)
            pages = await asyncio.to_thread(extract, data, extension)
        except DocumentError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        kind_name = KINDS.get(SUPPORTED_TYPES[extension], "document")
        unit = "page" if extension == ".pdf" else "part"
        count = f"{len(pages)} {unit}{'' if len(pages) == 1 else 's'}"
        attachment = Attachment(
            kind="document", name=name, detail=f"{kind_name} · {count}", pages=pages
        )

    state.attachments.add(attachment)
    return AttachmentOut(
        id=attachment.id, kind=attachment.kind, name=attachment.name, detail=attachment.detail
    )
