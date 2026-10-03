"""Tests for the scheduling tools.

These are the surface the owner actually touches, through sentences like
"remind me every morning" and "cancel the 7am one". Two things matter most:
creating a task can never be a way to perform an action that would otherwise
need confirmation, and the ids the model cancels by must come from a listing
rather than being invented.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from android_agent.schedule.store import ScheduleStore
from android_agent.tools.base import Risk, ToolResult, ToolSpec
from android_agent.tools.catalog import build_full_registry
from android_agent.tools.registry import ToolRegistry
from android_agent.tools.schedule_tools import schedule_tools

NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
SMS_SCHEMA = {
    "type": "object",
    "properties": {"number": {"type": "string"}, "text": {"type": "string"}},
    "required": ["number", "text"],
    "additionalProperties": False,
}
SMS_ARGS = {"number": "+919000000000", "text": "good morning"}


class ScheduleToolsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ScheduleStore(Path(self.tmp.name) / "s.db", max_tasks=5)
        self.addCleanup(self.store.close)
        self.registry = ToolRegistry([
            ToolSpec("get_battery_status", "Read battery.", NO_ARGS, Risk.READ_ONLY,
                     lambda c, a: ToolResult.ok("87%", {}), timeout_seconds=2),
            ToolSpec("send_sms", "Send SMS.", SMS_SCHEMA, Risk.EXTERNAL_SIDE_EFFECT,
                     lambda c, a: ToolResult.ok("sent", {}), timeout_seconds=2),
        ])
        self.authorised_calls = []

        def needs_authorisation(tool_name, arguments, *, force=False):
            self.authorised_calls.append((tool_name, dict(arguments), force))
            tool = self.registry.get(tool_name)
            if tool is None:
                return None
            if tool.risk is Risk.EXTERNAL_SIDE_EFFECT and not force:
                return None
            return f"hash-of-{tool_name}-{sorted(arguments.items())}"

        self.tools = {
            tool.name: tool
            for tool in schedule_tools(
                self.store, self.registry, needs_authorisation=needs_authorisation
            )
        }

    def call(self, name, **arguments):
        tool = self.tools[name]
        return tool.handler(None, tool.validate(arguments))


class CreationTests(ScheduleToolsTestCase):
    def test_a_daily_tool_task_is_created(self):
        result = self.call(
            "schedule_task", description="Morning battery", schedule="daily",
            daily_time="07:30", tool_name="get_battery_status",
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(self.store.all_tasks()), 1)
        self.assertIn("07:30", result.summary)

    def test_a_one_off_task_uses_minutes_from_now(self):
        result = self.call(
            "schedule_task", description="Ping me", schedule="once", in_minutes=30,
            prompt="say hello",
        )
        self.assertEqual(result.status, "ok")
        task = self.store.all_tasks()[0]
        self.assertAlmostEqual(task.next_run_at, time.time() + 1800, delta=5)

    def test_a_one_off_task_without_a_time_is_refused(self):
        result = self.call(
            "schedule_task", description="Ping", schedule="once", prompt="hello"
        )
        self.assertEqual(result.status, "error")
        self.assertEqual(self.store.all_tasks(), [])

    def test_both_a_tool_and_a_prompt_is_refused(self):
        result = self.call(
            "schedule_task", description="Confused", schedule="daily", daily_time="07:00",
            tool_name="get_battery_status", prompt="also do this",
        )
        self.assertEqual(result.error_code, "invalid_arguments")

    def test_neither_a_tool_nor_a_prompt_is_refused(self):
        result = self.call(
            "schedule_task", description="Empty", schedule="daily", daily_time="07:00"
        )
        self.assertEqual(result.error_code, "invalid_arguments")

    def test_an_unknown_tool_is_refused(self):
        result = self.call(
            "schedule_task", description="Bad", schedule="daily", daily_time="07:00",
            tool_name="no_such_tool",
        )
        self.assertEqual(result.error_code, "unknown_tool")

    def test_a_short_interval_warns_about_the_android_floor(self):
        result = self.call(
            "schedule_task", description="Frequent", schedule="interval",
            every_minutes=5, tool_name="get_battery_status",
        )
        self.assertEqual(result.status, "ok")
        self.assertIn("15 minutes", result.summary)

    def test_the_task_limit_is_surfaced(self):
        for index in range(5):
            self.call(
                "schedule_task", description=f"task {index}", schedule="daily",
                daily_time="07:00", tool_name="get_battery_status",
            )
        result = self.call(
            "schedule_task", description="one more", schedule="daily",
            daily_time="07:00", tool_name="get_battery_status",
        )
        self.assertEqual(result.error_code, "schedule_full")


class AuthorisationTests(ScheduleToolsTestCase):
    """Creating a task must never be a back door to a risky action."""

    def test_a_safe_tool_task_is_authorised_immediately(self):
        self.call(
            "schedule_task", description="Battery", schedule="daily",
            daily_time="07:00", tool_name="get_battery_status",
        )
        self.assertIsNotNone(self.store.all_tasks()[0].approved_hash)

    def test_a_risky_tool_task_is_stored_unauthorised(self):
        result = self.call(
            "schedule_task", description="Morning text", schedule="daily",
            daily_time="07:00", tool_name="send_sms", tool_arguments=SMS_ARGS,
        )
        self.assertEqual(result.status, "ok")
        self.assertIsNone(self.store.all_tasks()[0].approved_hash)
        self.assertTrue(result.data["needs_authorisation"])

    def test_the_reply_tells_the_owner_it_needs_confirming(self):
        result = self.call(
            "schedule_task", description="Morning text", schedule="daily",
            daily_time="07:00", tool_name="send_sms", tool_arguments=SMS_ARGS,
        )
        self.assertIn("needs your confirmation", result.summary)

    def test_authorising_freezes_the_hash(self):
        self.call(
            "schedule_task", description="Morning text", schedule="daily",
            daily_time="07:00", tool_name="send_sms", tool_arguments=SMS_ARGS,
        )
        task_id = self.store.all_tasks()[0].task_id
        result = self.call("authorize_scheduled_task", task_id=task_id)
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(self.store.get(task_id).approved_hash)

    def test_authorising_preserves_the_schedule(self):
        self.call(
            "schedule_task", description="Morning text", schedule="daily",
            daily_time="07:00", tool_name="send_sms", tool_arguments=SMS_ARGS,
        )
        before = self.store.all_tasks()[0]
        self.call("authorize_scheduled_task", task_id=before.task_id)
        after = self.store.get(before.task_id)
        self.assertEqual(after.daily_time, before.daily_time)
        self.assertEqual(after.next_run_at, before.next_run_at)
        self.assertEqual(after.arguments, before.arguments)

    def test_authorisation_is_gated_by_the_approval_policy(self):
        """It is EXTERNAL_SIDE_EFFECT, so the owner's prompt guards it."""
        self.assertEqual(
            self.tools["authorize_scheduled_task"].risk, Risk.EXTERNAL_SIDE_EFFECT
        )

    def test_creating_a_task_is_only_reversible_risk(self):
        """Creating is harmless because the runner still refuses to act."""
        self.assertEqual(self.tools["schedule_task"].risk, Risk.REVERSIBLE)

    def test_a_prompt_task_cannot_be_authorised(self):
        self.call(
            "schedule_task", description="Think", schedule="daily",
            daily_time="07:00", prompt="decide something",
        )
        task_id = self.store.all_tasks()[0].task_id
        result = self.call("authorize_scheduled_task", task_id=task_id)
        self.assertEqual(result.error_code, "invalid_task")

    def test_authorising_an_unknown_task_is_refused(self):
        result = self.call("authorize_scheduled_task", task_id="nope")
        self.assertEqual(result.error_code, "unknown_task")


