# The agent: how ARTHUR decides, checks itself and stays honest (Phase 27)

The Phase 27 spec lists twelve "advanced agent features". Most were built in earlier phases,
because a safe agent needed them from the start. This page shows where each one lives, what
Phase 27 added, and the one item deliberately left out.

| Feature | Where | Since |
|---|---|---|
| Task decomposition | `agent/planner.py` - multi-part requests become 2-5 steps; `PlanExecutor` runs each with its own tool loop | Phase 8 |
| **Parallel tool execution where safe** | `agent/executor.py` `ToolLoop._parallel` + `Tool.parallel_safe` | **Phase 27** |
| Retries | `llm/resilience.py` `RetryingProvider` (model calls, back-off), plan steps retried once, web search retried | Phases 3, 8, 10 |
| Fallback strategies | `FallbackProvider` (second model provider), search cache, answers without memory/documents when those fail, CUDA → CPU for speech | Phases 3, 10, 11 |
| **Context compression** | `Orchestrator._compress_later` → running notes on what scrolled out of the window | **Phase 27** |
| **Memory prioritisation** | `memory/policy.prioritize` - ranked, dated, capped; newer facts win conflicts | **Phase 27** |
| Tool ranking | not done - see below | - |
| Self-checking | `agent/verification.py` - claimed actions without a tool call, fake permission questions | Phases 7, 15 |
| Response verification | the same checks plus the source check below; plan answers use only step results and must say what is missing | Phases 7, 8, 27 |
| Hallucination reduction | the above; answers from documents only from passages; "not in your documents" only after a real search; prompt describes only tools that exist | Phases 9, 23 |
| **Source citation (verified)** | citations since Phase 9/10; **Phase 27 checks every cited page and link** against what was really read | 9, 10, **27** |
| Task cancellation | stop button / `{"type": "stop"}` cancels within ~0.2 s, keeps the partial answer | Phase 3 |

## New in Phase 27

### Parallel tools - only where safe
When the model asks for several lookups in one step ("weather in Singapore and London"),
they run at the same time **only if every one of them**:
- only reads (`permission_level` 0), **and**
- is marked `parallel_safe`: it shares no state with other calls.

Marked: calculator, current_time, weather, search_memory, document_search, list_documents,
web_search, read_webpage, find_files, list_folder, read_file, list_reminders.

Not marked, although read-only:
- **browser tools:** there is one page, so two `browser_open`s would race;
- **desktop tools:** one window, one COM thread;
- **describe_image:** the vision model needs the GPU.

Anything that changes something stays strictly one at a time, each with its own permission check.

Measured: two 0.3 s lookups take 0.3 s instead of 0.6 s (test). Live, "weather in Singapore and in London?" ran both weather calls in parallel (`tools_in_parallel` in the log).

### Context compression
Phase 24 made the history window jump in steps (good for the prompt cache), but whatever fell
out was simply forgotten. Now ARTHUR writes short notes (≤120 words) about the part that
scrolled out. They ride right after the system prompt, marked as data.

**Measured cost and the fix:**
- **Straight after each answer:** the next two answers were 2–4 s slower each. The summary occupied the GPU, and its prompt pushed the chat prompt out of Ollama's single cache slot.
- **Now:** notes are written only after **15 s without a new message** (`HISTORY_SUMMARY_DELAY_SECONDS`). A new message cancels the wait. In a 30-turn back-to-back conversation the timings are identical to Phase 24 (0.4 s per turn, 1.2 s at a jump). The notes then cost one extra re-read (~1–2 s) on the first message after the pause.

**Measured benefit** (`scripts/check_long_recall.py`): two facts stated in turn 1, then 30 long messages, then the question.
- Without notes: "I don't have that information" (0/2).
- With notes: "Your dog's name is Pickles, and your sister lives in Tromsø" (2/2).

### Memory prioritisation
Recalled memories used to be a plain list. Now they are:
- sorted by relevance;
- capped at 1,200 characters (the least relevant are left out first);
- each prefixed with the day it was saved.

The model is told that when two facts disagree, the newer one is correct.

### Source verification
After every answer, each `[file.pdf, p. N]` citation and each Markdown link is compared with
what ARTHUR actually gave the model in this turn: passages, attached files, tool results
(`ToolContext.sources`). Anything that matches nothing gets a visible note:

> ⚠️ **Source check:** I couldn't match this source to anything I actually read for this answer: [ghost.pdf, p. 9].

Rules that avoid false alarms:
- A link to a page that was read, or to the front page of its site, counts as read.
- A front-page link also counts when a result names the site, e.g. the weather tool's `"source": "Open-Meteo (open-meteo.com)"`. The first live run flagged that link; the rule was added after it.

Live check, no false alarms on: handbook answers (`[student_handbook.pdf, p. 2]`, `p. 3`), a web answer linking `python.org/downloads/latest/`, and the weather answer.

## Not done: tool ranking
"Tool ranking" usually means sending the model only the tools that look most relevant to each
message. ARTHUR sends all of its tools every time, on purpose:
- The tool descriptions are part of the cached prompt. Choosing a different set per message changes the prompt's beginning and costs the full re-read (Phase 24 measured +1.2 s per change).
- 27 tools fit comfortably in the window.
- A wrong guess would hide the right tool. The planner already narrows choices for multi-step tasks.

If ARTHUR grows to many more tools, the cache-friendly version would be a fixed set per kind of task (chosen once per conversation), not per message.

## Found on the way
`"Note 3 about the garden project…"` was taken as "remember this": "note" counted as an
instruction even when it was the noun. Now only "note that…", "note down…", "note: …" and "make a
note…" save. The test run had saved three junk memories; they were deleted, and the recall
script now runs on its own temporary data folder.
