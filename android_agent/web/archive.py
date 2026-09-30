"""Durable transcript archive for the web console's "recent chats" list.

This is deliberately *not* the same thing as `SqliteSessionStore`. The session
store holds the model's working context and expires it on an idle timer,
because sensitive tool output (contacts, SMS, location) should not linger where
a later prompt can pick it up. That privacy property is worth keeping.

What the archive stores is the rendered transcript the owner already read on
screen: the same list of bubbles the UI draws. Reopening an archived chat
restores the *view*, not the model's memory - the assistant genuinely does not
remember an expired conversation, and the UI says so rather than pretending.

Retention is bounded: the oldest conversations fall off past `max_conversations`,
and the owner can delete one or wipe the lot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONVERSATIONS = 30
#: Titles are shown in a narrow sidebar; longer text is cut with an ellipsis.
TITLE_LIMIT = 60


@dataclass(frozen=True)
class ConversationSummary:
    """One row in the recent-chats list."""

    conversation_id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.conversation_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
        }


def new_conversation_id() -> str:
    return uuid.uuid4().hex[:16]


def derive_title(transcript: Sequence[Mapping[str, Any]]) -> str:
    """Title a chat from its first user message, like every chat product does."""
    for entry in transcript:
        if entry.get("role") == "user":
            text = " ".join(str(entry.get("text", "")).split())
            if text:
                return text[: TITLE_LIMIT - 1] + "…" if len(text) > TITLE_LIMIT else text
    return "New chat"


class ChatArchive:
    """Append-only-ish store of rendered transcripts, capped and prunable."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
    ) -> None:
        if max_conversations < 1:
            raise ValueError("max_conversations must be at least 1")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_conversations = max_conversations
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT    PRIMARY KEY,
                title           TEXT    NOT NULL,
                created_at      REAL    NOT NULL,
                updated_at      REAL    NOT NULL,
                message_count   INTEGER NOT NULL,
                transcript      TEXT    NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS conversations_updated "
            "ON conversations (updated_at DESC)"
        )
        self._connection.commit()

    # -- writes -------------------------------------------------------

    def save(
        self,
        conversation_id: str,
        transcript: Sequence[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> None:
        """Create or update one conversation, then prune past the cap.

        An empty transcript is never stored: opening the console and typing
        nothing should not leave a ghost entry in the sidebar.
        """
        if not transcript:
            return
        moment = now if now is not None else time.time()
        payload = json.dumps(list(transcript))
        title = derive_title(transcript)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO conversations
                    (conversation_id, title, created_at, updated_at, message_count, transcript)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title         = excluded.title,
                    updated_at    = excluded.updated_at,
                    message_count = excluded.message_count,
                    transcript    = excluded.transcript
                """,
                (conversation_id, title, moment, moment, len(transcript), payload),
            )
            self._prune_locked()
            self._connection.commit()

    def delete(self, conversation_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def clear(self) -> int:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM conversations")
            self._connection.commit()
            return cursor.rowcount

    def _prune_locked(self) -> None:
        """Drop the oldest rows beyond the cap. Caller holds the lock."""
        self._connection.execute(
            """
            DELETE FROM conversations WHERE conversation_id IN (
                SELECT conversation_id FROM conversations
                ORDER BY updated_at DESC LIMIT -1 OFFSET ?
            )
            """,
            (self.max_conversations,),
        )

    # -- reads --------------------------------------------------------

    def recent(self, limit: int | None = None) -> list[ConversationSummary]:
        capped = self.max_conversations if limit is None else min(limit, self.max_conversations)
        with self._lock:
            rows = self._connection.execute(
                "SELECT conversation_id, title, created_at, updated_at, message_count "
                "FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (capped,),
            ).fetchall()
        return [ConversationSummary(*row) for row in rows]

    def transcript(self, conversation_id: str) -> list[dict[str, Any]] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT transcript FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            loaded = json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("Corrupt archived transcript %s; dropping it", conversation_id)
            self.delete(conversation_id)
            return None
        return loaded if isinstance(loaded, list) else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()
