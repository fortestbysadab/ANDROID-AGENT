"""Needle fast-path routing.

The security contract: confidence routes, it never authorizes. Every Needle
proposal must still pass schema validation and the policy engine.
"""

from __future__ import annotations

import logging
import unittest

from android_agent.agent.runtime import AgentRuntime, RunStatus
from android_agent.models.base import PlannerResponse
from android_agent.models.needle import (
    DEFAULT_FAST_PATH_TOOLS,
    NeedleRouter,
)
from android_agent.policy.engine import DefaultPolicy
from android_agent.tools.base import Risk, ToolResult, ToolSpec
from android_agent.tools.registry import ToolRegistry


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeNeedle:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete(self, text):
        self.calls += 1
        return self.response() if callable(self.response) else self.response


class FakeCloud:
    def __init__(self, response=None):
        self.response = response or PlannerResponse("cloud answer")
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return self.response


def _call(name, arguments, confidence=0.95):
    return {
        "type": "call",
        "success": True,
        "function_calls": [{"name": name, "arguments": arguments}],
        "confidence": confidence,
    }


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_torch",
            "description": "Torch.",
            "parameters": {
                "type": "object",
                "properties": {"on": {"type": "boolean"}},
                "required": ["on"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_sms",
            "description": "SMS.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class RoutingTests(unittest.TestCase):
    def _router(self, needle_response, **kwargs):
        self.cloud = FakeCloud()
        self.needle = FakeNeedle(needle_response)
        return NeedleRouter(cloud=self.cloud, agent=self.needle, **kwargs)

    def test_confident_allowlisted_call_skips_the_cloud(self):
        router = self._router(_call("set_torch", {"on": True}, 0.95))
        response = router.complete([{"role": "user", "content": "torch on"}], TOOLS)
        self.assertEqual(response.tool_calls[0].name, "set_torch")
        self.assertEqual(self.cloud.calls, 0)

    def test_low_confidence_escalates(self):
        router = self._router(_call("set_torch", {"on": True}, 0.40))
        response = router.complete([{"role": "user", "content": "torch on"}], TOOLS)
        self.assertEqual(response.text, "cloud answer")
        self.assertEqual(self.cloud.calls, 1)

    def test_missing_confidence_escalates(self):
        """Fine-tuned Needle builds report confidence as None."""
        payload = _call("set_torch", {"on": True})
        payload["confidence"] = None
        router = self._router(payload)
        router.complete([{"role": "user", "content": "torch on"}], TOOLS)
        self.assertEqual(self.cloud.calls, 1)

    def test_sensitive_tool_never_takes_the_fast_path(self):
        """Even at confidence 1.0, SMS is not fast-path eligible."""
        router = self._router(_call("send_sms", {"number": "1", "message": "x"}, 1.0))
        router.complete([{"role": "user", "content": "text mum"}], TOOLS)
        self.assertEqual(self.cloud.calls, 1)

    def test_undeclared_tool_escalates(self):
        router = self._router(_call("get_battery_status", {}, 0.99))
        router.complete([{"role": "user", "content": "battery"}], TOOLS)
        self.assertEqual(self.cloud.calls, 1)

    def test_no_call_escalates(self):
        router = self._router({"type": "respond", "confidence": 0.9})
        router.complete([{"role": "user", "content": "how are you"}], TOOLS)
        self.assertEqual(self.cloud.calls, 1)

    def test_multi_step_escalates(self):
        payload = _call("set_torch", {"on": True}, 0.99)
        payload["function_calls"].append({"name": "set_torch", "arguments": {"on": False}})
        router = self._router(payload)
        router.complete([{"role": "user", "content": "blink"}], TOOLS)
        self.assertEqual(self.cloud.calls, 1)

    def test_needle_crash_escalates(self):
        def boom():
            raise RuntimeError("engine missing")

        router = self._router(boom)
        response = router.complete([{"role": "user", "content": "torch"}], TOOLS)
        self.assertEqual(response.text, "cloud answer")

    def test_disabled_router_always_uses_cloud(self):
        cloud = FakeCloud()
        router = NeedleRouter(cloud=cloud, agent=None)
        router.complete([{"role": "user", "content": "torch"}], TOOLS)
        self.assertEqual(cloud.calls, 1)
        self.assertFalse(router.enabled)

    def test_followup_turns_go_to_cloud(self):
        router = self._router(_call("set_torch", {"on": True}, 0.99))
        messages = [
            {"role": "user", "content": "torch"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "other", "content": "{}"},
        ]
        router.complete(messages, TOOLS)
        self.assertEqual(self.needle.calls, 0)
        self.assertEqual(self.cloud.calls, 1)


class OfflineLoopTests(unittest.TestCase):
    """A simple command should complete with zero cloud calls."""

    def test_full_offline_round_trip(self):
        cloud = FakeCloud()
        needle = FakeNeedle(_call("set_torch", {"on": True}, 0.97))
        router = NeedleRouter(cloud=cloud, agent=needle)

        registry = ToolRegistry(
            [
                ToolSpec(
                    name="set_torch",
                    description="Turn the torch on or off.",
                    input_schema={
                        "type": "object",
                        "properties": {"on": {"type": "boolean"}},
                        "required": ["on"],
                        "additionalProperties": False,
                    },
                    risk=Risk.REVERSIBLE,
                    handler=lambda ctx, args: ToolResult.ok("Torch is now on."),
                )
            ]
        )
        runtime = AgentRuntime(
            planner=router,
            registry=registry,
            policy=DefaultPolicy("1"),
            system_prompt="test",
        )
        outcome = runtime.run("turn on the torch", actor_id="1", chat_id=1)

        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.text, "Torch is now on.")
        self.assertEqual(cloud.calls, 0, "simple command must not touch the network")

    def test_policy_still_governs_needle_proposals(self):
        """Confidence must not bypass authorization."""
        cloud = FakeCloud()
        needle = FakeNeedle(_call("set_torch", {"on": True}, 1.0))
        router = NeedleRouter(cloud=cloud, agent=needle)
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="set_torch",
                    description="Torch.",
                    input_schema={
                        "type": "object",
                        "properties": {"on": {"type": "boolean"}},
                        "required": ["on"],
                        "additionalProperties": False,
                    },
                    risk=Risk.REVERSIBLE,
                    handler=lambda ctx, args: ToolResult.ok("on"),
                )
            ]
        )
        runtime = AgentRuntime(
            planner=router,
            registry=registry,
            policy=DefaultPolicy("owner-1"),
            system_prompt="test",
        )
        # A non-owner actor must be denied even at confidence 1.0.
        outcome = runtime.run("torch on", actor_id="intruder", chat_id=1)
        statuses = [result.status for result in outcome.tool_results]
        self.assertIn("denied", statuses)

    def test_invalid_arguments_are_rejected(self):
        """Needle output is untrusted; the schema still applies."""
        cloud = FakeCloud()
        needle = FakeNeedle(_call("set_torch", {"on": "yes please"}, 0.99))
        router = NeedleRouter(cloud=cloud, agent=needle)
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="set_torch",
                    description="Torch.",
                    input_schema={
                        "type": "object",
                        "properties": {"on": {"type": "boolean"}},
                        "required": ["on"],
                        "additionalProperties": False,
                    },
                    risk=Risk.REVERSIBLE,
                    handler=lambda ctx, args: ToolResult.ok("on"),
                )
            ]
        )
        runtime = AgentRuntime(
            planner=router, registry=registry,
            policy=DefaultPolicy("1"), system_prompt="test",
        )
        outcome = runtime.run("torch on", actor_id="1", chat_id=1)
        codes = [r.error_code for r in outcome.tool_results]
        self.assertIn("invalid_arguments", codes)


