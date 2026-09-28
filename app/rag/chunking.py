"""Step 2 of RAG: cut each page into overlapping chunks.

Why chunks? A whole document doesn't fit in the model's context window, and
one embedding for 200 pages is too blurry to match a specific question.
~1000-character chunks (a few paragraphs) are precise enough to find, and
big enough to contain a complete answer.

Why overlap? If a key sentence falls on a boundary, the ~150 characters
repeated at the start of the next chunk make sure it appears whole somewhere.

Chunks never cross a page boundary, so every chunk has exactly one page to cite.
Splitting prefers paragraph breaks, then sentence ends, and only cuts inside a
sentence if a single sentence is longer than a whole chunk.
"""

import re
from dataclasses import dataclass

from app.rag.ingestion import Page


@dataclass
class Chunk:
    text: str
    index: int  # position within the document: 0, 1, 2 ...
    page_number: int | None
    page_label: str | None


def chunk_pages(pages: list[Page], size: int = 1000, overlap: int = 150) -> list[Chunk]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than the chunk size")
    chunks: list[Chunk] = []
    for page in pages:
        for text in _chunk_text(page.text, size, overlap):
            chunks.append(Chunk(text, len(chunks), page.number, page.label))
    return chunks


def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
    units = _units(text, size)
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for unit in units:
        if current and length + len(unit) + 1 > size:
            chunks.append(" ".join(current))
            # Start the next chunk with the last units that fit in `overlap`.
            tail: list[str] = []
            tail_length = 0
            for previous in reversed(current):
                if tail_length + len(previous) + 1 > overlap:
                    break
                tail.insert(0, previous)
                tail_length += len(previous) + 1
            current, length = tail, tail_length
        current.append(unit)
        length += len(unit) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def _units(text: str, size: int) -> list[str]:
    """Split into paragraphs, then sentences, then hard slices - each at most `size` long."""
    units: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            units.append(paragraph)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            if len(sentence) <= size:
                units.append(sentence)
            else:
                units.extend(sentence[i : i + size] for i in range(0, len(sentence), size))
    return units
