# Testing ARTHUR

## The commands
```powershell
pytest                        # everything that is safe to run (about 50 s)
pytest tests/unit             # fast tests with fakes (no model needed)
pytest tests/security         # the attack tests (see docs/SECURITY.md)
pytest -m "not integration"   # skip tests that need Ollama / the voice models
pytest --cov                  # + coverage report; fails if coverage is under 88 %
pytest --durations=10         # show the 10 slowest tests
ruff check . ; ruff format --check .
```

## The layers
| Layer | Where | Uses | Count |
|---|---|---|---|
| Unit | `tests/unit` | Fakes: `FakeLLM`, `FakeEmbeddings`, `FakeVision`, `FakeDesktop`, in-memory database | 515 |
| Security | `tests/security` | The same fakes; every test is an attack that must fail | 106 |
| Integration | `tests/integration` (marker `integration`) | The real Ollama model, Whisper and Piper, the real app start-up | 9 |
| Browser | `tests/unit/test_browser.py` | Real Chromium against a local test site (skipped if not installed) | 37 |
| Opt-in | see below | Your real desktop, the real vision model | 3 |

**Opt-in tests** are skipped unless you ask for them:
```powershell
$env:ARTHUR_DESKTOP_TESTS=1; pytest -k real_desktop    # real Notepad, Calculator, File Explorer
$env:ARTHUR_VISION_TESTS=1;  pytest -k live_vision     # real vision model (swaps GPU models)
```
The desktop tests send **no keystrokes**, so they are safe while you use the computer. They
leave one empty Notepad tab and one test file in the Recycle Bin.

## Coverage
Coverage = which lines of `app/` ran during the tests. It shows where there are **no** tests;
it can't tell you the tests are good.

| | Coverage |
|---|---|
| Normal run (`pytest --cov`) | **90 %** |
| `app/computer/desktop.py` in a normal run | 24 % – it drives real Windows apps, so fakes can't reach it |
| `app/computer/desktop.py` with `ARTHUR_DESKTOP_TESTS=1` | **89 %** |
| Everything except that file | ~95 % |

The gate (`fail_under = 88` in `pyproject.toml`) makes `pytest --cov` fail if coverage drops.

## Rules these tests follow
- **No real network or DNS in unit tests.** Websites are `httpx.MockTransport`; addresses are
  public IP literals. Two tests that waited 7 s on real DNS lookups were rewritten.
- **No randomness.** Piper adds random variation to speech (22 different clips in 60 runs of
  the same sentence), so the voice test switches it off.
- **No waiting for real time.** Reminders use a fake clock that is moved by hand.
- **Fakes at the edges, real code in the middle.** Tools, registry, orchestrator and API are
  the real ones; only the model, embeddings, search engine, desktop and vision are replaced.
- **A failure must explain itself.** Assertions carry what was actually seen, e.g.
  `heard 'What is 25 times 50?' with confidence 0.63`.
- **Every bug found live gets a test** that fails without the fix. Look for "seen live" or
  "found" in test docstrings.

## What the tests found in Session 9
Measuring coverage and reading the uncovered lines found real bugs, not just missing tests:
1. Simulated keystrokes landed in another app (see docs/SECURITY.md) → ARTHUR no longer sends
   keystrokes at all.
2. `save_file` asked for confirmation of a save that could not work.
3. The reports folder was never created on a fresh computer.
4. SearXNG results: the limit was applied before removing non-web links.
5. The metering wrapper recorded "stopped" late.
6. Development tracebacks printed local variables (message text, keys).
7. `file:///...` given to the browser was turned into `https://file/...` and waited on DNS.

## Known gaps
- The voice test `test_spoken_sentence_is_transcribed` failed about 1 run in 5 in earlier
  sessions. It did **not** fail in 10 full runs and 80 single runs while investigating, so the
  cause is unconfirmed. Its random input was removed and it now reports what it heard.
- `test_read_text_and_pdf` takes ~7 s inside a full run but 0.08 s alone – something else on
  the machine at that moment, not the code under test.
- Real-desktop and real-vision code is only covered when you opt in.
- The Grafana dashboard and Docker files in `deploy/` have not been run (no Docker yet).
