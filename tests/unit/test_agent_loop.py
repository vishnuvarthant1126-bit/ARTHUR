"""Phase 7: the agent loop - tool calling, limits, confirmation, and the API around it."""

import pytest

from app.agent.executor import AgentLimits, ToolLoop
from app.agent.orchestrator import Orchestrator
from app.agent.state import ConfirmationEvent, TextEvent, ToolEndEvent, ToolStartEvent
from app.llm.base import Message, Role
from app.memory.short_term import ConversationStore
from app.security.permissions import PermissionPolicy
from app.tools.base import ToolContext
from app.tools.defaults import create_tool_registry
from tests.conftest import FakeLLM, make_memory, offline_http_client, tool_call

QUESTION = [Message(role=Role.USER, content="What is 482 times 29?")]


def registry(memory=None, audit=None):
    return create_tool_registry(
        policy=PermissionPolicy(), audit=audit, http_client=offline_http_client(), memory=memory
    )


async def collect(stream) -> list:
    return [event async for event in stream]


def texts(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextEvent))


# ---------- the loop ----------


async def test_calculator_is_called_and_result_fed_back():
    llm = FakeLLM(script=[[tool_call("calculator", expression="482 * 29")], "It is 13978."])

    events = await collect(ToolLoop(llm, registry()).run(QUESTION, ToolContext()))

    start, end = events[0], events[1]
    assert isinstance(start, ToolStartEvent) and start.name == "calculator"
    assert isinstance(end, ToolEndEvent) and end.status == "ok"
    assert end.summary == "482 * 29 = 13978"
    assert texts(events) == "It is 13978."
    # Second LLM call saw: the assistant's tool request + the tool's result.
    second_call = llm.calls[1]
    assert second_call[-2].role == Role.ASSISTANT
    assert second_call[-2].tool_calls[0].name == "calculator"
    assert second_call[-1].role == Role.TOOL
    assert '"result": 13978' in second_call[-1].content


async def test_no_tool_for_small_talk():
    llm = FakeLLM(script=["Hi! How can I help?"])

    events = await collect(ToolLoop(llm, registry()).run(QUESTION, ToolContext()))

    assert [type(e) for e in events] == [TextEvent] * len(events)
    assert len(llm.calls) == 1


async def test_multiple_tools_across_steps():
    llm = FakeLLM(
        script=[
            [tool_call("current_time", timezone="Asia/Singapore")],
            [tool_call("calculator", expression="24 - 8")],
            "Done.",
        ]
    )

    events = await collect(ToolLoop(llm, registry()).run(QUESTION, ToolContext()))

    assert [e.name for e in events if isinstance(e, ToolEndEvent)] == ["current_time", "calculator"]
    assert texts(events) == "Done."


async def test_tool_error_is_reported_to_the_model_and_loop_continues():
    llm = FakeLLM(script=[[tool_call("calculator", expression="1 / 0")], "Can't divide by zero."])

    events = await collect(ToolLoop(llm, registry()).run(QUESTION, ToolContext()))

    end = next(e for e in events if isinstance(e, ToolEndEvent))
    assert end.status == "error"
    assert "Division by zero" in llm.calls[1][-1].content
    assert texts(events) == "Can't divide by zero."


async def test_unknown_tool_does_not_crash():
    llm = FakeLLM(script=[[tool_call("format_hard_drive")], "Sorry, I can't do that."])

    events = await collect(ToolLoop(llm, registry()).run(QUESTION, ToolContext()))

    assert "Unknown tool" in llm.calls[1][-1].content
    assert texts(events) == "Sorry, I can't do that."


async def test_identical_repeat_call_is_not_executed_twice():
    same = tool_call("calculator", expression="2 + 2")
    llm = FakeLLM(script=[[same], [same], "4"])

    events = await collect(ToolLoop(llm, registry()).run(QUESTION, ToolContext()))

    assert sum(isinstance(e, ToolStartEvent) for e in events) == 1
    assert "already made this exact call" in llm.calls[2][-1].content


async def test_step_limit_stops_a_model_that_never_finishes():
    llm = FakeLLM(script=[[tool_call("calculator", expression=f"1 + {i}")] for i in range(50)])
    loop = ToolLoop(llm, registry(), limits=AgentLimits(max_steps=3))

    events = await collect(loop.run(QUESTION, ToolContext()))

    assert len(llm.calls) == 3
    assert "stopped after 3 steps" in texts(events)


