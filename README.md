# ARTHUR – Personal Multimodal AI Agent

A modular, local-first AI assistant: voice + text, tools, memory, RAG,
planning, browser/computer control, with a permission-based security model.

> Status: Phase 19 – reminders that survive restarts ("remind me at 5pm…"); a reviewed
> security model with a test for every defence ([docs/SECURITY.md](docs/SECURITY.md));
> uses Notepad, Calculator and File Explorer (every click/keystroke
> confirmed by you); looks at pictures and app windows with a local vision model;
> finds, reads and (with your OK) saves files in folders you allow; browses
> the web in its own isolated browser, asking before risky clicks; talk to it (speech-to-text),
> hear it (text-to-speech), call it hands-free ("Hey Arthur"); searches the web with sources and answers from your documents with page
> citations; an agent that chooses tools itself and plans multi-step tasks, with
> conversation + long-term semantic memory, swappable LLM providers, and a permission-checked,
> audited tool system. Full documentation arrives as features land.

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
| 2 | Needs confirmation | Returns a preview until confirmed | `delete_memory`, `save_file`, risky browser clicks |
| 3 | Highly sensitive | Always denied | payments, password/card fields |

Some tools raise their level per call (`required_level`): a browser click on "Search" runs,
"Buy now" asks, "Pay" is denied.

The calculator never uses `eval()`; it evaluates a whitelisted syntax tree with size limits.

## Documents (RAG)
```
upload (PDF/DOCX/TXT/MD/CSV, ≤ 20 MB) ─► extract per page ─► clean ─► chunk (1000 chars, 150 overlap)
       ─► embed ─► ChromaDB "documents" (+ file name, page)   +   SQLite record   +   file on disk
every question ─► embed ─► passages with similarity ≥ RAG_MIN_SCORE (0.58) ─► added to the prompt
               ─► answer with citations like [handbook.pdf, p. 2]
```
Endpoints: `POST /documents` (multipart), `GET /documents`, `GET /documents/search?q=`,
`DELETE /documents/{id}`. Tools: `document_search`, `list_documents` (level 0).
Document text is treated as data; uploads from other websites are rejected (CSRF check).

## Web search
`web_search` (DuckDuckGo via `ddgs`, no key; or self-hosted SearXNG) with a 10-minute cache,
10 searches/minute limit and one retry. `read_webpage` reads one public page with SSRF
protection: http(s) only, every hop resolved and checked against private/local/reserved
addresses, 2 MB and text/HTML only. Results are untrusted data; answers cite Markdown links.

## Voice (all local)
| Part | How | Speed (CPU) |
|---|---|---|
| Speech-to-text | faster-whisper `small`, VAD, confidence check – `POST /voice/transcribe` | ~1.5 s / sentence |
| Text-to-speech | Piper voices in `data/voices` – `POST /voice/speak`, `GET /voice/voices` | ~0.3 s / sentence; first sound ~0.25 s after the first sentence |
| Wake word | browser loudness detection → clips → Whisper `base.en` → "Hey/OK Arthur" at the start – `POST /voice/wake` | ~0.6 s / clip |

