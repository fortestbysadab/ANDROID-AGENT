"""Tests for the scheduled-task store.

The cases that matter here are the ones that only show up on a real phone:
the device was off when a task was due, the owner created a daily task for a
time that has already passed today, and a repeating task must not fire a
hundred times to catch up.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from android_agent.schedule.store import (
    DAILY,
    INTERVAL,
    MIN_INTERVAL_SECONDS,
    ONCE,
    PROMPT,
    TOOL,
    ScheduleError,
    ScheduleStore,
    build_task,
    new_task_id,
    next_daily_run,
)

HOUR = 3600
DAY = 24 * HOUR


def local_hm(moment: float) -> str:
    return time.strftime("%H:%M", time.localtime(moment))


class DailyTimeTests(unittest.TestCase):
    def test_next_run_is_today_when_the_time_is_still_ahead(self):
        now = time.mktime((2026, 10, 3, 6, 0, 0, 0, 0, -1))
        self.assertEqual(local_hm(next_daily_run(7, 0, now=now)), "07:00")
        self.assertLess(next_daily_run(7, 0, now=now) - now, DAY)

    def test_next_run_rolls_to_tomorrow_when_the_time_has_passed(self):
        now = time.mktime((2026, 10, 3, 8, 0, 0, 0, 0, -1))
        following = next_daily_run(7, 0, now=now)
        self.assertEqual(local_hm(following), "07:00")
        self.assertGreater(following, now)
        self.assertLess(following - now, DAY)

    def test_the_boundary_second_counts_as_passed(self):
        """Firing 'now' for a time that is exactly now would double-run it."""
        now = time.mktime((2026, 10, 3, 7, 0, 0, 0, 0, -1))
        self.assertGreater(next_daily_run(7, 0, now=now), now)

    def test_month_end_rolls_over_correctly(self):
        now = time.mktime((2026, 10, 31, 23, 30, 0, 0, 0, -1))
        following = next_daily_run(7, 0, now=now)
        self.assertEqual(time.localtime(following).tm_mon, 11)
        self.assertEqual(time.localtime(following).tm_mday, 1)

    def test_ids_are_unique(self):
        self.assertEqual(len({new_task_id() for _ in range(500)}), 500)


class BuildTaskTests(unittest.TestCase):
    def test_a_daily_task_normalises_its_time(self):
        task = build_task(
            description="Morning battery", task_kind=TOOL, schedule_kind=DAILY,
            tool_name="get_battery_status", daily_time="7:5",
        )
        self.assertEqual(task.daily_time, "07:05")
        self.assertEqual(task.schedule_text(), "every day at 07:05")

    def test_a_one_off_in_the_past_is_refused(self):
        with self.assertRaises(ScheduleError) as caught:
            build_task(
                description="x", task_kind=TOOL, schedule_kind=ONCE,
                tool_name="get_battery_status", run_at=time.time() - 60,
            )
        self.assertIn("past", str(caught.exception))

    def test_a_too_short_interval_is_refused(self):
        with self.assertRaises(ScheduleError):
            build_task(
                description="x", task_kind=TOOL, schedule_kind=INTERVAL,
                tool_name="get_battery_status", interval_seconds=MIN_INTERVAL_SECONDS - 1,
            )

    def test_a_tool_task_without_a_tool_is_refused(self):
        with self.assertRaises(ScheduleError):
            build_task(description="x", task_kind=TOOL, schedule_kind=DAILY, daily_time="07:00")

    def test_a_prompt_task_without_text_is_refused(self):
        with self.assertRaises(ScheduleError):
            build_task(
                description="x", task_kind=PROMPT, schedule_kind=DAILY,
                prompt="   ", daily_time="07:00",
            )

    def test_a_nonsense_time_is_refused(self):
        for value in ("25:00", "07:61", "seven", "", None):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                build_task(
                    description="x", task_kind=TOOL, schedule_kind=DAILY,
                    tool_name="get_battery_status", daily_time=value,
                )

    def test_a_prompt_task_is_allowed(self):
        task = build_task(
            description="Morning summary", task_kind=PROMPT, schedule_kind=DAILY,
            prompt="tell me the battery level", daily_time="07:00",
        )
        self.assertEqual(task.task_kind, PROMPT)
        self.assertIsNone(task.tool_name)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "schedule.db"
        self.store = ScheduleStore(self.path, max_tasks=3)
        self.addCleanup(self.store.close)
        self.now = time.time()

    def _add(self, **overrides):
        options = {
            "description": "Battery check", "task_kind": TOOL, "schedule_kind": INTERVAL,
            "tool_name": "get_battery_status", "interval_seconds": 900, "now": self.now,
        }
        options.update(overrides)
        return self.store.add(build_task(**options))

    def test_round_trip(self):
        task = self._add()
        loaded = self.store.get(task.task_id)
        self.assertEqual(loaded.description, "Battery check")
        self.assertEqual(loaded.tool_name, "get_battery_status")

    def test_tasks_survive_a_restart(self):
        task = self._add()
        self.store.close()
        reopened = ScheduleStore(self.path, max_tasks=3)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(task.task_id).description, "Battery check")

    def test_only_due_and_enabled_tasks_are_returned(self):
        due = self._add(description="due")
        self._add(description="later", interval_seconds=7200)
        self.store.set_enabled(due.task_id, False)
        self.assertEqual(self.store.due(now=self.now + 1000), [])
        self.store.set_enabled(due.task_id, True)
        self.assertEqual([t.description for t in self.store.due(now=self.now + 1000)], ["due"])

    def test_a_one_off_task_retires_after_running(self):
        task = self._add(
            schedule_kind=ONCE, run_at=self.now + 60, interval_seconds=None
        )
        self.store.record_run(task.task_id, "ok", now=self.now + 60)
        reloaded = self.store.get(task.task_id)
        self.assertFalse(reloaded.enabled)
        self.assertEqual(reloaded.run_count, 1)
        self.assertEqual(reloaded.last_status, "ok")

    def test_a_repeating_task_moves_to_its_next_slot(self):
        task = self._add(interval_seconds=900)
        self.store.record_run(task.task_id, "ok", now=task.next_run_at)
        reloaded = self.store.get(task.task_id)
        self.assertTrue(reloaded.enabled)
        self.assertAlmostEqual(reloaded.next_run_at, task.next_run_at + 900, delta=1)

    def test_missed_slots_are_skipped_rather_than_replayed(self):
        """The phone was off for a day; do not fire 96 times to catch up."""
        task = self._add(interval_seconds=900)
        much_later = task.next_run_at + DAY
        self.store.record_run(task.task_id, "ok", now=much_later)
        reloaded = self.store.get(task.task_id)
        self.assertGreater(reloaded.next_run_at, much_later)
        self.assertLessEqual(reloaded.next_run_at, much_later + 900)
        self.assertEqual(reloaded.run_count, 1)

    def test_a_daily_task_reschedules_to_the_same_wall_clock_time(self):
        task = self._add(
            schedule_kind=DAILY, daily_time="07:00", interval_seconds=None
        )
        self.store.record_run(task.task_id, "ok", now=task.next_run_at)
        reloaded = self.store.get(task.task_id)
        self.assertEqual(local_hm(reloaded.next_run_at), "07:00")
        self.assertGreater(reloaded.next_run_at, task.next_run_at)

    def test_recording_a_run_for_a_missing_task_is_harmless(self):
        self.store.record_run("nope", "ok")

    def test_delete_and_clear(self):
        first = self._add(description="one")
        self._add(description="two")
        self.assertTrue(self.store.delete(first.task_id))
        self.assertFalse(self.store.delete(first.task_id))
        self.assertEqual(self.store.clear(), 1)
        self.assertEqual(self.store.all_tasks(), [])

    def test_the_task_limit_is_enforced(self):
        for index in range(3):
            self._add(description=f"task {index}")
        with self.assertRaises(ScheduleError) as caught:
            self._add(description="one too many")
        self.assertIn("limit", str(caught.exception))

    def test_cancelling_frees_a_slot(self):
        tasks = [self._add(description=f"task {i}") for i in range(3)]
        self.store.delete(tasks[0].task_id)
        self._add(description="replacement")
        self.assertEqual(len(self.store.all_tasks()), 3)

    def test_arguments_round_trip_as_data(self):
        task = self._add(
            tool_name="set_volume", arguments={"stream": "music", "level": 7}
        )
        self.assertEqual(self.store.get(task.task_id).arguments, {"stream": "music", "level": 7})

    def test_summary_is_json_safe(self):
        task = self._add()
        payload = task.as_dict()
        self.assertEqual(payload["id"], task.task_id)
        self.assertIn("next_run_local", payload)
        self.assertIn("minutes", payload["schedule"])


if __name__ == "__main__":
    unittest.main()
