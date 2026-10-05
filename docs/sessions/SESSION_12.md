# Session 12 – Phases 28 and 29: Failure handling, final demo, documentation (2026-10-05)

The last session, done the same day as Session 11 at the owner's request. Licence chosen: MIT.
Result: every failure from the spec is handled and tested, the full demo runs end to end, and
the repository has a professional README, CONTRIBUTING and LICENSE. 799 tests, coverage 91 %.
Commits: `117b9dd` (failure handling), `18436ff` (demo + documentation).

---

## Phase 28 – Failure handling

**Concepts**
- **Graceful degradation.** If an optional part fails (memory, documents, internet, voice),
  ARTHUR still answers without it, and says so when it matters. If an essential part fails
  (the language model), the message is clear and ARTHUR recovers by itself.
- **Circuit breaker.** After a service has failed even with retries, stop knocking for a
  while and report the failure at once. Like a fuse: it "trips", and resets after 30 s or on
  the first success.
- **Retry only what can help.** A quick hiccup is worth one retry. A timeout or a slow failure
  is not: retrying only doubles the wait.

**What was checked**
- All 11 failure cases from the spec, each with a test in `tests/unit/test_failures.py`.
- Live, on a separate test server so the real Ollama and network were never touched:
  Ollama "off" (a dead port), internet "off" (a dead proxy), and the browser's blocked microphone.

**Found and fixed (7)**
1. A broken memory database made **every** answer fail. Recall only caught model errors;
   now it catches everything, and remember/forget explain the problem.
2. A broken voice engine failed silently. Now there's a 503 with a clear message, shown once per reply.
3. "Ollama is off" took 10 s every time: on Windows a refused local connection takes 2 s.
   With circuit breakers in the model client and the embeddings, repeats take 2 s.
4. The status went back to ONLINE right after "Cannot reach Ollama". Now it stays on *Model offline*.
5. An offline web search took 22 s. Now: no retry after a timeout or a slow failure → 11.6 s.
6. **The browser kept running old page code after an update.** Found while testing #4. Now
   `Cache-Control: no-cache` on the page files; unchanged files answer 304.
7. Cosmetic: an error on an empty chat sat under the welcome screen.

## Phase 29 – Final demo and documentation

The spec's scenario, run for real (`docs/DEMO.md`). It used only fictional sample files, with
file access limited to `Documents\ARTHUR` and a temporary memory, so no personal data
could appear in screenshots.

| Step | Result |
|---|---|
| "Hey Arthur" | wake word ✓ (via ARTHUR's own voice; the app's browser blocks microphones) |
| find my latest resume | the 2026 one, 2.5 s |
| summarise my technical skills | 4.9 s |
| search the web for matching AI roles, compare | 6-step plan, 7 searches, cited comparison, 59 s |
| save the report → where → yes | written to `Documents\ARTHUR\reports` |
| read the report to me | read aloud, SPEAKING lamp on |

**The first run found two real agent bugs**
1. "Summarize my technical skills" failed. The previous *answer* named the file but not its
   folder, because history keeps answers, not raw tool results, and the model guessed a wrong path.
   → A bare file name is looked up inside the allowed folders; a wrong path gets a hint.
2. The research plan tried to **save** a report nobody had asked to save. The confirmation
   stopped it, but the comparison never reached the user.
   → A plan gets no confirmation-level tools unless the message asks for an action.

**Concept: why the demo needs one extra "yes".** The spec has ARTHUR save as soon as you
name the folder. ARTHUR only takes a short, plain "yes" as permission, so a sentence can never
be mistaken for consent. One extra word, on purpose.

**Documentation**
- `README.md`: the 18 sections the spec asks for, including a Mermaid architecture diagram,
  the API table, screenshots and demo-video instructions.
- `CONTRIBUTING.md`: setup, the non-negotiable safety rules, how to add a tool, style, commits.
- `LICENSE`: MIT, © 2026 Vishnu Varthan Thiagarajan.
- A secrets scan of every tracked file: no keys, no `.env`, no data, no personal files.
- `tests/security/test_no_shell.py`: "no shell anywhere" is now a test, not just a promise.

## What you can say in an interview
- "I tested failure on purpose: Ollama off, internet off, a broken database. I measured how
  long each took to *report*. A circuit breaker cut 'model offline' from 10 s to 2 s."
- "My final demo found two agent bugs that 700 unit tests hadn't: a model guessing a file
  path it had only seen in a tool result, and a plan taking an action nobody asked for."
- "Safety rules are enforced by tests that read the code itself, e.g. no shell calls anywhere."

## The project in numbers
- 12 sessions (+ 10b), 30 phases (0–29), 46 commits.
- 11,900 lines of Python, 8,600 lines of tests, 2,300 lines of frontend.
- 799 tests (110 security), coverage 91 %.

## What's left (optional, the owner's choice)
- **Put the project on GitHub.** Create an empty repository (no README/licence – both exist),
  then `git remote add origin <url>` and `git push -u origin main`.
- Record a demo video ([README §17](../../README.md#17-demo-video-instructions)).
- The ideas in README §18 (lock file + CI, OCR, persistent conversations, login before any
  network exposure).
