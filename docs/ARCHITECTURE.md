# ARCHITECTURE — Autonomous Android

_Last updated: 2026-10-04, written against commit `37b3e57`._

> `docs/AGENT_ARCHITECTURE.md` is the original design blueprint and is kept as
> written. It contains a **proposed** package layout that was never adopted.
> **This file documents the code as it actually exists.** Where the two
> disagree, this file is correct.

---

## Architecture Overview

A single Python process on an unrooted Android phone, inside Termux. Two
front ends (Telegram, local web) feed one shared runtime. A language model
acts as an **untrusted planner**: it proposes typed tool calls, and only
deterministic code validates, authorises, executes and audits them.

```text
Telegram update ─┐
                 ├─► identity check ─► session load ─► skill selection
Web console POST ┘                                          │
                                                            ▼
                                             planner (cloud LLM or Needle)
                                                            │ proposes typed calls
                                                            ▼
                               JSON-schema validation (tools/base.py)
                                                            ▼
                               deterministic policy (policy/engine.py)
                                      ALLOW │ REQUIRE_APPROVAL │ DENY
                                            ▼         ▼
                                      executor   approval store ─► owner taps
                                            │                          │
                                            ▼◄─────────────────────────┘
                              bounded execution with per-tool timeout
                                            ▼
                              structured observation ─► audit event
                                            ▼
                              loop (bounded) or final reply

Scheduler (in-process loop, or Android JobScheduler when the agent is dead)
        └─► claim due tasks ─► same policy and executor ─► report to owner
```

The model never sees a shell. There is no `run_shell` tool and no code path
that passes model text to an interpreter.

## Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.10+ | Termux-provided |
| Telegram front end | `pyTelegramBotAPI >= 4.20, < 5` | the only runtime dependency |
| Web front end | `http.server.ThreadingHTTPServer` + one HTML file | **zero** pip dependencies |
| Model | any OpenAI-compatible endpoint | currently `gemini-3.5-flash-lite` via Google's OpenAI-compat path |
| Optional local model | Needle 3 | off by default; Termux wheel caveat below |
| Storage | SQLite (stdlib `sqlite3`), WAL | four separate databases, by concern |
| Device access | Termux:API binaries, wireless ADB | subprocess with argv arrays, never a shell string |
| Audit | JSONL append-only | |
| Lint | ruff (line length 120, py310) | `requirements-dev.txt` |
| Tests | `unittest` via pytest, 420 tests | no test-only dependencies |

Deliberate dependency minimalism: the web console adds none, and the agent
runs with a single third-party package.

## Project Structure (actual)

```text
ANDROID-AGENT/
├── agent_bot.py                 # Telegram front end
├── android_agent/
│   ├── __main__.py              # universal command: python -m android_agent
│   ├── app.py                   # composition root: builds everything once
├── bot.py, config.py            # legacy v1, untouched, lint-excluded
│   ├── config.py                # Settings.from_env, validated
│   ├── doctor.py                # configuration + connectivity diagnosis
│   ├── agent/
│   │   ├── runtime.py           # AgentRuntime: plan/act/observe loop
│   │   └── session.py           # SqliteSessionStore, idle TTL
│   ├── models/
│   │   ├── base.py              # Planner protocol, PlannerResponse, ToolCall
│   │   ├── openai_compatible.py # cloud adapter
│   │   ├── compat.py            # per-provider schema sanitisation
│   │   └── needle.py            # optional on-device router
│   ├── tools/
│   │   ├── base.py              # ToolSpec, Risk, ToolResult, validator
│   │   ├── registry.py          # trusted catalogue
│   │   ├── catalog.py           # build_full_registry()
│   │   ├── termux.py            # _run() subprocess runner + core tools
│   │   ├── termux_extra.py      # location, calls, media, clipboard, …
│   │   ├── adb.py               # UI control, screenshot, app list
│   │   ├── files.py             # scoped file read/write/send
│   │   ├── media.py             # capture paths, library description
│   │   └── schedule_tools.py    # 4 scheduling tools
│   ├── policy/engine.py         # DefaultPolicy, UnattendedPolicy
│   ├── approvals/store.py       # InMemoryApprovalStore (not durable)
│   ├── schedule/
│   │   ├── store.py             # ScheduleStore + task model
│   │   ├── runner.py            # executes due tasks
│   │   ├── service.py           # background tick loop
│   │   └── __main__.py          # one-shot tick for JobScheduler
│   ├── skills/
│   │   ├── loader.py            # keyword-triggered progressive disclosure
│   │   └── bundled/<skill>/     # SKILL.md + skill.json
│   ├── observability/
│   │   ├── audit.py             # JsonlAuditSink, MemoryAuditSink
│   │   └── logging.py           # file + console logging
│   └── web/
│       ├── __main__.py          # web entry point
│       ├── server.py            # routes, WebApp, Handler
│       ├── security.py          # token validation, sessions, rate limit
│       ├── archive.py           # ChatArchive (reopenable chats)
│       └── ui.html              # single-file UI, inline CSS/JS
├── scripts/
│   ├── install_schedule_job.sh  # persisted Android JobScheduler job
│   └── probe_location.py        # diagnostic
├── tests/                       # 420 tests, mirroring modules
└── docs/
```

