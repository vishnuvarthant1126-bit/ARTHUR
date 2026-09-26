"""GET /health - is ARTHUR up, and can it reach its LLM?"""

from fastapi import APIRouter

from app.api.dependencies import LLMDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(llm: LLMDep) -> dict:
    reachable = await llm.health()
    return {
        "status": "ok" if reachable else "degraded",
        "llm": {"provider": llm.name, "model": llm.model, "reachable": reachable},
    }
