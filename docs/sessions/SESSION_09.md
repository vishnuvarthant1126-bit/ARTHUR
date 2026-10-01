# Session 9 – Phases 20, 21, 22: Observability, test suite, load tests (2026-10-01)

This was the second session of the day, at the owner's request.
Result: ARTHUR measures itself (metrics and a stats page), the tests are measured (90 %
coverage, with a gate), and its limits under load are known. The session also found and
fixed a real safety defect in Phase 16: **ARTHUR no longer sends keystrokes**. 671 tests passing.
Commits: `f136bb8` (observability), `fa1ccb5` (test suite + no keystrokes), `ed9ea4a` (load tests).

---

## Phase 20 – Observability

**Concepts**
- **Logs / metrics / traces**:
  - Logs say *what happened*.
  - Metrics are *numbers over time*: how many, how fast, how often it fails.
  - Traces follow one request across steps. ARTHUR's request ID on every log line is a
    small version.
- **Counter** (only goes up), **gauge** (up and down), **histogram** (measurements sorted into
  buckets).
- **p95** is the time 95 % of requests were faster than. It describes "slow" better than an
  average. It is *estimated* from the histogram buckets, so it is only as precise as the
  bucket edges.
- **Pull model**: ARTHUR publishes plain text at `GET /metrics`. Prometheus fetches it every
  few seconds and keeps the history; Grafana draws charts.
- **Labels and cardinality**: labels come from small fixed sets, such as route *templates*
  (`/memories/{memory_id}`, never the raw path) and registered tool names (an invented name
  becomes `unknown`). They never contain user text.

**Built**
- `app/observability/metrics.py`: HTTP, chat turns, open tabs, model calls (time, first
  words, tokens, outcome ok/error/stopped), tools, safety refusals by reason, reminders,
  uptime.
- `MeteredProvider`, a wrapper around the LLM provider.
- `GET /metrics` and `GET /metrics/summary`.
- **`/dashboard.html`** ("Stats" in the header): stat tiles and tables, refreshed every 5 s.
- `deploy/`: Prometheus config, Grafana data source and a generated dashboard (13 panels),
  and a compose file. **These have not been run** (no Docker yet). A test checks that every
  dashboard query uses a metric that exists. Session 10 runs them.

## Phase 21 – Test suite

**Concepts**
- **Coverage** shows which lines ran during tests. It finds places with *no* tests; it can't
  say the tests are good. Reading the uncovered lines found real bugs (below).
- **Flaky test**: fails now and then without a code change. It is worse than no test,
  because people learn to ignore red.
- **Determinism**: a test must get the same input every run. No randomness, no real clock,
  no real network.

**Numbers**
- 90 % coverage; gate at 88 % (`pytest --cov`).
- `docs/TESTING.md` explains the layers.
- `desktop.py`: 24 % in a normal run, **89 %** with the opt-in real-desktop tests.
- Search providers 45 → 96 %; OpenAI-compatible provider 75 → 97 %.

**The flaky voice test**
- It was not reproduced in 10 full runs and 80 single runs, so the cause is unconfirmed.
- Found on the way: Piper adds randomness, giving 22 different audio clips in 60 runs of one
  sentence.
- The test now uses fixed audio and reports what it heard if it fails.

## The keystroke finding (Phase 16 redesign)
While measuring Notepad typing on the live desktop, the test text **was typed into the Claude
chat window and sent**, Enter key included, because that window took the keyboard focus.

Simulated keystrokes go to whichever window has the focus. ARTHUR checked the focus before
every 20 characters, but 20 characters plus Enter is enough to send a message or run a
command in a terminal.

**ARTHUR now sends no keystrokes at all:**
| App | How |
|---|---|
| Notepad | Windows messages sent to the handle of Notepad's own text control (`EM_REPLACESEL`, `EM_SETSEL`, `EM_UNDO`, `WM_KEYDOWN`). They can't arrive anywhere else. Typed text is read back and compared. |
| Calculator | Buttons pressed through UI Automation |
| File Explorer | Refresh / go up / delete through the Windows shell, for the checked selected items (Recycle Bin). The confirmation names the files. |

A test fails if `send_keys` or similar ever returns to `app/`.

Lesson for me: never run raw input experiments on the owner's live desktop.

## Phase 22 – Load tests

**Concepts**
- **Load test**: simulated users at the same time. **RPS** = requests answered per second.
- **Locust**: each user is a Python class with weighted tasks.
- **Echo model**: a stand-in that waits and answers with fixed words, so the test measures
  ARTHUR's code and not the GPU.
- **Throughput vs latency**: at the ceiling, more users don't get more done. They wait longer.

**Measured** (details in `docs/LOAD_TESTS.md`)
| Situation | Result |
|---|---|
| 200 users with normal pauses | 155 req/s, p95 120 ms, 0 failures |
| Stress, no pauses | ceiling ~400–450 req/s (one process, one CPU core) |
| 100 users, answers take 1.5 s | each still 1.5–1.6 s, so no queueing (async) |
| Real model, 1 / 2 / 4 chats at once | 1.0 s / 1.3 s / 2.0 s per answer. **The GPU is the limit** (~1–1.5 answers/s) |

## Bugs found and fixed this session
1. Simulated keystrokes landed in another app (above).
2. **`database is locked`** (HTTP 500, 1 in 11,000 under load): SQLite writers wait by
   sleep-and-retry and gave up after 5 s. All database work now goes through one lock.
   My first explanation was wrong (a test proved it); the second was confirmed by a test
   that fails without the fix.
3. Crashing requests (500) were **not counted** in the metrics.
4. The metering wrapper recorded "stopped" late (an extra generator layer).
5. `save_file` asked for confirmation of a save that could not work.
6. The reports folder was never created on a fresh computer.
7. SearXNG applied the result limit before dropping non-web links.
8. Development tracebacks printed **local variables** (message text, keys), and took 3 s.
9. `file:///…` given to the browser became `https://file/…` (a 7 s DNS wait).
10. Disguised numeric hosts (`2130706433`, `0x7f.0.0.1`) are refused without DNS. My first
    rule would also have refused real sites such as `bbc.de`; a test caught it.
11. `current_time`: the model asked for London time unprompted, so the description is clearer.

## Commands
```powershell
pytest --cov                                   # tests + coverage (gate 88 %)
$env:ARTHUR_DESKTOP_TESTS=1; pytest -k real_desktop
python scripts/run_load_server.py              # test server on :8001
locust -f tests/load/locustfile.py --headless -u 20 -r 5 -t 30s --host http://127.0.0.1:8001
python scripts/make_grafana_dashboard.py       # regenerate the Grafana dashboard
```
Open http://127.0.0.1:8000/dashboard.html for the stats page.

## Known limitations
- Metrics reset when ARTHUR restarts. History needs Prometheus (Session 10).
- The Prometheus/Grafana/compose files are unverified until Docker is installed.
- The voice test's earlier failures remain unexplained.
- Load tests cover HTTP only: no WebSocket chat, voice, uploads, browser or desktop.
- One process: conversations, rate limits and the scheduler live in memory.
- The model still sometimes answers "what does the window show?" from memory without looking.
- My tests left a few empty "Untitled" Notepad tabs, the Calculator open, and small test
  files in the Recycle Bin.

## Next: Session 10 – Phases 23–24
Docker Compose (ARTHUR + Prometheus + Grafana, and SearXNG if wanted) and performance.
**Docker Desktop must be installed first.**
