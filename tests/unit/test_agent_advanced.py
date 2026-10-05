"""Phase 27: parallel lookups, context compression, memory priorities, source checks."""

import asyncio
import time
from datetime import UTC, datetime

from pydantic import BaseModel

from app.agent.executor import ToolLoop
from app.agent.orchestrator import Orchestrator
from app.agent.prompts import memory_section
from app.agent.state import ToolEndEvent, ToolStartEvent
from app.agent.verification import source_note, unverified_sources
from app.llm.base import LLMUnavailableError, Message, Role
from app.memory.long_term import Memory
from app.memory.manager import MemorySearchResult
from app.memory.policy import prioritize
from app.memory.short_term import ConversationStore
from app.security.permissions import PermissionPolicy
from app.tools.base import PermissionLevel, Tool, ToolContext
from app.tools.registry import ToolRegistry
from tests.conftest import FakeLLM, tool_call

# ---------- parallel tools ----------


class CityInput(BaseModel):
    city: str


class SlowLookup(Tool[CityInput]):
    name = "slow_lookup"
    description = "Looks something up (slowly)."
    input_model = CityInput
    parallel_safe = True

    async def run(self, args: CityInput, context: ToolContext) -> dict:
        await asyncio.sleep(0.3)
        return {"city": args.city, "temp": 30}


class SlowSharedThing(SlowLookup):
    """Read-only, but shares state (like the one browser page): never in parallel."""

    name = "slow_shared"
    parallel_safe = False


class SlowAction(SlowLookup):
    """Marked parallel by mistake, but it changes things: the level keeps it sequential."""

    name = "slow_action"
    permission_level = PermissionLevel.LOW_RISK


def loop_with(*tools: Tool, script: list) -> ToolLoop:
    registry = ToolRegistry(PermissionPolicy(), None)
    for tool in tools:
        registry.register(tool)
    return ToolLoop(FakeLLM(script=script), registry)


async def run_loop(loop: ToolLoop) -> tuple[list, float, ToolContext]:
    context = ToolContext()
    started = time.perf_counter()
    events = [e async for e in loop.run([Message(role=Role.USER, content="go")], context)]
    return events, time.perf_counter() - started, context


async def test_read_only_lookups_run_at_the_same_time():
    loop = loop_with(
        SlowLookup(),
        script=[
            [tool_call("slow_lookup", city="Singapore"), tool_call("slow_lookup", city="London")],
            "Both are warm.",
        ],
    )
    events, seconds, context = await run_loop(loop)
    assert seconds < 0.5  # two 0.3 s lookups, together
    kinds = [type(e) for e in events if isinstance(e, ToolStartEvent | ToolEndEvent)]
    assert kinds == [ToolStartEvent, ToolStartEvent, ToolEndEvent, ToolEndEvent]
    assert all(e.status == "ok" for e in events if isinstance(e, ToolEndEvent))
    # Both results reached the model, in the order they were asked for.
    tool_messages = [m for m in loop.llm.calls[-1] if m.role == Role.TOOL]
    assert ["Singapore" in tool_messages[0].content, "London" in tool_messages[1].content] == [
        True,
        True,
    ]
    assert len(context.sources) == 2  # kept for the source check


async def test_shared_or_changing_tools_stay_one_at_a_time():
    for tool in (SlowSharedThing(), SlowAction()):
        loop = loop_with(
            SlowLookup(),
            tool,
            script=[[tool_call("slow_lookup", city="A"), tool_call(tool.name, city="B")], "ok"],
        )
        _, seconds, _ = await run_loop(loop)
        assert seconds >= 0.6, tool.name


async def test_a_repeated_call_is_not_run_in_parallel_again():
    loop = loop_with(
        SlowLookup(),
        script=[
            [tool_call("slow_lookup", city="A")],
            [tool_call("slow_lookup", city="A"), tool_call("slow_lookup", city="B")],
            "done",
        ],
    )
    events, _, _ = await run_loop(loop)
    ran = [e.arguments["city"] for e in events if isinstance(e, ToolStartEvent)]
    assert ran == ["A", "B"]  # the repeat was answered from the guard, not run again


