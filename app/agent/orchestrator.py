"""The orchestrator: ARTHUR's "manager".

For each user message it decides what to do:

    1. A confirmation is pending ("forget X?", "run tool Y?")  -> handle yes / no
    2. "Remember that ..."                                      -> extract fact, save, confirm
    3. "Forget ..."                                             -> find memory, ask to confirm
    4. Anything else  -> recall relevant memories, then the agent loop:
                         the LLM answers directly or uses tools until it can answer

and assembles what the model sees:

    [system prompt + relevant long-term memories] + [recent conversation] + [new message]

Everything is reported as a stream of AgentEvents (text, tool start/end,
confirmation) so the UI can show what ARTHUR is doing.
"""

import asyncio
import re
import time
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from app.agent.executor import AgentLimits, PlanExecutor, PlanLimits, ToolLoop
from app.agent.planner import Plan, Planner, looks_complex
from app.agent.prompts import (
    SYNTHESIS_PROMPT,
    SYSTEM_PROMPT,
    document_section,
    memory_section,
)
from app.agent.state import (
    AgentEvent,
    ConfirmationEvent,
    StepState,
    TaskState,
    TextEvent,
    ToolEndEvent,
)
from app.agent.verification import (
    CORRECTION,
    FAKE_PERMISSION_NOTE,
    claims_action,
    fakes_permission_request,
)
from app.llm.base import LLMError, LLMProvider, Message, Role
from app.memory import policy
from app.memory.manager import MemoryManager, MemorySearchResult
from app.memory.short_term import Conversation, ConversationStore, PendingAction
from app.observability.logging import get_logger
from app.rag.retrieval import DocumentHit, DocumentRetriever
from app.tools.base import PermissionLevel, ToolContext
from app.tools.registry import ToolRegistry
from app.utils.tokens import estimate_message_tokens

log = get_logger(__name__)

# Tools the agent may choose by itself. save_memory is deliberately missing:
# the memory policy says only an explicit "remember ..." from the user saves.
AGENT_TOOLS = frozenset(
    {
        "calculator",
        "current_time",
        "weather",
        "search_memory",
        "delete_memory",
        "document_search",
        "list_documents",
        "web_search",
        "read_webpage",
        "find_files",
        "list_folder",
        "read_file",
        "save_file",
        "browser_open",
        "browser_find_text",
        "browser_click",
        "browser_type",
        "open_app",
        "read_window",
        "click_control",
        "type_text",
        "press_key",
        "describe_image",
        "look_at_screen",
        "set_reminder",
        "list_reminders",
        "cancel_reminder",
    }
)


