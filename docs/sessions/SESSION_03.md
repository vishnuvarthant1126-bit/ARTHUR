# Session 3 – Phases 7 and 8 (2026-09-28)

Result: ARTHUR decides for itself when to use tools, and splits multi-part requests into a
visible plan that it works through step by step. 209 tests passing.
Commits: `3af3aca` (agent loop), `9c5ff8c` (planner).

---

## Phase 7 – Tool calling and the agent loop

**Concepts**
- **Tool calling** – the LLM receives a "menu" of tools (name, description, input schema).
  Instead of text it can reply with a *request*: `calculator(expression="482 * 29")`.
  It never runs anything itself.
- **Agent loop** – ask the LLM → if it requests tools, run them through the registry
  (permissions, validation, timeout, audit) → give the results back → ask again → until it
  answers in text.
- **Hard limits** – max 8 rounds, 120 s, and an identical repeated call is refused.
  Whatever the model does, the loop always ends.
- **Confirmation** – a level-2 tool (e.g. `delete_memory`) stops the loop; ARTHUR asks you;
  "yes" runs it, "no" cancels. The model cannot confirm for you (passing `confirmed=true`
  as an argument does nothing).
- **Tool results are data** – the prompt says so, and they're wrapped as tool messages.
- **Hallucinated actions** – a small model once said "I have deleted the memory" without
  calling any tool. Nothing was deleted (actions only run through the registry), but ARTHUR
  misled the user. Two fixes: clearer instructions, plus a **code-level honesty check** that
  appends a visible correction when a reply claims an action no tool performed.

Files: `app/agent/executor.py` (ToolLoop), `app/agent/state.py` (events),
`app/agent/verification.py`, tool support in `app/llm/*`, tool chips in `frontend/`.

## Phase 8 – Planner

**Concepts**
- **Why plan?** Small models lose track of long requests. Splitting first keeps each step small.
- **Two-stage decision** – a free keyword check (`looks_complex`) filters simple messages;
  only multi-part ones ask the LLM for a structured `Plan` (2–6 self-contained steps).
- **Executor** – each step gets its own small agent loop (max 4 tool rounds) and sees the
  results of earlier steps. A failed step is retried once, then marked failed; the plan
  continues. A confirmation pauses the plan. There's an overall 240 s budget.
- **Task state** – every step's status/result/error. The final answer is written **only from
  the step results**, and must name any part that failed instead of guessing.
- **Cancellation** – Stop cancels the whole plan instantly (measured: 0.2 s).

Files: `app/agent/planner.py`, `PlanExecutor` in `executor.py`, `TaskState` in `state.py`,
plan checklist in `frontend/`.

---

## Verified with the real model
| Request | What happened |
|---|---|
| What is 482 × 29? | calculator → 13,978 |
| Should I carry an umbrella in Singapore today? | weather → "95% chance of rain… carry one" |
| Hi Arthur, how are you? | no tool (correct) |
| What day is it in Tokyo? | current_time(Asia/Tokyo) → Monday |
| Look up my main project and delete that memory | search_memory → delete_memory → **asked permission** → "no" → kept |
| Weather in Singapore and London + difference | 3-step plan: weather, weather, calculator (28.3 − 15.1 = 13.2) |
| Time in Tokyo and New York, hours apart | 3-step plan → "13 hours" (correct) |
| 15% of 2480 | calculator → 372 (after teaching it "% of") |
| Stop during a plan | cancelled in 0.2 s |

## Bugs found and fixed during testing
- The WebSocket `type` was overwritten by the event's own `type` → tool messages ignored by the page.
- The model wrote maths as LaTeX (`$a \times b$`) → the page now shows it as plain text.
- The confirmation question showed a memory **id** → now shows the memory's text.
- A plan step did arithmetic in its head → the step prompt now insists on the calculator.
- "15% of 2480" was rejected by the calculator → now supported.

## Commands
```powershell
uvicorn app.main:app --reload       # http://127.0.0.1:8000
pytest                              # 209 tests
```
Try: *"What's the weather in Singapore and London, and what's the temperature difference?"*

## Settings (`.env`)
`AGENT_MAX_STEPS=8`, `AGENT_MAX_SECONDS=120`, `AGENT_PLANNING=true`, `AGENT_PLAN_MAX_SECONDS=240`.

## Known limitations
- A small local model doesn't always follow instructions: e.g. a time-difference step was
  answered correctly but without the calculator. Rules help; they don't guarantee.
- Planned answers take ~10–15 s (several model calls). Simple questions stay ~2–4 s.
- The complexity check is keyword-based; unusual phrasing may skip planning (the normal
  agent loop still handles it, just less structured).
- If the server auto-reloads during several quick edits, a change can be missed → restart.

## Next: Session 4 – Phases 9–10
Document RAG (ask questions about your PDFs/DOCX with page citations) and web search.
