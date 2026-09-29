"""Deterministic authorization; model text never grants permission."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol

from android_agent.tools.base import Risk, ToolContext, ToolSpec


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    rule_id: str
    reason: str


class Policy(Protocol):
    def evaluate(
        self,
        context: ToolContext,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
    ) -> PolicyResult:
        ...


class DefaultPolicy:
    """Owner-autonomous profile selected for v2.

    Most bounded device actions are allowed. External communication requires
    approval and critical capabilities are denied. Unknown actors are denied
    by construction before risk is considered.
    """

    def __init__(self, owner_id: str) -> None:
        self.owner_id = str(owner_id)

    def evaluate(
        self,
        context: ToolContext,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
    ) -> PolicyResult:
        del arguments  # reserved for future argument-aware rules
        if str(context.actor_id) != self.owner_id:
            return PolicyResult(PolicyDecision.DENY, "deny-non-owner", "actor is not the owner")
        if tool.risk is Risk.CRITICAL:
            return PolicyResult(
                PolicyDecision.DENY,
                "deny-critical",
                "critical capabilities are unavailable to the model",
            )
        if tool.risk is Risk.EXTERNAL_SIDE_EFFECT:
            return PolicyResult(
                PolicyDecision.REQUIRE_APPROVAL,
                "approve-external-side-effect",
                "external communication requires exact user approval",
            )
        if tool.risk is Risk.SENSITIVE_READ and not context.direct_user_request:
            return PolicyResult(
                PolicyDecision.REQUIRE_APPROVAL,
                "approve-indirect-sensitive-read",
                "sensitive data was not directly requested",
            )
        return PolicyResult(PolicyDecision.ALLOW, "allow-owner-bounded", "bounded owner action")