def test_only_reading_tools_are_marked_parallel_safe():
    from tests.conftest import _app_with

    registry = _app_with(FakeLLM()).state.tools
    marked = {t.name for t in registry.all() if t.parallel_safe}
    assert "web_search" in marked and "calculator" in marked
    for tool in registry.all():
        if tool.parallel_safe:
            assert tool.permission_level == PermissionLevel.READ_ONLY, tool.name
    assert not marked & {"browser_open", "save_file", "delete_memory", "set_reminder"}


# ---------- context compression ----------


def long_chat_orchestrator(llm: FakeLLM, delay: float = 0.0) -> Orchestrator:
    return Orchestrator(
        llm,
        ConversationStore(),
        context_tokens=2200,
        reply_reserve_tokens=200,
        summary_delay_seconds=delay,
    )


async def test_older_messages_are_summarised_and_shown_as_notes():
    llm = FakeLLM(reply="ok " + "z" * 300)
    orchestrator = long_chat_orchestrator(llm)
    session = "compress-1"
    llm.structured_reply = None
    # generate() (used for the summary) answers from the same script/reply as chat.
    for i in range(12):
        await orchestrator.respond(
            session, f"Message {i}: my project is called Kestrel " + "x" * 300
        )
        await orchestrator.idle()
    conversation = orchestrator.conversations.get(session)
    assert conversation.window_start > 0  # older messages left the window...
    assert conversation.summary  # ...and were compressed
    assert conversation.summarized_until <= conversation.window_start

    messages = orchestrator.build_messages(conversation, "And now?")
    assert messages[1].role == Role.SYSTEM
    assert messages[1].content.startswith("Notes on the earlier part of this conversation")
    assert "DATA" in messages[1].content


async def test_the_summary_prompt_contains_what_scrolled_out():
    llm = FakeLLM(reply="noted " + "z" * 300)
    orchestrator = long_chat_orchestrator(llm)
    for i in range(12):
        await orchestrator.respond("compress-2", f"Fact {i}: " + "x" * 300)
        await orchestrator.idle()
    summary_calls = [c for c in llm.calls if "Older part of the conversation" in c[0].content]
    assert summary_calls
    assert "User: Fact 0:" in summary_calls[0][0].content


async def test_a_failed_summary_changes_nothing():
    llm = FakeLLM(reply="ok " + "z" * 300)
    orchestrator = long_chat_orchestrator(llm)

    async def broken(messages, **kwargs):
        raise LLMUnavailableError("Ollama went away")

    llm.generate = broken
    for i in range(12):
        reply = await orchestrator.respond("compress-3", f"Message {i} " + "x" * 300)
        await orchestrator.idle()
    conversation = orchestrator.conversations.get("compress-3")
    assert conversation.summary == "" and reply.content.startswith("ok")


def test_clearing_a_conversation_forgets_the_notes_too():
    store = ConversationStore()
    conversation = store.get("clear-notes")
    conversation.summary, conversation.summarized_until = "Kestrel project", 4
    conversation.clear()
    assert conversation.summary == "" and conversation.summarized_until == 0


# ---------- memory priorities ----------


def remembered(content: str, score: float, day: int) -> MemorySearchResult:
    when = datetime(2026, 9, day, tzinfo=UTC)
    memory = Memory(
        id=f"m{day}{score}", content=content, category="other", source="chat",
        created_at=when, updated_at=when,
    )  # fmt: skip
    return MemorySearchResult(memory=memory, score=score)


def test_memories_are_ranked_dated_and_capped():
    results = [
        remembered("My favourite colour is blue.", 0.70, 20),
        remembered("My favourite colour is green.", 0.70, 28),  # same relevance, newer
        remembered("I live in Singapore.", 0.90, 1),
        remembered("I like " + "very " * 80 + "long sentences.", 0.60, 2),
    ]
    lines = prioritize(results, max_chars=200)
    assert lines[0] == "[saved 2026-09-01] I live in Singapore."
    assert lines[1].endswith("green.") and lines[2].endswith("blue.")  # newer first on a tie
    assert not any("long sentences" in line for line in lines)  # did not fit


