"""Planner adapter behaviour: error classification, retries, parsing."""

from __future__ import annotations

import io
import json
import logging
import unittest
import urllib.error
from unittest import mock

from android_agent.models.compat import Dialect
from android_agent.models.openai_compatible import (
    OpenAICompatiblePlanner,
    PlannerAuthError,
    PlannerModelError,
    PlannerProtocolError,
    PlannerRateLimitError,
    PlannerSchemaError,
)


def setUpModule():
    """Silence expected error logs; these tests deliberately trigger failures."""
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _http_error(code: int, body: dict | str):
    payload = json.dumps(body) if isinstance(body, dict) else body
    return urllib.error.HTTPError(
        url="https://example.test/chat/completions",
        code=code,
        msg="error",
        hdrs=None,
        fp=io.BytesIO(payload.encode()),
    )


def _ok_response(body: dict):
    stream = io.BytesIO(json.dumps(body).encode())
    context = mock.MagicMock()
    context.__enter__.return_value = stream
    context.__exit__.return_value = False
    return context


class ErrorClassificationTests(unittest.TestCase):
    def setUp(self):
        self.planner = OpenAICompatiblePlanner(
            base_url="https://example.test/v1", model="m", api_key="k", max_retries=1
        )

    def _raise(self, error):
        with mock.patch("urllib.request.urlopen", side_effect=error):
            return self.planner.complete([{"role": "user", "content": "hi"}], [])

    def test_401_is_auth_error(self):
        with self.assertRaises(PlannerAuthError):
            self._raise(_http_error(401, {"error": "bad key"}))

    def test_404_is_model_error(self):
        with self.assertRaises(PlannerModelError):
            self._raise(_http_error(404, {"error": "model not found"}))

    def test_429_is_rate_limit(self):
        with self.assertRaises(PlannerRateLimitError):
            self._raise(_http_error(429, {"error": "quota"}))

    def test_gemini_schema_rejection_is_schema_error(self):
        body = {
            "error": {
                "code": 400,
                "message": (
                    'Invalid JSON payload received. Unknown name "additionalProperties" '
                    "at 'tools[0].function_declarations[0].parameters': Cannot find field."
                ),
            }
        }
        with self.assertRaises(PlannerSchemaError) as ctx:
            self._raise(_http_error(400, body))
        self.assertIn("additionalProperties", str(ctx.exception))

    def test_errors_carry_a_remedy(self):
        with self.assertRaises(PlannerAuthError) as ctx:
            self._raise(_http_error(403, "forbidden"))
        self.assertTrue(ctx.exception.remedy)

    def test_api_key_never_appears_in_error_text(self):
        with self.assertRaises(PlannerAuthError) as ctx:
            self._raise(_http_error(401, "denied"))
        self.assertNotIn("k", str(ctx.exception).replace("key", "").replace("check", ""))


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.planner = OpenAICompatiblePlanner(
            base_url="https://example.test/v1", model="m", max_retries=1
        )

    def _complete(self, envelope):
        with mock.patch("urllib.request.urlopen", return_value=_ok_response(envelope)):
            return self.planner.complete([{"role": "user", "content": "hi"}], [])

    def test_parses_text_reply(self):
        response = self._complete(
            {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        )
        self.assertEqual(response.text, "hello")
        self.assertEqual(response.tool_calls, ())

    def test_parses_tool_call(self):
        response = self._complete(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "get_battery_status",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "get_battery_status")

    def test_accepts_dict_arguments_from_lenient_providers(self):
        response = self._complete(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "set_torch", "arguments": {"on": True}}}
                            ]
                        }
                    }
                ]
            }
        )
        self.assertEqual(response.tool_calls[0].arguments, {"on": True})

    def test_flattens_content_parts(self):
        response = self._complete(
            {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]}
        )
        self.assertEqual(response.text, "hi")

    def test_rejects_invalid_envelope(self):
        with self.assertRaises(PlannerProtocolError):
            self._complete({"nonsense": True})

    def test_provider_level_error_object(self):
        with self.assertRaises(PlannerModelError):
            self._complete({"error": {"message": "overloaded"}})


class RetryTests(unittest.TestCase):
    def test_rate_limit_is_retried_then_succeeds(self):
        planner = OpenAICompatiblePlanner(
            base_url="https://example.test/v1", model="m", max_retries=3
        )
        good = {"choices": [{"message": {"content": "ok"}}]}
        attempts = [
            _http_error(429, "slow down"),
            _ok_response(good),
        ]

        def side_effect(*args, **kwargs):
            item = attempts.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch("urllib.request.urlopen", side_effect=side_effect), mock.patch(
            "time.sleep"
        ):
            response = planner.complete([{"role": "user", "content": "hi"}], [])
        self.assertEqual(response.text, "ok")

    def test_auth_error_is_not_retried(self):
        planner = OpenAICompatiblePlanner(
            base_url="https://example.test/v1", model="m", max_retries=3
        )
        with mock.patch(
            "urllib.request.urlopen", side_effect=_http_error(401, "nope")
        ) as urlopen, mock.patch("time.sleep"):
            with self.assertRaises(PlannerAuthError):
                planner.complete([{"role": "user", "content": "hi"}], [])
        self.assertEqual(urlopen.call_count, 1)


class GeminiWireTests(unittest.TestCase):
    def test_gemini_payload_is_sanitised_end_to_end(self):
        """The exact request body sent to Gemini must be free of bad keywords."""
        from android_agent.tools.catalog import build_full_registry

        planner = OpenAICompatiblePlanner(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-2.5-flash",
            api_key="secret",
            max_retries=1,
        )
        self.assertIs(planner.dialect, Dialect.GEMINI)

        captured = {}

        def capture(request, timeout=None):
            captured["body"] = request.data.decode()
            return _ok_response({"choices": [{"message": {"content": "ok"}}]})

        with mock.patch("urllib.request.urlopen", side_effect=capture):
            planner.complete(
                [{"role": "user", "content": "battery?"}],
                build_full_registry().model_schemas(),
            )

        self.assertNotIn("additionalProperties", captured["body"])
        self.assertIn("gemini-2.5-flash", captured["body"])


if __name__ == "__main__":
    unittest.main()
