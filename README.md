# Android Agent

A Telegram-controlled Android/Termux assistant. The repository currently contains:

- `bot.py` — the original command bot.
- `agent_bot.py` — a clean v2, provider-neutral agent runtime under active migration.
- `android_agent/` — typed tools, deterministic policy, bounded agent loop, model adapters, and audit support.
- `docs/AGENT_ARCHITECTURE.md` — researched architecture and migration plan.

Screen recording is intentionally out of scope.

## V2 safety model

The LLM is an untrusted planner. Tool proposals pass through strict schema validation and deterministic owner/risk policy before execution. The model has no generic shell tool.

V2 currently exposes **41 narrow typed tools** covering battery/system status, torch, brightness, volume, Wi-Fi, clipboard, camera, location, screenshots, audio recording, media, notifications, SMS inbox, contacts, calls/SMS with confirmation, scoped file creation/retrieval, and bounded ADB controls. Screen recording and arbitrary shell execution remain intentionally unavailable.

Five bundled skills add progressively loaded guidance for core reliability, device control, files, communications, and sensitive data. Skills can guide tool selection but cannot grant permissions.

## Run v2

Termux must have Python, `termux-api`, the Termux:API Android app, and the relevant Android permissions.

```sh
pkg install python termux-api
python -m pip install -r requirements-v2.txt
cp .env.example .env
# Edit .env with your values. It is loaded automatically at startup.

# Always run the preflight check first:
python -m android_agent.doctor

python agent_bot.py
```

### Configuration

| Variable | Required | Purpose |
|---|---|---|
| `ANDROID_AGENT_BOT_TOKEN` | yes | Telegram bot token from @BotFather |
| `ANDROID_AGENT_OWNER_CHAT_ID` | yes | Your numeric Telegram user ID |
| `ANDROID_AGENT_LLM_BASE_URL` | yes | Endpoint URL, or an alias: `gemini`, `openai`, `groq`, `openrouter` |
| `ANDROID_AGENT_LLM_MODEL` | yes | A model ID that exists at that endpoint |
| `ANDROID_AGENT_LLM_API_KEY` | usually | Required for all hosted endpoints |
| `ANDROID_AGENT_LLM_DIALECT` | no | `openai` or `gemini`; auto-detected from the URL |
| `ANDROID_AGENT_LLM_TIMEOUT` | no | Seconds to wait for the model (default 60) |
| `ANDROID_AGENT_LOG_LEVEL` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

The bot accepts only a private chat where both the sender ID and chat ID equal the configured owner ID.

### Using Google Gemini

```ini
ANDROID_AGENT_LLM_BASE_URL=gemini
ANDROID_AGENT_LLM_MODEL=gemini-2.5-flash
ANDROID_AGENT_LLM_API_KEY=<Google AI Studio key>
```

Three things bite people here, and the agent now handles all three:

1. **The base URL must end in `/v1beta/openai`**, not `/v1beta` or `/v1`. A wrong path returns an opaque 404. The loader repairs the common mistakes.
2. **Gemini rejects strict JSON Schema.** Its `function_declarations` parser accepts only a restricted OpenAPI 3.0 subset and returns `400 Unknown name "additionalProperties"`. `android_agent/models/compat.py` strips unsupported keywords from the *outbound* schema only — local argument validation stays strict and closed.
3. **The model name must be real.** `python -m android_agent.doctor` lists what the endpoint actually offers.
4. **Gemini 3 thinking models require thought signatures.** Each tool call comes back with an opaque `thought_signature` under `tool_calls[].extra_content.google`, and the follow-up turn is rejected with `400 Function call is missing a thought_signature in functionCall parts` unless it is replayed verbatim. The planner captures it onto `ToolCall.extra_content` and the runtime replays it. Signatures are opaque transport data: they are never interpreted, never shown, and never affect a policy decision.

### Troubleshooting

`python -m android_agent.doctor` checks configuration, endpoint reachability, credentials, model availability, schema sanitisation, a live tool-calling round trip, and `termux-api` presence. Logs are written to `~/telegram_agent_v2/agent.log`.

Audit metadata is written to `~/telegram_agent_v2/audit.jsonl`. Tool arguments and secrets are not included in these initial events.

## Test

```sh
python -m unittest discover -s tests -v   # 49 tests, no network required
ruff check android_agent tests agent_bot.py
```

See [the architecture blueprint](docs/AGENT_ARCHITECTURE.md) for policy tiers, approvals, skills, Needle integration, evaluation, and the phased migration plan.
