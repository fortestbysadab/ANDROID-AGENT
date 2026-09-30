"""Small structured audit interface used by the runtime."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


class AuditSink(Protocol):
    def emit(self, event: str, fields: Mapping[str, Any]) -> None:
        ...


class NullAuditSink:
    def emit(self, event: str, fields: Mapping[str, Any]) -> None:
        pass


class JsonlAuditSink:
    """Append-only JSONL events. Callers must provide redacted fields."""

    def __init__(self, path: str) -> None:
        self.path = os.path.realpath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: str, fields: Mapping[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **dict(fields),
        }
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")


@dataclass
class MemoryAuditSink:
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: str, fields: Mapping[str, Any]) -> None:
        self.events.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **dict(fields),
            }
        )
