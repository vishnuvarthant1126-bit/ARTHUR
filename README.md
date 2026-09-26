# ARTHUR – Personal Multimodal AI Agent

A modular, local-first AI assistant: voice + text, tools, memory, RAG,
planning, browser/computer control, with a permission-based security model.

> Status: Phase 6 – streamed web chat, conversation + long-term semantic memory,
> swappable LLM providers, and a permission-checked, audited tool system.
> Full documentation arrives as features land.

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
| `GET /memories` · `POST /memories` · `DELETE /memories/{id}` | List, add, forget long-term memories |
| `GET /memories/search?q=...` | Semantic memory search (returns similarity scores) |
| `GET /tools` | Available tools with permission level and input schema |
| `POST /tools/{name}/run` | `{"arguments": {...}, "confirmed": false}` → `ok` / `error` / `needs_confirmation` / `denied` |
| `GET /audit` | Recent tool calls (audit log) |
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

## Long-term memory
```
"Remember that ..." ─► secret filter ─► LLM extracts one clean fact ─► embedding (nomic-embed-text)
                                                                          ├─► SQLite  (the record)
                                                                          └─► ChromaDB (the vector)
Every message ─► embed question ─► nearest memories with similarity ≥ MEMORY_MIN_SCORE
             ─► added to the system prompt
"Forget ..." ─► find best match ─► ask "yes/no?" ─► delete from both stores
```
Memory policy: only explicit "remember…" requests are stored; questions never are; passwords,
PINs, keys and card/bank numbers are refused; forgetting needs confirmation; everything is visible
in the **Memory** panel. `MEMORY_MIN_SCORE` (0.55) was measured with `scripts/calibrate_memory.py`.

## Tools and permissions
Each tool declares a name, description, Pydantic input schema, permission level and timeout.
Every call goes through `ToolRegistry.execute`: lookup → permission check → argument validation →
confirmation → run with timeout → error capture → audit log.

| Level | Meaning | Behaviour | Examples |
|---|---|---|---|
| 0 | Read-only | Runs | `calculator`, `current_time`, `weather`, `search_memory` |
| 1 | Low-risk, reversible | Runs | `save_memory` |
| 2 | Needs confirmation | Returns a preview until confirmed | `delete_memory` |
| 3 | Highly sensitive | Always denied | (money, passwords, accounts) |

The calculator never uses `eval()`; it evaluates a whitelisted syntax tree with size limits.

## Test
```powershell
pytest              # unit tests + live Ollama tests (skipped if Ollama is off)
ruff check .        # lint
```
