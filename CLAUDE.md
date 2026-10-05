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
- Docker Desktop 29.8 installed (2 Oct 2026). `docker compose up -d --build` runs ARTHUR on
  :8000 with `restart: unless-stopped` – it then BLOCKS port 8000 for uvicorn; check
  `docker ps` and `docker compose down` before starting the dev server. `.env` has TZ.
- GitHub remote not set up yet (user must create the repo and push).

## Commands
```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload        # UI at http://127.0.0.1:8000, docs at /docs
pytest                               # unit + security + live tests (skipped if Ollama is off)
pytest --cov                         # coverage gate 88 % (see docs/TESTING.md)
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
- `app/api/middleware.py` – front door, in order: Host allow-list (421) → Origin check (403) →
  rate limit (429) → security headers + CSP (not on /docs). `app.state.allowed_hosts` and
  `app.state.rate_limiter` are set in `create_app`; tests add hosts "test"/"testserver".
- `app/security/` – `network.py` (`host_allowed`, `resolve_public` → `Target`, `is_public`),
  `egress_proxy.py` (`EgressProxy`: all Chromium traffic, lookup+check+pin, CONNECT and plain
  http), `rate_limit.py` (sliding window, `group_for`), `audit.py` (`redact` + `scrub_text`).
  `webpage.pinned_request` + `new_page_client()` (no keep-alive: pinned pools only see IPs).
  `BrowserAgent(resolver=...)` is the single test hook (fake (scheme, host, port) → Target).
  `policy.is_yes` = whole short message; `PendingAction.expired` (5 min). docs/SECURITY.md.
- `app/scheduler/` – `when.py` (`parse_when(text, now)` → (moment, Repeat); `describe`;
  `next_occurrence`), `reminders.py` (`ReminderService`, SQLite `reminders` table in UTC,
  injectable `clock`), `runner.py` (`ReminderScheduler.tick/poke`, `NotificationHub`; delivered
  only if a tab received it). Tools set_reminder (1), list_reminders (0), cancel_reminder (2,
  by words). `/reminders` API; WS pushes `{"type": "reminder"}`; Reminders panel.
- `app/search/` – `SearchProvider` (DuckDuckGo via ddgs / SearXNG), `WebSearchService` (cache,
  rate limit, retry, dedupe), `webpage.py` (`fetch_page` + `check_public_url` SSRF guard,
  `html_to_text`). Tools `web_search`, `read_webpage` (level 0, in AGENT_TOOLS). Tests use
  `FakeSearchProvider` and public IP literals (93.184.215.14) so no network/DNS is needed.
- Frontend renders `[text](https://...)` as safe links; `<ol start=N>` keeps numbering.
- Sample doc: `scripts/make_sample_handbook.py` → data/samples/student_handbook.pdf (has an
  injection line on p. 4 on purpose). It is uploaded on this machine.
- Never name a method `list` in a class that uses `list[...]` annotations (hit twice).
- `app/files/workspace.py` – `Workspace` sandbox (roots from ALLOWED_DIRECTORIES, resolve then
  check, blocked system/hidden/secret names); tools find_files/list_folder/read_file (0),
  save_file (2). Tests pass `system_roots=[SYSTEMROOT]` because pytest tmp is under AppData.
- `app/browser/` – `agent.py` `BrowserAgent` (Playwright in own thread + ProactorEventLoop,
  `_guard` route checks every request, snapshot numbers elements via `data-arthur-id`,
  injectable `url_checker`), `risk.py` (`click_level`, `type_level(text=)`). Tools
  browser_open/find_text (0), browser_click/type (1, raised per call via `Tool.required_level`).
  Tool results are cut at 4000 chars (executor `max_result_chars`) – `describe()` lists elements
  first. Tests use a local http.server + a fake checker that allows only it.
- Honesty checks (`verification.py`): `claims_action` and `fakes_permission_request`;
  orchestrator `_for_history` stores permission questions as a neutral note.
- `app/computer/` – `apps.py` (pure per-app rules: `WindowInfo`, `AppSpec.matches/problem`,
  key allow-lists), `desktop.py` (`DesktopController`: pywinauto UIA on ONE COM worker thread,
  own Notepad tabs tracked by UIA runtime_id, preferred window, PrintWindow screenshots,
  `workspace_rules`), `risk.py` (key/text levels). **NO KEYSTROKES, EVER** (send_keys leaked
  into the Claude chat in session 9): Notepad = Windows messages to the edit control's handle
  (`NOTEPAD_KEYS`, EM_REPLACESEL...), Calculator = UIA button invokes (`CALCULATOR_BUTTONS`),
  Explorer = shell COM (f5/alt+up/delete to Recycle Bin, `selection()`). A test forbids
  send_keys/pyautogui in app/. Tools open_app/read_window/click_control/type_text/
  press_key(shortcut). Tests use a FakeDesktop; real desktop only with ARTHUR_DESKTOP_TESTS=1
  (no keystrokes, safe while the owner works). Never run raw input experiments on the live desktop.
- `app/observability/metrics.py` – `Metrics` (own Prometheus registry on `app.state.metrics`;
  labels = route templates / registered tool names only), `summary()` for /dashboard.html;
  `app/llm/metered.py` `MeteredProvider`; `/metrics`, `/metrics/summary`; middleware counts
  requests (also crashes as 500) and refusals. `deploy/` (Prometheus, Grafana, compose) was
  verified in Docker; `scripts/make_grafana_dashboard.py` regenerates the dashboard.
- `app/database/database.py` – ONE process-wide RLock around every session (SQLite writer
  starvation → "database is locked" under load). `app/llm/echo.py` (LLM_PROVIDER=echo) and
  `HashEmbeddings` (EMBEDDING_PROVIDER=hash) for load tests: `scripts/run_load_server.py`
  (:8001, temp data) + `tests/load/locustfile.py`. docs/TESTING.md, docs/LOAD_TESTS.md.
- `app/vision/provider.py` – `OllamaVision` (qwen2.5vl:7b, keep_alive 2m), `prepare_image`.
  Tools describe_image (0), look_at_screen (1 window / 2 whole screen); `POST /vision/describe`.
  `FakeVision` in tests/conftest.py (also on the test app's state).
- Performance (Phase 24, docs/PERFORMANCE.md): the system prompt is STATIC (prompt cache);
  memories/passages go in `prompts.context_block` with the newest user message – never put
  changing text in the system prompt. `Orchestrator.tools_tokens` is part of the budget,
  `Conversation.window` trims in steps, `Orchestrator.warm_up` (LLM_WARM_UP) loads chat model
  then embeddings. `StreamStats` → `MeteredProvider` (prompt tokens/read/load/speed).
  Always `127.0.0.1`, not `localhost` (`app/utils/net.py`). Profile with
  `scripts/profile_chat.py` against a server WITHOUT `--reload` (port 8002).
- Docker (Phase 23, docs/DOCKER.md, verified; browser build + SearXNG NOT run): `docker/Dockerfile` (copies only app/ +
  frontend/, non-root), root `docker-compose.yml` (arthur + prometheus + grafana, searxng
  profile; ports 127.0.0.1 only; ALLOWED_HOSTS=arthur; data in volume `arthur-data`; Ollama
  on the host), `deploy/prometheus/prometheus.yml` (container) / `prometheus.host.yml`
  (monitoring-only compose). `tests/unit/test_docker_files.py` enforces these statically –
  new Compose env names must be real Settings fields; no top-level Windows-only imports.
- `prompts.system_prompt(tool_names)` → `Orchestrator.system_prompt` (built once): parts of
  SYSTEM_PROMPT about missing features (`OPTIONAL_PARTS`: desktop, browser, screen) are
  REPLACED by "not available" text – appending a note did not work with qwen3. Editing those
  prompt parts means editing the matching constant (a test checks they still match).
  `EMPTY_ANSWER` replaces a silent model reply. `av<19` is pinned (faster-whisper 1.2).
- Status rail (Phase 25): `GET /status` (`app/api/routes/system.py`, ok/off/problem per part,
  own timeout each); frontend `renderLamps(kind, label)` is called from `setStatus` - the
  lamps and the pill can't disagree; `hud*` functions mirror plan/tool events; `loadSystem`.
- Attachments (Phase 26): `app/agent/attachments.py` (`AttachmentStore` on app.state, RAM,
  30 min; `attachment_section`, `history_note`), `POST /attachments`; chat/WS take
  `attachments: [ids]`; orchestrator `_look_at` = vision first (as a describe_image event),
  documents = whole pages ≤ 8,000 chars, no auto-RAG then; no planner with attachments.
  Orchestrator(vision=...). Frontend: `attached`, `addFiles`, paste/drop handlers.
- Agent (Phase 27, docs/AGENT.md): `Tool.parallel_safe` (read-only AND no shared state;
  never browser/desktop/vision) → `ToolLoop` gathers; `ToolContext.sources` = every tool
  result shown → `verification.unverified_sources` + `source_note`; history notes:
  `Conversation.summary/summarized_until`, `_compress_later` waits
  HISTORY_SUMMARY_DELAY_SECONDS (15) and is cancelled by a new message (writing at once
  cost +2–4 s on the next two answers); `policy.prioritize` (ranked, dated, ≤1,200 chars).
  Experiments that chat with ARTHUR must use a TEMP data dir (DATABASE_PATH,
  VECTOR_STORE_PATH, DOCUMENTS_PATH) - a test once saved junk into the real memory.
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
| 4 | Mon 2026-09-28 | 9–10: document RAG with citations, web search | ✅ Done (same day as session 3, owner's request) |
| 5 | Tue 2026-09-29 | 11–13: speech-to-text, text-to-speech, wake word | ✅ Done (owner verified mic + spoken replies in Chrome) |
| 6 | Wed 2026-09-30 | 14–15: restricted file tools, Playwright browser agent | ✅ Done |
| 7 | Thu 2026-10-01 | 16–17: controlled computer use, vision | ✅ Done (Wed 2026-09-30, same day as session 6, owner's request) |
| 8 | Fri 2026-10-02 | 18–19: scheduler/reminders, full security system | ✅ Done (Thu 2026-10-01) |
| 9 | Sat 2026-10-03 | 20–22: observability (Prometheus/Grafana), test suite, Locust load tests | ✅ Done (Thu 2026-10-01, same day as session 8, owner's request) |
| 10 | Sun 2026-10-04 | 23–24: Docker Compose, performance | ✅ Done (Fri 2026-10-02) |
| 10b | Fri 2026-10-02 | first real Docker run (checklist in docs/DOCKER.md) | ✅ Done (same day, after the owner installed Docker) |
| 11 | Mon 2026-10-05 | 25–27: futuristic UI, multimodal input, advanced agent features | ✅ Done |
| 12 | Tue 2026-10-06 | 28–29: failure handling, final demo, full README/CONTRIBUTING/LICENSE | ⏭ Next |

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
- Session 4 part 2 (Phase 10, same day): 274 tests. Verified live: search only when needed,
  cited links, Singapore AI conferences flagship flow (plan → 2 searches → summary, 21 s),
  read_webpage works and blocks 127.0.0.1/192.168.x/file://. Snippets can be stale (3.14.6 vs
  3.14.7) – model flagged disagreement. Known gap: DNS rebinding (fix in Phase 19).
- Session 5 so far (Phases 11–12, commits 0e84f10, 116ad03): 313 tests. `app/voice/`:
  `speech_to_text.py` (WhisperSTT small/CPU/int8 ~1.5 s per sentence, VAD, confidence,
  CUDA→CPU runtime fallback since cublas64_12.dll is missing) and `text_to_speech.py`
  (PiperTTS, voices in data/voices: en_GB-alan-medium default, en_US-lessac-medium;
  `prepare_for_speech`). Routes /voice/transcribe, /voice/speak, /voice/voices. Frontend:
  mic button, sentence-streamed speech queue, Voice panel. Browser pane BLOCKS the mic –
  real mic tests need Chrome/Edge. Tests: Piper speaks → Whisper transcribes (no mic needed);
  tests/integration/test_startup.py runs the real lifespan on temp storage.
  Fixed: logging crash on cp1252 console, stale WHISPER_DEVICE=auto in .env, startup
  create_task(gather) crash.
- Session 5 done (Phase 13, commit b624d13): 338 tests. `app/voice/wake_word.py`
  (`detect_wake_phrase`: greeting+name or name first, SequenceMatcher ≥ 0.8, "offer" only after
  a greeting), `POST /voice/wake` (app.state.wake_stt = base.en; commands re-read with small).
  Frontend: AudioWorklet tap + energy VAD, `handleClip`, `sayYes`, `idleStatus`. Measured
  19/20 detected, 0/32 false alarms (scripts/evaluate_wake_word.py); Whisper hotwords made it
  worse (12/20) – don't use. `OLLAMA_KEEP_ALIVE=30m` fixes ~10 s reload after 5 min idle.
- Session 6 done (Phases 14–15, commits ce500a8, 0b1725e): 404 tests. Verified live: latest
  resume found + summarised, save asked (no → nothing, yes → saved), .env outside roots refused;
  python.org release read, Donate click asked, "no" cancelled, PyPI username asked, password in
  wrong field (search box) now asks, bot check stopped. Fixed: elements cut by 4000-char result
  limit, model-written fake "Reply yes" prompts, typing into links. Live tests: restart the
  server after edits (reload takes ~60 s and the old worker keeps answering). Avoid PowerShell
  Set-Content utf8 (adds BOM). Sample files (fictional) in Documents\ARTHUR\samples.
- Session 7 done (Phases 16–17, commits 74bcfc1, 4faab88): 440 tests (+2 opt-in:
  ARTHUR_DESKTOP_TESTS=1, ARTHUR_VISION_TESTS=1). Verified live: Notepad list typed after yes,
  Calculator 348×27 via app + read_window, Explorer list/select/delete asked ("no" kept files),
  Win+R/password/System32 refused, receipt picture read (SGD 11.20), Calculator screenshot
  "dark theme", whole-screen asked. qwen3 keeps copying permission messages from history →
  history stores a note + both forms flagged. Vision swap: 29 s first (10 s cached), qwen3
  reload ~10 s. The owner's Notepad has ~20 personal tabs incl. .env – never read them.
- Session 8 done (Phases 18–19, commits f544067, 615982a): 618 tests (101 in tests/security).
  Verified live: reminder in 1 min delivered after 4 s, one due while ARTHUR was off arrived
  after restart, cancel by words; Host evil.example → 421, cross-site → 403, flood → 429, page
  fine under CSP, python.org via proxy 2 s, localtest.me refused, "ok, what is this?" confirms
  nothing. Found: is_yes prefix bug, pooled connection reuse across names with pinning, model
  cancelling the wrong reminder (renumbered list). pip-audit: oauthlib pinned >=4.0.0; chromadb
  server advisories don't apply (embedded only) – re-run the audit when Chroma updates.
  Don't use patch scripts with backslashes in bash heredocs (escaping broke repeatedly) –
  use the Edit tool or write the script to a file.
- Session 9 done (Phases 20–22, commits f136bb8, fa1ccb5, ed9ea4a): 671 tests, coverage 90 %
  (gate 88; desktop.py 24 % normally, 89 % with opt-in). Measured: 200 users 155 req/s p95
  120 ms; ceiling ~400–450 req/s; real model 1.0/1.3/2.0 s per answer at 1/2/4 concurrent
  (GPU is the limit). Found: keystroke leak (redesigned, see app/computer), SQLite lock
  starvation, 500s missing from metrics, rich tracebacks printing locals, save_file asking
  for impossible saves, reports folder never created, SearXNG limit order. The flaky voice
  test was NOT reproduced (10 full + 80 single runs); its input is now deterministic and it
  reports what it heard. Kill a background server with netstat + taskkill //PID.
- Session 10 done (Phases 24 + 23, commits 5541e2f, 58239f3): 715 tests, coverage 91 %.
  Measured: first message 11–13 s → 0.6 s, small talk 0.4–0.8 s, tool question 1.2 s, long
  chats 3.6 → 1.6 s. Found: localhost IPv6 penalty (2 s per connection), context in the
  system prompt breaking the cache, tool schemas missing from the budget, planner false
  alarms on long prose; slim_schema gave NO gain with Ollama (said so). The real flaky test
  was the duplicate-upload test (PDF creation time to the second), not the voice test.
  Docker: nothing built or started; image tags checked on Docker Hub only.
- Session 10b done (first Docker run): 718 tests. Verified live in containers: build 1.51 GB,
  Ollama reached via host.docker.internal (Ollama on 127.0.0.1), chat/tools/search, memory
  across rebuilds, save_file → Windows folder, reminders +08, voice, 421/403, LAN refused,
  Prometheus UP, Grafana panels with data, monitoring-only compose, speed = Windows.
  Found: PyAV 19 broke transcription (unpinned deps → `av<19`), blank answer for missing
  tools (prompt now built from available tools + EMPTY_ANSWER). Open: browser build,
  SearXNG, a lock file, red Uptime tile in Grafana. /chat session ids need 8+ characters.
- Session 11 done (Phases 25–27, commits 915eda7, a023e87, a8d0814): 767 tests, coverage
  91 %. Verified live: lamps + task + system rail, phone overlay; screenshot of a
  ModuleNotFoundError diagnosed (34 s cold, 21 s vision load); handbook PDF explained, page-4
  injection ignored; weather in two cities ran in parallel; source check quiet on handbook,
  python.org and weather answers (after the named-front-page rule); recall test 0/2 → 2/2.
  Fixed: "Note 3 about…" saved as memory; 3 junk test memories deleted (owner's memory
  untouched). Model sometimes cites "(Page 2)" instead of the exact form.
- Phase 28–29 notes: failure handling list in the spec - LLM/internet unavailable, tool
  failure, invalid args, timeout, bad document, unsupported file, mic unavailable, TTS
  failure, database failure, model failure; much exists (classify_llm_error, ToolError,
  DocumentError, micError) - make a table like docs/AGENT.md and test each with fakes
  (Ollama off, DB locked, etc.). Final demo: a scripted walkthrough (docs/DEMO.md);
  CONTRIBUTING, LICENSE (ask the owner which licence), README rewrite. GitHub remote
  still not set up (owner).
