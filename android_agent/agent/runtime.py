"""Bounded plan/action/observation runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from android_agent.models.base import Planner, PlannerResponse, ToolCall
from android_agent.observability.audit import AuditSink, NullAuditSink
from android_agent.policy.engine import Policy, PolicyDecision, PolicyResult
from android_agent.tools.base import SchemaValidationError, ToolContext, ToolResult
from android_agent.tools.registry import ToolRegistry


class RunStatus(str, Enum):
    COMPLETED = "completed"
    APPROVAL_REQUIRED = "approval_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


@dataclass(frozen=True)
class PendingApproval:
    call: ToolCall
    tool_version: str
    reason: str
    argument_hash: str


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    text: str
    tool_results: tuple[ToolResult, ...] = ()
    pending_approvals: tuple[PendingApproval, ...] = ()
    messages: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RuntimeLimits:
    max_model_turns: int = 8
    max_tool_calls: int = 12
    max_same_call: int = 2


class AgentRuntime:
    def __init__(
        self,
        *,
        planner: Planner,
        registry: ToolRegistry,
        policy: Policy,
        system_prompt: str,
        limits: RuntimeLimits | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.planner = planner
        self.registry = registry
        self.policy = policy
        self.system_prompt = system_prompt.strip()
        self.limits = limits or RuntimeLimits()
        self.audit = audit or NullAuditSink()

    def run(
        self,
        user_text: str,
        *,
        actor_id: str,
        chat_id: int,
        prior_messages: Sequence[Mapping[str, Any]] = (),
    ) -> RunOutcome:
        run_id = uuid.uuid4().hex
        context = ToolContext(str(actor_id), chat_id, run_id, direct_user_request=True)
        messages: list[Mapping[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(prior_messages)
        messages.append({"role": "user", "content": user_text})

        results: list[ToolResult] = []
        call_counts: dict[str, int] = {}
        total_calls = 0
        self.audit.emit("run.started", {"run_id": run_id, "actor_id": str(actor_id)})

        for turn in range(self.limits.max_model_turns):
            self.audit.emit("model.requested", {"run_id": run_id, "turn": turn + 1})
            try:
                response = self.planner.complete(messages, self.registry.model_schemas())
            except Exception as exc:
                self.audit.emit(
                    "run.failed",
                    {"run_id": run_id, "reason": "planner_error", "error_type": type(exc).__name__},
                )
                return RunOutcome(
                    run_id,
                    RunStatus.FAILED,
                    "The agent brain is temporarily unavailable.",
                    tuple(results),
                    messages=tuple(messages),
                )

            self.audit.emit(
                "model.responded",
                {"run_id": run_id, "turn": turn + 1, "tool_calls": len(response.tool_calls)},
            )
            messages.append(_assistant_message(response))

            if not response.tool_calls:
                text = response.text or "Done."
                self.audit.emit("run.completed", {"run_id": run_id, "turns": turn + 1})
                return RunOutcome(
                    run_id,
                    RunStatus.COMPLETED,
                    text,
                    tuple(results),
                    messages=tuple(messages),
                )

            approvals: list[PendingApproval] = []
            for call in response.tool_calls:
                total_calls += 1
                if total_calls > self.limits.max_tool_calls:
                    return self._budget_outcome(run_id, results, messages, "tool call budget exhausted")

                signature = _call_signature(call)
                call_counts[signature] = call_counts.get(signature, 0) + 1
                if call_counts[signature] > self.limits.max_same_call:
                    return self._budget_outcome(run_id, results, messages, "repeated-call circuit breaker opened")

                result, approval = self._process_call(context, call)
                if approval is not None:
                    approvals.append(approval)
                    continue
                assert result is not None
                results.append(result)
                messages.append(_tool_message(call, result))

            if approvals:
                self.audit.emit(
                    "approval.requested",
                    {"run_id": run_id, "count": len(approvals)},
                )
                return RunOutcome(
                    run_id,
                    RunStatus.APPROVAL_REQUIRED,
                    "Approval is required before I can perform this action.",
                    tuple(results),
                    tuple(approvals),
                    tuple(messages),
                )

        return self._budget_outcome(run_id, results, messages, "model turn budget exhausted")

    def _process_call(
        self, context: ToolContext, call: ToolCall
    ) -> tuple[ToolResult | None, PendingApproval | None]:
        self.audit.emit(
            "tool.proposed",
            {"run_id": context.run_id, "call_id": call.id, "tool": call.name},
        )
        tool = self.registry.get(call.name)
        if tool is None:
            result = ToolResult.error(
                f"Unknown tool {call.name!r}; choose one of the declared tools.",
                code="unknown_tool",
            )
            self.audit.emit("tool.validation_failed", {"run_id": context.run_id, "code": "unknown_tool"})
            return result, None
        try:
            arguments = tool.validate(call.arguments)
        except SchemaValidationError as exc:
            result = ToolResult.error(str(exc), code="invalid_arguments")
            self.audit.emit(
                "tool.validation_failed",
                {"run_id": context.run_id, "tool": tool.name, "code": "invalid_arguments"},
            )
            return result, None

        policy = self.policy.evaluate(context, tool, arguments)
        self.audit.emit(
            f"policy.{policy.decision.value}",
            {"run_id": context.run_id, "tool": tool.name, "rule_id": policy.rule_id},
        )
        if policy.decision is PolicyDecision.DENY:
            return ToolResult(
                "denied",
                policy.reason,
                error_code="policy_denied",
            ), None
        if policy.decision is PolicyDecision.REQUIRE_APPROVAL:
            frozen_call = ToolCall(call.id, call.name, arguments)
            return None, PendingApproval(
                frozen_call,
                tool.version,
                policy.reason,
                _argument_hash(call.name, tool.version, arguments),
            )

        self.audit.emit("tool.started", {"run_id": context.run_id, "tool": tool.name})
        result = _execute_with_timeout(tool.handler, context, arguments, tool.timeout_seconds)
        self.audit.emit(
            "tool.completed" if result.status == "ok" else "tool.failed",
            {"run_id": context.run_id, "tool": tool.name, "status": result.status},
        )
        return result, None

    def _budget_outcome(
        self,
        run_id: str,
        results: list[ToolResult],
        messages: list[Mapping[str, Any]],
        reason: str,
    ) -> RunOutcome:
        self.audit.emit("run.budget_exhausted", {"run_id": run_id, "reason": reason})
        return RunOutcome(
            run_id,
            RunStatus.BUDGET_EXHAUSTED,
            f"I stopped safely because the {reason}.",
            tuple(results),
            messages=tuple(messages),
        )


def _assistant_message(response: PlannerResponse) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.text}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":"), sort_keys=True),
                },
            }
            for call in response.tool_calls
        ]
    return message


def _tool_message(call: ToolCall, result: ToolResult) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps(result.as_dict(), separators=(",", ":"), sort_keys=True),
    }


def _call_signature(call: ToolCall) -> str:
    encoded = json.dumps(
        {"name": call.name, "arguments": call.arguments},
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _argument_hash(name: str, version: str, arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"tool": name, "version": version, "arguments": arguments},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _execute_with_timeout(handler, context, arguments, timeout_seconds: float) -> ToolResult:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-tool")
    future = executor.submit(handler, context, arguments)
    try:
        result = future.result(timeout=timeout_seconds)
        if not isinstance(result, ToolResult):
            return ToolResult.error("Tool returned an invalid result.", code="invalid_tool_result")
        return result
    except FutureTimeout:
        future.cancel()
        return ToolResult.error("Tool timed out.", code="timeout", retryable=True)
    except Exception as exc:
        return ToolResult.error(
            f"Tool failed: {type(exc).__name__}", code="tool_exception", retryable=False
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
