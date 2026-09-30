"""Local web UI for the agent, designed to sit behind a Cloudflare tunnel.

Standard library only: Termux installs stay small, and there is no ASGI stack
to keep alive on a phone. Threaded HTTP with short polling is more than
enough for a single-owner chat UI.

The UI is a thin client over exactly the same `AgentRuntime`, policy engine,
approval store and session store that Telegram uses. It is not a second
privileged path: every request is evaluated by the same policy, and
consequential actions still require an explicit approval click.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
from collections.abc import Mapping
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from android_agent.agent.runtime import RunStatus
from android_agent.agent.session import SqliteSessionStore
from android_agent.tools.media import MEDIA_KINDS, media_root, storage_advice
from android_agent.web.security import AuthManager

logger = logging.getLogger(__name__)

COOKIE_NAME = "aa_session"
#: A hostile cross-site page cannot set a custom header on a simple request,
#: so requiring one blocks CSRF even if SameSite were bypassed.
CSRF_HEADER = "X-Android-Agent"
MAX_BODY_BYTES = 64 * 1024

UI_PATH = Path(__file__).with_name("ui.html")


class WebApp:
    """Holds the shared state the request handler needs."""

    def __init__(
        self,
        *,
        runtime,
        approvals,
        sessions: SqliteSessionStore,
        auth: AuthManager,
        owner_id: int,
        session_ttl_seconds: float,
        session_key: int | None = None,
    ) -> None:
        self.runtime = runtime
        self.approvals = approvals
        self.sessions = sessions
        self.auth = auth
        self.owner_id = owner_id
        self.session_ttl_seconds = session_ttl_seconds
        # Keep the web conversation separate from the Telegram one so the two
        # front ends do not interleave into a single history.
        self.session_key = session_key if session_key is not None else -abs(owner_id)
        self.run_lock = threading.Lock()
        self.transcript: list[dict[str, Any]] = []
        self.transcript_lock = threading.Lock()

    def add_message(self, role: str, text: str, **extra: Any) -> dict[str, Any]:
        entry = {"role": role, "text": text, "at": time.time(), **extra}
        with self.transcript_lock:
            self.transcript.append(entry)
            del self.transcript[:-200]
        return entry

    def clear_transcript(self) -> None:
        with self.transcript_lock:
            self.transcript.clear()


class Handler(BaseHTTPRequestHandler):
    server_version = "AndroidAgentWeb/2"
    protocol_version = "HTTP/1.1"

    app: WebApp  # injected by make_server

    # -- plumbing -----------------------------------------------------

    def log_message(self, fmt, *args):
        logger.debug("web %s - %s", self.client_identity(), fmt % args)

    def client_identity(self) -> str:
        """Identify the caller for rate limiting.

        Behind a Cloudflare tunnel every connection appears to come from
        localhost, so prefer the forwarded client IP when present.
        """
        for header in ("CF-Connecting-IP", "X-Forwarded-For"):
            value = self.headers.get(header)
            if value:
                return value.split(",")[0].strip()[:64]
        return self.client_address[0]

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self' data:; media-src 'self'; "
            "style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Mapping[str, Any], extra: dict | None = None):
        self._send(status, json.dumps(payload).encode(), "application/json", extra)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def _session_id(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _authenticated(self) -> bool:
        return self.app.auth.valid_session(self._session_id())

    def _require_api_auth(self) -> bool:
        """Auth + CSRF + rate limit for every /api route except login."""
        if not self.app.auth.allow_request(self.client_identity()):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Rate limit exceeded."})
            return False
        if self.headers.get(CSRF_HEADER) != "1":
            self._json(HTTPStatus.FORBIDDEN, {"error": "Missing request header."})
            return False
        if not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Not signed in."})
            return False
        return True

    # -- routes -------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path)
        path = route.path

        if path in ("/", "/index.html"):
            try:
                body = UI_PATH.read_bytes()
            except OSError:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"UI missing", "text/plain")
                return
            self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return

        if path == "/api/state":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, self._state())
            return

        if path == "/api/media":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, {"items": self._media_index(), "root": str(media_root())})
            return

        if path == "/api/media/file":
            if not self._require_api_auth():
                return
            self._serve_media(parse_qs(route.query).get("name", [""])[0])
            return

        if path == "/api/ping":
            self._json(HTTPStatus.OK, {"ok": True, "authenticated": self._authenticated()})
            return

        self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/login":
            self._login()
            return
        if path == "/api/logout":
            self.app.auth.logout(self._session_id())
            self._json(
                HTTPStatus.OK,
                {"ok": True},
                {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"},
            )
            return

        if not self._require_api_auth():
            return

        if path == "/api/message":
            self._message()
        elif path == "/api/approval":
            self._approval()
        elif path == "/api/new":
            self.app.sessions.reset(self.app.session_key)
            self.app.clear_transcript()
            self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint."})

    # -- handlers -----------------------------------------------------

    def _login(self):
        client = self.client_identity()
        if not self.app.auth.allow_request(client):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many requests."})
            return
        locked = self.app.auth.locked_out(client)
        if locked > 0:
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": f"Locked out. Try again in {int(locked // 60) + 1} minutes."},
            )
            return

        session_id = self.app.auth.login(str(self._body().get("token", "")), client)
        if session_id is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid access token."})
            return
        self._json(
            HTTPStatus.OK,
            {"ok": True},
            {
                "Set-Cookie": (
                    f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; "
                    "SameSite=Strict; Secure; Max-Age=43200"
                )
            },
        )

    def _message(self):
        text = str(self._body().get("text", "")).strip()
        if not text:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Empty message."})
            return
        if len(text) > 4000:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Message too long."})
            return

        if not self.app.run_lock.acquire(blocking=False):
            self._json(HTTPStatus.CONFLICT, {"error": "A request is already running."})
            return
        try:
            self.app.add_message("user", text)
            previous = self.app.sessions.active(self.app.session_key)
            outcome = self.app.runtime.run(
                text,
                actor_id=str(self.app.owner_id),
                chat_id=self.app.owner_id,
                prior_messages=list(previous.messages) if previous else (),
            )
            if outcome.status is not RunStatus.FAILED:
                self.app.sessions.save(self.app.session_key, outcome.messages)

            if outcome.status is RunStatus.APPROVAL_REQUIRED:
                for pending in outcome.pending_approvals:
                    record = self.app.approvals.create(
                        actor_id=str(self.app.owner_id),
                        chat_id=self.app.owner_id,
                        run_id=outcome.run_id,
                        pending=pending,
                    )
                    self.app.add_message(
                        "approval",
                        f"Approval required: {pending.call.name.replace('_', ' ')}",
                        approval_id=record.approval_id,
                        tool=pending.call.name,
                        arguments={k: str(v)[:300] for k, v in pending.call.arguments.items()},
                    )
            else:
                if outcome.status is RunStatus.FAILED:
                    logger.error("Web run %s failed: %s", outcome.run_id, outcome.error)
                self.app.add_message(
                    "assistant",
                    outcome.text,
                    artifacts=self._artifacts(outcome.tool_results),
                )
            self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})
        finally:
            self.app.run_lock.release()

    def _approval(self):
        body = self._body()
        approval_id = str(body.get("approval_id", ""))
        approve = bool(body.get("approve"))

        if not approve:
            self.app.approvals.deny(
                approval_id, actor_id=str(self.app.owner_id), chat_id=self.app.owner_id
            )
            self._mark_approval(approval_id, "Denied.")
            self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})
            return

        record = self.app.approvals.consume(
            approval_id, actor_id=str(self.app.owner_id), chat_id=self.app.owner_id
        )
        if record is None:
            self._mark_approval(approval_id, "Expired or already used.")
            self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})
            return

        result = self.app.runtime.execute_approved(
            record.pending,
            actor_id=str(self.app.owner_id),
            chat_id=self.app.owner_id,
            run_id=record.run_id,
        )
        self._mark_approval(approval_id, f"{result.status}: {result.summary}")
        self.app.add_message(
            "assistant", result.summary, artifacts=self._artifacts([result])
        )
        session = self.app.sessions.active(self.app.session_key)
        if session is not None:
            self.app.sessions.save(
                self.app.session_key,
                [
                    *session.messages,
                    {
                        "role": "assistant",
                        "content": (
                            f"The owner approved {record.pending.call.name}. "
                            f"Result: {result.status} - {result.summary}"
                        ),
                    },
                ],
            )
        self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})

    def _mark_approval(self, approval_id: str, outcome: str) -> None:
        with self.app.transcript_lock:
            for entry in self.app.transcript:
                if entry.get("approval_id") == approval_id:
                    entry["resolved"] = outcome

    # -- helpers ------------------------------------------------------

    def _artifacts(self, results) -> list[dict[str, str]]:
        artifacts = []
        for result in results:
            name = result.data.get("artifact_name")
            kind = result.data.get("media_kind")
            if isinstance(name, str) and isinstance(kind, str):
                artifacts.append({"name": name, "kind": kind})
            latitude = result.data.get("latitude")
            longitude = result.data.get("longitude")
            if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                artifacts.append(
                    {"kind": "location", "name": f"{latitude:.5f}, {longitude:.5f}"}
                )
        return artifacts

    def _state(self) -> dict[str, Any]:
        session = self.app.sessions.active(self.app.session_key)
        with self.app.transcript_lock:
            transcript = list(self.app.transcript)
        return {
            "messages": transcript,
            "session": {
                "active": session is not None,
                "id": session.session_id if session else None,
                "turns": session.turns if session else 0,
                "expires_in": int(session.expires_in(self.app.session_ttl_seconds))
                if session
                else 0,
            },
            "tools": len(self.app.runtime.registry),
        }

    def _media_index(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        root = media_root()
        for kind in MEDIA_KINDS.values():
            directory = root / kind.folder
            if not directory.is_dir():
                continue
            for item in sorted(
                (f for f in directory.iterdir() if f.is_file()),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )[:40]:
                items.append(
                    {
                        "name": item.name,
                        "kind": kind.name,
                        "folder": kind.folder,
                        "size": item.stat().st_size,
                        "modified": item.stat().st_mtime,
                    }
                )
        items.sort(key=lambda entry: entry["modified"], reverse=True)
        return items

    def _serve_media(self, name: str):
        """Serve one media file, refusing anything outside the media root."""
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid name."})
            return
        root = media_root().resolve()
        for kind in MEDIA_KINDS.values():
            candidate = (root / kind.folder / name).resolve()
            # Re-check containment after resolution to defeat symlink escapes.
            if not str(candidate).startswith(str(root) + os.sep):
                continue
            if candidate.is_file():
                content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)
                return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})


def make_server(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def storage_note() -> str:
    return storage_advice() or ""
