# ARTHUR – Personal Multimodal AI Agent

A modular, local-first AI assistant: voice + text, tools, memory, RAG,
planning, browser/computer control, with a permission-based security model.

> Status: Phase 0 – environment setup. Full documentation arrives as features land.

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
