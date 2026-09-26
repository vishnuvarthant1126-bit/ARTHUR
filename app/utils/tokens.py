"""Rough token counting.

Models read text as *tokens* - word pieces. In English one token is about
4 characters ("ARTHUR" ~ 2 tokens, "hello" ~ 1). Exact counts need the
model's own tokenizer; this estimate is good enough to keep the conversation
inside the context window with a safety margin.
"""

from app.llm.base import Message

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4  # role markers and separators the model adds per message


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def estimate_message_tokens(message: Message) -> int:
    return estimate_tokens(message.content) + MESSAGE_OVERHEAD_TOKENS
