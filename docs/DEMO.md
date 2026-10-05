# The final demo (Phase 29)

The scenario from the project spec, run for real on 5 Oct 2026. Setup for the run:
- ARTHUR on a laptop with an RTX 5060 (8 GB), qwen3:8b.
- File access limited to `Documents\ARTHUR`, which holds only **fictional** sample files
  (`python scripts/make_sample_files.py`): two resumes of "Alex Tan", from 2025 and 2026.
- A temporary memory, so no personal data could appear.

## The script

| # | You say | ARTHUR does | Measured |
|---|---|---|---|
| 0 | "Hey Arthur." | wake word detected → answers "Yes?" | `/voice/wake`: wake ✓ (also "Hey Arthur, check my documents…" → wake + command) |
| 1 | "Check my documents and find my latest resume." | `find_files` → newest first → *Sample_Resume_Alex_Tan_2026.pdf* (not 2025) | 2.5 s |
| 2 | "Summarize my technical skills." | `read_file` → Python, FastAPI, PyTorch, LLM agents, RAG with ChromaDB, Whisper, Docker, PostgreSQL, CI/CD + project, experience, education | 4.9 s |
| 3 | "Now search the web for AI engineering roles that match my skills and prepare a comparison." | a 6-step plan (job postings, requirements, salaries, company culture, remote options, compare), then 7 web searches (2 DuckDuckGo hiccups handled), then one comparison with a source link on every fact | 59 s |
| 4 | "Save the report." | asks: *"Save ai_engineering_jobs_report.md (1.7 KB) in C:\Users\…\Documents\ARTHUR\reports? Reply yes…"* | 10.7 s |
| 5 | "Save it in my ARTHUR reports folder." → **"yes"** | asks once more for that folder; after "yes" → the file is written (1.8 KB, all links kept) | 10.0 s + instant |
| 6 | "Read the report to me." | reads the report aloud with the Piper voice; SPEAKING lamp on; 29 sentences synthesised locally | 10.1 s to the text, speech starts after the first sentence |

Screenshots: [demo-comparison.jpg](images/demo-comparison.jpg) (step 3),
[demo-speaking.jpg](images/demo-speaking.jpg) (step 6).

### Deviations from the spec, on purpose
- **Step 5 needs a "yes".** The spec has ARTHUR save as soon as you name the folder. ARTHUR
  only accepts a short, plain "yes" as permission (Session 8). A sentence like "Save it in
  my ARTHUR reports folder" is treated as a new request, so it asks again, then saves on "yes".
  One extra word, and no sentence can ever be mistaken for permission.
- **Step 0 used ARTHUR's own voice instead of a microphone** for the recording, because the
  Claude app's built-in browser blocks microphones. The wake word with a real microphone in
  Chrome was verified in Session 5.
- **Step 6 read the report from the conversation** (it had just written it) instead of
  opening the file again. Same content.

## What the run found, and what was fixed before the final recording
1. **"Summarize my technical skills" failed at first.** The previous answer named the file
   but not its folder: the conversation history keeps answers, not raw tool results. The
   model guessed a wrong path. **Fix:** a bare file name is looked up inside the allowed
   folders, and a wrong path gets a hint where the file really is. This never reaches outside
   the allowed folders. 4 tests.
2. **The research plan tried to save a report nobody had asked to save.** The confirmation
   stopped it, so nothing was written, but the comparison never reached the user. **Fix:** a plan
   gets no confirmation-level tools (save, delete, type…) unless the message asks for an
   action. 2 tests.
3. Cosmetic, not fixed: the saved report ends with the chat line "Let me know if you'd like
   help applying…".

## Running it yourself
1. `python scripts/make_sample_files.py`, then in `.env`:
   `ALLOWED_DIRECTORIES=C:\Users\<you>\Documents\ARTHUR`.
2. `uvicorn app.main:app`; open http://127.0.0.1:8000 in **Chrome**.
3. Voice panel → *Speak replies: Always*; click the ear button (hands-free) and say
   **"Hey Arthur"**.
4. Follow the table above. Web results change daily, so your comparison will differ.
