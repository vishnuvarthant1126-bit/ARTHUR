# Docker (Phase 23)

> **Status: written and checked on paper, not run yet.** Docker Desktop is not installed on
> the development machine, so no image has been built and no container has been started.
> What *was* checked (without Docker) is listed below; the first real run has its own
> checklist at the end. Until then, treat every command on this page as "should work".

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

## What was checked without Docker
`tests/unit/test_docker_files.py` (28 tests, part of the normal `pytest` run):
valid YAML; every published port starts with `127.0.0.1:`; no privileged mode, host network
or Docker socket; no secret values and no `env_file`; pinned image versions; every mounted
config file exists; every setting name in Compose is a real ARTHUR setting and the values
validate; Prometheus target = Compose service name = allowed Host; Grafana data source and
dashboard agree; the Dockerfile copies only code, ends as a non-root user and starts the
server on the published port; no module needs Windows just to be imported; web page file
names match exactly (Linux is case-sensitive).

Also checked by hand: the image tags exist on Docker Hub (2 Oct 2026).

**Not checked, because it needs Docker:** that the image builds, that all Python packages
install on Linux, that the container reaches Ollama, file permissions on mounted folders,
voice inside the container, Grafana showing data, SearXNG, Chromium.

## Step 1 - install Docker Desktop (only you can do this)
1. Download "Docker Desktop for Windows" from docker.com and run the installer (needs
   administrator rights). Keep "Use WSL 2" ticked.
2. Restart the PC when asked. On first start Docker may install/update WSL and ask you to
   accept its licence terms (free for personal use).
3. Check in PowerShell:
   ```powershell
   docker --version
   docker compose version
   ```
Docker Desktop uses a few GB of RAM while running; this laptop has 16 GB and the GPU model
needs none of it, but close it when you don't use it.

## Step 2 - first run
```powershell
cd C:\Users\User\Projects\arthur
# optional, in .env:   TZ=Asia/Singapore      (so reminders use your local time)
docker compose up -d --build         # first build: several minutes
docker compose ps                    # arthur should become "healthy"
docker compose logs -f arthur
```
Then open http://127.0.0.1:8000 (stop the normal `uvicorn` first - both want port 8000).

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

## First-run checklist (Session 10b)
Work through this once Docker is installed; fix what fails and update this page.

1. `docker compose config` prints the merged file without errors.
2. `docker compose build` finishes. Note the image size (`docker images arthur`).
   *Risk:* a Python package without a Linux build.
3. `docker compose up -d`; `docker compose ps` shows `arthur` as healthy.
4. http://127.0.0.1:8000/health says `"reachable": true`.
   *Risk:* the container cannot reach Ollama. Docker Desktop normally forwards
   `host.docker.internal` to programs listening on 127.0.0.1. If not: set the Windows
   environment variable `OLLAMA_HOST=0.0.0.0` and restart Ollama - but then Ollama is open to
   the local network, so only on a trusted network (or add a firewall rule).
5. Chat answers; first answer is fast (warm-up works through the container).
6. "Remember that..." → `docker compose restart arthur` → still remembered (volume works).
7. From another device on the Wi-Fi, `http://<PC address>:8000` does **not** open.
8. http://127.0.0.1:9090/targets shows `arthur` as UP; Grafana (http://127.0.0.1:3000,
   dashboard "ARTHUR") shows numbers after a few messages.
9. File tools: save a note → it appears in `data\docker-files\reports` on Windows.
   *Risk:* the `arthur` user may not write to the mounted folder.
10. Voice: speak a message, hear the answer. *Risk:* `/voices` is read-only and `/models`
    must be writable for a first download.
11. A reminder "in 2 minutes" arrives; "at 5 pm" shows the right local time (TZ).
12. Optional: browser build - *known risk:* Chromium's own sandbox usually needs extra
    settings inside a container with `cap_drop: ALL`; SearXNG profile.
13. Measure: time to first token and memory use inside the container vs. on Windows
    (`scripts/profile_chat.py`, `docker stats`), and add the numbers to docs/PERFORMANCE.md.
