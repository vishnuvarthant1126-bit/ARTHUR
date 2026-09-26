"""Measure similarity scores to choose MEMORY_MIN_SCORE.

Prints the score of every (question, memory) pair so you can see where
"related" ends and "unrelated" begins for your embedding model.

    python scripts/calibrate_memory.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import get_settings  # noqa: E402
from app.memory.vector_store import cosine  # noqa: E402
from app.rag.embeddings import create_embedding_provider  # noqa: E402

MEMORIES = [
    "The user's main project is called ARTHUR.",
    "The user's favourite programming language is Python.",
    "The user is allergic to peanuts.",
    "The user prefers short answers.",
]

# (question, index of the memory that SHOULD match, or None if none should)
QUESTIONS = [
    ("What is my main project called?", 0),
    ("Which software am I building?", 0),
    ("What language do I like to code in?", 1),
    ("Can I eat a peanut butter sandwich?", 2),
    ("How detailed should your replies be?", 3),
    ("What's the capital of France?", None),
    ("Tell me a joke", None),
    ("What is 482 times 29?", None),
    ("How's the weather in Singapore?", None),
    ("Hello Arthur", None),
]


async def main() -> None:
    s = get_settings()
    embeddings = create_embedding_provider(
        s.embedding_provider, s.ollama_base_url, s.embedding_model
    )
    memory_vectors = await embeddings.embed_documents(MEMORIES)
    related, unrelated = [], []
    for question, expected in QUESTIONS:
        q = await embeddings.embed_query(question)
        scores = [cosine(q, m) for m in memory_vectors]
        for i, score in enumerate(scores):
            (related if i == expected else unrelated).append(score)
        best = max(range(len(scores)), key=scores.__getitem__)
        print(f"{question:40s} best={scores[best]:.3f} -> {MEMORIES[best][:40]}")
    await embeddings.aclose()
    print(f"\nrelated pairs:   min={min(related):.3f}  max={max(related):.3f}")
    print(f"unrelated pairs: min={min(unrelated):.3f}  max={max(unrelated):.3f}")
    print(f"suggested threshold: {(min(related) + max(unrelated)) / 2:.3f} (midpoint)")


if __name__ == "__main__":
    asyncio.run(main())
