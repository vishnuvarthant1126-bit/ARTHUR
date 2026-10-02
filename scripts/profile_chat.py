"""Where does the time go in a chat answer?  (Phase 24)

    python scripts/profile_chat.py                 # against http://127.0.0.1:8000
    python scripts/profile_chat.py --url http://127.0.0.1:8001

Sends a few typical messages to a running ARTHUR and, for each one, reads the change in
its /metrics to show the stages:

    recall    looking up memories and document passages (embedding the question)
    load      loading the model into the GPU (0 when it is already there)
    read      the model reading the prompt: system text + tool descriptions + history
    write     the model writing its answer (and any tool-call request)
    total     what the user waited

"prompt" is the size of what the model had to read, in tokens; "calls" is how many times
the model was asked (a tool question needs two: decide on the tool, then answer).
"""

import argparse
import re
import time
import uuid

import httpx

SUMS = {
    "recall": "arthur_chat_stage_seconds_sum",
    "load": "arthur_llm_load_seconds_sum",
    "read": "arthur_llm_prompt_read_seconds_sum",
    "llm": "arthur_llm_request_duration_seconds_sum",
    "prompt": "arthur_llm_prompt_tokens_sum",
    "calls": "arthur_llm_prompt_tokens_count",
    "first": "arthur_llm_first_token_seconds_sum",
}

SCENARIOS = [
    ("small talk (new chat)", None, "Hello! Answer in one short sentence."),
    ("small talk (new chat)", None, "Say good morning in one short sentence."),
    ("follow-up, same chat", "A", "Tell me one fact about the moon in one sentence."),
    ("follow-up, same chat", "A", "And one about the sun, in one sentence."),
    ("follow-up, same chat", "A", "Which of the two is bigger? One sentence."),
    ("uses a tool", None, "What is 348 times 27?"),
    ("uses a tool", None, "What time is it?"),
]

# One conversation in which the extra context changes from message to message: a passage
# from an uploaded document, nothing, another passage, a memory... (needs the sample
# handbook uploaded and a memory about "my main project").
CONTEXT_CHANGES = [
    ("nothing extra", "C", "Hello! Answer with the single word OK."),
    ("+ document passage", "C", "When is the library open, according to the handbook?"),
    ("nothing extra", "C", "Thanks. Answer with the single word OK."),
    ("+ other passage", "C", "What attendance does the handbook require? Short answer."),
    ("+ memory", "C", "What is my main project called? Short answer."),
    ("nothing extra", "C", "Good. Answer with the single word OK."),
    ("+ document passage", "C", "Is there wifi in the library? Short answer."),
    ("nothing extra", "C", "Fine. Answer with the single word OK."),
]


def totals(client: httpx.Client) -> dict[str, float]:
    text = client.get("/metrics").text
    result = {}
    for key, name in SUMS.items():
        values = re.findall(rf"^{name}(?:{{[^}}]*}})? ([0-9.e+-]+)$", text, re.MULTILINE)
        result[key] = sum(float(v) for v in values)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="seconds to wait between messages (a person reading and typing; try 8)",
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="run the 'context changes' conversation instead of the basic messages",
    )
    args = parser.parse_args()

    scenarios = CONTEXT_CHANGES if args.context else SCENARIOS
    sessions: dict[str, str] = {}
    columns = ("total", "recall", "load", "read", "write", "prompt", "calls")
    print(f"{'what':<24}" + "".join(f"{c:>8}" for c in columns) + "   (seconds; prompt in tokens)")
    with httpx.Client(base_url=args.url, timeout=300, headers={"Origin": args.url}) as client:
        client.get("/health").raise_for_status()
        for label, session_key, message in scenarios:
            time.sleep(args.pause)
            session = (
                sessions.setdefault(session_key, uuid.uuid4().hex)
                if session_key
                else uuid.uuid4().hex
            )
            before = totals(client)
            start = time.perf_counter()
            response = client.post("/chat", json={"message": message, "session_id": session})
            total = time.perf_counter() - start
            response.raise_for_status()
            after = totals(client)
            d = {key: after[key] - before[key] for key in SUMS}
            write = max(d["llm"] - d["load"] - d["read"], 0.0)
            calls = int(d["calls"])
            prompt = round(d["prompt"] / calls) if calls else 0
            print(
                f"{label:<24}{total:>8.2f}{d['recall']:>8.2f}{d['load']:>8.2f}{d['read']:>8.2f}"
                f"{write:>8.2f}{prompt:>8}{calls:>8}"
            )


if __name__ == "__main__":
    main()
