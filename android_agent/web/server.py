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
from android_agent.web.archive import ChatArchive, new_conversation_id
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
        archive: ChatArchive,
        auth: AuthManager,
        owner_id: int,
        session_ttl_seconds: float,
        session_key: int | None = None,
        documents=None,
        schedule_store=None,
    ) -> None:
        self.runtime = runtime
        self.approvals = approvals
        self.sessions = sessions
        self.archive = archive
        self.auth = auth
        self.owner_id = owner_id
        self.session_ttl_seconds = session_ttl_seconds
        # Optional read-only views. Absent in tests that only exercise chat.
        self.documents = documents
        self.schedule_store = schedule_store
        # Keep the web conversation separate from the Telegram one so the two
        # front ends do not interleave into a single history.
        self.session_key = session_key if session_key is not None else -abs(owner_id)
        self.run_lock = threading.Lock()
        self.transcript_lock = threading.Lock()
        # Resume the most recent archived chat so a restart (Termux kills
        # background processes freely) does not present a blank screen.
        recent = self.archive.recent(limit=1)
        self.conversation_id = recent[0].conversation_id if recent else new_conversation_id()
        self.transcript: list[dict[str, Any]] = (
            self.archive.transcript(self.conversation_id) or [] if recent else []
        )

    def add_message(self, role: str, text: str, **extra: Any) -> dict[str, Any]:
        entry = {"role": role, "text": text, "at": time.time(), **extra}
        with self.transcript_lock:
            self.transcript.append(entry)
            del self.transcript[:-200]
            snapshot = list(self.transcript)
        self.archive.save(self.conversation_id, snapshot)
        return entry

    def persist(self) -> None:
        """Write the current transcript back to the archive."""
        with self.transcript_lock:
            snapshot = list(self.transcript)
        self.archive.save(self.conversation_id, snapshot)

    def start_new_conversation(self) -> None:
        """Archive whatever is on screen and begin an empty chat."""
        self.persist()
        self.sessions.reset(self.session_key)
        with self.transcript_lock:
            self.transcript.clear()
            self.conversation_id = new_conversation_id()

    def open_conversation(self, conversation_id: str) -> bool:
        """Restore an archived transcript for viewing.

        The model's context is deliberately not restored: it expired, and
        pretending otherwise would make the assistant look like it forgot
        things it can see on screen. The UI surfaces this as a notice.
        """
        restored = self.archive.transcript(conversation_id)
        if restored is None:
            return False
        self.persist()
        self.sessions.reset(self.session_key)
        with self.transcript_lock:
            self.conversation_id = conversation_id
            self.transcript[:] = restored
        return True

    def forget_conversation(self, conversation_id: str) -> bool:
        deleted = self.archive.delete(conversation_id)
        if conversation_id == self.conversation_id:
            self.sessions.reset(self.session_key)
            with self.transcript_lock:
                self.transcript.clear()
                self.conversation_id = new_conversation_id()
        return deleted

    def forget_all(self) -> int:
        removed = self.archive.clear()
        self.sessions.reset(self.session_key)
        with self.transcript_lock:
            self.transcript.clear()
            self.conversation_id = new_conversation_id()
        return removed


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

    def _serve_document(self, name: str) -> None:
        """Serve one file by name from any folder the agent writes into.

        A name, never a path: separators and leading dots are refused before
        anything touches the filesystem, and the resolved path must still sit
        inside one of the known folders.
        """
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Bad file name."})
            return
        candidate = None
        for _, folder in self._file_folders():
            try:
                root = folder.resolve()
                attempt = (root / name).resolve()
                attempt.relative_to(root)
            except (ValueError, OSError):
                continue
            if attempt.is_file():
                candidate = attempt
                break
        if candidate is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "No such file."})
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send(
            HTTPStatus.OK, candidate.read_bytes(), content_type,
            {
                "Content-Disposition": f'inline; filename="{candidate.name}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=60",
            },
        )

    def _require_media_auth(self) -> bool:
        """Auth + rate limit, without the custom-header requirement."""
        if not self.app.auth.allow_request(self.client_identity()):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Rate limit exceeded."})
            return False
        if not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Not signed in."})
            return False
        return True

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

        if path == "/api/conversations":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, {"items": self._conversations()})
            return

        if path == "/api/media":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, {"items": self._media_index(), "root": str(media_root())})
            return

        if path == "/api/files/download":
            if not self._require_media_auth():
                return
            self._serve_document(parse_qs(route.query).get("name", [""])[0])
            return
        if path == "/api/media/file":
            # Deliberately exempt from the custom-header check: this URL is
            # opened as a top-level navigation (tapping a photo opens a tab)
            # and a browser cannot attach a custom header to that. CSRF is not
            # the relevant risk for a read anyway, and the SameSite=Strict
            # cookie already stops another site from loading these bytes.
            if not self._require_media_auth():
                return
            self._serve_media(parse_qs(route.query).get("name", [""])[0])
            return

        if path == "/api/files":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, {"files": self._files()})
            return
        if path == "/api/tools":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, {"tools": self._tools()})
            return
        if path == "/api/schedule":
            if not self._require_api_auth():
                return
            self._json(HTTPStatus.OK, {"tasks": self._tasks()})
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
            self.app.start_new_conversation()
            self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})
        elif path == "/api/conversations/open":
            wanted = str(self._body().get("id", ""))
            if not self.app.open_conversation(wanted):
                self._json(HTTPStatus.NOT_FOUND, {"error": "That chat is gone."})
                return
            self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})
        elif path == "/api/conversations/delete":
            self.app.forget_conversation(str(self._body().get("id", "")))
            self._json(HTTPStatus.OK, {"ok": True, "state": self._state()})
        elif path == "/api/conversations/clear":
            removed = self.app.forget_all()
            logger.info("Owner cleared %d archived web conversations", removed)
            self._json(HTTPStatus.OK, {"ok": True, "removed": removed, "state": self._state()})
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
        body = self._body()
        text = str(body.get("text", "")).strip()
        # The composer's Web search toggle. It expresses intent rather than
        # forcing a call: the model still chooses, and the tool still passes
        # through policy and taint like any other.
        wants_search = bool(body.get("web_search"))
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
            prompt = text
            if wants_search:
                prompt = (
                    f"{text}\n\n[The owner turned web search on for this "
                    "message. Use web_search and cite the links you used.]"
                )
            outcome = self.app.runtime.run(
                prompt,
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

    def _file_folders(self) -> list[tuple[str, Path]]:
        """Every folder the agent writes into, with the kind it holds.

        Documents and captures were being kept apart for no reason the owner
        cares about: a screenshot is a file they made and expect to find.
        """
        from android_agent.documents.store import documents_root
        from android_agent.tools.media import MEDIA_KINDS, media_root

        folders = [("document", documents_root())]
        root = media_root()
        for kind in MEDIA_KINDS.values():
            folders.append((kind.name, Path(root) / kind.folder))
        return folders

    def _files(self) -> list[dict[str, object]]:
        """Everything the agent has written, newest first.

        Lists the folders themselves rather than only the document store, so
        files the owner put there by hand show up too.
        """
        known = {}
        if self.app.documents is not None:
            for document in self.app.documents.recent():
                known[Path(document.path).name] = document

        entries: list[dict[str, object]] = []
        for kind, folder in self._file_folders():
            try:
                paths = [path for path in folder.iterdir() if path.is_file()]
            except OSError:
                continue  # a folder only exists once something is written there
            for path in paths:
                stat = path.stat()
                document = known.get(path.name)
                entries.append({
                    "name": path.name,
                    "kind": kind,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "format": path.suffix.lstrip(".").lower(),
                    "title": document.title if document else path.stem,
                    "version": document.version if document else None,
                    "id": document.document_id if document else None,
                    "has_script": bool(document),
                })
        entries.sort(key=lambda item: item["modified"], reverse=True)
        return entries

    def _tools(self) -> list[dict[str, object]]:
        registry = self.app.runtime.registry
        tools = []
        for schema in registry.model_schemas():
            name = schema["function"]["name"]
            spec = registry.get(name)
            tools.append({
                "name": name,
                "risk": spec.risk.value,
                "description": spec.description,
                "idempotent": spec.idempotent,
                "untrusted": spec.returns_untrusted_content,
            })
        return sorted(tools, key=lambda item: item["name"])

    def _tasks(self) -> list[dict[str, object]]:
        if self.app.schedule_store is None:
            return []
        return [task.as_dict() for task in self.app.schedule_store.all_tasks()]

    def _artifacts(self, results) -> list[dict[str, object]]:
        artifacts = []
        for result in results:
            # Media tools report a name and kind; documents report only the
            # path they wrote. Both are files the owner just made and expects
            # to be able to open, so both become artifacts.
            name = result.data.get("artifact_name")
            kind = result.data.get("media_kind")
            path = result.data.get("artifact_path")
            if not isinstance(name, str) and isinstance(path, str):
                name = Path(path).name
            if isinstance(name, str) and name:
                suffix = Path(name).suffix.lower().lstrip(".")
                artifacts.append({
                    "name": name,
                    "kind": kind if isinstance(kind, str) else "document",
                    "format": suffix,
                    "previewable": suffix in {"png", "jpg", "jpeg", "gif", "webp"},
                })
            latitude = result.data.get("latitude")
            longitude = result.data.get("longitude")
            if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                # Coordinates travel as numbers as well as text so the UI can
                # offer a map without parsing a sentence back apart.
                artifact = {
                    "kind": "location",
                    "name": f"{latitude:.5f}, {longitude:.5f}",
                    "latitude": round(float(latitude), 6),
                    "longitude": round(float(longitude), 6),
                }
                accuracy = result.data.get("accuracy")
                if isinstance(accuracy, (int, float)):
                    artifact["accuracy"] = round(float(accuracy), 1)
                if result.data.get("approximate"):
                    artifact["approximate"] = True
                artifacts.append(artifact)
        return artifacts

    def _conversations(self) -> list[dict[str, Any]]:
        return [
            {**summary.as_dict(), "current": summary.conversation_id == self.app.conversation_id}
            for summary in self.app.archive.recent()
        ]

    def _state(self) -> dict[str, Any]:
        session = self.app.sessions.active(self.app.session_key)
        with self.app.transcript_lock:
            transcript = list(self.app.transcript)
        return {
            "messages": transcript,
            "conversation_id": self.app.conversation_id,
            "conversations": self._conversations(),
            # True when the screen shows history the model can no longer see.
            "context_lost": bool(transcript) and session is None,
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
                self._send(
                    HTTPStatus.OK,
                    candidate.read_bytes(),
                    content_type,
                    {
                        # Show it in the tab; never let the browser re-sniff a
                        # photo into something executable.
                        "Content-Disposition": f'inline; filename="{candidate.name}"',
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "private, max-age=300",
                    },
                )
                return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})


def make_server(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def storage_note() -> str:
    return storage_advice() or ""
