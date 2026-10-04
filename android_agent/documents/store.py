"""Durable record of documents the agent has written.

Keeping the **source** is the whole point. A rendered PDF cannot be revised,
but the Markdown behind it can, so every document keeps its source and
produces a new numbered version each time it is rendered. "Make the heading
bigger" edits the source and renders v2; v1 stays on disk, because the owner
asked for a new version rather than a destroyed one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Where finished files land, inside the folder the owner already browses.
FILES_FOLDER = "files"
MAX_VERSIONS_LISTED = 50


def documents_root() -> Path:
    """`<media root>/files`, created on demand."""
    from android_agent.tools.media import media_root

    root = Path(media_root()) / FILES_FOLDER
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_document_id() -> str:
    return uuid.uuid4().hex[:8]


def slugify(title: str) -> str:
    """A filename a human can recognise, in any script.

    Only characters that break filesystems are replaced. Devanagari and
    Bengali titles keep their own letters rather than being transliterated
    into unreadable ASCII.
    """
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title or "")
    cleaned = re.sub(r"\s+", "-", cleaned.strip())
    cleaned = cleaned.strip("-.")
    return (cleaned or "document")[:60]


@dataclass(frozen=True)
class Document:
    document_id: str
    title: str
    kind: str
    fmt: str
    source: Any
    version: int
    path: str
    created_at: float
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.document_id,
            "title": self.title,
            "kind": self.kind,
            "format": self.fmt,
            "version": self.version,
            "path": self.path,
            "filename": Path(self.path).name,
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at)),
        }

    def summary_line(self) -> str:
        return (
            f"[{self.document_id}] {self.title} — {self.fmt.upper()} "
            f"v{self.version} ({Path(self.path).name})"
        )


def _row(row: sqlite3.Row) -> Document:
    return Document(
        document_id=row["document_id"],
        title=row["title"],
        kind=row["kind"],
        fmt=row["fmt"],
        source=json.loads(row["source"]),
        version=row["version"],
        path=row["path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class DocumentStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                fmt TEXT NOT NULL,
                source TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                path TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()

    def save(
        self, *, document_id: str, title: str, kind: str, fmt: str,
        source: Any, version: int, path: str, now: float | None = None,
    ) -> Document:
        moment = time.time() if now is None else now
        encoded = json.dumps(source, ensure_ascii=False)
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            created = existing["created_at"] if existing else moment
            self._connection.execute(
                "INSERT INTO documents (document_id, title, kind, fmt, source, version, "
                "path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(document_id) DO UPDATE SET title=excluded.title, "
                "kind=excluded.kind, fmt=excluded.fmt, source=excluded.source, "
                "version=excluded.version, path=excluded.path, updated_at=excluded.updated_at",
                (document_id, title, kind, fmt, encoded, version, path, created, moment),
            )
            self._connection.commit()
        return Document(
            document_id, title, kind, fmt, source, version, path, created, moment
        )

    def get(self, document_id: str) -> Document | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
        return _row(row) if row else None

    def find_by_title(self, title: str) -> Document | None:
        """Case-insensitive title lookup, so the owner can say the name."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM documents WHERE lower(title) = lower(?) "
                "ORDER BY updated_at DESC LIMIT 1",
                (title.strip(),),
            ).fetchone()
        return _row(row) if row else None

    def recent(self, limit: int = MAX_VERSIONS_LISTED) -> list[Document]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM documents ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, MAX_VERSIONS_LISTED)),),
            ).fetchall()
        return [_row(row) for row in rows]

    def delete(self, document_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM documents WHERE document_id = ?", (document_id,)
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()
