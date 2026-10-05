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
import json
import re
import time
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from app.agent.attachments import (
    MAX_DOCUMENT_CHARS,
    VISION_QUESTION,
    Attachment,
    attachment_section,
    history_note,
)
from app.agent.executor import AgentLimits, PlanExecutor, PlanLimits, ToolLoop
from app.agent.planner import Plan, Planner, looks_complex
from app.agent.prompts import (
    MAX_SUMMARY_CHARS,
    SUMMARY_PROMPT,
    SYNTHESIS_PROMPT,
    SYSTEM_PROMPT,
    context_block,
    summary_note,
    system_prompt,
)
from app.agent.state import (
    AgentEvent,
    ConfirmationEvent,
    StepState,
    TaskState,
    TextEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from app.agent.verification import (
    CORRECTION,
    FAKE_PERMISSION_NOTE,
    claims_action,
    fakes_permission_request,
    source_note,
    unverified_sources,
)
from app.llm.base import LLMError, LLMProvider, Message, Role
from app.memory import policy
from app.memory.manager import MemoryManager, MemorySearchResult
from app.memory.short_term import Conversation, ConversationStore, PendingAction
from app.observability.logging import get_logger
from app.observability.metrics import Metrics
from app.rag.retrieval import DocumentHit, DocumentRetriever
from app.tools.base import PermissionLevel, ToolContext
from app.tools.registry import ToolRegistry
from app.utils.tokens import estimate_message_tokens, estimate_tokens
from app.vision.provider import VisionProvider

log = get_logger(__name__)

# Tools the agent may choose by itself. save_memory is deliberately missing:
# the memory policy says only an explicit "remember ..." from the user saves.
# A confirmation question is only valid for this long. After that "yes" is refused.
PENDING_MAX_AGE_SECONDS = 300

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


# Words that ask for an action - then a plan keeps its confirmation-level tools.
ACTION_WORDS = re.compile(
    r"\b(save|store|write|export|delete|remove|forget|cancel|send|type|press|click|"
    r"open|create|make a file)\b",
    re.IGNORECASE,
)

STORAGE_TROUBLE = (
    "Sorry, my memory store isn't working right now, so I can't do that. Everything else "
    "still works - the details are in ARTHUR's log."
)

EMPTY_ANSWER = (
    "I couldn't produce an answer to that. It may need something that isn't available "
    "in this installation - please try asking in a different way."
)


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
        metrics: Metrics | None = None,
        vision: VisionProvider | None = None,
        summary_delay_seconds: float = 15.0,
    ) -> None:
        self.llm = llm
        self.vision = vision
        self.metrics = metrics
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
        # The tool descriptions travel with EVERY request and take room in the context
        # window (about 3,000 tokens for 27 tools) - they must be part of the budget.
        self.tools_tokens = (
            estimate_tokens(json.dumps(tools.llm_schemas(set(agent_tools)))) if tools else 0
        )
        # Fixed for the life of the process (prompt cache); names what this install lacks.
        self.system_prompt = (
            system_prompt({t.name for t in tools.all()} & agent_tools) if tools else SYSTEM_PROMPT
        )
        self.context_tokens = context_tokens
        self.reply_reserve_tokens = reply_reserve_tokens
        self.max_history_messages = max_history_messages
        self.memory_top_k = memory_top_k
        self.memory_min_score = memory_min_score
        self._background: set[asyncio.Task] = set()  # running summaries
        self._compressions: dict[str, asyncio.Task] = {}  # session id -> waiting summary
        self.summary_delay_seconds = summary_delay_seconds

    # ---------- public API ----------

    async def events(
        self,
        session_id: str,
        user_text: str,
        request_id: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Handle one user message (with any attached files), reporting progress as events."""
        conversation = self.conversations.get(session_id)
        self._cancel_compression(session_id)  # the user is active: no summary right now
        parts: list[str] = []
        seen: dict[str, str | None] = {}  # attachment id -> what the vision model saw
        try:
            async for event in self._turn(
                conversation, user_text, request_id, attachments or [], seen
            ):
                if isinstance(event, TextEvent):
                    parts.append(event.text)
                yield event
        finally:
            # Also runs when the user presses stop: keep what ARTHUR already said,
            # so the next turn ("continue") has the context. Nothing is saved if
            # the model failed before producing any text.
            if parts:
                asked = user_text + history_note(attachments or [], seen)
                conversation.add_exchange(asked, _for_history("".join(parts).strip()))
                self._compress_later(conversation)

    async def respond(
        self,
        session_id: str,
        user_text: str,
        request_id: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> AgentReply:
        """Handle one user message and return the complete reply."""
        start = time.perf_counter()
        parts: list[str] = []
        tools_used: list[ToolEndEvent] = []
        async for event in self.events(session_id, user_text, request_id, attachments):
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
        attachments: list[str] | None = None,
    ) -> list[Message]:
        """The prompt, ordered so that as much as possible stays identical between turns:

            system text (never changes) -> [tool descriptions] -> history -> this message

        The model re-reads only what differs from the previous request, so everything that
        changes per message - memories, document passages - rides with the newest message
        at the very end (see prompts.context_block).
        """
        system = Message(role=Role.SYSTEM, content=self.system_prompt)
        context = context_block(
            policy.prioritize(memories or []),
            # None = the user has no documents at all
            None if passages is None else [(p.citation, p.text) for p in passages],
            attachments,
        )
        user = Message(role=Role.USER, content=context + user_text)
        # Phase 27: what scrolled out of the window, compressed into short notes.
        notes = (
            [Message(role=Role.SYSTEM, content=summary_note(conversation.summary))]
            if conversation.summary
            else []
        )
        # Budget for history = window - room for the reply - everything else in the prompt.
        budget = (
            self.context_tokens
            - self.reply_reserve_tokens
            - estimate_message_tokens(system)
            - sum(map(estimate_message_tokens, notes))
            - self.tools_tokens
            - estimate_message_tokens(user)
        )
        history = conversation.window(max(budget, 0), self.max_history_messages)
        dropped = len(conversation.messages) - len(history)
        if dropped > 0:
            log.info("history_trimmed", kept=len(history), dropped=dropped)
        return [system, *notes, *history, user]

    async def warm_up(self) -> None:
        """Get the models ready before the first message (run in the background at start).

        Measured without it: the first message waited ~11 s - loading the chat model into
        the GPU (~7 s), loading the embedding model (~2 s) and reading the ~4,000-token
        prompt (~1.2 s). This sends one tiny request with the REAL system prompt and tool
        descriptions, so the models are loaded and that prompt is already cached.
        Failures are only logged: ARTHUR must start even when Ollama isn't running yet.
        """
        started = time.perf_counter()
        try:
            messages = [
                Message(role=Role.SYSTEM, content=self.system_prompt),
                Message(role=Role.USER, content="Reply with the single word: ready"),
            ]
            tools = self.loop.registry.llm_schemas(self.loop.tool_names) if self.loop else None
            async for _ in self.llm.stream_chat(messages, tools=tools):
                pass
            # The small embedding model AFTER the big chat model: loaded first, it was
            # pushed out of the GPU again when the chat model arrived (measured).
            if self.memory is not None:
                await self.memory.embeddings.embed_query("warm up")
        except Exception as exc:
            log.warning("model_warm_up_failed", error=str(exc)[:200])
            return
        log.info("models_warmed_up", seconds=round(time.perf_counter() - started, 1))

    def _compress_later(self, conversation: Conversation) -> None:
        """Older messages left the window: summarise them - once the user pauses.

        Without this, a long conversation simply forgets its beginning (Phase 24 note).
        Measured: writing the summary right after an answer made the NEXT two answers 2-4 s
        slower each - the summary occupied the GPU, and its different prompt pushed the
        chat prompt out of Ollama's cache. So it waits for a pause (`summary_delay_seconds`
        without a new message); a new message cancels the wait and it is tried again later.
        """
        if conversation.window_start <= conversation.summarized_until:
            return
        self._cancel_compression(conversation.session_id)
        task = asyncio.create_task(self._compress_after_pause(conversation))
        self._compressions[conversation.session_id] = task
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _cancel_compression(self, session_id: str) -> None:
        task = self._compressions.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _compress_after_pause(self, conversation: Conversation) -> None:
        await asyncio.sleep(self.summary_delay_seconds)
        until = conversation.window_start
        dropped = conversation.messages[conversation.summarized_until : until]
        if not dropped:
            return
        transcript = "\n".join(
            f"{'User' if m.role == Role.USER else 'ARTHUR'}: {m.content[:600]}" for m in dropped
        )
        prompt = SUMMARY_PROMPT.format(summary=conversation.summary or "-", transcript=transcript)
        started = time.perf_counter()
        try:
            reply = await self.llm.generate([Message(role=Role.USER, content=prompt)])
            text = reply.content.strip()
            if text:
                conversation.summary = text[:MAX_SUMMARY_CHARS]
                conversation.summarized_until = until
                log.info(
                    "history_compressed",
                    messages=len(dropped),
                    chars=len(conversation.summary),
                    ms=round((time.perf_counter() - started) * 1000),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # notes are a bonus: never break the conversation over them
            log.warning("history_compression_failed", error=str(exc)[:200])

    async def idle(self) -> None:
        """Wait for background work (summaries) - for tests and shutdown."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    def stop_background(self) -> None:
        """Cancel waiting summaries (at shutdown)."""
        for task in list(self._background):
            task.cancel()

    def history(self, session_id: str) -> list[Message]:
        if session_id not in self.conversations:
            return []
        return list(self.conversations.get(session_id).messages)

    def clear(self, session_id: str) -> None:
        self.conversations.clear(session_id)

    # ---------- decision making ----------

    async def _turn(
        self,
        conversation: Conversation,
        user_text: str,
        request_id: str | None,
        attachments: list[Attachment],
        seen: dict[str, str | None],
    ) -> AsyncIterator[AgentEvent]:
        sections: list[str] = []
        budget = MAX_DOCUMENT_CHARS
        for attachment in attachments:
            if attachment.kind == "image":
                async for event in self._look_at(attachment, user_text, seen):
                    yield event
            section = attachment_section(attachment, seen.get(attachment.id), budget)
            if attachment.kind == "document":
                budget = max(budget - len(section), 600)
            sections.append(section)

        prepared = await self._prepare(conversation, user_text, sections)
        if isinstance(prepared, str):  # ARTHUR answers directly (memory action, yes/no)
            yield TextEvent(text=prepared)
            return

        if self.loop is None:  # no tools configured: plain chat
            async for token in self.llm.stream(prepared):
                yield TextEvent(text=token)
            return

        context = ToolContext(request_id=request_id, session_id=conversation.session_id)
        work = self.loop.run(prepared, context)
        # A plan's steps would not see the attached files: answer them in one go.
        if self.planner and not attachments and looks_complex(user_text):
            started = time.perf_counter()
            plan = await self.planner.make_plan(user_text)
            if self.metrics is not None:  # an extra model call before the answer starts
                self.metrics.chat_stage.labels("plan").observe(time.perf_counter() - started)
            if plan:
                work = self._run_plan(plan, prepared, user_text, context)
        async for event in self._supervise(work, conversation, prepared, context):
            yield event

    async def _look_at(
        self, attachment: Attachment, user_text: str, seen: dict[str, str | None]
    ) -> AsyncIterator[AgentEvent]:
        """Show an attached picture to the vision model, with the user's question.

        Reported like a tool call, so the page shows "describe_image" while it works
        (the first time ~10-30 s: the two models take turns in the GPU).
        """
        call_id = f"attachment-{attachment.id[:8]}"
        yield ToolStartEvent(
            call_id=call_id, name="describe_image", arguments={"picture": attachment.name}
        )
        started = time.perf_counter()
        description, status, summary = None, "error", "vision is switched off"
        if self.vision is not None and attachment.image is not None:
            try:
                description = await self.vision.describe(
                    attachment.image, VISION_QUESTION.format(question=user_text[:500])
                )
                status, summary = "ok", description[:120]
            except Exception as exc:  # a broken picture must not end the conversation
                log.warning("attachment_vision_failed", error=str(exc)[:200])
                summary = "the vision model could not look at it"
        seen[attachment.id] = description
        yield ToolEndEvent(
            call_id=call_id,
            name="describe_image",
            status=status,
            summary=summary,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    def _actions_not_asked_for(self, user_text: str) -> frozenset[str]:
        """Tools a plan may NOT use: the ones that need confirmation, unless asked for.

        Found in the Phase 29 demo: "search the web for matching roles and prepare a
        comparison" became a plan whose last step tried to SAVE a report nobody asked to
        save (the confirmation stopped it, but the comparison never reached the user).
        Plans gather information; saving, deleting, typing... happen when the user asks.
        """
        if ACTION_WORDS.search(user_text) or self.tools is None:
            return frozenset()
        return frozenset(
            t.name for t in self.tools.all() if t.permission_level >= PermissionLevel.CONFIRM
        )

    async def _run_plan(
        self, plan: Plan, prepared: list[Message], user_text: str, context: ToolContext
    ) -> AsyncIterator[AgentEvent]:
        """Execute the plan, then write one answer from the step results."""
        state = TaskState(
            goal=plan.goal, steps=[StepState(id=s.id, task=s.task) for s in plan.steps]
        )
        async for event in self.plan_executor.run(
            state, context, exclude=self._actions_not_asked_for(user_text)
        ):
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
        self,
        work: AsyncIterator[AgentEvent],
        conversation: Conversation,
        prepared: list[Message] | None = None,
        context: ToolContext | None = None,
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
        if not asked and not text.strip():
            # The model sent neither words nor a tool call. Silence looks like a crash.
            log.warning("empty_answer")
            yield TextEvent(text=EMPTY_ANSWER)
        elif not asked and fakes_permission_request(text):
            log.warning("fake_permission_request")
            yield TextEvent(text=FAKE_PERMISSION_NOTE)
        elif not acted and claims_action(text):
            log.warning("hallucinated_action_claim")
            yield TextEvent(text=CORRECTION)
        if prepared is not None and not asked:
            seen = "\n".join(m.content for m in prepared if m.role != Role.SYSTEM)
            missing = unverified_sources(
                text, seen + "\n" + "\n".join(context.sources if context else [])
            )
            if missing:
                log.warning("unverified_sources", count=len(missing))
                yield TextEvent(text=source_note(missing))

    async def _prepare(
        self, conversation: Conversation, user_text: str, attachments: list[str] | None = None
    ) -> str | list[Message]:
        """Return either a direct reply (str) or the messages to send to the LLM."""
        if conversation.pending:
            reply = await self._resolve_pending(conversation, user_text)
            if reply is not None:
                return reply

        if self.memory is not None and not attachments:
            match policy.detect_intent(user_text):
                case policy.MemoryIntent.REMEMBER:
                    return await self._remember(user_text)
                case policy.MemoryIntent.FORGET:
                    return await self._forget(conversation, user_text)

        started = time.perf_counter()
        # An attached document is already in the message: no automatic passages on top.
        with_document = any("Attached document" in s for s in attachments or [])
        memories, passages = await asyncio.gather(
            self._recall(user_text),
            self._no_passages() if with_document else self._recall_documents(user_text),
        )
        if self.metrics is not None:
            self.metrics.chat_stage.labels("recall").observe(time.perf_counter() - started)
        return self.build_messages(conversation, user_text, memories, passages, attachments)

    async def _no_passages(self) -> None:
        return None

    async def _recall_documents(self, user_text: str) -> list[DocumentHit] | None:
        """Automatic RAG: relevant passages from the user's documents (None = no documents)."""
        if self.retriever is None or self.retriever.vectors.count() == 0:
            return None
        try:
            return await self.retriever.search(
                user_text, k=self.rag_top_k, min_score=self.rag_min_score
            )
        except Exception as exc:  # embeddings down, vector store broken: answer without them
            log.warning("document_recall_failed", error=str(exc)[:200])
            return None

    async def _recall(self, user_text: str) -> list[MemorySearchResult]:
        if self.memory is None:
            return []
        try:
            return await self.memory.search_memory(
                user_text, k=self.memory_top_k, min_score=self.memory_min_score
            )
        except Exception as exc:
            # Memory is helpful, not essential: answer without it rather than fail - also
            # when the database itself is broken (Phase 28).
            log.warning("memory_recall_failed", error=str(exc)[:200])
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
        except Exception as exc:  # database trouble: say so, without internals
            log.warning("memory_save_failed", error=str(exc)[:200])
            return STORAGE_TROUBLE

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
        except Exception as exc:
            log.warning("memory_forget_failed", error=str(exc)[:200])
            return STORAGE_TROUBLE
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
        if policy.is_yes(user_text) and pending.expired(PENDING_MAX_AGE_SECONDS):
            # A "yes" long after the question might be meant for something else entirely.
            return "That request has expired (I asked a while ago). Please ask me again."
        if policy.is_yes(user_text):
            if pending.kind == "delete_memory" and self.memory:
                try:
                    await self.memory.delete_memory(pending.target_id)
                except Exception as exc:
                    log.warning("memory_delete_failed", error=str(exc)[:200])
                    return STORAGE_TROUBLE
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
