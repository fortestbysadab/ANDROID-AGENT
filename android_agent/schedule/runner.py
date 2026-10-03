"""Executes scheduled tasks and reports what happened.

Design constraints that shaped this:

* **Nobody is watching.** A scheduled run cannot answer an approval prompt, so
  anything that would normally pause for confirmation is refused unless the
  owner pre-authorised that exact tool and arguments when creating the task.
  The authorisation is bound to a hash, so an approved task cannot later be
  edited into a different action.
* **The phone is hostile to background work.** Runs are best-effort and may
  arrive late; the store skips missed slots rather than replaying them.
* **A failed task must be visible.** Silence is indistinguishable from "it
  never ran", which is the worst outcome for something the owner is relying
  on, so every run reports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from android_agent.agent.runtime import (
    AgentRuntime,
    PendingApproval,
    RunStatus,
    _argument_hash,
)
from android_agent.models.base import ToolCall
from android_agent.observability.audit import AuditSink, NullAuditSink
from android_agent.policy.engine import PolicyDecision
from android_agent.schedule.store import PROMPT, TOOL, ScheduledTask, ScheduleStore
from android_agent.tools.base import SchemaValidationError, ToolContext, ToolResult

logger = logging.getLogger(__name__)

#: Deliver a finished run to the owner. Receives the whole run, not just its
#: text, so a caller can also send any artifact the tool produced - a
#: scheduled screenshot is useless as a sentence.
Reporter = Callable[["TaskRun"], None]


@dataclass(frozen=True)
class TaskRun:
    task_id: str
    description: str
    status: str
    message: str
    #: The tool result, when the task was a fixed tool call. Carries
    #: artifact_path and coordinates for callers that can deliver them.
    result: ToolResult | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def approval_hash_for(registry, tool_name: str, arguments: Mapping[str, Any]):
    """Hash identifying one exact action, or None if the tool is unknown.

    Returned at creation time so the owner's authorisation can be frozen
    against the precise arguments they saw. Takes a registry rather than a
    runtime because the catalogue is built before the runtime exists.
    """
    tool = registry.get(tool_name)
    if tool is None:
        return None
    try:
        validated = tool.validate(arguments)
    except SchemaValidationError:
        return None
    return _argument_hash(tool.name, tool.version, validated)


class ScheduleRunner:
    """Finds due tasks, runs them, and reports the outcome."""

    def __init__(
        self,
        *,
        store: ScheduleStore,
        runtime: AgentRuntime,
        unattended_runtime: AgentRuntime,
        owner_id: str,
        chat_id: int,
        reporter: Reporter,
        audit: AuditSink | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        #: Same planner and tools, but a policy that refuses anything needing
        #: confirmation. Used for natural-language tasks, where the model
        #: chooses the actions at run time and cannot be pre-authorised.
        self.unattended_runtime = unattended_runtime
        self.owner_id = str(owner_id)
        self.chat_id = int(chat_id)
        self.reporter = reporter
        self.audit = audit or NullAuditSink()

    def tick(self, *, now: float | None = None) -> list[TaskRun]:
        """Run everything that is due. Never raises."""
        runs: list[TaskRun] = []
        for task in self.store.due(now=now):
            try:
                run = self.run_task(task)
            except Exception as exc:  # a scheduler that dies is worse than a bad task
                logger.exception("Scheduled task %s crashed", task.task_id)
                run = TaskRun(
                    task.task_id, task.description, "error",
                    f"The scheduled task '{task.description}' crashed: {type(exc).__name__}.",
                )
            self.store.record_run(task.task_id, run.status, now=now)
            self.audit.emit(
                "schedule.ran",
                {"task_id": task.task_id, "status": run.status, "kind": task.task_kind},
            )
            self._report(run)
            runs.append(run)
        return runs

    def run_task(self, task: ScheduledTask) -> TaskRun:
        if task.task_kind == TOOL:
            return self._run_tool_task(task)
        if task.task_kind == PROMPT:
            return self._run_prompt_task(task)
        return TaskRun(
            task.task_id, task.description, "error",
            f"'{task.description}' has an unknown task type and did not run.",
        )

    def _run_tool_task(self, task: ScheduledTask) -> TaskRun:
        tool = self.runtime.registry.get(task.tool_name or "")
        if tool is None:
            return TaskRun(
                task.task_id, task.description, "error",
                f"'{task.description}' refers to a tool that no longer exists "
                f"({task.tool_name}). Cancel or recreate it.",
            )
        try:
            arguments = tool.validate(task.arguments)
        except SchemaValidationError as exc:
            return TaskRun(
                task.task_id, task.description, "error",
                f"'{task.description}' has arguments this tool no longer accepts: {exc}",
            )

        context = ToolContext(self.owner_id, self.chat_id, "schedule", direct_user_request=False)
        decision = self.runtime.policy.evaluate(context, tool, arguments)
        expected = _argument_hash(tool.name, tool.version, arguments)

        if decision.decision is PolicyDecision.DENY:
            return TaskRun(
                task.task_id, task.description, "blocked",
                f"'{task.description}' was blocked: {decision.reason}",
            )
        if decision.decision is PolicyDecision.REQUIRE_APPROVAL and task.approved_hash != expected:
            # Either never authorised, or authorised for different arguments.
            return TaskRun(
                task.task_id, task.description, "blocked",
                f"'{task.description}' needs your confirmation and nobody is here to give "
                "it. Recreate the task and approve it once, and it will run unattended "
                "from then on.",
            )

        pending = PendingApproval(
            call=ToolCall(f"sched-{task.task_id}", tool.name, dict(arguments)),
            tool_version=tool.version,
            reason="scheduled task",
            argument_hash=expected,
        )
        result = self.runtime.execute_approved(
            pending, actor_id=self.owner_id, chat_id=self.chat_id
        )
        status = "ok" if result.status == "ok" else "error"
        return TaskRun(
            task.task_id, task.description, status,
            f"{task.description}: {result.summary}", result,
        )

    def _run_prompt_task(self, task: ScheduledTask) -> TaskRun:
        outcome = self.unattended_runtime.run(
            task.prompt or "", actor_id=self.owner_id, chat_id=self.chat_id
        )
        # Defence in depth. UnattendedPolicy should already have turned every
        # approval into a denial, so reaching here means the policy was
        # misconfigured. Leaving a pending approval from an unattended run is
        # the dangerous outcome: the owner would later be asked to confirm an
        # action with no memory of what asked for it.
        if outcome.status is RunStatus.APPROVAL_REQUIRED or outcome.pending_approvals:
            wanted = ", ".join(
                pending.call.name for pending in outcome.pending_approvals
            ) or "an action"
            logger.error(
                "Unattended task %s produced a pending approval for %s; refusing",
                task.task_id, wanted,
            )
            return TaskRun(
                task.task_id, task.description, "blocked",
                f"'{task.description}' wanted to use {wanted}, which needs your "
                "confirmation. It was not run. Schedule that exact action instead "
                "if it should happen automatically.",
            )
        text = (outcome.text or "").strip() or "(the assistant returned nothing)"
        status = "ok" if outcome.status is RunStatus.COMPLETED else "error"
        return TaskRun(task.task_id, task.description, status, f"{task.description}: {text}")

    def _report(self, run: TaskRun) -> None:
        try:
            self.reporter(run)
        except Exception:
            # Delivery is best effort: Telegram being unreachable must not
            # stop the next task from running or corrupt the schedule.
            logger.exception("Could not deliver scheduled result for %s", run.task_id)
