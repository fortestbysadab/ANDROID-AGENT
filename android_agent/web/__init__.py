"""Local web console for the Android agent.

`python -m android_agent.web` serves a single-page UI over the same
AgentRuntime the Telegram bot uses, so policy, approvals and audit behave
identically on both front ends.
"""

from android_agent.web.security import AuthManager, WebConfigError, validate_token
from android_agent.web.server import WebApp, make_server

__all__ = ["AuthManager", "WebApp", "WebConfigError", "make_server", "validate_token"]
