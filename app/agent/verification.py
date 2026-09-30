"""Response checks that don't rely on the model behaving.

Small models sometimes *say* they did something ("I've deleted it") without
calling any tool. Nothing happens in that case - actions only run through the
ToolRegistry - but the user would be misled. This check spots such claims and
lets the orchestrator add a visible correction.
"""

import re

_ACTION_CLAIM = re.compile(
    r"\b(?:I(?:'ve| have| just| successfully)?|has been|have been|was|were|is now|are now)\s+"
    r"(?:successfully\s+|now\s+)?"
    r"(?:deleted|removed|erased|forgotten|saved|stored|sent|created|updated|changed|"
    r"booked|purchased|bought|submitted|scheduled|moved|renamed)\b",
    re.IGNORECASE,
)

CORRECTION = (
    "\n\n> ⚠️ **Correction:** no action was actually performed in this reply - "
    "nothing was changed. Ask again and I'll use the proper tool."
)


# The model copying ARTHUR's own "I need your permission first ... Reply yes" message
# (it sees earlier ones in the chat history) without calling a tool.
_FAKE_PERMISSION = re.compile(
    r"need your permission|reply \W*yes\W* to (go ahead|continue|proceed|confirm)", re.IGNORECASE
)

FAKE_PERMISSION_NOTE = (
    "\n\n> ⚠️ **Note:** that question came from the language model, not ARTHUR's safety "
    "system - no action is waiting for approval. Ask again and I'll use the proper tool."
)


def claims_action(text: str) -> bool:
    return bool(_ACTION_CLAIM.search(text))


def fakes_permission_request(text: str) -> bool:
    return bool(_FAKE_PERMISSION.search(text))