## System Components

### AgentRuntime (`agent/runtime.py`)
- **Responsibility:** bounded plan → act → observe loop.
- **Inputs:** user text, actor id, chat id, prior messages.
- **Outputs:** `RunOutcome` (status, text, tool results, pending approvals).
- **Depends on:** Planner, ToolRegistry, Policy, AuditSink, SkillRouter, clock.
- Injects the device's current date/time/timezone into every system message.
- `execute_approved()` revalidates tool version and argument hash.
- `_record_outcome()` is the single audit path for both execution routes.

### ToolSpec / validator (`tools/base.py`)
- Declares name, description, JSON schema, `Risk`, handler, timeout,
  idempotency.
- The validator rejects unknown fields unless `additionalProperties: true`, in
  which case extra fields are **preserved** (a bug fixed in `a6593e2`: they
  were previously dropped silently).

### Subprocess runner (`tools/termux.py::_run`)
- argv arrays only; `stdin=DEVNULL`; per-call timeout; bounded output.
- `start_new_session=True` so a timeout can kill the whole process group —
  an orphaned `termux-api` helper is what makes Termux:API show the owner a
  "Connection refused" error screen.
- `kill_on_timeout=False` for location reads: abandoning instead of killing
  lets the Termux:API app deliver to a live client, with a 120 s reaper.
- Logs timeouts, non-zero exits and the exit-0-with-no-output case.

### Policy (`policy/engine.py`)
- `DefaultPolicy(owner_id)`: deny non-owner → deny `critical` → approve
  `external_side_effect` → approve indirect `sensitive_read` → else allow.
- `UnattendedPolicy(inner)`: converts REQUIRE_APPROVAL into DENY with an
  explanation. Used for scheduled natural-language runs.

### Scheduler (`schedule/`)
- `ScheduleStore`: SQLite, autocommit, `busy_timeout=10000`. `claim_due()`
  selects and leases inside one `BEGIN IMMEDIATE` so two tickers cannot both
  run a task.
- `ScheduleRunner`: enforces the approval hash for fixed tool tasks, runs
  prompt tasks under `UnattendedPolicy`, refuses any run that produces a
  pending approval, reports every outcome.
- `ScheduleService`: 30 s loop; ticks immediately on start; a failing tick
  cannot kill the thread.
- `__main__.py`: one-shot tick for JobScheduler; posts to Telegram over HTTPS
  because the bot process may be dead.

### Web console (`web/`)
- `ThreadingHTTPServer`, localhost by default, mandatory token.
- `AuthManager`: constant-time token compare, lockout, session TTL, rate limit.
- Cookie `aa_session`, `Secure`, `HttpOnly`, `SameSite=Strict`.
- CSRF: `X-Android-Agent: 1` required on state-changing routes. Deliberately
  **not** on `GET /api/media/file`, which is a top-level navigation and cannot
  carry a custom header; `SameSite=Strict` is what protects it.

## Data Model

```text
sessions(chat_id PK, messages JSON, updated_at, …)        sessions.db
conversations(conversation_id PK, title, created_at,
              updated_at, message_count, transcript)       web_chats.db
tasks(task_id PK, description, task_kind, tool_name,
      arguments JSON, prompt, schedule_kind,
      interval_seconds, daily_time, next_run_at, enabled,
      created_at, last_run_at, last_status, run_count,
      approved_hash, condition JSON)                       schedule.db
audit events (append-only JSON lines)                      audit.jsonl
```

