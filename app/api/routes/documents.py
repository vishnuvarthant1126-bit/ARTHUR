"""Document endpoints: upload, list, search and delete the user's documents."""

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel

from app.rag.documents import DocumentInfo
from app.rag.ingestion import DocumentError
from app.rag.retrieval import DocumentHit

router = APIRouter(prefix="/documents", tags=["documents"])


class UploadResult(BaseModel):
    document: DocumentInfo
    created: bool  # False = this exact file was already imported


class DocumentList(BaseModel):
    count: int
    documents: list[DocumentInfo]


@router.post("", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
async def upload_document(file: UploadFile, request: Request, response: Response) -> UploadResult:
    service = request.app.state.documents
    # Read at most max+1 bytes: enough to detect "too large" without loading a huge file.
    data = await file.read(service.max_bytes + 1)
    try:
        info, created = await service.ingest(file.filename or "document", data)
    except DocumentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return UploadResult(document=info, created=created)


@router.get("", response_model=DocumentList)
async def list_documents(request: Request) -> DocumentList:
    documents = await request.app.state.documents.list_all()
    return DocumentList(count=len(documents), documents=documents)


@router.get("/search", response_model=list[DocumentHit])
async def search_documents(
    request: Request,
    q: str = Query(min_length=2, max_length=500),
    k: int = Query(default=5, ge=1, le=20),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
) -> list[DocumentHit]:
    return await request.app.state.retriever.search(q, k=k, min_score=min_score)


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: str, request: Request) -> Response:
    if not await request.app.state.documents.delete(doc_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
