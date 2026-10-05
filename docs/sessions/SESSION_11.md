# Session 11 – Phases 25, 26, 27: Status UI, attachments, advanced agent (2026-10-05)

Result: you can see what ARTHUR is doing and whether every part is healthy; you can show it
a screenshot or a PDF straight from the chat box; and the agent got four new reliability
features. 767 tests passing, coverage 91 %.
Commits: `915eda7` (status rail), `a023e87` (attachments), `a8d0814` (advanced agent).

---

## Phase 25 – Status rail

**Concepts**
- **Health check vs. status page.** `/health` asks one question: can ARTHUR reach its model?
  `/status` asks every part separately (model, memory, documents, reminders, voice, vision,
  browser, desktop apps). Each part reports **ok**, **off** (switched off on purpose) or
  **problem**. Each check has its own timeout, so one broken part never breaks the page.
- **One source of truth for state.** The five lamps (ONLINE · LISTENING · THINKING ·
  EXECUTING · SPEAKING) are drawn by the same function that sets the old status pill, so
  they can never disagree.

**Built**
- `app/api/routes/system.py`: `GET /status`.
- Frontend:
  - a side rail (≥ 1100 px) or a "Status" overlay (smaller screens);
  - the lamps, the tool in use and the microphone state;
  - a Task card that mirrors plan steps and tool calls;
  - System health refreshed every 20 s and after each answer.

**Verified:** lamps during a weather + calculator question, SPEAKING during voice playback, the phone layout (one scrolling button row, solid overlay). EXECUTING only flashes, because tools take ~0.3 s.

## Phase 26 – Multimodal input

**Concepts**
- **Describe, then reason.** The chat model (qwen3) cannot see. The vision model
  (qwen2.5-VL) can see, but has no tools or memory and is weaker at conversation. So the
  vision model looks first, *told what the user asks*, and its description goes to qwen3 as
  data.
- **Upload first, send ids.** A file is uploaded the moment it is attached. The chat
  message then carries only a short id, so sending is instant and the WebSocket stays small.
- **Budgets.** The model's window is 8,192 tokens and the standing prompt takes ~4,000.
  An attached document gets up to 8,000 characters (~2,000 tokens) of whole pages, plus a
  note saying how much was left out.

**Built**
- `POST /attachments`:
  - pictures are checked and shrunk, never saved;
  - documents are read and also imported into Docs;
  - everything is kept in RAM for 30 minutes.
- Chat (WebSocket and `POST /chat`) accepts attachment ids; pictures show as a `describe_image` step.
- Composer: 📎, paste, drag and drop, chips with thumbnails.

**Verified live**
- "Arthur, look at this screenshot and tell me what's wrong" (a terminal with
  `ModuleNotFoundError`) → found the missing `requests` module. 34 s, of which 21 s was
  loading the vision model.
- "Read this PDF and explain the important parts" (handbook) → a page-by-page summary;
  the planted instruction on page 4 was ignored.

## Phase 27 – Advanced agent features

**Already there before today:** task decomposition, retries, fallback strategies,
self-checking, response verification, citations, cancellation. `docs/AGENT.md` shows where.

**New**
1. **Parallel lookups where safe.** Several read-only, independent tools in one step run
   at the same time. Browser, desktop and vision are excluded (shared page/window/GPU);
   actions always run one at a time.
2. **Context compression.** Short notes on what scrolled out of the history window.
3. **Memory prioritisation.** Facts are ranked, dated and capped; newer facts win conflicts.
4. **Source check.** Every cited `[file, p. N]` and link is matched against what ARTHUR
   actually read in this turn; unmatched ones get a visible note.

**Measured, and changed because of it**
- Notes written right after an answer made the next **two** answers 2–4 s slower each:
  the summary took the GPU and pushed the chat prompt out of Ollama's cache. Fix: notes are
  written after **15 s without a new message**. 30 back-to-back turns are now exactly as fast
  as in Session 10.
- Benefit: two facts from turn 1, asked after 30 long messages – **0/2 without notes, 2/2
  with notes** (`scripts/check_long_recall.py`).
- The first live source check flagged the Open-Meteo link, although the weather tool names
  that site. A front-page link now counts when a result names the site; deeper links still
  need an exact match.

**Not done: tool ranking** (sending only "likely" tools). It would change the cached prompt on
every message (+1.2 s each, measured in Phase 24). Explained in `docs/AGENT.md`.

## Found on the way
- **"Note 3 about the garden project…" was saved as a memory.** "Note" counted as an
  instruction even as a noun. Only "note that / note down / note: / make a note" save now.
- **My test wrote into the real memory database** (3 junk memories). They were deleted at
  once; the owner's real memory was untouched. Lesson: experiments run on a
  **temporary data folder** – the recall script's server now uses one.
- A plain answer cited "(Page 2)" instead of the exact `[file.pdf, p. 2]` form after an
  attached PDF. That is harmless, but the source check can only verify the exact form.

## What you can say in an interview
- "The agent can look at a screenshot although the chat model is text-only: the vision model
  describes the picture *for the user's question*, and the description is passed on as data."
- "I made safe tool calls parallel, but only for read-only tools that share no state – a
  browser page or a desktop window is a shared resource even when you only read it."
- "My first context-compression design made answers slower. I measured it, moved the work
  into the user's pauses, and showed with a recall test that the feature actually helps."

## Next
Session 12 – Phases 28–29: failure handling, final demo, full README/CONTRIBUTING/LICENSE.
