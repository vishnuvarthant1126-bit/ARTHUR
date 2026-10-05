"""Phase 28: every failure from the spec, one by one - and ARTHUR keeps running.

LLM unavailable · internet unavailable · tool failure · invalid tool arguments · timeout ·
bad document · unsupported file · microphone unavailable · TTS failure · database failure ·
model failure. docs/FAILURES.md lists what the user sees for each.
"""

import asyncio
import re
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.exc import OperationalError

from app.agent.orchestrator import EMPTY_ANSWER, STORAGE_TROUBLE, Orchestrator
from app.llm.base import LLMResponseError, LLMUnavailableError
from app.memory.short_term import ConversationStore
from app.security.permissions import PermissionPolicy
from app.tools.base import Tool, ToolContext
from app.tools.calculator import CalculatorTool
from app.tools.registry import ToolRegistry
from tests.conftest import FakeLLM, tool_call

ROOT = Path(__file__).resolve().parents[2]


async def still_alive(client) -> None:
    """The point of every test here: after the failure, ARTHUR still answers."""
    assert (await client.get("/health")).status_code == 200


def database_down(*args, **kwargs):
    raise OperationalError("SELECT ...", {}, Exception("database disk image is malformed"))


async def failing_async(*args, **kwargs):
    database_down()


# ---------- 1. LLM unavailable ----------


async def test_llm_unavailable_gives_a_clear_error_and_recovers(client, fake_llm):
    fake_llm.error = LLMUnavailableError("Can't reach Ollama at http://127.0.0.1:11434.")
    response = await client.post("/chat", json={"message": "Hello"})
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "llm_unavailable" and "Ollama" in error["message"]
    status = (await client.get("/status")).json()
    assert status["overall"] == "ok" or status["components"][0]["state"] in ("ok", "problem")

    fake_llm.error = None  # Ollama is back: no restart needed
    assert (await client.post("/chat", json={"message": "Hello"})).status_code == 200


def test_llm_unavailable_over_the_websocket_is_an_error_event(ws_client, fake_llm):
    from tests.unit.test_websocket import receive_until_done, session

    fake_llm.error = LLMUnavailableError("Can't reach Ollama.")
    with session(ws_client) as (ws, _):
        ws.send_json({"type": "chat", "message": "Hello"})
        events = receive_until_done(ws)
        assert events[-1]["type"] == "error" and events[-1]["error_type"] == "llm_unavailable"
        fake_llm.error = None
        ws.send_json({"type": "chat", "message": "Hello again"})  # same connection still works
        assert receive_until_done(ws)[-1]["type"] == "done"


# ---------- 2. Internet unavailable ----------


async def test_no_internet_the_tool_fails_and_the_answer_says_so(client, fake_llm):
    # The test app's HTTP client fails every request, like a cable pulled out.
    fake_llm.script = [[tool_call("weather", location="Paris")], "I couldn't get the weather."]
    response = await client.post("/chat", json={"message": "Weather in Paris?"})
    body = response.json()
    assert response.status_code == 200
    assert body["tools_used"][0]["name"] == "weather"
    assert body["tools_used"][0]["status"] == "error"
    assert body["response"] == "I couldn't get the weather."
    await still_alive(client)


# ---------- 3. Tool failure ----------


class Empty(BaseModel):
    pass


class CrashingTool(Tool[Empty]):
    name = "crashes"
    description = "Always crashes."
    input_model = Empty

    async def run(self, args: Empty, context: ToolContext) -> None:
        raise RuntimeError("segfault in a library")


class SlowTool(Tool[Empty]):
    name = "slow"
    description = "Takes too long."
    input_model = Empty
    timeout_seconds = 0.05

    async def run(self, args: Empty, context: ToolContext) -> None:
        await asyncio.sleep(5)


def agent_with(*tools: Tool, script: list) -> Orchestrator:
    registry = ToolRegistry(PermissionPolicy(), None)
    for tool in tools:
        registry.register(tool)
    names = frozenset(t.name for t in tools)
    return Orchestrator(
        FakeLLM(script=script), ConversationStore(), tools=registry, agent_tools=names,
        planning=False,
    )  # fmt: skip


async def test_a_crashing_tool_becomes_an_error_result_not_a_crash():
    agent = agent_with(CrashingTool(), script=[[tool_call("crashes")], "That tool failed."])
    reply = await agent.respond("tool-crash-1", "Do it")
    assert reply.tools_used[0].status == "error"
    assert "segfault" not in reply.tools_used[0].summary  # no internals shown
    assert reply.content == "That tool failed."
    # The model was told the tool failed, so it could say so.
    tool_message = [m for m in agent.llm.calls[-1] if m.role == "tool"][0]
    assert '"status": "error"' in tool_message.content


# ---------- 4. Invalid tool arguments ----------


async def test_invalid_arguments_are_refused_and_the_model_can_retry():
    agent = agent_with(
        CalculatorTool(),
        script=[
            [tool_call("calculator", formula="2+2")],  # wrong argument name
            [tool_call("calculator", expression="2+2")],  # corrected
            "2 + 2 = 4",
        ],
    )
    reply = await agent.respond("bad-args-1", "What is 2+2?")
    assert [t.status for t in reply.tools_used] == ["error", "ok"]
    assert reply.content == "2 + 2 = 4"


async def test_a_tool_the_model_invented_is_refused():
    agent = agent_with(CalculatorTool(), script=[[tool_call("launch_rocket")], "I can't do that."])
    reply = await agent.respond("unknown-tool-1", "Launch it")
    assert reply.tools_used[0].status in ("error", "denied")
    assert reply.content == "I can't do that."


# ---------- 5. Timeout ----------


