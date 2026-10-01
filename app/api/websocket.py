"""WebSocket /ws - real-time chat with streamed replies.

HTTP is request -> response: the browser waits for the whole answer.
A WebSocket stays open in both directions, so ARTHUR can push each word
as the model produces it, and the browser can send "stop" mid-answer.

Connect to /ws?session_id=<id> to continue a conversation (the browser
keeps its id in localStorage). Without a valid id a new one is created.

Protocol (every frame is one JSON object with a "type"):

  browser -> server
    {"type": "chat", "message": "Hello Arthur"}
    {"type": "stop"}                     cancel the answer in progress
    {"type": "clear"}                    forget this conversation
    {"type": "ping"}                     keep-alive / latency check

  server -> browser
    {"type": "session", "session_id": "...", "history": [{"role": "user", "content": "..."}]}
    {"type": "status",  "state": "thinking", "request_id": "..."}
    {"type": "status",  "state": "executing", "tool": "calculator"}
    {"type": "tool",    "phase": "start", "call_id": "...", "name": "calculator",
                        "arguments": {"expression": "482 * 29"}}
    {"type": "tool",    "phase": "end", "call_id": "...", "name": "calculator", "status": "ok",
                        "summary": "482 * 29 = 13978", "duration_ms": 0.4}
    {"type": "confirmation", "name": "delete_memory", "arguments": {...}, "preview": "..."}
    {"type": "plan",    "goal": "...", "steps": [{"id": 1, "task": "..."}, ...]}
    {"type": "step",    "id": 1, "status": "running|done|failed|skipped", "attempt": 1,
                        "detail": "result preview or error"}
    {"type": "token",   "content": "Hel"}  repeated
    {"type": "done",    "latency_ms": 812.4, "stopped": false, "model": "...", "request_id": "..."}
    {"type": "error",   "error_type": "llm_unavailable", "message": "..."}
    {"type": "cleared"}
    {"type": "pong"}
    {"type": "reminder", "id": 3, "text": "call mum", "due": "today at 5:00 pm",
                         "due_at": "2026-10-01T17:00+08:00", "late": false, "repeat": "none"}
                         pushed at any time, also in the middle of an answer
"""

