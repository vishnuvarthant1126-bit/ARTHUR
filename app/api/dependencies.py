"""FastAPI dependencies: small functions that hand shared objects to route handlers.

Routes ask for the provider via the `LLMDep` type instead of creating one.
Tests can then swap in a fake provider without touching the routes.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.llm.base import LLMProvider


def get_llm(request: Request) -> LLMProvider:
    return request.app.state.llm


# Use as a parameter type:  async def route(llm: LLMDep): ...
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
