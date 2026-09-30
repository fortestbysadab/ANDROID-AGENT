"""Central logging setup.

Logs go to stderr and to a rotating file so failures are diagnosable after the
fact on a phone, where scrollback is short. Secrets are never logged: the
planner logs endpoints and status codes, never headers.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

DEFAULT_LOG_DIR = "~/telegram_agent_v2"
_CONFIGURED = False


def configure_logging(level: str = "INFO", log_dir: str = DEFAULT_LOG_DIR) -> None:
    """Idempotently configure root logging for the agent process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    try:
        directory = os.path.expanduser(log_dir)
        os.makedirs(directory, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(directory, "agent.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        root.warning("File logging unavailable: %s", exc)

    # Third-party libraries are far too chatty at DEBUG.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("TeleBot").setLevel(logging.WARNING)
    _CONFIGURED = True
