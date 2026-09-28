"""Phase 8: planner, plan executor (retries, failures, limits) and the planned chat flow."""

import json

import pytest

from app.agent.executor import PlanExecutor, PlanLimits
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner, looks_complex
from app.agent.state import (
    ConfirmationEvent,
    PlanEvent,
    StepEvent,
    StepState,
    StepStatus,
    TaskState,
    TextEvent,
    ToolEndEvent,
)
from app.llm.base import LLMUnavailableError
from app.memory.short_term import ConversationStore
from app.security.permissions import PermissionPolicy
from app.tools.base import ToolContext
from app.tools.defaults import create_tool_registry
from tests.conftest import FakeLLM, make_memory, offline_http_client, tool_call

TOOLS = {"calculator": "math", "current_time": "clock"}


def registry(memory=None):
    return create_tool_registry(
        policy=PermissionPolicy(), audit=None, http_client=offline_http_client(), memory=memory
    )


def plan_json(*tasks: str, goal: str = "Answer the question") -> str:
    return json.dumps(
        {"goal": goal, "steps": [{"id": i, "task": t} for i, t in enumerate(tasks, 1)]}
    )


def task_state(*tasks: str) -> TaskState:
    return TaskState(goal="g", steps=[StepState(id=i, task=t) for i, t in enumerate(tasks, 1)])


async def collect(stream) -> list:
    return [event async for event in stream]


# ---------- deciding whether to plan ----------


@pytest.mark.parametrize(
    ("text", "complex_"),
    [
        (
            "What's the weather in Singapore and London, and what's the temperature difference?",
            True,
        ),
        ("Check the time in Tokyo and then tell me how many hours until midnight there", True),
        ("Compare the weather in Paris with the weather in Rome today please", True),
        ("What is 482 times 29?", False),
        ("Hi Arthur, how are you?", False),
        ("Compare them", False),  # too short to be a real multi-part request
    ],
)
def test_looks_complex(text, complex_):
    assert looks_complex(text) is complex_


async def test_planner_returns_structured_plan():
    llm = FakeLLM(structured_reply=plan_json("Get weather in Singapore", "Get weather in London"))

    plan = await Planner(llm, TOOLS).make_plan("weather in Singapore and London")

    assert [s.task for s in plan.steps] == ["Get weather in Singapore", "Get weather in London"]
    assert "calculator: math" in llm.calls[0][0].content  # tool menu is in the prompt


async def test_single_step_plan_means_no_plan():
    llm = FakeLLM(structured_reply=plan_json("Multiply the numbers"))
    assert await Planner(llm, TOOLS).make_plan("x") is None


async def test_planner_renumbers_steps():
    raw = json.dumps({"goal": "g", "steps": [{"id": 7, "task": "a"}, {"id": 7, "task": "b"}]})
    plan = await Planner(FakeLLM(structured_reply=raw), TOOLS).make_plan("x")
    assert [s.id for s in plan.steps] == [1, 2]


async def test_planner_failure_falls_back_to_no_plan():
    llm = FakeLLM(error=LLMUnavailableError("down"))
    assert await Planner(llm, TOOLS).make_plan("x") is None


async def test_invalid_plan_json_means_no_plan():
    assert await Planner(FakeLLM(structured_reply="nonsense"), TOOLS).make_plan("x") is None


# ---------- executing a plan ----------


async def test_executor_runs_each_step_and_records_results():
    llm = FakeLLM(
        script=[
            [tool_call("calculator", expression="2 + 3")],
            "The sum is 5.",
            [tool_call("calculator", expression="5 * 10")],
            "The product is 50.",
        ]
    )
    state = task_state("Add 2 and 3", "Multiply the sum by 10")

    events = await collect(PlanExecutor(llm, registry()).run(state, ToolContext()))

    assert isinstance(events[0], PlanEvent)
    assert [s.status for s in state.steps] == [StepStatus.DONE, StepStatus.DONE]
    assert state.steps[1].result == "The product is 50."
    # Step 2's prompt contained step 1's result.
    step2_prompt = llm.calls[2][0].content
    assert "Step 1: The sum is 5." in step2_prompt
    # Step texts are internal - only plan/step/tool events reach the user.
    assert not any(isinstance(e, TextEvent) for e in events)
    assert sum(isinstance(e, ToolEndEvent) for e in events) == 2


async def test_failed_step_is_retried_once():
    llm = FakeLLM(script=[LLMUnavailableError("blip"), "Recovered: 42."])
    state = task_state("Find the answer")

    events = await collect(PlanExecutor(llm, registry()).run(state, ToolContext()))

    step = state.steps[0]
    assert step.status == StepStatus.DONE
    assert step.attempts == 2
    running = [e for e in events if isinstance(e, StepEvent) and e.status == "running"]
    assert [e.attempt for e in running] == [1, 2]


