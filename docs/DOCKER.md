# Docker (Phase 23)

> **Status: built and run on 2 Oct 2026** (Docker Desktop 29.8, Compose 5.5, Windows 11).
> ARTHUR, Prometheus and Grafana work in containers; the results are in the table at the
> end. **Not run yet:** the optional browser build (Chromium) and the optional SearXNG
> search engine.

## The idea in simple words
- An **image** is a packed box: Linux + Python + ARTHUR's code. Built once from a recipe
  (the `Dockerfile`).
- A **container** is that box running. Throw it away, start a new one - same result.
- **Compose** (`docker-compose.yml`) starts several containers together and connects them:
  ARTHUR, Prometheus (collects numbers) and Grafana (draws charts).
- A **volume** is a storage box that survives when a container is deleted.

```
 your browser ── 127.0.0.1:8000 ──▶ [ arthur ] ──▶ host.docker.internal:11434 ──▶ Ollama (Windows, GPU)
                 127.0.0.1:3000 ──▶ [ grafana ] ──▶ [ prometheus ] ──▶ arthur:8000/metrics
```

## What runs where

| Part | In the container? | Why |
|---|---|---|
| Chat, memory, documents, reminders, web search, metrics | yes | plain Python |
| Voice (Whisper + Piper, on the CPU) | yes | models reused from `data/models`, `data/voices` |
| File tools | yes, but only the mounted `/files` folder | a container sees nothing else |
| Language models (Ollama) | **no - stays on Windows** | needs the GPU |
| Vision (pictures you upload) | yes (asks Ollama on Windows) | |
| Notepad / Calculator / Explorer control, "look at my screen" | **no** | a container cannot see the Windows desktop |
| Browser tools | off by default | needs Chromium in the image (+ ~500 MB), see below |

For everyday use on this PC, `uvicorn app.main:app` remains the full-featured way. Docker is
for a clean, repeatable setup - and for Grafana.

## Files
| File | What it is |
|---|---|
| `docker/Dockerfile` | the recipe for ARTHUR's image |
| `.dockerignore` | what Docker may not even look at (`.env`, `data/`, `.git`...) |
| `docker-compose.yml` | ARTHUR + Prometheus + Grafana (+ optional SearXNG) |
| `deploy/docker-compose.observability.yml` | only Prometheus + Grafana; ARTHUR stays on Windows |
| `deploy/prometheus/prometheus.yml`, `prometheus.host.yml` | where Prometheus fetches the numbers (container / Windows) |
| `deploy/grafana/` | Grafana's data source and the "ARTHUR" dashboard |
| `docker/searxng/settings.yml` | settings for the optional private search engine |

## Safety decisions
- **Ports only on `127.0.0.1`.** Inside the container the server listens on `0.0.0.0` (it
  must, to be reachable from outside the box), but Compose publishes it on this PC's
  loopback address only. Nothing is reachable from the Wi-Fi.
- **No secrets in the image or in Compose.** The Dockerfile copies only `app/` and
  `frontend/`; `.env` and `data/` are also in `.dockerignore`. Compose passes a fixed list of
  settings, never the whole `.env`.
- **Ordinary user, no extra powers:** `USER arthur`, `cap_drop: ALL`, `no-new-privileges`,
  no access to the Docker control socket.
- **ARTHUR's own checks still apply:** Host allow-list (`ALLOWED_HOSTS: arthur` adds exactly
  the name Prometheus uses), Origin check, rate limits, tool permission levels.
- **Linux system folders are off limits** for the file tools (`/etc`, `/proc`, `/app`...),
  like `C:\Windows` on Windows.
- Versions of Prometheus, Grafana and SearXNG are fixed, so a later download cannot quietly
  bring different software.

## Checked on every test run (without Docker)
`tests/unit/test_docker_files.py` (28 tests, part of the normal `pytest` run):
valid YAML; every published port starts with `127.0.0.1:`; no privileged mode, host network
or Docker socket; no secret values and no `env_file`; pinned image versions; every mounted
config file exists; every setting name in Compose is a real ARTHUR setting and the values
validate; Prometheus target = Compose service name = allowed Host; Grafana data source and
dashboard agree; the Dockerfile copies only code, ends as a non-root user and starts the
server on the published port; no module needs Windows just to be imported; web page file
names match exactly (Linux is case-sensitive).

These tests keep the promises true when the files are edited later. Whether the setup
*works* was checked by running it - see "Results of the first run".