async def test_time_limit_stops_the_loop():
    llm = FakeLLM(script=[[tool_call("calculator", expression="1 + 1")], "never reached"])
    loop = ToolLoop(llm, registry(), limits=AgentLimits(max_seconds=-1))

    events = await collect(loop.run(QUESTION, ToolContext()))

    assert "took too long" in texts(events)
    assert llm.calls == []


async def test_save_memory_is_not_offered_to_the_agent():
    memory, _ = make_memory()
    llm = FakeLLM(script=["ok"])
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))

    await orchestrator.respond("s1", "hello")

    offered = {t["function"]["name"] for t in llm.tools_offered[0]}
    assert "calculator" in offered
    assert "save_memory" not in offered


async def test_long_tool_output_is_truncated_for_the_model():
    llm = FakeLLM(script=[[tool_call("calculator", expression="2 ** 5000")], "big"])
    loop = ToolLoop(llm, registry(), limits=AgentLimits(max_result_chars=200))

    await collect(loop.run(QUESTION, ToolContext()))

    assert llm.calls[1][-1].content.endswith("...(truncated)")


# ---------- confirmation (level-2 tools) ----------


async def make_delete_scenario():
    memory, _ = make_memory()
    saved, _ = await memory.save_memory("The user's favourite colour is blue.")
    llm = FakeLLM(
        script=[[tool_call("delete_memory", memory_id=saved.id, confirmed=True)]],
    )
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))
    return orchestrator, memory


async def test_level_2_tool_pauses_for_confirmation():
    orchestrator, memory = await make_delete_scenario()

    events = await collect(orchestrator.events("s1", "Delete my colour memory"))

    confirmation = next(e for e in events if isinstance(e, ConfirmationEvent))
    assert confirmation.preview == 'Forget the memory "The user\'s favourite colour is blue."'
    assert "I need your permission first" in texts(events)
    # The model even tried to pass confirmed=True itself - it has no effect.
    assert await memory.count() == 1


async def test_yes_runs_the_confirmed_tool():
    orchestrator, memory = await make_delete_scenario()
    await orchestrator.respond("s1", "Delete my colour memory")

    reply = await orchestrator.respond("s1", "yes")

    assert reply.content.startswith("Done:")
    assert await memory.count() == 0


async def test_no_cancels_the_tool():
    orchestrator, memory = await make_delete_scenario()
    await orchestrator.respond("s1", "Delete my colour memory")

    reply = await orchestrator.respond("s1", "no")

    assert reply.content == "Okay, I won't do that."
    assert await memory.count() == 1


# ---------- honesty check ----------


@pytest.mark.parametrize(
    ("text", "claims"),
    [
        ("I have deleted the memory entry for your main project.", True),
        ("I've saved that for you.", True),
        ("The memory has been removed.", True),
        ("Would you like me to delete it?", False),
        ("I can delete it if you want.", False),
        ("It is 13978.", False),
    ],
)
def test_claims_action(text, claims):
    from app.agent.verification import claims_action

    assert claims_action(text) is claims


async def test_false_action_claim_gets_a_visible_correction():
    memory, _ = make_memory()
    llm = FakeLLM(script=["I have deleted the memory entry for your main project."])
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))

    reply = await orchestrator.respond("s1", "yes, go ahead and delete it")

    assert "Correction" in reply.content
    assert "no action was actually performed" in reply.content


async def test_fake_permission_question_is_flagged():
    """The model copied ARTHUR's own permission message without calling a tool."""
    memory, _ = make_memory()
    llm = FakeLLM(
        script=[
            'I need your permission first: **Type "x" into "Password"**\n\n'
            "Reply **yes** to go ahead or **no** to cancel."
        ]
    )
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))

    reply = await orchestrator.respond("s1", "type my password")

    assert "no action is waiting for approval" in reply.content
    assert orchestrator.conversations.get("s1").pending is None


def test_copied_history_note_is_flagged_too():
    from app.agent.verification import fakes_permission_request

    copied = '(ARTHUR\'s safety system asked the user to approve: **Delete "x" in Explorer**)'
    assert fakes_permission_request(copied)
    assert not fakes_permission_request("The calculator shows 9,396.")


