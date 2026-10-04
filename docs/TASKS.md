# TASKS — Autonomous Android

_Last updated: 2026-10-04, against commit `37b3e57`. 420 tests passing._

Status key: `[x]` done · `[-]` in progress · `[ ]` not started · `[!]` blocked

Status is assigned from the code, not from intent. A task is `[x]` only if it
is implemented **and** covered by passing tests.

---

## Phase 0 — Project Setup `[x]`

- [x] Python package layout under `android_agent/`
- [x] `Settings.from_env()` with validation and redaction
- [x] ruff configuration (line length 120, py310, legacy files excluded)
- [x] pytest layout, 420 tests, no test-only dependencies
- [x] `.env.example` documenting every setting
- [x] `python -m android_agent.doctor` diagnostics
- [x] Documentation foundation (`docs/PRD|ARCHITECTURE|DESIGN|RULES|TASKS|MEMORY`)

## Phase 1 — Foundation `[x]`

- [x] `ToolSpec` / `Risk` / `ToolResult` and the JSON-schema validator
- [x] `ToolRegistry` and `build_full_registry()`
- [x] Bounded `AgentRuntime` plan/act/observe loop with per-tool timeouts
- [x] Provider-neutral `Planner` protocol + OpenAI-compatible adapter
- [x] Per-provider schema sanitisation (`models/compat.py`)
- [x] Gemini thought-signature round-tripping
- [x] `DefaultPolicy` with deterministic decisions
- [x] Approval flow with argument-hash binding
- [x] JSONL audit sink; failures carry `error_code` and `retryable`
- [x] File + console logging
- [x] Subprocess runner: argv-only, timeouts, process-group kill, logging
- [ ] **Durable approval store** — `InMemoryApprovalStore` loses pending
      approvals on restart
      - Goal: an approval survives a process restart, or expires predictably
      - Files: `android_agent/approvals/store.py`, `agent_bot.py`
      - Acceptance: a pending approval is still valid after a restart within
        its TTL; one-time use still enforced atomically
      - Depends on: PRD Open Question 5

## Phase 2 — Core Features `[x]`

- [x] 41 typed tools (6 read-only, 7 reversible, 14 sensitive-read,
      8 device-mutation, 2 external-side-effect, 4 raw-control)
- [x] Telegram front end with approval buttons and artifact delivery
- [x] Expiring SQLite sessions (idle TTL, trim at user boundaries, orphaned
      tool-call repair)
- [x] Progressive-disclosure skills (5 bundled)
- [x] Media capture filed by kind and timestamp
- [x] Scoped file read/write/send
- [x] Location: GPS-first, cached-fix fast path, accuracy honesty, staleness
      labelling, background-permission diagnosis
- [x] Phone calls: verified against telephony state, remedy named
- [x] ADB UI control: tap, swipe, key events (12 keys), type text, screenshot
- [x] Optional Needle on-device fast path (off by default)

## Phase 3 — Secondary Features

### Web console `[x]`
- [x] Token auth, lockout, rate limiting, `SameSite=Strict` session cookie
- [x] CSRF header on state-changing routes; media navigation exempt by design
- [x] Single-file UI, zero pip dependencies, offline-safe
- [x] Light/dark/system themes applied before first paint
- [x] Archived reopenable chats (cap 30) with context-lost banner
- [x] WCAG 2.2 AA contrast in both themes, keyboard paths, reduced motion

### Scheduling `[x]`
- [x] `ScheduleStore`: one-off, daily-at-local-time, interval; cap 50
- [x] Missed slots skipped, not replayed
- [x] Finished one-offs listed 24 h then purged; recurring never purged
- [x] `ScheduleRunner` with `UnattendedPolicy` and hash-bound authorisation
- [x] Refuses to leave a pending approval from an unattended run
- [x] `ScheduleService` 30 s loop; immediate first tick; crash-proof
- [x] Four scheduling tools (create / authorise / list / cancel)
- [x] Bot wiring, status-glyph reporting, artifact delivery
- [x] `python -m android_agent.schedule` one-shot tick
- [x] `scripts/install_schedule_job.sh` persisted JobScheduler job
- [x] Claim-and-lease so two tickers cannot double-run a task
- [x] Device clock injected into every system message
- [x] One-off tasks accept a clock time, resolved on device