def test_the_model_is_told_that_newer_facts_win():
    section = memory_section(["[saved 2026-09-28] My favourite colour is green."])
    assert "more recently saved one is correct" in section


# ---------- source verification ----------

SEEN = """<context>Relevant passages:
[handbook.pdf, p. 2] Students need 120 credits.
</context>
{"status": "ok", "output": {"results": [{"url": "https://www.python.org/downloads/release/python-3148/"}]}}"""


def test_real_citations_pass_and_invented_ones_are_caught():
    answer = (
        "You need 120 credits [handbook.pdf, p. 2]. Fees are listed too [handbook.pdf, p. 9]. "
        "See [the release](https://www.python.org/downloads/release/python-3148/) and "
        "[python.org](https://www.python.org/) but also [a blog](https://blog.example.com/x)."
    )
    assert unverified_sources(answer, SEEN) == [
        "[handbook.pdf, p. 9]",
        "https://blog.example.com/x",
    ]


def test_answers_without_citations_are_never_flagged():
    assert unverified_sources("The weather is warm. 15% of 240 is 36.", "") == []


def test_the_note_names_the_sources():
    note = source_note(["[ghost.pdf, p. 3]"])
    assert "Source check" in note and "[ghost.pdf, p. 3]" in note and "this source" in note


async def test_an_invented_citation_gets_a_visible_note():
    llm = FakeLLM(
        script=[[tool_call("slow_lookup", city="Paris")], "It is 30 degrees [weather.pdf, p. 4]."]
    )
    registry = ToolRegistry(PermissionPolicy(), None)
    registry.register(SlowLookup())
    orchestrator = Orchestrator(
        llm, ConversationStore(), tools=registry, agent_tools=frozenset({"slow_lookup"}),
        planning=False,
    )  # fmt: skip
    reply = await orchestrator.respond("source-check-1", "How warm is Paris?")
    assert "Source check" in reply.content and "[weather.pdf, p. 4]" in reply.content


async def test_a_link_from_a_tool_result_is_trusted():
    llm = FakeLLM(
        script=[
            [tool_call("slow_lookup", city="https://example.org/paris")],
            "Details: [example.org](https://example.org/paris).",
        ]
    )
    registry = ToolRegistry(PermissionPolicy(), None)
    registry.register(SlowLookup())
    orchestrator = Orchestrator(
        llm, ConversationStore(), tools=registry, agent_tools=frozenset({"slow_lookup"}),
        planning=False,
    )  # fmt: skip
    reply = await orchestrator.respond("source-check-2", "Paris?")
    assert "Source check" not in reply.content


def test_a_front_page_link_to_a_named_source_is_fine_but_a_made_up_page_is_not():
    seen = '{"source": "Open-Meteo (open-meteo.com)", "temp": 31}'
    assert unverified_sources("From [Open-Meteo](https://open-meteo.com).", seen) == []
    assert unverified_sources("See [docs](https://open-meteo.com/en/docs/made-up).", seen) == [
        "https://open-meteo.com/en/docs/made-up"
    ]


async def test_the_summary_waits_until_the_user_pauses():
    llm = FakeLLM(reply="ok " + "z" * 300)
    orchestrator = long_chat_orchestrator(llm, delay=0.3)
    session = "compress-pause"
    for i in range(12):  # back-to-back messages: every new one cancels the waiting summary
        await orchestrator.respond(session, f"Message {i} " + "x" * 300)
    conversation = orchestrator.conversations.get(session)
    assert conversation.window_start > 0 and conversation.summary == ""
    summary_calls = [c for c in llm.calls if "Older part" in c[0].content]
    assert summary_calls == []  # no summary competed with the answers
    await asyncio.sleep(0.5)  # the user pauses...
    assert conversation.summary  # ...and only now the notes are written
