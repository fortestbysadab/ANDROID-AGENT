"""Provider-neutral Android agent runtime.

This package is the clean v2 implementation.  The legacy ``bot.py`` entry point
remains untouched while capabilities are migrated behind typed tools.
"""

from .agent.runtime import AgentRuntime, RunOutcome, RunStatus
from .models.base import Planner, PlannerResponse, ToolCall
from .policy.engine import DefaultPolicy, PolicyDecision
from .tools.base import Risk, ToolContext, ToolResult, ToolSpec
from .tools.registry import ToolRegistry

__all__ = [
    "AgentRuntime",
    "DefaultPolicy",
    "Planner",
    "PlannerResponse",
    "PolicyDecision",
    "Risk",
    "RunOutcome",
    "RunStatus",
    "ToolCall",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
]