Separation is deliberate: sessions expire as a privacy control, archives hold
rendered transcripts only (never model context), schedules must outlive both.

## API / Service Contracts (web console)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/` | none | UI |
| GET | `/api/ping` | none | liveness |
| GET | `/api/state` | cookie + header | transcript, session, conversations |
| GET | `/api/conversations` | cookie + header | |
| GET | `/api/media`, `/api/media/file?name=` | cookie (no header) | inline, `nosniff` |
| POST | `/api/login`, `/api/logout` | token / cookie | |
| POST | `/api/message`, `/api/approval`, `/api/new` | cookie + header | |
| POST | `/api/conversations/open\|delete\|clear` | cookie + header | |

Errors return JSON `{"error": "..."}` with a correct HTTP status. Body size is
capped at 64 KiB.

## Authentication & Authorization

- **Telegram:** the owner's chat id, from `.env`. Any other chat is ignored.
- **Web:** a mandatory shared token, then a session cookie. Rate limiting and
  lockout on repeated failures.
- **Authorization:** one role (owner). The policy engine is the only authority;
  the model has none.

## State Management

Model context lives in `sessions.db` with an idle-sliding TTL — expiry is a
**privacy control**, so sensitive tool output cannot linger in a prompt.
Reopening an archived web chat restores the view only and resets the session,
surfaced as a `context_lost` banner.

## Error Handling

1. Tools return `ToolResult.error(summary, code=…, retryable=…)` — they do not
   raise into the runtime.
2. The runtime wraps handlers in a timeout; an exception becomes
   `tool_exception`.
3. Audit records `error_code` and `retryable`; the human summary is excluded
   because it can quote device content.
4. Error text names the exact remedy where one is known (four Android
   failure classes are covered by name).
5. A tool must not claim success it cannot verify (`place_phone_call` reads
   the telephony state back).

## Security

**Threat model.** The language model is untrusted. The owner is trusted. The
network is untrusted. Other apps on the device are untrusted.

- **No shell tool**; argv arrays only; no `shell=True` anywhere.
- **Schema validation before execution**, with bounded strings and enums.
- **Policy is data-driven and deterministic**, never model text.
- **Approval binding:** sha256 over tool name + version + arguments.
- **Secrets** live only in `.env` (git-ignored). Audit redacts arguments.
  Documentation stores no secrets.
- **Media** stays on device.
- **No third-party calls.** An IP-geolocation fallback was built and removed.
- **Accessibility service: not used.** UI control goes through wireless ADB
  instead. An accessibility service can read all screen content in every app,
  which is a much larger grant; adopting one is Open Question 4.
- **Android permissions the agent depends on** (the owner grants these; the
  agent cannot):
  - Termux:API Location → *Allow all the time* (background use)
  - Termux "Display over other apps" (dialer/dialog activity starts)
  - Termux + Termux:API Battery → Unrestricted (scheduled jobs)
  - Developer options → Disable child process restrictions (phantom killer)

## Reliability

- **Per-tool timeouts**, enforced by the runtime, independent of the tool.
- **Process-group kill** on timeout to avoid orphaned Termux:API helpers;
  **abandon-instead-of-kill** for location so the app can still deliver.
- **Circuit breaker:** after a failed live location request, live requests are
  skipped for 5 minutes and the cached fix is used.
- **Scheduler:** claim/lease prevents double-runs; the lease expires so a
  killed process retries; missed slots are skipped rather than replayed; a
  failing task never stops the others; a failing reporter never corrupts state.
- **Reconnect loop** around Telegram polling.
- **Mutation testing** is the project's standard: a regression test that still
  passes when the fix is removed is treated as worthless. Every bug fix in the
  log was verified by deliberately reintroducing the bug.

## Multilingual Support (current state: not implemented)

What exists: the model is multilingual, so requests are often understood and
replies usually mirror the user's language.

What does not:

1. **Skill routing is English-only.** `SkillRouter.instructions_for` lowercases
   the request and substring-matches English triggers. Measured: an English
   battery question loads 2063 characters of guidance; the Hindi equivalent
   loads 1261 — the device-control skill never fires.
