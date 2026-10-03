"""Bounded plan/action/observation runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import Enum
from typing import Any

from android_agent.models.base import Planner, PlannerResponse, ToolCall
from android_agent.observability.audit import AuditSink, NullAuditSink
from android_agent.policy.engine import Policy, PolicyDecision
from android_agent.skills.loader import SkillRouter
from android_agent.tools.base import SchemaValidationError, ToolContext, ToolResult
from android_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


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
    error: str | None = None


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
        skill_router: SkillRouter | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.planner = planner
        self.registry = registry
        self.policy = policy
        self.system_prompt = system_prompt.strip()
        self.limits = limits or RuntimeLimits()
        self.audit = audit or NullAuditSink()
        self.skill_router = skill_router
        #: The model has no clock. Without one it cannot resolve "today",
        #: "tonight" or "in an hour", and will silently invent an offset -
        #: which is how a task asked for at 11:10 PM was scheduled for 07:59
        #: the next morning.
        self.clock = clock or _local_time_line

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
        skill_instructions = (
            self.skill_router.instructions_for(user_text) if self.skill_router is not None else ""
        )
        system_content = "\n\n".join(
            part
            for part in (self.system_prompt, self.clock(), skill_instructions)
            if part.strip()
        )
        if system_content:
            messages.append({"role": "system", "content": system_content})
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
                logger.exception("Planner call failed on run %s turn %d", run_id, turn + 1)
                self.audit.emit(
                    "run.failed",
                    {
                        "run_id": run_id,
                        "reason": "planner_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
                return RunOutcome(
                    run_id,
                    RunStatus.FAILED,
                    _planner_failure_text(exc),
                    tuple(results),
                    messages=tuple(messages),
                    error=f"{type(exc).__name__}: {exc}",
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
                if result is None:  # defensive: _process_call always returns one or the other
                    raise RuntimeError("tool processing returned neither result nor approval")
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

    def execute_approved(
        self,
        pending: PendingApproval,
        *,
        actor_id: str,
        chat_id: int,
        run_id: str | None = None,
    ) -> ToolResult:
        """Execute one exact, previously approved proposal.

        The caller owns approval identity, expiry, and one-time-use checks. This
        method revalidates tool version, arguments, and the frozen hash.
        """
        approval_run_id = run_id or uuid.uuid4().hex
        context = ToolContext(str(actor_id), chat_id, approval_run_id, direct_user_request=True)
        tool = self.registry.get(pending.call.name)
        if tool is None or tool.version != pending.tool_version:
            return ToolResult.error("The approved tool changed; request the action again.", code="approval_stale")
        try:
            arguments = tool.validate(pending.call.arguments)
        except SchemaValidationError:
            return ToolResult.error("The approved arguments are no longer valid.", code="approval_stale")
        expected = _argument_hash(tool.name, tool.version, arguments)
        if expected != pending.argument_hash:
            return ToolResult.error("Approval arguments did not match.", code="approval_mismatch")
        self.audit.emit("approval.executing", {"run_id": approval_run_id, "tool": tool.name})
        result = _execute_with_timeout(tool.handler, context, arguments, tool.timeout_seconds)
        self._record_outcome(approval_run_id, tool.name, result)
        return result

    def _record_outcome(self, run_id: str, tool_name: str, result: ToolResult) -> None:
        """Audit a tool outcome, including *why* it failed.

        The error code is recorded because "status: error" alone forces every
        later diagnosis to be guesswork from the outside. Codes are fixed
        identifiers chosen by the tool, never free text and never arguments,
        so this adds no new exposure of user data. The human-readable summary
        is deliberately still excluded: it can quote device content.
        """
        failed = result.status != "ok"
        event: dict[str, Any] = {"run_id": run_id, "tool": tool_name, "status": result.status}
        if failed:
            event["error_code"] = result.error_code or "unspecified"
            event["retryable"] = bool(result.retryable)
            logger.warning(
                "Tool %s failed on run %s: %s", tool_name, run_id, event["error_code"]
            )
        self.audit.emit("tool.failed" if failed else "tool.completed", event)

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
        self._record_outcome(context.run_id, tool.name, result)
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


def _local_time_line() -> str:
    """The device's current date and time, in its own timezone.

    Included in every run's system message. Phrased as a fact rather than an
    instruction so it reads naturally wherever the model needs it.
    """
    now = time.localtime()
    zone = time.strftime("%Z", now) or "local time"
    return (
        "Current device date and time: "
        f"{time.strftime('%A %d %B %Y, %H:%M', now)} ({zone}). "
        "Use this whenever the owner says today, tonight, tomorrow or a clock "
        "time; never assume a different timezone."
    )


def _planner_failure_text(exc: Exception) -> str:
    """Owner-facing explanation of a planner failure.

    The bot is single-owner, so a precise diagnostic is more useful than a
    vague apology. Secrets never reach this path: the planner keeps the API
    key in headers and out of exception text.
    """
    remedy = getattr(exc, "remedy", "")
    detail = str(exc).strip() or type(exc).__name__
    lines = ["I could not reach the model.", "", f"Reason: {detail[:600]}"]
    if remedy:
        lines += ["", f"Fix: {remedy}"]
    return "\n".join(lines)


def _assistant_message(response: PlannerResponse) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.text}
    if response.tool_calls:
        serialized = []
        for call in response.tool_calls:
            entry: dict[str, Any] = {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":"), sort_keys=True),
                },
            }
            # Replay opaque provider metadata verbatim. Gemini 3 requires its
            # thought_signature back or the next turn fails with HTTP 400.
            if call.extra_content:
                entry["extra_content"] = dict(call.extra_content)
            serialized.append(entry)
        message["tool_calls"] = serialized
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
