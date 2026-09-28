"""The planner: turns a complex request into a short, ordered list of steps.

Two stages:
1. `looks_complex()` - a free, instant check (no LLM call) that filters out the
   ~90% of messages that are simple, so they don't pay for planning.
2. `Planner.make_plan()` - asks the LLM for a structured Plan (validated by
   Pydantic). A plan with a single step means "not really complex" and the
   normal agent loop handles it instead.
"""

import re

from pydantic import BaseModel, Field

from app.llm.base import LLMError, LLMProvider, Message, Role
from app.observability.logging import get_logger

log = get_logger(__name__)

MAX_PLAN_STEPS = 6

_MULTI_PART = re.compile(
    r"\b(?:and then|then|after that|afterwards|compare|comparison|difference|versus|vs\.?|"
    r"research|report|step by step|for each|each of|both|summari[sz]e|plan)\b",
    re.IGNORECASE,
)


def looks_complex(text: str) -> bool:
    """Cheap first filter: multi-part wording in a message of reasonable length."""
    if len(text.split()) < 8:
        return False
    signals = len(_MULTI_PART.findall(text))
    signals += text.lower().count(" and ") >= 2
    signals += text.count("?") >= 2
    return signals >= 1


class PlanStep(BaseModel):
    id: int
    task: str = Field(max_length=300, description="One small, self-contained sub-task")


class Plan(BaseModel):
    goal: str = Field(max_length=300)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)


PLANNER_PROMPT = """You are the planning module of ARTHUR, a personal AI assistant.
Break the user's request into a short list of ordered steps (at most {max_steps}).

Available tools:
{tools}

Rules:
- Each step is ONE small sub-task, usually one tool call (e.g. "Get the current weather in London").
- Each step must be self-contained: repeat names, places and numbers - don't say "the city above".
- Put steps in the order they must happen; a later step may use earlier results.
- Do NOT add a final "write the answer" or "summarize" step - that happens automatically.
- If the request is simple, return exactly ONE step.
- Only plan what the tools can do; don't invent tools.

Respond as JSON: {{"goal": "...", "steps": [{{"id": 1, "task": "..."}}, ...]}}"""


class Planner:
    def __init__(self, llm: LLMProvider, tool_descriptions: dict[str, str]) -> None:
        self.llm = llm
        self.tool_descriptions = tool_descriptions

    async def make_plan(self, user_text: str) -> Plan | None:
        """A plan with 2+ steps, or None if the request doesn't need one."""
        tools = "\n".join(f"- {name}: {desc}" for name, desc in self.tool_descriptions.items())
        prompt = PLANNER_PROMPT.format(max_steps=MAX_PLAN_STEPS, tools=tools)
        try:
            plan = await self.llm.generate_structured(
                [
                    Message(role=Role.SYSTEM, content=prompt),
                    Message(role=Role.USER, content=user_text),
                ],
                Plan,
            )
        except LLMError as exc:
            # Planning is an optimisation: without a plan, the normal loop still works.
            log.warning("planning_failed", error=str(exc))
            return None

        steps = [s for s in plan.steps if s.task.strip()][:MAX_PLAN_STEPS]
        if len(steps) < 2:
            log.info("plan_not_needed")
            return None
        # Renumber 1..n: models sometimes skip or repeat ids.
        plan = Plan(
            goal=plan.goal,
            steps=[PlanStep(id=i, task=s.task.strip()) for i, s in enumerate(steps, 1)],
        )
        log.info("plan_created", steps=len(plan.steps), goal=plan.goal)
        return plan
