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
    #: Opaque provider metadata that must be replayed verbatim on later turns.
    #: Gemini 3 thinking models put a required `thought_signature` here. It is
    #: never interpreted, never shown to the user, and never authorizes
    #: anything; policy decisions ignore it entirely.
    extra_content: Mapping[str, Any] = field(default_factory=dict)


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
