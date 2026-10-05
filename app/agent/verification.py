"""Response checks that don't rely on the model behaving.

Small models sometimes *say* they did something ("I've deleted it") without
calling any tool. Nothing happens in that case - actions only run through the
ToolRegistry - but the user would be misled. This check spots such claims and
lets the orchestrator add a visible correction.
"""

import re
from urllib.parse import urlsplit

_ACTION_CLAIM = re.compile(
    r"\b(?:I(?:'ve| have| just| successfully)?|has been|have been|was|were|is now|are now)\s+"
    r"(?:successfully\s+|now\s+)?"
    r"(?:deleted|removed|erased|forgotten|saved|stored|sent|created|updated|changed|"
    r"booked|purchased|bought|submitted|scheduled|moved|renamed|cancelled|canceled|"
    r"set (?:a|the|your|that) reminder)\b",
    re.IGNORECASE,
)

CORRECTION = (
    "\n\n> ⚠️ **Correction:** no action was actually performed in this reply - "
    "nothing was changed. Ask again and I'll use the proper tool."
)


# The model copying ARTHUR's own "I need your permission first ... Reply yes" message, or
# the note that replaces it in the history, without calling a tool (both seen live).
_FAKE_PERMISSION = re.compile(
    r"need your permission|reply \W*yes\W* to (go ahead|continue|proceed|confirm)"
    r"|safety system asked|asked the user to approve",
    re.IGNORECASE,
)

FAKE_PERMISSION_NOTE = (
    "\n\n> ⚠️ **Note:** that question came from the language model, not ARTHUR's safety "
    "system - no action is waiting for approval. Ask again and I'll use the proper tool."
)


def claims_action(text: str) -> bool:
    return bool(_ACTION_CLAIM.search(text))


def fakes_permission_request(text: str) -> bool:
    return bool(_FAKE_PERMISSION.search(text))


# ---------- Phase 27: are the cited sources real? ----------
# A citation the model invented looks exactly like a real one. After each answer ARTHUR
# checks every cited document page and web link against what it actually showed the model
# in this turn (passages, attached files, tool results). Unmatched ones get a visible note.
DOCUMENT_CITATION = re.compile(r"\[([^\[\]\n]+?\.(?:pdf|docx|txt|md|csv)),\s*([^\[\]\n]+?)\]", re.I)
MARKDOWN_LINK = re.compile(r"\[[^\]\n]*\]\((https?://[^)\s]+)\)")
BARE_URL = re.compile(r"https?://[^\s\"'<>)\]\\]+")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def unverified_sources(answer: str, seen: str) -> list[str]:
    """Citations in `answer` that appear nowhere in `seen` (what the model was given)."""
    known = _squash(seen)
    missing: list[str] = []
    for name, label in DOCUMENT_CITATION.findall(answer):
        citation = f"{name.strip()}, {label.strip()}"
        if _squash(citation) not in known and f"[{citation}]" not in missing:
            missing.append(f"[{citation}]")
    seen_urls = [u.rstrip(".,;") for u in BARE_URL.findall(seen)]
    for url in MARKDOWN_LINK.findall(answer):
        stem = url.rstrip("/")
        # A link to a page that was read, or to the front page of a site it came from,
        # counts as read. So does a front page whose site a result names by name
        # ("source": "Open-Meteo (open-meteo.com)") - deeper links must match a real URL.
        parts = urlsplit(url)
        host = parts.netloc.lower().removeprefix("www.")
        front_page_named = parts.path in ("", "/") and host in known
        read = front_page_named or any(u.startswith(stem) for u in seen_urls)
        if not read and url not in missing:
            missing.append(url)
    return missing


def source_note(missing: list[str]) -> str:
    shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
    return (
        "\n\n> ⚠️ **Source check:** I couldn't match "
        + ("this source" if len(missing) == 1 else "these sources")
        + f" to anything I actually read for this answer: {shown}. "
        "Treat the claims attached to them with care."
    )
