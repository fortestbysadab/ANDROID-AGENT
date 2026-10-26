"""One command to run the agent: `python -m android_agent`.

Previously there were four: `python agent_bot.py`, `python -m
android_agent.web`, `python -m android_agent.schedule` and `python -m
android_agent.doctor`. Each still works, but nobody should have to remember
which combination to start, or notice that forgetting the scheduler means
their reminders quietly never fire.

    python -m android_agent              # everything that is configured
    python -m android_agent bot          # Telegram only
    python -m android_agent web          # web console only
    python -m android_agent tick         # one scheduler pass, then exit
    python -m android_agent doctor       # diagnose the configuration
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from android_agent.config import Settings
from android_agent.observability.logging import configure_logging

logger = logging.getLogger("android_agent")


def _settings(require_telegram: bool) -> Settings:
    try:
        return Settings.from_env(require_telegram=require_telegram)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Run `python -m android_agent doctor` for a full diagnosis.", file=sys.stderr)
        raise SystemExit(2) from exc


def run_bot() -> int:
    import agent_bot

    agent_bot.main()
    return 0


def run_web() -> int:
    from android_agent.web.__main__ import main as web_main

    return web_main() or 0


def run_tick() -> int:
    from android_agent.schedule.__main__ import main as tick_main

    return tick_main()


def run_doctor() -> int:
    from android_agent.doctor import main as doctor_main

    return doctor_main() or 0


def run_all() -> int:
    """Telegram in the foreground, the web console beside it.

    The scheduler already runs inside the bot, so starting both here is the
    whole agent. The web console goes in a daemon thread: if Telegram stops,
    the process should end rather than linger half-alive.
    """
    settings = _settings(require_telegram=True)
    configure_logging(settings.log_level)

    web_thread = threading.Thread(target=_web_forever, name="web-console", daemon=True)
    web_thread.start()
    # Give the console a moment to bind or fail loudly before the bot's own
    # logging starts, so a port clash is readable rather than interleaved.
    time.sleep(0.5)

    import agent_bot

    agent_bot.main()
    return 0


def _web_forever() -> None:
    try:
        from android_agent.web.__main__ import main as web_main

        web_main()
    except SystemExit as exc:
        # A missing web token is a configuration choice, not a crash: the
        # owner may be running Telegram only.
        logger.info("Web console not started (%s). Telegram is unaffected.", exc)
    except Exception:
        logger.exception("Web console stopped; Telegram is unaffected")


COMMANDS = {
    "all": run_all,
    "bot": run_bot,
    "web": run_web,
    "tick": run_tick,
    "doctor": run_doctor,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m android_agent",
        description="Run the Android agent.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=sorted(COMMANDS),
        help="all (default): Telegram plus the web console. "
             "bot / web: one front end. tick: one scheduler pass. "
             "doctor: diagnose the configuration.",
    )
    options = parser.parse_args(argv)
    try:
        return COMMANDS[options.command]()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