class ListAndCancelTests(ScheduleToolsTestCase):
    def test_an_empty_schedule_says_so(self):
        result = self.call("list_scheduled_tasks")
        self.assertEqual(result.status, "ok")
        self.assertIn("no scheduled tasks", result.summary)

    def test_listing_shows_ids_timing_and_next_run(self):
        self.call(
            "schedule_task", description="Morning battery", schedule="daily",
            daily_time="07:30", tool_name="get_battery_status",
        )
        result = self.call("list_scheduled_tasks")
        task_id = self.store.all_tasks()[0].task_id
        self.assertIn(task_id, result.summary)
        self.assertIn("Morning battery", result.summary)
        self.assertIn("every day at 07:30", result.summary)
        self.assertEqual(len(result.data["tasks"]), 1)

    def test_cancelling_removes_the_task(self):
        self.call(
            "schedule_task", description="Morning battery", schedule="daily",
            daily_time="07:30", tool_name="get_battery_status",
        )
        task_id = self.store.all_tasks()[0].task_id
        result = self.call("cancel_scheduled_task", task_id=task_id)
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.store.all_tasks(), [])

    def test_cancelling_an_unknown_id_says_how_to_find_the_right_one(self):
        result = self.call("cancel_scheduled_task", task_id="abcd1234")
        self.assertEqual(result.error_code, "unknown_task")
        self.assertIn("List the tasks", result.summary)

    def test_listing_is_read_only_and_idempotent(self):
        tool = self.tools["list_scheduled_tasks"]
        self.assertEqual(tool.risk, Risk.READ_ONLY)
        self.assertTrue(tool.idempotent)