2. **All deterministic strings are English.** Tool summaries, error messages
   with remedies, approval prompts, scheduler reports, web console UI. These
   are exactly the strings the owner reads when something breaks.
3. **No locale configuration.** Nothing in `Settings`.
4. **`type_text` cannot reliably type non-ASCII** (ADB `input text`).
5. **Date/time formatting** uses `%a %d %b`, which is English-only.

Planned direction is in TASKS.md Phase 4; the shape depends on Open Question 1.

## Email connector (planned)

### Why IMAP/SMTP with an app password is the recommendation

| | App password + IMAP/SMTP | Gmail API + OAuth |
|---|---|---|
| New dependencies | **none** (`imaplib`, `smtplib`, `email`) | `google-auth`, `google-api-python-client` + transitive |
| Setup | enable 2SV, generate a 16-char password | Cloud project, consent screen, client secret on device |
| Scope | **full mailbox, including delete** | `gmail.readonly` + `gmail.send` |
| Verification | none | `gmail.send` is *sensitive*; `gmail.readonly` is *restricted* and carries an annual CASA security assessment for published apps |
| Unattended lifetime | until the account password changes | a personal app left in "testing" gets refresh tokens that expire in about a week |
| Revocation | revoke that one app password | revoke the grant |

Plain-password access died on 1 May 2025; app passwords still work with
2-Step Verification, and Google calls them "not recommended" without
announcing removal.

**Recommendation: app password for v1**, because the OAuth path costs a Google
Cloud project, a client secret shipped to the device, and — decisively for an
unattended agent — weekly refresh-token expiry unless the app goes through
verification. Revisit if Google sets a removal date, or if this ever ships to
anyone but its author.

**Mitigating the wider credential.** The app password permits delete; the
agent must not. Blast radius is bounded by the tool surface, not the
credential:

- No delete, no label-modify, no settings tool is implemented.
- Reading is `sensitive_read`; sending is `external_side_effect`.
- The credential lives in `.env`, is never logged, audited, or echoed.
- Optional: point the agent at a secondary mailbox that the primary forwards
  to (PRD Open Question 8).

### Untrusted content and the taint rule (implemented)

Email put third-party text into the planner's context, but SMS, notifications
and the clipboard had done so all along with no protection. A tool now
declares `returns_untrusted_content` when its output is written by someone
other than the owner. That is a property of the **data source**, not of the
risk level: `get_location` is sensitive but nobody else authors it, while
`get_recent_sms` hands the model text a stranger composed.

Once such a tool succeeds, the run is **tainted** for the rest of its life:

- Further `sensitive_read` calls require approval, even though the owner
  started the run — the proposal may now be the stranger's idea rather than
  theirs.
- `read_only` and `reversible` tools are unaffected, so the agent stays
  usable after reading one email.
- `external_side_effect` already required approval; the prompt now adds that
  the request followed untrusted content, so the owner knows *why* to look
  twice.
- Under `UnattendedPolicy` the escalation becomes a denial, so a scheduled
  run cannot be steered at all.
- A failed read does not taint: nothing entered the context.
- `run.tainted` is audited with the tool that caused it.

This is defence in depth, not a proof. A determined injection can still ask
for something that looks reasonable; what it cannot do is act without the
owner seeing the real arguments first.

### Shape

```text
android_agent/channels/
  base.py        # Channel protocol: list, fetch, send
  gmail.py       # IMAP read + SMTP send, stdlib only
android_agent/tools/email_tools.py
  list_recent_email     sensitive_read
  read_email            sensitive_read
  send_email            external_side_effect   (approval + hash)
  reply_to_email        external_side_effect   (approval + hash)
```

Settings: `ANDROID_AGENT_EMAIL_ADDRESS`, `ANDROID_AGENT_EMAIL_APP_PASSWORD`,
optional `ANDROID_AGENT_EMAIL_IMAP_HOST/PORT`, `…_SMTP_HOST/PORT` so a
non-Gmail IMAP account works unchanged.

**Multilingual requirements, built in from the start rather than retrofitted:**
decode RFC 2047 encoded-word headers; honour the part charset and fall back
safely; prefer `text/plain` and strip HTML with the stdlib parser; never
assume ASCII; truncate on character boundaries, not bytes.

