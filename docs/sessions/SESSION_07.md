# Session 7 – Phases 16, 17: Computer use and vision (2026-09-30)

This was the second session of the day, at the owner's request.
Result: ARTHUR can use Notepad, Calculator and File Explorer with your "yes" for every action.
It can also look at pictures and app windows with a second, image-capable model. 440 tests passing.
Commits: `74bcfc1` (computer use), `4faab88` (vision).

---

## Phase 16 – Controlled computer use

**Concepts**
- **UI Automation** is the accessibility system screen readers use. Every window is a tree of
  named controls ("Button 'Seven'", "Document 'Text editor'"). ARTHUR gets them **numbered**,
  like the browser's links, instead of guessing mouse coordinates.
- **"Invoke" instead of clicking**: buttons are pressed through UI Automation, so the real
  mouse never moves.
- **Re-check at the moment of acting**: the window may have changed since ARTHUR looked, so
  the rules are checked again right before every action.
- **Chunked typing**: text goes in 20-character pieces. Before each piece ARTHUR checks the
  right window still has the focus, Notepad still shows ARTHUR's tab, and Stop wasn't pressed.

**The rules (owner chose: Notepad, Calculator, File Explorer)**
| App | ARTHUR may | Never |
|---|---|---|
| Notepad | work in the **tab it opened itself** | see or switch to your tabs, use the Save dialog (use `save_file`) |
| Calculator | press buttons, type sums, read the display | – |
| File Explorer | list and select items while **every tab** shows an allowed folder | open items (could run a program), see `.env`/key/password files |

| Action | Level |
|---|---|
| open_app, read_window | 1 / 0, runs |
| click, type, press a key | 2, **asks every time** |
| Win-key shortcuts, Ctrl+Alt+Del, Alt+F4, Shift+Delete, Ctrl+V/C/X, passwords, card numbers | 3, refused |

**Found on this laptop, and why the rules look like this**
- Notepad had ~20 of your files open as tabs, **including `.env`**. Reading "Notepad" would
  have gone around the folder sandbox.
- Win11 Notepad renames a new tab after its first line. So "only Untitled tabs" didn't work,
  and ARTHUR now remembers the Windows ID of the tab it created.
- A minimized Notepad has no text area in UI Automation, so ARTHUR brings it to the front first.
- Calculator shares a window class and process with Settings, so it is matched by title too.
- `explorer.exe` also runs the taskbar and desktop, so only real folder windows count.
- Win11 Explorer tabs share one window, so *all* tabs must be inside allowed folders.

**Verified live (qwen3:8b)**
- "Open Notepad and write a shopping list" opened a new tab and showed the text. It typed
  after "yes".
- "Use the Calculator app for 348 × 27": ARTHUR asked, typed, then read the display (9,396).
- Explorer listed `samples`. Select asked. Delete asked with "moves to Recycle Bin". "No"
  deleted nothing, and all 3 files are still there.
- Win+R was refused (level 3). A password was never typed. `C:\Windows\System32` was outside
  the allowed folders.

## Phase 17 – Vision

**Concepts**
- **VLM (vision-language model)** takes an image and a question and answers in text.
  `qwen2.5vl:7b` (6 GB) is good at reading text in images.
- **GPU swapping**: qwen3 (5.2 GB) and qwen2.5vl (6 GB) don't fit in 8 GB together, so Ollama
  swaps them. The vision model is kept only 2 minutes (`VISION_KEEP_ALIVE`).
- **Images are untrusted**: text in a picture is content, never instructions. The answer
  reaches qwen3 as tool data.
- **Window-only screenshots** (PrintWindow) capture only that app's pixels, even if other
  windows cover it.

| Tool | Level |
|---|---|
| `describe_image` (picture in your allowed folders) | 0 |
| `look_at_screen` (one allowed app's window) | 1 |
| `look_at_screen` (whole screen) | 2, asks first |

API: `POST /vision/describe` (upload an image and a question; never stored).

**Measured**
| Step | Time |
|---|---|
| First vision question (unload qwen3, load qwen2.5vl) | 29 s (10 s once cached) |
| Next vision question | 0.5 s |
| qwen3 back for the next chat turn | ~10 s |
| Whole chat turn with a picture | ~27–35 s |

**Verified live**
- The cafe receipt picture (fictional sample) gave the items and a total of SGD 11.20.
- "Take a screenshot of the Calculator window": dark theme, blue equals button (correct).
- "Look at my whole screen" asked first. "No" meant no screenshot was taken.
- The live test read "INVOICE 4721" from a generated picture.

## Bugs found and fixed this session
1. qwen3 **copied ARTHUR's permission message** instead of calling a tool, again. The history
   now stores it as a neutral note, and copied notes are flagged too. In a fresh chat the
   model calls `press_key` correctly.
2. The model called `delete_memory` with a **file name**. ARTHUR asked "delete memory (not
   found)?". Unknown ids are now refused before any question.
3. The model said the Calculator display showed "0" **without looking**. `read_window` now
   says to always look first.
4. The model claimed a **"light theme"** from text-only data. `read_window` now says it holds
   no visual information.
5. It used the built-in calculator tool when asked for the Calculator *app*. The prompt
   now separates the two.
6. Calculator kept the previous number (712×7), so typed sums now start fresh (Esc).
7. Explorer reported an older window, so the window ARTHUR opened last is preferred.
8. Pressed keys showed as `***` in the audit log (the filter treats "key" as secret), so the
   argument was renamed `shortcut`.
9. A new prompt word ("ins**tea**d") broke a memory test that checks "tea" isn't leaked. Reworded.
10. The focus check sometimes ran before Windows finished switching, so it now waits up to 1 s.

## Commands
```powershell
ollama pull qwen2.5vl:7b                          # done on this laptop
python scripts/make_sample_files.py               # adds Sample_Receipt_Cafe.png
$env:ARTHUR_DESKTOP_TESTS=1; pytest -k real_desktop   # real Calculator test (opt-in)
$env:ARTHUR_VISION_TESTS=1; pytest -k live_vision     # real vision model test (opt-in)
```

## Settings (`.env`)
`COMPUTER_USE_ENABLED=true` (emergency off switch), `COMPUTER_ALLOWED_APPS=notepad;calculator;explorer`,
`VISION_ENABLED=true`, `VISION_MODEL=qwen2.5vl:7b`, `VISION_KEEP_ALIVE=2m`.

## Known limitations
- qwen3:8b sometimes copies earlier replies instead of calling a tool. ARTHUR flags it and
  nothing happens, but you have to ask again.
- In combined requests ("open X and look at it") the model often reads text instead of
  taking a screenshot. Ask "take a screenshot of…" directly.
- Vision questions take ~30 s because of GPU swapping. A GPU with more VRAM (or a smaller
  chat model) would avoid it.
- A Notepad window screenshot also shows your tab names. It stays local, but be aware.
- Explorer can't open files or go into folders by clicking. ARTHUR opens a new window at the
  folder instead.
- My test runs left some "Shopping list" Notepad tabs and Explorer windows open. Close them
  anytime.

## Next: Session 8 – Phases 18–19
Scheduler and reminders ("remind me at 5 pm") and the full security system (DNS rebinding
fix, rate limits, security review of everything so far).
