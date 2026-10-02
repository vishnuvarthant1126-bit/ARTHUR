# Session 10 – Phases 24 and 23: Performance, Docker files (2026-10-02)

Docker Desktop is not installed yet, so the owner chose: **performance fully today, Docker
written and checked without running it**. That is why Phase 24 came first.
Result: answers are 2–20× faster in the situations that were slow, and the Docker setup is
ready to try. 715 tests passing, coverage 91 %.
Commits: `5541e2f` (performance), `58239f3` (Docker files).

---

## Phase 24 – Performance

**Concepts**
- **Measure first.** A guess about what is slow is usually wrong. Every fix today came from a
  number: `scripts/profile_chat.py` splits each answer into *recall / load / read / write*.
- **The model reads before it writes.** ARTHUR's standing prompt (instructions + 27 tool
  descriptions) is ~4,000 tokens. Reading it takes ~1.2 s.
- **Prompt cache.** Ollama remembers the prompt it read last time and only reads what comes
  *after the first difference*. So the order matters: things that never change go first,
  things that change go last.
- **Cold start vs. warm.** Loading a model into the GPU takes seconds; a loaded model answers
  at once. Warm-up = do the loading before the user asks.
- **Keep-alive.** Keeping a connection (or a model) open is cheaper than opening it again.

**What the numbers showed, and the fixes**
1. `localhost` cost up to 2 s per new connection on Windows (IPv6 is tried first, Ollama
   listens on IPv4). → `127.0.0.1`, an old `.env` is corrected automatically
   (`app/utils/net.py`); connections stay open 5 minutes instead of 5 seconds.
2. Memories and document passages sat at the *front* of the prompt, so every change threw
   the cache away. → The system text is now identical on every request; changing facts travel
   in a `<context>` block with the newest message.
3. Tool descriptions were not counted in the context budget. Long chats overflowed the
   model's window, Ollama cut the start off itself, the cache broke every turn. → Counted
   now; history is trimmed in bigger steps (`Conversation.window`).
4. First message after start took 11–13 s. → Models are loaded in the background at start-up
   (`Orchestrator.warm_up`).
5. Long pasted text triggered a useless planning call. → "and" only counts in short messages.

**Before → after** (details in `docs/PERFORMANCE.md`)

| Situation | Before | After |
|---|---|---|
| First message after a cold start | 11–13 s | 0.6 s |
| Small talk | 0.9–1.3 s | 0.4–0.8 s |
| Tool question | 2.7–3.4 s | 1.2 s |
| Answer from a document | 1.6–2.9 s | 0.6–1.3 s |
| Long conversation (turn 22+) | 3.6 s | 1.6 s |

After the prompt was restructured, quality was re-checked live: citations, memory, "not in
your documents", and the planted instruction in the sample handbook is still ignored.

**Honest notes**
- Shorter tool descriptions (`slim_schema`) did **not** shrink the prompt with Ollama – it
  already drops those fields. Kept because it helps other providers; reported as "no gain".
- What ARTHUR trims from a long chat is gone, not summarised.
- The model's writing speed (40–60 tokens/s) is now the biggest part. That is the GPU.

**Found on the way: the real flaky test.** Session 9 blamed a voice test. The real one was
the duplicate-upload test: the test PDF contained its creation time to the second, so two
"identical" uploads differed when a second ticked over between them. Fixed with a fixed date.
Lesson: *a flaky test is a clue – find what differs between runs.*

## Phase 23 – Docker (written, not run)

**Concepts**
- **Image** = a packed box (Linux + Python + ARTHUR's code), built from a recipe, the
  **Dockerfile**. **Container** = the box running.
- **Compose** starts several containers together and gives them a private network where
  service names work as host names (`http://arthur:8000`).
- **Volume** = storage that survives the container.
- **Publishing a port**: `127.0.0.1:8000:8000` means "this PC's loopback address only".
  `8000:8000` would mean "every network card" – the whole Wi-Fi.
- **`host.docker.internal`** = "the PC", seen from inside a container. Ollama stays on
  Windows because it needs the GPU.
- **Static check** = testing a file by reading it. It proves the file says what we intend;
  it cannot prove that it *works*.

**Built**
- `docker/Dockerfile`, `.dockerignore`, `docker-compose.yml` (ARTHUR + Prometheus + Grafana,
  optional SearXNG), `deploy/` updated, `docs/DOCKER.md`.
- Safety: ports on 127.0.0.1 only; no secrets and no `.env` in the image or container;
  non-root user, all Linux capabilities dropped; app control off (a container cannot see
  the desktop); file tools see only one mounted folder; Linux system folders blocked.
- `tests/unit/test_docker_files.py`: 28 checks without Docker – syntax, ports, secrets,
  pinned versions, names/ports agreeing between Compose, Prometheus and Grafana, every
  Compose setting being a real ARTHUR setting, and "no module needs Windows to be imported".

**Not done – needs Docker Desktop (only the owner can install it)**
Nothing was built or started. `docs/DOCKER.md` ends with a 13-point first-run checklist.
The likeliest surprises: a Python package without a Linux build, the container not reaching
Ollama, write permission on mounted folders, Chromium's sandbox inside the container.

## What you can say in an interview
- "I profiled before optimising. The biggest win was not code speed but prompt order: keeping
  the unchanging part first lets the model's prompt cache work."
- "A 2-second delay turned out to be `localhost` resolving to IPv6 first on Windows."
- "I wrote the container setup before I could run it, and tested the properties I could test
  statically – and I labelled it 'not run' instead of pretending."

## Next
Session 11 – Phases 25–27: futuristic UI, multimodal input, advanced agent features.
Whenever Docker Desktop is installed: run the checklist in `docs/DOCKER.md` (a short extra
session, "10b").
