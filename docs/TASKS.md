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
- [x] Location card in the web console: coordinates, accuracy, Open in Maps,
      and an embedded map loaded only on tap so the page stays offline-safe
- [ ] Scheduler support in the web console UI (tools work there; no dedicated
      view)

## Phase 4 — Multilingual `[ ]` MOSTLY UNNECESSARY (assessment corrected 2026-10-04)

The original assessment was wrong. Tool summaries are English, but they are
inputs to the model, which replies in the owner's language — so the
conversational path already works in Bengali, Hindi and English. The owner
has declined general i18n work on that basis, which is reasonable.

What remains is narrow and specific:

- [ ] **Multilingual skill triggers** — the one functional gap. A Hindi
      request loads 1261 characters of skill guidance where the English
      equivalent loads 2063; the device-control skill never fires. The model
      still understands the request, but operates with less guidance.
      - Files: `android_agent/skills/bundled/*/skill.json` (one line each)
      - Precedent: the email skill already ships English, Hindi and Bengali
        triggers and is covered by a test
      - Acceptance: equivalent requests in English and Hindi select the same
        skills
      - Effort: small
- [ ] Strings that bypass the model and so stay English regardless of the
      request: approval prompts, scheduled task reports, slash-command
      output, operational replies, web console UI. Only worth doing if the
      owner finds them intrusive in practice.
- [ ] `type_text` cannot reliably type non-ASCII (ADB `input text`). Needs a
      clipboard-paste strategy. Independent of the rest of this phase.

Dropped from this phase as unnecessary: a translation catalogue, locale
configuration, and externalising the ~72 deterministic tool strings.

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

## Phase 10 — Documents (script-generated) `[-]`

The agent writes a Python script; the script writes the file. The first
attempt used fixed Markdown templates, which can only ever emit a title and
paragraphs — the owner's expenses report came out bland, correctly reported
as wrong, and the templates were deleted rather than left as dead code.

- [x] `documents/sandbox.py` — proot isolation, resource limits, scrubbed
      environment, one workspace per document, output copied out
- [x] Workspaces under `$PREFIX/tmp`, outside `$HOME`, so hiding home does
      not hide the script's own folder
- [x] Workspace cleared before each run (a reused one made revision 2 look
      like a script that wrote nothing — caught by a test)
- [x] `documents/reader.py` — PDF, XLSX, PPTX and text extraction, plus
      reporting which optional libraries a script may import
- [x] Tools: create, revise, show script, list, read
- [x] Risk follows containment: with proot, writing is DEVICE_MUTATION; with
      no proot it is EXTERNAL_SIDE_EFFECT so the owner sees the code first
- [x] Script failures return the traceback and write nothing; a failed
      revision leaves the previous version intact
- [x] Documents skill rewritten around scripts and *design* — tables with
      totals, charts, margins — not just correct words
- [x] 48 tests, mutation-checked against ungating without proot, an unscrubbed
      environment, not hiding home, and reporting failures as success
- [ ] Verify on-device: proot isolation and a real reportlab PDF
- [ ] Optional libraries the owner may want: `pip install pypdf matplotlib`

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

## Phase 9 — App-level automation `[ ]` DEFERRED (owner, 2026-10-04)

**Deferred at the owner's request**, to avoid building an Android APK.
Recorded for accuracy: **stage 1 needs no APK** — `uiautomator dump` runs
over the wireless ADB already in use. Only stage 2 requires an installed
app. If this is picked up again, stage 1 is unblocked and needs no new
permission.

Owner chose structured perception (accessibility-style). Staged after
research: stage 1 gets the same node tree through existing ADB; stage 2 adds
the accessibility service once stage 1 shows what is missing. Design in
ARCHITECTURE.md § App-level automation.

### Stage 1 — structured screen reading over ADB

- [ ] `read_screen` tool — `uiautomator dump` to a file, read back, parse
      - Files: `android_agent/tools/screen.py`, `android_agent/ui/tree.py`
      - Notes: dump to `/data/local/tmp/ui.xml` then `cat`; `/dev/tty` is
        intermittently empty or truncated. Retry once on "could not get idle
        state", then report rather than act blind.
      - Risk: `sensitive_read`, `returns_untrusted_content=True`
      - Acceptance: returns a compact element list, not raw XML; invisible
        and off-screen nodes filtered out
- [ ] Element matching by `resource-id` > `content-desc` > `text`
      - Acceptance: a workflow keyed on `resource-id` survives switching the
        phone's language; text matching is Unicode-aware
- [ ] `tap_element` / `type_into_element` — act on an element reference, never
      a model-supplied coordinate
      - Acceptance: the model cannot tap something that was not in the last
        screen read
- [ ] Verify-after-act: re-read and confirm the expected change
      - Acceptance: an unexpected screen aborts and is named in the report
- [ ] Step budget per workflow, with a clear abort reason
- [ ] Stored workflow recipes (reviewable before a scheduled run uses one)
- [ ] Wire into scheduling so a recipe can run on a timer
- [ ] Tests against recorded XML dumps from the real device, including a
      dialog-overlay screen and a mid-animation failure

### Stage 2 — accessibility service `[!]` blocked on Open Question 10

Only worth building once stage 1 has proven its limits.

- [ ] Decide after stage 1: is re-enabling ADB after reboot, or the absence
      of event triggers, actually blocking real use?
- [ ] Minimal Android app exposing the node tree and gesture injection
      (`BIND_ACCESSIBILITY_SERVICE` is signature-level; it cannot live in
      Termux and must be enabled by hand in Settings)
- [ ] Decide the Termux ↔ service interface
- [ ] Document exactly what the grant exposes before asking for it

### Blocked on the owner

- [ ] **Name two or three real workflows** `[!]` Open Question 9. The design
      changes materially: messaging needs text entry and a recipient check,
      a bill payment needs reading a confirmation back, data entry needs
      scrolling.

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
7. **App-level automation stage 1** (Phase 9) — `read_screen` is the
   unblocking piece and needs no decision from the owner; the workflow
   recipes that sit on top of it do.
