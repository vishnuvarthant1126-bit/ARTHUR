"""Memory endpoints: see, add, search and delete what ARTHUR remembers.

Transparency is part of the memory policy - the user can always inspect
and remove every stored fact.
"""

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import MemoryDep
from app.memory.long_term import Memory
from app.memory.manager import CATEGORIES, MAX_MEMORY_CHARS, MemorySearchResult
from app.memory.policy import contains_sensitive_data

router = APIRouter(prefix="/memories", tags=["memory"])


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MEMORY_CHARS)
    category: str = Field(default="other", examples=list(CATEGORIES))


class MemoryList(BaseModel):
    count: int
    memories: list[Memory]


@router.get("", response_model=MemoryList)
async def list_memories(memory: MemoryDep) -> MemoryList:
    memories = await memory.list_memories()
    return MemoryList(count=len(memories), memories=memories)


@router.post("", response_model=Memory, status_code=status.HTTP_201_CREATED)
async def add_memory(body: MemoryIn, memory: MemoryDep) -> Memory:
    if contains_sensitive_data(body.content):
        raise HTTPException(422, "Refusing to store passwords, card numbers or other secrets.")
    saved, _ = await memory.save_memory(body.content, body.category, source="api")
    return saved


@router.get("/search", response_model=list[MemorySearchResult])
async def search_memories(
    memory: MemoryDep,
    q: str = Query(min_length=1, max_length=500),
    k: int = Query(default=5, ge=1, le=20),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
) -> list[MemorySearchResult]:
    return await memory.search_memory(q, k=k, min_score=min_score)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: str, memory: MemoryDep) -> Response:
    if not await memory.delete_memory(memory_id):
        raise HTTPException(404, "Memory not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