### Remaining in this phase
- [ ] **Event triggers** — requested; deferred pending the latency decision
      - Goal: react to battery below a threshold, charging state, wifi
        connect/disconnect, foreground app change
      - Files: `android_agent/schedule/` (new condition evaluator),
        `schedule_tools.py` (schema), `store.py` (`condition` column exists
        and is currently unused)
      - Implementation notes: needs polling. ~60 s granularity while the agent
        runs; **15 min floor** when only the Android job is alive. Battery
        cost must be measured, not assumed.
      - Acceptance: "tell me when the battery drops below 20%" fires once per
        crossing, not repeatedly while below
      - Depends on: owner accepting the latency (see MEMORY.md § Open)
- [ ] Verify on-device that a persisted job fires after a real reboot with
      Termux never opened. Script and docs exist; **not yet proven on hardware**
- [ ] Scheduler support in the web console UI (tools work there; no dedicated
      view)

## Phase 4 — Multilingual `[ ]` (nothing implemented)

The product goal says "works in any language". Today nothing supports it.
Shape depends on **PRD Open Question 1**.

- [ ] Decide the model: reply-language mirroring vs. configured locale vs.
      per-message detection `[!]` blocked on Open Question 1
- [ ] Add `ANDROID_AGENT_LANGUAGE` (or equivalent) to `Settings`
- [ ] System-prompt directive: reply in the owner's language
- [ ] **Fix English-only skill routing.** Measured defect: English battery
      question loads 2063 chars of skill guidance, the Hindi equivalent 1261.
      Options: multilingual triggers, embeddings, or let the model select.
      - Files: `android_agent/skills/loader.py`, `bundled/*/skill.json`
      - Acceptance: equivalent requests in English and Hindi select the same
        skills
- [ ] String inventory and externalisation (~72 user-facing literals across
      `tools/`, `web/`, `agent_bot.py`)
- [ ] Translate the four Android-remedy messages (location permission,
      overlay permission, phantom killer, location off) — these matter most
      because they are read when something is broken
- [ ] Locale-aware date/time formatting (currently `%a %d %b`, English-only)
- [ ] Dynamic `<html lang>` and `dir` in the web console; verify RTL
- [ ] Confirm Noto fallback renders Devanagari/Bengali on the reference device
- [ ] Non-ASCII input path for `type_text` (ADB `input text` cannot do it;
      needs a clipboard-paste strategy or an IME)
- [ ] Tests: a non-English request completes the same flows as English

## Phase 5 — Reliability & Hardening `[-]`

- [x] Mutation testing adopted as the standard for regression tests
- [x] Circuit breaker on repeatedly failing live location requests
- [x] Abandon-instead-of-kill for Termux:API clients
- [x] Claim/lease concurrency safety in the scheduler
- [x] Structured failure codes in the audit trail
- [-] **Failure-path coverage for the remaining tools.** Location, calls and
      scheduling are thoroughly covered; several Termux tools only have
      happy-path tests
      - Acceptance: every tool handles empty-output, API_ERROR and timeout
- [ ] Retry policy for transient Telegram network failures (polling reconnects;
      individual sends do not retry — observed `ReadTimeout` in the field)
- [ ] Audit log rotation (`audit.jsonl` grows unbounded)
- [ ] Health check: report scheduler liveness and last tick on request
- [ ] CI workflow (parked at `/home/user/ci-workflow-to-add-manually.yml`;
      pushing it needs a PAT with `workflow` scope) `[!]`

## Phase 6 — Testing `[-]`

