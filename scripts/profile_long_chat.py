"""How does a LONG conversation behave?  (Phase 24)

    python scripts/profile_long_chat.py [--turns 14]

Sends long messages in one conversation and prints, per turn, the prompt size the model
received and how long it spent reading it. A healthy run shows the prompt growing while
"read" stays near zero (the unchanged beginning is cached). Trouble looks like the
prompt hitting the context window (8192) and "read" jumping to seconds on every turn.
"""

import argparse
import time
import uuid

import httpx
from profile_chat import SUMS, totals

PARAGRAPH = (
    "Paragraph {n} of my travel notes: on day {n} we walked along the river, visited market "
    "number {n}, counted {n} bridges and ate at a small place called Cafe {n}. "
) * 4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--turns", type=int, default=14)
    args = parser.parse_args()

    session = uuid.uuid4().hex
    print(f"{'turn':>4}{'total':>8}{'read':>7}{'write':>7}{'prompt':>8}   answer")
    with httpx.Client(base_url=args.url, timeout=300, headers={"Origin": args.url}) as client:
        for turn in range(1, args.turns + 1):
            message = PARAGRAPH.format(n=turn) + "Just reply with the single word OK."
            if turn == args.turns:
                message = "What was the name of the place we ate at on day 1? One short sentence."
            before = totals(client)
            start = time.perf_counter()
            response = client.post("/chat", json={"message": message, "session_id": session})
            total = time.perf_counter() - start
            response.raise_for_status()
            after = totals(client)
            d = {key: after[key] - before[key] for key in SUMS}
            calls = max(int(d["calls"]), 1)
            write = max(d["llm"] - d["load"] - d["read"], 0.0)
            answer = response.json()["response"].replace("\n", " ")[:60]
            print(
                f"{turn:>4}{total:>8.2f}{d['read']:>7.2f}{write:>7.2f}"
                f"{round(d['prompt'] / calls):>8}   {answer}"
            )


if __name__ == "__main__":
    main()
