"""Tests for get_location.

Every case here is a failure mode reported against termux-location upstream:
exit 0 with empty output, an API_ERROR object instead of coordinates, a cold
GPS start that takes longer than any sane timeout, and a missing permission.
The old implementation turned all of them into one unhelpful error.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from android_agent.tools import termux_extra
from android_agent.tools.catalog import build_full_registry

FIX = {
    "latitude": 22.5726,
    "longitude": 88.3639,
    "accuracy": 18.0,
    "altitude": 9.0,
    "bearing": 0.0,
    "speed": 0.0,
    "elapsedMs": 12,
    "provider": "network",
}


def spec():
    return build_full_registry().get("get_location")


def run(arguments=None):
    return termux_extra._location(None, arguments or {})


class FakeTermux:
    """Stands in for _run, keyed by (provider, request)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, args, timeout=12.0):
        provider, request = args[2], args[4]
        self.calls.append((provider, request, timeout))
        reply = self.responses.get((provider, request), (True, ""))
        return reply(self) if callable(reply) else reply


def patched(responses):
    return mock.patch.object(termux_extra, "_run", FakeTermux(responses))


class LocationSuccessTests(unittest.TestCase):
    def test_fresh_network_fix_is_returned(self):
        with patched({("network", "once"): (True, json.dumps(FIX))}) as fake:
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.data["latitude"], 22.5726)
        self.assertEqual(result.data["provider"], "network")
        self.assertFalse(result.data["stale"])
        self.assertEqual(fake.calls[0][0], "network")

    def test_summary_is_human_readable(self):
        with patched({("network", "once"): (True, json.dumps(FIX))}):
            result = run()
        self.assertIn("22.57260", result.summary)
        self.assertIn("18 m", result.summary)
        self.assertIn("network", result.summary)

    def test_requested_provider_is_tried_first(self):
        gps = dict(FIX, provider="gps")
        with patched({("gps", "once"): (True, json.dumps(gps))}) as fake:
            result = run({"provider": "gps"})
        self.assertEqual(result.data["provider"], "gps")
        self.assertEqual(fake.calls[0][0], "gps")

    def test_gps_gets_a_longer_budget_than_network(self):
        with patched({("gps", "once"): (True, json.dumps(FIX))}) as fake:
            run({"provider": "gps"})
        gps_timeout = fake.calls[0][2]
        with patched({("network", "once"): (True, json.dumps(FIX))}) as fake:
            run({"provider": "network"})
        self.assertGreater(gps_timeout, fake.calls[0][2])

    def test_tool_timeout_exceeds_the_worst_case_provider_budget(self):
        """A tool killed by the runtime mid-fix would waste the whole wait."""
        worst = max(termux_extra._LOCATION_ONCE_TIMEOUTS.values())
        self.assertGreater(spec().timeout_seconds, worst)


class LocationFallbackTests(unittest.TestCase):
    def test_falls_back_to_the_other_provider(self):
        with patched({
            ("network", "once"): (False, "termux-location timed out"),
            ("gps", "once"): (True, json.dumps(dict(FIX, provider="gps"))),
        }) as fake:
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["provider"], "gps")
        self.assertEqual([c[0] for c in fake.calls], ["network", "gps"])

    def test_falls_back_to_a_cached_fix_and_flags_it_stale(self):
        cached = dict(FIX, elapsedMs=15 * 60 * 1000)
        with patched({
            ("network", "once"): (True, ""),
            ("gps", "once"): (True, ""),
            ("network", "last"): (True, json.dumps(cached)),
        }) as fake:
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["stale"])
        self.assertEqual(result.data["fix_age_seconds"], 900.0)
        self.assertIn("last known fix", result.summary)
        self.assertIn("15 min old", result.summary)
        self.assertIn(("network", "last", termux_extra._LOCATION_LAST_TIMEOUT), fake.calls)

    def test_a_recent_cached_fix_is_not_called_stale_in_the_summary(self):
        cached = dict(FIX, elapsedMs=5000)
        with patched({
            ("network", "once"): (True, ""),
            ("gps", "once"): (True, ""),
            ("network", "last"): (True, json.dumps(cached)),
        }):
            result = run()
        self.assertFalse(result.data["stale"])
        self.assertNotIn("last known", result.summary)

    def test_fresh_fix_is_preferred_over_cache(self):
        with patched({
            ("network", "once"): (True, json.dumps(FIX)),
            ("network", "last"): (True, json.dumps(dict(FIX, latitude=0.0))),
        }) as fake:
            result = run()
        self.assertAlmostEqual(result.data["latitude"], 22.5726)
        self.assertNotIn("last", [c[1] for c in fake.calls])


class LocationFailureTests(unittest.TestCase):
    """Upstream termux-location lies about success in several ways."""

    def test_empty_output_with_exit_zero_is_not_treated_as_data(self):
        with patched({}):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "location_unavailable")
        self.assertIn("no fix returned", result.summary)

    def test_api_error_object_is_surfaced(self):
        with patched({
            ("network", "once"): (True, '{"API_ERROR": "Failed to get location"}'),
            ("gps", "once"): (True, '{"API_ERROR": "Failed to get location"}'),
        }):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertIn("Failed to get location", result.summary)

    def test_json_without_coordinates_is_rejected(self):
        with patched({("network", "once"): (True, '{"provider": "network"}')}):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertIn("no coordinates", result.summary)

    def test_unparseable_output_is_rejected(self):
        with patched({("network", "once"): (True, "not json at all")}):
            result = run()
        self.assertEqual(result.status, "error")
        self.assertIn("unreadable", result.summary)

    def test_error_explains_permissions_and_indoor_gps(self):
        with patched({}):
            result = run()
        self.assertIn("Location permission", result.summary)
        self.assertIn("Termux:API", result.summary)
        self.assertIn("indoors", result.summary.lower())

    def test_error_is_retryable(self):
        with patched({}):
            self.assertTrue(run().retryable)

    def test_missing_binary_is_reported_clearly(self):
        with patched({
            ("network", "once"): (False, "termux-location is not installed"),
            ("gps", "once"): (False, "termux-location is not installed"),
        }):
            result = run()
        self.assertIn("not installed", result.summary)

    def test_every_provider_is_attempted_before_giving_up(self):
        with patched({}) as fake:
            run()
        attempted = {(provider, request) for provider, request, _ in fake.calls}
        self.assertIn(("network", "once"), attempted)
        self.assertIn(("gps", "once"), attempted)
        self.assertIn(("passive", "last"), attempted)


class LocationSchemaTests(unittest.TestCase):
    def test_provider_enum_is_advertised_to_the_model(self):
        properties = spec().input_schema["properties"]
        self.assertEqual(
            set(properties["provider"]["enum"]), {"network", "gps", "passive"}
        )

    def test_description_warns_the_model_that_gps_is_slow(self):
        description = spec().description.lower()
        self.assertIn("network", description)
        self.assertIn("can take a minute", description)

    def test_location_stays_a_sensitive_read(self):
        self.assertEqual(spec().risk.value, "sensitive_read")


if __name__ == "__main__":
    unittest.main()
