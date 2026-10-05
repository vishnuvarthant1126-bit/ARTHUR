"""Files attached to a chat message (Phase 26): pictures, screenshots and documents.

The browser uploads a file as soon as it is attached (POST /attachments) and gets an id.
The chat message then carries only the ids. Attachments live in RAM for 30 minutes - long
enough to type the question - and are then forgotten. Pictures are never written to disk;
documents are ALSO imported into the user's documents, so later questions can find them.

How they reach the language model (qwen3 cannot see pictures):

    picture  -> the vision model looks at it WITH the user's question, writes what it sees
    document -> its text (as much as fits) with page labels for citations

Both travel in the <context> block of that one message, marked as DATA.
"""

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.rag.ingestion import Page

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_PER_MESSAGE = 4
# All attached document text for one message. The model's window is 8,192 tokens and the
# standing prompt takes ~4,000 of them; 8,000 characters are ~2,000 tokens.
MAX_DOCUMENT_CHARS = 8000
MAX_DESCRIPTION_CHARS = 3000
ATTACHMENT_ID = r"^[a-f0-9]{32}$"

VISION_QUESTION = (
    "The user attached this picture and asks: «{question}»\n"
    "Describe everything in the picture that matters for that question. Copy important "
    "text exactly (error messages, numbers, names, code). If something looks wrong, broken "
    "or unusual, say what and where."
)


@dataclass
class Attachment:
    kind: Literal["image", "document"]
    name: str
    detail: str  # shown on the chip: "1280×720 picture", "PDF · 12 pages"
    image: bytes | None = None  # prepared JPEG (pictures)
    pages: list[Page] = field(default_factory=list)  # text (documents)
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    created: float = 0.0


class AttachmentError(Exception):
    """A missing or expired attachment, safe to show the user."""


class AttachmentStore:
    """The most recent attachments, in RAM, each for `ttl_seconds`."""

    def __init__(
        self,
        ttl_seconds: float = 1800,
        max_items: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl_seconds
        self.max_items = max_items
        self.clock = clock
        self._items: OrderedDict[str, Attachment] = OrderedDict()

    def add(self, attachment: Attachment) -> Attachment:
        attachment.created = self.clock()
        self._items[attachment.id] = attachment
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)  # the oldest goes first
        return attachment

    def get(self, attachment_id: str) -> Attachment:
        attachment = self._items.get(attachment_id)
        if attachment is None or self.clock() - attachment.created > self.ttl:
            self._items.pop(attachment_id, None)
            raise AttachmentError("An attached file has expired - please attach it again.")
        return attachment

    def resolve(self, ids: list[str]) -> list[Attachment]:
        return [self.get(i) for i in dict.fromkeys(ids)]  # in order, without repeats


def cite(name: str, page: Page) -> str:
    return f"[{name}, {page.label}]" if page.label else f"[{name}]"


def document_text(attachment: Attachment, budget: int) -> tuple[str, int]:
    """As many whole pages as fit into `budget` characters -> (text, characters used)."""
    parts, used, shown = [], 0, 0
    for page in attachment.pages:
        block = f"{cite(attachment.name, page)}\n{page.text.strip()}\n"
        if used + len(block) > budget:
            if shown == 0:  # even page one is too long: show its beginning
                block = block[:budget] + " …\n"
                parts.append(block)
                used += len(block)
                shown = 1
            break
        parts.append(block)
        used += len(block)
        shown += 1
    text = "".join(parts)
    if shown < len(attachment.pages):
        text += (
            f"(Only the first {shown} of {len(attachment.pages)} parts fit here. The whole "
            "file is in the user's documents - use document_search for the rest.)\n"
        )
    return text, used


def attachment_section(attachment: Attachment, description: str | None, budget: int) -> str:
    """One attachment, written for the <context> block."""
    if attachment.kind == "image":
        if description is None:
            return (
                f'\nAttached picture "{attachment.name}": the vision model could not look at '
                "it. Tell the user you couldn't see the picture.\n"
            )
        return (
            f'\nAttached picture "{attachment.name}" ({attachment.detail}). You cannot see '
            "pictures; this is what ARTHUR's vision model saw in it:\n"
            f"{description[:MAX_DESCRIPTION_CHARS].strip()}\n"
        )
    text, _ = document_text(attachment, budget)
    return (
        f'\nAttached document "{attachment.name}" ({attachment.detail}), attached to THIS '
        "message. Answer from it and cite pages exactly as labelled:\n"
        f"{text}"
    )


def history_note(attachments: list[Attachment], descriptions: dict[str, str | None]) -> str:
    """What the conversation history keeps, so follow-up questions still make sense."""
    notes = []
    for a in attachments:
        if a.kind == "image":
            seen = descriptions.get(a.id)
            summary = f" - the vision model saw: {seen[:600].strip()}" if seen else ""
            notes.append(f"[Attached picture {a.name}{summary}]")
        else:
            notes.append(f"[Attached document {a.name} - imported into the user's documents]")
    return "\n" + "\n".join(notes) if notes else ""
