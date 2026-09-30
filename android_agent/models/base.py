"""Model-neutral planner contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    """A model proposal. It has no authority until policy allows it."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class PlannerResponse:
    """One planner turn containing either text, tool calls, or both."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


class Planner(Protocol):
    """Interface implemented by cloud, local, and test planners."""

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> PlannerResponse:
        """Return a final message and/or typed tool proposals."""
