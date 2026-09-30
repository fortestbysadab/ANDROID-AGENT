"""Regression tests for provider schema compatibility.

These cover the bug that made every Gemini request fail with HTTP 400:
`Unknown name "additionalProperties" at tools[0].function_declarations[0]`.
"""

from __future__ import annotations

import json
import unittest

from android_agent.models.compat import (
    Dialect,
    adapt_messages,
    adapt_tools,
    normalize_base_url,
)
from android_agent.tools.catalog import build_full_registry


class DialectDetectionTests(unittest.TestCase):
    def test_detects_gemini_endpoint(self):
        self.assertIs(
            Dialect.detect("https://generativelanguage.googleapis.com/v1beta/openai"),
            Dialect.GEMINI,
        )

    def test_detects_openai_endpoint(self):
        self.assertIs(Dialect.detect("https://api.openai.com/v1"), Dialect.OPENAI)

    def test_local_server_is_openai(self):
        self.assertIs(Dialect.detect("http://127.0.0.1:8000/v1"), Dialect.OPENAI)

    def test_hostname_matching_is_not_substring_spoofable(self):
        self.assertIs(
            Dialect.detect("https://generativelanguage.googleapis.com.evil.test/v1"),
            Dialect.OPENAI,
        )


class BaseUrlTests(unittest.TestCase):
    def test_repairs_missing_openai_suffix(self):
        self.assertEqual(
            normalize_base_url(
                "https://generativelanguage.googleapis.com/v1beta", Dialect.GEMINI
            ),
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )

    def test_leaves_correct_url_alone(self):
        url = "https://generativelanguage.googleapis.com/v1beta/openai"
        self.assertEqual(normalize_base_url(url, Dialect.GEMINI), url)

    def test_openai_url_untouched(self):
        self.assertEqual(
            normalize_base_url("https://api.openai.com/v1/", Dialect.OPENAI),
            "https://api.openai.com/v1",
        )


class GeminiSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schemas = build_full_registry().model_schemas()

    def test_no_unsupported_keyword_reaches_gemini(self):
        wire = adapt_tools(self.schemas, Dialect.GEMINI)
        encoded = json.dumps(wire)
        for keyword in ("additionalProperties", "patternProperties", "$schema", "const"):
            self.assertNotIn(keyword, encoded, f"{keyword} must be stripped for Gemini")

    def test_parameterless_tools_omit_parameters(self):
        wire = adapt_tools(self.schemas, Dialect.GEMINI)
        battery = next(t for t in wire if t["function"]["name"] == "get_battery_status")
        self.assertNotIn(
            "parameters", battery["function"], "Gemini rejects an empty properties object"
        )

    def test_constraints_are_preserved(self):
        wire = adapt_tools(self.schemas, Dialect.GEMINI)
        sms = next(t for t in wire if t["function"]["name"] == "send_sms")
        params = sms["function"]["parameters"]
        self.assertEqual(sorted(params["required"]), ["message", "number"])
        self.assertEqual(params["properties"]["message"]["maxLength"], 1600)
        self.assertEqual(params["type"], "object")

    def test_openai_dialect_keeps_strict_schema(self):
        wire = adapt_tools(self.schemas, Dialect.OPENAI)
        sms = next(t for t in wire if t["function"]["name"] == "send_sms")
        self.assertIs(sms["function"]["parameters"]["additionalProperties"], False)

    def test_adaptation_does_not_mutate_the_registry(self):
        before = json.dumps(self.schemas, sort_keys=True)
        adapt_tools(self.schemas, Dialect.GEMINI)
        self.assertEqual(before, json.dumps(self.schemas, sort_keys=True))

    def test_local_validation_stays_strict_after_adaptation(self):
        """Stripping the wire schema must not weaken the security boundary."""
        from android_agent.tools.base import SchemaValidationError

        registry = build_full_registry()
        adapt_tools(registry.model_schemas(), Dialect.GEMINI)
        tool = registry.require("send_sms")
        with self.assertRaises(SchemaValidationError):
            tool.validate({"number": "123", "message": "hi", "injected": "payload"})


class MessageAdaptationTests(unittest.TestCase):
    def test_null_assistant_content_removed_when_tool_calls_present(self):
        messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]}]
        adapted = adapt_messages(messages, Dialect.GEMINI)
        self.assertNotIn("content", adapted[0])

    def test_null_assistant_content_becomes_empty_string(self):
        adapted = adapt_messages([{"role": "assistant", "content": None}], Dialect.GEMINI)
        self.assertEqual(adapted[0]["content"], "")

    def test_tool_name_field_dropped_for_gemini(self):
        messages = [{"role": "tool", "tool_call_id": "1", "name": "x", "content": "{}"}]
        adapted = adapt_messages(messages, Dialect.GEMINI)
        self.assertNotIn("name", adapted[0])
        self.assertEqual(adapted[0]["tool_call_id"], "1")

    def test_openai_messages_pass_through(self):
        messages = [{"role": "tool", "tool_call_id": "1", "name": "x", "content": "{}"}]
        self.assertEqual(adapt_messages(messages, Dialect.OPENAI), messages)


if __name__ == "__main__":
    unittest.main()
