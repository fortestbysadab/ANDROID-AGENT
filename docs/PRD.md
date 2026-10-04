# PRD — Autonomous Android

_Last updated: 2026-10-04. Written against the code at commit `37b3e57`._

> This describes an **existing, partly-built** system. Everything in "Core
> Features" is marked with what actually works today. Nothing here is a
> requirement invented by the author of this document; unresolved items are in
> **Open Questions** rather than being guessed at.

---

## Product Overview

**Product name:** Autonomous Android (repository: `ANDROID-AGENT`; runtime
package `android_agent`, v2 entry point `agent_bot.py`).

**One-line description:** An AI agent running inside Termux on an unrooted
Android phone that performs repetitive device tasks on request or on a
schedule, driven from Telegram or a local web console.

**Problem.** Routine phone tasks — checking status, capturing media, reading
notifications, sending a daily message, fetching location — require picking up
the device, unlocking it, and tapping through apps. Existing automation tools
are either rigid (fixed macros) or unsafe (a shell the model can type into).

**Goal.** Automate as many repetitive Android tasks as possible, quickly,
reliably, in any language the owner speaks, without handing a language model
unbounded control of the device.

**Non-goals** (explicit, from the existing architecture blueprint and code):

- Screen recording (`docs/AGENT_ARCHITECTURE.md` excludes it).
- A generic `run_shell` tool. No tool accepts free-form shell input.
- Multi-user or multi-tenant operation. Exactly one owner.
- Root. Everything works on a stock, unrooted device.
- Cloud storage of device data. Captured media stays on the phone.
- Replacing the legacy `bot.py`; it remains untouched for reference.

---

## Target Users

**Primary user:** the phone's owner, a single technical individual who has
Termux installed and can grant Android permissions. Currently one real user.

**User needs**

- Act on the phone while away from it, or without unlocking it.
- Say what they want in ordinary words rather than memorising commands.
- Schedule repeating work and trust it to happen.
- Know whether an action actually happened, not merely that it was attempted.

**User pain points observed during development** (these drove real fixes)

- Silent failures: a tool reporting success for an action Android blocked.
- Unexplained failures: "it works in the terminal but not from the bot".
- Wrong-but-confident answers: a location 2 km out presented as a position.
- Work lost when Android kills Termux in the background.

---

## User Stories

- As the owner, I want to ask "how much battery is left?" in my own words, so
  that I do not have to remember a command syntax.
- As the owner, I want to ask for my location and be told how accurate it is,
  so that I know whether to trust it.
- As the owner, I want risky actions such as calls and SMS to ask me first, so
  that a model mistake cannot contact someone on my behalf.
- As the owner, I want to schedule a task once or daily in plain language, so
  that routine work happens without me.
- As the owner, I want a scheduled risky action to ask me **once** when I
  create it and never again, so that automation is not constant nagging.
- As the owner, I want to see what is scheduled and cancel it conversationally,
  so that I do not need to manage ids by hand.
- As the owner, I want to use the agent from a browser as well as Telegram, so
  that I am not dependent on one transport.
- As the owner, I want failures to tell me what to fix, so that I am not
  reverse-engineering Android behaviour.
- **(Not yet met)** As a non-English speaker, I want to use the agent in my own
  language and get replies in it, so that it is usable day to day.

---

## Core Features

### 1. Natural-language device control — **working**

**Description.** The owner sends a request; a language model proposes typed
tool calls; deterministic code validates, authorises, executes and audits them.

**Requirements**

- The model is an untrusted planner. It may propose; only application code may
  execute.
- Every tool has a JSON schema; arguments are validated before the handler runs.
- 41 tools across Termux:API, ADB and local files.

**Acceptance criteria**

- A proposed call with invalid arguments never reaches a handler. ✔ tested
- A tool denied by policy is never executed regardless of model confidence.
  ✔ tested
- Every execution emits an audit event. ✔ tested

### 2. Deterministic safety policy — **working**

**Description.** `DefaultPolicy` decides ALLOW / REQUIRE_APPROVAL / DENY from
the tool's declared risk and the actor, never from model text.

| Risk | Count | Decision |
|---|---|---|
| `read_only` | 6 | allow |
| `reversible` | 7 | allow |
| `sensitive_read` | 14 | allow if directly requested, else approval |
| `device_mutation` | 8 | allow |
| `external_side_effect` | 2 | **approval always** |
| `raw_control` | 4 | allow (ADB-gated) |
| `critical` | 0 | **deny** |

**Acceptance criteria**

