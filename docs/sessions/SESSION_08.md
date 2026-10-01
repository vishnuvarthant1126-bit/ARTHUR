# Session 8 – Phases 18, 19: Reminders and the security system (2026-10-01)

Result: "Remind me at 5 pm to call mum" works and survives restarts. Every way into and out
of ARTHUR was reviewed, the known gaps were closed, and each defence has a test.
618 tests passing.
Commits: `f544067` (reminders), `615982a` (security).

---

## Phase 18 – Scheduler and reminders

**Concepts**
- **Scheduler**: a background loop that wakes every 5 seconds, asks the database "is anything
  due?", and delivers it. It's the same idea as cron jobs and phone alarms.
- **Persistence**: reminders are rows in SQLite (`reminders` table, stored in UTC), so a
  restart loses nothing.
- **Delivered means received**: a reminder counts as delivered only when an open tab got it.
  If ARTHUR was off or no tab was open, it is delivered when you return, marked "late".
- **Time parsing in Python, not in the model**: models are unreliable at date arithmetic. The
  model passes your words unchanged ("5pm", "in 20 minutes") and `app/scheduler/when.py`
  calculates the moment. "At 5" means the next 5 o'clock. Unclear or past times are refused.
- **Reminders only tell, never act**: an unattended timer must not be able to run tools.

**What you can say**
`in 20 minutes` · `in 1 hour 30 minutes` · `5pm` · `17:30` · `noon` · `tonight` ·
`tomorrow 9am` · `friday 3pm` · `2026-10-05 14:30` · `5 october 9am` ·
`every day at 8am` · `every weekday 9am` · `every monday 8am`

| Tool | Level |
|---|---|
| `list_reminders` | 0 |
| `set_reminder` | 1 (easy to undo) |
| `cancel_reminder` | 2, asks first |

Also built:
- `GET/POST/DELETE /reminders`.
- A **Reminders** panel: add with two fields, cancel with ✕.
- A ⏰ message in the chat, with a chime and speech if voice replies are on.
- "⏰" in the tab title until you look.

**Verified live**
- "In 1 minute" was delivered 4 s after it was due.
- 5 pm and "every weekday 8am" were set with exact times.
- A reminder that became due while ARTHUR was **switched off** arrived right after the restart.
- Cancel asked first.

## Phase 19 – Full security system

Full table: `docs/SECURITY.md` (threat → defence → test).

**Concepts**
- **DNS rebinding (outgoing)**: a site's name answers with a public address when ARTHUR checks
  it and with `192.168.1.1` when ARTHUR connects. Fix: **pin** the address. Look it up once,
  check it, and connect to exactly that address.
  - `read_webpage` puts the IP in the URL and keeps the name in the `Host` header and the TLS
    name, so certificates are still verified.
  - The browser can't be pinned from outside, because Chromium looks names up itself. So all
    its traffic goes through ARTHUR's own **egress proxy** (`app/security/egress_proxy.py`).
    A browser behind a proxy asks the proxy to connect, and the proxy does the lookup, check
    and pinning. HTTPS stays encrypted end to end; the proxy only copies bytes.
- **DNS rebinding (incoming)**: a website points *its own* name at 127.0.0.1, and your browser
  then sends that site's requests to ARTHUR. `Origin` and `Host` both say `evil.example`, so
  the old "Origin must equal Host" check passed. Fix: a **Host allow-list** (only localhost,
  127.0.0.1, ::1).
- **Rate limiting** (sliding window): at most N requests per minute per kind of request.
- **Content-Security-Policy**: the browser itself refuses to run any script that isn't
  ARTHUR's own file, even if some text slipped past the HTML escaping.
- **Defence in depth**: every rule assumes the layer before it can fail. The model in
  particular is assumed to be wrong or manipulated.

**Found during the review (real issues, now fixed)**
1. `"ok, what is this?"` **counted as yes** to a pending action, because only the start of the
   message was checked. A yes must now be the whole short message, and questions expire after
   5 minutes.
2. With pinning, the connection pool only sees IP addresses. A connection TLS-verified for
   site A was **reused for site B** on the same address, without checking B's certificate.
   Web reading now never keeps connections open.
3. The Host/Origin check could be passed by a rebound website (see above).
4. Secrets inside ordinary arguments (e.g. the text of a refused `type_text`) went into the
   audit log in clear text. They are now masked by value.
5. Plain-http pages refused by the proxy arrived as a normal "403" page. They are now
   reported as blocked.

**Dependency audit** (`python -m pip_audit`)
- oauthlib CVE-2026-49265 → pinned `>=4.0.0`.
- chromadb: 4 advisories, no fix yet. All are about Chroma's HTTP **server**. ARTHUR uses
  Chroma embedded, so they don't apply; a test fails if that changes.

**Verified live**
- A request with `Host: evil.example` got 421.
- A cross-site POST got 403.
- The 31st chat request in a minute got 429.
- The page works under the CSP (chat, streaming, Markdown) with no violations.
- python.org loads through the proxy in 2 s.
- `localtest.me` (a real public name pointing at 127.0.0.1) was refused by browser and
  read_webpage.
- "ok, what is this?" confirmed nothing.

## Bugs found and fixed this session (besides the security list)
1. Asked to cancel "the email one", the model cancelled **"call mum"**. It had renumbered the
   list 1, 2 while the ids were 2, 3. `cancel_reminder` now takes the reminder's *words* and
   Python finds the match; ambiguous or unknown words are refused before asking.
2. Parser: "in half an hour" was 90 minutes, and "in 5 years" was read as "5 o'clock".
3. Windows doesn't understand addresses like `http://2130706433/` (Linux reads them as
   127.0.0.1). Both outcomes refuse, and the test accepts both.

## Commands
```powershell
pytest tests/security                 # the security pack (101 tests)
python -m pip_audit                   # known vulnerabilities in dependencies
```

## Settings (`.env`)
`REMINDER_CHECK_SECONDS=5`, `ALLOWED_HOSTS=` (extra host names, normally empty),
`RATE_LIMIT_ENABLED=true`, `RATE_LIMIT_CHAT_PER_MINUTE=30`.

## Known limitations
- Reminders need an open ARTHUR tab to be shown. There are no Windows notifications yet;
  late ones arrive when you come back.
- Reminders only notify. "Every morning tell me the weather" (a scheduled *task*) is not
  built; it would need its own safety rules.
- "in 1 month" means 30 days.
- No login. ARTHUR listens on 127.0.0.1 only, so never start it with `--host 0.0.0.0`.
- Rate limits reset on restart.
- Wikipedia answers 403 to ARTHUR's client name (their policy, not a bug in pinning).
- One timing-sensitive voice test fails now and then when the machine is busy. Fix it in
  Phase 21.

## Next: Session 9 – Phases 20–22
Observability (metrics, Prometheus/Grafana), the full test suite (coverage, the flaky voice
test) and Locust load tests.
