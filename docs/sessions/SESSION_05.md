# Session 5 – Phases 11, 12, 13: Voice (2026-09-29)

Result: you can talk to ARTHUR, hear its answers, and call it hands-free with "Hey Arthur".
Everything runs on your computer. 338 tests passing.
Commits: `0e84f10` (speech-to-text), `116ad03` (text-to-speech), `6b4084d` (mic message),
`b624d13` (wake word).

Verified by the owner with a real microphone in Chrome: speech recognised clearly and ARTHUR
answered spoken commands.

---

## Phase 11 – Speech-to-text (faster-whisper)

**Concepts**
- **Whisper / faster-whisper** – open-source speech recognition, running locally (no audio
  leaves the PC). faster-whisper is a faster re-implementation (CTranslate2).
- **VAD (voice activity detection)** – only the parts of a recording with speech are
  transcribed: faster, and Whisper doesn't "hear" words in silence.
- **Confidence** – very unsure results become "I couldn't understand that" instead of a guess.
- **Limits** – 60 s / 10 MB per recording; clear errors for empty, too short, unreadable audio.

**Measured (3-second sentence)**: Whisper *small* on CPU **~1.5 s**; *base.en* 0.6 s. The GPU
needs NVIDIA cuBLAS (not installed) – ARTHUR now falls back to CPU even if CUDA fails *while
running*, not just while loading.

**Browser**: 🎤 click → MediaRecorder (WebM/Opus) → `POST /voice/transcribe` → text is sent.
The browser pane inside the Claude app blocks microphones – use Chrome/Edge at
http://127.0.0.1:8000 (Windows privacy switches were already "Allow" on this laptop).

## Phase 12 – Text-to-speech (Piper)

- **Piper** – fast local voices (`data/voices/*.onnx`): **alan** (British, default) and
  **lessac** (American). ~0.3 s per sentence.
- **Streaming by sentence** – finished sentences are sent to `POST /voice/speak` while the reply
  is still being written, fetched ahead and played in order: first sound **~0.25 s** after the
  first sentence (was 2.3 s before pre-loading the voice at startup).
- **Cleaning for speech** – Markdown, links, citations like `[handbook.pdf, p. 2]`, code and
  emoji are removed; "×" becomes "times", "°C" "degrees Celsius".
- **Controls** (Voice panel) – speak replies *when I talk / always / never*, voice, speed,
  volume, test. Stop / new message / mic / Escape silences ARTHUR ("barge-in").

## Phase 13 – "Hey Arthur" wake word

**How**: the browser measures loudness ~125×/s (≈0 CPU when quiet), cuts out a clip when
someone speaks, and sends it to `POST /voice/wake`. The fast *base.en* model transcribes it
(~0.6 s) and the phrase must be at the **start**: "Hey/OK/Hello Arthur…" or "Arthur, …".
"Hey Arthur, <command>" runs the command (re-read with the accurate model); "Hey Arthur" alone
→ ARTHUR says "Yes?" and listens (7 s), then goes back to waiting.

**Why not openWakeWord?** It has no pre-trained "Hey Arthur" model; training one is a project of
its own. Re-using Whisper gives the exact phrase with no training.

**Explained**
- *False positive* (wakes when not called) – measured **0 / 32** (incl. "author", "King
  Arthur", "Did you call Arthur yesterday?", "Offer them…"). ARTHUR also ignores audio while it
  is thinking or speaking, so it can't wake itself.
- *False negative* (missed "Hey Arthur") – measured **19 / 20** detected with two voices, clean
  and noisy. Saying "**Hey** Arthur" is more reliable than "Arthur" alone.
- *CPU* – loudness check is nearly free; Whisper runs only when there's sound. Noisy rooms
  (TV, music) cause more checks – switch hands-free off then.
- *Privacy* – while on, the mic stays open (browser mic indicator + "Say 'Hey Arthur'" status).
  Clips go only to ARTHUR on this PC and are never saved or logged. Off by default.

**Experiments (measured, not guessed)** – `scripts/evaluate_wake_word.py`:
| Change | Detected | False alarms |
|---|---|---|
| Baseline | 18/20 | 0/28 |
| Whisper "hotwords" hint "Hey Arthur" | **12/20** (Whisper dropped the phrase!) | 0/28 |
| No hint + accept "offer" only after a greeting | **19/20** | **0/32** |

## Bugs found and fixed this session
1. Old `.env` had `WHISPER_DEVICE=auto` → GPU chosen, then failed at run time (cublas missing).
2. The error logger crashed on characters the Windows console (cp1252) can't print.
3. The server didn't start: `create_task(gather(...))` → now a test starts the **real** app.
4. Citations like `[weather, p. 1]` were read aloud → broader citation filter.
5. First answer after a pause took ~13 s: Ollama unloads the model after 5 min →
   `OLLAMA_KEEP_ALIVE=30m`.
6. Confusing mic error ("lock icon", no mention of the Claude app) → clearer message.

## Commands
```powershell
uvicorn app.main:app --reload                # then open http://127.0.0.1:8000 in Chrome/Edge
python scripts/evaluate_wake_word.py         # wake-word accuracy
pytest
```

## Settings (`.env`)
`WHISPER_MODEL=small`, `WHISPER_DEVICE=cpu`, `WHISPER_LANGUAGE=en`, `WHISPER_WAKE_MODEL=base.en`,
`TTS_DEFAULT_VOICE=en_GB-alan-medium`, `OLLAMA_KEEP_ALIVE=30m`.

## Known limitations
- Wake word tested with synthetic voices; real accents/rooms may differ – test and tell me.
- No GPU speech recognition yet (needs NVIDIA libraries) – Phase 24.
- Hands-free mode only runs while the ARTHUR tab is open.
- A noisy "Arthur, …" without "Hey" can be missed.

## Next: Session 6 – Phases 14–15
Restricted file tools (find your resume, summarise a report – only in folders you allow) and a
Playwright browser agent with confirmation for risky actions.
