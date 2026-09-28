"""Measure document-search scores to choose RAG_MIN_SCORE.

Uses the sample handbook (run scripts/make_sample_handbook.py first). For each
question we know which page *should* answer it (or that none should), and print
the best score so you can see where "relevant" ends.

    python scripts/calibrate_rag.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import get_settings  # noqa: E402
from app.memory.vector_store import cosine  # noqa: E402
from app.rag.chunking import chunk_pages  # noqa: E402
from app.rag.embeddings import create_embedding_provider  # noqa: E402
from app.rag.ingestion import extract  # noqa: E402

HANDBOOK = Path(__file__).resolve().parents[1] / "data" / "samples" / "student_handbook.pdf"

# (question, page that answers it, or None if the handbook doesn't)
QUESTIONS = [
    ("What are the requirements for graduation?", "p. 2"),
    ("How many credits do I need to finish my degree?", "p. 2"),
    ("When is the deadline to apply to graduate?", "p. 2"),
    ("What happens if I hand in my assignment late?", "p. 3"),
    ("How much attendance is required?", "p. 3"),
    ("When does the library close?", "p. 4"),
    ("How much does tuition cost for international students?", "p. 5"),
    ("What GPA do I need for the Dean's scholarship?", "p. 5"),
    ("What's the weather in Singapore?", None),
    ("Tell me a joke", None),
    ("What is 482 times 29?", None),
    ("Who won the football world cup?", None),
]


async def main() -> None:
    s = get_settings()
    chunks = chunk_pages(
        extract(HANDBOOK.read_bytes(), ".pdf"), s.rag_chunk_size, s.rag_chunk_overlap
    )
    embeddings = create_embedding_provider(
        s.embedding_provider, s.ollama_base_url, s.embedding_model
    )
    vectors = await embeddings.embed_documents([c.text for c in chunks])
    right, wrong, unrelated = [], [], []
    for question, page in QUESTIONS:
        q = await embeddings.embed_query(question)
        scored = sorted(
            ((cosine(q, v), c.page_label) for c, v in zip(chunks, vectors, strict=True)),
            reverse=True,
        )
        best_score, best_page = scored[0]
        if page is None:
            unrelated.append(best_score)
            verdict = "(should match nothing)"
        else:
            ok = best_page == page
            (right if ok else wrong).append(best_score)
            verdict = "OK" if ok else f"WRONG, expected {page}"
        print(f"{question:58s} best={best_score:.3f} {best_page:6s} {verdict}")
    await embeddings.aclose()
    print(f"\ncorrect top hits: {len(right)}/{len(right) + len(wrong)}")
    print(f"relevant:  min={min(right):.3f}")
    print(f"unrelated: max={max(unrelated):.3f}")
    print(f"suggested RAG_MIN_SCORE: {(min(right) + max(unrelated)) / 2:.3f} (midpoint)")


if __name__ == "__main__":
    asyncio.run(main())
