"""GET /status - every part of ARTHUR, one by one (Phase 25).

/health answers a single question ("can ARTHUR reach its model?"). This asks each part
separately, so when something breaks the page shows WHICH part. Every part is

    ok       working
    off      switched off on purpose (settings, or not possible here - e.g. in Docker)
    problem  should work but doesn't

One broken part never breaks the whole answer: each check is wrapped on its own.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.observability.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 4.0


class Component(BaseModel):
    name: str
    state: Literal["ok", "off", "problem"]
    detail: str


class SystemStatus(BaseModel):
    overall: Literal["ok", "problem"]
    uptime_seconds: int
    components: list[Component]


async def _check(name: str, probe: Callable[[], Awaitable[Component]]) -> Component:
    try:
        return await asyncio.wait_for(probe(), CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        return Component(name=name, state="problem", detail="no answer in time")
    except Exception as exc:  # a status page must still answer when a part is broken
        log.warning("status_check_failed", component=name, error=str(exc)[:200])
        return Component(name=name, state="problem", detail="check failed - see the log")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@router.get("/status", response_model=SystemStatus)
async def system_status(request: Request) -> SystemStatus:
    state = request.app.state
    tools = getattr(state, "tools", None)

    def has_tool(name: str) -> bool:
        return tools is not None and tools.get(name) is not None

    async def model() -> Component:
        llm = state.llm
        reachable = await llm.health()
        return Component(
            name="Language model",
            state="ok" if reachable else "problem",
            detail=llm.model if reachable else f"{llm.model} - Ollama not reachable",
        )

    async def memory() -> Component:
        count = await state.memory.count()
        return Component(name="Memory", state="ok", detail=_plural(count, "fact"))

    async def documents() -> Component:
        count = len(await state.documents.list_all())
        return Component(name="Documents", state="ok", detail=_plural(count, "document"))

    async def reminders() -> Component:
        upcoming = await asyncio.to_thread(state.reminders.upcoming)
        return Component(name="Reminders", state="ok", detail=f"{len(upcoming)} upcoming")

    async def voice() -> Component:
        stt, tts = getattr(state, "stt", None), getattr(state, "tts", None)
        if stt is None and tts is None:
            return Component(name="Voice", state="off", detail="not set up")
        voices = len(tts.voices()) if tts is not None else 0
        if tts is not None and voices == 0:
            return Component(name="Voice", state="problem", detail="no voice installed")
        ready = getattr(stt, "loaded", True)
        speech = "speech recognition ready" if ready else "speech recognition loads on first use"
        return Component(name="Voice", state="ok", detail=f"{speech} · {_plural(voices, 'voice')}")

    async def vision() -> Component:
        provider = getattr(state, "vision", None)
        if provider is None:
            return Component(name="Vision", state="off", detail="switched off")
        return Component(name="Vision", state="ok", detail=provider.model)

    async def browser() -> Component:
        if has_tool("browser_open"):
            return Component(name="Browser", state="ok", detail="ARTHUR's own Chromium")
        return Component(name="Browser", state="off", detail="not available here")

    async def desktop() -> Component:
        if has_tool("open_app"):
            return Component(name="Desktop apps", state="ok", detail="asks before every action")
        return Component(name="Desktop apps", state="off", detail="not available here")

    checks = {
        "Language model": model,
        "Memory": memory,
        "Documents": documents,
        "Reminders": reminders,
        "Voice": voice,
        "Vision": vision,
        "Browser": browser,
        "Desktop apps": desktop,
    }
    components = list(await asyncio.gather(*(_check(n, p) for n, p in checks.items())))
    metrics = getattr(state, "metrics", None)
    started = metrics.started if metrics is not None else time.time()
    return SystemStatus(
        overall="problem" if any(c.state == "problem" for c in components) else "ok",
        uptime_seconds=round(time.time() - started),
        components=components,
    )
