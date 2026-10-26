"""Choosing a screen backend at startup.

The companion app is preferred when it is paired, because it needs no host
machine and survives a reboot. ADB remains the fallback so the capability
still works on a device where the APK has not been installed.
"""

from __future__ import annotations

import logging
import os

from .backend import AdbBackend, Backend
from .companion import TOKEN_VARIABLE, CompanionBackend

logger = logging.getLogger(__name__)


def choose_backend(environment: dict[str, str] | None = None) -> Backend:
    """Return the companion backend if a pairing token is configured.

    Only the presence of a token is checked, not whether the service is
    answering. Probing here would make startup depend on the phone's
    accessibility toggle, and the backend already reports an unreachable
    service with an actionable message at the moment it is used.
    """
    source = os.environ if environment is None else environment
    token = (source.get(TOKEN_VARIABLE) or "").strip()
    if token:
        logger.info("App automation will use the companion app.")
        return CompanionBackend(token=token)
    logger.info("App automation will use ADB; no companion token is configured.")
    return AdbBackend()