async def test_real_permission_question_is_not_flagged():
    memory, _ = make_memory()
    saved, _ = await memory.save_memory("The user likes tea.")
    llm = FakeLLM(script=[[tool_call("delete_memory", memory_id=saved.id)]])
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))

    reply = await orchestrator.respond("s1", "delete my tea memory")

    assert "Reply **yes**" in reply.content
    assert "no action is waiting" not in reply.content


async def test_history_keeps_no_permission_template_to_copy():
    """The model copied earlier "Reply **yes**" messages instead of calling tools."""
    memory, _ = make_memory()
    saved, _ = await memory.save_memory("The user likes tea.")
    llm = FakeLLM(script=[[tool_call("delete_memory", memory_id=saved.id)]])
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))

    reply = await orchestrator.respond("s1", "delete my tea memory")
    assert "Reply **yes**" in reply.content  # the user still sees the real question

    history = orchestrator.conversations.get("s1").messages[-1].content
    assert "Reply **yes**" not in history
    assert "safety system asked the user to approve" in history


async def test_deleting_a_memory_that_does_not_exist_never_asks():
    """Seen live: the model passed a FILE name to delete_memory."""
    memory, _ = make_memory()
    llm = FakeLLM(
        script=[
            [tool_call("delete_memory", memory_id="Sample_Resume_2025.pdf")],
            "That isn't a memory, so nothing was deleted.",
        ]
    )
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))

    reply = await orchestrator.respond("s1", "delete the 2025 resume")

    assert "Reply **yes**" not in reply.content
    assert orchestrator.conversations.get("s1").pending is None


async def test_true_action_claim_is_not_corrected():
    memory, _ = make_memory()
    saved, _ = await memory.save_memory("The user likes tea.")
    llm = FakeLLM(script=[[tool_call("delete_memory", memory_id=saved.id)]])
    orchestrator = Orchestrator(llm, ConversationStore(), memory=memory, tools=registry(memory))

    reply = await orchestrator.respond("s1", "delete my tea memory")

    assert "Correction" not in reply.content  # it asked for confirmation instead


async def test_read_only_tools_do_not_count_as_actions():
    llm = FakeLLM(
        script=[[tool_call("calculator", expression="1 + 1")], "I've saved the result: 2."]
    )
    orchestrator = Orchestrator(llm, ConversationStore(), tools=registry())

    reply = await orchestrator.respond("s1", "what is 1 + 1")

    assert "Correction" in reply.content


# ---------- API ----------


async def test_chat_api_reports_tools_used(client, fake_llm):
    fake_llm.script = [[tool_call("calculator", expression="25 * 50")], "25 Ã— 50 = 1250"]

    body = (await client.post("/chat", json={"message": "What is 25 Ã— 50?"})).json()

    assert body["response"] == "25 Ã— 50 = 1250"
    assert body["tools_used"] == [
        {"name": "calculator", "status": "ok", "summary": "25 * 50 = 1250"}
    ]


def test_websocket_streams_tool_events(ws_client, fake_llm):
    fake_llm.script = [[tool_call("calculator", expression="482 * 29")], "13978"]

    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "chat", "message": "What is 482 times 29?"})
        events = []
        while not events or events[-1]["type"] not in ("done", "error"):
            events.append(ws.receive_json())

    kinds = [(e["type"], e.get("phase") or e.get("state")) for e in events]
    assert ("status", "executing") in kinds
    assert ("tool", "start") in kinds
    assert ("tool", "end") in kinds
    tool_end = next(e for e in events if e.get("phase") == "end")
    assert tool_end["summary"] == "482 * 29 = 13978"
    assert events[-1]["type"] == "done"


@pytest.mark.parametrize("answer", ["yes", "no"])
async def test_audit_log_records_agent_tool_calls(answer):
    memory, db = make_memory()
    from app.security.audit import AuditLog

    audit = AuditLog(db)
    llm = FakeLLM(script=[[tool_call("calculator", expression="1 + 1")], "2"])
    orchestrator = Orchestrator(
        llm, ConversationStore(), memory=memory, tools=registry(memory, audit)
    )

    await orchestrator.respond("s1", "What is 1 + 1?")

    assert [e.tool for e in audit.recent()] == ["calculator"]
