"""WebSocket /ws - real-time chat with streamed replies.

HTTP is request -> response: the browser waits for the whole answer.
A WebSocket stays open in both directions, so ARTHUR can push each word
as the model produces it, and the browser can send "stop" mid-answer.

Protocol (every frame is one JSON object with a "type"):

  browser -> server
    {"type": "chat", "message": "Hello Arthur"}
    {"type": "stop"}                     cancel the answer in progress
    {"type": "ping"}                     keep-alive / latency check

  server -> browser
    {"type": "status", "state": "thinking", "request_id": "..."}
    {"type": "token",  "content": "Hel"}  repeated
    {"type": "done",   "latency_ms": 812.4, "stopped": false, "model": "...", "request_id": "..."}
    {"type": "error",  "error_type": "llm_unavailable", "message": "..."}
    {"type": "pong"}
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

from app.agent.prompts import build_chat_messages
from app.api.errors import classify_llm_error
from app.api.routes.chat import ChatRequest
from app.llm.base import LLMError, LLMProvider
from app.observability.logging import get_logger

router = APIRouter()
log = get_logger(__name__)


# ---------- incoming message schemas ----------


class ChatIn(ChatRequest):  # reuses the same validation as POST /chat
    type: Literal["chat"]


class StopIn(BaseModel):
    type: Literal["stop"]


class PingIn(BaseModel):
    type: Literal["ping"]


# "discriminator" = pick the right model by looking at the "type" field.
ClientMessage = TypeAdapter(Annotated[ChatIn | StopIn | PingIn, Field(discriminator="type")])


# ---------- security ----------


def origin_allowed(websocket: WebSocket) -> bool:
    """Block other websites from using ARTHUR through your browser.

    Browsers let any web page open a WebSocket to localhost. Without this check,
    a malicious site you visit could silently chat with (and later command) ARTHUR.
    Browsers always send an Origin header, so we require it to match our own host.
    Non-browser clients (scripts, tests) send no Origin and are allowed.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == websocket.headers.get("host")


# ---------- one connected browser tab ----------


class ChatSession:
    def __init__(self, websocket: WebSocket, llm: LLMProvider) -> None:
        self.ws = websocket
        self.llm = llm
        self.reply_task: asyncio.Task | None = None

    async def run(self) -> None:
        try:
            while True:
                await self._handle(await self.ws.receive_text())
        except WebSocketDisconnect:
            log.info("ws_disconnected")
        finally:
            if self.reply_task:
                self.reply_task.cancel()  # stop generating for a closed tab

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
                # Run the reply as a background task so we keep listening for "stop".
                self.reply_task = asyncio.create_task(self._reply(message.message))
            case StopIn():
                if self.reply_task and self.busy:
                    self.reply_task.cancel()
            case PingIn():
                await self._send({"type": "pong"})

    async def _reply(self, user_message: str) -> None:
        request_id = uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(request_id=request_id, path="/ws")
        await self._send({"type": "status", "state": "thinking", "request_id": request_id})

        start = time.perf_counter()
        first_token_ms: float | None = None
        token_count = 0
        stopped = False
        try:
            async for token in self.llm.stream(build_chat_messages(user_message)):
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - start) * 1000, 1)
                token_count += 1
                await self._send({"type": "token", "content": token})
        except asyncio.CancelledError:
            stopped = True  # user pressed stop (or the tab closed)
        except LLMError as exc:
            _, error_type = classify_llm_error(exc)
            log.warning("llm_error", error_type=error_type, detail=str(exc))
            await self._send_error(error_type, str(exc))
            return
        except Exception:
            log.exception("ws_reply_failed")
            await self._send_error("internal_error", "Something went wrong inside ARTHUR.")
            return

        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        log.info(
            "chat_stream_completed",
            model=self.llm.model,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,  # "time to first token": how fast it *feels*
            tokens=token_count,
            stopped=stopped,
        )
        await self._send(
            {
                "type": "done",
                "latency_ms": latency_ms,
                "stopped": stopped,
                "model": self.llm.model,
                "request_id": request_id,
            }
        )

    async def _send(self, event: dict) -> None:
        # If the tab already closed there is nobody left to tell - ignore the failure.
        with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
            await self.ws.send_json(event)

    async def _send_error(self, error_type: str, message: str) -> None:
        await self._send({"type": "error", "error_type": error_type, "message": message})


@router.websocket("/ws")
async def chat_socket(websocket: WebSocket) -> None:
    if not origin_allowed(websocket):
        log.warning("ws_origin_rejected", origin=websocket.headers.get("origin"))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    log.info("ws_connected")
    await ChatSession(websocket, websocket.app.state.llm).run()
