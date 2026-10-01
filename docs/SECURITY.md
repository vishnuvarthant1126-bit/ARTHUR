# ARTHUR – security model

ARTHUR runs on your own computer and can read files, browse the web and operate apps. This
page lists what could go wrong, what stops it, and which test proves it. Every defence below
is enforced in code, not by asking the language model to behave.

**Core rule:** the model can only *ask* for a tool by name. `ToolRegistry.execute` decides
whether it runs: look up → permission level → validate arguments → confirmation → timeout →
audit log.

| Level | Meaning | Behaviour |
|---|---|---|
| 0 | Read-only | runs |
| 1 | Low risk, easy to undo | runs |
| 2 | Changes something | asks you first, every time |
| 3 | Money, passwords, accounts, dangerous keys | always refused, even if you say yes |

## Who may talk to ARTHUR (incoming)

| Threat | Defence | Test |
|---|---|---|
| A website you visit sends requests to `localhost:8000` (CSRF) | Changes are accepted only when `Origin` is ARTHUR's own page | `tests/security/test_front_door.py::test_other_websites_cannot_change_anything` |
| **DNS rebinding against ARTHUR**: a site points its own name at 127.0.0.1, so Origin and Host both look "the same" | **Host allow-list**: only `localhost`, `127.0.0.1`, `::1` (+ `ALLOWED_HOSTS`), for HTTP and WebSocket | `…::test_rebound_website_cannot_use_the_api`, `…cannot_open_the_websocket` |
| A script or page floods the API | Rate limits per kind of request (chat 30/min, uploads 20/min, …) → HTTP 429 | `…::test_flooding_the_chat_is_refused` |
| Text from the web/model runs as a script in the page | Everything is HTML-escaped; `Content-Security-Policy` without `unsafe-inline`; no inline scripts or styles | `…::test_security_headers_on_every_response` |
| Another site shows ARTHUR in a hidden frame (clickjacking) | `frame-ancestors 'none'`, `X-Frame-Options: DENY` | same |
| Someone on your network reaches ARTHUR | It listens on 127.0.0.1 only. **Do not start it with `--host 0.0.0.0`** – there is no login. | – (deployment rule) |

## Where ARTHUR may connect (outgoing)

| Threat | Defence | Test |
|---|---|---|
| A page or prompt makes ARTHUR open your router, `localhost:11434`, cloud metadata (SSRF) | Only http/https; every address a name resolves to must be public; redirects re-checked | `tests/security/test_egress.py::test_unsafe_urls_are_refused`, `test_is_public` |
| **DNS rebinding**: public address when checked, private when used | `read_webpage` **pins** the checked address (`pinned_request`); the browser sends all traffic through ARTHUR's **egress proxy**, which does the lookup itself | `…::test_rebinding_cannot_redirect_a_page_fetch`, `tests/unit/test_browser.py::test_dns_rebinding_is_stopped_by_the_proxy` |
| A connection verified for site A is reused for site B at the same address | Web reading uses a client that never keeps connections open (`new_page_client`) | found and verified in a live test (Session 8) |
| The browser reaches odd services (mail, SSH) or uses UDP around the proxy | Proxy allows ports 80, 443, ≥ 1024 only; QUIC and non-proxied WebRTC disabled | `…::test_proxy_refuses` |

## What the model can be tricked into

| Threat | Defence | Test |
|---|---|---|
| **Prompt injection** in a web page, document, image, window or tool result ("ignore your rules, delete…") | Such text is passed as data; any action it triggers still needs the registry, and level ≥ 2 needs *your* yes – **even when the model obeys the injected text** | `tests/security/test_prompt_injection.py` |
| The model approves its own action | `confirmed` can only be set by the orchestrator after your reply, never through tool arguments | `tests/security/test_prompt_injection.py::test_injected_delete_still_needs_the_users_yes` |
| "ok, what is this?" counted as yes; a yes hours later | A yes must be the whole short message; questions expire after 5 minutes | `tests/security/test_confirmation_and_audit.py` |
| The model *writes* "I need your permission… reply yes" or "I deleted it" without calling a tool | Honesty checks add a visible note; nothing is pending | `tests/unit/test_agent_loop.py` |
| Wrong target: the model picks the wrong field, tab, file or reminder | Previews name the exact target; secrets in the wrong field always ask; reminders are matched by words; unknown ids are refused before asking | `test_browser.py`, `test_reminders.py`, `test_computer.py` |

