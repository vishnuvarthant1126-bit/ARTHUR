"""Does a long conversation still remember its beginning?  (Phase 27, context compression)

    python scripts/check_long_recall.py --url http://127.0.0.1:8002 --pause 3

Turn 1 states two facts that can't be guessed. Then come long filler messages until the
beginning has scrolled out of the model's window. The last question asks for the facts.
Run it against a server with HISTORY_SUMMARY_DELAY_SECONDS shorter than --pause (notes are
written during the pauses) and once with a very long delay (no notes) to compare.
"""

import argparse
import time
import uuid

import httpx

FIRST = (
    "Some background before we start: my dog is called Pickles and my sister Maya lives in "
    "Tromsø. Just reply with the single word OK."
)
FILLER = (
    "Garden plan, part {n}: the north bed needs {n} bags of compost, the path "
    "gets gravel, the shed roof leaks near the window and the tomatoes go along the fence. "
) * 4 + "Just reply with the single word OK."
QUESTION = "What is my dog's name, and where does my sister live? One short sentence."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--fillers", type=int, default=22)
    parser.add_argument("--pause", type=float, default=3.0)
    args = parser.parse_args()

    session = uuid.uuid4().hex
    with httpx.Client(base_url=args.url, timeout=300, headers={"Origin": args.url}) as client:

        def say(text: str) -> str:
            response = client.post("/chat", json={"message": text, "session_id": session})
            response.raise_for_status()
            return response.json()["response"]

        say(FIRST)
        for n in range(1, args.fillers + 1):
            time.sleep(args.pause)
            say(FILLER.format(n=n))
        time.sleep(args.pause)
        answer = say(QUESTION)
    print(answer)
    found = [word for word in ("Pickles", "Tromsø") if word.lower() in answer.lower()]
    print(f"remembered {len(found)}/2: {', '.join(found) or 'nothing'}")


if __name__ == "__main__":
    main()