async def test_step_failing_twice_is_marked_failed_and_plan_continues():
    llm = FakeLLM(
        script=[LLMUnavailableError("down"), LLMUnavailableError("still down"), "Step two worked."]
    )
    state = task_state("Broken step", "Working step")

    await collect(PlanExecutor(llm, registry()).run(state, ToolContext()))

    assert [s.status for s in state.steps] == [StepStatus.FAILED, StepStatus.DONE]
    assert "still down" in state.steps[0].error
    assert "NOT COMPLETED" in state.report()


async def test_step_that_never_finishes_hits_its_round_limit():
    endless = [[tool_call("calculator", expression=f"1 + {i}")] for i in range(20)]
    llm = FakeLLM(script=endless)
    limits = PlanLimits(max_attempts_per_step=2, tool_rounds_per_step=3)
    state = task_state("Loop forever")

    await collect(PlanExecutor(llm, registry(), limits=limits).run(state, ToolContext()))

    assert state.steps[0].status == StepStatus.FAILED
    assert len(llm.calls) == 6  # 2 attempts x 3 rounds - then it gives up


async def test_time_budget_skips_remaining_steps():
    llm = FakeLLM(script=["done"])
    limits = PlanLimits(max_seconds=-1)
    state = task_state("a", "b")

    await collect(PlanExecutor(llm, registry(), limits=limits).run(state, ToolContext()))

    assert [s.status for s in state.steps] == [StepStatus.SKIPPED, StepStatus.SKIPPED]


async def test_confirmation_stops_the_plan():
    memory, _ = make_memory()
    saved, _ = await memory.save_memory("The user likes tea.")
    llm = FakeLLM(script=[[tool_call("delete_memory", memory_id=saved.id)], "never used"])
    state = task_state("Delete the tea memory", "Tell the user")

    events = await collect(PlanExecutor(llm, registry(memory)).run(state, ToolContext()))

    assert isinstance(events[-1], ConfirmationEvent)
    assert state.steps[1].status == StepStatus.PENDING  # never started
    assert await memory.count() == 1


# ---------- the whole planned turn ----------


def planned_orchestrator(script, plan: str, final: str = "Singapore is 16.1 °C warmer."):
    llm = FakeLLM(reply=final, structured_reply=plan, script=script)
    return Orchestrator(llm, ConversationStore(), tools=registry()), llm


async def test_complex_request_is_planned_executed_and_summarized():
    orchestrator, llm = planned_orchestrator(
        script=["Singapore is 31.2 °C.", "London is 15.1 °C.",
                [tool_call("calculator", expression="31.2 - 15.1")], "Difference is 16.1 °C."],
        plan=plan_json("Get weather in Singapore", "Get weather in London",
                       "Calculate the temperature difference"),
    )  # fmt: skip

    events = await collect(
        orchestrator.events("s1", "What's the weather in Singapore and London, and the difference?")
    )

    plan = next(e for e in events if isinstance(e, PlanEvent))
    assert len(plan.steps) == 3
    done = [e.id for e in events if isinstance(e, StepEvent) and e.status == "done"]
    assert done == [1, 2, 3]
    answer = "".join(e.text for e in events if isinstance(e, TextEvent))
    assert answer == "Singapore is 16.1 °C warmer."
    # The final answer was written from the step results.
    synthesis_prompt = llm.calls[-1][-1].content
    assert (
        "Step 3 (Calculate the temperature difference): Difference is 16.1 °C." in synthesis_prompt
    )
    # Only the request and the final answer go into conversation history.
    history = orchestrator.history("s1")
    assert [m.content for m in history][1] == "Singapore is 16.1 °C warmer."


async def test_simple_request_skips_the_planner():
    orchestrator, llm = planned_orchestrator(script=["Hello!"], plan=plan_json("a", "b"))

    events = await collect(orchestrator.events("s1", "Hi Arthur"))

    assert not any(isinstance(e, PlanEvent) for e in events)
    assert all(len(call) > 0 for call in llm.calls)  # no planner prompt was sent
    assert not any("planning module" in call[0].content for call in llm.calls)


async def test_planning_can_be_disabled():
    llm = FakeLLM(script=["ok"], structured_reply=plan_json("a", "b"))
    orchestrator = Orchestrator(llm, ConversationStore(), tools=registry(), planning=False)

    events = await collect(
        orchestrator.events("s1", "Compare the weather in Paris and Rome and then summarize it")
    )

    assert not any(isinstance(e, PlanEvent) for e in events)


def test_websocket_streams_plan_and_step_events(ws_client, fake_llm):
    fake_llm.structured_reply = plan_json("Add 1 and 1", "Add 2 and 2")
    fake_llm.script = ["2", "4"]
    fake_llm.reply = "The results are 2 and 4."

    with ws_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "chat", "message": "Add 1 and 1, and then add 2 and 2 for me please"})
        events = []
        while not events or events[-1]["type"] not in ("done", "error"):
            events.append(ws.receive_json())

    types = [e["type"] for e in events]
    assert "plan" in types
    steps = [(e["id"], e["status"]) for e in events if e["type"] == "step"]
    assert (1, "done") in steps and (2, "done") in steps
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert answer == "The results are 2 and 4."
