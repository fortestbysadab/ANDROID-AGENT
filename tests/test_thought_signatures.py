"""Regression tests for Gemini 3 thought-signature round-tripping.

Gemini 3 thinking models return an opaque `thought_signature` on each tool
call and reject the follow-up turn unless it is replayed verbatim:

    400 Function call is missing a thought_signature in functionCall parts.

On the OpenAI-compatible wire the value lives at
`tool_calls[].extra_content.google.thought_signature`.
"""

from __future__ import annotations

import io
import json
import logging
import unittest
import urllib.error
from unittest import mock

from android_agent.agent.runtime import AgentRuntime, RunStatus, _assistant_message
from android_agent.models.base import PlannerResponse, ToolCall
from android_agent.models.compat import (
    UNSIGNED_PLACEHOLDER,
    Dialect,
    adapt_messages,
)
from android_agent.models.openai_compatible import OpenAICompatiblePlanner
from android_agent.policy.engine import DefaultPolicy
from android_agent.tools.base import Risk, ToolResult, ToolSpec
from android_agent.tools.registry import ToolRegistry

SIGNATURE = "EpoECpcEARFNMg_fake_opaque_signature"
NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _missing_signature_error(
    message: str = (
        "Function call is missing a thought_signature in functionCall parts. "
        "This is required for tools to work correctly."
    ),
) -> urllib.error.HTTPError:
    """The exact 400 Gemini 3 returns when a signature is absent or wrong."""
    return urllib.error.HTTPError(
        "u",
        400,
        "e",
        None,
        io.BytesIO(json.dumps({"error": {"code": 400, "message": message}}).encode()),
    )


def _battery_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="get_battery_status",
                description="Read the battery level.",
                input_schema=NO_ARGS,
                risk=Risk.READ_ONLY,
                handler=lambda ctx, args: ToolResult.ok("Battery 43%", {"percentage": 43}),
            )
        ]
    )


class CaptureTests(unittest.TestCase):
    def test_planner_captures_thought_signature(self):
        envelope = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "extra_content": {"google": {"thought_signature": SIGNATURE}},
                                "function": {"name": "get_battery_status", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
        stream = io.BytesIO(json.dumps(envelope).encode())
        context = mock.MagicMock()
        context.__enter__.return_value = stream
        context.__exit__.return_value = False

        planner = OpenAICompatiblePlanner(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-3.5-flash-lite",
            api_key="k",
            max_retries=1,
        )
        with mock.patch("urllib.request.urlopen", return_value=context):
            response = planner.complete([{"role": "user", "content": "battery?"}], [])

        call = response.tool_calls[0]
        self.assertEqual(
            call.extra_content["google"]["thought_signature"], SIGNATURE
        )

    def test_missing_extra_content_is_empty_not_none(self):
        call = ToolCall("c1", "get_battery_status", {})
        self.assertEqual(call.extra_content, {})


class ReplayTests(unittest.TestCase):
    def test_assistant_message_replays_signature(self):
        response = PlannerResponse(
            None,
            (
                ToolCall(
                    "c1",
                    "get_battery_status",
                    {},
                    extra_content={"google": {"thought_signature": SIGNATURE}},
                ),
            ),
        )
        message = _assistant_message(response)
        self.assertEqual(
            message["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
            SIGNATURE,
        )

    def test_no_extra_content_key_when_absent(self):
        response = PlannerResponse(None, (ToolCall("c1", "get_battery_status", {}),))
        self.assertNotIn("extra_content", _assistant_message(response)["tool_calls"][0])

    def test_openai_dialect_strips_extra_content(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "extra_content": {"google": {"thought_signature": SIGNATURE}},
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            }
        ]
        adapted = adapt_messages(messages, Dialect.OPENAI)
        self.assertNotIn("extra_content", adapted[0]["tool_calls"][0])

    def test_gemini_dialect_preserves_signature(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "extra_content": {"google": {"thought_signature": SIGNATURE}},
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            }
        ]
        adapted = adapt_messages(messages, Dialect.GEMINI)
        self.assertEqual(
            adapted[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
            SIGNATURE,
        )

    def test_unsigned_history_gets_placeholder(self):
        """Unsignable history must not hard-fail the run."""
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}],
            }
        ]
        adapted = adapt_messages(messages, Dialect.GEMINI)
        self.assertEqual(
            adapted[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
            UNSIGNED_PLACEHOLDER,
        )

    def test_parallel_calls_only_need_leading_signature(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "extra_content": {"google": {"thought_signature": SIGNATURE}},
                        "function": {"name": "a", "arguments": "{}"},
                    },
                    {"id": "c2", "function": {"name": "b", "arguments": "{}"}},
                ],
            }
        ]
        adapted = adapt_messages(messages, Dialect.GEMINI)
        calls = adapted[0]["tool_calls"]
        self.assertEqual(
            calls[0]["extra_content"]["google"]["thought_signature"], SIGNATURE
        )
        self.assertNotIn("extra_content", calls[1])


class EndToEndLoopTests(unittest.TestCase):
    """Drive the real runtime against a server enforcing Gemini's rule."""

    def _run(self, capture_signatures: bool):
        turns: list[dict] = []

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data.decode())
            turns.append(body)

            for message in body["messages"]:
                if message.get("role") != "assistant":
                    continue
                for call in message.get("tool_calls") or []:
                    signature = (
                        (call.get("extra_content") or {})
                        .get("google", {})
                        .get("thought_signature")
                    )
                    if not signature:
                        raise _missing_signature_error()
                    # The real API validates the signature it issued; a
                    # placeholder is only accepted for history it never signed.
                    if capture_signatures and signature != SIGNATURE:
                        raise _missing_signature_error(
                            "Invalid thought signature: expected the value "
                            "returned on the previous turn."
                        )

            has_tool_result = any(m.get("role") == "tool" for m in body["messages"])
            if has_tool_result:
                envelope = {
                    "choices": [{"message": {"content": "Your battery is at 43%."}}]
                }
            else:
                call = {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_battery_status", "arguments": "{}"},
                }
                if capture_signatures:
                    call["extra_content"] = {"google": {"thought_signature": SIGNATURE}}
                envelope = {
                    "choices": [{"message": {"content": None, "tool_calls": [call]}}]
                }

            stream = io.BytesIO(json.dumps(envelope).encode())
            context = mock.MagicMock()
            context.__enter__.return_value = stream
            context.__exit__.return_value = False
            return context

        runtime = AgentRuntime(
            planner=OpenAICompatiblePlanner(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                model="gemini-3.5-flash-lite",
                api_key="k",
                max_retries=1,
            ),
            registry=_battery_registry(),
            policy=DefaultPolicy("1"),
            system_prompt="test",
        )
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            outcome = runtime.run("how much battery charge", actor_id="1", chat_id=1)
        return outcome, turns

    def test_two_turn_loop_succeeds_with_signature(self):
        outcome, turns = self._run(capture_signatures=True)
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.text, "Your battery is at 43%.")
        self.assertEqual(len(turns), 2, "expected a tool turn then a final turn")
        self.assertEqual(outcome.tool_results[0].data["percentage"], 43)

    def test_placeholder_rescues_a_response_with_no_signature(self):
        """Even if the provider omits the signature, the loop must not 400."""
        outcome, _ = self._run(capture_signatures=False)
        self.assertIs(outcome.status, RunStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
