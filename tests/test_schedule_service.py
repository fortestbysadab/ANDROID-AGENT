"""Tests for the scheduler loop and the end-to-end path.

The loop is deliberately simple, so the tests concentrate on the two ways a
background scheduler fails in the field: it dies silently after one bad tick,
or it refuses to stop and hangs shutdown.

The end-to-end case goes through the real tools and the real runner, because
every seam between them has been a source of bugs in this project.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from android_agent.agent.runtime import AgentRuntime
from android_agent.models.base import PlannerResponse
from android_agent.observability.audit import MemoryAuditSink
from android_agent.policy.engine import DefaultPolicy, PolicyDecision, UnattendedPolicy
from android_agent.schedule.runner import ScheduleRunner, approval_hash_for
from android_agent.schedule.service import ScheduleService
from android_agent.schedule.store import ScheduleStore
from android_agent.tools.base import Risk, SchemaValidationError, ToolContext, ToolResult, ToolSpec
from android_agent.tools.catalog import build_full_registry
from android_agent.tools.registry import ToolRegistry
from android_agent.tools.schedule_tools import schedule_tools

OWNER = "77"
NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
SMS_SCHEMA = {
    "type": "object",
    "properties": {"number": {"type": "string"}, "text": {"type": "string"}},
    "required": ["number", "text"],
    "additionalProperties": False,
}


class CountingRunner:
    def __init__(self, explode_times=0):
        self.ticks = 0
        self.explode_times = explode_times
        self.seen = threading.Event()

    def tick(self, *, now=None):
        self.ticks += 1
        self.seen.set()
        if self.ticks <= self.explode_times:
            raise RuntimeError("tick blew up")
        return []


class ServiceLoopTests(unittest.TestCase):
    def test_it_ticks_immediately_rather_than_waiting_a_full_interval(self):
        runner = CountingRunner()
        service = ScheduleService(runner, tick_seconds=60)
        self.addCleanup(service.stop)
        service.start()
        self.assertTrue(runner.seen.wait(2), "no tick within two seconds")

    def test_a_failing_tick_does_not_kill_the_loop(self):
        """A dead scheduler thread is silent forever, which is the worst case."""
        runner = CountingRunner(explode_times=2)
        service = ScheduleService(runner, tick_seconds=0.05)
        self.addCleanup(service.stop)
        service.start()
        deadline = time.monotonic() + 3
        while runner.ticks < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(runner.ticks, 4)
        self.assertTrue(service.running)

    def test_stopping_is_prompt_and_does_not_wait_out_the_sleep(self):
        runner = CountingRunner()
        service = ScheduleService(runner, tick_seconds=30)
        service.start()
        started = time.monotonic()
        service.stop(timeout=5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(service.running)

    def test_starting_twice_does_not_create_a_second_thread(self):
        runner = CountingRunner()
        service = ScheduleService(runner, tick_seconds=30)
        self.addCleanup(service.stop)
        service.start()
        first = service._thread
        service.start()
        self.assertIs(service._thread, first)

    def test_stopping_an_unstarted_service_is_harmless(self):
        ScheduleService(CountingRunner()).stop()

    def test_a_nonsense_interval_is_refused(self):
        with self.assertRaises(ValueError):
            ScheduleService(CountingRunner(), tick_seconds=0)


class EndToEndTests(unittest.TestCase):
    """From 'schedule this' through to the task actually firing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ScheduleStore(Path(self.tmp.name) / "s.db")
        self.addCleanup(self.store.close)
        self.calls = []
        self.policy = DefaultPolicy(OWNER)

        def battery(context, arguments):
            self.calls.append("battery")
            return ToolResult.ok("Battery is at 91%.", {"percentage": 91})

        def send_sms(context, arguments):
            self.calls.append("sms")
            return ToolResult.ok("Sent.", {})

        self.registry = ToolRegistry([
            ToolSpec("get_battery_status", "Battery.", NO_ARGS, Risk.READ_ONLY,
                     battery, timeout_seconds=2),
            ToolSpec("send_sms", "SMS.", SMS_SCHEMA, Risk.EXTERNAL_SIDE_EFFECT,
                     send_sms, timeout_seconds=2),
        ])

        # The same callback agent_bot wires in.
        def needs_authorisation(tool_name, arguments, *, force=False):
            tool = self.registry.get(tool_name)
            if tool is None:
                return None
            context = ToolContext(OWNER, 77, "auth", direct_user_request=False)
            try:
                validated = tool.validate(arguments)
            except SchemaValidationError:
                return None
            decision = self.policy.evaluate(context, tool, validated)
            if decision.decision is not PolicyDecision.ALLOW and not force:
                return None
            return approval_hash_for(self.registry, tool_name, validated)

        for tool in schedule_tools(
            self.store, self.registry, needs_authorisation=needs_authorisation
        ):
            self.registry.register(tool)

        planner = type("P", (), {"complete": lambda s, m, t: PlannerResponse(text="ok")})()
        runtime = AgentRuntime(
            planner=planner, registry=self.registry, policy=self.policy,
            system_prompt="x", audit=MemoryAuditSink(),
        )
        unattended = AgentRuntime(
            planner=planner, registry=self.registry, policy=UnattendedPolicy(self.policy),
            system_prompt="x", audit=MemoryAuditSink(),
        )
        self.reported = []
        self.runner = ScheduleRunner(
            store=self.store, runtime=runtime, unattended_runtime=unattended,
            owner_id=OWNER, chat_id=77, reporter=self.reported.append,
        )

    def schedule(self, **arguments):
        tool = self.registry.get("schedule_task")
        return tool.handler(None, tool.validate(arguments))

    def test_a_safe_task_created_by_tool_fires_on_the_loop(self):
        created = self.schedule(
            description="Battery", schedule="once", in_minutes=1,
            tool_name="get_battery_status",
        )
        self.assertEqual(created.status, "ok")
        service = ScheduleService(self.runner, tick_seconds=0.05)
        self.addCleanup(service.stop)
        # Pretend a minute passed.
        task = self.store.all_tasks()[0]
        self.store._connection.execute(
            "UPDATE tasks SET next_run_at = ? WHERE task_id = ?",
            (time.time() - 1, task.task_id),
        )
        self.store._connection.commit()
        service.start()
        deadline = time.monotonic() + 3
        while not self.calls and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.calls, ["battery"])
        self.assertIn("91%", self.reported[0].message)

    def test_a_risky_task_does_not_fire_until_authorised(self):
        self.schedule(
            description="Morning text", schedule="once", in_minutes=1,
            tool_name="send_sms",
            tool_arguments={"number": "+919000000000", "text": "hi"},
        )
        task = self.store.all_tasks()[0]
        self.assertIsNone(task.approved_hash)
        self.runner.tick(now=task.next_run_at + 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reported[0].status, "blocked")

    def test_the_same_task_fires_once_authorised(self):
        self.schedule(
            description="Morning text", schedule="once", in_minutes=1,
            tool_name="send_sms",
            tool_arguments={"number": "+919000000000", "text": "hi"},
        )
        task_id = self.store.all_tasks()[0].task_id
        authorize = self.registry.get("authorize_scheduled_task")
        result = authorize.handler(None, authorize.validate({"task_id": task_id}))
        self.assertEqual(result.status, "ok")
        task = self.store.get(task_id)
        self.runner.tick(now=task.next_run_at + 1)
        self.assertEqual(self.calls, ["sms"])

    def test_listing_then_cancelling_stops_it_firing(self):
        self.schedule(
            description="Battery", schedule="once", in_minutes=1,
            tool_name="get_battery_status",
        )
        listing = self.registry.get("list_scheduled_tasks")
        shown = listing.handler(None, {})
        task_id = shown.data["tasks"][0]["id"]
        cancel = self.registry.get("cancel_scheduled_task")
        cancel.handler(None, cancel.validate({"task_id": task_id}))
        self.runner.tick(now=time.time() + 3600)
        self.assertEqual(self.calls, [])

    def test_tool_arguments_survive_the_round_trip(self):
        """The validator bug this feature exposed: arguments were dropped."""
        self.schedule(
            description="Morning text", schedule="once", in_minutes=1,
            tool_name="send_sms",
            tool_arguments={"number": "+919000000000", "text": "hi"},
        )
        stored = self.store.all_tasks()[0]
        self.assertEqual(stored.arguments, {"number": "+919000000000", "text": "hi"})


class CatalogueWiringTests(unittest.TestCase):
    def test_the_bot_catalogue_gains_exactly_four_tools(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ScheduleStore(Path(tmp.name) / "s.db")
        self.addCleanup(store.close)
        without = {s["function"]["name"] for s in build_full_registry().model_schemas()}
        with_schedule = {
            s["function"]["name"]
            for s in build_full_registry(
                store, needs_authorisation=lambda *a, **k: "h"
            ).model_schemas()
        }
        self.assertEqual(
            with_schedule - without,
            {"schedule_task", "authorize_scheduled_task",
             "list_scheduled_tasks", "cancel_scheduled_task"},
        )


if __name__ == "__main__":
    unittest.main()