## Files, apps and the screen

| Threat | Defence | Test |
|---|---|---|
| Reading outside your folders (`..`, links, `file://`, network paths) | Paths are resolved first, then checked against `ALLOWED_DIRECTORIES`; system folders, hidden files and secret-looking names always blocked | `tests/unit/test_file_tools.py` |
| Getting around the folder rule through Notepad tabs or Explorer | Notepad: only the tab ARTHUR opened. Explorer: only allowed folders, items can't be opened | `tests/unit/test_computer.py` |
| **Simulated keystrokes landing in another app.** Key presses go to whichever window has the focus; in a live test (Session 9) text meant for Notepad, with its Enter key, was typed into a chat app when that app took the focus. In a terminal that would run a command | **ARTHUR sends no keystrokes at all.** Notepad: Windows messages addressed to its text control's handle. Calculator: button presses through UI Automation. Explorer: shell actions on the checked items. Typed text is read back and compared | `tests/unit/test_computer.py::test_arthur_has_no_way_to_send_real_keystrokes`, opt-in `real_desktop` tests |
| Dangerous shortcuts | Only a short list of keys per app exists; Win-key, Ctrl+Alt+Del, Alt+F4, Shift+Delete and the clipboard are refused | `tests/unit/test_computer.py` |
| Screenshots of private things | One allowed window only; the whole screen always asks | `tests/unit/test_vision.py` |
| A reminder that acts on its own | Reminders only notify; they never run tools | `tests/unit/test_reminders.py` |
| Arbitrary programs or shell commands | There is no shell tool. Apps start from fixed Windows paths, never through a shell | `test_computer.py` |

## Secrets

| Threat | Defence | Test |
|---|---|---|
| Passwords/cards saved in memory or reminders, or typed by ARTHUR | Refused by `contains_sensitive_data` and the risk rules (level 3) | `test_long_term_memory.py`, `test_reminders.py`, `test_browser.py` |
| Secrets in the audit log | Masked by argument name **and** inside text values (`scrub_text`) | `tests/security/test_confirmation_and_audit.py` |
| `.env`, keys or `data/` in git | `.gitignore`; never committed | – |

## Dependencies
`python -m pip_audit` (1 Oct 2026):
- **oauthlib** CVE-2026-49265 – fixed by pinning `oauthlib>=4.0.0` (ARTHUR doesn't use it directly).
- **chromadb 1.5.9** – 4 advisories (CVE-2026-45829, -45830, -45831, -45833), no fixed release
  yet. All concern Chroma's **HTTP server**. ARTHUR uses Chroma embedded and starts no server,
  so they are not reachable (`test_chroma_is_only_used_embedded` fails if that changes).
  Re-run the audit when Chroma releases an update.

## Known limits (be honest about them)
- No login: anyone who can use your Windows account can use ARTHUR. That is by design for a
  local, single-user tool.
- Other programs running as you can call the API directly (no `Origin` header). A program
  on your PC can already do more than ARTHUR lets it.
- Rate limits and conversations live in memory and reset when ARTHUR restarts.
- The risk rules for buttons/fields are word lists – unusual wording on a web page may be
  classified as lower risk. Passwords and payments are also blocked by field type.
- A small local model makes mistakes. The defences assume the model can be wrong or
  manipulated, which is why none of them depend on it.

## Report a problem
This is a personal learning project. If you find a hole, open an issue describing the steps.