**Reliability:** per-call timeouts; one retry on a transient IMAP/SMTP error
then a clear failure; bounded fetch count and body size; never report a send
as successful unless the SMTP transaction was accepted.

## App-level automation (planned, staged)

### Perception

`uiautomator dump` returns Android's accessibility node tree over the ADB
connection the project already uses. It is the same data an accessibility
service receives, minus live events. Each node carries `resource-id`, `text`,
`content-desc`, `class`, `bounds`, `clickable` and `enabled` — enough to find
"the Send button" rather than guessing a pixel.

Implementation details that matter, from practitioners who hit them:

- Dump **to a file then read it back** (`uiautomator dump /data/local/tmp/ui.xml`
  then `cat`). Piping to `/dev/tty` intermittently returns empty or truncated
  XML.
- Dumps fail on some animating screens ("could not get idle state"); retry
  once after a short settle, then report rather than tapping blind.
- Invisible nodes persist in the tree: a dialog's elements can be present
  while no dialog is shown. Filter on `clickable`/`enabled` and bounds inside
  the screen, never on existence alone.

### Element matching, in priority order

1. `resource-id` — stable across languages and most app updates.
2. `content-desc` — usually localised, but set deliberately by developers.
3. visible `text` — last resort.

**This ordering is a multilingual requirement, not just robustness.** A
workflow matching the literal string "Send" breaks the moment the phone's
language changes; `resource-id` does not. Any text matching must be
Unicode-aware and must not casefold with ASCII assumptions.

### Action

The model never supplies coordinates. It selects an **element reference**
from the parsed screen, and application code resolves that to the centre of
the element's bounds and taps it. This removes an entire class of failure —
and of risk, since a model cannot invent a tap on something it was never
shown.

### The act / verify loop

```text
read screen  ->  model picks an element and an action
             ->  policy check
             ->  act
             ->  read screen again
             ->  did the expected thing change?
                   yes -> next step
                   no  -> abort and report the screen we did not expect
```

A workflow carries a **step budget**. Blind retry loops on a phone that can
spend money are not acceptable; stopping and reporting is.

### Safety

- **Screen content is untrusted.** A dump contains other people's messages,
  so the screen-reading tool sets `returns_untrusted_content` and taints the
  run, exactly like email. An app showing "tap Send to confirm" must not be
  able to steer the agent.
