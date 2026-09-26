"""The orchestrator: ARTHUR's "manager".

For now it does one job - assemble what the model sees for each turn:

    [system prompt] + [recent conversation that fits] + [new user message]

and save the exchange afterwards. Later phases add memory lookup, tool use
and planning here, while the API layer stays unchanged.
"""

from collections.abc import AsyncIterator

from app.agent.prompts import SYSTEM_PROMPT
from app.llm.base import LLMProvider, LLMResponse, Message, Role
from app.memory.short_term import Conversation, ConversationStore
from app.observability.logging import get_logger
from app.utils.tokens import estimate_message_tokens

log = get_logger(__name__)


class Orchestrator:
    def __init__(
        self,
        llm: LLMProvider,
        conversations: ConversationStore,
        *,
        context_tokens: int = 8192,
        reply_reserve_tokens: int = 1024,
        max_history_messages: int = 40,
    ) -> None:
        self.llm = llm
        self.conversations = conversations
        self.context_tokens = context_tokens
        self.reply_reserve_tokens = reply_reserve_tokens
        self.max_history_messages = max_history_messages

    def build_messages(self, conversation: Conversation, user_text: str) -> list[Message]:
        system = Message(role=Role.SYSTEM, content=SYSTEM_PROMPT)
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

    async def respond(self, session_id: str, user_text: str) -> LLMResponse:
        conversation = self.conversations.get(session_id)
        result = await self.llm.generate(self.build_messages(conversation, user_text))
        conversation.add_exchange(user_text, result.content)
        return result

    async def stream(self, session_id: str, user_text: str) -> AsyncIterator[str]:
        conversation = self.conversations.get(session_id)
        parts: list[str] = []
        try:
            async for token in self.llm.stream(self.build_messages(conversation, user_text)):
                parts.append(token)
                yield token
        finally:
            # Also runs when the user presses stop: keep what ARTHUR already said,
            # so the next turn ("continue") has the context. Nothing is saved if
            # the model failed before producing any text.
            if parts:
                conversation.add_exchange(user_text, "".join(parts).strip())

    def history(self, session_id: str) -> list[Message]:
        if session_id not in self.conversations:
            return []
        return list(self.conversations.get(session_id).messages)

    def clear(self, session_id: str) -> None:
        self.conversations.clear(session_id)
