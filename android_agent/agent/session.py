"""Expiring conversation sessions with durable, structurally valid history.

A session is a short-lived conversation window. It expires after a period of
inactivity so that an old, stale context never silently leaks into a new
request, and so that sensitive tool output (contacts, SMS, location) does not
linger in model context indefinitely.

Storage is SQLite because Android freely kills background Termux processes;
an in-memory store would lose the conversation on every restart.

Two structural invariants matter, because violating either makes the provider
reject the next request with HTTP 400:

1. Every assistant message carrying `tool_calls` must be followed by exactly
   one `tool` message per call id. Trimming must never orphan a tool call.
2. History is trimmed only at user-message boundaries, which keeps whole
   request/response cycles intact.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_MAX_MESSAGES = 60
DEFAULT_MAX_BYTES = 120_000


@dataclass(frozen=True)
class Session:
    session_id: str
    chat_id: int
    created_at: float
    last_active_at: float
    messages: tuple[Mapping[str, Any], ...]
    turns: int

    def age_seconds(self, now: float | None = None) -> float:
        return (now or time.time()) - self.created_at

    def idle_seconds(self, now: float | None = None) -> float:
        return (now or time.time()) - self.last_active_at

    def expires_in(self, ttl_seconds: float, now: float | None = None) -> float:
        return max(0.0, ttl_seconds - self.idle_seconds(now))


def close_open_tool_calls(
    messages: Sequence[Mapping[str, Any]], reason: str = "not_executed"
) -> list[dict[str, Any]]:
    """Append a synthetic result for any tool call that never got one.

    An assistant message proposing a tool call with no matching `tool` reply
    is a protocol violation. This happens legitimately when a run pauses for
    approval or is cut short by a budget. Rather than discard the turn, close
    it honestly so the model can see what happened.
    """
    answered: set[str] = {
        str(message.get("tool_call_id"))
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }

    closed: list[dict[str, Any]] = []
    for message in messages:
        closed.append(dict(message))
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            answered.add(call_id)
            closed.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": (call.get("function") or {}).get("name", "unknown"),
                    "content": json.dumps(
                        {
                            "status": reason,
                            "summary": "This action was not executed.",
                            "data": {},
                        },
                        separators=(",", ":"),
                    ),
                }
            )
    return closed


def _blocks(messages: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Group messages into user-initiated blocks, a safe place to cut."""
    grouped: list[list[Mapping[str, Any]]] = []
    for message in messages:
        if message.get("role") == "user" or not grouped:
            grouped.append([message])
        else:
            grouped[-1].append(message)
    return grouped


def trim_history(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Mapping[str, Any]]:
    """Drop the oldest whole turns until the history fits the budget.

    Cutting only at user boundaries guarantees no tool call is orphaned.
    """
    grouped = _blocks([m for m in messages if m.get("role") != "system"])
    kept: list[list[Mapping[str, Any]]] = []
    total_messages = 0
    total_bytes = 0

    for block in reversed(grouped):
        block_bytes = len(json.dumps(block, default=str))
        if kept and (
            total_messages + len(block) > max_messages
            or total_bytes + block_bytes > max_bytes
        ):
            break
        kept.insert(0, block)
        total_messages += len(block)
        total_bytes += block_bytes

    return [message for block in kept for message in block]


class SqliteSessionStore:
    """Durable, TTL-bounded conversation sessions keyed by chat id."""

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id        INTEGER PRIMARY KEY,
                session_id     TEXT    NOT NULL,
                created_at     REAL    NOT NULL,
                last_active_at REAL    NOT NULL,
                turns          INTEGER NOT NULL DEFAULT 0,
                messages       TEXT    NOT NULL
            )
            """
        )
        self._connection.commit()

    # -- reads --------------------------------------------------------

    def active(self, chat_id: int, now: float | None = None) -> Session | None:
        """Return the live session for a chat, expiring a stale one first."""
        moment = now or time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT session_id, created_at, last_active_at, turns, messages "
                "FROM sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is None:
                return None
            session_id, created_at, last_active_at, turns, payload = row
            if moment - last_active_at >= self.ttl_seconds:
                self._connection.execute(
                    "DELETE FROM sessions WHERE chat_id = ?", (chat_id,)
                )
                self._connection.commit()
                logger.info(
                    "Session %s for chat %s expired after %.0fs idle",
                    session_id,
                    chat_id,
                    moment - last_active_at,
                )
                return None
            try:
                messages = tuple(json.loads(payload))
            except json.JSONDecodeError:
                logger.warning("Corrupt session payload for chat %s; discarding", chat_id)
                return None
            return Session(session_id, chat_id, created_at, last_active_at, messages, turns)

    def history(self, chat_id: int, now: float | None = None) -> list[Mapping[str, Any]]:
        session = self.active(chat_id, now)
        return list(session.messages) if session else []

    # -- writes -------------------------------------------------------

    def save(
        self,
        chat_id: int,
        messages: Iterable[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> Session:
        """Persist history for a chat, creating or extending its session."""
        moment = now or time.time()
        cleaned = close_open_tool_calls(list(messages))
        trimmed = trim_history(
            cleaned, max_messages=self.max_messages, max_bytes=self.max_bytes
        )
        payload = json.dumps(trimmed, default=str)

        with self._lock:
            row = self._connection.execute(
                "SELECT session_id, created_at, last_active_at, turns "
                "FROM sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

            if row is None or (moment - row[2]) >= self.ttl_seconds:
                session_id = uuid.uuid4().hex[:12]
                created_at = moment
                turns = 1
            else:
                session_id, created_at, _, turns = row[0], row[1], row[2], row[3] + 1

            self._connection.execute(
                "INSERT INTO sessions "
                "(chat_id, session_id, created_at, last_active_at, turns, messages) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "session_id=excluded.session_id, created_at=excluded.created_at, "
                "last_active_at=excluded.last_active_at, turns=excluded.turns, "
                "messages=excluded.messages",
                (chat_id, session_id, created_at, moment, turns, payload),
            )
            self._connection.commit()

        return Session(session_id, chat_id, created_at, moment, tuple(trimmed), turns)

    def reset(self, chat_id: int) -> bool:
        """End the current session. Returns True if one was active."""
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM sessions WHERE chat_id = ?", (chat_id,)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def purge_expired(self, now: float | None = None) -> int:
        moment = now or time.time()
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM sessions WHERE ? - last_active_at >= ?",
                (moment, self.ttl_seconds),
            )
            self._connection.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._connection.close()
