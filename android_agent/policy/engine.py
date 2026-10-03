"""Deterministic authorization; model text never grants permission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

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


class UnattendedPolicy:
    """Policy for a run with nobody watching.

    Every approval gate in this project assumes a human is present to answer
    it. A scheduled task removes that human, so an action that would normally
    pause for confirmation has no one to confirm it. Rather than silently
    auto-approving, this converts REQUIRE_APPROVAL into DENY and says why -
    the owner is told what the task wanted to do and can either pre-authorise
    that exact action or run it themselves.

    Anything the inner policy allows outright is still allowed: reading the
    battery or toggling the torch on a schedule needs no supervision.
    """

    def __init__(self, inner: Policy) -> None:
        self.inner = inner

    def evaluate(
        self,
        context: ToolContext,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
    ) -> PolicyResult:
        result = self.inner.evaluate(context, tool, arguments)
        if result.decision is PolicyDecision.REQUIRE_APPROVAL:
            return PolicyResult(
                PolicyDecision.DENY,
                "deny-unattended-" + result.rule_id,
                (
                    f"{tool.name} needs the owner's confirmation and this is an "
                    "unattended scheduled run. Schedule this exact action with "
                    "its arguments if it should happen automatically."
                ),
            )
        return result
