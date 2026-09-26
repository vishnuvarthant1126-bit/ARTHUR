# ARTHUR – project guide for Claude

ARTHUR is a local-first personal AI agent (FastAPI + Ollama) built phase by phase as a
learning and portfolio project. The owner is a beginner/intermediate developer.

## How to work on this project
- **One session = the phases listed for it in the plan below. Do not continue past them.**
  At the end of a session: update "Progress" below, commit, and stop.
- Explain every new concept in simple language before implementing it.
- At the start of each phase show: CURRENT PHASE, OBJECTIVE, WHAT WE ARE BUILDING,
  FILES, DEPENDENCIES, EXPECTED RESULT. At the end: WHAT WE BUILT, HOW TO RUN,
  HOW TO TEST, KNOWN LIMITATIONS, NEXT PHASE.
- Test every phase for real (pytest + run the server + try it in the browser) before committing.
- One meaningful commit per phase or phase group (`feat: ...`), ending with the Co-Authored-By line.
- Never commit `.env`, `data/` contents, keys or personal information.
- Safety rules are non-negotiable: the LLM never executes anything directly; every tool goes
  through permission levels (0 read-only … 3 highly sensitive), confirmation for level ≥ 2,
  audit logging, and no arbitrary shell commands.

## Environment
- Windows 11, PowerShell. Project: `C:\Users\User\Projects\arthur`.
- Python 3.12 venv at `.venv` (system default Python is 3.14 – don't use it; ML wheels lag).
- Ollama with `qwen3:8b` (chat) and `nomic-embed-text` (embeddings). RTX 5060 Laptop, 8 GB VRAM, 16 GB RAM.
- Docker Desktop NOT installed yet (user must install; needed from Phase 23).
- GitHub remote not set up yet (user must create the repo and push).

## Commands
```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload        # UI at http://127.0.0.1:8000, docs at /docs
pytest                               # unit + live Ollama tests (skipped if Ollama is off)
ruff check . ; ruff format --check .
```

## Architecture (current)
- `app/main.py` – app factory, lifespan builds LLM provider + Orchestrator.
- `app/llm/` – `LLMProvider` interface (`generate`, `stream`, `generate_structured`);
  `OllamaProvider`, `OpenAICompatProvider`; `RetryingProvider`/`FallbackProvider` wrappers;
  `factory.py` is the only place that knows concrete classes.
- `app/agent/orchestrator.py` – builds system prompt + trimmed history + user message; saves exchanges.
- `app/memory/short_term.py` – per-session conversation in RAM (token-budget trimming, TTL, LRU).
- `app/api/` – `POST /chat`, `DELETE /chat/{id}`, `GET /health`, `WS /ws` (streaming, stop,
  clear, session history; Origin check), JSON error format, request-ID middleware.
- `frontend/` – plain HTML/CSS/JS, no external dependencies; safe Markdown rendering.
- Tests use `FakeLLM` (tests/conftest.py); don't run the real lifespan in tests.
- qwen3 runs with `think: false`.

## Session plan
| Session | Phases | Status |
|---|---|---|
| 1 | 0–4: setup, chat API, web UI, LLM abstraction, conversation memory | ✅ Done (2026-09-26) |
| 2 | 5–6: long-term memory (SQLite + ChromaDB + embeddings, memory policy), tool system + registry | ⏭ Next |
| 3 | 7–8: agent loop (tool calling), planner/executor with step limits | |
| 4 | 9–10: document RAG with citations, web search | |
| 5 | 11–13: speech-to-text, text-to-speech, wake word | |
| 6 | 14–15: restricted file tools, Playwright browser agent | |
| 7 | 16–17: controlled computer use, vision | |
| 8 | 18–19: scheduler/reminders, full security system | |
| 9 | 20–22: observability (Prometheus/Grafana), test suite, Locust load tests | |
| 10 | 23–24: Docker Compose, performance | |
| 11 | 25–27: futuristic UI, multimodal input, advanced agent features | |
| 12 | 28–29: failure handling, final demo, full README/CONTRIBUTING/LICENSE | |

## Progress notes
- Session 1: 76 tests passing. Verified in browser: streaming, stop, reconnect, memory
  ("What is my name?" → "Vishnu"), history restored on reload, clear forgets.
- Known limitations: conversation memory is RAM-only; token counts are estimates (chars/4);
  OpenAI-compatible fallback only tested with mocks.
