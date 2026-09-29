"""Wake word: did the user just say "Hey Arthur"?

The browser sends short clips only when it hears sound. Each clip is
transcribed with a small, fast Whisper model, and this module decides whether
the text *starts* with the wake phrase:

    "Hey Arthur, what's the weather?"  -> wake, command "what's the weather?"
    "OK Arthur."                       -> wake, no command (ARTHUR answers "Yes?")
    "Arthur, set a timer"              -> wake, command "set a timer"
    "I like that author"               -> no  (name not at the start; "author" isn't "Arthur")

Requiring the name at the start keeps false alarms low: people often mention a
name mid-sentence, but rarely *begin* a sentence with it unless they're calling.
Small spelling tolerance ("Arther", "Artur") reduces misses.
"""

import re
from difflib import SequenceMatcher

from pydantic import BaseModel

WAKE_NAME = "arthur"
GREETINGS = {"hey", "hi", "hello", "ok", "okay", "yo", "hay"}
MIN_SIMILARITY = 0.8  # "arther" 0.83 and "artur" 0.91 pass; "author" 0.67 and "archer" 0.67 don't
# Measured mishearings of "Arthur" in noise (scripts/evaluate_wake_word.py). Accepted ONLY
# right after a greeting: "Hey offer" is never real speech, but "Offer them..." might be.
MISHEARINGS_AFTER_GREETING = {"offer"}


class WakeResult(BaseModel):
    wake: bool
    command: str | None = None  # what followed the wake phrase, if anything
    heard: str = ""  # the transcript (for debugging; never stored)


def _is_name(word: str) -> bool:
    return SequenceMatcher(None, word, WAKE_NAME).ratio() >= MIN_SIMILARITY


def detect_wake_phrase(text: str) -> WakeResult:
    # Words with their positions in the original text, so the command keeps its wording.
    words = [(m.group().lower(), m.end()) for m in re.finditer(r"[A-Za-z']+", text)]
    if not words:
        return WakeResult(wake=False, heard=text)

    name_at = None
    if _is_name(words[0][0]):  # "Arthur, ..."
        name_at = 0
    elif len(words) > 1 and words[0][0] in GREETINGS:  # "Hey Arthur"
        second = words[1][0]
        if _is_name(second) or second in MISHEARINGS_AFTER_GREETING:
            name_at = 1
    if name_at is None:
        return WakeResult(wake=False, heard=text)

    command = text[words[name_at][1] :].strip(" ,.!?;:-")
    return WakeResult(wake=True, command=command if len(command) >= 2 else None, heard=text)
