# ARTHUR – Personal Multimodal AI Agent

A modular, local-first AI assistant: voice + text, tools, memory, RAG,
planning, browser/computer control, with a permission-based security model.

> Status: Phase 4 – streamed web chat with conversation memory, swappable LLM providers
> with retries and fallback. Full documentation arrives as features land.

## Requirements
- Python 3.12
- [Ollama](https://ollama.com) with `qwen3:8b` and `nomic-embed-text` pulled
- Git; Docker Desktop (from Phase 23)

## Quick start (Windows PowerShell)
```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
```

macOS / Linux: `python3.12 -m venv .venv && source .venv/bin/activate`

## Run
```powershell
uvicorn app.main:app --reload
```
- Chat UI: http://127.0.0.1:8000
- Interactive API docs: http://127.0.0.1:8000/docs

| Endpoint | Purpose |
|---|---|
| `GET /` | Web chat interface (`frontend/`) |
| `GET /health` | Service status and LLM reachability |
| `POST /chat` | `{"message": "Hello Arthur", "session_id": "<optional>"}` → `{"response": "...", "session_id": "...", "model": "...", "latency_ms": 412.0}` |
| `DELETE /chat/{session_id}` | Forget a conversation |
| `WS /ws?session_id=<optional>` | Streaming chat. Send `chat` / `stop` / `clear` / `ping`; receive `session` (with history), `status`, `token`…, `done`, `error`, `cleared`. Browser connections from other origins are rejected. |

Errors always look like `{"error": {"type": "llm_unavailable", "message": "..."}, "request_id": "..."}`
(503 LLM unreachable · 504 LLM timeout · 502 bad LLM output · 422 invalid input).

## LLM providers
The app talks only to the `LLMProvider` interface (`generate`, `stream`, `generate_structured`).
Choose providers in `.env`:

| Setting | Effect |
|---|---|
| `LLM_PROVIDER=ollama` | Local model via Ollama (default) |
| `LLM_PROVIDER=openai_compat` | Any OpenAI-compatible API (OpenAI, Groq, OpenRouter, LM Studio…); set `OPENAI_COMPAT_*` |
| `LLM_FALLBACK_PROVIDER=openai_compat` | Used automatically if the main provider fails |
| `LLM_MAX_RETRIES=2` | Retries for brief outages / rate limits (exponential backoff + jitter) |

## Conversation memory
Each browser session keeps its conversation in server RAM. Every turn, ARTHUR sends the
system prompt + the newest messages that fit in `LLM_CONTEXT_TOKENS` (minus a reply reserve) +
the new message. Older messages drop out first. Idle sessions expire after
`MEMORY_SESSION_TTL_MINUTES`; restarting the server forgets all conversations.

## Test
```powershell
pytest              # unit tests + live Ollama tests (skipped if Ollama is off)
ruff check .        # lint
```
