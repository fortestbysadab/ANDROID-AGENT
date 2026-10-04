# MEMORY — Autonomous Android

_Durable context for future sessions. Last updated: 2026-10-04, commit
`37b3e57`._

---

## Project Context

**Product.** Autonomous Android — an AI agent inside Termux on an unrooted
Android phone that performs device tasks on request or on a schedule, driven
from Telegram or a local web console.

**Current stage.** Working v2, in daily use by its owner. Core runtime,
safety model, 41 tools, web console and scheduling are built and tested.
Multilingual support is **not started** despite being a stated goal.

**Environment**
- Reference device: realme RMP2402, Android 14, SDK 34, arm64, **unrooted**.
- Termux 0.118.3 + Termux:API 0.53.0, both F-Droid builds.
- Model: `gemini-3.5-flash-lite` via Google's OpenAI-compatible endpoint.
- Timezone: Asia/Kolkata (IST). The code uses the device clock, never a
  hard-coded zone.
- State in `~/telegram_agent_v2/`: `sessions.db`, `schedule.db`,
  `web_chats.db`, `audit.jsonl`, `agent.log`.
- Media in `~/storage/shared/AndroidAgent`.

---

## Important Decisions

1. **The model proposes; application code executes.** Validator → policy →
   approval → executor → audit are all deterministic. Never put the model in
   front of a shell.
2. **Risk is declared per tool** and is the only input to authorisation
   alongside actor identity.
3. **Approvals are hash-bound** (tool + version + exact arguments).
4. **Session expiry is a privacy control**, not a cache policy. Sensitive tool
   output must not linger in model context.
5. **Reopening an archived chat restores the view, not the memory**, and says
   so.
6. **Needle routes, never authorises.** `complete()` only, never `run()`.
7. **Unattended runs deny rather than auto-approve.** A scheduled risky action
   needs a one-time, hash-bound authorisation.
8. **Four separate stores** because retention rules differ.
9. **No third-party network calls from tools.** IP geolocation was built and
   removed.
10. **No accessibility service.** UI control uses wireless ADB instead; the
    accessibility grant would expose all screen content in every app.
11. **Mutation testing is mandatory** for regression tests.

---

## Current Implementation

### Working
- Bounded agent runtime with per-tool timeouts and audit.
- 41 tools; deterministic summaries; failure codes.
- Telegram front end with approval buttons, artifacts, map pins.
- Local web console: token auth, light/dark, archived chats, WCAG 2.2 AA.
- Expiring SQLite sessions with orphaned-tool-call repair.
- Five bundled skills (progressive disclosure).
- Scheduling: one-off / daily / interval, authorisation model, 24 h retention
  of finished one-offs, claim-and-lease concurrency safety, persisted Android
  job, device clock in every prompt.
- Optional Needle fast path (off by default).
- `doctor` diagnostics; structured logging.

### In progress
- Failure-path test coverage for the remaining Termux tools.
- Scheduling proven on-device after a real reboot (script exists, unverified).

### Known limitations
1. **Approvals are not durable** — a restart loses pending approvals.
2. **No multilingual support** (see below); the stated goal is unmet.
3. **Event triggers not implemented**; the `condition` column exists unused.
4. **UI control needs wireless ADB**, which must be re-enabled after reboot.
5. **Audit log never rotates.**
6. **Individual Telegram sends do not retry** (polling reconnects; observed
   `ReadTimeout` in the field).
7. **Scheduling precision is bounded by Android:** 15-minute job floor, plus
   Doze deferral. A 07:00 task fires at the first tick at or after 07:00.
8. **Tasks fire only while the agent runs**, unless the persisted job is
   installed.
9. **`type_text` cannot reliably type non-ASCII.**
10. **Legacy `bot.py` still present**, frozen and lint-excluded.

### Multilingual: measured state
- The model understands non-English input and usually replies in kind.
- All deterministic strings are English (~72 user-facing literals).
- **Skill routing is English-only and silently degrades.** Measured:
  `"what is my battery level"` → 2063 chars of skill guidance;
  `"मेरी बैटरी कितनी है"` → 1261 chars (device-control never loads).
- No locale setting; date formats are English; `<html lang>` is fixed `en`.

---

## Important Constraints

- **Unrooted.** Anything needing root is out.
- **Android kills background work.** Phantom process killer (Android 12+),
  Doze, and OEM battery management all apply. `MONITOR_PHANTOM_PROCS` is true
  on the reference device.
- **Android blocks background activity starts** (since Android 10). Calls and
  dialogs need the "Display over other apps" permission.
- **Background location needs "Allow all the time."** With "while using the
  app", Android refuses even the *cached* fix.
- **JobScheduler minimum period is 15 minutes**, and Doze stretches it.
- **Termux:API serves one location request at a time.**
- **Termux is bionic**, so published Needle wheels (glibc/musl) do not load.
- **One runtime dependency** (`pyTelegramBotAPI`); the web console adds none.

---

## Things That Must Not Change

- No shell tool, ever. No `shell=True`.
- Policy decisions must never depend on model output.
- Approval hashing must stay tool+version+arguments.
- Audit must never contain tool arguments, secrets, or human summaries that
  could quote device content.
- The web session cookie keeps `Secure`, `HttpOnly`, `SameSite=Strict`.
- Captured media stays on the device.
- `bot.py` / `config.py` stay untouched; `config.py`'s placeholder value is
  deliberate.
- A tool must never report success it cannot verify.