import asyncio
import contextlib
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.agent.orchestrator import Orchestrator
from app.agent.state import (
    ConfirmationEvent,
    PlanEvent,
    StepEvent,
    TextEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from app.api.dependencies import new_session_id, valid_session_id
from app.api.errors import classify_llm_error
from app.api.routes.chat import MessageIn
from app.llm.base import LLMError
from app.observability.logging import get_logger
from app.scheduler.runner import NotificationHub, ReminderScheduler
from app.security.network import host_allowed

router = APIRouter()
log = get_logger(__name__)


# ---------- incoming message schemas ----------


class ChatIn(MessageIn):  # reuses the same validation as POST /chat
    type: Literal["chat"]


class StopIn(BaseModel):
    type: Literal["stop"]


class ClearIn(BaseModel):
    type: Literal["clear"]


class PingIn(BaseModel):
    type: Literal["ping"]


# "discriminator" = pick the right model by looking at the "type" field.
ClientMessage = TypeAdapter(
    Annotated[ChatIn | StopIn | ClearIn | PingIn, Field(discriminator="type")]
)


# ---------- security ----------


def origin_allowed(websocket: WebSocket) -> bool:
    """Block other websites from using ARTHUR through your browser.

    Browsers let any web page open a WebSocket to localhost. Without this check,
    a malicious site you visit could silently chat with (and later command) ARTHUR.
    Browsers always send an Origin header, so we require it to match our own host.
    Non-browser clients (scripts, tests) send no Origin and are allowed.
    """
    host = websocket.headers.get("host")
    extra = getattr(websocket.app.state, "allowed_hosts", frozenset())
    if not host_allowed(host, extra):
        return False  # DNS rebinding: a foreign name pointed at 127.0.0.1
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == host


# ---------- one connected browser tab ----------


class ChatSession:
    def __init__(
        self,
        websocket: WebSocket,
        orchestrator: Orchestrator,
        session_id: str,
        hub: NotificationHub | None = None,
        scheduler: ReminderScheduler | None = None,
    ) -> None:
        self.ws = websocket
        self.orchestrator = orchestrator
        self.session_id = session_id
        self.hub = hub
        self.scheduler = scheduler
        self.reply_task: asyncio.Task | None = None

    async def run(self) -> None:
        history = [
            m.model_dump(mode="json", include={"role", "content"})
            for m in self.orchestrator.history(self.session_id)
        ]
        await self._send({"type": "session", "session_id": self.session_id, "history": history})
        metrics = getattr(self.ws.app.state, "metrics", None)
        if metrics is not None:
            metrics.ws_connections.inc()
        if self.hub is not None:
            self.hub.subscribe(self._push)  # this tab now receives reminders
        if self.scheduler is not None:
            self.scheduler.poke()  # deliver anything that became due while no tab was open
        try:
            while True:
                await self._handle(await self.ws.receive_text())
        except WebSocketDisconnect:
            log.info("ws_disconnected")
        finally:
            if metrics is not None:
                metrics.ws_connections.dec()
            if self.hub is not None:
                self.hub.unsubscribe(self._push)
            if self.reply_task:
                self.reply_task.cancel()  # stop generating for a closed tab

    async def _push(self, event: dict) -> None:
        """A notification from the scheduler. Unlike _send, a closed tab raises here,
        so the reminder is not counted as delivered."""
        await self.ws.send_json(event)

    @property
    def busy(self) -> bool:
        return self.reply_task is not None and not self.reply_task.done()

    async def _handle(self, raw: str) -> None:
        try:
            message = ClientMessage.validate_json(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            await self._send_error("invalid_message", f"{first['msg']} ({first['loc']})")
            return

        match message:
            case ChatIn():
                if self.busy:
                    await self._send_error("busy", "ARTHUR is still answering. Stop it first.")
                    return
                limiter = getattr(self.ws.app.state, "rate_limiter", None)
                client = self.ws.client.host if self.ws.client else "unknown"
                wait = limiter.check(client, "chat") if limiter else None
                if wait is not None:
                    await self._send_error(
                        "rate_limited", f"Too many messages - try again in {round(wait)} seconds."
                    )
                    return
                # Run the reply as a background task so we keep listening for "stop".
                self.reply_task = asyncio.create_task(self._reply(message.message))
            case StopIn():
                await self._cancel_reply()
            case ClearIn():
                await self._cancel_reply()
                self.orchestrator.clear(self.session_id)
                log.info("conversation_cleared")
                await self._send({"type": "cleared"})
            case PingIn():
                await self._send({"type": "pong"})

    async def _cancel_reply(self) -> None:
        if self.reply_task and self.busy:
            self.reply_task.cancel()
            # Wait until it has finished (and saved any partial answer).
            with contextlib.suppress(asyncio.CancelledError):
                await self.reply_task

    async def _reply(self, user_message: str) -> None:
        request_id = uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(request_id=request_id, path="/ws")
        await self._send({"type": "status", "state": "thinking", "request_id": request_id})

        start = time.perf_counter()
        first_token_ms: float | None = None
        token_count = 0
        tool_count = 0
        stopped = False
        try:
            # aclosing() guarantees the stream's cleanup (saving the answer) runs
            # immediately, even when we're cancelled between two events.
            async with contextlib.aclosing(
                self.orchestrator.events(self.session_id, user_message, request_id)
            ) as events:
                async for event in events:
                    if isinstance(event, TextEvent):
                        if first_token_ms is None:
                            first_token_ms = round((time.perf_counter() - start) * 1000, 1)
                        token_count += 1
                        await self._send({"type": "token", "content": event.text})
                    elif isinstance(event, ToolStartEvent):
                        tool_count += 1
                        await self._send(
                            {"type": "status", "state": "executing", "tool": event.name}
                        )
                        await self._send({"type": "tool", "phase": "start", **_fields(event)})
                    elif isinstance(event, ToolEndEvent):
                        await self._send({"type": "tool", "phase": "end", **_fields(event)})
                        await self._send({"type": "status", "state": "thinking"})
                    elif isinstance(event, ConfirmationEvent):
                        await self._send({"type": "confirmation", **_fields(event)})
                    elif isinstance(event, PlanEvent):
                        await self._send({"type": "plan", **_fields(event)})
                    elif isinstance(event, StepEvent):
                        await self._send({"type": "step", **_fields(event)})
        except asyncio.CancelledError:
            stopped = True  # user pressed stop (or the tab closed)
        except LLMError as exc:
            _, error_type = classify_llm_error(exc)
            log.warning("llm_error", error_type=error_type, detail=str(exc))
            await self._send_error(error_type, str(exc))
            self._count_turn("error")
            return
        except Exception:
            log.exception("ws_reply_failed")
            await self._send_error("internal_error", "Something went wrong inside ARTHUR.")
            self._count_turn("error")
            return
        self._count_turn("stopped" if stopped else "ok")

        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        log.info(
            "chat_stream_completed",
            model=self.orchestrator.llm.model,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,  # "time to first token": how fast it *feels*
            tokens=token_count,
            tool_calls=tool_count,
            stopped=stopped,
        )
        await self._send(
            {
                "type": "done",
                "latency_ms": latency_ms,
                "stopped": stopped,
                "model": self.orchestrator.llm.model,
                "request_id": request_id,
            }
        )

    def _count_turn(self, outcome: str) -> None:
        metrics = getattr(self.ws.app.state, "metrics", None)
        if metrics is not None:
            metrics.chat_turns.labels("ws", outcome).inc()

    async def _send(self, event: dict) -> None:
        # If the tab already closed there is nobody left to tell - ignore the failure.
        with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
            await self.ws.send_json(event)

    async def _send_error(self, error_type: str, message: str) -> None:
        await self._send({"type": "error", "error_type": error_type, "message": message})


def _fields(event: BaseModel) -> dict:
    """Event data without its internal `type`, so it can't overwrite the wire message type."""
    return event.model_dump(mode="json", exclude={"type"})


@router.websocket("/ws")
async def chat_socket(websocket: WebSocket) -> None:
    if not origin_allowed(websocket):
        log.warning("ws_origin_rejected", origin=websocket.headers.get("origin"))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    session_id = valid_session_id(websocket.query_params.get("session_id")) or new_session_id()
    structlog.contextvars.bind_contextvars(session_id=session_id[:8])
    await websocket.accept()
    log.info("ws_connected")
    state = websocket.app.state
    await ChatSession(
        websocket,
        state.orchestrator,
        session_id,
        hub=getattr(state, "hub", None),
        scheduler=getattr(state, "scheduler", None),
    ).run()