- [x] 420 tests across 22 files
- [x] Real-server tests for the web console
- [x] End-to-end scheduling tests through real tools
- [x] Schema/validator tests including the additionalProperties fix
- [ ] On-device smoke checklist (what to verify manually after a release)
- [ ] Soak test: scheduler running for 24 h with the phone idle

## Phase 8 — Channel connectors: Gmail `[-]`

Design in ARCHITECTURE.md § Email connector. Decisions taken 2026-10-04:
dedicated mailbox, app password over OAuth, bodies may reach the model.

- [x] Auth decided: app password + IMAP/SMTP, stdlib only, no new dependency
- [x] `channels/base.py` — Channel protocol, tidy/truncate, untrusted wrapper
- [x] `channels/gmail.py` — IMAP read + SMTP send, injectable factories so the
      whole path is testable without a network
- [x] Header and body decoding: RFC 2047 encoded-words, part charsets,
      text/plain preferred, stdlib HTML strip, character-safe truncation
- [x] `list_recent_email`, `read_email` — `sensitive_read`, BODY.PEEK so
      reading through the agent never marks the owner's mail as read
- [x] `send_email`, `reply_to_email` — `external_side_effect`, so the existing
      approval gate and argument hash apply unchanged
- [x] Reply threads correctly (In-Reply-To / References, no double "Re:")
- [x] Settings + `.env.example`; the app password never reaches logs, audit,
      `redacted()` or error text
- [x] No delete, label or settings tool exposed — asserted by a test
- [x] Dedicated email skill, with English, Hindi and Bengali triggers
- [x] Untrusted-content wrapper around every fetched body and subject
- [x] 40 tests, mutation-checked against: downgrading send to reversible,
      fetching without PEEK, reporting a refused recipient as sent, dropping
      the untrusted wrapper, and echoing the credential in an error
- [x] **Taint rule** — implemented
      - `ToolSpec.returns_untrusted_content` declares a third-party source;
        set on `read_email`, `list_recent_email`, `get_recent_sms`,
        `get_notifications`, `get_clipboard`
      - Once such a tool succeeds, the run is tainted for the rest of its
        life, and further `sensitive_read` calls need approval even though
        the owner started the run
      - Approval prompts say the request followed untrusted content
      - Unattended tainted runs deny rather than ask, via UnattendedPolicy
      - `run.tainted` is audited
- [ ] Inbox summary as a scheduled task, verified end to end on-device
- [ ] On-device verification with a real mailbox (never yet run against Gmail)

## Phase 9 — App-level automation `[ ]` (next after Gmail, not yet specified)

Needs PRD Open Questions 3–4 answered first: which apps and workflows, and
whether an accessibility service is acceptable. Today UI control is wireless
ADB only (`tap_screen`, `swipe_screen`, `send_key_event`, `type_text`,
`capture_screenshot`), which needs re-enabling after every reboot and cannot
read structured screen content.

- [ ] Specify 2–3 concrete target workflows with the owner
- [ ] Decide on an accessibility service `[!]` blocked on Open Question 4
- [ ] Decide how a screen is perceived: screenshot + vision model, or
      accessibility node tree
- [ ] Reliability design: UI automation is brittle; define how a step
      verifies it worked before continuing

## Phase 7 — Release & Operations `[ ]`

- [ ] First-run setup script: permissions checklist + verification
- [ ] Single documented install path from a clean Termux
- [ ] Backup/restore for the four state stores
- [ ] Versioning and a changelog

---

## Immediate next tasks (suggested order)

1. **Answer PRD Open Questions 1–2** → unblocks all of Phase 4.
2. **Durable approvals** (Phase 1) — the only known gap in the core safety
   model.
3. **Fix English-only skill routing** — smallest change with real
   multilingual benefit, and measurable.
4. **Verify the persisted job on hardware** — the scheduling story is
   unproven without it.
5. **Gmail connector** (Phase 8) — once Open Questions 6–8 are answered. The
   taint rule should land with it, not after.
6. **Event triggers** — once the latency trade-off is accepted.
7. **App-level automation** (Phase 9) — needs target workflows named first.
