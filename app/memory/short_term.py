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
    """An action waiting for the user's "yes" (forgetting a memory, a level-2 tool call)."""

    kind: str  # "delete_memory" | "tool_call"
    target_id: str  # memory id, or tool name
    description: str  # what will happen, shown to the user
    payload: dict = field(default_factory=dict)  # e.g. the tool call's arguments
    asked_at: float = field(default_factory=time.monotonic)  # a "yes" much later doesn't count

    def expired(self, max_age_seconds: float) -> bool:
        return time.monotonic() - self.asked_at > max_age_seconds


@dataclass
class Conversation:
    session_id: str
    max_stored_messages: int = 200
    messages: list[Message] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    pending: PendingAction | None = None
    window_start: int = 0  # index of the oldest message the model is still shown
    # Phase 27: what scrolled out of the window, compressed (messages[:summarized_until]).
    summary: str = ""
    summarized_until: int = 0

    def add_exchange(self, user_text: str, assistant_text: str) -> None:
        """Save one user message and ARTHUR's reply, as a pair."""
        self.messages.append(Message(role=Role.USER, content=user_text))
        self.messages.append(Message(role=Role.ASSISTANT, content=assistant_text))
        # Hard cap so one endless conversation can't eat all the RAM.
        if len(self.messages) > self.max_stored_messages:
            removed = len(self.messages) - self.max_stored_messages
            del self.messages[:removed]
            self.window_start = max(self.window_start - removed, 0)
            self.summarized_until = max(self.summarized_until - removed, 0)
        self.touch()

    def window(self, token_budget: int, max_messages: int, keep: float = 0.6) -> list[Message]:
        """The part of the conversation shown to the model: `messages[window_start:]`.

        Unlike `recent()`, the start does not creep forward one message per turn. While the
        visible part still fits, the start stays put - so the beginning of the prompt is
        identical to last time and the model's cache stays valid. Only when it no longer
        fits does the start jump ahead, far enough (down to `keep` x the limits) to leave
        room for several more turns. Measured in Phase 24: without this, every turn of a
        long conversation cost an extra 1.8 s of re-reading.
        """
        if token_budget <= 0 or not max_messages:
            return []
        costs = [estimate_message_tokens(m) for m in self.messages]
        total = len(self.messages)

        def fits(start: int, tokens: float, count: float) -> bool:
            return sum(costs[start:]) <= tokens and total - start <= count

        start = min(self.window_start, total)
        if not fits(start, token_budget, max_messages):
            while start < total and not fits(start, token_budget * keep, max_messages * keep):
                start += 1
        # History must start with a user turn; a lone assistant reply confuses models.
        while start < total and self.messages[start].role != Role.USER:
            start += 1
        self.window_start = start
        return self.messages[start:]

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
        self.window_start = 0
        self.summary = ""
        self.summarized_until = 0
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
