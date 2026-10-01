"""Start a throw-away ARTHUR for load tests on http://127.0.0.1:8001.

    python scripts/run_load_server.py              # echo model answers instantly
    python scripts/run_load_server.py --delay 1.5  # ...or "thinks" for 1.5 s like a real model

It is isolated from your real ARTHUR:
  - its own temporary database and vector store (deleted afterwards)
  - the echo model and hash embeddings: no Ollama, no GPU
  - no browser, desktop control or vision; rate limits off (we WANT to flood it)

Then, in another terminal:
    locust -f tests/load/locustfile.py --headless -u 20 -r 5 -t 30s --host http://127.0.0.1:8001
"""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import uvicorn

PORT = 8001


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delay", type=float, default=0.0, help="seconds each answer takes")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    data = Path(tempfile.mkdtemp(prefix="arthur-load-"))
    os.environ.update(
        {
            "ARTHUR_ENV": "test",
            "LOG_LEVEL": "WARNING",  # logging every request would be part of what we measure
            "LLM_PROVIDER": "echo",
            "ECHO_DELAY_SECONDS": str(args.delay),
            "EMBEDDING_PROVIDER": "hash",
            "DATABASE_PATH": str(data / "arthur.db"),
            "VECTOR_STORE_PATH": str(data / "chroma"),
            "DOCUMENTS_PATH": str(data / "documents"),
            "ALLOWED_DIRECTORIES": str(data),
            "FILES_SAVE_DIR": str(data / "reports"),
            "RATE_LIMIT_ENABLED": "false",
            "BROWSER_ENABLED": "false",
            "COMPUTER_USE_ENABLED": "false",
            "VISION_ENABLED": "false",
            "VOICE_WARM_UP": "false",
            "AGENT_PLANNING": "false",
        }
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    print(
        f"Load-test ARTHUR on http://127.0.0.1:{args.port}  (data in {data}, delay {args.delay}s)"
    )
    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
