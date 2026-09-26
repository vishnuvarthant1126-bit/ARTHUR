# Session 1 – Phases 0 to 4 (2026-09-26)

Result: a local AI assistant with a streaming web chat that remembers the conversation.
76 tests passing. Commits `e115e6c` → `7f076e9`.

---

## Phase 0 – Environment

**Concepts**
- **Terminal** – text window for commands (PowerShell on Windows).
- **Virtual environment (venv)** – a private package folder for this project only, so projects
  don't break each other. Activate with `.venv\Scripts\Activate.ps1` (macOS/Linux: `source .venv/bin/activate`).
- **pip** – installs packages; `requirements.txt` is the shopping list.
- **Git / GitHub** – Git saves snapshots (commits); GitHub stores a copy online.
- **Environment variables / `.env`** – settings and secrets outside the code. `.env` is never
  committed; `.env.example` is the public template.
- **Docker** – packages an app with everything it needs so it runs the same everywhere.
- **REST API** – programs talk over HTTP: method + URL + body → status code + body
  (200 OK, 422 invalid input, 500 server error).
- **JSON** – the data format: `{"message": "Hello Arthur"}`.

**Done**: installed Python 3.12 next to 3.14, created the folder structure, venv, requirements,
`.env.example`, `.gitignore`, README, downloaded `qwen3:8b` and `nomic-embed-text`, first commit.

**Still for you**: install Docker Desktop (needs admin + restart, only required from Phase 23);
create a private GitHub repo `arthur` and push:
```powershell
git remote add origin https://github.com/YOUR-USERNAME/arthur.git
git push -u origin main
```

---

## Phase 1 – Basic text ARTHUR

`You → POST /chat → FastAPI → validate → system prompt + message → LLMProvider → Ollama`

| File | Purpose |
|---|---|
| `app/main.py` | Starts the app; connects to the model at startup, closes at shutdown |
| `app/config/settings.py` | Reads `.env` with type checking |
| `app/llm/base.py` | The LLM interface: `generate`, `stream`, `generate_structured`, roles, error types |
| `app/llm/ollama.py` | Ollama implementation; clear errors ("Is it running?", "ollama pull …") |
| `app/llm/factory.py` | The only place that chooses the provider |
| `app/api/routes/chat.py` | `POST /chat`; rejects empty or too long messages (422) |
| `app/api/routes/health.py` | `GET /health`; is the model reachable? |
| `app/api/errors.py` | Same JSON error shape everywhere; no internal details leak |
| `app/api/middleware.py` | Request ID per request, safe against log injection |
| `app/observability/logging.py` | Structured key=value logs (JSON in production) |

Decisions: qwen3 "thinking" is off (`think: false`) for speed; tests use a fake model so they
are fast and repeatable.

---

## Phase 2 – Web interface

- **WebSocket** – a connection that stays open both ways, so words stream in as they are
  generated and "stop" can be sent mid-answer. Protocol documented in `app/api/websocket.py`.
- UI (`frontend/`): streaming, stop button, status light, typing dots, error bubbles, clear,
  auto-reconnect with backoff (1 s, 2 s, 4 s … 15 s), Markdown formatting, phone layout.
- **Security**: only ARTHUR's own page may connect (Origin check blocks other websites);
  model output is escaped before formatting, so it can never run code in the browser.
- Bugs found in the browser and fixed: welcome screen didn't hide; code blocks were squeezed.

---

## Phase 3 – LLM abstraction (completed)

- `OpenAICompatProvider` – one class for OpenAI, Groq, OpenRouter, LM Studio, …
- `RetryingProvider` – retries brief outages/rate limits with exponential backoff + jitter;
  bad API keys fail immediately (errors carry a `retryable` flag).
- `FallbackProvider` – tries a backup provider if the main one fails.
- Never retries after words were already shown (would duplicate text).
- API key stored as `SecretStr`, hidden from logs.

Cloud backup example for `.env`:
```
LLM_FALLBACK_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1
OPENAI_COMPAT_API_KEY=your-key-here
OPENAI_COMPAT_MODEL=gpt-4o-mini
```

---

## Phase 4 – Conversation memory

**Concepts**
- The model has **no memory of its own**; "memory" = re-sending earlier messages every turn.
- **Roles**: `system` (standing instructions), `user` (you), `assistant` (ARTHUR).
- **Tokens** – word pieces, ~4 characters each.
- **Context window** – how many tokens the model reads at once (set to 8,192). 1,024 are kept
  for the reply; the newest messages that fit fill the rest; the oldest drop out first.
- A random **session ID** links the browser to its conversation; stored in server RAM.

Files: `app/memory/short_term.py`, `app/agent/orchestrator.py`, `app/utils/tokens.py`.
Stopping keeps the partial answer; a failed answer saves nothing.

Verified in the browser: "My name is Vishnu…" → "What is my name?" → correct; reload restores
the conversation; Clear forgets it.

---

## Commands

```powershell
cd C:\Users\User\Projects\arthur
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload     # http://127.0.0.1:8000  ·  API docs: /docs
pytest                            # all tests
ruff check .                      # lint
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "running scripts is disabled" | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| `python --version` shows 3.14 | venv not active |
| `ModuleNotFoundError: app` | run uvicorn from the project folder |
| Port 8000 in use | `--port 8001` or close the other server |
| Page "Offline" | server not running |
| "Model offline" / 503 | start Ollama |
| 502 "not installed" | `ollama pull qwen3:8b` |
| First reply times out | model still loading into the GPU; resend |
| ARTHUR forgot everything | server restarted (memory is RAM-only until Phase 5) |
| Page changes not visible | Ctrl+F5 |

## Known limitations
- Conversation memory is lost on server restart.
- Token counts are estimates.
- OpenAI-compatible fallback tested only with simulated responses.

## Next: Session 2 – Phases 5–6
Long-term memory (embeddings, ChromaDB, SQLite, memory policy) and the tool system.
Start a new session in `C:\Users\User\Projects\arthur` and say **"Start session 2."**
