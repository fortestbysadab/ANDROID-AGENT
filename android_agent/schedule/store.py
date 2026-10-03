"""Durable store for scheduled tasks.

Separate from both the session store and the chat archive on purpose. Sessions
expire for privacy and the archive holds rendered transcripts; a schedule has
to survive both, and must survive the process and the phone rebooting, because
a task the owner created last week has to still fire next Tuesday.

Times are stored as UTC epoch seconds. Daily tasks are computed against the
device's *local* timezone via `time.localtime`/`time.mktime`, because "every
morning at seven" means seven o'clock where the phone is, and Android keeps
that correct across travel and DST without us shipping a timezone database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Android's JobScheduler will not run a periodic job more often than every
#: 15 minutes, so an interval below this can only be honoured while the bot
#: process happens to be alive. Allowed, but the owner is told.
JOB_SCHEDULER_FLOOR_SECONDS = 900
#: A hard floor to stop a typo creating a hot loop against the device.
MIN_INTERVAL_SECONDS = 60
#: Keeps one runaway task from filling the phone's storage with history.
DEFAULT_MAX_TASKS = 50
#: How long a finished one-off task stays listed after it runs. Long enough
#: that the owner can look back at what happened overnight, short enough that
#: the schedule does not become a graveyard. A repeating task is never
#: purged: it is still part of the schedule precisely because it repeats.
COMPLETED_RETENTION_SECONDS = 24 * 3600
#: How long a claimed task is held before another ticker may retry it. Long
#: enough for the slowest tool to finish, short enough that a process killed
#: mid-run does not lose the task for good.
CLAIM_LEASE_SECONDS = 300

ONCE = "once"
INTERVAL = "interval"
DAILY = "daily"
SCHEDULE_KINDS = (ONCE, INTERVAL, DAILY)

TOOL = "tool"
PROMPT = "prompt"
TASK_KINDS = (TOOL, PROMPT)


class ScheduleError(ValueError):
    """Raised for a task definition that could never work."""


def new_task_id() -> str:
    return uuid.uuid4().hex[:8]


def next_daily_run(hour: int, minute: int, *, now: float | None = None) -> float:
    """Next occurrence of a local wall-clock time, strictly in the future."""
    moment = time.time() if now is None else now
    parts = time.localtime(moment)
    candidate = time.mktime(
        (parts.tm_year, parts.tm_mon, parts.tm_mday, hour, minute, 0, 0, 0, -1)
    )
    if candidate <= moment:
        # tm_mday + 1 is deliberate: mktime normalises month and year ends.
        candidate = time.mktime(
            (parts.tm_year, parts.tm_mon, parts.tm_mday + 1, hour, minute, 0, 0, 0, -1)
        )
    return candidate


@dataclass(frozen=True)
class ScheduledTask:
    task_id: str
    description: str
    task_kind: str
    #: Tool name for a tool task, or None for a prompt task.
    tool_name: str | None
    arguments: dict[str, Any]
    prompt: str | None
    schedule_kind: str
    #: Seconds for INTERVAL, "HH:MM" for DAILY, unused for ONCE.
    interval_seconds: int | None
    daily_time: str | None
    next_run_at: float
    enabled: bool
    created_at: float
    last_run_at: float | None = None
    last_status: str | None = None
    run_count: int = 0
    #: Set when the owner authorised a risky action at creation time. The
    #: runner refuses to execute a risky tool whose arguments no longer hash
    #: to this value, so an approved task cannot be edited into a different one.
    approved_hash: str | None = None
    condition: dict[str, Any] = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        """A one-off that has already run. Distinct from a paused task."""
        return self.schedule_kind == ONCE and not self.enabled and self.run_count > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "description": self.description,
            "kind": self.task_kind,
            "tool": self.tool_name,
            "arguments": dict(self.arguments),
            "prompt": self.prompt,
            "schedule": self.schedule_text(),
            "next_run_at": self.next_run_at,
            "next_run_local": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.next_run_at)),
            "enabled": self.enabled,
            "completed": self.is_completed,
            "last_run_at": self.last_run_at,
            "run_count": self.run_count,
            "last_status": self.last_status,
            "condition": dict(self.condition),
        }

    def schedule_text(self) -> str:
        if self.schedule_kind == DAILY:
            return f"every day at {self.daily_time}"
        if self.schedule_kind == INTERVAL:
            minutes = (self.interval_seconds or 0) / 60
            return f"every {minutes:g} minutes"
        return "once, at " + time.strftime("%Y-%m-%d %H:%M", time.localtime(self.next_run_at))


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
    return ScheduledTask(
        task_id=row["task_id"],
        description=row["description"],
        task_kind=row["task_kind"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments"] or "{}"),
        prompt=row["prompt"],
        schedule_kind=row["schedule_kind"],
        interval_seconds=row["interval_seconds"],
        daily_time=row["daily_time"],
        next_run_at=row["next_run_at"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        last_run_at=row["last_run_at"],
        last_status=row["last_status"],
        run_count=row["run_count"],
        approved_hash=row["approved_hash"],
        condition=json.loads(row["condition"] or "{}"),
    )


class ScheduleStore:
    """Thread-safe SQLite store of scheduled tasks."""

    def __init__(self, path: str | Path, *, max_tasks: int = DEFAULT_MAX_TASKS) -> None:
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")
        self.path = str(path)
        self.max_tasks = max_tasks
        self._lock = threading.Lock()
        # isolation_level=None puts sqlite3 in autocommit mode so transactions
        # are explicit. claim_due needs a real write lock held across its read,
        # and the implicit transaction handling will not give it one.
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        # Wait rather than fail when the other ticker holds the write lock.
        self._connection.execute("PRAGMA busy_timeout=10000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                task_kind TEXT NOT NULL,
                tool_name TEXT,
                arguments TEXT NOT NULL DEFAULT '{}',
                prompt TEXT,
                schedule_kind TEXT NOT NULL,
                interval_seconds INTEGER,
                daily_time TEXT,
                next_run_at REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                last_run_at REAL,
                last_status TEXT,
                run_count INTEGER NOT NULL DEFAULT 0,
                approved_hash TEXT,
                condition TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS tasks_due ON tasks (enabled, next_run_at)"
        )
        self._connection.commit()

    def add(self, task: ScheduledTask) -> ScheduledTask:
        with self._lock:
            count = self._connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            if count >= self.max_tasks:
                raise ScheduleError(
                    f"You already have {count} scheduled tasks, which is the limit. "
                    "Cancel one before adding another."
                )
            self._connection.execute(
                """
                INSERT INTO tasks (
                    task_id, description, task_kind, tool_name, arguments, prompt,
                    schedule_kind, interval_seconds, daily_time, next_run_at, enabled,
                    created_at, last_run_at, last_status, run_count, approved_hash, condition
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task.task_id, task.description, task.task_kind, task.tool_name,
                    json.dumps(task.arguments, sort_keys=True), task.prompt,
                    task.schedule_kind, task.interval_seconds, task.daily_time,
                    task.next_run_at, int(task.enabled), task.created_at,
                    task.last_run_at, task.last_status, task.run_count,
                    task.approved_hash, json.dumps(task.condition, sort_keys=True),
                ),
            )
            self._connection.commit()
        return task

    def get(self, task_id: str) -> ScheduledTask | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def all_tasks(self) -> list[ScheduledTask]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tasks ORDER BY enabled DESC, next_run_at ASC"
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def due(self, *, now: float | None = None) -> list[ScheduledTask]:
        moment = time.time() if now is None else now
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tasks WHERE enabled = 1 AND next_run_at <= ? "
                "ORDER BY next_run_at ASC",
                (moment,),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def claim_due(
        self, *, now: float | None = None, lease_seconds: float = CLAIM_LEASE_SECONDS
    ) -> list[ScheduledTask]:
        """Take ownership of everything due, so no one else runs it too.

        Two tickers can be alive at once: the loop inside a running agent and
        a persisted Android job that fires when the agent is dead but has not
        been noticed yet. Selecting due tasks and running them in two steps
        would let both pick the same task and send the owner two messages, or
        two SMS. Claiming pushes the next run forward by a lease in the same
        transaction as the read, so the second ticker sees nothing due.

        If the claiming process dies mid-run the lease expires and the task is
        retried, which is the right failure direction for a reminder.
        """
        moment = time.time() if now is None else now
        with self._lock:
            # BEGIN IMMEDIATE takes the write lock before the read. Without it
            # two processes can both SELECT the same due row and both UPDATE
            # it, and the owner gets two of everything. The in-process lock
            # above cannot help: the other ticker is a different process.
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT * FROM tasks WHERE enabled = 1 AND next_run_at <= ? "
                    "ORDER BY next_run_at ASC",
                    (moment,),
                ).fetchall()
                claimed = [_row_to_task(row) for row in rows]
                if claimed:
                    self._connection.executemany(
                        "UPDATE tasks SET next_run_at = ? WHERE task_id = ?",
                        [(moment + lease_seconds, task.task_id) for task in claimed],
                    )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return claimed

    def record_run(
        self,
        task_id: str,
        status: str,
        *,
        now: float | None = None,
        scheduled_for: float | None = None,
    ) -> None:
        """Mark a run and move the task to its next slot, or retire it.

        `scheduled_for` is the time the task was *due*, which the caller knows
        and the row no longer does once the task has been claimed. Interval
        schedules are advanced from it so a task keeps its phase instead of
        drifting by however long each run took.
        """
        moment = time.time() if now is None else now
        task = self.get(task_id)
        if task is None:
            return
        due_at = scheduled_for if scheduled_for is not None else task.next_run_at
        if task.schedule_kind == ONCE:
            next_run, enabled = due_at, False
        elif task.schedule_kind == DAILY:
            hour, minute = (int(part) for part in (task.daily_time or "0:0").split(":"))
            next_run, enabled = next_daily_run(hour, minute, now=moment), True
        else:
            interval = task.interval_seconds or JOB_SCHEDULER_FLOOR_SECONDS
            # Skip forward past any slots missed while the phone was off, so a
            # task dormant for a day does not fire a hundred times at once.
            elapsed = max(0.0, moment - due_at)
            skipped = int(elapsed // interval) + 1
            next_run, enabled = due_at + skipped * interval, True

        with self._lock:
            self._connection.execute(
                "UPDATE tasks SET last_run_at = ?, last_status = ?, run_count = run_count + 1, "
                "next_run_at = ?, enabled = ? WHERE task_id = ?",
                (moment, status, next_run, int(enabled), task_id),
            )
            self._connection.commit()

    def purge_completed(self, *, now: float | None = None) -> int:
        """Forget one-off tasks that finished more than a day ago.

        Only one-offs: a repeating task that the owner paused is still
        something they chose to keep, and deleting it would be data loss.
        """
        cutoff = (time.time() if now is None else now) - COMPLETED_RETENTION_SECONDS
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM tasks WHERE schedule_kind = ? AND enabled = 0 "
                "AND run_count > 0 AND last_run_at IS NOT NULL AND last_run_at < ?",
                (ONCE, cutoff),
            )
            self._connection.commit()
        return cursor.rowcount

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE tasks SET enabled = ? WHERE task_id = ?", (int(enabled), task_id)
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def delete(self, task_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM tasks WHERE task_id = ?", (task_id,)
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def clear(self) -> int:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM tasks")
            self._connection.commit()
        return cursor.rowcount

    def __iter__(self) -> Iterator[ScheduledTask]:
        return iter(self.all_tasks())

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def build_task(
    *,
    description: str,
    task_kind: str,
    schedule_kind: str,
    tool_name: str | None = None,
    arguments: Mapping[str, Any] | None = None,
    prompt: str | None = None,
    run_at: float | None = None,
    interval_seconds: int | None = None,
    daily_time: str | None = None,
    approved_hash: str | None = None,
    condition: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> ScheduledTask:
    """Validate a task definition and compute its first run time."""
    moment = time.time() if now is None else now
    if task_kind not in TASK_KINDS:
        raise ScheduleError(f"Unknown task kind {task_kind!r}.")
    if schedule_kind not in SCHEDULE_KINDS:
        raise ScheduleError(f"Unknown schedule kind {schedule_kind!r}.")
    if task_kind == TOOL and not tool_name:
        raise ScheduleError("A tool task needs a tool name.")
    if task_kind == PROMPT and not (prompt or "").strip():
        raise ScheduleError("A prompt task needs an instruction.")

    if schedule_kind == ONCE:
        if run_at is None:
            raise ScheduleError("A one-off task needs a time to run.")
        if run_at <= moment:
            raise ScheduleError("That time is already in the past.")
        next_run = float(run_at)
    elif schedule_kind == INTERVAL:
        if not interval_seconds:
            raise ScheduleError("A repeating task needs an interval.")
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleError(
                f"The shortest interval is {MIN_INTERVAL_SECONDS // 60} minute(s)."
            )
        next_run = moment + interval_seconds
    else:
        hour, minute = _parse_daily_time(daily_time)
        daily_time = f"{hour:02d}:{minute:02d}"
        next_run = next_daily_run(hour, minute, now=moment)

    return ScheduledTask(
        task_id=new_task_id(),
        description=description.strip() or "Scheduled task",
        task_kind=task_kind,
        tool_name=tool_name,
        arguments=dict(arguments or {}),
        prompt=prompt,
        schedule_kind=schedule_kind,
        interval_seconds=interval_seconds,
        daily_time=daily_time,
        next_run_at=next_run,
        enabled=True,
        created_at=moment,
        approved_hash=approved_hash,
        condition=dict(condition or {}),
    )


def _parse_daily_time(value: str | None) -> tuple[int, int]:
    if not value or ":" not in value:
        raise ScheduleError("A daily task needs a time like 07:30.")
    hour_text, _, minute_text = value.partition(":")
    try:
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise ScheduleError("A daily task needs a time like 07:30.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError("That is not a real time of day.")
    return hour, minute