- A non-owner actor is denied before risk is considered. ✔ tested
- Approval is bound to a hash of tool + version + exact arguments; a mismatch
  refuses execution. ✔ tested

### 3. Approvals — **working, with a known durability gap**

One-time, expiring (300 s) confirmations delivered as Telegram buttons or web
console prompts. **Limitation:** `InMemoryApprovalStore` is process-local, so a
restart loses pending approvals. Durable storage is an open task.

### 4. Scheduled tasks — **working**

One-off, daily-at-a-local-time, or every-N-minutes. A task is either a fixed
tool call with fixed arguments, or a natural-language instruction.

- Stored in `schedule.db`, capped at 50, surviving restart and reboot.
- Unattended safety: actions that would normally need confirmation are held
  until the owner authorises that task once; the exact arguments are hashed.
- Natural-language tasks run under `UnattendedPolicy`, which converts
  REQUIRE_APPROVAL into DENY rather than auto-approving.
- Finished one-off tasks stay listed for 24 h, then are purged.
- Missed slots are skipped, not replayed.
- Two tickers (in-process loop + Android JobScheduler) cannot double-run a task;
  tasks are claimed in a single `BEGIN IMMEDIATE` transaction.

**Acceptance criteria**

- A scheduled risky action never runs unauthorised. ✔ tested
- An authorisation cannot transfer to different arguments or another tool.
  ✔ tested
- A daily task fires at the right local wall-clock time. ✔ tested

### 5. Local web console — **working**

`python -m android_agent.web`, bound to localhost, mandatory access token,
intended to sit behind a Cloudflare tunnel. Shares the same runtime, policy,
approvals and audit as Telegram. Light/dark themes, archived reopenable chats
(capped at 30).

### 6. Optional on-device fast path (Needle) — **working, off by default**

`NeedleRouter` implements the `Planner` protocol, so a simple request can be
routed locally with zero cloud calls. Confidence routes, never authorises:
every proposal still passes validator → policy → approval → audit.

### 7. Observability — **working**

`audit.jsonl` (structured, redacted; failures carry `error_code`), `agent.log`,
and `python -m android_agent.doctor` for configuration diagnosis.

### 8. Multilingual operation — **largely working via the model** (assessment corrected 2026-10-04)

The earlier version of this section overstated the gap. Corrected after
checking which strings actually reach the owner.

**Working today, with no i18n machinery:** the planner is multilingual, so a
request in Bengali, Hindi or English is understood and the right tool is
selected. Deterministic tool summaries are written in English, but they are
**inputs to the model**, not output to the owner — the model re-renders them
in the owner's language when it replies. The conversational path therefore
works in any language the model speaks, which is the bulk of the product.

**Still English, because these bypass the model entirely:**

| Path | Example |
|---|---|
| Approval prompts | "Approval required for: send email … Expires in 5 minutes." |
| Scheduled task reports | "⏰ Battery check: Battery is at 87%." |
| Slash commands (`/help`, `/tools`, `/session`, `/media`) | tool listings, session details |
| Operational replies | "Unauthorized.", "I am still working on your previous request." |
| Web console UI | buttons, labels, empty states |

**One functional (not cosmetic) gap:** skill routing matches English keyword
triggers, so a Hindi request loads less procedural guidance than its English
equivalent — measured at 1261 vs 2063 characters, with the device-control
skill never activating. The model still understands the request; it simply
receives weaker guidance. The email skill already demonstrates the fix:
multilingual triggers, about one line of JSON per skill.

**Owner's decision (2026-10-04):** no general i18n work. The model handles
the conversational path, which is what matters day to day.

### 9. Channel connectors — Gmail first — **planned, not built**

**Description.** Let the agent read, summarise and send email, so inbox
triage becomes one of the repetitive tasks it handles. Gmail first; other
channels only if a need appears.

**Scope for v1**
- Read recent mail (sender, subject, date, snippet or body).
- Summarise the inbox, or a sender, or a time window.
- Send a message, and reply in-thread.
- Combine with scheduling: a morning inbox summary.

**Explicitly out of scope for v1:** deleting mail, moving or labelling,
attachments outbound, multiple accounts, calendar.

**Requirements**
- Reading mail is `sensitive_read`; sending is `external_side_effect` and so
  is approval-gated with hash binding, like SMS and calls.
- No delete capability is exposed, whatever the credential technically allows.
- Email bodies are **untrusted input**. See the taint rule in
  ARCHITECTURE.md § Untrusted content.