async def test_a_tool_that_hangs_is_stopped_by_its_timeout():
    agent = agent_with(SlowTool(), script=[[tool_call("slow")], "It took too long."])
    started = asyncio.get_running_loop().time()
    reply = await agent.respond("timeout-1", "Go")
    assert asyncio.get_running_loop().time() - started < 2
    assert reply.tools_used[0].status == "error"
    assert "time" in reply.tools_used[0].summary.lower()


# ---------- 6. Bad document / 7. Unsupported file ----------


async def test_a_broken_document_is_refused_with_a_reason(client):
    response = await client.post(
        "/documents", files={"file": ("report.pdf", b"%PDF-1.4 garbage", "application/pdf")}
    )
    assert response.status_code == 422
    assert "PDF" in response.json()["detail"]
    await still_alive(client)


async def test_unsupported_files_are_refused_everywhere(client):
    upload = await client.post("/documents", files={"file": ("tool.exe", b"MZ", "x/y")})
    attach = await client.post("/attachments", files={"file": ("tool.exe", b"MZ", "x/y")})
    assert upload.status_code == 422 and "Unsupported" in upload.json()["detail"]
    assert attach.status_code == 415 and "Unsupported" in attach.json()["detail"]


# ---------- 8. Microphone unavailable (browser side) ----------


def test_every_microphone_problem_has_a_human_message():
    script = (ROOT / "frontend" / "app.js").read_text("utf-8")
    block = re.search(r"function micError\(error\) \{(.+?)\n\}", script, re.DOTALL).group(1)
    for name in ("NotAllowedError", "NotFoundError", "NotReadableError", "SecurityError"):
        assert f"{name}:" in block
    assert "Voice input isn't supported in this browser" in script  # no MediaRecorder at all


# ---------- 9. TTS failure ----------


class BrokenTTS:
    def voices(self):
        return []

    async def synthesize(self, text, *, voice=None, speed=1.0):
        raise RuntimeError("onnxruntime: model file is corrupt")


async def test_a_broken_voice_engine_is_reported_not_silent(client):
    client._transport.app.state.tts = BrokenTTS()
    response = await client.post("/voice/speak", json={"text": "Hello there."})
    assert response.status_code == 503
    assert "still shown as text" in response.json()["detail"]
    assert "onnxruntime" not in response.text
    parts = {c["name"]: c for c in (await client.get("/status")).json()["components"]}
    assert parts["Voice"]["state"] == "problem"  # and the status rail shows it
    await still_alive(client)


def test_the_page_tells_the_user_when_speech_fails():
    script = (ROOT / "frontend" / "app.js").read_text("utf-8")
    assert "function speechFailed(" in script
    assert "speechFailed(generation" in script  # called on errors and on network failure


# ---------- 10. Database failure ----------


async def test_a_broken_memory_database_does_not_stop_answers(client, fake_llm):
    memory = client._transport.app.state.memory
    memory.search_memory = failing_async
    response = await client.post("/chat", json={"message": "What's my name?"})
    assert response.status_code == 200
    assert response.json()["response"] == fake_llm.reply  # answered, just without memory


async def test_remember_and_forget_explain_a_broken_database(client):
    memory = client._transport.app.state.memory
    # A broken database file breaks every memory operation at once.
    memory.save_memory = memory.search_memory = memory.count = failing_async
    saved = (await client.post("/chat", json={"message": "Remember that I like tea"})).json()
    forgot = (await client.post("/chat", json={"message": "Forget that I like tea"})).json()
    assert saved["response"] == STORAGE_TROUBLE
    assert forgot["response"] == STORAGE_TROUBLE
    assert "malformed" not in saved["response"]
    parts = {c["name"]: c for c in (await client.get("/status")).json()["components"]}
    assert parts["Memory"]["state"] == "problem"
    await still_alive(client)


async def test_confirming_a_delete_during_a_database_failure_is_explained(client):
    from app.memory.short_term import PendingAction

    app = client._transport.app
    session_id = "db-down-delete"
    conversation = app.state.orchestrator.conversations.get(session_id)
    conversation.pending = PendingAction(
        kind="delete_memory", target_id="m1", description="I like tea"
    )
    app.state.memory.delete_memory = failing_async
    body = (await client.post("/chat", json={"message": "yes", "session_id": session_id})).json()
    assert body["response"] == STORAGE_TROUBLE


# ---------- 11. Model failure ----------


async def test_a_missing_model_says_how_to_fix_it(client, fake_llm):
    fake_llm.error = LLMResponseError(
        "Model 'qwen3:8b' is not installed. Run: ollama pull qwen3:8b"
    )
    response = await client.post("/chat", json={"message": "Hi"})
    assert response.status_code == 502
    assert "ollama pull" in response.json()["error"]["message"]


async def test_a_model_that_says_nothing_still_gets_a_visible_reply():
    agent = agent_with(CalculatorTool(), script=[""])
    reply = await agent.respond("silent-model-1", "Hello?")
    assert reply.content == EMPTY_ANSWER


async def test_a_model_that_fails_mid_answer_keeps_the_conversation_usable(client, fake_llm):
    fake_llm.script = [LLMUnavailableError("Ollama stopped")]
    first = await client.post("/chat", json={"message": "Hi", "session_id": "mid-fail-0001"})
    assert first.status_code == 503
    second = await client.post("/chat", json={"message": "Hi", "session_id": "mid-fail-0001"})
    assert second.status_code == 200


async def test_the_page_files_are_always_rechecked_so_updates_arrive(client):
    # Found live: after an update the browser kept running the old app.js from its cache.
    response = await client.get("/app.js")
    assert response.headers["cache-control"] == "no-cache"
    again = await client.get("/app.js", headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304  # unchanged: cheap answer, nothing downloaded
