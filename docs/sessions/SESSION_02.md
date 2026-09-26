# Session 2 – Phases 5 and 6 (2026-09-26)

Result: ARTHUR remembers facts permanently (across conversations and restarts) and has a
safe tool system. 157 tests passing (unit + live Ollama/ChromaDB).

---

## Phase 5 – Long-term memory

**Concepts**
- **Embedding** – an embedding model turns a sentence into a list of numbers (768 for
  `nomic-embed-text`): coordinates on a "map of meaning". Similar meanings land close together,
  so "Which software am I building?" finds "The user's main project is called ARTHUR" even
  though they share no keywords.
- **Cosine similarity** – how closely two embeddings point the same way: 1 = same meaning,
  ~0.5 = loosely related, lower = unrelated.
- **Vector database (ChromaDB)** – stores embeddings and finds the nearest ones fast.
- **SQLite** – a normal database in one file (`data/arthur.db`) holding the actual records.
  SQLite is the source of truth; ChromaDB holds a searchable copy under the same id.
- **Memory policy** – what may be saved: only explicit "remember…" requests; never questions;
  never secrets (passwords, PINs, keys, card/bank numbers); forgetting needs a "yes"; everything
  visible and deletable in the Memory panel.

**Flow**
- "Remember that …" → secret filter → the LLM extracts one clean third-person fact
  (structured output) → embedding → SQLite + ChromaDB → "Got it. I'll remember that: …"
- Every other message → embed it → memories with similarity ≥ 0.55 are added to the system prompt.
- "Forget …" → best match → "Should I forget …? yes/no" → deleted from both stores on "yes".
- If the embedding model is down, ARTHUR answers without memories instead of failing.

**Calibration (measured, not guessed)** – `scripts/calibrate_memory.py`:
related questions scored 0.59–0.83, unrelated 0.46–0.51 → threshold 0.55. The first guess
(0.50) let "What's the capital of France?" pull in unrelated memories; the live test caught it.

Files: `app/rag/embeddings.py`, `app/memory/{vector_store,long_term,manager,policy}.py`,
`app/database/{database,models}.py`, `app/api/routes/memory.py`, Memory panel in `frontend/`.

---

## Phase 6 – Tool system

**Concepts**
- A **tool** is a Python function ARTHUR may use. The LLM can only *ask* for one (name + JSON
  arguments); our code decides whether it runs.
- Each tool has: name, description, **input schema** (Pydantic model), **permission level**,
  **timeout**, `run()`.
- The **registry** is the single gate: lookup → permission → validation → confirmation →
  run with timeout → catch errors → **audit log** (every attempt, allowed or not).

| Level | Meaning | Behaviour |
|---|---|---|
| 0 | read-only | runs |
| 1 | low-risk, reversible | runs |
| 2 | needs confirmation | returns a preview until confirmed – cannot be configured away |
| 3 | highly sensitive | always denied |

Tools: `calculator` (safe syntax-tree evaluator, never `eval()`), `current_time`, `weather`
(Open-Meteo, free, no key), `search_memory` (0), `save_memory` (1), `delete_memory` (2).

The LLM does not choose tools yet – that is Phase 7. For now, run them yourself at
http://127.0.0.1:8000/docs → `POST /tools/{name}/run`.

---

## Verified for real
- "Remember that my main project is called ARTHUR…" → saved; **Clear** → "Why am I building my
  project?" → correct answer from long-term memory.
- "Remember my password is hunter2" → refused (the secret never reaches the model).
- "Forget my main project" → asks first; "no" keeps it.
- Server restart → memory still there; a brand-new session still knows it.
- Calculator `482 * 29 = 13978`; `__import__('os')…` rejected; live weather for Singapore;
  `delete_memory` returned `needs_confirmation`; all calls visible in `GET /audit`.

## Commands
```powershell
uvicorn app.main:app --reload            # http://127.0.0.1:8000  (Memory button top right)
pytest                                   # all tests
python scripts/calibrate_memory.py       # re-measure the memory threshold
```

## Troubleshooting
| Problem | Fix |
|---|---|
| "Embedding model … not installed" | `ollama pull nomic-embed-text` |
| Irrelevant memories in answers / relevant ones missed | re-run the calibration script, adjust `MEMORY_MIN_SCORE` |
| Port 8000 busy (WinError 10048) | another program uses it; `--port 8001` |
| Want to wipe all memories | stop the server, delete `data/arthur.db` and `data/memory/chroma/` |
| `weather` fails | needs internet; disable with `TOOLS_BLOCKED=weather` |

## Known limitations
- The LLM can't call tools on its own yet (Phase 7).
- "Remember" detection is pattern-based: unusual phrasings may be missed – use "remember that…".
- Long-term memory is shared by everyone using this ARTHUR (single-user design).
- Embedding a query takes ~50 ms normally, but up to ~3 s when Ollama must swap models in VRAM.

## Next: Session 3 – Phases 7–8
The agent loop (ARTHUR decides when to call tools) and the planner for multi-step tasks.
