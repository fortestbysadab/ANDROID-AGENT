"""Tests for the scheduled-task runner.

The property these protect: a scheduled run has nobody present to answer an
approval prompt, so it must never perform an action that would normally ask
for one unless the owner froze that exact action in advance.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

from android_agent.agent.runtime import AgentRuntime
from android_agent.models.base import PlannerResponse, ToolCall
from android_agent.observability.audit import MemoryAuditSink
from android_agent.policy.engine import DefaultPolicy, UnattendedPolicy
from android_agent.schedule.runner import ScheduleRunner, approval_hash_for
from android_agent.schedule.store import (
    DAILY,
    INTERVAL,
    PROMPT,
    TOOL,
    ScheduleStore,
    build_task,
)
from android_agent.tools.base import Risk, ToolResult, ToolSpec
from android_agent.tools.registry import ToolRegistry

OWNER = "4242"
CHAT = 4242

NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
SMS_SCHEMA = {
    "type": "object",
    "properties": {"number": {"type": "string"}, "text": {"type": "string"}},
    "required": ["number", "text"],
    "additionalProperties": False,
}


class FakePlanner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((list(messages), list(tools)))
        return self.responses.pop(0) if self.responses else PlannerResponse(text="done")


def make_registry(calls):
    def battery(context, arguments):
        calls.append(("get_battery_status", dict(arguments)))
        return ToolResult.ok("Battery is at 87%.", {"percentage": 87})

    def send_sms(context, arguments):
        calls.append(("send_sms", dict(arguments)))
        return ToolResult.ok("Message sent.", {})

    return ToolRegistry([
        ToolSpec("get_battery_status", "Read the battery level.", NO_ARGS,
                 Risk.READ_ONLY, battery, timeout_seconds=2),
        ToolSpec("send_sms", "Send an SMS to a number.", SMS_SCHEMA,
                 Risk.EXTERNAL_SIDE_EFFECT, send_sms, timeout_seconds=2),
    ])


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ScheduleStore(Path(self.tmp.name) / "schedule.db")
        self.addCleanup(self.store.close)
        self.calls = []
        self.registry = make_registry(self.calls)
        self.audit = MemoryAuditSink()
        self.reported = []
        self.planner = FakePlanner()
        policy = DefaultPolicy(OWNER)
        self.runtime = AgentRuntime(
            planner=self.planner, registry=self.registry, policy=policy,
            system_prompt="Be useful.", audit=self.audit,
        )
        self.unattended = AgentRuntime(
            planner=self.planner, registry=self.registry, policy=UnattendedPolicy(policy),
            system_prompt="Be useful.", audit=self.audit,
        )
        self.runner = ScheduleRunner(
            store=self.store, runtime=self.runtime, unattended_runtime=self.unattended,
            owner_id=OWNER, chat_id=CHAT, reporter=self.reported.append, audit=self.audit,
        )
        self.now = time.time()

    def add(self, **overrides):
        options = {
            "description": "Battery check", "task_kind": TOOL, "schedule_kind": INTERVAL,
            "tool_name": "get_battery_status", "interval_seconds": 900, "now": self.now,
        }
        options.update(overrides)
        return self.store.add(build_task(**options))


class SafeToolTaskTests(RunnerTestCase):
    def test_a_read_only_task_runs_unattended(self):
        self.add()
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["ok"])
        self.assertEqual(self.calls, [("get_battery_status", {})])

    def test_the_result_is_reported(self):
        self.add()
        self.runner.tick(now=self.now + 1000)
        self.assertEqual(len(self.reported), 1)
        self.assertIn("Battery is at 87%", self.reported[0])
        self.assertIn("Battery check", self.reported[0])

    def test_nothing_runs_before_it_is_due(self):
        self.add()
        self.assertEqual(self.runner.tick(now=self.now), [])
        self.assertEqual(self.calls, [])

    def test_the_task_is_rescheduled_after_running(self):
        task = self.add()
        self.runner.tick(now=self.now + 1000)
        self.assertGreater(self.store.get(task.task_id).next_run_at, self.now + 1000)

    def test_a_run_is_audited(self):
        self.add()
        self.runner.tick(now=self.now + 1000)
        events = [e for e in self.audit.events if e["event"] == "schedule.ran"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "ok")


class UnattendedApprovalTests(RunnerTestCase):
    """The core safety property."""

    SMS_ARGS: ClassVar[dict] = {"number": "+919000000000", "text": "good morning"}

    def test_an_unapproved_risky_task_does_not_run(self):
        self.add(
            description="Morning text", tool_name="send_sms", arguments=self.SMS_ARGS,
        )
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["blocked"])
        self.assertEqual(self.calls, [])

    def test_the_owner_is_told_why_it_was_blocked(self):
        self.add(description="Morning text", tool_name="send_sms", arguments=self.SMS_ARGS)
        self.runner.tick(now=self.now + 1000)
        self.assertIn("confirmation", self.reported[0])
        self.assertIn("approve it once", self.reported[0])

    def test_a_pre_approved_risky_task_runs_without_asking(self):
        approved = approval_hash_for(self.runtime, "send_sms", self.SMS_ARGS)
        self.add(
            description="Morning text", tool_name="send_sms",
            arguments=self.SMS_ARGS, approved_hash=approved,
        )
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["ok"])
        self.assertEqual(self.calls, [("send_sms", self.SMS_ARGS)])

    def test_approval_does_not_transfer_to_different_arguments(self):
        """The whole point of hashing: an approved task cannot be edited."""
        approved = approval_hash_for(self.runtime, "send_sms", self.SMS_ARGS)
        self.add(
            description="Morning text", tool_name="send_sms",
            arguments={"number": "+919999999999", "text": "good morning"},
            approved_hash=approved,
        )
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["blocked"])
        self.assertEqual(self.calls, [])

    def test_approval_does_not_transfer_to_a_different_tool(self):
        approved = approval_hash_for(self.runtime, "get_battery_status", {})
        self.add(
            description="Sneaky", tool_name="send_sms", arguments=self.SMS_ARGS,
            approved_hash=approved,
        )
        self.assertEqual([r.status for r in self.runner.tick(now=self.now + 1000)], ["blocked"])
        self.assertEqual(self.calls, [])

    def test_a_blocked_task_is_still_rescheduled(self):
        """Blocking must not silently disable a recurring task."""
        task = self.add(
            description="Morning text", tool_name="send_sms", arguments=self.SMS_ARGS
        )
        self.runner.tick(now=self.now + 1000)
        reloaded = self.store.get(task.task_id)
        self.assertTrue(reloaded.enabled)
        self.assertEqual(reloaded.last_status, "blocked")


class PromptTaskTests(RunnerTestCase):
    def test_a_prompt_task_runs_through_the_agent(self):
        self.planner.responses = [PlannerResponse(text="Battery is fine.")]
        self.add(
            description="Morning summary", task_kind=PROMPT, tool_name=None,
            prompt="how is the battery?",
        )
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["ok"])
        self.assertIn("Battery is fine.", self.reported[0])

    def test_a_prompt_task_may_use_safe_tools(self):
        self.planner.responses = [
            PlannerResponse(tool_calls=(ToolCall("c1", "get_battery_status", {}),)),
            PlannerResponse(text="It is at 87%."),
        ]
        self.add(
            description="Morning summary", task_kind=PROMPT, tool_name=None,
            prompt="how is the battery?",
        )
        self.runner.tick(now=self.now + 1000)
        self.assertEqual(self.calls, [("get_battery_status", {})])

    def test_a_prompt_task_cannot_reach_a_tool_that_needs_approval(self):
        """Nobody is present, so an approval-gated tool must be refused.

        Checking only that the tool did not run is too weak: a policy that
        returned REQUIRE_APPROVAL would also not run it, but would leave a
        pending approval for the owner to confirm later, with no memory of
        what asked for it. The run must be denied outright.
        """
        self.planner.responses = [
            PlannerResponse(tool_calls=(
                ToolCall("c1", "send_sms", {"number": "+919000000000", "text": "hi"}),
            )),
            PlannerResponse(text="I could not send that."),
        ]
        self.add(
            description="Reply to messages", task_kind=PROMPT, tool_name=None,
            prompt="reply to anything urgent",
        )
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual(self.calls, [])
        self.assertEqual([r.status for r in runs], ["ok"])
        self.assertNotIn("approve", self.reported[0].lower())

    def test_a_pending_approval_from_an_unattended_run_is_refused(self):
        """Defence in depth: if the policy is misconfigured, still refuse."""
        unguarded = AgentRuntime(
            planner=self.planner, registry=self.registry, policy=DefaultPolicy(OWNER),
            system_prompt="Be useful.", audit=self.audit,
        )
        self.runner.unattended_runtime = unguarded
        self.planner.responses = [
            PlannerResponse(tool_calls=(
                ToolCall("c1", "send_sms", {"number": "+919000000000", "text": "hi"}),
            )),
        ]
        self.add(description="Reply", task_kind=PROMPT, tool_name=None, prompt="reply")
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["blocked"])
        self.assertEqual(self.calls, [])
        self.assertIn("send_sms", self.reported[0])

    def test_the_refusal_explains_itself_to_the_model(self):
        self.planner.responses = [
            PlannerResponse(tool_calls=(
                ToolCall("c1", "send_sms", {"number": "+919000000000", "text": "hi"}),
            )),
            PlannerResponse(text="I could not send that."),
        ]
        self.add(
            description="Reply", task_kind=PROMPT, tool_name=None, prompt="reply",
        )
        self.runner.tick(now=self.now + 1000)
        tool_messages = [
            message
            for messages, _ in self.planner.requests
            for message in messages
            if message.get("role") == "tool"
        ]
        self.assertTrue(any("unattended" in str(m).lower() for m in tool_messages))


class ResilienceTests(RunnerTestCase):
    def test_a_missing_tool_is_reported_not_crashed(self):
        task = self.add(description="Old task", tool_name="get_battery_status")
        self.store._connection.execute(
            "UPDATE tasks SET tool_name = ? WHERE task_id = ?", ("gone_tool", task.task_id)
        )
        self.store._connection.commit()
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["error"])
        self.assertIn("no longer exists", self.reported[0])

    def test_invalid_stored_arguments_are_reported(self):
        self.add(
            description="Bad args", tool_name="send_sms", arguments={"number": "+91900"},
        )
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["error"])

    def test_a_failing_reporter_does_not_stop_the_scheduler(self):
        def explode(message):
            raise RuntimeError("telegram is down")

        self.runner.reporter = explode
        self.add()
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual([r.status for r in runs], ["ok"])

    def test_one_bad_task_does_not_stop_the_others(self):
        self.add(description="Bad args", tool_name="send_sms", arguments={"number": "x"})
        self.add(description="Battery check")
        runs = self.runner.tick(now=self.now + 1000)
        self.assertEqual(len(runs), 2)
        self.assertIn("ok", [r.status for r in runs])

    def test_a_daily_task_is_picked_up_when_due(self):
        due_time = time.strftime("%H:%M", time.localtime(self.now + 120))
        self.add(
            description="Daily ping", schedule_kind=DAILY, daily_time=due_time,
            interval_seconds=None,
        )
        self.assertEqual(self.runner.tick(now=self.now + 60), [])
        self.assertEqual(len(self.runner.tick(now=self.now + 180)), 1)


if __name__ == "__main__":
    unittest.main()
