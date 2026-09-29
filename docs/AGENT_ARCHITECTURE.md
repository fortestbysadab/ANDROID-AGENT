# Android Agent Architecture Blueprint

_Status: accepted direction and incremental implementation, 2026-09-30_

Selected implementation decisions:

- Provider-neutral OpenAI-compatible model adapter first
- Owner-autonomous policy for bounded actions; external side effects still require approval and critical tools are denied
- Clean v2 (`agent_bot.py` and `android_agent/`) alongside the untouched legacy `bot.py`

## 1. Goal

Turn the current Telegram-to-Termux command bot into a reliable, model-agnostic agent runtime.

The language model is an **untrusted planner**. It may propose an action, but only deterministic application code may validate, authorize, approve, execute, and audit that action.

Screen recording is explicitly out of scope.

## 2. Key design decision

Do not put an LLM directly in front of the existing command handlers and do not let it generate shell commands.

Use this control flow:

```text
Telegram update
  -> identity/authentication
  -> input limits and normalization
  -> conversation/session coordinator
  -> skill + tool retrieval
  -> model adapter (planner)
  -> proposed typed tool call(s)
  -> JSON-schema/Pydantic validation
  -> deterministic policy decision
       -> ALLOW
       -> REQUIRE_APPROVAL
       -> DENY
  -> bounded tool executor
  -> structured observation
  -> model loop or final response
  -> audit event
```

Explicit slash commands and natural-language requests must converge on the same tool registry, policy engine, and executor. Slash commands are a deterministic UI, not a second privileged execution path.

## 3. Needle's role

