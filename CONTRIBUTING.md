# Contributing to ARTHUR

Thanks for your interest! ARTHUR is a personal learning and portfolio project, but issues,
ideas and pull requests are welcome.

## Getting started
1. Follow the installation steps in the [README](README.md#5-installation).
2. Run the checks once to see that everything is green:
   ```powershell
   pytest                       # ~60 s; tests that need Ollama skip when it is off
   ruff check . ; ruff format --check .
   ```
3. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/SECURITY.md](docs/SECURITY.md)
   before changing anything in `app/agent`, `app/tools` or `app/security`.

## Ground rules (non-negotiable)
These are what make ARTHUR safe to run on a personal computer. A change that weakens one of
them will not be merged.
- **The model never executes anything directly.** Every action is a `Tool` that goes through
  `ToolRegistry.execute`: permission level, argument validation, confirmation for level ≥ 2,
  timeout, audit log.
- **No arbitrary shell commands, no keystrokes into other windows.** A test forbids
  `subprocess` shells, `send_keys`, `pyautogui` and `SendInput` in `app/`.
- **Level 3 (payments, passwords, card data) is always refused**; it can't be configured away.
- **Untrusted text is data**: web pages, documents, tool results and image text are never
  followed as instructions.
- **No secrets or personal data in the repository**: `.env`, `data/`, keys, real documents.
  `.env.example` holds placeholders only.

## Adding a tool
1. Create a class in `app/tools/` with `name`, `description` (tell the model *when* to use it),
   a Pydantic `input_model`, a `permission_level` and, if needed, `timeout_seconds`.
2. Set `parallel_safe = True` only if it just reads **and** shares no state with other calls.
3. Raise the level per call with `required_level()` when some arguments are riskier than others.
4. Register it in `app/tools/defaults.py`. Mention it in `SYSTEM_PROMPT` only if the model needs
   guidance; if the feature can be missing, add it to `OPTIONAL_PARTS` in `app/agent/prompts.py`.
5. Write tests with the fakes in `tests/conftest.py` (`FakeLLM(script=[tool_call(...), "answer"])`):
   - the happy path;
   - invalid arguments;
   - the permission behaviour;
   - one attack (in `tests/security` if it is a defence).

## Code style
- Python 3.12, type hints everywhere, `ruff` for linting and formatting (line length 100).
- Comments explain **why**, in plain language. The project is meant to be readable by learners.
- Measure before optimising (`scripts/profile_chat.py`) and write the numbers down in `docs/`.
- Experiments that talk to a running ARTHUR use a **temporary data folder**
  (`DATABASE_PATH`, `VECTOR_STORE_PATH`, `DOCUMENTS_PATH`), never your real memory.

## Commits and pull requests
- One meaningful commit per change, in the style `feat: …`, `fix: …`, `docs: …`, `test: …`.
- The pull request says what changed, why, and how it was tested: tests, plus a live check if
  the change is visible in the browser.
- `pytest --cov` must stay above the 88 % gate.

## Reporting a security problem
Please don't open a public issue for a vulnerability. Contact the maintainer privately through
the contact details on their GitHub profile, with steps to reproduce. You'll get an answer
before anything is published.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
