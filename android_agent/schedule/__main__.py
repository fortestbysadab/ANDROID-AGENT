"""One-shot scheduler tick: `python -m android_agent.schedule`.

This exists because the in-process scheduler only runs while `agent_bot.py` is
alive, and Android kills Termux freely. A persisted JobScheduler job runs this
every fifteen minutes, so a daily task still fires when the agent died
overnight - late, but fired.

It is deliberately short-lived. A process that starts, does its work and exits
within seconds is far less likely to be killed mid-flight than a long-running
one, and it cannot be the reason the phone's battery drains.

Results are delivered straight to Telegram over HTTPS rather than through the
bot library, because the bot process is precisely what may not be running.
Claiming is atomic in SQLite, so this ticking at the same moment as a live
agent cannot double-send.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from android_agent.app import build_application
from android_agent.config import Settings
from android_agent.observability.logging import configure_logging
from android_agent.schedule.runner import ScheduleRunner
from android_agent.schedule.store import ScheduleStore

logger = logging.getLogger(__name__)

TELEGRAM_TIMEOUT = 20


def send_message(token: str, chat_id: int, text: str) -> bool:
    """Post one message. Returns False rather than raising."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
    try:
        request = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT) as response:
            return json.loads(response.read()).get("ok", False)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Could not deliver scheduled result: %s", type(exc).__name__)
        return False


def send_location(token: str, chat_id: int, latitude: float, longitude: float) -> None:
    url = f"https://api.telegram.org/bot{token}/sendLocation"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "latitude": latitude, "longitude": longitude}
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=TELEGRAM_TIMEOUT
        ):
            pass
    except (urllib.error.URLError, OSError):
        logger.warning("Could not deliver scheduled location")


def build_runner(settings: Settings, store: ScheduleStore) -> ScheduleRunner:
    """Reuse the shared composition root, with HTTPS delivery.

    The bot process may be dead - that is why this entry point exists - so
    results go straight to the Telegram API rather than through the bot
    library.
    """
    def report(run) -> None:
        prefix = {"ok": "⏰", "blocked": "🔒", "error": "⚠️"}.get(run.status, "⏰")
        send_message(
            settings.telegram_bot_token, settings.owner_chat_id, f"{prefix} {run.message}"
        )
        data = run.result.data if run.result is not None else {}
        latitude, longitude = data.get("latitude"), data.get("longitude")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            send_location(
                settings.telegram_bot_token, settings.owner_chat_id, latitude, longitude
            )

    application = build_application(
        settings,
        reporter=report,
        system_prompt="You are an Android device assistant running a scheduled task.",
    )
    # The caller already opened the store to check whether anything was due;
    # keep using that one so the claim it holds stays valid.
    application.schedule_runner.store = store
    return application.schedule_runner


def main() -> int:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)
    state_dir = os.path.expanduser(settings.state_dir)
    os.makedirs(state_dir, exist_ok=True)
    store = ScheduleStore(os.path.join(state_dir, "schedule.db"))
    try:
        store.purge_completed()
        pending = [task for task in store.all_tasks() if task.enabled]
        if not pending:
            logger.info("Scheduler tick: nothing scheduled")
            return 0
        # Peek without claiming: building a planner and a 45-tool registry is
        # the expensive part, and most ticks have nothing to do.
        if not store.due():
            logger.info("Scheduler tick: %d task(s) scheduled, none due", len(pending))
            return 0
        runner = build_runner(settings, store)
        runs = runner.tick()
        logger.info("Scheduler tick ran %d task(s)", len(runs))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