class CompletedTaskVisibilityTests(ScheduleToolsTestCase):
    """A one-off that has run is history, not schedule - but recent history."""

    def _finished_one_off(self, *, ran_hours_ago=1.0):
        self.call(
            "schedule_task", description="Ping me", schedule="once",
            in_minutes=1, tool_name="get_battery_status",
        )
        task = self.store.all_tasks()[0]
        ran_at = time.time() - ran_hours_ago * 3600
        self.store.record_run(task.task_id, "ok", now=ran_at)
        return task.task_id

    def test_a_finished_one_off_is_listed_as_done(self):
        self._finished_one_off(ran_hours_ago=2)
        result = self.call("list_scheduled_tasks")
        self.assertIn("done", result.summary)
        self.assertIn("2h ago", result.summary)
        self.assertNotIn("paused", result.summary)

    def test_the_headline_separates_upcoming_from_finished(self):
        self._finished_one_off()
        self.call(
            "schedule_task", description="Daily battery", schedule="daily",
            daily_time="07:00", tool_name="get_battery_status",
        )
        result = self.call("list_scheduled_tasks")
        self.assertIn("1 scheduled task(s)", result.summary)
        self.assertIn("1 finished in the last 24 hours", result.summary)

    def test_a_finished_one_off_disappears_after_a_day(self):
        task_id = self._finished_one_off(ran_hours_ago=25)
        result = self.call("list_scheduled_tasks")
        self.assertIn("no scheduled tasks", result.summary)
        self.assertIsNone(self.store.get(task_id))

    def test_listing_purges_without_needing_a_tick(self):
        self._finished_one_off(ran_hours_ago=30)
        self.assertEqual(len(self.store.all_tasks()), 1)
        self.call("list_scheduled_tasks")
        self.assertEqual(self.store.all_tasks(), [])

    def test_a_recurring_task_is_never_described_as_done(self):
        self.call(
            "schedule_task", description="Daily battery", schedule="daily",
            daily_time="07:00", tool_name="get_battery_status",
        )
        task = self.store.all_tasks()[0]
        self.store.record_run(task.task_id, "ok")
        result = self.call("list_scheduled_tasks")
        self.assertNotIn("done", result.summary)
        self.assertIn("every day at 07:00", result.summary)

    def test_a_completed_task_shows_no_next_run(self):
        """It is not going to happen again; offering a time would be a lie."""
        self._finished_one_off()
        result = self.call("list_scheduled_tasks")
        self.assertNotIn("next ", result.summary)


class CatalogueTests(unittest.TestCase):
    def test_scheduling_tools_are_absent_without_a_store(self):
        names = {s["function"]["name"] for s in build_full_registry().model_schemas()}
        self.assertNotIn("schedule_task", names)

    def test_scheduling_tools_appear_when_a_store_is_supplied(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ScheduleStore(Path(tmp.name) / "s.db")
        self.addCleanup(store.close)
        registry = build_full_registry(store, needs_authorisation=lambda *a, **k: "h")
        names = {s["function"]["name"] for s in registry.model_schemas()}
        for expected in (
            "schedule_task", "authorize_scheduled_task",
            "list_scheduled_tasks", "cancel_scheduled_task",
        ):
            with self.subTest(tool=expected):
                self.assertIn(expected, names)

    def test_a_store_without_a_callback_is_a_programming_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ScheduleStore(Path(tmp.name) / "s.db")
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            build_full_registry(store)


if __name__ == "__main__":
    unittest.main()
