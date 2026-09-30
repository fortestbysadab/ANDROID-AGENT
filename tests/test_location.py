"""Tests for get_location.

Every case here is a failure mode reported against termux-location upstream:
exit 0 with empty output, an API_ERROR object instead of coordinates, a cold
GPS start that takes longer than any sane timeout, and a missing permission.
The old implementation turned all of them into one unhelpful error.
"""

from __future__ import annotations

import json
import unittest
from typing import ClassVar
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


class LocationAccuracyTests(unittest.TestCase):
    """The reported position must be the most accurate one available.

    A cell-tower network fix is routinely 600 m to several km wide. Returning
    whichever provider answered first meant that estimate won every time,
    because GPS is always slower.
    """

    COARSE: ClassVar[dict] = dict(FIX, accuracy=2100.0, provider="network")
    FINE: ClassVar[dict] = dict(FIX, accuracy=12.0, provider="gps")

    def test_gps_beats_a_kilometre_wide_network_fix(self):
        with patched({
            ("network", "once"): (True, json.dumps(self.COARSE)),
            ("gps", "once"): (True, json.dumps(self.FINE)),
        }):
            result = run()
        self.assertEqual(result.data["provider"], "gps")
        self.assertEqual(result.data["accuracy"], 12.0)
        self.assertFalse(result.data["approximate"])

    def test_both_providers_are_asked_at_once(self):
        with patched({
            ("network", "once"): (True, json.dumps(self.COARSE)),
            ("gps", "once"): (True, json.dumps(self.FINE)),
        }) as fake:
            run()
        self.assertEqual({c[0] for c in fake.calls}, {"network", "gps"})

    def test_a_coarse_fix_is_still_returned_when_it_is_all_there_is(self):
        with patched({
            ("network", "once"): (True, json.dumps(self.COARSE)),
            ("gps", "once"): (True, ""),
        }):
            result = run()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["approximate"])

    def test_a_coarse_fix_says_so_in_kilometres(self):
        with patched({
            ("network", "once"): (True, json.dumps(self.COARSE)),
            ("gps", "once"): (True, ""),
        }):
            summary = run().summary
        self.assertIn("2.1 km", summary)
        self.assertIn("coarse", summary.lower())
        self.assertIn("precise", summary)

    def test_a_precise_fix_carries_no_warning(self):
        with patched({("gps", "once"): (True, json.dumps(self.FINE))}):
            summary = run({"provider": "gps"}).summary
        self.assertNotIn("coarse", summary.lower())
        self.assertIn("12 m", summary)

    def test_fix_without_stated_accuracy_counts_as_approximate(self):
        blind = {k: v for k, v in FIX.items() if k != "accuracy"}
        with patched({
            ("network", "once"): (True, json.dumps(blind)),
            ("gps", "once"): (True, ""),
        }):
            result = run()
        self.assertTrue(result.data["approximate"])

    def test_an_explicit_provider_is_not_second_guessed(self):
        with patched({
            ("network", "once"): (True, json.dumps(self.COARSE)),
            ("gps", "once"): (True, json.dumps(self.FINE)),
        }) as fake:
            result = run({"provider": "network"})
        self.assertEqual({c[0] for c in fake.calls}, {"network"})
        self.assertEqual(result.data["provider"], "network")

    def test_precision_levels_set_different_budgets(self):
        fast = termux_extra._LOCATION_PRECISION["fast"]
        balanced = termux_extra._LOCATION_PRECISION["balanced"]
        precise = termux_extra._LOCATION_PRECISION["precise"]
        self.assertLess(fast[1], balanced[1])
        self.assertLess(balanced[1], precise[1])
        # Tighter accuracy target the longer we are willing to wait.
        self.assertGreater(fast[0], balanced[0])
        self.assertGreater(balanced[0], precise[0])

    def test_the_slowest_precision_still_fits_the_tool_timeout(self):
        slowest = max(deadline for _, deadline in termux_extra._LOCATION_PRECISION.values())
        self.assertGreater(spec().timeout_seconds, slowest)

    def test_unknown_precision_falls_back_to_balanced(self):
        with patched({("network", "once"): (True, json.dumps(self.FINE))}):
            result = run({"precision": "nonsense"})
        self.assertEqual(result.status, "ok")


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
