"""Record a real ARTHUR session for the replay demo on GitHub Pages.

    python scripts/record_demo.py --url ws://127.0.0.1:8002/ws

Sends the demo questions one by one over the WebSocket - exactly like the web page - and
saves every event ARTHUR sends back, with its timing, to demo/recording.json. The replay
page (demo/replay.js) plays them back, so visitors see the real status lamps, plans, tool
calls and streamed answers without any AI running.

Run it against a server with ONLY fictional files and a temporary memory (see docs/DEMO.md),
because everything recorded here is published.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]

QUESTIONS = [
    "Check my documents and find my latest resume.",
    "Summarize my technical skills.",
    "What's the weather in Singapore and in London right now, and what is 15% of 240?",
    "Now search the web for AI engineering roles that match my skills and prepare a comparison.",
    "Save the report.",
    "yes",
]


async def ask(ws, question: str) -> dict:
    await ws.send(json.dumps({"type": "chat", "message": question}))
    started = time.perf_counter()
    events = []
    while True:
        event = json.loads(await ws.recv())
        events.append({"t": round((time.perf_counter() - started) * 1000), "event": event})
        if event["type"] in ("done", "error"):
            break
    return {"question": question, "events": events}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="ws://127.0.0.1:8002/ws")
    args = parser.parse_args()
    parts = urlsplit(args.url)
    http = f"{'https' if parts.scheme == 'wss' else 'http'}://{parts.netloc}"

    async with httpx.AsyncClient(base_url=http, timeout=30) as client:
        health = (await client.get("/health")).json()
        status = (await client.get("/status")).json()

    turns = []
    async with websockets.connect(args.url, max_size=None) as ws:
        json.loads(await ws.recv())  # the "session" greeting
        for question in QUESTIONS:
            turn = await ask(ws, question)
            last = turn["events"][-1]["event"]
            print(f"{question[:60]:60} {last['type']:5} {turn['events'][-1]['t'] / 1000:6.1f} s")
            turns.append(turn)

    out = ROOT / "demo" / "recording.json"
    out.parent.mkdir(exist_ok=True)
    recording = {
        "recorded": time.strftime("%Y-%m-%d"),
        "health": health,
        "status": status,
        "turns": turns,
    }
    out.write_text(json.dumps(recording, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    asyncio.run(main())
