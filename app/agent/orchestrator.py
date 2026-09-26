"""The orchestrator: ARTHUR's "manager".

For each user message it decides what to do:

    1. A confirmation is pending ("forget X?")  -> handle yes / no
    2. "Remember that ..."                       -> extract fact, save, confirm
    3. "Forget ..."                              -> find memory, ask to confirm
    4. Anything else                             -> recall relevant memories, ask the LLM

and assembles what the model sees:

    [system prompt + relevant long-term memories] + [recent conversation] + [new message]

Later phases add tool use and planning here, while the API layer stays unchanged.
"""

import time
from collections.abc import AsyncIterator

from app.agent.prompts import SYSTEM_PROMPT, memory_section
from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, Role
from app.memory import policy
from app.memory.manager import MemoryManager, MemorySearchResult
from app.memory.short_term import Conversation, ConversationStore, PendingAction
from app.observability.logging import get_logger
from app.utils.tokens import estimate_message_tokens

log = get_logger(__name__)


class Orchestrator:
    def __init__(
        self,
        llm: LLMProvider,
        conversations: ConversationStore,
        *,
        memory: MemoryManager | None = None,
        context_tokens: int = 8192,
        reply_reserve_tokens: int = 1024,
        max_history_messages: int = 40,
        memory_top_k: int = 5,
        memory_min_score: float = 0.55,
    ) -> None:
        self.llm = llm
        self.conversations = conversations
        self.memory = memory
        self.context_tokens = context_tokens
        self.reply_reserve_tokens = reply_reserve_tokens
        self.max_history_messages = max_history_messages
        self.memory_top_k = memory_top_k
        self.memory_min_score = memory_min_score

    # ---------- public API ----------

    async def respond(self, session_id: str, user_text: str) -> LLMResponse:
        conversation = self.conversations.get(session_id)
        start = time.perf_counter()
        prepared = await self._prepare(conversation, user_text)
        if isinstance(prepared, str):  # ARTHUR answered directly (memory action)
            conversation.add_exchange(user_text, prepared)
            latency = round((time.perf_counter() - start) * 1000, 1)
            return LLMResponse(content=prepared, model=self.llm.model, latency_ms=latency)
        result = await self.llm.generate(prepared)
        conversation.add_exchange(user_text, result.content)
        return result

    async def stream(self, session_id: str, user_text: str) -> AsyncIterator[str]:
        conversation = self.conversations.get(session_id)
        parts: list[str] = []
        try:
            prepared = await self._prepare(conversation, user_text)
            if isinstance(prepared, str):
                parts.append(prepared)
                yield prepared
                return
            async for token in self.llm.stream(prepared):
                parts.append(token)
                yield token
        finally:
            # Also runs when the user presses stop: keep what ARTHUR already said,
            # so the next turn ("continue") has the context. Nothing is saved if
            # the model failed before producing any text.
            if parts:
                conversation.add_exchange(user_text, "".join(parts).strip())

    def build_messages(
        self,
        conversation: Conversation,
        user_text: str,
        memories: list[MemorySearchResult] | None = None,
    ) -> list[Message]:
        system_text = SYSTEM_PROMPT + memory_section([m.memory.content for m in memories or []])
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

    async def _prepare(self, conversation: Conversation, user_text: str) -> str | list[Message]:
        """Return either a direct reply (str) or the messages to send to the LLM."""
        if conversation.pending:
            reply = await self._resolve_pending(conversation, user_text)
            if reply is not None:
                return reply

        if self.memory is None:
            return self.build_messages(conversation, user_text)

        match policy.detect_intent(user_text):
            case policy.MemoryIntent.REMEMBER:
                return await self._remember(user_text)
            case policy.MemoryIntent.FORGET:
                return await self._forget(conversation, user_text)

        return self.build_messages(conversation, user_text, await self._recall(user_text))

    async def _recall(self, user_text: str) -> list[MemorySearchResult]:
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
        if policy.is_yes(user_text) and pending.kind == "delete_memory" and self.memory:
            await self.memory.delete_memory(pending.target_id)
            return f"Done. I've forgotten: *{pending.description}*"
        if policy.is_no(user_text):
            return "Okay, I'll keep it."
        return None  # user moved on; the pending action is cancelled
