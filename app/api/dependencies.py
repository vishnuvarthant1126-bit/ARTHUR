"""FastAPI dependencies: small functions that hand shared objects to route handlers.

Routes ask for shared objects via types like `LLMDep` instead of creating them.
Tests can then swap in fakes without touching the routes.
"""

import re
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Request

from app.agent.orchestrator import Orchestrator
from app.llm.base import LLMProvider

SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)


def get_llm(request: Request) -> LLMProvider:
    return request.app.state.llm


def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def valid_session_id(value: str | None) -> str | None:
    return value if value and _SESSION_ID_RE.match(value) else None


def new_session_id() -> str:
    return uuid4().hex


# Use as a parameter type:  async def route(llm: LLMDep): ...
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]
