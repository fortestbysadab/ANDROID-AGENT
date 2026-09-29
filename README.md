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
python -m pip install -r requirements-v2.txt
cp .env.example .env
# Edit .env, then load it without committing it:
set -a
. ./.env
set +a
python agent_bot.py
```

Required variables:

- `ANDROID_AGENT_BOT_TOKEN`
- `ANDROID_AGENT_OWNER_CHAT_ID`
- `ANDROID_AGENT_LLM_BASE_URL`
- `ANDROID_AGENT_LLM_MODEL`
- `ANDROID_AGENT_LLM_API_KEY` when required by the selected endpoint

The model endpoint must implement the OpenAI-compatible `POST /chat/completions` tool-calling format. The bot accepts only a private chat where both the sender ID and chat ID equal the configured owner ID.

Audit metadata is written to `~/telegram_agent_v2/audit.jsonl`. Tool arguments and secrets are not included in these initial events.

## Test

```sh
python -m unittest discover -s tests -v
```

See [the architecture blueprint](docs/AGENT_ARCHITECTURE.md) for policy tiers, approvals, skills, Needle integration, evaluation, and the phased migration plan.