- Headers and bodies in any language and encoding must round-trip correctly
  (RFC 2047 encoded-words, non-UTF-8 charsets).

**Acceptance criteria**
- Sending requires a fresh approval showing the real recipient, subject and
  body — never a paraphrase.
- A scheduled summary cannot send mail, even if a send was pre-authorised in
  the same task.
- An email whose body contains instructions aimed at the agent cannot cause a
  tool call to execute without owner approval.
- A subject line in Hindi, Bengali or with emoji displays correctly.
- The mailbox credential never appears in logs, audit or error text.

### 10. App-level automation — **planned, staged**

**Description.** Drive other apps to complete a repeated task end to end:
open an app, find the right control, act, confirm it worked.

**The blocker today** is perception, not action. `tap_screen` and
`swipe_screen` already work over ADB, but they are **blind** — the agent taps
a coordinate without knowing what is there, so any layout change, dialog or
slow load silently taps the wrong thing. Blind automation on a phone that can
send messages and spend money is not acceptable.

**Two ways to see the screen**

| | `uiautomator dump` (ADB) | Accessibility service |
|---|---|---|
| What it returns | the same node tree: `resource-id`, `text`, `content-desc`, `bounds`, `clickable`, `enabled` | the same tree, plus live events |
| Needs a new APK | no | **yes** — `BIND_ACCESSIBILITY_SERVICE` is signature-level and only the system may bind it, so it must ship in an installed app |
| Needs wireless ADB | yes, re-enabled after each reboot | no |
| Event-driven triggers | no, polling only | yes |
| Speed | ~0.5–1 s per read | faster, no shell round trip |
| Standing grant | none beyond ADB | reads **all screen content in every app**, permanently until revoked |

**Decision: stage it.** Stage 1 uses `uiautomator dump`, which delivers the
structured perception this feature actually needs, using transport that
already exists. Stage 2 adds the accessibility service once there are real
workflows proving what is still missing — chiefly surviving a reboot without
re-enabling ADB, and reacting to events.

**Requirements (stage 1)**
- The agent acts on **named elements**, never raw coordinates chosen by the
  model.
- Every step verifies the screen changed as expected before the next one.
- A workflow has a step budget and aborts on an unexpected screen.
- Screen content is third-party text and taints the run, exactly like email.

**Acceptance criteria**
- A workflow aborts rather than guessing when the expected element is absent.
- The agent can report *why* it stopped, naming the screen it did not expect.
- Changing the phone's language does not break a workflow that matches on
  `resource-id`.
- No automation step can send, pay or delete without the existing approval
  gate.

---

## User Flows

**Ad-hoc request**

1. Owner sends "turn the torch on" (Telegram or web).
2. Identity check → session loaded → skills selected → planner called.
3. Planner proposes `set_torch{"on": true}`.
4. Schema validation → policy ALLOW → execute → audit.
5. Reply with the tool's deterministic summary.

**Risky request**

1. Owner sends "call 7407486131".
2. Planner proposes `place_phone_call`.
3. Policy returns REQUIRE_APPROVAL; an approval record is created with the
   argument hash and a 300 s TTL.
4. Owner taps Approve. The hash is revalidated, then executed.
5. The telephony call state is read back; if the radio stayed idle the agent
   reports failure with the remedy rather than claiming success.

**Scheduling a risky task**

1. "Text mum good morning every day at 9."
2. `schedule_task` stores it **unauthorised** and says so.
3. Owner agrees; `authorize_scheduled_task` (itself approval-gated) freezes the
   hash.
4. It runs daily, unattended, with no further prompts.

**Scheduled run while the agent is dead**

1. Android JobScheduler fires every ~15 min and runs `python -m
   android_agent.schedule`.
2. Due tasks are claimed atomically, executed, reported over HTTPS directly,
   and the process exits.

---

## Functional Requirements

1. Only the configured owner chat id may cause any tool to execute.
2. Tool arguments must validate against the tool's schema before execution.
3. Policy decisions must derive from declared risk and actor, never model text.
4. External side effects must be confirmed against an exact argument hash.
5. Every tool execution must emit an audit event; failures must record a code.
6. Tool results must be deterministic summaries produced by application code,
   not model prose.
7. A tool must not report success for an action it cannot confirm happened.
8. Sessions must expire on an idle TTL (default 15 min).
9. Scheduled tasks must survive process restart and device reboot.
10. A scheduled task must never perform an unauthorised approval-gated action.
11. Concurrent schedulers must not double-run a task.
12. Captured media must remain on the device.
13. No tool may contact a third-party service. (IP geolocation was built and
    then removed for violating this.)

