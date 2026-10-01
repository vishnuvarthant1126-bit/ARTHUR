"""Memory policy: what ARTHUR may remember, and when.

Rules (MVP):
1. Save ONLY when the user explicitly asks ("remember that...", "don't forget...",
   "note that..."). Ordinary chat is never stored automatically.
2. Questions are not requests: "Do you remember my name?" stores nothing.
3. Never store secrets: passwords, PINs, API keys, card/bank numbers, ID numbers.
   These belong in a password manager, not an AI's notebook.
4. Forgetting always shows the matching memory and waits for a "yes" first.
5. The user can see and delete every memory (Memory panel / GET /memories).

Detection uses simple patterns - fast, free and predictable. In Phase 7 the
agent can also call memory tools itself, still under these rules.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from app.memory.manager import CATEGORIES

_PREFIX = r"^\s*(?:(?:hey|ok|okay)\s+)?(?:arthur\s*[,!.:]?\s*)?(?:please\s+)?(?:can you\s+)?"

_REMEMBER = re.compile(
    _PREFIX + r"(?:remember|don'?t forget|do not forget|keep in mind|note|make a note)\b"
    r"(?:\s+(?:that|this|:))?\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
# "forget X", or "delete/remove/erase the memory X" ("delete this file" is NOT a memory request).
_FORGET = re.compile(
    _PREFIX + r"(?:forget(?:\s+(?:that|about))?"
    r"|(?:delete|remove|erase)\s+(?:(?:the|my|that|this)\s+)?memory(?:\s+(?:that|about|of))?)"
    r"\s+(?P<rest>.+)$",
    re.IGNORECASE | re.DOTALL,
)

_SENSITIVE = re.compile(
    r"\b(?:password|passcode|passphrase|pin(?:\s+code)?|otp|one[- ]time code|"
    r"api[ _-]?key|secret key|access token|auth token|private key|seed phrase|recovery phrase|"
    r"credit card|debit card|card number|cvv|cvc|iban|bank account|account number|"
    r"routing number|sort code|ssn|social security|passport number|nric)\b"
    r"|\b(?:\d[ -]?){12,19}\b",  # long digit runs: card / account numbers
    re.IGNORECASE,
)

# A confirmation must be the WHOLE message and nothing else: "ok, what is this?" or
# "yes but not the second one" must never approve a pending action.
_YES_START = {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
              "go", "do", "proceed", "absolutely", "definitely", "alright"}  # fmt: skip
_YES_FILLER = {"please", "it", "that", "this", "go", "ahead", "do", "now", "thanks", "thank",
               "you", "arthur", "yes", "ok", "okay", "sure", "proceed", "confirm"}  # fmt: skip
_NO = re.compile(r"^\s*(?:no|n|nope|cancel|keep it|don'?t|stop)\b", re.I)


class MemoryIntent(StrEnum):
    REMEMBER = "remember"
    FORGET = "forget"
    NONE = "none"


def detect_intent(text: str) -> MemoryIntent:
    if text.strip().endswith("?"):
        return MemoryIntent.NONE  # "Do you remember...?" is a question, not a request
    if (m := _REMEMBER.match(text)) and m.group("rest").strip():
        return MemoryIntent.REMEMBER
    if _FORGET.match(text):
        return MemoryIntent.FORGET
    return MemoryIntent.NONE


def forget_query(text: str) -> str:
    """'Forget that my favourite colour is blue' -> 'my favourite colour is blue'."""
    m = _FORGET.match(text)
    return m.group("rest").strip(" .!") if m else text


def remember_fallback(text: str) -> str:
    """If the LLM can't extract a fact, keep the user's own words minus the trigger."""
    m = _REMEMBER.match(text)
    return (m.group("rest") if m else text).strip(" .!") + "."


def contains_sensitive_data(text: str) -> bool:
    return bool(_SENSITIVE.search(text))


def is_yes(text: str) -> bool:
    """True only for a short, pure confirmation: "yes", "ok do it", "yes please, go ahead"."""
    words = re.findall(r"[a-z']+", text.lower())
    if not words or len(words) > 5 or re.search(r"[?]", text):
        return False
    return words[0] in _YES_START and all(w in _YES_FILLER for w in words[1:])


def is_no(text: str) -> bool:
    return bool(_NO.match(text))


class MemoryDraft(BaseModel):
    """What the LLM extracts from a 'remember...' message."""

    fact: str = Field(max_length=500)
    category: str = Field(description=f"One of: {', '.join(CATEGORIES)}")


EXTRACTION_PROMPT = f"""Extract what the user wants you to remember from their message.

Write it as ONE short, self-contained sentence about the user, in the third person,
for example: "The user's favourite programming language is Python."
Keep names, numbers and specifics exactly as given. Do not add anything that was not said.
Choose category from: {", ".join(CATEGORIES)}.
Respond as JSON with keys "fact" and "category"."""
