"""One-time, expiring approval store for Telegram callbacks."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from android_agent.agent.runtime import PendingApproval


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    actor_id: str
    chat_id: int
    run_id: str
    pending: PendingApproval
    expires_at: float


class InMemoryApprovalStore:
    """Process-local first implementation; records are consumed atomically."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, ApprovalRecord] = {}
        self._lock = threading.Lock()

    def create(
        self, *, actor_id: str, chat_id: int, run_id: str, pending: PendingApproval
    ) -> ApprovalRecord:
        approval_id = secrets.token_urlsafe(12)
        record = ApprovalRecord(
            approval_id,
            str(actor_id),
            chat_id,
            run_id,
            pending,
            time.time() + self.ttl_seconds,
        )
        with self._lock:
            self._purge_locked()
            self._records[approval_id] = record
        return record

    def consume(self, approval_id: str, *, actor_id: str, chat_id: int) -> ApprovalRecord | None:
        with self._lock:
            self._purge_locked()
            record = self._records.get(approval_id)
            if record is None or record.actor_id != str(actor_id) or record.chat_id != chat_id:
                return None
            return self._records.pop(approval_id)

    def deny(self, approval_id: str, *, actor_id: str, chat_id: int) -> bool:
        return self.consume(approval_id, actor_id=actor_id, chat_id=chat_id) is not None

    def _purge_locked(self) -> None:
        now = time.time()
        for key in [key for key, value in self._records.items() if value.expires_at <= now]:
            self._records.pop(key, None)
