# ARTHUR – Personal Multimodal AI Agent

A modular, local-first AI assistant: voice + text, tools, memory, RAG,
planning, browser/computer control, with a permission-based security model.

> Status: Phase 1 – text chat API over a local LLM. Full documentation arrives as features land.

## Requirements
- Python 3.12
- [Ollama](https://ollama.com) with `qwen3:8b` and `nomic-embed-text` pulled
- Git; Docker Desktop (from Phase 23)

## Quick start (Windows PowerShell)
```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
```

macOS / Linux: `python3.12 -m venv .venv && source .venv/bin/activate`

## Run
```powershell
uvicorn app.main:app --reload
```
Interactive API docs: http://127.0.0.1:8000/docs

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service status and LLM reachability |
| `POST /chat` | `{"message": "Hello Arthur"}` → `{"response": "...", "model": "...", "latency_ms": 412.0}` |

Errors always look like `{"error": {"type": "llm_unavailable", "message": "..."}, "request_id": "..."}`
(503 LLM unreachable · 504 LLM timeout · 502 bad LLM output · 422 invalid input).

## Test
```powershell
pytest              # unit tests + live Ollama test (skipped if Ollama is off)
ruff check .        # lint
```