[Needle](https://github.com/cactus-compute/needle) is a small on-device model specialized for tool selection, argument extraction, structured extraction, and embeddings. Needle 3 is not a general chat model. Its own guide says it reads declared tools, selects calls, grounds arguments in spans from the request, and returns a calibrated confidence score.

It is a good optional **local fast-path router**, not the entire agent brain:

```text
simple device request
  -> Needle complete()
  -> high confidence + low-risk tool -> policy -> execute
  -> medium confidence -> Telegram confirmation or larger LLM
  -> no call / low confidence -> larger LLM or clarification

complex or multi-step request
  -> general LLM planner
  -> policy -> execute -> observation -> repeat
```

Important: do **not** use `Needle.run()` for device actions. In the reviewed implementation, `run()` executes registered Python callables itself. Use `Needle.complete()` to obtain proposals, then send those proposals through this project's validator, policy engine, approval flow, and executor.

Needle-specific rules:

- One narrow tool per action; avoid generic `control_device(action, value)` tools.
- Put ranges, enums, patterns, and length bounds in the schema.
- Use clear argument descriptions and values users naturally say.
- Treat confidence as routing evidence, never authorization.
- Start with the base model and a held-out evaluation set before considering fine-tuning.
- Make telemetry behavior explicit in deployment configuration.

## 4. Proposed package layout

```text
android_agent/
  app.py                    # composition root
  config.py                 # validated environment-based settings
  transport/
    telegram.py             # update parsing, replies, approval buttons
  agent/
    runtime.py              # bounded plan/action/observation loop
    state.py                # run/session state machine
    context.py              # context construction and compaction
  models/
    base.py                 # provider-neutral Planner protocol
    openai_compatible.py    # optional cloud/local adapter
    needle.py               # optional local tool-router adapter
  tools/
    base.py                 # ToolSpec, ToolContext, ToolResult
    registry.py             # trusted tool catalogue
    device.py               # Termux tools
    adb.py                  # ADB tools
    information.py          # read-only status tools
  policy/
    engine.py               # deterministic policy evaluation
    models.py               # Decision, Risk, Approval
    default.yaml            # reviewed policy data
  approvals/
    store.py                # pending approvals with TTL and argument hash
  skills/
    loader.py               # trusted, versioned progressive disclosure
    device_control/
      SKILL.md
      skill.yaml
  storage/
    sqlite.py               # sessions, approvals, audit events
  observability/
    audit.py                # structured redacted event log
  schemas/
    messages.py             # provider-neutral tool call/result envelopes

tests/
  unit/
  integration/
  policy/
  evals/
```

Keep `bot.py` as a temporary compatibility entry point during migration, then reduce it to startup wiring or remove it after parity tests pass.

## 5. Tool contract

Every tool has a stable name, version, strict input model, risk metadata, timeout, and narrow implementation.

Example conceptual contract:

```python
class ToolResult(BaseModel):
    status: Literal["ok", "error", "denied", "approval_required", "timeout"]
    summary: str
    data: dict[str, object] = {}
    retryable: bool = False
    error_code: str | None = None

class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    risk: Risk
    read_only: bool
    destructive: bool
    idempotent: bool
    timeout_seconds: float
    execute: Callable[[ToolContext, BaseModel], ToolResult]
```

Requirements:

1. Validate all model output with closed schemas; reject unknown fields.
2. Never concatenate model output into shell commands. Use argument arrays and allowlists.
3. Canonicalize and authorize paths after symlink resolution.
4. Return concise, structured, actionable errors.
5. Apply a timeout and output-size cap to every tool.
6. Attach a unique call ID and idempotency key to side-effecting calls.
7. Always produce a result for every proposed call, including denials and timeouts.
8. Keep secrets and credentials outside model context and tool results.

The current `/cmd` feature must not be registered as an agent tool. It can remain an owner-only legacy command behind a separate explicit policy, or be disabled.

## 6. Initial risk policy

| Risk | Examples | Default decision |
|---|---|---|
| Low/read-only | battery, system info, Wi-Fi info, current app, volume status | Allow |
| Low/reversible | torch, volume, brightness, media control, vibrate, toast | Allow with bounds and rate limits |
| Sensitive read | location, notifications, inbox, contacts, clipboard, files, screenshot, photo | Require explicit request; optionally confirm when context is ambiguous |
| External side effect | SMS, phone call, dialog, text-to-speech | Require approval with exact destination/content preview |
| Device mutation | wallpaper, app force-stop, screen power, clipboard write, Wi-Fi toggle | Require approval initially; downgrade selected tools only after evaluation |
| Raw control | tap, swipe, keyevent, input text | Approval per bounded sequence; never allow an unbounded UI-control loop |
| Critical | arbitrary shell, unrestricted file access, installing packages, privilege changes | Deny to the model |

Policy is evaluated in code using authenticated actor, tool identity/version, validated arguments, session state, recent actions, and configured limits. Model confidence, reasoning text, prompts, skills, and MCP annotations cannot override policy.

Example policy data:

```yaml
rules:
  - id: allow-owner-readonly
    effect: allow
    actor: owner
    tool_tags: [read_only]
  - id: approve-external-write
    effect: require_approval
    actor: owner
    tool_tags: [external_side_effect]
  - id: deny-agent-shell
    effect: deny
    tools: [system_run_shell]
limits:
  max_steps_per_run: 8
  max_tool_calls_per_run: 12
  max_same_tool_retries: 2
  run_timeout_seconds: 90
```

Default-deny applies when no rule matches.

## 7. Approval protocol

A confirmation is a durable state transition, not a free-form "yes" interpreted by the model.

1. Runtime creates a pending approval containing actor, tool version, canonical arguments, argument hash, reason, expiry, and run ID.
2. Telegram displays the exact action and inline **Approve** / **Deny** buttons.
3. Callback data contains only an opaque random approval ID.
4. Approval store verifies owner identity, TTL, unused status, and argument hash.
5. Execution uses the frozen approved arguments. Any changed argument requires a new approval.
6. Approval is consumed atomically before execution to prevent replay.

## 8. Skills and policies are different

A **tool** performs one typed operation. A **skill** contains procedural guidance for achieving a task with tools. A **policy** is deterministic authority over whether an operation may happen.

Suggested skill bundle:

```text
skills/send_status_report/
  SKILL.md       # concise workflow and edge cases
  skill.yaml     # id, version, intent examples, required tools, budgets
  tests.yaml     # positive, negative, ambiguous, injection cases
  scripts/       # optional deterministic helpers
```

Rules:

- Load only skill names/descriptions initially; load full content when selected.
- Skills come only from a trusted local allowlist and are version-pinned.
- A skill declares required tools but cannot grant access to them.
- Skill scripts run with the same policy checks, timeouts, and audit trail.
- Treat downloaded/community skills as executable supply-chain content requiring review.
- Include positive, negative, ambiguous, and adversarial routing examples.

Start with a small set of skills such as `device_status`, `find_and_send_file`, and `safe_ui_action`. Do not start with self-modifying skills or autonomous skill installation.

## 9. Agent loop

Use one agent first. Multi-agent orchestration adds little value for this device-control scope.

```text
receive request
  validate identity and rate limits
  load session + relevant skill summaries
  ask planner for response or typed tool proposals
  for each proposal:
    validate schema and grounding
    evaluate policy
    deny, pause for approval, or execute
    append structured result
  continue until:
    final answer
    approval pause
    clarification needed
    step/tool/time budget exhausted
    repeated-call circuit breaker opens
```

Hard limits must live in code:

- Maximum model turns
- Maximum tool calls
- Maximum identical retries
- Per-tool and total timeout
- Maximum model/tool-result bytes
- Maximum side effects per run
- Cancellation/kill switch
- Per-chat serialization lock

Parallel execution is allowed only for independent read-only tools. Preserve declared order for side effects.

## 10. State and memory

Use SQLite first; a vector database is unnecessary for the initial scope.

Persist:

- Runs and their terminal reason
- Normalized tool proposals and results
- Policy decisions and rule IDs
- Pending/consumed approvals
- Skill/model/tool versions
- Redacted audit metadata

Do not automatically turn Telegram messages, notifications, SMS, contacts, clipboard contents, or files into long-term memory. Durable memory should require an explicit user action and have inspect/delete controls.

Model context should contain only what the current task needs. Tool output is untrusted data, never a new system instruction.

## 11. Reliability and observability

Emit structured events for:

```text
run.started
model.requested
model.responded
tool.proposed
tool.validation_failed
policy.allowed
policy.denied
approval.requested
approval.approved
approval.expired
tool.started
tool.completed
tool.failed
run.completed
run.budget_exhausted
```

Record latency, status, retry count, versions, and redacted argument hashes. Never log bot tokens or full sensitive payloads. Keep a user-visible `/agent_status`, `/pending`, `/cancel`, and `/audit` interface.

## 12. Evaluation strategy

Before enabling automatic execution, build a frozen test corpus for every tool:

- Positive requests and paraphrases
- Missing required values
- Negation ("don't turn off Wi-Fi")
- Ambiguous targets
- Unsupported requests
- Multiple actions and ordering
- Out-of-range arguments
- Prompt-injection text inside tool results
- Attempts to obtain shell, unrestricted files, credentials, or policy bypass
- Repeated calls and replayed approvals
- Timeouts and malformed model output

Measure separately:

- Tool-selection precision/recall
- Exact argument accuracy
- Unsafe execution rate (target: zero in the suite)
- Appropriate clarification/approval rate
- Task completion rate
- Mean tool calls and latency
- Budget-exhaustion and retry-loop rate

Run policy and tool tests without an LLM. Run the same model-agnostic agent evals against every model adapter before changing the production model.

## 13. Migration plan

### Phase 0 — secure the baseline

- Move secrets to environment variables and commit only `.env.example`.
- Add dependency locking, formatting, linting, and tests.
- Inventory every existing command and classify its risk.
- Stop advertising unsupported screen recording.

### Phase 1 — extract deterministic tools

- Create typed tool contracts and registry.
- Move Termux/ADB operations out of Telegram handlers.
- Make existing slash commands call the registry through policy.
- Add structured results, timeouts, rate limits, and unit tests.

### Phase 2 — policy and approvals

- Implement default-deny policy rules.
- Add SQLite approvals and Telegram inline buttons.
- Add immutable-enough structured audit events and `/cancel`.

### Phase 3 — provider-neutral LLM loop

- Add the `Planner` interface and one selected LLM adapter.
- Support final text, typed calls, clarification, and bounded looping.
- Keep dangerous tools unavailable to the model regardless of prompting.

### Phase 4 — skills

- Add trusted skill manifests, progressive loading, versions, and fixtures.
- Start with three narrow skills and measure routing accuracy.

### Phase 5 — optional Needle fast path

- Add Needle through `complete()`, not `run()`.
- Enable automatic execution only for evaluated low-risk tools above a measured per-tool confidence threshold.
- Route uncertainty to approval or the general LLM.
- Fine-tune only if held-out eval failures justify it.

### Phase 6 — hardening

- Fault-injection tests, restart recovery, approval replay tests, concurrency tests, and deployment runbook.
- Review dependency/model provenance and disable optional telemetry where required.

## 14. Research basis

- Needle repository and guides: <https://github.com/cactus-compute/needle>, <https://cactuscompute.com/blog/designing-tools-for-needle>, <https://cactuscompute.com/blog/needle-confidence>
- Anthropic, _Writing effective tools for agents_: <https://www.anthropic.com/engineering/writing-tools-for-agents>
- Model Context Protocol security principles: <https://modelcontextprotocol.io/specification/2025-06-18>
- MCP security baseline: <https://github.com/mcp-security-project/mcp-security-best-practices>
- OpenAI, _A practical guide to building agents_: <https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf>

The common conclusion is consistent: narrow typed tools, deterministic authorization outside the model, explicit approval for consequential actions, bounded loops, high-signal results, and evaluation-driven iteration matter more than adding a complicated multi-agent framework.
