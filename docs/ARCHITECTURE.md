# ARTHUR Architecture

## System overview

```
┌──────────────────────────────────────────────────────────────────┐
│  INTERFACES      Web UI (text) · Microphone · Speaker · Uploads  │
└───────────────┬──────────────────────────────────▲───────────────┘
                │ HTTP / WebSocket                 │ text + audio
┌───────────────▼──────────────────────────────────┴───────────────┐
│  API LAYER (FastAPI)   /chat  /ws  /voice/*  /documents  /memory │
│  request IDs · validation (Pydantic) · error handling · metrics  │
└───────────────┬──────────────────────────────────────────────────┘
┌───────────────▼──────────────────────────────────────────────────┐
│  INPUT PROCESSING   STT (faster-whisper) · file parsing · images │
└───────────────┬──────────────────────────────────────────────────┘
┌───────────────▼──────────────────────────────────────────────────┐
│  AGENT / ORCHESTRATOR  (our own code)                            │
│   Context builder → Planner (steps) → Executor (loop, max N)     │
│   → Responder (final answer + sources)                           │
└────────┬─────────────────────────────┬───────────────────────────┘
         │                             │ every tool call passes through ↓
┌────────▼────────┐   ┌────────────────▼─────────────────────────────┐
│ MEMORY          │   │ SECURITY GATE  permission level · allow/deny │
│ short-term      │   │ confirmation · rate limit · audit log        │
│ long-term (SQL) │   └────────────────┬─────────────────────────────┘
│ semantic (vec)  │   ┌────────────────▼─────────────────────────────┐
└────────┬────────┘   │ TOOL REGISTRY                                │
         │            │ calculator · time · web_search · file_search │
┌────────▼────────┐   │ document_search(RAG) · tasks · browser       │
│ RAG             │   │ computer · vision · custom tools             │
│ ingest→chunk→   │   └──────────────────────────────────────────────┘
│ embed→retrieve  │
└─────────────────┘   ┌──────────────────────────────────────────────┐
                      │ LLM LAYER (interface) → Ollama | OpenAI-     │
                      │ compatible | others. Chosen by .env setting. │
                      └──────────────────────────────────────────────┘
 Cross-cutting: config (settings.py) · logging · Prometheus metrics · SQLite
```

## Components in plain language

| Component | What it is | Analogy |
|---|---|---|
| Interface | Web page, microphone, speaker | Face, ears and mouth |
| API layer (FastAPI) | Web server that receives requests and returns answers | Front desk |
| Input processing | Turns audio into text and files into readable text | Translator |
| Orchestrator | Decides: answer directly, use a tool, or make a plan | Manager |
| LLM | Reads and writes text. Knows nothing current, cannot act by itself | Brain with no hands |
| LLM abstraction | One interface (`generate`, `stream`, `generate_structured`), one class per provider | Universal power adapter |
| Planner | Splits big requests into ordered steps | To-do list writer |
| Executor | Runs steps, calls tools, retries, stops after N steps | Worker with a time limit |
| Tools | Python functions the LLM may *request*; our code runs them | Hands |
| Security gate | Checks every tool call: allowed? needs your "yes"? | Security guard |
| Short-term memory | Recent messages in this conversation | What you remember from 5 minutes ago |
| Long-term memory | Facts saved on purpose | Notebook |
| Vector DB / semantic memory | Finds memories and document passages by meaning | Librarian who understands topics |
| RAG | Retrieve relevant passages, then answer from them | Open-book exam |
| Voice | Whisper (speech→text), Piper (text→speech), wake word | Ears and voice |
| Observability | Logs, metrics, dashboards | Car dashboard |

**Key safety idea:** the LLM never executes anything. It only proposes a tool call as JSON,
e.g. `{"tool":"calculator","args":{"expression":"482*29"}}`. ARTHUR's code validates it,
checks permissions, runs the tool, and gives the result back to the LLM.

## Example flow

*"Arthur, search for the best AI conferences in Singapore, compare them and prepare a summary."*

1. **Microphone** – browser records audio and sends it over the WebSocket.
2. **Speech-to-text** – faster-whisper returns text + confidence. Silence/low confidence → "Sorry, I didn't catch that."
3. **Context** – system prompt + recent messages + relevant long-term memories + available tools.
4. **Intent check** – multi-step task → planner.
5. **Planner** – returns structured JSON (validated by Pydantic): search ×2 → extract → compare → summarize.
6. **Executor** – `web_search` is permission level 0 (read-only), no confirmation. Async, 10 s timeout, 2 retries. Logged to the audit log.
7. Independent read-only searches run **in parallel**.
8. **Extraction** – structured output into a `Conference` schema; missing fields become "unknown", never invented.
9. **Comparison** – table built only from extracted data.
10. **Response** – summary with a source URL per item; model inferences labelled as such.
11. **Memory policy** – nothing saved automatically; ARTHUR may *offer* to remember.
12. **Text-to-speech** – Piper speaks a short version sentence by sentence; the full table shows in the UI.
13. **Guards** – max 8 steps, 120 s budget, cancel button, live status in the UI.

## Technology stack (free MVP)

| Layer | MVP choice | Runs | Optional paid/alternative |
|---|---|---|---|
| API | FastAPI + Uvicorn | Local | – |
| LLM | Ollama + `qwen3:8b` | Local (GPU) | OpenAI, Anthropic, Groq, OpenRouter |
| Embeddings | Ollama `nomic-embed-text` | Local | OpenAI embeddings |
| Vector DB | ChromaDB | Local | FAISS, Qdrant, Pinecone |
| Database | SQLite | Local | PostgreSQL |
| Web search | `ddgs` (DuckDuckGo) → self-hosted SearXNG | Internet | Tavily, Brave, SerpAPI |
| Speech-to-text | faster-whisper | Local | OpenAI / Deepgram |
| Text-to-speech | Piper | Local | ElevenLabs, OpenAI TTS |
| Wake word | openWakeWord + push-to-talk | Local | Picovoice Porcupine |
| Vision | Ollama vision model | Local | Cloud vision APIs |
| Browser | Playwright | Local | – |
| Computer control | pyautogui, pywinauto, mss | Local | – |
| Scheduler | APScheduler | Local | – |
| Monitoring | Prometheus + Grafana | Docker | Grafana Cloud |
| Tests | pytest, pytest-asyncio, Locust | Local | – |

No paid API is required. Only web search and the browser agent need internet access.

## Hardware notes
- i7-13650HX, 16 GB RAM, RTX 5060 Laptop (8 GB VRAM): an 8B model at 4-bit uses ~5–6 GB VRAM.
- 8 GB VRAM can't hold the LLM, Whisper and a vision model at once; Ollama unloads idle models,
  and Whisper can run on CPU (`int8`).
- Plan ~25 GB disk for models, Docker images and packages.

## Design decisions
- **Python 3.12**, not 3.14: ML libraries publish pre-built wheels late for new Python versions.
- **Permission levels and audit log start with the tool system (Phase 6)**, hardened in Phase 19.
- **Tests from Phase 1 onwards**, not only in Phase 21.
- **Dependencies are added per phase**, so each one has a known reason.
- **Frontend has no external libraries**: works offline, loads nothing from third parties.