- **Risk classes:** `read_screen` is `sensitive_read` (it can see anything on
  display, including other apps' content); `tap_element` and `type_into` stay
  `raw_control`.
- Nothing bypasses the existing gates: a workflow that ends in sending a
  message still meets the approval prompt.
- Workflows are **stored recipes**, not free-form improvisation each run, so
  what a scheduled automation will do is reviewable in advance.

### Stage 2: accessibility service

Worth building only once real workflows show what stage 1 cannot do. It needs
a separate Android app, because `BIND_ACCESSIBILITY_SERVICE` is a
signature-level permission that only the system may bind, and the service
must be enabled by hand in Settings. In exchange: no wireless ADB (so it
survives a reboot untouched), event-driven triggers, and faster reads.

The cost is a standing grant to read every screen in every app, including
banking and password managers. That is the largest permission this project
would hold, and it is why stage 1 exists first — to find out how much of the
value arrives without it.

## Script-generated documents (design, not yet built)

The agent writes a Python script; the script writes the file. This is the
only way to get a real report — a table with totals, a chart, a layout the
owner can argue with — because a fixed template cannot know what an expenses
report should look like.

**It is also arbitrary code execution by an untrusted planner**, which this
project has refused from the start. Treating it as "just another tool" would
be dishonest. What makes it acceptable is containment, and the strength of
the containment decides how much friction the owner must accept.

### The concrete risk

A script runs as the Termux user. Without isolation it can read
`~/.env`, which holds the Telegram bot token, the LLM API key and the Gmail
app password — the last of which grants the whole mailbox. Exfiltration is
four lines of Python. Scrubbing environment variables does not help: the
file is on disk.

### Layers

1. **Filesystem isolation — `proot`** (`pkg install proot`). The script sees
   its own workspace and a read-only Python; `$HOME` and the state directory
   are not in its view. This is the layer that actually contains the risk.
2. **Resource limits**, always: `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`,
   `RLIMIT_NPROC`, plus a wall-clock timeout. Stops a runaway loop filling
   the phone or draining the battery.
3. **Scrubbed environment**: no `ANDROID_AGENT_*` variables are passed.
4. **One workspace per document**, named by document id, so scripts cannot
   read each other's working files.
5. **Output by copy**: the script writes into its workspace; the agent copies
   the declared output into the files folder. The script never holds a handle
   to shared storage.

### Friction follows containment

- **With `proot`:** the script cannot reach secrets, so generation runs
  without an approval prompt each time.
- **Without `proot`:** the script can read `~/.env`, so running one is an
  approval-gated action and the full script is shown before it runs.

That asymmetry is deliberate: the owner should not be asked to rubber-stamp
code they will stop reading by the third document, and should be asked when
there is genuinely nothing else protecting them.

## Architecture Decisions

```text
Decision: Model proposes, application executes
Date: 2026-09
Context: An LLM with shell access on a personal phone is unbounded risk.
Decision: Typed tool registry; validator, policy, approvals, executor, audit
          are all deterministic code.
Alternatives: LLM-generated shell; function-calling straight to handlers.
Reason: Lets safety be tested independently of the model.
Consequences: Every capability needs an explicit tool; more code, but every
              action is auditable and testable.
```

```text
Decision: Four separate SQLite stores rather than one
Date: 2026-09 / 2026-10
Context: Sessions expire for privacy; archives are a UI record; schedules must
         outlive both; audit is append-only.
Decision: sessions.db, web_chats.db, schedule.db, audit.jsonl.
Alternatives: One database with a type column.
Reason: Different retention rules. Mixing them makes "expire for privacy"
        either unsafe or impossible.
Consequences: Four connections; no cross-store transactions (not needed).
```

```text
Decision: Needle is a router, not the brain
Date: 2026-09
Context: Needle 3 selects tools and grounds arguments; it is not a chat model.
Decision: NeedleRouter implements the Planner protocol; complete() only.
Alternatives: Needle.run() executing callables itself.
Reason: run() would bypass validator, policy, approvals and audit.
Consequences: Confidence routes, never authorises. Zero runtime changes needed.
```

```text
Decision: Unattended runs deny rather than auto-approve
Date: 2026-10
Context: Scheduled runs have nobody to answer an approval prompt.
Decision: UnattendedPolicy converts REQUIRE_APPROVAL to DENY; fixed tool tasks
          may carry an owner-approved argument hash instead.
Alternatives: Auto-approve in scheduled context; skip the task silently.
Reason: Auto-approval would make the scheduler a way to bypass every gate.
Consequences: Risky scheduled actions need a one-time authorisation step.
```

```text
Decision: Claim-and-lease for due tasks
Date: 2026-10
Context: The in-process loop and the Android job can tick simultaneously.
Decision: claim_due() selects and leases inside one BEGIN IMMEDIATE.
Alternatives: A lock file; trusting the two never to overlap.
Reason: A concurrency test failed ~40% of runs against the naive version —
        two connections both ran the same task, which for SMS means sending
        twice.
Consequences: Tasks carry a 5-minute lease; a killed process retries.
```

```text
Decision: Structured screen reading before an accessibility service
Date: 2026-10-04
Context: Blind coordinate tapping is unreliable and unsafe; the owner chose
         an accessibility service for structured perception.
Decision: Stage it. `uiautomator dump` over existing ADB first; accessibility
          service later, driven by what stage 1 cannot do.
Alternatives: Build the APK immediately; screenshot plus a vision model.
Reason: uiautomator returns the same node tree with no new APK, no permanent
        all-screens grant, and no new runtime dependency. A vision model
        would be slower, cost tokens per step, and send screen contents -
        including other people's messages - to the cloud.
Consequences: Stage 1 still needs wireless ADB re-enabled after each reboot
              and cannot react to events. Those two gaps are the concrete
              case for stage 2, rather than an assumption.
```

```text
Decision: No third-party network calls from tools
Date: 2026-10
Context: An IP-geolocation fallback was added for when Location is off.
Decision: Removed entirely.
Alternatives: Keep it behind an opt-in flag.
Reason: It reported a city ~100 km away while the device's own GPS was
        working, and it leaked the public IP to an external service.
Consequences: With Location off and no cached fix, the agent says so.
```