class AllowlistTests(unittest.TestCase):
    def test_allowlist_excludes_every_dangerous_tool(self):
        forbidden = {
            "send_sms", "place_phone_call", "get_location", "get_recent_sms",
            "search_contacts", "get_notifications", "capture_photo",
            "capture_screenshot", "get_clipboard", "read_text_file",
            "tap_screen", "swipe_screen", "type_text", "send_key_event",
            "set_wallpaper", "force_stop_app", "set_wifi",
        }
        self.assertEqual(DEFAULT_FAST_PATH_TOOLS & forbidden, set())

    def test_allowlist_matches_real_registry_tools(self):
        from android_agent.tools.catalog import build_full_registry

        registry = build_full_registry()
        for name in DEFAULT_FAST_PATH_TOOLS:
            self.assertIsNotNone(registry.get(name), f"{name} is not a real tool")

    def test_allowlist_contains_only_low_risk_tools(self):
        from android_agent.tools.catalog import build_full_registry

        registry = build_full_registry()
        for name in DEFAULT_FAST_PATH_TOOLS:
            tool = registry.require(name)
            self.assertIn(
                tool.risk,
                (Risk.READ_ONLY, Risk.REVERSIBLE),
                f"{name} is {tool.risk.value}; too risky for an unattended fast path",
            )


if __name__ == "__main__":
    unittest.main()
