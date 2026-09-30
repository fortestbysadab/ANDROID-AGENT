"""Preflight diagnostics.

Run `python -m android_agent.doctor` to verify configuration, endpoint
reachability, credentials, model availability, tool-schema acceptance, and
Termux API availability before starting the bot.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from android_agent.config import Settings
from android_agent.models.compat import adapt_tools

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"


def _headers(settings: Settings) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "android-agent-doctor/2"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"
    return headers


def _line(status: str, title: str, detail: str = "") -> bool:
    icon = {STATUS_OK: "[ok]  ", STATUS_FAIL: "[FAIL]", STATUS_WARN: "[warn]"}[status]
    print(f"{icon} {title}")
    if detail:
        for chunk in str(detail).splitlines():
            print(f"       {chunk}")
    return status != STATUS_FAIL


def check_config() -> tuple[Settings | None, bool]:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        _line(STATUS_FAIL, "Configuration", str(exc))
        return None, False
    _line(STATUS_OK, "Configuration", json.dumps(settings.redacted(), indent=2))
    return settings, True


def check_models(settings: Settings) -> bool:
    """List models and confirm the configured one exists."""
    url = f"{settings.llm_base_url.rstrip('/')}/models"
    request = urllib.request.Request(url, headers=_headers(settings))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            envelope = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace")
        return _line(STATUS_FAIL, f"Model listing (HTTP {exc.code})", body)
    except Exception as exc:
        return _line(STATUS_WARN, "Model listing unavailable", str(exc))

    names = [str(item.get("id", "")) for item in envelope.get("data", [])]
    short = {name.rsplit("/", 1)[-1] for name in names}
    wanted = settings.llm_model
    if wanted in names or wanted in short:
        return _line(STATUS_OK, f"Model '{wanted}' is available")

    suggestions = sorted(n for n in short if "flash" in n or "gpt" in n or "gemini" in n)
    return _line(
        STATUS_FAIL,
        f"Model '{wanted}' was NOT found at this endpoint",
        "Available (filtered):\n  " + "\n  ".join(suggestions[:25] or sorted(short)[:25]),
    )


def check_chat(settings: Settings) -> bool:
    from android_agent.models.openai_compatible import OpenAICompatiblePlanner, PlannerError

    planner = OpenAICompatiblePlanner(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        dialect=settings.llm_dialect,
        timeout_seconds=settings.request_timeout_seconds,
    )
    try:
        result = planner.health_check()
    except PlannerError as exc:
        return _line(STATUS_FAIL, "Plain chat completion", f"{exc}\nFix: {exc.remedy}")
    return _line(STATUS_OK, "Plain chat completion", f"model={result['model']} reply={result['content']!r}")


def check_tool_calling(settings: Settings) -> bool:
    """The check that would have caught the additionalProperties bug."""
    from android_agent.models.openai_compatible import OpenAICompatiblePlanner, PlannerError
    from android_agent.tools.catalog import build_full_registry

    registry = build_full_registry()
    planner = OpenAICompatiblePlanner(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        dialect=settings.llm_dialect,
        timeout_seconds=settings.request_timeout_seconds,
    )
    wire = adapt_tools(registry.model_schemas(), settings.llm_dialect)
    leaked = [
        tool["function"]["name"]
        for tool in wire
        if "additionalProperties" in json.dumps(tool)
    ]
    if leaked:
        return _line(STATUS_FAIL, "Schema sanitisation", f"additionalProperties survived in: {leaked[:5]}")
    _line(STATUS_OK, f"Schema sanitisation ({len(wire)} tools, dialect={settings.llm_dialect.value})")

    try:
        response = planner.complete(
            [{"role": "user", "content": "What is my battery percentage right now?"}],
            registry.model_schemas(),
        )
    except PlannerError as exc:
        return _line(STATUS_FAIL, "Tool-calling round trip", f"{exc}\nFix: {exc.remedy}")

    if not response.tool_calls:
        return _line(
            STATUS_WARN,
            "Tool-calling round trip returned no tool call",
            f"text={(response.text or '')[:200]!r} — the model may be too weak for tool use.",
        )
    picked = ", ".join(call.name for call in response.tool_calls)
    return _line(STATUS_OK, f"Tool-calling round trip selected: {picked}")


def check_multi_turn(settings: Settings) -> bool:
    """Drive a full tool loop: propose -> execute -> feed result back.

    Gemini 3 thinking models reject the second turn unless the tool call's
    thought_signature is replayed, so a single-turn check is not enough.
    """
    from android_agent.agent.runtime import AgentRuntime, RunStatus
    from android_agent.models.openai_compatible import OpenAICompatiblePlanner
    from android_agent.policy.engine import DefaultPolicy
    from android_agent.tools.catalog import build_full_registry

    runtime = AgentRuntime(
        planner=OpenAICompatiblePlanner(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            dialect=settings.llm_dialect,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        registry=build_full_registry(),
        policy=DefaultPolicy(str(settings.owner_chat_id)),
        system_prompt="You are a device assistant. Use tools for live device data.",
    )
    outcome = runtime.run(
        "how much battery charge",
        actor_id=str(settings.owner_chat_id),
        chat_id=settings.owner_chat_id,
    )
    if outcome.status is RunStatus.FAILED:
        return _line(STATUS_FAIL, "Multi-turn tool loop", outcome.error or outcome.text)
    used = ", ".join(sorted({r.summary[:60] for r in outcome.tool_results})) or "none"
    return _line(
        STATUS_OK,
        f"Multi-turn tool loop ({outcome.status.value})",
        f"reply: {outcome.text[:200]}\ntool results: {used}",
    )


def check_termux() -> bool:
    if shutil.which("termux-battery-status") is None:
        return _line(
            STATUS_WARN,
            "termux-api not found on PATH",
            "Install with: pkg install termux-api (plus the Termux:API app from F-Droid).",
        )
    try:
        subprocess.run(
            ["termux-battery-status"], capture_output=True, timeout=10, check=True
        )
    except Exception as exc:
        return _line(STATUS_WARN, "termux-battery-status failed", str(exc))
    return _line(STATUS_OK, "termux-api responds")


def main() -> int:
    print("Android Agent v2 — preflight diagnostics\n")
    settings, ok = check_config()
    if settings is None:
        print("\nResult: configuration is invalid; fix the above and re-run.")
        return 1

    results = [
        ok,
        check_models(settings),
        check_chat(settings),
        check_tool_calling(settings),
        check_multi_turn(settings),
        check_termux(),
    ]
    print()
    if all(results):
        print("Result: all checks passed. Start the bot with: python agent_bot.py")
        return 0
    print("Result: one or more checks FAILED. The bot will not work until they pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