## Step 1 - Docker Desktop
Installed on this PC (2 Oct 2026). On another PC: install "Docker Desktop for Windows"
from docker.com (administrator rights, keep "Use WSL 2" ticked, restart), then check with
`docker --version` and `docker compose version`. Docker Desktop uses a few GB of RAM while
it runs - close it when you don't need it.

## Step 2 - run
```powershell
cd C:\Users\User\Projects\arthur
# .env has TZ=Asia/Singapore, so reminders use your local time
docker compose up -d --build         # first build ~4 minutes, later ones seconds
docker compose ps                    # arthur should become "healthy"
docker compose logs -f arthur
```
Then open http://127.0.0.1:8000 (stop the normal `uvicorn` first - both want port 8000).
The containers restart together with Docker Desktop until you run `docker compose down`,
and while they run, port 8000 is taken - `uvicorn` on Windows will not start.

The container has **its own, empty memory** (a Docker volume). Your memories and documents
in `data/` on Windows are untouched and not visible inside.

Stop: `docker compose down`. Delete the container's data as well: `docker compose down -v`.

### Options
| Want | Do |
|---|---|
| Other folder for the file tools | `.env`: `ARTHUR_DOCKER_FILES=C:/Users/User/Documents/ARTHUR` |
| Browser tools | `.env`: `ARTHUR_DOCKER_BROWSER=true`, then `docker compose up -d --build` |
| Private search engine | `.env`: `ARTHUR_DOCKER_SEARCH=searxng` and `SEARXNG_SECRET=<long random text>`, then `docker compose --profile search up -d` |
| Monitoring only (ARTHUR on Windows) | see the top of `deploy/docker-compose.observability.yml` |

## Results of the first run (2 Oct 2026)

| # | Check | Result |
|---|---|---|
| 1 | `docker compose config` | OK; all ports resolve to `127.0.0.1` |
| 2 | Build | OK on the first try; image **1.51 GB**; every Python package has a Linux build |
| 3 | Start | `arthur` healthy after ~20 s; models warmed up through the container in 13 s |
| 4 | Reaches Ollama on Windows | **Yes**, although Ollama listens on 127.0.0.1 only - no `OLLAMA_HOST` change needed |
| 5 | Chat, tools | Answers, calculator, current time (+08), web search with sources |
| 6 | Memory survives a restart | "Remember..." → restart and three rebuilds → still recalled (volume works) |
| 7 | Not reachable from the network | The PC's Wi-Fi address refuses ports 8000, 3000 and 9090 |
| 8 | Prometheus + Grafana | Target `arthur:8000` UP; the "ARTHUR" dashboard shows data in every panel |
| 9 | File tools | Save asked for permission, then the note appeared in `data/docker-files/reports` on Windows; `/etc/passwd` refused |
| 10 | Voice | Speech 0.15 s; transcription 1.4 s per sentence - **after a fix** (below) |
| 11 | Reminders | "at 5 pm" = 5 pm Singapore time; one that came due with no tab open was delivered when the page opened, across rebuilds |
| 12 | Front-door checks | Wrong Host → 421, other website → 403, same as on Windows |
| 13 | Speed and memory | Same as on Windows: small talk 0.35–0.8 s, tool question ~1.1 s. Memory: ARTHUR 930 MB, Grafana 290 MB, Prometheus 30 MB |
| 14 | Monitoring-only setup | Prometheus in Docker reached ARTHUR on Windows while it listened on 127.0.0.1 only |

### What the run found (and no static check could)
1. **Voice was broken in the container.** `requirements.txt` had no upper limits, so the
   image got PyAV 19 (released days earlier) while Windows had 18. Version 19 removed an
   option the speech library uses, and every clip failed with "Couldn't read this audio".
   Fixed with `av<19`. The same would have hit a fresh install on Windows.
2. **Blank answers.** Asked to "open Notepad", ARTHUR answered with nothing: its
   instructions said "use open_app", but that tool does not exist in a container, and the
   model went silent. Now the instructions are built once at start-up from the tools that
   really exist (`prompts.system_prompt`) - the missing parts are replaced by "not available
   here" - and an empty model answer becomes a visible message instead of silence.
   This also covers `BROWSER_ENABLED=false` or `COMPUTER_USE_ENABLED=false` on Windows.

### Still open
- **Browser build** (`ARTHUR_DOCKER_BROWSER=true`): not run. Known risk: Chromium's own
  sandbox usually needs extra settings in a container with `cap_drop: ALL`.
- **SearXNG profile**: not run.
- Versions in `requirements.txt` are mostly open-ended ("at least X"). A lock file with exact
  versions would make the image fully repeatable - finding 1 shows why that matters.
- Cosmetic: Grafana draws the "Uptime" tile in red.
