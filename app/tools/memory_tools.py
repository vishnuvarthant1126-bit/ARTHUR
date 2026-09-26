"""Long-term memory exposed as tools, so the agent (Phase 7) can use it too.

Permission levels follow the memory policy:
    search_memory  level 0 - reading
    save_memory    level 1 - easy to undo; secrets are refused
    delete_memory  level 2 - needs the user's confirmation
"""

from pydantic import BaseModel, Field

from app.memory.manager import CATEGORIES, MAX_MEMORY_CHARS, MemoryManager
from app.memory.policy import contains_sensitive_data
from app.tools.base import PermissionLevel, Tool, ToolContext, ToolError


class SearchMemoryInput(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="What to look for")
    limit: int = Field(default=5, ge=1, le=20)


class SearchMemoryTool(Tool[SearchMemoryInput]):
    name = "search_memory"
    description = "Search the user's long-term memory for facts related to a query."
    input_model = SearchMemoryInput
    permission_level = PermissionLevel.READ_ONLY

    def __init__(self, memory: MemoryManager, min_score: float = 0.55) -> None:
        self.memory = memory
        self.min_score = min_score

    async def run(self, args: SearchMemoryInput, context: ToolContext) -> list[dict]:
        results = await self.memory.search_memory(
            args.query, k=args.limit, min_score=self.min_score
        )
        return [{"id": r.memory.id, "content": r.memory.content, "score": r.score} for r in results]


class SaveMemoryInput(BaseModel):
    content: str = Field(
        min_length=1,
        max_length=MAX_MEMORY_CHARS,
        description="One short fact about the user, "
        "e.g. 'The user's favourite language is Python.'",
    )
    category: str = Field(default="other", description=f"One of: {', '.join(CATEGORIES)}")


class SaveMemoryTool(Tool[SaveMemoryInput]):
    name = "save_memory"
    description = (
        "Save a fact to long-term memory. Only use when the user explicitly asks you to "
        "remember something. Never save passwords or other secrets."
    )
    input_model = SaveMemoryInput
    permission_level = PermissionLevel.LOW_RISK

    def __init__(self, memory: MemoryManager) -> None:
        self.memory = memory

    async def run(self, args: SaveMemoryInput, context: ToolContext) -> dict:
        if contains_sensitive_data(args.content):
            raise ToolError("Refused: this looks like a password or other secret.")
        memory, created = await self.memory.save_memory(args.content, args.category, "tool")
        return {"id": memory.id, "content": memory.content, "created": created}


class DeleteMemoryInput(BaseModel):
    memory_id: str = Field(min_length=1, max_length=64)


class DeleteMemoryTool(Tool[DeleteMemoryInput]):
    name = "delete_memory"
    description = "Delete one long-term memory by id. Requires the user's confirmation."
    input_model = DeleteMemoryInput
    permission_level = PermissionLevel.CONFIRM

    def __init__(self, memory: MemoryManager) -> None:
        self.memory = memory

    def preview(self, args: DeleteMemoryInput) -> str:
        return f"Delete memory {args.memory_id}"

    async def run(self, args: DeleteMemoryInput, context: ToolContext) -> dict:
        if not await self.memory.delete_memory(args.memory_id):
            raise ToolError(f"No memory with id '{args.memory_id}'.")
        return {"deleted": args.memory_id}
