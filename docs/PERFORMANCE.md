# Performance (Phase 24)

Measured on 2 Oct 2026 on the development laptop (Windows 11, RTX 5060 Laptop 8 GB,
qwen3:8b through Ollama). Rule of the phase: **measure first, then fix what the numbers show.**

## How to measure
```powershell
python scripts/profile_chat.py --pause 8        # typical messages, 8 s apart like a person
python scripts/profile_chat.py --context        # memories / document passages come and go
cd scripts; python profile_long_chat.py --turns 30   # a long conversation
```
Run them against a server started **without** `--reload` (the reloader restarts the server
whenever a file changes, which resets the numbers):
`python -m uvicorn app.main:app --port 8002`, then add `--url http://127.0.0.1:8002`.

Each line shows where the time went:

| Column | Meaning |
|---|---|
| recall | looking up memories and document passages (embedding the question) |
| load | loading the model into the GPU (0 when already loaded) |
| read | the model reading the prompt: instructions + tool descriptions + conversation |
| write | the model writing (answer or tool request), plus the planner if it ran |
| prompt | how much the model had to read, in tokens |

The same numbers are on the stats page (`/dashboard.html`) and in `/metrics`.

## The key idea: the model re-reads only what changed
Before writing a word, the model must read the whole prompt – about **4,000 tokens** before
you have typed anything (instructions ~1,300 + 27 tool descriptions ~2,700). Reading that
takes ~1.2 s. Ollama caches the prompt, and the next request only pays for the part **after
the first difference**. So the order of the prompt decides the speed:

```
system text (never changes) → tool descriptions → conversation so far → newest message
└──────────────── identical to last time: cached ────────────────┘   └─ read: ~0.1 s ─┘
```

## Before → after

| Situation | Before | After | What changed |
|---|---|---|---|
| First message after a cold start | 11–13 s | **0.6 s** | Models are loaded and the prompt cached in the background at start-up (ready ~13 s after start) |
| Small talk, messages 8 s apart | 0.9–1.3 s | **0.4–0.8 s** | `127.0.0.1` instead of `localhost`; connections kept open |
| Tool question, 8 s apart | 2.7–3.4 s | **1.2 s** | same |
| Message that brings in a document passage | 1.6–2.9 s | **0.6–1.3 s** | Passages and memories moved to the end of the prompt |
| Long conversation, from turn ~22 on | 3.6 s per message | **1.6 s**, with 2.1 s once every ~6 turns | Tool descriptions counted in the budget; history trimmed in steps |
| Long pasted text (notes, e-mail) | +1.2 s | +0 s | No planning call for prose that merely contains several "and"s |

## What was found

1. **`localhost` cost 2 seconds per connection.** On Windows `localhost` is tried over IPv6
   first; Ollama listens on IPv4 only, and every new connection waited for the IPv6 attempt
   to fail (2.05 s with plain sockets, ~0.3 s with ARTHUR's async client; `127.0.0.1`: 1–15 ms).
   ARTHUR reused connections, but only for 5 seconds – so with normal pauses between messages
   every message opened two new ones (embeddings + model).
   → `http://127.0.0.1:11434` by default, an old `.env` with `localhost` is corrected
   automatically (`app/utils/net.py`), and connections stay open for 5 minutes.
2. **Memories and document passages lived in the system text** – in front of the 2,700 tokens
   of tool descriptions. Whenever they changed, the cache was useless and the model re-read
   everything (+1.3 s). → The system text is now identical on every request; the changing
   parts travel in a `<context>` block with the newest message (`prompts.context_block`).
3. **The tool descriptions were missing from the context budget.** ARTHUR allowed 5,800
   tokens of history when about 2,500 fit. In a long conversation the prompt hit the model's
   limit (8,192), Ollama cut off the oldest part by itself, the cache broke on *every* turn
   (+1.8 s), and ARTHUR no longer controlled what the model saw.
   → `Orchestrator.tools_tokens` is part of the budget; `Conversation.window` moves the start
   of the visible history in one larger step (down to 60 %) instead of one message per turn.
4. **Cold starts.** Loading the chat model takes ~7 s and the embedding model ~2 s; Ollama
   also unloaded the embedding model after 5 minutes. → `Orchestrator.warm_up` at start-up
   (`LLM_WARM_UP`), chat model first (loaded second, it pushed the small model out of the GPU);
   embeddings now use the same keep-alive as the chat model.
5. **Planner false alarms.** A long message with several "and"s triggered a planning call
   (~1.2 s) that answered "nothing to plan". → "and" only counts in messages up to 60 words.

## Honest notes
- **Shorter tool descriptions did not make the prompt smaller with Ollama.** `slim_schema`
  removes titles, length limits and roundabout "optional" forms (–18 % in characters), but
  Ollama already drops those fields before the model sees them – the measured prompt size
  did not change. It still helps other providers, and Ollama now receives choices (enums) and
  optional types it previously lost.
- Trimming history loses old context: in the 30-turn test, turn 1 was no longer visible at
  turn 30. ARTHUR does not summarise what it drops (a possible later improvement).
- The model *writing* is now the largest part of most answers (about 40–60 tokens/s on this
  GPU). That is the hardware limit, not something ARTHUR's code can change.
- A bigger context window would allow longer conversations but needs more GPU memory than
  this laptop has to spare next to the 6.2 GB model.
- After a 30-minute pause (`OLLAMA_KEEP_ALIVE`) the models are unloaded and the next message
  pays the ~9 s load again. Set `OLLAMA_KEEP_ALIVE=-1` to keep them loaded for ever.
- Numbers vary by ±0.2 s between runs; after a few idle seconds the GPU itself needs
  ~0.1–0.2 s to wake up, which shows as slightly higher "read" times with pauses.

## In Docker (2 Oct 2026)
The same profile against ARTHUR in a container (`--url http://127.0.0.1:8000`): small talk
0.35–0.8 s, tool question 1.05–1.15 s - no measurable difference from Windows, because the
time is spent in the model, which runs on the same GPU through Ollama either way. The
prompt is smaller there (2,900 instead of ~4,000 tokens): the container has no desktop and
browser tools. Memory: ARTHUR 930 MB, Grafana 290 MB, Prometheus 30 MB.