---

## Lessons Learned

1. **A green test that still passes when the fix is deleted is worse than no
   test.** Several tests in this project were found worthless this way —
   including one that "proved" an unattended run couldn't send an SMS, when in
   fact a pending-approval path produced the same observable result.
2. **Instrumentation beats reasoning.** Four plausible explanations for a
   location failure (background throttling, spawn style, phantom killer, cold
   GPS) were each disproved by measurement. The subprocess runner had **no
   logging at all**; adding it ended the guessing in one round.
3. **"Works in the terminal but not from the bot" means foreground vs
   background**, nearly always a permission or an Android background
   restriction — not a code difference.
4. **A cached read that is slow is a different fault from a slow device
   operation.** `-r last` does no satellite work; if it is slow or refused,
   the OS is refusing, and that is a permission problem.
5. **Killing a timed-out Termux:API client is what produces the user-facing
   "Connection refused" error screen.** Abandon instead, and reap later.
6. **Flaky tests are findings.** The scheduler double-run race appeared as a
   ~40% intermittent failure; the naive select-then-update was genuinely
   broken across processes.
7. **`str.replace` fails silently.** Four documentation edits silently did
   nothing. Always assert the edit landed, with a whitespace-tolerant regex —
   multi-word phrases wrap across lines.
8. **A confident wrong answer is worse than no answer.** The IP-geolocation
   fallback named a city 100 km away while GPS was working. Removed.
9. **Accuracy must be reported, not just position.** An 800 m network fix and
   an 8 m GPS fix presented identically is how a 2 km error becomes invisible.
10. **The model has no clock.** Without the current time in the prompt it will
    invent an offset — a task asked for at 23:10 was scheduled for 07:59.
11. **Permissive schemas must preserve what they permit.** The validator
    accepted `additionalProperties: true` and then dropped those fields,
    which would have run scheduled tools with no arguments.
12. **Stale `.pyc` files can fake a test failure** after mutation testing.
    Clear `__pycache__` when a result looks impossible.

---

## Common Pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Location works in terminal, fails from bot | Termux:API location is "while using the app" | Set Location to **Allow all the time** |
| Red "Connection refused" Termux:API screen | Client killed (our timeout) or phantom killer | Abandon-not-kill is in; enable *Disable child process restrictions* |
| Call accepted but nothing happens | Android blocks background activity starts | Grant Termux **Display over other apps**; on ColorOS also *background pop-up windows* |
| Scheduled task never fires | Agent not running and no persisted job, or battery-restricted | `bash scripts/install_schedule_job.sh`; set both Termux apps to Unrestricted |
| Scheduled time is wrong | Model computed an offset | Fixed: device clock in prompt + `at_time` resolved on device |
| Position ~2 km out | Network provider instead of GPS | Fixed: GPS first, accuracy reported |
| `ruff: No module named ruff` | Not preinstalled in a fresh sandbox | `pip install ruff` |
| Push rejected for `.github/workflows` | PAT lacks `workflow` scope | Add the workflow manually |
| `origin` missing after a snapshot | `.git/config` is not persisted | Re-add the remote before pushing |

---

## Planned: Gmail connector (decided 2026-10-04, not yet built)

- **Recommendation: IMAP/SMTP with a Gmail app password**, not OAuth. Reasons:
  zero new dependencies; no Google Cloud project or consent screen; and a
  personal OAuth app left in "testing" receives refresh tokens that expire in
  about a week, which would break unattended use. Plain-password access ended
  1 May 2025; app passwords still work with 2-Step Verification.
- **Known cost of that choice:** an app password grants the whole mailbox,
  including delete, and cannot be scoped. Blast radius is therefore bounded
  by the tool surface instead: no delete, label or settings tool will exist.
- **Revisit if** Google announces removal of app passwords, or this is ever
  distributed to anyone but its author.
- **Email introduces prompt injection**, and the **taint rule is now built**
  (2026-10-04). A tool declares `returns_untrusted_content`; once one
  succeeds the run is tainted, further sensitive reads need approval, the
  approval prompt says so, and unattended runs deny outright. It covers
  `get_recent_sms`, `get_notifications` and `get_clipboard`, which had the
  same exposure since long before email existed.
- **Summarising sends email bodies to the cloud model.** That is a privacy
  decision for the owner, tracked as PRD Open Question 7, not an assumption.

## External Integrations

**Telegram Bot API.** Long polling via `pyTelegramBotAPI`; inline keyboards for
approvals; `sendMessage` / `sendLocation` called directly over HTTPS from the
one-shot scheduler tick, because the bot process may be dead. Token in `.env`.

**OpenAI-compatible model endpoint.** Currently Gemini. Tool schemas are
sanitised per provider (`models/compat.py`); Gemini thought signatures are
captured and replayed on the OpenAI-compat path
(`tool_calls[].extra_content.google.thought_signature`) — dropping them breaks
every multi-turn tool run with a 400.

**Termux:API.** Subprocess binaries. Three failure modes that all look like
success: empty output with exit 0, an `API_ERROR` object, and a hang. One
location request at a time.

**Wireless ADB.** Same-device only, for UI control and screenshots. Must be
re-enabled after a reboot.

**Android JobScheduler.** Via `termux-job-scheduler`, job id 4242, persisted,
15-minute period. Owned by Termux:API, executed by Termux.

**Needle (optional, off).** On-device tool router. Termux wheels are
unavailable (bionic); documented fallbacks are unverified.
