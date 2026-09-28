# ARTHUR – project guide for Claude

ARTHUR is a local-first personal AI agent (FastAPI + Ollama) built phase by phase as a
learning and portfolio project. The owner is a beginner/intermediate developer.

## How to work on this project
- **Pace: one session per day, relaxed.** The owner wants slow, steady, low-stress progress.
  Never rush or squeeze extra phases into a day on your own initiative. If a day's session runs
  long, stop at a clean point and continue the next day. Missing a day is fine – just continue
  with the next session. If the owner explicitly asks to do another session the same day, do it.
- **One session = the phases listed for it in the plan below. Do not continue past them.**
- Start each session with a short recap of the previous one (from `docs/sessions/`), then the plan for today.
- At the end of a session: write `docs/sessions/SESSION_NN.md`, update the table and
  "Progress notes" below, commit, make a backup bundle in `C:\Users\User\Documents\ARTHUR-backups\`, and stop.
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
- `app/main.py` – app factory; lifespan builds DB, LLM, embeddings, Chroma, MemoryManager,
  AuditLog, ToolRegistry, Orchestrator (all on `app.state`).
- `app/llm/` – `LLMProvider` interface (`generate`, `stream`, `generate_structured`);
  `OllamaProvider`, `OpenAICompatProvider`; `RetryingProvider`/`FallbackProvider` wrappers;
  `factory.py` is the only place that knows concrete classes.
- `app/agent/orchestrator.py` – per message: pending yes/no → remember → forget → recall
  memories → planner (if `looks_complex`) or ToolLoop; everything through `_supervise`
  (confirmations → PendingAction, honesty check). Emits `AgentEvent`s (`state.py`).
- `app/agent/executor.py` – `ToolLoop` (LLM ⇄ registry, step/time limits, repeat guard,
  `stop_reason`) and `PlanExecutor` (per-step ToolLoop, retry once, TaskState).
  `planner.py` – `looks_complex` + structured `Plan`. `verification.py` – action-claim check.
- Agent tools = `AGENT_TOOLS` (no save_memory). LLM layer: `generate(tools=)`, `stream_chat`
  → `TextDelta | ToolCallsRequested`; `Message` has `tool_calls`, `tool_call_id`, `name`.
- `app/memory/` – `short_term.py` (per-session RAM, PendingAction), `long_term.py` (SQLite repo),
  `vector_store.py` (Chroma + in-memory), `manager.py` (save/retrieve/search/delete, keeps both
  stores in step), `policy.py` (intent regexes, secret filter, extraction prompt).
- `app/rag/embeddings.py` – `EmbeddingProvider`, Ollama `nomic-embed-text` with task prefixes.
- `app/rag/` – `ingestion.py` (PDF/DOCX/TXT/MD/CSV → `Page`s, `DocumentError`), `chunking.py`
  (page-aware, overlap), `documents.py` (`DocumentService`: ingest/list_all/delete, SHA-256 dedupe,
  rollback), `retrieval.py` (`DocumentRetriever`, `DocumentHit.citation`). Chroma collection
  "documents" with metadata. Orchestrator auto-retrieves passages (≥ `RAG_MIN_SCORE`=0.58,
  measured by scripts/calibrate_rag.py) into the system prompt; tools document_search/list_documents.
- `app/api/middleware.py` – rejects non-GET requests whose Origin isn't ARTHUR (CSRF).
- Sample doc: `scripts/make_sample_handbook.py` → data/samples/student_handbook.pdf (has an
  injection line on p. 4 on purpose). It is uploaded on this machine.
- Never name a method `list` in a class that uses `list[...]` annotations (hit twice).
- `app/tools/` – `Tool` base (name, description, input_model, permission_level, timeout, run),
  `ToolRegistry.execute` (lookup → permission → validate → confirm → timeout → audit),
  tools: calculator (AST, no eval), current_time, weather (Open-Meteo), memory tools.
- `app/security/` – `PermissionPolicy` (level ≥2 always confirms, 3 always denied), `AuditLog`.
- `app/api/` – chat, memories, tools, audit, health, `WS /ws`; JSON errors; request IDs.
- `frontend/` – plain HTML/CSS/JS, no external deps; safe Markdown; Memory panel.
- Tests use `FakeLLM`/`FakeEmbeddings`/in-memory DB (tests/conftest.py); never the real lifespan.
  `FakeLLM(script=[...])`: each chat turn pops a str (answer), list of `tool_call(...)`, or an
  Exception to raise; `structured_reply` feeds `generate_structured` (plans, memory drafts).
- Commit messages: PowerShell here-strings silently failed once (quotes/% in text) – prefer
  `git commit -F -` with a bash heredoc, and check `git log` afterwards.
- qwen3 runs with `think: false`. `MEMORY_MIN_SCORE=0.55` was measured (scripts/calibrate_memory.py).
- Port 8000 may be occupied by an unrelated Python 3.14 process on this machine; use 8001 if so.

## Session plan
One session per day. Dates are a guide, not a deadline – if a day is skipped, everything shifts.

| Session | Planned day | Phases | Status |
|---|---|---|---|
| 1 | Sat 2026-09-26 | 0–4: setup, chat API, web UI, LLM abstraction, conversation memory | ✅ Done |
| 2 | Sat 2026-09-26 | 5–6: long-term memory (SQLite + ChromaDB + embeddings, memory policy), tool system + registry | ✅ Done (same day, at the owner's request) |
| 3 | Mon 2026-09-28 | 7–8: agent loop (tool calling), planner/executor with step limits | ✅ Done |
| 4 | Mon 2026-09-28 / Tue 2026-09-29 | 9–10: document RAG with citations, web search | 🟡 Phase 9 ✅ (extra session, owner's request) · Phase 10 ⏭ Next |
| 5 | Wed 2026-09-30 | 11–13: speech-to-text, text-to-speech, wake word | |
| 6 | Thu 2026-10-01 | 14–15: restricted file tools, Playwright browser agent | |
| 7 | Fri 2026-10-02 | 16–17: controlled computer use, vision | |
| 8 | Sat 2026-10-03 | 18–19: scheduler/reminders, full security system | |
| 9 | Sun 2026-10-04 | 20–22: observability (Prometheus/Grafana), test suite, Locust load tests | |
| 10 | Mon 2026-10-05 | 23–24: Docker Compose, performance (Docker Desktop must be installed first) | |
| 11 | Tue 2026-10-06 | 25–27: futuristic UI, multimodal input, advanced agent features | |
| 12 | Wed 2026-10-07 | 28–29: failure handling, final demo, full README/CONTRIBUTING/LICENSE | |

## Docs
- `docs/ARCHITECTURE.md` – full architecture, example flow, stack, hardware, design decisions.
- `docs/sessions/SESSION_NN.md` – per-session learning notes. Write one at the end of every session.

## Progress notes
- Session 1: 76 tests passing. Verified in browser: streaming, stop, reconnect, memory
  ("What is my name?" → "Vishnu"), history restored on reload, clear forgets.
- Session 2: 157 tests passing. Verified live: remember → clear → recalled; password refused;
  forget asks yes/no; memory survives restart; calculator/time/weather/confirmation/audit via API.
- Session 3: 209 tests passing. Verified live: tool choice (calculator/weather/time, none for
  small talk), model-driven delete stopped by confirmation, 3-step plans with correct answers,
  stop mid-plan in 0.2 s. Found & fixed: hallucinated "I deleted it" (honesty check),
  LaTeX output, WS type overwrite, id-only confirmation text, "15% of" in calculator.
- Known limitations: conversation memory is RAM-only; token counts are estimates (chars/4);
  OpenAI-compatible fallback only tested with mocks; "remember" detection is regex-based;
  qwen3:8b sometimes skips the calculator inside plan steps; planned answers ~10–15 s.
- Session 4 part 1 (Phase 9, same day as session 3 at owner's request): 241 tests. Verified live
  with the sample handbook: cited answers, injection ignored, "not in documents" only after a
  real search (fixed: it used to claim that without searching → automatic retrieval).
- Phase 10 notes: `web_search` tool (ddgs, no key) returning title/url/snippet; rules for when
  to search in the prompt; add to AGENT_TOOLS and planner menu; results are untrusted data
  (prompt injection); timeouts, retries, rate limiting; cite URLs.
