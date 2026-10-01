# Load tests (Phase 22)

Measured on 1 Oct 2026 on the development laptop (Windows 11, RTX 5060 Laptop 8 GB,
16 GB RAM), ARTHUR running as one process.

## How to run
```powershell
python scripts/run_load_server.py                # terminal 1: throw-away ARTHUR on :8001
locust -f tests/load/locustfile.py --headless -u 20 -r 5 -t 30s --host http://127.0.0.1:8001
```
`-u` users, `-r` users started per second, `-t` duration. Without `--headless` Locust opens
a web page with live charts at http://localhost:8089.

The test server is separate from your real ARTHUR: temporary database, **echo model**
(`LLM_PROVIDER=echo`, answers with fixed words after a set delay), hash embeddings, rate
limits off, no browser/desktop/vision. A load test should measure ARTHUR's own code – with
the real model in the loop every result would only say "the GPU is the limit".

Simulated users (`tests/load/locustfile.py`): 60 % chat, 20 % stats page / health checks,
20 % adding and cancelling reminders (database writes).

## Results

### 1. Normal use: users pause 0.5–3 s between actions
| Users | Requests/s | Typical (p50) | Slow (p95) | Slowest | Failures |
|---|---|---|---|---|---|
| 5 | 3.9 | 6 ms | 49 ms | 104 ms | 0 |
| 20 | 15.5 | 5 ms | 24 ms | 404 ms | 0 |
| 50 | 39.1 | 5 ms | 29 ms | 104 ms | 0 |
| 100 | 78.1 | 6 ms | 55 ms | 209 ms | 0 |
| 200 | 154.8 | 8 ms | 120 ms | 515 ms | 0 |

Throughput grows in a straight line with the number of users and response times stay flat:
ARTHUR is nowhere near its limit here.

### 2. Stress: no pauses, every user fires as fast as it can
| Users | Requests/s | Typical (p50) | Slow (p95) | Slowest | Failures |
|---|---|---|---|---|---|
| 10 | 447 | 21 ms | 43 ms | 309 ms | 0 |
| 50 | 419 | 110 ms | 210 ms | 324 ms | 0 |
| 200 | 406 | 510 ms | 730 ms | 4.0 s | 0 |

**The ceiling is about 400–450 requests per second.** More users don't get more work done –
they wait longer. That is one Python process using one CPU core; requests queue up in an
orderly way and none fail.

### 3. Slow answers: the echo model takes 1.5 s per answer (like a real model)
| Users | Chat p50 | Chat p95 | Failures |
|---|---|---|---|
| 20 | 1.5 s | 1.6 s | 0 |
| 100 | 1.5 s | 1.6 s | 0 |

With 60 chats waiting for a "model" at the same time, each still takes 1.5 s: ARTHUR adds
almost nothing and doesn't make them queue behind each other. This is what `async` buys –
while one request waits, the same process serves the others.

### 4. The real model (qwen3:8b on the GPU), short answers
| Chats at the same time | Time per answer | Answers per minute |
|---|---|---|
| 1 | 1.0 s | 60 |
| 2 | 1.3 s | 72 |
| 4 | 2.0 s (slowest 3.0 s) | 83 |

**The GPU is the real limit: roughly 1–1.5 answers per second**, about 300 times less than
ARTHUR's own code can carry. Four people asking at once each wait twice as long. For one
user (what ARTHUR is built for) this is plenty.

## What the load test found (both fixed)
1. **`database is locked` (HTTP 500), 1 request in 11,000.** SQLite allows one writer at a
   time; other writers wait by sleep-and-retry – not a fair queue – and give up after 5 s.
   Under heavy writing one request waited 6 s and failed. Fix: all database work goes through
   one lock inside ARTHUR (`Database._lock`), so writers queue in order and SQLite never makes
   anyone wait. After the fix: 0 failures in ~57,000 requests, and the slowest request
   dropped from 6.0 s to 4.0 s. Test: `tests/unit/test_database_concurrency.py` (fails
   without the lock).
2. **Crashes were missing from the metrics.** The HTTP 500 above showed "0 errors" on the
   stats page, because a crashing request skipped the counting code. Fixed in the middleware;
   test: `test_crashes_are_counted_as_server_errors`.

## Reading these numbers honestly
- The echo model does no thinking and calls no tools. These numbers are the cost of ARTHUR's
  plumbing, not of answering a question.
- Locust and ARTHUR ran on the same laptop and shared its CPU; a separate load generator
  would show somewhat higher limits.
- Rate limits were switched off. In normal use ARTHUR allows 30 chat messages per minute –
  far below anything measured here.
- Not load-tested: the WebSocket chat, voice, document upload, browser and desktop tools.

## If ARTHUR ever needed more
- More answers per second → a faster GPU, a smaller model, or a hosted model through the
  OpenAI-compatible provider. Nothing in ARTHUR's own code is the bottleneck.
- More requests per second → several worker processes (`uvicorn --workers`), which first needs
  shared storage for conversations, rate limits and the scheduler (they live in memory today).
