"""Tests for the ADB-backed UI control tools.

These are the tools that replace the legacy bot's /keyevent, /tap, /swipe and
/inputtext commands. The parity test below exists so a key the owner used to
rely on cannot quietly disappear in a refactor.
"""

from __future__ import annotations

import unittest
from unittest import mock

from android_agent.tools import adb as adbmod
from android_agent.tools.base import Risk
from android_agent.tools.catalog import build_full_registry

#: The exact map the legacy bot exposed as /keyevent [name].
LEGACY_KEYEVENTS = {
    "home": "3", "back": "4", "recents": "187", "power": "26",
    "volup": "24", "voldown": "25", "camera": "27", "menu": "82",
    "enter": "66", "del": "67", "play": "126", "pause": "127",
}
#: Names the v2 tool deliberately spells out in full.
RENAMED = {"volup": "volume_up", "voldown": "volume_down", "del": "delete"}


def spec(name):
    return build_full_registry().get(name)


def key_enum():
    return spec("send_key_event").input_schema["properties"]["key"]["enum"]


class KeyEventParityTests(unittest.TestCase):
    def test_every_legacy_key_is_still_reachable(self):
        available = set(key_enum())
        for legacy in LEGACY_KEYEVENTS:
            with self.subTest(key=legacy):
                self.assertIn(RENAMED.get(legacy, legacy), available)

    def test_navigation_keys_are_present(self):
        for key in ("home", "back", "recents"):
            with self.subTest(key=key):
                self.assertIn(key, key_enum())

    def test_key_codes_match_the_android_constants(self):
        sent = {}

        def fake_shell(arguments, timeout=15):
            sent["args"] = arguments
            return True, ""

        registry = build_full_registry()
        handler = registry.get("send_key_event").handler
        for legacy, code in LEGACY_KEYEVENTS.items():
            name = RENAMED.get(legacy, legacy)
            with self.subTest(key=name):
                with mock.patch.object(adbmod, "_shell", fake_shell):
                    result = handler(None, {"key": name})
                self.assertEqual(result.status, "ok")
                self.assertEqual(sent["args"], ["input", "keyevent", code])

    def test_unknown_keys_are_rejected_by_the_schema(self):
        self.assertNotIn("selfdestruct", key_enum())
        self.assertNotIn("3", key_enum())

    def test_raw_numeric_codes_are_not_accepted(self):
        """The legacy bot allowed any integer; that is a much wider surface."""
        schema = spec("send_key_event").input_schema
        self.assertEqual(schema["properties"]["key"]["type"], "string")
        self.assertFalse(schema.get("additionalProperties", True))


class UiControlRiskTests(unittest.TestCase):
    """Screen control is raw control and must stay behind approval."""

    def test_ui_control_tools_are_raw_control(self):
        for name in ("send_key_event", "tap_screen", "swipe_screen", "type_text"):
            with self.subTest(tool=name):
                self.assertEqual(spec(name).risk, Risk.RAW_CONTROL)

    def test_screenshot_is_a_sensitive_read(self):
        self.assertEqual(spec("capture_screenshot").risk, Risk.SENSITIVE_READ)

    def test_status_and_inspection_tools_stay_read_only(self):
        for name in ("get_adb_status", "get_current_app", "list_installed_apps"):
            with self.subTest(tool=name):
                self.assertEqual(spec(name).risk, Risk.READ_ONLY)

    def test_descriptions_say_adb_is_required(self):
        for name in ("send_key_event", "tap_screen", "swipe_screen", "type_text"):
            with self.subTest(tool=name):
                self.assertIn("ADB", spec(name).description)


class CoordinateBoundsTests(unittest.TestCase):
    def test_tap_coordinates_are_bounded(self):
        properties = spec("tap_screen").input_schema["properties"]
        for axis in ("x", "y"):
            with self.subTest(axis=axis):
                self.assertEqual(properties[axis]["minimum"], 0)
                self.assertLessEqual(properties[axis]["maximum"], 10000)

    def test_typed_text_is_length_capped(self):
        text = spec("type_text").input_schema["properties"]["text"]
        self.assertGreaterEqual(text["minLength"], 1)
        self.assertLessEqual(text["maxLength"], 1000)


if __name__ == "__main__":
    unittest.main()