## Non-Functional Requirements

**Performance.** A warm cached-GPS location returns in ~1 s; a live GPS fix in
2.7–4.5 s on the reference device. Tool timeouts are per-tool. Scheduler tick
is 30 s in-process.

**Security.** See ARCHITECTURE.md § Security. Single owner; token-gated web
console; no shell tool; secrets only in `.env`.

**Reliability.** See ARCHITECTURE.md § Reliability. Retries, backoff,
claim/lease, missed-slot skipping, mutation-tested regressions.

**Accessibility.** Web console targets WCAG 2.2 AA: 4.5:1 body contrast
verified in both themes, 24 px minimum targets, `prefers-reduced-motion`
respected, transcript as a polite live region.

**Scalability.** Explicitly single-device, single-owner. Not a goal.

## Data Requirements

| Entity | Store | Retention |
|---|---|---|
| Session (model context per chat) | `sessions.db` | idle TTL 900 s, 60 messages |
| Approval | memory | 300 s, one-time use |
| Scheduled task | `schedule.db` | until cancelled; finished one-offs 24 h; cap 50 |
| Web conversation transcript | `web_chats.db` | cap 30 |
| Audit event | `audit.jsonl` | append-only, unbounded |
| Captured media | `~/storage/shared/AndroidAgent` | until the owner deletes it |

## Success Criteria

1. A new repetitive task can be automated conversationally, with no code.
2. No unauthorised external side effect ever occurs. (Currently: enforced by
   policy + hash, covered by mutation-checked tests.)
3. A failure tells the owner what to change. (Current failure messages name
   the exact Android setting for the four known classes.)
4. A scheduled daily task fires on a day when Termux was killed overnight.
   **Not yet verified on-device.**
5. A non-English speaker can complete the same flows as an English speaker.
   **Not met.**

---

## Open Questions

These are genuinely unresolved. They are **not** assumptions.

1. **Multilingual scope.** Should the agent (a) reply in the owner's language
   but keep one configured language for tool/error strings, (b) fully localise
   every string to a configured locale, or (c) detect and mirror the input
   language per message? This decides whether we need a translation catalogue
   or just a language directive in the system prompt.
2. **Which languages, concretely?** The reference device is in India. English +
   Hindi + Bengali? Or any language the model handles, best-effort?
3. **"Automate as many repetitive tasks as possible" — which ones?** The 41
   tools cover device state, media, messaging and UI control. Are there
   specific recurring workflows (per-app automation, WhatsApp, payments,
   scraping a screen) that matter? App-specific automation likely needs an
   accessibility service, which is a significant security decision.
4. **Accessibility service.** UI control currently requires wireless ADB, which
   must be re-enabled after each reboot. An accessibility service would remove
   that but can read all screen content. Acceptable or not?
5. **Durable approvals.** Pending approvals are lost on restart. Should an
   approval survive a restart (durable store), or is "ask again" correct?

6. **Gmail auth: app password or OAuth?** Recommendation and trade-offs are
   in ARCHITECTURE.md § Email connector. Short version: an app password needs
   no Google verification and no new dependency, but grants full mailbox
   access; OAuth can be scoped to read+send, but a personal project stuck in
   "testing" gets refresh tokens that expire about weekly, which breaks
   unattended use.
7. **May email bodies be sent to the cloud model?** Summarising requires it.
   This is the first feature that would send third-party content off-device.
   Alternatives: metadata-only summaries (sender/subject/date, no body), or
   accept it.
8. **One mailbox or a dedicated one?** Pointing the agent at a secondary
   Gmail that the main account forwards to would bound the blast radius of a
   stolen credential.
## Assumptions

9. **Which two or three workflows?** The design needs real examples. Naming
   them changes what gets built: a messaging flow needs text entry and a
   recipient check, a bill payment needs a confirmation screen read back, a
   data-entry flow needs scrolling and lists.
10. **Is a persistent on-screen reader acceptable later?** Stage 2's
    accessibility service can read every app's screen, permanently. That is
    the largest standing grant this project would hold.

Recorded so they can be challenged:

- One owner, one device. No shared or family use.
- The device is unrooted and will stay that way.
- The owner can grant Android permissions when told precisely which.
- Network access is intermittent; the agent must degrade, not fail.
- The model provider is OpenAI-compatible (currently Gemini via its
  compatibility endpoint).
- Timing precision beyond Android's 15-minute job floor is not required.