class AgentReply(BaseModel):
    """The complete result of one turn (used by POST /chat)."""

    content: str
    model: str
    latency_ms: float
    tools_used: list[ToolEndEvent] = Field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        llm: LLMProvider,
        conversations: ConversationStore,
        *,
        memory: MemoryManager | None = None,
        tools: ToolRegistry | None = None,
        agent_tools: frozenset[str] = AGENT_TOOLS,
        agent_limits: AgentLimits | None = None,
        planning: bool = True,
        plan_limits: PlanLimits | None = None,
        context_tokens: int = 8192,
        reply_reserve_tokens: int = 1024,
        max_history_messages: int = 40,
        memory_top_k: int = 5,
        memory_min_score: float = 0.55,
        retriever: DocumentRetriever | None = None,
        rag_top_k: int = 5,
        rag_min_score: float = 0.58,
    ) -> None:
        self.llm = llm
        self.conversations = conversations
        self.memory = memory
        self.tools = tools
        self.retriever = retriever
        self.rag_top_k = rag_top_k
        self.rag_min_score = rag_min_score
        self.loop = (
            ToolLoop(llm, tools, tool_names=set(agent_tools), limits=agent_limits)
            if tools
            else None
        )
        self.planner: Planner | None = None
        if tools and planning:
            self.planner = Planner(
                llm,
                {t.name: t.description for t in tools.all() if t.name in agent_tools},
            )
            self.plan_executor = PlanExecutor(
                llm, tools, tool_names=set(agent_tools), limits=plan_limits
            )
        self.context_tokens = context_tokens
        self.reply_reserve_tokens = reply_reserve_tokens
        self.max_history_messages = max_history_messages
        self.memory_top_k = memory_top_k
        self.memory_min_score = memory_min_score

    # ---------- public API ----------

    async def events(
        self, session_id: str, user_text: str, request_id: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Handle one user message, reporting progress as events."""
        conversation = self.conversations.get(session_id)
        parts: list[str] = []
        try:
            async for event in self._turn(conversation, user_text, request_id):
                if isinstance(event, TextEvent):
                    parts.append(event.text)
                yield event
        finally:
            # Also runs when the user presses stop: keep what ARTHUR already said,
            # so the next turn ("continue") has the context. Nothing is saved if
            # the model failed before producing any text.
            if parts:
                conversation.add_exchange(user_text, _for_history("".join(parts).strip()))

    async def respond(
        self, session_id: str, user_text: str, request_id: str | None = None
    ) -> AgentReply:
        """Handle one user message and return the complete reply."""
        start = time.perf_counter()
        parts: list[str] = []
        tools_used: list[ToolEndEvent] = []
        async for event in self.events(session_id, user_text, request_id):
            if isinstance(event, TextEvent):
                parts.append(event.text)
            elif isinstance(event, ToolEndEvent):
                tools_used.append(event)
        return AgentReply(
            content="".join(parts).strip(),
            model=self.llm.model,
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            tools_used=tools_used,
        )

    def build_messages(
        self,
        conversation: Conversation,
        user_text: str,
        memories: list[MemorySearchResult] | None = None,
        passages: list[DocumentHit] | None = None,
    ) -> list[Message]:
        system_text = SYSTEM_PROMPT + memory_section([m.memory.content for m in memories or []])
        if passages is not None:  # None = the user has no documents at all
            system_text += document_section([(p.citation, p.text) for p in passages])
        system = Message(role=Role.SYSTEM, content=system_text)
        user = Message(role=Role.USER, content=user_text)
        # Budget for history = window - room for the reply - the fixed parts.
        budget = (
            self.context_tokens
            - self.reply_reserve_tokens
            - estimate_message_tokens(system)
            - estimate_message_tokens(user)
        )
        history = conversation.recent(max(budget, 0), self.max_history_messages)
        dropped = len(conversation.messages) - len(history)
        if dropped > 0:
            log.info("history_trimmed", kept=len(history), dropped=dropped)
        return [system, *history, user]

    def history(self, session_id: str) -> list[Message]:
        if session_id not in self.conversations:
            return []
        return list(self.conversations.get(session_id).messages)

    def clear(self, session_id: str) -> None:
        self.conversations.clear(session_id)

    # ---------- decision making ----------

    async def _turn(
        self, conversation: Conversation, user_text: str, request_id: str | None
    ) -> AsyncIterator[AgentEvent]:
        prepared = await self._prepare(conversation, user_text)
        if isinstance(prepared, str):  # ARTHUR answers directly (memory action, yes/no)
            yield TextEvent(text=prepared)
            return

        if self.loop is None:  # no tools configured: plain chat
            async for token in self.llm.stream(prepared):
                yield TextEvent(text=token)
            return

        context = ToolContext(request_id=request_id, session_id=conversation.session_id)
        work = self.loop.run(prepared, context)
        if self.planner and looks_complex(user_text):
            plan = await self.planner.make_plan(user_text)
            if plan:
                work = self._run_plan(plan, prepared, user_text, context)
        async for event in self._supervise(work, conversation):
            yield event

    async def _run_plan(
        self, plan: Plan, prepared: list[Message], user_text: str, context: ToolContext
    ) -> AsyncIterator[AgentEvent]:
        """Execute the plan, then write one answer from the step results."""
        state = TaskState(
            goal=plan.goal, steps=[StepState(id=s.id, task=s.task) for s in plan.steps]
        )
        async for event in self.plan_executor.run(state, context):
            yield event
            if isinstance(event, ConfirmationEvent):
                return  # the user decides first; no summary yet

        # prepared = [system (+memories), history..., user]; swap the last message
        # for the original request plus what the steps found.
        synthesis = Message(
            role=Role.USER,
            content=SYNTHESIS_PROMPT.format(request=user_text, report=state.report()),
        )
        async for token in self.llm.stream([*prepared[:-1], synthesis]):
            yield TextEvent(text=token)

    async def _supervise(
        self, work: AsyncIterator[AgentEvent], conversation: Conversation
    ) -> AsyncIterator[AgentEvent]:
        """Pass events through, handling confirmations and checking honesty."""
        answer: list[str] = []
        acted = False  # did a state-changing tool (level >= 1) actually succeed?
        asked = False  # did the real permission system ask the user?
        async for event in work:
            yield event
            if isinstance(event, TextEvent):
                answer.append(event.text)
            elif isinstance(event, ToolEndEvent) and event.status == "ok":
                tool = self.tools.get(event.name)
                acted = acted or bool(tool and tool.permission_level >= PermissionLevel.LOW_RISK)
            elif isinstance(event, ConfirmationEvent):
                acted = True  # the system is asking the user; nothing is being claimed
                asked = True
                conversation.pending = PendingAction(
                    kind="tool_call",
                    target_id=event.name,
                    description=event.preview,
                    payload={"arguments": event.arguments},
                )
                yield TextEvent(
                    text=f"I need your permission first: {_show_preview(event.preview)}\n\n"
                    "Reply **yes** to go ahead or **no** to cancel."
                )

        text = "".join(answer)
        if not asked and fakes_permission_request(text):
            log.warning("fake_permission_request")
            yield TextEvent(text=FAKE_PERMISSION_NOTE)
        elif not acted and claims_action(text):
            log.warning("hallucinated_action_claim")
            yield TextEvent(text=CORRECTION)

    async def _prepare(self, conversation: Conversation, user_text: str) -> str | list[Message]:
        """Return either a direct reply (str) or the messages to send to the LLM."""
        if conversation.pending:
            reply = await self._resolve_pending(conversation, user_text)
            if reply is not None:
                return reply

        if self.memory is not None:
            match policy.detect_intent(user_text):
                case policy.MemoryIntent.REMEMBER:
                    return await self._remember(user_text)
                case policy.MemoryIntent.FORGET:
                    return await self._forget(conversation, user_text)

        memories, passages = await asyncio.gather(
            self._recall(user_text), self._recall_documents(user_text)
        )
        return self.build_messages(conversation, user_text, memories, passages)

    async def _recall_documents(self, user_text: str) -> list[DocumentHit] | None:
        """Automatic RAG: relevant passages from the user's documents (None = no documents)."""
        if self.retriever is None or self.retriever.vectors.count() == 0:
            return None
        try:
            return await self.retriever.search(
                user_text, k=self.rag_top_k, min_score=self.rag_min_score
            )
        except LLMError as exc:
            log.warning("document_recall_failed", error=str(exc))
            return None

    async def _recall(self, user_text: str) -> list[MemorySearchResult]:
        if self.memory is None:
            return []
        try:
            return await self.memory.search_memory(
                user_text, k=self.memory_top_k, min_score=self.memory_min_score
            )
        except LLMError as exc:
            # Memory is helpful, not essential: answer without it rather than fail.
            log.warning("memory_recall_failed", error=str(exc))
            return []

    async def _remember(self, user_text: str) -> str:
        if policy.contains_sensitive_data(user_text):
            log.info("memory_refused_sensitive")
            return (
                "I won't store that - it looks like a password, PIN, card/bank number or "
                "another secret. Please keep those in a password manager instead."
            )

        try:
            draft = await self.llm.generate_structured(
                [
                    Message(role=Role.SYSTEM, content=policy.EXTRACTION_PROMPT),
                    Message(role=Role.USER, content=user_text),
                ],
                policy.MemoryDraft,
            )
            fact, category = draft.fact.strip(), draft.category
        except LLMError as exc:
            log.warning("memory_extraction_failed", error=str(exc))
            fact, category = policy.remember_fallback(user_text), "other"

        if not fact or policy.contains_sensitive_data(fact):
            return "I couldn't find something safe to remember in that message."

        try:
            memory, created = await self.memory.save_memory(fact, category, source="chat")
        except (LLMError, ValueError) as exc:
            log.warning("memory_save_failed", error=str(exc))
            return f"Sorry, I couldn't save that right now ({exc})."

        if created:
            return f"Got it. I'll remember that: *{memory.content}*"
        return f"I already knew something like that, so I updated it: *{memory.content}*"

    async def _forget(self, conversation: Conversation, user_text: str) -> str:
        try:
            matches = await self.memory.search_memory(
                policy.forget_query(user_text), k=1, min_score=self.memory_min_score
            )
        except LLMError as exc:
            return f"Sorry, I can't search my memory right now ({exc})."
        if not matches:
            return (
                "I couldn't find a memory matching that. "
                "Open the Memory panel to see everything I remember."
            )
        target = matches[0].memory
        conversation.pending = PendingAction(
            kind="delete_memory", target_id=target.id, description=target.content
        )
        return (
            f"Should I forget this memory: *{target.content}*\n\n"
            "Reply **yes** to confirm or **no** to keep it."
        )

    async def _resolve_pending(self, conversation: Conversation, user_text: str) -> str | None:
        """Handle a reply to a confirmation question. None = not a yes/no; carry on normally."""
        pending, conversation.pending = conversation.pending, None
        if policy.is_yes(user_text):
            if pending.kind == "delete_memory" and self.memory:
                await self.memory.delete_memory(pending.target_id)
                return f"Done. I've forgotten: *{pending.description}*"
            if pending.kind == "tool_call" and self.tools:
                result = await self.tools.execute(
                    pending.target_id,
                    pending.payload.get("arguments", {}),
                    ToolContext(session_id=conversation.session_id, confirmed=True),
                )
                if result.ok:
                    return f"Done: {pending.description.splitlines()[0].rstrip(':.')}."
                return f"That didn't work: {result.error}"
        if policy.is_no(user_text):
            return (
                "Okay, I won't do that." if pending.kind == "tool_call" else "Okay, I'll keep it."
            )
        return None  # user moved on; the pending action is cancelled


def _show_preview(preview: str) -> str:
    """First line in bold; more lines (e.g. the text to be typed) as a quoted block."""
    head, _, rest = preview.partition("\n")
    shown = f"**{head.strip()}**"
    if rest.strip():
        shown += "\n\n" + "\n".join(f"> {line}" for line in rest.strip().splitlines())
    return shown


_PERMISSION_QUESTION = re.compile(
    r"I need your permission first: (.*?)\n\nReply \*\*yes\*\* to go ahead or \*\*no\*\* to "
    r"cancel\.",
    re.DOTALL,
)


def _for_history(reply: str) -> str:
    """What the model sees of this reply in later turns.

    Permission questions are stored as a plain note: the small model copied the exact
    "I need your permission... Reply yes" wording instead of calling the tool (seen live).
    """
    return _PERMISSION_QUESTION.sub(
        lambda m: f"(ARTHUR's safety system asked the user to approve: {m.group(1).strip()})",
        reply,
    )
