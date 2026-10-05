# When things go wrong (Phase 28)

Rule: **one broken part never takes ARTHUR down.**
- If an *optional* part fails (memory, documents, internet, voice), ARTHUR still answers, without that part, and says so when it matters.
- If an *essential* part fails (the language model), the message is clear and ARTHUR recovers by itself when the part is back. No restart needed.

Every row has a test in `tests/unit/test_failures.py` (or the file named). "Live" means it
was also checked on a running ARTHUR on 5 Oct 2026:
- Ollama "off": a test server pointed at a dead port; the real Ollama was never stopped.
- Internet "off": every outside connection sent to a dead proxy.
- Microphone: the Claude app's built-in browser, which blocks the mic.

| Failure | What you see | Behind the scenes | Checked |
|---|---|---|---|
| **LLM unavailable** (Ollama off) | ⚠ "Cannot reach Ollama at … Is it running?"; status shows **MODEL OFFLINE**, the ONLINE lamp turns red, System marks only *Language model* | 2 retries for a brief hiccup; then a 30-s circuit breaker so the next messages report it at once; memory lookups skip the dead server too | test + live |
| **Internet unavailable** | "I couldn't check the weather … isn't reachable" / "Please check if you're online"; offline tools (calculator, files, memory, documents) keep working | tool returns an error result, the model explains it; search retries only after a *quick* failure | test + live |
| **Tool failure** (a tool crashes) | the answer says the tool failed; no internal error text | `ToolRegistry.execute` turns any exception into an error result and logs the traceback | test |
| **Invalid tool arguments** | usually nothing - the model corrects itself on the next step | Pydantic validation → error result with the reason; an invented tool name is refused | test |
| **Timeout** | "… took too long" | every tool has a time limit (default 10 s); the agent has 8 steps / 120 s per message | test |
| **Bad document** | "Couldn't read this PDF file (PdfStreamError)." / "No readable text found … scanned PDF?" | `DocumentError`; nothing is stored | test + live |
| **Unsupported file** | "Unsupported file type '.exe'. Supported: …" (Docs) / "ARTHUR reads pictures … and documents …" (chat attachments) | checked by extension before reading | test + live |
| **Microphone unavailable** | blocked / missing / busy / unsupported → a specific explanation (e.g. "open it in Chrome or Edge"); ARTHUR stays Online, mic "off" | `micError` in the page | test + live |
| **TTS failure** (broken voice model) | ⚠ "ARTHUR's voice isn't working right now - the answer is still shown as text." once per reply; System → *Voice: problem* | `/voice/speak` → 503 with that message; the page reports it instead of staying silent | test |
| **Database failure** (SQLite broken/locked) | answers still come, without memories; "remember"/"forget" → "my memory store isn't working right now…"; System → *Memory: problem* | recall catches *any* error, not only model errors; saves/deletes explain; one process-wide lock prevents "database is locked" (Phase 22) | test |
| **Model failure** | not installed → "Model 'qwen3:8b' is not installed. Run: ollama pull qwen3:8b"; silent model → "I couldn't produce an answer…"; failure mid-chat → the next message works normally | `LLMResponseError` → 502; `EMPTY_ANSWER`; honesty and source checks catch claimed actions and invented sources (Phase 27) | test |

Safety nets under all of this:
- Unexpected errors in any request → `{"error": {"type": "internal_error", "message": "Something went wrong inside ARTHUR."}}`. The traceback goes to the log only, never to the page.
- WebSocket: an error ends that one answer; the connection and the conversation stay usable.
- Background jobs (reminder scheduler, conversation notes, model warm-up) catch and log their own errors.

## Found and fixed in Phase 28
1. **A broken memory database stopped every answer.** Memory and document lookups only
   caught *model* errors. A SQLite error made the whole message fail, although memory is
   optional. They now catch everything and answer without memory. Remember, forget and
   "yes, delete it" explain the problem in plain words.
2. **A broken voice engine failed silently.** The page skipped audio it couldn't get. Now
   `/voice/speak` answers 503 with a clear message, and the page shows it once per reply.
3. **"Ollama is off" took 10 s to report, every time.** On Windows a refused local connection
   takes 2 s; memory lookup + 3 model attempts + pauses = 10 s. With the circuit breakers,
   the first report still takes 10 s (it might just be restarting), the next ones 2 s.
4. **The status said ONLINE right after "Cannot reach Ollama".** It now stays on *Model
   offline* until an answer succeeds again.
5. **An offline web search took 22 s to fail.** A timeout was retried, and so was a slow
   failure. Now only quick failures are retried: 11.6 s, the rest is the search library itself.
6. **The browser kept running the OLD page code after an update** (found while testing #4).
   Page files are now sent with `Cache-Control: no-cache`: the browser re-checks them on
   every load, and unchanged files answer "304 not modified".
7. Cosmetic: an error shown on an empty chat no longer sits under the welcome screen.

## Not covered
- **Disk full.** Saving a memory or document would fail with the "memory store" message or a
  500; not tested.
- **GPU out of memory.** Ollama reports it as a model error (502 + message); not reproduced on
  purpose.
- **Recovery after a crash of ARTHUR itself:** conversations are RAM-only, so a restart forgets
  the current chat (memories, documents and reminders are kept).
