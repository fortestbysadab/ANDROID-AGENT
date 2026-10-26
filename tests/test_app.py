"""Tests for the composition root and the universal command.

The reason this exists: three entry points each used to assemble their own
registry, runtime, policy and authorisation callback. Copies like that do not
fail loudly — they drift, and a safety rule added to one silently misses the
others. These tests pin the properties that must hold wherever the agent is
started from.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from android_agent import __main__ as cli
from android_agent.app import build_application
from android_agent.config import Dialect, Settings
from android_agent.models.base import PlannerResponse
from android_agent.policy.engine import DefaultPolicy, UnattendedPolicy


class StubPlanner:
    def complete(self, messages, tools):
        return PlannerResponse(text="ok")


def make_settings(tmp, **overrides):
    values = {
        "telegram_bot_token": "token",
        "owner_chat_id": 4242,
        "llm_base_url": "https://example.invalid/v1",
        "llm_model": "test-model",
        "llm_api_key": "key",
        "llm_dialect": Dialect.OPENAI,
        "state_dir": tmp,
    }
    values.update(overrides)
    return Settings(**values)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(self.tmp.name)

    def build(self, **overrides):
        settings = make_settings(self.tmp.name, **overrides)
        application = build_application(settings, planner=StubPlanner())
        self.addCleanup(application.close)
        return application

    def test_it_builds_without_optional_features(self):
        application = self.build()
        self.assertIsNotNone(application.runtime)
        self.assertIsNotNone(application.schedule_runner)
        self.assertIsNone(application.email_channel)

    def test_the_two_runtimes_share_tools_but_not_policy(self):
        """The unattended runtime must refuse what the normal one asks about."""
        application = self.build()
        self.assertIs(application.runtime.registry, application.unattended_runtime.registry)
        self.assertIsInstance(application.runtime.policy, DefaultPolicy)
        self.assertIsInstance(application.unattended_runtime.policy, UnattendedPolicy)

    def test_scheduling_tools_are_always_registered(self):
        names = {s["function"]["name"] for s in self.build().registry.model_schemas()}
        self.assertIn("schedule_task", names)
        self.assertIn("cancel_scheduled_task", names)

    def test_email_tools_appear_only_when_configured(self):
        without = {s["function"]["name"] for s in self.build().registry.model_schemas()}
        self.assertNotIn("send_email", without)

        with_email = self.build(
            email_address="agent@example.com",
            email_app_password="abcd efgh ijkl mnop",  # noqa: S106 - fake
        )
        names = {s["function"]["name"] for s in with_email.registry.model_schemas()}
        self.assertIn("send_email", names)
        self.assertIsNotNone(with_email.email_channel)

    def test_the_email_display_name_reaches_the_channel(self):
        application = self.build(
            email_address="agent@example.com",
            email_app_password="abcd efgh ijkl mnop",  # noqa: S106 - fake
            email_display_name="Agent Mailer",
        )
        self.assertEqual(application.email_channel.display_name, "Agent Mailer")

    def test_a_safe_scheduled_action_is_authorised_at_creation(self):
        """The authorisation callback must agree with the live policy."""
        application = self.build()
        tool = application.registry.get("schedule_task")
        result = tool.handler(None, tool.validate({
            "description": "Battery", "schedule": "daily", "daily_time": "07:00",
            "tool_name": "get_battery_status",
        }))
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.data["needs_authorisation"])

    def test_a_risky_scheduled_action_is_held_for_authorisation(self):
        """Arguments must be *valid*, or this passes for the wrong reason.

        The first version of this test used "text" where send_sms wants
        "message". Validation failed, the callback returned None because the
        arguments were unusable rather than because the policy said so, and
        the test stayed green even with the policy check deleted.
        """
        application = self.build()
        tool = application.registry.get("schedule_task")
        arguments = {"number": "+919000000000", "message": "good morning"}
        # Guard: if the schema ever changes, fail here rather than silently
        # testing nothing.
        application.registry.get("send_sms").validate(arguments)

        result = tool.handler(None, tool.validate({
            "description": "Morning text", "schedule": "daily", "daily_time": "07:00",
            "tool_name": "send_sms", "tool_arguments": arguments,
        }))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["needs_authorisation"])
        self.assertIsNone(application.schedule_store.all_tasks()[0].approved_hash)

    def test_unusable_arguments_are_not_mistaken_for_a_policy_refusal(self):
        """Both return 'not authorised'; only one is a safety decision."""
        application = self.build()
        tool = application.registry.get("schedule_task")
        result = tool.handler(None, tool.validate({
            "description": "Broken", "schedule": "daily", "daily_time": "07:00",
            "tool_name": "send_sms", "tool_arguments": {"number": "+919000000000"},
        }))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["needs_authorisation"])

    def test_the_state_directory_is_created(self):
        import os

        nested = os.path.join(self.tmp.name, "deep", "state")
        application = build_application(
            make_settings(nested), planner=StubPlanner()
        )
        self.addCleanup(application.close)
        self.assertTrue(os.path.isdir(nested))

    def test_a_default_reporter_exists_so_the_scheduler_never_crashes(self):
        """A front end that forgets to pass one must still be safe."""
        application = self.build()
        application.schedule_runner.reporter(
            type("Run", (), {"task_id": "t", "message": "m", "status": "ok"})()
        )

    def test_close_is_safe_to_call_twice(self):
        application = build_application(self.settings, planner=StubPlanner())
        application.close()
        application.close()


class CommandLineTests(unittest.TestCase):
    def test_every_documented_command_is_wired(self):
        self.assertEqual(
            set(cli.COMMANDS), {"all", "bot", "web", "tick", "doctor"}
        )

    def test_running_with_no_argument_starts_everything(self):
        with mock.patch.dict(cli.COMMANDS, {"all": mock.Mock(return_value=0)}) as commands:
            self.assertEqual(cli.main([]), 0)
            commands["all"].assert_called_once()

    def test_each_subcommand_dispatches_to_its_own_runner(self):
        for name in ("bot", "web", "tick", "doctor"):
            with self.subTest(command=name):
                runner = mock.Mock(return_value=0)
                with mock.patch.dict(cli.COMMANDS, {name: runner}):
                    self.assertEqual(cli.main([name]), 0)
                runner.assert_called_once()

    def test_an_unknown_command_is_rejected_rather_than_guessed(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["definitely-not-a-command"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_interrupting_exits_cleanly(self):
        with mock.patch.dict(cli.COMMANDS, {"all": mock.Mock(side_effect=KeyboardInterrupt)}):
            self.assertEqual(cli.main([]), 130)

    def test_a_web_console_failure_does_not_take_down_telegram(self):
        """The console is optional; Telegram is the primary front end."""
        with mock.patch(
            "android_agent.web.__main__.main", side_effect=SystemExit("no token")
        ):
            cli._web_forever()  # must not raise

    def test_an_unexpected_web_error_is_also_contained(self):
        with mock.patch(
            "android_agent.web.__main__.main", side_effect=RuntimeError("boom")
        ):
            cli._web_forever()


if __name__ == "__main__":
    unittest.main()