Wake word, measured with `scripts/evaluate_wake_word.py`: 19/20 detected, 0/32 false alarms.
Clips are never stored; hands-free mode is off by default. Use Chrome or Edge – embedded
browser views (like the Claude app's) block the microphone.

## Files (only folders you allow)
`ALLOWED_DIRECTORIES` in `.env` (semicolon-separated; blank = `Documents\ARTHUR`). Paths are
resolved before checking (no `..`, junction, `file://` or network-path tricks). System folders,
AppData, hidden files and secret-looking files (`.env`, keys, `*password*`…) are always blocked.
Tools: `find_files`, `list_folder`, `read_file` (level 0), `save_file` (level 2: asks first,
.md/.txt/.csv only, never overwrites). `python scripts/make_sample_files.py` makes fictional samples.

## Browser agent (Playwright)
`python -m playwright install chromium` once. ARTHUR's own Chromium: fresh profile, no downloads,
every request checked against private/local addresses. Pages come back as text plus numbered
elements. Tools: `browser_open`, `browser_find_text` (level 0), `browser_click`,
`browser_type` (level raised per action by `app/browser/risk.py`):
links and search boxes run; buy/submit/send/sign in/other fields ask; pay/transfer, password
and card fields, card numbers are refused. CAPTCHAs are never bypassed.
`BROWSER_HEADLESS=false` shows the window.

## Computer use (Windows)
`COMPUTER_ALLOWED_APPS=notepad;calculator;explorer`. ARTHUR reads windows through Windows UI
Automation as numbered controls (no pixel guessing; buttons are "invoked", the mouse never moves).
`open_app` (1) and `read_window` (0) run; `click_control`, `type_text`, `press_key` (2) **ask every
time**; Win-key shortcuts, Ctrl+Alt+Del, Alt+F4, Shift+Delete, the clipboard, passwords and card
numbers are refused. Notepad: only the tab ARTHUR opened itself. Explorer: only while every tab
shows an allowed folder; items can be selected, never opened. Rules are re-checked before every
action; typing stops if the focus or Notepad tab changes, or you press Stop.
`COMPUTER_USE_ENABLED=false` turns it all off.

## Vision
`ollama pull qwen2.5vl:7b` (~6 GB). `describe_image` (0) looks at a picture in your allowed
folders; `look_at_screen` (1) at one allowed app's window, or (2, asks first) the whole screen.
`POST /vision/describe` takes an uploaded image. On an 8 GB GPU Ollama swaps qwen3 and the vision
model: ~30 s per picture question. Text in images is treated as data, never instructions.

## Reminders
"Remind me at 5pm to call mum", "in 20 minutes", "every weekday at 8am". The model passes your
time wording unchanged and `app/scheduler/when.py` (plain Python) calculates the moment.
Reminders are stored in SQLite, checked every 5 s, and pushed to the open chat as a ⏰ message
(with a chime, and speech if voice is on). One that was due while ARTHUR was off is delivered
when you return. Reminders only notify – they never run tools. Tools: `set_reminder` (1),
`list_reminders` (0), `cancel_reminder` (2). API: `GET/POST/DELETE /reminders`; Reminders panel.

## Security
Full threat → defence → test table: [docs/SECURITY.md](docs/SECURITY.md). In short:
- **Incoming:** Host allow-list (localhost only – stops DNS rebinding), Origin check (CSRF),
  rate limits, Content-Security-Policy and other security headers.
- **Outgoing:** public addresses only, and the *checked* address is the one used: `read_webpage`
  pins it; the browser sends everything through ARTHUR's own egress proxy.
- **Agent:** tools run only through the registry (levels 0–3); a "yes" must be a short, pure
  confirmation and expires after 5 minutes; injected text can't approve anything.
- **Secrets:** never stored or typed; masked in the audit log by key and by value.
- `pytest tests/security` (101 tests) and `python -m pip_audit`.
- ARTHUR has no login and listens on 127.0.0.1 only – don't expose it to a network.

## Agent loop and planner
```
message ─► memory intents ("remember…", "forget…")? ─► handled directly
        └► looks multi-part? ── yes ─► Planner (structured Plan, 2–6 steps)
                │                        └► PlanExecutor: per step a bounded ToolLoop,
                │                           1 retry, failures recorded, confirmation pauses
                │                        └► final answer written only from step results
                └─ no ─► ToolLoop: LLM ⇄ tools until it answers (≤ 8 rounds, ≤ 120 s)
every reply ─► honesty check: claims an action no tool performed? → visible correction
              or writes its own "Reply yes" question? → note that nothing is waiting
```
Level-2 tools stop the loop and ask the user; the model can never confirm on its own.
`save_memory` is not offered to the agent (only an explicit "remember…" saves).

## Test
```powershell
pytest              # unit tests + live Ollama tests (skipped if Ollama is off)
ruff check .        # lint
```
