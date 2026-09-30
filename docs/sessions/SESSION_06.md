# Session 6 – Phases 14, 15: Files and browser (2026-09-30)

Result: ARTHUR can find, read and (with your OK) save files, but only in the folders you allowed.
It can also use its own isolated web browser: open pages, click, and type into search boxes,
asking you first for anything risky. 404 tests passing.
Commits: `ce500a8` (file tools), `0b1725e` (browser agent).

---

## Phase 14 – Restricted file tools

**Concepts**
- **Sandbox / allow-list**: ARTHUR sees only the folders in `ALLOWED_DIRECTORIES`. On this
  laptop that is Documents\ARTHUR, Documents and Downloads. Everything else doesn't exist for it.
- **Path traversal**: tricks like `..\..\Windows`, `C:/Users/../..`, `file:///C:/...`,
  network paths `\\server\share` and Windows device paths. ARTHUR **resolves** the real path
  first and checks it afterwards, so a trick can't slip through.
- **Symlinks / junctions**: shortcuts that point elsewhere. They are resolved before
  checking, and search skips them.
- **Always blocked**, even inside allowed folders:
  - Windows, Program Files, ProgramData and AppData;
  - hidden or system files;
  - `.git`, `.ssh` and `node_modules`;
  - `.env`, `*.key` and `*.pem`;
  - `id_rsa`, password-manager files, and names containing password, secret or credential;
  - `.lnk` shortcut files.

**Tools**
| Tool | Level | What |
|---|---|---|
| `find_files` | 0 | Words in the file name, newest first ("my latest resume") |
| `list_folder` | 0 | What is in one folder |
| `read_file` | 0 | PDF/DOCX/TXT/MD/CSV text (reuses the RAG readers), size-limited |
| `save_file` | 2 | .md/.txt/.csv only, never overwrites, **asks you first** |

**Verified live**
- "Find my latest resume" found `Sample_Resume_Alex_Tan_2026.pdf`, not the 2025 one.
- The skills summary was correct.
- "Summarise my project report and save it" asked first. "No" wrote nothing. "Yes" saved
  `Documents\ARTHUR\reports\arthur_report_summary.md`.
- Reading `Projects\arthur\.env` was refused because it is outside the allowed folders.

The sample files in `Documents\ARTHUR\samples` are **fictional**. They were made by
`scripts/make_sample_files.py`, and you can delete them or add your real resume.

**Small-model habits fixed**
- qwen3 filtered a .docx as "pdf", so the search now widens itself.
- It sent `file:///C%3A...` paths, which are now converted.
- It asked for 100,000 characters, which is now capped.
- It used `document_search` (uploads) for computer files, so the tool descriptions are clearer.

## Phase 15 – Playwright browser agent

**Concepts**
- **Playwright** remote-controls a real Chromium browser (`python -m playwright install
  chromium`, ~150 MB). Unlike `read_webpage`, it runs JavaScript and can click and type.
- **Isolation**: ARTHUR's browser is not your Chrome.
  - It has a fresh profile each time: no cookies, logins or history.
  - Downloads are off and service workers are blocked.
  - **Every** request the page makes (pages, images, scripts, redirects) passes the same
    private-address check as Phase 10. Routers, localhost and `file://` are blocked.
- **Snapshot**: after each action ARTHUR lists the page text plus **numbered** links,
  buttons and fields. The model says "click [9]" instead of guessing CSS selectors.
- **Risk per action**: the tool's level is raised for each call, based on what is clicked
  or typed:

| Action | Level | Result |
|---|---|---|
| Links, "Search", "Next", typing into a search box | 1 | Runs |
| Buy, order, add to cart, submit a form, send, sign in, delete, donate, other fields | 2 | Asks you |
| Pay, transfer, password/card/CVV/OTP fields, typing a card number | 3 | Refused, even if you say yes |
| Password-like text ("Hunter2pass") in *any* field | 2 | Asks you, because the model may pick the wrong field |

- **Windows detail**: Playwright starts Chromium as a subprocess. That needs the
  "Proactor" event loop, which `uvicorn --reload` doesn't use. So the browser runs in its
  own thread with its own loop.

**Verified live (qwen3:8b)**
- "Open python.org, latest release?" → "Python 3.14.7" (13 s).
- "Click Donate" asked for permission. "No" meant nothing was clicked.
- PyPI login: after a wrong first guess, the model picked the username field and ARTHUR
  asked first. "Yes" typed it. Nothing was submitted.
- Password: the model put it into the **search box** by mistake. ARTHUR now asks, and the
  preview shows the mistake ("into Search PyPI").
- PyPI search showed a bot check ("Client Challenge"). ARTHUR stopped and didn't try to get
  around it.

## Bugs found and fixed this session
1. The agent loop cuts tool results at 4000 characters. The page text filled all of it, so
   the model never saw the numbered elements and guessed [1]. Elements now come first, and
   the text fills the remaining space.
2. A click leading to a blocked address landed silently on an error page. Now ARTHUR goes
   back and says "Blocked for safety".
3. The model typed into a link. Now it is refused before asking, with a list of the real fields.
4. The model **wrote its own "I need your permission… Reply yes"** without calling a tool.
   It copies earlier replies. A new honesty check adds a note that no action is waiting.
5. The password went into the search box. Password-like text now always asks.
6. The browser now restarts itself if it was closed or crashed.
7. Live tests right after editing code hit the *old* server during reload. Restart the
   server before a live test.
8. PowerShell `Set-Content -Encoding utf8` adds an invisible BOM that broke `.env`. It was
   stripped; don't use that command for project files.

## Commands
```powershell
python -m playwright install chromium        # once (done on this laptop)
python scripts/make_sample_files.py          # fictional sample resume/report
uvicorn app.main:app --reload
pytest
```

## Settings (`.env`)
`ALLOWED_DIRECTORIES=C:\Users\User\Documents\ARTHUR;C:\Users\User\Documents;C:\Users\User\Downloads`,
`FILES_SAVE_DIR=` (blank = Documents\ARTHUR\reports), `BROWSER_ENABLED=true`,
`BROWSER_HEADLESS=true` (false = watch the window).

## Known limitations
- qwen3:8b often picks the wrong element number at first. The safety checks catch it, but
  some tasks need 2–3 tries.
- File search matches names, not contents. Use the Docs panel for searching inside documents.
- Some sites show bot checks to automated browsers. ARTHUR stops there by design.
- The browser keeps one tab. Pages behind logins aren't possible, because ARTHUR never
  types passwords.
- DNS rebinding (a site switching to a private IP between check and load) is still open
  → Phase 19.

## Next: Session 7 – Phases 16–17
Controlled computer use (screenshots, mouse/keyboard only inside allowed apps, with
confirmation) and vision (describe an image or screenshot).
