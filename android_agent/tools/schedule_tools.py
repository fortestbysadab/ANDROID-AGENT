"""Tools for managing scheduled tasks in plain language.

Four tools, deliberately split by what they risk:

* ``schedule_task`` creates a task. Creating one is reversible - it can be
  listed and cancelled - so it does not itself require approval, even when the
  action it schedules would. The runner refuses to perform a risky action
  unattended until it has been authorised, so a task created in error can
  never act on its own.
* ``authorize_scheduled_task`` freezes that authorisation. It is an
  EXTERNAL_SIDE_EFFECT, so the owner's existing approve/deny prompt gates it,
  and the arguments shown in that prompt are the ones hashed into the task.
  Approve once; it then runs unattended forever.
* ``list_scheduled_tasks`` and ``cancel_scheduled_task`` are the management
  surface that makes "what's my schedule?" and "cancel the morning one" work.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from android_agent.schedule.store import (
    DAILY,
    INTERVAL,
    JOB_SCHEDULER_FLOOR_SECONDS,
    ONCE,
    PROMPT,
    TOOL,
    ScheduleError,
    ScheduleStore,
    at_local_time,
    build_task,
)

from .base import Risk, ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry

SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string", "minLength": 1, "maxLength": 120,
            "description": "Short human label, e.g. 'morning battery check'.",
        },
        "schedule": {
            "type": "string", "enum": [ONCE, DAILY, INTERVAL],
            "description": "'once' for a one-off, 'daily' for every day at a "
                           "time, 'interval' to repeat every N minutes.",
        },
        "daily_time": {
            "type": "string", "pattern": r"^[0-9]{1,2}:[0-9]{2}$",
            "description": "Local time for a daily task, e.g. '07:30'.",
        },
        "at_time": {
            "type": "string", "pattern": r"^[0-9]{1,2}:[0-9]{2}$",
            "description": "For 'once': the clock time to run it, 24-hour, in "
                           "the device's own timezone, e.g. '23:10' for 11:10 "
                           "PM. Prefer this whenever the owner names a time. "
                           "Today if that time is still ahead, otherwise "
                           "tomorrow unless tomorrow is set.",
        },
        "tomorrow": {
            "type": "boolean",
            "description": "With at_time, force the next day rather than today.",
        },
        "in_minutes": {
            "type": "integer", "minimum": 1, "maximum": 10080,
            "description": "For 'once': minutes from now. Use only when the "
                           "owner says a duration ('in 20 minutes'), never to "
                           "convert a clock time.",
        },
        "every_minutes": {
            "type": "integer", "minimum": 1, "maximum": 10080,
            "description": "For 'interval': minutes between runs. Below 15 is "
                           "only honoured while the agent is running.",
        },
        "prompt": {
            "type": "string", "maxLength": 500,
            "description": "An instruction to carry out, for anything needing "
                           "judgement. Cannot use tools that need confirmation.",
        },
        "tool_name": {
            "type": "string", "maxLength": 64,
            "description": "Exact tool to run instead of a prompt. Use this "
                           "for a precise repeatable action.",
        },
        "tool_arguments": {
            "type": "object",
            "additionalProperties": True,
            "description": "Arguments for tool_name, matching that tool's own "
                           "schema. Validated against it before the task runs.",
        },
    },
    "required": ["description", "schedule"],
    "additionalProperties": False,
}

TASK_ID_SCHEMA = {
    "type": "object",
    "properties": {"task_id": {"type": "string", "minLength": 1, "maxLength": 32}},
    "required": ["task_id"],
    "additionalProperties": False,
}

NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def _parse_clock(value: str) -> tuple[int, int]:
    hour_text, _, minute_text = str(value).partition(":")
    try:
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise ScheduleError("Give the time as HH:MM, e.g. 23:10.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError("That is not a real time of day.")
    return hour, minute


def _ago(moment: float | None) -> str:
    if not moment:
        return "recently"
    seconds = max(0.0, time.time() - moment)
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    return f"{int(seconds // 3600)}h ago"


def _describe(task) -> str:
    if task.is_completed:
        state = f" - done {_ago(task.last_run_at)}"
    elif not task.enabled:
        state = " (paused)"
    else:
        state = ""
    return f"[{task.task_id}] {task.description} - {task.schedule_text()}{state}"


def schedule_tools(
    store: ScheduleStore, registry: ToolRegistry, *, needs_authorisation
) -> list[ToolSpec]:
    """Build the scheduling tools.

    `needs_authorisation(tool_name, arguments)` returns the approval hash when
    the action may run unattended as-is, or None when the owner must authorise
    it first. Injected rather than imported so the policy stays in one place.
    """

    def create(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        del context
        schedule_kind = str(arguments["schedule"])
        tool_name = arguments.get("tool_name")
        prompt = arguments.get("prompt")

        if tool_name and prompt:
            return ToolResult.error(
                "Give either a tool to run or an instruction, not both.",
                code="invalid_arguments",
            )
        if not tool_name and not prompt:
            return ToolResult.error(
                "Say what the task should do: either a tool_name or a prompt.",
                code="invalid_arguments",
            )
        if tool_name and registry.get(tool_name) is None:
            return ToolResult.error(
                f"There is no tool called {tool_name}.", code="unknown_tool"
            )

        run_at = None
        if schedule_kind == ONCE:
            at_time = arguments.get("at_time")
            minutes = arguments.get("in_minutes")
            if at_time and minutes:
                return ToolResult.error(
                    "Give either a clock time or a number of minutes, not both.",
                    code="invalid_arguments",
                )
            if at_time:
                try:
                    hour, minute = _parse_clock(at_time)
                except ScheduleError as exc:
                    return ToolResult.error(str(exc), code="invalid_schedule")
                run_at = at_local_time(hour, minute)
                # A time already gone means they meant tomorrow, which is also
                # what "tomorrow at 7" asks for explicitly.
                if arguments.get("tomorrow") or run_at <= time.time():
                    run_at = at_local_time(hour, minute, day_offset=1)
            elif minutes:
                run_at = time.time() + minutes * 60
            else:
                return ToolResult.error(
                    "For a one-off task give at_time (a clock time) or "
                    "in_minutes (a duration).",
                    code="invalid_arguments",
                )
        every_minutes = arguments.get("every_minutes")

        try:
            task = build_task(
                description=str(arguments["description"]),
                task_kind=TOOL if tool_name else PROMPT,
                schedule_kind=schedule_kind,
                tool_name=tool_name,
                arguments=arguments.get("tool_arguments") or {},
                prompt=prompt,
                run_at=run_at,
                interval_seconds=every_minutes * 60 if every_minutes else None,
                daily_time=arguments.get("daily_time"),
            )
        except ScheduleError as exc:
            return ToolResult.error(str(exc), code="invalid_schedule")

        authorised = None
        if tool_name:
            authorised = needs_authorisation(tool_name, task.arguments)
        task = type(task)(**{**task.__dict__, "approved_hash": authorised})

        try:
            store.add(task)
        except ScheduleError as exc:
            return ToolResult.error(str(exc), code="schedule_full")

        data = task.as_dict()
        summary = f"Scheduled: {_describe(task)}. First run {data['next_run_local']}."
        if tool_name and authorised is None:
            summary += (
                f" This action needs your confirmation once before it can run "
                f"unattended - authorise task {task.task_id} and it will run "
                "automatically from then on."
            )
        if every_minutes and every_minutes * 60 < JOB_SCHEDULER_FLOOR_SECONDS:
            summary += (
                f" Note: Android will not wake the phone more often than every "
                f"{JOB_SCHEDULER_FLOOR_SECONDS // 60} minutes, so a "
                f"{every_minutes}-minute interval only holds while the agent is "
                "running."
            )
        data["needs_authorisation"] = tool_name is not None and authorised is None
        return ToolResult.ok(summary, data)

    def authorize(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        del context
        task_id = str(arguments["task_id"])
        task = store.get(task_id)
        if task is None:
            return ToolResult.error(f"There is no task {task_id}.", code="unknown_task")
        if task.task_kind != TOOL:
            return ToolResult.error(
                "Only a fixed tool task can be authorised; an instruction task "
                "chooses its own actions and is limited to ones that need no "
                "confirmation.",
                code="invalid_task",
            )
        approved = needs_authorisation(task.tool_name or "", task.arguments, force=True)
        if approved is None:
            return ToolResult.error(
                "That task's tool or arguments are no longer valid.", code="invalid_task"
            )
        store.delete(task_id)
        store.add(type(task)(**{**task.__dict__, "approved_hash": approved}))
        return ToolResult.ok(
            f"Authorised: {_describe(task)}. It will now run unattended.",
            {"id": task_id},
        )

    def listing(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        del context, arguments
        # Drop finished one-offs older than a day before showing the list, so
        # the schedule reflects what is actually still going to happen even if
        # no tick has run since.
        store.purge_completed()
        tasks = store.all_tasks()
        if not tasks:
            return ToolResult.ok("There are no scheduled tasks.", {"tasks": []})
        lines = "\n".join(
            _describe(task)
            if task.is_completed
            else f"{_describe(task)}, next {task.as_dict()['next_run_local']}"
            for task in tasks
        )
        upcoming = sum(1 for task in tasks if task.enabled)
        done = sum(1 for task in tasks if task.is_completed)
        headline = f"{upcoming} scheduled task(s)"
        if done:
            headline += f", plus {done} finished in the last 24 hours"
        return ToolResult.ok(
            f"{headline}:\n{lines}",
            {"tasks": [task.as_dict() for task in tasks]},
        )

    def cancel(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        del context
        task_id = str(arguments["task_id"])
        task = store.get(task_id)
        if task is None:
            return ToolResult.error(
                f"There is no task {task_id}. List the tasks to see the current ids.",
                code="unknown_task",
            )
        store.delete(task_id)
        return ToolResult.ok(f"Cancelled: {_describe(task)}.", {"id": task_id})

    return [
        ToolSpec(
            "schedule_task",
            "Create a scheduled task: a one-off, a daily time, or a repeating "
            "interval. Give either tool_name with tool_arguments for a precise "
            "repeatable action, or prompt for something needing judgement. Use "
            "when the owner asks for something to happen later or regularly. "
            "If the reply says the task needs confirmation, tell the owner and "
            "offer to authorise it.",
            SCHEDULE_SCHEMA,
            Risk.REVERSIBLE,
            create,
        ),
        ToolSpec(
            "authorize_scheduled_task",
            "Authorise one scheduled task to perform an action that would "
            "otherwise need confirmation each time, such as sending a message. "
            "Use only when the owner explicitly agrees to it running unattended. "
            "The exact tool and arguments are frozen, so the task cannot later "
            "act differently.",
            TASK_ID_SCHEMA,
            Risk.EXTERNAL_SIDE_EFFECT,
            authorize,
        ),
        ToolSpec(
            "list_scheduled_tasks",
            "List scheduled tasks with their ids, timing and next run. Use when "
            "the owner asks what is scheduled, or before cancelling so the right "
            "id is used.",
            NO_ARGS,
            Risk.READ_ONLY,
            listing,
            idempotent=True,
        ),
        ToolSpec(
            "cancel_scheduled_task",
            "Cancel one scheduled task by id. List the tasks first if the owner "
            "described it in words rather than giving an id.",
            TASK_ID_SCHEMA,
            Risk.REVERSIBLE,
            cancel,
        ),
    ]
