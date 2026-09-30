"""Authentication and abuse controls for the web UI.

This UI is intended to be reachable through a Cloudflare tunnel, which means
a public URL. Behind that URL sits an agent that can use the camera, read
SMS, place calls and read files. Every control here is therefore fail-closed:

* No token configured means the server refuses to start.
* A short or weak token is rejected at startup, not at first login.
* Token comparison is constant time.
* Failed logins are rate limited per client, then locked out.
* Cookies are HttpOnly + SameSite=Strict, and API calls additionally require
  a custom header, so a hostile page cannot drive the agent cross-site.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Refuse anything a human could plausibly guess or brute force.
MIN_TOKEN_LENGTH = 16

#: Login throttling.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 900.0
LOCKOUT_SECONDS = 900.0

#: General API request ceiling per client.
MAX_REQUESTS_PER_MINUTE = 60

DEFAULT_SESSION_TTL = 12 * 3600.0


class WebConfigError(RuntimeError):
    """Raised when the web UI is misconfigured in an unsafe way."""


def validate_token(token: str | None) -> str:
    """Return a usable access token or explain why there is not one."""
    candidate = (token or "").strip()
    if not candidate:
        raise WebConfigError(
            "ANDROID_AGENT_WEB_TOKEN is not set. The web UI controls your phone "
            "and would be exposed by a public tunnel URL, so it refuses to start "
            "without one.\nGenerate a strong token with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if len(candidate) < MIN_TOKEN_LENGTH:
        raise WebConfigError(
            f"ANDROID_AGENT_WEB_TOKEN must be at least {MIN_TOKEN_LENGTH} characters; "
            f"got {len(candidate)}."
        )
    if candidate.lower() in {"password", "changeme", "secret", "token", "admin", "test"}:
        raise WebConfigError("ANDROID_AGENT_WEB_TOKEN is a well-known value; choose another.")
    return candidate


@dataclass
class _ClientState:
    failures: deque[float] = field(default_factory=deque)
    requests: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class AuthManager:
    """Token login, browser sessions, rate limiting, and lockout."""

    def __init__(
        self,
        token: str,
        *,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL,
    ) -> None:
        self.token = validate_token(token)
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, float] = {}
        self._clients: dict[str, _ClientState] = defaultdict(_ClientState)
        self._lock = threading.Lock()

    # -- login --------------------------------------------------------

    def locked_out(self, client: str, now: float | None = None) -> float:
        """Seconds remaining on a lockout, or 0."""
        moment = now or time.time()
        with self._lock:
            state = self._clients[client]
            return max(0.0, state.locked_until - moment)

    def login(self, supplied: str, client: str, now: float | None = None) -> str | None:
        """Exchange a token for a session id, or None on failure."""
        moment = now or time.time()
        remaining = self.locked_out(client, moment)
        if remaining > 0:
            logger.warning("Login attempt from locked-out client %s", client)
            return None

        # Constant time: never leak token length or prefix through timing.
        if not hmac.compare_digest(str(supplied or ""), self.token):
            self._record_failure(client, moment)
            logger.warning("Failed web login from %s", client)
            return None

        with self._lock:
            self._clients[client].failures.clear()
            self._prune_sessions(moment)
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = moment + self.session_ttl_seconds
        logger.info("Web login succeeded for %s", client)
        return session_id

    def _record_failure(self, client: str, now: float) -> None:
        with self._lock:
            state = self._clients[client]
            state.failures.append(now)
            while state.failures and now - state.failures[0] > FAILURE_WINDOW_SECONDS:
                state.failures.popleft()
            if len(state.failures) >= MAX_FAILURES:
                state.locked_until = now + LOCKOUT_SECONDS
                state.failures.clear()
                logger.error(
                    "Client %s locked out for %.0f minutes after %d failed logins",
                    client,
                    LOCKOUT_SECONDS / 60,
                    MAX_FAILURES,
                )

    # -- sessions -----------------------------------------------------

    def valid_session(self, session_id: str | None, now: float | None = None) -> bool:
        if not session_id:
            return False
        moment = now or time.time()
        with self._lock:
            expiry = self._sessions.get(session_id)
            if expiry is None:
                return False
            if expiry <= moment:
                self._sessions.pop(session_id, None)
                return False
            return True

    def logout(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def _prune_sessions(self, now: float) -> None:
        for key in [key for key, expiry in self._sessions.items() if expiry <= now]:
            self._sessions.pop(key, None)

    # -- rate limiting ------------------------------------------------

    def allow_request(self, client: str, now: float | None = None) -> bool:
        moment = now or time.time()
        with self._lock:
            requests = self._clients[client].requests
            requests.append(moment)
            while requests and moment - requests[0] > 60.0:
                requests.popleft()
            return len(requests) <= MAX_REQUESTS_PER_MINUTE
