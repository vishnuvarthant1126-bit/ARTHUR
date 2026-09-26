"""Short-term memory: the running conversation, per browser session.

LLMs are *stateless*: each request is answered from scratch, using only the
messages sent with it. "Memory" in a chat is therefore just us re-sending
the previous messages every time. This module stores them and picks the
most recent ones that fit in the model's context window.

Stored in RAM: fast, private, and gone when the server restarts. Durable
long-term memory comes in Phase 5.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass, field

from app.llm.base import Message, Role
from app.utils.tokens import estimate_message_tokens


@dataclass
class PendingAction:
    """An action waiting for the user's "yes" (e.g. forgetting a memory)."""

    kind: str
    target_id: str
    description: str


@dataclass
class Conversation:
    session_id: str
    max_stored_messages: int = 200
    messages: list[Message] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    pending: PendingAction | None = None

    def add_exchange(self, user_text: str, assistant_text: str) -> None:
        """Save one user message and ARTHUR's reply, as a pair."""
        self.messages.append(Message(role=Role.USER, content=user_text))
        self.messages.append(Message(role=Role.ASSISTANT, content=assistant_text))
        # Hard cap so one endless conversation can't eat all the RAM.
        if len(self.messages) > self.max_stored_messages:
            del self.messages[: len(self.messages) - self.max_stored_messages]
        self.touch()

    def recent(self, token_budget: int, max_messages: int) -> list[Message]:
        """Newest messages that fit in `token_budget`, returned oldest-first.

        We walk backwards from the newest message and stop when the budget is
        spent - so when a conversation grows too long, the *oldest* messages
        are the ones that fall out of view.
        """
        selected: list[Message] = []
        used = 0
        for message in reversed(self.messages[-max_messages:] if max_messages else []):
            cost = estimate_message_tokens(message)
            if used + cost > token_budget:
                break
            selected.append(message)
            used += cost
        selected.reverse()
        # History must start with a user turn; a lone assistant reply confuses models.
        while selected and selected[0].role != Role.USER:
            selected.pop(0)
        return selected

    def clear(self) -> None:
        self.messages.clear()
        self.pending = None
        self.touch()

    def touch(self) -> None:
        self.last_active = time.monotonic()


class ConversationStore:
    """All active conversations, with limits so memory use stays bounded.

    - TTL: conversations idle longer than `ttl_seconds` are forgotten.
    - LRU: above `max_sessions`, the least-recently-used one is dropped.
    """

    def __init__(self, max_sessions: int = 100, ttl_seconds: float = 4 * 3600) -> None:
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self._sessions: OrderedDict[str, Conversation] = OrderedDict()

    def get(self, session_id: str) -> Conversation:
        self._evict_expired()
        conversation = self._sessions.get(session_id)
        if conversation is None:
            conversation = Conversation(session_id=session_id)
            self._sessions[session_id] = conversation
        conversation.touch()
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
        return conversation

    def clear(self, session_id: str) -> None:
        if conversation := self._sessions.get(session_id):
            conversation.clear()

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._sessions

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        for session_id in [s for s, c in self._sessions.items() if c.last_active < cutoff]:
            del self._sessions[session_id]
