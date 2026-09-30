"""Phone call placement.

The original implementation treated exit code 0 as success. Termux exits 0
while doing nothing when Termux:API lacks the Phone permission or when
Android blocks a background app from starting the dialer, so the agent
reported a call it had not placed.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from android_agent.tools import termux_extra
from android_agent.tools.base import SchemaValidationError, ToolContext
from android_agent.tools.catalog import build_full_registry

CONTEXT = ToolContext("1", 1, "run")


def _device_info(state):
    return json.dumps({"call_state": state})


class NumberValidationTests(unittest.TestCase):
    def setUp(self):
        self.tool = build_full_registry().require("place_phone_call")

    def test_accepts_real_formats(self):
        for number in ["+919876543210", "9876543210", "+1 (555) 123-4567", "98765-43210"]:
            self.assertEqual(self.tool.validate({"number": number})["number"], number)

    def test_rejects_non_numbers(self):
        for bad in ["abc", "'; rm -rf /", "+", "<script>", "", "tel:911"]:
            with self.assertRaises(SchemaValidationError):
                self.tool.validate({"number": bad})

    def test_rejects_unknown_fields(self):
        with self.assertRaises(SchemaValidationError):
            self.tool.validate({"number": "911", "silent": True})

    def test_sms_number_is_validated_too(self):
        sms = build_full_registry().require("send_sms")
        with self.assertRaises(SchemaValidationError):
            sms.validate({"number": "not a number", "message": "hi"})


class NormalisationTests(unittest.TestCase):
    def test_strips_formatting_but_keeps_plus(self):
        self.assertEqual(termux_extra._normalise_number("+1 (555) 123-4567"), "+15551234567")
        self.assertEqual(termux_extra._normalise_number(" 98765 43210 "), "9876543210")


class PlaceCallTests(unittest.TestCase):
    def _run(self, call_result, device_info=None):
        def fake_run(args, timeout=12.0):
            if args[0] == "termux-telephony-call":
                return call_result
            if args[0] == "termux-telephony-deviceinfo":
                return device_info if device_info else (False, "unavailable")
            return (False, "unexpected")

        with mock.patch.object(termux_extra, "_run", side_effect=fake_run):
            return termux_extra._place_call(CONTEXT, {"number": "+919876543210"})

    def test_confirmed_call_reports_ok(self):
        result = self._run((True, ""), (True, _device_info("OFFHOOK")))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["confirmed"])

    def test_silent_failure_is_reported_as_an_error(self):
        """Exit 0 with the radio still idle means nothing happened."""
        result = self._run((True, ""), (True, _device_info("IDLE")))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "call_not_started")
        self.assertIn("foreground", result.summary)

    def test_unverifiable_call_is_reported_honestly(self):
        result = self._run((True, ""), (False, "no info"))
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.data["confirmed"])
        self.assertIn("could not confirm", result.summary)

    def test_permission_denied_gives_actionable_advice(self):
        result = self._run((False, "java.lang.SecurityException: Permission Denial"))
        self.assertEqual(result.error_code, "call_permission_denied")
        self.assertIn("Termux:API", result.summary)

    def test_missing_binary_is_explained(self):
        result = self._run((False, "termux-telephony-call is not installed"))
        self.assertEqual(result.error_code, "termux_api_missing")
        self.assertIn("pkg install termux-api", result.summary)

    def test_timeout_blames_the_background_restriction(self):
        result = self._run((False, "termux-telephony-call timed out"))
        self.assertEqual(result.error_code, "call_timeout")
        self.assertTrue(result.retryable)

    def test_number_is_passed_as_an_argv_element(self):
        seen = {}

        def fake_run(args, timeout=12.0):
            if args[0] == "termux-telephony-call":
                seen["args"] = args
                return (True, "")
            return (True, _device_info("OFFHOOK"))

        with mock.patch.object(termux_extra, "_run", side_effect=fake_run):
            termux_extra._place_call(CONTEXT, {"number": "+1 (555) 123-4567"})
        self.assertEqual(seen["args"], ["termux-telephony-call", "+15551234567"])


class PolicyTests(unittest.TestCase):
    def test_calling_still_requires_approval(self):
        from android_agent.policy.engine import DefaultPolicy, PolicyDecision

        tool = build_full_registry().require("place_phone_call")
        decision = DefaultPolicy("1").evaluate(CONTEXT, tool, {"number": "911"})
        self.assertIs(decision.decision, PolicyDecision.REQUIRE_APPROVAL)


if __name__ == "__main__":
    unittest.main()
